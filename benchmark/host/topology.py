#!/usr/bin/env python3
import argparse
import contextlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmark import lib  # noqa: E402

HDR = struct.Struct("<7Q4I")

NESTED_CPUINFO = "processor\t: 0\nflags\t\t: fpu vme sse2 hypervisor vmx\n"
BARE_CPUINFO = "processor\t: 0\nflags\t\t: fpu vme sse2 svm\n"


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def fixed(topology):
    cpu, dv = {lib.TOPOLOGY_NESTED: (NESTED_CPUINFO, "kvm"),
               lib.TOPOLOGY_SINGLE: (BARE_CPUINFO, "none"),
               lib.TOPOLOGY_UNKNOWN: (BARE_CPUINFO, "kvm")}[topology]
    real = lib.host_topology
    return lambda cpuinfo=None, detect_virt=None: real(cpu, dv)


def refused(fn, *a, **kw):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fn(*a, **kw)
    except SystemExit as e:
        return e.code not in (0, None)
    except Exception:
        return False
    return False


def tsv_rows(path):
    if not Path(path).exists():
        return []
    lines = Path(path).read_text().splitlines()
    head = lines[0].split("\t")
    return [dict(zip(head, l.split("\t"))) for l in lines[1:]]


def experiment_dir(exp, name, topology=None, n=4):
    d = exp / name
    (d / "l2").mkdir(parents=True)
    meta = "load1=0.1 free_gb=100 foreign_pipelines=0 exclusive=1\nwhy=idle\n"
    if topology:
        meta += f"topology={topology} hypervisor_flag=0 detect_virt=none\n"
    (d / "host.meta").write_text(meta + "exclusive_end=1\n")
    (d / "inject.meta").write_text("INJECT mode=none watched_cpu_window_ns=1 cpu_consumed_ns=0\n")
    (d / "l2" / "header.txt").write_text(
        f"frames={n} period_ns=1000000 kernel=x clocksource=tsc sched=fifo\n")
    t0 = 1_000_000_000
    (d / "l2" / "telemetry.bin").write_bytes(b"".join(
        HDR.pack(i, t0 + i * 1_000_000, t0 + i * 1_000_000 + 10, t0 + i * 1_000_000 + 20,
                 t0 + i * 1_000_000 + 30, 0, 0, 0, 0, 0, 0) for i in range(n)))
    return d


