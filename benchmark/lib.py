import hashlib
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOST_CONF = ROOT / "benchmark" / "host" / "settings.conf"
SINGLE_LEVEL = ROOT / "results" / "single_level"
SINGLE_LEVEL_FOLDERS = (("r1_r3", r"pcn?-(kern16|adapt|r3)"), ("kernels", r"pc-kernels"),
                        ("r4", r"pc-r4(-pilot)?"), ("levels", r"pcn?-ld.*"), ("levels2", r"pcn?-lb.*"),
                        ("r4b", r"pc-r4b.*"), ("r5", r"pcn-r5.*"), ("r6", r"pcn?-r6.*"), ("r7", r"pcn-r7.*"))


def single_level_folder(name):
    for folder, rx in SINGLE_LEVEL_FOLDERS:
        if re.fullmatch(rx, name):
            return folder
    raise ValueError(f"no folder: {name!r}")


def single_level_file(name):
    stem = name[:-5] if name.endswith(".json") else name
    return SINGLE_LEVEL / single_level_folder(stem) / f"{stem}.json"


def host_conf(key, path=HOST_CONF):
    for line in Path(path).read_text().splitlines():
        k, eq, v = line.partition("=")
        if eq and k.strip() == key and not k.lstrip().startswith("#"):
            return v.strip()
    raise SystemExit(f"benchmark/host/settings.conf: no {key}= in {path}")


RENAMED_TSV = ROOT / "benchmark" / "renamed.tsv"
RENAMED_KINDS = ("path", "env", "flag", "value")


def renamed_rows(kinds=RENAMED_KINDS, table=RENAMED_TSV):
    out = []
    if not Path(table).exists():
        return out
    for line in Path(table).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        old, new, kind = line.split("\t")
        if kind in kinds and (kind == "text" or "," not in new):
            out.append((old, new))
    return out


def old_names(new, kinds=("marker", "word"), table=RENAMED_TSV):
    return [o for o, n in renamed_rows(kinds, table) if n == new]


def renamed_translator(kinds=RENAMED_KINDS, table=RENAMED_TSV):
    if not Path(table).exists():
        return lambda text: text
    rows = renamed_rows(kinds, table)
    if not rows:
        raise SystemExit(f"renamed: no {','.join(kinds)} rows in {table}")
    new_of = dict(rows)
    olds = sorted(new_of, key=len, reverse=True)
    alts = [re.escape(o) if "/" in o else r"(?<!/)" + re.escape(o) for o in olds]
    pat = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(alts) + r")(?![A-Za-z0-9_])")
    return lambda text: pat.sub(lambda m: new_of[m.group(1)], text)
RESULTS = ROOT / "results"


def need_input(path, flag, prog):
    if not (Path(path).exists() or (ROOT / path).exists()):
        raise SystemExit(f"{prog}: {path} not found")


BIN = ROOT / "build" / "pipeline"

COLUMNS = [
    "run_id", "repo_sha", "isio_sha", "config_id", "factor", "factor_value",
    "verdict", "verdict_reason", "host_exclusive", "host_measurable",
    "kernel_release", "kernel_preempt", "kernel_cmdline", "clocksource", "tsc_flags",
    "firmware_version", "cpu_model", "cores_online", "smt", "freq_governor",
    "isolcpus", "nohz_full", "rcu_nocbs", "irq_default_affinity", "rt_runtime_us",
    "policy", "prio", "period_ns", "deadline_ns", "slopes", "acts",
    "cpu_sensor", "cpu_compute", "cpu_actuator", "cpu_telemetry", "neighbour",
    "frames_requested", "frames_done", "records",
    "lat_min_ns", "lat_p50_ns", "lat_p99_ns", "lat_p999_ns", "lat_max_ns", "lat_mean_ns",
    "jitter_rms_ns", "jitter_p2p_ns", "over_deadline", "over_deadline_frac",
    "wake_p50_ns", "wake_p99_ns", "wake_max_ns",
    "compute_p50_ns", "compute_p99_ns", "compute_max_ns",
    "post_p50_ns", "post_p99_ns", "post_max_ns",
    "att_irq", "att_ipi", "att_tlb", "att_softirq", "att_preempt", "att_fault",
    "att_unattributed",
    "hwlat_max_ns", "notes",
    "recon", "modes", "tier", "adapt", "block",
    "modes_done_p50", "mean_relerr", "full_quality_frac",
    "inloop_gap_sum_ns", "inloop_observed_ns", "inloop_stopped_frames",
    "topology",
]

