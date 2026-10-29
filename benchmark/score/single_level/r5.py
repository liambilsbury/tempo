#!/usr/bin/env python3
"""Apply R5's nested pilot selection and score its batch against R4b's load A

Usage: benchmark/score/single_level/r5.py --select PILOT [--record FILE] [--prefix P] [--one-round-fallback]
       benchmark/score/single_level/r5.py --grid
       benchmark/score/single_level/r5.py --split BATCH OUT [--prefix P]
       benchmark/score/single_level/r5.py --ratio ARRIVED
       benchmark/score/single_level/r5.py --cross-level R5_SCORE [R4B_FROZEN]
       benchmark/score/single_level/r5.py --selftest
  --select PILOT        select the duty from the pilot
  --record FILE         record the selection
  --one-round-fallback  lowest duty when none qualifies
  --grid                the grid's duties and labels
  --split               the rounds that show arrival
  --ratio               adaptive / fixed-hi-probe ratio
  --cross-level         verdict against R4b's load A"""
import json
import re
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.score.single_level.r4 import need, num, steal_rate, above, fmt   # noqa: E402
from benchmark.score.single_level import r4 as r4_select   # noqa: E402

GRID = (5, 10, 15) + tuple(range(20, 43))
BAND_A = (0.20, 0.80)
PILOT_CONTROL = "pcn-r5p-control"
KEEP = 2 / 3
PILOT_ROUNDS = 5
ARM = "pcn-r5"
CONTROL = "pcn-r5-control"
R4B_FROZEN = ROOT / "results" / "single_level" / "r4b" / "pc-r4b.json"


def dtxt(d):
    """format a duty without a trailing .0"""
    return f"{d:g}"


def pilot_arm(d):
    """return the pilot label of a duty"""
    return "pcn-r5p-d" + dtxt(d).replace(".", "p")


def duty_stats(d, dd, prefix=""):
    """return a duty's pilot medians, drops and arrival counts"""
    rounds = [r if isinstance(r, dict) else {} for r in d.get("rounds", [])]
    arm, ctl = prefix + pilot_arm(dd), prefix + PILOT_CONTROL
    vals = [r[arm]["over_deadline_frac"] for r in rounds
            if isinstance(r.get(arm), dict) and num(r[arm].get("over_deadline_frac"))]
    drops = [r[arm]["dropped"] for r in rounds if isinstance(r.get(arm), dict) and num(r[arm].get("dropped"))]
    pairs = [(r[arm], r[ctl]) for r in rounds if isinstance(r.get(arm), dict) and isinstance(r.get(ctl), dict)]
    conf = sum(1 for a, c in pairs if above(a, c))
    return {"vals": vals, "med": median(vals) if vals else None, "drops": median(drops) if drops else None,
            "pairs": len(pairs), "conf": conf, "rates": [steal_rate(a) for a, _ in pairs],
            "crates": [steal_rate(c) for _, c in pairs]}


