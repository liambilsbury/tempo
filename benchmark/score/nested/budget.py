#!/usr/bin/env python3
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
    return f"adaptive-d{d}"


def probe_label(d):
    return f"probe-d{d}"


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
    print(f"{'duty':>5} {'probe od':>20} {'adaptive eb':>32}")
    for d in DUTIES:
        probe_od = col(rounds, probe_label(d), "over_deadline_frac")
        adap_eb = col(rounds, adaptive_label(d), "exceeded_budget_frac")
        m_probe = median(probe_od) if probe_od else None
        m_adap = median(adap_eb) if adap_eb else None
        vacuous[d] = m_probe is not None and m_probe < VACUOUS_BELOW
        medians[d] = m_adap
        print(f"{d:>5} {str(m_probe):>20} {str(m_adap):>32}"
              f"{'  VACUOUS' if vacuous[d] else ''}")

    print(f"\nP26 eb < {P26_BOUND} duty={P26_DUTY}")
    if vacuous[P26_DUTY]:
        out["P26"] = verdict("duty=20", None, f"VACUOUS: probe od < {VACUOUS_BELOW}")
    elif medians[P26_DUTY] is None:
        out["P26"] = verdict("duty=20", None, "no rounds")
    else:
        out["P26"] = verdict("duty=20", bool(medians[P26_DUTY] < P26_BOUND), f"eb={medians[P26_DUTY]}")

    print(f"\nP27 crossing {P27_CROSS}, monotone")
    usable = [d for d in DUTIES if not vacuous[d] and medians[d] is not None]
    crossing = next((d for d in DUTIES if d in usable and medians[d] >= P27_CROSS), None)
    if len(usable) < len(DUTIES):
        missing = [d for d in DUTIES if d not in usable]
        print(f"  unusable {len(missing)}/{len(DUTIES)}: {missing}")
    if crossing is None:
        out["P27"] = verdict("crossing", False, f"no crossing {medians}")
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
                print(f"  non-monotone: duty={b} < duty={a} w p={w}")
        out["P27"] = verdict(
            "crossing", bool(not non_monotone),
            f"crossing={crossing} {medians} {'monotone' if not non_monotone else 'NOT monotone'}")

    print(f"\nP28 eb < {P28_BOUND} duty={P28_DUTY}")
    if vacuous[P28_DUTY]:
        out["P28"] = verdict("duty=95", None, f"VACUOUS: probe od < {VACUOUS_BELOW}")
    elif medians[P28_DUTY] is None:
        out["P28"] = verdict("duty=95", None, "no rounds")
    else:
        out["P28"] = verdict("duty=95", bool(medians[P28_DUTY] < P28_BOUND), f"eb={medians[P28_DUTY]}")

    tags = {True: "HIT", False: "MISS", None: "VOID"}
    print("\nverdicts  " + "  ".join(f"{k}={tags.get(out.get(k))}" for k in ("P26", "P27", "P28")))
    return out


def _round(i, eb_by_duty, probe_od_by_duty):
    r = {"control": {"over_deadline_frac": 0.0, "exceeded_budget_frac": 0.0}}
    for d in DUTIES:
        r[probe_label(d)] = {"over_deadline_frac": probe_od_by_duty[d]}
        r[adaptive_label(d)] = {"exceeded_budget_frac": eb_by_duty(d, i)}
    return r


def _batch(eb_by_duty, probe_od_by_duty=None, n=16):
    import tempfile
    probe_od_by_duty = probe_od_by_duty or {d: 0.3 for d in DUTIES}
    rounds = [_round(i, eb_by_duty, probe_od_by_duty) for i in range(n)]
    p = Path(tempfile.mkdtemp()) / "budget.json"
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
    expect("non-monotone", "P27", False, p_nonmono)

    hot20 = {20: 0.05, 50: 0.06, 80: 0.07, 95: 0.08}
    p_hot20 = _batch(lambda d, i: hot20[d])
    expect("duty=20 hot", "P26", False, p_hot20)

    collapse = {20: 0.0, 50: 0.02, 80: 0.3, 95: 0.7}
    p_collapse = _batch(lambda d, i: collapse[d])
    expect("duty=95 collapse", "P28", False, p_collapse)

    vac_probe = {20: 0.3, 50: 0.3, 80: 0.01, 95: 0.3}
    p_vac = _batch(lambda d, i: clean[d], probe_od_by_duty=vac_probe)
    got = score(p_vac)
    if got.get("P27") is None:
        print("  FAIL  P27 one VACUOUS duty")
        failures.append("one VACUOUS duty voided P27")
    else:
        print("  ok  P27 one VACUOUS duty")

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


MD_BEGIN = "<!-- BUDGET-VERDICTS:BEGIN -->"
MD_END = "<!-- BUDGET-VERDICTS:END -->"


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
           f"| **P26** | eb < {P26_BOUND} duty=20 | "
           f"**{tag[got.get('P26')]}** |",
           f"| **P27** | crossing duty 50..95, monotone | "
           f"**{tag[got.get('P27')]}** |",
           f"| **P28** | eb < {P28_BOUND} duty=95 | "
           f"**{tag[got.get('P28')]}** |"]
    out += ["", "*`benchmark/score/nested/budget.py`*",
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
    lib.need_input(args.paired, "--paired", "budget")
    if args.markdown:
        write_markdown(args.markdown, args.paired, check=args.check)
        print(f"{args.markdown}: {'ok' if args.check else 'written'}")
        return 0
    run(args.paired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
