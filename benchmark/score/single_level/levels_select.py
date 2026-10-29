#!/usr/bin/env python3
"""Apply the virtualisation levels' pilot selection rule

Usage: levels_select.py --select PILOT [--record FILE] [--prefix P] [--one-round-fallback] [--range A-B] [--variant levels2]
       levels_select.py --coverage BATCH LABEL...
       levels_select.py --grid [--variant levels2]
       levels_select.py --verify-frozen SELECTION FROZEN
       levels_select.py --selftest
  --select PILOT        select the duty from the pilot
  --record FILE         record the selection
  --one-round-fallback  lowest duty when none qualifies
  --range A-B           frames to read
  --variant levels2     use revision 2's grid and labels
  --coverage            runs per label covering the range
  --grid                the grid's duties and labels
  --verify-frozen       check revision 1's selection against its frozen record"""
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from statistics import median

GRID = (36, 38, 40, 41, 42, 43, 43.5, 44, 44.25, 44.5, 44.75, 45, 45.5, 46, 47, 48)
GRID2 = tuple(round(42 + i / 10, 1) for i in range(11))
VARIANTS = {"levels": (GRID, "pc-ldp"), "levels2": (GRID2, "pc-lbp")}
BAND_MIN, BAND_MAX = 200.0, 600.0
RANGE = (2000, 9000)
PILOT_KEEP_FRACTION = 2 / 3
PILOT_CONTROL = "pc-ldp-control"
ROOT = Path(__file__).resolve().parents[3]


def dtxt(d):
    """format a duty without a trailing .0"""
    return f"{d:g}"


def pilot_arm(d, variant="levels"):
    """return the pilot label of a duty"""
    return VARIANTS[variant][1] + "-d" + dtxt(d).replace(".", "p")


def pilot_control(variant="levels"):
    """return the pilot control label of a variant"""
    return VARIANTS[variant][1] + "-control"


def need(n, fraction):
    """return the round count a fraction of n requires, at least 1"""
    return max(1, math.ceil(n * fraction - 1e-9))


