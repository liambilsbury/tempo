#!/usr/bin/env python3
"""Cut per-class VM exit counts to the injection window"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

BUCKET_NS = 100_000_000

REASONS = {
    0: "exception_nmi", 1: "external_interrupt", 2: "triple_fault", 3: "init_signal",
    6: "other_smi", 7: "interrupt_window", 9: "task_switch", 10: "cpuid",
    12: "hlt", 13: "invd", 14: "invlpg", 15: "rdpmc", 16: "rdtsc", 17: "rsm",
    18: "vmcall", 28: "cr_access", 29: "dr_access", 30: "io_instruction",
    31: "msr_read", 32: "msr_write", 36: "mwait", 39: "monitor", 40: "pause",
    43: "tpr_below_threshold", 44: "apic_access", 45: "eoi_induced", 46: "gdtr_idtr",
    47: "ldtr_tr", 48: "ept_violation", 49: "ept_misconfig", 51: "rdtscp",
    52: "preemption_timer", 54: "wbinvd", 55: "xsetbv", 56: "apic_write", 58: "invpcid",
    68: "tpause", 74: "bus_lock",
}

IDLE_REASONS = {12, 36, 68}

ISA_VMX, ISA_SVM = 1, 2

SVM_QUAL = 1 << 16
SVM_ONLY = 1 << 32

SVM_TO_CLASS = {
    **{c: 28 for c in range(0x00, 0x20)},
    **{c: 29 for c in range(0x20, 0x40)},
    **{c: 0 for c in range(0x40, 0x60)},
    **{c: 28 for c in range(0x90, 0xa0)},
    0x060: 1,
    0x061: 0,
    0x062: 6,
    0x063: 3,
    0x064: 7,
    0x065: 28,
    0x066: 46, 0x067: 46, 0x06a: 46, 0x06b: 46,
    0x068: 47, 0x069: 47, 0x06c: 47, 0x06d: 47,
    0x06e: 16,
    0x06f: 15,
    0x072: 10,
    0x073: 17,
    0x076: 13,
    0x077: 40,
    0x078: 12,
    0x079: 14,
    0x07b: 30,
    0x07c: 31,
    0x07c | SVM_QUAL: 32,
    0x07d: 9,
    0x07f: 2,
    0x081: 18,
    0x087: 51,
    0x089: 54,
    0x08a: 39,
    0x08b: 36,
    0x08c: 36,
    0x08d: 55,
    0x0a2: 58,
    0x0a5: 74,
    0x0a6: 12,
    0x400: 48,
    0x400 | SVM_QUAL: 49,
}

SVM_NAMES = {
    0x070: "svm_pushf", 0x071: "svm_popf", 0x074: "svm_iret", 0x075: "svm_swint",
    0x07a: "svm_invlpga", 0x07e: "svm_ferr_freeze", 0x080: "svm_vmrun", 0x082: "svm_vmload",
    0x083: "svm_vmsave", 0x084: "svm_stgi", 0x085: "svm_clgi", 0x086: "svm_skinit",
    0x088: "svm_icebp", 0x08e: "svm_rdpru", 0x08f: "svm_efer_write_trap",
    0x401: "svm_avic_incomplete_ipi",
    0x402: "svm_avic_unaccelerated_access",
    0x403: "svm_vmgexit",
    0xf0000000: "svm_sw",
    0xffffffff: "svm_err",
}


def svm_class(code):
    """return the class id an SVM exit code is counted under"""
    return SVM_TO_CLASS.get(code, SVM_ONLY | code)


def decode_as(isas):
    """return vmx, svm or None for the set of isa values a run saw"""
    isas = set(isas)
    if isas <= {ISA_VMX}:
        return "vmx"
    if isas == {ISA_SVM}:
        return "svm"
    return None

MAP_LINE = re.compile(r"^@(\w+)\[([^\]]*)\]:\s+(\d+)\s*$")
SCALAR_LINE = re.compile(r"^@(\w+):\s+(\d+)\s*$")


def reason_name(r):
    """return the name of a class id"""
    if r & SVM_ONLY:
        code = r & ~SVM_ONLY
        return SVM_NAMES.get(code, f"svm_0x{code:x}")
    return REASONS.get(r, f"reason_{r}")


def parse(path):
    """parse a bpftrace map dump of exit counts and times"""
    out = {"exits": {}, "exit_ns": {}, "isa": {}, "foreign": {},
           "attach_ns": 0, "end_ns": 0, "attached": False}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(errors="replace").splitlines():
        if line.startswith("Attaching"):
            out["attached"] = True
            continue
        m = SCALAR_LINE.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
            continue
        m = MAP_LINE.match(line)
        if not m:
            continue
        name, key, val = m.group(1), m.group(2), int(m.group(3))
        parts = [k.strip() for k in key.split(",")]
        if name in ("exits", "exit_ns") and len(parts) == 3:
            out[name][(int(parts[0]), int(parts[1]), int(parts[2]))] = val
        elif name in ("isa", "foreign") and len(parts) == 1:
            out[name][int(parts[0])] = val

    out["decoded_as"] = decode_as(out["isa"])
    if out["decoded_as"] == "svm":
        for name in ("exits", "exit_ns"):
            dec = {}
            for (b, v, r), n in out[name].items():
                k = (b, v, svm_class(r))
                dec[k] = dec.get(k, 0) + n
            out[name] = dec
    return out


def _in_window(bucket, t0, t1):
    """check whether a bucket's midpoint is inside the window"""
    if not t0 or not t1:
        return True
    mid = bucket * BUCKET_NS + BUCKET_NS // 2
    return t0 <= mid < t1


