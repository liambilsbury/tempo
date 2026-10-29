#!/usr/bin/env python3
"""Score the guest gap detector and the steal counter against L1's exit time"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402
from benchmark.analysis.exits.exits import parse, per_vcpu_outside_ns    # noqa: E402

EXP = ROOT / "results" / "experiments"
GAP = re.compile(r"(?:GAP_DETECTOR|GAPDET)\s+(.*)$")
KV = re.compile(r"(\w+)=(\S+)")


def gap_detector_of(outdir):
    """read the detector's key=value report from the guest console"""
    con = Path(outdir) / "l2" / "console.log"
    if not con.exists():
        return {}
    d = {}
    for line in con.read_text(errors="replace").splitlines():
        m = GAP.search(line)
        if not m:
            continue
        for k, v in KV.findall(m.group(1)):
            d[k] = int(v) if v.isdigit() else v
    return d


BUCKET_NS = 100_000_000


def detector_buckets(ex, gapcpu):
    """return the longest run of buckets where the detector's vCPU took no HLT exit"""
    hlt, seen = {}, set()
    for (b, v, r), n in ex["exits"].items():
        if v != gapcpu:
            continue
        seen.add(b)
        if r == 12:
            hlt[b] = hlt.get(b, 0) + n
    if not seen:
        return None, None
    best = cur = None
    best_lo = best_hi = cur_lo = None
    for b in sorted(seen):
        if hlt.get(b, 0) == 0:
            if cur_lo is None:
                cur_lo = b
            cur = b
        else:
            if cur_lo is not None and (best is None or cur - cur_lo > best_hi - best_lo):
                best_lo, best_hi, best = cur_lo, cur, cur
            cur_lo = None
    if cur_lo is not None and (best is None or cur - cur_lo > best_hi - best_lo):
        best_lo, best_hi = cur_lo, cur
    if best_lo is None:
        return None, None
    if best_hi - best_lo < 10:
        return None, None
    return best_lo, best_hi


def inject_meta(outdir):
    """parse the key=value pairs of inject.meta"""
    d = {}
    f = Path(outdir) / "inject.meta"
    if not f.exists():
        return d
    for line in f.read_text(errors="replace").splitlines():
        for tok in line.replace("INJECT ", "").split():
            k, _, v = tok.partition("=")
            if v.isdigit():
                d[k] = int(v)
            elif v:
                d[k] = v
    return d


def one(outdir, gapcpu):
    """return the outside, offcore, detector and steal fractions of a run"""
    g, m = gap_detector_of(outdir), inject_meta(outdir)
    if not g or not m or "gap_sum_ns" not in g:
        return None
    wall = m.get("watched_wall_ns", 0)
    rt = m.get(f"watched_vcpu{gapcpu}_runtime_window_ns", 0)
    rd = m.get(f"watched_vcpu{gapcpu}_rundelay_window_ns", 0)
    gw = g.get("wall_ns", 0)
    if not wall or not rt or not gw:
        return None

    outside, own_window = 0, 0
    ex = parse(Path(outdir) / "exits.txt")
    if ex.get("exit_ns"):
        lo, hi = detector_buckets(ex, gapcpu)
        if lo is not None:
            own_window = (hi - lo + 1) * BUCKET_NS
            for (b, v, r), ns in ex["exit_ns"].items():
                if v == gapcpu and lo <= b <= hi:
                    outside += ns
        else:
            t0, t1 = m.get("watched_window_start_ns", 0), m.get("watched_window_end_ns", 0)
            outside = per_vcpu_outside_ns(ex, t0, t1).get(gapcpu, 0)
            own_window = wall

    return {
        "offcore_frac": max(0.0, (wall - rt) / wall),
        "outside_frac": (outside / own_window) if (outside and own_window) else 0.0,
        "outside_window_ns": own_window,
        "detector_frac": g["gap_sum_ns"] / gw,
        "steal_frac": rd / wall,
        "gap_count": g.get("gap_count", 0),
        "gap_max_ns": g.get("gap_max_ns", 0),
        "ns_steal_ns": g.get("ns_steal_window_ns", 0),
        "detector_wall_ns": gw,
        "window_ns": wall,
    }


