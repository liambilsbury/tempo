#!/usr/bin/env python3
"""Score the virtualisation levels batches against their pre-registered predictions

Usage: benchmark/score/single_level/levels.py <pc-ld0.json> <pc-ld1.json> <pcn-ld2.json> --duty D [--prefix P [--range A-B]] [--variant levels2]
       benchmark/score/single_level/levels.py --measurement-code preregistrations/<batch>.txt [--mark ladder2-harness:]
       benchmark/score/single_level/levels.py --selftest
  --duty D        the duty the pilot selected
  --prefix P      score a one-round run's prefixed labels
  --range A-B     frames to score, default 2000-9000
  --variant       levels2 scores pc-lb0, pc-lb1, pcn-lb2
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.score.single_level.r1_r3 import mann_whitney_p, sign_p   # noqa: E402

LEVEL_TABLE = (("L0", "pc-ld0", "single-level", True),
         ("L1", "pc-ld1", "single-level", False),
         ("L2", "pcn-ld2", "nested", False))
ROUNDS = 16
MIN_KEPT = 12
ALPHA = 0.05
RANGE = (2000, 9000)
RANGE_FRAMES = RANGE[1] - RANGE[0]
SATURATED = 900
FLOOR = 1.0
DUTY, SLOT_US = 45, 125
USER_HZ_NS = 10_000_000
PREDICTIONS = (("LAD1", "the first layer adds worst case", "L1", "L0", "max"),
               ("LAD2", "the first layer adds late frames", "L1", "L0", "late"),
               ("LAD3", "nesting adds worst case", "L2", "L1", "max"),
               ("LAD4", "nesting adds late frames", "L2", "L1", "late"))
MEASUREMENT_CODE_FILES = ("benchmark/", "src/", "tools/build/", "Makefile")
MEASUREMENT_CODE_MARK = "ladder-harness:"
R4_JSON = ROOT / "results" / "single_level" / "r4" / "pc-r4.json"
R4_PILOT_JSON = ROOT / "results" / "single_level" / "r4" / "pc-r4-pilot.json"
R4_RUNS = ROOT / "results" / "experiments"


def _num(v):
    """check that a value is an int or float, excluding bool"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def late_per_1000(a):
    """return frames lost per 1000 of the range, or None"""
    if not isinstance(a, dict) or not _num(a.get("range_lost")) or a.get("range_frames") != RANGE_FRAMES:
        return None
    return 1000.0 * a["range_lost"] / RANGE_FRAMES


def covered(a):
    """check that an arm's injection window covered the range"""
    return isinstance(a, dict) and a.get("range_covered") == 1


def signal(a, host):
    """return an arm's arrival signal"""
    if not isinstance(a, dict):
        return None
    if host:
        v = a.get("loop_rundelay_frac")
        return v if _num(v) else None
    if _num(a.get("steal_ticks")) and _num(a.get("frames")) and a["frames"] > 0:
        return 1000.0 * a["steal_ticks"] / a["frames"]
    return None


def arrived(hog, ctl):
    """check that the hog signal is strictly above the control's"""
    return hog is not None and ctl is not None and hog > ctl


def level(d, name, label, topology, host, prefix=""):
    """return a level's kept rounds and the values scored on them"""
    hog_l, ctl_l = f"{prefix}{label}-hog", f"{prefix}{label}-control"
    out = {"name": name, "void": "", "attempted": 0, "kept": 0, "arrival": 0, "complete": 0, "covered": 0,
           "hog": [], "ctl": [], "sig": [], "loaded": False, "saturated": False}
    if not isinstance(d, dict) or not isinstance(d.get("rounds"), list):
        out["void"] = "no rounds list"
        return out
    rounds = d["rounds"]
    out["rounds"] = rounds
    out["attempted"] = len(rounds)
    if d.get("topology") != topology:
        out["void"] = f"stamped {d.get('topology', 'no topology')}, not {topology}"
        return out
    out["windows"] = [a.get("window_seq") for r in rounds if isinstance(r, dict)
                      for a in (r.get(hog_l), r.get(ctl_l)) if isinstance(a, dict)]
    wrong = 0
    for r in rounds:
        for lab in (hog_l, ctl_l):
            a = r.get(lab) if isinstance(r, dict) else None
            if isinstance(a, dict) and ((a.get("level") == "host") != host):
                wrong += 1
    if wrong:
        out["void"] = (f"{wrong} arm result(s) not level=host" if host
                       else f"{wrong} arm result(s) level=host in a guest level")
        return out
    for i, r in enumerate(rounds):
        h = r.get(hog_l) if isinstance(r, dict) else None
        c = r.get(ctl_l) if isinstance(r, dict) else None
        if not all(isinstance(a, dict) and _num(a.get("range_max_ns")) and late_per_1000(a) is not None
                   for a in (h, c)):
            continue
        out["complete"] += 1
        if not (covered(h) and covered(c)):
            continue
        out["covered"] += 1
        sh, sc = signal(h, host), signal(c, host)
        if not arrived(sh, sc):
            continue
        out["arrival"] += 1
        out["hog"].append(h)
        out["ctl"].append(c)
        out["sig"].append((sh, sc, i))
    out["kept"] = len(out["hog"])
    if out["kept"] < MIN_KEPT:
        out["void"] = f"{out['kept']} of {ROUNDS} rounds kept, at least {MIN_KEPT} required"
        return out
    diffs = [h["range_max_ns"] - c["range_max_ns"] for h, c in zip(out["hog"], out["ctl"])]
    up, down = sum(x > 0 for x in diffs), sum(x < 0 for x in diffs)
    out["load"] = (up, down, sign_p(diffs))
    out["loaded"] = up > down and sign_p(diffs) < ALPHA
    out["saturated"] = median(late_per_1000(h) for h in out["hog"]) >= SATURATED
    return out


