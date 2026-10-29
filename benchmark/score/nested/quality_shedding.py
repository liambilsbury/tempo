#!/usr/bin/env python3
"""Score the adaptation predictions P5 to P8 against a paired results file

Usage: benchmark/score/nested/quality_shedding.py [--paired FILE] [--selftest] [--markdown DOC [--check]]"""

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
    """print a prediction's verdict line and return its state"""
    print(f"  {'HIT ' if ok else 'MISS'}  {name}\n        {detail}")
    return ok


def rounds_of(path):
    """load the rounds of a paired results file, or None if it is missing"""
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return None
    return json.loads(p.read_text())["rounds"]


def paired(rounds, a, b, key):
    """return b minus a per round, for rounds where both arms have the metric"""
    return [r[b][key] - r[a][key]
            for r in rounds if a in r and b in r and key in r[a] and key in r[b]]


def arm_median(rounds, arm, key):
    """return the median of an arm's metric over the rounds that have it"""
    v = [r[arm][key] for r in rounds if arm in r and key in r[arm]]
    return median(v) if v else None


def separates(diffs):
    """run the two-sided sign test and wilcoxon on paired differences"""
    if not diffs:
        return False, None, None, (0, 0)
    s, pos, neg = sign_test(diffs)
    w, _ = wilcoxon(diffs)
    return (s < ALPHA), s, w, (pos, neg)


