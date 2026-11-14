#!/usr/bin/env python3

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median   # noqa: E402

GRID = (30, 35, 40, 42, 44, 45, 46, 47, 48, 49, 50, 52, 55, 60)
MISS_MIN = 0.20
MISS_MAX = 0.80
PILOT_CONTROL = "pc-r4p-control"
PILOT_KEEP_FRACTION = 2 / 3


def pilot_arm(d):
    return f"pc-r4p-probe-d{d}"


ARMS = ("fixed-hi", "fixed-hi-probe", "adaptive", "fixed-lo")
R4_CONTROL = "pc-r4-control"
ARRIVAL_ARMS = ("fixed-hi", "fixed-hi-probe")
REPORTED_ARMS = ("adaptive", "fixed-lo")
VOID_FRACTION = 3 / 4
RATIO_MAX = 0.5
GUARD_FULL = 0.9
GUARD_LO = 0.20
R2_DUTY = "20"
MEASUREMENT_CODE_FILES = ("benchmark/", "src/", "tools/build/", "Makefile")
MEASUREMENT_CODE_MARK = "r4-harness:"


def need(n, fraction):
    return max(1, math.ceil(n * fraction - 1e-9))


def num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def steal_rate(fact):
    if not isinstance(fact, dict):
        return None
    s, f = fact.get("steal_ticks"), fact.get("frames")
    if not num(s) or not num(f) or f <= 0:
        return None
    return 1000.0 * s / f


def above(fact, ctl):
    a, c = steal_rate(fact), steal_rate(ctl)
    return a is not None and c is not None and a > c


def load(path):
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("rounds"), list) else None


def fmt(x):
    return "-" if x is None else f"{x:.4g}"


def select(d, prefix="", runs=None):
    rounds = [r if isinstance(r, dict) else {} for r in d["rounds"]]
    n = len(rounds)
    k = need(n, PILOT_KEEP_FRACTION)
    ctl = prefix + PILOT_CONTROL
    lines = [f"pilot: {n} round(s) attempted; a duty needs results in at least {k}",
             f"rule: the lowest duty in {','.join(map(str, GRID))} whose fixed-hi-probe median "
             f"over_deadline_frac is in [{MISS_MIN:.2f}, {MISS_MAX:.2f}], with steal_ticks per 1000 "
             f"frames above the same round's {PILOT_CONTROL} in every round holding both"]
    chosen, in_band = None, []
    for dd in GRID:
        arm = prefix + pilot_arm(dd)
        vals = [r[arm]["over_deadline_frac"] for r in rounds
                if isinstance(r.get(arm), dict) and num(r[arm].get("over_deadline_frac"))]
        med = median(vals) if vals else None
        pairs = [(r[arm], r[ctl]) for r in rounds
                 if isinstance(r.get(arm), dict) and isinstance(r.get(ctl), dict)]
        conf = sum(1 for a, c in pairs if above(a, c))
        rates = [steal_rate(a) for a, _ in pairs]
        crates = [steal_rate(c) for _, c in pairs]
        rd, crd = [], []
        if runs is not None:
            rd = [x for x in (inject_rundelay(runs, arm, i) for i in range(n)) if x is not None]
            crd = [x for x in (inject_rundelay(runs, ctl, i) for i in range(n)) if x is not None]
        why = []
        band = len(vals) >= k and MISS_MIN <= med <= MISS_MAX
        if len(vals) < k:
            why.append(f"results in {len(vals)} round(s), fewer than {k}")
        elif med < MISS_MIN:
            why.append(f"median below {MISS_MIN:.2f}")
        elif med > MISS_MAX:
            why.append(f"median above {MISS_MAX:.2f}")
        if band:
            in_band.append(dd)
        if len(pairs) < k:
            why.append(f"{len(pairs)} round(s) with its control, fewer than {k}")
        elif conf < len(pairs):
            why.append(f"arrival not shown in {len(pairs) - conf} round(s)")
        ok = not why
        if ok and chosen is None:
            chosen = dd
        lines.append(
            f"duty {dd}: over_deadline_frac median {fmt(med)} over {len(vals)} round(s) "
            f"[{', '.join(fmt(v) for v in vals)}]; arrival {conf}/{len(pairs)} "
            f"(steal/1000 frames {', '.join(fmt(x) for x in rates)} against control "
            f"{', '.join(fmt(x) for x in crates)}); injector run-delay fraction "
            f"[{', '.join(fmt(x) for x in rd)}] against control [{', '.join(fmt(x) for x in crd)}] "
            f"(reported, not a rule) -> "
            f"{'qualifies' if ok else 'no: ' + '; '.join(why)}")
    reason = None
    if chosen is None and not in_band:
        reason = (f"not run: no duty in the grid loads the loop into [{MISS_MIN:.2f}, "
                  f"{MISS_MAX:.2f}] (fixed-hi-probe's median over_deadline_frac)")
    elif chosen is None:
        reason = (f"not run: duties {','.join(map(str, in_band))} loaded the loop into "
                  f"[{MISS_MIN:.2f}, {MISS_MAX:.2f}], but steal did not confirm arrival at any "
                  f"of them")
    return lines, chosen, reason