def values(rg, metric, arm="hog"):
    """return a metric's values over a level's kept rounds"""
    rows = rg[arm]
    if metric == "max":
        return [a["range_max_ns"] for a in rows]
    if metric == "p999":
        return [a["p999_ns"] for a in rows if _num(a.get("p999_ns"))]
    return [late_per_1000(a) for a in rows]


def compare(up, lo, metric, arm="hog"):
    """return (verdict, upper median, lower median, ratio, p) for two levels"""
    a, b = values(up, metric, arm), values(lo, metric, arm)
    if not a or not b:
        return "VOID", None, None, None, None
    mu, ml = median(a), median(b)
    p = mann_whitney_p(a, b)
    return ("HIT" if mu > ml and p < ALPHA else "MISS"), mu, ml, (mu / ml if ml else None), p


def _g(x):
    """format a number, or - for None"""
    return "-" if x is None else f"{x:.4g}"


def score(batches, prefix="", runs=None, r4=None):
    """score the three levels and return (lines, verdicts, any_void)"""
    rg = {}
    for d, (name, label, topo, host) in zip(batches, LEVEL_TABLE):
        rg[name] = level(d, name, label, topo, host, prefix)
    lines = [f"levels: three levels, hog duty {DUTY:g} (the pilot's selection) at slot {SLOT_US} against a no-injection control, "
             f"{ROUNDS} rounds each; a round is kept when the hog arm's arrival signal is strictly "
             f"above the same round's control's"]
    for name, label, topo, host in LEVEL_TABLE:
        r = rg[name]
        what = ("the loop's run-delay over the injector's window (loop_rundelay_frac)" if host
                else "steal_ticks per 1000 frames")
        lines.append(f"{name} {prefix}{label} ({'host, no guest' if host else topo}): "
                     f"attempted {r['attempted']}, complete {r['complete']}, window covered frames "
                     f"{RANGE[0]}-{RANGE[1]} in both arms {r['covered']}/{r['complete']}, arrival "
                     f"{r['arrival']}/{r['covered']} ({what}), kept {r['kept']}"
                     + (f" -- {name}: VOID ({r['void']})" if r["void"] else ""))
    verdicts = {}
    for key, words, up, lo, metric in PREDICTIONS:
        U, L = rg[up], rg[lo]
        if U["void"] or L["void"]:
            verdicts[key] = "VOID"
            which = " and ".join(n for n in (lo, up) if rg[n]["void"])
            lines.append(f"{key} ({words}): VOID -- {which} VOID, not scored")
            continue
        v, mu, ml, ratio, p = compare(U, L, metric)
        verdicts[key] = v
        unit = ("hog max_ns over the range" if metric == "max"
                else "hog frames lost per 1000 of the range")
        rtxt = f"{ratio:.3f}" if ratio is not None else f"undefined ({lo}'s median is 0)"
        lines.append(f"{key} ({words}): {v} -- {up}/{lo} median {unit} {rtxt} "
                     f"({_g(mu)} against {_g(ml)}), Mann-Whitney p={p:.4g}; "
                     f"falsified unless {up}'s median is above {lo}'s and p < {ALPHA}")
        if metric == "max":
            lines.append("  reported: hog minus control, median per round, " + "; ".join(
                f"{n} {_g(median(h['range_max_ns'] - c['range_max_ns'] for h, c in zip(rg[n]['hog'], rg[n]['ctl'])))} "
                f"({_g(median(h['range_max_ns'] - c['range_max_ns'] for h, c in zip(rg[n]['hog'], rg[n]['ctl'])) / median(values(rg[n], 'max', 'ctl')))} of its control median)"
                for n in (up, lo)))
        labels = []
        for n in (lo, up):
            if not rg[n]["loaded"]:
                labels.append(f"not informative: the injection did not load the loop at {n}")
            if metric == "late" and rg[n]["saturated"]:
                labels.append(f"not informative: {n} is saturated (hog median frames lost "
                              f">= {SATURATED} per 1000 of the range)")
        if metric == "late":
            uh, uc = median(values(U, "late")), median(values(U, "late", "ctl"))
            if not uh > uc or uh < FLOOR:
                labels.append(f"not informative: {up}'s hog median loses {uh:.4g} per 1000 of the range "
                              f"against its control's {uc:.4g} (floor {FLOOR}): too little loss to compare")
        for t in labels:
            lines.append(f"  guard {key}: {t}")
    for name, *_ in LEVEL_TABLE:
        r = rg[name]
        if r["void"]:
            continue
        up, down, p = r["load"]
        lines.append(f"guard load {name}: {'loaded' if r['loaded'] else 'NOT LOADED'} -- hog max above "
                     f"its round's control in {up} of {r['kept']} kept rounds, below in {down}, "
                     f"sign test p={p:.4g}")
        sat = median(values(r, "late"))
        lines.append(f"guard saturation {name}: {'SATURATED' if r['saturated'] else 'not saturated'} "
                     f"-- hog median frames lost {sat:.4g} per 1000 of the range (bound {SATURATED})")
    lines += reported(rg, prefix, runs, r4)
    lines.append("verdicts  " + "  ".join(f"{k}={verdicts[k]}" for k, *_ in PREDICTIONS))
    return lines, verdicts, any(rg[n]["void"] for n, *_ in LEVEL_TABLE)


