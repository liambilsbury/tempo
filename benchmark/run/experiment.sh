#!/usr/bin/env bash
# run the control loop in L2 while injecting interference at L1, recording both sides
#
#   benchmark/run/experiment.sh --label wake-2k --inject wake --rate-hz 2000 --pin 8,9,10,11
#   benchmark/run/experiment.sh --label control --inject none --pin 8,9,10,11
#   benchmark/run/experiment.sh --label tlb-x --inject tlb --frames 30000 --inject-s 20
#
#   --inject-s N               inject for N seconds, default the whole loop
#   --pin L                    host CPUs, default HOST_PINS
#   TEMPO_NO_PROBE=1           skip the L1 IPI probe
#   TEMPO_NO_EXIT_PROBE=1      skip the VM exit counter
#   TEMPO_CHECK_DEDICATED=1    check the pinned cores were idle before the run
#   TEMPO_TRIGGER=irq          frames start on a UART interrupt
#   TEMPO_LEVEL=host           run the loop on this host with no guest
#   TEMPO_LATE_RANGE=A-B       also report lost frames and the worst case over frames [A, B)
#   TEMPO_SETTLE_S=N           wait up to N s for an idle host
#   TEMPO_SLOT_US=N            hog slot in us
#   TEMPO_KERNEL, TEMPO_GUEST_ARGS   the L2 kernel and its arguments
#   TEMPO_RECONSTRUCT, TEMPO_TIER, TEMPO_ADAPT, TEMPO_BLOCK, TEMPO_BLOCK_NS   the loop's arms
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
ROOT="$PWD"
. benchmark/probes/pid.sh
. benchmark/host/settings.conf

LABEL="" INJECT=none RATE=2000 DUTY=20 RTPRIO=0 PIN="$HOST_PINS" FRAMES=5000 PERIOD=1000000
VCPUS=4 SCHED=fifo KERNEL="" GUEST_ARGS="" SLOPES=3200 ACTS=500 INJECT_S=0
NO_PROBE="${TEMPO_NO_PROBE:-0}"
NO_EXIT_PROBE="${TEMPO_NO_EXIT_PROBE:-0}"
KERNEL="${TEMPO_KERNEL:-$KERNEL}"
GUEST_ARGS="${TEMPO_GUEST_ARGS:-$GUEST_ARGS}"
RECONSTRUCT="${TEMPO_RECONSTRUCT:-dense}"
TIER="${TEMPO_TIER:-0}"
ADAPT="${TEMPO_ADAPT:-off}"
BLOCK="${TEMPO_BLOCK:-16}"
BLOCK_NS="${TEMPO_BLOCK_NS:-0}"
SLOT_US="${TEMPO_SLOT_US:-1000}"
TRIGGER="${TEMPO_TRIGGER:-timer}"

while [ $# -gt 0 ]; do
    case "$1" in
        --label) LABEL="$2"; shift 2;;
        --inject) INJECT="$2"; shift 2;;
        --rate-hz) RATE="$2"; shift 2;;
        --duty) DUTY="$2"; shift 2;;
        --rt-prio) RTPRIO="$2"; shift 2;;
        --pin) PIN="$2"; shift 2;;
        --frames) FRAMES="$2"; shift 2;;
        --period-ns) PERIOD="$2"; shift 2;;
        --vcpus) VCPUS="$2"; shift 2;;
        --slopes) SLOPES="$2"; shift 2;;
        --acts) ACTS="$2"; shift 2;;
        --inject-s) INJECT_S="$2"; shift 2;;
        --sched) SCHED="$2"; shift 2;;
        --kernel) KERNEL="$2"; shift 2;;
        --guest-args) GUEST_ARGS="$2"; shift 2;;
        -h|--help) sed -n '2,19p' "$0"; exit 0;;
        *) echo "experiment: unknown argument '$1'" >&2; exit 2;;
    esac
done
[ -n "$LABEL" ] || { echo "experiment: --label is required" >&2; exit 2; }
LEVEL="${TEMPO_LEVEL:-guest}"
case "$LEVEL" in
    guest) ;;
    host)
        [ -z "$KERNEL" ] || { echo "experiment: refusing -- TEMPO_LEVEL=host takes no guest kernel: $KERNEL" >&2; exit 2; }
        [ -z "$GUEST_ARGS" ] || { echo "experiment: refusing -- TEMPO_LEVEL=host takes no guest kernel arguments" >&2; exit 2; }
        [ "$TRIGGER" = timer ] || { echo "experiment: refusing -- TEMPO_LEVEL=host takes only the timer trigger" >&2; exit 2; }
        NO_EXIT_PROBE=1 ;;
    *) echo "experiment: refusing -- TEMPO_LEVEL must be guest or host, not '$LEVEL'" >&2; exit 2;;
