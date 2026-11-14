#!/usr/bin/env python3
import argparse
import json
import struct
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
EXP = ROOT / "results" / "experiments"
FROZEN = ROOT / "results" / "earlier_phase" / "frozen" / "adapt-modes-frozen.json"
QUALITY = ROOT / "results" / "earlier_phase" / "frozen" / "quality-frozen.json"
HDR = struct.Struct("<7Q4I")


def modes_of(telemetry):
    p = Path(telemetry)
    if not p.exists():
        return []
    raw = p.read_bytes()
    if len(raw) % HDR.size:
        return []
    return [HDR.unpack_from(raw, i)[10] for i in range(0, len(raw), HDR.size)]


def collect(paired_path, arm="adaptive"):
    rounds = json.loads((ROOT / paired_path).read_text())["rounds"]
    hist, used, missing = Counter(), 0, []
    for i in range(len(rounds)):
        d = EXP / f"{arm}-r{i}" / "l2" / "telemetry.bin"
        m = modes_of(d)
        if not m:
            missing.append(i)
            continue
        hist.update(m)
        used += 1
    return hist, used, missing


def summarise(hist, arm="adaptive"):
    if not hist:
        raise SystemExit(f"adapt_modes: no frames {arm}")
    q = json.loads(QUALITY.read_text()) if QUALITY.exists() else {}
    curve, full = q.get("relerr_curve"), q.get("modes")
    total = sum(hist.values())

    if not curve or full is None:
        raise SystemExit("adapt_modes: no curve")
    outside = [m for m in hist if not 0 <= m <= full]
    if outside:
        raise SystemExit(f"adapt_modes: modes {sorted(outside)[:5]} outside 0..{full}")

    mean_err = sum(n * curve[m] for m, n in hist.items()) / total
    ordered = sorted(hist)
    cum, pcts = 0, {}
    for m in ordered:
        cum += hist[m]
        for p in (0.01, 0.5, 0.99):
            key = f"p{int(p*100)}"
            if key not in pcts and cum >= p * total:
                pcts[key] = m
    return {
        "arm": arm,
        "frames": total,
        "full_modes": full,
        "distinct_stops": len(hist),
        "full_quality_frames": hist.get(full, 0),
        "full_quality_frac": hist.get(full, 0) / total,
        "modes_min": ordered[0],
        "modes_p01": pcts.get("p1"),
        "modes_p50": pcts.get("p50"),
        "modes_p99": pcts.get("p99"),
        "mean_relerr": mean_err,
        "worst_frame_relerr": max(curve[m] for m in hist),
        "histogram": {str(m): hist[m] for m in ordered},
    }


def selftest():
    global QUALITY
    if not QUALITY.exists():
        tmp = tempfile.TemporaryDirectory()
        QUALITY = Path(tmp.name) / "quality.json"
        QUALITY.write_text(json.dumps({"modes": 480, "relerr_curve": [(480 - m) / 480 for m in range(481)]}))
    ok = True

    def expect(name, fn):
        nonlocal ok
        try:
            fn()
        except SystemExit as e:
            print(f"  refused {name}: {str(e)[:90]}")
            return
        print(f"  ACCEPTED {name}")
        ok = False

    expect("Counter() empty: refused", lambda: summarise(Counter()))
    q = json.loads(QUALITY.read_text()) if QUALITY.exists() else {"modes": 480}
    full = q["modes"]
    expect("modes=full+1: refused",
           lambda: summarise(Counter({full + 1: 10})))
    expect("modes=-1: refused", lambda: summarise(Counter({-1: 10})))

    good = summarise(Counter({full: 60, full - 16: 30, full // 2: 10}))
    if not (0 < good["mean_relerr"] < 1 and good["full_quality_frac"] == 0.6):
        print(f"  BROKEN: {good['mean_relerr']} / "
              f"{good['full_quality_frac']}")
        ok = False
    else:
        print(f"  ok mean_relerr={good['mean_relerr']:.5f} full={good['full_quality_frac']:.2f}")

    print("selftest: " + ("ok" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paired", default="results/earlier_phase/paired/paired-adapt.json")
    ap.add_argument("--arm", default="adaptive")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    from benchmark import lib
    lib.need_input(a.paired, "--paired", "adapt_modes")

    hist, used, missing = collect(a.paired, a.arm)
    s = summarise(hist, a.arm)
    s["rounds_used"] = used
    s["rounds_missing"] = missing
    print(f"{a.arm}: frames={s['frames']} rounds={used}"
          + (f" no_telemetry={missing}" if missing else ""))
    print(f"  stops={s['distinct_stops']} min={s['modes_min']} p1={s['modes_p01']} "
          f"p50={s['modes_p50']} p99={s['modes_p99']} full={s['full_modes']}")
    print(f"  full_quality_frac={s['full_quality_frac']:.3f}")
    print(f"  mean_relerr={s['mean_relerr']:.6f} worst={s['worst_frame_relerr']:.6f}")
    if a.freeze:
        from benchmark import lib
        lib.refuse_unpublishable_experiments()
        FROZEN.write_text(json.dumps(s, indent=2) + "\n")
        print(f"froze {FROZEN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
