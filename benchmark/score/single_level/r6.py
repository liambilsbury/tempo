#!/usr/bin/env python3
"""Split and score R6's adaptive-rs batches at the frozen R5 and R4b duties

Usage: benchmark/score/single_level/r6.py --duty nested|single
       benchmark/score/single_level/r6.py --split BATCH nested|single adaptive-rs|adaptive OUT [--prefix P]
       benchmark/score/single_level/r6.py --ratio nested|single adaptive-rs|adaptive ARRIVED
       benchmark/score/single_level/r6.py --vs-fixed-hi nested|single adaptive-rs|adaptive ARRIVED
       benchmark/score/single_level/r6.py --verdict nested|single SCORE
       benchmark/score/single_level/r6.py --selftest
  --duty          a level's frozen duty
  --split         the rounds that show arrival
  --ratio         the arm's ratio to fixed-hi-probe
  --vs-fixed-hi   the arm against fixed-hi
  --verdict       the level's verdict"""
import json
import re
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.score.single_level import r4 as r4_select   # noqa: E402
from benchmark import lib         # noqa: E402
FORCED_RE = re.compile(r"^(" + "|".join(re.escape(m) for m in ["one_round_forced"] + lib.old_names("one_round_forced"))
                       + r")=1$", re.M)

LEVELS = {
    "nested": {"prefix": "pcn-r6-", "control": "pcn-r6-control", "source": "pcn-r5-pilot.json",
               "key": "selected_duty", "name": "R6 nested"},
    "single": {"prefix": "pc-r6-", "control": "pc-r6-control", "source": "pc-r4b-pilot.json",
               "key": "selected_duty_a", "name": "R6 single-level"},
}
ARMS = ("adaptive-rs", "adaptive")


def duty(level, frozen=None):
    """return a level's duty from its frozen selection, or None"""
    L = LEVELS[level]
    try:
        t = json.loads((Path(frozen) / L["source"] if frozen else lib.single_level_file(L["source"])).read_text())["selection_text"]
    except (OSError, ValueError, KeyError):
        return None
    m = re.findall(rf"^{L['key']}=([0-9.]+)$", t, re.M)
    if len(m) != 1 or FORCED_RE.search(t):
        return None
    return m[0]


def split(d, level, arm, prefix=""):
    """apply R4's arrival rule with the arm in the adaptive slot"""
    L = LEVELS[level]
    rounds = []
    for r in d.get("rounds", []):
        r = r if isinstance(r, dict) else {}
        x = {}
        for a in r4_select.ARMS:
            src = arm if a == "adaptive" else a
            v = r.get(f"{prefix}{L['prefix']}{src}")
            if isinstance(v, dict):
                x[a] = v
        if isinstance(r.get(prefix + L["control"]), dict):
            x[r4_select.R4_CONTROL] = r[prefix + L["control"]]
        rounds.append(x)
    lines, kept, void = r4_select.arrival({"rounds": rounds})
    tag = f"{L['name']} {arm}"
    lines = [f"{tag}: " + ln.replace("R4: VOID", f"{tag}: VOID") for ln in lines]
    return lines, kept, void


def ratio_lines(level, arm, rounds):
    """return the guard lines and the arm's ratio line to fixed-hi-probe"""
    from benchmark.score.nested.quality_shedding import arm_median
    tag = f"{LEVELS[level]['name']} {arm}"
    kept = [r for r in rounds if isinstance(r, dict) and r4_select.hit_ok(r)]
    out = [g.replace("guard ", f"{tag} guard ", 1) for g in r4_select.guards(kept)]
    ad = arm_median(kept, "adaptive", "over_deadline_frac") if kept else None
    pr = arm_median(kept, "fixed-hi-probe", "over_deadline_frac") if kept else None
    what = "pre-registered: at most %s; scored by hand" % r4_select.RATIO_MAX if arm == "adaptive-rs" \
        else "re-measured, reported, not a prediction"
    if ad is None or pr is None or not pr:
        out.append(f"{tag} ratio / fixed-hi-probe over_deadline_frac: - ({len(kept)} kept round(s); "
                   f"medians {ad} / {pr})")
    else:
        out.append(f"{tag} ratio / fixed-hi-probe over_deadline_frac: {ad} / {pr} = {ad / pr:.6g} over "
                   f"{len(kept)} kept round(s) ({what})")
    return out


