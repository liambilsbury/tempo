#!/usr/bin/env python3
"""Print the per-run checks of a paired batch and its round counts

Usage: per_run_checks.py <paired.json> <label...>
       per_run_checks.py --attempted <paired.json> <n>
       per_run_checks.py --status <paired.json> <n> <label...>
       per_run_checks.py --complete <paired.json> <n> <label...>
       per_run_checks.py --strip-prefix <prefix> <in.json> <out.json>
       per_run_checks.py --r2-ratio <paired.json>
  no option        per-run checks
  --attempted      whether n rounds were attempted
  --status         round counts
  --complete       whether n rounds are complete
  --strip-prefix   drop <prefix> from every arm label
  --r2-ratio       R2 adaptive over fixed ratio"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "results" / "experiments"
sys.path.insert(0, str(ROOT))
from benchmark import lib   # noqa: E402
OFF_PIN_OLD = [o + "_busy_cpus" for o in lib.old_names("off_pin")]
LEFTOVER_OLD = lib.old_names("leftover_ipi_probes")
COLS = ("run", "exclusive", "exclusive_end", "off_pin_busy_cpus", "probe_covers_inject",
        "probe_lost", "hit", "topology", "kernel_image_preempt", "probe", "leftover_ipi_probes")


def load(path):
    """load a paired batch, or None when it is unreadable"""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("rounds"), list) else None


def complete_rounds(d, labels):
    """count the rounds that hold every label"""
    return sum(1 for r in d["rounds"]
               if isinstance(r, dict) and all(isinstance(r.get(l), dict) for l in labels))


def kv(path):
    """parse the key=value tokens of a meta file, keeping the first of each key"""
    out = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for tok in text.split():
        k, sep, v = tok.partition("=")
        if sep and k not in out:
            out[k] = v
    return out


def rows(d, labels, runs=RUNS):
    """yield the check row of each run directory"""
    for i, r in enumerate(d["rounds"]):
        for label in labels:
            run = f"{label}-r{i}"
            host = kv(runs / run / "host.meta")
            probe = kv(runs / run / "probe.meta")
            kern = kv(runs / run / "l2" / "kernel.meta")
            fact = r.get(label) if isinstance(r, dict) else None
            hit = fact.get("hit", "-") if isinstance(fact, dict) else "-"
            yield (run, host.get("exclusive", "-"), host.get("exclusive_end", "-") or "-",
                   next((host[k] for k in ["off_pin_busy_cpus"] + OFF_PIN_OLD if k in host), "-"), probe.get("probe_covers_inject", "-"),
                   probe.get("probe_lost", "-"), hit, host.get("topology", "-"),
                   kern.get("kernel_image_preempt", "-"), probe.get("probe", "-"),
                   next((probe[k] for k in ["leftover_ipi_probes"] + LEFTOVER_OLD if k in probe), "-"))


def strip_prefix(prefix, src, dst):
    """copy a batch with a prefix removed from every arm label"""
    d = load(src)
    if d is None:
        print(f"run_checks: {src} is not a paired batch", file=sys.stderr)
        return 1
    d["rounds"] = [{(k[len(prefix):] if k.startswith(prefix) else k): v for k, v in r.items()}
                   if isinstance(r, dict) else r for r in d["rounds"]]
    Path(dst).write_text(json.dumps(d, indent=1))
    return 0


def r2_ratio(path):
    """print the adaptive over fixed-hi-probe over_deadline_frac ratio"""
    sys.path.insert(0, str(ROOT))
    from benchmark.score.nested.quality_shedding import ARMS, arm_median
    d = load(path)
    if d is None:
        print(f"R2 ratio adaptive / fixed-hi-probe over_deadline_frac: - no batch at {path}")
        return 0
    kept = [r for r in d["rounds"] if isinstance(r, dict)
            and all(isinstance(r.get(a), dict) and r[a].get("hit") for a in ARMS)]
    ad = arm_median(kept, "adaptive", "over_deadline_frac") if kept else None
    pr = arm_median(kept, "fixed-hi-probe", "over_deadline_frac") if kept else None
    if ad is None or pr is None or not pr:
        print(f"R2 ratio adaptive / fixed-hi-probe over_deadline_frac: - "
              f"{len(kept)} kept round(s); medians {ad} / {pr}")
        return 0
    print(f"R2 ratio adaptive / fixed-hi-probe over_deadline_frac: {ad} / {pr} = {ad / pr:.3f} "
          f"over {len(kept)} kept round(s); pre-registered: at most 0.5")
    return 0


def main(argv):
    """run the subcommand named by argv"""
    if len(argv) == 3 and argv[0] == "--attempted":
        d = load(argv[1])
        return 0 if d is not None and argv[2].isdigit() and len(d["rounds"]) >= int(argv[2]) else 1
    if len(argv) >= 4 and argv[0] == "--status":
        d = load(argv[1])
        labels = argv[3:]
        if d is None:
            print("attempted=0 complete=0 failed_runs=0")
            return 0
        a, c = len(d["rounds"]), complete_rounds(d, labels)
        k = sum(1 for r in d["rounds"] for l in labels
                if not (isinstance(r, dict) and isinstance(r.get(l), dict)))
        print(f"attempted={a} complete={c} failed_runs={k}")
        return 0
    if len(argv) >= 3 and argv[0] == "--complete":
        d = load(argv[1])
        try:
            n = int(argv[2])
        except ValueError:
            print(__doc__, file=sys.stderr)
            return 2
        labels = argv[3:]
        if not labels:
            print(__doc__, file=sys.stderr)
            return 2
        return 0 if d is not None and complete_rounds(d, labels) >= n else 1
    if len(argv) == 4 and argv[0] == "--strip-prefix":
        return strip_prefix(*argv[1:])
    if len(argv) == 2 and argv[0] == "--r2-ratio":
        return r2_ratio(argv[1])
    if len(argv) < 2 or argv[0].startswith("--"):
        print(__doc__, file=sys.stderr)
        return 2
    d = load(argv[0])
    labels = argv[1:]
    if d is None:
        print(" ".join(COLS))
        print(f"complete_rounds=0 no readable batch at {argv[0]}")
        return 1
    runs = Path(argv[0]).resolve().parent
    table = [COLS] + [tuple(str(x) for x in row) for row in rows(d, labels, runs)]
    w = [max(len(t[i]) for t in table) for i in range(len(COLS))]
    for t in table:
        print("  ".join(c.ljust(w[i]) for i, c in enumerate(t)).rstrip())
    print(f"complete_rounds={complete_rounds(d, labels)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