def _kv(path):
    """parse key=value tokens from a meta file"""
    out = {}
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return out
    for tok in text.replace("INJECT ", "").split():
        k, sep, v = tok.partition("=")
        if sep and k not in out:
            out[k] = v
    return out


def steal_share(rounds, hog_l, ctl_l, rdir, want_duty=None):
    """return per round the steal the guest read over the run-delay the host charged"""
    out = []
    for i, r in enumerate(rounds):
        h, c = (r.get(hog_l), r.get(ctl_l)) if isinstance(r, dict) else (None, None)
        if not (isinstance(h, dict) and isinstance(c, dict) and _num(h.get("steal_ticks"))
                and _num(c.get("steal_ticks"))):
            continue
        hm, cm = _kv(Path(rdir) / f"{hog_l}-r{i}" / "inject.meta"), _kv(Path(rdir) / f"{ctl_l}-r{i}" / "inject.meta")
        if want_duty is not None and (hm.get("duty") != f"{want_duty:g}" or hm.get("slot_us") != str(SLOT_US)
                                      or cm.get("mode") != "none"):
            continue
        try:
            told = int(hm["watched_vcpu_rundelay_window_ns"]) - int(cm["watched_vcpu_rundelay_window_ns"])
        except (KeyError, ValueError):
            continue
        if told <= 0:
            continue
        out.append((h["steal_ticks"] - c["steal_ticks"]) * USER_HZ_NS / told)
    return out


