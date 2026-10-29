#!/usr/bin/env python3
"""Measure the host-level loop's summed thread run-delay over the injection window

Usage: level0.py snap <stages.pid> [--proc DIR]
       level0.py window <before> <after>
       level0.py --selftest
  snap     one run-delay sample
  window   run delay between two samples"""

import sys
import time
from pathlib import Path

COMM = "pipeline"


def snap(pidfile, proc="/proc"):
    """sum the run_delay of every thread of the recorded pipeline pids now"""
    proc = Path(proc)
    try:
        pids = [p for p in Path(pidfile).read_text().split() if p.isdigit()]
    except OSError:
        pids = []
    total, threads, ours, foreign = 0, 0, [], 0
    for pid in pids:
        try:
            comm = (proc / pid / "comm").read_text().strip()
        except OSError:
            comm = ""
        if comm != COMM:
            foreign += 1
            continue
        ours.append(pid)
        for st in sorted((proc / pid / "task").glob("*/schedstat")):
            try:
                f = st.read_text().split()
                total += int(f[1])
                threads += 1
            except (OSError, IndexError, ValueError):
                continue
    return {"t_ns": time.monotonic_ns(), "rundelay_ns": total, "threads": threads,
            "pids": ",".join(ours) or "-", "foreign": foreign}


def fmt(d):
    """format a snapshot as a key=value line"""
    return " ".join(f"{k}={d[k]}" for k in ("t_ns", "rundelay_ns", "threads", "pids", "foreign"))


def parse(text):
    """parse key=value tokens into a dict"""
    out = {}
    for tok in text.split():
        k, sep, v = tok.partition("=")
        if sep:
            out[k] = v
    return out


def window(before, after):
    """return the l0.meta lines for the window between two snapshots"""
    lines = []
    why = ""
    try:
        t0, t1 = int(before["t_ns"]), int(after["t_ns"])
        r0, r1 = int(before["rundelay_ns"]), int(after["rundelay_ns"])
    except (KeyError, ValueError):
        return ["loop_rundelay_frac=-", "loop_rundelay_why=unreadable_snapshot"]
    pb, pa = before.get("pids", "-"), after.get("pids", "-")
    if pb == "-":
        why = "no_stage_pid_is_the_pipeline"
    elif pb != pa:
        why = "stage_pids_differ_between_snapshots"
    elif t1 <= t0:
        why = "window_not_positive"
    elif r1 < r0:
        why = "run_delay_went_backwards"
    lines += [f"loop_rundelay_window_ns={max(r1 - r0, 0)}", f"loop_wall_ns={max(t1 - t0, 0)}",
              f"loop_threads={after.get('threads', '-')}", f"loop_pids={pa}",
              f"loop_foreign_pids={after.get('foreign', '-')}"]
    if why:
        lines += ["loop_rundelay_frac=-", f"loop_rundelay_why={why}"]
    else:
        lines += [f"loop_rundelay_frac={(r1 - r0) / (t1 - t0):.6f}"]
    return lines


def selftest():
    """check snap and window on a constructed /proc"""
    import tempfile
    cases = []
    with tempfile.TemporaryDirectory() as d:
        proc = Path(d) / "proc"

        def task(pid, comm, delays):
            """write a fake /proc entry with a schedstat per thread"""
            (proc / pid).mkdir(parents=True, exist_ok=True)
            (proc / pid / "comm").write_text(comm + "\n")
            for i, rd in enumerate(delays):
                t = proc / pid / "task" / f"{pid}{i}"
                t.mkdir(parents=True, exist_ok=True)
                (t / "schedstat").write_text(f"123 {rd} 7\n")

        task("10", "pipeline", [100, 50])
        task("11", "pipeline", [5])
        task("12", "bash", [999999])
        pf = Path(d) / "stages.pid"
        pf.write_text("10 11 12\n")
        s = snap(pf, proc)
        cases.append(("sums run_delay over every thread of the pipeline pids",
                      (s["rundelay_ns"], s["threads"], s["pids"], s["foreign"]) == (155, 3, "10,11", 1)))
        cases.append(("a snapshot round-trips through its one line", parse(fmt(s))["rundelay_ns"] == "155"))
        a = dict(s, t_ns=s["t_ns"] + 1000, rundelay_ns=655)
        w = parse(" ".join(window(parse(fmt(s)), parse(fmt(a)))))
        cases.append(("the window is the difference over the wall time",
                      (w.get("loop_rundelay_window_ns"), w.get("loop_rundelay_frac")) == ("500", "0.500000")))
        gone = dict(a, pids="10")
        w = parse(" ".join(window(parse(fmt(s)), parse(fmt(gone)))))
        cases.append(("a stage that vanished mid-window reads '-'",
                      w.get("loop_rundelay_frac") == "-" and "differ" in w.get("loop_rundelay_why", "")))
        back = dict(a, rundelay_ns=10)
        cases.append(("a sum that goes backwards reads '-'",
                      parse(" ".join(window(parse(fmt(s)), parse(fmt(back))))).get("loop_rundelay_frac") == "-"))
        pf.write_text("12\n")
        none = snap(pf, proc)
        cases.append(("no pipeline pid at all reads '-'",
                      parse(" ".join(window(parse(fmt(none)), parse(fmt(dict(none, t_ns=none["t_ns"] + 5)))))
                            ).get("loop_rundelay_frac") == "-"))
        cases.append(("a missing pid file is an empty snapshot",
                      snap(Path(d) / "absent", proc)["pids"] == "-"))
        cases.append(("an unreadable snapshot reads '-'", window({}, {})[0] == "loop_rundelay_frac=-"))
    bad = [c for c, ok in cases if not ok]
    for c, ok in cases:
        print(("ok   " if ok else "FAIL ") + c)
    print(f"level0 selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    """run the snap, window or selftest subcommand"""
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) in (2, 4) and argv[0] == "snap":
        proc = "/proc"
        if len(argv) == 4:
            if argv[2] != "--proc":
                print(__doc__, file=sys.stderr)
                return 2
            proc = argv[3]
        print(fmt(snap(argv[1], proc)))
        return 0
    if len(argv) == 3 and argv[0] == "window":
        try:
            b, a = parse(Path(argv[1]).read_text()), parse(Path(argv[2]).read_text())
        except OSError:
            b, a = {}, {}
        print("\n".join(window(b, a)))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
