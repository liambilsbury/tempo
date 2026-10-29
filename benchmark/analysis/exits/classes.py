#!/usr/bin/env python3
"""Compare each interference class against control"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, wilcoxon  # noqa: E402


def kv(p):
    """parse the key=value tokens of a meta file"""
    d = {}
    if not Path(p).exists():
        return d
    for line in open(p, errors="replace"):
        for tok in line.replace("INJECT ", "").split():
            if "=" in tok:
                k, _, v = tok.partition("=")
                d[k] = v
    return d


def guest_cpu_delta(label, i):
    """return the guest CPU of control minus the injected run over the window"""
    c = kv(ROOT / "results" / "experiments" / f"control-r{i}" / "inject.meta")
    h = kv(ROOT / "results" / "experiments" / f"{label}-r{i}" / "inject.meta")
    try:
        return int(c["watched_cpu_window_ns"]) - int(h["watched_cpu_window_ns"])
    except (KeyError, ValueError):
        return None


def main():
    """print the per-class comparison table"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--paired", default="results/earlier_phase/paired/paired-classes.json")
    args = ap.parse_args()

    p = ROOT / args.paired
    if not p.exists():
        print(f"{p} does not exist", file=sys.stderr)
        return 1
    rounds = json.loads(p.read_text())["rounds"]
    labels = [k for k in rounds[0] if k != "control"] if rounds else []

    print(f"{len(rounds)} interleaved rounds, each class against the same control\n")
    print(f"{'class':10} {'p99.9 delta':>13} {'wilcoxon':>9} | "
          f"{'guest CPU lost':>15} | {'steal ticks delta':>17} {'wilcoxon':>9}")
    print("-" * 87)

    for label in labels:
        lat = [r[label]["p999_ns"] - r["control"]["p999_ns"]
               for r in rounds if label in r and "control" in r]
        steal = [r[label]["steal_ticks"] - r["control"]["steal_ticks"]
                 for r in rounds if label in r and "control" in r]
        cpu = [d for i in range(len(rounds)) if (d := guest_cpu_delta(label, i)) is not None]

        wl, _ = wilcoxon(lat) if lat else (1.0, 0)
        ws, _ = wilcoxon(steal) if steal else (1.0, 0)
        cpu_s = f"{median(cpu)/1e9:+9.3f} s" if cpu else "        -"
        print(f"{label:10} {median(lat)/1e6:+10.2f} ms {wl:9.4f} | {cpu_s:>15} | "
              f"{median(steal):+17.0f} {ws:9.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
