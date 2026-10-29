#!/usr/bin/env python3
"""Score the anchor batch's P21 verdict and its P10/P17 adjudication"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-anchor.json"

ARMS = ["fixed-hi-probe", "adaptive", "feedback"]
ROUNDS_REQUIRED = 20
ALPHA = 0.05

VACUOUS_BELOW = 0.05

P10_MEDIAN_P999_MS = 0.080
INCONCLUSIVE_FRACTION = 0.5
INCONCLUSIVE_BOUND_MS = P10_MEDIAN_P999_MS * INCONCLUSIVE_FRACTION


def verdict(name, state, detail):
    """print a prediction's verdict line and return its state"""
    tag = {True: "HIT ", False: "MISS", None: "VOID"}[state]
    print(f"  {tag}  {name}\n        {detail}")
    return state


def rounds_of(path):
    """load the rounds of a paired results file, or None if it is missing"""
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return None
    return json.loads(p.read_text())["rounds"]


def arm_median(rounds, arm, key):
    """return the median of an arm's metric over the rounds that have it"""
    v = [r[arm][key] for r in rounds if arm in r and key in r[arm]]
    return median(v) if v else None


def paired(rounds, a, b, key):
    """return b minus a per round, for rounds where both arms have the metric"""
    return [r[b][key] - r[a][key]
            for r in rounds if a in r and b in r and key in r[a] and key in r[b]]


def run(paired_path=PAIRED):
    """score P21 and the adjudication on a paired results file and print each verdict"""
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

    probe_od = arm_median(rounds, "fixed-hi-probe", "over_deadline_frac")
    if probe_od is not None and probe_od < VACUOUS_BELOW:
        out["P21"] = None
        out["ADJUDICATION"] = None
        print("P21 -- adaptive against feedback, p99.9")
        verdict("adaptive and feedback separate on p99.9", None,
                f"VACUOUS: fixed-hi-probe over_deadline_frac {probe_od}, below the "
                f"{VACUOUS_BELOW} floor")
        print("\nADJUDICATION: VACUOUS")
        print("\nverdicts  ADJUDICATION=VACUOUS  P21=VACUOUS")
        return out

    d999 = paired(rounds, "adaptive", "feedback", "p999_ns")
    dod = paired(rounds, "adaptive", "feedback", "over_deadline_frac")

    w999, _ = wilcoxon(d999)
    s999, pos999, neg999 = sign_test(d999)
    m999_ns = median(d999) if d999 else None
    m999_ms = m999_ns / 1e6 if m999_ns is not None else None
    sep999 = w999 < ALPHA

    wod, _ = wilcoxon(dod)
    sod, posod, negod = sign_test(dod)
    mod_ = median(dod) if dod else None

    print("P21 -- adaptive against feedback, p99.9")
    out["P21"] = verdict(
        "p99.9 does not separate at p<0.05 by Wilcoxon",
        bool(not sep999),
        f"wilcoxon p={w999} ({pos999} up / {neg999} down by sign, median {m999_ms:+.4f} ms "
        f"feedback-adaptive)")

    print(f"\n  secondary: over_deadline_frac  sign p={sod} ({posod} up / {negod} "
          f"down)  wilcoxon p={wod}  median {mod_:+.6f} feedback-adaptive")

    print("\nADJUDICATION -- P10/P17")
    if sep999:
        if m999_ns is not None and m999_ns > 0:
            out["ADJUDICATION"] = "SEPARATES-ADAPTIVE"
            print("  RESOLVED -- separates favouring adaptive")
        else:
            out["ADJUDICATION"] = "SEPARATES-FEEDBACK"
            print("  RESOLVED -- separates favouring feedback")
    else:
        if m999_ms is not None and abs(m999_ms) < INCONCLUSIVE_BOUND_MS:
            out["ADJUDICATION"] = "RESOLVED-TIE"
            print(f"  RESOLVED -- tie at n={len(rounds)}: median difference {m999_ms:+.4f} ms, "
                  f"under the {INCONCLUSIVE_BOUND_MS} ms bound (P10 median {P10_MEDIAN_P999_MS} ms)")
        else:
            out["ADJUDICATION"] = "INCONCLUSIVE-AT-THIS-POWER"
            print(f"  INCONCLUSIVE AT THIS POWER: wilcoxon p={w999}, median difference "
                  f"{m999_ms:+.4f} ms, at or above the {INCONCLUSIVE_BOUND_MS} ms bound "
                  f"(P10 median {P10_MEDIAN_P999_MS} ms), n={len(rounds)}")

    print("\nverdicts  ADJUDICATION=" + str(out["ADJUDICATION"]) + "  P21="
          + ("HIT" if out["P21"] else "MISS" if out["P21"] is False else "VACUOUS"))
    return out


def _arm(od, p999=1_000_000, err=0.0):
    """build an arm's result"""
    return {"over_deadline_frac": od, "p999_ns": p999, "max_ns": p999 * 2, "mean_relerr": err,
            "hit": 40000}


