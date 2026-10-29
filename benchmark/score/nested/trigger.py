#!/usr/bin/env python3
"""Score the trigger batch, where the frame starts at a device interrupt"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-trigger.json"

FRAMES_REQUESTED = 300
FRAMES_TOLERANCE = 0.10
P29_BAND = 0.20
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
    """score P29 and P30 on a paired results file and print each verdict"""
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

    print(f"P29 -- trigger-irq's p999 is within {P29_BAND:.0%} of trigger-timer's")
    diffs = [r["trigger-irq"]["p999_ns"] - r["trigger-timer"]["p999_ns"] for r in rounds]
    ratios = [r["trigger-irq"]["p999_ns"] / r["trigger-timer"]["p999_ns"] for r in rounds
              if r["trigger-timer"]["p999_ns"] > 0]
    w, _ = wilcoxon(diffs)
    s, pos, neg = sign_test(diffs)
    m_ratio = median(ratios) if ratios else None
    within_band = m_ratio is not None and (1 - P29_BAND) <= m_ratio <= (1 + P29_BAND)
    sig_outside = w < ALPHA and not within_band
    out["P29"] = verdict(
        "irq within band of timer", bool(within_band or not sig_outside),
        f"median ratio irq/timer={m_ratio}, wilcoxon p={w} ({pos} up / {neg} down), "
        f"median diff={median(diffs):+.0f} ns")

    print("\nP30 -- (irq-hog - irq) - (timer-hog - timer) on p999 is positive and separates")
    resid = [
        (r["trigger-irq-hog"]["p999_ns"] - r["trigger-irq"]["p999_ns"])
        - (r["trigger-timer-hog"]["p999_ns"] - r["trigger-timer"]["p999_ns"])
        for r in rounds
    ]
    wr, _ = wilcoxon(resid)
    sr, posr, negr = sign_test(resid)
    m_resid = median(resid)
    out["P30"] = verdict(
        "delivery-specific residual is positive and separates", bool(m_resid > 0 and wr < ALPHA),
        f"median residual={m_resid:+.0f} ns, wilcoxon p={wr} ({posr} up / {negr} down)")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(f"{k}={tags.get(out.get(k))}" for k in ("P29", "P30")))
    return out


def _arm(p999=1_000_000, frames=FRAMES_REQUESTED):
    """build a condition's result"""
    return {"p999_ns": p999, "frames": frames}


def _round(timer=1_000_000, timer_hog=1_200_000, irq=1_000_000, irq_hog=1_200_000):
    """build a round of the four conditions"""
    return {
        "trigger-timer": _arm(timer), "trigger-timer-hog": _arm(timer_hog),
        "trigger-irq": _arm(irq), "trigger-irq-hog": _arm(irq_hog),
    }


def _batch(rounds):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    p = Path(tempfile.mkdtemp()) / "trigger.json"
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

    clean = _batch([_round(1_000_000 + i * 1000, 1_200_000 + i * 1000,
                            1_010_000 + i * 1000, 1_210_000 + i * 1000)
                     for i in range(16)])
    expect("irq tracks timer, hog cost cancels", "P29", True, clean)
    expect("irq tracks timer, hog cost cancels", "P30", False, clean)

    bad_p29 = _batch([_round(1_000_000, 1_200_000, 2_000_000 + i * 1000, 2_200_000 + i * 1000)
                       for i in range(16)])
    expect("irq far from timer", "P29", False, bad_p29)

    delivery_cost = _batch([_round(1_000_000, 1_200_000, 1_000_000, 1_600_000 + i * 2000)
                             for i in range(16)])
    expect("a real delivery-specific cost", "P30", True, delivery_cost)

    short = [_round() for _ in range(15)]
    short.append({
        "trigger-timer": _arm(), "trigger-timer-hog": _arm(),
        "trigger-irq": _arm(frames=50), "trigger-irq-hog": _arm(),
    })
    p_short = _batch(short)
    with contextlib.redirect_stdout(io.StringIO()):
        got = run(p_short)
    if got.get("P29") is None:
        failures.append("a single short round should not void the whole batch")
        print("  DID NOT FIRE  one short round excluded, not fatal")
    else:
        print("  scored with one short round excluded")

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: P29/P30 scored every constructed batch as built")
    return 0


MD_BEGIN = "<!-- TRIGGER-VERDICTS:BEGIN -->"
MD_END = "<!-- TRIGGER-VERDICTS:END -->"


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
           f"| **P29** | trigger-irq's p999 is within 20% of trigger-timer's | "
           f"**{tag[got.get('P29')]}** |",
           f"| **P30** | the delivery-specific residual is positive and separates | "
           f"**{tag[got.get('P30')]}** |"]
    out += ["", "*Scored by `benchmark/score/nested/trigger.py`.*",
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
    lib.need_input(args.paired, "--paired", "trigger")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
