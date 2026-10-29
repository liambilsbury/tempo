#!/usr/bin/env bash
# run the loop once as an Android app in the batch's Rhizo VM
#
# Usage: benchmark/run/rhizo/run.sh --label L [--inject none|cpu-load --duty PCT --rt-prio N] [--frames N]
#                                   [--period-ns N] --pin LIST [--slopes N] [--acts N] [--dry-run]
#   TEMPO_RHIZO_LEVEL=vm|pc   where a cpu-load runs
#   TEMPO_RHIZO_ADPF=on|off   ADPF hint sessions for deadline stages
#   TEMPO_RHIZO_INJECT=PATH   inject binary, default build/inject
#   TEMPO_RHIZO_SLOT_US=N     load slot in us, default 100000
#   TEMPO_RHIZO_TELEMETRY_ADPF=on|off   ADPF for telemetry too, default off
#   TEMPO_RHIZO_CPU_COUNTERS=on|off     save guest CPU counters, default off
#   TEMPO_RHIZO_HEADROOM=on|off         sample CPU headroom, default off
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
        -h|--help) sed -n '2,12p' "$0"; exit 0;;
        *) die "unknown argument '$1'";;
    esac
done
LEVEL="${TEMPO_RHIZO_LEVEL:-none}" ADPF="${TEMPO_RHIZO_ADPF:-on}" INJECT_BIN="${TEMPO_RHIZO_INJECT:-$ROOT/build/inject}"
SLOT_US="${TEMPO_RHIZO_SLOT_US:-100000}" TELEMETRY_ADPF="${TEMPO_RHIZO_TELEMETRY_ADPF:-off}" CPU_COUNTERS="${TEMPO_RHIZO_CPU_COUNTERS:-off}"
HEADROOM="${TEMPO_RHIZO_HEADROOM:-off}"
[[ "$LABEL" =~ ^[a-z0-9-]+$ ]] || die "--label is [a-z0-9-]+, got '$LABEL'"
case "$INJECT:$LEVEL" in
    none:none|cpu-load:vm|cpu-load:pc) ;;
    none:*) die "TEMPO_RHIZO_LEVEL=$LEVEL needs --inject cpu-load";;
    cpu-load:*) die "--inject cpu-load needs TEMPO_RHIZO_LEVEL=vm or pc, not '$LEVEL'";;
    *) die "--inject is none or cpu-load, not '$INJECT'";;