def selftest():
    tmp = Path(tempfile.mkdtemp(prefix="tempo-topo-"))
    os.environ.setdefault("MILK_SHM_DIR", str(tmp / "shm"))
    try:
        return _selftest(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest(tmp):
    cases = []

    for cpu, dv, want, name in [
            (NESTED_CPUINFO, "kvm", "nested", "flag=1 virt=kvm: nested"),
            (NESTED_CPUINFO, "absent", "nested", "flag=1 virt=absent: nested"),
            (BARE_CPUINFO, "none", "single-level", "flag=0 virt=none: single-level"),
            (BARE_CPUINFO, "absent", "single-level", "flag=0 virt=absent: single-level"),
            (NESTED_CPUINFO, "none", "unknown", "flag=1 virt=none: unknown"),
            (BARE_CPUINFO, "kvm", "unknown", "flag=0 virt=kvm: unknown")]:
        cases.append((name, lib.host_topology(cpu, dv)["topology"] == want))
    cases.append(("'hypervisor' in model name: single-level",
                  lib.host_topology("model name\t: hypervisor-ready cpu\n" + BARE_CPUINFO,
                                    "none")["topology"] == "single-level"))

    cases.append(("table_for(nested): PUBLISHED_TABLE",
                  lib.table_for("nested") == lib.PUBLISHED_TABLE))
    cases.append(("table_for(single-level): single-level.tsv",
                  lib.table_for("single-level") == "single-level.tsv"))
    cases.append(("table_for(unknown): unknown.tsv",
                  lib.table_for("unknown") == "unknown.tsv"))
    cases.append(("table_for(-): refused", refused(lib.table_for, "-")))

    res = tmp / "append"
    with patched(lib, RESULTS=res):
        row = lib.blank_row()
        cases.append(("append results.tsv topology=single-level: refused", refused(
            lib.append_rows, [dict(row, topology="single-level")], lib.PUBLISHED_TABLE)))
        cases.append(("append results.tsv topology=unknown: refused", refused(
            lib.append_rows, [dict(row, topology="unknown")], lib.PUBLISHED_TABLE)))
        cases.append(("append results.tsv no topology: refused", refused(
            lib.append_rows, [dict(row)], lib.PUBLISHED_TABLE)))
        ok = not refused(lib.append_rows, [dict(row, topology="nested")], lib.PUBLISHED_TABLE)
        cases.append(("append results.tsv topology=nested: 1 row", ok and len(
            tsv_rows(res / lib.PUBLISHED_TABLE)) == 1))

    res = tmp / "run"
    from benchmark.run import configuration as run
    measurable = lambda: {"measurable": 1, "why": "ok"}           # noqa: E731
    idle = lambda idle_required=True: {"load1": 0.0, "free_gb": 100, "foreign_pipelines": 0,  # noqa: E731,E501
                                         "exclusive": 1, "why": "idle"}
    with patched(lib, RESULTS=res, host_measurable=measurable, check_exclusive=idle,
                 host_topology=fixed("single-level")), \
            patched(sys, argv=["benchmark/run/configuration.py", "--config-id", "topo-probe", "--frames", "10",
                               "--slopes", "200", "--acts", "50"]):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                run.main()
            except SystemExit:
                pass
    sl = tsv_rows(res / "single-level.tsv")
    cases.append(("configuration.py host=single-level: single-level.tsv only",
                  not (res / lib.PUBLISHED_TABLE).exists() and len(sl) == 1
                  and sl[0]["topology"] == "single-level"))

    res = tmp / "ingest"
    exp = res / "experiments"
    experiment_dir(exp, "sl-r0", "single-level")
    experiment_dir(exp, "ne-r0", "nested")
    experiment_dir(exp, "old-r0", None)
    from benchmark.run import ingest
    with patched(lib, RESULTS=res, host_topology=fixed("single-level")), \
            patched(ingest, EXPERIMENTS=exp), patched(sys, argv=["ingest.py"]):
        with contextlib.redirect_stdout(io.StringIO()):
            ingest.main()
            ingest.main()
    pub = [r["run_id"] for r in tsv_rows(res / lib.PUBLISHED_TABLE)]
    sep = [r["run_id"] for r in tsv_rows(res / "single-level.tsv")]
    cases.append(("ingest nested: results.tsv == [ne-r0]", pub == ["ne-r0"]))
    cases.append(("ingest single-level: sl-r0 in single-level.tsv", "sl-r0" in sep))
    cases.append(("ingest no topology field: host topology",
                  "old-r0" in sep and "old-r0" not in pub))
    cases.append(("ingest x2: single-level.tsv rows=2", len(sep) == 2))

    try:
        from tools.results import verify_claims
    except ImportError:
        verify_claims = None
    res = tmp / "readers"
    res.mkdir()
    legacy = [c for c in lib.COLUMNS if c != "topology"]
    (res / lib.PUBLISHED_TABLE).write_text(
        "\t".join(legacy) + "\n" + "\t".join("-" for _ in legacy) + "\n")
    (res / "paired-old.json").write_text(json.dumps({"metric": "p999_ns", "rounds": []}))
    cases.append(("legacy no topology column: unpublishable=[]",
                  lib.unpublishable(res) == []))
    with patched(lib, RESULTS=res):
        cases.append(("legacy results/: refuse_unpublishable passes",
                      not refused(lib.refuse_unpublishable)))
        cols = legacy + ["topology"]
        (res / lib.PUBLISHED_TABLE).write_text(
            "\t".join(cols) + "\n" + "\t".join("-" for _ in cols) + "\n"
            + "\t".join("sl" if c == "run_id" else "single-level" if c == "topology" else "-"
                        for c in cols) + "\n")
        cases.append(("results.tsv single-level row: refused",
                      refused(lib.refuse_unpublishable)))
        (res / lib.PUBLISHED_TABLE).write_text(
            "\t".join(legacy) + "\n" + "\t".join("-" for _ in legacy) + "\n")
        sl_json = res / "paired-pc.json"
        sl_json.write_text(json.dumps({"metric": "p999_ns", "topology": "single-level",
                                       "rounds": []}))
        cases.append(("results/paired-pc.json single-level: refused",
                      refused(lib.refuse_unpublishable)))
        if verify_claims:
            cases.append(("verify_claims.py single-level paired: refused",
                          refused(verify_claims.main)))
        from benchmark.score.nested import budget as preregistration_budget
        cases.append(("budget.write_markdown single-level: refused",
                      refused(preregistration_budget.write_markdown, str(res / "no-doc.md"),
                              paired_path=str(sl_json))))
        sl_json.unlink()

    from benchmark.run import paired
    res = tmp / "paired"
    launched = []
    FACT_STUB = {"hit": 1, "late": 0, "p999_ns": 100, "max_ns": 100, "steal_ticks": 0}
    stub = lambda *a, **k: launched.append(a) or dict(FACT_STUB)  # noqa: E731
    with patched(lib, host_topology=fixed("single-level")), \
            patched(paired, ROOT=res, run_one=stub):
        with patched(sys, argv=["paired.py", "--condition", "a:none", "--repeats", "1",
                                "--out", "results/earlier_phase/paired/paired-fixed.json"]):
            cases.append(("paired.py single-level --out results/earlier_phase: refused",
                          refused(paired.main) and not launched))
        with patched(sys, argv=["paired.py", "--condition", "a:none", "--repeats", "1"]):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                paired.main()
    out = res / "results" / "experiments" / "paired-latest.json"
    cases.append(("paired.py single-level: results/experiments/paired-latest.json, topology=single-level",
                  out.exists() and json.loads(out.read_text()).get("topology") == "single-level"
                  and not (res / "results" / "paired-latest.json").exists()))

    res = tmp / "freeze"
    exp = res / "experiments"
    experiment_dir(exp, "control-r0", "nested")
    with patched(lib, RESULTS=res):
        cases.append(("freeze nested corpus: ok",
                      not refused(lib.refuse_unpublishable_experiments)))
        experiment_dir(exp, "hogsync-r0", "single-level")
        from benchmark.analysis.recall import rows as recall_rows
        with patched(recall_rows, FROZEN=res / "recall-frozen.json"):
            cases.append(("recall_rows.snapshot with single-level run: refused",
                          refused(recall_rows.snapshot)
                          and not (res / "recall-frozen.json").exists()))

    live = lib.unpublishable()
    cases.append(("results/: unpublishable=[]" +
                  (f": {'; '.join(live[:3])}" if live else ""), not live))

    bad = sum(1 for _, good in cases if not good)
    for name, good in cases:
        print(f"  {'ok  ' if good else 'FAIL'} {name}")
    print(f"topology selftest: {len(cases) - bad}/{len(cases)}")
    return 0 if bad == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    t = lib.host_topology()
    print(f"topology={t['topology']} hypervisor_flag={t['hypervisor_flag']} "
          f"detect_virt={t['detect_virt']}")
    print(f"table={lib.table_for(t['topology'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