VERDICT_OK = "ok"


def blank_row():
    return {c: "-" for c in COLUMNS}


def run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def sha(path=ROOT):
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "-"


def imagestreamio_sha():
    d = ROOT / "third_party" / "ImageStreamIO"
    return sha(d) if (d / ".git").exists() else "-"


def append_rows(rows, table="results.tsv", columns=None):
    columns = list(columns or COLUMNS)
    if table == PUBLISHED_TABLE:
        bad = sorted({str(r.get("topology", "-")) for r in rows} - set(PUBLISHED_TOPOLOGIES))
        if bad:
            raise SystemExit(
                f"{table}: topology {bad} refused")
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / table
    new = not out.exists() or out.stat().st_size == 0
    if not new:
        header = out.open().readline().rstrip("\n").split("\t")
        if header != columns:
            added = [c for c in columns if c not in header]
            removed = [c for c in header if c not in columns]
            if columns[:len(header)] == header and not removed:
                pad = "\t".join(["-"] * (len(columns) - len(header)))
                rows_in = out.read_text().splitlines()
                with out.open("w") as f:
                    f.write("\t".join(columns) + "\n")
                    for line in rows_in[1:]:
                        f.write(line + "\t" + pad + "\n")
            else:
                raise SystemExit(
                    f"{out.name}: schema mismatch added={added} "
                    f"removed={removed}")
    with out.open("a") as f:
        if new:
            f.write("\t".join(columns) + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(c, "-")).replace("\t", " ") for c in columns) + "\n")
    return out


def _read(path, default="-"):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _cmd(argv, default="-"):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or default
    except (OSError, subprocess.SubprocessError):
        return default


def hash_root_ids(cmdline):
    def one(m):
        return m.group(1) + "sha256:" + hashlib.sha256(m.group(2).encode()).hexdigest()
    return re.sub(r"(\broot=(?:PART)?UUID=)(?!sha256:)(\S+)", one, cmdline)


def machine_config():
    f = {}
    f["kernel_release"] = platform.release()
    ver = _read("/proc/version")
    m = re.search(r"(PREEMPT_RT|PREEMPT_DYNAMIC|PREEMPT|VOLUNTARY)", ver)
    f["kernel_preempt"] = m.group(1) if m else "-"
    f["kernel_cmdline"] = hash_root_ids(_read("/proc/cmdline"))
    f["clocksource"] = _read("/sys/devices/system/clocksource/clocksource0/current_clocksource")

    cpuinfo = _read("/proc/cpuinfo", "")
    flags = set(re.findall(r"\b(constant_tsc|nonstop_tsc|tsc_reliable)\b", cpuinfo))
    f["tsc_flags"] = ",".join(sorted(flags)) or "none"
    mm = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    f["cpu_model"] = mm.group(1).strip() if mm else "-"
    f["cores_online"] = _read("/sys/devices/system/cpu/online")
    f["smt"] = _read("/sys/devices/system/cpu/smt/control")
    f["freq_governor"] = _read(
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "none")

    cl = f["kernel_cmdline"]
    for key in ("isolcpus", "nohz_full", "rcu_nocbs"):
        m = re.search(rf"\b{key}=(\S+)", cl)
        f[key] = m.group(1) if m else "unset"
    f["irq_default_affinity"] = _read("/proc/irq/default_smp_affinity")
    f["rt_runtime_us"] = _read("/proc/sys/kernel/sched_rt_runtime_us")
    f["firmware_version"] = _read("/sys/class/dmi/id/bios_version")
    return f


