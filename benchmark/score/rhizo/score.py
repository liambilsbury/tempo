#!/usr/bin/env python3
import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test  # noqa: E402

ARMS = ("noload-noadpf", "noload-adpf", "vmload-noadpf", "vmload-adpf", "pcload-noadpf", "pcload-adpf")
KEEP_FLOOR = 12
RECALL_BAR, FALSE_BAR, BELOW_BAR, RATIO_BAR, P_BAR = 0.90, 0.02, 0.05, 0.5, 0.05
MIN_HELD, MIN_CLEAR, MIN_MISSED = 50, 1000, 50
GRANT_GROWTH = 1.25
RENDERER = {"on": "virgl", "off": "llvmpipe"}
DRIFT_SLOT_US = "97000"
DRIFT_GUEST_KERNEL = "steal"
USAGE = """usage: score.py <paired.json> [--gpu on|off] [--drift]
       score.py --compare <rhizo.json> <rhizo-nogpu.json>
       score.py --selftest"""


def arm_of(label):
    return next((a for a in ARMS if label == f"rhizo-{a}" or label.endswith(f"-rhizo-{a}")), None)


def arrived(f):
    level, duty = f.get("level"), float(f.get("duty", 0)) / 100
    if level == "none":
        return True
    if level == "vm":
        return f["injected_ns"] >= 0.9 * duty * f["span_ns"] and f["load_held_ns"] >= 0.8 * f["load_all_windows_ns"]
    if level == "pc":
        return f["inject_cpu_ns"] >= 0.9 * duty * float(f["load_s"]) * 1e9 * int(f["load_cpus"])
    return False


def not_kept(f):
    if not isinstance(f, dict):
        return ["no run"]
    why = [name for k, name in (("exclusive_start", "busy start"),
                                ("exclusive_end", "busy end"),
                                ("vcpus_pinned_start", "unpinned start"),
                                ("vcpus_pinned_end", "unpinned end")) if f.get(k) != 1]
    if not arrived(f):
        why.append("no arrival")
    if f.get("adpf") == "on" and f.get("sessions_paused") != 0:
        why.append("hint sessions paused")
    return why


def rounds_kept(d):
    keep, notes = [], []
    for i, r in enumerate(d["rounds"]):
        arms = {arm_of(l): f for l, f in r.items() if arm_of(l)}
        bad = {a: w for a in ARMS for w in [not_kept(arms.get(a))] if w}
        if bad:
            notes += [f"round {i + 1} {a}: {', '.join(w)}" for a, w in bad.items()]
        else:
            keep.append(arms)
    return keep, notes


def pooled(rounds, arms, num, den):
    return sum(r[a][num] for r in rounds for a in arms), sum(r[a][den] for r in rounds for a in arms)


def frac_line(name, n, d, bar, side, minimum, what):
    if d < minimum:
        return f"{name} MISS: {what} {n}/{d} < {minimum}"
    v = n / d
    ok = v >= bar if side == "at least" else v <= bar
    return f"{name} {'HIT' if ok else 'MISS'}: {what} {n}/{d} = {v:.3f} ({side} {bar})"


def ratio(rounds, arm, base):
    a = [r[arm]["missed_frac"] for r in rounds]
    b = [r[base]["missed_frac"] for r in rounds]
    mb = median(b)
    p, up, down = sign_test([x - y for x, y in zip(a, b)])
    return (median(a) / mb if mb else None), p, up, down


def _r(x):
    return "undefined" if x is None else f"{x:.4f}"


def gpu_disagreements(d, gpu):
    want = {"gpu": gpu, "renderer": RENDERER[gpu]}
    absent = want if gpu == "on" else {"gpu": "none", "renderer": "none"}
    out = []
    for i, r in enumerate(d["rounds"]):
        for label, f in r.items():
            got = {k: str(f.get(k, absent[k])) if isinstance(f, dict) else "none" for k in want}
            if got != want:
                out.append(f"run {label} round {i + 1} has gpu={got['gpu']} renderer={got['renderer']}, "
                           f"not gpu={gpu} renderer={want['renderer']}")
    return out


