#!/usr/bin/env bash
usage() {
    cat <<'EOF2'
usage: benchmark/run/rhizo/run.sh --label L --pin LIST [--inject none|cpu-load --duty PCT --rt-prio N]
           [--frames N] [--period-ns N] [--slopes N] [--acts N] [--dry-run]
env: TEMPO_RHIZO_LEVEL TEMPO_RHIZO_ADPF TEMPO_RHIZO_INJECT TEMPO_RHIZO_SLOT_US TEMPO_RHIZO_TELEMETRY_ADPF
     TEMPO_RHIZO_CPU_COUNTERS TEMPO_RHIZO_HEADROOM
EOF2
}
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 2
ROOT="$PWD"
RHIZO_SCRIPT=rhizo_run
. benchmark/run/rhizo/lib.sh

LABEL="" INJECT=none FRAMES=6000 PERIOD=10000000 PIN="" SLOPES=1600 ACTS=400 DUTY=0 RT_PRIO=90 DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --label) LABEL="$2"; shift 2;;
        --inject) INJECT="$2"; shift 2;;
        --frames) FRAMES="$2"; shift 2;;
        --period-ns) PERIOD="$2"; shift 2;;
        --pin) PIN="$2"; shift 2;;
        --slopes) SLOPES="$2"; shift 2;;
        --acts) ACTS="$2"; shift 2;;
        --duty) DUTY="$2"; shift 2;;
        --rt-prio) RT_PRIO="$2"; shift 2;;
        --inject-s|--vcpus) shift 2;;
        --dry-run) DRY=1; shift;;
        -h|--help) usage; exit 0;;
        *) die "unknown argument '$1'";;
    esac
done
LEVEL="${TEMPO_RHIZO_LEVEL:-none}" ADPF="${TEMPO_RHIZO_ADPF:-on}" INJECT_BIN="${TEMPO_RHIZO_INJECT:-$ROOT/build/inject}"
SLOT_US="${TEMPO_RHIZO_SLOT_US:-100000}" TELEMETRY_ADPF="${TEMPO_RHIZO_TELEMETRY_ADPF:-off}" CPU_COUNTERS="${TEMPO_RHIZO_CPU_COUNTERS:-off}"
HEADROOM="${TEMPO_RHIZO_HEADROOM:-off}"
[[ "$LABEL" =~ ^[a-z0-9-]+$ ]] || die "--label '$LABEL'"
case "$INJECT:$LEVEL" in
    none:none|cpu-load:vm|cpu-load:pc) ;;
    none:*) die "TEMPO_RHIZO_LEVEL=$LEVEL needs --inject cpu-load";;
    cpu-load:*) die "TEMPO_RHIZO_LEVEL='$LEVEL'";;
    *) die "--inject '$INJECT'";;