def host_measurable():
    f = machine_config()
    why = []
    if f["kernel_preempt"] != "PREEMPT_RT":
        why.append("not PREEMPT_RT")
    if f["clocksource"] != "tsc":
        why.append(f"clocksource={f['clocksource']}")
    for want in ("constant_tsc", "nonstop_tsc"):
        if want not in f["tsc_flags"]:
            why.append(f"no {want}")
    if _read("/sys/hypervisor/type", "") or "hypervisor" in _read("/proc/cpuinfo", ""):
        why.append("hypervisor")
    topo = host_topology()
    if topo["topology"] == TOPOLOGY_UNKNOWN:
        why.append("topology unknown")
    return {"measurable": 0 if why else 1, "why": "; ".join(why) or "ok"}


def measurable_from_row(row):
    why = []
    if row.get("kernel_preempt", "-") != "PREEMPT_RT":
        why.append("not PREEMPT_RT")
    if row.get("clocksource", "-") != "tsc":
        why.append(f"clocksource={row.get('clocksource', '-')}")
    flags = row.get("tsc_flags", "") or ""
    for want in ("constant_tsc", "nonstop_tsc"):
        if want not in flags:
            why.append(f"no {want}")
    return {"measurable": 0 if why else 1, "why": "; ".join(why) or "ok"}


TOPOLOGY_NESTED = "nested"
TOPOLOGY_SINGLE = "single-level"
TOPOLOGY_UNKNOWN = "unknown"
PUBLISHED_TOPOLOGIES = (TOPOLOGY_NESTED,)
PUBLISHED_TABLE = "results.tsv"
LEGACY_TOPOLOGY = TOPOLOGY_NESTED


def host_topology(cpuinfo=None, detect_virt=None):
    if cpuinfo is None:
        cpuinfo = _read("/proc/cpuinfo", "")
    flag = 1 if re.search(r"^flags\s*:.*\bhypervisor\b", cpuinfo, re.M) else 0
    if detect_virt is None:
        detect_virt = (_cmd(["systemd-detect-virt", "--vm"], "absent")
                       if shutil.which("systemd-detect-virt") else "absent")
    dv = (detect_virt.split() or ["absent"])[0]
    vm = dv not in ("none", "absent")
    if flag and dv != "none":
        topo, why = TOPOLOGY_NESTED, f"hypervisor=1 virt={dv}"
    elif not flag and not vm:
        topo, why = TOPOLOGY_SINGLE, f"hypervisor=0 virt={dv}"
    else:
        topo, why = TOPOLOGY_UNKNOWN, f"disagree: hypervisor={flag} virt={dv}"
    return {"topology": topo, "hypervisor_flag": flag, "detect_virt": dv, "why": why}


def table_for(topology, requested=PUBLISHED_TABLE):
    if topology not in (TOPOLOGY_NESTED, TOPOLOGY_SINGLE, TOPOLOGY_UNKNOWN):
        raise SystemExit(f"no topology ({topology!r})")
    if requested == PUBLISHED_TABLE and topology not in PUBLISHED_TOPOLOGIES:
        return f"{topology}.tsv"
    return requested


def row_topology(row):
    v = str(row.get("topology", "-") or "-")
    return LEGACY_TOPOLOGY if v == "-" else v


def experiment_topology(d):
    meta = Path(d) / "host.meta"
    try:
        for line in meta.read_text(errors="replace").splitlines():
            for tok in line.split():
                if tok.startswith("topology="):
                    return tok.partition("=")[2]
    except OSError:
        pass
    return host_topology()["topology"]


def unpublishable(results=None):
    results = Path(results or RESULTS)
    bad = []
    tsv = results / PUBLISHED_TABLE
    if tsv.exists():
        import csv
        with tsv.open() as f:
            for r in csv.DictReader(f, delimiter="\t"):
                if row_topology(r) not in PUBLISHED_TOPOLOGIES:
                    bad.append(f"{PUBLISHED_TABLE}: {r.get('run_id')} topology={row_topology(r)}")
    for p in sorted(results.glob("*.json")):
        t = json_topology(p)
        if t not in PUBLISHED_TOPOLOGIES:
            bad.append(f"{p.name}: topology={t}")
    return bad


