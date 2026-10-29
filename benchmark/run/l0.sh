#!/usr/bin/env bash
# run a matrix row's control loop on the host with no VM
#
# Usage: benchmark/run/l0.sh --label x --frames 20000 --pin 8,9,10,11 --outdir <run>/l2 [row arguments]
#   --label NAME        label written to header.txt, default unlabelled
#   --frames N          frames to run, default 2000
#   --period-ns N       frame period in ns, default 1000000
#   --slopes N          slope count, default 800
#   --acts N            actuator count, default 200
#   --sched P           stage scheduling policy, default fifo
#   --prio N            stage priority, default 80
#   --pin CPUS          host cores, at least four
#   --outdir DIR        output directory
#   --timeout S         stop the stages after S seconds, default frames x period + 180
#   --reconstruct M     compute stage reconstruction, default dense
#   --tier N            compute stage tier, default 0
#   --modes N           compute stage modes, default 0
#   --alpha X           compute stage alpha, default 1.0
#   --adapt M           compute stage adaptation, default off
#   --block N           compute stage block, default 16
#   --block-ns N        compute stage block-ns, default 0
#   TEMPO_L0_BIN=PATH   run this binary, default build/l2/bin/pipeline



set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
ROOT="$PWD"

LABEL=unlabelled FRAMES=2000 PERIOD=1000000 SLOPES=800 ACTS=200 SCHED=fifo PRIO=80
PIN="" OUTDIR="" TIMEOUT=0
RECONSTRUCT=dense TIER=0 MODES=0 ALPHA=1.0 ADAPT=off BLOCK=16 BLOCKNS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --label) LABEL="$2"; shift 2;;
        --frames) FRAMES="$2"; shift 2;;
        --period-ns) PERIOD="$2"; shift 2;;
        --slopes) SLOPES="$2"; shift 2;;
        --acts) ACTS="$2"; shift 2;;
        --sched) SCHED="$2"; shift 2;;
        --prio) PRIO="$2"; shift 2;;
        --pin) PIN="$2"; shift 2;;
        --outdir) OUTDIR="$2"; shift 2;;
        --timeout) TIMEOUT="$2"; shift 2;;
        --reconstruct) RECONSTRUCT="$2"; shift 2;;
        --tier) TIER="$2"; shift 2;;
        --modes) MODES="$2"; shift 2;;
        --alpha) ALPHA="$2"; shift 2;;
        --adapt) ADAPT="$2"; shift 2;;
        --block) BLOCK="$2"; shift 2;;
        --block-ns) BLOCKNS="$2"; shift 2;;
        -h|--help) sed -n '2,25p' "$0"; exit 0;;
        *) echo "run_l0: unknown argument '$1'" >&2; exit 2;;
    esac
done
[ -n "$OUTDIR" ] || { echo "run_l0: --outdir is required" >&2; exit 2; }
IFS=, read -ra P <<<"$PIN"
[ "${#P[@]}" -ge 4 ] || { echo "run_l0: refusing -- --pin needs at least four host cores, got '${PIN:-none}'" >&2; exit 2; }
L0_MEMLOCK_KIB=8192
ML="$(ulimit -l)"
if [ "$ML" != unlimited ] && [ "$ML" -lt "$L0_MEMLOCK_KIB" ]; then
    echo "run_l0: refusing -- memlock limit is ${ML} KiB, the loop needs ${L0_MEMLOCK_KIB} KiB; raise ulimit -l" >&2
    exit 2
fi
BIN="${TEMPO_L0_BIN:-$ROOT/build/l2/bin/pipeline}"
[ -x "$BIN" ] || bash tools/build/make_image.sh >/dev/null
[ -x "$BIN" ] || { echo "run_l0: $BIN is not built; run bash tools/build/make_image.sh" >&2; exit 2; }
[ "$TIMEOUT" -eq 0 ] && TIMEOUT=$(( FRAMES * PERIOD / 1000000000 + 180 ))

