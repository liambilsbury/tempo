#!/usr/bin/env python3
"""Apply R4b's two load selections to the pilot and score the batch per load

Usage: benchmark/score/single_level/r4b.py --select PILOT [--record FILE] [--prefix P] [--one-round-fallback]
       benchmark/score/single_level/r4b.py --grid
       benchmark/score/single_level/r4b.py --split BATCH a|b OUT [--prefix P]
       benchmark/score/single_level/r4b.py --ratio a|b ARRIVED
       benchmark/score/single_level/r4b.py --cross SCORE_A SCORE_B
       benchmark/score/single_level/r4b.py --selftest
  --select PILOT        select both loads' duties from the pilot
  --record FILE         record the selection
  --one-round-fallback  lowest duty for a load with none
  --grid                the grid's duties and labels
  --split               one load's rounds that show arrival
  --ratio               one load's adaptive / fixed-hi-probe ratio
  --cross               cross-load verdict"""
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.score.single_level.r4 import need, num, steal_rate, above, fmt   # noqa: E402
from benchmark.score.single_level import r4 as r4_select   # noqa: E402

GRID = (42, 42.5, 42.75, 43, 43.25, 43.5, 43.75, 44, 44.25, 44.5, 44.75, 45, 45.5)
BAND_A = (0.20, 0.80)
BAND_B = (0.40, 0.80)
DROP_MAX_B = 50
PILOT_CONTROL = "pc-r4bp-control"
KEEP = 2 / 3
PILOT_ROUNDS = 5
LOADS = ("a", "b")
CONTROL = "pc-r4b-control"


def dtxt(d):
    """format a duty without a trailing .0"""
    return f"{d:g}"


def pilot_arm(d):
    """return the pilot label of a duty"""
    return "pc-r4bp-d" + dtxt(d).replace(".", "p")


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
    """return the report lines, loads A and B or None, and the reason for each none"""
    n = len(d.get("rounds", []))
    k = need(n, KEEP)
    lines = [f"pilot: {n} round(s) attempted; a duty needs results in at least {k}",
             f"rule A (R4's, unchanged): the lowest duty in {','.join(map(dtxt, GRID))} whose "
             f"fixed-hi-probe median over_deadline_frac is in [{BAND_A[0]:.2f}, {BAND_A[1]:.2f}], with "
             f"steal_ticks per 1000 frames above the same round's {PILOT_CONTROL} in every round holding both",
             f"rule B: the lowest duty strictly above A whose median is in [{BAND_B[0]:.2f}, "
             f"{BAND_B[1]:.2f}], with median dropped frames at most {DROP_MAX_B}, arrival as for A"]
    a = b = None
    in_a = []
    stats = {dd: duty_stats(d, dd, prefix) for dd in GRID}

    def neighbour_ok(dd, edge):
        """check that the next grid duty's median is at or above edge"""
        i = GRID.index(dd)
        if i + 1 >= len(GRID):
            return False
        nb = stats[GRID[i + 1]]
        return len(nb["vals"]) >= k and nb["med"] >= edge

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
        in_band_a = len(s["vals"]) >= k and BAND_A[0] <= s["med"] <= BAND_A[1]
        if in_band_a:
            in_a.append(dd)
        ok_a = base_ok and in_band_a and neighbour_ok(dd, BAND_A[0])
        if base_ok and in_band_a and not ok_a:
            why.append(f"its neighbour above is not at or above {BAND_A[0]:.2f}" if dd != GRID[-1]
                       else "the top of the grid has no neighbour above")
        ok_b = (base_ok and a is not None and dd > a and len(s["vals"]) >= k
                and BAND_B[0] <= s["med"] <= BAND_B[1] and s["drops"] is not None and s["drops"] <= DROP_MAX_B
                and neighbour_ok(dd, BAND_B[0]))
        if ok_a and a is None:
            a = dd
            ok_b = False
        if ok_b and b is None:
            b = dd
        tags = [t for t, ok in (("A", dd == a), ("B", dd == b)) if ok]
        lines.append(f"duty {dtxt(dd)}: over_deadline_frac median {fmt(s['med'])} over {len(s['vals'])} "
                     f"round(s) [{', '.join(fmt(v) for v in s['vals'])}]; median dropped {fmt(s['drops'])}; "
                     f"arrival {s['conf']}/{s['pairs']} (steal/1000 frames "
                     f"{', '.join(fmt(x) for x in s['rates'])} against control "
                     f"{', '.join(fmt(x) for x in s['crates'])}) -> "
                     + (f"selected for load {' and '.join(tags)}" if tags else
                        ("; ".join(why) if why else "not selected")))
    ra = rb = None
    if a is None:
        ra = (f"not run: no duty in the grid loads the loop into [{BAND_A[0]:.2f}, {BAND_A[1]:.2f}]"
              if not in_a else f"not run: duties {','.join(map(dtxt, in_a))} loaded the loop into "
              f"[{BAND_A[0]:.2f}, {BAND_A[1]:.2f}], but steal did not confirm arrival at any of them, "
              f"or none had its neighbour above at or above {BAND_A[0]:.2f}")
        rb = "not run: load A was not selected"
    elif b is None:
        rb = (f"not run: no duty above A ({dtxt(a)}) in [{BAND_B[0]:.2f}, {BAND_B[1]:.2f}] with at most "
              f"{DROP_MAX_B} dropped frames, arrival shown and its neighbour above at or above "
              f"{BAND_B[0]:.2f}")
    return lines, a, b, ra, rb