def select(d, prefix=""):
    """return the report lines, the selected duty or None, and the reason for none"""
    n = len(d.get("rounds", []))
    k = need(n, KEEP)
    lines = [f"pilot: {n} round(s) attempted; a duty needs results in at least {k}",
             f"rule (R4's, unchanged, with R4b's neighbour condition): the lowest duty in "
             f"{','.join(map(dtxt, GRID))} whose fixed-hi-probe median over_deadline_frac is in "
             f"[{BAND_A[0]:.2f}, {BAND_A[1]:.2f}], with steal_ticks per 1000 frames above the same "
             f"round's {PILOT_CONTROL} in every round holding both, and whose neighbour above has a "
             f"median at or above {BAND_A[0]:.2f}"]
    a = None
    in_a = []
    stats = {dd: duty_stats(d, dd, prefix) for dd in GRID}

    def neighbour_ok(dd):
        """check that the next grid duty's median is at or above the band"""
        i = GRID.index(dd)
        if i + 1 >= len(GRID):
            return False
        nb = stats[GRID[i + 1]]
        return len(nb["vals"]) >= k and nb["med"] >= BAND_A[0]

    for dd in GRID:
        s = stats[dd]
        why = []
        if len(s["vals"]) < k:
            why.append(f"results in {len(s['vals'])} round(s), fewer than {k}")
        if s["pairs"] < k:
            why.append(f"{s['pairs']} round(s) with its control, fewer than {k}")
        elif s["conf"] < s["pairs"]:
            why.append(f"arrival not shown in {s['pairs'] - s['conf']} round(s)")
        base_ok = not why
        in_band = len(s["vals"]) >= k and BAND_A[0] <= s["med"] <= BAND_A[1]
        if in_band:
            in_a.append(dd)
        ok = base_ok and in_band and neighbour_ok(dd)
        if base_ok and in_band and not ok:
            why.append(f"its neighbour above is not at or above {BAND_A[0]:.2f}" if dd != GRID[-1]
                       else "the top of the grid has no neighbour above")
        if ok and a is None:
            a = dd
        lines.append(f"duty {dtxt(dd)}: over_deadline_frac median {fmt(s['med'])} over {len(s['vals'])} "
                     f"round(s) [{', '.join(fmt(v) for v in s['vals'])}]; median dropped {fmt(s['drops'])}; "
                     f"arrival {s['conf']}/{s['pairs']} (steal/1000 frames "
                     f"{', '.join(fmt(x) for x in s['rates'])} against control "
                     f"{', '.join(fmt(x) for x in s['crates'])}) -> "
                     + ("selected" if dd == a else ("; ".join(why) if why else
                                                    "qualifies, above the selected duty" if ok else
                                                    "not selected")))
    reason = None
    if a is None:
        reason = (f"not run: no duty in the grid loads the nested loop into [{BAND_A[0]:.2f}, "
                  f"{BAND_A[1]:.2f}]" if not in_a else
                  f"not run: duties {','.join(map(dtxt, in_a))} loaded the nested loop into "
                  f"[{BAND_A[0]:.2f}, {BAND_A[1]:.2f}], but steal did not confirm arrival at any of "
                  f"them, or none had its neighbour above at or above {BAND_A[0]:.2f}")
    return lines, a, reason


def selection_text(d, prefix="", one_round_fallback=False):
    """return the selection text and the selected duty"""
    lines, a, reason = select(d, prefix)
    out = list(lines)
    if a is None and one_round_fallback and prefix:
        out += [f"ONE-ROUND: the rule selected none ({reason}); the grid's lowest duty is forced so the "
                f"batch runs", "one_round_forced=1"]
        a, reason = GRID[0], None
    if reason:
        out.append(reason)
    out.append(f"selected_duty={dtxt(a) if a is not None else 'none'}")
    return "\n".join(out) + "\n", a


def record(path, text):
    """write the selection once, or compare it with an existing one"""
    import subprocess
    p = Path(path)
    body = [x for x in text.splitlines() if not x.startswith("selected at ")]
    if p.exists():
        old = [x for x in p.read_text().splitlines() if not x.startswith("selected at ")]
        if old != body:
            print(f"r5_select: refusing -- the selection at {p} does not match the rule re-applied to the pilot", file=sys.stderr)
            return 3
        print(f"r5_select: the selection recorded at {p} matches the rule re-applied; not rewritten")
        return 0
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "-"
    p.write_text(f"selected at {sha}\n" + "\n".join(body) + "\n")
    print(f"r5_select: selection recorded at {p}")
    return 0


def split(d, prefix=""):
    """apply R4's arrival rule to the batch's rounds under R4's labels"""
    rounds = []
    for r in d.get("rounds", []):
        r = r if isinstance(r, dict) else {}
        x = {a: r[f"{prefix}{ARM}-{a}"] for a in r4_select.ARMS if isinstance(r.get(f"{prefix}{ARM}-{a}"), dict)}
        if isinstance(r.get(prefix + CONTROL), dict):
            x[r4_select.R4_CONTROL] = r[prefix + CONTROL]
        rounds.append(x)
    lines, kept, void = r4_select.arrival({"rounds": rounds})
    lines = [ln.replace("R4: VOID", "R5: VOID") for ln in lines]
    return lines, kept, void


