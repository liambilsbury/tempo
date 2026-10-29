#!/usr/bin/env python3
"""Run a configuration of the pipeline and append its row to the fact table"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmark import lib  # noqa: E402

HDR = struct.Struct("<7Q4I")
STAGES = ("telemetry", "actuator", "compute", "sensor")


def read_records(path):
    """read the per-frame records of a telemetry file"""
    raw = Path(path).read_bytes()
    if len(raw) % HDR.size:
        raise SystemExit(f"{path}: {len(raw)} bytes is not a whole number of "
                         f"{HDR.size}-byte records")
    return [HDR.unpack_from(raw, i) for i in range(0, len(raw), HDR.size)]


def main():
    """run the stages and append the row"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-id", required=True)
    ap.add_argument("--factor", default="none", help="which axis this row varies")
    ap.add_argument("--factor-value", default="none")
    ap.add_argument("--frames", type=int, default=60000)
    ap.add_argument("--period-ns", type=int, default=1000000)
    ap.add_argument("--slopes", type=int, default=1600)
    ap.add_argument("--acts", type=int, default=400)
    ap.add_argument("--sched", default="other", choices=("other", "fifo", "deadline"))
    ap.add_argument("--prio", type=int, default=80)
    ap.add_argument("--cpus", default="",
                    help="one per stage: sensor,compute,actuator,telemetry")
    ap.add_argument("--neighbour", default="none",
                    help="the contending load, recorded only")
    ap.add_argument("--deadline-ns", type=int, default=0, help="0 means the loop period")
    ap.add_argument("--table", default="results.tsv")
    ap.add_argument("--keep-telemetry", default="",
                    help="copy the raw per-frame records here")
    ap.add_argument("--allow-unmeasurable-host", action="store_true",
                    help="write the row anyway, as RED")
    args = ap.parse_args()

    if not lib.BIN.exists():
        raise SystemExit(f"{lib.BIN} not built -- run make")

    facts = lib.machine_config()
    meas = lib.host_measurable()
    topo = lib.host_topology()
    table = lib.table_for(topo["topology"], args.table)
    pre = lib.check_exclusive()
    deadline = args.deadline_ns or args.period_ns

    if not meas["measurable"] and not args.allow_unmeasurable_host:
        print(f"run: this host cannot produce a real-time number ({meas['why']})", file=sys.stderr)
        return 3

    cpus = [c.strip() for c in args.cpus.split(",") if c.strip()] if args.cpus else []
    per_stage = dict(zip(("sensor", "compute", "actuator", "telemetry"), cpus))

    run = lib.run_id()
    tag = f"r{os.getpid()}"
    shm = Path(os.environ.get("MILK_SHM_DIR", "/tmp/tempo-shm"))
    shm.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, MILK_SHM_DIR=str(shm))

    with tempfile.TemporaryDirectory(prefix="tempo-") as tmp:
        telemetry = Path(tmp) / "telemetry.bin"
        procs, result = [], 0
        try:
            for st in STAGES:
                argv = [str(lib.BIN), "--stage", st, "--tag", tag,
                        "--frames", str(args.frames), "--period-ns", str(args.period_ns),
                        "--slopes", str(args.slopes), "--acts", str(args.acts),
                        "--sched", args.sched, "--prio", str(args.prio)]
                if st in per_stage:
                    argv += ["--cpu", per_stage[st]]
                if st == "telemetry":
                    argv += ["--out", str(telemetry)]
                procs.append(subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True))
                if st != "sensor":
                    pass
            budget = args.frames * args.period_ns / 1e9 + 120
            for p in procs:
                try:
                    p.wait(timeout=budget)
                except subprocess.TimeoutExpired:
                    result = "timeout"
                    break
            if result != "timeout":
                result = max(p.returncode for p in procs)
        finally:
            lib.stop_recorded(procs, tag)
            for f in shm.glob(f"tempo_{tag}_*"):
                f.unlink(missing_ok=True)

        records = read_records(telemetry) if telemetry.exists() else []
        if args.keep_telemetry and telemetry.exists():
            Path(args.keep_telemetry).write_bytes(telemetry.read_bytes())

    lat = [r[4] - r[1] for r in records if r[4] and r[1]]
    wake = [r[2] - r[1] for r in records if r[2] and r[1]]
    comp = [r[3] - r[2] for r in records if r[3] and r[2]]
    post = [r[4] - r[3] for r in records if r[4] and r[3]]
    over = sum(1 for v in lat if v > deadline)

    row = lib.blank_row()
    row.update(facts)
    row.update(lib.percentiles(lat, "lat"))
    row.update(lib.jitter(lat))
    for name, vals in (("wake", wake), ("compute", comp), ("post", post)):
        p = lib.percentiles(vals, name)
        for k in ("p50_ns", "p99_ns", "max_ns"):
            row[f"{name}_{k}"] = p[f"{name}_{k}"]
    row.update({
        "run_id": run, "repo_sha": lib.sha(), "isio_sha": lib.imagestreamio_sha(),
        "config_id": args.config_id, "factor": args.factor, "factor_value": args.factor_value,
        "host_measurable": meas["measurable"],
        "policy": args.sched, "prio": args.prio,
        "period_ns": args.period_ns, "deadline_ns": deadline,
        "slopes": args.slopes, "acts": args.acts, "neighbour": args.neighbour,
        "cpu_sensor": per_stage.get("sensor", "-"),
        "cpu_compute": per_stage.get("compute", "-"),
        "cpu_actuator": per_stage.get("actuator", "-"),
        "cpu_telemetry": per_stage.get("telemetry", "-"),
        "frames_requested": args.frames, "frames_done": len(records), "records": len(records),
        "over_deadline": over,
        "over_deadline_frac": f"{over / len(lat):.3e}" if lat else "-",
        "notes": meas["why"] if not meas["measurable"] else pre["why"],
        "topology": topo["topology"],
    })

    verdict, reason = lib.verdict_for(result, len(records), args.frames,
                                      pre["exclusive"], meas["measurable"])
    row["verdict"], row["verdict_reason"] = verdict, reason
    row["host_exclusive"] = pre["exclusive"]

    rows, _ = lib.keep_only_if_still_idle([row], bool(pre["exclusive"]))
    out = lib.append_rows(rows, table)
    print(f"{run} {args.config_id}: {rows[0]['verdict']} ({rows[0]['verdict_reason']}) "
          f"n={len(records)} p50={row['lat_p50_ns']} p99.9={row['lat_p999_ns']} "
          f"max={row['lat_max_ns']} over={over} -> {out}")
    return 0 if rows[0]["verdict"] == "GREEN" else 1


if __name__ == "__main__":
    sys.exit(main())
