#!/usr/bin/env python3
import collections
import json
import re
import statistics
import struct
import sys
import tempfile
from pathlib import Path

HDR = struct.Struct("<7Q4I")
HELD_NS = 2_000_000
SEQ, J_SENSOR, J_ACTUATOR = 0, 5, 6
CPU_COMPUTE = 8
STEAL_FIELD = 8
HEADROOM_SKIP_NS = 5_000_000_000
USAGE = """usage: frames.py RUN_DIR
       frames.py --selftest"""


def read_frames(path):
    raw = Path(path).read_bytes()
    return [HDR.unpack_from(raw, i) for i in range(0, len(raw) - len(raw) % HDR.size, HDR.size)]


def read_windows(text):
    rx = re.compile(r"^WIN cpu=(\d+) start_ns=(\d+) end_ns=(\d+)$")
    out = {}
    for line in text.splitlines():
        m = rx.match(line)
        if m:
            out.setdefault(int(m.group(1)), []).append((int(m.group(2)), int(m.group(3))))
    return out


def load_held(text):
    return sum(int(m.group(1)) for m in re.finditer(r"^CPU_LOAD cpu=\d+ .*held_ns=(\d+)$", text, re.M))


def overlap(a0, a1, b0, b1):
    return max(0, min(a1, b1) - max(a0, b0))


