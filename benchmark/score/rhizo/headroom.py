#!/usr/bin/env python3
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
USAGE = """usage: headroom.py <paired.json>
       headroom.py --selftest"""


def arm_of(label):
    return next((a for a in ARMS if label == f"headroom-rhizo-{a}" or label.endswith(f"-headroom-rhizo-{a}")), None)


def not_kept(f):
    if not isinstance(f, dict):
        return ["no run"]
    why = rhizo_score.not_kept(f)
    if f.get("headroom_done") != 1:
        why.append("sampler unfinished")
    if f.get("headroom_answers", 0) < MIN_ANSWERS:
        why.append(f"answers < {MIN_ANSWERS}")
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


def headroom_disagreements(d):
    return [f"run {label} round {i + 1} has headroom={f.get('headroom', 'off') if isinstance(f, dict) else 'none'}, not on"
            for i, r in enumerate(d["rounds"]) for label, f in r.items()
            if not isinstance(f, dict) or f.get("headroom") != "on"]


def drop_ratio(keep, arm):
    drops = [r["noload"]["headroom_median"] - r[arm]["headroom_median"] for r in keep]
    p, up, down = sign_test(drops)
    return median(drops) / LOAD_POINTS, p, up, down


def _r(x):
    return "undefined" if x is None else f"{x:.4f}"


def score(d):
    for off in (rhizo_score.gpu_disagreements(d, "on"), rhizo_score.slot_disagreements(d, rhizo_score.DRIFT_SLOT_US),
                rhizo_score.kernel_disagreements(d, rhizo_score.DRIFT_GUEST_KERNEL), headroom_disagreements(d)):
        if off:
            return [f"VOID: {l}" for l in off], 4
    keep, notes = rounds_kept(d)
    lines = [f"rounds kept {len(keep)} of {len(d['rounds'])} (floor {KEEP_FLOOR})"] + [f"not kept: {n}" for n in notes]
    if len(keep) < KEEP_FLOOR:
        return lines + [f"VOID: kept < {KEEP_FLOOR}"], 4
    vm, pvm, upvm, dnvm = drop_ratio(keep, "vmload")
    ok1 = VM_LOW <= vm <= VM_HIGH
    lines.append(f"H1 {'HIT' if ok1 else 'MISS'}: VM drop/{LOAD_POINTS} {_r(vm)} "
                 f"[{VM_LOW}, {VM_HIGH}] sign p={pvm:.4f} {upvm} down / {dnvm} up")
    pc, ppc, uppc, dnpc = drop_ratio(keep, "pcload")
    ok2 = pc <= PC_BAR
    lines.append(f"H2 {'HIT' if ok2 else 'MISS'}: PC drop/{LOAD_POINTS} {_r(pc)} "
                 f"<= {PC_BAR} sign p={ppc:.4f} {uppc} down / {dnpc} up")
    base = median([r["noload"]["headroom_median"] for r in keep])
    lines.append(f"reported: noload headroom/100 {_r(base / 100)}")
    runs = [r["pcload"] for r in keep if r["pcload"].get("loop_steal_ns", -1) >= 0 and r["pcload"].get("loop_span_ns", 0) > 0]
    span = sum(4 * f["loop_span_ns"] for f in runs)
    steal = sum(f["loop_steal_ns"] for f in runs) / span if span else None
    lines.append(f"reported: PC steal {_r(steal)}, "
                 f"share {_r(steal * 100 / LOAD_POINTS if steal is not None else None)}")
    for a in ARMS:
        answers = sum(r[a]["headroom_answers"] for r in keep)
        asked = answers + sum(r[a]["headroom_nan"] for r in keep)
        lines.append(f"reported: {a}, answers {answers}/{asked}")
    return lines, 0


