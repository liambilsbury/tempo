#!/usr/bin/env python3
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
    return f"adaptive-r{r}"


def probe_label(r):
    return f"probe-r{r}"


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


def col(rounds, label, key):
    return [r[label][key] for r in rounds if label in r and key in r[label]
            and isinstance(r[label][key], (int, float))]


def run(paired_path=PAIRED):
    rounds = rounds_of(paired_path)
    if rounds is None:
        print(f"{paired_path}: no data yet")
        return {}
    print(f"{len(rounds)} rounds\n")

    out = {}
    vacuous = {}
    medians = {}
    print(f"{'rate':>6} {'probe od':>20} {'adaptive eb':>32}")
    for r_hz in RATES:
        probe_od = col(rounds, probe_label(r_hz), "over_deadline_frac")
        adap_eb = col(rounds, adaptive_label(r_hz), "exceeded_budget_frac")
        m_probe = median(probe_od) if probe_od else None
        m_adap = median(adap_eb) if adap_eb else None
        vacuous[r_hz] = m_probe is not None and m_probe < VACUOUS_BELOW
        medians[r_hz] = m_adap
        print(f"{r_hz:>6} {str(m_probe):>20} {str(m_adap):>32}"
              f"{'  VACUOUS' if vacuous[r_hz] else ''}")

    print(f"\nP35 eb {P35_RATE} Hz > {P35_HOG20_DUTY20}")
    if vacuous[P35_RATE]:
        out["P35"] = verdict("tlb 2kHz", None, f"VACUOUS: probe od < {VACUOUS_BELOW}")
    elif medians[P35_RATE] is None:
        out["P35"] = verdict("tlb 2kHz", None, "no rounds")
    else:
        out["P35"] = verdict(
            "tlb 2kHz", bool(medians[P35_RATE] > P35_HOG20_DUTY20), f"eb={medians[P35_RATE]}")

    print(f"\nP36 crossing {P36_CROSS}, monotone")
    usable = [r_hz for r_hz in RATES if not vacuous[r_hz] and medians[r_hz] is not None]
    crossing = next((r_hz for r_hz in RATES if r_hz in usable and medians[r_hz] >= P36_CROSS),
                     None)
    if len(usable) < len(RATES):
        missing = [r_hz for r_hz in RATES if r_hz not in usable]
        print(f"  unusable {len(missing)}/{len(RATES)}: {missing}")
    if crossing is None:
        out["P36"] = verdict("crossing", False, f"no crossing {medians}")
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
                print(f"  non-monotone: rate={b} < rate={a} w p={w}")
        out["P36"] = verdict(
            "crossing", bool(not non_monotone),
            f"crossing={crossing} {medians} {'monotone' if not non_monotone else 'NOT monotone'}")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(f"{k}={tags.get(out.get(k))}" for k in ("P35", "P36")))
    return out


def _round(i, eb_by_rate, probe_od_by_rate):
    r = {"control": {"over_deadline_frac": 0.0, "exceeded_budget_frac": 0.0}}
    for r_hz in RATES:
        r[probe_label(r_hz)] = {"over_deadline_frac": probe_od_by_rate[r_hz]}
        r[adaptive_label(r_hz)] = {"exceeded_budget_frac": eb_by_rate(r_hz, i)}
    return r


def _batch(eb_by_rate, probe_od_by_rate=None, n=16):
    import tempfile
    probe_od_by_rate = probe_od_by_rate or {r_hz: 0.3 for r_hz in RATES}
    rounds = [_round(i, eb_by_rate, probe_od_by_rate) for i in range(n)]
    p = Path(tempfile.mkdtemp()) / "budget-class.json"
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

    clean = {1000: 0.0002, 2000: 0.0005, 4000: 0.05, 8000: 0.20}
    p_clean = _batch(lambda r, i: clean[r])
    expect("clean crossing", "P35", True, p_clean)
    expect("clean crossing", "P36", True, p_clean)

    never = {1000: 0.00001, 2000: 0.00005, 4000: 0.0001, 8000: 0.0002}
    p_never = _batch(lambda r, i: never[r])
    expect("never crosses", "P36", False, p_never)

    nonmono = {1000: 0.0, 2000: 0.02, 4000: 0.005, 8000: 0.03}
    p_nonmono = _batch(lambda r, i: nonmono[r])
    expect("non-monotone", "P36", False, p_nonmono)

    cold2k = {1000: 0.00001, 2000: 0.00005, 4000: 0.0001, 8000: 0.0002}
    p_cold2k = _batch(lambda r, i: cold2k[r])
    expect("tlb 2kHz <= hog20 floor", "P35", False, p_cold2k)

    vac_probe = {1000: 0.3, 2000: 0.3, 4000: 0.01, 8000: 0.3}
    p_vac = _batch(lambda r, i: clean[r], probe_od_by_rate=vac_probe)
    got = score(p_vac)
    if got.get("P36") is None:
        print("  FAIL  P36 one VACUOUS rate")
        failures.append("one VACUOUS rate voided P36")
    else:
        print("  ok  P36 one VACUOUS rate")

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


MD_BEGIN = "<!-- BUDGETCLASS-VERDICTS:BEGIN -->"
MD_END = "<!-- BUDGETCLASS-VERDICTS:END -->"


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
           f"| **P35** | tlb eb 2kHz > hog20 duty=20 | "
           f"**{tag[got.get('P35')]}** |",
           f"| **P36** | crossing rate 2000..8000 Hz, monotone | "
           f"**{tag[got.get('P36')]}** |"]
    out += ["", "*`benchmark/score/nested/budget_class.py`*",
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
    lib.need_input(args.paired, "--paired", "budget_class")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: {'ok' if args.check else 'written'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