mkdir -p "$OUTDIR"
LOGS="$OUTDIR/l0-logs"; rm -rf "$LOGS"; mkdir -p "$LOGS"
rm -f "$OUTDIR/stages.pid" "$OUTDIR/ready" "$OUTDIR/telemetry.bin" "$OUTDIR/header.txt"
SHM="$(mktemp -d /dev/shm/tempo-l0-XXXXXX)" || { echo "run_l0: cannot create a shm directory" >&2; exit 2; }
export MILK_SHM_DIR="$SHM"

PREEMPT="$(grep -oE 'PREEMPT_RT|PREEMPT_DYNAMIC|PREEMPT|VOLUNTARY' /proc/version | head -1)"
[ "$(cat /sys/kernel/realtime 2>/dev/null)" = 1 ] && PREEMPT=PREEMPT_RT
{
    echo "kernel_arm=host kernel_path=- kernel_src=-"
    echo "kernel_image_release=$(uname -r) kernel_image_preempt=${PREEMPT:--} host_release=$(uname -r)"
    echo "kernel_sha256=-"
    echo "level=host pipeline=$BIN pipeline_sha256=$(sha256sum "$BIN" | cut -d' ' -f1)"
    echo "sched_rt_runtime_us=$(cat /proc/sys/kernel/sched_rt_runtime_us 2>/dev/null || echo -) sched_rt_period_us=$(cat /proc/sys/kernel/sched_rt_period_us 2>/dev/null || echo -)"
} > "$OUTDIR/kernel.meta"

# print the steal field of /proc/stat
steal() { awk '/^cpu /{print $9}' /proc/stat; }
STEAL_B="$(steal)"
COMMON=(--tag l0 --frames "$FRAMES" --period-ns "$PERIOD")
# check that a pid is still one of this binary's stages
ours() { [ -d "/proc/$1" ] && tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -qF -- "$BIN --stage"; }
# send SIGTERM then SIGKILL to the recorded stages
stop_stages() {
    local p
    for p in "${PIDS[@]}"; do ours "$p" && kill -TERM "$p" 2>/dev/null; done
    sleep 1
    for p in "${PIDS[@]}"; do ours "$p" && kill -KILL "$p" 2>/dev/null; done
}
trap 'stop_stages; rm -rf -- "$SHM"; exit 143' TERM INT
PIDS=()
# start a stage confined to the pins and record its pid
start() {
    local log="$1" core="$2"; shift 2
    local pin=(); [ "$core" != - ] && pin=(--cpu "$core")
    taskset -c "$PIN" "$BIN" "$@" "${pin[@]}" > "$LOGS/$log.log" 2>&1 &
    PIDS+=("$!")
}
start telemetry "${P[3]}" --stage telemetry "${COMMON[@]}" --slopes "$SLOPES" --acts "$ACTS" \
    --sched "$SCHED" --prio "$PRIO" --out "$OUTDIR/telemetry.bin"
start actuator "${P[2]}" --stage actuator "${COMMON[@]}" --slopes "$SLOPES" --acts "$ACTS" \
    --sched "$SCHED" --prio "$PRIO"
start compute "${P[1]}" --stage compute "${COMMON[@]}" --slopes "$SLOPES" --acts "$ACTS" \
    --sched "$SCHED" --prio "$PRIO" --reconstruct "$RECONSTRUCT" --tier "$TIER" --modes "$MODES" \
    --alpha "$ALPHA" --adapt "$ADAPT" --block "$BLOCK" --block-ns "$BLOCKNS"
start detector - --stage detector "${COMMON[@]}" --sched "$SCHED" --prio "$PRIO"
start sensor "${P[0]}" --stage sensor "${COMMON[@]}" --slopes "$SLOPES" --acts "$ACTS" \
    --sched "$SCHED" --prio "$PRIO" --trigger timer --irq-dev /dev/ttyS1
SPID="${PIDS[4]}"
echo "${PIDS[*]}" > "$OUTDIR/stages.pid"

T0=$SECONDS
while ! grep -q 'downstream_ready=1' "$LOGS/sensor.log" 2>/dev/null; do
    kill -0 "$SPID" 2>/dev/null || break
    [ $((SECONDS - T0)) -ge 30 ] || [ $((SECONDS - T0)) -ge "$TIMEOUT" ] && break
    sleep 0.05
