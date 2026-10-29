#!/usr/bin/env python3
"""Compare the interference L1 saw arrive at the guest's cores with what the guest saw"""

import argparse
import json
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from benchmark.analysis.quality import frame_budget   # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
QUALITY = ROOT / "results" / "earlier_phase" / "frozen" / "quality-frozen.json"

HDR = struct.Struct("<7Q4I")


def read_frames(path):
    """read the telemetry records of a run"""
    raw = Path(path).read_bytes()
    return [HDR.unpack_from(raw, i) for i in range(0, len(raw), HDR.size)]


def read_header(path):
    """parse the key=value lines of a header file"""
    out = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


REMOTE_TLB_REASONS = ("reason=1", "reason=4")


def is_remote(kind, detail):
    """return True when an event is work another core sent to this one"""
    if kind.startswith("ipi"):
        return True
    if kind == "tlb":
        return any(r in detail for r in REMOTE_TLB_REASONS)
    return False


def read_events(path, targets, lo, hi):
    """count the L1 events aimed at the target cores inside the window"""
    hit, elsewhere, by = 0, 0, Counter()
    local = 0
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.startswith("EV "):
            continue
        p = line.split(None, 5)
        if len(p) < 6:
            continue
        try:
            t, kind, target = int(p[1]), p[2], int(p[4])
        except ValueError:
            continue
        if lo and not (lo <= t <= hi):
            continue
        if not (target in targets or target == -1):
            elsewhere += 1
            continue
        if is_remote(kind, p[5]):
            hit += 1
            by[f"{kind}:{p[5]}"] += 1
        else:
            local += 1
    return hit, elsewhere, by, local


