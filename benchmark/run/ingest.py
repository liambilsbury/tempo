#!/usr/bin/env python3
"""Append completed experiment directories to the fact table by topology"""

import argparse
import csv
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmark import lib  # noqa: E402

HDR = struct.Struct("<7Q4I")
EXPERIMENTS = lib.ROOT / "results" / "experiments"


def kv(path):
    """parse the key=value tokens of a meta file"""
    out = {}
    if not Path(path).exists():
        return out
    for line in Path(path).read_text(errors="replace").splitlines():
        for tok in line.replace("INJECT ", "").split():
            if "=" in tok:
                k, _, v = tok.partition("=")
                out[k.strip()] = v.strip()
    return out


def guest_config(d):
    """read the arm the guest ran from its startup console line"""
    con = Path(d) / "l2" / "console.log"
    out = {}
    if not con.exists():
        return out
    for line in con.read_text(errors="replace").splitlines():
        if "TEMPO-L2 recon=" in line or "ARTC-L2 recon=" in line:
            for tok in line.split():
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    out[k] = v
            break
    return out


def frames_of(path):
    """read the telemetry records of a run"""
    if not Path(path).exists():
        return []
    raw = Path(path).read_bytes()
    return [HDR.unpack_from(raw, i) for i in range(0, len(raw), HDR.size)]


def existing_keys(table):
    """return the (run_id, config_id) pairs already in a table"""
    p = lib.RESULTS / table
    if not p.exists():
        return set()
    with p.open() as f:
        return {(r["run_id"], r["config_id"]) for r in csv.DictReader(f, delimiter="\t")}