def strip(r, prefix):
    if not prefix:
        return dict(r)
    return {(k[len(prefix):] if k.startswith(prefix) else k): v for k, v in r.items()}


def hit_ok(r):
    return all(isinstance(r.get(a), dict) and r[a].get("hit") for a in ARMS)


def inject_rundelay(runs, label, i):
    p = runs / f"{label}-r{i}" / "inject.meta"
    try:
        kv = dict(t.split("=", 1) for t in p.read_text().split() if "=" in t)
        rd, wall, v = (float(kv["watched_vcpu_rundelay_window_ns"]), float(kv["watched_wall_ns"]),
                       float(kv["watched_vcpus"]))
    except (OSError, KeyError, ValueError):
        return None
    return rd / (wall * v) if wall > 0 and v > 0 else None


def arrival(d, prefix="", runs=None):
    rounds = [strip(r, prefix) if isinstance(r, dict) else {} for r in d["rounds"]]
    n = len(rounds)
    k = need(n, VOID_FRACTION)
    lines, kept = [], []
    both = 0
    rep = {a: 0 for a in REPORTED_ARMS}
    rep_n = {a: 0 for a in REPORTED_ARMS}
    rd = {a: [] for a in ARMS + (R4_CONTROL,)}
    for i, r in enumerate(rounds):
        ctl = r.get(R4_CONTROL)
        ok = isinstance(ctl, dict) and all(above(r.get(a), ctl) for a in ARRIVAL_ARMS)
        for a in REPORTED_ARMS:
            if isinstance(r.get(a), dict) and isinstance(ctl, dict):
                rep_n[a] += 1
                rep[a] += above(r[a], ctl)
        if runs is not None:
            for a in ARMS + (R4_CONTROL,):
                x = inject_rundelay(runs, prefix + a, i)
                if x is not None:
                    rd[a].append(x)
        h = hit_ok(r)
        if ok:
            kept.append(r)
            both += h
        rates = ", ".join(f"{a} {fmt(steal_rate(r.get(a)))}" for a in ARMS)
        lines.append(f"round {i}: steal/1000 control {fmt(steal_rate(ctl))}; {rates} -> "
                     f"arrival {'shown' if ok else 'NOT shown'}; hit {'ok' if h else 'fails'}")
    lines.append(f"arrival: {len(kept)}/{n} rounds")
    lines.append("reported, not a rule: " + ", ".join(
        f"{a} above control {rep[a]}/{rep_n[a]}" for a in REPORTED_ARMS))
    if runs is not None:
        lines.append("reported, not a rule: run-delay median " + ", ".join(
            f"{a} {fmt(median(rd[a]) if rd[a] else None)} ({len(rd[a])})" for a in ARMS + (R4_CONTROL,)))
    lines.append(f"kept by both rules: {both}/{n}, need {k}")
    void = both < k
    if void:
        lines.append(f"R4: VOID -- {both}/{n} < {k}")
    return lines, kept, void