def slot_disagreements(d, slot):
    return [f"run {label} round {i + 1} has slot_us={f.get('slot_us', '100000') if isinstance(f, dict) else 'none'}, not {slot}"
            for i, r in enumerate(d["rounds"]) for label, f in r.items()
            if not isinstance(f, dict) or str(f.get("slot_us", "100000")) != slot]


def kernel_disagreements(d, kernel):
    return [f"run {label} round {i + 1} has guest_kernel={f.get('guest_kernel', 'stock') if isinstance(f, dict) else 'none'}, not {kernel}"
            for i, r in enumerate(d["rounds"]) for label, f in r.items()
            if not isinstance(f, dict) or str(f.get("guest_kernel", "stock")) != kernel]


def drift_lines(keep):
    def cpu(arm):
        got = [r[arm]["compute_frame_cpu_ns"] for r in keep if r[arm].get("compute_frame_cpu_ns", 0) > 0]
        return median(got) if got else 0
    base = cpu("noload-adpf")
    vm, pc = (cpu("vmload-adpf") / base, cpu("pcload-adpf") / base) if base else (None, None)

    def over_work(arm):
        got = [r[arm]["compute_frame_cpu_ns"] / r[arm]["compute_work_ns"] for r in keep
               if r[arm].get("compute_frame_cpu_ns", 0) > 0 and r[arm].get("compute_work_ns", 0) > 0]
        return median(got) if got else None

    def steal(arm):
        runs = [r[arm] for r in keep if r[arm]["loop_steal_ns"] >= 0 and r[arm]["loop_span_ns"] > 0]
        span = sum(4 * f["loop_span_ns"] for f in runs)
        return sum(f["loop_steal_ns"] for f in runs) / span if span else None
    adpf = [r[a] for r in keep for a in ARMS if a.endswith("-adpf")]
    dl = sum(f.get("telemetry_policy") == "deadline" for f in adpf)
    lost, sent = pooled(keep, ["vmload-adpf"], "recorder_lost", "frames_sent")
    return [f"reported: compute CPU/frame, VM/none {_r(vm)}, PC/none {_r(pc)}",
            f"reported: compute CPU/work, "
            f"none {_r(over_work('noload-adpf'))}, VM {_r(over_work('vmload-adpf'))}, PC {_r(over_work('pcload-adpf'))}",
            f"reported: PC steal, "
            f"noadpf {_r(steal('pcload-noadpf'))}, adpf {_r(steal('pcload-adpf'))}",
            f"reported: SCHED_DEADLINE telemetry {dl}/{len(adpf)}",
            f"reported: vmload-adpf recorder lost {lost}/{sent}"]


