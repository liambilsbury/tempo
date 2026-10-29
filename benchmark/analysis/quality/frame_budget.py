#!/usr/bin/env python3
"""Print the fraction of frames that shed to the floor block and still missed the deadline"""

import argparse
import struct
import sys
from pathlib import Path

HDR = struct.Struct("<7Q4I")


def read_frames(path):
    """read the telemetry records of a run"""
    raw = Path(path).read_bytes()
    return [HDR.unpack_from(raw, i) for i in range(0, len(raw), HDR.size)]


def read_header(path):
    """parse the key=value lines of a header file"""
    out = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def per_frame(frames):
    """return (modes_done, latency_ns) of every frame with both stamps"""
    return [(f[10], f[4] - f[1]) for f in frames if f[4] and f[1]]


def exceeded_fraction(frames, floor_modes, period_ns):
    """return the fraction of frames at or below floor_modes that missed period_ns"""
    pairs = per_frame(frames)
    if not pairs:
        return None
    exceeded = sum(1 for done, lat in pairs if done <= floor_modes and lat > period_ns)
    return exceeded / len(pairs)


def main():
    """print the exceeded-budget fraction of a run"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--floor-modes", type=int, default=16)
    ap.add_argument("--period-ns", type=int, default=1_000_000)
    args = ap.parse_args()
    h = read_header(Path(args.outdir) / "l2" / "header.txt")
    frames = read_frames(Path(args.outdir) / "l2" / "telemetry.bin")
    frac = exceeded_fraction(frames, args.floor_modes, args.period_ns)
    print(f"block={h.get('block', '-')} recon={h.get('recon', '-')} adapt={h.get('adapt', '-')}")
    print(f"exceeded_budget_frac={frac}")
    return 0 if frac is not None else 1


if __name__ == "__main__":
    sys.exit(main())
