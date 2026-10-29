#!/usr/bin/env python3
"""Score the Rhizo batch

Usage: score.py <paired.json> [--gpu on|off] [--drift]
       score.py --compare <rhizo.json> <rhizo-nogpu.json>
       score.py --selftest
  --gpu      renderer every run used: on virgl, off llvmpipe, default on
  --drift    score a drift batch
  --compare  GPU-off over GPU-on missed_frac per arm
"""
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


def arm_of(label):
    """return the arm a label ends with, or None"""
    return next((a for a in ARMS if label == f"rhizo-{a}" or label.endswith(f"-rhizo-{a}")), None)


def arrived(f):
    """return whether a run's load arrived as its duty says"""
    level, duty = f.get("level"), float(f.get("duty", 0)) / 100
    if level == "none":
        return True
    if level == "vm":
        return f["injected_ns"] >= 0.9 * duty * f["span_ns"] and f["load_held_ns"] >= 0.8 * f["load_all_windows_ns"]
    if level == "pc":
        return f["inject_cpu_ns"] >= 0.9 * duty * float(f["load_s"]) * 1e9 * int(f["load_cpus"])
    return False


def not_kept(f):
    """return why a run is not kept, empty when it is"""
    if not isinstance(f, dict):
        return ["no run"]
    why = [name for k, name in (("exclusive_start", "host not idle at the start"),
                                ("exclusive_end", "host not idle at the end"),
                                ("vcpus_pinned_start", "vCPUs not pinned at the start"),
                                ("vcpus_pinned_end", "vCPUs not pinned at the end")) if f.get(k) != 1]
    if not arrived(f):
        why.append("the load did not arrive")
    if f.get("adpf") == "on" and f.get("sessions_paused") != 0:
        why.append("the app's sessions were paused")
    return why


def rounds_kept(d):
    """return the kept rounds and a note per run not kept"""
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
    """return a numerator and a denominator summed over the given arms"""
    return sum(r[a][num] for r in rounds for a in arms), sum(r[a][den] for r in rounds for a in arms)


def frac_line(name, n, d, bar, side, minimum, what):
    """return a prediction's line for a pooled fraction held to a bar"""
    if d < minimum:
        return f"{name} MISS: {what} {n}/{d}, fewer than {minimum} frames to score"
    v = n / d
    ok = v >= bar if side == "at least" else v <= bar
    return f"{name} {'HIT' if ok else 'MISS'}: {what} {n}/{d} = {v:.3f} ({side} {bar})"


def ratio(rounds, arm, base):
    """return arm's median missed_frac over base's, the sign test p and counts"""
    a = [r[arm]["missed_frac"] for r in rounds]
    b = [r[base]["missed_frac"] for r in rounds]
    mb = median(b)
    p, up, down = sign_test([x - y for x, y in zip(a, b)])
    return (median(a) / mb if mb else None), p, up, down


def _r(x):
    """format a ratio, or say it is undefined"""
    return "undefined" if x is None else f"{x:.4f}"


def gpu_disagreements(d, gpu):
    """return a line per run whose gpu or renderer is not gpu's"""
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
    """return a line per run whose recorded slot_us is not slot"""
    return [f"run {label} round {i + 1} has slot_us={f.get('slot_us', '100000') if isinstance(f, dict) else 'none'}, not {slot}"
            for i, r in enumerate(d["rounds"]) for label, f in r.items()
            if not isinstance(f, dict) or str(f.get("slot_us", "100000")) != slot]


def kernel_disagreements(d, kernel):
    """return a line per run whose recorded guest kernel is not kernel"""
    return [f"run {label} round {i + 1} has guest_kernel={f.get('guest_kernel', 'stock') if isinstance(f, dict) else 'none'}, not {kernel}"
            for i, r in enumerate(d["rounds"]) for label, f in r.items()
            if not isinstance(f, dict) or str(f.get("guest_kernel", "stock")) != kernel]