def delivered_quality(frames, modes_full):
    """look up the delivered quality of a run from the frozen error curve"""
    if not frames:
        return {}
    done = [f[10] for f in frames]
    if not any(done):
        return {}
    if not QUALITY.exists():
        return {"quality_note": "no-frozen-curve"}
    q = json.loads(QUALITY.read_text())
    curve = q.get("relerr_curve")
    if not curve or q.get("modes") != modes_full:
        return {"quality_note": f"frozen-curve-is-for-{q.get('modes')}-modes"}
    errs = [curve[m] if 0 <= m < len(curve) else None for m in done]
    errs = [e for e in errs if e is not None]
    if not errs:
        return {"quality_note": "modes-outside-curve"}
    full = sum(1 for m in done if m >= modes_full)
    return {
        "mean_relerr": sum(errs) / len(errs),
        "full_quality_frac": full / len(done),
        "modes_min": min(done),
        "modes_p50": sorted(done)[len(done) // 2],
    }


def range_metrics(frames, a, b, period, hdr, meta, clock):
    """return the frames lost, worst latency and window coverage over frames [a, b)"""
    recs = {f[0]: f for f in frames}
    lost = sum(1 for s in range(a, b)
               if s not in recs or (recs[s][4] and recs[s][1] and recs[s][4] - recs[s][1] > period))
    lats = [recs[s][4] - recs[s][1] for s in range(a, b)
            if s in recs and recs[s][4] and recs[s][1]]
    out = {"range_frames": b - a, "range_lost": lost, "range_max_ns": max(lats) if lats else "-",
           "range_covered": "-", "window_seq": "-"}
    try:
        off_loop = int(hdr["clock_pair_real_ns"]) - int(hdr["clock_pair_mono_ns"])
        off_host = int(clock["host_real_minus_mono_ns"])
        ws = int(meta["INJECT start_ns"]) + off_host
        we = int(meta["INJECT end_ns"]) + off_host
    except (KeyError, ValueError):
        return out
    t = {sq: f[5] + off_loop for sq, f in recs.items() if f[5]}
    inside = sorted(sq for sq, v in t.items() if ws <= v <= we)
    if inside:
        out["window_seq"] = f"{inside[0]}-{inside[-1]}"
    rng = sorted(sq for sq in t if a <= sq < b)
    if rng:
        out["range_covered"] = 1 if t[rng[0]] >= ws and t[rng[-1]] <= we else 0
    return out


def pct(a, b):
    """format a over b as a percentage"""
    return f"{100.0 * a / b:.2f}%" if b else "-"


def main():
    """print the comparison and the FACT line of a run"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--pin", default="")
    ap.add_argument("--period-ns", type=int, default=1000000)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--late-range", default="",
                    help="A-B: frame range to report losses over")
    args = ap.parse_args()

    out = Path(args.outdir)
    targets = {int(c) for c in args.pin.split(",") if c.strip()}

    meta = read_header(out / "inject.meta") if (out / "inject.meta").exists() else {}
    lo = int(meta.get("INJECT start_ns", 0) or 0)
    hi = int(meta.get("INJECT end_ns", 0) or 0)

    hdr = read_header(out / "l2" / "header.txt")
    frames = read_frames(out / "l2" / "telemetry.bin")

    lat = [f[4] - f[1] for f in frames if f[4] and f[1]]
    lat_sorted = sorted(lat)
    deadline = args.period_ns
    late = [v for v in lat if v > deadline]

    hit, elsewhere, by, local = read_events(out / "l1-events.txt", targets, lo, hi)

    steal_b = int(hdr.get("steal_before", 0) or 0)
    steal_a = int(hdr.get("steal_after", 0) or 0)
    steal_ticks = steal_a - steal_b

    def at(p):
        """return the latency at quantile p"""
        return lat_sorted[min(int(p * len(lat_sorted)), len(lat_sorted) - 1)] if lat_sorted else 0

    print(f"=== {out.name} ===")
    print(f"injection        {meta.get('INJECT mode', '?')} "
          f"rate_hz={meta.get('INJECT rate_hz', '-')} window={(hi - lo) / 1e9:.2f}s")
    print()
    print("-- L1 --")
    print(f"  interference events aimed at {sorted(targets) or 'any'}   {hit}")
    print(f"  events aimed elsewhere                     {elsewhere}")
    print(f"  local flushes                              {local}")
    for k, v in by.most_common(args.top):
        print(f"    {v:8}  {k}")
    print()
    print("-- L2 latency --")
    print(f"  frames recorded         {len(frames)}")
    print(f"  latency p50/p99/p99.9   {at(.5)/1000:.1f} / {at(.99)/1000:.1f} / "
          f"{at(.999)/1000:.1f} us")
    print(f"  max                     {(lat_sorted[-1] if lat_sorted else 0)/1000:.1f} us")
    print(f"  over deadline           {len(late)}  ({pct(len(late), len(lat))})")
    print()
    print("-- L2 visibility --")
    print(f"  steal ticks accrued     {steal_ticks}")
    print(f"  events it can name      0 of {hit}")
    comp = sorted(f[3] - f[2] for f in frames if f[3] and f[2])

    def cat(s, p):
        """return the value of a sorted list at quantile p"""
        return s[min(int(p * len(s)), len(s) - 1)] if s else 0

    dropped = (frames[-1][0] + 1) - len(frames) if frames else 0
    modes_full = int(hdr.get("recon_modes", 0) or 0)
    q = delivered_quality(frames, modes_full)
    floor_modes = int(hdr.get("block", 0) or 16)
    exceeded_budget_frac = frame_budget.exceeded_fraction(frames, floor_modes, args.period_ns)

    in_loop = {k: hdr[k] for k in hdr if k.startswith("inloop_")}
    if q or in_loop:
        print()
        print("-- L2 quality and in-loop --")
        for k, v in sorted(q.items()):
            print(f"  {k:22}  {v}")
        for k, v in sorted(in_loop.items()):
            print(f"  {k:22}  {v}")

    fact = {
        "hit": hit, "late": len(late), "frames": len(frames), "steal_ticks": steal_ticks,
        "p999_ns": at(.999), "max_ns": lat_sorted[-1] if lat_sorted else 0,
        "over_deadline": len(late),
        "over_deadline_frac": f"{len(late) / len(lat):.6f}" if lat else "0",
        "compute_p50_ns": cat(comp, .5), "compute_p99_ns": cat(comp, .99),
        "dropped": dropped,
        "exceeded_budget_frac": (f"{exceeded_budget_frac:.6f}"
                                  if exceeded_budget_frac is not None else "-"),
    }
    fact.update({k: (f"{v:.6f}" if isinstance(v, float) else v) for k, v in q.items()})
    fact.update({k: v for k, v in in_loop.items()})
    if hdr.get("level") == "host":
        l0 = read_header(out / "l0.meta") if (out / "l0.meta").exists() else {}
        fact["level"] = "host"
        fact["loop_rundelay_frac"] = l0.get("loop_rundelay_frac", "-") or "-"
    if args.late_range:
        a, _, b = args.late_range.partition("-")
        if not (a.isdigit() and b.isdigit() and int(a) < int(b)):
            raise SystemExit(f"compare: --late-range must be A-B with A < B, not {args.late_range!r}")
        clock = read_header(out / "clock.meta") if (out / "clock.meta").exists() else {}
        fact.update(range_metrics(frames, int(a), int(b), deadline, hdr, meta, clock))
    print("  FACT " + " ".join(f"{k}={v}" for k, v in fact.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