def gather(paired_path, gapcpu):
    """collect the per-round fractions of each class in a paired file"""
    rounds = json.loads((ROOT / paired_path).read_text())["rounds"]
    labels = sorted({k for r in rounds for k in r})
    out = {}
    for label in labels:
        rows = []
        for i in range(len(rounds)):
            r = one(EXP / f"{label}-r{i}", gapcpu)
            if r:
                r["round"] = i
                rows.append(r)
        if rows:
            out[label] = rows
    return out


def recalls(rows, denom="outside_frac"):
    """return the detector and steal recalls of each row"""
    det = [r["detector_frac"] / r[denom] for r in rows if r.get(denom, 0) > 0]
    ste = [r["steal_frac"] / r[denom] for r in rows if r.get(denom, 0) > 0]
    return det, ste


def paired_recalls(rows, ctl, denom="outside_frac"):
    """return the detector and steal recalls with the control round subtracted"""
    det, ste = [], []
    by_round = {r["round"]: r for r in ctl}
    for r in rows:
        c = by_round.get(r["round"])
        if not c:
            continue
        d_true = r.get(denom, 0) - c.get(denom, 0)
        if d_true <= 0:
            continue
        det.append((r["detector_frac"] - c["detector_frac"]) / d_true)
        ste.append((r["steal_frac"] - c["steal_frac"]) / d_true)
    return det, ste


def report(data):
    """print the recall tables and return the control-subtracted medians"""
    hdr = (f"{'class':9} {'n':>3} {'outside %':>10} {'offcore %':>10} {'detector %':>11} "
           f"{'steal %':>8} {'detector recall':>18} {'steal recall':>18}")
    print(hdr)
    print("-" * len(hdr))
    for label, rows in data.items():
        det, ste = recalls(rows)
        fmt = lambda xs: (f"{median(xs):.2f} [{min(xs):.2f}-{max(xs):.2f}]" if xs else "-")
        print(f"{label:9} {len(rows):>3} "
              f"{100*median([r['outside_frac'] for r in rows]):>10.1f} "
              f"{100*median([r['offcore_frac'] for r in rows]):>10.1f} "
              f"{100*median([r['detector_frac'] for r in rows]):>11.1f} "
              f"{100*median([r['steal_frac'] for r in rows]):>8.1f} "
              f"{fmt(det):>18} {fmt(ste):>18}")

    ctl = data.get("control", [])
    inj = {k: v for k, v in data.items() if k != "control"}
    if ctl:
        cd = median([r["detector_frac"] for r in ctl])
        print(f"\ndetector floor with nothing injected: {100*cd:.1f}% of its window as gaps, "
              f"hypervisor says {100*median([r['outside_frac'] for r in ctl]):.1f}%; "
              f"subtracted below")

    print(f"\n{'class':9} {'n':>3} {'detector recall':>20} {'steal recall':>20}   "
          f"control-subtracted")
    print("-" * 76)
    alld, alls, pairs = [], [], []
    for label, rows in inj.items():
        d, s = paired_recalls(rows, ctl)
        if not d:
            continue
        alld += d
        alls += s
        pairs += [a - b for a, b in zip(d, s)]
        fmt = lambda xs: f"{median(xs):.2f} [{min(xs):.2f}-{max(xs):.2f}]"
        print(f"{label:9} {len(d):>3} {fmt(d):>20} {fmt(s):>20}")

    if alld and alls:
        print(f"\nacross the injected classes: detector {median(alld):.2f}, "
              f"steal {median(alls):.2f}")
        p, pos, neg = sign_test(pairs)
        w, _ = wilcoxon(pairs)
        print(f"detector minus steal, per condition-round: median {median(pairs):+.2f}, "
              f"{pos} up / {neg} down, sign p={p:.4f}, wilcoxon p={w:.4f}")
    return {"detector": median(alld) if alld else None,
            "steal": median(alls) if alls else None}


def freeze(data, out_path):
    """write the gathered classes to a frozen JSON file"""
    from benchmark import lib
    lib.refuse_unpublishable_experiments()
    p = ROOT / out_path
    p.write_text(json.dumps({"classes": data}, indent=1, sort_keys=True) + "\n")
    print(f"froze {sum(len(v) for v in data.values())} condition-rounds to {out_path}")


def thawed(frozen):
    """load the classes from a frozen JSON file"""
    return json.loads((ROOT / frozen).read_text())["classes"]


