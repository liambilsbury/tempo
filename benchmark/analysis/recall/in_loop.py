#!/usr/bin/env python3
"""Score the in-loop detector against L1's exit time

Usage: in_loop.py [--paired F] [--control LABEL] [--freeze] [--selftest]"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median      # noqa: E402
from benchmark.analysis.exits.exits import parse, per_vcpu_outside_ns, IDLE_REASONS, _in_window   # noqa: E402
from benchmark.analysis.recall.gap_detector import inject_meta               # noqa: E402

EXP = ROOT / "results" / "experiments"
FROZEN = ROOT / "results" / "earlier_phase" / "frozen" / "inloop-frozen.json"
COMPUTE_VCPU = 1


def header_of(outdir):
    """parse the guest header of a run"""
    f = Path(outdir) / "l2" / "header.txt"
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text(errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip()
            out[k.strip()] = int(v) if v.isdigit() else v
    return out


def one(outdir, vcpu=COMPUTE_VCPU):
    """return a run's fractions, or None when a needed input is missing"""
    h, m = header_of(outdir), inject_meta(outdir)
    if not h or not m:
        return None
    gap = h.get("inloop_gap_sum_ns")
    obs = h.get("inloop_observed_ns")
    if not gap or not obs:
        return None

    ex = parse(Path(outdir) / "exits.txt")
    if not ex.get("exit_ns"):
        return None
    t0, t1 = m.get("watched_window_start_ns", 0), m.get("watched_window_end_ns", 0)
    outside_all = per_vcpu_outside_ns(ex, t0, t1).get(vcpu, 0)
    outside = sum(ns for (b, v, r), ns in ex["exit_ns"].items()
                  if v == vcpu and r not in IDLE_REASONS and _in_window(b, t0, t1))
    window = m.get("watched_wall_ns", 0)
    if not outside or not window:
        return None

    loop_ns = int(h.get("frames", 0) or 0) * int(h.get("period_ns", 0) or 0)
    overlap = min(1.0, window / loop_ns) if loop_ns else 1.0

    return {
        "outside_frac": outside / window,
        "outside_all_frac": outside_all / window,
        "detector_frac": gap / obs,
        "overlap": overlap,
        "loop_ns": loop_ns,
        "observed_ns": obs,
        "window_ns": window,
        "duty": obs / window,
        "gap_count": h.get("inloop_gap_count", 0),
        "gap_max_ns": h.get("inloop_gap_max_ns", 0),
    }


def gather(paired_path, vcpu=COMPUTE_VCPU):
    """collect the per-round fractions of each class in a paired file"""
    rounds = json.loads((ROOT / paired_path).read_text())["rounds"]
    labels = sorted({k for r in rounds for k in r})
    out = {}
    for label in labels:
        rows = []
        for i in range(len(rounds)):
            r = one(EXP / f"{label}-r{i}", vcpu)
            if r:
                r["round"] = i
                rows.append(r)
        if rows:
            out[label] = rows
    return out


def paired_recall(rows, ctl):
    """return the recall of each round with the control round subtracted"""
    by_round = {r["round"]: r for r in ctl}
    out = []
    for r in rows:
        c = by_round.get(r["round"])
        if not c:
            continue
        d_true = r["outside_frac"] - c["outside_frac"]
        if d_true <= 0:
            continue
        ov = r.get("overlap", 1.0)
        if ov <= 0:
            continue
        out.append(((r["detector_frac"] - c["detector_frac"]) / ov) / d_true)
    return out


