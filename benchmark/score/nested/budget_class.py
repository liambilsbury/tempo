#!/usr/bin/env python3
"""Score the budget batch P35 and P36 for tlb interference: exceeded_budget_frac by rate"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, wilcoxon   # noqa: E402

PAIRED = "results/earlier_phase/paired/paired-budget-class.json"

RATES = [1000, 2000, 4000, 8000]
VACUOUS_BELOW = 0.05
P35_RATE = 2000
P35_HOG20_DUTY20 = 0.000075
P36_CROSS = 0.01
ALPHA = 0.05


def adaptive_label(r):
    """return the adaptive arm's label at a rate"""
    return f"adaptive-r{r}"


def probe_label(r):
    """return the probe arm's label at a rate"""
    return f"probe-r{r}"


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
    """score P35 and P36 on a paired results file and print each verdict"""
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}
    print(f"{len(rounds)} rounds read from {paired_path}\n")

    out = {}
    vacuous = {}
    medians = {}
    print(f"{'rate':>6} {'probe over_deadline':>20} {'adaptive exceeded_budget_frac':>32}")
    for r_hz in RATES:
        probe_od = col(rounds, probe_label(r_hz), "over_deadline_frac")
        adap_eb = col(rounds, adaptive_label(r_hz), "exceeded_budget_frac")
        m_probe = median(probe_od) if probe_od else None
        m_adap = median(adap_eb) if adap_eb else None
        vacuous[r_hz] = m_probe is not None and m_probe < VACUOUS_BELOW
        medians[r_hz] = m_adap
        print(f"{r_hz:>6} {str(m_probe):>20} {str(m_adap):>32}"
              f"{'  VACUOUS' if vacuous[r_hz] else ''}")

    print(f"\nP35 -- exceeded_budget_frac at {P35_RATE} Hz above hog20 duty=20 "
          f"({P35_HOG20_DUTY20})")
    if vacuous[P35_RATE]:
        out["P35"] = verdict("tlb at 2kHz above hog20 duty=20", None,
                              f"VACUOUS: probe over_deadline_frac below "
                              f"{VACUOUS_BELOW} at {P35_RATE} Hz")
    elif medians[P35_RATE] is None:
        out["P35"] = verdict("tlb at 2kHz above hog20 duty=20", None, "no rounds")
    else:
        out["P35"] = verdict(
            "tlb at 2kHz above hog20 duty=20",
            bool(medians[P35_RATE] > P35_HOG20_DUTY20),
            f"median exceeded_budget_frac={medians[P35_RATE]}")

    print(f"\nP36 -- first rate crossing {P36_CROSS}, monotone after")
    usable = [r_hz for r_hz in RATES if not vacuous[r_hz] and medians[r_hz] is not None]
    crossing = next((r_hz for r_hz in RATES if r_hz in usable and medians[r_hz] >= P36_CROSS),
                     None)
    if len(usable) < len(RATES):
        missing = [r_hz for r_hz in RATES if r_hz not in usable]
        print(f"  ({len(missing)} of {len(RATES)} rates VACUOUS or empty: {missing})")
    if crossing is None:
        out["P36"] = verdict("crossing rate exists, monotone after", False,
                              f"exceeded_budget_frac never reached {P36_CROSS} at any usable "
                              f"rate: {medians}")
    else:
        after = [r_hz for r_hz in usable if r_hz >= crossing]
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
                print(f"  non-monotone: rate={b} median exceeded_budget_frac below rate={a}'s, "
                      f"wilcoxon p={w}")
        out["P36"] = verdict(
            "crossing rate exists, monotone after", bool(not non_monotone),
            f"first rate at or above {P36_CROSS}: {crossing} (medians: {medians}); "
            f"{'monotone after crossing' if not non_monotone else 'NOT monotone after crossing'}")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(f"{k}={tags.get(out.get(k))}" for k in ("P35", "P36")))
    return out


def _round(i, eb_by_rate, probe_od_by_rate):
    """build round i with each rate's exceeded-budget and probe miss fractions"""
    r = {"control": {"over_deadline_frac": 0.0, "exceeded_budget_frac": 0.0}}
    for r_hz in RATES:
        r[probe_label(r_hz)] = {"over_deadline_frac": probe_od_by_rate[r_hz]}
        r[adaptive_label(r_hz)] = {"exceeded_budget_frac": eb_by_rate(r_hz, i)}
    return r


def _batch(eb_by_rate, probe_od_by_rate=None, n=16):
    """write rounds to a temporary paired results file and return its path"""
    import tempfile
    probe_od_by_rate = probe_od_by_rate or {r_hz: 0.3 for r_hz in RATES}
    rounds = [_round(i, eb_by_rate, probe_od_by_rate) for i in range(n)]
    p = Path(tempfile.mkdtemp()) / "budget-class.json"
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

    clean = {1000: 0.0002, 2000: 0.0005, 4000: 0.05, 8000: 0.20}
    p_clean = _batch(lambda r, i: clean[r])
    expect("clean crossing, above hog20's floor at 2kHz", "P35", True, p_clean)
    expect("clean crossing, above hog20's floor at 2kHz", "P36", True, p_clean)

    never = {1000: 0.00001, 2000: 0.00005, 4000: 0.0001, 8000: 0.0002}
    p_never = _batch(lambda r, i: never[r])
    expect("never crosses", "P36", False, p_never)

    nonmono = {1000: 0.0, 2000: 0.02, 4000: 0.005, 8000: 0.03}
    p_nonmono = _batch(lambda r, i: nonmono[r])
    expect("crosses then drops -- non-monotone", "P36", False, p_nonmono)

    cold2k = {1000: 0.00001, 2000: 0.00005, 4000: 0.0001, 8000: 0.0002}
    p_cold2k = _batch(lambda r, i: cold2k[r])
    expect("tlb at 2kHz no better than hog20's own floor", "P35", False, p_cold2k)

    vac_probe = {1000: 0.3, 2000: 0.3, 4000: 0.01, 8000: 0.3}
    p_vac = _batch(lambda r, i: clean[r], probe_od_by_rate=vac_probe)
    got = score(p_vac)
    if got.get("P36") is None:
        print("  DID NOT FIRE  a vacuous rate is excluded and P36 still scores")
        failures.append("vacuous rate should not itself force P36 to VOID unless every rate is")
    else:
        print("  scored P36 despite one VACUOUS rate, excluding it from the crossing search")

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: P35/P36 scored every constructed batch as built")
    return 0


MD_BEGIN = "<!-- BUDGETCLASS-VERDICTS:BEGIN -->"
MD_END = "<!-- BUDGETCLASS-VERDICTS:END -->"


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
           f"| **P35** | tlb exceeded_budget_frac at 2kHz above hog20 duty=20 | "
           f"**{tag[got.get('P35')]}** |",
           f"| **P36** | first crossing rate between 2000 and 8000 Hz, monotone after | "
           f"**{tag[got.get('P36')]}** |"]
    out += ["", "*Scored by `benchmark/score/nested/budget_class.py`.*",
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
    lib.need_input(args.paired, "--paired", "budget_class")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: verdict block "
              f"{'matches the scorer' if args.check else 'regenerated'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
