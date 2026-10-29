#!/usr/bin/env python3
"""Run conditions interleaved in rounds and compare them pairwise"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXP = Path(os.environ.get("TEMPO_EXPERIMENT_SH") or ROOT / "benchmark" / "run" / "experiment.sh")
sys.path.insert(0, str(ROOT))
from benchmark import lib  # noqa: E402
FACT = re.compile(r"FACT (.+)")
INJECT_MODES = ("none", "wake", "tlb", "tlbq", "cache", "cpu-load")


def launch_argv(label, mode, arg, frames, period, pin, slopes=3200, acts=500, inject_s=0, vcpus=0):
    """build the experiment.sh command for a run"""
    argv = ["bash", str(EXP), "--label", label, "--inject", mode,
            "--frames", str(frames), "--period-ns", str(period), "--pin", pin,
            "--slopes", str(slopes), "--acts", str(acts)]
    if inject_s:
        argv += ["--inject-s", str(inject_s)]
    if vcpus:
        argv += ["--vcpus", str(vcpus)]
    if mode == "cpu-load":
        argv += ["--duty", str(arg), "--rt-prio", "90"]
    elif mode != "none":
        argv += ["--rate-hz", str(arg)]
    return argv


def run_one(label, mode, arg, frames, period, pin, slopes=3200, acts=500, env_extra=None,
            inject_s=0, vcpus=0):
    """run an experiment and parse its FACT line"""
    argv = launch_argv(label, mode, arg, frames, period, pin, slopes, acts, inject_s, vcpus)
    env = os.environ.copy()
    env.update(env_extra or {})
    r = subprocess.run(argv, capture_output=True, text=True, timeout=1800, env=env)
    m = FACT.search(r.stdout)
    if not m:
        print(f"  {label}: no FACT line -- run failed", file=sys.stderr)
        print(r.stdout[-500:], file=sys.stderr)
        return None
    out = {}
    for kv in m.group(1).split():
        k, _, v = kv.partition("=")
        if k.endswith(("_sha", "_sha256")):
            out[k] = v
            continue
        for cast in (int, float):
            try:
                out[k] = cast(v)
                break
            except ValueError:
                continue
        else:
            out[k] = v
    return out


def median(xs):
    """return the median of a list, or None when it is empty"""
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def sign_test(diffs):
    """return the two-sided sign test p and the up and down counts"""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    n = pos + neg
    if n == 0:
        return 1.0, pos, neg
    from math import comb
    k = min(pos, neg)
    p = sum(comb(n, i) for i in range(k + 1)) / (2 ** n) * 2
    return min(1.0, p), pos, neg


def wilcoxon(diffs):
    """return the exact two-sided Wilcoxon signed-rank p and W"""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return 1.0, 0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    w_minus = sum(r for r, x in zip(ranks, d) if x < 0)
    w = min(w_plus, w_minus)
    from itertools import product
    total = 0
    hits = 0
    for signs in product((1, -1), repeat=n):
        wp = sum(r for r, sg in zip(ranks, signs) if sg > 0)
        wm = sum(r for r, sg in zip(ranks, signs) if sg < 0)
        total += 1
        if min(wp, wm) <= w:
            hits += 1
    return hits / total, w


def dry_run(args, conds, topo, unpublished):
    """check every argument and print round 1's commands, running nothing"""
    bad = []
    if args.repeats < 1:
        bad.append(f"--repeats {args.repeats} runs no round")
    if args.frames < 1 or args.period_ns < 1 or args.slopes < 1 or args.acts < 1 or args.inject_s < 0 or args.vcpus < 0:
        bad.append("--frames, --period-ns, --slopes and --acts must be positive, --inject-s and --vcpus not negative")
    if not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", args.pin):
        bad.append(f"--pin {args.pin!r} is not a CPU list such as 2,3 or 4-7")
    if not EXP.is_file():
        bad.append(f"{EXP} is missing")
    if len({c[0] for c in conds}) != len(conds):
        bad.append("two conditions share a label: their run directories would overwrite each other")
    prereq = []
    for label, mode, arg, envs in conds:
        try:
            float(arg)
        except ValueError:
            bad.append(f"--condition {label}: the argument {arg!r} of {mode} is not a number")
            continue
        if mode == "cpu-load" and not 0 < float(arg) <= 100:
            bad.append(f"--condition {label}: a cpu-load duty is 1-100, not {arg}")
        for k, v in envs.items():
            if not re.fullmatch(r"TEMPO_[A-Z0-9_]+", k):
                bad.append(f"--condition {label}: {k} is not a TEMPO_* variable")
            if k == "TEMPO_KERNEL" and not (ROOT / v).is_file() and not Path(v).is_file():
                prereq.append(f"{v} is not built: bash tools/build/guest_kernel.sh")
    if unpublished and (ROOT / "results" / "experiments").resolve() not in (ROOT / args.out).resolve().parents:
        prereq.append(f"topology is {topo['topology']}: the run would refuse --out {args.out}; "
                      f"write under results/experiments/")
    for label, mode, arg, envs in conds:
        argv = launch_argv(f"{label}-r0", mode, arg, args.frames, args.period_ns, args.pin, args.slopes, args.acts,
                           args.inject_s, args.vcpus)
        shown = [str(Path(a).relative_to(ROOT)) if a.startswith(str(ROOT) + os.sep) else a for a in argv]
        print("+ " + " ".join([f"{k}={v}" for k, v in sorted(envs.items())] + shown))
    n = args.repeats * len(conds)
    print(f"paired: dry run -- {args.repeats} rounds x {len(conds)} conditions = {n} runs, written to {args.out}; nothing was run")
    for q in dict.fromkeys(prereq):
        print(f"paired: host prerequisite you supply -- {q}")
    for b in bad:
        print(f"paired: REFUSED -- {b}", file=sys.stderr)
    return 1 if bad else 0