def ratio(path):
    from benchmark.score.nested.quality_shedding import arm_median
    d = load(path)
    if d is None:
        print(f"R4 ratio adaptive / fixed-hi-probe over_deadline_frac: - (no {path})")
        return 0
    kept = [r for r in d["rounds"] if isinstance(r, dict) and hit_ok(r)]
    for g in guards(kept):
        print(g)
    ad = arm_median(kept, "adaptive", "over_deadline_frac") if kept else None
    pr = arm_median(kept, "fixed-hi-probe", "over_deadline_frac") if kept else None
    if ad is None or pr is None or not pr:
        print(f"R4 ratio adaptive / fixed-hi-probe over_deadline_frac: - "
              f"(kept {len(kept)}; medians {ad} / {pr})")
        return 0
    print(f"R4 ratio adaptive / fixed-hi-probe over_deadline_frac: {ad} / {pr} = {ad / pr:.3f} "
          f"kept {len(kept)}")
    return 0


def guards(rounds):
    from benchmark.score.nested.quality_shedding import arm_median
    pr = arm_median(rounds, "fixed-hi-probe", "over_deadline_frac") if rounds else None
    lo = arm_median(rounds, "fixed-lo", "over_deadline_frac") if rounds else None
    out = []
    if pr is not None and pr >= GUARD_FULL:
        out.append(f"guard P7/P8: NOT INFORMATIVE -- probe od {pr} >= {GUARD_FULL}")
    else:
        out.append(f"guard P7/P8: not triggered (probe od {fmt(pr)} < {GUARD_FULL})")
    if lo is not None and lo >= GUARD_LO:
        out.append(f"guard P6: NOT INFORMATIVE -- fixed-lo od {lo} >= {GUARD_LO}")
    else:
        out.append(f"guard P6: not triggered (fixed-lo od {fmt(lo)} < {GUARD_LO})")
    return out


def record(path, text):
    p = Path(path)
    body = [ln for ln in text.splitlines() if not ln.startswith("selected at ")]
    if p.exists():
        old = [ln for ln in p.read_text().splitlines() if not ln.startswith("selected at ")]
        if old != body:
            print(f"r4_select: refusing -- {p} differs (recorded "
                  f"{next((x for x in old if x.startswith('selected_duty=')), 'nothing')}, now "
                  f"{next((x for x in body if x.startswith('selected_duty=')), 'nothing')})", file=sys.stderr)
            return 3
        print(f"r4_select: {p} matches")
        return 0
    import subprocess
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "-"
    p.write_text(f"selected at {sha}\n" + "\n".join(body) + "\n")
    print(f"r4_select: recorded {p}")
    return 0


def measurement_code(doc):
    import re
    import subprocess
    try:
        m = re.search(r"<!--\s*" + MEASUREMENT_CODE_MARK + r"\s*([0-9a-f]{7,40})\s*-->", Path(doc).read_text())
    except OSError:
        m = None
    if not m:
        print(f"r4_select: refusing -- {doc}: no {MEASUREMENT_CODE_MARK} sha", file=sys.stderr)
        return 3
    sha = m.group(1)
    r = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", sha, "HEAD", "--",
                        *MEASUREMENT_CODE_FILES], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"r4_select: refusing -- differs from {sha} "
              f"({r.stderr.strip() or 'exit ' + str(r.returncode)})", file=sys.stderr)
        return 3
    print(f"r4_select: {sha} matches HEAD")
    return 0