def score(d, gpu="on", drift=False):
    disagree = gpu_disagreements(d, gpu)
    if disagree:
        return [f"VOID: {l}" for l in disagree], 4
    if drift:
        off = slot_disagreements(d, DRIFT_SLOT_US) or kernel_disagreements(d, DRIFT_GUEST_KERNEL)
        if off:
            return [f"VOID: {l}" for l in off], 4
    keep, notes = rounds_kept(d)
    lines = [f"rounds kept {len(keep)} of {len(d['rounds'])} (floor {KEEP_FLOOR})"] + [f"not kept: {n}" for n in notes]
    if len(keep) < KEEP_FLOOR:
        return lines + [f"VOID: kept < {KEEP_FLOOR}"], 4
    lines.append(frac_line("P1", *pooled(keep, ["vmload-noadpf"], "missed_held_labelled", "missed_held"),
                           RECALL_BAR, "at least", MIN_HELD, "VM-load missed frames held by the load that Android was told of"))
    lines.append(frac_line("P2", *pooled(keep, ["noload-noadpf", "noload-adpf", "vmload-noadpf", "vmload-adpf"],
                                         "clear_labelled", "clear"),
                           FALSE_BAR, "at most", MIN_CLEAR, "load-free frames Android was told were held"))
    lines.append(frac_line("P3", *pooled(keep, ["pcload-noadpf"], "missed_labelled", "missed"),
                           BELOW_BAR, "at most", MIN_MISSED, "PC-load missed frames Android was told were held"))
    r4, p4, up4, dn4 = ratio(keep, "vmload-adpf", "vmload-noadpf")
    ok4 = r4 is not None and r4 <= RATIO_BAR and p4 < P_BAR and dn4 > up4
    lines.append(f"P4 {'HIT' if ok4 else 'MISS'}: VM load, missed_frac with ADPF over without {_r(r4)} "
                 f"(a HIT is at most {RATIO_BAR} with p below {P_BAR} and more down), sign test p={p4:.4f}, "
                 f"{dn4} down / {up4} up")
    r5, p5, up5, dn5 = ratio(keep, "pcload-adpf", "pcload-noadpf")
    recovered = r5 is not None and r5 <= RATIO_BAR and p5 < P_BAR and dn5 > up5
    p5_word = "MISS" if recovered or r5 is None else "HIT"
    p5_none = ", none missed without ADPF" if r5 is None else ""
    lines.append(f"P5 {p5_word}: PC load, missed_frac with ADPF over without {_r(r5)}{p5_none} "
                 f"(a recovery is at most {RATIO_BAR} with p below {P_BAR} and more down), sign test p={p5:.4f}, "
                 f"{dn5} down / {up5} up")
    rc, pc, _, _ = ratio(keep, "noload-adpf", "noload-noadpf")
    lines.append(f"reported: no load, missed_frac with ADPF over without {_r(rc)}, sign test p={pc:.4f}")
    hv, iv = pooled(keep, ["vmload-noadpf"], "delayd_held_ns", "injected_ns")
    lines.append(f"reported: VM load, held time Android was told of, summed over the three deadline threads, over the time "
                 f"the load held every vCPU inside the run {_r(hv / iv if iv else None)}")
    hp, ip = pooled(keep, ["pcload-noadpf"], "delayd_held_ns", "inject_cpu_ns")
    lines.append(f"reported: PC load, held time Android was told of over the load's own CPU time on four CPUs across its whole run "
                 f"{_r(hp / ip if ip else None)}")
    sp, _ = pooled(keep, ["pcload-noadpf"], "guest_steal_ns", "inject_cpu_ns")
    lines.append(f"reported: PC load, the VM's own steal time over the load's own CPU time {_r(sp / ip if ip else None)}")
    g_no = median([r["noload-adpf"]["compute_grant_bw"] for r in keep])
    grant = median([r["pcload-adpf"]["compute_grant_bw"] for r in keep]) / g_no if g_no else None
    lines.append(f"reported: the compute thread's granted bandwidth across the loop, PC load over no load {_r(grant)}")
    torn, sent = pooled(keep, ARMS, "torn_records", "frames_sent")
    lost, _ = pooled(keep, ARMS, "recorder_lost", "frames_sent")
    lines.append(f"reported: all kept runs, torn records {torn} and frames the telemetry recorder lost {lost}, of {sent} frames sent")
    if drift:
        lines += drift_lines(keep)
    if recovered:
        reading = "helps: missed at recovery bar"
    elif grant is not None and grant >= GRANT_GROWTH:
        reading = (f"hides: the granted bandwidth rose to {GRANT_GROWTH} times or more and the missed frames "
                   f"did not fall to the recovery bar")
    else:
        reading = f"neither: grant < {GRANT_GROWTH}x, missed above bar"
    lines.append(f"interpretation: {reading}")
    return lines, 0


def compare(gpu_d, nogpu_d):
    for d, gpu, name in ((gpu_d, "on", "rhizo"), (nogpu_d, "off", "rhizo-nogpu")):
        if gpu_disagreements(d, gpu):
            return [f"no comparison: {name} gpu != {gpu}"], 4
    on, off = rounds_kept(gpu_d)[0], rounds_kept(nogpu_d)[0]
    short = [f"{n} kept {len(k)}" for n, k in (("rhizo", on), ("rhizo-nogpu", off)) if len(k) < KEEP_FLOOR]
    if short:
        return [f"no comparison: {', '.join(short)} < {KEEP_FLOOR}"], 4
    lines = []
    for a in ARMS:
        m_on, m_off = median([r[a]["missed_frac"] for r in on]), median([r[a]["missed_frac"] for r in off])
        lines.append(f"reported: {a}, missed_frac off/on {_r(m_off / m_on if m_on else None)}, kept {len(off)}/{len(on)}")
    return lines, 0


