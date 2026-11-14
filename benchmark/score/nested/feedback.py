#!/usr/bin/env python3
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

    print("P9 feedback/probe deadlines")
    d9 = paired(rounds, "fixed-hi-probe", "feedback", "over_deadline_frac")
    sep9, p9, w9, ud9 = separates(d9)
    fell9 = sep9 and median(d9) is not None and median(d9) < 0
    verdict("misses fall", bool(fell9),
            f"median {median(d9)} sign p={p9} {ud9[0]}+/{ud9[1]}- w {w9}" if p9 is not None else "no pairs")
    if sep9 and median(d9) is not None and median(d9) > 0:
        print("        WRONG DIRECTION")
    out["P9"] = bool(fell9)

    print("\nP10 adaptive/feedback deadlines")
    d10 = paired(rounds, "feedback", "adaptive", "over_deadline_frac")
    sep10, p10, w10, ud10 = separates(d10)
    fell10 = sep10 and median(d10) is not None and median(d10) < 0
    ad = arm_median(rounds, "adaptive", "over_deadline_frac")
    fb = arm_median(rounds, "feedback", "over_deadline_frac")
    ratio_ok = ad is not None and fb is not None and fb > 0 and ad <= P10_MISS_FACTOR * fb

    verdict("misses fall", bool(fell10),
            f"median {median(d10)} sign p={p10} {ud10[0]}+/{ud10[1]}- w {w10}" if p10 is not None else "no pairs")
    verdict("ratio", bool(ratio_ok),
            f"adaptive {ad} feedback {fb} x{P10_MISS_FACTOR}"
            if ad is not None and fb is not None else "-")
    out["P10"] = bool(fell10 and ratio_ok)

    print("\nP11 adaptive/feedback quality")
    a_err = arm_median(rounds, "adaptive", "mean_relerr")
    f_err = arm_median(rounds, "feedback", "mean_relerr")
    not_worse = (a_err is not None and f_err is not None
                 and a_err <= P11_QUALITY_TOL * f_err)
    falsified = (a_err is not None and f_err is not None and a_err > 0
                 and f_err < P11_QUALITY_FALSIFY * a_err)
    d = verdict("relerr",
                bool(not_worse and not falsified),
                f"adaptive {a_err} feedback {f_err} x{P11_QUALITY_TOL} falsify x{P11_QUALITY_FALSIFY}"
                if a_err is not None and f_err is not None else "-")
    if falsified:
        print("        FALSIFIED")
    out["P11"] = bool(d)

    print("\nP12 full quality feedback/adaptive")
    d12 = paired(rounds, "adaptive", "feedback", "full_quality_frac")
    sep12, p12, w12, ud12 = separates(d12)
    higher = sep12 and median(d12) is not None and median(d12) > 0
    f_full = arm_median(rounds, "feedback", "full_quality_frac")
    a_full = arm_median(rounds, "adaptive", "full_quality_frac")
    verdict("higher", bool(higher),
            f"feedback {f_full} adaptive {a_full} d={median(d12)} sign p={p12} {ud12[0]}+/{ud12[1]}- w {w12}"
            if p12 is not None else "no pairs")
    out["P12"] = bool(higher)

    print("\nverdicts  " + "  ".join(
        f"{k}={'HIT' if v else 'MISS'}" for k, v in sorted(out.items())))
    return out


def _arm(od, err=0.0, full=1.0, comp=700_000, p999=2_000_000, drop=0):
    return {"over_deadline": int(od * 1000), "over_deadline_frac": od,
            "compute_p50_ns": comp, "p999_ns": p999, "dropped": drop,
            "mean_relerr": err, "full_quality_frac": full,
            "hit": 40000}


def _round(over_probe, over_ad, over_fb, over_lo, err_ad, err_fb, full_ad, full_fb):
    return {
        "fixed-hi": _arm(over_probe * 1.05),
        "fixed-hi-probe": _arm(over_probe),
        "adaptive": _arm(over_ad, err_ad, full_ad, p999=1_000_000),
        "feedback": _arm(over_fb, err_fb, full_fb, p999=1_200_000),
        "fixed-lo": _arm(over_lo, 0.0786, 0.0, comp=140_000, p999=900_000),
    }


def _batch(n=16, **kw):
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
    import contextlib
    import io
    failures = []

    def score(**over):
        kw = dict(ALL_HIT_BATCH)
        kw.update(over)
        with contextlib.redirect_stdout(io.StringIO()):
            return run(_batch(**kw))

    def expect(name, key, want, **over):
        got = score(**over)
        if got.get(key) is not want:
            failures.append(f"{name}: {key}={got.get(key)} want {want}")
            print(f"  FAIL  {name}")
        else:
            print(f"  ok  {key}={'HIT' if want else 'MISS'}  {name}")

    for k in ("P9", "P10", "P11", "P12"):
        expect("all HIT", k, True)

    expect("feedback = probe", "P9", False, over_fb=0.50)

    expect("feedback < adaptive", "P10", False,
           over_ad=0.20, over_fb=0.11)
    expect("no separation", "P10", False, over_fb=0.11)
    expect("adaptive margin < 0.2", "P10", False, over_fb=0.13)

    expect("adaptive relerr high", "P11", False, err_ad=0.020)
    expect("feedback relerr low", "P11", False, err_fb=0.0050)

    expect("feedback full lower", "P12", False, full_fb=0.30)
    expect("full no separation", "P12", False, full_fb=0.44)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


MD_BEGIN = "<!-- FEEDBACK-VERDICTS:BEGIN -->"
MD_END = "<!-- FEEDBACK-VERDICTS:END -->"

TITLES = {
    "P9": "od feedback < fixed-hi-probe",
    "P10": "od adaptive < feedback",
    "P11": "relerr adaptive <= bound x feedback",
    "P12": "full_quality_frac feedback > adaptive",
}


def markdown(paired_path=PAIRED):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*unscored*\n{MD_END}"
    out = [MD_BEGIN, "", "| prediction | claim | verdict |", "|---|---|---|"]
    for k in ("P9", "P10", "P11", "P12"):
        out.append(f"| **{k}** | {TITLES[k]} | **{'HIT' if got.get(k) else 'MISS'}** |")
    out += ["", "*`benchmark/score/nested/feedback.py`*", "",
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
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(args.paired, "--paired", "feedback")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: {'ok' if args.check else 'written'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
