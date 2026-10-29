"""Lint a bpftrace probe and check its event coverage"""
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
    """remove comments and string contents from a probe source"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)


def probe_blocks(text):
    """return (header, body) of each top-level block"""
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
    """return each (probe, helper) where a per-event body calls a userspace helper"""
    bad = []
    for header, body in probe_blocks(text):
        if header in ("BEGIN", "END") or header.startswith("interval:"):
            continue
        for m in _HELPER.finditer(body):
            bad.append((header, m.group(1)))
    return bad


def coverage(events_text, end_ns):
    """return the last event time and whether it reaches end_ns"""
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
    """return the covers flag and the summed Lost N events count"""
    lost = sum(int(m) for m in re.findall(r"Lost (\d+) events", log_text))
    covers = 0 if lost > 0 else (None if cov is None else int(cov))
    return covers, lost


def coverage_report(events_text, end_ns, log_text=""):
    """return probe_covers_inject and probe_lost for an events file and probe log"""
    _, cov = coverage(events_text, end_ns)
    covers, lost = _covers_and_lost(cov, log_text)
    return {"probe_covers_inject": covers, "probe_lost": lost}


def _inject_end(meta_text):
    """parse INJECT end_ns from inject.meta"""
    for line in meta_text.splitlines():
        if line.startswith("INJECT end_ns="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return 0
    return 0


def selftest():
    """check lint, coverage and the --coverage output on constructed input"""
    cases = []
    real = lint(PROBE.read_text())
    cases.append(("the shipped probe resolves nothing in userspace per event"
                  + (f" (found {real})" if real else ""), not real))
    hot = ("tracepoint:ipi:ipi_send_cpu\n{\n    printf(\"%s\\n\", ksym(args.callsite));\n}\n")
    cases.append(("ksym in a tracepoint body is flagged", lint(hot) == [("tracepoint:ipi:ipi_send_cpu", "ksym")]))
    stack = "kprobe:foo { @[kstack] = count(); }\n"
    cases.append(("kstack in a kprobe body is flagged", lint(stack) == [("kprobe:foo", "kstack")]))
    cold = ("BEGIN { printf(\"ksym(x)\\n\"); }\nEND\n{\n    print(ksym(0));\n}\n"
            "/* ksym(args.callsite) in a comment */\ntracepoint:tlb:tlb_flush { printf(\"%d\\n\", args.reason); }\n")
    cases.append(("BEGIN/END, comments and strings are not per-event", lint(cold) == []))
    ev = "# header\nEV 100 tlb 3 -1 reason=0\nEV 250 ipi 4 8 0x1|0x2\n"
    cases.append(("events past the window's end cover it", coverage(ev, 200) == (250, True)))
    cases.append(("events that stop before the end do not", coverage(ev, 300) == (250, False)))
    cases.append(("no events covers nothing", coverage("# header\n", 300) == (None, False)))
    cases.append(("no window reads None, not covered", coverage(ev, 0) == (250, None)))
    cases.append(("inject end parsed from inject.meta", _inject_end("INJECT start_ns=5\nINJECT end_ns=900\n") == 900))
    ev = "EV 100 x\nEV 900 y\n"
    cases.append(("lost events void coverage",
                  coverage_report(ev, 800, "Lost 12 events\nLost 3 events\n")
                  == {"probe_covers_inject": 0, "probe_lost": 15}))
    cases.append(("no lost line reads 0",
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
        """run main and capture its exit code, stdout and stderr"""
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            rc = main(args)
            return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    rc, out, err = _run_main(["--coverage", str(ev_path), str(meta_path), str(log_path)])
    cases.append(("main: a present log's Lost line is reported",
                  "probe_lost=5" in out.splitlines() and "probe_covers_inject=0" in out.splitlines()))
    rc, out, err = _run_main(["--coverage", str(ev_path), str(meta_path), str(missing_log_path)])
    cases.append(("main: a missing log reads unknown loss, not zero",
                  "probe_lost=-" in out.splitlines() and "probe_covers_inject=0" in out.splitlines()
                  and "missing" in err and "loss unknown" in err))

    scratch_dir.cleanup()
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(("ok   " if ok else "FAIL ") + n)
    print(f"probe_check selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    """run the selftest, lint or coverage subcommand"""
    if argv[:1] == ["--selftest"]:
        return selftest()
    if len(argv) == 2 and argv[0] == "--lint":
        bad = lint(Path(argv[1]).read_text())
        for h, f in bad:
            print(f"{argv[1]}: {h} calls {f}() per event")
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
                print(f"probe_check: probe log {argv[3]} missing -- loss unknown, coverage not confirmed",
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