def ratio_lines(rounds):
    """return the guard lines and the adaptive / fixed-hi-probe ratio line"""
    from benchmark.score.nested.quality_shedding import arm_median
    kept = [r for r in rounds if isinstance(r, dict) and r4_select.hit_ok(r)]
    out = list(r4_select.guards(kept))
    ad = arm_median(kept, "adaptive", "over_deadline_frac") if kept else None
    pr = arm_median(kept, "fixed-hi-probe", "over_deadline_frac") if kept else None
    if ad is None or pr is None or not pr:
        out.append(f"R5 ratio adaptive / fixed-hi-probe over_deadline_frac: - ({len(kept)} kept "
                   f"round(s); medians {ad} / {pr})")
    else:
        out.append(f"R5 ratio adaptive / fixed-hi-probe over_deadline_frac: {ad} / {pr} = {ad / pr:.6g} "
                   f"over {len(kept)} kept round(s) (pre-registered: at most {r4_select.RATIO_MAX}; "
                   f"scored by hand)")
    return out


def _level(text, ratio_re):
    """return the P6 verdict and ratio read from a score text"""
    p6 = re.search(r"^verdicts .*P6=(\w+)", text or "", re.M)
    rt = re.search(ratio_re, text or "", re.M)
    return (p6.group(1) if p6 else None, float(rt.group(1)) if rt else None)


def r4b_load_a(frozen=None):
    """return the P6 verdict and ratio of R4b's frozen load A"""
    try:
        t = json.loads(Path(frozen or R4B_FROZEN).read_text()).get("score_text", "")
    except (OSError, ValueError):
        t = ""
    a = t.split("load B:", 1)[0]
    return _level(a, r"^R4b load A ratio .*= ([0-9.eE+-]+) over")


def cross_level(r5_text, frozen=None):
    """return the cross-level statement lines for R5 against R4b's load A"""
    single = r4b_load_a(frozen)
    nested = _level(r5_text, r"^R5 ratio .*= ([0-9.eE+-]+) over")
    detail = (f"single-level (R4b load A, frozen) P6={single[0]}, ratio "
              f"{'-' if single[1] is None else f'{single[1]:.6g}'}; nested (R5) P6={nested[0]}, ratio "
              f"{'-' if nested[1] is None else f'{nested[1]:.6g}'}")
    if None in single or None in nested:
        missing = [n for n, v in (("single-level", single), ("nested", nested)) if None in v]
        return [f"cross-level: not applicable -- {' and '.join(missing)} has no P6 verdict and ratio "
                f"(VOID, not run or not found); {detail}", "cross-level verdict: NOT APPLICABLE"]
    hit = all(p6 == "HIT" and r <= r4_select.RATIO_MAX for p6, r in (single, nested))
    return [f"cross-level: P6 HIT and the ratio at most {r4_select.RATIO_MAX} at both levels? {detail}",
            f"cross-level verdict: {'HIT' if hit else 'MISS'}"]


def _pilot(med, drops=None, steal=None, rounds=5, prefix=""):
    """build a pilot batch from per-duty over_deadline_frac, drops and steal"""
    rs = []
    for i in range(rounds):
        r = {PILOT_CONTROL: {"frames": 20000, "steal_ticks": 40, "over_deadline_frac": 0.002, "dropped": 9}}
        for dd, v in med.items():
            r[pilot_arm(dd)] = {"frames": 20000, "steal_ticks": (steal or {}).get(dd, [600] * rounds)[i],
                                "over_deadline_frac": v[i], "dropped": (drops or {}).get(dd, 2)}
        rs.append({prefix + x: y for x, y in r.items()})
    return {"rounds": rs}


def _batch(n=16, low=(), od=0.3, prefix=""):
    """build a confirmatory batch with chosen low-steal rounds"""
    rs = []
    for i in range(n):
        arm = lambda o, s, hit=5: {"frames": 20000, "steal_ticks": s, "over_deadline_frac": o, "hit": hit,  # noqa: E731
                                   "p999_ns": 1000, "compute_p50_ns": 700, "dropped": 0, "mean_relerr": 0.01,
                                   "full_quality_frac": 0.5}
        r = {CONTROL: arm(0.002, 10), f"{ARM}-fixed-hi": arm(od, 5 if i in low else 600),
             f"{ARM}-fixed-hi-probe": arm(od, 600), f"{ARM}-adaptive": arm(0.001, 500),
             f"{ARM}-fixed-lo": arm(0.0, 300)}
        rs.append({prefix + k: v for k, v in r.items()})
    return {"topology": "nested", "rounds": rs}