def selection_text(d, prefix="", one_round_fallback=False):
    """return the selection text and loads A and B"""
    lines, a, b, ra, rb = select(d, prefix)
    out = list(lines)
    if (a is None or b is None) and one_round_fallback and prefix:
        fa = a if a is not None else GRID[0]
        above_a = [g for g in GRID if g > fa]
        fb = b if b is not None else (above_a[0] if above_a else fa)
        out += [f"ONE-ROUND: the rule selected A={dtxt(a) if a is not None else 'none'}, "
                f"B={dtxt(b) if b is not None else 'none'} ({ra or rb}); the missing load(s) are forced "
                f"so both loads' levels run", "one_round_forced=1"]
        a, b, ra, rb = fa, fb, None, None
    for r in (ra, rb):
        if r:
            out.append(r)
    out.append(f"selected_duty_a={dtxt(a) if a is not None else 'none'}")
    out.append(f"selected_duty_b={dtxt(b) if b is not None else 'none'}")
    return "\n".join(out) + "\n", a, b


def record(path, text):
    """write the selection once, or compare it with an existing one"""
    import subprocess
    p = Path(path)
    body = [x for x in text.splitlines() if not x.startswith("selected at ")]
    if p.exists():
        old = [x for x in p.read_text().splitlines() if not x.startswith("selected at ")]
        if old != body:
            print(f"r4b_select: refusing -- the selection at {p} does not match the rule re-applied to the pilot", file=sys.stderr)
            return 3
        print(f"r4b_select: the selection recorded at {p} matches the rule re-applied; not rewritten")
        return 0
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "-"
    p.write_text(f"selected at {sha}\n" + "\n".join(body) + "\n")
    print(f"r4b_select: selection recorded at {p}")
    return 0


def split(d, load, prefix=""):
    """apply R4's arrival rule to a load's rounds under R4's labels"""
    rounds = []
    for r in d.get("rounds", []):
        r = r if isinstance(r, dict) else {}
        x = {a: r[f"{prefix}{load}-{a}"] for a in r4_select.ARMS if isinstance(r.get(f"{prefix}{load}-{a}"), dict)}
        if isinstance(r.get(prefix + CONTROL), dict):
            x[r4_select.R4_CONTROL] = r[prefix + CONTROL]
        rounds.append(x)
    lines, kept, void = r4_select.arrival({"rounds": rounds})
    lines = [f"load {load.upper()}: " + ln.replace("R4: VOID", f"R4b load {load.upper()}: VOID") for ln in lines]
    return lines, kept, void


