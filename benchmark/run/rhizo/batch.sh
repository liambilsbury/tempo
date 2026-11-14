#!/usr/bin/env bash
usage() {
    cat <<'EOF2'
usage: benchmark/run/rhizo/batch.sh --rhizo TREE [--gpu on|off] [--headroom on|off] [--guest-kernel stock|steal] [--dry-run] -- COMMAND [ARG...]
       benchmark/run/rhizo/batch.sh --teardown
env: RHIZO_TREE
EOF2
}
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 2
ROOT="$PWD"
RHIZO_SCRIPT=rhizo_batch
. benchmark/host/settings.conf
. benchmark/run/rhizo/lib.sh

RHIZO="${RHIZO_TREE:-}" DRY=0 TEARDOWN=0 GPU=on HEADROOM=off GUEST_KERNEL=stock
while [ $# -gt 0 ]; do
    case "$1" in
        --rhizo) (( $# >= 2 )) || die "--rhizo needs TREE"; RHIZO="$2"; shift 2;;
        --gpu) (( $# >= 2 )) || die "--gpu needs on|off"; GPU="$2"; shift 2;;
        --headroom) (( $# >= 2 )) || die "--headroom needs on|off"; HEADROOM="$2"; shift 2;;
        --guest-kernel) (( $# >= 2 )) || die "--guest-kernel needs stock|steal"; GUEST_KERNEL="$2"; shift 2;;
        --dry-run) DRY=1; shift;;
        --teardown) TEARDOWN=1; shift;;
        --) shift; break;;
        -h|--help) usage; exit 0;;
        *) die "unknown argument '$1'";;
    esac