def reported(rg, prefix, runs, r4):
    """return the lines printed beside the verdicts and never scored"""
    L = ["reported, not scored:"]
    for name, *_ in LEVEL_TABLE:
        w = [x.split("-") for x in rg[name].get("windows", []) if isinstance(x, str) and re.fullmatch(r"\d+-\d+", x)]
        n = len(rg[name].get("windows", []))
        lo_ = f"{median(int(a) for a, _ in w):.0f}" if w else "-"
        hi_ = f"{median(int(b) for _, b in w):.0f}" if w else "-"
        L.append(f"  {name}: the injection window spanned frames {lo_}"
                 f" to {hi_} (medians over {len(w)} of {n} runs "
                 f"with a readable window; the range is {RANGE[0]}-{RANGE[1]})")
    for name, label, topo, host in LEVEL_TABLE:
        r = rg[name]
        if r["void"]:
            continue
        hm, cm = median(values(r, "max")), median(values(r, "max", "ctl"))
        per = median(h["range_max_ns"] / c["range_max_ns"] for h, c in zip(r["hog"], r["ctl"]) if c["range_max_ns"])
        sh, sc = median(s[0] for s in r["sig"]), median(s[1] for s in r["sig"])
        L.append(f"  {name}: hog/control median max {hm / cm:.3f} (per-round median {per:.3f}); "
                 f"control frames lost {median(values(r, 'late', 'ctl')):.4g} per 1000 of the range; "
                 f"arrival signal median hog {sh:.4g} against control {sc:.4g}")
    for up, lo in (("L1", "L0"), ("L2", "L1")):
        if rg[up]["void"] or rg[lo]["void"]:
            continue
        _, mu, ml, ratio, p = compare(rg[up], rg[lo], "max", "ctl")
        L.append(f"  control arms {up}/{lo}: median max {_g(ratio)} (p={p:.4g}) -- what the layer "
                 f"adds with nothing injected")
        _, mu, ml, ratio, p = compare(rg[up], rg[lo], "p999")
        L.append(f"  hog arms {up}/{lo}: median p99.9 {_g(ratio)} (p={_g(p)})")
    if runs:
        for (name, label, topo, host), rdir, d in zip(LEVEL_TABLE, runs, rg.values()):
            if not rdir:
                continue
            rd = Path(rdir)
            n = d["attempted"]
            inj = sum(1 for i in range(n)
                      if _kv(rd / f"{prefix}{label}-hog-r{i}" / "inject.meta").get("duty") == f"{DUTY:g}"
                      and _kv(rd / f"{prefix}{label}-hog-r{i}" / "inject.meta").get("slot_us") == str(SLOT_US)
                      and _kv(rd / f"{prefix}{label}-hog-r{i}" / "inject.meta").get("mode") in ("cpu-load", "hog"))
            none = sum(1 for i in range(n)
                       if _kv(rd / f"{prefix}{label}-control-r{i}" / "inject.meta").get("mode") == "none")
            line = f"  {name} injection as pre-registered: hog {inj}/{n} runs (mode cpu-load or hog, duty {DUTY:g}, slot {SLOT_US}), control {none}/{n} (mode none)"
            if host:
                rt = sum(1 for lab in ("hog", "control") for i in range(n)
                         if _kv(rd / f"{prefix}{label}-{lab}-r{i}" / "l2" / "kernel.meta").get("kernel_image_preempt") == "PREEMPT_RT")
                ml = sum(1 for lab in ("hog", "control") for i in range(n)
                         if _kv(rd / f"{prefix}{label}-{lab}-r{i}" / "l2" / "header.txt").get("mlock_failed_stages", "0") not in ("0", ""))
                line += f"; the loop's kernel PREEMPT_RT in {rt}/{2 * n} runs; mlockall failed in some stage in {ml}/{2 * n} runs"
            L.append(line)
    shares = []
    if runs:
        for (name, label, topo, host), rdir, d in zip(LEVEL_TABLE, runs, rg.values()):
            if host or not rdir or d["void"]:
                continue
            s = steal_share(d.get("rounds", []), f"{prefix}{label}-hog", f"{prefix}{label}-control", rdir)
            shares.append(f"{name} {_g(median(s)) if s else '-'} ({len(s)} rounds)")
    if r4 is not None:
        r4json, r4pilot, r4runs = r4
        src, hog, ctl = None, None, None
        if DUTY == 45:
            src, hog, ctl = r4json, "fixed-hi-probe", "pc-r4-control"
        elif float(DUTY).is_integer() and int(DUTY) in _r4_pilot_grid():
            src, hog, ctl = r4pilot, f"pc-r4p-probe-d{int(DUTY)}", "pc-r4p-control"
        try:
            rr = json.loads(Path(src).read_text()).get("rounds", []) if src else []
        except (OSError, ValueError):
            rr = []
        s = steal_share(rr, hog, ctl, r4runs, want_duty=DUTY) if src else []
        shares.append(f"R4's {hog or 'arms'} against {ctl or 'its control'} on the non-RT host run "
                      f"{_g(median(s)) if s else '-'} ({len(s)} rounds with run directories at duty "
                      f"{DUTY:g}{'' if src else '; R4 ran no arm at this duty'})")
    if shares:
        L.append("  cross-run, reported and never scored: steal the guest read over the vCPU run-delay "
                 "its host charged, hog minus control, median per round: " + "; ".join(shares))
    return L


def _r4_pilot_grid():
    """return R4's pilot duty grid"""
    from benchmark.score.single_level import r4 as r4_select
    return tuple(r4_select.GRID)


LEVEL_TABLE_BY_VARIANT = {
    "levels": LEVEL_TABLE,
    "levels2": (("L0", "pc-lb0", "single-level", True),
                ("L1", "pc-lb1", "single-level", False),
                ("L2", "pcn-lb2", "nested", False)),
}


def measurement_code(doc, mark=None):
    """return 0 when the pre-registered measurement code commit matches HEAD"""
    try:
        m = re.search(r"<!--\s*" + (mark or MEASUREMENT_CODE_MARK) + r"\s*([0-9a-f]{7,40})\s*-->", Path(doc).read_text())
    except OSError:
        m = None
    if not m:
        print(f"levels_score: refusing -- {doc} names no measurement code commit (<!-- {mark or MEASUREMENT_CODE_MARK} <sha> -->)",
              file=sys.stderr)
        return 3
    sha = m.group(1)
    r = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", sha, "HEAD", "--", *MEASUREMENT_CODE_FILES],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"levels_score: refusing -- measurement code differs from {sha} "
              f"in {' '.join(MEASUREMENT_CODE_FILES)} "
              f"({r.stderr.strip() or 'git diff --quiet exited ' + str(r.returncode)})", file=sys.stderr)
        return 3
    print(f"levels_score: measurement code commit {sha} matches HEAD in {' '.join(MEASUREMENT_CODE_FILES)}")
    return 0