def union(lists):
    out = []
    for s, e in sorted(iv for lst in lists for iv in lst):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def intersect(a, b):
    a, b, out, i, j = union([a]), union([b]), [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def windows_in(windows, a, b):
    return sum(overlap(s, e, a, b) for s, e in windows)


def held_in(slices, a, b):
    total = 0
    for _, ts, dur, held in slices:
        if dur > 0:
            total += held * overlap(ts, ts + dur, a, b) // dur
        elif a <= ts < b:
            total += held
    return total


def epoch(frames, period):
    return min(f[J_SENSOR] - f[SEQ] * period for f in frames)


def keep_records(frames, period, n_frames):
    inside = [f for f in frames if f[SEQ] < n_frames]
    if not inside:
        return [], len(frames)
    floor = statistics.median(f[J_SENSOR] - f[SEQ] * period for f in inside) - period / 2
    kept = [f for f in inside if f[J_SENSOR] - f[SEQ] * period >= floor]
    return kept, len(frames) - len(kept)


def count(frames, period, n_frames, windows_by_cpu, slices, level):
    if not frames:
        raise ValueError("no frames")
    kept, torn = keep_records(frames, period, n_frames)
    if not kept:
        raise ValueError("no seq < frames")
    e0 = epoch(kept, period)
    recorded = {f[SEQ]: f for f in kept}
    late = {k for k, f in recorded.items() if f[J_ACTUATOR] > e0 + k * period + period}
    lists = list(windows_by_cpu.values())
    every = union(lists[:1])
    for w in lists[1:]:
        every = intersect(every, w)
    some = union(lists)
    c = dict(frames_sent=n_frames, recorded=len(recorded), torn_records=torn, dropped=n_frames - len(recorded), late=len(late),
             missed=0, missed_labelled=0, missed_held=0, missed_held_labelled=0, clear=0, clear_labelled=0, partial=0)
    for k in range(n_frames):
        r = e0 + k * period
        missed = int(k not in recorded or k in late)
        labelled = int(held_in(slices, r, r + period) >= HELD_NS)
        c["missed"] += missed
        c["missed_labelled"] += missed * labelled
        if level == "pc":
            continue
        if level == "vm" and windows_in(every, r, r + period) >= HELD_NS:
            c["missed_held"] += missed
            c["missed_held_labelled"] += missed * labelled
        elif level != "vm" or windows_in(some, r, r + period) == 0:
            c["clear"] += 1
            c["clear_labelled"] += labelled
        else:
            c["partial"] += 1
    a, b = e0, e0 + n_frames * period
    c["span_ns"] = b - a
    c["missed_frac"] = round(c["missed"] / n_frames, 6)
    c["injected_ns"] = windows_in(every, a, b) if level == "vm" else 0
    c["delayd_held_ns"] = held_in(slices, a, b)
    c["cpus_used"] = len({f[CPU_COMPUTE] for f in kept})
    return c


def kv(path):
    p = Path(path)
    if not p.exists():
        return {}
    return dict(t.split("=", 1) for t in p.read_text().split() if "=" in t)


def steal_ns(before, after):
    def ticks(text):
        lines = text.strip().split("\n")
        return int(lines[0].split()[STEAL_FIELD]), int(lines[1])
    try:
        (s0, hz), (s1, _) = ticks(before), ticks(after)
    except (IndexError, ValueError):
        return -1
    return (s1 - s0) * 1_000_000_000 // hz


def grant_of(status_text, tid):
    try:
        st = json.loads(status_text)
        sessions, grants = st.get("sessions", {}), st.get("grants", {})
    except (ValueError, AttributeError):
        return "none", 0
    for s in sessions.values():
        for host_tid, t in s.get("threads", {}).items():
            if t.get("ns_tid") == tid:
                runtime, period = (grants.get(host_tid) or [0, 0])[:2]
                bw = runtime / period if t.get("policy") == "deadline" and period else 0
                return t.get("policy", "none"), bw
    return "none", 0


def grant_over(texts, tid):
    got = [grant_of(t, tid) for t in texts]
    if not got:
        return "none", 0, 0
    policy = collections.Counter(p for p, _ in got).most_common(1)[0][0]
    return policy, round(sum(bw for _, bw in got) / len(got), 6), len(got)


def frame_cpu_over(texts, tid):
    got = []
    for t in texts:
        try:
            sessions = json.loads(t).get("sessions", {})
        except (ValueError, AttributeError):
            continue
        for s in sessions.values():
            for th in s.get("threads", {}).values():
                v = th.get("frame_cpu_ns")
                if th.get("ns_tid") == tid and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    got.append(v)
    return int(statistics.median(got)) if got else 0


def counters_span(d):
    files = sorted(Path(d).glob("rtd-stat-[0-9]*.txt"))
    if len(files) < 2:
        return -1, 0
    try:
        first, last = (f.read_text().strip().split("\n") for f in (files[0], files[-1]))
        span = round((float(last[2].split()[0]) - float(first[2].split()[0])) * 1e9)
    except (IndexError, ValueError):
        return -1, 0
    steal = steal_ns("\n".join(first[:2]), "\n".join(last[:2]))
    return (steal, span) if steal >= 0 and span > 0 else (-1, 0)


def headroom_facts(text):
    calls = []
    for line in text.splitlines():
        m = re.fullmatch(r"H t_ns=(\d+) rc=(-?\d+) value=(\S+) call_us=\d+", line.strip())
        if m:
            calls.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    start = calls[0][0] + HEADROOM_SKIP_NS if calls else 0
    late = [(rc, v) for t, rc, v in calls if t >= start]
    answers = [float(v) for rc, v in late if rc == 0 and v != "nan"]
    return {"headroom_calls": len(calls), "headroom_answers": len(answers),
            "headroom_nan": sum(rc == 0 and v == "nan" for rc, v in late), "headroom_errors": sum(rc != 0 for _, rc, _ in calls),
            "headroom_median": round(statistics.median(answers), 2) if answers else -1,
            "headroom_done": int(any(l.strip() == "HEADROOM done" for l in text.splitlines()))}


def rtd_facts(text, loop_ids):
    opened, paused, refused = {}, 0, 0
    for line in text.splitlines():
        m = re.search(r"\bsession (\S+) opened tgid=(\d+)", line)
        if m:
            opened[m.group(1)] = int(m.group(2))
            continue
        m = re.search(r"\bsession (\S+) paused\b", line)
        if m:
            paused += opened.get(m.group(1)) in loop_ids
            continue
        m = re.search(r"\brefused tid=(\d+) ", line)
        if m:
            refused += int(m.group(1)) in loop_ids
    return paused, refused


def run_facts(d):
    d = Path(d)
    conf = kv(d / "run.conf")
    period, level, n_frames, load_cpus = int(conf["period_ns"]), conf["level"], int(conf["frames"]), int(conf["load_cpus"])
    compute = int(conf["compute_tid"])
    watched = {int(conf["sensor_tid"]), compute, int(conf["actuator_tid"])}
    slices = [s for s in json.loads((d / "slices.json").read_text())["slices"] if s[0] in watched]
    log = (d / "cpu_load.log").read_text() if level == "vm" else ""
    read = read_windows(log)
    windows = {c: read.get(c, []) for c in range(load_cpus)} if level == "vm" else {}
    f = {k: conf[k] for k in ("label", "level", "adpf", "duty", "load_s")}
    f["load_cpus"] = load_cpus
    actuator = read_frames(d / "actuator.bin")
    f.update(count(actuator, period, n_frames, windows, slices, level))
    f["actuator_records"] = len(actuator)
    work = [r[3] - r[2] for r in actuator if r[3] > r[2]]
    f["compute_work_ns"] = int(statistics.median(work)) if work else 0
    f["telemetry_records"] = len(read_frames(d / "telemetry.bin"))
    f["recorder_lost"] = f["actuator_records"] - f["telemetry_records"]
    f["load_all_windows_ns"] = sum(e - s for w in windows.values() for s, e in w)
    f["load_held_ns"] = load_held(log)
    f["inject_cpu_ns"] = int(kv(d / "inject.out").get("cpu_consumed_ns", 0)) if level == "pc" else 0
    sb, sa = d / "stat-before.txt", d / "stat-after.txt"
    f["guest_steal_ns"] = steal_ns(sb.read_text(), sa.read_text()) if sb.exists() and sa.exists() else -1
    loop_ids = {int(p) for p in (d / "pids.txt").read_text().split()} | watched
    f["sessions_paused"], f["grant_refusals"] = rtd_facts((d / "rtd.log").read_text(), loop_ids)
    samples = [p.read_text() for p in sorted(d.glob("rtd-status-[0-9]*.json"))]
    f["compute_policy"], f["compute_grant_bw"], f["status_samples"] = grant_over(samples, compute)
    f["slot_us"], f["telemetry_adpf"] = conf.get("slot_us", "100000"), conf.get("telemetry_adpf", "off")
    tel = int(conf.get("telemetry_tid", "0"))
    f["telemetry_policy"] = grant_over(samples, tel)[0] if tel else "none"
    for name in ("sensor", "compute", "actuator"):
        f[f"{name}_frame_cpu_ns"] = frame_cpu_over(samples, int(conf[f"{name}_tid"]))
    f["loop_steal_ns"], f["loop_span_ns"] = counters_span(d)
    logs = [d / "android" / f"{n}.log" for n in ("sensor", "compute", "actuator", "telemetry")]
    f["hint_sessions_open"] = sum(p.exists() and any(l.startswith("OBS hint_session=open") for l in p.read_text().splitlines()) for p in logs)
    if conf.get("headroom") == "on":
        f["headroom"] = "on"
        f.update(headroom_facts((d / "android" / "headroom.log").read_text()))
    f["settle_s"] = int(kv(d / "settle.txt").get("settle_s", -1))
    for when in ("start", "end"):
        e = kv(d / f"exclusive-{when}.txt")
        f[f"exclusive_{when}"] = int(e.get("exclusive", 0))
        f[f"why_{when}"] = e.get("why", "missing")
        f[f"load1_{when}"] = e.get("load1", "-")
        f[f"vcpus_pinned_{when}"] = int(kv(d / f"vcpus-{when}.txt").get("vcpus_pinned", 0))
    f.update(kv(d / "versions.txt"))
    f["slices"] = len(slices)
    return f


def fact_line(f):
    return "FACT " + " ".join(f"{k}={v}" for k, v in f.items())


def selftest():
    P, t0, MS = 10_000_000, 1_000_000_000, 1_000_000

    def fr(seq, lat=MS, wait=0, cpus=(0, 1, 1)):
        t = t0 + seq * P + wait
        return (seq, t, t + 1, t + 2, t + lat, t, t + lat, *cpus, 0)

    def four(*spans):
        return {c: list(spans) for c in range(4)}
    all10 = [fr(k) for k in range(10)]
    held3 = [fr(k) for k in range(10) if k != 3] + [fr(3, lat=3 * MS, wait=8 * MS)]
    sens3 = [[5, t0 + 3 * P, 8 * MS, 8 * MS]]
    cases = []
    c = count(all10, P, 10, {}, [], "none")
    cases.append(("level=none, 10 on time: missed=0, clear=10",
                  (c["frames_sent"], c["missed"], c["late"], c["clear"], c["clear_labelled"]) == (10, 0, 0, 10, 0)))
    c = count(held3, P, 10, four((t0 + 3 * P, t0 + 3 * P + 8 * MS)), sens3, "vm")
    cases.append(("vm, sensor held on 4 vCPUs: late, missed_held, labelled",
                  (c["late"], c["missed"], c["missed_held"], c["missed_held_labelled"], c["clear"], c["partial"])
                  == (1, 1, 1, 1, 9, 0)))
    cases.append(("vm: injected_ns=4-vCPU intersection, delayd_held_ns summed",
                  c["injected_ns"] == 8 * MS and c["delayd_held_ns"] == 8 * MS and c["span_ns"] == 10 * P))
    w3 = {c: [(t0 + 5 * P, t0 + 6 * P)] for c in range(3)}
    w3[3] = []
    c = count(all10, P, 10, w3, [], "vm")
    cases.append(("window on vCPUs 0-2 of 4: partial",
                  (c["partial"], c["missed_held"], c["clear"], c["injected_ns"]) == (1, 0, 9, 0)))
    c = count(all10[1:], P, 10, {}, [], "none")
    cases.append(("seq 0 dropped: dropped=1, frames_sent=10",
                  (c["frames_sent"], c["recorded"], c["dropped"], c["missed"]) == (10, 9, 1, 1)))
    c = count(all10[:9], P, 10, {}, [], "none")
    cases.append(("seq 9 dropped: dropped=1", (c["dropped"], c["missed"]) == (1, 1)))
    torn5 = all10[:5] + [(5,) + fr(4)[1:6] + (t0 + 5 * P + MS,) + fr(4)[7:]] + all10[6:]
    c = count(torn5, P, 10, {}, [], "none")
    cases.append(("torn seq 5: torn_records=1, E unchanged, missed=1",
                  (c["torn_records"], c["recorded"], c["dropped"], c["late"], c["missed"]) == (1, 9, 1, 0, 1)))
    c = count(all10 + [fr(10)], P, 10, {}, [], "none")
    cases.append(("seq >= frames: torn_records=1, ignored",
                  (c["torn_records"], c["recorded"], c["frames_sent"], c["missed"]) == (1, 10, 10, 0)))
    c = count(all10, P, 10, {}, [], "none")
    cases.append(("no torn record: torn_records=0", c["torn_records"] == 0))
    full5 = [(t0 + 5 * P, t0 + 6 * P)]
    c = count(all10, P, 10, {0: full5, 1: full5, 2: [], 3: full5}, [], "vm")
    cases.append(("vCPU 2 no window: partial",
                  (c["partial"], c["missed_held"], c["injected_ns"]) == (1, 0, 0)))
    c = count(all10, P, 10, {k: [(t0 + 5 * P + k * MS // 2, t0 + 5 * P + k * MS // 2 + 3 * MS)] for k in range(4)}, [], "vm")
    cases.append(("4 offset 3 ms windows, 1.5 ms shared: partial",
                  (c["partial"], c["missed_held"], c["clear"], c["injected_ns"]) == (1, 0, 9, 3 * MS // 2)))
    c = count(all10, P, 10, {0: [(t0 + 5 * P, t0 + 5 * P + 3 * MS), (t0 + 5 * P + MS, t0 + 5 * P + 4 * MS)]}, [], "vm")
    cases.append(("overlapping windows, one vCPU: injected_ns=4 ms", c["injected_ns"] == 4 * MS))
    early = [fr(0, wait=4 * MS)] + all10[1:]
    cases.append(("epoch: minimum, late frame 0 ignored",
                  epoch(early, P) == t0 and epoch(held3, P) == t0))
    c = count(early, P, 10, {}, [], "none")
    cases.append(("wake +4 ms, done in period: not missed", (c["late"], c["missed"]) == (0, 0)))
    c = count(all10[:4] + [fr(4, lat=12 * MS)] + all10[5:], P, 10, {}, [], "none")
    cases.append(("actuator > release + P: late, missed",
                  (c["late"], c["missed"], c["missed_frac"]) == (1, 1, 0.1)))
    c = count(all10, P, 10, {}, [[7, t0 + 6 * P, 2 * P, 4 * MS]], "none")
    cases.append(("slice over 2 periods: clear_labelled=2", c["clear_labelled"] == 2))
    drop3 = [fr(k) for k in range(10) if k != 3]
    c = count(drop3, P, 10, {}, [[7, t0 + 3 * P, P, 9 * MS]], "pc")
    cases.append(("level=pc: missed, missed_labelled; clear=0",
                  (c["missed"], c["missed_labelled"], c["clear"], c["missed_held"], c["partial"]) == (1, 1, 0, 0, 0)))
    c = count(all10[:5] + [fr(k, cpus=(0, 2, 2)) for k in range(5, 10)], P, 10, {}, [], "none")
    cases.append(("cpus_used=2, no pinned", c["cpus_used"] == 2 and "pinned" not in c))
    cases.append(("intersect, union", intersect([(0, 10), (20, 30)], [(5, 25)]) == [(5, 10), (20, 25)]
                  and union([[(0, 10)], [(5, 15)], [(20, 30)]]) == [(0, 15), (20, 30)]))
    log = ("CPU_LOAD pid=9 cpus=0,1,2,3 duration_s=1 duty=30 slot_us=100000 rt_prio=90 clock=boottime\n"
           "CPU_LOAD cpu=3 ran_cpu_first=3 ran_cpu_last=3 windows=2 held_ns=500\n"
           "WIN cpu=3 start_ns=10 end_ns=20\nWIN cpu=1 start_ns=5 end_ns=9\nWIN cpu=3 start_ns=30 end_ns=40\n"
           "CPU_LOAD cpu=1 ran_cpu_first=1 ran_cpu_last=1 windows=1 held_ns=700\n")
    cases.append(("read_windows: per-cpu windows", read_windows(log) == {3: [(10, 20), (30, 40)], 1: [(5, 9)]}))
    cases.append(("load_held: 1200, empty 0", load_held(log) == 1200 and load_held("") == 0))
    try:
        count([], P, 10, {}, [], "none")
        cases.append(("count no records: ValueError", False))
    except ValueError:
        cases.append(("count no records: ValueError", True))
    def status(policy, runtime, period=P, tid=7):
        grants = {"900": [runtime, period]} if policy == "deadline" else {}
        return json.dumps({"grants": grants, "sessions": {"1:1": {"threads": {"900": {
            "ns_tid": tid, "policy": policy, "runtime_ns": runtime if policy == "deadline" else 0}}}}})
    cases.append(("grant_of by ns_tid: 0.06",
                  grant_of(status("deadline", 600000), 7) == ("deadline", 0.06) and grant_of(status("deadline", 600000), 8) == ("none", 0)))
    cases.append(("grant_of invalid json: (none, 0)", grant_of("{", 7) == ("none", 0)))
    three = [status("deadline", 600000), status("deadline", 800000), status("other", 0)]
    cases.append(("grant_over: majority policy, mean bw 0.046667",
                  grant_over(three, 7) == ("deadline", 0.046667, 3)))
    cases.append(("grant_over tid absent: (none, 0, 2)",
                  grant_over([status("deadline", 600000, tid=9)] * 2, 7) == ("none", 0, 2)))
    cases.append(("grant_over []: (none, 0, 0)", grant_over([], 7) == ("none", 0, 0)))
    rtd = ("session 1:9 opened tgid=482 uid=10078 target_ns=11666666 tids=482\n"
           "session 1:13 opened tgid=9931 uid=10108 target_ns=10000000 tids=9931\n"
           "session 1:14 opened tgid=9934 uid=10108 target_ns=10000000 tids=9934\n"
           "grant tid=29785 runtime=200000 period=10000000\n"
           "session 1:9 paused\n"
           "session 1:14 paused\n"
           "refused tid=9934 session=-: sched_setattr grant failed, errno=1 runtime=200000 period=10000000\n"
           "refused tid=700 session=-: sched_setattr grant failed, errno=1 runtime=200000 period=10000000\n"
           "session 1:14 resumed\n")
    cases.append(("rtd_facts loop ids only: (1, 1)",
                  rtd_facts(rtd, {9931, 9934, 9935, 9936}) == (1, 1)))
    reused = ("session 1:2 opened tgid=9931 uid=10108 target_ns=10000000 tids=9931\nsession 1:2 closed\n"
              "session 1:2 opened tgid=600 uid=10072 target_ns=16666667 tids=600\nsession 1:2 paused\n")
    cases.append(("rtd_facts reused hint session id: (0, 0)",
                  rtd_facts(reused, {9931}) == (0, 0)))
    cases.append(("rtd_facts empty: (0, 0)", rtd_facts("", {1, 2}) == (0, 0)))
    def cpu_sample(v, tid=7):
        return json.dumps({"sessions": {"1:1": {"threads": {"900": {"ns_tid": tid, "policy": "deadline", "frame_cpu_ns": v}}}}})
    cases.append(("frame_cpu_over float: 734375",
                  frame_cpu_over([cpu_sample(400000), cpu_sample(500000), cpu_sample(968750.0), cpu_sample(990000.5)], 7) == 734375))
    cases.append(("frame_cpu_over bool/str/0: ignored",
                  frame_cpu_over([cpu_sample(True), cpu_sample("5"), cpu_sample(0), cpu_sample(300000)], 7) == 300000))
    cases.append(("steal_ns: 500000000",
                  steal_ns("cpu  1 2 3 4 5 6 7 100 0 0\n100", "cpu  1 2 3 4 5 6 7 150 0 0\n100") == 500_000_000))
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "actuator.bin").write_bytes(b"".join(HDR.pack(*f) for f in held3))
        (d / "telemetry.bin").write_bytes(b"".join(HDR.pack(*f) for f in held3))
        (d / "run.conf").write_text("label=t-r0\nlevel=vm\nadpf=off\nduty=30\nload_s=75\nperiod_ns=10000000\nframes=10\n"
                                    "load_cpus=4\nsensor_tid=5\ncompute_tid=7\nactuator_tid=8\ncompute_pid=7\n")
        (d / "slices.json").write_text(json.dumps({"slices": sens3 + [[99, t0, P, 9 * MS]]}))
        (d / "cpu_load.log").write_text("".join(f"CPU_LOAD cpu={c} x=1 held_ns=9000000\n"
                                                f"WIN cpu={c} start_ns={t0 + 3 * P} end_ns={t0 + 3 * P + 8 * MS}\n"
                                                for c in range(4)))
        (d / "exclusive-start.txt").write_text("exclusive=1 why=idle load1=0.5\n")
        (d / "settle.txt").write_text("settle_s=15 load1=5.80 ceiling=6.20\n")
        (d / "pids.txt").write_text("5\n7\n8\n9\n")
        for i, (pol, rt) in enumerate((("deadline", 500000), ("deadline", 700000), ("deadline", 600000)), 1):
            (d / f"rtd-status-{i:03d}.json").write_text(status(pol, rt))
        (d / "rtd.log").write_text("session 1:3 opened tgid=7 uid=10108 target_ns=10000000 tids=7\nsession 1:3 paused\n"
                                   "refused tid=8 session=-: sched_setattr grant failed, errno=1 runtime=1 period=2\n")
        (d / "vcpus-start.txt").write_text("vcpus_pinned=1\n")
        f = run_facts(d)
        cases.append(("run_facts: tid 5 slice kept, tid 99 dropped",
                      f["missed_held_labelled"] == 1 and f["slices"] == 1 and f["delayd_held_ns"] == 8 * MS))
        cases.append(("run_facts: cpus_used, load_cpus, no pinned",
                      f["cpus_used"] == 1 and f["load_cpus"] == 4 and "pinned" not in f and f["frames_sent"] == 10
                      and f["torn_records"] == 0))
        cases.append(("run_facts: load summed over 4 vCPUs",
                      f["load_all_windows_ns"] == 4 * 8 * MS and f["load_held_ns"] == 4 * 9 * MS and f["injected_ns"] == 8 * MS))
        cases.append(("run_facts: settle_s=15", f["settle_s"] == 15))
        cases.append(("no end snapshot: exclusive_end=0, vcpus_pinned_end=0",
                      f["exclusive_start"] == 1 and f["exclusive_end"] == 0 and f["vcpus_pinned_end"] == 0))
        cases.append(("fact_line: no space in values", all("=" in t for t in fact_line(f).split()[1:])))
        cases.append(("run_facts: compute grant from rtd-status-*.json",
                      (f["compute_policy"], f["compute_grant_bw"], f["status_samples"]) == ("deadline", 0.06, 3)
                      and "compute_grant_runtime_ns" not in f))
        cases.append(("run_facts: sessions_paused=1, grant_refusals=1",
                      (f["sessions_paused"], f["grant_refusals"]) == (1, 1)))
        cases.append(("telemetry complete: recorder_lost=0",
                      (f["actuator_records"], f["telemetry_records"], f["recorder_lost"]) == (10, 10, 0)))
        cases.append(("old run.conf: new-fact defaults",
                      (f["slot_us"], f["telemetry_adpf"], f["telemetry_policy"], f["compute_frame_cpu_ns"], f["loop_steal_ns"],
                       f["loop_span_ns"]) == ("100000", "off", "none", 0, -1, 0)))
        conf = (d / "run.conf").read_text()
        (d / "run.conf").write_text(conf + "slot_us=97000\ntelemetry_adpf=on\ntelemetry_tid=9\n")
        for i, (cpu, steal, up) in enumerate(((400000, 10, "100.00"), (500000, 15, "105.00"), (600000, 30, "110.00")), 1):
            (d / f"rtd-status-{i:03d}.json").write_text(json.dumps({"grants": {"900": [600000, P], "901": [200000, P]}, "sessions": {
                "1:1": {"threads": {"900": {"ns_tid": 7, "policy": "deadline", "frame_cpu_ns": cpu},
                                    "901": {"ns_tid": 9, "policy": "deadline", "frame_cpu_ns": 30000}}}}}))
            (d / f"rtd-stat-{i:03d}.txt").write_text(f"cpu  1 2 3 4 5 6 7 {steal} 0 0\n100\n{up}\n")
        f2 = run_facts(d)
        cases.append(("compute_work_ns: median finish-wake = 1",
                      f2["compute_work_ns"] == 1))
        cases.append(("slot_us, telemetry_adpf from run.conf",
                      (f2["slot_us"], f2["telemetry_adpf"]) == ("97000", "on")))
        cases.append(("*_frame_cpu_ns: sample median, 0 if absent",
                      f2["compute_frame_cpu_ns"] == 500000 and f2["sensor_frame_cpu_ns"] == 0))
        cases.append(("telemetry_policy=deadline", f2["telemetry_policy"] == "deadline"))
        cases.append(("loop_steal_ns, loop_span_ns: first to last sample",
                      (f2["loop_steal_ns"], f2["loop_span_ns"]) == (200_000_000, 10_000_000_000)))
        cases.append(("fact_line, slot/telemetry facts: no space in values", all("=" in t for t in fact_line(f2).split()[1:])))
        cases.append(("headroom off: no headroom_* facts", not any(k.startswith("headroom") for k in f2)))
        cases.append(("no stage logs: hint_sessions_open=0", f2["hint_sessions_open"] == 0))
        (d / "android").mkdir(exist_ok=True)
        (d / "android" / "compute.log").write_text("OBS tid=7\nOBS hint_session=open tids=1 target_ns=10000000 preferred_rate_ns=16666667\n")
        (d / "android" / "sensor.log").write_text("OBS hint_session=unsupported reason=create_failed preferred_rate_ns=0\n")
        cases.append(("hint_session open/unsupported: hint_sessions_open=1",
                      run_facts(d)["hint_sessions_open"] == 1))
        (d / "run.conf").write_text(conf + "slot_us=97000\ntelemetry_adpf=on\ntelemetry_tid=9\nheadroom=on\n")
        (d / "android").mkdir(exist_ok=True)
        (d / "android" / "headroom.log").write_text(
            "HEADROOM interval_ms=250 window_ms=1000 type=average clock=boottime\n"
            + "".join(f"H t_ns={t0 + k * 250 * MS} rc=0 value={'nan' if k % 4 == 1 else 70 + k % 3} call_us=900\n" for k in range(80))
            + "H t_ns=99000000000 rc=95 value=nan call_us=3\nHEADROOM done\n")
        f4 = run_facts(d)
        cases.append(("headroom: calls=81 answers=45 nan=15 errors=1",
                      (f4["headroom_calls"], f4["headroom_answers"], f4["headroom_nan"], f4["headroom_errors"]) == (81, 45, 15, 1)))
        cases.append(("headroom_median=71.0", f4["headroom_median"] == 71.0))
        cases.append(("fact_line, headroom facts: no space in values", all("=" in t for t in fact_line(f4).split()[1:])))
        (d / "android" / "headroom.log").write_text("HEADROOM error=no_symbol\n")
        f5 = run_facts(d)
        cases.append(("headroom no answers: median=-1, done=0",
                      (f5["headroom_calls"], f5["headroom_answers"], f5["headroom_median"], f5["headroom_done"]) == (0, 0, -1, 0)))
        (d / "android" / "headroom.log").unlink()
        try:
            run_facts(d)
            cases.append(("headroom.log missing: OSError", False))
        except OSError:
            cases.append(("headroom.log missing: OSError", True))
        (d / "run.conf").write_text(conf + "slot_us=97000\ntelemetry_adpf=on\ntelemetry_tid=9\n")
        (d / "rtd-stat-003.txt").write_text("cpu  1 2 3 4 5 6 7 30 0 0\nx\n110.00\n")
        f3 = run_facts(d)
        cases.append(("rtd-stat bad sample: loop_steal_ns=-1", (f3["loop_steal_ns"], f3["loop_span_ns"]) == (-1, 0)))
        (d / "run.conf").write_text(conf)
        for i, (pol, rt) in enumerate((("deadline", 500000), ("deadline", 700000), ("deadline", 600000)), 1):
            (d / f"rtd-status-{i:03d}.json").write_text(status(pol, rt))
        for p in d.glob("rtd-stat-*.txt"):
            p.unlink()
        (d / "actuator.bin").write_bytes(b"".join(HDR.pack(*f) for f in all10))
        (d / "telemetry.bin").write_bytes(b"".join(HDR.pack(*f) for k, f in enumerate(all10) if k not in (2, 3, 7)))
        f = run_facts(d)
        cases.append(("telemetry lost 3: missed=0, recorder_lost=3",
                      (f["missed"], f["dropped"], f["recorded"], f["actuator_records"], f["telemetry_records"], f["recorder_lost"])
                      == (0, 0, 10, 10, 7, 3)))
        (d / "rtd.log").unlink()
        try:
            run_facts(d)
            cases.append(("rtd.log missing: OSError", False))
        except OSError:
            cases.append(("rtd.log missing: OSError", True))
        (d / "rtd.log").write_text("")
        (d / "actuator.bin").unlink()
        try:
            run_facts(d)
            cases.append(("actuator.bin missing: OSError", False))
        except OSError:
            cases.append(("actuator.bin missing: OSError", True))
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
        print(fact_line(run_facts(argv[0])))
    except (OSError, KeyError, ValueError) as e:
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
