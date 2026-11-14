#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path
from statistics import median

USAGE = """Usage: benchmark/score/single_level/levels.py <pc-ld0.json> <pc-ld1.json> <pcn-ld2.json> --duty D [--prefix P [--range A-B]] [--variant levels2]
       benchmark/score/single_level/levels.py --measurement-code preregistrations/<batch>.txt [--mark ladder2-harness:]
       benchmark/score/single_level/levels.py --selftest
"""

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
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def late_per_1000(a):
    if not isinstance(a, dict) or not _num(a.get("range_lost")) or a.get("range_frames") != RANGE_FRAMES:
        return None
    return 1000.0 * a["range_lost"] / RANGE_FRAMES


def covered(a):
    return isinstance(a, dict) and a.get("range_covered") == 1


def signal(a, host):
    if not isinstance(a, dict):
        return None
    if host:
        v = a.get("loop_rundelay_frac")
        return v if _num(v) else None
    if _num(a.get("steal_ticks")) and _num(a.get("frames")) and a["frames"] > 0:
        return 1000.0 * a["steal_ticks"] / a["frames"]
    return None


def arrived(hog, ctl):
    return hog is not None and ctl is not None and hog > ctl


def level(d, name, label, topology, host, prefix=""):
    hog_l, ctl_l = f"{prefix}{label}-hog", f"{prefix}{label}-control"
    out = {"name": name, "void": "", "attempted": 0, "kept": 0, "arrival": 0, "complete": 0, "covered": 0,
           "hog": [], "ctl": [], "sig": [], "loaded": False, "saturated": False}
    if not isinstance(d, dict) or not isinstance(d.get("rounds"), list):
        out["void"] = "no rounds"
        return out
    rounds = d["rounds"]
    out["rounds"] = rounds
    out["attempted"] = len(rounds)
    if d.get("topology") != topology:
        out["void"] = f"topology {d.get('topology')} != {topology}"
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
        out["void"] = f"{wrong} not level=host" if host else f"{wrong} level=host"
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
        out["void"] = f"kept {out['kept']}/{ROUNDS} < {MIN_KEPT}"
        return out
    diffs = [h["range_max_ns"] - c["range_max_ns"] for h, c in zip(out["hog"], out["ctl"])]
    up, down = sum(x > 0 for x in diffs), sum(x < 0 for x in diffs)
    out["load"] = (up, down, sign_p(diffs))
    out["loaded"] = up > down and sign_p(diffs) < ALPHA
    out["saturated"] = median(late_per_1000(h) for h in out["hog"]) >= SATURATED
    return out


def values(rg, metric, arm="hog"):
    rows = rg[arm]
    if metric == "max":
        return [a["range_max_ns"] for a in rows]
    if metric == "p999":
        return [a["p999_ns"] for a in rows if _num(a.get("p999_ns"))]
    return [late_per_1000(a) for a in rows]


def compare(up, lo, metric, arm="hog"):
    a, b = values(up, metric, arm), values(lo, metric, arm)
    if not a or not b:
        return "VOID", None, None, None, None
    mu, ml = median(a), median(b)
    p = mann_whitney_p(a, b)
    return ("HIT" if mu > ml and p < ALPHA else "MISS"), mu, ml, (mu / ml if ml else None), p


def _g(x):
    return "-" if x is None else f"{x:.4g}"