def selftest():
    def fact(level, headroom, **kw):
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
        rounds = []
        for i in range(n):
            r = {f"{prefix}headroom-rhizo-noload": fact("none", 97.0 + i * 0.01),
                 f"{prefix}headroom-rhizo-vmload": fact("vm", vm), f"{prefix}headroom-rhizo-pcload": fact("pc", pc)}
            if bad and i == 0:
                r[f"{prefix}headroom-rhizo-{bad[0]}"].update(bad[1])
            rounds.append(r)
        return {"topology": "single-level", "metric": "missed_frac", "rounds": rounds}

    def verdict(lines, p):
        return next((l.split()[1].rstrip(":") for l in lines if l.startswith(p + " ")), None)
    cases = []
    lines, rc = score(batch())
    cases.append(("vm=67 pc=85: H1 HIT, H2 HIT",
                  rc == 0 and (verdict(lines, "H1"), verdict(lines, "H2")) == ("HIT", "HIT")))
    cases.append(("H1 ratio (noload-vmload)/30: 1.0025",
                  any(l.startswith("H1 HIT: VM drop/30 1.0025") for l in lines)))
    cases.append(("pcload steal line: 0.1667, share 0.5556",
                  "reported: PC steal 0.1667, share 0.5556" in lines))
    cases.append(("vmload answers line: 800/3200",
                  "reported: vmload, answers 800/3200" in lines))
    cases.append(("no _ns/ms in output", not any("_ns" in l or " ms" in l for l in lines)))
    lines, rc = score(batch(vm=80.0))
    cases.append(("vm=80 (0.57): H1 MISS", verdict(lines, "H1") == "MISS"))
    lines, rc = score(batch(vm=55.0))
    cases.append(("vm=55 (1.4): H1 MISS", verdict(lines, "H1") == "MISS"))
    lines, rc = score(batch(pc=70.0))
    cases.append(("pc=70 (0.9): H2 MISS", verdict(lines, "H2") == "MISS"))
    lines, rc = score(batch(n=11))
    cases.append(("n=11: VOID", rc == 4 and lines[-1].startswith("VOID: kept < 12")))
    lines, rc = score(batch(bad=("vmload", {"headroom_answers": 9})))
    cases.append(("headroom_answers=9: round dropped",
                  rc == 0 and lines[0].startswith("rounds kept 15 of 16") and any("answers < 10" in l for l in lines)))
    lines, rc = score(batch(bad=("pcload", {"headroom_answers": 10})))
    cases.append(("headroom_answers=10: round kept", rc == 0 and lines[0].startswith("rounds kept 16 of 16")))
    lines, rc = score(batch(bad=("pcload", {"headroom_done": 0})))
    cases.append(("headroom_done=0: round dropped", any("sampler unfinished" in l for l in lines)))
    lines, rc = score(batch(bad=("noload", {"exclusive_end": 0})))
    cases.append(("exclusive_end=0: round dropped", any("busy end" in l for l in lines)))
    lines, rc = score(batch(bad=("pcload", {"inject_cpu_ns": 22_000_000_000})))
    cases.append(("pcload inject_cpu_ns=22e9: round dropped", any("no arrival" in l for l in lines)))
    lines, rc = score(batch(bad=("vmload", {"headroom": "off"})))
    cases.append(("headroom=off: VOID", rc == 4 and lines[0].startswith("VOID: run headroom-rhizo-vmload")))
    lines, rc = score(batch(bad=("noload", {"slot_us": 100000})))
    cases.append(("slot_us=100000: VOID", rc == 4 and "slot_us=100000" in lines[0]))
    lines, rc = score(batch(bad=("vmload", {"guest_kernel": "stock"})))
    cases.append(("guest_kernel=stock: VOID", rc == 4 and "guest_kernel=stock" in lines[0]))
    lines, rc = score(batch(bad=("noload", {"renderer": "llvmpipe"})))
    cases.append(("renderer=llvmpipe: VOID", rc == 4 and "renderer=llvmpipe" in lines[0]))
    lines, rc = score(batch(prefix="one-round-"))
    cases.append(("prefix one-round-: H1 HIT", rc == 0 and verdict(lines, "H1") == "HIT"))
    cases.append(("arm_of(drift-*, rhizo-noload): None", arm_of("drift-rhizo-noload-adpf") is None and arm_of("rhizo-noload") is None))
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'ok' if ok else 'FAIL'} {n}")
    print(f"selftest {len(cases) - len(bad)}/{len(cases)} ok")
    return 1 if bad else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        d = json.loads(Path(argv[0]).read_text())
        lines, rc = score(d)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