def num(x):
    """check that x is an int or float, excluding bool"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def steal_rate(a):
    """return steal_ticks per 1000 frames, or None"""
    if not isinstance(a, dict) or not num(a.get("steal_ticks")) or not num(a.get("frames")) or a["frames"] <= 0:
        return None
    return 1000.0 * a["steal_ticks"] / a["frames"]


def lost(a, rng):
    """return frames lost per 1000 of the range, or None when not covered"""
    if (not isinstance(a, dict) or not num(a.get("range_lost")) or a.get("range_frames") != rng[1] - rng[0]
            or a.get("range_covered") != 1):
        return None
    return 1000.0 * a["range_lost"] / (rng[1] - rng[0])


def fmt(x):
    """format a number to 4 significant digits, or - for None"""
    return "-" if x is None else f"{x:.4g}"


def select(d, prefix="", rng=RANGE, variant="levels"):
    """return the report lines, the selected duty or None, and the reason for none"""
    grid = VARIANTS[variant][0]
    rounds = [r if isinstance(r, dict) else {} for r in d.get("rounds", [])]
    n = len(rounds)
    k = need(n, PILOT_KEEP_FRACTION)
    ctl = prefix + pilot_control(variant)
    lines = [f"pilot: {n} round(s) attempted; a duty needs results in at least {k}",
             f"rule: the lowest duty in {','.join(map(dtxt, grid))} whose L1 hog median frames lost "
             f"per 1000 of frames {rng[0]}-{rng[1]} is in [{BAND_MIN:g}, {BAND_MAX:g}] (a round's "
             f"result counts only when the window covered the range in its hog run and its "
             f"control), with steal_ticks per 1000 frames above the same round's {PILOT_CONTROL} "
             f"in every round holding both"]
    if variant != "levels":
        lines[1] = lines[1].replace(PILOT_CONTROL, pilot_control(variant))
    chosen, in_band, any_covered = None, [], False
    below, uncovered = [], []
    for dd in grid:
        arm = prefix + pilot_arm(dd, variant)
        vals = []
        for r in rounds:
            h, c = r.get(arm), r.get(ctl)
            v = lost(h, rng)
            if v is not None and isinstance(c, dict) and c.get("range_covered") == 1:
                vals.append(v)
        if len(vals) >= k:
            any_covered = True
        med = median(vals) if vals else None
        pairs = [(r[arm], r[ctl]) for r in rounds if isinstance(r.get(arm), dict) and isinstance(r.get(ctl), dict)]
        conf = sum(1 for a, c in pairs
                   if steal_rate(a) is not None and steal_rate(c) is not None and steal_rate(a) > steal_rate(c))
        why = []
        band = len(vals) >= k and BAND_MIN <= med <= BAND_MAX
        if len(vals) < k:
            why.append(f"covered results in {len(vals)} round(s), fewer than {k}")
            uncovered.append(dd)
        elif med < BAND_MIN:
            below.append(dd)
            why.append(f"median below {BAND_MIN:g}")
        elif med > BAND_MAX:
            why.append(f"median above {BAND_MAX:g}")
        if band:
            in_band.append(dd)
        if len(pairs) < k:
            why.append(f"{len(pairs)} round(s) with its control, fewer than {k}")
        elif conf < len(pairs):
            why.append(f"arrival not shown in {len(pairs) - conf} round(s)")
        ok = not why
        if ok and chosen is None:
            chosen = dd
        lines.append(f"duty {dtxt(dd)}: frames lost per 1000 of the range median {fmt(med)} over "
                     f"{len(vals)} covered round(s) [{', '.join(fmt(v) for v in vals)}]; arrival "
                     f"{conf}/{len(pairs)} (steal/1000 frames "
                     f"{', '.join(fmt(steal_rate(a)) for a, _ in pairs)} against control "
                     f"{', '.join(fmt(steal_rate(c)) for _, c in pairs)}) -> "
                     f"{'qualifies' if ok else 'no: ' + '; '.join(why)}")
    reason = None
    hidden = [dd for dd in uncovered if dd > max(below, default=float("-inf"))]
    if chosen is None and any_covered and hidden and not in_band:
        reason = (f"not run: the injection window did not cover frames {rng[0]}-{rng[1]} in enough "
                  f"pilot rounds at duty {','.join(map(dtxt, hidden))}, above the highest covered duty "
                  f"below the band")
    elif chosen is None and not any_covered:
        reason = (f"not run: the injection window did not cover frames {rng[0]}-{rng[1]} in "
                  f"enough pilot rounds at any duty")
    elif chosen is None and not in_band:
        reason = (f"not run: no duty in the grid loads L1 into [{BAND_MIN:g}, {BAND_MAX:g}] frames "
                  f"lost per 1000 of the range")
    elif chosen is None:
        reason = (f"not run: duties {','.join(map(dtxt, in_band))} loaded L1 into the band, but "
                  f"steal did not confirm arrival at any of them")
    return lines, chosen, reason


def selection_text(d, prefix="", one_round_fallback=False, rng=RANGE, variant="levels"):
    """return the selection text and the selected duty"""
    lines, chosen, reason = select(d, prefix, rng, variant)
    out = list(lines)
    if chosen is None and one_round_fallback and prefix:
        out += [f"ONE-ROUND: the rule selected none ({reason}); the grid's lowest duty is forced so the "
                f"one-round run's levels still run", "one_round_forced=1"]
        chosen = VARIANTS[variant][0][0]
    elif reason:
        out.append(reason)
    out.append(f"selected_duty={dtxt(chosen) if chosen is not None else 'none'}")
    return "\n".join(out) + "\n", chosen


def record(path, text):
    """write the selection once, or compare it with an existing one"""
    p = Path(path)
    body = [ln for ln in text.splitlines() if not ln.startswith("selected at ")]
    if p.exists():
        old = [ln for ln in p.read_text().splitlines() if not ln.startswith("selected at ")]
        if old != body:
            print(f"levels_select: refusing -- the selection at {p} does not match the rule re-applied to the pilot "
                  f"(recorded "
                  f"{next((x for x in old if x.startswith('selected_duty=')), 'nothing')}, now "
                  f"{next((x for x in body if x.startswith('selected_duty=')), 'nothing')})", file=sys.stderr)
            return 3
        print(f"levels_select: the selection recorded at {p} matches the rule re-applied; not rewritten")
        return 0
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "-"
    p.write_text(f"selected at {sha}\n" + "\n".join(body) + "\n")
    print(f"levels_select: selection recorded at {p}")
    return 0


def coverage(d, labels):
    """return the covered and total run counts over these labels"""
    per = coverage_per_arm(d, labels)
    return sum(k for k, _ in per.values()), sum(n for _, n in per.values())


def coverage_per_arm(d, labels):
    """return the covered and total run counts per label"""
    per = {lab: [0, 0] for lab in labels}
    for r in d.get("rounds", []) if isinstance(d, dict) else []:
        for lab in labels:
            a = r.get(lab) if isinstance(r, dict) else None
            if isinstance(a, dict):
                per[lab][1] += 1
                per[lab][0] += a.get("range_covered") == 1
    return {lab: tuple(v) for lab, v in per.items()}


def verify_frozen(sel_path, frozen_path):
    """check revision 1's selection against its frozen record and the rule re-applied"""
    try:
        live = [x for x in Path(sel_path).read_text().splitlines() if not x.startswith("selected at ")]
        fz = json.loads(Path(frozen_path).read_text())
        frozen = [x for x in fz["selection_text"].splitlines() if not x.startswith("selected at ")]
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"levels_select: refusing -- revision 1's selection or its frozen record cannot be read ({e})",
              file=sys.stderr)
        return 3
    again = selection_text(fz, "", False, RANGE, "levels")[0].splitlines()
    if live != frozen:
        print(f"levels_select: refusing -- {sel_path} was edited; it differs from {frozen_path}",
              file=sys.stderr)
        return 3
    if again != frozen:
        print(f"levels_select: refusing -- the rule on {frozen_path} no longer gives its recorded selection",
              file=sys.stderr)
        return 3
    print(f"levels_select: revision 1's selection {sel_path} is the frozen record and the rule re-applied")
    return 0