def verdict(paired_path="results/earlier_phase/paired/paired-inloop.json", frozen=None, control="control"):
    """return the frozen verdict, or compute the per-class recalls"""
    src = frozen or FROZEN
    if frozen is not False and Path(src).exists():
        return json.loads(Path(src).read_text())
    data = gather(paired_path)
    ctl = data.get(control, [])
    out = {"classes": {}, "duty": None}
    duties = [r["duty"] for rows in data.values() for r in rows]
    out["duty"] = median(duties) if duties else None
    ovs = [r["overlap"] for rows in data.values() for r in rows]
    out["overlap"] = median(ovs) if ovs else None
    every = []
    for label, rows in data.items():
        if label == control:
            continue
        rec = paired_recall(rows, ctl)
        if not rec:
            continue
        every += rec
        out["classes"][label] = {
            "n": len(rec), "median": median(rec), "min": min(rec), "max": max(rec),
            "floor": median([c["detector_frac"] for c in ctl]) if ctl else None,
            "bias": median(rec) - 1,
            "coverage": median([r["duty"] for r in rows]) if rows else None,
        }
    out["detector"] = median(every) if every else None
    out["n"] = len(every)
    return out


def report(paired_path, control="control"):
    """print the per-class table and return the verdict"""
    data = gather(paired_path)
    ctl = data.get(control, [])
    hdr = (f"{'class':10} {'n':>3} {'outside %':>10} {'+idle %':>8} {'detector %':>11} "
           f"{'duty':>7} {'overlap':>8} {'recall':>20}")
    print(hdr)
    print("-" * len(hdr))
    for label, rows in data.items():
        rec = paired_recall(rows, ctl) if label != control else []
        r = (f"{median(rec):.2f} [{min(rec):.2f}-{max(rec):.2f}]" if rec else "-")
        print(f"{label:10} {len(rows):>3} "
              f"{100*median([x['outside_frac'] for x in rows]):>10.1f} "
              f"{100*median([x['outside_all_frac'] for x in rows]):>8.1f} "
              f"{100*median([x['detector_frac'] for x in rows]):>11.1f} "
              f"{median([x['duty'] for x in rows]):>7.3f} "
              f"{median([x['overlap'] for x in rows]):>8.3f} {r:>20}")
    v = verdict(paired_path, frozen=False, control=control)
    print(f"\nin-loop detector recall, control subtracted: "
          f"{v['detector'] if v['detector'] is None else round(v['detector'], 4)} over {v['n']} "
          f"condition-rounds")
    return v


def selftest():
    """check that runs missing an input are dropped"""
    import tempfile
    ok = True
    d = Path(tempfile.mkdtemp())
    (d / "l2").mkdir()

    cases = {
        "a run with no header at all": ("", ""),
        "a run whose loop carried no detector": (
            "steal_before=1\nsteal_after=2\nbytes=0\n", "INJECT watched_wall_ns=1000\n"),
        "a run with a detector but no injector metadata": (
            "inloop_gap_sum_ns=100\ninloop_observed_ns=1000\n", ""),
    }
    for name, (hdr, meta) in cases.items():
        (d / "l2" / "header.txt").write_text(hdr)
        (d / "inject.meta").write_text(meta)
        if one(d) is not None:
            print(f"  ACCEPTED {name} -- the check does not fire")
            ok = False
        else:
            print(f"  dropped {name}")

    (d / "l2" / "header.txt").write_text("inloop_gap_sum_ns=100\ninloop_observed_ns=1000\n")
    (d / "inject.meta").write_text("INJECT watched_wall_ns=1000 watched_window_start_ns=0 "
                                   "watched_window_end_ns=1000\n")
    (d / "exits.txt").write_text("")
    if one(d) is not None:
        print("  ACCEPTED a run with no exit counter")
        ok = False
    else:
        print("  dropped a run with no exit counter")

    print("selftest: " + ("every refusal fired" if ok else "a refusal did not fire"))
    return 0 if ok else 1


def main():
    """report, freeze or run the selftest"""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paired", default="results/earlier_phase/paired/paired-inloop.json")
    ap.add_argument("--control", default="control",
                    help="label of the no-injection arm")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(a.paired, "--paired", "in_loop")
    v = report(a.paired, control=a.control)
    if a.freeze:
        from benchmark import lib
        lib.refuse_unpublishable_experiments()
        FROZEN.write_text(json.dumps(v, indent=2) + "\n")
        print(f"froze {FROZEN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
