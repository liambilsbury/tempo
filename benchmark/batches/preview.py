#!/usr/bin/env python3
import argparse
import sys

REPEATS = 6
FRAMES = 6000
INJECT_S = 8
NAME = {"l0": "pc-pv0", "l1": "pc-pv1", "l2": "pcn-pv2"}
DUTIES = {"l1": (35, 44, 50), "l2": (2, 10, 30)}
ARMS = ("fixed-hi-probe", "adaptive", "adaptive-rs", "fixed-lo")
ARM_ENV = {"fixed-hi-probe": "TEMPO_TIER=0,TEMPO_ADAPT=observe", "adaptive": "TEMPO_TIER=0,TEMPO_ADAPT=enforce",
           "adaptive-rs": "TEMPO_TIER=0,TEMPO_ADAPT=enforce-rs", "fixed-lo": "TEMPO_TIER=3,TEMPO_ADAPT=off"}
CLASSES = (("wake", "wake", 5000), ("tlb", "tlb", 2000), ("tlbq", "tlbq", 500), ("cache", "cache", 0))
L0_DUTY = 44
RT_DUTY = 44
LOOP = "TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=125"
OBSERVE = ARM_ENV["fixed-hi-probe"]


def conditions(level, kernel_dir="build", prefix=""):
    n = prefix + NAME[level]
    if level == "l0":
        e = f"TEMPO_LEVEL=host,{LOOP},{OBSERVE}"
        out = [f"{n}-control:none:0:{e}", f"{n}-hog-d{L0_DUTY}:cpu-load:{L0_DUTY}:{e}"]
        return out + [f"{n}-{lab}:{mode}:{arg}:{e}" for lab, mode, arg in CLASSES]
    guest = f"{LOOP},TEMPO_KERNEL={kernel_dir}/kernel-nonrt/vmlinuz"
    out = [f"{n}-control:none:0:{guest},{OBSERVE}"]
    out += [f"{n}-{a}-d{d}:cpu-load:{d}:{guest},{ARM_ENV[a]}" for d in DUTIES[level] for a in ARMS]
    out += [f"{n}-{lab}:{mode}:{arg}:{guest},{OBSERVE}" for lab, mode, arg in CLASSES]
    if level == "l1":
        out.append(f"{n}-rt-d{RT_DUTY}:cpu-load:{RT_DUTY}:{LOOP},TEMPO_KERNEL={kernel_dir}/kernel-rt/vmlinuz,{OBSERVE}")
    return out


def labels(level, prefix=""):
    return [c.split(":")[0] for c in conditions(level, prefix=prefix)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("conditions", "labels", "name", "repeats", "frames", "inject-s"))
    ap.add_argument("level", nargs="?", choices=tuple(NAME))
    ap.add_argument("--kernel-dir", default="build")
    ap.add_argument("--prefix", default="")
    a = ap.parse_args()
    if a.what in ("conditions", "labels", "name") and not a.level:
        ap.error("needs level")
    if a.what == "conditions":
        print("\n".join(conditions(a.level, a.kernel_dir, a.prefix)))
    elif a.what == "labels":
        print(" ".join(labels(a.level, a.prefix)))
    elif a.what == "name":
        print(NAME[a.level])
    else:
        print({"repeats": REPEATS, "frames": FRAMES, "inject-s": INJECT_S}[a.what])
    return 0


if __name__ == "__main__":
    sys.exit(main())
