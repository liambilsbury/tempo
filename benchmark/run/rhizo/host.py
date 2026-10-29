#!/usr/bin/env python3
"""The PC side of a Rhizo VM batch"""
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
    """return [(cpu index, thread id)] from qemu's query-cpus-fast"""
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
    """return {tid: cpu}: vCPU i on cpus[i], every other qemu thread on other"""
    if len(vcpus) != len(cpus):
        raise ValueError(f"{len(vcpus)} vCPUs but {len(cpus)} host CPUs to pin them to")
    pins = {tid: cpus[i] for i, (_, tid) in enumerate(sorted(vcpus))}
    for t in tids:
        pins.setdefault(t, other)
    return pins


def threads(pid):
    """return the thread ids of a process"""
    return sorted(int(t) for t in os.listdir(f"/proc/{pid}/task"))


def pin(sock, pid, cpus, other, state, get=os.sched_getaffinity, put=os.sched_setaffinity, vcpus=None, tids=None):
    """pin a qemu's threads, writing their prior affinity to the state file first"""
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
    """return 1 when every vCPU thread is still on its own host CPU alone"""
    st = json.loads(Path(state).read_text())
    try:
        return int(all(sorted(get(tid)) == [st["pins"][str(tid)]] for _, tid in st["vcpus"]))
    except (OSError, KeyError):
        return 0


def restore(state, put=os.sched_setaffinity, alive=None):
    """put back each pinned thread's prior affinity, skipping threads that are gone"""
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
    """return the pids of every running qemu-system process"""
    r = subprocess.run(["pgrep", "-f", "^[^ ]*qemu-system-"], capture_output=True, text=True)
    return sorted(int(p) for p in r.stdout.split())


def ceiling(vcpus, level, duty):
    """return the PC's load1 ceiling"""
    return lib.LOAD_CEILING + vcpus + (vcpus * duty / 100 if level == "pc" else 0)


def exclusive(qemu_pid, vcpus, facts=None, qemus=None, level="none", duty=0):
    """return the PC's idle check"""
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
    """wait up to max_s for load1 at or under top"""
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
    """check the pin plan, the state round trip and the idle rule on built inputs"""
    cases = []
    vcpus = [(1, 501), (0, 500), (3, 503), (2, 502)]
    p = plan(vcpus, [500, 501, 502, 503, 600, 601], [8, 9, 10, 11], 12)
    cases.append(("vCPU i goes to CPU i of the list, by index not by tid order",
                  p == {500: 8, 501: 9, 502: 10, 503: 11, 600: 12, 601: 12}))
    try:
        plan(vcpus, [], [8, 9, 10], 12)
        cases.append(("four vCPUs on three CPUs is refused", False))
    except ValueError:
        cases.append(("four vCPUs on three CPUs is refused", True))
    aff = {t: {0, 1, 2, 3, 8, 9, 10, 11, 12} for t in (500, 501, 502, 503, 600)}

    def get(t):
        """return a built thread's affinity"""
        return set(aff[t])

    def put(t, cpus):
        """set a built thread's affinity"""
        aff[t] = set(cpus)
    with tempfile.TemporaryDirectory() as td:
        st = Path(td) / "vcpus.json"
        pin(None, 1, [8, 9, 10, 11], 12, st, get=get, put=put, vcpus=vcpus, tids=[500, 501, 502, 503, 600])
        saved = json.loads(st.read_text())
        cases.append(("the prior affinity is saved before pinning", saved["prior"]["500"] == [0, 1, 2, 3, 8, 9, 10, 11, 12]))
        cases.append(("every thread is on its CPU", aff[502] == {10} and aff[600] == {12}))
        cases.append(("a pinned VM checks as pinned", check(st, get=get) == 1))
        aff[503] = {8, 11}
        cases.append(("a vCPU moved off its CPU checks as not pinned", check(st, get=get) == 0))
        n = restore(st, put=put, alive=lambda t: t != 600)
        cases.append(("restore puts back every live thread and skips a gone one",
                      n == 4 and aff[500] == {0, 1, 2, 3, 8, 9, 10, 11, 12} and aff[600] == {12}))
        cases.append(("restore again is the same", restore(st, put=put, alive=lambda t: t != 600) == 4))
    idle = {"load1": 0.4, "free_gb": 200.0, "foreign_pipelines": 0}
    cases.append(("an idle PC with only this VM is exclusive", exclusive(7, 4, idle, [7])["exclusive"] == 1))
    cases.append(("the VM's own vCPUs raise the load ceiling by one each", exclusive(7, 4, dict(idle, load1=4.9), [7])["exclusive"] == 1))
    for name, f, q in (("a load above 1 plus the vCPUs is not exclusive", dict(idle, load1=5.2), [7]),
                       ("a second qemu is not exclusive", idle, [7, 8]),
                       ("a full disk is not exclusive", dict(idle, free_gb=10.0), [7]),
                       ("a foreign pipeline is not exclusive", dict(idle, foreign_pipelines=1), [7])):
        r = exclusive(7, 4, f, q)
        cases.append((name, r["exclusive"] == 0 and " " not in r["why"]))
    cases.append(("a PC load's own duty on each vCPU's CPU raises its arm's ceiling",
                  ceiling(4, "pc", 30) == 1 + 4 + 1.2 and ceiling(4, "vm", 30) == 5 and ceiling(4, "none", 0) == 5))
    cases.append(("a PC-load arm under its own load is exclusive",
                  exclusive(7, 4, dict(idle, load1=6.1), [7], level="pc", duty=30)["exclusive"] == 1))
    cases.append(("a PC-load arm above its own load is not exclusive",
                  exclusive(7, 4, dict(idle, load1=6.3), [7], level="pc", duty=30)["exclusive"] == 0))
    cases.append(("a VM-load arm gets no PC-load allowance",
                  exclusive(7, 4, dict(idle, load1=6.1), [7], level="vm", duty=30)["exclusive"] == 0))
    cases.append(("the idle check names the ceiling", exclusive(7, 4, idle, [7], level="pc", duty=30)["ceiling"] == 6.2))
    slept = []

    def loads(*xs):
        """return a load reader that gives xs in turn, then the last one forever"""
        it = iter(xs)
        last = [xs[-1]]

        def read():
            """return the next built load1"""
            last[0] = next(it, last[0])
            return last[0]
        return read
    cases.append(("a PC already under its ceiling settles at once", settle(5.0, load=loads(4.0), sleep=slept.append) == (0, 4.0)))
    cases.append(("a load falling under the ceiling is waited for, polled every 5 s",
                  settle(5.0, load=loads(7.0, 6.0, 5.0), sleep=slept.append) == (10, 5.0) and slept == [5, 5]))
    slept.clear()
    cases.append(("a load that never falls is waited for 90 s at most", settle(5.0, load=loads(9.0), sleep=slept.append) == (90, 9.0)
                  and sum(slept) == 90 and len(slept) == 18))
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'ok' if ok else 'FAIL'} {n}")
    print(f"rhizo_host selftest: {len(cases) - len(bad)}/{len(cases)} ok")
    return 1 if bad else 0


def main(argv=None):
    """run a subcommand"""
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
        print(f"pinned {len(pin(a.qmp, a.pid, [int(c) for c in a.cpus.split(',')], a.other, a.state))} threads")
    elif a.cmd == "check":
        print(f"vcpus_pinned={check(a.state)}")
    elif a.cmd == "restore":
        print(f"restored {restore(a.state)} threads")
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