def score(batches, prefix="", runs=None, r4=None):
    rg = {}
    for d, (name, label, topo, host) in zip(batches, LEVEL_TABLE):
        rg[name] = level(d, name, label, topo, host, prefix)
    lines = [f"levels: hog duty {DUTY:g} slot {SLOT_US}, {ROUNDS} rounds"]
    for name, label, topo, host in LEVEL_TABLE:
        r = rg[name]
        what = "loop_rundelay_frac" if host else "steal/1000 frames"
        lines.append(f"{name} {prefix}{label} ({'host' if host else topo}): "
                     f"attempted {r['attempted']}, complete {r['complete']}, covered {r['covered']}/{r['complete']}, arrival "
                     f"{r['arrival']}/{r['covered']} ({what}), kept {r['kept']}"
                     + (f" -- {name}: VOID ({r['void']})" if r["void"] else ""))
    verdicts = {}
    for key, words, up, lo, metric in PREDICTIONS:
        U, L = rg[up], rg[lo]
        if U["void"] or L["void"]:
            verdicts[key] = "VOID"
            which = " and ".join(n for n in (lo, up) if rg[n]["void"])
            lines.append(f"{key} ({words}): VOID -- {which}")
            continue
        v, mu, ml, ratio, p = compare(U, L, metric)
        verdicts[key] = v
        unit = "hog max_ns" if metric == "max" else "hog lost/1000"
        rtxt = f"{ratio:.3f}" if ratio is not None else f"undefined ({lo} median 0)"
        lines.append(f"{key} ({words}): {v} -- {up}/{lo} median {unit} {rtxt} "
                     f"({_g(mu)} against {_g(ml)}), Mann-Whitney p={p:.4g}")
        if metric == "max":
            lines.append("  reported: hog-control median, " + "; ".join(
                f"{n} {_g(median(h['range_max_ns'] - c['range_max_ns'] for h, c in zip(rg[n]['hog'], rg[n]['ctl'])))} "
                f"({_g(median(h['range_max_ns'] - c['range_max_ns'] for h, c in zip(rg[n]['hog'], rg[n]['ctl'])) / median(values(rg[n], 'max', 'ctl')))} of control)"
                for n in (up, lo)))
        labels = []
        for n in (lo, up):
            if not rg[n]["loaded"]:
                labels.append(f"not informative: {n} not loaded")
            if metric == "late" and rg[n]["saturated"]:
                labels.append(f"not informative: {n} saturated")
        if metric == "late":
            uh, uc = median(values(U, "late")), median(values(U, "late", "ctl"))
            if not uh > uc or uh < FLOOR:
                labels.append(f"not informative: {up} hog {uh:.4g} vs control {uc:.4g} (floor {FLOOR})")
        for t in labels:
            lines.append(f"  guard {key}: {t}")
    for name, *_ in LEVEL_TABLE:
        r = rg[name]
        if r["void"]:
            continue
        up, down, p = r["load"]
        lines.append(f"guard load {name}: {'loaded' if r['loaded'] else 'NOT LOADED'} -- up {up}/{r['kept']}, "
                     f"down {down}, sign p={p:.4g}")
        sat = median(values(r, "late"))
        lines.append(f"guard saturation {name}: {'SATURATED' if r['saturated'] else 'not saturated'} "
                     f"-- lost/1000 {sat:.4g} (bound {SATURATED})")
    lines += reported(rg, prefix, runs, r4)
    lines.append("verdicts  " + "  ".join(f"{k}={verdicts[k]}" for k, *_ in PREDICTIONS))
    return lines, verdicts, any(rg[n]["void"] for n, *_ in LEVEL_TABLE)


