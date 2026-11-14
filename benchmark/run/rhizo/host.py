#!/usr/bin/env python3
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from benchmark import lib  # noqa: E402


def qmp_vcpus(sock, timeout=10):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(sock)
    f = s.makefile("rw")
    json.loads(f.readline())
    reply = {}
    for cmd in ("qmp_capabilities", "query-cpus-fast"):
        f.write(json.dumps({"execute": cmd}) + "\n")
        f.flush()
        while True:
            reply = json.loads(f.readline())
            if "return" in reply or "error" in reply:
                break
        if "error" in reply:
            raise RuntimeError(f"qmp {cmd}: {reply['error']}")
    s.close()
    return sorted((c["cpu-index"], c["thread-id"]) for c in reply["return"])


def plan(vcpus, tids, cpus, other):
    if len(vcpus) != len(cpus):
        raise ValueError(f"vcpus={len(vcpus)} cpus={len(cpus)}")
    pins = {tid: cpus[i] for i, (_, tid) in enumerate(sorted(vcpus))}
    for t in tids:
        pins.setdefault(t, other)
    return pins


def threads(pid):
    return sorted(int(t) for t in os.listdir(f"/proc/{pid}/task"))


def pin(sock, pid, cpus, other, state, get=os.sched_getaffinity, put=os.sched_setaffinity, vcpus=None, tids=None):
    vcpus = vcpus if vcpus is not None else qmp_vcpus(sock)
    tids = tids if tids is not None else threads(pid)
    pins = plan(vcpus, tids, cpus, other)
    prior = {str(t): sorted(get(t)) for t in pins}
    Path(state).write_text(json.dumps({"pid": pid, "vcpus": [list(v) for v in sorted(vcpus)],
                                       "pins": {str(t): c for t, c in pins.items()}, "prior": prior}, indent=1))
    for t, c in pins.items():
        put(t, {c})
    return pins


def check(state, get=os.sched_getaffinity):
    st = json.loads(Path(state).read_text())
    try:
        return int(all(sorted(get(tid)) == [st["pins"][str(tid)]] for _, tid in st["vcpus"]))
    except (OSError, KeyError):
        return 0


def restore(state, put=os.sched_setaffinity, alive=None):
    st = json.loads(Path(state).read_text())
    alive = alive or (lambda t: Path(f"/proc/{st['pid']}/task/{t}").exists())
    done = 0
    for t, cpus in st["prior"].items():
        if alive(int(t)):
            try:
                put(int(t), set(cpus))
                done += 1
            except OSError:
                pass
    return done


def qemu_pids():
    r = subprocess.run(["pgrep", "-f", "^[^ ]*qemu-system-"], capture_output=True, text=True)
    return sorted(int(p) for p in r.stdout.split())


def ceiling(vcpus, level, duty):
    return lib.LOAD_CEILING + vcpus + (vcpus * duty / 100 if level == "pc" else 0)


def exclusive(qemu_pid, vcpus, facts=None, qemus=None, level="none", duty=0):
    f = facts if facts is not None else lib.check_exclusive()
    q = qemus if qemus is not None else qemu_pids()
    top = ceiling(vcpus, level, duty)
    reasons = []
    if f["free_gb"] < lib.DISK_FLOOR_GB:
        reasons.append(f"free_disk_{f['free_gb']}G")
    if f["foreign_pipelines"]:
        reasons.append(f"{f['foreign_pipelines']}_foreign_pipelines")
    if q != [qemu_pid]:
        reasons.append("qemu_pids_" + ("_".join(map(str, q)) or "none"))
    if f["load1"] > top:
        reasons.append(f"load_{f['load1']:.2f}_above_{top:.2f}")
    return {"exclusive": 0 if reasons else 1, "why": ",".join(reasons) or "idle", "load1": round(f["load1"], 2),
            "ceiling": round(top, 2), "free_gb": f["free_gb"], "foreign_pipelines": f["foreign_pipelines"], "qemus": len(q)}


def settle(top, max_s=90, every_s=5, load=None, sleep=None):
    load = load or (lambda: os.getloadavg()[0])
    sleep = sleep or time.sleep
    waited, now = 0, load()
    while now > top and waited < max_s:
        step = min(every_s, max_s - waited)
        sleep(step)
        waited += step
        now = load()
    return waited, now