done
grep -q 'downstream_ready=1' "$LOGS/sensor.log" 2>/dev/null && : > "$OUTDIR/ready"

RC=""
while kill -0 "$SPID" 2>/dev/null; do
    if [ $((SECONDS - T0)) -ge "$TIMEOUT" ]; then
        echo "run_l0: the loop is still running after ${TIMEOUT}s; stopping its stages" >&2
        stop_stages; RC=124; break
    fi
    sleep 0.2
done
if [ -z "$RC" ]; then wait "$SPID"; RC=$?; fi
for p in "${PIDS[@]}"; do
    for _ in $(seq 150); do kill -0 "$p" 2>/dev/null || break; sleep 0.2; done
done
for p in "${PIDS[@]}"; do kill -0 "$p" 2>/dev/null && { stop_stages; break; }; done
RCS=()
for p in "${PIDS[@]}"; do wait "$p" 2>/dev/null; RCS+=("$?"); done
[ "${RCS[4]}" != 0 ] && [ "$RC" = 0 ] && RC="${RCS[4]}"
rm -rf -- "$SHM"

MLF="$(grep -l 'mlockall failed' "$LOGS"/*.log 2>/dev/null | wc -l)"
BAD=""
[ "$MLF" != 0 ] && BAD="mlockall failed in $MLF stage(s)"
for i in "${!RCS[@]}"; do [ "${RCS[$i]}" != 0 ] && BAD="${BAD:+$BAD; }a stage exited ${RCS[$i]}"; done
if [ -n "$BAD" ] && [ -f "$OUTDIR/telemetry.bin" ]; then
    mv -f "$OUTDIR/telemetry.bin" "$OUTDIR/telemetry.rejected"
    echo "run_l0: failed run: $BAD; records moved to $OUTDIR/telemetry.rejected, logs in $LOGS" >&2
fi
SZ=0; [ -f "$OUTDIR/telemetry.bin" ] && SZ=$(stat -c %s "$OUTDIR/telemetry.bin")
{
    echo "TEMPO-RESULT-V1"
    echo "label=$LABEL"
    echo "frames=$FRAMES"
    echo "period_ns=$PERIOD"
    echo "sched=$SCHED"
    echo "kernel=$(uname -r)"
    echo "clocksource=$(cat /sys/devices/system/clocksource/clocksource0/current_clocksource 2>/dev/null)"
    echo "steal_before=$STEAL_B"
    echo "steal_after=$(steal)"
    echo "sensor_rc=$RC"
    grep -h 'OBS det_' "$LOGS/detector.log" 2>/dev/null | sed 's/OBS //'
    grep -h 'OBS det_' "$LOGS/compute.log" 2>/dev/null | sed 's/OBS det_/inloop_/'
    grep -h 'OBS modes=' "$LOGS/compute.log" 2>/dev/null | sed 's/OBS modes=/recon_modes=/'
    grep -h 'OBS clock_pair_' "$LOGS/sensor.log" 2>/dev/null | sed 's/OBS //'
    echo "recon=$RECONSTRUCT"
    echo "tier=$TIER"
    echo "adapt=$ADAPT"
    echo "block=$BLOCK"
    echo "blockns=$BLOCKNS"
    echo "level=host"
    echo "mlock_failed_stages=$MLF"
    echo "stage_rcs=$(IFS=,; echo "${RCS[*]}")"
    echo "bytes=$SZ"
    echo "END"
} > "$OUTDIR/header.txt"

echo "run_l0: $LABEL  pin=$PIN frames=$FRAMES sched=$SCHED prio=$PRIO rc=$RC"
echo "run_l0: kernel host $(uname -r) ${PREEMPT:--}"
if [ "$SZ" -gt 0 ]; then
    echo "run_l0: records  $OUTDIR/telemetry.bin ($SZ bytes, $((SZ / 72)) frames)"
else
    echo "run_l0: NO RECORDS returned; see $LOGS" >&2
    exit 1
fi
[ "$RC" = 0 ] && [ -z "$BAD" ] || exit 1
exit 0
