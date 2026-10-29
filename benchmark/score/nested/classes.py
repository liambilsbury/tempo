#!/usr/bin/env python3
"""Score the class-sweep predictions P13 to P16 against a paired results file"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-classes-sweep.json"
HOG = "results/earlier_phase/paired/paired-feedback.json"

CLASSES = ["wake20k", "tlb"]
ARMS = ["fixed-hi-probe", "adaptive", "feedback", "fixed-lo"]
ROUNDS_REQUIRED = 16

HOG20_RATIO_PUBLISHED = 0.224
HOG20_ADAPTIVE_RELERR = 0.007103

P13_RATIO_MIN = 0.5
P13_VACUOUS_BELOW = 0.05
P14_RATIO_MIN = 0.4
P16_VACUOUS_WITHIN = 0.10


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


def arm_median(rounds, label, key):
    """return the median of an arm's metric over the rounds that have it"""
    v = [r[label][key] for r in rounds if label in r and key in r[label]]
    return median(v) if v else None


def ratio(rounds, cls, num, den, key="over_deadline_frac"):
    """return the numerator arm's median over the denominator arm's median"""
    a = arm_median(rounds, f"{cls}-{num}" if cls else num, key)
    b = arm_median(rounds, f"{cls}-{den}" if cls else den, key)
    if a is None or b is None or b == 0:
        return None
    return a / b


def run(paired_path=PAIRED, hog_path=HOG):
    """score P13 to P16 on a paired results file and print each verdict"""
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}

    labels = [f"{c}-{a}" for c in CLASSES for a in ARMS]
    kept, void = [], []
    for i, r in enumerate(rounds):
        missing = [l for l in labels if l not in r]
        unconfirmed = [l for l in labels if l in r and not r[l].get("hit")]
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
                why.append(f"no interference confirmed for {', '.join(unconfirmed)}")
            print(f"  round {i}: {'; '.join(why)}")
    rounds = kept
    if not rounds:
        print("no usable rounds")
        return {}
    if len(rounds) < ROUNDS_REQUIRED:
        print(f"{paired_path}: {len(rounds)} rounds of {ROUNDS_REQUIRED} required")

    out = {}
    r_wake = ratio(rounds, "wake20k", "adaptive", "fixed-hi-probe")
    r_tlb = ratio(rounds, "tlb", "adaptive", "fixed-hi-probe")

    print("P13 -- wake20k, adaptive against fixed-hi-probe")
    probe_wake = arm_median(rounds, "wake20k-fixed-hi-probe", "over_deadline_frac")
    if probe_wake is not None and probe_wake < P13_VACUOUS_BELOW:
        out["P13"] = None
        verdict("adaptation does not halve deadline misses under wake20k", None,
                f"VACUOUS: fixed-hi-probe over_deadline_frac {probe_wake}, below the "
                f"{P13_VACUOUS_BELOW} floor")
    else:
        ok = r_wake is not None and r_wake > P13_RATIO_MIN
        out["P13"] = verdict("adaptation does not halve deadline misses under wake20k", bool(ok),
                             f"ratio {r_wake} (bound above {P13_RATIO_MIN}); hog20 "
                             f"{HOG20_RATIO_PUBLISHED}")

    print("\nP14 -- tlb, adaptive against fixed-hi-probe")
    err_tlb = arm_median(rounds, "tlb-adaptive", "mean_relerr")
    a = verdict("adaptive/fixed-hi-probe ratio under tlb above bound",
                bool(r_tlb is not None and r_tlb > P14_RATIO_MIN),
                f"ratio {r_tlb} (bound above {P14_RATIO_MIN}); hog20 "
                f"{HOG20_RATIO_PUBLISHED}")
    b = verdict("adaptive mean relative error above hog20's",
                bool(err_tlb is not None and err_tlb > HOG20_ADAPTIVE_RELERR),
                f"mean relative error {err_tlb} vs hog20 {HOG20_ADAPTIVE_RELERR}")
    out["P14"] = bool(a and b)

    print("\nP15 -- adaptive/fixed-hi-probe ratio ordering across classes")
    hog_rounds = rounds_of(hog_path)
    r_hog = ratio(hog_rounds, None, "adaptive", "fixed-hi-probe") if hog_rounds else None
    if r_hog is None:
        r_hog = HOG20_RATIO_PUBLISHED
        hog_src = f"published {HOG20_RATIO_PUBLISHED} (five-arm batch missing)"
    else:
        hog_src = f"{r_hog} from the five-arm batch (between batches)"
    ok = (r_wake is not None and r_tlb is not None
          and r_wake > r_tlb > r_hog)
    out["P15"] = verdict("ratio(wake20k) > ratio(tlb) > ratio(hog20)", bool(ok),
                         f"wake20k {r_wake} > tlb {r_tlb} (within-round) > hog20 {hog_src}")

    print("\nP16 -- adaptive/feedback ratio, tlb against hog20")
    fb_tlb = ratio(rounds, "tlb", "adaptive", "feedback")
    fb_hog = ratio(hog_rounds, None, "adaptive", "feedback") if hog_rounds else None
    ad_tlb = arm_median(rounds, "tlb-adaptive", "over_deadline_frac")
    fbm_tlb = arm_median(rounds, "tlb-feedback", "over_deadline_frac")
    probe_tlb = arm_median(rounds, "tlb-fixed-hi-probe", "over_deadline_frac")
    idle = (probe_tlb and ad_tlb and fbm_tlb
            and abs(ad_tlb - probe_tlb) / probe_tlb < P16_VACUOUS_WITHIN
            and abs(fbm_tlb - probe_tlb) / probe_tlb < P16_VACUOUS_WITHIN)
    if idle:
        out["P16"] = None
        verdict("adaptive/feedback closer to 1 under tlb than under hog20", None,
                f"VACUOUS: both arms within {P16_VACUOUS_WITHIN} of fixed-hi-probe "
                f"({ad_tlb} and {fbm_tlb} vs {probe_tlb})")
    elif fb_hog is None:
        out["P16"] = None
        verdict("adaptive/feedback closer to 1 under tlb than under hog20", None,
                "VACUOUS: no five-arm hog20 batch with a feedback arm")
    else:
        ok = fb_tlb is not None and abs(fb_tlb - 1.0) < abs(fb_hog - 1.0)
        out["P16"] = verdict("adaptive/feedback closer to 1 under tlb than under hog20",
                             bool(ok),
                             f"tlb {fb_tlb} vs hog20 {fb_hog} (between batches)")

    print("\nverdicts  " + "  ".join(
        f"{k}={'HIT' if v else 'VACUOUS' if v is None else 'MISS'}"
        for k, v in sorted(out.items())))
    return out