def ratio_lines(load, rounds):
    """return a load's guard lines and adaptive / fixed-hi-probe ratio line"""
    from benchmark.score.nested.quality_shedding import arm_median
    kept = [r for r in rounds if isinstance(r, dict) and r4_select.hit_ok(r)]
    out = [g.replace("guard ", f"load {load.upper()} guard ", 1) for g in r4_select.guards(kept)]
    ad = arm_median(kept, "adaptive", "over_deadline_frac") if kept else None
    pr = arm_median(kept, "fixed-hi-probe", "over_deadline_frac") if kept else None
    if ad is None or pr is None or not pr:
        out.append(f"R4b load {load.upper()} ratio adaptive / fixed-hi-probe over_deadline_frac: - "
                   f"({len(kept)} kept round(s); medians {ad} / {pr})")
    else:
        out.append(f"R4b load {load.upper()} ratio adaptive / fixed-hi-probe over_deadline_frac: {ad} / {pr} "
                   f"= {ad / pr:.6g} over {len(kept)} kept round(s) (pre-registered: at most "
                   f"{r4_select.RATIO_MAX}; scored by hand)")
    return out


def cross(texts):
    """return the cross-load statement lines from each load's score text"""
    import re
    got = {}
    for load in LOADS:
        t = texts.get(load) or ""
        p6 = re.search(r"^verdicts .*P6=(\w+)", t, re.M)
        rt = re.search(r"^R4b load %s ratio .*= ([0-9.eE+-]+) over" % load.upper(), t, re.M)
        got[load] = (p6.group(1) if p6 else None, float(rt.group(1)) if rt else None)
    if any(v[0] is None or v[1] is None for v in got.values()):
        missing = [l.upper() for l, v in got.items() if v[0] is None or v[1] is None]
        return [f"cross-load: not applicable -- load {' and '.join(missing)} has no P6 verdict and ratio "
                f"(VOID or not run)", "cross-load verdict: NOT APPLICABLE"]
    hit = all(p6 == "HIT" and r <= r4_select.RATIO_MAX for p6, r in got.values())
    detail = "; ".join(f"load {l.upper()} P6={v[0]}, ratio {v[1]:.6g}" for l, v in got.items())
    return [f"cross-load: P6 HIT and the ratio at most {r4_select.RATIO_MAX} at both loads? {detail}",
            f"cross-load verdict: {'HIT' if hit else 'MISS'}"]


def _pilot(med, drops=None, steal=None, rounds=3, prefix=""):
    """build a pilot batch from per-duty over_deadline_frac, drops and steal"""
    rs = []
    for i in range(rounds):
        r = {PILOT_CONTROL: {"frames": 20000, "steal_ticks": 40, "over_deadline_frac": 0.0001, "dropped": 0}}
        for dd, v in med.items():
            r[pilot_arm(dd)] = {"frames": 20000, "steal_ticks": (steal or {}).get(dd, [600] * rounds)[i],
                                "over_deadline_frac": v[i], "dropped": (drops or {}).get(dd, 2)}
        rs.append({prefix + x: y for x, y in r.items()})
    return {"rounds": rs}


