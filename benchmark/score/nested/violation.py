#!/usr/bin/env python3
"""Score the guarantee batch P22 to P25: in-loop, gap_detector and steal against guarantee-control"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test, wilcoxon   # noqa: E402
from benchmark.analysis.recall import in_loop as in_loop_recall                             # noqa: E402
from benchmark.analysis.recall import gap_detector as gap_detector_recall                             # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-guarantee.json"
DETECTORS_FROZEN = "results/earlier_phase/frozen/guarantee-detectors-frozen.json"

CONTROL = "guarantee-control"
CLASSES = ["guarantee-violate-tlbq", "guarantee-violate-wake"]
GAPCPU = 4
ALPHA = 0.05
USER_HZ_NS = 10_000_000


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


def raw_diffs(rounds, label, key):
    """return label minus control per round for a FACT-line field"""
    return [r[label][key] - r[CONTROL][key]
            for r in rounds if label in r and CONTROL in r
            and key in r[label] and key in r[CONTROL]]


def hit_deltas(rounds, label):
    """return label minus control per round for the hit count"""
    return [r[label]["hit"] - r[CONTROL]["hit"]
            for r in rounds if label in r and CONTROL in r
            and "hit" in r[label] and "hit" in r[CONTROL]]


def arm_scores(rows_by_label, paired_recall_fn, denom_key="outside_frac"):
    """return an arm's control-subtracted recall and separation test"""
    ctl = rows_by_label.get(CONTROL, [])
    by_round = {r["round"]: r for r in ctl}
    out = {}
    for label in CLASSES:
        rows = rows_by_label.get(label, [])
        recall = paired_recall_fn(rows, ctl)
        diffs = [r["detector_frac"] - by_round[r["round"]]["detector_frac"]
                 for r in rows if r["round"] in by_round]
        w, _ = wilcoxon(diffs) if diffs else (1.0, 0)
        s, pos, neg = sign_test(diffs) if diffs else (1.0, 0, 0)
        out[label] = {
            "recall": median(recall) if recall else None, "n_recall": len(recall),
            "diffs": diffs, "wilcoxon_p": w, "sign_p": s, "pos": pos, "neg": neg,
            "median_diff": median(diffs) if diffs else None,
        }
    return out


def detector_data(paired_path=PAIRED, frozen=DETECTORS_FROZEN, live=False):
    """return the in-loop and gap_detector rows, from the frozen snapshot unless live"""
    p = Path(frozen)
    if not p.is_absolute():
        p = ROOT / p
    if not live and p.exists():
        d = json.loads(p.read_text())
        return d["inloop"], d["gapdet"]
    return in_loop_recall.gather(paired_path, vcpu=1), gap_detector_recall.gather(paired_path, GAPCPU)


def freeze_detectors(paired_path=PAIRED, out=DETECTORS_FROZEN):
    """write both detectors' gathered rows to the frozen snapshot"""
    from benchmark import lib
    lib.refuse_unpublishable_experiments()
    in_loop_rows = in_loop_recall.gather(paired_path, vcpu=1)
    gap_detector_rows = gap_detector_recall.gather(paired_path, GAPCPU)
    p = ROOT / out
    p.write_text(json.dumps({"inloop": in_loop_rows, "gapdet": gap_detector_rows}, indent=1,
                             sort_keys=True) + "\n")
    n = sum(len(v) for v in in_loop_rows.values()) + sum(len(v) for v in gap_detector_rows.values())
    print(f"froze {n} condition-rounds across both detectors to {out}")
    return p