def _round(k, ad999=1_400_000, gap=0, probe_od=0.5, err=0.007):
    """build a round, with adaptive's p999 scaled by k and feedback's offset by gap ns"""
    r = {}
    r["fixed-hi-probe"] = _arm(probe_od * k, p999=2_000_000)
    a999 = ad999 * k
    f999 = a999 + gap
    r["adaptive"] = _arm(0.12 * k, p999=a999, err=err)
    r["feedback"] = _arm(0.12 * k, p999=f999, err=err * 1.05)
    return r


def _batch(n, gap=0, probe_od=0.5, ad999=1_400_000, err=0.007):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    rounds = []
    for i in range(n):
        k = 1 + 0.01 * (i % 5)
        rounds.append(_round(k, ad999=ad999, gap=gap, probe_od=probe_od, err=err))
    p = Path(tempfile.mkdtemp()) / "anchor.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def _tie_batch(n):
    """write a batch of alternating small gaps under the inconclusive bound"""
    import tempfile
    rounds = []
    for i in range(n):
        k = 1 + 0.01 * (i % 5)
        gap = 10_000 if i % 2 == 0 else -10_000
        rounds.append(_round(k, gap=gap))
    p = Path(tempfile.mkdtemp()) / "anchor.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


_P10_DIFFS_NS = [210753, -309828, 14359, 96847, 63071, 174483, -201433, 171562, 205826, 155489,
                 58128, 286319, -20478, 360327, 29511, -40275]


def _inconclusive_batch(n=16):
    """write a batch from P10's differences, just above the inconclusive bound"""
    import tempfile
    rounds = []
    for i, gap in enumerate(_P10_DIFFS_NS[:n]):
        k = 1 + 0.01 * (i % 5)
        rounds.append(_round(k, gap=gap))
    p = Path(tempfile.mkdtemp()) / "anchor.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def selftest():
    """score synthetic batches and check each verdict comes out as expected"""
    import contextlib
    import io
    failures = []

    def score(builder, *args, **kw):
        """run the scorer with its output discarded"""
        p = builder(*args, **kw)
        with contextlib.redirect_stdout(io.StringIO()):
            return run(p)

    def expect(name, key, want, builder, *args, **kw):
        """record a failure if a verdict differs from want"""
        got = score(builder, *args, **kw)
        if got.get(key) != want:
            failures.append(f"{name}: {key} came out {got.get(key)!r}, expected {want!r}")
            print(f"  DID NOT FIRE  {name}")
        else:
            print(f"  scored {key}={want!r}  {name}")

    expect("a tight, balanced tie", "P21", True, _tie_batch, 20)
    expect("a tight, balanced tie", "ADJUDICATION", "RESOLVED-TIE", _tie_batch, 20)

    expect("P10's own real batch, re-scored under this batch's stricter bound", "P21", True,
           _inconclusive_batch, 16)
    expect("P10's own real batch, re-scored under this batch's stricter bound", "ADJUDICATION",
           "INCONCLUSIVE-AT-THIS-POWER", _inconclusive_batch, 16)

    expect("separates favouring adaptive", "P21", False, _batch, 20, gap=300_000)
    expect("separates favouring adaptive", "ADJUDICATION", "SEPARATES-ADAPTIVE", _batch, 20,
           gap=300_000)

    expect("separates favouring feedback", "P21", False, _batch, 20, gap=-300_000)
    expect("separates favouring feedback", "ADJUDICATION", "SEPARATES-FEEDBACK", _batch, 20,
           gap=-300_000)

    expect("the comparator holds its own deadlines", "P21", None, _batch, 20, gap=0,
           probe_od=0.01)
    expect("the comparator holds its own deadlines", "ADJUDICATION", None, _batch, 20, gap=0,
           probe_od=0.01)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: P21 scored HIT, MISS and VACUOUS, and ADJUDICATION each outcome, as built")
    return 0


MD_BEGIN = "<!-- ANCHOR-VERDICTS:BEGIN -->"
MD_END = "<!-- ANCHOR-VERDICTS:END -->"


def markdown(paired_path=PAIRED):
    """render the verdict block for the pre-registration doc"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*Not yet scored.*\n{MD_END}"
    tag = {True: "HIT", False: "MISS", None: "VACUOUS"}
    out = [MD_BEGIN, "", "| prediction | what it said | verdict |", "|---|---|---|",
           f"| **P21** | adaptive and feedback do not separate on p99.9 "
           f"| **{tag[got.get('P21')]}** |",
           f"| **ADJUDICATION** | P10/P17 | "
           f"**{got.get('ADJUDICATION')}** |"]
    out += ["", "*Scored by `benchmark/score/nested/anchor.py`.*",
            "", "```", buf.getvalue().rstrip(), "```", "", MD_END]
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
    lib.need_input(args.paired, "--paired", "anchor")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