def run(paired_path=PAIRED):
    """score P5 to P8 on a paired results file and print each verdict"""
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
        print(f"{len(void)} round(s) void of {len(rounds)}, dropped and counted:")
        for i, missing, unconfirmed in void:
            why = []
            if missing:
                why.append(f"no {', '.join(missing)}")
            if unconfirmed:
                why.append(f"no interference arrival confirmed for {', '.join(unconfirmed)}")
            print(f"  round {i}: {'; '.join(why)}")
    rounds = kept
    if not rounds:
        print("no usable rounds")
        return {}

    if len(rounds) < ROUNDS_REQUIRED:
        print(f"{paired_path}: {len(rounds)} rounds of {ROUNDS_REQUIRED} required")

    out = {}

    print("P5 -- in-loop detector cost and recall")
    cost = paired(rounds, "fixed-hi", "fixed-hi-probe", "compute_p50_ns")
    base = arm_median(rounds, "fixed-hi", "compute_p50_ns")
    frac = (median(cost) / base) if cost and base else None
    c_ok = frac is not None and abs(frac) < P5_COMPUTE_COST_TOL

    miss = paired(rounds, "fixed-hi", "fixed-hi-probe", "over_deadline")
    m_sep, m_p, _, m_ud = separates(miss)
    tail = paired(rounds, "fixed-hi", "fixed-hi-probe", "p999_ns")
    t_sep, t_p, _, _ = separates(tail)

    c_sep, c_p, c_w, c_ud = separates(cost)
    a = verdict("compute p50 rise under the bound",
                c_ok,
                (f"{100*frac:+.3f}% of tier 0 compute time (bound "
                 f"{100*P5_COMPUTE_COST_TOL:.0f}%); sign test p={c_p}, "
                 f"{c_ud[0]} up / {c_ud[1]} down, "
                 f"{'separates' if c_sep else 'does not separate'}")
                if frac is not None else "no paired rounds")
    b = verdict("deadline misses do not separate", not m_sep,
                f"sign test p={m_p}, {m_ud[0]} up / {m_ud[1]} down" if m_p is not None else "-")
    c = verdict("p99.9 does not separate", not t_sep,
                f"sign test p={t_p}" if t_p is not None else "-")

    rec = json.loads(IN_LOOP.read_text()).get("detector") if Path(IN_LOOP).exists() else None
    if rec is None:
        d = verdict("recall below gap_detector's, ratio in range", False,
                    "results/earlier_phase/frozen/inloop-frozen.json missing: UNTESTED, scored MISS")
    else:
        ratio = rec / GAP_DETECTOR_RECALL
        d = verdict("recall below gap_detector's, ratio in range",
                    rec < GAP_DETECTOR_RECALL and P5_RECALL_RATIO_LO <= ratio <= P5_RECALL_RATIO_HI,
                    f"in-loop {rec:.3f} vs gap_detector {GAP_DETECTOR_RECALL}, ratio {ratio:.3f} "
                    f"(bound [{P5_RECALL_RATIO_LO}, {P5_RECALL_RATIO_HI}])")
    out["P5"] = bool(a and b and c and d)

    print("\nP6 -- adaptive against fixed-hi-probe, deadlines")
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

    e = verdict("deadline misses fall and separate", bool(fell),
                f"sign test p={p}, {ud[0]} up / {ud[1]} down, wilcoxon {w}" if p is not None
                else "no paired rounds")
    f = verdict("fall by at least half", bool(halved),
                f"adaptive {ad}, probe arm {pr} (bound {P6_MISS_FACTOR})"
                if ad is not None and pr is not None else "-")
    g = verdict("p99.9 falls", bool(tail_fell),
                f"sign test p={t_p2}" if t_p2 is not None else "-")
    h = verdict("dropped frames do not rise", bool(drops_ok),
                f"sign test p={drop_p}" if drop_p is not None else "-")
    out["P6"] = bool(e and f and g and h)

    print("\nP7 -- adaptive quality")
    err = arm_median(rounds, "adaptive", "mean_relerr")
    full = arm_median(rounds, "adaptive", "full_quality_frac")
    i = verdict("mean relative error under the bound",
                err is not None and err < P7_MEAN_RELERR_MAX,
                f"{err} (bound {P7_MEAN_RELERR_MAX}, falsified above "
                f"{P7_MEAN_RELERR_FALSIFY})" if err is not None else "-")
    j = verdict("most frames complete at full quality",
                full is not None and full > P7_FULL_QUALITY_FRAC_MIN,
                f"{full} (bound {P7_FULL_QUALITY_FRAC_MIN})" if full is not None else "-")
    out["P7"] = bool(i and j)

    print("\nP8 -- adaptive against fixed-lo")
    a_miss = arm_median(rounds, "adaptive", "over_deadline_frac")
    l_miss = arm_median(rounds, "fixed-lo", "over_deadline_frac")
    l_err = arm_median(rounds, "fixed-lo", "mean_relerr")
    l_err = TIER3_RELERR if l_err is None else l_err

    misses_more = a_miss is not None and l_miss is not None and a_miss > l_miss
    better = err is not None and err <= P8_QUALITY_FACTOR * l_err

    verdict("adaptive misses more deadlines than fixed-lo",
            bool(misses_more),
            f"adaptive {a_miss} vs fixed-lo {l_miss}"
            if a_miss is not None and l_miss is not None else "-")
    verdict("adaptive error within bound of fixed-lo's", bool(better),
            f"adaptive {err} vs fixed-lo {l_err} "
            f"(bound {P8_QUALITY_FACTOR}x)" if err is not None else "-")
    dominated = misses_more and not better
    if dominated:
        print("        DOMINATED: fixed-lo better on both axes")
    out["P8"] = bool(misses_more and better)

    print("\nverdicts  " + "  ".join(
        f"{k2}={'HIT' if v else 'MISS'}" for k2, v in sorted(out.items())))
    return out