def run(paired_path=PAIRED, live=False):
    """score P22 to P25 on a paired results file and print each verdict"""
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}
    print(f"{len(rounds)} rounds read from {paired_path}\n")

    in_loop_rows, gap_detector_rows = detector_data(paired_path, live=live)
    in_loop_scored = arm_scores(in_loop_rows, in_loop_recall.paired_recall)
    gap_detector_scored = arm_scores(gap_detector_rows, lambda rows, ctl: gap_detector_recall.paired_recalls(rows, ctl)[0])

    out = {}

    ctl_od = [r[CONTROL]["over_deadline_frac"] for r in rounds if CONTROL in r]
    ctl_hit = [r[CONTROL]["hit"] for r in rounds if CONTROL in r]
    print("guarantee-control:")
    print(f"  over_deadline_frac  median {median(ctl_od):.4f}  [{min(ctl_od):.4f}-"
          f"{max(ctl_od):.4f}]  n={len(ctl_od)}")
    print(f"  hit (L1 events on the pinned cores)  "
          f"median {median(ctl_hit):.0f}  [{min(ctl_hit)}-{max(ctl_hit)}]  n={len(ctl_hit)}")
    out["control_over_deadline_frac"] = median(ctl_od)
    out["control_hit"] = median(ctl_hit)

    print("\nP22 -- the in-loop signal separates from guarantee-control for tlbq")
    p22 = in_loop_scored["guarantee-violate-tlbq"]
    out["P22"] = verdict("in-loop separates for tlbq", bool(p22["wilcoxon_p"] < ALPHA),
                          f"wilcoxon p={p22['wilcoxon_p']} ({p22['pos']} up / {p22['neg']} down), "
                          f"recall={p22['recall']} over {p22['n_recall']} rounds")

    print("\nP23 -- the in-loop signal does not separate from guarantee-control for wake")
    p23 = in_loop_scored["guarantee-violate-wake"]
    out["P23"] = verdict("in-loop does not separate for wake", bool(p23["wilcoxon_p"] >= ALPHA),
                          f"wilcoxon p={p23['wilcoxon_p']} ({p23['pos']} up / {p23['neg']} down), "
                          f"recall={p23['recall']} over {p23['n_recall']} rounds")

    print("\nP24 -- gap_detector recall against in-loop, wake and tlbq")
    gd_wake, il_wake = gap_detector_scored["guarantee-violate-wake"]["recall"], p23["recall"]
    gd_tlbq, il_tlbq = gap_detector_scored["guarantee-violate-tlbq"]["recall"], p22["recall"]
    if gd_wake is None or il_wake is None or gd_tlbq is None or il_tlbq is None:
        out["P24"] = verdict("gap_detector vs in-loop", None,
                              "VOID: a recall has no rounds, "
                              f"gap_detector_wake={gd_wake} in_loop_wake={il_wake} "
                              f"gap_detector_tlbq={gd_tlbq} in_loop_tlbq={il_tlbq}")
    else:
        wake_ok = gd_wake <= il_wake
        ratio_tlbq = (gd_tlbq / il_tlbq) if il_tlbq not in (0, None) else float("inf")
        tlbq_ok = 0.5 <= ratio_tlbq <= 2.0
        out["P24"] = verdict(
            "gap_detector lower for wake, within 2x for tlbq", bool(wake_ok and tlbq_ok),
            f"wake: gap_detector={gd_wake:.3f} in-loop={il_wake:.3f} (gap_detector<=in-loop: {wake_ok});  "
            f"tlbq: gap_detector={gd_tlbq:.3f} in-loop={il_tlbq:.3f} ratio={ratio_tlbq:.3f} "
            f"(within 2x: {tlbq_ok})")

    print("\nP25 -- the steal counter does not separate from guarantee-control for either class")
    steal_detail = []
    steal_hit = True
    for label in CLASSES:
        diffs = raw_diffs(rounds, label, "steal_ticks")
        w, _ = wilcoxon(diffs) if diffs else (1.0, 0)
        s, pos, neg = sign_test(diffs) if diffs else (1.0, 0, 0)
        sep = w < ALPHA
        steal_hit = steal_hit and not sep
        steal_detail.append(f"{label}: wilcoxon p={w} ({pos} up / {neg} down), "
                             f"median {median(diffs) if diffs else None:+} ticks "
                             f"({USER_HZ_NS/1e6:.0f}ms/tick), n={len(diffs)}")
    out["P25"] = verdict("steal does not separate for either class", steal_hit,
                          "; ".join(steal_detail))

    print("\nground truth (secondary): hit delta, violate minus control, per class")
    for label in CLASSES:
        hd = hit_deltas(rounds, label)
        print(f"  {label}: median {median(hd) if hd else None} events "
              f"[{min(hd) if hd else '-'}-{max(hd) if hd else '-'}], "
              f"{sum(1 for d in hd if d <= 0)} of {len(hd)} rounds non-positive")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(f"{k}={tags.get(out.get(k))}" for k in
                                      ("P22", "P23", "P24", "P25")))
    return out