def keep_r2_runs(res):
    res = Path(res)
    dest = res / "pc-adapt-runs"
    moved = 0
    for a in ARMS:
        for run in sorted(res.glob(f"{a}-r*")):
            if not run.is_dir() or not run.name[len(a) + 2:].isdigit():
                continue
            try:
                head = (run / "inject.meta").read_text().split("\n", 1)[0].split()
            except OSError:
                continue
            kv = dict(t.split("=", 1) for t in head if "=" in t)
            if kv.get("mode") not in ("cpu-load", "hog") or kv.get("duty") != R2_DUTY:
                continue
            if (dest / run.name).exists():
                print(f"r4_select: refusing -- {dest / run.name} exists", file=sys.stderr)
                return 1
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(run), str(dest / run.name))
            moved += 1
    print(f"r4_select: moved {moved} -> {dest}")
    return 0


def _pilot(od, steal=None, ctl_steal=100, rounds=3, missing=()):
    rs = []
    for i in range(rounds):
        r = {PILOT_CONTROL: {"frames": 20000, "steal_ticks": ctl_steal, "over_deadline_frac": 0.0}}
        for dd, vals in od.items():
            if (dd, i) in missing:
                continue
            s = (steal or {}).get(dd, [3000] * rounds)[i]
            r[pilot_arm(dd)] = {"frames": 20000, "steal_ticks": s, "over_deadline_frac": vals[i]}
        rs.append(r)
    return {"rounds": rs}


def _confirm(n=16, low=(), hit0=(), lo_below=True, prefix=""):
    rs = []
    for i in range(n):
        def arm(od, s, hit=5):
            return {"frames": 20000, "steal_ticks": s, "over_deadline_frac": od, "hit": hit}
        r = {"fixed-hi": arm(0.4, 50 if i in low else 3000),
             "fixed-hi-probe": arm(0.4, 3000, 0 if i in hit0 else 5),
             "adaptive": arm(0.1, 80),
             "fixed-lo": arm(0.01, 60 if lo_below else 900),
             R4_CONTROL: arm(0.0, 100)}
        rs.append({prefix + k: v for k, v in r.items()})
    return {"rounds": rs}