def _kv(path):
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
    L = ["reported, not scored:"]
    for name, *_ in LEVEL_TABLE:
        w = [x.split("-") for x in rg[name].get("windows", []) if isinstance(x, str) and re.fullmatch(r"\d+-\d+", x)]
        n = len(rg[name].get("windows", []))
        lo_ = f"{median(int(a) for a, _ in w):.0f}" if w else "-"
        hi_ = f"{median(int(b) for _, b in w):.0f}" if w else "-"
        L.append(f"  {name}: the injection window {lo_}-{hi_} (median, {len(w)}/{n} runs)")
    for name, label, topo, host in LEVEL_TABLE:
        r = rg[name]
        if r["void"]:
            continue
        hm, cm = median(values(r, "max")), median(values(r, "max", "ctl"))
        per = median(h["range_max_ns"] / c["range_max_ns"] for h, c in zip(r["hog"], r["ctl"]) if c["range_max_ns"])
        sh, sc = median(s[0] for s in r["sig"]), median(s[1] for s in r["sig"])
        L.append(f"  {name}: hog/control max {hm / cm:.3f} (per-round {per:.3f}); "
                 f"control lost/1000 {median(values(r, 'late', 'ctl')):.4g}; "
                 f"signal hog {sh:.4g} vs control {sc:.4g}")
    for up, lo in (("L1", "L0"), ("L2", "L1")):
        if rg[up]["void"] or rg[lo]["void"]:
            continue
        _, mu, ml, ratio, p = compare(rg[up], rg[lo], "max", "ctl")
        L.append(f"  control arms {up}/{lo}: median max {_g(ratio)} (p={p:.4g})")
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
            line = f"  {name} injection: hog {inj}/{n}, control {none}/{n}"
            if host:
                rt = sum(1 for lab in ("hog", "control") for i in range(n)
                         if _kv(rd / f"{prefix}{label}-{lab}-r{i}" / "l2" / "kernel.meta").get("kernel_image_preempt") == "PREEMPT_RT")
                ml = sum(1 for lab in ("hog", "control") for i in range(n)
                         if _kv(rd / f"{prefix}{label}-{lab}-r{i}" / "l2" / "header.txt").get("mlock_failed_stages", "0") not in ("0", ""))
                line += f"; PREEMPT_RT {rt}/{2 * n}; mlockall failed {ml}/{2 * n}"
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
        shares.append(f"R4 {hog or 'arms'}/{ctl or 'control'} non-RT "
                      f"{_g(median(s)) if s else '-'} ({len(s)} rounds, duty {DUTY:g}{'' if src else '; no R4 arm'})")
    if shares:
        L.append("  cross-run steal/run-delay, hog-control median: " + "; ".join(shares))
    return L


def _r4_pilot_grid():
    from benchmark.score.single_level import r4 as r4_select
    return tuple(r4_select.GRID)


LEVEL_TABLE_BY_VARIANT = {
    "levels": LEVEL_TABLE,
    "levels2": (("L0", "pc-lb0", "single-level", True),
                ("L1", "pc-lb1", "single-level", False),
                ("L2", "pcn-lb2", "nested", False)),
}


def measurement_code(doc, mark=None):
    try:
        m = re.search(r"<!--\s*" + (mark or MEASUREMENT_CODE_MARK) + r"\s*([0-9a-f]{7,40})\s*-->", Path(doc).read_text())
    except OSError:
        m = None
    if not m:
        print(f"levels_score: refusing -- {doc}: no {mark or MEASUREMENT_CODE_MARK} sha", file=sys.stderr)
        return 3
    sha = m.group(1)
    r = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", sha, "HEAD", "--", *MEASUREMENT_CODE_FILES],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"levels_score: refusing -- differs from {sha} "
              f"({r.stderr.strip() or 'exit ' + str(r.returncode)})", file=sys.stderr)
        return 3
    print(f"levels_score: {sha} matches HEAD")
    return 0


def _arm(mx, late=0, sig=1.0, host=False, frames=20000, dropped=0, p999=None):
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
    hog = hog if isinstance(hog, list) else [hog] * n
    ctl = ctl if isinstance(ctl, list) else [ctl or 100] * n
    hs = hsig if isinstance(hsig, list) else [hsig] * n
    late = kw.get("late", 0)
    late = late if isinstance(late, list) else [late] * n
    return {"topology": topo, "rounds": [
        {f"{label}-hog": _arm(hog[i], late[i], hs[i], host),
         f"{label}-control": _arm(ctl[i], 0, csig, host)} for i in range(n)]}


def _levels(l0=150, l1=200, l2=300, late=(10, 400, 500), **kw):
    return (_batch("pc-ld0", "single-level", True, l0, late=late[0], **kw.get("k0", {})),
            _batch("pc-ld1", "single-level", False, l1, late=late[1], **kw.get("k1", {})),
            _batch("pcn-ld2", "nested", False, l2, late=late[2], **kw.get("k2", {})))


