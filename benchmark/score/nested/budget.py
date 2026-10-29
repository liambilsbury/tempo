#!/usr/bin/env python3
"""Score the budget batch P26 to P28: exceeded_budget_frac by hog duty"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-budget.json"

DUTIES = [20, 50, 80, 95]
VACUOUS_BELOW = 0.05
P26_DUTY = 20
P26_BOUND = 0.005
P27_CROSS = 0.01
P28_DUTY = 95
P28_BOUND = 0.5
ALPHA = 0.05


def adaptive_label(d):
    """return the adaptive arm's label at a duty"""
    return f"adaptive-d{d}"


def probe_label(d):
    """return the probe arm's label at a duty"""
    return f"probe-d{d}"


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


def col(rounds, label, key):
    """return an arm's numeric metric over the rounds that have it"""
    return [r[label][key] for r in rounds if label in r and key in r[label]
            and isinstance(r[label][key], (int, float))]


def run(paired_path=PAIRED):
    """score P26 to P28 on a paired results file and print each verdict"""
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}
    print(f"{len(rounds)} rounds read from {paired_path}\n")

    out = {}
    vacuous = {}
    medians = {}
    print(f"{'duty':>5} {'probe over_deadline':>20} {'adaptive exceeded_budget_frac':>32}")
    for d in DUTIES:
        probe_od = col(rounds, probe_label(d), "over_deadline_frac")
        adap_eb = col(rounds, adaptive_label(d), "exceeded_budget_frac")
        m_probe = median(probe_od) if probe_od else None
        m_adap = median(adap_eb) if adap_eb else None
        vacuous[d] = m_probe is not None and m_probe < VACUOUS_BELOW
        medians[d] = m_adap
        print(f"{d:>5} {str(m_probe):>20} {str(m_adap):>32}"
              f"{'  VACUOUS' if vacuous[d] else ''}")

    print(f"\nP26 -- exceeded_budget_frac below {P26_BOUND} at duty={P26_DUTY}")
    if vacuous[P26_DUTY]:
        out["P26"] = verdict("duty=20 below bound", None,
                              f"VACUOUS: probe over_deadline_frac below "
                              f"{VACUOUS_BELOW} at duty={P26_DUTY}")
    elif medians[P26_DUTY] is None:
        out["P26"] = verdict("duty=20 below bound", None, "no rounds")
    else:
        out["P26"] = verdict("duty=20 below bound", bool(medians[P26_DUTY] < P26_BOUND),
                              f"median exceeded_budget_frac={medians[P26_DUTY]}")

    print(f"\nP27 -- first duty crossing {P27_CROSS}, monotone after")
    usable = [d for d in DUTIES if not vacuous[d] and medians[d] is not None]
    crossing = next((d for d in DUTIES if d in usable and medians[d] >= P27_CROSS), None)
    if len(usable) < len(DUTIES):
        missing = [d for d in DUTIES if d not in usable]
        print(f"  ({len(missing)} of {len(DUTIES)} duties VACUOUS or empty: {missing})")
    if crossing is None:
        out["P27"] = verdict("crossing duty exists, monotone after", False,
                              f"exceeded_budget_frac never reached {P27_CROSS} at any usable "
                              f"duty: {medians}")
    else:
        after = [d for d in usable if d >= crossing]
        non_monotone = False
        for a, b in zip(after, after[1:]):
            diffs = [r[adaptive_label(b)]["exceeded_budget_frac"]
                     - r[adaptive_label(a)]["exceeded_budget_frac"]
                     for r in rounds if adaptive_label(a) in r and adaptive_label(b) in r
                     and "exceeded_budget_frac" in r[adaptive_label(a)]
                     and "exceeded_budget_frac" in r[adaptive_label(b)]]
            if not diffs:
                continue
            w, _ = wilcoxon(diffs)
            if w < ALPHA and median(diffs) < 0:
                non_monotone = True
                print(f"  non-monotone: duty={b} median exceeded_budget_frac below duty={a}'s, "
                      f"wilcoxon p={w}")
        out["P27"] = verdict(
            "crossing duty exists, monotone after", bool(not non_monotone),
            f"first duty at or above {P27_CROSS}: {crossing} (medians: {medians}); "
            f"{'monotone after crossing' if not non_monotone else 'NOT monotone after crossing'}")

    print(f"\nP28 -- exceeded_budget_frac below {P28_BOUND} at duty={P28_DUTY}")
    if vacuous[P28_DUTY]:
        out["P28"] = verdict("duty=95 below bound", None,
                              f"VACUOUS: probe over_deadline_frac below "
                              f"{VACUOUS_BELOW} at duty={P28_DUTY}")
    elif medians[P28_DUTY] is None:
        out["P28"] = verdict("duty=95 below bound", None, "no rounds")
    else:
        out["P28"] = verdict("duty=95 below bound",
                              bool(medians[P28_DUTY] < P28_BOUND),
                              f"median exceeded_budget_frac={medians[P28_DUTY]}")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(f"{k}={tags.get(out.get(k))}" for k in ("P26", "P27", "P28")))
    return out