def summarise(ex, t0=0, t1=0):
    """sum exits per reason across vCPUs within [t0, t1)"""
    by_reason = {}
    vcpus = set()
    for (b, vcpu, r), n in ex["exits"].items():
        vcpus.add(vcpu)
        if not _in_window(b, t0, t1):
            continue
        by_reason.setdefault(r, {"count": 0, "ns": 0})["count"] += n
    for (b, vcpu, r), ns in ex["exit_ns"].items():
        if not _in_window(b, t0, t1):
            continue
        by_reason.setdefault(r, {"count": 0, "ns": 0})["ns"] += ns

    work_ns = sum(d["ns"] for r, d in by_reason.items() if r not in IDLE_REASONS)
    idle_ns = sum(d["ns"] for r, d in by_reason.items() if r in IDLE_REASONS)
    total_n = sum(d["count"] for d in by_reason.values())
    work_n = sum(d["count"] for r, d in by_reason.items() if r not in IDLE_REASONS)

    probe_w = max(0, ex.get("end_ns", 0) - ex.get("attach_ns", 0))
    if t0 and t1:
        overlap = max(0, min(t1, ex.get("end_ns", 0)) - max(t0, ex.get("attach_ns", 0)))
        coverage = overlap / (t1 - t0) if t1 > t0 else 0.0
        window_ns = t1 - t0
    else:
        coverage = 1.0 if probe_w else 0.0
        window_ns = probe_w
    outside_ns = work_ns + idle_ns
    return {
        "by_reason": by_reason,
        "exits_total": total_n,
        "exits_work": work_n,
        "work_ns": work_ns,
        "idle_ns": idle_ns,
        "outside_ns": outside_ns,
        "nvcpu": len(vcpus),
        "window_ns": window_ns,
        "probe_window_ns": probe_w,
        "coverage": coverage,
        "foreign": ex.get("foreign", {}),
        "isa": ex.get("isa", {}),
    }


def per_vcpu_outside_ns(ex, t0=0, t1=0):
    """return the nanoseconds each vCPU spent outside guest mode"""
    out = {}
    for (b, vcpu, r), ns in ex["exit_ns"].items():
        if not _in_window(b, t0, t1):
            continue
        out[vcpu] = out.get(vcpu, 0) + ns
    return out