def main():
    """run the interleaved rounds and print the paired comparison"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", action="append", required=True,
                    help="label:mode[:arg[:VAR=val,VAR=val]], e.g. control:none or "
                         "hog80:cpu-load:80 or probe-off:none:0:TEMPO_NO_PROBE=1")
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--frames", type=int, default=4000)
    ap.add_argument("--slopes", type=int, default=3200)
    ap.add_argument("--acts", type=int, default=500)
    ap.add_argument("--period-ns", type=int, default=1000000)
    ap.add_argument("--inject-s", type=int, default=0,
                    help="injection window in seconds")
    ap.add_argument("--pin", default=lib.host_conf("HOST_PINS"),
                    help="host CPUs, default HOST_PINS")
    ap.add_argument("--vcpus", type=int, default=0,
                    help="guest vCPU count")
    ap.add_argument("--metric", default="p999_ns",
                    help="FACT key to summarise")
    ap.add_argument("--out", default=None,
                    help="rounds file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print round 1, run nothing")
    ap.add_argument("--runner", default=None,
                    help="per-run script, default benchmark/run/experiment.sh")
    args = ap.parse_args()
    if args.runner:
        global EXP
        EXP = Path(args.runner) if Path(args.runner).is_absolute() else ROOT / args.runner

    topo = lib.host_topology()
    unpublished = topo["topology"] not in lib.PUBLISHED_TOPOLOGIES
    if args.out is None:
        args.out = ("results/experiments/paired-latest.json" if unpublished
                    else "results/paired-latest.json")
    exp_dir = (ROOT / "results" / "experiments").resolve()
    if unpublished and exp_dir not in (ROOT / args.out).resolve().parents:
        raise SystemExit(
            f"paired: topology is {topo['topology']} ({topo['why']}); refusing --out {args.out}; "
            f"write under results/experiments/")

    conds = []
    for c in args.condition:
        parts = c.split(":")
        envs = {}
        if len(parts) > 3 and parts[3]:
            for kv in parts[3].split(","):
                k, _, v = kv.partition("=")
                envs[k] = v
        if len(parts) < 2 or parts[1] not in INJECT_MODES:
            sys.exit(f"paired: --condition {c}: the injector mode is one of {', '.join(INJECT_MODES)}")
        conds.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "0", envs))

    if args.dry_run:
        return dry_run(args, conds, topo, unpublished)

    rounds = []
    for r in range(args.repeats):
        row = {}
        failed = 0
        for label, mode, arg, envs in conds:
            print(f"round {r+1}/{args.repeats}  {label}", flush=True)
            got = run_one(f"{label}-r{r}", mode, arg, args.frames, args.period_ns, args.pin,
                          args.slopes, args.acts, env_extra=envs, inject_s=args.inject_s,
                          vcpus=args.vcpus)
            if got is None:
                failed += 1
                continue
            row[label] = got
            print(f"  {label}: hit={got.get('hit')} late={got.get('late')} "
                  f"p999={got.get('p999_ns')} max={got.get('max_ns')} "
                  f"steal={got.get('steal_ticks')} "
                  f"relerr={got.get('mean_relerr', '-')} "
                  f"full={got.get('full_quality_frac', '-')}", flush=True)
        rounds.append(row)
        out = Path(ROOT / args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"metric": args.metric, "topology": topo["topology"],
                                   "topology_why": topo["why"], "rounds": rounds}, indent=1))
        if r == 0 and failed > 0:
            print(f"paired: round 1 had {failed} failed run(s); stopping the batch")
            sys.exit(5)

    out = Path(ROOT / args.out)

    base = conds[0][0]
    print(f"\n=== paired comparison on {args.metric}, {len(rounds)} rounds, "
          f"baseline '{base}' ===")
    for label, _, _, _ in conds[1:]:
        diffs = [rd[label][args.metric] - rd[base][args.metric]
                 for rd in rounds if label in rd and base in rd
                 and isinstance(rd[label].get(args.metric), (int, float))
                 and isinstance(rd[base].get(args.metric), (int, float))]
        if not diffs:
            print(f"{label:12} no complete rounds")
            continue
        p, pos, neg = sign_test(diffs)
        med = median(diffs)
        wp, w = wilcoxon(diffs)
        best = min(p, wp)
        print(f"{label:12} median delta {med:+12.0f}   {pos} up / {neg} down")
        print(f"{'':12}   sign-test p={p:.4f}   wilcoxon p={wp:.4f}   "
              f"{'SEPARATES' if best < 0.05 else 'does not separate'}")
    print(f"\nrounds written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