def _round(i, eb_by_duty, probe_od_by_duty):
    """build round i with each duty's exceeded-budget and probe miss fractions"""
    r = {"control": {"over_deadline_frac": 0.0, "exceeded_budget_frac": 0.0}}
    for d in DUTIES:
        r[probe_label(d)] = {"over_deadline_frac": probe_od_by_duty[d]}
        r[adaptive_label(d)] = {"exceeded_budget_frac": eb_by_duty(d, i)}
    return r


def _batch(eb_by_duty, probe_od_by_duty=None, n=16):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    probe_od_by_duty = probe_od_by_duty or {d: 0.3 for d in DUTIES}
    rounds = [_round(i, eb_by_duty, probe_od_by_duty) for i in range(n)]
    p = Path(tempfile.mkdtemp()) / "budget.json"
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

    clean = {20: 0.0, 50: 0.002, 80: 0.05, 95: 0.20}
    p_clean = _batch(lambda d, i: clean[d])
    expect("clean crossing", "P26", True, p_clean)
    expect("clean crossing", "P27", True, p_clean)
    expect("clean crossing", "P28", True, p_clean)

    never = {20: 0.0, 50: 0.001, 80: 0.002, 95: 0.003}
    p_never = _batch(lambda d, i: never[d])
    expect("never crosses", "P27", False, p_never)

    nonmono = {20: 0.0, 50: 0.02, 80: 0.005, 95: 0.03}
    p_nonmono = _batch(lambda d, i: nonmono[d])
    expect("crosses then drops -- non-monotone", "P27", False, p_nonmono)

    hot20 = {20: 0.05, 50: 0.06, 80: 0.07, 95: 0.08}
    p_hot20 = _batch(lambda d, i: hot20[d])
    expect("duty=20 already hot", "P26", False, p_hot20)

    collapse = {20: 0.0, 50: 0.02, 80: 0.3, 95: 0.7}
    p_collapse = _batch(lambda d, i: collapse[d])
    expect("collapses at duty=95", "P28", False, p_collapse)

    vac_probe = {20: 0.3, 50: 0.3, 80: 0.01, 95: 0.3}
    p_vac = _batch(lambda d, i: clean[d], probe_od_by_duty=vac_probe)
    got = score(p_vac)
    if got.get("P27") is None:
        print("  DID NOT FIRE  a vacuous duty is excluded and P27 still scores")
        failures.append("vacuous duty should not itself force P27 to VOID unless every duty is")
    else:
        print("  scored P27 despite one VACUOUS duty, excluding it from the crossing search")

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: P26/P27/P28 scored every constructed batch as built")
    return 0


MD_BEGIN = "<!-- BUDGET-VERDICTS:BEGIN -->"
MD_END = "<!-- BUDGET-VERDICTS:END -->"


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
           f"| **P26** | exceeded_budget_frac below {P26_BOUND} at duty=20 | "
           f"**{tag[got.get('P26')]}** |",
           f"| **P27** | first crossing duty between 50 and 95, monotone after | "
           f"**{tag[got.get('P27')]}** |",
           f"| **P28** | exceeded_budget_frac below {P28_BOUND} at duty=95 | "
           f"**{tag[got.get('P28')]}** |"]
    out += ["", "*Scored by `benchmark/score/nested/budget.py`.*",
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
    lib.need_input(args.paired, "--paired", "budget")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