def _round(over_hi, over_probe, over_ad, over_lo, err_ad, err_lo, comp_hi, comp_probe,
           p999_hi=2_000_000, p999_probe=2_000_000, p999_ad=1_000_000, drop=0, rec=0.6,
           full=0.8):
    """build a round of the four arms"""
    def arm(od, comp, p999, mean_err=0.0, fq=1.0):
        """build an arm's result"""
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
    """write rounds to a temporary paired results file and return its path"""
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
    """score synthetic batches and check each verdict comes out as expected"""
    import contextlib
    import io
    failures = []

    import tempfile
    saved = IN_LOOP

    def score(recall=0.60, **over):
        """run the scorer with its output discarded"""
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
        """record a failure if a verdict differs from want"""
        got = score(**over)
        if got.get(key) is not want:
            failures.append(f"{name}: {key} came out {got.get(key)}, expected {want}")
            print(f"  DID NOT FIRE  {name}")
        else:
            print(f"  scored {key}={'HIT' if want else 'MISS'}  {name}")

    expect("a batch built to satisfy every prediction", "P5", True)
    expect("a batch built to satisfy every prediction", "P6", True)
    expect("a batch built to satisfy every prediction", "P7", True)
    expect("a batch built to satisfy every prediction", "P8", True)

    expect("a detector that costs 3% of compute", "P5", False, comp_probe=721_000)
    expect("a detector that recovers more than one owning a core", "P5", False, recall=0.99)
    expect("a detector whose recall was never measured", "P5", False, recall=None)
    expect("adaptation that does not reduce misses", "P6", False, over_ad=0.041)
    expect("adaptation that reduces misses but not by half", "P6", False, over_ad=0.030)
    expect("a loop that sheds on nearly every frame", "P7", False, err_ad=0.045)
    expect("a loop that sheds too rarely to have quality left over", "P7", False, err_ad=0.025)
    expect("always-shed dominating on both axes", "P8", False, err_ad=0.060)

    if failures:
        print("\nselftest: the scorer cannot score these:")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: every prediction can be scored HIT and MISS")
    return 0


MD_BEGIN = "<!-- ADAPT-VERDICTS:BEGIN -->"
MD_END = "<!-- ADAPT-VERDICTS:END -->"


def markdown(paired_path=PAIRED):
    """render the verdict block for the pre-registration doc"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*Not yet scored.*\n{MD_END}"
    titles = {
        "P5": "in-loop detector cost under bound, recall below gap_detector's",
        "P6": "adaptive misses fewer deadlines than fixed-hi-probe",
        "P7": "adaptive error and full-quality fraction within bounds",
        "P8": "adaptive not dominated by fixed-lo",
    }
    out = [MD_BEGIN, "", "| prediction | what it said | verdict |", "|---|---|---|"]
    for k in ("P5", "P6", "P7", "P8"):
        v = got.get(k)
        out.append(f"| **{k}** | {titles[k]} | **{'HIT' if v else 'MISS'}** |")
    out += ["", "*Scored by `benchmark/score/nested/quality_shedding.py`.*", "",
            "```", buf.getvalue().rstrip(), "```", "", MD_END]
    return "\n".join(out)


def write_markdown(doc, paired_path=PAIRED, check=False):
    """write the verdict block into a doc, or with check compare it"""
    from benchmark import lib
    lib.refuse_unpublishable_json(paired_path)
    p = Path(doc)
    if not p.is_absolute():
        p = ROOT / p
    text = p.read_text()
    block = markdown(paired_path)
    if MD_BEGIN not in text or MD_END not in text:
        raise SystemExit(f"{doc} has no {MD_BEGIN} / {MD_END} markers")
    head, rest = text.split(MD_BEGIN, 1)
    _, tail = rest.split(MD_END, 1)
    if check:
        current = MD_BEGIN + rest.split(MD_END, 1)[0] + MD_END
        if lib.renamed_translator(("path", "env", "flag", "label", "text"))(current).strip() != block.strip():
            raise SystemExit(
                f"{doc}: the verdict block differs from the scorer; regenerate with --markdown")
        return p
    p.write_text(head + block + tail)
    return p


def main():
    """parse arguments and run the selftest, the markdown writer or the scorer"""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paired", default=str(PAIRED))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--markdown", metavar="DOC", default=None,
                    help="rewrite the generated verdict block in DOC")
    ap.add_argument("--check", action="store_true",
                    help="with --markdown: exit nonzero if the block differs from the scorer, write nothing")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(a.paired, "--paired", "adapt")
    if a.markdown:
        p = write_markdown(a.markdown, a.paired, check=a.check)
        print(f"{'verdict block is current in' if a.check else 'rewrote the verdict block in'} {p}")
        return 0
    run(a.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
