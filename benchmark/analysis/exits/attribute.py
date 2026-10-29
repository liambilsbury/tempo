#!/usr/bin/env python3
"""Attribute each late frame to the L1 events inside its window"""

import argparse
import struct
import sys
from collections import Counter
from pathlib import Path

HDR = struct.Struct("<7Q4I")
FIELDS = ("seq", "t_sensor", "t_compute_in", "t_compute_out", "t_actuator",
          "t_sensor_join", "t_actuator_join", "cpu_sensor", "cpu_compute", "cpu_actuator",
          "pad")


def read_frames(path):
    """read the telemetry records as dicts"""
    raw = Path(path).read_bytes()
    if len(raw) % HDR.size:
        raise SystemExit(f"{path}: {len(raw)} bytes is not a whole number of "
                         f"{HDR.size}-byte records")
    return [dict(zip(FIELDS, HDR.unpack_from(raw, i)))
            for i in range(0, len(raw), HDR.size)]


def read_events(path):
    """parse the EV lines sorted by time"""
    out = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.startswith("EV "):
            continue
        p = line.split(None, 5)
        if len(p) < 6:
            continue
        try:
            out.append({"t": int(p[1]), "kind": p[2], "origin": int(p[3]),
                        "target": int(p[4]), "detail": p[5]})
        except ValueError:
            continue
    out.sort(key=lambda e: e["t"])
    return out


def in_window(events, lo, hi):
    """return the sorted events between lo and hi"""
    import bisect
    ts = [e["t"] for e in events]
    return events[bisect.bisect_left(ts, lo):bisect.bisect_right(ts, hi)]


def main():
    """print the attribution of the late frames and the FACT line"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--telemetry", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--deadline-ns", type=int, required=True)
    ap.add_argument("--loop-cpus", default="",
                    help="comma list of the loop's CPUs")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    frames = read_frames(args.telemetry)
    events = read_events(args.events)
    loop = {int(c) for c in args.loop_cpus.split(",") if c.strip()} if args.loop_cpus else set()

    if not frames:
        raise SystemExit("no frames")
    if not events:
        print("no events: the probe produced nothing; every late frame is unattributed", file=sys.stderr)

    f_lo = min(f["t_sensor_join"] for f in frames)
    f_hi = max(f["t_actuator_join"] for f in frames)
    if events:
        e_lo, e_hi = events[0]["t"], events[-1]["t"]
        overlap = min(f_hi, e_hi) - max(f_lo, e_lo)
        if overlap <= 0:
            raise SystemExit(
                f"frames span [{f_lo},{f_hi}] and events span [{e_lo},{e_hi}]; they do not overlap")
        print(f"join: frames and events overlap for {overlap/1e9:.2f} s")

    ev_sorted = events
    late = []
    for f in frames:
        lat = f["t_actuator"] - f["t_sensor"]
        if lat > args.deadline_ns:
            late.append((f, lat))

    classes = Counter()
    culprits = Counter()
    origins = Counter()
    unattributed = 0

    for f, lat in late:
        hits = in_window(ev_sorted, f["t_sensor_join"], f["t_actuator_join"])
        if loop:
            hits = [e for e in hits if e["target"] in loop or e["target"] == -1]
        if not hits:
            unattributed += 1
            continue
        for e in hits:
            classes[e["kind"]] += 1
            culprits[e["detail"]] += 1
            if e["kind"].startswith("ipi"):
                origins[e["origin"]] += 1

    n = len(frames)
    print(f"\nframes            {n}")
    print(f"deadline          {args.deadline_ns} ns")
    print(f"late              {len(late)}  ({len(late)/n:.3e})")
    print(f"unattributed      {unattributed}"
          f"  ({unattributed/len(late):.3f} of late)" if late else "unattributed      -")

    if classes:
        print("\nevents inside late-frame windows, by class")
        for k, v in classes.most_common():
            print(f"  {k:10} {v}")
        print(f"\ntop {args.top} callsites")
        for k, v in culprits.most_common(args.top):
            print(f"  {v:8}  {k}")
        print("\nremote origin cores")
        for k, v in origins.most_common(args.top):
            print(f"  cpu {k:<4} {v}")

    print("\nFACT att_ipi={} att_tlb={} att_unattributed={} late={} frames={}".format(
        classes.get("ipi", 0) + classes.get("ipi_mask", 0),
        classes.get("tlb", 0), unattributed, len(late), n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
