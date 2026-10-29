#!/usr/bin/env python3
"""Select R7's two nested duties from the frozen R5 pilot and state each batch's verdict

Usage: benchmark/score/single_level/r7.py --duty a|b
       benchmark/score/single_level/r7.py --split BATCH a|b adaptive-rs|adaptive OUT [--prefix P]
       benchmark/score/single_level/r7.py --ratio a|b adaptive-rs|adaptive ARRIVED
       benchmark/score/single_level/r7.py --vs-fixed-hi a|b adaptive-rs|adaptive ARRIVED
       benchmark/score/single_level/r7.py --verdict a|b SCORE
       benchmark/score/single_level/r7.py --selftest
  --duty          the batch's duty
  --split         r6.py --split under R7's labels
  --ratio         r6.py --ratio under R7's labels
  --vs-fixed-hi   r6.py --vs-fixed-hi under R7's labels
  --verdict       the batch's verdict"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.score.single_level import r4 as r4_select     # noqa: E402
from benchmark.score.single_level import r5 as r5_select     # noqa: E402
from benchmark.score.single_level import r6 as r6_select     # noqa: E402
from benchmark import lib  # noqa: E402

PILOT = "pcn-r5-pilot.json"
LEVELS = {
    "a": {"prefix": "pcn-r7a-", "control": "pcn-r7a-control", "name": "R7 a"},
    "b": {"prefix": "pcn-r7b-", "control": "pcn-r7b-control", "name": "R7 b"},
}
r6_select.LEVELS.update(LEVELS)
BAND_B = (0.40, 0.80)
DROP_PRIMARY = 50
DROP_FALLBACK = 100
P7_ERR_FALSIFY = 0.039


def _pilot(frozen=None):
    """read the frozen R5 pilot, or None"""
    try:
        return json.loads((Path(frozen) / PILOT if frozen else lib.single_level_file(PILOT)).read_text())
    except (OSError, ValueError):
        return None


def duty_a(frozen=None):
    """return R5's recorded duty, or None when forced or missing"""
    d = _pilot(frozen)
    t = (d or {}).get("selection_text", "")
    m = re.findall(r"^selected_duty=([0-9.]+)$", t, re.M)
    if len(m) != 1 or r6_select.FORCED_RE.search(t):
        return None
    return m[0]


def duty_b(frozen=None):
    """return batch b's duty or None, the rule that chose it, and the report lines"""
    d = _pilot(frozen)
    a = duty_a(frozen)
    if d is None or a is None:
        return None, None, ["no frozen R5 pilot or selection"]
    grid = r5_select.GRID
    n = len(d.get("rounds", []))
    k = r4_select.need(n, r5_select.KEEP)
    stats = {g: r5_select.duty_stats(d, g) for g in grid}
    lines = []

    def pick(cap):
        """return the lowest load-B duty above a with drops at most cap"""
        for i, g in enumerate(grid):
            if g <= float(a):
                continue
            s = stats[g]
            ok = (len(s["vals"]) >= k and s["pairs"] >= k and s["conf"] == s["pairs"]
                  and BAND_B[0] <= s["med"] <= BAND_B[1] and s["drops"] is not None and s["drops"] <= cap)
            nb = stats[grid[i + 1]] if i + 1 < len(grid) else None
            ok = ok and nb is not None and len(nb["vals"]) >= k and nb["med"] >= BAND_B[0]
            if ok:
                return g
        return None

    b = pick(DROP_PRIMARY)
    if b is not None:
        return r5_select.dtxt(b), "primary", lines
    lines.append(f"primary (R4b's load-B rule, drops <= {DROP_PRIMARY}): none")
    b = pick(DROP_FALLBACK)
    if b is not None:
        return r5_select.dtxt(b), "fallback", lines + [f"fallback (drops <= {DROP_FALLBACK}): {r5_select.dtxt(b)}"]
    return None, None, lines + [f"fallback (drops <= {DROP_FALLBACK}): none"]


def verdict(level, text):
    """return a batch's verdict and interpretation lines from its score text"""
    tag = LEVELS[level]["name"]
    p6 = re.search(r"^verdicts .*P6=(\w+)", text or "", re.M)
    p8 = re.search(r"^verdicts .*P8=(\w+)", text or "", re.M)
    err = re.search(r"mean relative error under the bound\n\s+([0-9.eE+-]+)", text or "")
    rt = re.search(rf"^{tag} adaptive-rs ratio .*= ([0-9.eE+-]+) over", text or "", re.M)
    want = f"adaptive-rs's P6 HIT and its ratio at most {r4_select.RATIO_MAX}"
    if not p6 or not rt:
        return [f"{tag}: {want}? not applicable (VOID, not run or not scored)", f"{tag} verdict: NOT APPLICABLE",
                f"{tag} interpretation: not applicable"]
    r = float(rt.group(1))
    hit = p6.group(1) == "HIT" and r <= r4_select.RATIO_MAX
    out = [f"{tag}: {want}? P6={p6.group(1)}, ratio {r:.6g}", f"{tag} verdict: {'HIT' if hit else 'MISS'}"]
    if not hit:
        out.append(f"{tag} interpretation: no HIT to interpret")
    else:
        e = float(err.group(1)) if err else None
        shed = (p8 and p8.group(1) == "MISS") or (e is not None and e > P7_ERR_FALSIFY)
        out.append(f"{tag} interpretation: " + (
            f"P6 met by shedding, as fixed-lo does (P8={p8.group(1) if p8 else '-'}, mean relative error {e}), not restored"
            if shed else f"restored: P8={p8.group(1) if p8 else '-'}, mean relative error {e} (not above {P7_ERR_FALSIFY})"))
    return out