def inject_meta(outdir):
    """parse inject.meta into a dict"""
    d = {}
    f = Path(outdir) / "inject.meta"
    if not f.exists():
        return d
    for line in f.read_text(errors="replace").splitlines():
        for tok in line.replace("INJECT ", "").split():
            k, _, v = tok.partition("=")
            if v.isdigit():
                d[k] = int(v)
            elif v:
                d[k] = v
    return d


def window_of(meta):
    """return the window the injector's /proc numbers span"""
    return meta.get("watched_window_start_ns", 0), meta.get("watched_window_end_ns", 0)


def load(outdir):
    """load and cut a run's exits, with its inject.meta"""
    outdir = Path(outdir)
    meta = inject_meta(outdir)
    t0, t1 = window_of(meta)
    return summarise(parse(outdir / "exits.txt"), t0, t1), meta


COVERAGE_FLOOR = 0.80
IDENTITY_SLACK = 0.15


def guest_wall_ns(summary):
    """return the time the vCPUs spent executing guest code, by difference"""
    return max(0, summary["nvcpu"] * summary["window_ns"] - summary["outside_ns"])


def reconcile(summary, meta):
    """check that the probe and /proc describe the same window"""
    bad = []

    if not summary["exits_total"]:
        bad.append("probe counted zero exits in the window: it was not attached to a "
                   "running guest while the interference was live")
    if summary["isa"] and decode_as(summary["isa"]) is None:
        bad.append(f"exit reasons decode as VMX (isa 1) or SVM (isa 2), one ISA per run, "
                   f"but isa={sorted(summary['isa'])}")
    if summary["foreign"]:
        who = ",".join(f"{p}:{n}" for p, n in sorted(summary["foreign"].items()))
        bad.append(f"another VM was running during the window ({who})")
    if meta.get("watched_gone"):
        bad.append("the guest exited before the injection window closed; the window was "
                   "closed at its last live sample")
    if summary["coverage"] < COVERAGE_FLOOR:
        bad.append(f"probe covered {summary['coverage']*100:.0f}% of the window, "
                   f"below the {COVERAGE_FLOOR*100:.0f}% floor")

    vcpu_wall = summary["nvcpu"] * summary["window_ns"]
    if vcpu_wall and summary["outside_ns"] > vcpu_wall * (1 + IDENTITY_SLACK):
        bad.append(f"vCPUs were outside the guest for {summary['outside_ns']/1e9:.2f}s of a "
                   f"{vcpu_wall/1e9:.2f}s vCPU-window: the probe is over-counting")

    runtime = meta.get("watched_runtime_window_ns", 0)
    gw = guest_wall_ns(summary)
    if runtime and gw and runtime < gw * (1 - IDENTITY_SLACK):
        bad.append(f"scheduler runtime {runtime/1e9:.2f}s is below the {gw/1e9:.2f}s of guest "
                   f"execution the exit counts imply")
    return (not bad), bad


def report_one(outdir):
    """print a run's exit table and reconcile it"""
    s, meta = load(outdir)
    ok, bad = reconcile(s, meta)

    print(f"exits: {Path(outdir).name}")
    print(f"  window {s['window_ns']/1e9:.2f}s  probe covered {s['coverage']*100:.0f}%  "
          f"{s['exits_total']} exits ({s['exits_work']} non-idle)  "
          f"isa {decode_as(s['isa']) or sorted(s['isa'])}")
    if s["exits_total"]:
        print(f"  {'reason':22} {'count':>9} {'total ms':>10} {'us/exit':>9}")
        for r, d in sorted(s["by_reason"].items(), key=lambda kv: -kv[1]["ns"]):
            mark = "  idle" if r in IDLE_REASONS else ""
            per = d["ns"] / d["count"] / 1000 if d["count"] else 0
            print(f"  {reason_name(r):22} {d['count']:>9} {d['ns']/1e6:>10.1f} {per:>9.2f}{mark}")
        print(f"  non-idle exit time {s['work_ns']/1e6:.1f} ms   "
              f"outside guest {s['outside_ns']/1e9:.2f}s of {s['nvcpu']*s['window_ns']/1e9:.2f}s "
              f"vCPU-window   guest execution by difference {guest_wall_ns(s)/1e9:.2f}s")
    if meta:
        print(f"  scheduler over the same window: runtime "
              f"{meta.get('watched_runtime_window_ns',0)/1e9:.2f}s over "
              f"{meta.get('watched_threads',0)} threads   (utime+stime "
              f"{meta.get('watched_cpu_window_ns',0)/1e9:.2f}s, USER_HZ)")
    print(f"  reconciled: {'yes' if ok else 'NO -- ' + '; '.join(bad)}")
    return ok, s


