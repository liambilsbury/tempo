import io
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "benchmark" / "probes" / "ipi_origin.bt"
PER_EVENT_USERSPACE = ("ksym", "usym", "kstack", "ustack", "system", "cat")
_HELPER = re.compile(r"\b(" + "|".join(PER_EVENT_USERSPACE) + r")\b")


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)


def probe_blocks(text):
    text = _strip_comments(text)
    out, i, n = [], 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j < 0:
            break
        header = text[i:j].strip().splitlines()[-1].strip() if text[i:j].strip() else ""
        depth, k = 0, j
        while k < n:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append((header, text[j + 1:k]))
        i = k + 1
    return out


def lint(text):
    bad = []
    for header, body in probe_blocks(text):
        if header in ("BEGIN", "END") or header.startswith("interval:"):
            continue
        for m in _HELPER.finditer(body):
            bad.append((header, m.group(1)))
    return bad


def coverage(events_text, end_ns):
    last = None
    for line in events_text.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0] == "EV":
            try:
                t = int(p[1])
            except ValueError:
                continue
            last = t if last is None else max(last, t)
    if not end_ns:
        return last, None
    return last, (last is not None and last >= end_ns)


def _covers_and_lost(cov, log_text):
    lost = sum(int(m) for m in re.findall(r"Lost (\d+) events", log_text))
    covers = 0 if lost > 0 else (None if cov is None else int(cov))
    return covers, lost


def coverage_report(events_text, end_ns, log_text=""):
    _, cov = coverage(events_text, end_ns)
    covers, lost = _covers_and_lost(cov, log_text)
    return {"probe_covers_inject": covers, "probe_lost": lost}


def _inject_end(meta_text):
    for line in meta_text.splitlines():
        if line.startswith("INJECT end_ns="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return 0
    return 0


def selftest():
    cases = []
    real = lint(PROBE.read_text())
    cases.append(("lint(PROBE): []"
                  + (f" (found {real})" if real else ""), not real))
    hot = ("tracepoint:ipi:ipi_send_cpu\n{\n    printf(\"%s\\n\", ksym(args.callsite));\n}\n")
    cases.append(("lint tracepoint ksym: flagged", lint(hot) == [("tracepoint:ipi:ipi_send_cpu", "ksym")]))
    stack = "kprobe:foo { @[kstack] = count(); }\n"
    cases.append(("lint kprobe kstack: flagged", lint(stack) == [("kprobe:foo", "kstack")]))
    cold = ("BEGIN { printf(\"ksym(x)\\n\"); }\nEND\n{\n    print(ksym(0));\n}\n"
            "/* ksym(args.callsite) in a comment */\ntracepoint:tlb:tlb_flush { printf(\"%d\\n\", args.reason); }\n")
    cases.append(("lint BEGIN/END/comment/string: []", lint(cold) == []))
    ev = "# header\nEV 100 tlb 3 -1 reason=0\nEV 250 ipi 4 8 0x1|0x2\n"
    cases.append(("coverage(ev, 200): (250, True)", coverage(ev, 200) == (250, True)))
    cases.append(("coverage(ev, 300): (250, False)", coverage(ev, 300) == (250, False)))
    cases.append(("coverage(no events): (None, False)", coverage("# header\n", 300) == (None, False)))
    cases.append(("coverage(ev, 0): (250, None)", coverage(ev, 0) == (250, None)))
    cases.append(("_inject_end: end_ns=900", _inject_end("INJECT start_ns=5\nINJECT end_ns=900\n") == 900))
    ev = "EV 100 x\nEV 900 y\n"
    cases.append(("Lost 12+3: covers=0 lost=15",
                  coverage_report(ev, 800, "Lost 12 events\nLost 3 events\n")
                  == {"probe_covers_inject": 0, "probe_lost": 15}))
    cases.append(("no Lost line: covers=1 lost=0",
                  coverage_report(ev, 800, "Attaching 4 probes...\n")
                  == {"probe_covers_inject": 1, "probe_lost": 0}))

    scratch_dir = tempfile.TemporaryDirectory(prefix="probe_check-")
    scratch = Path(scratch_dir.name)
    ev_path = scratch / "t3r1_events.txt"
    meta_path = scratch / "t3r1_inject.meta"
    log_path = scratch / "t3r1_probe.log"
    missing_log_path = scratch / "t3r1_probe_missing.log"
    ev_path.write_text(ev)
    meta_path.write_text("INJECT start_ns=5\nINJECT end_ns=800\n")
    log_path.write_text("Lost 5 events\n")
    missing_log_path.unlink(missing_ok=True)

    def _run_main(args):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            rc = main(args)
            return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    rc, out, err = _run_main(["--coverage", str(ev_path), str(meta_path), str(log_path)])
    cases.append(("--coverage Lost 5: probe_lost=5",
                  "probe_lost=5" in out.splitlines() and "probe_covers_inject=0" in out.splitlines()))
    rc, out, err = _run_main(["--coverage", str(ev_path), str(meta_path), str(missing_log_path)])
    cases.append(("--coverage log missing: probe_lost=-",
                  "probe_lost=-" in out.splitlines() and "probe_covers_inject=0" in out.splitlines()
                  and f"no {missing_log_path}" in err))

    scratch_dir.cleanup()
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(("ok   " if ok else "FAIL ") + n)
    print(f"probe_check selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    if len(argv) == 2 and argv[0] == "--lint":
        bad = lint(Path(argv[1]).read_text())
        for h, f in bad:
            print(f"{argv[1]}: {h} {f}()")
        return 1 if bad else 0
    if len(argv) in (3, 4) and argv[0] == "--coverage":
        ev = Path(argv[1]).read_text(errors="replace") if Path(argv[1]).exists() else ""
        end = _inject_end(Path(argv[2]).read_text()) if Path(argv[2]).exists() else 0
        last, cov = coverage(ev, end)
        print(f"probe_last_ns={last if last is not None else '-'}")
        if len(argv) == 4:
            try:
                log = Path(argv[3]).read_text(errors="replace")
            except OSError:
                print("probe_covers_inject=0")
                print("probe_lost=-")
                print(f"probe_check: no {argv[3]}",
                      file=sys.stderr)
                return 1
            covers, lost = _covers_and_lost(cov, log)
            print(f"probe_covers_inject={'-' if covers is None else covers}")
            print(f"probe_lost={lost}")
        else:
            print(f"probe_covers_inject={'-' if cov is None else int(cov)}")
        return 0
    print("usage: benchmark/probes/check.py --selftest | --lint FILE | --coverage EVENTS INJECT_META [PROBE_LOG]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