def vs_fixed_hi(level, arm, rounds):
    """return the arm's median ratio and sign test line against fixed-hi"""
    from benchmark.run.paired import sign_test
    tag = f"{LEVELS[level]['name']} {arm}"
    pairs = [(r["adaptive"]["over_deadline_frac"], r["fixed-hi"]["over_deadline_frac"]) for r in rounds
             if isinstance(r, dict) and r4_select.num((r.get("adaptive") or {}).get("over_deadline_frac"))
             and r4_select.num((r.get("fixed-hi") or {}).get("over_deadline_frac"))]
    if not pairs:
        return [f"reported, not scored: {tag} against fixed-hi: no paired rounds"]
    diffs = [a - b for a, b in pairs]
    p, _, _ = sign_test(diffs)
    rat = [a / b for a, b in pairs if b]
    return [f"reported, not scored: {tag} against fixed-hi (no instrument): median per-round ratio "
            f"{median(rat):.4g} over {len(rat)} round(s), {sum(x > 0 for x in diffs)} up / "
            f"{sum(x < 0 for x in diffs)} down, sign test p={p:.4g}" if rat else
            f"reported, not scored: {tag} against fixed-hi: fixed-hi missed nothing"]


def verdict(level, text):
    """return a level's verdict lines from its adaptive-rs score text"""
    tag = LEVELS[level]["name"]
    p6 = re.search(r"^verdicts .*P6=(\w+)", text or "", re.M)
    rt = re.search(rf"^{tag} adaptive-rs ratio .*= ([0-9.eE+-]+) over", text or "", re.M)
    want = ("adaptive-rs's P6 HIT and its ratio at most %s (R6's prediction)" if level == "nested"
            else "adaptive-rs keeps R4b's result: P6 HIT and its ratio at most %s") % r4_select.RATIO_MAX
    if not p6 or not rt:
        return [f"{tag}: {want}? not applicable (VOID, not run or not scored)", f"{tag} verdict: NOT APPLICABLE"]
    r = float(rt.group(1))
    hit = p6.group(1) == "HIT" and r <= r4_select.RATIO_MAX
    return [f"{tag}: {want}? P6={p6.group(1)}, ratio {r:.6g}", f"{tag} verdict: {'HIT' if hit else 'MISS'}"]


def _batch(level, n=16, low=(), od=0.3, rs_od=0.01, prefix=""):
    """build a level's batch with chosen low-steal rounds"""
    L = LEVELS[level]
    rs = []
    for i in range(n):
        arm = lambda o, s, p999=1000, err=0.01, full=0.6, hit=5: {  # noqa: E731
            "frames": 20000, "steal_ticks": s, "over_deadline_frac": o, "hit": hit, "p999_ns": p999,
            "compute_p50_ns": 700, "dropped": 0, "mean_relerr": err, "full_quality_frac": full}
        r = {L["control"]: arm(0.002, 10), L["prefix"] + "fixed-hi": arm(od - 0.1, 5 if i in low else 600),
             L["prefix"] + "fixed-hi-probe": arm(od, 600), L["prefix"] + "adaptive": arm(od * 0.9, 500, 950),
             L["prefix"] + "adaptive-rs": arm(rs_od, 500, 900 if rs_od < od else 1000),
             L["prefix"] + "fixed-lo": arm(0.0, 300, 500, 0.0786, 0.0)}
        rs.append({prefix + k: v for k, v in r.items()})
    return {"topology": "nested" if level == "nested" else "single-level", "metric": "over_deadline_frac", "rounds": rs}


