"""Freeze and read the per-round steal recall rows"""
from pathlib import Path
import json

import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
FROZEN = ROOT / "results" / "earlier_phase" / "frozen" / "recall-frozen.json"


def kv(p):
    """parse the key=value tokens of a meta file"""
    d = {}
    if not Path(p).exists():
        return d
    for l in open(p, errors="replace"):
        for t in l.replace("INJECT ", "").split():
            if "=" in t:
                k, _, v = t.partition("=")
                d[k] = v
    return d


def scan():
    """compute the recall rows from the experiment directories"""
    out = []
    for i in range(64):
        c = ROOT / "results" / "experiments" / f"control-r{i}"
        h = ROOT / "results" / "experiments" / f"hogsync-r{i}"
        ci, hi = kv(c / "inject.meta"), kv(h / "inject.meta")
        ch, hh = kv(c / "l2" / "header.txt"), kv(h / "l2" / "header.txt")
        try:
            lost = int(ci["watched_cpu_window_ns"]) - int(hi["watched_cpu_window_ns"])
            inj = int(hi["cpu_consumed_ns"])
            ds = (int(hh["det_steal_ticks"]) - int(ch["det_steal_ticks"])) * 10_000_000
        except (KeyError, ValueError):
            continue
        if inj <= 0 or lost <= 0:
            continue
        out.append(dict(rd=i, inj=inj, lost=lost, steal=ds,
                        recall=ds / lost, land=lost / inj))
    return out


def snapshot():
    """write the scanned recall rows to the frozen file"""
    from benchmark import lib
    lib.refuse_unpublishable_experiments()
    rs = scan()
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    FROZEN.write_text(json.dumps({"rounds": rs}, indent=1))
    return rs


def rows():
    """return the frozen recall rows, or scan when there is no frozen file"""
    if FROZEN.exists():
        return json.loads(FROZEN.read_text())["rounds"]
    return scan()


if __name__ == "__main__":
    rs = snapshot()
    print(f"froze {len(rs)} rounds to {FROZEN}")