def _arm(od, err=0.0, full=1.0):
    """build an arm's result"""
    return {"over_deadline_frac": od, "over_deadline": int(od * 1000), "mean_relerr": err,
            "full_quality_frac": full, "p999_ns": 2_000_000, "max_ns": 9_000_000,
            "compute_p50_ns": 700_000, "dropped": 0, "hit": 40000}


def _batch(n, wake_probe, wake_ad, tlb_probe, tlb_ad, tlb_fb, tlb_err, path_name):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    rounds = []
    for i in range(n):
        k = 1 + 0.01 * (i % 5)
        rounds.append({
            "wake20k-fixed-hi-probe": _arm(wake_probe * k),
            "wake20k-adaptive": _arm(wake_ad * k, 0.006),
            "wake20k-feedback": _arm(wake_ad * k * 1.2, 0.004),
            "wake20k-fixed-lo": _arm(0.001, 0.0786, 0.0),
            "tlb-fixed-hi-probe": _arm(tlb_probe * k),
            "tlb-adaptive": _arm(tlb_ad * k, tlb_err),
            "tlb-feedback": _arm(tlb_fb * k, tlb_err * 1.1),
            "tlb-fixed-lo": _arm(0.02, 0.0786, 0.0),
        })
    p = Path(tempfile.mkdtemp()) / path_name
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def _hog(n, ad, probe, fb):
    """write a temporary hog20 batch and return its path"""
    import tempfile
    rounds = [{"adaptive": _arm(ad, 0.0071), "fixed-hi-probe": _arm(probe),
               "feedback": _arm(fb, 0.009)} for _ in range(n)]
    p = Path(tempfile.mkdtemp()) / "hog.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


ALL_HIT_BATCH = dict(wake_probe=0.50, wake_ad=0.42, tlb_probe=0.50, tlb_ad=0.30, tlb_fb=0.33,
                     tlb_err=0.030)