def drift_lines(keep):
    """return the drift batch's own reported lines over the kept rounds"""
    def cpu(arm):
        """return an arm's median compute CPU per frame, or 0"""
        got = [r[arm]["compute_frame_cpu_ns"] for r in keep if r[arm].get("compute_frame_cpu_ns", 0) > 0]
        return median(got) if got else 0
    base = cpu("noload-adpf")
    vm, pc = (cpu("vmload-adpf") / base, cpu("pcload-adpf") / base) if base else (None, None)

    def over_work(arm):
        """return an arm's median of rhizo-rtd's CPU over the compute stage's own time"""
        got = [r[arm]["compute_frame_cpu_ns"] / r[arm]["compute_work_ns"] for r in keep
               if r[arm].get("compute_frame_cpu_ns", 0) > 0 and r[arm].get("compute_work_ns", 0) > 0]
        return median(got) if got else None

    def steal(arm):
        """return the pooled steal over four vCPUs' time across the loop, or None"""
        runs = [r[arm] for r in keep if r[arm]["loop_steal_ns"] >= 0 and r[arm]["loop_span_ns"] > 0]
        span = sum(4 * f["loop_span_ns"] for f in runs)
        return sum(f["loop_steal_ns"] for f in runs) / span if span else None
    adpf = [r[a] for r in keep for a in ARMS if a.endswith("-adpf")]
    dl = sum(f.get("telemetry_policy") == "deadline" for f in adpf)
    lost, sent = pooled(keep, ["vmload-adpf"], "recorder_lost", "frames_sent")
    return [f"reported: the compute thread's CPU per frame by rhizo-rtd's own samples, VM load over no load {_r(vm)}, "
            f"PC load over no load {_r(pc)}",
            f"reported: rhizo-rtd's CPU per frame for the compute thread over the compute stage's own time per frame, "
            f"no load {_r(over_work('noload-adpf'))}, VM load {_r(over_work('vmload-adpf'))}, PC load {_r(over_work('pcload-adpf'))}",
            f"reported: PC load, the guest's steal over its four vCPUs' time across the loop, without ADPF "
            f"{_r(steal('pcload-noadpf'))}, with ADPF {_r(steal('pcload-adpf'))}",
            f"reported: ADPF runs whose telemetry stage ran under SCHED_DEADLINE {dl}/{len(adpf)}",
            f"reported: VM load with ADPF, frames the telemetry recorder lost {lost}, of {sent} frames sent"]


def score(d, gpu="on", drift=False):
    """return the verdict lines and 0, or the VOID lines and 4"""
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
        return lines + [f"VOID: fewer than {KEEP_FLOOR} rounds kept; no prediction is scored"], 4
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
    p5_none = ", no evidence: no frame was missed without ADPF" if r5 is None else ""
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
        reading = "helps: the missed frames fell to the recovery bar under the PC load"
    elif grant is not None and grant >= GRANT_GROWTH:
        reading = (f"hides: the granted bandwidth rose to {GRANT_GROWTH} times or more and the missed frames "
                   f"did not fall to the recovery bar")
    else:
        reading = (f"neither: the granted bandwidth rose less than {GRANT_GROWTH} times and the missed frames "
                   f"did not fall to the recovery bar")
    lines.append(f"interpretation: {reading}")
    return lines, 0


def compare(gpu_d, nogpu_d):
    """return a reported line per arm and 0, or why not and 4"""
    for d, gpu, name in ((gpu_d, "on", "rhizo"), (nogpu_d, "off", "rhizo-nogpu")):
        if gpu_disagreements(d, gpu):
            return [f"no comparison: {name} holds a run not rendered as gpu {gpu} says"], 4
    on, off = rounds_kept(gpu_d)[0], rounds_kept(nogpu_d)[0]
    short = [f"{n} kept {len(k)} rounds" for n, k in (("rhizo", on), ("rhizo-nogpu", off)) if len(k) < KEEP_FLOOR]
    if short:
        return [f"no comparison: {', '.join(short)}, under the floor of {KEEP_FLOOR}"], 4
    lines = []
    for a in ARMS:
        m_on, m_off = median([r[a]["missed_frac"] for r in on]), median([r[a]["missed_frac"] for r in off])
        lines.append(f"reported: {a}, median missed_frac without the GPU over with it {_r(m_off / m_on if m_on else None)}, "
                     f"{len(off)} and {len(on)} rounds kept")
    return lines, 0


