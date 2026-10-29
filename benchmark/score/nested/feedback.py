#!/usr/bin/env python3
"""Score the intra-frame against inter-frame predictions P9 to P12 on a paired results file"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-feedback.json"

ARMS = ["fixed-hi", "fixed-hi-probe", "adaptive", "feedback", "fixed-lo"]
ROUNDS_REQUIRED = 16
ALPHA = 0.05

P10_MISS_FACTOR = 0.8

P11_QUALITY_TOL = 1.2
P11_QUALITY_FALSIFY = 0.8


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
    """score P9 to P12 on a paired results file and print each verdict"""
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

    print("P9 -- feedback against fixed-hi-probe, deadlines")
    d9 = paired(rounds, "fixed-hi-probe", "feedback", "over_deadline_frac")
    sep9, p9, w9, ud9 = separates(d9)
    fell9 = sep9 and median(d9) is not None and median(d9) < 0
    verdict("feedback deadline misses fall against fixed-hi-probe and separate",
            bool(fell9),
            f"median {median(d9)}, sign test p={p9}, {ud9[0]} up / {ud9[1]} down, "
            f"wilcoxon {w9}" if p9 is not None else "no paired rounds")
    if sep9 and median(d9) is not None and median(d9) > 0:
        print("        WRONG DIRECTION: feedback misses more than fixed-hi-probe")
    out["P9"] = bool(fell9)

    print("\nP10 -- adaptive against feedback, deadlines")
    d10 = paired(rounds, "feedback", "adaptive", "over_deadline_frac")
    sep10, p10, w10, ud10 = separates(d10)
    fell10 = sep10 and median(d10) is not None and median(d10) < 0
    ad = arm_median(rounds, "adaptive", "over_deadline_frac")
    fb = arm_median(rounds, "feedback", "over_deadline_frac")
    ratio_ok = ad is not None and fb is not None and fb > 0 and ad <= P10_MISS_FACTOR * fb

    verdict("adaptive misses fewer deadlines than feedback, and separates", bool(fell10),
            f"median {median(d10)}, sign test p={p10}, {ud10[0]} up / {ud10[1]} down, "
            f"wilcoxon {w10}" if p10 is not None else "no paired rounds")
    verdict("by at least a fifth", bool(ratio_ok),
            f"adaptive {ad} vs feedback {fb} (bound {P10_MISS_FACTOR}x)"
            if ad is not None and fb is not None else "-")
    if sep10 and median(d10) is not None and median(d10) > 0:
        print("        INTER-FRAME CONTROLLER WON on deadlines")
    out["P10"] = bool(fell10 and ratio_ok)

    print("\nP11 -- adaptive against feedback, quality")
    a_err = arm_median(rounds, "adaptive", "mean_relerr")
    f_err = arm_median(rounds, "feedback", "mean_relerr")
    not_worse = (a_err is not None and f_err is not None
                 and a_err <= P11_QUALITY_TOL * f_err)
    falsified = (a_err is not None and f_err is not None and a_err > 0
                 and f_err < P11_QUALITY_FALSIFY * a_err)
    d = verdict("adaptive mean relative error within bound of feedback's",
                bool(not_worse and not falsified),
                f"adaptive {a_err} vs feedback {f_err} (bound {P11_QUALITY_TOL}x, "
                f"falsified below {P11_QUALITY_FALSIFY}x)"
                if a_err is not None and f_err is not None else "-")
    if falsified:
        print("        FALSIFIED: feedback error below the falsify bound")
    out["P11"] = bool(d)

    print("\nP12 -- full-quality frame fraction, feedback against adaptive")
    d12 = paired(rounds, "adaptive", "feedback", "full_quality_frac")
    sep12, p12, w12, ud12 = separates(d12)
    higher = sep12 and median(d12) is not None and median(d12) > 0
    f_full = arm_median(rounds, "feedback", "full_quality_frac")
    a_full = arm_median(rounds, "adaptive", "full_quality_frac")
    verdict("feedback full-quality fraction higher and separates", bool(higher),
            f"feedback {f_full} vs adaptive {a_full}; median difference {median(d12)}, "
            f"sign test p={p12}, {ud12[0]} up / {ud12[1]} down, wilcoxon {w12}"
            if p12 is not None else "no paired rounds")
    out["P12"] = bool(higher)

    print("\nverdicts  " + "  ".join(
        f"{k}={'HIT' if v else 'MISS'}" for k, v in sorted(out.items())))
    return out


def _arm(od, err=0.0, full=1.0, comp=700_000, p999=2_000_000, drop=0):
    """build an arm's result"""
    return {"over_deadline": int(od * 1000), "over_deadline_frac": od,
            "compute_p50_ns": comp, "p999_ns": p999, "dropped": drop,
            "mean_relerr": err, "full_quality_frac": full,
            "hit": 40000}


