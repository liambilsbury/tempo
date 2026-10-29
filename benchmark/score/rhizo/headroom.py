#!/usr/bin/env python3
"""Score the Rhizo headroom batch

Usage: headroom.py <paired.json>
       headroom.py --selftest
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark.run.paired import median, sign_test  # noqa: E402
from benchmark.score.rhizo import score as rhizo_score  # noqa: E402

ARMS = ("noload", "vmload", "pcload")
KEEP_FLOOR = rhizo_score.KEEP_FLOOR
LOAD_POINTS = 30
MIN_ANSWERS = 10
VM_LOW, VM_HIGH, PC_BAR = 0.8, 1.2, 0.75


def arm_of(label):
    """return the arm a label ends with, or None"""
    return next((a for a in ARMS if label == f"headroom-rhizo-{a}" or label.endswith(f"-headroom-rhizo-{a}")), None)


def not_kept(f):
    """return why a headroom run is not kept, empty when it is"""
    if not isinstance(f, dict):
        return ["no run"]
    why = rhizo_score.not_kept(f)
    if f.get("headroom_done") != 1:
        why.append("the headroom sampler did not finish")
    if f.get("headroom_answers", 0) < MIN_ANSWERS:
        why.append(f"fewer than {MIN_ANSWERS} headroom answers after the first 5 s")
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


def headroom_disagreements(d):
    """return a line per run that did not run the headroom sampler"""
    return [f"run {label} round {i + 1} has headroom={f.get('headroom', 'off') if isinstance(f, dict) else 'none'}, not on"
            for i, r in enumerate(d["rounds"]) for label, f in r.items()
            if not isinstance(f, dict) or f.get("headroom") != "on"]


def drop_ratio(keep, arm):
    """return an arm's median drop ratio, the sign test p and the counts"""
    drops = [r["noload"]["headroom_median"] - r[arm]["headroom_median"] for r in keep]
    p, up, down = sign_test(drops)
    return median(drops) / LOAD_POINTS, p, up, down


def _r(x):
    """format a ratio, or say it is undefined"""
    return "undefined" if x is None else f"{x:.4f}"


def score(d):
    """return the verdict lines and 0, or the VOID lines and 4"""
    for off in (rhizo_score.gpu_disagreements(d, "on"), rhizo_score.slot_disagreements(d, rhizo_score.DRIFT_SLOT_US),
                rhizo_score.kernel_disagreements(d, rhizo_score.DRIFT_GUEST_KERNEL), headroom_disagreements(d)):
        if off:
            return [f"VOID: {l}" for l in off], 4
    keep, notes = rounds_kept(d)
    lines = [f"rounds kept {len(keep)} of {len(d['rounds'])} (floor {KEEP_FLOOR})"] + [f"not kept: {n}" for n in notes]
    if len(keep) < KEEP_FLOOR:
        return lines + [f"VOID: fewer than {KEEP_FLOOR} rounds kept; no prediction is scored"], 4
    vm, pvm, upvm, dnvm = drop_ratio(keep, "vmload")
    ok1 = VM_LOW <= vm <= VM_HIGH
    lines.append(f"H1 {'HIT' if ok1 else 'MISS'}: VM load, the headroom drop over the load's {LOAD_POINTS} points {_r(vm)} "
                 f"(a HIT is from {VM_LOW} to {VM_HIGH}), sign test p={pvm:.4f}, {upvm} down / {dnvm} up")
    pc, ppc, uppc, dnpc = drop_ratio(keep, "pcload")
    ok2 = pc <= PC_BAR
    lines.append(f"H2 {'HIT' if ok2 else 'MISS'}: PC load, the headroom drop over the load's {LOAD_POINTS} points {_r(pc)} "
                 f"(a HIT is at most {PC_BAR}), sign test p={ppc:.4f}, {uppc} down / {dnpc} up")
    base = median([r["noload"]["headroom_median"] for r in keep])
    lines.append(f"reported: no load, the median headroom over 100 {_r(base / 100)}")
    runs = [r["pcload"] for r in keep if r["pcload"].get("loop_steal_ns", -1) >= 0 and r["pcload"].get("loop_span_ns", 0) > 0]
    span = sum(4 * f["loop_span_ns"] for f in runs)
    steal = sum(f["loop_steal_ns"] for f in runs) / span if span else None
    lines.append(f"reported: PC load, the guest's steal over its four vCPUs' time across the loop {_r(steal)}, "
                 f"over the load's share {_r(steal * 100 / LOAD_POINTS if steal is not None else None)}")
    for a in ARMS:
        answers = sum(r[a]["headroom_answers"] for r in keep)
        asked = answers + sum(r[a]["headroom_nan"] for r in keep)
        lines.append(f"reported: {a}, headroom answers after the first 5 s that were a number {answers}/{asked}")
    lines.append(f"interpretation: the guest's headroom showed {_r(vm)} of the VM load and {_r(pc)} of the PC load")
    return lines, 0


