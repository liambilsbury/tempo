#!/usr/bin/env python3
"""Score the frequency-sweep predictions P17 to P20 against a paired results file"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-freqsweep.json"

SLOTS = [125, 500, 1000, 2000, 4000]
BELOW = [125, 500]
ABOVE = [2000, 4000]
ARMS = ["fixed-hi-probe", "adaptive", "feedback"]
ROUNDS_REQUIRED = 16
ALPHA = 0.05

VACUOUS_BELOW = 0.05

P19_QUALITY_TOL = 1.2

P20_MISS_FACTOR = 0.8


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


def label(slot, arm):
    """return an arm's label at a slot"""
    return f"s{slot}-{arm}"


def slot_vacuous(rounds, slot):
    """return whether a slot's comparator holds its deadlines, and its miss fraction"""
    probe = arm_median(rounds, label(slot, "fixed-hi-probe"), "over_deadline_frac")
    return probe is not None and probe < VACUOUS_BELOW, probe


def run(paired_path=PAIRED):
    """score P17 to P20 on a paired results file and print each verdict"""
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}

    labels = [label(s, a) for s in SLOTS for a in ARMS]
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
                why.append(f"no interference arrival confirmed for {', '.join(unconfirmed)}")
            print(f"  round {i}: {'; '.join(why)}")
    rounds = kept
    if not rounds:
        print("no usable rounds")
        return {}
    if len(rounds) < ROUNDS_REQUIRED:
        print(f"{paired_path}: {len(rounds)} rounds of {ROUNDS_REQUIRED} required")

    out = {}

    print("P17 -- adaptive against feedback at slot_us=1000")
    od = paired(rounds, label(1000, "adaptive"), label(1000, "feedback"), "over_deadline_frac")
    p999 = paired(rounds, label(1000, "adaptive"), label(1000, "feedback"), "p999_ns")
    od_sep, od_p, od_w, od_c = separates(od)
    p999_sep, p999_p, p999_w, p999_c = separates(p999)
    ok = (not od_sep) and (not p999_sep)
    out["P17"] = verdict("slot_us=1000 does not separate on misses or on p99.9", bool(ok),
                         f"over_deadline sign p={od_p} {od_c}; p99.9 wilcoxon p={p999_w} {p999_c}")

    print("\nP18 -- adaptive against feedback below the period, p99.9")
    p18_bits = []
    for slot in BELOW:
        vac, probe = slot_vacuous(rounds, slot)
        if vac:
            p18_bits.append(None)
            print(f"  slot_us={slot}: VACUOUS -- fixed-hi-probe over_deadline_frac {probe}, "
                  f"below the {VACUOUS_BELOW} floor")
            continue
        d = paired(rounds, label(slot, "adaptive"), label(slot, "feedback"), "p999_ns")
        sep, p, w, c = separates(d)
        p18_bits.append(not sep)
        print(f"  slot_us={slot}: {'MISS -- separates' if sep else 'holds -- does not separate'} "
              f"on p99.9, wilcoxon p={w} {c}")
    live = [b for b in p18_bits if b is not None]
    out["P18"] = None if not live else bool(all(live))
    verdict("neither sub-period slot separates on p99.9", out["P18"],
            f"per-slot: {dict(zip(BELOW, p18_bits))}")

    print("\nP19 -- adaptive against feedback above the period, p99.9")
    p19_diffs = {}
    p19_vac = {}
    for slot in ABOVE:
        vac, probe = slot_vacuous(rounds, slot)
        p19_vac[slot] = vac
        if vac:
            print(f"  slot_us={slot}: VACUOUS -- fixed-hi-probe over_deadline_frac {probe}, "
                  f"below the {VACUOUS_BELOW} floor")
            continue
        d = paired(rounds, label(slot, "adaptive"), label(slot, "feedback"), "p999_ns")
        p19_diffs[slot] = d
        sep, p, w, c = separates(d)
        m = median(d)
        print(f"  slot_us={slot}: median (feedback-adaptive)={m}, wilcoxon p={w} {c}, "
              f"{'separates' if sep else 'does not separate'}")
    if any(p19_vac.values()):
        out["P19"] = None
        verdict("both above-period slots separate favouring adaptive, margin grows with slot",
                None, f"VACUOUS at {[s for s in ABOVE if p19_vac[s]]}")
    else:
        seps = {s: separates(p19_diffs[s]) for s in ABOVE}
        both_sep = all(seps[s][0] and median(p19_diffs[s]) > 0 for s in ABOVE)
        mags = {s: median(p19_diffs[s]) for s in ABOVE}
        grows = mags[4000] > mags[2000] if len(ABOVE) == 2 else None
        ok = bool(both_sep and grows)
        out["P19"] = verdict("both above-period slots separate favouring adaptive, "
                             f"margin at {ABOVE[-1]} above {ABOVE[0]}", ok,
                             f"medians {mags}, both separate favouring adaptive: {both_sep}, "
                             f"grows: {grows}")
        a_err = arm_median(rounds, label(4000, "adaptive"), "mean_relerr")
        f_err = arm_median(rounds, label(4000, "feedback"), "mean_relerr")
        if a_err is not None and f_err is not None and f_err > 0:
            q_ok = a_err <= P19_QUALITY_TOL * f_err
            print(f"  secondary: mean relative error at 4000, adaptive {a_err} vs "
                  f"feedback {f_err} (bound {P19_QUALITY_TOL}x) -- "
                  f"{'holds' if q_ok else 'MISSES'}")

    print("\nP20 -- adaptive against feedback above the period, deadlines")
    p20_bits = []
    for slot in ABOVE:
        if p19_vac.get(slot):
            p20_bits.append(None)
            continue
        d = paired(rounds, label(slot, "adaptive"), label(slot, "feedback"), "over_deadline_frac")
        sep, p, w, c = separates(d)
        ad = arm_median(rounds, label(slot, "adaptive"), "over_deadline_frac")
        fb = arm_median(rounds, label(slot, "feedback"), "over_deadline_frac")
        cleared = sep and ad is not None and fb not in (None, 0) and ad < P20_MISS_FACTOR * fb
        p20_bits.append(not cleared)
        print(f"  slot_us={slot}: adaptive {ad} vs feedback {fb}, separates={sep}, "
              f"clears {P20_MISS_FACTOR}x bound={cleared} -- "
              f"{'MISS' if cleared else 'holds'}")
    live20 = [b for b in p20_bits if b is not None]
    out["P20"] = None if not live20 else bool(all(live20))
    verdict("misses do not also separate and clear the bound at either above-period slot",
            out["P20"], f"per-slot: {dict(zip(ABOVE, p20_bits))}")

    print("\nverdicts  " + "  ".join(
        f"{k}={'HIT' if v else 'VACUOUS' if v is None else 'MISS'}"
        for k, v in sorted(out.items())))
    return out