def json_topology(path):
    import json
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return LEGACY_TOPOLOGY
    return str(d.get("topology", LEGACY_TOPOLOGY)) if isinstance(d, dict) else LEGACY_TOPOLOGY


def refuse_unpublishable(results=None, rows=None, where=PUBLISHED_TABLE):
    bad = unpublishable(results)
    if rows is not None:
        bad += [f"{where}: {r.get('run_id')} topology={row_topology(r)}"
                for r in rows if row_topology(r) not in PUBLISHED_TOPOLOGIES]
    if bad:
        raise SystemExit("non-nested:\n  " + "\n  ".join(bad))


def refuse_unpublishable_json(path):
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    t = json_topology(p)
    if t not in PUBLISHED_TOPOLOGIES:
        raise SystemExit(f"{path}: topology={t} unpublishable")


def refuse_unpublishable_experiments(exp=None):
    exp = Path(exp or RESULTS / "experiments")
    if not exp.is_dir():
        return
    bad = [d.name for d in sorted(exp.iterdir())
           if d.is_dir() and experiment_topology(d) not in PUBLISHED_TOPOLOGIES]
    if bad:
        raise SystemExit(f"{exp}: {len(bad)} non-nested "
                         f"({', '.join(bad[:6])}{' ...' if len(bad) > 6 else ''})")


LOAD_CEILING = 1.0
DISK_FLOOR_GB = 20


def check_exclusive(idle_required=True):
    facts = {}
    facts["load1"] = os.getloadavg()[0]
    facts["free_gb"] = round(shutil.disk_usage("/").free / 1e9, 1)

    listing = _cmd(["pgrep", "-af", "build/pipeline"], "").splitlines()
    mine = str(os.getpid())
    real = [l for l in listing
            if not any(w in l for w in ("until ", "pgrep", "sleep ")) and not l.startswith(mine)]
    facts["foreign_pipelines"] = len(real)

    reasons = []
    if facts["free_gb"] < DISK_FLOOR_GB:
        reasons.append(f"disk {facts['free_gb']}G<{DISK_FLOOR_GB}G")
    if facts["foreign_pipelines"]:
        reasons.append(f"foreign_pipelines={facts['foreign_pipelines']}")
    if facts["load1"] > LOAD_CEILING:
        reasons.append(f"load {facts['load1']:.2f}>{LOAD_CEILING}")

    facts["exclusive"] = 0 if reasons else 1
    facts["why"] = "; ".join(reasons) if reasons else "idle"
    if not idle_required:
        facts["exclusive"] = 1
    return facts


SETTLE_POLL_S = 5


def settle(timeout_s, target=LOAD_CEILING, _load=os.getloadavg, _sleep=None):
    import time
    _sleep = _sleep or time.sleep
    target = min(target, LOAD_CEILING)
    before = _load()[0]
    waited = 0
    while _load()[0] > target and waited < timeout_s:
        step = min(SETTLE_POLL_S, timeout_s - waited)
        _sleep(step)
        waited += step
    return {"settle_s": waited, "settled": 0 if _load()[0] > target else 1,
            "settle_target": target, "load1_before": round(before, 2)}


DEDICATED_IDLE_FLOOR = 0.95


def _cpu_times(n):
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith(f"cpu{n} "):
                return [int(x) for x in line.split()[1:9]]
    return None


def check_dedicated_idle(pin_csv, seconds=1.0):
    import time
    cores = [int(c) for c in pin_csv.split(",") if c.strip()]
    before = {c: _cpu_times(c) for c in cores}
    time.sleep(seconds)
    after = {c: _cpu_times(c) for c in cores}
    idle_frac = {}
    for c in cores:
        b, a = before.get(c), after.get(c)
        if not b or not a:
            idle_frac[c] = None
            continue
        d = [x - y for x, y in zip(a, b)]
        total = sum(d)
        idle_frac[c] = (d[3] + d[4]) / total if total > 0 else None
    bad = [c for c, f in idle_frac.items() if f is None or f < DEDICATED_IDLE_FLOOR]
    return {
        "cores": cores, "idle_frac": idle_frac, "dedicated": 0 if bad else 1,
        "why": "idle" if not bad else "; ".join(
            f"core{c} idle {idle_frac[c]}" if idle_frac[c] is not None else f"core{c} unreadable"
            for c in bad),
    }