def selftest():
    """score built batches that pass, fail and void each rule"""
    def fact(level, adpf, missed_frac, **kw):
        """build a run's facts with every rule passing"""
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
        """build n rounds of the six arms, the arm in bad given the facts in it"""
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
        """return the first word after a prediction's name"""
        return next((l.split()[1].rstrip(":") for l in lines if l.startswith(p + " ")), None)
    cases = []
    lines, rc = score(batch())
    cases.append(("a passing batch scores", rc == 0 and [verdict(lines, p) for p in ("P1", "P2", "P3", "P4", "P5")]
                  == ["HIT", "HIT", "HIT", "HIT", "HIT"]))
    cases.append(("a grant grown by half with no recovery reads as hides",
                  any(l.startswith("interpretation: hides: the granted bandwidth rose") for l in lines)))
    cases.append(("the grant line is the compute thread's granted bandwidth sampled across the loop, PC load over no load",
                  "reported: the compute thread's granted bandwidth across the loop, PC load over no load 1.5000" in lines))
    cases.append(("no absolute time is printed", not any("_ns" in l or " ms" in l for l in lines)))
    cases.append(("the VM ratio is told time over the three deadline threads against the time every vCPU was held",
                  any(l.startswith("reported: VM load, held time Android was told of, summed over the three deadline "
                                   "threads, over the time the load held every vCPU inside the run ") for l in lines)))
    cases.append(("the PC line names its window: the load's own CPU time on four CPUs across its whole run",
                  any(l.startswith("reported: PC load, held time Android was told of over the load's own CPU time on four CPUs "
                                   "across its whole run ") for l in lines)))
    cases.append(("torn records and frames the recorder lost are pooled over every kept run",
                  "reported: all kept runs, torn records 0 and frames the telemetry recorder lost 144, of 576000 frames sent"
                  in lines))
    lines, rc = score(batch(pc_no=0.0, pc_adpf=0.0))
    cases.append(("P5 with no misses without ADPF under the PC load is an undefined MISS: no evidence",
                  verdict(lines, "P5") == "MISS" and any(l.startswith("P5") and "undefined" in l and "no evidence" in l
                                                          for l in lines)))
    lines, rc = score(batch(n=11))
    cases.append(("eleven rounds is VOID", rc == 4 and any(l.startswith("VOID") for l in lines)))
    lines, rc = score(batch(bad=("rhizo-vmload-adpf", {"exclusive_end": 0})))
    cases.append(("a run not idle at its end drops its round",
                  rc == 0 and lines[0].startswith("rounds kept 15 of 16") and any("host not idle at the end" in l for l in lines)))
    lines, rc = score(batch(bad=("rhizo-pcload-adpf", {"inject_cpu_ns": 22_000_000_000})))
    cases.append(("a PC load below 90% of its duty on four CPUs drops the round: one CPU's worth does not arrive",
                  lines[0].startswith("rounds kept 15") and any("the load did not arrive" in l for l in lines)))
    lines, rc = score(batch(bad=("rhizo-vmload-adpf", {"sessions_paused": 2})))
    cases.append(("an adpf run whose app's sessions were paused drops its round",
                  lines[0].startswith("rounds kept 15") and any("vmload-adpf: the app's sessions were paused" in l for l in lines)))
    lines, rc = score(batch(bad=("rhizo-pcload-adpf", {"grant_refusals": 40})))
    cases.append(("refused grants are rt.budgets' own behaviour and keep the round", lines[0].startswith("rounds kept 16")))
    lines, rc = score(batch(bad=("rhizo-vmload-noadpf", {"injected_ns": 10_000_000_000})))
    cases.append(("a VM load below 90% of its duty drops the round", lines[0].startswith("rounds kept 15")))
    lines, rc = score(batch(bad=("rhizo-pcload-noadpf", {"inject_cpu_ns": 1_000_000_000})))
    cases.append(("a PC load below 90% of its duty drops the round", lines[0].startswith("rounds kept 15")))
    lines, rc = score(batch(vm_no=0.0, vm_adpf=0.0))
    cases.append(("P4 with no misses without ADPF is an undefined MISS",
                  verdict(lines, "P4") == "MISS" and any(l.startswith("P4") and "undefined" in l for l in lines)))
    b = batch()
    for r in b["rounds"]:
        r["rhizo-vmload-noadpf"]["missed_held"] = 2
    lines, rc = score(b)
    cases.append(("P1 with fewer than 50 held frames is a MISS", verdict(lines, "P1") == "MISS"))
    lines, rc = score(batch(vm_adpf=0.2))
    cases.append(("a VM load with ADPF above no ADPF in every round is a P4 MISS with 0 down",
                  verdict(lines, "P4") == "MISS" and any(l.startswith("P4") and "0 down / 16 up" in l for l in lines)))
    lines, rc = score(batch(pc_adpf=0.001))
    cases.append(("a recovery under the PC load misses P5 and reads as helps",
                  verdict(lines, "P5") == "MISS" and any(l.startswith("interpretation: helps") for l in lines)))
    lines, rc = score(batch(grant_pc=0.06))
    cases.append(("an unchanged grant with no recovery reads as neither",
                  any(l.startswith("interpretation: neither") for l in lines)))
    cases.append(("a prefixed label is read as its arm", arm_of("one-round-rhizo-pcload-adpf") == "pcload-adpf"
                  and arm_of("rhizo-pcload-adpf") == "pcload-adpf" and arm_of("pc-r4-control") is None))

    def with_gpu(d, gpu, renderer):
        """set every run's gpu and renderer"""
        for r in d["rounds"]:
            for f in r.values():
                f.update(gpu=gpu, renderer=renderer)
        return d
    lines, rc = score(with_gpu(batch(), "off", "llvmpipe"), gpu="off")
    cases.append(("a gpu-off batch of runs rendered on llvmpipe is scored", rc == 0))
    d = with_gpu(batch(), "off", "llvmpipe")
    d["rounds"][0]["rhizo-vmload-adpf"]["renderer"] = "virgl"
    lines, rc = score(d, gpu="off")
    cases.append(("a gpu-off batch holding a run rendered on virgl is VOID, naming the run",
                  rc == 4 and any("rhizo-vmload-adpf" in l and "virgl" in l for l in lines)))
    lines, rc = score(with_gpu(batch(), "on", "virgl"), gpu="off")
    cases.append(("a gpu-on batch scored as gpu off is VOID", rc == 4))
    lines, rc = score(batch())
    cases.append(("a batch whose runs carry no gpu keys still scores as gpu on", rc == 0))
    lines, rc = score(batch(), gpu="off")
    cases.append(("a batch whose runs carry no gpu keys scored as gpu off is VOID", rc == 4))
    on, off = with_gpu(batch(), "on", "virgl"), with_gpu(batch(vm_no=0.10), "off", "llvmpipe")
    lines, rc = compare(on, off)
    vm_line = next((l for l in lines if l.startswith("reported: vmload-noadpf,")), "")
    cases.append(("compare prints one reported line per arm, the GPU-off median over the GPU-on one, with the rounds kept",
                  rc == 0 and len(lines) == len(ARMS) and "16 and 16 rounds kept" in vm_line
                  and abs(float(vm_line.split(" over with it ")[1].split(",")[0]) - median(
                      [0.10 + i * 1e-4 for i in range(16)]) / median([0.05 + i * 1e-4 for i in range(16)])) < 1e-4))
    cases.append(("compare reads a GPU-on median of 0 as undefined",
                  any(l.startswith("reported: noload-noadpf,") and "undefined" in l for l in lines)))
    bad_off = with_gpu(batch(), "off", "llvmpipe")
    bad_off["rounds"][2]["rhizo-pcload-adpf"]["renderer"] = "virgl"
    lines, rc = compare(on, bad_off)
    cases.append(("compare refuses a GPU-off batch holding a run rendered on virgl", rc == 4 and "rhizo-nogpu" in lines[0]))
    lines, rc = compare(on, with_gpu(batch(n=11), "off", "llvmpipe"))
    cases.append(("compare refuses a batch under the floor, naming it",
                  rc == 4 and "rhizo-nogpu kept 11 rounds" in lines[0] and "rhizo kept" not in lines[0]))
    with contextlib.redirect_stderr(io.StringIO()) as err:
        rc = main(["--compare", "/nonexistent-a.json", "/nonexistent-b.json"])
    cases.append(("compare with an unreadable file exits 2, saying which", rc == 2 and "/nonexistent-a.json" in err.getvalue()))

    def drifted(d):
        """give every run of a built batch the drift batch's own facts"""
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
    cases.append(("a drift batch prints the CPU-per-frame line",
                  "reported: the compute thread's CPU per frame by rhizo-rtd's own samples, VM load over no load 1.8000, "
                  "PC load over no load 1.6000" in lines))
    cases.append(("a drift batch holds rhizo-rtd's CPU per frame against the compute stage's own time per frame in each ADPF arm",
                  "reported: rhizo-rtd's CPU per frame for the compute thread over the compute stage's own time per frame, "
                  "no load 1.0345, VM load 1.8621, PC load 1.6552" in lines))
    cases.append(("a drift batch prints the steal line",
                  "reported: PC load, the guest's steal over its four vCPUs' time across the loop, without ADPF 0.2000, "
                  "with ADPF 0.2500" in lines))
    cases.append(("a drift batch counts the ADPF runs whose telemetry ran under SCHED_DEADLINE",
                  "reported: ADPF runs whose telemetry stage ran under SCHED_DEADLINE 48/48" in lines))
    cases.append(("a drift batch prints the VM-load ADPF recorder line",
                  "reported: VM load with ADPF, frames the telemetry recorder lost 0, of 96000 frames sent" in lines))
    cases.append(("the drift lines come after the batch's own reported lines and before the interpretation",
                  [l.split(":")[0] for l in lines[-7:]] == ["reported"] * 6 + ["interpretation"]))
    slow = drifted(batch())
    slow["rounds"][3]["rhizo-vmload-noadpf"]["slot_us"] = "100000"
    lines, rc = score(slow, drift=True)
    cases.append(("a drift batch with one run at slot 100000 is VOID and names it",
                  rc == 4 and any(l.startswith("VOID: run rhizo-vmload-noadpf round 4 has slot_us=100000, not 97000") for l in lines)))
    stock = drifted(batch())
    stock["rounds"][5]["rhizo-pcload-adpf"]["guest_kernel"] = "stock"
    lines, rc = score(stock, drift=True)
    cases.append(("a drift batch with one run on the stock guest kernel is VOID and names it",
                  rc == 4 and any(l.startswith("VOID: run rhizo-pcload-adpf round 6 has guest_kernel=stock, not steal") for l in lines)))
    nokernel = drifted(batch())
    del nokernel["rounds"][0]["rhizo-noload-noadpf"]["guest_kernel"]
    cases.append(("a drift run that recorded no guest kernel counts as stock", score(nokernel, drift=True)[1] == 4))
    cases.append(("an old batch is not checked for its guest kernel", score(batch())[1] == 0))
    one_other = drifted(batch())
    one_other["rounds"][0]["rhizo-vmload-adpf"]["telemetry_policy"] = "other"
    cases.append(("an ADPF run whose telemetry stayed other is counted, not dropped",
                  "reported: ADPF runs whose telemetry stage ran under SCHED_DEADLINE 47/48" in score(one_other, drift=True)[0]))
    no_steal = drifted(batch())
    for r in no_steal["rounds"]:
        for f in r.values():
            f["loop_steal_ns"], f["loop_span_ns"] = -1, 0
    cases.append(("missing counters print undefined steal",
                  any(l.endswith("without ADPF undefined, with ADPF undefined") for l in score(no_steal, drift=True)[0])))
    zero = drifted(batch())
    for r in zero["rounds"][:9]:
        r["rhizo-vmload-adpf"]["compute_frame_cpu_ns"] = 0
    cases.append(("runs with no CPU per frame are left out of its median, not counted as zero",
                  any(l.startswith("reported: the compute thread's CPU per frame by rhizo-rtd's own samples, VM load over no load 1.8000")
                      for l in score(zero, drift=True)[0])))
    no_work = drifted(batch())
    for r in no_work["rounds"][:9]:
        r["rhizo-pcload-adpf"]["compute_work_ns"] = 0
    cases.append(("runs with no compute time per frame are left out of that line's median",
                  any(l.endswith("PC load 1.6552") and "own time per frame" in l for l in score(no_work, drift=True)[0])))
    cases.append(("an old batch prints no drift line", not any("CPU per frame" in l for l in score(drifted(batch()))[0])))
    with contextlib.redirect_stderr(io.StringIO()):
        main_rc = main([str(Path(__file__).resolve()), "--drift"])
    cases.append(("--drift with an unreadable file still exits 2", main_rc == 2))
    for name, gpu in (("rhizo", "on"), ("rhizo-nogpu", "off")):
        p = ROOT / "results" / "rhizo" / f"{name}.json"
        if p.exists():
            fz = json.loads(p.read_text())
            want = [l for l in fz["score_text"].splitlines() if not l.startswith("scored at ")]
            cases.append((f"frozen {name} re-scores to its frozen score text", score(fz, gpu=gpu)[0] == want))
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'ok' if ok else 'FAIL'} {n}")
    print(f"rhizo score selftest: {len(cases) - len(bad)}/{len(cases)} ok")
    return 1 if bad else 0


def read(path):
    """return a paired file's contents, or None after saying why it cannot be read"""
    try:
        d = json.loads(Path(path).read_text())
        assert isinstance(d.get("rounds"), list)
        return d
    except (OSError, ValueError, AssertionError) as e:
        print(f"rhizo score: cannot read {path}: {e}", file=sys.stderr)
        return None


def main(argv=None):
    """score a paired file, or run the selftest"""
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
        print(__doc__, file=sys.stderr)
        return 2
    d = read(argv[0])
    if d is None:
        return 2
    lines, rc = score(d, gpu, drift)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