def _batch(level, **kw):
    """build R6's test batch with nested topology"""
    d = r6_select._batch(level, **kw)
    d["topology"] = "nested"
    return d


def selftest():
    """run the rule cases and return 1 on any failure"""
    import tempfile
    cases = []
    cases.append(("batch a is R5's recorded duty, 10", duty_a() == "10"))
    b, how, lines = duty_b()
    cases.append(("on the frozen R5 pilot the primary rule selects none and the fallback 20",
                  b == "20" and how == "fallback" and any("primary" in x and "none" in x for x in lines)))
    with tempfile.TemporaryDirectory() as td:
        d = _pilot()
        for r in d["rounds"]:
            r["pcn-r5p-d20"]["dropped"] = 40
        Path(td, PILOT).write_text(json.dumps(d))
        cases.append(("a 20 dropping at most 50 is the primary rule's pick", duty_b(td)[:2] == ("20", "primary")))
        for r in d["rounds"]:
            for g in r5_select.GRID:
                if g > 10:
                    r[r5_select.pilot_arm(g)]["dropped"] = 500
        Path(td, PILOT).write_text(json.dumps(d))
        cases.append(("heavy drops everywhere above a: no batch b", duty_b(td)[0] is None))
        Path(td, PILOT).write_text(json.dumps({"selection_text": "one_round_forced=1\nselected_duty=5\n", "rounds": []}))
        cases.append(("a forced or missing record gives neither duty", duty_a(td) is None and duty_b(td)[0] is None))
        for m in r6_select.lib.old_names("one_round_forced"):
            Path(td, PILOT).write_text(json.dumps({"selection_text": f"{m}=1\nselected_duty=5\n", "rounds": []}))
            cases.append((f"a record forced under the old marker {m} gives no duty", duty_a(td) is None))
    for lv in LEVELS:
        lines, kept, void = r6_select.split(_batch(lv), lv, "adaptive-rs")
        cases.append((f"{lv}: R6's split under R7's labels keeps 16 rounds", len(kept) == 16 and not void
                      and any(x.startswith(f"R7 {lv} adaptive-rs: arrival") for x in lines)))
        tag = LEVELS[lv]["name"]
        good = (f"  HIT   mean relative error under the bound\n        0.006 (bound 0.02, falsified above 0.039)\n"
                f"verdicts  P5=MISS  P6=HIT  P7=HIT  P8=HIT\n{tag} adaptive-rs ratio / fixed-hi-probe over_deadline_frac: 0.01 / 0.3 = 0.0333333 over 16 kept round(s)")
        v = verdict(lv, good)
        cases.append((f"{lv}: HIT with P7 and P8 is restored", v[1].endswith("HIT") and "restored:" in v[2]))
        v = verdict(lv, good.replace("P8=HIT", "P8=MISS"))
        cases.append((f"{lv}: HIT with P8 MISS is shedding", v[1].endswith("HIT") and "by shedding" in v[2]))
        v = verdict(lv, good.replace("0.006 (bound", "0.05 (bound"))
        cases.append((f"{lv}: HIT with error above 0.039 is shedding", "by shedding" in v[2]))
        cases.append((f"{lv}: ratio above 0.5 is a MISS", verdict(lv, good.replace("= 0.0333333", "= 0.6"))[1].endswith("MISS")))
        cases.append((f"{lv}: P6 MISS is a MISS", verdict(lv, good.replace("P6=HIT", "P6=MISS"))[1].endswith("MISS")))
        cases.append((f"{lv}: no score is not applicable", verdict(lv, "")[1].endswith("NOT APPLICABLE")))
    bad = [c for c, ok in cases if not ok]
    for c, ok in cases:
        print(("ok   " if ok else "FAIL ") + c)
    print(f"r7_select selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    """parse the arguments and run a mode"""
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) == 2 and argv[0] == "--duty" and argv[1] in LEVELS:
        d = duty_a() if argv[1] == "a" else duty_b()[0]
        if d is None:
            print(f"r7_select: no duty for batch {argv[1]} from the frozen R5 pilot", file=sys.stderr)
            return 1
        print(d)
        return 0
    if len(argv) == 3 and argv[0] == "--verdict" and argv[1] in LEVELS:
        try:
            t = Path(argv[2]).read_text()
        except OSError:
            t = ""
        print("\n".join(verdict(argv[1], t)))
        return 0
    if argv and argv[0] in ("--split", "--ratio", "--vs-fixed-hi") and len(argv) > 2 \
            and (argv[2] if argv[0] == "--split" else argv[1]) in LEVELS:
        return r6_select.main(argv)
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