esac
case "$ADPF" in on|off) ;; *) die "TEMPO_RHIZO_ADPF='$ADPF'";; esac
case "$TELEMETRY_ADPF" in on|off) ;; *) die "TEMPO_RHIZO_TELEMETRY_ADPF='$TELEMETRY_ADPF'";; esac
case "$CPU_COUNTERS" in on|off) ;; *) die "TEMPO_RHIZO_CPU_COUNTERS='$CPU_COUNTERS'";; esac
case "$HEADROOM" in on|off) ;; *) die "TEMPO_RHIZO_HEADROOM='$HEADROOM'";; esac
[[ "$SLOT_US" =~ ^[1-9][0-9]*$ ]] && (( SLOT_US >= 10000 && SLOT_US <= 1000000 )) || die "TEMPO_RHIZO_SLOT_US='$SLOT_US'"
for v in FRAMES PERIOD SLOPES ACTS RT_PRIO; do [[ "${!v}" =~ ^[1-9][0-9]*$ ]] || die "$v='${!v}'"; done
[[ "$DUTY" =~ ^[0-9]+$ ]] && (( DUTY <= 100 )) || die "--duty '$DUTY'"
[ "$INJECT" = none ] || (( DUTY > 0 )) || die "--duty 0"
IFS=, read -ra PINS <<<"$PIN"
(( ${#PINS[@]} == 4 )) || die "--pin '$PIN'"
(( FRAMES * PERIOD % 1000000000 == 0 )) || die "frames*period_ns % 1e9 != 0"

LOOP_S=$(( FRAMES * PERIOD / 1000000000 ))
LOAD_S=$(( LOOP_S + 15 ))
SAMPLE_S=5
VM_CPUS="$(seq -s, 0 $(( ${#PINS[@]} - 1 )))"
LOAD_CPUS=0; [ "$LEVEL" = none ] || LOAD_CPUS=${#PINS[@]}
ARGS="--tag tr$$ --slopes $SLOPES --acts $ACTS --period-ns $PERIOD --frames $FRAMES --adpf $ADPF"
HEADROOM_ARGS="interval_ms=1000 window_ms=1000 spin_ms=110"
CPU_LOAD_LOG=/root/tempo-cpu-load.log
RECORD_BYTES=72
PF_CONFIG=/data/misc/perfetto-configs/tempo-rhizo.pbtxt
PF_TRACE=/data/misc/perfetto-traces/tempo-rhizo.pftrace
PF_GZ=$RDIR/tempo-rhizo.pftrace.gz
PF_LOG=$RDIR/perfetto.log
RTD_LOG=/var/log/rhizo/rhizo-rtd.log
exec 3>&1
GUEST_CPU_LOAD_PID="" INJECT_PID="" PF_PID="" PF_PIDS="" SAMPLER="" STARTED=0
PF_CONFIG_RE="${PF_CONFIG%.pbtxt}[.]pbtxt"
as_app() { local s="${1//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//\$/\\\$}"; s="${s//\`/\\\`}"; printf 'run-as %s sh -c "%s"' "$APP" "$s"; }
STOP_BODY="$(as_app 'for p in $(cat files/pids 2>/dev/null); do tr "\0" " " < /proc/$p/cmdline 2>/dev/null | grep -q "libpipeline.so --stage " && kill $p; done')"

if (( DRY )); then
    OUT="$(mktemp -d)"; mkdir -p "$OUT/android"
    echo "# $LABEL: level=$LEVEL adpf=$ADPF frames=$FRAMES period_ns=$PERIOD duty=$DUTY slot_us=$SLOT_US load_s=$LOAD_S headroom=$HEADROOM"
    INIT="<init>" SERIAL="<serial>" ADB=adb BATCH_QEMU_PID="<qemu pid>" BATCH_GPU="<gpu>" BATCH_RENDERER="<renderer>"
else
    read_state
    RHIZO="$BATCH_RHIZO"; rhizo_tools || exit 2
    RHIZO_VM="$BATCH_VM"; export RHIZO_SESSION_ID="$BATCH_SESSION"
    rhizo_source
    [ "$PIN" = "$BATCH_VCPU_CPUS" ] || die "--pin $PIN != $BATCH_VCPU_CPUS"
    [[ "${BATCH_APK_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || die "no apk_sha256"
    [ "$HEADROOM" = off ] || [ "${BATCH_HEADROOM:-off}" = on ] || die "TEMPO_RHIZO_HEADROOM=on needs --headroom on"
    held_vm_is "$BATCH_VM" || die "vm $BATCH_VM not held"
    SERIAL="$BATCH_SERIAL" ADB="$BATCH_ADB"
    INIT="$(guest_init_pid 10)"; [ -n "$INIT" ] || die "no Android init"
    OUT="$ROOT/results/experiments/$LABEL"
    rm -rf "$OUT"; mkdir -p "$OUT/android" || die "cannot create $OUT"
fi

cleanup() {
    trap - EXIT
    if (( DRY )); then
        A "$STOP_BODY"
        rm -rf "$OUT"; return 0
    fi
    (( STARTED )) || return 0
    [[ "$SAMPLER" =~ ^[0-9]+$ ]] && tr '\0' ' ' <"/proc/$SAMPLER/cmdline" 2>/dev/null | grep -q 'rhizo/run[.]sh' && kill "$SAMPLER" 2>/dev/null
    A "$STOP_BODY" >/dev/null 2>&1
    [[ "$PF_PIDS" =~ ^[0-9]+( [0-9]+)*$ ]] && A "for p in $PF_PIDS; do if tr '\\0' ' ' < /proc/\$p/cmdline 2>/dev/null | grep -q -- '-c $PF_CONFIG_RE'; then kill -TERM \$p; fi; done" >/dev/null 2>&1
    [[ "$GUEST_CPU_LOAD_PID" =~ ^[0-9]+$ ]] && G "if tr '\\0' ' ' < /proc/$GUEST_CPU_LOAD_PID/cmdline 2>/dev/null | grep -q '^$GUEST_CPU_LOAD '; then kill $GUEST_CPU_LOAD_PID; fi" >/dev/null 2>&1
    [[ "$INJECT_PID" =~ ^[0-9]+$ ]] && wait "$INJECT_PID" 2>/dev/null
    return 0
}
trap cleanup EXIT
perfetto_pids() { A "for p in /proc/[0-9]*; do if tr '\\0' ' ' < \$p/cmdline 2>/dev/null | grep -q -- '-c $PF_CONFIG_RE'; then echo \${p#/proc/}; fi; done"; }
cpu_load_started() { POLL_READ="$(ssh_vm -t 10 "sed -n 's/^CPU_LOAD pid=\\([0-9]*\\) .*/\\1/p' $CPU_LOAD_LOG" | tr -d '\r')"; [[ "$POLL_READ" =~ ^[0-9]+$ ]]; }
cpu_load_done() { POLL_READ="$(ssh_vm -t 10 "grep -c '^CPU_LOAD done' $CPU_LOAD_LOG" | tr -d '\r\n')"; [ "$POLL_READ" = 1 ]; }
sensor_ready() { POLL_READ="$(A "run-as $APP grep -c downstream_ready=1 files/log/sensor.log" | tr -d '\r\n')"; [ "$POLL_READ" = 1 ]; }
loop_done() { POLL_READ="$(A "run-as $APP cat files/rc 2>/dev/null" | tr -d '\r')"; grep -q '^RC ' <<<"$POLL_READ"; }
sample_status() {
    poll_until 90 2 sensor_ready || { echo "no downstream_ready" >"$OUT/snapshot-failed.txt"; return 1; }
    local n=0 last=$(( SECONDS + LOOP_S + 120 )) f
    while (( SECONDS < last )); do
        sleep $SAMPLE_S
        loop_done && break
        n=$(( n + 1 )); f="$OUT/rtd-status-$(printf %03d "$n").json"
        G "cat /run/rhizo-rtd/status.json 2>&1" >"$f" && [ -s "$f" ] || { echo "sample $n: no status.json" >>"$OUT/snapshot-failed.txt"; rm -f "$f"; }
        [ "$CPU_COUNTERS" = on ] || continue
        f="$OUT/rtd-stat-$(printf %03d "$n").txt"
        G "grep '^cpu ' /proc/stat; getconf CLK_TCK; cut -d' ' -f1 /proc/uptime" >"$f" 2>>"$OUT/rtd-stat.err" \
            || { echo "sample $n: no /proc/stat" >>"$OUT/snapshot-failed.txt"; rm -f "$f"; }
    done
    compgen -G "$OUT/rtd-status-[0-9]*.json" >/dev/null || { echo "no status sample" >>"$OUT/snapshot-failed.txt"; return 1; }
}
host_check() {
    if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py exclusive --qemu-pid $BATCH_QEMU_PID --vcpus ${#PINS[@]} --level $LEVEL --duty $DUTY; python3 benchmark/run/rhizo/host.py check --state $STATE_DIR/vcpus.json"; return 0; fi
    python3 benchmark/run/rhizo/host.py exclusive --qemu-pid "$BATCH_QEMU_PID" --vcpus "${#PINS[@]}" --level "$LEVEL" --duty "$DUTY" >"$OUT/exclusive-$1.txt" || die "idle check failed"
    python3 benchmark/run/rhizo/host.py check --state "$STATE_DIR/vcpus.json" >"$OUT/vcpus-$1.txt" || die "vCPU check failed"
}

phase "check"
if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py settle --vcpus ${#PINS[@]} --level $LEVEL --duty $DUTY"
else python3 benchmark/run/rhizo/host.py settle --vcpus "${#PINS[@]}" --level "$LEVEL" --duty "$DUTY" >"$OUT/settle.txt" || die "settle failed"; fi
host_check start
left="$(A "pidof libpipeline.so perfetto; true" | tr -d '[:space:]')"
[ -z "$left" ] || die "leftover Android pids $left"
left="$(G "pgrep -x tempo-cpu-load; true" | tr -d '[:space:]')"
[ -z "$left" ] || die "leftover guest load pids $left"
STARTED=1
G "grep '^cpu ' /proc/stat; getconf CLK_TCK" >"$OUT/stat-before.txt"

phase "trace: rhizo.host_delay linux.process_stats -> $PF_TRACE"
config="buffers { size_kb: 65536 fill_policy: RING_BUFFER }
data_sources { config { name: \"rhizo.host_delay\" } }
data_sources { config { name: \"linux.process_stats\" process_stats_config { scan_all_processes_on_start: true } } }
write_into_file: true
file_write_period_ms: 500
duration_ms: $(( (LOAD_S + 60) * 1000 ))"
out="$(AT=60 A "echo $(printf '%s\n' "$config" | base64 -w0) | base64 -d > $PF_CONFIG && rm -f $PF_TRACE $PF_GZ && perfetto --background --txt -c $PF_CONFIG -o $PF_TRACE </dev/null >$PF_LOG 2>&1; echo perfetto_rc=\$?" | tr -d '\r')"
if (( DRY )); then PF_PID="<perfetto pid>"
else
    grep -qx 'perfetto_rc=0' <<<"$out" || die "perfetto: $out"
    PF_PIDS="$(perfetto_pids | tr -d '\r' | grep -xE '[0-9]+' | tr '\n' ' ' | sed 's/ $//')"
    [ -n "$PF_PIDS" ] || die "no perfetto"
    [[ "$PF_PIDS" != *" "* ]] || die "perfetto pids $PF_PIDS"
    PF_PID="$PF_PIDS"
    out="$(AT=40 A "i=0; while [ \$i -lt 40 ] && ! [ -s $PF_TRACE ]; do sleep 0.5; i=\$((i + 1)); done; [ -s $PF_TRACE ] && echo PF_WRITING || echo PF_SILENT")"
    grep -q PF_WRITING <<<"$out" || die "perfetto silent"
fi

if [ "$LEVEL" = vm ]; then
    phase "load vm"
    launched="$(G "rm -f $CPU_LOAD_LOG; setsid $GUEST_CPU_LOAD --cpus $VM_CPUS --duration-s $LOAD_S --duty $DUTY --slot-us $SLOT_US --rt-prio $RT_PRIO --log $CPU_LOAD_LOG </dev/null >/root/tempo-cpu-load.out 2>&1 & echo \$!")" \
        || die "vm load failed"
    if (( DRY )); then GUEST_CPU_LOAD_PID="<cpu_load pid>"
    else
        launched="$(tr -d '\r\n' <<<"$launched")"
        [[ "$launched" =~ ^[0-9]+$ ]] || die "vm load pid '$launched'"
        GUEST_CPU_LOAD_PID="$launched"
        poll_until 20 1 cpu_load_started || die "vm load no pid line"
        [ "$POLL_READ" = "$GUEST_CPU_LOAD_PID" ] || die "vm load pid $POLL_READ != $GUEST_CPU_LOAD_PID"
    fi
elif [ "$LEVEL" = pc ]; then
    phase "load pc"
    if (( DRY )); then show "local>   sudo -n $INJECT_BIN --mode cpu-load --cpus $PIN --duration-s $LOAD_S --duty $DUTY --slot-us $SLOT_US --rt-prio $RT_PRIO"
    else
        # shellcheck disable=SC2024
        sudo -n "$INJECT_BIN" --mode cpu-load --cpus "$PIN" --duration-s "$LOAD_S" --duty "$DUTY" --slot-us "$SLOT_US" --rt-prio "$RT_PRIO" >"$OUT/inject.out" 2>&1 &
        INJECT_PID=$!
    fi
fi

phase "loop"
RTD_AT="$(G "stat -c %s $RTD_LOG" | tr -d '\r\n')"
if (( DRY )); then RTD_AT="<offset>" RTD_FROM="<offset+1>"
else [[ "$RTD_AT" =~ ^[0-9]+$ ]] || die "$RTD_LOG size '$RTD_AT'"; RTD_FROM=$(( RTD_AT + 1 )); fi
TEL_FLAG=""; [ "$TELEMETRY_ADPF" = on ] && TEL_FLAG=" --adpf-telemetry on"
RUN_ARGS="$(printf '%s\n' "--stage sensor $ARGS" "--stage compute $ARGS" "--stage actuator $ARGS --record rec/actuator.bin" \
    "--stage telemetry $ARGS$TEL_FLAG --out rec/telemetry.bin")"
if (( DRY )); then while IFS= read -r l; do show "# run.args: $l"; done <<<"$RUN_ARGS"; fi
A "echo $(printf '%s\n' "$RUN_ARGS" | base64 -w0) | base64 -d | $(as_app 'mkdir -p files/rec files/log files/shm && rm -f files/rc files/pids files/headroom.args files/rec/* files/log/* files/shm/* && cat > files/run.args')"
if [ "$HEADROOM" = on ]; then
    (( DRY )) && show "# headroom.args: $HEADROOM_ARGS"
    A "echo $HEADROOM_ARGS | $(as_app 'cat > files/headroom.args')"
fi
if (( ! DRY )); then
    got="$(A "run-as $APP cat files/run.args" | tr -d '\r')"
    [ "$got" = "$RUN_ARGS" ] || die "run.args: $got"
    got="$(A "run-as $APP cat files/headroom.args 2>/dev/null" | tr -d '\r')"
    if [ "$HEADROOM" = on ]; then [ "$got" = "$HEADROOM_ARGS" ] || die "headroom.args: $got"
    else [ -z "$got" ] || die "stale headroom.args: $got"; fi
fi
out="$(A "am start -W -n $APP/android.app.NativeActivity" | tr -d '\r')"
if (( DRY )); then show "# files/rc every 2s <=$(( LOOP_S + 120 ))s; status.json every ${SAMPLE_S}s -> <out>/rtd-status-NNN.json$([ "$CPU_COUNTERS" = on ] && echo ", <out>/rtd-stat-NNN.txt")"
else
    printf '%s\n' "$out" >"$OUT/am-start.txt"
    grep -q 'Status: ok' <<<"$out" || die "app start: $(tr '\n' ' ' <"$OUT/am-start.txt")"
    sample_status & SAMPLER=$!
    poll_until $(( LOOP_S + 120 )) 2 loop_done; done_rc=$?
    printf '%s\n' "$POLL_READ" >"$OUT/loop-rc.txt"
    (( done_rc == 0 )) || die "no RC line"
    grep -qx 'RC sensor=0 compute=0 actuator=0 telemetry=0' "$OUT/loop-rc.txt" || die "$(cat "$OUT/loop-rc.txt")"
    wait "$SAMPLER" || die "status: $(cat "$OUT/snapshot-failed.txt" 2>/dev/null)"
    SAMPLER=""
fi

phase "wait"
if [ "$LEVEL" = vm ] && (( ! DRY )); then poll_until $(( LOAD_S + 60 )) 3 cpu_load_done || die "vm load unfinished"; GUEST_CPU_LOAD_PID=""; fi
if [ "$LEVEL" = pc ] && (( ! DRY )); then wait "$INJECT_PID" || die "pc load: $(tail -2 "$OUT/inject.out")"; INJECT_PID=""; fi

phase "collect"
out="$(AT=90 A "kill -TERM $PF_PID; i=0; while kill -0 $PF_PID 2>/dev/null && [ \$i -lt 120 ]; do sleep 0.5; i=\$((i + 1)); done; kill -0 $PF_PID 2>/dev/null && echo PF_RUNNING || echo PF_STOPPED")"
if (( ! DRY )); then
    grep -q PF_STOPPED <<<"$out" || die "perfetto $PF_PID survived SIGTERM"
    PF_PID="" PF_PIDS=""
    want="$(AT=120 A "gzip -c $PF_TRACE > $PF_GZ && sha256sum $PF_GZ | cut -d' ' -f1" | tr -d '\r' | head -1)"
    [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "gzip: $want"
    AT=300 A "base64 -w0 $PF_GZ" | tr -d '\r\n' | base64 -d >"$OUT/trace.pftrace.gz" || die "trace copy failed"
    [ "$(sha256sum "$OUT/trace.pftrace.gz" | cut -d' ' -f1)" = "$want" ] || die "trace sha mismatch"
    gunzip -f "$OUT/trace.pftrace.gz" || die "trace not gzip"
    A "cat $PF_LOG" >"$OUT/perfetto.log" 2>&1
    A "rm -f $PF_CONFIG $PF_TRACE $PF_GZ $PF_LOG" >/dev/null 2>&1
fi
adbx exec-out run-as $APP cat files/rec/actuator.bin >"$OUT/actuator.bin" || die "no actuator.bin"
(( DRY )) || [ -s "$OUT/actuator.bin" ] || die "no actuator.bin"
adbx exec-out run-as $APP cat files/rec/telemetry.bin >"$OUT/telemetry.bin" || die "no telemetry.bin"
(( DRY )) || [ -s "$OUT/telemetry.bin" ] || die "no telemetry.bin"
for st in sensor compute actuator telemetry; do
    adbx exec-out run-as $APP cat files/log/$st.log >"$OUT/android/$st.log" || die "no $st.log"
    (( DRY )) || [ -s "$OUT/android/$st.log" ] || die "no $st.log"
done
if [ "$HEADROOM" = on ]; then
    adbx exec-out run-as $APP cat files/log/headroom.log >"$OUT/android/headroom.log" || die "no headroom.log"
    (( DRY )) || grep -q '^HEADROOM done' "$OUT/android/headroom.log" || die "headroom.log: $(head -1 "$OUT/android/headroom.log")"
fi
adbx exec-out run-as $APP cat files/pids >"$OUT/pids.txt" || die "no pids"
(( DRY )) || [ -s "$OUT/pids.txt" ] || die "no pids"
if (( ! DRY )); then
    RECORDS="$(tr -d '\r' <"$OUT/android/telemetry.log" | sed -n 's/^OBS records=\([0-9]*\)$/\1/p' | head -1)"
    [[ "$RECORDS" =~ ^[0-9]+$ ]] || die "no OBS records="
    BYTES="$(stat -c %s "$OUT/telemetry.bin")"
    [ "$BYTES" = $(( RECORDS * RECORD_BYTES )) ] || die "telemetry.bin $BYTES != $RECORDS x $RECORD_BYTES"
    ACTUATOR_RECORDS="$(tr -d '\r' <"$OUT/android/actuator.log" | sed -n 's/^OBS actuator_records=\([0-9]*\)$/\1/p' | head -1)"
    [[ "$ACTUATOR_RECORDS" =~ ^[0-9]+$ ]] || die "no OBS actuator_records="
    BYTES="$(stat -c %s "$OUT/actuator.bin")"
    [ "$BYTES" = $(( ACTUATOR_RECORDS * RECORD_BYTES )) ] || die "actuator.bin $BYTES != $ACTUATOR_RECORDS x $RECORD_BYTES"
fi
G "[ \$(stat -c %s $RTD_LOG) -ge $RTD_AT ] && tail -c +$RTD_FROM $RTD_LOG" | tr -d '\r' >"$OUT/rtd.log" \
    || die "rtd log copy failed"
[ "$LEVEL" = vm ] && G "cat $CPU_LOAD_LOG" >"$OUT/cpu_load.log"
G "grep '^cpu ' /proc/stat; getconf CLK_TCK" >"$OUT/stat-after.txt"
host_check end

phase "facts"
if (( DRY )); then
    show "local>   python3 benchmark/score/rhizo/trace.py <out>/trace.pftrace --tids <sensor tid>,<compute tid>,<actuator tid> --pid <compute pid> > <out>/slices.json"
    show "local>   python3 benchmark/score/rhizo/frames.py <out>"
    exit 0
fi
tid_of() { tr -d '\r' <"$OUT/android/$1.log" | sed -n 's/^OBS tid=\([0-9]*\)$/\1/p' | head -1; }
SENSOR_TID="$(tid_of sensor)" COMPUTE_TID="$(tid_of compute)" ACTUATOR_TID="$(tid_of actuator)" TELEMETRY_TID="$(tid_of telemetry)" COMPUTE_PID="$(sed -n 2p "$OUT/pids.txt")"
[[ "$SENSOR_TID" =~ ^[0-9]+$ && "$COMPUTE_TID" =~ ^[0-9]+$ && "$ACTUATOR_TID" =~ ^[0-9]+$ && "$TELEMETRY_TID" =~ ^[0-9]+$ && "$COMPUTE_PID" =~ ^[0-9]+$ ]] \
    || die "tids sensor='$SENSOR_TID' compute='$COMPUTE_TID' actuator='$ACTUATOR_TID' telemetry='$TELEMETRY_TID' pid='$COMPUTE_PID'"
printf '%s\n' "label=$LABEL" "level=$LEVEL" "adpf=$ADPF" "duty=$DUTY" "load_s=$LOAD_S" "period_ns=$PERIOD" "frames=$FRAMES" \
    "load_cpus=$LOAD_CPUS" "sensor_tid=$SENSOR_TID" "compute_tid=$COMPUTE_TID" "actuator_tid=$ACTUATOR_TID" \
    "compute_pid=$COMPUTE_PID" "slot_us=$SLOT_US" "telemetry_adpf=$TELEMETRY_ADPF" "telemetry_tid=$TELEMETRY_TID" "headroom=$HEADROOM" >"$OUT/run.conf"
printf '%s\n' "rhizo_sha=$BATCH_RHIZO_SHA" "tempo_sha=$(git rev-parse --short=12 HEAD)" "apk_sha256=$BATCH_APK_SHA256" "gpu=$BATCH_GPU" "renderer=$BATCH_RENDERER" "guest_kernel=${BATCH_GUEST_KERNEL:-stock}" \
    "guest_kernel_sha256=${BATCH_GUEST_KERNEL_SHA256:-}" "guest_initrd_sha256=${BATCH_GUEST_INITRD_SHA256:-}" >"$OUT/versions.txt"
python3 benchmark/score/rhizo/trace.py "$OUT/trace.pftrace" --tids "$SENSOR_TID,$COMPUTE_TID,$ACTUATOR_TID" --pid "$COMPUTE_PID" \
    >"$OUT/slices.json" 2>"$OUT/trace.err" || die "trace: $(cat "$OUT/trace.err")"
idle_key() { sed -n "s/.*\b$1=\([^ ]*\).*/\1/p" "$OUT/exclusive-$2.txt"; }
printf 'exclusive=%s exclusive_end=%s topology=single-level\n' "$(idle_key exclusive start)" "$(idle_key exclusive end)" >"$OUT/host.meta"
python3 benchmark/score/rhizo/frames.py "$OUT" | tee "$OUT/fact.txt" || die "frames.py failed"
