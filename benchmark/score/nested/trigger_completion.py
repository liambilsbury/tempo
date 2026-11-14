#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-trigger-completion.json"

FRAMES_REQUESTED = 300
FRAMES_TOLERANCE = 0.10
P34_BAND = 0.20
ALPHA = 0.05


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


LABELS = ("trigger-timer", "trigger-timer-hog", "trigger-irq", "trigger-irq-hog")


def usable_rounds(rounds):
    kept, dropped = [], []
    lo = FRAMES_REQUESTED * (1 - FRAMES_TOLERANCE)
    for i, r in enumerate(rounds):
        missing = [l for l in LABELS if l not in r]
        short = [l for l in LABELS if l in r and r[l].get("frames", 0) < lo]
        if missing or short:
            dropped.append((i, missing, short))
        else:
            kept.append(r)
    return kept, dropped


def run(paired_path=PAIRED):
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}
    rounds, dropped = usable_rounds(rounds)
    if dropped:
        print(f"{len(dropped)}/{len(rounds) + len(dropped)} round(s) void:")
        for i, missing, short in dropped:
            why = []
            if missing:
                why.append(f"missing {', '.join(missing)}")
            if short:
                why.append(f"short {', '.join(short)}")
            print(f"  round {i}: {'; '.join(why)}")
    print(f"{len(rounds)} rounds\n")
    if not rounds:
        print("no usable rounds")
        return {}

    out = {}

    print("P31 irq-hog frames < timer-hog")
    d31 = [r["trigger-irq-hog"]["frames"] - r["trigger-timer-hog"]["frames"] for r in rounds]
    w31, _ = wilcoxon(d31)
    s31, pos31, neg31 = sign_test(d31)
    m31 = median(d31)
    out["P31"] = verdict(
        "fewer frames", bool(m31 < 0 and w31 < ALPHA),
        f"d={m31:+.1f} frames w p={w31} {pos31}+/{neg31}-")

    print("\nP32 frames residual < 0")
    d32 = [
        (r["trigger-irq-hog"]["frames"] - r["trigger-irq"]["frames"])
        - (r["trigger-timer-hog"]["frames"] - r["trigger-timer"]["frames"])
        for r in rounds
    ]
    w32, _ = wilcoxon(d32)
    s32, pos32, neg32 = sign_test(d32)
    m32 = median(d32)
    out["P32"] = verdict(
        "residual", bool(m32 < 0 and w32 < ALPHA),
        f"residual={m32:+.1f} frames w p={w32} {pos32}+/{neg32}-")

    print("\nP33 p999 residual > 0")
    d33 = [
        (r["trigger-irq-hog"]["p999_ns"] - r["trigger-irq"]["p999_ns"])
        - (r["trigger-timer-hog"]["p999_ns"] - r["trigger-timer"]["p999_ns"])
        for r in rounds
    ]
    w33, _ = wilcoxon(d33)
    s33, pos33, neg33 = sign_test(d33)
    m33 = median(d33)
    out["P33"] = verdict(
        "residual", bool(m33 > 0 and w33 < ALPHA),
        f"residual={m33:+.0f} ns w p={w33} {pos33}+/{neg33}-")

    print(f"\nP34 irq/timer p999 within {P34_BAND:.0%}")
    d34 = [r["trigger-irq"]["p999_ns"] - r["trigger-timer"]["p999_ns"] for r in rounds]
    ratios = [r["trigger-irq"]["p999_ns"] / r["trigger-timer"]["p999_ns"] for r in rounds
              if r["trigger-timer"]["p999_ns"] > 0]
    w34, _ = wilcoxon(d34)
    s34, pos34, neg34 = sign_test(d34)
    m_ratio = median(ratios) if ratios else None
    within_band = m_ratio is not None and (1 - P34_BAND) <= m_ratio <= (1 + P34_BAND)
    sig_outside = w34 < ALPHA and not within_band
    out["P34"] = verdict(
        "within band", bool(within_band or not sig_outside),
        f"ratio={m_ratio} w p={w34} {pos34}+/{neg34}- d={median(d34):+.0f} ns")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(
        f"{k}={tags.get(out.get(k))}" for k in ("P31", "P32", "P33", "P34")))
    return out