esac
case "$ADPF" in on|off) ;; *) die "TEMPO_RHIZO_ADPF is on or off, not '$ADPF'";; esac
case "$TELEMETRY_ADPF" in on|off) ;; *) die "TEMPO_RHIZO_TELEMETRY_ADPF is on or off, not '$TELEMETRY_ADPF'";; esac
case "$CPU_COUNTERS" in on|off) ;; *) die "TEMPO_RHIZO_CPU_COUNTERS is on or off, not '$CPU_COUNTERS'";; esac
case "$HEADROOM" in on|off) ;; *) die "TEMPO_RHIZO_HEADROOM is on or off, not '$HEADROOM'";; esac
[[ "$SLOT_US" =~ ^[1-9][0-9]*$ ]] && (( SLOT_US >= 10000 && SLOT_US <= 1000000 )) || die "TEMPO_RHIZO_SLOT_US is 10000-1000000 us, got '$SLOT_US'"
for v in FRAMES PERIOD SLOPES ACTS RT_PRIO; do [[ "${!v}" =~ ^[1-9][0-9]*$ ]] || die "$v must be a positive integer, got '${!v}'"; done
[[ "$DUTY" =~ ^[0-9]+$ ]] && (( DUTY <= 100 )) || die "--duty is 0-100, got '$DUTY'"
[ "$INJECT" = none ] || (( DUTY > 0 )) || die "--inject cpu-load needs --duty above 0"
IFS=, read -ra PINS <<<"$PIN"
(( ${#PINS[@]} == 4 )) || die "--pin names the 4 host CPUs of the VM's vCPUs, got '$PIN'"
(( FRAMES * PERIOD % 1000000000 == 0 )) || die "--frames x --period-ns must be whole seconds"

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
# print a shell body that runs a script as the loop's app
as_app() { local s="${1//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//\$/\\\$}"; s="${s//\`/\\\`}"; printf 'run-as %s sh -c "%s"' "$APP" "$s"; }
STOP_BODY="$(as_app 'for p in $(cat files/pids 2>/dev/null); do tr "\0" " " < /proc/$p/cmdline 2>/dev/null | grep -q "libpipeline.so --stage " && kill $p; done')"

if (( DRY )); then
    OUT="$(mktemp -d)"; mkdir -p "$OUT/android"
    echo "# $LABEL: level $LEVEL, adpf $ADPF, $FRAMES frames at $PERIOD ns, load $DUTY% of each $SLOT_US us for $LOAD_S s, headroom $HEADROOM"
    INIT="<init>" SERIAL="<serial>" ADB=adb BATCH_QEMU_PID="<qemu pid>" BATCH_GPU="<gpu>" BATCH_RENDERER="<renderer>"
else
    read_state
    RHIZO="$BATCH_RHIZO"; rhizo_tools || exit 2
    RHIZO_VM="$BATCH_VM"; export RHIZO_SESSION_ID="$BATCH_SESSION"
    rhizo_source
    [ "$PIN" = "$BATCH_VCPU_CPUS" ] || die "--pin $PIN is not the batch's vCPU CPUs $BATCH_VCPU_CPUS"
    [[ "${BATCH_APK_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || die "the batch state names no installed apk sha256"
    [ "$HEADROOM" = off ] || [ "${BATCH_HEADROOM:-off}" = on ] || die "TEMPO_RHIZO_HEADROOM=on needs a batch set up with --headroom on"
    held_vm_is "$BATCH_VM" || die "this batch no longer holds vm $BATCH_VM"
    SERIAL="$BATCH_SERIAL" ADB="$BATCH_ADB"
    INIT="$(guest_init_pid 10)"; [ -n "$INIT" ] || die "vm $BATCH_VM has no Android init"
    OUT="$ROOT/results/experiments/$LABEL"
    rm -rf "$OUT"; mkdir -p "$OUT/android" || die "cannot create $OUT"
fi

# stop what this run started
cleanup() {
    trap - EXIT
    if (( DRY )); then
        show "# cleanup: the app's recorded pids while their cmdline is a libpipeline.so stage, the VM load's pid while it is $GUEST_CPU_LOAD, each recorded perfetto pid while its cmdline has -c $PF_CONFIG; the PC load ends by itself within $LOAD_S s"
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
# print the pids of the perfetto started with this run's config file
perfetto_pids() { A "for p in /proc/[0-9]*; do if tr '\\0' ' ' < \$p/cmdline 2>/dev/null | grep -q -- '-c $PF_CONFIG_RE'; then echo \${p#/proc/}; fi; done"; }
# read the VM load's pid from its log into POLL_READ
cpu_load_started() { POLL_READ="$(ssh_vm -t 10 "sed -n 's/^CPU_LOAD pid=\\([0-9]*\\) .*/\\1/p' $CPU_LOAD_LOG" | tr -d '\r')"; [[ "$POLL_READ" =~ ^[0-9]+$ ]]; }
# check the VM load's log for its done line
cpu_load_done() { POLL_READ="$(ssh_vm -t 10 "grep -c '^CPU_LOAD done' $CPU_LOAD_LOG" | tr -d '\r\n')"; [ "$POLL_READ" = 1 ]; }
# check the sensor log for downstream_ready=1
sensor_ready() { POLL_READ="$(A "run-as $APP grep -c downstream_ready=1 files/log/sensor.log" | tr -d '\r\n')"; [ "$POLL_READ" = 1 ]; }
# read the app's files/rc into POLL_READ and check it holds an RC line
loop_done() { POLL_READ="$(A "run-as $APP cat files/rc 2>/dev/null" | tr -d '\r')"; grep -q '^RC ' <<<"$POLL_READ"; }
# save status samples until the app writes files/rc
sample_status() {
    poll_until 90 2 sensor_ready || { echo "the sensor never reported downstream_ready" >"$OUT/snapshot-failed.txt"; return 1; }
    local n=0 last=$(( SECONDS + LOOP_S + 120 )) f
    while (( SECONDS < last )); do
        sleep $SAMPLE_S
        loop_done && break
        n=$(( n + 1 )); f="$OUT/rtd-status-$(printf %03d "$n").json"
        G "cat /run/rhizo-rtd/status.json 2>&1" >"$f" && [ -s "$f" ] || { echo "sample $n: the status.json read failed or was empty" >>"$OUT/snapshot-failed.txt"; rm -f "$f"; }
        [ "$CPU_COUNTERS" = on ] || continue
        f="$OUT/rtd-stat-$(printf %03d "$n").txt"
        G "grep '^cpu ' /proc/stat; getconf CLK_TCK; cut -d' ' -f1 /proc/uptime" >"$f" 2>>"$OUT/rtd-stat.err" \
            || { echo "sample $n: the /proc/stat read failed" >>"$OUT/snapshot-failed.txt"; rm -f "$f"; }
    done
    compgen -G "$OUT/rtd-status-[0-9]*.json" >/dev/null || { echo "no status sample was saved" >>"$OUT/snapshot-failed.txt"; return 1; }
}
# write the PC's idle check and the vCPU pin check
host_check() {
    if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py exclusive --qemu-pid $BATCH_QEMU_PID --vcpus ${#PINS[@]} --level $LEVEL --duty $DUTY; python3 benchmark/run/rhizo/host.py check --state $STATE_DIR/vcpus.json"; return 0; fi
    python3 benchmark/run/rhizo/host.py exclusive --qemu-pid "$BATCH_QEMU_PID" --vcpus "${#PINS[@]}" --level "$LEVEL" --duty "$DUTY" >"$OUT/exclusive-$1.txt" || die "the idle check could not run"
    python3 benchmark/run/rhizo/host.py check --state "$STATE_DIR/vcpus.json" >"$OUT/vcpus-$1.txt" || die "the vCPU check could not run"
}

phase "check: the PC settled under this arm's ceiling, then idle, the vCPUs pinned, nothing left in Android or the guest from an earlier run"
if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py settle --vcpus ${#PINS[@]} --level $LEVEL --duty $DUTY"
else python3 benchmark/run/rhizo/host.py settle --vcpus "${#PINS[@]}" --level "$LEVEL" --duty "$DUTY" >"$OUT/settle.txt" || die "the settle could not run"; fi
host_check start
left="$(A "pidof libpipeline.so perfetto; true" | tr -d '[:space:]')"
[ -z "$left" ] || die "Android still runs a libpipeline.so stage or perfetto from an earlier run: pids $left"
left="$(G "pgrep -x tempo-cpu-load; true" | tr -d '[:space:]')"
[ -z "$left" ] || die "the guest still runs a load from an earlier run: pids $left"
STARTED=1
G "grep '^cpu ' /proc/stat; getconf CLK_TCK" >"$OUT/stat-before.txt"

phase "trace: perfetto in Android with rhizo.host_delay and linux.process_stats, BOOTTIME, into $PF_TRACE"
config="buffers { size_kb: 65536 fill_policy: RING_BUFFER }
data_sources { config { name: \"rhizo.host_delay\" } }
data_sources { config { name: \"linux.process_stats\" process_stats_config { scan_all_processes_on_start: true } } }
write_into_file: true
file_write_period_ms: 500
duration_ms: $(( (LOAD_S + 60) * 1000 ))"
out="$(AT=60 A "echo $(printf '%s\n' "$config" | base64 -w0) | base64 -d > $PF_CONFIG && rm -f $PF_TRACE $PF_GZ && perfetto --background --txt -c $PF_CONFIG -o $PF_TRACE </dev/null >$PF_LOG 2>&1; echo perfetto_rc=\$?" | tr -d '\r')"
if (( DRY )); then PF_PID="<perfetto pid>"
else
    grep -qx 'perfetto_rc=0' <<<"$out" || die "perfetto did not start: $out"
    PF_PIDS="$(perfetto_pids | tr -d '\r' | grep -xE '[0-9]+' | tr '\n' ' ' | sed 's/ $//')"
    [ -n "$PF_PIDS" ] || die "no perfetto with -c $PF_CONFIG is running in Android"
    [[ "$PF_PIDS" != *" "* ]] || die "more than one perfetto uses $PF_CONFIG: pids $PF_PIDS"
    PF_PID="$PF_PIDS"
    out="$(AT=40 A "i=0; while [ \$i -lt 40 ] && ! [ -s $PF_TRACE ]; do sleep 0.5; i=\$((i + 1)); done; [ -s $PF_TRACE ] && echo PF_WRITING || echo PF_SILENT")"
    grep -q PF_WRITING <<<"$out" || die "perfetto wrote nothing to $PF_TRACE in 20 s"
fi

if [ "$LEVEL" = vm ]; then
    phase "load in the VM: vCPUs $VM_CPUS at once, $DUTY% of each $SLOT_US us, SCHED_FIFO $RT_PRIO, $LOAD_S s"
    launched="$(G "rm -f $CPU_LOAD_LOG; setsid $GUEST_CPU_LOAD --cpus $VM_CPUS --duration-s $LOAD_S --duty $DUTY --slot-us $SLOT_US --rt-prio $RT_PRIO --log $CPU_LOAD_LOG </dev/null >/root/tempo-cpu-load.out 2>&1 & echo \$!")" \
        || die "could not start the load in the VM"
    if (( DRY )); then GUEST_CPU_LOAD_PID="<cpu_load pid>"
    else
        launched="$(tr -d '\r\n' <<<"$launched")"
        [[ "$launched" =~ ^[0-9]+$ ]] || die "the load in the VM gave no pid at launch ('$launched')"
        GUEST_CPU_LOAD_PID="$launched"
        poll_until 20 1 cpu_load_started || die "the load in the VM wrote no pid line"
        [ "$POLL_READ" = "$GUEST_CPU_LOAD_PID" ] || die "the load's logged pid $POLL_READ is not the launched pid $GUEST_CPU_LOAD_PID"
    fi
elif [ "$LEVEL" = pc ]; then
    phase "load on the PC: CPUs $PIN under the vCPUs at once, $DUTY% of each $SLOT_US us, SCHED_FIFO $RT_PRIO, $LOAD_S s"
    if (( DRY )); then show "local>   sudo -n $INJECT_BIN --mode cpu-load --cpus $PIN --duration-s $LOAD_S --duty $DUTY --slot-us $SLOT_US --rt-prio $RT_PRIO"
    else
        # shellcheck disable=SC2024
        sudo -n "$INJECT_BIN" --mode cpu-load --cpus "$PIN" --duration-s "$LOAD_S" --duty "$DUTY" --slot-us "$SLOT_US" --rt-prio "$RT_PRIO" >"$OUT/inject.out" 2>&1 &
        INJECT_PID=$!
    fi
fi

phase "loop: the app $APP runs sensor, compute, actuator and telemetry on any of the ${#PINS[@]} vCPUs, --adpf $ADPF"
RTD_AT="$(G "stat -c %s $RTD_LOG" | tr -d '\r\n')"
if (( DRY )); then RTD_AT="<offset>" RTD_FROM="<offset+1>"
else [[ "$RTD_AT" =~ ^[0-9]+$ ]] || die "cannot read the size of $RTD_LOG in the guest ('$RTD_AT')"; RTD_FROM=$(( RTD_AT + 1 )); fi
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
    [ "$got" = "$RUN_ARGS" ] || die "the app's files/run.args reads back as: $got"
    got="$(A "run-as $APP cat files/headroom.args 2>/dev/null" | tr -d '\r')"
    if [ "$HEADROOM" = on ]; then [ "$got" = "$HEADROOM_ARGS" ] || die "the app's files/headroom.args reads back as: $got"
    else [ -z "$got" ] || die "the app still has a files/headroom.args: $got"; fi
fi
out="$(A "am start -W -n $APP/android.app.NativeActivity" | tr -d '\r')"
if (( DRY )); then show "# poll the app's files/rc every 2 s for $(( LOOP_S + 120 )) s; in the background: rhizo-rtd's status.json every $SAMPLE_S s from the sensor's ready line until the app writes files/rc, into <out>/rtd-status-NNN.json$([ "$CPU_COUNTERS" = on ] && echo ", with the guest's /proc/stat cpu line, CLK_TCK and uptime into <out>/rtd-stat-NNN.txt")"
else
    printf '%s\n' "$out" >"$OUT/am-start.txt"
    grep -q 'Status: ok' <<<"$out" || die "the app did not start: $(tr '\n' ' ' <"$OUT/am-start.txt")"
    sample_status & SAMPLER=$!
    poll_until $(( LOOP_S + 120 )) 2 loop_done; done_rc=$?
    printf '%s\n' "$POLL_READ" >"$OUT/loop-rc.txt"
    (( done_rc == 0 )) || die "the app wrote no RC line to files/rc in $(( LOOP_S + 120 )) s"
    grep -qx 'RC sensor=0 compute=0 actuator=0 telemetry=0' "$OUT/loop-rc.txt" || die "a stage failed: $(cat "$OUT/loop-rc.txt")"
    wait "$SAMPLER" || die "rhizo-rtd's status was not saved ($(cat "$OUT/snapshot-failed.txt" 2>/dev/null))"
    SAMPLER=""
fi

phase "wait: the load ends by itself"
if [ "$LEVEL" = vm ] && (( ! DRY )); then poll_until $(( LOAD_S + 60 )) 3 cpu_load_done || die "the load in the VM never finished"; GUEST_CPU_LOAD_PID=""; fi
if [ "$LEVEL" = pc ] && (( ! DRY )); then wait "$INJECT_PID" || die "the PC load failed ($(tail -2 "$OUT/inject.out"))"; INJECT_PID=""; fi

phase "collect: the trace, the actuator's and the telemetry's frame records, the stage logs, rhizo-rtd's log since the loop began, the load's own records, the guest's steal"
out="$(AT=90 A "kill -TERM $PF_PID; i=0; while kill -0 $PF_PID 2>/dev/null && [ \$i -lt 120 ]; do sleep 0.5; i=\$((i + 1)); done; kill -0 $PF_PID 2>/dev/null && echo PF_RUNNING || echo PF_STOPPED")"
if (( ! DRY )); then
    grep -q PF_STOPPED <<<"$out" || die "perfetto $PF_PID did not exit after SIGTERM"
    PF_PID="" PF_PIDS=""
    want="$(AT=120 A "gzip -c $PF_TRACE > $PF_GZ && sha256sum $PF_GZ | cut -d' ' -f1" | tr -d '\r' | head -1)"
    [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "Android's gzip of the trace reported: $want"
    copy_at=$SECONDS
    AT=300 A "base64 -w0 $PF_GZ" | tr -d '\r\n' | base64 -d >"$OUT/trace.pftrace.gz" || die "could not copy the trace"
    [ "$(sha256sum "$OUT/trace.pftrace.gz" | cut -d' ' -f1)" = "$want" ] || die "the trace arrived changed"
    say "the trace arrived: $(stat -c %s "$OUT/trace.pftrace.gz") compressed bytes in $(( SECONDS - copy_at )) s"
    gunzip -f "$OUT/trace.pftrace.gz" || die "the copied trace is not gzip"
    A "cat $PF_LOG" >"$OUT/perfetto.log" 2>&1
    A "rm -f $PF_CONFIG $PF_TRACE $PF_GZ $PF_LOG" >/dev/null 2>&1
fi
adbx exec-out run-as $APP cat files/rec/actuator.bin >"$OUT/actuator.bin" || die "no actuator record from the app"
(( DRY )) || [ -s "$OUT/actuator.bin" ] || die "no actuator record from the app"
adbx exec-out run-as $APP cat files/rec/telemetry.bin >"$OUT/telemetry.bin" || die "no frame record from the app"
(( DRY )) || [ -s "$OUT/telemetry.bin" ] || die "no frame record from the app"
for st in sensor compute actuator telemetry; do
    adbx exec-out run-as $APP cat files/log/$st.log >"$OUT/android/$st.log" || die "no $st log from the app"
    (( DRY )) || [ -s "$OUT/android/$st.log" ] || die "no $st log from the app"
done
if [ "$HEADROOM" = on ]; then
    adbx exec-out run-as $APP cat files/log/headroom.log >"$OUT/android/headroom.log" || die "no headroom log from the app"
    (( DRY )) || grep -q '^HEADROOM done' "$OUT/android/headroom.log" || die "the app's headroom log has no done line: $(head -1 "$OUT/android/headroom.log")"
fi
adbx exec-out run-as $APP cat files/pids >"$OUT/pids.txt" || die "no pids file from the app"
(( DRY )) || [ -s "$OUT/pids.txt" ] || die "no pids file from the app"
if (( ! DRY )); then
    RECORDS="$(tr -d '\r' <"$OUT/android/telemetry.log" | sed -n 's/^OBS records=\([0-9]*\)$/\1/p' | head -1)"
    [[ "$RECORDS" =~ ^[0-9]+$ ]] || die "the telemetry log has no OBS records= line"
    BYTES="$(stat -c %s "$OUT/telemetry.bin")"
    [ "$BYTES" = $(( RECORDS * RECORD_BYTES )) ] || die "telemetry.bin is $BYTES bytes, not $RECORDS records of $RECORD_BYTES"
    ACTUATOR_RECORDS="$(tr -d '\r' <"$OUT/android/actuator.log" | sed -n 's/^OBS actuator_records=\([0-9]*\)$/\1/p' | head -1)"
    [[ "$ACTUATOR_RECORDS" =~ ^[0-9]+$ ]] || die "the actuator log has no OBS actuator_records= line"
    BYTES="$(stat -c %s "$OUT/actuator.bin")"
    [ "$BYTES" = $(( ACTUATOR_RECORDS * RECORD_BYTES )) ] || die "actuator.bin is $BYTES bytes, not $ACTUATOR_RECORDS records of $RECORD_BYTES"
fi
G "[ \$(stat -c %s $RTD_LOG) -ge $RTD_AT ] && tail -c +$RTD_FROM $RTD_LOG" | tr -d '\r' >"$OUT/rtd.log" \
    || die "could not copy rhizo-rtd's log from byte $RTD_AT on"
[ "$LEVEL" = vm ] && G "cat $CPU_LOAD_LOG" >"$OUT/cpu_load.log"
G "grep '^cpu ' /proc/stat; getconf CLK_TCK" >"$OUT/stat-after.txt"
host_check end

phase "facts: the trace's slices on the sensor, compute and actuator threads, then frames.py"
if (( DRY )); then
    show "local>   python3 benchmark/score/rhizo/trace.py <out>/trace.pftrace --tids <sensor tid>,<compute tid>,<actuator tid> --pid <compute pid> > <out>/slices.json"
    show "local>   python3 benchmark/score/rhizo/frames.py <out>"
    exit 0
fi
# print a stage's thread id from its OBS tid line
tid_of() { tr -d '\r' <"$OUT/android/$1.log" | sed -n 's/^OBS tid=\([0-9]*\)$/\1/p' | head -1; }
SENSOR_TID="$(tid_of sensor)" COMPUTE_TID="$(tid_of compute)" ACTUATOR_TID="$(tid_of actuator)" TELEMETRY_TID="$(tid_of telemetry)" COMPUTE_PID="$(sed -n 2p "$OUT/pids.txt")"
[[ "$SENSOR_TID" =~ ^[0-9]+$ && "$COMPUTE_TID" =~ ^[0-9]+$ && "$ACTUATOR_TID" =~ ^[0-9]+$ && "$TELEMETRY_TID" =~ ^[0-9]+$ && "$COMPUTE_PID" =~ ^[0-9]+$ ]] \
    || die "no thread ids in the stage logs: sensor '$SENSOR_TID', compute '$COMPUTE_TID', actuator '$ACTUATOR_TID', telemetry '$TELEMETRY_TID', compute pid '$COMPUTE_PID'"
printf '%s\n' "label=$LABEL" "level=$LEVEL" "adpf=$ADPF" "duty=$DUTY" "load_s=$LOAD_S" "period_ns=$PERIOD" "frames=$FRAMES" \
    "load_cpus=$LOAD_CPUS" "sensor_tid=$SENSOR_TID" "compute_tid=$COMPUTE_TID" "actuator_tid=$ACTUATOR_TID" \
    "compute_pid=$COMPUTE_PID" "slot_us=$SLOT_US" "telemetry_adpf=$TELEMETRY_ADPF" "telemetry_tid=$TELEMETRY_TID" "headroom=$HEADROOM" >"$OUT/run.conf"
printf '%s\n' "rhizo_sha=$BATCH_RHIZO_SHA" "tempo_sha=$(git rev-parse --short=12 HEAD)" "apk_sha256=$BATCH_APK_SHA256" "gpu=$BATCH_GPU" "renderer=$BATCH_RENDERER" "guest_kernel=${BATCH_GUEST_KERNEL:-stock}" \
    "guest_kernel_sha256=${BATCH_GUEST_KERNEL_SHA256:-}" "guest_initrd_sha256=${BATCH_GUEST_INITRD_SHA256:-}" >"$OUT/versions.txt"
python3 benchmark/score/rhizo/trace.py "$OUT/trace.pftrace" --tids "$SENSOR_TID,$COMPUTE_TID,$ACTUATOR_TID" --pid "$COMPUTE_PID" \
    >"$OUT/slices.json" 2>"$OUT/trace.err" || die "the trace was refused: $(cat "$OUT/trace.err")"
# print a key of an idle-check file
idle_key() { sed -n "s/.*\b$1=\([^ ]*\).*/\1/p" "$OUT/exclusive-$2.txt"; }
printf 'exclusive=%s exclusive_end=%s topology=single-level\n' "$(idle_key exclusive start)" "$(idle_key exclusive end)" >"$OUT/host.meta"
python3 benchmark/score/rhizo/frames.py "$OUT" | tee "$OUT/fact.txt" || die "frames.py could not read $OUT"