def selftest():
    import tempfile
    fails = []

    def expect(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, expected {want!r}")
            print(f"  FAIL  {name}")
        else:
            print(f"  ok  {name}")

    def grid(f):
        return {dd: [f(dd)] * 3 for dd in GRID}
    base = grid(lambda dd: 0.05 if dd < 45 else 0.30 if dd == 45 else 0.60)
    sel = lambda *a, **k: select(*a, **k)[1]   # noqa: E731
    expect("base: selected 45", sel(_pilot(base)), 45)
    expect("d42 median=0.20: selected 42", sel(_pilot({**base, 42: [0.20, 0.25, 0.10]})), 42)
    expect("d42 median=0.199: selected 45", sel(_pilot({**base, 42: [0.199, 0.25, 0.10]})), 45)
    expect("d45 median=0.80: selected 45", sel(_pilot({**base, 45: [0.80] * 3})), 45)
    expect("d45 median=0.81: selected 46",
           sel(_pilot({**base, 45: [0.81] * 3})), 46)
    expect("d30 [0.0,0.1,0.9]: selected 45",
           sel(_pilot({**base, 30: [0.0, 0.1, 0.9]})), 45)
    jump = grid(lambda dd: 0.05 if dd < 45 else 0.95)
    got = select(_pilot(jump))
    expect("jump over band: none, 'no duty in the grid'",
           (got[1], "no duty in the grid loads the loop into" in (got[2] or "")), (None, True))
    got = select(_pilot(grid(lambda dd: 0.19)))
    expect("all 0.19: none, 'no duty'", (got[1], "no duty" in (got[2] or "")), (None, True))
    got = select(_pilot(base, steal={dd: [100, 100, 100] for dd in GRID}))
    expect("steal<=control: none, 'steal did not confirm arrival'",
           (got[1], "steal did not confirm arrival" in (got[2] or "")), (None, True))
    expect("d45 steal low r1: selected 46",
           sel(_pilot(base, steal={45: [3000, 100, 3000]})), 46)
    expect("d45 steal tie r2: selected 46",
           sel(_pilot(base, steal={45: [3000, 3000, 100]}, ctl_steal=100)), 46)
    expect("d45 results 1/3: selected 46",
           sel(_pilot(base, missing={(45, 0), (45, 1)})), 46)
    expect("d45 results 2/3: selected 45",
           sel(_pilot({**base, 45: [0.30, 0.10, 0.30]}, missing={(45, 1)})), 45)
    nc = _pilot(base)
    for r in nc["rounds"]:
        del r[PILOT_CONTROL]
    expect("no PILOT_CONTROL: none", sel(nc), None)
    pf = _pilot(base)
    pf["rounds"] = [{"one-round-" + k: v for k, v in r.items()} for r in pf["rounds"]]
    expect("prefix one-round-: selected 45", sel(pf, "one-round-"), 45)
    expect("prefixed, no prefix arg: none", sel(pf), None)
    tp = Path(tempfile.mkdtemp())
    for lab, rd in ((PILOT_CONTROL, 0.05), (pilot_arm(45), 0.3)):
        for i in range(3):
            (tp / f"{lab}-r{i}").mkdir()
            (tp / f"{lab}-r{i}" / "inject.meta").write_text(
                f"INJECT watched_wall_ns=1000\nINJECT watched_vcpus=4\n"
                f"INJECT watched_vcpu_rundelay_window_ns={int(rd * 4000)}\n")
    line = next(x for x in select(_pilot(base), runs=tp)[0] if x.startswith("duty 45:"))
    expect("duty 45 line: run-delay [0.3]x3 vs control [0.05]x3",
           "run-delay fraction [0.3, 0.3, 0.3] against control [0.05, 0.05, 0.05]" in line, True)
    shutil.rmtree(tp)

    import contextlib
    import io
    tr = Path(tempfile.mkdtemp())
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc1 = record(tr / "s", "a\nselected_duty=45")
        first = (tr / "s").read_text()
        rc2 = record(tr / "s", "a\nselected_duty=45")
        rc3 = record(tr / "s", "a\nselected_duty=46")
    expect("record new/match/mismatch: rc 0/0/3, file unchanged",
           (rc1, rc2, rc3, (tr / "s").read_text() == first), (0, 0, 3, True))
    shutil.rmtree(tr)

    def gd(pr, lo):
        return guards([{"fixed-hi-probe": {"over_deadline_frac": pr},
                        "fixed-lo": {"over_deadline_frac": lo}}])
    expect("probe=0.4 lo=0.01: guards not triggered",
           [x.split(":")[1].strip()[:3] for x in gd(0.4, 0.01)], ["not", "not"])
    expect("probe=0.9: P7/P8 NOT INFORMATIVE", gd(0.9, 0.01)[0].startswith("guard P7/P8: NOT INFORMATIVE"), True)
    expect("probe=0.899: P7/P8 not triggered", gd(0.899, 0.01)[0].startswith("guard P7/P8: not triggered"), True)
    expect("lo=0.20: P6 NOT INFORMATIVE",
           gd(0.4, 0.2)[1].startswith("guard P6: NOT INFORMATIVE"), True)
    expect("lo=0.199: P6 not triggered", gd(0.4, 0.199)[1].startswith("guard P6: not triggered"), True)

    lines, kept, void = arrival(_confirm())
    expect("all arrive: kept 16, not VOID", (len(kept), void), (16, False))
    expect("adaptive/fixed-lo<=control: kept 16", len(kept), 16)
    lines, kept, void = arrival(_confirm(low={0, 1, 2}))
    expect("fixed-hi<=control x3: kept 13", (len(kept), void), (13, False))
    lines, kept, void = arrival(_confirm(low={0, 1, 2, 3, 4}))
    expect("fixed-hi<=control x5: kept 11, VOID", (len(kept), void), (11, True))
    lines, kept, void = arrival(_confirm(low={0, 1}, hit0={2, 3}))
    expect("arrive 14, hit=0 x2: kept 14, not VOID",
           (len(kept), void), (14, False))
    lines, kept, void = arrival(_confirm(low={0, 1}, hit0={2, 3, 4}))
    expect("arrive 14, hit=0 x3: VOID", void, True)
    c = _confirm()
    for r in c["rounds"]:
        del r[R4_CONTROL]
    expect("no R4_CONTROL: kept 0, VOID", arrival(c)[1:], ([], True))
    lines, kept, void = arrival(_confirm(prefix="one-round-"), prefix="one-round-")
    expect("prefix one-round-: kept 16, plain labels",
           (len(kept), sorted(kept[0])), (16, sorted(list(ARMS) + [R4_CONTROL])))

    t = Path(tempfile.mkdtemp())
    for a, duty in (("fixed-hi", "20"), ("adaptive", "20"), ("fixed-lo", "50")):
        (t / f"{a}-r0").mkdir()
        (t / f"{a}-r0" / "inject.meta").write_text(
            f"INJECT mode=cpu-load cpus=8,9,10,11 partner=31 duration_s=12 rate_hz=2000 duty={duty} "
            f"rt_prio=90 slot_us=1000\n")
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        rc = keep_r2_runs(t)
    expect("keep_r2_runs: duty=20 moved, duty=50 left",
           (rc, (t / "pc-adapt-runs" / "fixed-hi-r0").is_dir(),
            (t / "pc-adapt-runs" / "adaptive-r0").is_dir(), (t / "fixed-lo-r0").is_dir()),
           (0, True, True, True))
    (t / "fixed-hi-r0").mkdir()
    (t / "fixed-hi-r0" / "inject.meta").write_text("INJECT mode=hog duty=20\n")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = keep_r2_runs(t)
    expect("keep_r2_runs existing: rc 1, not overwritten", (rc, (t / "fixed-hi-r0").is_dir()), (1, True))
    shutil.rmtree(t)

    if fails:
        print("\nSELFTEST FAILED")
        for f in fails:
            print(f"  {f}")
        return 1
    print("\nselftest: ok")
    return 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--select", metavar="PILOT")
    g.add_argument("--arrival", nargs=2, metavar=("BATCH", "OUT"))
    g.add_argument("--ratio", metavar="KEPT")
    g.add_argument("--keep-r2-runs", metavar="RESULTS_DIR")
    g.add_argument("--measurement-code", metavar="PREREGISTRATION")
    g.add_argument("--selftest", action="store_true")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--one-round-fallback", action="store_true")
    ap.add_argument("--record", metavar="SELECTION")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.ratio:
        return ratio(a.ratio)
    if a.keep_r2_runs:
        return keep_r2_runs(a.keep_r2_runs)
    if a.measurement_code:
        return measurement_code(a.measurement_code)
    if a.select:
        d = load(a.select)
        if d is None:
            print(f"r4_select: {a.select}: unreadable", file=sys.stderr)
            return 2
        lines, chosen, reason = select(d, a.prefix, Path(a.select).resolve().parent)
        out = [f"pilot: {a.select}"] + lines
        if chosen is None and a.one_round_fallback:
            out.append(f"ONE-ROUND: no duty qualified ({reason}); the grid's lowest, {GRID[0]}, is "
                       f"forced")
            out.append("one_round_forced=1")
            chosen = GRID[0]
        elif chosen is None:
            out.append(reason)
        out.append(f"selected_duty={'none' if chosen is None else chosen}")
        text = "\n".join(out)
        print(text)
        return record(a.record, text) if a.record else 0
    src, out = a.arrival
    d = load(src)
    if d is None:
        print(f"r4_select: {src}: unreadable", file=sys.stderr)
        return 2
    lines, kept, void = arrival(d, a.prefix, Path(src).resolve().parent)
    print("\n".join(lines))
    Path(out).write_text(json.dumps({
        "metric": d.get("metric"), "topology": d.get("topology"),
        "topology_why": d.get("topology_why"), "source": str(src),
        "note": "rounds of the source in which the injection arrived (benchmark/score/single_level/r4.py "
                "--arrival); labels stripped of any one-round prefix, values verbatim",
        "rounds": kept}, indent=1))
    return 4 if void else 0


if __name__ == "__main__":
    sys.exit(main())