def selftest():
    def fact(level, adpf, missed_frac, **kw):
        vm, pc = level == "vm", level == "pc"
        f = dict(level=level, adpf=adpf, duty=0 if level == "none" else 30, load_s=75, span_ns=60_000_000_000,
                 load_cpus=0 if level == "none" else 4,
                 injected_ns=18_000_000_000 if vm else 0, load_all_windows_ns=90_000_000_000 if vm else 0,
                 load_held_ns=88_000_000_000 if vm else 0, inject_cpu_ns=88_000_000_000 if pc else 0,
                 exclusive_start=1, exclusive_end=1, vcpus_pinned_start=1, vcpus_pinned_end=1,
                 missed_frac=missed_frac, missed_held=100 if vm else 0, missed_held_labelled=95 if vm else 0,
                 clear=4000, clear_labelled=10, missed=300 if pc else 10, missed_labelled=3 if pc else 0,
                 delayd_held_ns=16_000_000_000 if vm else 0, guest_steal_ns=15_000_000_000 if pc else 0,
                 compute_grant_bw=0.06 if adpf == "on" else 0, sessions_paused=0, grant_refusals=0,
                 frames_sent=6000, torn_records=0, recorder_lost=2 if adpf == "on" else 1)
        f.update(kw)
        return f

    def batch(n=16, vm_adpf=0.001, pc_adpf=0.05, grant_pc=0.09, vm_no=0.05, pc_no=0.05, bad=None):
        rounds = []
        for i in range(n):
            r = {"rhizo-noload-noadpf": fact("none", "off", 0.0), "rhizo-noload-adpf": fact("none", "on", 0.0),
                 "rhizo-vmload-noadpf": fact("vm", "off", vm_no + i * 1e-4 if vm_no else 0.0),
                 "rhizo-vmload-adpf": fact("vm", "on", vm_adpf + i * 1e-5 if vm_adpf else 0.0),
                 "rhizo-pcload-noadpf": fact("pc", "off", pc_no + i * 1e-4 if pc_no else 0.0),
                 "rhizo-pcload-adpf": fact("pc", "on", pc_adpf + i * 1e-4, compute_grant_bw=grant_pc)}
            if bad and i == 0:
                r[bad[0]].update(bad[1])
            rounds.append(r)
        return {"topology": "single-level", "metric": "missed_frac", "rounds": rounds}

    def verdict(lines, p):
        return next((l.split()[1].rstrip(":") for l in lines if l.startswith(p + " ")), None)
    cases = []
    lines, rc = score(batch())
    cases.append(("default batch: P1-P5 HIT", rc == 0 and [verdict(lines, p) for p in ("P1", "P2", "P3", "P4", "P5")]
                  == ["HIT", "HIT", "HIT", "HIT", "HIT"]))
    cases.append(("grant x1.5, no recovery: interpretation hides",
                  any(l.startswith("interpretation: hides: the granted bandwidth rose") for l in lines)))
    cases.append(("grant line: 1.5000",
                  "reported: the compute thread's granted bandwidth across the loop, PC load over no load 1.5000" in lines))
    cases.append(("no _ns/ms in output", not any("_ns" in l or " ms" in l for l in lines)))
    cases.append(("VM held-time line text",
                  any(l.startswith("reported: VM load, held time Android was told of, summed over the three deadline "
                                   "threads, over the time the load held every vCPU inside the run ") for l in lines)))
    cases.append(("PC held-time line text",
                  any(l.startswith("reported: PC load, held time Android was told of over the load's own CPU time on four CPUs "
                                   "across its whole run ") for l in lines)))
    cases.append(("torn/lost line: 0, 144 of 576000",
                  "reported: all kept runs, torn records 0 and frames the telemetry recorder lost 144, of 576000 frames sent"
                  in lines))
    lines, rc = score(batch(pc_no=0.0, pc_adpf=0.0))
    cases.append(("pc_no=0: P5 MISS, undefined, no evidence",
                  verdict(lines, "P5") == "MISS" and any(l.startswith("P5") and "undefined" in l and "none missed" in l
                                                          for l in lines)))
    lines, rc = score(batch(n=11))
    cases.append(("n=11: VOID", rc == 4 and any(l.startswith("VOID") for l in lines)))
    lines, rc = score(batch(bad=("rhizo-vmload-adpf", {"exclusive_end": 0})))
    cases.append(("exclusive_end=0: round dropped",
                  rc == 0 and lines[0].startswith("rounds kept 15 of 16") and any("busy end" in l for l in lines)))
    lines, rc = score(batch(bad=("rhizo-pcload-adpf", {"inject_cpu_ns": 22_000_000_000})))
    cases.append(("pcload-adpf inject_cpu_ns=22e9: round dropped",
                  lines[0].startswith("rounds kept 15") and any("no arrival" in l for l in lines)))
    lines, rc = score(batch(bad=("rhizo-vmload-adpf", {"sessions_paused": 2})))
    cases.append(("sessions_paused=2: round dropped",
                  lines[0].startswith("rounds kept 15") and any("vmload-adpf: hint sessions paused" in l for l in lines)))
    lines, rc = score(batch(bad=("rhizo-pcload-adpf", {"grant_refusals": 40})))
    cases.append(("grant_refusals=40: round kept", lines[0].startswith("rounds kept 16")))
    lines, rc = score(batch(bad=("rhizo-vmload-noadpf", {"injected_ns": 10_000_000_000})))
    cases.append(("vmload-noadpf injected_ns=10e9: round dropped", lines[0].startswith("rounds kept 15")))
    lines, rc = score(batch(bad=("rhizo-pcload-noadpf", {"inject_cpu_ns": 1_000_000_000})))
    cases.append(("pcload-noadpf inject_cpu_ns=1e9: round dropped", lines[0].startswith("rounds kept 15")))
    lines, rc = score(batch(vm_no=0.0, vm_adpf=0.0))
    cases.append(("vm_no=0: P4 MISS, undefined",
                  verdict(lines, "P4") == "MISS" and any(l.startswith("P4") and "undefined" in l for l in lines)))
    b = batch()
    for r in b["rounds"]:
        r["rhizo-vmload-noadpf"]["missed_held"] = 2
    lines, rc = score(b)
    cases.append(("missed_held=2: P1 MISS", verdict(lines, "P1") == "MISS"))
    lines, rc = score(batch(vm_adpf=0.2))
    cases.append(("vm_adpf=0.2: P4 MISS, 0 down / 16 up",
                  verdict(lines, "P4") == "MISS" and any(l.startswith("P4") and "0 down / 16 up" in l for l in lines)))
    lines, rc = score(batch(pc_adpf=0.001))
    cases.append(("pc_adpf=0.001: P5 MISS, interpretation helps",
                  verdict(lines, "P5") == "MISS" and any(l.startswith("interpretation: helps") for l in lines)))
    lines, rc = score(batch(grant_pc=0.06))
    cases.append(("grant_pc=0.06: interpretation neither",
                  any(l.startswith("interpretation: neither") for l in lines)))
    cases.append(("arm_of prefixed, plain, pc-r4-control", arm_of("one-round-rhizo-pcload-adpf") == "pcload-adpf"
                  and arm_of("rhizo-pcload-adpf") == "pcload-adpf" and arm_of("pc-r4-control") is None))

    def with_gpu(d, gpu, renderer):
        for r in d["rounds"]:
            for f in r.values():
                f.update(gpu=gpu, renderer=renderer)
        return d
    lines, rc = score(with_gpu(batch(), "off", "llvmpipe"), gpu="off")
    cases.append(("gpu=off, llvmpipe: rc 0", rc == 0))
    d = with_gpu(batch(), "off", "llvmpipe")
    d["rounds"][0]["rhizo-vmload-adpf"]["renderer"] = "virgl"
    lines, rc = score(d, gpu="off")
    cases.append(("gpu=off, one virgl run: VOID, run named",
                  rc == 4 and any("rhizo-vmload-adpf" in l and "virgl" in l for l in lines)))
    lines, rc = score(with_gpu(batch(), "on", "virgl"), gpu="off")
    cases.append(("gpu=on batch, scored off: VOID", rc == 4))
    lines, rc = score(batch())
    cases.append(("no gpu keys, gpu=on: rc 0", rc == 0))
    lines, rc = score(batch(), gpu="off")
    cases.append(("no gpu keys, gpu=off: VOID", rc == 4))
    on, off = with_gpu(batch(), "on", "virgl"), with_gpu(batch(vm_no=0.10), "off", "llvmpipe")
    lines, rc = compare(on, off)
    vm_line = next((l for l in lines if l.startswith("reported: vmload-noadpf,")), "")
    cases.append(("compare: line per arm, off/on median, 16 and 16 kept",
                  rc == 0 and len(lines) == len(ARMS) and "kept 16/16" in vm_line
                  and abs(float(vm_line.split(" off/on ")[1].split(",")[0]) - median(
                      [0.10 + i * 1e-4 for i in range(16)]) / median([0.05 + i * 1e-4 for i in range(16)])) < 1e-4))
    cases.append(("compare on median=0: undefined",
                  any(l.startswith("reported: noload-noadpf,") and "undefined" in l for l in lines)))
    bad_off = with_gpu(batch(), "off", "llvmpipe")
    bad_off["rounds"][2]["rhizo-pcload-adpf"]["renderer"] = "virgl"
    lines, rc = compare(on, bad_off)
    cases.append(("compare nogpu with virgl run: rc 4", rc == 4 and "rhizo-nogpu" in lines[0]))
    lines, rc = compare(on, with_gpu(batch(n=11), "off", "llvmpipe"))
    cases.append(("compare nogpu n=11: rc 4, rhizo-nogpu named",
                  rc == 4 and "rhizo-nogpu kept 11" in lines[0] and "rhizo kept" not in lines[0]))
    with contextlib.redirect_stderr(io.StringIO()) as err:
        rc = main(["--compare", "/nonexistent-a.json", "/nonexistent-b.json"])
    cases.append(("compare unreadable file: rc 2, path on stderr", rc == 2 and "/nonexistent-a.json" in err.getvalue()))

    def drifted(d):
        cpu = {"noload": 300000, "vmload": 540000, "pcload": 480000}
        steal = {"pcload-noadpf": 48_000_000_000, "pcload-adpf": 60_000_000_000}
        for r in d["rounds"]:
            for label, f in r.items():
                arm = arm_of(label)
                f.update(slot_us="97000", guest_kernel="steal", telemetry_policy="deadline" if f["adpf"] == "on" else "other",
                         compute_frame_cpu_ns=cpu[arm.split("-")[0]], compute_work_ns=290000, loop_steal_ns=steal.get(arm, 0),
                         loop_span_ns=60_000_000_000, recorder_lost=0)
        return d
    drift = drifted(batch())
    lines, rc = score(drift, drift=True)
    cases.append(("drift CPU-per-frame line: 1.8000, 1.6000",
                  "reported: compute CPU/frame, VM/none 1.8000, PC/none 1.6000" in lines))
    cases.append(("drift CPU/work line: 1.0345, 1.8621, 1.6552",
                  "reported: compute CPU/work, none 1.0345, VM 1.8621, PC 1.6552" in lines))
    cases.append(("drift steal line: 0.2000, 0.2500",
                  "reported: PC steal, noadpf 0.2000, adpf 0.2500" in lines))
    cases.append(("drift SCHED_DEADLINE count: 48/48",
                  "reported: SCHED_DEADLINE telemetry 48/48" in lines))
    cases.append(("drift vmload-adpf recorder line: 0 of 96000",
                  "reported: vmload-adpf recorder lost 0/96000" in lines))
    cases.append(("drift line order: 6 reported, interpretation",
                  [l.split(":")[0] for l in lines[-7:]] == ["reported"] * 6 + ["interpretation"]))
    slow = drifted(batch())
    slow["rounds"][3]["rhizo-vmload-noadpf"]["slot_us"] = "100000"
    lines, rc = score(slow, drift=True)
    cases.append(("drift slot_us=100000: VOID, run named",
                  rc == 4 and any(l.startswith("VOID: run rhizo-vmload-noadpf round 4 has slot_us=100000, not 97000") for l in lines)))
    stock = drifted(batch())
    stock["rounds"][5]["rhizo-pcload-adpf"]["guest_kernel"] = "stock"
    lines, rc = score(stock, drift=True)
    cases.append(("drift guest_kernel=stock: VOID, run named",
                  rc == 4 and any(l.startswith("VOID: run rhizo-pcload-adpf round 6 has guest_kernel=stock, not steal") for l in lines)))
    nokernel = drifted(batch())
    del nokernel["rounds"][0]["rhizo-noload-noadpf"]["guest_kernel"]
    cases.append(("drift guest_kernel absent: VOID", score(nokernel, drift=True)[1] == 4))
    cases.append(("drift=False, guest_kernel absent: rc 0", score(batch())[1] == 0))
    one_other = drifted(batch())
    one_other["rounds"][0]["rhizo-vmload-adpf"]["telemetry_policy"] = "other"
    cases.append(("telemetry_policy=other: 47/48",
                  "reported: SCHED_DEADLINE telemetry 47/48" in score(one_other, drift=True)[0]))
    no_steal = drifted(batch())
    for r in no_steal["rounds"]:
        for f in r.values():
            f["loop_steal_ns"], f["loop_span_ns"] = -1, 0
    cases.append(("loop_steal_ns=-1: steal undefined",
                  any(l.endswith("noadpf undefined, adpf undefined") for l in score(no_steal, drift=True)[0])))
    zero = drifted(batch())
    for r in zero["rounds"][:9]:
        r["rhizo-vmload-adpf"]["compute_frame_cpu_ns"] = 0
    cases.append(("compute_frame_cpu_ns=0 x9: excluded, 1.8000",
                  any(l.startswith("reported: compute CPU/frame, VM/none 1.8000")
                      for l in score(zero, drift=True)[0])))
    no_work = drifted(batch())
    for r in no_work["rounds"][:9]:
        r["rhizo-pcload-adpf"]["compute_work_ns"] = 0
    cases.append(("compute_work_ns=0 x9: excluded, 1.6552",
                  any(l.endswith("PC 1.6552") and "CPU/work" in l for l in score(no_work, drift=True)[0])))
    cases.append(("drift=False: no CPU-per-frame line", not any("CPU/frame" in l for l in score(drifted(batch()))[0])))
    with contextlib.redirect_stderr(io.StringIO()):
        main_rc = main([str(Path(__file__).resolve()), "--drift"])
    cases.append(("--drift unreadable file: rc 2", main_rc == 2))
    for name, gpu in (("rhizo", "on"), ("rhizo-nogpu", "off")):
        p = ROOT / "results" / "rhizo" / f"{name}.json"
        if p.exists():
            fz = json.loads(p.read_text())
            want = [l for l in fz["score_text"].splitlines() if not l.startswith("scored at ")]
            cases.append((f"results/rhizo/{name}.json: score == score_text", score(fz, gpu=gpu)[0] == want))
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'ok' if ok else 'FAIL'} {n}")
    print(f"selftest {len(cases) - len(bad)}/{len(cases)} ok")
    return 1 if bad else 0


def read(path):
    try:
        d = json.loads(Path(path).read_text())
        assert isinstance(d.get("rounds"), list)
        return d
    except (OSError, ValueError, AssertionError) as e:
        print(f"{path}: {e}", file=sys.stderr)
        return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) == 3 and argv[0] == "--compare":
        ds = [read(a) for a in argv[1:]]
        if None in ds:
            return 2
        lines, rc = compare(*ds)
        print("\n".join(lines))
        return rc
    gpu, drift = "on", "--drift" in argv
    argv = [a for a in argv if a != "--drift"]
    if len(argv) == 3 and argv[1] == "--gpu" and argv[2] in RENDERER:
        argv, gpu = argv[:1], argv[2]
    if len(argv) != 1 or argv[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    d = read(argv[0])
    if d is None:
        return 2
    lines, rc = score(d, gpu, drift)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