def verdict(frozen="results/earlier_phase/frozen/gapdet-frozen.json"):
    """return the control-subtracted medians and the detector floor"""
    data = thawed(frozen)
    ctl = data.get("control", [])
    inj = {k: v for k, v in data.items() if k != "control"}
    alld, alls = [], []
    for rows in inj.values():
        d, s = paired_recalls(rows, ctl)
        alld += d
        alls += s
    return {"detector": median(alld) if alld else None,
            "steal": median(alls) if alls else None,
            "floor": median([r["detector_frac"] for r in ctl]) if ctl else None}


def selftest():
    """check the recall and parsing helpers on fixed inputs"""
    cases = []
    rows = [{"outside_frac": 0.40, "offcore_frac": 0.30,
             "detector_frac": 0.36, "steal_frac": 0.20}]
    d, s = recalls(rows)
    cases.append(("detector recall is its fraction over the truth's", abs(d[0] - 0.9) < 1e-9))
    cases.append(("steal recall is its fraction over the truth's", abs(s[0] - 0.5) < 1e-9))

    zero = [{"outside_frac": 0.0, "offcore_frac": 0.0,
             "detector_frac": 0.1, "steal_frac": 0.1}]
    d0, s0 = recalls(zero)
    cases.append(("a window with no loss yields no recall",
                  d0 == [] and s0 == []))

    txt = ("TEMPO-L2 GAP_DETECTOR cpu=4 wall_ns=18000000000 iters=9 threshold_ns=2000 cal_max_ns=5\n"
           "TEMPO-L2 GAP_DETECTOR gap_sum_ns=5400000000 gap_count=100 gap_max_ns=999\n"
           "ARTC-L2 GAPDET ns_steal_window_ns=3000000000\n")
    tmp = ROOT / "results" / ".selftest-gap_detector"
    (tmp / "l2").mkdir(parents=True, exist_ok=True)
    (tmp / "l2" / "console.log").write_text(txt)
    (tmp / "inject.meta").write_text(
        "INJECT watched_wall_ns=20000000000\n"
        "INJECT watched_vcpu4_runtime_window_ns=14000000000\n"
        "INJECT watched_vcpu4_rundelay_window_ns=4000000000\n")
    got = one(tmp, 4)
    import shutil
    shutil.rmtree(tmp)
    cases.append(("a run parses out of the guest console and the injector meta",
                  got is not None and abs(got["offcore_frac"] - 0.30) < 1e-9
                  and abs(got["detector_frac"] - 0.30) < 1e-9
                  and abs(got["steal_frac"] - 0.20) < 1e-9))
    cases.append(("a run with no exit counter reports no `outside`",
                  got is not None and got["outside_frac"] == 0.0))
    cases.append(("the two denominators are different quantities and both are kept",
                  got is not None and "offcore_frac" in got and "outside_frac" in got))
    cases.append(("the detector's own window is used for its fraction",
                  got is not None and got["detector_wall_ns"] == 18_000_000_000
                  and got["window_ns"] == 20_000_000_000))

    cases.append(("a run without a detector yields nothing",
                  one(ROOT / "results" / "does-not-exist", 4) is None))

    bad = sum(1 for _, good in cases if not good)
    for name, good in cases:
        print(f"  {'ok  ' if good else 'FAIL'} {name}")
    print(f"gap_detector_recall selftest: {len(cases) - bad}/{len(cases)}")
    return 0 if bad == 0 else 1


def main():
    """run the selftest, freeze, or report"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--paired", default="results/earlier_phase/paired/paired-gapdet.json")
    ap.add_argument("--frozen", default="results/earlier_phase/frozen/gapdet-frozen.json")
    ap.add_argument("--gapcpu", type=int, default=4)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--freeze")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.live or a.freeze or not (ROOT / a.frozen).exists():
        from benchmark import lib
        lib.need_input(a.paired, "--paired", "gap_detector")
    if a.freeze:
        freeze(gather(a.paired, a.gapcpu), a.freeze)
        return 0
    data = (thawed(a.frozen) if (not a.live and (ROOT / a.frozen).exists())
            else gather(a.paired, a.gapcpu))
    if not data:
        print("no rounds carried a detector report", file=sys.stderr)
        return 1
    report(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
