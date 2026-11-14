#!/usr/bin/env python3
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


def arm_median(rounds, arm, key):
    v = [r[arm][key] for r in rounds if arm in r and key in r[arm]]
    return median(v) if v else None


def paired(rounds, a, b, key):
    return [r[b][key] - r[a][key]
            for r in rounds if a in r and b in r and key in r[a] and key in r[b]]


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

    probe_od = arm_median(rounds, "fixed-hi-probe", "over_deadline_frac")
    if probe_od is not None and probe_od < VACUOUS_BELOW:
        out["P21"] = None
        out["ADJUDICATION"] = None
        print("P21 adaptive/feedback p99.9")
        verdict("p99.9", None, f"VACUOUS: probe od {probe_od} < {VACUOUS_BELOW}")
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

    print("P21 adaptive/feedback p99.9")
    out["P21"] = verdict(
        "p99.9 no separation",
        bool(not sep999),
        f"w p={w999} {pos999}+/{neg999}- median {m999_ms:+.4f} ms")

    print(f"\n  od: sign p={sod} {posod}+/{negod}- w p={wod} median {mod_:+.6f}")

    print("\nADJUDICATION P10/P17")
    if sep999:
        if m999_ns is not None and m999_ns > 0:
            out["ADJUDICATION"] = "SEPARATES-ADAPTIVE"
            print("  RESOLVED adaptive")
        else:
            out["ADJUDICATION"] = "SEPARATES-FEEDBACK"
            print("  RESOLVED feedback")
    else:
        if m999_ms is not None and abs(m999_ms) < INCONCLUSIVE_BOUND_MS:
            out["ADJUDICATION"] = "RESOLVED-TIE"
            print(f"  RESOLVED tie n={len(rounds)} d={m999_ms:+.4f} ms < {INCONCLUSIVE_BOUND_MS}")
        else:
            out["ADJUDICATION"] = "INCONCLUSIVE-AT-THIS-POWER"
            print(f"  INCONCLUSIVE AT THIS POWER: w p={w999} d={m999_ms:+.4f} ms >= {INCONCLUSIVE_BOUND_MS} n={len(rounds)}")

    print("\nverdicts  ADJUDICATION=" + str(out["ADJUDICATION"]) + "  P21="
          + ("HIT" if out["P21"] else "MISS" if out["P21"] is False else "VACUOUS"))
    return out


def _arm(od, p999=1_000_000, err=0.0):
    return {"over_deadline_frac": od, "p999_ns": p999, "max_ns": p999 * 2, "mean_relerr": err,
            "hit": 40000}


def _round(k, ad999=1_400_000, gap=0, probe_od=0.5, err=0.007):
    r = {}
    r["fixed-hi-probe"] = _arm(probe_od * k, p999=2_000_000)
    a999 = ad999 * k
    f999 = a999 + gap
    r["adaptive"] = _arm(0.12 * k, p999=a999, err=err)
    r["feedback"] = _arm(0.12 * k, p999=f999, err=err * 1.05)
    return r


def _batch(n, gap=0, probe_od=0.5, ad999=1_400_000, err=0.007):
    import tempfile
    rounds = []
    for i in range(n):
        k = 1 + 0.01 * (i % 5)
        rounds.append(_round(k, ad999=ad999, gap=gap, probe_od=probe_od, err=err))
    p = Path(tempfile.mkdtemp()) / "anchor.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def _tie_batch(n):
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
    import tempfile
    rounds = []
    for i, gap in enumerate(_P10_DIFFS_NS[:n]):
        k = 1 + 0.01 * (i % 5)
        rounds.append(_round(k, gap=gap))
    p = Path(tempfile.mkdtemp()) / "anchor.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def selftest():
    import contextlib
    import io
    failures = []

    def score(builder, *args, **kw):
        p = builder(*args, **kw)
        with contextlib.redirect_stdout(io.StringIO()):
            return run(p)

    def expect(name, key, want, builder, *args, **kw):
        got = score(builder, *args, **kw)
        if got.get(key) != want:
            failures.append(f"{name}: {key}={got.get(key)!r} want {want!r}")
            print(f"  FAIL  {name}")
        else:
            print(f"  ok  {key}={want!r}  {name}")

    expect("tie", "P21", True, _tie_batch, 20)
    expect("tie", "ADJUDICATION", "RESOLVED-TIE", _tie_batch, 20)

    expect("P10 batch", "P21", True,
           _inconclusive_batch, 16)
    expect("P10 batch", "ADJUDICATION",
           "INCONCLUSIVE-AT-THIS-POWER", _inconclusive_batch, 16)

    expect("adaptive lower", "P21", False, _batch, 20, gap=300_000)
    expect("adaptive lower", "ADJUDICATION", "SEPARATES-ADAPTIVE", _batch, 20,
           gap=300_000)

    expect("feedback lower", "P21", False, _batch, 20, gap=-300_000)
    expect("feedback lower", "ADJUDICATION", "SEPARATES-FEEDBACK", _batch, 20,
           gap=-300_000)

    expect("probe od < 0.05", "P21", None, _batch, 20, gap=0,
           probe_od=0.01)
    expect("probe od < 0.05", "ADJUDICATION", None, _batch, 20, gap=0,
           probe_od=0.01)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


MD_BEGIN = "<!-- ANCHOR-VERDICTS:BEGIN -->"
MD_END = "<!-- ANCHOR-VERDICTS:END -->"


def markdown(paired_path=PAIRED):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*unscored*\n{MD_END}"
    tag = {True: "HIT", False: "MISS", None: "VACUOUS"}
    out = [MD_BEGIN, "", "| prediction | claim | verdict |", "|---|---|---|",
           f"| **P21** | adaptive/feedback p99.9 no separation "
           f"| **{tag[got.get('P21')]}** |",
           f"| **ADJUDICATION** | P10/P17 | "
           f"**{got.get('ADJUDICATION')}** |"]
    out += ["", "*`benchmark/score/nested/anchor.py`*",
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
    lib.need_input(args.paired, "--paired", "anchor")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: {'ok' if args.check else 'written'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