def selftest():
    cases = []
    vcpus = [(1, 501), (0, 500), (3, 503), (2, 502)]
    p = plan(vcpus, [500, 501, 502, 503, 600, 601], [8, 9, 10, 11], 12)
    cases.append(("plan: vCPU index i -> cpus[i], others -> other",
                  p == {500: 8, 501: 9, 502: 10, 503: 11, 600: 12, 601: 12}))
    try:
        plan(vcpus, [], [8, 9, 10], 12)
        cases.append(("plan 4 vCPUs, 3 cpus: ValueError", False))
    except ValueError:
        cases.append(("plan 4 vCPUs, 3 cpus: ValueError", True))
    aff = {t: {0, 1, 2, 3, 8, 9, 10, 11, 12} for t in (500, 501, 502, 503, 600)}

    def get(t):
        return set(aff[t])

    def put(t, cpus):
        aff[t] = set(cpus)
    with tempfile.TemporaryDirectory() as td:
        st = Path(td) / "vcpus.json"
        pin(None, 1, [8, 9, 10, 11], 12, st, get=get, put=put, vcpus=vcpus, tids=[500, 501, 502, 503, 600])
        saved = json.loads(st.read_text())
        cases.append(("pin: prior affinity saved", saved["prior"]["500"] == [0, 1, 2, 3, 8, 9, 10, 11, 12]))
        cases.append(("pin: affinity set", aff[502] == {10} and aff[600] == {12}))
        cases.append(("check pinned: 1", check(st, get=get) == 1))
        aff[503] = {8, 11}
        cases.append(("check vCPU moved: 0", check(st, get=get) == 0))
        n = restore(st, put=put, alive=lambda t: t != 600)
        cases.append(("restore: 4 restored, gone tid skipped",
                      n == 4 and aff[500] == {0, 1, 2, 3, 8, 9, 10, 11, 12} and aff[600] == {12}))
        cases.append(("restore twice: 4", restore(st, put=put, alive=lambda t: t != 600) == 4))
    idle = {"load1": 0.4, "free_gb": 200.0, "foreign_pipelines": 0}
    cases.append(("load1=0.4 qemus=[7]: exclusive=1", exclusive(7, 4, idle, [7])["exclusive"] == 1))
    cases.append(("load1=4.9 vcpus=4: exclusive=1", exclusive(7, 4, dict(idle, load1=4.9), [7])["exclusive"] == 1))
    for name, f, q in (("load1=5.2 vcpus=4: exclusive=0", dict(idle, load1=5.2), [7]),
                       ("qemus=[7, 8]: exclusive=0", idle, [7, 8]),
                       ("free_gb=10: exclusive=0", dict(idle, free_gb=10.0), [7]),
                       ("foreign_pipelines=1: exclusive=0", dict(idle, foreign_pipelines=1), [7])):
        r = exclusive(7, 4, f, q)
        cases.append((name, r["exclusive"] == 0 and " " not in r["why"]))
    cases.append(("ceiling pc/30=6.2, vm/30=5, none=5",
                  ceiling(4, "pc", 30) == 1 + 4 + 1.2 and ceiling(4, "vm", 30) == 5 and ceiling(4, "none", 0) == 5))
    cases.append(("pc duty=30 load1=6.1: exclusive=1",
                  exclusive(7, 4, dict(idle, load1=6.1), [7], level="pc", duty=30)["exclusive"] == 1))
    cases.append(("pc duty=30 load1=6.3: exclusive=0",
                  exclusive(7, 4, dict(idle, load1=6.3), [7], level="pc", duty=30)["exclusive"] == 0))
    cases.append(("vm duty=30 load1=6.1: exclusive=0",
                  exclusive(7, 4, dict(idle, load1=6.1), [7], level="vm", duty=30)["exclusive"] == 0))
    cases.append(("exclusive pc duty=30: ceiling=6.2", exclusive(7, 4, idle, [7], level="pc", duty=30)["ceiling"] == 6.2))
    slept = []

    def loads(*xs):
        it = iter(xs)
        last = [xs[-1]]

        def read():
            last[0] = next(it, last[0])
            return last[0]
        return read
    cases.append(("settle 4.0, top=5: (0, 4.0)", settle(5.0, load=loads(4.0), sleep=slept.append) == (0, 4.0)))
    cases.append(("settle 7,6,5, top=5: (10, 5.0), sleeps [5, 5]",
                  settle(5.0, load=loads(7.0, 6.0, 5.0), sleep=slept.append) == (10, 5.0) and slept == [5, 5]))
    slept.clear()
    cases.append(("settle 9.0, top=5: (90, 9.0), 18 sleeps", settle(5.0, load=loads(9.0), sleep=slept.append) == (90, 9.0)
                  and sum(slept) == 90 and len(slept) == 18))
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'ok' if ok else 'FAIL'} {n}")
    print(f"rhizo_host selftest: {len(cases) - len(bad)}/{len(cases)} ok")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pin")
    p.add_argument("--qmp", required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--cpus", required=True)
    p.add_argument("--other", type=int, required=True)
    p.add_argument("--state", required=True)
    for name in ("check", "restore"):
        sub.add_parser(name).add_argument("--state", required=True)
    e = sub.add_parser("exclusive")
    e.add_argument("--qemu-pid", type=int, required=True)
    t = sub.add_parser("settle")
    for p in (e, t):
        p.add_argument("--vcpus", type=int, required=True)
        p.add_argument("--level", choices=("none", "vm", "pc"), required=True)
        p.add_argument("--duty", type=float, required=True)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.cmd == "pin":
        print(f"pinned={len(pin(a.qmp, a.pid, [int(c) for c in a.cpus.split(',')], a.other, a.state))}")
    elif a.cmd == "check":
        print(f"vcpus_pinned={check(a.state)}")
    elif a.cmd == "restore":
        print(f"restored={restore(a.state)}")
    elif a.cmd == "exclusive":
        print(" ".join(f"{k}={v}" for k, v in exclusive(a.qemu_pid, a.vcpus, level=a.level, duty=a.duty).items()))
    elif a.cmd == "settle":
        top = ceiling(a.vcpus, a.level, a.duty)
        waited, now = settle(top)
        print(f"settle_s={waited} load1={now:.2f} ceiling={top:.2f}")
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