def selftest():
    """run the rule cases and return 1 on any failure"""
    cases = []
    curve = {42: .02, 42.5: .25, 42.75: .3, 43: .31, 43.25: .33, 43.5: .35, 43.75: .38, 44: .41,
             44.25: .48, 44.5: .48, 44.75: .49, 45: .49, 45.5: .48}
    base = {d: [v] * 3 for d, v in curve.items()}
    sel = lambda *a, **k: select(*a, **k)[1:3]           # noqa: E731
    cases.append(("A is R4's rule (lowest in [0.20, 0.80]); B the lowest above A in [0.40, 0.80]",
                  sel(_pilot(base)) == (42.5, 44)))
    cases.append(("B needs at most 50 dropped frames: 44 dropping 60 moves B to 44.25",
                  sel(_pilot(base, drops={44: 60})) == (42.5, 44.25)))
    cases.append(("exactly 50 dropped still qualifies", sel(_pilot(base, drops={44: 50})) == (42.5, 44)))
    cases.append(("B is strictly above A even when A itself is in B's band",
                  sel(_pilot({d: [0.45 if d >= 43 else 0.01] * 3 for d in GRID})) == (43, 43.25)))
    got = select(_pilot({d: [0.01] * 3 for d in GRID}))
    cases.append(("no A: neither load, and both reasons say so",
                  got[1:3] == (None, None) and "no duty in the grid" in got[3] and "load A was not selected" in got[4]))
    got = select(_pilot({d: [0.3 if d < 43 else 0.35] * 3 for d in GRID}))
    cases.append(("A but no heavier duty in B's band: B not run with its reason, A runs",
                  got[1] == 42 and got[2] is None and "no duty above A" in got[4]))
    cases.append(("a steal tie at A's duty skips it for A",
                  sel(_pilot(base, steal={42.5: [600, 40, 600]}))[0] == 42.75))
    got = select(_pilot(base, steal={d: [40] * 3 for d in GRID}))
    cases.append(("in the band but steal never above the control: none, the arrival reason",
                  got[1] is None and "steal did not confirm arrival" in got[3]))
    t, a, b = selection_text(_pilot({d: [0.01] * 1 for d in GRID}, rounds=1, prefix="one-round-"), "one-round-", True)
    cases.append(("a one-round run's none forces the two lowest duties, marked",
                  (a, b) == (42, 42.5) and "one_round_forced=1" in t and t.endswith("selected_duty_a=42\nselected_duty_b=42.5\n")))
    t, a, b = selection_text(_pilot({d: [0.3 if d < 43 else 0.35] * 1 for d in GRID}, rounds=1, prefix="one-round-"), "one-round-", True)
    cases.append(("a one-round run whose pilot selects A but no B forces B too, so both loads run",
                  (a, b) == (42, 42.5) and "one_round_forced=1" in t))
    t, a, b = selection_text(_pilot(base))
    cases.append(("the record ends with both selections", t.endswith("selected_duty_a=42.5\nselected_duty_b=44\n")))
    cases.append(("labels spell a fraction with p", pilot_arm(42.75) == "pc-r4bp-d42p75"))
    spike = {**base, 42.5: [0.25] * 3, 42.75: [0.05] * 3}
    cases.append(("an isolated duty in the band with its neighbour above below 0.20 is skipped for A",
                  sel(_pilot(spike))[0] == 43))
    top = {d: [0.01] * 3 for d in GRID}
    top[GRID[-1]] = [0.3] * 3
    got = select(_pilot(top))
    cases.append(("the top of the grid is never selected (no neighbour above)", got[1] is None and "neighbour" in got[3]))
    bspike = {**base, 44: [0.41] * 3, 44.25: [0.39] * 3, 44.5: [0.45] * 3}
    cases.append(("B's neighbour rule: 44 with 44.25 below 0.40 is skipped, 44.5 needs 45 at or above 0.40",
                  sel(_pilot(bspike)) == (42.5, 44.5)))
    def conf(n=16, low_b=()):
        """build an interleaved batch with chosen low-steal load B rounds"""
        rs = []
        for i in range(n):
            arm = lambda od, s, hit=5: {"frames": 20000, "steal_ticks": s, "over_deadline_frac": od, "hit": hit,
                                        "p999_ns": 1000, "compute_p50_ns": 700, "dropped": 0, "mean_relerr": 0.01,
                                        "full_quality_frac": 0.5}
            r = {CONTROL: arm(0.0, 10)}
            for ld, od in (("a", 0.3), ("b", 0.45)):
                low = ld == "b" and i in low_b
                r.update({f"{ld}-fixed-hi": arm(od, 5 if low else 600), f"{ld}-fixed-hi-probe": arm(od, 600),
                          f"{ld}-adaptive": arm(0.001, 500), f"{ld}-fixed-lo": arm(0.0, 300)})
            rs.append(r)
        return {"topology": "single-level", "rounds": rs}
    la, ka, va = split(conf(), "a")
    lb, kb, vb = split(conf(low_b=range(5)), "b")
    cases.append(("split keeps a load's rounds by R4's arrival rule against the shared control",
                  len(ka) == 16 and not va and set(ka[0]) == {"fixed-hi", "fixed-hi-probe", "adaptive", "fixed-lo", "pc-r4-control"}))
    cases.append(("a load with 11 of 16 rounds arriving is VOID on its own, the other load unaffected",
                  vb and len(kb) == 11 and any("R4b load B: VOID" in x for x in lb)))
    rl = ratio_lines("a", ka)
    cases.append(("the ratio is per load, with R4's guards", any(x.startswith("R4b load A ratio") and "= 0.00333333 over 16" in x for x in rl)
                  and any(x.startswith("load A guard P6") for x in rl)))
    sa = "verdicts  P5=MISS  P6=HIT  P7=MISS  P8=HIT\nR4b load A ratio adaptive / fixed-hi-probe over_deadline_frac: 0.001 / 0.3 = 0.00333333 over 16 kept round(s)"
    sb = sa.replace("load A", "load B")
    cases.append(("cross-load HIT when P6 HIT and the ratio at most 0.5 at both", cross({"a": sa, "b": sb})[-1].endswith("HIT")))
    cases.append(("cross-load MISS when one load's P6 misses",
                  cross({"a": sa, "b": sb.replace("P6=HIT", "P6=MISS")})[-1].endswith("MISS")))
    cases.append(("cross-load MISS when one load's ratio is above 0.5",
                  cross({"a": sa, "b": sb.replace("= 0.00333333", "= 0.6")})[-1].endswith("MISS")))
    cases.append(("cross-load not applicable when a load is VOID or not run",
                  cross({"a": sa, "b": None})[-1].endswith("NOT APPLICABLE")))
    bad = [c for c, ok in cases if not ok]
    for c, ok in cases:
        print(("ok   " if ok else "FAIL ") + c)
    print(f"r4b_select selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    """parse the arguments and run a mode"""
    import json
    if argv == ["--selftest"]:
        return selftest()
    if argv == ["--grid"]:
        for d in GRID:
            print(dtxt(d), pilot_arm(d))
        return 0
    if len(argv) >= 2 and argv[0] == "--select":
        rest, opts, fb = argv[2:], {"--record": None, "--prefix": ""}, False
        while rest:
            x = rest.pop(0)
            if x == "--one-round-fallback":
                fb = True
            elif x in opts and rest:
                opts[x] = rest.pop(0)
            else:
                print(__doc__, file=sys.stderr)
                return 2
        try:
            d = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError) as e:
            print(f"r4b_select: cannot read the pilot {argv[1]} ({e})", file=sys.stderr)
            return 1
        text, _, _ = selection_text(d, opts["--prefix"], fb)
        print(text, end="")
        if opts["--record"]:
            return record(opts["--record"], text)
        return 0
    if len(argv) in (4, 6) and argv[0] == "--split" and argv[2] in LOADS:
        prefix = ""
        if len(argv) == 6:
            if argv[4] != "--prefix":
                print(__doc__, file=sys.stderr)
                return 2
            prefix = argv[5]
        try:
            d = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError) as e:
            print(f"r4b_select: cannot read {argv[1]} ({e})", file=sys.stderr)
            return 1
        lines, kept, void = split(d, argv[2], prefix)
        print("\n".join(lines))
        Path(argv[3]).write_text(json.dumps({"metric": d.get("metric", "over_deadline_frac"),
                                             "topology": d.get("topology"), "rounds": kept}, indent=1))
        return 4 if void else 0
    if len(argv) == 3 and argv[0] == "--ratio" and argv[1] in LOADS:
        try:
            d = json.loads(Path(argv[2]).read_text())
        except (OSError, ValueError):
            d = {"rounds": []}
        print("\n".join(ratio_lines(argv[1], d.get("rounds", []))))
        return 0
    if len(argv) == 3 and argv[0] == "--cross":
        texts = {}
        for load, f in zip(LOADS, argv[1:]):
            try:
                texts[load] = Path(f).read_text()
            except OSError:
                texts[load] = None
        print("\n".join(cross(texts)))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