def selftest():
    """run the rule cases and return 1 on any failure"""
    import tempfile
    cases = []
    sel = lambda *a, **k: select(*a, **k)[1]           # noqa: E731
    curve = {d: (0.002 if d < 30 else 0.05 if d < 33 else 0.3 if d < 36 else 0.42) for d in GRID}
    base = {d: [v] * 5 for d, v in curve.items()}
    cases.append(("the grid brackets the edge below the levels comparison's saturated L2 (42.1): 5 to 42, steps of 1 "
                  "from 20", GRID[0] == 5 and GRID[-1] == 42 < 42.1 and all(b - a == 1 for a, b in zip(GRID[3:], GRID[4:]))))
    cases.append(("R4's rule: the lowest duty in [0.20, 0.80] with arrival", sel(_pilot(base)) == 33))
    cases.append(("a duty with results in 3 of 5 rounds does not count",
                  sel(_pilot({**base, 33: [0.3, 0.3, 0.3, None, None]})) == 34))
    cases.append(("a steal tie in one round skips the duty", sel(_pilot(base, steal={33: [600, 40, 600, 600, 600]})) == 34))
    spike = {**base, 25: [0.25] * 5, 26: [0.05] * 5}
    cases.append(("an isolated duty in the band with its neighbour above below 0.20 is skipped (R4b)",
                  sel(_pilot(spike)) == 33))
    top = {d: [0.01] * 5 for d in GRID}
    top[GRID[-1]] = [0.3] * 5
    got = select(_pilot(top))
    cases.append(("the top of the grid is never selected", got[1] is None and "neighbour" in got[2]))
    got = select(_pilot({d: [0.01] * 5 for d in GRID}))
    cases.append(("none in the band: not run, the reason says so", got[1] is None and "no duty in the grid" in got[2]))
    got = select(_pilot(base, steal={d: [40] * 5 for d in GRID}))
    cases.append(("in the band but steal never above the control: not run, the arrival reason",
                  got[1] is None and "steal did not confirm arrival" in got[2]))
    cases.append(("the plateau with drops is still R4's band (no drop cap; disclosed)",
                  sel(_pilot({d: [0.002 if d < 38 else 0.41] * 5 for d in GRID}, drops={d: 3977 for d in GRID})) == 38))
    t, a = selection_text(_pilot({d: [0.01] for d in GRID}, rounds=1, prefix="one-round-"), "one-round-", True)
    cases.append(("a one-round run's none forces the lowest duty, marked", a == 5 and "one_round_forced=1" in t and t.endswith("selected_duty=5\n")))
    t, a = selection_text(_pilot(base))
    cases.append(("the record ends with the one selection", a == 33 and t.endswith("selected_duty=33\n") and "load B" not in t))
    cases.append(("pilot labels are nested and R5's own", pilot_arm(20) == "pcn-r5p-d20" and PILOT_CONTROL.startswith("pcn-r5p")))
    with tempfile.TemporaryDirectory() as td:
        rec = Path(td) / "s.selection"
        cases.append(("recorded once; re-applied it only compares; a mismatch refuses (3)",
                      record(rec, t) == 0 and record(rec, t) == 0
                      and record(rec, t.replace("selected_duty=33", "selected_duty=34")) == 3
                      and rec.read_text().endswith("selected_duty=33\n")))
    lines, kept, void = split(_batch())
    cases.append(("split keeps the rounds by R4's arrival rule against R5's control",
                  len(kept) == 16 and not void and set(kept[0]) == {"fixed-hi", "fixed-hi-probe", "adaptive", "fixed-lo", "pc-r4-control"}))
    lines, kept, void = split(_batch(low=range(5)))
    cases.append(("11 of 16 arriving is VOID, named R5", void and len(kept) == 11 and any("R5: VOID" in x for x in lines)))
    lines, kept, void = split(_batch(prefix="one-round-", n=1), "one-round-")
    cases.append(("a one-round run's batch splits through its prefix", len(kept) == 1))
    rl = ratio_lines(split(_batch())[1])
    cases.append(("the ratio with R4's guards", any(x.startswith("R5 ratio") and "= 0.00333333 over 16" in x for x in rl)
                  and any(x.startswith("guard P6") for x in rl) and any(x.startswith("guard P7/P8") for x in rl)))
    r5 = "verdicts  P5=MISS  P6=HIT  P7=MISS  P8=HIT\nR5 ratio adaptive / fixed-hi-probe over_deadline_frac: 0.001 / 0.3 = 0.00333333 over 16 kept round(s)"
    with tempfile.TemporaryDirectory() as td:
        fz = Path(td) / "pc-r4b.json"
        fz.write_text(json.dumps({"score_text": "load A: arrival\nverdicts  P5=MISS  P6=HIT  P7=HIT  P8=HIT\n"
                                  "R4b load A ratio adaptive / fixed-hi-probe over_deadline_frac: 0.00055 / 0.2665 = 0.00206406 over 16 kept round(s)\n"
                                  "load B: arrival\nverdicts  P5=MISS  P6=MISS  P7=HIT  P8=HIT\n"
                                  "R4b load B ratio adaptive / fixed-hi-probe over_deadline_frac: 0.6 / 1 = 0.6 over 16 kept round(s)\n"}))
        cases.append(("R4b's load A is read from its frozen score, not load B", r4b_load_a(fz) == ("HIT", 0.00206406)))
        cases.append(("cross-level HIT when P6 HIT and the ratio at most 0.5 at both levels",
                      cross_level(r5, fz)[-1] == "cross-level verdict: HIT"))
        cases.append(("cross-level MISS when the nested P6 misses",
                      cross_level(r5.replace("P6=HIT", "P6=MISS"), fz)[-1] == "cross-level verdict: MISS"))
        cases.append(("cross-level MISS when the nested ratio is above 0.5",
                      cross_level(r5.replace("= 0.00333333", "= 0.6"), fz)[-1] == "cross-level verdict: MISS"))
        cases.append(("cross-level not applicable when R5 is VOID or not run",
                      cross_level("", fz)[-1] == "cross-level verdict: NOT APPLICABLE"))
        cases.append(("cross-level not applicable when the frozen R4b cannot be read",
                      cross_level(r5, Path(td) / "absent.json")[-1] == "cross-level verdict: NOT APPLICABLE"))
    cases.append(("the committed R4b load A reads P6 HIT, ratio 0.00206406",
                  r4b_load_a() == ("HIT", 0.00206406)))
    bad = [c for c, ok in cases if not ok]
    for c, ok in cases:
        print(("ok   " if ok else "FAIL ") + c)
    print(f"r5_select selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv=None):
    """parse the arguments and run a mode"""
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    if argv[:1] == ["--grid"]:
        for d in GRID:
            print(dtxt(d), pilot_arm(d))
        return 0
    prefix = argv[argv.index("--prefix") + 1] if "--prefix" in argv else ""
    if argv[:1] in (["--select"], ["--split"], ["--ratio"]) and len(argv) >= 2:
        try:
            d = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError) as e:
            print(f"r5_select: cannot read {argv[1]} ({e})", file=sys.stderr)
            return 1
    if argv[:1] == ["--select"] and len(argv) >= 2:
        text, _ = selection_text(d, prefix, "--one-round-fallback" in argv)
        print(text, end="")
        if "--record" in argv:
            return record(argv[argv.index("--record") + 1], text)
        return 0
    if argv[:1] == ["--split"] and len(argv) >= 3:
        lines, kept, void = split(d, prefix)
        print("\n".join(lines))
        Path(argv[2]).write_text(json.dumps({"metric": d.get("metric", "over_deadline_frac"),
                                             "topology": d.get("topology"), "rounds": kept}, indent=1))
        return 4 if void else 0
    if argv[:1] == ["--ratio"] and len(argv) >= 2:
        print("\n".join(ratio_lines(d.get("rounds", []))))
        return 0
    if argv[:1] == ["--cross-level"] and len(argv) >= 2:
        try:
            t = Path(argv[1]).read_text()
        except OSError:
            t = ""
        print("\n".join(cross_level(t, argv[2] if len(argv) >= 3 else None)))
        return 0
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