def selftest():
    """score built batches that pass, fail and void each rule"""
    def fact(level, headroom, **kw):
        """build a run's facts with every rule passing"""
        vm, pc = level == "vm", level == "pc"
        f = dict(level=level, adpf="off", duty=0 if level == "none" else 30, load_s=75, span_ns=60_000_000_000,
                 load_cpus=0 if level == "none" else 4, injected_ns=18_000_000_000 if vm else 0,
                 load_all_windows_ns=90_000_000_000 if vm else 0, load_held_ns=88_000_000_000 if vm else 0,
                 inject_cpu_ns=88_000_000_000 if pc else 0, exclusive_start=1, exclusive_end=1, vcpus_pinned_start=1,
                 vcpus_pinned_end=1, gpu="on", renderer="virgl", slot_us=97000, headroom="on", guest_kernel="steal",
                 headroom_done=1, headroom_answers=50, headroom_nan=150, headroom_median=headroom,
                 loop_steal_ns=40_000_000_000 if pc else 0, loop_span_ns=60_000_000_000)
        f.update(kw)
        return f

    def batch(n=16, vm=67.0, pc=85.0, bad=None, prefix=""):
        """build n rounds of the three arms, the arm in bad given the facts in it"""
        rounds = []
        for i in range(n):
            r = {f"{prefix}headroom-rhizo-noload": fact("none", 97.0 + i * 0.01),
                 f"{prefix}headroom-rhizo-vmload": fact("vm", vm), f"{prefix}headroom-rhizo-pcload": fact("pc", pc)}
            if bad and i == 0:
                r[f"{prefix}headroom-rhizo-{bad[0]}"].update(bad[1])
            rounds.append(r)
        return {"topology": "single-level", "metric": "missed_frac", "rounds": rounds}

    def verdict(lines, p):
        """return the first word after a prediction's name"""
        return next((l.split()[1].rstrip(":") for l in lines if l.startswith(p + " ")), None)
    cases = []
    lines, rc = score(batch())
    cases.append(("a VM load that drops headroom by its 30 points and a PC load seen at 0.4 both hit",
                  rc == 0 and (verdict(lines, "H1"), verdict(lines, "H2")) == ("HIT", "HIT")))
    cases.append(("the VM drop is the round's no-load headroom less the loaded arm's, over 30 points",
                  any(l.startswith("H1 HIT: VM load, the headroom drop over the load's 30 points 1.0025") for l in lines)))
    cases.append(("the steal line pools the PC-load runs' steal over four vCPUs' time and over the load's share",
                  "reported: PC load, the guest's steal over its four vCPUs' time across the loop 0.1667, over the load's share 0.5556"
                  in lines))
    cases.append(("the answers line counts numbers over numbers and NaNs after the first 5 s",
                  "reported: vmload, headroom answers after the first 5 s that were a number 800/3200" in lines))
    cases.append(("no absolute time is printed", not any("_ns" in l or " ms" in l for l in lines)))
    lines, rc = score(batch(vm=80.0))
    cases.append(("a VM load seen at 0.57 of its points misses H1", verdict(lines, "H1") == "MISS"))
    lines, rc = score(batch(vm=55.0))
    cases.append(("a VM load seen at 1.4 of its points misses H1", verdict(lines, "H1") == "MISS"))
    lines, rc = score(batch(pc=70.0))
    cases.append(("a PC load seen at 0.9 of its points misses H2", verdict(lines, "H2") == "MISS"))
    lines, rc = score(batch(n=11))
    cases.append(("eleven rounds is VOID", rc == 4 and lines[-1].startswith("VOID: fewer than 12")))
    lines, rc = score(batch(bad=("vmload", {"headroom_answers": 9})))
    cases.append(("a run with fewer than 10 answers drops its round",
                  rc == 0 and lines[0].startswith("rounds kept 15 of 16") and any("fewer than 10 headroom answers" in l for l in lines)))
    lines, rc = score(batch(bad=("pcload", {"headroom_answers": 10})))
    cases.append(("a run with 10 answers is kept", rc == 0 and lines[0].startswith("rounds kept 16 of 16")))
    lines, rc = score(batch(bad=("pcload", {"headroom_done": 0})))
    cases.append(("a run whose sampler did not finish drops its round", any("did not finish" in l for l in lines)))
    lines, rc = score(batch(bad=("noload", {"exclusive_end": 0})))
    cases.append(("a run not idle at its end drops its round", any("host not idle at the end" in l for l in lines)))
    lines, rc = score(batch(bad=("pcload", {"inject_cpu_ns": 22_000_000_000})))
    cases.append(("a PC load that did not arrive drops its round", any("the load did not arrive" in l for l in lines)))
    lines, rc = score(batch(bad=("vmload", {"headroom": "off"})))
    cases.append(("a run without the sampler voids the batch", rc == 4 and lines[0].startswith("VOID: run headroom-rhizo-vmload")))
    lines, rc = score(batch(bad=("noload", {"slot_us": 100000})))
    cases.append(("a run at another slot voids the batch", rc == 4 and "slot_us=100000" in lines[0]))
    lines, rc = score(batch(bad=("vmload", {"guest_kernel": "stock"})))
    cases.append(("a run on the stock guest kernel voids the batch", rc == 4 and "guest_kernel=stock" in lines[0]))
    lines, rc = score(batch(bad=("noload", {"renderer": "llvmpipe"})))
    cases.append(("a run not on virgl voids the batch", rc == 4 and "renderer=llvmpipe" in lines[0]))
    lines, rc = score(batch(prefix="one-round-"))
    cases.append(("a prefixed label is read as its arm", rc == 0 and verdict(lines, "H1") == "HIT"))
    cases.append(("a drift or old label is not a headroom arm", arm_of("drift-rhizo-noload-adpf") is None and arm_of("rhizo-noload") is None))
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'ok' if ok else 'FAIL'} {n}")
    print(f"headroom selftest: {len(cases) - len(bad)}/{len(cases)} ok")
    return 1 if bad else 0


def main(argv=None):
    """score a headroom batch's paired json, or run the selftest"""
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        d = json.loads(Path(argv[0]).read_text())
        lines, rc = score(d)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"headroom: {argv[0]}: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
