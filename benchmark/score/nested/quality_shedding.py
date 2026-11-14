#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-adapt.json"
QUALITY = ROOT / "results" / "earlier_phase" / "frozen" / "quality-frozen.json"
IN_LOOP = ROOT / "results" / "earlier_phase" / "frozen" / "inloop-frozen.json"

ARMS = ["fixed-hi", "fixed-hi-probe", "adaptive", "fixed-lo"]
ROUNDS_REQUIRED = 16
ALPHA = 0.05

P5_COMPUTE_COST_TOL = 0.01
P5_RECALL_RATIO_LO = 0.4
P5_RECALL_RATIO_HI = 1.0
GAP_DETECTOR_RECALL = 0.96

P6_MISS_FACTOR = 0.5

P7_MEAN_RELERR_MAX = 0.020
P7_MEAN_RELERR_FALSIFY = 0.039
P7_FULL_QUALITY_FRAC_MIN = 0.5

P8_QUALITY_FACTOR = 0.5
TIER3_RELERR = 0.0786


def verdict(name, ok, detail):
    print(f"  {'HIT ' if ok else 'MISS'}  {name}\n        {detail}")
    return ok


def rounds_of(path):
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return None
    return json.loads(p.read_text())["rounds"]


def paired(rounds, a, b, key):
    return [r[b][key] - r[a][key]
            for r in rounds if a in r and b in r and key in r[a] and key in r[b]]


def arm_median(rounds, arm, key):
    v = [r[arm][key] for r in rounds if arm in r and key in r[arm]]
    return median(v) if v else None


def separates(diffs):
    if not diffs:
        return False, None, None, (0, 0)
    s, pos, neg = sign_test(diffs)
    w, _ = wilcoxon(diffs)
    return (s < ALPHA), s, w, (pos, neg)


