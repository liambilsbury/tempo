#!/usr/bin/env python3
"""Score the trigger-completion batch, frame completion and p999 under interrupt delivery"""

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


LABELS = ("trigger-timer", "trigger-timer-hog", "trigger-irq", "trigger-irq-hog")


def usable_rounds(rounds):
    """split rounds into kept and dropped by missing labels and short frame delivery"""
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
    """score P31 to P34 on a paired results file and print each verdict"""
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}
    rounds, dropped = usable_rounds(rounds)
    if dropped:
        print(f"{len(dropped)} round(s) void of {len(rounds) + len(dropped)}, dropped:")
        for i, missing, short in dropped:
            why = []
            if missing:
                why.append(f"missing {', '.join(missing)}")
            if short:
                why.append(f"delivery short of {FRAMES_REQUESTED} on {', '.join(short)}")
            print(f"  round {i}: {'; '.join(why)}")
    print(f"{len(rounds)} usable rounds\n")
    if not rounds:
        print("no usable rounds")
        return {}

    out = {}

    print("P31 -- trigger-irq-hog completes fewer frames than trigger-timer-hog")
    d31 = [r["trigger-irq-hog"]["frames"] - r["trigger-timer-hog"]["frames"] for r in rounds]
    w31, _ = wilcoxon(d31)
    s31, pos31, neg31 = sign_test(d31)
    m31 = median(d31)
    out["P31"] = verdict(
        "irq-hog completes fewer frames, separating", bool(m31 < 0 and w31 < ALPHA),
        f"median diff={m31:+.1f} frames, wilcoxon p={w31} ({pos31} up / {neg31} down)")

    print("\nP32 -- (irq-hog - irq) - (timer-hog - timer) on frames is negative and separates")
    d32 = [
        (r["trigger-irq-hog"]["frames"] - r["trigger-irq"]["frames"])
        - (r["trigger-timer-hog"]["frames"] - r["trigger-timer"]["frames"])
        for r in rounds
    ]
    w32, _ = wilcoxon(d32)
    s32, pos32, neg32 = sign_test(d32)
    m32 = median(d32)
    out["P32"] = verdict(
        "delivery-specific completion residual is negative and separates",
        bool(m32 < 0 and w32 < ALPHA),
        f"median residual={m32:+.1f} frames, wilcoxon p={w32} ({pos32} up / {neg32} down)")

    print("\nP33 -- (irq-hog - irq) - (timer-hog - timer) on p999 is positive and separates")
    d33 = [
        (r["trigger-irq-hog"]["p999_ns"] - r["trigger-irq"]["p999_ns"])
        - (r["trigger-timer-hog"]["p999_ns"] - r["trigger-timer"]["p999_ns"])
        for r in rounds
    ]
    w33, _ = wilcoxon(d33)
    s33, pos33, neg33 = sign_test(d33)
    m33 = median(d33)
    out["P33"] = verdict(
        "delivery-specific latency residual is positive and separates",
        bool(m33 > 0 and w33 < ALPHA),
        f"median residual={m33:+.0f} ns, wilcoxon p={w33} ({pos33} up / {neg33} down)")

    print(f"\nP34 -- trigger-irq's p999 is within {P34_BAND:.0%} of trigger-timer's")
    d34 = [r["trigger-irq"]["p999_ns"] - r["trigger-timer"]["p999_ns"] for r in rounds]
    ratios = [r["trigger-irq"]["p999_ns"] / r["trigger-timer"]["p999_ns"] for r in rounds
              if r["trigger-timer"]["p999_ns"] > 0]
    w34, _ = wilcoxon(d34)
    s34, pos34, neg34 = sign_test(d34)
    m_ratio = median(ratios) if ratios else None
    within_band = m_ratio is not None and (1 - P34_BAND) <= m_ratio <= (1 + P34_BAND)
    sig_outside = w34 < ALPHA and not within_band
    out["P34"] = verdict(
        "irq within band of timer", bool(within_band or not sig_outside),
        f"median ratio irq/timer={m_ratio}, wilcoxon p={w34} ({pos34} up / {neg34} down), "
        f"median diff={median(d34):+.0f} ns")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(
        f"{k}={tags.get(out.get(k))}" for k in ("P31", "P32", "P33", "P34")))
    return out


def _arm(p999=1_000_000, frames=FRAMES_REQUESTED):
    """build a condition's result"""
    return {"p999_ns": p999, "frames": frames}