def _arm(mx, late=0, sig=1.0, host=False, frames=20000, dropped=0, p999=None):
    """build an arm's result for the selftest"""
    a = {"max_ns": mx, "p999_ns": p999 if p999 is not None else mx, "over_deadline": 0,
         "late": 0, "frames": frames, "dropped": dropped, "hit": 5,
         "range_frames": RANGE_FRAMES, "range_lost": round(late * RANGE_FRAMES / 1000),
         "range_max_ns": mx, "range_covered": 1, "window_seq": "0-11300"}
    if host:
        a["level"] = "host"
        a["loop_rundelay_frac"] = sig
    else:
        a["steal_ticks"] = int(sig * frames / 1000)
    return a


def _batch(label, topo, host, hog, ctl=None, n=16, hsig=5.0, csig=1.0, **kw):
    """build a level's batch for the selftest"""
    hog = hog if isinstance(hog, list) else [hog] * n
    ctl = ctl if isinstance(ctl, list) else [ctl or 100] * n
    hs = hsig if isinstance(hsig, list) else [hsig] * n
    late = kw.get("late", 0)
    late = late if isinstance(late, list) else [late] * n
    return {"topology": topo, "rounds": [
        {f"{label}-hog": _arm(hog[i], late[i], hs[i], host),
         f"{label}-control": _arm(ctl[i], 0, csig, host)} for i in range(n)]}


def _levels(l0=150, l1=200, l2=300, late=(10, 400, 500), **kw):
    """build the three levels' batches for the selftest"""
    return (_batch("pc-ld0", "single-level", True, l0, late=late[0], **kw.get("k0", {})),
            _batch("pc-ld1", "single-level", False, l1, late=late[1], **kw.get("k1", {})),
            _batch("pcn-ld2", "nested", False, l2, late=late[2], **kw.get("k2", {})))