esac
LATE_RANGE="${TEMPO_LATE_RANGE:-}"
if [ -n "$LATE_RANGE" ]; then
    [[ "$LATE_RANGE" =~ ^([0-9]+)-([0-9]+)$ ]] && [ "${BASH_REMATCH[1]}" -lt "${BASH_REMATCH[2]}" ] \
        || { echo "experiment: refusing -- TEMPO_LATE_RANGE must be A-B with A < B, not '$LATE_RANGE'" >&2; exit 2; }
fi

OUT="$ROOT/results/experiments/$LABEL"
rm -rf "$OUT"; mkdir -p "$OUT"

# print the start check of an exclusive host and its topology for host.meta
host_assert() {
    python3 -c "
import sys; sys.path.insert(0, '.')
from benchmark import lib
f = lib.check_exclusive()
print(' '.join(f'{k}={v}' for k, v in f.items() if k != 'why'))
print('why=' + f['why'].replace(' ', '_'))
# whether L1 is nested or single-level
t = lib.host_topology()
print(f\"topology={t['topology']} hypervisor_flag={t['hypervisor_flag']} detect_virt={t['detect_virt']}\")
print('topology_why=' + t['why'].replace(' ', '_'))
" 2>/dev/null
}
SETTLE_LINE=""
if [ "${TEMPO_SETTLE_S:-0}" -gt 0 ]; then
    SETTLE_LINE="$(python3 -c "
import sys; sys.path.insert(0, '.')
from benchmark import lib
f = lib.settle(int(sys.argv[1]), float(sys.argv[2] or lib.LOAD_CEILING))
print(' '.join(f'{k}={v}' for k, v in f.items()))
" "$TEMPO_SETTLE_S" "${TEMPO_SETTLE_LOAD:-}" 2>/dev/null)"
fi
host_assert > "$OUT/host.meta"
[ -n "$SETTLE_LINE" ] && echo "$SETTLE_LINE" >> "$OUT/host.meta"
if [ "${TEMPO_CHECK_DEDICATED:-0}" = 1 ]; then
    python3 -c "
import sys; sys.path.insert(0, '.')
from benchmark import lib
f = lib.check_dedicated_idle('$PIN')
print(' '.join(f'{k}={v}' for k, v in f.items() if k not in ('why', 'idle_frac')))
print('why=' + f['why'].replace(' ', '_'))
" 2>/dev/null >> "$OUT/host.meta"
fi
make -s >/dev/null || { echo "experiment: build failed" >&2; exit 2; }
bash tools/build/make_image.sh >/dev/null || exit 2

RUN_S=$(( FRAMES * PERIOD / 1000000000 + 1 ))
[ "$RUN_S" -lt 2 ] && RUN_S=2
[ "$INJECT_S" -gt 0 ] && RUN_S="$INJECT_S"

echo "experiment: $LABEL  inject=$INJECT pin=$PIN frames=$FRAMES loop=~${RUN_S}s probe=$([ "$NO_PROBE" = 1 ] && echo off || echo on)"
echo "experiment: recon=$RECONSTRUCT tier=$TIER adapt=$ADAPT block=$BLOCK block_ns=$BLOCK_NS slot_us=$SLOT_US"

BPFTRACE_BIN="$(command -v bpftrace || echo /usr/bin/bpftrace)"
IPIPROBE_CMD="$BPFTRACE_BIN benchmark/probes/ipi_origin.bt -o $OUT/l1-events.txt"
IPIPROBE_RE="$(exact_re "$IPIPROBE_CMD")"
IPIPROBE_PIDFILE="$OUT/.ipiprobe.pid"
LEFTOVER="$(pgrep -f 'bpftrace benchmark/probes/ipi_origin.bt' | wc -l)"
echo "leftover_ipi_probes=$LEFTOVER" > "$OUT/probe.meta.pre"
[ "$LEFTOVER" -gt 0 ] && echo "experiment: WARNING $LEFTOVER leftover ipi probe(s) already running; left running, recorded in probe.meta" >&2
if [ "$NO_PROBE" = 1 ]; then
    echo "experiment: TEMPO_NO_PROBE=1; ipi probe not started"
    : > "$OUT/l1-events.txt"
else
    # shellcheck disable=SC2024
    sudo "$BPFTRACE_BIN" benchmark/probes/ipi_origin.bt -o "$OUT/l1-events.txt" >"$OUT/l1-probe.log" 2>&1 &
    PROBE_WAIT=0
    while [ ! -s "$OUT/l1-events.txt" ] && [ "$PROBE_WAIT" -lt 30 ]; do
        sleep 1; PROBE_WAIT=$((PROBE_WAIT+1))
    done
    [ -s "$OUT/l1-events.txt" ] || echo "experiment: WARNING probe produced nothing in ${PROBE_WAIT}s" >&2
    record_exact_pid "$IPIPROBE_RE" "$IPIPROBE_PIDFILE" || \
        echo "experiment: WARNING ipi probe pid not resolved; matches: $(count_matching "$IPIPROBE_RE")" >&2
fi



OFF_PIN_BEFORE="$OUT/.off_pin-before.json"
python3 -c "
import json, sys; sys.path.insert(0, '.')
from benchmark import lib; json.dump(lib.cpu_snapshot(), open(sys.argv[1], 'w'))
" "$OFF_PIN_BEFORE" 2>/dev/null

if [ "$LEVEL" = host ]; then
echo "experiment: level=host; running the loop on this host with benchmark/run/l0.sh"
bash benchmark/run/l0.sh --label "$LABEL" --frames "$FRAMES" --period-ns "$PERIOD" \
      --sched "$SCHED" --pin "$PIN" --outdir "$OUT/l2" \
      --slopes "$SLOPES" --acts "$ACTS" \
      --reconstruct "$RECONSTRUCT" --tier "$TIER" --adapt "$ADAPT" --block "$BLOCK" --block-ns "$BLOCK_NS" \
      >"$OUT/l0-run.log" 2>&1 &
L2PID=$!
READY=0
for i in $(seq 1 600); do
    [ -e "$OUT/l2/ready" ] && { READY=1; break; }
    kill -0 "$L2PID" 2>/dev/null || break
    sleep 0.05
done
echo "experiment: host loop ready=$READY after $(echo "$i" | awk '{print $1/20}')s"
QPID=""
if [ "$READY" != 1 ]; then
    echo "experiment: the host loop never became ready; see $OUT/l0-run.log" >&2
    kill "$L2PID" 2>/dev/null
    wait "$L2PID" 2>/dev/null
    if [ "$NO_PROBE" != 1 ]; then
        stop_exact "$IPIPROBE_PIDFILE" "$IPIPROBE_RE" 2>>"$OUT/probe-stop.log" || \
            echo "experiment: WARNING ipi probe pid not ours or not recorded; left running, see $OUT/probe-stop.log" >&2
    fi
    rm -f "$IPIPROBE_PIDFILE"
    exit 4
fi
else
( bash benchmark/run/l2.sh --label "$LABEL" --frames "$FRAMES" --period-ns "$PERIOD" \
      --vcpus "$VCPUS" --sched "$SCHED" --pin "$PIN" --outdir "$OUT/l2" \
      --slopes "$SLOPES" --acts "$ACTS" \
      --reconstruct "$RECONSTRUCT" --tier "$TIER" --adapt "$ADAPT" --block "$BLOCK" --block-ns "$BLOCK_NS" \
      --trigger "$TRIGGER" \
      ${KERNEL:+--kernel "$KERNEL"} ${GUEST_ARGS:+--guest-args "$GUEST_ARGS"} \
      >"$OUT/l2-run.log" 2>&1 ) &
L2PID=$!

BOOTED=0
for i in $(seq 1 60); do
    if ps -eo comm | grep -q qemu; then BOOTED=1; break; fi
    sleep 0.5
done
echo "experiment: qemu up after $(echo "$i" | awk '{print $1/2}')s, booted=$BOOTED"

QPID=""
for i in $(seq 1 40); do
    for p in $(pgrep qemu-system-x86 2>/dev/null); do
        if tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "tempo\.label=$LABEL\b"; then
            QPID="$p"; break
        fi
    done
    [ -n "$QPID" ] && break
    sleep 0.25
done
echo "experiment: qemu pid=${QPID:-none} for label=$LABEL"

if [ -z "$QPID" ]; then
    echo "experiment: no guest booted; see $OUT/l2-run.log" >&2
    kill "$L2PID" 2>/dev/null
    wait "$L2PID" 2>/dev/null
    if [ "$NO_PROBE" != 1 ]; then
        stop_exact "$IPIPROBE_PIDFILE" "$IPIPROBE_RE" 2>>"$OUT/probe-stop.log" || \
            echo "experiment: WARNING ipi probe pid not ours or not recorded; left running, see $OUT/probe-stop.log" >&2
    fi
    rm -f "$IPIPROBE_PIDFILE"
    exit 4
fi
fi

TRIGPID=""
if [ "$TRIGGER" = irq ]; then
    ./build/irq_trigger --sock "$OUT/l2/irq_trigger.sock" --period-ns "$PERIOD" --duration-s "$RUN_S" \
        --start-delay-s 2 \
        > "$OUT/irq_trigger.meta" 2>&1 &
    TRIGPID=$!
fi




EXIT_PROBE_PIDFILE="$OUT/.exit_probe.pid"
EXIT_PROBE_ON=0
EXIT_PROBE_CMD="$BPFTRACE_BIN benchmark/probes/vmexit.bt $QPID -o $OUT/exits.txt"
EXIT_PROBE_RE="$(exact_re "$EXIT_PROBE_CMD")"
rm -f "$EXIT_PROBE_PIDFILE"
if [ "$NO_EXIT_PROBE" = 1 ]; then
    echo "experiment: TEMPO_NO_EXIT_PROBE=1; exit counter not started"
    : > "$OUT/exits.txt"
elif [ -z "$QPID" ]; then
    echo "experiment: WARNING no qemu pid resolved; exit counter not started" >&2
    : > "$OUT/exits.txt"
else
    # shellcheck disable=SC2024
    sudo "$BPFTRACE_BIN" benchmark/probes/vmexit.bt "$QPID" -o "$OUT/exits.txt" \
        >"$OUT/exit_probe.log" 2>&1 &
    EXIT_PROBE_ON=1
    EW=0
    while [ ! -s "$OUT/exits.txt" ] && [ "$EW" -lt 30 ]; do sleep 1; EW=$((EW+1)); done
    [ -s "$OUT/exits.txt" ] || echo "experiment: WARNING exit counter produced nothing in ${EW}s" >&2
    if ! record_exact_pid "$EXIT_PROBE_RE" "$EXIT_PROBE_PIDFILE"; then
        EXIT_PROBE_PIDS="$(pgrep -f -x "$EXIT_PROBE_RE" 2>/dev/null)"
        echo "experiment: WARNING exit counter pid not resolved; matches: ${EXIT_PROBE_PIDS:-none}" >&2
    fi
fi

WATCH=(--watch qemu); SUDOWATCH=()
if [ "$LEVEL" = host ]; then
    WATCH=(--watch pipeline); SUDOWATCH=("${WATCH[@]}")
    python3 benchmark/analysis/level0.py snap "$OUT/l2/stages.pid" > "$OUT/.l0-before" 2>>"$OUT/l0-run.log"
fi
if [ -n "$LATE_RANGE" ]; then
    python3 -c "
import time
a = time.clock_gettime_ns(time.CLOCK_MONOTONIC); r = time.clock_gettime_ns(time.CLOCK_REALTIME)
b = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
print(f'host_real_minus_mono_ns={r - (a + (b - a) // 2)}')" > "$OUT/clock.meta" 2>/dev/null
fi
if [ "$INJECT" != none ]; then
    if [ "$RTPRIO" -gt 0 ]; then
        # shellcheck disable=SC2024
        sudo ./build/inject --mode "$INJECT" --cpus "$PIN" --duration-s "$RUN_S" \
            --rate-hz "$RATE" --duty "$DUTY" --rt-prio "$RTPRIO" --slot-us "$SLOT_US" "${SUDOWATCH[@]}" \
            > "$OUT/inject.meta" 2>&1
    else
        ./build/inject --mode "$INJECT" --cpus "$PIN" --duration-s "$RUN_S" \
            --rate-hz "$RATE" --duty "$DUTY" --slot-us "$SLOT_US" "${WATCH[@]}" \
            > "$OUT/inject.meta" 2>&1
    fi
else
    ./build/inject --mode none --cpus "$PIN" --duration-s "$RUN_S" "${WATCH[@]}" \
        > "$OUT/inject.meta" 2>&1
fi

if [ "$LEVEL" = host ]; then
    python3 benchmark/analysis/level0.py snap "$OUT/l2/stages.pid" > "$OUT/.l0-after" 2>>"$OUT/l0-run.log"
    python3 benchmark/analysis/level0.py window "$OUT/.l0-before" "$OUT/.l0-after" > "$OUT/l0.meta" 2>>"$OUT/l0-run.log"
    rm -f "$OUT/.l0-before" "$OUT/.l0-after"
fi

if [ "$EXIT_PROBE_ON" = 1 ]; then
    if [ -s "$EXIT_PROBE_PIDFILE" ]; then
        if stop_exact "$EXIT_PROBE_PIDFILE" "$EXIT_PROBE_RE" 2>>"$OUT/probe-stop.log"
        then
            sleep 2
            echo "exit_probe=on pid=$QPID" > "$OUT/exits.meta"
        else
            echo "experiment: WARNING exit counter pid $(cat "$EXIT_PROBE_PIDFILE") not stopped; see $OUT/probe-stop.log" >&2
            echo "exit_probe=lost" > "$OUT/exits.meta"
        fi
    else
        echo "experiment: WARNING exit counter pid was never recorded" >&2
        echo "exit_probe=lost" > "$OUT/exits.meta"
    fi
    rm -f "$EXIT_PROBE_PIDFILE"
else
    echo "exit_probe=off" > "$OUT/exits.meta"
fi

[ -n "$TRIGPID" ] && wait "$TRIGPID" 2>/dev/null
wait "$L2PID"; L2RC=$?

grep -E "watched_cpu_window_ns" "$OUT/inject.meta" | tr "\n" " "; echo
if [ "$NO_PROBE" = 1 ]; then
    echo "probe=off" > "$OUT/probe.meta"
else
    if stop_exact "$IPIPROBE_PIDFILE" "$IPIPROBE_RE" 2>>"$OUT/probe-stop.log"; then
        sleep 2
        echo "probe=on" > "$OUT/probe.meta"
    else
        echo "experiment: WARNING ipi probe pid not ours or not recorded; left running, see $OUT/probe-stop.log" >&2
        echo "probe=lost" > "$OUT/probe.meta"
    fi
    python3 benchmark/probes/check.py --coverage "$OUT/l1-events.txt" "$OUT/inject.meta" "$OUT/l1-probe.log" \
        >> "$OUT/probe.meta" 2>"$OUT/probe_check.err"
    [ -s "$OUT/probe_check.err" ] && echo "experiment: WARNING probe coverage check failed; see $OUT/probe_check.err" >&2
    grep -q '^probe_covers_inject=0$' "$OUT/probe.meta" && \
        echo "experiment: WARNING probe record ends before the injection window or lost events" >&2
fi
cat "$OUT/probe.meta.pre" >> "$OUT/probe.meta"
rm -f "$OUT/probe.meta.pre" "$IPIPROBE_PIDFILE"
[ -s "$OUT/probe-stop.log" ] || rm -f "$OUT/probe-stop.log"

python3 -c "
import json, sys; sys.path.insert(0, '.')
from benchmark import lib
b = {int(k): v for k, v in json.load(open(sys.argv[1])).items()}
r = lib.off_pin_busy(b, lib.cpu_snapshot(), sys.argv[2])
print(f\"off_pin_busy_cpus={r['off_pin_busy_cpus']} off_pin_cores={r['off_pin_cores']} off_pin_why={r['why']}\")
" "$OFF_PIN_BEFORE" "$PIN" 2>/dev/null >> "$OUT/host.meta" || echo "off_pin_busy_cpus=- off_pin_why=snapshot_failed" >> "$OUT/host.meta"
rm -f "$OFF_PIN_BEFORE"

{
    python3 -c "
import sys; sys.path.insert(0, '.')
from benchmark import lib
f = lib.check_exclusive()
print(f\"exclusive_end={f['exclusive']}\")
print(f\"load1_end={f['load1']} why_end={f['why'].replace(' ', '_')}\")
" 2>/dev/null || echo "exclusive_end="
} >> "$OUT/host.meta"

echo "experiment: l2 exit=$L2RC"
[ -f "$OUT/l2/telemetry.bin" ] || { echo "experiment: no records came back" >&2; exit 1; }

python3 benchmark/analysis/exits/compare.py --outdir "$OUT" --pin "$PIN" --period-ns "$PERIOD" ${LATE_RANGE:+--late-range "$LATE_RANGE"} \
    | tee "$OUT/summary.txt"

if [ "$EXIT_PROBE_ON" = 1 ]; then
    python3 benchmark/analysis/exits/exits.py --outdir "$OUT" | tee "$OUT/exits-summary.txt"
fi