def selftest():
    """run the rule cases and return 1 on any failure"""
    import tempfile
    cases = []
    cases.append(("the nested duty is R5's recorded selection (10)", duty("nested") == "10"))
    cases.append(("the single-level duty is R4b's recorded load A (43)", duty("single") == "43"))
    with tempfile.TemporaryDirectory() as td:
        Path(td, "pcn-r5-pilot.json").write_text(json.dumps({"selection_text": "one_round_forced=1\nselected_duty=5\n"}))
        cases.append(("a forced or missing record gives no duty", duty("nested", td) is None and duty("single", td) is None))
        for m in lib.old_names("one_round_forced"):
            Path(td, "pcn-r5-pilot.json").write_text(json.dumps({"selection_text": f"{m}=1\nselected_duty=5\n"}))
            cases.append((f"a record forced under the old marker {m} gives no duty", duty("nested", td) is None))
    for level in LEVELS:
        lines, kept, void = split(_batch(level), level, "adaptive-rs")
        cases.append((f"{level}: adaptive-rs fills the adaptive slot, 16 kept",
                      len(kept) == 16 and not void and kept[0]["adaptive"]["over_deadline_frac"] == 0.01
                      and set(kept[0]) == {"fixed-hi", "fixed-hi-probe", "adaptive", "fixed-lo", "pc-r4-control"}))
        _, kept_old, _ = split(_batch(level), level, "adaptive")
        cases.append((f"{level}: the old adaptive fills it in the reported split, same rounds",
                      len(kept_old) == 16 and abs(kept_old[0]["adaptive"]["over_deadline_frac"] - 0.27) < 1e-9))
        lines, kept, void = split(_batch(level, low=range(5)), level, "adaptive-rs")
        cases.append((f"{level}: 11 of 16 arriving is VOID, named", void and any(f"{LEVELS[level]['name']} adaptive-rs: VOID" in x for x in lines)))
        rl = ratio_lines(level, "adaptive-rs", split(_batch(level), level, "adaptive-rs")[1])
        cases.append((f"{level}: adaptive-rs's ratio is the pre-registered one, with R4's guards",
                      any("adaptive-rs ratio" in x and "= 0.0333333 over 16" in x and "pre-registered" in x for x in rl)
                      and any("adaptive-rs guard P6" in x for x in rl)))
        ro = ratio_lines(level, "adaptive", split(_batch(level), level, "adaptive")[1])
        cases.append((f"{level}: the old arm's ratio is reported, not a prediction", any("not a prediction" in x for x in ro)))
        vf = vs_fixed_hi(level, "adaptive-rs", split(_batch(level), level, "adaptive-rs")[1])
        cases.append((f"{level}: against fixed-hi is reported, not scored", vf[0].startswith("reported, not scored") and "0 up / 16 down" in vf[0]))
        name = LEVELS[level]["name"]
        good = f"verdicts  P5=MISS  P6=HIT  P7=HIT  P8=HIT\n{name} adaptive-rs ratio / fixed-hi-probe over_deadline_frac: 0.01 / 0.3 = 0.0333333 over 16 kept round(s)"
        cases.append((f"{level}: verdict HIT when P6 HIT and ratio at most 0.5", verdict(level, good)[-1].endswith("HIT")))
        cases.append((f"{level}: verdict MISS when P6 misses", verdict(level, good.replace("P6=HIT", "P6=MISS"))[-1].endswith("MISS")))
        cases.append((f"{level}: verdict MISS when the ratio is above 0.5", verdict(level, good.replace("= 0.0333333", "= 0.6"))[-1].endswith("MISS")))
        cases.append((f"{level}: not applicable without a score", verdict(level, "")[-1].endswith("NOT APPLICABLE")))
        cases.append((f"{level}: the old arm's ratio line is not read as adaptive-rs's",
                      verdict(level, good.replace("adaptive-rs ratio", "adaptive ratio"))[-1].endswith("NOT APPLICABLE")))
    lines, kept, void = split(_batch("nested", n=1, prefix="one-round-"), "nested", "adaptive-rs", "one-round-")
    cases.append(("a one-round run's batch splits through its prefix", len(kept) == 1))
    bad = [c for c, ok in cases if not ok]
    for c, ok in cases:
        print(("ok   " if ok else "FAIL ") + c)
    print(f"r6_select selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    """parse the arguments and run a mode"""
    if argv == ["--selftest"]:
        return selftest()
    prefix = ""
    if "--prefix" in argv:
        i = argv.index("--prefix")
        prefix = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) == 2 and argv[0] == "--duty" and argv[1] in LEVELS:
        d = duty(argv[1])
        if d is None:
            print(f"r6_select: no {argv[1]} duty in the frozen record", file=sys.stderr)
            return 1
        print(d)
        return 0
    if len(argv) == 5 and argv[0] == "--split" and argv[2] in LEVELS and argv[3] in ARMS:
        try:
            d = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError) as e:
            print(f"r6_select: cannot read {argv[1]} ({e})", file=sys.stderr)
            return 1
        lines, kept, void = split(d, argv[2], argv[3], prefix)
        print("\n".join(lines))
        Path(argv[4]).write_text(json.dumps({"metric": d.get("metric", "over_deadline_frac"),
                                             "topology": d.get("topology"), "rounds": kept}, indent=1))
        return 4 if void else 0
    if len(argv) == 4 and argv[0] in ("--ratio", "--vs-fixed-hi") and argv[1] in LEVELS and argv[2] in ARMS:
        try:
            d = json.loads(Path(argv[3]).read_text())
        except (OSError, ValueError):
            d = {"rounds": []}
        f = ratio_lines if argv[0] == "--ratio" else vs_fixed_hi
        print("\n".join(f(argv[1], argv[2], d.get("rounds", []))))
        return 0
    if len(argv) == 3 and argv[0] == "--verdict" and argv[1] in LEVELS:
        try:
            t = Path(argv[2]).read_text()
        except OSError:
            t = ""
        print("\n".join(verdict(argv[1], t)))
        return 0
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
