#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.analysis.exits.exits import load, guest_wall_ns, COVERAGE_FLOOR, thawed   # noqa: E402
from benchmark.run.paired import median, sign_test, wilcoxon                  # noqa: E402

EXP = ROOT / "results" / "experiments"
EXTINT = 1

FROZEN = None

P1_RUNTIME_TOL = 0.10
P1_COST_FACTOR = 3.0
P1_TAIL_FACTOR = 3.0
P2_COUNT_TOL = 0.15
P2_WORKNS_TOL = 0.25
P2_RUNTIME_TOL = 0.05
P3_SCALE, P3_SCALE_TOL = 4.0, 0.40
P3_COST_TOL = 0.50
P3_RUNTIME_TOL = 0.05
P4_RATIO_LO, P4_RATIO_HI = 0.7, 1.3


def rounds_for(paired):
    return json.loads((ROOT / paired).read_text())["rounds"]


def per_round(label, i):
    if FROZEN is not None:
        if i >= len(FROZEN) or label not in FROZEN[i]:
            return None
        s = FROZEN[i][label]
        meta = {"watched_runtime_window_ns": s["runtime_ns"], "watched_threads": s["threads"]}
    else:
        d = EXP / f"{label}-r{i}"
        if not d.exists():
            return None
        s, meta = load(d)
    if not s["exits_total"] or s["coverage"] < COVERAGE_FLOOR:
        return None
    br = s["by_reason"]
    ext = br.get(EXTINT, {"count": 0, "ns": 0})
    return {
        "exits": s["exits_total"],
        "work_ns": s["work_ns"],
        "guest_ns": guest_wall_ns(s),
        "ext_n": ext["count"],
        "ext_ns": ext["ns"],
        "ext_cost": (ext["ns"] / ext["count"]) if ext["count"] else 0.0,
        "runtime_ns": meta.get("watched_runtime_window_ns", 0),
        "steal": None,
    }


def paired_deltas(rounds, a, b):
    out = []
    for i in range(len(rounds)):
        ra, rb = per_round(a, i), per_round(b, i)
        if ra is None or rb is None:
            continue
        out.append((i, ra, rb))
    return out


def med(xs):
    return median(xs) if xs else None


def verdict(name, ok, detail):
    print(f"  {'HIT ' if ok else 'MISS'}  {name}\n        {detail}")
    return ok


def tail_stats(rounds, base, label, metric="p999_ns"):
    d = [r[label][metric] - r[base][metric]
         for r in rounds if label in r and base in r]
    if not d:
        return None, None, None
    w, _ = wilcoxon(d)
    s, pos, neg = sign_test(d)
    return median(d), w, (pos, neg)