done
[[ "$GPU" == on || "$GPU" == off ]] || die "--gpu '$GPU' not on or off"
[[ "$HEADROOM" == on || "$HEADROOM" == off ]] || die "--headroom '$HEADROOM' not on or off"
[[ "$GUEST_KERNEL" == stock || "$GUEST_KERNEL" == steal ]] || die "--guest-kernel '$GUEST_KERNEL'"
IFS=, read -ra VCPUS <<<"$RHIZO_VCPU_CPUS"
NV=${#VCPUS[@]}
(( NV == 4 )) || die "RHIZO_VCPU_CPUS: $NV CPUs, want 4"
export RHIZO_VM_CPUSET="$RHIZO_VCPU_CPUS,$RHIZO_OTHER_CPU" RHIZO_VM_SMP="$NV" RHIZO_SESSION_ID=tempo-rhizo-batch
export RHIZO_SESSION_PID=$$
APK="$ROOT/build/android/tempo-loop.apk" CPU_LOAD_BIN="$ROOT/build/rhizo_vm/cpu_load"
POLICY='[rt]\nbudgets = on\nbudget_percent = 25\n\n[trace]\nhost_delay = on\n'
export RHIZO_VM_GPU="$GPU"
if [ "$GUEST_KERNEL" = steal ]; then export RHIZO_KERNEL=steal; else unset RHIZO_KERNEL; fi
if [ "$GPU" = off ]; then RENDERER_WANT=llvmpipe; POLICY+='\n[devices]\ndri = off\n'; else RENDERER_WANT=virgl; fi
[ "$HEADROOM" = on ] && POLICY+='\n[power]\ncpu_headroom = on\n'
RENDERER=""
exec 3>&1
TD_FAIL=0 POLICY_WRITTEN=0 PINNED=0 STAGED=0 AWAKE=0 UP=0 INIT="" SERIAL="" ADB=""

T() { if (( DRY )); then show "tools>   $*"; else "$@" 3>&-; fi; }
uninstall_app() {
    adbx uninstall "$APP" >"$OUT/uninstall.txt" 2>&1
    (( DRY )) && return 0
    local left
    left="$(A "pm path $APP; echo PM_PATH_DONE" 2>&1 | tr -d '\r')"
    grep -q PM_PATH_DONE <<<"$left" && ! grep -q '^package:' <<<"$left"
}
teardown() {
    trap - EXIT
    phase "teardown"
    if (( PINNED )); then
        if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py restore --state $OUT/vcpus.json"
        elif [ -f "$OUT/vcpus.json" ]; then
            if python3 benchmark/run/rhizo/host.py restore --state "$OUT/vcpus.json"; then rm -f "$OUT/vcpus.json"
            else say "vCPU affinity NOT restored"; TD_FAIL=1; fi
        fi
    fi
    if (( STAGED )); then
        A "rm -rf $RDIR" >/dev/null 2>&1
        uninstall_app || { say "$APP still installed"; TD_FAIL=1; }
        G "rm -f $GUEST_CPU_LOAD" >/dev/null 2>&1
    fi
    if (( AWAKE )); then
        if (( DRY )); then show "tools>   android_release_stay_awake <init>"
        else android_release_stay_awake "$INIT" >"$OUT/release-awake.txt" 2>&1 \
            || { say "display timeout NOT restored"; TD_FAIL=1; }; fi
    fi
    if (( POLICY_WRITTEN )); then G "$(policy_restore_cmd "$POLICY_CONF" "$POLICY_SAVED")" || { say "policy.conf NOT restored"; TD_FAIL=1; }; fi
    if (( UP && ! DRY )) && [ -n "$INIT" ] && declare -F android_sh >/dev/null; then
        mkdir -p "$ROOT/build/rhizo_vm"; A 'logcat -b crash -d' >"$ROOT/build/rhizo_vm/logcat-crash-gpu-$GPU.txt" 2>&1
        say "crash lines=$(grep -c . "$ROOT/build/rhizo_vm/logcat-crash-gpu-$GPU.txt" 2>/dev/null)"
    fi
    if (( UP )); then T "$DEV" down || { say "dev.sh down failed"; TD_FAIL=1; }; fi
    if (( DRY )); then rm -rf "$OUT"
    elif (( TD_FAIL )); then say "failed: kept $STATE"
    else rm -f "$STATE"; fi
    if (( TD_FAIL )); then exit 2; fi
}
write_state() {
    printf '%s\n' "rhizo=$RHIZO" "rhizo_sha=$(git -C "$RHIZO" rev-parse HEAD)" "vm=$RHIZO_VM" "session=$RHIZO_SESSION_ID" \
        "qemu_pid=${QPID:-}" "serial=${SERIAL:-}" "adb=${ADB:-}" "vcpu_cpus=$RHIZO_VCPU_CPUS" "other_cpu=$RHIZO_OTHER_CPU" "awake=$AWAKE" \
        "gpu=$GPU" "renderer=$RENDERER" "headroom=$HEADROOM" "guest_kernel=$GUEST_KERNEL" \
        "guest_kernel_sha256=${KERNEL_SHA:-}" "guest_initrd_sha256=${INITRD_SHA:-}" \
        "apk_sha256=${APK_SHA256:-}" >"$STATE"
}

if (( TEARDOWN )); then
    OUT="$STATE_DIR"
    read_state
    RHIZO="$BATCH_RHIZO"; rhizo_tools || exit 2
    RHIZO_VM="$BATCH_VM"; export RHIZO_SESSION_ID="$BATCH_SESSION"
    rhizo_source
    INIT="$(guest_init_pid 10)" SERIAL="$BATCH_SERIAL" ADB="$BATCH_ADB"
    POLICY_WRITTEN=1 PINNED=1 STAGED=1 AWAKE="${BATCH_AWAKE:-0}" UP=1
    teardown
    exit "$TD_FAIL"
fi

[ $# -gt 0 ] || die "no command after --"
[ -n "$RHIZO" ] || die "no Rhizo tree"
[ -d "$RHIZO" ] || die "no $RHIZO"
RHIZO="$(cd "$RHIZO" && pwd)"
rhizo_tools || exit 2
if (( DRY )); then OUT="$(mktemp -d)"
else
    OUT="$STATE_DIR"; mkdir -p "$OUT" || die "cannot create $OUT"
    [ ! -e "$STATE" ] || die "batch already set up"
    [ -f "$APK" ] && [ -x "$CPU_LOAD_BIN" ] || die "build first"
    make -q build/android/tempo-loop.apk build/rhizo_vm/cpu_load build/inject >/dev/null 2>&1 \
        || die "build is older than its sources"
    if [ -n "${TEMPO_RHIZO_INJECT:-}" ]; then
        [[ "$TEMPO_RHIZO_INJECT" == */build/inject ]] && make -q -C "${TEMPO_RHIZO_INJECT%/build/inject}" build/inject >/dev/null 2>&1 \
            || die "TEMPO_RHIZO_INJECT=$TEMPO_RHIZO_INJECT stale"
    fi
    APK_SHA256="$(sha256sum "$APK" | cut -d' ' -f1)"
fi
trap teardown EXIT

phase "boot: RHIZO_KERNEL=${RHIZO_KERNEL:-<distro>} RHIZO_VM_GPU=$GPU RHIZO_VM_CPUSET=$RHIZO_VM_CPUSET RHIZO_VM_SMP=$NV RHIZO_SESSION_ID=$RHIZO_SESSION_ID RHIZO_SESSION_PID=$RHIZO_SESSION_PID"
UP=1
T "$DEV" up >"$OUT/up.log" 2>&1 || die "dev.sh up failed ($OUT/up.log)"
if (( DRY )); then RHIZO_VM="<vm>"
else
    RHIZO_VM="$("$CLAIM" --held 2>/dev/null | head -1)"
    [[ "$RHIZO_VM" =~ ^[0-9]+$ ]] || die "no vm held"
    rhizo_source
    write_state
fi

phase "policy"
POLICY_WRITTEN=1
G "$(policy_save_cmd "$POLICY_CONF" "$POLICY_SAVED")" || die "policy.conf save failed"
G "printf '$POLICY' > $POLICY_CONF" || die "$POLICY_CONF write failed"
T "$DEV" push >"$OUT/push.log" 2>&1 || die "dev.sh push failed ($OUT/push.log)"

headroom_link() { HEADROOM_LINK="$(GT=10 G "dmesg | grep -o 'RHIZO_MARKER power_headroom=[a-z]*' | tail -1" 2>/dev/null | tr -d '\r')"; HEADROOM_LINK="${HEADROOM_LINK##*=}"; [ "$HEADROOM_LINK" = on ]; }
if [ "$HEADROOM" = on ]; then
    if (( DRY )); then show "guest>   dmesg | grep 'RHIZO_MARKER power_headroom='"
    elif ! poll_until 60 2 headroom_link; then
        say "power_headroom=${HEADROOM_LINK:-none}"
        T "$DEV" push >"$OUT/push-again.log" 2>&1 || die "dev.sh push failed ($OUT/push-again.log)"
        poll_until 60 2 headroom_link || die "power_headroom=${HEADROOM_LINK:-none}"
    fi
fi

phase "check"
hd_connected() { POLL_READ="$(A "dumpsys rhizo.host_delay" 2>&1 | tr -d '\r')"; grep -q 'connected=yes' <<<"$POLL_READ"; }
if (( DRY )); then
    QPID="<qemu pid>" INIT="<init>"
    show "# qemu affinity=$RHIZO_VM_CPUSET nproc=$NV uname -v $([ "$GUEST_KERNEL" = steal ] && echo "+steal" || echo "!+steal"); is-active rhizo-rtd rhizo-delayd; rhizo.host_delay connected=yes <=90s"
else
    QPID="$(cat "$VM_PIDFILE" 2>/dev/null)"
    [[ "$QPID" =~ ^[0-9]+$ ]] || die "no qemu pid in $VM_PIDFILE"
    python3 -c 'import os, sys; sys.exit(sorted(os.sched_getaffinity(int(sys.argv[1]))) != sorted(int(c) for c in sys.argv[2].split(",")))' \
        "$QPID" "$RHIZO_VM_CPUSET" || die "qemu affinity != $RHIZO_VM_CPUSET"
    n="$(ssh_vm -t 10 nproc | tr -d '\r')"; [ "$n" = "$NV" ] || die "nproc '$n' != $NV"
    uv="$(ssh_vm -t 10 'uname -r; uname -v' | tr -d '\r' | tr '\n' ' ')"; echo "$uv" >"$OUT/guest-kernel.txt"
    KERNEL_SHA="$(sha256sum "$RHIZO/var/$([ "$GUEST_KERNEL" = steal ] && echo vmlinuz-steal || echo vmlinuz)" 2>/dev/null | cut -d' ' -f1)"
    INITRD_SHA="$(sha256sum "$RHIZO/var/initrd.img" 2>/dev/null | cut -d' ' -f1)"
    [[ "$KERNEL_SHA" =~ ^[0-9a-f]{64}$ && "$INITRD_SHA" =~ ^[0-9a-f]{64}$ ]] || die "no kernel/initrd sha256"
    if [ "$GUEST_KERNEL" = steal ]; then [[ "$uv" == *"+steal "* ]] || die "kernel '$uv' not +steal"
    else [[ "$uv" != *"+steal "* ]] || die "kernel '$uv' is +steal"; fi
    st="$(ssh_vm -t 15 "systemctl is-active rhizo-rtd.service rhizo-delayd.service" | tr -d '\r' | tr '\n' ' ')"
    [ "$st" = "active active " ] || die "rhizo-rtd rhizo-delayd: $st"
    if [ "$HEADROOM" = on ]; then
        st="$(ssh_vm -t 15 "systemctl is-active rhizo-headroomd.service" | tr -d '\r\n')"
        [ "$st" = active ] || die "rhizo-headroomd: $st"
    fi
    INIT="$(guest_init_pid 30)"; [ -n "$INIT" ] || die "no Android init"
    poll_until 90 2 hd_connected || die "host_delay not connected"
fi

sf_renderer() { RENDERER="$(A 'dumpsys SurfaceFlinger' | tr -d '\r' | gles_renderer)"; [ -n "$RENDERER" ]; }
check_renderer() {
    if (( DRY )); then show "android> dumpsys SurfaceFlinger | grep '^GLES: '"; RENDERER="$RENDERER_WANT"; return 0; fi
    poll_until 30 2 sf_renderer
    [ "$RENDERER" = "$RENDERER_WANT" ] || die "renderer '${RENDERER:-none}' != $RENDERER_WANT"
}
check_renderer

power_link() { POWER="$(GT=10 G "dmesg | grep -o 'RHIZO_MARKER power_sessions=[a-z]*' | tail -1" 2>/dev/null | tr -d '\r')"; POWER="${POWER##*=}"; [ "$POWER" = on ]; }
check_power_sessions() {
    if (( DRY )); then show "guest>   dmesg | grep 'RHIZO_MARKER power_sessions='"; return 0; fi
    poll_until 60 2 power_link || die "power_sessions=${POWER:-none}"
}
check_power_sessions

phase "awake"
AWAKE=1
if (( DRY )); then show "tools>   android_stay_awake <init>"
else
    write_state
    android_stay_awake "$INIT" >"$OUT/stay-awake.txt" 2>&1 || die "android_stay_awake failed"
    grep -q 'mWakefulness=Awake' "$OUT/stay-awake.txt" || die "Android not awake"
fi

phase "pin"
PINNED=1
rm -f "$OUT/vcpus.json"
if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py pin --qmp <qmp socket> --pid <qemu pid> --cpus $RHIZO_VCPU_CPUS --other $RHIZO_OTHER_CPU --state $OUT/vcpus.json"
else
    python3 benchmark/run/rhizo/host.py pin --qmp "$QMP_SOCK" --pid "$QPID" --cpus "$RHIZO_VCPU_CPUS" --other "$RHIZO_OTHER_CPU" \
        --state "$OUT/vcpus.json" >"$OUT/pin.txt" 2>&1 || die "pin failed"
fi

phase "stage"
STAGED=1
T "$TUNNEL" up >"$OUT/adb-tunnel.log" 2>&1 || die "adb-tunnel.sh up failed"
if (( DRY )); then ADB=adb SERIAL="127.0.0.1:<adb port>"
else ADB="$(command -v adb)" || die "no adb"; SERIAL="127.0.0.1:$ADB_PORT"; fi
(( DRY )) || write_state
A "rm -rf $RDIR; mkdir -p $RDIR" || die "mkdir $RDIR failed"
uninstall_app || die "uninstall failed"
adbx install -r "$APK" >"$OUT/install.txt" 2>&1 || die "adb install failed"
(( DRY )) || grep -q Success "$OUT/install.txt" || die "adb install: no Success"
if (( DRY )); then show "guest>   cat > $GUEST_CPU_LOAD && chmod 755 $GUEST_CPU_LOAD   # from $CPU_LOAD_BIN"
else G "cat > $GUEST_CPU_LOAD && chmod 755 $GUEST_CPU_LOAD" <"$CPU_LOAD_BIN" || die "load stage failed"; fi

guest_busy() { GT=30 G "a=\$(grep '^cpu ' /proc/stat); sleep 10; b=\$(grep '^cpu ' /proc/stat); echo \$a; echo \$b" | tr -d '\r' \
    | awk 'NR == 1 { for (i = 2; i <= 9; i++) a[i] = $i } NR == 2 { t = 0; for (i = 2; i <= 9; i++) { d[i] = $i - a[i]; t += d[i] } if (t > 0) printf "%d", 100 * (d[2] + d[3] + d[4]) / t }'; }
if [ "$HEADROOM" = on ]; then
    phase "settle"
    if (( DRY )); then show "guest>   /proc/stat busy < 10%, 300 s max"
    else
        settle_end=$(( SECONDS + 300 )) busy=""
        while :; do
            busy="$(guest_busy)"
            [[ "$busy" =~ ^[0-9]+$ ]] && (( busy < 10 )) && break
            (( SECONDS >= settle_end )) && break
            [[ "$busy" =~ ^[0-9]+$ ]] || sleep 1
        done
        echo "guest_busy_pct=${busy:-unread} waited_s=$(( SECONDS - settle_end + 300 ))" >"$OUT/guest-settle.txt"
        say "$(cat "$OUT/guest-settle.txt")"
    fi
fi

(( DRY )) || write_state
phase "run: $*"
if (( DRY )); then show "local>   $*"; rc=0; else "$@" 3>&-; rc=$?; fi
exit "$rc"