def run(paired_path=PAIRED):
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}

    kept, void = [], []
    for i, r in enumerate(rounds):
        missing = [a for a in ARMS if a not in r]
        unconfirmed = [a for a in ARMS if a in r and not r[a].get("hit")]
        if missing or unconfirmed:
            void.append((i, missing, unconfirmed))
        else:
            kept.append(r)
    if void:
        print(f"{len(void)}/{len(rounds)} round(s) void:")
        for i, missing, unconfirmed in void:
            why = []
            if missing:
                why.append(f"no {', '.join(missing)}")
            if unconfirmed:
                why.append(f"no arrival {', '.join(unconfirmed)}")
            print(f"  round {i}: {'; '.join(why)}")
    rounds = kept
    if not rounds:
        print("no usable rounds")
        return {}

    if len(rounds) < ROUNDS_REQUIRED:
        print(f"{paired_path}: rounds {len(rounds)}/{ROUNDS_REQUIRED}")

    out = {}

    print("P5 detector cost, recall")
    cost = paired(rounds, "fixed-hi", "fixed-hi-probe", "compute_p50_ns")
    base = arm_median(rounds, "fixed-hi", "compute_p50_ns")
    frac = (median(cost) / base) if cost and base else None
    c_ok = frac is not None and abs(frac) < P5_COMPUTE_COST_TOL

    miss = paired(rounds, "fixed-hi", "fixed-hi-probe", "over_deadline")
    m_sep, m_p, _, m_ud = separates(miss)
    tail = paired(rounds, "fixed-hi", "fixed-hi-probe", "p999_ns")
    t_sep, t_p, _, _ = separates(tail)

    c_sep, c_p, c_w, c_ud = separates(cost)
    a = verdict("compute p50 cost",
                c_ok,
                (f"{100*frac:+.3f}% < {100*P5_COMPUTE_COST_TOL:.0f}% sign p={c_p} "
                 f"{c_ud[0]}+/{c_ud[1]}- sep={c_sep}")
                if frac is not None else "no pairs")
    b = verdict("misses no separation", not m_sep,
                f"sign p={m_p} {m_ud[0]}+/{m_ud[1]}-" if m_p is not None else "-")
    c = verdict("p99.9 no separation", not t_sep,
                f"sign p={t_p}" if t_p is not None else "-")

    rec = json.loads(IN_LOOP.read_text()).get("detector") if Path(IN_LOOP).exists() else None
    if rec is None:
        d = verdict("recall", False, "UNTESTED no inloop-frozen.json")
    else:
        ratio = rec / GAP_DETECTOR_RECALL
        d = verdict("recall",
                    rec < GAP_DETECTOR_RECALL and P5_RECALL_RATIO_LO <= ratio <= P5_RECALL_RATIO_HI,
                    f"in-loop {rec:.3f} gap_detector {GAP_DETECTOR_RECALL} ratio {ratio:.3f} "
                    f"[{P5_RECALL_RATIO_LO}, {P5_RECALL_RATIO_HI}]")
    out["P5"] = bool(a and b and c and d)

    print("\nP6 adaptive/probe deadlines")
    d_miss = paired(rounds, "fixed-hi-probe", "adaptive", "over_deadline_frac")
    sep, p, w, ud = separates(d_miss)
    fell = sep and median(d_miss) < 0
    ad = arm_median(rounds, "adaptive", "over_deadline_frac")
    pr = arm_median(rounds, "fixed-hi-probe", "over_deadline_frac")
    halved = ad is not None and pr is not None and pr > 0 and ad <= P6_MISS_FACTOR * pr

    d_tail = paired(rounds, "fixed-hi-probe", "adaptive", "p999_ns")
    t_sep2, t_p2, _, _ = separates(d_tail)
    tail_fell = t_sep2 and median(d_tail) < 0

    d_drop = paired(rounds, "fixed-hi-probe", "adaptive", "dropped")
    drop_sep, drop_p, _, _ = separates(d_drop)
    drops_ok = not (drop_sep and median(d_drop) > 0)

    e = verdict("misses fall", bool(fell),
                f"sign p={p} {ud[0]}+/{ud[1]}- w {w}" if p is not None else "no pairs")
    f = verdict("halved", bool(halved),
                f"adaptive {ad} probe {pr} x{P6_MISS_FACTOR}"
                if ad is not None and pr is not None else "-")
    g = verdict("p99.9 falls", bool(tail_fell),
                f"sign p={t_p2}" if t_p2 is not None else "-")
    h = verdict("drops flat", bool(drops_ok),
                f"sign p={drop_p}" if drop_p is not None else "-")
    out["P6"] = bool(e and f and g and h)

    print("\nP7 quality")
    err = arm_median(rounds, "adaptive", "mean_relerr")
    full = arm_median(rounds, "adaptive", "full_quality_frac")
    i = verdict("mean relative error under the bound",
                err is not None and err < P7_MEAN_RELERR_MAX,
                f"{err} < {P7_MEAN_RELERR_MAX}, falsify {P7_MEAN_RELERR_FALSIFY}" if err is not None else "-")
    j = verdict("full quality",
                full is not None and full > P7_FULL_QUALITY_FRAC_MIN,
                f"{full} > {P7_FULL_QUALITY_FRAC_MIN}" if full is not None else "-")
    out["P7"] = bool(i and j)

    print("\nP8 adaptive/fixed-lo")
    a_miss = arm_median(rounds, "adaptive", "over_deadline_frac")
    l_miss = arm_median(rounds, "fixed-lo", "over_deadline_frac")
    l_err = arm_median(rounds, "fixed-lo", "mean_relerr")
    l_err = TIER3_RELERR if l_err is None else l_err

    misses_more = a_miss is not None and l_miss is not None and a_miss > l_miss
    better = err is not None and err <= P8_QUALITY_FACTOR * l_err

    verdict("misses more",
            bool(misses_more),
            f"adaptive {a_miss} vs fixed-lo {l_miss}"
            if a_miss is not None and l_miss is not None else "-")
    verdict("error within bound", bool(better),
            f"adaptive {err} vs fixed-lo {l_err} x{P8_QUALITY_FACTOR}" if err is not None else "-")
    dominated = misses_more and not better
    if dominated:
        print("        DOMINATED")
    out["P8"] = bool(misses_more and better)

    print("\nverdicts  " + "  ".join(
        f"{k2}={'HIT' if v else 'MISS'}" for k2, v in sorted(out.items())))
    return out