def _arm(od, p999=1_000_000, err=0.0):
    """build an arm's result"""
    return {"over_deadline_frac": od, "p999_ns": p999, "max_ns": p999 * 2, "mean_relerr": err,
            "hit": 40000}


def _round(k, tie999=1_400_000, below_gap=0, above_gap=0, anchor_gap=0, probe_od=0.5, err=0.007):
    """build a round, with feedback's p999 above adaptive's by each gap in ns"""
    r = {}
    for slot in SLOTS:
        probe = probe_od * k
        r[label(slot, "fixed-hi-probe")] = _arm(probe, p999=2_000_000)
        if slot == 1000:
            gap = anchor_gap
        elif slot in BELOW:
            gap = below_gap
        else:
            gap = above_gap
        ad999 = tie999 * k
        fb999 = ad999 + gap
        r[label(slot, "adaptive")] = _arm(0.15 * k, p999=ad999, err=err)
        r[label(slot, "feedback")] = _arm(0.15 * k, p999=fb999, err=err * 1.05)
    return r


def _batch(n, **over):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    rounds = []
    for i in range(n):
        k = 1 + 0.01 * (i % 5)
        rounds.append(_round(k, **over))
    p = Path(tempfile.mkdtemp()) / "frequency_sweep.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def _all_hit_batch(n):
    """write a batch on which every prediction scores HIT"""
    import tempfile
    rounds = []
    for i in range(n):
        k = 1 + 0.01 * (i % 5)
        r = _round(k, tie999=1_400_000, below_gap=0, above_gap=0, probe_od=0.5, err=0.007)
        r[label(2000, "feedback")]["p999_ns"] = r[label(2000, "adaptive")]["p999_ns"] + 300_000
        r[label(4000, "feedback")]["p999_ns"] = r[label(4000, "adaptive")]["p999_ns"] + 900_000
        rounds.append(r)
    p = Path(tempfile.mkdtemp()) / "frequency_sweep.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def selftest():
    """score synthetic batches and check each verdict comes out as expected"""
    import contextlib
    import io
    failures = []

    def score(builder=_all_hit_batch, **kw):
        """run the scorer with its output discarded"""
        p = builder(16, **kw) if builder is _batch else builder(16)
        with contextlib.redirect_stdout(io.StringIO()):
            return run(p)

    def expect(name, key, want, builder=_all_hit_batch, **kw):
        """record a failure if a verdict differs from want"""
        got = score(builder, **kw)
        if got.get(key) is not want:
            failures.append(f"{name}: {key} came out {got.get(key)}, expected {want}")
            print(f"  DID NOT FIRE  {name}")
        else:
            tag = {True: "HIT", False: "MISS", None: "VACUOUS"}[want]
            print(f"  scored {key}={tag}  {name}")

    for k in ("P17", "P18", "P19", "P20"):
        expect("a batch built to satisfy every prediction", k, True)

    expect("the anchor separates instead of tying", "P17", False, _batch,
           tie999=1_400_000, below_gap=0, above_gap=0, anchor_gap=500_000)

    expect("a below-period slot separates", "P18", False, _batch,
           tie999=1_400_000, below_gap=400_000, above_gap=500_000)
    expect("both below-period slots vacuous", "P18", None, _batch,
           probe_od=0.01, below_gap=400_000, above_gap=500_000)

    expect("no separation above the period", "P19", False, _batch,
           tie999=1_400_000, below_gap=0, above_gap=0)
    def _shrinking(n):
        """write a batch whose margin at 4000 is smaller than at 2000"""
        import tempfile
        rounds = []
        for i in range(n):
            k = 1 + 0.01 * (i % 5)
            r = _round(k, tie999=1_400_000, below_gap=0, above_gap=0, probe_od=0.5, err=0.007)
            r[label(2000, "feedback")]["p999_ns"] = r[label(2000, "adaptive")]["p999_ns"] + 900_000
            r[label(4000, "feedback")]["p999_ns"] = r[label(4000, "adaptive")]["p999_ns"] + 300_000
            rounds.append(r)
        p = Path(tempfile.mkdtemp()) / "frequency_sweep.json"
        p.write_text(json.dumps({"rounds": rounds}))
        return p
    expect("margin shrinks instead of growing", "P19", False, _shrinking)
    expect("an above-period slot vacuous", "P19", None, _batch,
           probe_od=0.01, below_gap=0, above_gap=500_000)

    def _misses_too(n):
        """write a batch whose misses also separate and clear the bound"""
        import tempfile
        rounds = []
        for i in range(n):
            k = 1 + 0.01 * (i % 5)
            r = _round(k, tie999=1_400_000, below_gap=0, above_gap=0, probe_od=0.5, err=0.007)
            r[label(2000, "feedback")]["p999_ns"] = r[label(2000, "adaptive")]["p999_ns"] + 300_000
            r[label(4000, "feedback")]["p999_ns"] = r[label(4000, "adaptive")]["p999_ns"] + 900_000
            r[label(2000, "adaptive")]["over_deadline_frac"] = 0.10 * k
            r[label(2000, "feedback")]["over_deadline_frac"] = 0.30 * k
            r[label(4000, "adaptive")]["over_deadline_frac"] = 0.10 * k
            r[label(4000, "feedback")]["over_deadline_frac"] = 0.30 * k
            rounds.append(r)
        p = Path(tempfile.mkdtemp()) / "frequency_sweep.json"
        p.write_text(json.dumps({"rounds": rounds}))
        return p
    expect("misses also separate and clear the P10 bound", "P20", False, _misses_too)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: every prediction scored HIT, MISS and VACUOUS as built")
    return 0


MD_BEGIN = "<!-- FREQSWEEP-VERDICTS:BEGIN -->"
MD_END = "<!-- FREQSWEEP-VERDICTS:END -->"

TITLES = {
    "P17": "slot_us=1000 does not separate on misses or p99.9",
    "P18": "slots below the period do not separate on p99.9",
    "P19": "slots above the period separate on p99.9 favouring adaptive, margin grows with slot",
    "P20": "slots above the period do not separate and clear the bound on misses",
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
    tag = {True: "HIT", False: "MISS", None: "VACUOUS"}
    out = [MD_BEGIN, "", "| prediction | what it said | verdict |", "|---|---|---|"]
    for k in ("P17", "P18", "P19", "P20"):
        out.append(f"| **{k}** | {TITLES[k]} | **{tag[got.get(k)]}** |")
    out += ["", "*Scored by `benchmark/score/nested/frequency_sweep.py`.*",
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
    lib.need_input(args.paired, "--paired", "frequency_sweep")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