def selftest():
    """run the scorer's selftest cases"""
    global DUTY
    import tempfile
    cases = []

    def v(b, **kw):
        """return the verdicts for batches"""
        return score(b, **kw)[1]

    def text(b, **kw):
        """return the printed text for batches"""
        return "\n".join(score(b, **kw)[0])

    ok = v(_levels())
    cases.append(("every prediction HIT when each level adds worst case and late frames",
                  ok == {"LAD1": "HIT", "LAD2": "HIT", "LAD3": "HIT", "LAD4": "HIT"}))
    cases.append(("MISS when L1's worst case equals L0's", v(_levels(l1=150))["LAD1"] == "MISS"))
    cases.append(("MISS when nesting lowers the worst case", v(_levels(l2=180))["LAD3"] == "MISS"))
    lo = list(range(100, 260, 10))
    hi = [x + 1 for x in lo]
    cases.append(("MISS when the medians rise but do not separate",
                  v(_levels(l0=lo, l1=hi))["LAD1"] == "MISS"))
    b = _levels(late=(0, 400, 500))
    cases.append(("LAD2 HIT with L0 at zero late frames, its ratio printed as undefined",
                  v(b)["LAD2"] == "HIT" and "undefined (L0's median is 0)" in text(b)))
    b = _levels(late=(0, 0, 500))
    cases.append(("floor: neither L0 nor L1 loses a frame -- LAD2 scored MISS and labelled not informative",
                  v(b)["LAD2"] == "MISS" and "guard LAD2: not informative: L1's hog median loses 0" in text(b)))
    b = _levels(late=(0, 0.5, 500))
    cases.append(("floor: L1 above L0 but below 1 per 1000 of the range is labelled",
                  "guard LAD2: not informative: L1's hog median loses 0.5" in text(b)))
    b = _levels()
    for r in b[1]["rounds"]:
        r["pc-ld1-control"]["range_lost"] = r["pc-ld1-hog"]["range_lost"]
    cases.append(("floor: L1's hog no lossier than its own control is labelled",
                  "guard LAD2: not informative: L1's hog median loses 400" in text(b)
                  and "guard LAD4" not in text(b)))
    b = list(_levels())
    for r in b[1]["rounds"][:5]:
        r["pc-ld1-hog"]["range_frames"] = 5000
    cases.append(("a round scored over another range is incomplete (11 left: L1 VOID)", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    for r in b[2]["rounds"][:5]:
        r["pcn-ld2-control"]["range_covered"] = 0
    vv, t = v(tuple(b)), text(tuple(b))
    cases.append(("a window that did not cover the range excludes the round, counted: L2 11 kept, VOID",
                  vv["LAD3"] == "VOID" and vv["LAD1"] == "HIT" and "in both arms 11/16" in t))
    b = list(_levels())
    for r in b[0]["rounds"][:4]:
        r["pc-ld0-hog"]["range_covered"] = "-"
    cases.append(("an unreadable coverage is not covered, and 12 of 16 is enough", v(tuple(b))["LAD1"] == "HIT"))
    b = _levels()
    for r in b[1]["rounds"]:
        r["pc-ld1-hog"]["max_ns"] = 1
    cases.append(("the worst case is the range's (a whole-run max is ignored)", v(b)["LAD1"] == "HIT"))
    cases.append(("each level's window span is reported, also for a VOID level",
                  "  L2: the injection window spanned frames 0 to 11300 (medians over 32 of 32 runs" in text(_levels())
                  and "  L1: the injection window spanned frames 0 to 11300 (medians over 22 of 22" in
                  text((_levels()[0], dict(_levels()[1], rounds=_levels()[1]["rounds"][:11]), _levels()[2]))))
    cases.append(("hog minus control is reported per level beside LAD1 and LAD3",
                  "  reported: hog minus control, median per round, L1 100 (1 of its control median); L0 50 (0.5 of its control median)" in text(_levels())))
    b = list(_levels())
    for r in b[1]["rounds"][:5]:
        r["pc-ld1-hog"]["steal_ticks"] = r["pc-ld1-control"]["steal_ticks"]
    vv = v(tuple(b))
    cases.append(("a steal tie is not arrival: L1 with 11 kept rounds is VOID, and so is every prediction using it",
                  vv == {"LAD1": "VOID", "LAD2": "VOID", "LAD3": "VOID", "LAD4": "VOID"}))
    b = list(_levels())
    for r in b[1]["rounds"][:4]:
        r["pc-ld1-hog"]["steal_ticks"] = r["pc-ld1-control"]["steal_ticks"]
    cases.append(("12 kept rounds of 16 is enough", v(tuple(b))["LAD1"] == "HIT"))
    b = list(_levels())
    for r in b[0]["rounds"][:5]:
        r["pc-ld0-hog"]["loop_rundelay_frac"] = r["pc-ld0-control"]["loop_rundelay_frac"]
    vv = v(tuple(b))
    cases.append(("L0's arrival is the loop's run-delay: a tie there voids L0 and only its predictions",
                  (vv["LAD1"], vv["LAD2"], vv["LAD3"]) == ("VOID", "VOID", "HIT")))
    b = list(_levels())
    for r in b[0]["rounds"][:5]:
        r["pc-ld0-hog"]["loop_rundelay_frac"] = "-"
    cases.append(("a run-delay that could not be read is not arrival", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    for r in b[2]["rounds"][:6]:
        del r["pcn-ld2-hog"]
    cases.append(("a level with 10 complete rounds is VOID", v(tuple(b))["LAD3"] == "VOID"))
    b = list(_levels())
    b[2] = dict(b[2], topology="single-level")
    cases.append(("L2 stamped single-level is VOID", v(tuple(b))["LAD3"] == "VOID"))
    b = list(_levels())
    b[0] = dict(b[0], topology="nested")
    cases.append(("L0 stamped nested is VOID", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    for r in b[0]["rounds"]:
        del r["pc-ld0-hog"]["level"]
    cases.append(("an L0 arm that did not run at level=host voids L0", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    b[1]["rounds"][0]["pc-ld1-control"]["level"] = "host"
    cases.append(("a level=host arm in a guest level voids it", v(tuple(b))["LAD1"] == "VOID"))
    b = _levels(l0=100)
    t = text(b)
    cases.append(("load guard: L0's hog no higher than its control labels LAD1 and LAD2, verdicts unchanged",
                  v(b)["LAD1"] == "HIT" and "guard LAD1: not informative: the injection did not load the loop at L0" in t
                  and "guard LAD2: not informative: the injection did not load the loop at L0" in t
                  and "guard LAD3" not in t and "guard load L0: NOT LOADED" in t))
    b = _levels(late=(10, 400, 950))
    t = text(b)
    cases.append(("saturation guard labels LAD4 only, and says SATURATED",
                  "guard LAD4: not informative: L2 is saturated" in t and "guard LAD3" not in t
                  and "guard saturation L2: SATURATED" in t and v(b)["LAD4"] == "HIT"))
    cases.append(("guards stay silent on a clean levels", "  guard LAD" not in text(_levels())))
    b = tuple({"topology": x["topology"], "rounds": [{f"one-round-{k}": a for k, a in r.items()} for r in x["rounds"]]}
              for x in _levels())
    cases.append(("a one-round run's prefixed labels are scored with --prefix", v(b, prefix="one-round-")["LAD1"] == "HIT"))
    cases.append(("without the prefix a one-round run's batch is VOID", v(b)["LAD1"] == "VOID"))
    cases.append(("the verdicts line", "verdicts  LAD1=HIT  LAD2=HIT  LAD3=HIT  LAD4=HIT" in text(_levels())))
    with tempfile.TemporaryDirectory() as d:
        dd = Path(d)
        b = _levels()
        for lab, arm, rd in (("pc-ld1", "hog", 3_000_000_000), ("pc-ld1", "control", 1_000_000_000)):
            for i in range(16):
                (dd / f"{lab}-{arm}-r{i}").mkdir(parents=True)
                (dd / f"{lab}-{arm}-r{i}" / "inject.meta").write_text(
                    f"INJECT mode={'hog' if arm == 'hog' else 'none'} cpus=8,9,10,11 duty={DUTY if arm == 'hog' else 20} "
                    f"slot_us={SLOT_US}\nINJECT watched_vcpu_rundelay_window_ns={rd}\n")
        for i in range(16):
            (dd / f"pc-ld0-hog-r{i}" / "l2").mkdir(parents=True)
            (dd / f"pc-ld0-hog-r{i}" / "l2" / "kernel.meta").write_text("kernel_arm=host kernel_image_preempt=PREEMPT_RT\n")
            (dd / f"pc-ld0-hog-r{i}" / "l2" / "header.txt").write_text("mlock_failed_stages=1\n")
        t = text(b, runs=(str(dd), str(dd), None))
        cases.append(("the cross-run steal share is read over told run-delay, hog minus control",
                      "L1 0.4 (16 rounds)" in t))
        cases.append(("the injection record counts hog runs at the pre-registered duty and slot",
                      "L1 injection as pre-registered: hog 16/16 runs" in t and "control 16/16 (mode none)" in t))
        cases.append(("L0's kernel and mlock record are reported",
                      "the loop's kernel PREEMPT_RT in 16/32 runs; mlockall failed in some stage in 16/32 runs" in t))
        r4d = dd / "r4"
        r4r = {"rounds": [{"fixed-hi-probe": {"steal_ticks": 60}, "pc-r4-control": {"steal_ticks": 10}}] * 16}
        (r4d).mkdir()
        (r4d / "pc-r4.json").write_text(json.dumps(r4r))
        for i in range(16):
            for arm, mode, rd in (("fixed-hi-probe", "hog", 1_500_000_000), ("pc-r4-control", "none", 500_000_000)):
                (r4d / f"{arm}-r{i}").mkdir()
                duty = 45 if i < 10 else 20
                (r4d / f"{arm}-r{i}" / "inject.meta").write_text(
                    f"INJECT mode={mode} duty={duty} slot_us=125\nINJECT watched_vcpu_rundelay_window_ns={rd}\n")
        t = text(b, r4=(r4d / "pc-r4.json", r4d / "absent-pilot.json", r4d))
        cases.append(("R4's reference uses only its duty-45 rounds (50 ticks x 10 ms / 1 s = 0.5)",
                      "non-RT host run 0.5 (10 rounds with run directories at duty 45)" in t))
        keep = DUTY
        pil = {"rounds": [{"pc-r4p-probe-d44": {"steal_ticks": 30}, "pc-r4p-control": {"steal_ticks": 10}}] * 3}
        (r4d / "pc-r4-pilot.json").write_text(json.dumps(pil))
        for i in range(3):
            for arm, mode, dut in (("pc-r4p-probe-d44", "hog", 44), ("pc-r4p-control", "none", 0)):
                (r4d / f"{arm}-r{i}").mkdir()
                (r4d / f"{arm}-r{i}" / "inject.meta").write_text(
                    f"INJECT mode={mode} duty={dut} slot_us=125\nINJECT watched_vcpu_rundelay_window_ns={1_000_000_000 if mode == 'hog' else 0}\n")
        try:
            DUTY = 44
            t44 = text(b, r4=(r4d / "pc-r4.json", r4d / "pc-r4-pilot.json", r4d))
            DUTY = 44.5
            t445 = text(b, r4=(r4d / "pc-r4.json", r4d / "pc-r4-pilot.json", r4d))
        finally:
            DUTY = keep
        cases.append(("at a whole duty of R4's pilot grid, R4's pilot arm is the reference (20 ticks x 10 ms / 1 s = 0.2)",
                      "R4's pc-r4p-probe-d44 against pc-r4p-control on the non-RT host run 0.2 (3 rounds" in t44))
        try:
            DUTY = 43
            t43 = text(b, r4=(r4d / "pc-r4.json", r4d / "pc-r4-pilot.json", r4d))
        finally:
            DUTY = keep
        cases.append(("a whole duty R4's grid never had is said so, not looked up as a pilot arm",
                      "R4 ran no arm at this duty" in t43 and "pc-r4p-probe-d43" not in t43))
        cases.append(("at a duty R4 never ran, no reference is claimed",
                      "R4 ran no arm at this duty" in t445 and "hog duty 44.5 (the pilot's selection)" in t445))
    global LEVEL_TABLE
    keep_r = LEVEL_TABLE
    try:
        LEVEL_TABLE = LEVEL_TABLE_BY_VARIANT["levels2"]
        ren = {"pc-ld0": "pc-lb0", "pc-ld1": "pc-lb1", "pcn-ld2": "pcn-lb2"}
        b2 = tuple(dict(x, rounds=[{next(ren[p] + k[len(p):] for p in ren if k.startswith(p + "-")): a
                                     for k, a in r.items()} for r in x["rounds"]]) for x in _levels())
        v2 = v(b2)
        v1 = v(_levels())
    finally:
        LEVEL_TABLE = keep_r
    cases.append(("revision 2's levels score under pc-lb0, pc-lb1, pcn-lb2; revision 1's labels are VOID there",
                  v2["LAD1"] == "HIT" and v1["LAD1"] == "VOID"))
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "doc.md"
        doc.write_text("no marker\n")
        cases.append(("the measurement code check refuses a doc with no marker", measurement_code(doc) == 3))
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        doc.write_text(f"<!-- {MEASUREMENT_CODE_MARK} {head} -->\n")
        dirty = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", *MEASUREMENT_CODE_FILES]).returncode
        cases.append(("the measurement code check passes on HEAD's own commit (clean tree)", dirty != 0 or measurement_code(doc) == 0))
    def safe(f):
        """return f's result, or the name of what it raised"""
        try:
            return f()
        except Exception as e:  # noqa: BLE001
            return f"raised {type(e).__name__}"
    cases.append(("a batch with no rounds list is VOID",
                  safe(lambda: v(({"topology": "single-level"},) + _levels()[1:])["LAD1"]) == "VOID"))
    cases.append(("a '-' max is an incomplete round",
                  safe(lambda: v(tuple(dict(x, rounds=[{k: dict(a, range_max_ns="-") for k, a in r.items()} for r in x["rounds"]])
                                       for x in _levels()))["LAD1"]) == "VOID"))
    bad = [c for c, good in cases if not good]
    for c, good in cases:
        print(("ok   " if good else "FAIL ") + c)
    print(f"levels_score selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
    """parse arguments and score three batches"""
    global DUTY, RANGE, RANGE_FRAMES, LEVEL_TABLE
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) == 2 and argv[0] == "--measurement-code":
        return measurement_code(argv[1])
    if len(argv) == 4 and argv[0] == "--measurement-code" and argv[2] == "--mark" and argv[3] in (MEASUREMENT_CODE_MARK, "ladder2-harness:", "r4b-harness:", "r5-harness:", "r6-harness:", "r7-harness:", "r7b-harness:", "rhizo-harness:", "rhizo-drift-harness:", "rhizo-headroom-harness:"):
        return measurement_code(argv[1], argv[3])
    opts = {}
    for o in ("--prefix", "--duty", "--range", "--variant"):
        if o in argv:
            i = argv.index(o)
            if i + 1 >= len(argv):
                print(__doc__, file=sys.stderr)
                return 2
            opts[o] = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
    prefix = opts.get("--prefix", "")
    if opts.get("--variant", "levels") not in LEVEL_TABLE_BY_VARIANT:
        print(__doc__, file=sys.stderr)
        return 2
    LEVEL_TABLE = LEVEL_TABLE_BY_VARIANT[opts.get("--variant", "levels")]
    try:
        DUTY = float(opts["--duty"])
    except (KeyError, ValueError):
        print("levels_score: --duty <the pilot's selected duty> is required\n" + __doc__, file=sys.stderr)
        return 2
    if "--range" in opts:
        m = re.fullmatch(r"(\d+)-(\d+)", opts["--range"])
        if not prefix or not m or int(m.group(1)) >= int(m.group(2)):
            print("levels_score: --range is a one-round run's A-B, with --prefix, A < B", file=sys.stderr)
            return 2
        RANGE = (int(m.group(1)), int(m.group(2)))
        RANGE_FRAMES = RANGE[1] - RANGE[0]
    if len(argv) != 3 or any(a.startswith("--") for a in argv):
        print(__doc__, file=sys.stderr)
        return 2
    batches = []
    for p in argv:
        try:
            batches.append(json.loads(Path(p).read_text()))
        except (OSError, ValueError) as e:
            print(f"levels: cannot read {p} ({e}); not scored")
            return 1
    runs = tuple(str(Path(p).resolve().parent) for p in argv)
    lines, verdicts, void = score(batches, prefix, runs, (R4_JSON, R4_PILOT_JSON, R4_RUNS))
    print("\n".join(lines))
    return 4 if void else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