def run(paired="results/earlier_phase/paired/paired-prereg.json", frozen="results/earlier_phase/frozen/exits-frozen.json", live=False):
    global FROZEN
    if not live and (ROOT / frozen).exists():
        FROZEN = thawed(frozen)
        src = frozen
    else:
        FROZEN = None
        src = "live"
    rounds = rounds_for(paired)
    print(f"{paired}: {len(rounds)} rounds, exits {src}\n")

    labels = ["control", "tlb", "tlbq", "cache", "wake5k", "wake20k", "hog20"]
    print(f"{'class':9} {'n':>3} {'d exits':>10} {'d nonidle ms':>13} {'d ext n':>9} "
          f"{'ext us/exit':>12} {'d runtime ms':>13} {'d p99.9 ms':>11} {'wilcoxon':>9}")
    print("-" * 92)
    table = {}
    for label in labels:
        if label == "control":
            pairs = [(i, per_round("control", i), per_round("control", i))
                     for i in range(len(rounds))]
            pairs = [(i, x, y) for i, x, y in pairs if x is not None]
        else:
            pairs = paired_deltas(rounds, "control", label)
        if not pairs:
            print(f"{label:9} {'-':>3}  no rounds")
            continue
        t = {
            "n": len(pairs),
            "d_exits": med([b["exits"] - c["exits"] for _, c, b in pairs]),
            "d_work_ms": med([(b["work_ns"] - c["work_ns"]) / 1e6 for _, c, b in pairs]),
            "d_ext_n": med([b["ext_n"] - c["ext_n"] for _, c, b in pairs]),
            "ext_cost_us": med([b["ext_cost"] / 1000 for _, c, b in pairs]),
            "ctl_ext_cost_us": med([c["ext_cost"] / 1000 for _, c, b in pairs]),
            "d_runtime_ms": med([(b["runtime_ns"] - c["runtime_ns"]) / 1e6 for _, c, b in pairs]),
            "ctl_runtime_ms": med([c["runtime_ns"] / 1e6 for _, c, b in pairs]),
            "ctl_exits": med([c["exits"] for _, c, b in pairs]),
            "ctl_work_ms": med([c["work_ns"] / 1e6 for _, c, b in pairs]),
        }
        dp, w, ud = tail_stats(rounds, "control", label) if label != "control" else (0, 1.0, (0, 0))
        t["d_p999_ms"] = (dp or 0) / 1e6
        t["p999_wilcoxon"] = w
        table[label] = t
        print(f"{label:9} {t['n']:>3} {t['d_exits']:>10.0f} {t['d_work_ms']:>13.1f} "
              f"{t['d_ext_n']:>9.0f} {t['ext_cost_us']:>12.2f} {t['d_runtime_ms']:>13.1f} "
              f"{t['d_p999_ms']:>11.2f} {w if w is None else f'{w:.4f}':>9}")

    print()
    ok = {}

    print("P1  tlbq/control")
    if "tlbq" in table and "tlb" in table:
        t, tq = table["tlb"], table["tlbq"]
        r_run = abs(tq["d_runtime_ms"]) / tq["ctl_runtime_ms"] if tq["ctl_runtime_ms"] else 9
        r_cost = tq["ext_cost_us"] / tq["ctl_ext_cost_us"] if tq["ctl_ext_cost_us"] else 9
        r_tail = (abs(t["d_p999_ms"]) / abs(tq["d_p999_ms"])) if tq["d_p999_ms"] else float("inf")
        c1 = verdict("ext count up",
                     tq["d_ext_n"] > 0, f"d ext count {tq['d_ext_n']:+.0f}")
        c2 = verdict(f"cost/exit <= {P1_COST_FACTOR:g}x",
                     r_cost <= P1_COST_FACTOR,
                     f"{tq['ext_cost_us']:.2f}/{tq['ctl_ext_cost_us']:.2f} us = {r_cost:.2f}x")
        c3 = verdict(f"runtime < {P1_RUNTIME_TOL:.0%}",
                     r_run < P1_RUNTIME_TOL,
                     f"d {tq['d_runtime_ms']:+.0f}/{tq['ctl_runtime_ms']:.0f} ms = {r_run:.1%}")
        c4 = verdict(f"p99.9 >= {P1_TAIL_FACTOR:g}x smaller",
                     r_tail >= P1_TAIL_FACTOR,
                     f"tlb {t['d_p999_ms']:+.2f} tlbq {tq['d_p999_ms']:+.2f} ms = {r_tail:.2f}x")
        ok["P1"] = all((c1, c2, c3, c4))
    else:
        print("  UNTESTED  no tlb/tlbq")
        ok["P1"] = None

    print("\nP2  cache/control")
    if "cache" in table:
        c = table["cache"]
        r_n = abs(c["d_exits"]) / c["ctl_exits"] if c["ctl_exits"] else 9
        r_w = abs(c["d_work_ms"]) / c["ctl_work_ms"] if c["ctl_work_ms"] else 9
        r_r = abs(c["d_runtime_ms"]) / c["ctl_runtime_ms"] if c["ctl_runtime_ms"] else 9
        d1 = verdict(f"exits <= {P2_COUNT_TOL:.0%}", r_n <= P2_COUNT_TOL,
                     f"d {c['d_exits']:+.0f}/{c['ctl_exits']:.0f} = {r_n:.1%}")
        d2 = verdict(f"exit time <= {P2_WORKNS_TOL:.0%}", r_w <= P2_WORKNS_TOL,
                     f"d {c['d_work_ms']:+.1f}/{c['ctl_work_ms']:.1f} ms = {r_w:.1%}")
        d3 = verdict(f"runtime <= {P2_RUNTIME_TOL:.0%}", r_r <= P2_RUNTIME_TOL,
                     f"d {c['d_runtime_ms']:+.0f}/{c['ctl_runtime_ms']:.0f} ms = {r_r:.1%}")
        st, sw, sud = tail_stats(rounds, "control", "cache", "steal_ticks")
        d4 = verdict("steal no separation", (sw or 1) > 0.05,
                     f"d {st:+.0f} ticks w p={sw:.4f}")
        ok["P2"] = all((d1, d2, d3, d4))
        print(f"  d p99.9 {c['d_p999_ms']:+.2f} ms w p={c['p999_wilcoxon']:.4f}")
    else:
        print("  UNTESTED  no cache")
        ok["P2"] = None

    print("\nP3  wake5k/wake20k")
    pairs = paired_deltas(rounds, "wake5k", "wake20k")
    if pairs and "wake5k" in table and "wake20k" in table:
        w5, w20 = table["wake5k"], table["wake20k"]
        scale_n = (w20["d_ext_n"] / w5["d_ext_n"]) if w5["d_ext_n"] else float("inf")
        scale_w = (w20["d_work_ms"] / w5["d_work_ms"]) if w5["d_work_ms"] else float("inf")
        cost_r = (w20["ext_cost_us"] / w5["ext_cost_us"]) if w5["ext_cost_us"] else float("inf")
        r_run = abs(w20["d_runtime_ms"]) / w20["ctl_runtime_ms"] if w20["ctl_runtime_ms"] else 9
        lo, hi = P3_SCALE * (1 - P3_SCALE_TOL), P3_SCALE * (1 + P3_SCALE_TOL)
        e1 = verdict(f"ext count {lo:.1f}-{hi:.1f}x",
                     lo <= scale_n <= hi,
                     f"d ext n {w5['d_ext_n']:+.0f} -> {w20['d_ext_n']:+.0f} = {scale_n:.2f}x")
        e2 = verdict(f"cost/exit < {P3_COST_TOL:.0%}",
                     abs(cost_r - 1) < P3_COST_TOL,
                     f"{w5['ext_cost_us']:.2f} -> {w20['ext_cost_us']:.2f} us = {cost_r:.2f}x")
        e3 = verdict(f"exit time {lo:.1f}-{hi:.1f}x", lo <= scale_w <= hi,
                     f"d {w5['d_work_ms']:+.1f} -> {w20['d_work_ms']:+.1f} ms = {scale_w:.2f}x")
        e4 = verdict(f"runtime < {P3_RUNTIME_TOL:.0%}",
                     r_run < P3_RUNTIME_TOL,
                     f"d {w20['d_runtime_ms']:+.0f}/{w20['ctl_runtime_ms']:.0f} ms = {r_run:.1%}")
        ok["P3"] = all((e1, e2, e3, e4))
    else:
        print("  UNTESTED  no wake5k/wake20k")
        ok["P3"] = None

    print("\nP4  hog20/control")
    if "hog20" in table:
        h = table["hog20"]
        f1 = verdict("runtime falls", h["d_runtime_ms"] < 0,
                     f"d {h['d_runtime_ms']:+.0f} ms")
        ratio = (h["d_work_ms"] / abs(h["d_runtime_ms"])) if h["d_runtime_ms"] else float("inf")
        f2 = verdict(f"d exit time / |d runtime| in [{P4_RATIO_LO}, {P4_RATIO_HI}]",
                     P4_RATIO_LO <= ratio <= P4_RATIO_HI,
                     f"{h['d_work_ms']:+.1f} ms / {abs(h['d_runtime_ms']):.1f} ms = {ratio:.2f}")
        ok["P4"] = all((f1, f2))
        print(f"        d ext {h['d_ext_n']:+.0f}, cost/exit {h['ext_cost_us']:.2f}/{h['ctl_ext_cost_us']:.2f} us")
    else:
        print("  UNTESTED  no hog20")
        ok["P4"] = None

    print("\n" + "=" * 60)
    for k in ("P1", "P2", "P3", "P4"):
        v = ok.get(k)
        print(f"{k}: {'HIT' if v else ('UNTESTED' if v is None else 'MISS')}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paired", default="results/earlier_phase/paired/paired-prereg.json")
    ap.add_argument("--frozen", default="results/earlier_phase/frozen/exits-frozen.json")
    ap.add_argument("--live", action="store_true",
                    help="results/experiments/")
    a = ap.parse_args()
    from benchmark import lib
    lib.need_input(a.paired, "--paired", "exit_rule")
    run(a.paired, a.frozen, a.live)
    return 0


if __name__ == "__main__":
    sys.exit(main())
