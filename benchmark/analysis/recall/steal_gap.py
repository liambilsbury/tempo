#!/usr/bin/env python3
"""Split steal recall into presentation and definitional ratios"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median   # noqa: E402

EXP = ROOT / "results" / "experiments"
USER_HZ_NS = 10_000_000


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


def thawed(frozen_path):
    """load the classes from a frozen JSON file"""
    return json.loads((ROOT / frozen_path).read_text())["classes"]


def gather(paired_path):
    """return told, read and lost per class and round, each minus that round's control"""
    rounds = json.loads((ROOT / paired_path).read_text())["rounds"]
    labels = [k for k in rounds[0] if k != "control"] if rounds else []
    out = {}
    for label in labels:
        rows = []
        for i, r in enumerate(rounds):
            if label not in r or "control" not in r:
                continue
            cm = inject_meta(EXP / f"control-r{i}")
            hm = inject_meta(EXP / f"{label}-r{i}")
            if not cm or not hm:
                continue
            told = (hm.get("watched_vcpu_rundelay_window_ns", 0)
                    - cm.get("watched_vcpu_rundelay_window_ns", 0))
            read = (r[label]["steal_ticks"] - r["control"]["steal_ticks"]) * USER_HZ_NS
            lost = (cm.get("watched_runtime_window_ns", 0)
                    - hm.get("watched_runtime_window_ns", 0))
            rows.append({"round": i, "told_ns": told, "read_ns": read, "lost_ns": lost})
        if rows:
            out[label] = rows
    return out


def ratios(rows):
    """return the per-round presentation, definitional and overall ratios"""
    pres = [r["read_ns"] / r["told_ns"] for r in rows if r["told_ns"] > 0]
    defi = [r["told_ns"] / r["lost_ns"] for r in rows if r["lost_ns"] > 0]
    tot = [r["read_ns"] / r["lost_ns"] for r in rows if r["lost_ns"] > 0]
    return pres, defi, tot


def report(paired_path, frozen="results/earlier_phase/frozen/stealgap-frozen.json", live=False):
    """print the ratio table and return the per-class summary"""
    data = (thawed(frozen) if (not live and (ROOT / frozen).exists())
            else gather(paired_path))
    if not data:
        print("no rounds with both an injected condition and its control", file=sys.stderr)
        return 1
    print("T = vcpu run_delay on L1, G = steal ticks x 10 ms, L = scheduler runtime deficit\n")
    hdr = (f"{'class':9} {'n':>3} {'T ms':>9} {'G ms':>9} {'L ms':>9} "
           f"{'G/T presentation':>19} {'T/L definitional':>19} {'G/L overall':>13}")
    print(hdr)
    print("-" * len(hdr))
    summary = {}
    for label, rows in data.items():
        pres, defi, tot = ratios(rows)
        fmt = lambda xs: (f"{median(xs):.2f} [{min(xs):.2f}-{max(xs):.2f}]" if xs else "-")
        summary[label] = {
            "n": len(rows),
            "told_ms": median([r["told_ns"] for r in rows]) / 1e6,
            "read_ms": median([r["read_ns"] for r in rows]) / 1e6,
            "lost_ms": median([r["lost_ns"] for r in rows]) / 1e6,
            "presentation": pres, "definitional": defi, "overall": tot,
        }
        s = summary[label]
        print(f"{label:9} {s['n']:>3} {s['told_ms']:>9.0f} {s['read_ms']:>9.0f} "
              f"{s['lost_ms']:>9.0f} {fmt(pres):>19} {fmt(defi):>19} {fmt(tot):>13}")


    allp = [x for s in summary.values() for x in s["presentation"]]
    alld = [x for s in summary.values() for x in s["definitional"]]
    if allp and alld:
        mp, md = median(allp), median(alld)
        print(f"\nacross all classes: presentation {mp:.2f}, definitional {md:.2f}")
    return summary


def verdict(paired="results/earlier_phase/paired/paired-stealgap.json", frozen="results/earlier_phase/frozen/stealgap-frozen.json"):
    """return the median presentation, definitional and overall ratios"""
    data = thawed(frozen) if (ROOT / frozen).exists() else gather(paired)
    allp, alld, allo = [], [], []
    for rows in data.values():
        p, d, o = ratios(rows)
        allp += p
        alld += d
        allo += o
    return {"presentation": median(allp) if allp else None,
            "definitional": median(alld) if alld else None,
            "overall": median(allo) if allo else None}


def freeze(paired_path, out_path):
    """write the gathered classes to a frozen JSON file"""
    from benchmark import lib
    lib.refuse_unpublishable_experiments()
    data = gather(paired_path)
    p = ROOT / out_path
    p.write_text(json.dumps({"source": str(paired_path), "classes": data},
                            indent=1, sort_keys=True) + "\n")
    print(f"froze {sum(len(v) for v in data.values())} condition-rounds to {out_path}")
    return 0


def selftest():
    """check the ratios on rows with known answers"""
    cases = []
    rows = [{"round": 0, "told_ns": 1_000_000_000, "read_ns": 500_000_000,
             "lost_ns": 4_000_000_000}]
    pres, defi, tot = ratios(rows)
    cases.append(("presentation is read over told", abs(pres[0] - 0.5) < 1e-9))
    cases.append(("definitional is told over lost", abs(defi[0] - 0.25) < 1e-9))
    cases.append(("overall is read over lost", abs(tot[0] - 0.125) < 1e-9))
    cases.append(("the two gaps compose to the overall",
                  abs(pres[0] * defi[0] - tot[0]) < 1e-9))

    zero = [{"round": 0, "told_ns": 0, "read_ns": 5, "lost_ns": 0}]
    p2, d2, t2 = ratios(zero)
    cases.append(("a class that was told nothing yields no presentation ratio", p2 == []))
    cases.append(("a class that lost nothing yields no definitional ratio",
                  d2 == [] and t2 == []))

    neg = [{"round": 0, "told_ns": 10, "read_ns": 5, "lost_ns": -100}]
    p3, d3, _ = ratios(neg)
    cases.append(("a class that GAINED runtime yields no definitional ratio",
                  d3 == [] and p3 == [0.5]))

    many = [{"round": i, "told_ns": 100, "read_ns": v, "lost_ns": 200}
            for i, v in enumerate((10, 50, 90))]
    p4, _, _ = ratios(many)
    cases.append(("the spread is kept per round, not collapsed to a ratio of medians",
                  len(p4) == 3 and abs(median(p4) - 0.5) < 1e-9
                  and abs(min(p4) - 0.1) < 1e-9 and abs(max(p4) - 0.9) < 1e-9))

    bad = sum(1 for _, good in cases if not good)
    for name, good in cases:
        print(f"  {'ok  ' if good else 'FAIL'} {name}")
    print(f"steal_gap selftest: {len(cases) - bad}/{len(cases)}")
    return 0 if bad == 0 else 1


def main():
    """run the selftest, freeze, or report"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--paired", default="results/earlier_phase/paired/paired-stealgap.json")
    ap.add_argument("--frozen", default="results/earlier_phase/frozen/stealgap-frozen.json")
    ap.add_argument("--live", action="store_true",
                    help="read results/experiments/")
    ap.add_argument("--freeze")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.live or a.freeze or not (ROOT / a.frozen).exists():
        from benchmark import lib
        lib.need_input(a.paired, "--paired", "steal_gap")
    if a.freeze:
        return freeze(a.paired, a.freeze)
    report(a.paired, a.frozen, a.live)
    return 0


if __name__ == "__main__":
    sys.exit(main())