def _round(timer=(1_000_000, FRAMES_REQUESTED), timer_hog=(1_200_000, FRAMES_REQUESTED),
           irq=(1_000_000, FRAMES_REQUESTED), irq_hog=(1_200_000, FRAMES_REQUESTED)):
    """build a round of the four conditions from (p999, frames) pairs"""
    return {
        "trigger-timer": _arm(*timer), "trigger-timer-hog": _arm(*timer_hog),
        "trigger-irq": _arm(*irq), "trigger-irq-hog": _arm(*irq_hog),
    }


def _batch(rounds):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    p = Path(tempfile.mkdtemp()) / "trigger-completion.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def selftest():
    """score synthetic batches and check each verdict comes out as expected"""
    import contextlib
    import io
    failures = []

    def score(paired_path):
        """run the scorer with its output discarded"""
        with contextlib.redirect_stdout(io.StringIO()):
            return run(paired_path)

    def expect(name, key, want, paired_path):
        """record a failure if a verdict differs from want"""
        got = score(paired_path)
        if got.get(key) != want:
            failures.append(f"{name}: {key} came out {got.get(key)!r}, expected {want!r}")
            print(f"  DID NOT FIRE  {name}")
        else:
            print(f"  scored {key}={want!r}  {name}")

    deficit = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(1_010_000, 300), irq_hog=(1_210_000, 300 - 5 - (i % 3)))
        for i in range(16)])
    expect("a real completion deficit, delivery-specific", "P31", True, deficit)
    expect("a real completion deficit, delivery-specific", "P32", True, deficit)

    baseline_noise = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(1_010_000, 295), irq_hog=(1_210_000, 295))
        for i in range(16)])
    expect("irq's own baseline noise is not a delivery-specific effect", "P32", False,
           baseline_noise)

    latency_only = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(1_000_000, 300), irq_hog=(1_600_000 + i * 2000, 300))
        for i in range(16)])
    expect("a real delivery-specific latency cost", "P33", True, latency_only)
    expect("no completion deficit alongside the latency cost", "P31", False, latency_only)

    bad_p34 = _batch([_round(
        timer=(1_000_000, 300), timer_hog=(1_200_000, 300),
        irq=(2_000_000 + i * 1000, 300), irq_hog=(2_200_000 + i * 1000, 300))
        for i in range(16)])
    expect("irq far from timer, absent interference", "P34", False, bad_p34)

    short = [_round() for _ in range(15)]
    short.append({
        "trigger-timer": _arm(), "trigger-timer-hog": _arm(),
        "trigger-irq": _arm(frames=50), "trigger-irq-hog": _arm(),
    })
    p_short = _batch(short)
    with contextlib.redirect_stdout(io.StringIO()):
        got = run(p_short)
    if got.get("P34") is None:
        failures.append("a single short round should not void the whole batch")
        print("  DID NOT FIRE  one short round excluded, not fatal")
    else:
        print("  scored with one short round excluded")

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: P31-P34 scored every constructed batch as built")
    return 0


MD_BEGIN = "<!-- TRIGCOMP-VERDICTS:BEGIN -->"
MD_END = "<!-- TRIGCOMP-VERDICTS:END -->"


def markdown(paired_path=PAIRED):
    """render the verdict block for the pre-registration doc"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = run(paired_path)
    if not got:
        return f"{MD_BEGIN}\n*Not yet scored.*\n{MD_END}"
    tag = {True: "HIT", False: "MISS", None: "VOID"}
    out = [MD_BEGIN, "", "| prediction | what it said | verdict |", "|---|---|---|",
           f"| **P31** | trigger-irq-hog completes fewer frames than trigger-timer-hog | "
           f"**{tag[got.get('P31')]}** |",
           f"| **P32** | the delivery-specific completion residual is negative and separates | "
           f"**{tag[got.get('P32')]}** |",
           f"| **P33** | the delivery-specific latency residual is positive and separates | "
           f"**{tag[got.get('P33')]}** |",
           f"| **P34** | trigger-irq's p999 is within 20% of trigger-timer's | "
           f"**{tag[got.get('P34')]}** |"]
    out += ["", "*Scored by `benchmark/score/nested/trigger_completion.py`.*",
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
    lib.need_input(args.paired, "--paired", "trigger_completion")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