def _fact(hit=6, over_deadline_frac=0.10, steal_ticks=10):
    """build a condition's FACT fields"""
    return {"hit": hit, "over_deadline_frac": over_deadline_frac, "steal_ticks": steal_ticks}


def _round(i, hit_ctl=11000, hit_extra=6, steal_extra=0):
    """build round i of the control and violate conditions"""
    return {
        CONTROL: _fact(hit=hit_ctl, steal_ticks=20),
        "guarantee-violate-tlbq": _fact(hit=hit_ctl + hit_extra, steal_ticks=20 + steal_extra),
        "guarantee-violate-wake": _fact(hit=hit_ctl + hit_extra, steal_ticks=20 + steal_extra),
    }


def _write_batch(rounds):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    p = Path(tempfile.mkdtemp()) / "guarantee.json"
    p.write_text(json.dumps({"rounds": rounds}))
    return p


def _write_experiment_dirs(rounds, gap_frac_by_label, gap_detector_frac_by_label,
                            outside_frac_ctl=0.02, outside_frac_extra=0.03):
    """write synthetic <label>-r<i>/ experiment trees that the detectors' one() accepts"""
    import tempfile
    base = Path(tempfile.mkdtemp())
    exp_dir = base / "experiments"
    frames, period_ns, window_ns = 6000, 1_000_000, 6_000_000_000
    for i in range(len(rounds)):
        for label in (CONTROL, "guarantee-violate-tlbq", "guarantee-violate-wake"):
            d = exp_dir / f"{label}-r{i}"
            (d / "l2").mkdir(parents=True, exist_ok=True)
            is_ctl = label == CONTROL
            gap_frac = gap_frac_by_label[label] if not is_ctl else gap_frac_by_label[CONTROL]
            observed_ns = int(window_ns * 0.75)
            gap_ns = int(observed_ns * gap_frac)
            (d / "l2" / "header.txt").write_text(
                f"inloop_gap_sum_ns={gap_ns}\ninloop_observed_ns={observed_ns}\n"
                f"inloop_gap_count=10\ninloop_gap_max_ns=1000\n"
                f"frames={frames}\nperiod_ns={period_ns}\n")
            outside_frac = outside_frac_ctl if is_ctl else outside_frac_ctl + outside_frac_extra
            outside_ns = int(outside_frac * window_ns)
            gap_detector_frac = gap_detector_frac_by_label[label] if not is_ctl else \
                gap_detector_frac_by_label[CONTROL]
            gap_detector_gap_ns = int(window_ns * gap_detector_frac)
            (d / "inject.meta").write_text(
                f"INJECT watched_wall_ns={window_ns}\n"
                f"INJECT watched_window_start_ns=0\nINJECT watched_window_end_ns={window_ns}\n"
                f"INJECT watched_vcpu{GAPCPU}_runtime_window_ns={int(window_ns * 0.99)}\n"
                f"INJECT watched_vcpu{GAPCPU}_rundelay_window_ns=1000000\n")
            lines = []
            n_buckets = int(window_ns // 100_000_000)
            per_bucket_ns = int(outside_ns / n_buckets) if n_buckets else 0
            for b in range(n_buckets):
                lines.append(f"@exit_ns[{b},1,8]: {per_bucket_ns}")
                lines.append(f"@exits[{b},1,8]: 1")
                lines.append(f"@exit_ns[{b},{GAPCPU},8]: 1000")
                lines.append(f"@exits[{b},{GAPCPU},8]: 1")
            (d / "exits.txt").write_text("\n".join(lines) + "\n")
            (d / "l2" / "console.log").write_text(
                f"GAP_DETECTOR cpu={GAPCPU} wall_ns={window_ns} iters=1 threshold_ns=2000 "
                f"cal_max_ns=100\nGAP_DETECTOR gap_sum_ns={gap_detector_gap_ns} gap_count=5 "
                f"gap_max_ns=1000\nGAP_DETECTOR gap_fraction=0\nGAP_DETECTOR ns_steal_window_ns=absent\n"
                f"GAP_DETECTOR tick_steal_window_ns=0\n")
    return exp_dir


def selftest():
    """score synthetic batches and check each verdict comes out as expected"""
    import contextlib
    import io
    failures = []

    def score(rounds, gap_frac, gap_detector_frac, **kw):
        """run the scorer with its output discarded"""
        paired = _write_batch(rounds)
        exp_dir = _write_experiment_dirs(rounds, gap_frac, gap_detector_frac, **kw)
        old_exp = {"inloop": in_loop_recall.EXP, "gapdet": gap_detector_recall.EXP}
        in_loop_recall.EXP, gap_detector_recall.EXP = exp_dir, exp_dir
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return run(paired, live=True)
        finally:
            in_loop_recall.EXP, gap_detector_recall.EXP = old_exp["inloop"], old_exp["gapdet"]

    def expect(name, key, want, rounds, gap_frac, gap_detector_frac, **kw):
        """record a failure if a verdict differs from want"""
        got = score(rounds, gap_frac, gap_detector_frac, **kw)
        if got.get(key) != want:
            failures.append(f"{name}: {key} came out {got.get(key)!r}, expected {want!r}")
            print(f"  DID NOT FIRE  {name}")
        else:
            print(f"  scored {key}={want!r}  {name}")

    rounds20 = [_round(i) for i in range(20)]

    gap_sep = {CONTROL: 0.02, "guarantee-violate-tlbq": 0.15, "guarantee-violate-wake": 0.02}
    gd_sep = {CONTROL: 0.01, "guarantee-violate-tlbq": 0.20, "guarantee-violate-wake": 0.01}
    expect("tlbq separates, wake does not", "P22", True, rounds20, gap_sep, gd_sep)
    expect("tlbq separates, wake does not", "P23", True, rounds20, gap_sep, gd_sep)

    gap_both = {CONTROL: 0.02, "guarantee-violate-tlbq": 0.15, "guarantee-violate-wake": 0.15}
    expect("wake also separates -- P23 misses", "P23", False, rounds20, gap_both, gd_sep)

    gd_zero = {CONTROL: 0.0, "guarantee-violate-tlbq": 0.0, "guarantee-violate-wake": 0.0}
    rounds_no_hit = [_round(i, hit_extra=0) for i in range(20)]
    expect("no ground-truth confirmation at all -- gap_detector recall has no rounds", "P24", None,
           rounds_no_hit, gap_sep, gd_zero)

    rounds_steal = [_round(i, steal_extra=50) for i in range(20)]
    expect("steal ticks move a lot -- P25 misses", "P25", False, rounds_steal, gap_sep, gd_sep)

    expect("steal ticks flat -- P25 hits", "P25", True, rounds20, gap_sep, gd_sep)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: P22-P25 scored every constructed batch as built")
    return 0


MD_BEGIN = "<!-- GUARANTEE-VERDICTS:BEGIN -->"
MD_END = "<!-- GUARANTEE-VERDICTS:END -->"


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
           f"| **P22** | in-loop separates from guarantee-control for tlbq | "
           f"**{tag[got.get('P22')]}** |",
           f"| **P23** | in-loop does not separate for wake | **{tag[got.get('P23')]}** |",
           f"| **P24** | gap_detector recall <= in-loop for wake, within 2x for tlbq | "
           f"**{tag[got.get('P24')]}** |",
           f"| **P25** | steal does not separate for either class | **{tag[got.get('P25')]}** |"]
    out += ["", "*Scored by `benchmark/score/nested/violation.py`.*",
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
    """parse arguments and run the selftest, the freeze, the markdown writer or the scorer"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--paired", default=str(PAIRED))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--markdown", metavar="DOC", default=None,
                    help="rewrite the generated verdict block in DOC")
    ap.add_argument("--check", action="store_true",
                    help="with --markdown: exit nonzero if the block differs from the scorer, write nothing")
    ap.add_argument("--freeze", action="store_true",
                    help="snapshot results/experiments/'s detector data")
    ap.add_argument("--live", action="store_true",
                    help="score results/experiments/")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(args.paired, "--paired", "guarantee")
    if args.freeze:
        freeze_detectors(args.paired)
        return 0
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired, live=args.live)
    return 0


if __name__ == "__main__":
    sys.exit(main())
