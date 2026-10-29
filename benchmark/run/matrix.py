#!/usr/bin/env python3
"""Plan or run the one-factor configuration matrix rows the current boot supports"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmark import lib  # noqa: E402

RUN = Path(__file__).resolve().parent / "configuration.py"

MATRIX = [
    dict(id="newearthlab-baseline", factor="baseline", value="as-published",
         args=["--sched", "other"], needs={"kernel_preempt": "PREEMPT_RT"},
         note="PREEMPT_RT, stages pinned, NO rt priorities, NO isolation, desktop running"),

    dict(id="sched-other", factor="sched", value="SCHED_OTHER",
         args=["--sched", "other"], needs={}),
    dict(id="sched-fifo", factor="sched", value="SCHED_FIFO",
         args=["--sched", "fifo", "--prio", "80"], needs={}),
    dict(id="sched-deadline", factor="sched", value="SCHED_DEADLINE",
         args=["--sched", "deadline"], needs={}),

    dict(id="affinity-off", factor="affinity", value="none", args=[], needs={}),
    dict(id="affinity-on", factor="affinity", value="pinned", args=[], pin=True, needs={}),

    dict(id="freq-performance", factor="freq", value="performance",
         args=[], needs={"freq_governor": "performance"}),
    dict(id="freq-powersave", factor="freq", value="powersave",
         args=[], needs={"freq_governor": "powersave"}),

    dict(id="kernel-mainline", factor="kernel", value="PREEMPT_DYNAMIC",
         args=[], needs={"kernel_preempt": "PREEMPT_DYNAMIC"}),
    dict(id="kernel-rt", factor="kernel", value="PREEMPT_RT",
         args=[], needs={"kernel_preempt": "PREEMPT_RT"}),
    dict(id="isolation-off", factor="isolation", value="none",
         args=[], needs={"isolcpus": "unset"}),
    dict(id="isolation-isolcpus", factor="isolation", value="isolcpus",
         args=[], needs={"isolcpus": "*"}),
    dict(id="isolation-full", factor="isolation", value="isolcpus+nohz_full+rcu_nocbs",
         args=[], needs={"isolcpus": "*", "nohz_full": "*", "rcu_nocbs": "*"}),
]


def satisfied(need, facts):
    """check a row's boot needs against the machine config"""
    for key, want in need.items():
        have = facts.get(key, "-")
        if want == "*":
            if have in ("unset", "-", ""):
                return False, f"{key} is {have}, needs to be set at boot"
        elif have != want:
            return False, f"{key} is {have}, needs {want}"
    return True, ""


def main():
    """print the plan and run the ready rows"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--frames", type=int, default=60000)
    ap.add_argument("--period-ns", type=int, default=1000000)
    ap.add_argument("--cpus", default="", help="CPUs for pinned rows")
    args = ap.parse_args()
    if not (args.plan or args.run):
        ap.error("choose --plan or --run")

    facts = lib.machine_config()
    meas = lib.host_measurable()

    ready, pending = [], []
    for row in MATRIX:
        ok, why = satisfied(row["needs"], facts)
        (ready if ok else pending).append((row, why))

    print(f"this boot: kernel={facts['kernel_preempt']} clocksource={facts['clocksource']} "
          f"isolcpus={facts['isolcpus']} nohz_full={facts['nohz_full']} "
          f"rcu_nocbs={facts['rcu_nocbs']} governor={facts['freq_governor']}")
    print(f"measurable: {'yes' if meas['measurable'] else 'NO -- ' + meas['why']}\n")

    print(f"{len(ready)} of {len(MATRIX)} rows are runnable on this boot")
    for row, _ in ready:
        print(f"  ready    {row['id']:26} {row['factor']}={row['value']}")
    print()
    print(f"{len(pending)} rows need a different boot")
    for row, why in pending:
        print(f"  pending  {row['id']:26} {why}")

    if args.plan:
        return 0

    if not meas["measurable"] and not args.dry_run:
        print(f"\nmatrix: refusing to run -- {meas['why']}", file=sys.stderr)
        return 3

    print()
    failures = 0
    for row, _ in ready:
        argv = [sys.executable, str(RUN), "--config-id", row["id"],
                "--factor", row["factor"], "--factor-value", row["value"],
                "--frames", str(args.frames), "--period-ns", str(args.period_ns)]
        if row.get("pin") and args.cpus:
            argv += ["--cpus", args.cpus]
        argv += row["args"]
        if args.dry_run:
            print("  " + " ".join(argv))
            continue
        print(f"--- {row['id']}")
        if subprocess.run(argv).returncode != 0:
            failures += 1
    if not args.dry_run:
        print(f"\n{len(ready) - failures}/{len(ready)} rows GREEN")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
