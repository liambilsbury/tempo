#!/usr/bin/env python3
"""Print the guest's steal and lateness recall of the CPU the cpu-load injector took"""

import argparse
import sys
from pathlib import Path

USER_HZ = 100
TICK_NS = 1_000_000_000 // USER_HZ



CPU_LOAD = ("cpu-load", "hog")

def read_kv(path):
    """parse the key=value tokens of a meta file"""
    out = {}
    if not Path(path).exists():
        return out
    for line in Path(path).read_text(errors="replace").splitlines():
        line = line.replace("INJECT ", "").strip()
        for tok in line.split():
            if "=" in tok:
                k, _, v = tok.partition("=")
                out[k.strip()] = v.strip()
    return out


def one(outdir):
    """print the recall of a run directory"""
    out = Path(outdir)
    meta = read_kv(out / "inject.meta")
    hdr = read_kv(out / "l2" / "header.txt")

    mode = meta.get("mode", "?")
    injected_ns = int(meta.get("cpu_consumed_ns", 0) or 0)

    steal_ticks = int(hdr.get("det_steal_ticks", hdr.get("steal_after", 0) or 0) or 0)
    sum_late = int(hdr.get("det_sum_late_ns", 0) or 0)
    window = int(hdr.get("det_window_ns", 0) or 0)

    print(f"=== {out.name} ===")
    print(f"  class                     {mode}")
    if mode in CPU_LOAD and injected_ns:
        print(f"  CPU the injector took     {injected_ns/1e9:.3f} s")
    print(f"  guest steal ticks         {steal_ticks}  = {steal_ticks*TICK_NS/1e9:.3f} s "
          f"at {USER_HZ} Hz granularity")
    print(f"  guest summed lateness     {sum_late/1e9:.3f} s over a {window/1e9:.1f} s window")

    if mode in CPU_LOAD and injected_ns:
        by_steal = steal_ticks * TICK_NS / injected_ns
        by_late = sum_late / injected_ns
        print(f"  RECALL by steal counter   {by_steal:.3f}")
        print(f"  RECALL by summed lateness {by_late:.3f}")
        if by_late > 0 and by_steal > 0:
            print(f"  the two guest signals disagree by {max(by_late/by_steal, by_steal/by_late):.1f}x")
        print(f"  FACT class={mode} injected_ns={injected_ns} stolen_ns={steal_ticks*TICK_NS} "
              f"late_ns={sum_late} recall_steal={by_steal:.4f} recall_late={by_late:.4f}")
    else:
        print("  no measured CPU denominator for this class")
    return 0


def collect(pattern):
    """return the recall of each cpu-load round matching a glob"""
    rows = []
    for d in sorted(Path("results/experiments").glob(pattern)):
        meta = read_kv(d / "inject.meta")
        hdr = read_kv(d / "l2" / "header.txt")
        inj = int(meta.get("cpu_consumed_ns", 0) or 0)
        if meta.get("mode") not in CPU_LOAD or not inj:
            continue
        steal = int(hdr.get("det_steal_ticks", 0) or 0) * TICK_NS
        late = int(hdr.get("det_sum_late_ns", 0) or 0)
        gcpu = read_kv(d / "l2" / "guest_cpu.txt").get("guest_cpu_ns", 0)
        if not late and not steal:
            continue
        rows.append({"round": d.name, "injected_ns": inj, "stolen_ns": steal, "late_ns": late,
                     "guest_cpu_ns": int(gcpu or 0),
                     "recall_steal": steal / inj, "recall_late": late / inj})
    return rows


def med(xs):
    """return the median of a list, or None when it is empty"""
    s = sorted(xs)
    n = len(s)
    return None if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def paired(pattern):
    """print the recall table across the matching rounds"""
    rows = collect(pattern)
    if not rows:
        print(f"no completed hog rounds matching {pattern}", file=sys.stderr)
        return 1
    print(f"=== recall across {len(rows)} hog rounds ({pattern}) ===\n")
    print(f"{'round':16} {'injected s':>11} {'steal s':>9} {'late s':>9} "
          f"{'R_steal':>8} {'R_late':>8}")
    for r in rows:
        print(f"{r['round']:16} {r['injected_ns']/1e9:11.3f} {r['stolen_ns']/1e9:9.3f} "
              f"{r['late_ns']/1e9:9.3f} {r['recall_steal']:8.3f} {r['recall_late']:8.3f}")
    rs = [r["recall_steal"] for r in rows]
    rl = [r["recall_late"] for r in rows]
    print(f"\nmedian recall by steal counter    {med(rs):.3f}   "
          f"range {min(rs):.3f}-{max(rs):.3f}")
    print(f"median recall by summed lateness  {med(rl):.3f}   "
          f"range {min(rl):.3f}-{max(rl):.3f}")
    return 0


def main():
    """report a run or the matching rounds"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir")
    ap.add_argument("--paired", nargs="?", const="hogsync-r*",
                    help="glob over results/experiments, default hogsync-r*")
    args = ap.parse_args()
    if args.outdir:
        return one(args.outdir)
    if args.paired:
        return paired(args.paired)
    ap.error("give --outdir or --paired")


if __name__ == "__main__":
    sys.exit(main())
