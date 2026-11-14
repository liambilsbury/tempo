#!/usr/bin/env python3
import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TRACE_PROCESSOR = ROOT / "third_party" / "perfetto" / "trace_processor_shell"
SLICE = "held off by the host"
WORK_PERIODS = "rhizo work periods"
CLOCK_BOOTTIME = 6
SLICES_SQL = ("select t.tid as tid, s.ts as ts, s.dur as dur, "
              "extract_arg(s.arg_set_id, 'debug.held_us') as held_us "
              "from slice s join thread_track tt on s.track_id = tt.id join thread t using (utid) "
              f"where s.name = '{SLICE}' order by s.ts, t.tid;")
PRODUCER_SQL = ("select distinct p.pid as pid from process_counter_track pct join process p using (upid) "
                f"where pct.name = '{WORK_PERIODS}';")
CLOCK_SQL = "select int_value as clock from metadata where name = 'trace_time_clock_id';"


class TraceError(Exception):
    pass


def parse_csv(text):
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith('"')), None)
    if start is None:
        return []
    rows = csv.DictReader(io.StringIO("\n".join(l for l in lines[start:] if l.strip())))
    return [{k: (None if v == "[NULL]" else v) for k, v in r.items()} for r in rows]


def query(trace, sql, tp=TRACE_PROCESSOR):
    if not Path(tp).is_file():
        raise TraceError(f"no trace_processor at {tp}")
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
        f.write(sql)
    try:
        r = subprocess.run([str(tp), "-q", f.name, str(trace)], capture_output=True, text=True, timeout=600)
    finally:
        os.unlink(f.name)
    if r.returncode != 0:
        raise TraceError(f"trace_processor {trace}: exit {r.returncode}: {r.stderr.strip()[-300:]}")
    return parse_csv(r.stdout)


def slices_of(rows, tids):
    out = []
    for r in rows:
        tid = int(r["tid"])
        if tid in tids and r["held_us"] is not None:
            out.append([tid, int(r["ts"]), int(r["dur"]), int(float(r["held_us"])) * 1000])
    return out


def read(trace, tids, pid, run_query=query):
    clock_rows = run_query(trace, CLOCK_SQL)
    if clock_rows and clock_rows[0]["clock"] is not None:
        clock, source = int(clock_rows[0]["clock"]), "metadata"
    else:
        clock, source = CLOCK_BOOTTIME, "default"
    if clock != CLOCK_BOOTTIME:
        raise TraceError(f"{trace}: clock {clock} != {CLOCK_BOOTTIME}")
    pids = sorted({int(r["pid"]) for r in run_query(trace, PRODUCER_SQL)})
    if pid not in pids:
        raise TraceError(f"{trace}: no counters pid {pid}")
    rows = run_query(trace, SLICES_SQL)
    watched = [r for r in rows if int(r["tid"]) in set(tids)]
    if watched and all(r["held_us"] is None for r in watched):
        raise TraceError(f"{trace}: no held_us")
    return {"clock": clock, "clock_source": source, "producer_pids": pids, "slices": slices_of(rows, set(tids))}


def selftest():
    cases = []
    text = 'a banner line\n"tid","ts","dur","held_us"\n101,5000,2000000,1500\n102,6000,1000,[NULL]\n'
    rows = parse_csv(text)
    cases.append(("parse_csv: rows after banner, [NULL] -> None", len(rows) == 2 and rows[1]["held_us"] is None))
    cases.append(("slices_of tid 101: held_us -> ns", slices_of(rows, {101}) == [[101, 5000, 2000000, 1500000]]))
    cases.append(("slices_of tid 102, held_us null: []", slices_of(rows, {102}) == []))
    cases.append(("parse_csv no header: []", parse_csv("nothing\n") == []))

    def fake(clock, pids, slices):
        def q(trace, sql):
            if sql == CLOCK_SQL:
                return [{"clock": clock}] if clock is not None else []
            if sql == PRODUCER_SQL:
                return [{"pid": str(p)} for p in pids]
            return slices
        return q
    good = read("t", [101], 101, fake("6", [101, 7], rows))
    cases.append(("clock=6, producer pid: read, clock_source=metadata",
                  good["slices"] == [[101, 5000, 2000000, 1500000]] and good["clock_source"] == "metadata"))
    cases.append(("no clock row: clock_source=default",
                  read("t", [101], 101, fake(None, [101], rows))["clock_source"] == "default"))
    cases.append(("held_us on unwatched tids only: slices=[]",
                  read("t", [103], 101, fake("6", [101], rows))["slices"] == []))
    nulls = parse_csv('"tid","ts","dur","held_us"\n101,5000,2000000,[NULL]\n101,9000,1000,[NULL]\n102,6000,1000,1500\n')
    for name, q in (("clock=3: TraceError", fake("3", [101], rows)),
                    ("pid not in producer pids: TraceError", fake("6", [7], rows)),
                    ("watched held_us all null: TraceError", fake("6", [101], nulls))):
        try:
            read("t", [101], 101, q)
            cases.append((name, False))
        except TraceError:
            cases.append((name, True))
    try:
        query("t", "select 1;", tp=Path("/nonexistent/trace_processor_shell"))
        cases.append(("trace_processor missing: TraceError", False))
    except TraceError:
        cases.append(("trace_processor missing: TraceError", True))
    bad = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'ok' if ok else 'FAIL'} {n}")
    print(f"selftest {len(cases) - len(bad)}/{len(cases)} ok")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", nargs="?")
    ap.add_argument("--tids", default="")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--trace-processor", default=str(TRACE_PROCESSOR))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.trace or a.pid is None or not a.tids:
        ap.error("need TRACE --tids --pid")
    tids = [int(t) for t in a.tids.split(",")]
    try:
        out = read(a.trace, tids, a.pid, lambda t, s: query(t, s, a.trace_processor))
    except TraceError as e:
        print(e, file=sys.stderr)
        return 3
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