def _round(over_hi, over_probe, over_ad, over_lo, err_ad, err_lo, comp_hi, comp_probe,
           p999_hi=2_000_000, p999_probe=2_000_000, p999_ad=1_000_000, drop=0, rec=0.6,
           full=0.8):
    def arm(od, comp, p999, mean_err=0.0, fq=1.0):
        return {"over_deadline": int(od * 1000), "over_deadline_frac": od,
                "compute_p50_ns": comp, "p999_ns": p999, "dropped": drop,
                "mean_relerr": mean_err, "full_quality_frac": fq,
                "hit": 40000}
    r = {
        "fixed-hi": arm(over_hi, comp_hi, p999_hi),
        "fixed-hi-probe": arm(over_probe, comp_probe, p999_probe),
        "adaptive": arm(over_ad, comp_probe, p999_ad, err_ad, full),
        "fixed-lo": arm(over_lo, comp_hi // 5, 900_000, err_lo, 0.0),
    }
    r["adaptive"]["detector_recall"] = rec
    return r


def _batch(n=16, **kw):
    import tempfile
    rounds = []
    for i in range(n):
        d = dict(kw)
        d["over_hi"] = kw["over_hi"] * (1 + 0.01 * (i % 5))
        d["over_probe"] = kw["over_probe"] * (1 + 0.01 * (i % 5))
        d["over_ad"] = kw["over_ad"] * (1 + 0.01 * ((i + 2) % 5))
        d["over_lo"] = kw["over_lo"] * (1 + 0.01 * ((i + 1) % 5))
        rounds.append(_round(**d))
    p = Path(tempfile.mkdtemp()) / "adapt.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


ALL_HIT_BATCH = dict(over_hi=0.040, over_probe=0.040, over_ad=0.015, over_lo=0.001,
                     err_ad=0.010, err_lo=0.0786, comp_hi=700_000, comp_probe=703_000)


def selftest():
    import contextlib
    import io
    failures = []

    import tempfile
    saved = IN_LOOP

    def score(recall=0.60, **over):
        global IN_LOOP
        kw = dict(ALL_HIT_BATCH)
        kw.update(over)
        if recall is None:
            IN_LOOP = Path(tempfile.mkdtemp()) / "absent.json"
        else:
            f = Path(tempfile.mkdtemp()) / "in_loop.json"
            f.write_text(json.dumps({"detector": recall, "n": 16}))
            IN_LOOP = f
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return run(_batch(**kw))
        finally:
            IN_LOOP = saved

    def expect(name, key, want, **over):
        got = score(**over)
        if got.get(key) is not want:
            failures.append(f"{name}: {key}={got.get(key)} want {want}")
            print(f"  FAIL  {name}")
        else:
            print(f"  ok  {key}={'HIT' if want else 'MISS'}  {name}")

    expect("all HIT", "P5", True)
    expect("all HIT", "P6", True)
    expect("all HIT", "P7", True)
    expect("all HIT", "P8", True)

    expect("detector cost 3%", "P5", False, comp_probe=721_000)
    expect("recall 0.99", "P5", False, recall=0.99)
    expect("no recall", "P5", False, recall=None)
    expect("od flat", "P6", False, over_ad=0.041)
    expect("od not halved", "P6", False, over_ad=0.030)
    expect("relerr 0.045", "P7", False, err_ad=0.045)
    expect("relerr 0.025", "P7", False, err_ad=0.025)
    expect("fixed-lo dominates", "P8", False, err_ad=0.060)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


MD_BEGIN = "<!-- ADAPT-VERDICTS:BEGIN -->"
MD_END = "<!-- ADAPT-VERDICTS:END -->"


def markdown(paired_path=PAIRED):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*unscored*\n{MD_END}"
    titles = {
        "P5": "in-loop detector cost < bound, recall < gap_detector",
        "P6": "od adaptive <= 0.5 x fixed-hi-probe",
        "P7": "adaptive relerr, full_quality_frac in bounds",
        "P8": "adaptive not dominated by fixed-lo",
    }
    out = [MD_BEGIN, "", "| prediction | claim | verdict |", "|---|---|---|"]
    for k in ("P5", "P6", "P7", "P8"):
        v = got.get(k)
        out.append(f"| **{k}** | {titles[k]} | **{'HIT' if v else 'MISS'}** |")
    out += ["", "*`benchmark/score/nested/quality_shedding.py`*", "",
            "```", buf.getvalue().rstrip(), "```", "", MD_END]
    return "\n".join(out)


def write_markdown(doc, paired_path=PAIRED, check=False):
    from benchmark import lib
    lib.refuse_unpublishable_json(paired_path)
    p = Path(doc)
    if not p.is_absolute():
        p = ROOT / p
    text = p.read_text()
    block = markdown(paired_path)
    if MD_BEGIN not in text or MD_END not in text:
        raise SystemExit(f"{doc}: no markers")
    head, rest = text.split(MD_BEGIN, 1)
    _, tail = rest.split(MD_END, 1)
    if check:
        current = MD_BEGIN + rest.split(MD_END, 1)[0] + MD_END
        if lib.renamed_translator(("path", "env", "flag", "label", "text"))(current).strip() != block.strip():
            raise SystemExit(f"{doc}: block differs")
        return p
    p.write_text(head + block + tail)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paired", default=str(PAIRED))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--markdown", metavar="DOC", default=None)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(a.paired, "--paired", "adapt")
    if a.markdown:
        p = write_markdown(a.markdown, a.paired, check=a.check)
        print(f"{p}: {'ok' if a.check else 'written'}")
        return 0
    run(a.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
