#!/usr/bin/env python3
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
    tag = {True: "HIT ", False: "MISS", None: "VOID"}[state]
    print(f"  {tag}  {name}\n        {detail}")
    return state


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


def label(slot, arm):
    return f"s{slot}-{arm}"


def slot_vacuous(rounds, slot):
    probe = arm_median(rounds, label(slot, "fixed-hi-probe"), "over_deadline_frac")
    return probe is not None and probe < VACUOUS_BELOW, probe


def run(paired_path=PAIRED):
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

    print("P17 adaptive/feedback slot_us=1000")
    od = paired(rounds, label(1000, "adaptive"), label(1000, "feedback"), "over_deadline_frac")
    p999 = paired(rounds, label(1000, "adaptive"), label(1000, "feedback"), "p999_ns")
    od_sep, od_p, od_w, od_c = separates(od)
    p999_sep, p999_p, p999_w, p999_c = separates(p999)
    ok = (not od_sep) and (not p999_sep)
    out["P17"] = verdict("no separation", bool(ok),
                         f"od sign p={od_p} {od_c}; p99.9 w p={p999_w} {p999_c}")

    print("\nP18 below period p99.9")
    p18_bits = []
    for slot in BELOW:
        vac, probe = slot_vacuous(rounds, slot)
        if vac:
            p18_bits.append(None)
            print(f"  slot_us={slot}: VACUOUS probe od {probe} < {VACUOUS_BELOW}")
            continue
        d = paired(rounds, label(slot, "adaptive"), label(slot, "feedback"), "p999_ns")
        sep, p, w, c = separates(d)
        p18_bits.append(not sep)
        print(f"  slot_us={slot}: {'MISS' if sep else 'holds'} w p={w} {c}")
    live = [b for b in p18_bits if b is not None]
    out["P18"] = None if not live else bool(all(live))
    verdict("no separation", out["P18"], f"{dict(zip(BELOW, p18_bits))}")

    print("\nP19 above period p99.9")
    p19_diffs = {}
    p19_vac = {}
    for slot in ABOVE:
        vac, probe = slot_vacuous(rounds, slot)
        p19_vac[slot] = vac
        if vac:
            print(f"  slot_us={slot}: VACUOUS probe od {probe} < {VACUOUS_BELOW}")
            continue
        d = paired(rounds, label(slot, "adaptive"), label(slot, "feedback"), "p999_ns")
        p19_diffs[slot] = d
        sep, p, w, c = separates(d)
        m = median(d)
        print(f"  slot_us={slot}: median {m} w p={w} {c} sep={sep}")
    if any(p19_vac.values()):
        out["P19"] = None
        verdict("separate, grows", None, f"VACUOUS {[s for s in ABOVE if p19_vac[s]]}")
    else:
        seps = {s: separates(p19_diffs[s]) for s in ABOVE}
        both_sep = all(seps[s][0] and median(p19_diffs[s]) > 0 for s in ABOVE)
        mags = {s: median(p19_diffs[s]) for s in ABOVE}
        grows = mags[4000] > mags[2000] if len(ABOVE) == 2 else None
        ok = bool(both_sep and grows)
        out["P19"] = verdict("separate, grows", ok, f"medians {mags} sep={both_sep} grows={grows}")
        a_err = arm_median(rounds, label(4000, "adaptive"), "mean_relerr")
        f_err = arm_median(rounds, label(4000, "feedback"), "mean_relerr")
        if a_err is not None and f_err is not None and f_err > 0:
            q_ok = a_err <= P19_QUALITY_TOL * f_err
            print(f"  relerr 4000: adaptive {a_err} feedback {f_err} x{P19_QUALITY_TOL} {'holds' if q_ok else 'MISSES'}")

    print("\nP20 above period deadlines")
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
        print(f"  slot_us={slot}: adaptive {ad} feedback {fb} sep={sep} cleared={cleared} {'MISS' if cleared else 'holds'}")
    live20 = [b for b in p20_bits if b is not None]
    out["P20"] = None if not live20 else bool(all(live20))
    verdict("misses not cleared", out["P20"], f"{dict(zip(ABOVE, p20_bits))}")

    print("\nverdicts  " + "  ".join(
        f"{k}={'HIT' if v else 'VACUOUS' if v is None else 'MISS'}"
        for k, v in sorted(out.items())))
    return out


def _arm(od, p999=1_000_000, err=0.0):
    return {"over_deadline_frac": od, "p999_ns": p999, "max_ns": p999 * 2, "mean_relerr": err,
            "hit": 40000}


def _round(k, tie999=1_400_000, below_gap=0, above_gap=0, anchor_gap=0, probe_od=0.5, err=0.007):
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
    import tempfile
    rounds = []
    for i in range(n):
        k = 1 + 0.01 * (i % 5)
        rounds.append(_round(k, **over))
    p = Path(tempfile.mkdtemp()) / "frequency_sweep.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def _all_hit_batch(n):
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
    import contextlib
    import io
    failures = []

    def score(builder=_all_hit_batch, **kw):
        p = builder(16, **kw) if builder is _batch else builder(16)
        with contextlib.redirect_stdout(io.StringIO()):
            return run(p)

    def expect(name, key, want, builder=_all_hit_batch, **kw):
        got = score(builder, **kw)
        if got.get(key) is not want:
            failures.append(f"{name}: {key}={got.get(key)} want {want}")
            print(f"  FAIL  {name}")
        else:
            tag = {True: "HIT", False: "MISS", None: "VACUOUS"}[want]
            print(f"  ok  {key}={tag}  {name}")

    for k in ("P17", "P18", "P19", "P20"):
        expect("all HIT", k, True)

    expect("slot_us=1000 separates", "P17", False, _batch,
           tie999=1_400_000, below_gap=0, above_gap=0, anchor_gap=500_000)

    expect("below separates", "P18", False, _batch,
           tie999=1_400_000, below_gap=400_000, above_gap=500_000)
    expect("below VACUOUS", "P18", None, _batch,
           probe_od=0.01, below_gap=400_000, above_gap=500_000)

    expect("above no separation", "P19", False, _batch,
           tie999=1_400_000, below_gap=0, above_gap=0)
    def _shrinking(n):
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
    expect("above margin shrinks", "P19", False, _shrinking)
    expect("above VACUOUS", "P19", None, _batch,
           probe_od=0.01, below_gap=0, above_gap=500_000)

    def _misses_too(n):
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
    expect("above od cleared", "P20", False, _misses_too)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


MD_BEGIN = "<!-- FREQSWEEP-VERDICTS:BEGIN -->"
MD_END = "<!-- FREQSWEEP-VERDICTS:END -->"

TITLES = {
    "P17": "slot_us=1000 od, p99.9 no separation",
    "P18": "below period p99.9 no separation",
    "P19": "above period p99.9 adaptive lower, margin grows",
    "P20": "above period od not cleared",
}


def markdown(paired_path=PAIRED):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*unscored*\n{MD_END}"
    tag = {True: "HIT", False: "MISS", None: "VACUOUS"}
    out = [MD_BEGIN, "", "| prediction | claim | verdict |", "|---|---|---|"]
    for k in ("P17", "P18", "P19", "P20"):
        out.append(f"| **{k}** | {TITLES[k]} | **{tag[got.get(k)]}** |")
    out += ["", "*`benchmark/score/nested/frequency_sweep.py`*",
            "", "```", buf.getvalue().rstrip(), "```", "", MD_END]
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
    lib.need_input(args.paired, "--paired", "frequency_sweep")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: {'ok' if args.check else 'written'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