def selftest():
    global DUTY
    import tempfile
    cases = []

    def v(b, **kw):
        return score(b, **kw)[1]

    def text(b, **kw):
        return "\n".join(score(b, **kw)[0])

    ok = v(_levels())
    cases.append(("l0=150 l1=200 l2=300: LAD1-4 HIT",
                  ok == {"LAD1": "HIT", "LAD2": "HIT", "LAD3": "HIT", "LAD4": "HIT"}))
    cases.append(("l0=l1=150: LAD1 MISS", v(_levels(l1=150))["LAD1"] == "MISS"))
    cases.append(("l2=180: LAD3 MISS", v(_levels(l2=180))["LAD3"] == "MISS"))
    lo = list(range(100, 260, 10))
    hi = [x + 1 for x in lo]
    cases.append(("l1=l0+1: LAD1 MISS",
                  v(_levels(l0=lo, l1=hi))["LAD1"] == "MISS"))
    b = _levels(late=(0, 400, 500))
    cases.append(("L0 late=0: LAD2 HIT, ratio undefined",
                  v(b)["LAD2"] == "HIT" and "undefined (L0 median 0)" in text(b)))
    b = _levels(late=(0, 0, 500))
    cases.append(("L0 L1 late=0: LAD2 MISS, guard not informative",
                  v(b)["LAD2"] == "MISS" and "guard LAD2: not informative: L1 hog 0 vs" in text(b)))
    b = _levels(late=(0, 0.5, 500))
    cases.append(("L1 late=0.5: guard LAD2 not informative",
                  "guard LAD2: not informative: L1 hog 0.5" in text(b)))
    b = _levels()
    for r in b[1]["rounds"]:
        r["pc-ld1-control"]["range_lost"] = r["pc-ld1-hog"]["range_lost"]
    cases.append(("L1 control range_lost=hog: guard LAD2 not informative, no guard LAD4",
                  "guard LAD2: not informative: L1 hog 400 vs" in text(b)
                  and "guard LAD4" not in text(b)))
    b = list(_levels())
    for r in b[1]["rounds"][:5]:
        r["pc-ld1-hog"]["range_frames"] = 5000
    cases.append(("L1 range_frames=5000 x5: LAD1 VOID", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    for r in b[2]["rounds"][:5]:
        r["pcn-ld2-control"]["range_covered"] = 0
    vv, t = v(tuple(b)), text(tuple(b))
    cases.append(("L2 range_covered=0 x5: LAD3 VOID, LAD1 HIT, 11/16",
                  vv["LAD3"] == "VOID" and vv["LAD1"] == "HIT" and "covered 11/16" in t))
    b = list(_levels())
    for r in b[0]["rounds"][:4]:
        r["pc-ld0-hog"]["range_covered"] = "-"
    cases.append(("L0 range_covered='-' x4: LAD1 HIT", v(tuple(b))["LAD1"] == "HIT"))
    b = _levels()
    for r in b[1]["rounds"]:
        r["pc-ld1-hog"]["max_ns"] = 1
    cases.append(("L1 max_ns=1: LAD1 HIT", v(b)["LAD1"] == "HIT"))
    cases.append(("window span 0-11300: L2, L1 n=11",
                  "  L2: the injection window 0-11300 (median, 32/32 runs)" in text(_levels())
                  and "  L1: the injection window 0-11300 (median, 22/22 runs)" in
                  text((_levels()[0], dict(_levels()[1], rounds=_levels()[1]["rounds"][:11]), _levels()[2]))))
    cases.append(("hog-control: L1 100, L0 50",
                  "  reported: hog-control median, L1 100 (1 of control); L0 50 (0.5 of control)" in text(_levels())))
    b = list(_levels())
    for r in b[1]["rounds"][:5]:
        r["pc-ld1-hog"]["steal_ticks"] = r["pc-ld1-control"]["steal_ticks"]
    vv = v(tuple(b))
    cases.append(("L1 steal tie x5: LAD1-4 VOID",
                  vv == {"LAD1": "VOID", "LAD2": "VOID", "LAD3": "VOID", "LAD4": "VOID"}))
    b = list(_levels())
    for r in b[1]["rounds"][:4]:
        r["pc-ld1-hog"]["steal_ticks"] = r["pc-ld1-control"]["steal_ticks"]
    cases.append(("L1 steal tie x4: LAD1 HIT", v(tuple(b))["LAD1"] == "HIT"))
    b = list(_levels())
    for r in b[0]["rounds"][:5]:
        r["pc-ld0-hog"]["loop_rundelay_frac"] = r["pc-ld0-control"]["loop_rundelay_frac"]
    vv = v(tuple(b))
    cases.append(("L0 loop_rundelay_frac tie x5: LAD1 LAD2 VOID, LAD3 HIT",
                  (vv["LAD1"], vv["LAD2"], vv["LAD3"]) == ("VOID", "VOID", "HIT")))
    b = list(_levels())
    for r in b[0]["rounds"][:5]:
        r["pc-ld0-hog"]["loop_rundelay_frac"] = "-"
    cases.append(("L0 loop_rundelay_frac='-' x5: LAD1 VOID", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    for r in b[2]["rounds"][:6]:
        del r["pcn-ld2-hog"]
    cases.append(("L2 hog missing x6, n=10: LAD3 VOID", v(tuple(b))["LAD3"] == "VOID"))
    b = list(_levels())
    b[2] = dict(b[2], topology="single-level")
    cases.append(("L2 topology=single-level: LAD3 VOID", v(tuple(b))["LAD3"] == "VOID"))
    b = list(_levels())
    b[0] = dict(b[0], topology="nested")
    cases.append(("L0 topology=nested: LAD1 VOID", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    for r in b[0]["rounds"]:
        del r["pc-ld0-hog"]["level"]
    cases.append(("L0 hog no level: LAD1 VOID", v(tuple(b))["LAD1"] == "VOID"))
    b = list(_levels())
    b[1]["rounds"][0]["pc-ld1-control"]["level"] = "host"
    cases.append(("L1 control level=host: LAD1 VOID", v(tuple(b))["LAD1"] == "VOID"))
    b = _levels(l0=100)
    t = text(b)
    cases.append(("l0=100: LAD1 HIT, guard LAD1 LAD2, NOT LOADED",
                  v(b)["LAD1"] == "HIT" and "guard LAD1: not informative: L0 not loaded" in t
                  and "guard LAD2: not informative: L0 not loaded" in t
                  and "guard LAD3" not in t and "guard load L0: NOT LOADED" in t))
    b = _levels(late=(10, 400, 950))
    t = text(b)
    cases.append(("L2 late=950: guard LAD4, SATURATED, LAD4 HIT",
                  "guard LAD4: not informative: L2 saturated" in t and "guard LAD3" not in t
                  and "guard saturation L2: SATURATED" in t and v(b)["LAD4"] == "HIT"))
    cases.append(("default: no guard LAD", "  guard LAD" not in text(_levels())))
    b = tuple({"topology": x["topology"], "rounds": [{f"one-round-{k}": a for k, a in r.items()} for r in x["rounds"]]}
              for x in _levels())
    cases.append(("prefix one-round-: LAD1 HIT", v(b, prefix="one-round-")["LAD1"] == "HIT"))
    cases.append(("one-round-, no prefix: LAD1 VOID", v(b)["LAD1"] == "VOID"))
    cases.append(("verdicts line: LAD1-4 HIT", "verdicts  LAD1=HIT  LAD2=HIT  LAD3=HIT  LAD4=HIT" in text(_levels())))
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
        cases.append(("runs: L1 steal share 0.4",
                      "L1 0.4 (16 rounds)" in t))
        cases.append(("runs: L1 hog 16/16, control 16/16",
                      "L1 injection: hog 16/16, control 16/16" in t))
        cases.append(("runs: L0 PREEMPT_RT 16/32, mlockall failed 16/32",
                      "PREEMPT_RT 16/32; mlockall failed 16/32" in t))
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
        cases.append(("r4 duty=45 x10: reference 0.5",
                      "non-RT 0.5 (10 rounds, duty 45)" in t))
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
        cases.append(("DUTY=44: pc-r4p-probe-d44 reference 0.2",
                      "R4 pc-r4p-probe-d44/pc-r4p-control non-RT 0.2 (3 rounds" in t44))
        try:
            DUTY = 43
            t43 = text(b, r4=(r4d / "pc-r4.json", r4d / "pc-r4-pilot.json", r4d))
        finally:
            DUTY = keep
        cases.append(("DUTY=43: no R4 arm, no d43",
                      "no R4 arm" in t43 and "pc-r4p-probe-d43" not in t43))
        cases.append(("DUTY=44.5: no R4 arm",
                      "no R4 arm" in t445 and "hog duty 44.5 slot" in t445))
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
    cases.append(("levels2: pc-lb* LAD1 HIT, pc-ld* LAD1 VOID",
                  v2["LAD1"] == "HIT" and v1["LAD1"] == "VOID"))
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "doc.md"
        doc.write_text("no marker\n")
        cases.append(("measurement_code no marker: rc 3", measurement_code(doc) == 3))
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        doc.write_text(f"<!-- {MEASUREMENT_CODE_MARK} {head} -->\n")
        dirty = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", *MEASUREMENT_CODE_FILES]).returncode
        cases.append(("measurement_code HEAD, clean: rc 0", dirty != 0 or measurement_code(doc) == 0))
    def safe(f):
        try:
            return f()
        except Exception as e:  # noqa: BLE001
            return f"raised {type(e).__name__}"
    cases.append(("L0 no rounds key: LAD1 VOID",
                  safe(lambda: v(({"topology": "single-level"},) + _levels()[1:])["LAD1"]) == "VOID"))
    cases.append(("range_max_ns='-': LAD1 VOID",
                  safe(lambda: v(tuple(dict(x, rounds=[{k: dict(a, range_max_ns="-") for k, a in r.items()} for r in x["rounds"]])
                                       for x in _levels()))["LAD1"]) == "VOID"))
    bad = [c for c, good in cases if not good]
    for c, good in cases:
        print(("ok   " if good else "FAIL ") + c)
    print(f"selftest {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0


def main(argv):
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
                print(USAGE, file=sys.stderr)
                return 2
            opts[o] = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
    prefix = opts.get("--prefix", "")
    if opts.get("--variant", "levels") not in LEVEL_TABLE_BY_VARIANT:
        print(USAGE, file=sys.stderr)
        return 2
    LEVEL_TABLE = LEVEL_TABLE_BY_VARIANT[opts.get("--variant", "levels")]
    try:
        DUTY = float(opts["--duty"])
    except (KeyError, ValueError):
        print("levels_score: need --duty\n" + USAGE, file=sys.stderr)
        return 2
    if "--range" in opts:
        m = re.fullmatch(r"(\d+)-(\d+)", opts["--range"])
        if not prefix or not m or int(m.group(1)) >= int(m.group(2)):
            print("levels_score: --range needs --prefix, A < B", file=sys.stderr)
            return 2
        RANGE = (int(m.group(1)), int(m.group(2)))
        RANGE_FRAMES = RANGE[1] - RANGE[0]
    if len(argv) != 3 or any(a.startswith("--") for a in argv):
        print(USAGE, file=sys.stderr)
        return 2
    batches = []
    for p in argv:
        try:
            batches.append(json.loads(Path(p).read_text()))
        except (OSError, ValueError) as e:
            print(f"{p}: {e}")
            return 1
    runs = tuple(str(Path(p).resolve().parent) for p in argv)
    lines, verdicts, void = score(batches, prefix, runs, (R4_JSON, R4_PILOT_JSON, R4_RUNS))
    print("\n".join(lines))
    return 4 if void else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