def cpu_snapshot():
    snap = {}
    with open("/proc/stat") as f:
        for line in f:
            head = line.split(" ", 1)[0]
            if head.startswith("cpu") and head[3:].isdigit():
                snap[int(head[3:])] = [int(x) for x in line.split()[1:9]]
    return snap


def off_pin_busy(before, after, pin_csv):
    pin = {int(c) for c in pin_csv.split(",") if c.strip()}
    cores = sorted(c for c in after if c not in pin)
    busy = total = 0
    for c in cores:
        b, a = before.get(c), after.get(c)
        if not b or not a:
            return {"off_pin_busy_cpus": "-", "off_pin_cores": len(cores), "why": f"core{c}_unreadable"}
        d = [x - y for x, y in zip(a, b)]
        t = sum(d)
        total += t
        busy += t - (d[3] + d[4])
    if total <= 0 or not cores:
        return {"off_pin_busy_cpus": "-", "off_pin_cores": len(cores), "why": "no_elapsed_time"}
    per_core_elapsed = total / len(cores)
    return {"off_pin_busy_cpus": round(busy / per_core_elapsed, 3), "off_pin_cores": len(cores),
            "why": "ok"}


def keep_only_if_still_idle(rows, started_idle):
    if not started_idle:
        for r in rows:
            if r.get("verdict") == "GREEN":
                r["verdict"] = "RED"
                r["verdict_reason"] = "not_exclusive"
            r["host_exclusive"] = 0
        return rows, False
    facts = check_exclusive()
    if facts["exclusive"]:
        return rows, True
    for r in rows:
        if r.get("verdict") == "GREEN":
            r["verdict"] = "RED"
            r["verdict_reason"] = "not_exclusive_end"
        r["host_exclusive"] = 0
    return rows, False


def verdict_for(proc_result, records, expected_frames, exclusive, measurable):
    if proc_result == "timeout":
        return "RED", "hang"
    if proc_result != 0:
        return "RED", "crash"
    if records == 0:
        return "RED", "zero_result"
    if records != expected_frames:
        return "RED", f"count_mismatch:{records}/{expected_frames}"
    if not measurable:
        return "RED", "host_not_measurable"
    if not exclusive:
        return "RED", "not_exclusive"
    return "GREEN", VERDICT_OK


def percentiles(values, prefix="lat"):
    keys = [f"{prefix}_min_ns", f"{prefix}_p50_ns", f"{prefix}_p99_ns", f"{prefix}_p999_ns",
            f"{prefix}_max_ns", f"{prefix}_mean_ns"]
    if not values:
        return {k: "-" for k in keys}
    s = sorted(values)

    def at(p):
        return s[min(int(p * len(s)), len(s) - 1)]

    return {
        f"{prefix}_min_ns": s[0], f"{prefix}_p50_ns": at(0.50), f"{prefix}_p99_ns": at(0.99),
        f"{prefix}_p999_ns": at(0.999), f"{prefix}_max_ns": s[-1],
        f"{prefix}_mean_ns": sum(s) // len(s),
    }


def jitter(values):
    if not values:
        return {"jitter_rms_ns": "-", "jitter_p2p_ns": "-"}
    mean = sum(values) / len(values)
    rms = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    return {"jitter_rms_ns": int(rms), "jitter_p2p_ns": max(values) - min(values)}


def still_ours(pid, marker):
    try:
        return marker in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
    except OSError:
        return False


def stop_recorded(procs, marker, grace=5):
    for p in procs:
        if p.poll() is None and still_ours(p.pid, marker):
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            if still_ours(p.pid, marker):
                p.kill()