def selftest():
    """score synthetic batches and check each verdict comes out as expected"""
    import contextlib
    import io
    failures = []

    def score(hog=(0.11, 0.50, 0.20), **over):
        """run the scorer with its output discarded"""
        kw = dict(ALL_HIT_BATCH)
        kw.update(over)
        p = _batch(16, path_name="sweep.json", **kw)
        h = _hog(16, *hog) if hog else Path("/nonexistent/hog.json")
        with contextlib.redirect_stdout(io.StringIO()):
            return run(p, h)

    def expect(name, key, want, **over):
        """record a failure if a verdict differs from want"""
        got = score(**over)
        if got.get(key) is not want:
            failures.append(f"{name}: {key} came out {got.get(key)}, expected {want}")
            print(f"  DID NOT FIRE  {name}")
        else:
            tag = {True: "HIT", False: "MISS", None: "VACUOUS"}[want]
            print(f"  scored {key}={tag}  {name}")

    for k in ("P13", "P14", "P15", "P16"):
        expect("a batch built to satisfy every prediction", k, True)

    expect("adaptation halves misses under an IPI class too", "P13", False, wake_ad=0.20)
    expect("a class the comparator holds its deadlines under", "P13", None,
           wake_probe=0.02, wake_ad=0.01)

    expect("adaptation works as well under tlb as under hog", "P14", False, tlb_ad=0.15)
    expect("tlb sheds no harder than hog20 did", "P14", False, tlb_err=0.005)

    expect("tlb recovers better than wake20k", "P15", False, tlb_ad=0.42, wake_ad=0.30)

    expect("intra-frame's advantage widens under tlb", "P16", False, tlb_fb=0.60)
    expect("neither arm sheds usefully under tlb", "P16", None,
           tlb_ad=0.49, tlb_fb=0.49, tlb_err=0.030)
    expect("no hog20 batch to compare against", "P16", None, hog=None)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: every prediction scored HIT, MISS and VACUOUS as built")
    return 0


MD_BEGIN = "<!-- CLASSES-VERDICTS:BEGIN -->"
MD_END = "<!-- CLASSES-VERDICTS:END -->"

TITLES = {
    "P13": "adaptive does not halve deadline misses under wake20k",
    "P14": "under tlb, adaptive recovers less and has higher error than under hog20",
    "P15": "ratio(wake20k) > ratio(tlb) > ratio(hog20)",
    "P16": "adaptive/feedback closer to 1 under tlb than under hog20",
}


def markdown(paired_path=PAIRED, hog_path=HOG):
    """render the verdict block for the pre-registration doc"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path, hog_path)
    if not got:
        return f"{MD_BEGIN}\n*Not yet scored.*\n{MD_END}"
    tag = {True: "HIT", False: "MISS", None: "VACUOUS"}
    out = [MD_BEGIN, "", "| prediction | what it said | verdict |", "|---|---|---|"]
    for k in ("P13", "P14", "P15", "P16"):
        out.append(f"| **{k}** | {TITLES[k]} | **{tag[got.get(k)]}** |")
    out += ["", "*Scored by `benchmark/score/nested/classes.py`.*",
            "", "```", buf.getvalue().rstrip(), "```", "", MD_END]
    return "\n".join(out)


def write_markdown(doc, paired_path=PAIRED, hog_path=HOG, check=False):
    """write the verdict block into a doc, or with check compare it"""
    from benchmark import lib
    lib.refuse_unpublishable_json(paired_path)
    lib.refuse_unpublishable_json(hog_path)
    p = Path(doc)
    if not p.is_absolute():
        p = ROOT / p
    text = p.read_text()
    block = markdown(paired_path, hog_path)
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
    ap.add_argument("--hog", default=str(HOG))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--markdown", metavar="DOC", default=None,
                    help="rewrite the generated verdict block in DOC")
    ap.add_argument("--check", action="store_true",
                    help="with --markdown: exit nonzero if the block differs from the scorer, write nothing")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(args.paired, "--paired", "classes")
    lib.need_input(args.hog, "--hog", "classes")
    if args.markdown:
        write_markdown(args.markdown, args.paired, args.hog, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired, args.hog)
    return 0


if __name__ == "__main__":
    sys.exit(main())