def _pilot(per, steal=None, rounds=3, cover=None, prefix="", variant="levels"):
    """build a pilot batch from per-duty loss, steal and coverage"""
    rs = []
    for i in range(rounds):
        r = {pilot_control(variant): {"frames": 20000, "steal_ticks": 100, "range_frames": 7000, "range_lost": 0,
                             "range_covered": (cover or {}).get(("ctl", i), 1)}}
        for dd, vals in per.items():
            r[pilot_arm(dd, variant)] = {"frames": 20000, "steal_ticks": (steal or {}).get(dd, [3000] * rounds)[i],
                                "range_frames": 7000, "range_lost": round(vals[i] * 7),
                                "range_covered": (cover or {}).get((dd, i), 1)}
        rs.append({prefix + k: v for k, v in r.items()})
    return {"rounds": rs}


def selftest():
    """run the rule cases and return 1 on any failure"""
    import tempfile
    cases = []
    grid = lambda f: {dd: [f(dd)] * 3 for dd in GRID}   # noqa: E731
    base = grid(lambda dd: 50 if dd < 44.5 else 400 if dd == 44.5 else 800)
    sel = lambda *a, **k: select(*a, **k)[1]            # noqa: E731
    cases.append(("the lowest duty in the band is chosen, a fractional one", sel(_pilot(base)) == 44.5))
    cases.append(("a median exactly at 200 qualifies", sel(_pilot({**base, 43: [200, 250, 100]})) == 43))
    cases.append(("just below 200 does not", sel(_pilot({**base, 43: [199, 250, 100]})) == 44.5))
    cases.append(("exactly 600 qualifies", sel(_pilot({**base, 44.5: [600] * 3})) == 44.5))
    cases.append(("just above 600 does not: the next duty in the band", sel(_pilot({**base, 44.5: [601] * 3, 44.75: [500] * 3})) == 44.75))
    got = select(_pilot(grid(lambda dd: 50 if dd < 44.5 else 900)))
    cases.append(("a jump over the band: none, 'no duty' reason", got[1] is None and "no duty in the grid" in (got[2] or "")))
    got = select(_pilot(base, steal={dd: [100] * 3 for dd in GRID}))
    cases.append(("in the band, steal never above the control: none, arrival reason",
                  got[1] is None and "steal did not confirm arrival" in (got[2] or "")))
    cases.append(("a steal tie is not arrival: that duty is skipped",
                  sel(_pilot({**base, 44.75: [400] * 3}, steal={44.5: [3000, 100, 3000]})) == 44.75))
    cover = {(dd, i): 0 for dd in GRID for i in range(3)}
    got = select(_pilot(base, cover=cover))
    cases.append(("no window covered the range: none, coverage reason",
                  got[1] is None and "did not cover" in (got[2] or "")))
    cases.append(("a round whose control's window missed the range gives no result (2 of 3 still enough)",
                  sel(_pilot({**base, 44.5: [400, 10, 400]}, cover={("ctl", 1): 0})) == 44.5))
    cases.append(("results in only 1 of 3 covered rounds do not qualify",
                  sel(_pilot({**base, 44.75: [400] * 3}, cover={(44.5, 0): 0, (44.5, 1): 0})) == 44.75))
    cases.append(("the median decides: 0, 100, 900 (mean 333) does not qualify", sel(_pilot({**base, 36: [0, 100, 900]})) == 44.5))
    pf = _pilot(base, prefix="one-round-")
    cases.append(("a prefixed pilot is read through its prefix", sel(pf, "one-round-") == 44.5))
    t, ch = selection_text(_pilot(grid(lambda dd: 0), rounds=1, prefix="one-round-"), "one-round-", True)
    cases.append(("a one-round run's none forces the lowest duty, marked", ch == GRID[0] and "one_round_forced=1" in t and t.endswith("selected_duty=36\n")))
    t, ch = selection_text(_pilot(grid(lambda dd: 0)), "", True)
    cases.append(("a real none is never forced", ch is None and "one_round_forced" not in t and t.endswith("selected_duty=none\n")))
    cases.append(("labels spell a fraction with p", pilot_arm(44.25) == "pc-ldp-d44p25" and pilot_arm(45) == "pc-ldp-d45"))
    with tempfile.TemporaryDirectory() as dd:
        f = Path(dd) / "sel"
        t, _ = selection_text(_pilot(base))
        cases.append(("recorded once", record(f, t) == 0 and f.read_text().startswith("selected at ")))
        cases.append(("a matching re-application does not rewrite", record(f, t) == 0))
        t2, _ = selection_text(_pilot({**base, 44.5: [900] * 3, 44.75: [300] * 3}))
        before = f.read_text()
        cases.append(("a mismatch refuses and rewrites nothing", record(f, t2) == 3 and f.read_text() == before))
    k, n = coverage(_pilot(base, cover={(44.5, 0): 0}), [pilot_arm(44.5), PILOT_CONTROL])
    cases.append(("coverage counts covered runs", (k, n) == (5, 6)))
    per = coverage_per_arm(_pilot(base, cover={(44.5, i): 0 for i in range(3)}), [pilot_arm(44.5), PILOT_CONTROL])
    cases.append(("coverage per arm: one arm at 0 of 3 is seen, not pooled away",
                  per == {pilot_arm(44.5): (0, 3), PILOT_CONTROL: (3, 3)}))
    cases.append(("revision 2's grid is 42.0 to 43.0 by 0.1 with its own labels",
                  GRID2 == (42, 42.1, 42.2, 42.3, 42.4, 42.5, 42.6, 42.7, 42.8, 42.9, 43)
                  and pilot_arm(42.3, "levels2") == "pc-lbp-d42p3" and pilot_control("levels2") == "pc-lbp-control"))
    g2 = {dd: [100 if dd < 42.6 else 350 if dd == 42.6 else 700] * 3 for dd in GRID2}
    cases.append(("revision 2 selects the lowest of its grid in the band", sel(_pilot(g2, variant="levels2"), variant="levels2") == 42.6))
    cases.append(("revision 2's pilot read as revision 1 selects nothing", sel(_pilot(g2, variant="levels2")) is None))
    t, ch = selection_text(_pilot({dd: [0] * 1 for dd in GRID2}, rounds=1, prefix="one-round-", variant="levels2"), "one-round-", True, variant="levels2")
    cases.append(("revision 2's one-round run forces its own lowest duty", ch == 42 and t.endswith("selected_duty=42\n")))
    g3 = {dd: [100 if dd < 42.5 else 900] * 3 for dd in GRID2}
    got = select(_pilot(g3, cover={(42.5, i): 0 for i in range(3)}, variant="levels2"), variant="levels2")
    cases.append(("an uncovered duty above the highest covered below-band duty: the coverage reason",
                  got[1] is None and "did not cover" in (got[2] or "") and "42.5" in (got[2] or "")))
    got = select(_pilot(g3, cover={(42, i): 0 for i in range(3)}, variant="levels2"), variant="levels2")
    cases.append(("an uncovered duty below the highest covered below-band duty: still 'no duty'",
                  got[1] is None and "no duty in the grid" in (got[2] or "")))
    with tempfile.TemporaryDirectory() as dd:
        fz = _pilot(grid(lambda d: 50 if d < 44.5 else 400 if d == 44.5 else 800))
        t1, _ = selection_text(fz)
        fz["selection_text"] = "selected at abc1234\n" + t1
        fp = Path(dd) / "frozen.json"
        fp.write_text(json.dumps(fz))
        live = Path(dd) / "sel"
        live.write_text("selected at abc1234\n" + t1)
        cases.append(("revision 1's untouched selection passes", verify_frozen(live, fp) == 0))
        live.write_text("selected at abc1234\n" + t1.replace("selected_duty=44.5", "selected_duty=44"))
        cases.append(("an edited revision-1 selection refuses", verify_frozen(live, fp) == 3))
        live.unlink()
        cases.append(("a missing revision-1 selection refuses", verify_frozen(live, fp) == 3))
        fz["selection_text"] = "selected at abc1234\n" + t1.replace("selected_duty=44.5", "selected_duty=45")
        fp.write_text(json.dumps(fz))
        live.write_text(fz["selection_text"])
        cases.append(("a frozen record the rule no longer gives refuses", verify_frozen(live, fp) == 3))
    bad = [c for c, ok in cases if not ok]
    for c, ok in cases:
        print(("ok   " if ok else "FAIL ") + c)
    print(f"levels_select selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    """parse the arguments and run a mode"""
    if argv == ["--selftest"]:
        return selftest()
    if argv[:1] == ["--grid"] and argv[1:] in ([], ["--variant", "levels"], ["--variant", "levels2"]):
        v = argv[2] if len(argv) == 3 else "levels"
        for d in VARIANTS[v][0]:
            print(dtxt(d), pilot_arm(d, v))
        return 0
    if len(argv) == 3 and argv[0] == "--verify-frozen":
        return verify_frozen(argv[1], argv[2])
    if len(argv) >= 3 and argv[0] == "--coverage":
        try:
            d = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError):
            d = {}
        per = coverage_per_arm(d, argv[2:])
        k, n = sum(a for a, _ in per.values()), sum(b for _, b in per.values())
        print(f"window covered the range in {k}/{n} runs")
        for lab, (a, b) in per.items():
            print(f"  {lab} {a}/{b}")
        return 0
    if len(argv) >= 2 and argv[0] == "--select":
        path, rest = argv[1], argv[2:]
        opts = {"--record": None, "--prefix": "", "--range": None, "--variant": "levels"}
        fallback = False
        while rest:
            a = rest.pop(0)
            if a == "--one-round-fallback":
                fallback = True
            elif a in opts and rest:
                opts[a] = rest.pop(0)
            else:
                print(__doc__, file=sys.stderr)
                return 2
        rng = RANGE
        if opts["--range"]:
            m = re.fullmatch(r"(\d+)-(\d+)", opts["--range"])
            if not opts["--prefix"] or not m or int(m.group(1)) >= int(m.group(2)):
                print("levels_select: refusing -- --range is a one-round run's A-B, with --prefix, A < B", file=sys.stderr)
                return 2
            rng = (int(m.group(1)), int(m.group(2)))
        try:
            d = json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            print(f"levels_select: cannot read the pilot {path} ({e})", file=sys.stderr)
            return 1
        if opts["--variant"] not in VARIANTS:
            print(__doc__, file=sys.stderr)
            return 2
        text, _ = selection_text(d, opts["--prefix"], fallback, rng, opts["--variant"])
        print(text, end="")
        if opts["--record"]:
            return record(opts["--record"], text)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