def _arm(p999=1_000_000, frames=FRAMES_REQUESTED):
    return {"p999_ns": p999, "frames": frames}


def _round(timer=(1_000_000, FRAMES_REQUESTED), timer_hog=(1_200_000, FRAMES_REQUESTED),
           irq=(1_000_000, FRAMES_REQUESTED), irq_hog=(1_200_000, FRAMES_REQUESTED)):
    return {
        "trigger-timer": _arm(*timer), "trigger-timer-hog": _arm(*timer_hog),
        "trigger-irq": _arm(*irq), "trigger-irq-hog": _arm(*irq_hog),
    }


def _batch(rounds):
    import tempfile
    p = Path(tempfile.mkdtemp()) / "trigger-completion.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def selftest():
    import contextlib
    import io
    failures = []

    def score(paired_path):
        with contextlib.redirect_stdout(io.StringIO()):
            return run(paired_path)

    def expect(name, key, want, paired_path):
        got = score(paired_path)
        if got.get(key) != want:
            failures.append(f"{name}: {key}={got.get(key)!r} want {want!r}")
            print(f"  FAIL  {name}")
        else:
            print(f"  ok  {key}={want!r}  {name}")

    deficit = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(1_010_000, 300), irq_hog=(1_210_000, 300 - 5 - (i % 3)))
        for i in range(16)])
    expect("frames deficit", "P31", True, deficit)
    expect("frames deficit", "P32", True, deficit)

    baseline_noise = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(1_010_000, 295), irq_hog=(1_210_000, 295))
        for i in range(16)])
    expect("irq baseline noise", "P32", False,
           baseline_noise)

    latency_only = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(1_000_000, 300), irq_hog=(1_600_000 + i * 2000, 300))
        for i in range(16)])
    expect("p999 residual > 0", "P33", True, latency_only)
    expect("no frames deficit", "P31", False, latency_only)

    bad_p34 = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(2_000_000 + i * 1000, 300), irq_hog=(2_200_000 + i * 1000, 300))
        for i in range(16)])
    expect("irq far from timer", "P34", False, bad_p34)

    short = [_round() for _ in range(15)]
    short.append({
        "trigger-timer": _arm(), "trigger-timer-hog": _arm(),
        "trigger-irq": _arm(frames=50), "trigger-irq-hog": _arm(),
    })
    p_short = _batch(short)
    with contextlib.redirect_stdout(io.StringIO()):
        got = run(p_short)
    if got.get("P34") is None:
        failures.append("one short round voided batch")
        print("  FAIL  one short round")
    else:
        print("  ok  one short round")

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


MD_BEGIN = "<!-- TRIGCOMP-VERDICTS:BEGIN -->"
MD_END = "<!-- TRIGCOMP-VERDICTS:END -->"


def markdown(paired_path=PAIRED):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*unscored*\n{MD_END}"
    tag = {True: "HIT", False: "MISS", None: "VOID"}
    out = [MD_BEGIN, "", "| prediction | claim | verdict |", "|---|---|---|",
           f"| **P31** | frames irq-hog < timer-hog | "
           f"**{tag[got.get('P31')]}** |",
           f"| **P32** | frames residual < 0, separates | "
           f"**{tag[got.get('P32')]}** |",
           f"| **P33** | p999 residual > 0, separates | "
           f"**{tag[got.get('P33')]}** |",
           f"| **P34** | p999 irq/timer within 20% | "
           f"**{tag[got.get('P34')]}** |"]
    out += ["", "*`benchmark/score/nested/trigger_completion.py`*",
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
    lib.need_input(args.paired, "--paired", "trigger_completion")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: {'ok' if args.check else 'written'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