def build_row(d):
    """build the fact table row and verdict of a run directory"""
    meta = kv(d / "inject.meta")
    hdr = kv(d / "l2" / "header.txt")
    frames = frames_of(d / "l2" / "telemetry.bin")
    if not frames:
        return None

    lat = [f[4] - f[1] for f in frames if f[4] and f[1]]
    wake = [f[2] - f[1] for f in frames if f[2] and f[1]]
    comp = [f[3] - f[2] for f in frames if f[3] and f[2]]
    post = [f[4] - f[3] for f in frames if f[4] and f[3]]
    period = int(hdr.get("period_ns", 1000000) or 1000000)
    over = sum(1 for v in lat if v > period)

    row = lib.blank_row()
    row.update(lib.machine_config())
    row.update(lib.percentiles(lat, "lat"))
    row.update(lib.jitter(lat))
    for name, vals in (("wake", wake), ("compute", comp), ("post", post)):
        p = lib.percentiles(vals, name)
        for k in ("p50_ns", "p99_ns", "max_ns"):
            row[f"{name}_{k}"] = p[f"{name}_{k}"]

    dropped = (frames[-1][0] + 1) - len(frames)
    reconciles = len(frames) + dropped == int(hdr.get("frames", 0) or 0)

    modes_done = [f[10] for f in frames]
    gc = guest_config(d)
    full = int(hdr.get("recon_modes", gc.get("modes", 0)) or 0)
    adapt_cols = {
        "recon": hdr.get("recon", gc.get("recon", "-")),
        "modes": hdr.get("recon_modes", gc.get("modes", "-")),
        "tier": hdr.get("tier", gc.get("tier", "-")),
        "adapt": hdr.get("adapt", gc.get("adapt", "-")),
        "block": hdr.get("block", gc.get("block", "-")),
        "inloop_gap_sum_ns": hdr.get("inloop_gap_sum_ns", "-"),
        "inloop_observed_ns": hdr.get("inloop_observed_ns", "-"),
        "inloop_stopped_frames": hdr.get("inloop_stopped_frames", "-"),
    }
    if any(modes_done):
        adapt_cols["modes_done_p50"] = sorted(modes_done)[len(modes_done) // 2]
        adapt_cols["full_quality_frac"] = (
            f"{sum(1 for m in modes_done if m >= full) / len(modes_done):.6f}" if full else "-")
        qf = lib.RESULTS / "earlier_phase" / "frozen" / "quality-frozen.json"
        if qf.exists():
            import json as _json
            curve = _json.loads(qf.read_text()).get("relerr_curve")
            if curve and full and len(curve) > full and all(0 <= m <= full for m in modes_done):
                adapt_cols["mean_relerr"] = (
                    f"{sum(curve[m] for m in modes_done) / len(modes_done):.6f}")

    row.update({
        "run_id": d.name,
        "repo_sha": lib.sha(),
        "isio_sha": lib.imagestreamio_sha(),
        "config_id": d.name.rsplit("-r", 1)[0],
        "factor": meta.get("mode", "-"),
        "factor_value": meta.get("duty", meta.get("rate_hz", "-")),
        "kernel_release": hdr.get("kernel", "-"),
        "clocksource": hdr.get("clocksource", "-"),
        "policy": hdr.get("sched", "-"),
        "period_ns": period,
        "deadline_ns": period,
        "frames_requested": hdr.get("frames", "-"),
        "frames_done": len(frames),
        "records": len(frames),
        "over_deadline": over,
        "over_deadline_frac": f"{over/len(lat):.3e}" if lat else "-",
        "neighbour": meta.get("mode", "none"),
        "notes": (f"injected_cpu_ns={meta.get('cpu_consumed_ns','-')} "
                  f"guest_cpu_window_ns={meta.get('watched_cpu_window_ns','-')} "
                  f"steal_ticks={hdr.get('det_steal_ticks','-')} "
                  f"det_sum_late_ns={hdr.get('det_sum_late_ns','-')} "
                  f"dropped={dropped} "
                  f"slot_us={meta.get('slot_us','-')}"),
    })
    row.update(adapt_cols)

    predates_fix = meta.get("mode") is not None and "watched_cpu_window_ns" not in meta

    hm = kv(d / "host.meta")
    exclusive = 1 if hm.get("exclusive") == "1" and hm.get("exclusive_end") == "1" else 0
    measurable = lib.measurable_from_row(row)
    row["host_exclusive"] = exclusive
    row["host_measurable"] = measurable["measurable"]
    row["topology"] = lib.experiment_topology(d)

    if predates_fix:
        row["verdict"] = "RED"
        row["verdict_reason"] = "void_instrument_predates_window_fix"
    elif not reconciles:
        row["verdict"], row["verdict_reason"] = "RED", "records_plus_drops_mismatch"
    elif not frames:
        row["verdict"], row["verdict_reason"] = "RED", "zero_result"
    elif not measurable["measurable"]:
        row["verdict"], row["verdict_reason"] = "RED", "host_not_measurable"
    elif not exclusive:
        row["verdict"], row["verdict_reason"] = (
            "RED", "not_exclusive" if hm else "no_host_assertion")
    else:
        row["verdict"], row["verdict_reason"] = "GREEN", lib.VERDICT_OK
    return row


def main():
    """append or list the new rows of every experiment directory"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--table", default="results.tsv")
    args = ap.parse_args()

    if not EXPERIMENTS.exists():
        print("no experiments to ingest")
        return 0
    have = {}
    rows, skipped = [], 0
    for d in sorted(EXPERIMENTS.iterdir()):
        if not d.is_dir():
            continue
        r = build_row(d)
        if r is None:
            continue
        table = lib.table_for(r["topology"], args.table)
        if table not in have:
            have[table] = existing_keys(table)
        if (r["run_id"], r["config_id"]) in have[table]:
            skipped += 1
            continue
        rows.append((table, r))

    if args.list:
        for table, r in rows:
            print(f"{r['run_id']:22} {r['config_id']:12} {r['verdict']:6} "
                  f"n={r['records']} p999={r['lat_p999_ns']} -> {table}")
        print(f"{len(rows)} to ingest, {skipped} already present")
        return 0

    if not rows:
        print(f"nothing new, {skipped} already present")
        return 0
    for table in sorted({t for t, _ in rows}):
        mine = [r for t, r in rows if t == table]
        out = lib.append_rows(mine, table)
        green = sum(1 for r in mine if r["verdict"] == "GREEN")
        print(f"ingested {len(mine)} rows ({green} GREEN, {len(mine)-green} RED) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