def report_paired(paired_path):
    """print per-class exit deltas against the control, round by round"""
    p = ROOT / paired_path
    rounds = json.loads(p.read_text())["rounds"]
    labels = [k for k in rounds[0] if k != "control"] if rounds else []
    exp = ROOT / "results" / "experiments"

    print(f"{len(rounds)} rounds, exit counts against the same control\n")
    hdr = (f"{'class':10} {'n':>3} {'exits/s':>9} {'d exits/s':>10} {'d nonidle ms':>13} "
           f"{'d extint':>9} {'d ept':>7} {'d guest ms':>11} {'d runtime':>10}")
    print(hdr)
    print("-" * len(hdr))
    rows = {}
    for label in labels:
        acc = []
        for i in range(len(rounds)):
            cs, cm = load(exp / f"control-r{i}")
            hs, hm = load(exp / f"{label}-r{i}")
            if not cs["exits_total"] or not hs["exits_total"]:
                continue
            if cs["coverage"] < COVERAGE_FLOOR or hs["coverage"] < COVERAGE_FLOOR:
                continue
            rate = lambda s: s["exits_total"] / (s["window_ns"] / 1e9) if s["window_ns"] else 0
            n = lambda s, r: s["by_reason"].get(r, {"count": 0})["count"]
            acc.append({
                "rate": rate(hs),
                "d_rate": rate(hs) - rate(cs),
                "d_work_ms": (hs["work_ns"] - cs["work_ns"]) / 1e6,
                "d_extint": n(hs, 1) - n(cs, 1),
                "d_ept": n(hs, 48) - n(cs, 48),
                "d_guest_ms": (guest_wall_ns(hs) - guest_wall_ns(cs)) / 1e6,
                "d_runtime_ms": (hm.get("watched_runtime_window_ns", 0)
                                 - cm.get("watched_runtime_window_ns", 0)) / 1e6,
            })
        if not acc:
            print(f"{label:10} {'-':>3} no paired exit data that met the coverage floor")
            continue
        med = lambda k: sorted(a[k] for a in acc)[len(acc) // 2]
        rows[label] = {k: med(k) for k in acc[0]}
        rows[label]["n"] = len(acc)
        print(f"{label:10} {len(acc):>3} {med('rate'):>9.0f} {med('d_rate'):>10.0f} "
              f"{med('d_work_ms'):>13.1f} {med('d_extint'):>9.0f} {med('d_ept'):>7.0f} "
              f"{med('d_guest_ms'):>11.1f} {med('d_runtime_ms'):>10.1f}")
    return rows


def freeze(paired_path, out_path):
    """write the per-round exit summaries to a committed snapshot"""
    from benchmark import lib
    lib.refuse_unpublishable_experiments()
    rounds = json.loads((ROOT / paired_path).read_text())["rounds"]
    labels = sorted({k for r in rounds for k in r})
    exp = ROOT / "results" / "experiments"
    out = {"source": str(paired_path), "rounds": []}
    for i in range(len(rounds)):
        row = {}
        for label in labels:
            d = exp / f"{label}-r{i}"
            if not d.exists():
                continue
            s, meta = load(d)
            if not s["exits_total"]:
                continue
            row[label] = {
                "exits_total": s["exits_total"],
                "work_ns": s["work_ns"],
                "idle_ns": s["idle_ns"],
                "outside_ns": s["outside_ns"],
                "nvcpu": s["nvcpu"],
                "window_ns": s["window_ns"],
                "coverage": round(s["coverage"], 4),
                "by_reason": {str(r): [d_["count"], d_["ns"]]
                              for r, d_ in sorted(s["by_reason"].items())},
                "runtime_ns": meta.get("watched_runtime_window_ns", 0),
                "threads": meta.get("watched_threads", 0),
                "guest_wall_ns": guest_wall_ns(s),
            }
            if s["isa"] and decode_as(s["isa"]) != "vmx":
                row[label]["isa"] = sorted(s["isa"])
        out["rounds"].append(row)
    p = ROOT / out_path
    p.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    kept = sum(len(r) for r in out["rounds"])
    print(f"froze {kept} condition-rounds from {len(out['rounds'])} rounds to {out_path}")
    return 0


def thawed(frozen_path):
    """read a frozen snapshot back in the shape summarise() returns"""
    d = json.loads((ROOT / frozen_path).read_text())
    rows = []
    for r in d["rounds"]:
        row = {}
        for label, v in r.items():
            s = dict(v)
            s["by_reason"] = {int(k): {"count": c, "ns": n}
                              for k, (c, n) in v["by_reason"].items()}
            s["exits_work"] = sum(x["count"] for k, x in s["by_reason"].items()
                                  if k not in IDLE_REASONS)
            s["probe_window_ns"] = s["window_ns"]
            s["foreign"] = {}
            s["isa"] = {i: s["exits_total"] for i in v.get("isa", [ISA_VMX])}
            row[label] = s
        rows.append(row)
    return rows


def selftest():
    """run each refusal case"""
    cases = []
    W0, W1 = 1_000_000_000, 6_000_000_000
    base = {"by_reason": {1: {"count": 10, "ns": 80_000_000}}, "exits_total": 10,
            "exits_work": 10, "work_ns": 80_000_000, "idle_ns": 11_920_000_000,
            "outside_ns": 12_000_000_000, "nvcpu": 4,
            "window_ns": W1 - W0, "probe_window_ns": W1 - W0, "coverage": 1.0,
            "foreign": {}, "isa": {1: 10}}
    meta = {"watched_window_start_ns": W0, "watched_window_end_ns": W1,
            "watched_runtime_window_ns": 9_000_000_000, "watched_gone": 0}

    ok, _ = reconcile(dict(base), dict(meta))
    cases.append(("a healthy pair reconciles", ok is True))

    s = dict(base); s["exits_total"] = 0
    ok, why = reconcile(s, dict(meta))
    cases.append(("zero exits in the window refuses", ok is False and "zero exits" in why[0]))

    s = dict(base); s["isa"] = {0: 10}
    ok, why = reconcile(s, dict(meta))
    cases.append(("an isa that is neither VMX nor SVM refuses",
                  ok is False and any("isa" in w for w in why)))

    s = dict(base); s["foreign"] = {4242: 99}
    ok, why = reconcile(s, dict(meta))
    cases.append(("a foreign VM refuses", ok is False and any("another VM" in w for w in why)))

    s = dict(base); s["coverage"] = 0.4
    ok, why = reconcile(s, dict(meta))
    cases.append(("a probe that missed half the window refuses",
                  ok is False and any("covered" in w for w in why)))

    m = dict(meta); m["watched_gone"] = 1
    ok, why = reconcile(dict(base), m)
    cases.append(("a guest that exited mid-window refuses",
                  ok is False and any("exited before" in w for w in why)))

    s = dict(base); s["outside_ns"] = 40_000_000_000
    ok, why = reconcile(s, dict(meta))
    cases.append(("more time outside the guest than the vCPUs had refuses",
                  ok is False and any("over-counting" in w for w in why)))

    m = dict(meta); m["watched_runtime_window_ns"] = 1_000_000_000
    ok, why = reconcile(dict(base), m)
    cases.append(("runtime below the guest execution the exits imply refuses",
                  ok is False and any("below the" in w for w in why)))

    cases.append(("guest execution is wall minus outside, never negative",
                  guest_wall_ns(base) == 8_000_000_000
                  and guest_wall_ns({**base, "outside_ns": 99_000_000_000,
                                     "nvcpu": 4}) == 0))

    txt = "\n".join([
        "Attaching 5 probes...",
        "@exits[5, 0, 12]: 7",
        "@exits[15, 0, 12]: 11",
        "@exits[15, 0, 1]: 4",
        "@exit_ns[15, 0, 12]: 700",
        "@exit_ns[15, 0, 1]: 250",
        "@isa[1]: 22",
        "@attach_ns: 500000000",
        "@end_ns: 6500000000",
    ])
    tmp = ROOT / "results" / ".selftest-exits.txt"
    tmp.write_text(txt)
    got = parse(tmp)
    tmp.unlink()
    cases.append(("the bucketed dump parses",
                  got["exits"].get((15, 0, 1)) == 4 and got["attach_ns"] == 500_000_000
                  and got["attached"] is True))

    cut = summarise(got, W0, W1)
    cases.append(("buckets outside the window are dropped", cut["exits_total"] == 15))
    cases.append(("halt time is not counted as overhead",
                  cut["work_ns"] == 250 and cut["idle_ns"] == 700))
    cases.append(("coverage is measured against the injector's window",
                  abs(cut["coverage"] - 1.0) < 1e-9))

    late = dict(got); late["attach_ns"] = 4_000_000_000
    cases.append(("a late attach shows up as coverage below 1",
                  abs(summarise(late, W0, W1)["coverage"] - 0.4) < 1e-9))

    cases += svm_cases()
    return _report_cases("exits selftest", cases)


def _report_cases(title, cases):
    """print the cases and return 1 if any failed"""
    bad = sum(1 for _, good in cases if not good)
    for name, good in cases:
        print(f"  {'ok  ' if good else 'FAIL'} {name}")
    print(f"{title}: {len(cases) - bad}/{len(cases)}")
    return 0 if bad == 0 else 1


def _parse_text(txt):
    """parse a dump given as text"""
    tmp = ROOT / "results" / ".selftest-exits.txt"
    tmp.write_text(txt)
    try:
        return parse(tmp)
    finally:
        tmp.unlink()


def svm_cases():
    """return the SVM decode and isa refusal cases"""
    cases = []
    W0, W1 = 1_000_000_000, 6_000_000_000
    Q = SVM_QUAL
    txt = "\n".join([
        "Attaching 5 probes...",
        "@exits[15, 0, 120]: 11",
        "@exits[15, 0, 166]: 2",
        "@exits[15, 0, 96]: 4",
        "@exits[15, 1, 124]: 3",
        f"@exits[15, 1, {0x7c | Q}]: 5",
        "@exits[15, 1, 1024]: 6",
        f"@exits[15, 1, {0x400 | Q}]: 7",
        "@exits[15, 1, 48]: 2",
        "@exits[15, 1, 12]: 1",
        "@exits[15, 1, 1]: 1",
        "@exits[15, 1, 78]: 1",
        "@exits[15, 1, 1025]: 1",
        "@exits[15, 1, 139]: 1",
        "@exit_ns[15, 0, 120]: 700",
        "@exit_ns[15, 0, 166]: 50",
        "@exit_ns[15, 0, 96]: 250",
        "@exit_ns[15, 1, 1024]: 100",
        "@exit_ns[15, 1, 139]: 30",
        "@exit_ns[15, 1, 12]: 5",
        "@isa[2]: 45",
        "@attach_ns: 500000000",
        "@end_ns: 6500000000",
    ])
    got = _parse_text(txt)
    ex = got["exits"]
    cases.append(("an isa=2 dump is decoded as SVM", got["decoded_as"] == "svm"))
    cases.append(("SVM INTR is external_interrupt, and nothing else is",
                  ex.get((15, 0, 1)) == 4 and (15, 1, 1) not in ex))
    cases.append(("SVM HLT and IDLE_HLT are both hlt",
                  ex.get((15, 0, 12)) == 13 and (15, 1, 12) not in ex))
    cases.append(("SVM MSR splits into msr_read and msr_write on the probe's qualifier",
                  ex.get((15, 1, 31)) == 3 and ex.get((15, 1, 32)) == 5))
    cases.append(("SVM NPF splits into ept_violation and ept_misconfig on PFERR_RSVD",
                  ex.get((15, 1, 48)) == 6 and ex.get((15, 1, 49)) == 7))
    cases.append(("raw SVM codes that collide with VMX ids decode by their SVM meaning",
                  ex.get((15, 1, 29)) == 2 and ex.get((15, 1, 28)) == 2
                  and ex.get((15, 1, 0)) == 1))
    avic = SVM_ONLY | 0x401
    cases.append(("an SVM exit with no VMX counterpart is kept apart under its own name",
                  ex.get((15, 1, avic)) == 1 and reason_name(avic) == "svm_avic_incomplete_ipi"
                  and reason_name(SVM_ONLY | 0x7ff) == "svm_0x7ff"))
    cases.append(("SVM MWAIT is mwait", ex.get((15, 1, 36)) == 1))

    cut = summarise(got, W0, W1)
    cases.append(("SVM halt time is idle, not overhead",
                  cut["idle_ns"] == 700 + 50 + 30 and cut["work_ns"] == 250 + 100 + 5))
    cases.append(("every SVM exit is counted once after decoding",
                  cut["exits_total"] == 45 and cut["exits_work"] == 45 - 11 - 2 - 1))

    meta = {"watched_window_start_ns": W0, "watched_window_end_ns": W1,
            "watched_runtime_window_ns": 9_000_000_000, "watched_gone": 0}
    base = {"by_reason": {1: {"count": 10, "ns": 80_000_000}}, "exits_total": 10,
            "exits_work": 10, "work_ns": 80_000_000, "idle_ns": 11_920_000_000,
            "outside_ns": 12_000_000_000, "nvcpu": 4,
            "window_ns": W1 - W0, "probe_window_ns": W1 - W0, "coverage": 1.0,
            "foreign": {}, "isa": {2: 10}}
    ok, why = reconcile(dict(base), dict(meta))
    cases.append(("a healthy SVM pair reconciles", ok is True))
    s = dict(base); s["coverage"] = 0.4
    ok, why = reconcile(s, dict(meta))
    cases.append(("an SVM probe that missed half the window still refuses",
                  ok is False and any("covered" in w for w in why)))
    s = dict(base); s["isa"] = {1: 5, 2: 5}
    ok, why = reconcile(s, dict(meta))
    cases.append(("a run that saw both ISAs refuses",
                  ok is False and any("isa" in w for w in why)))

    vmx = _parse_text(txt.replace("@isa[2]: 45", "@isa[1]: 45"))
    cases.append(("an isa=1 dump is left as VMX reasons",
                  vmx["decoded_as"] == "vmx" and vmx["exits"].get((15, 1, 48)) == 2
                  and vmx["exits"].get((15, 0, 120)) == 11))
    both = _parse_text(txt + "\n@isa[1]: 3")
    cases.append(("a mixed-ISA dump is left undecoded, and refused",
                  both["decoded_as"] is None
                  and reconcile(summarise(both, W0, W1), dict(meta))[0] is False))
    return cases


def selftest_svm():
    """run the SVM cases"""
    return _report_cases("exits SVM selftest", svm_cases())


def main():
    """run the selected report, freeze or selftest"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir")
    ap.add_argument("--paired")
    ap.add_argument("--freeze", metavar="OUT",
                    help="snapshot file for --paired")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-svm", action="store_true",
                    help="only the SVM decoding and ISA cases of --selftest")
    ap.add_argument("--require-reconciled", action="store_true",
                    help="fail if the two instruments disagree")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.selftest_svm:
        return selftest_svm()
    if a.freeze:
        if not a.paired:
            print("--freeze needs --paired", file=sys.stderr)
            return 2
        return freeze(a.paired, a.freeze)
    if a.paired:
        report_paired(a.paired)
        return 0
    if a.outdir:
        ok, _ = report_one(a.outdir)
        return 0 if (ok or not a.require_reconciled) else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