def _round(over_probe, over_ad, over_fb, over_lo, err_ad, err_fb, full_ad, full_fb):
    """build a round of the five arms"""
    return {
        "fixed-hi": _arm(over_probe * 1.05),
        "fixed-hi-probe": _arm(over_probe),
        "adaptive": _arm(over_ad, err_ad, full_ad, p999=1_000_000),
        "feedback": _arm(over_fb, err_fb, full_fb, p999=1_200_000),
        "fixed-lo": _arm(over_lo, 0.0786, 0.0, comp=140_000, p999=900_000),
    }


def _batch(n=16, **kw):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    rounds = []
    for i in range(n):
        d = dict(kw)
        for off, k in enumerate(("over_probe", "over_ad", "over_fb", "over_lo")):
            d[k] = kw[k] * (1 + 0.01 * ((i + off) % 5))
        rounds.append(_round(**d))
    p = Path(tempfile.mkdtemp()) / "feedback.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


ALL_HIT_BATCH = dict(over_probe=0.50, over_ad=0.11, over_fb=0.20, over_lo=0.001,
                     err_ad=0.0071, err_fb=0.0090, full_ad=0.44, full_fb=0.70)


def selftest():
    """score synthetic batches and check each verdict comes out as expected"""
    import contextlib
    import io
    failures = []

    def score(**over):
        """run the scorer with its output discarded"""
        kw = dict(ALL_HIT_BATCH)
        kw.update(over)
        with contextlib.redirect_stdout(io.StringIO()):
            return run(_batch(**kw))

    def expect(name, key, want, **over):
        """record a failure if a verdict differs from want"""
        got = score(**over)
        if got.get(key) is not want:
            failures.append(f"{name}: {key} came out {got.get(key)}, expected {want}")
            print(f"  DID NOT FIRE  {name}")
        else:
            print(f"  scored {key}={'HIT' if want else 'MISS'}  {name}")

    for k in ("P9", "P10", "P11", "P12"):
        expect("a batch built to satisfy every prediction", k, True)

    expect("feedback no better than fixed-hi-probe", "P9", False, over_fb=0.50)

    expect("the inter-frame controller beats intra-frame", "P10", False,
           over_ad=0.20, over_fb=0.11)
    expect("the two arms do not separate on deadlines", "P10", False, over_fb=0.11)
    expect("adaptive wins but by less than a fifth", "P10", False, over_fb=0.13)

    expect("adaptive's quality materially worse than feedback's", "P11", False, err_ad=0.020)
    expect("feedback delivers materially better quality", "P11", False, err_fb=0.0050)

    expect("feedback leaves no more frames at full quality", "P12", False, full_fb=0.30)
    expect("full-quality fractions that do not separate", "P12", False, full_fb=0.44)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: every prediction scored HIT and MISS as built")
    return 0


MD_BEGIN = "<!-- FEEDBACK-VERDICTS:BEGIN -->"
MD_END = "<!-- FEEDBACK-VERDICTS:END -->"

TITLES = {
    "P9": "feedback misses fewer deadlines than fixed-hi-probe",
    "P10": "adaptive misses fewer deadlines than feedback",
    "P11": "adaptive error within bound of feedback's",
    "P12": "feedback leaves more frames at full quality",
}


def markdown(paired_path=PAIRED):
    """render the verdict block for the pre-registration doc"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*Not yet scored.*\n{MD_END}"
    out = [MD_BEGIN, "", "| prediction | what it said | verdict |", "|---|---|---|"]
    for k in ("P9", "P10", "P11", "P12"):
        out.append(f"| **{k}** | {TITLES[k]} | **{'HIT' if got.get(k) else 'MISS'}** |")
    out += ["", "*Scored by `benchmark/score/nested/feedback.py`.*", "",
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--paired", default=str(PAIRED))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--markdown", metavar="DOC", default=None,
                    help="rewrite the generated verdict block in DOC")
    ap.add_argument("--check", action="store_true",
                    help="with --markdown: exit nonzero if the block differs from the scorer, write nothing")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(args.paired, "--paired", "feedback")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
