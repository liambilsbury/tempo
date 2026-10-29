#!/usr/bin/env bash
# run a command with a Rhizo VM up for a batch
#
# Usage: benchmark/run/rhizo/batch.sh --rhizo TREE [--gpu on|off] [--headroom on|off] [--guest-kernel stock|steal] [--dry-run] -- COMMAND [ARG...]
#        benchmark/run/rhizo/batch.sh --teardown        put back an interrupted batch
#   --rhizo TREE   Rhizo checkout, or RHIZO_TREE
#   --gpu on|off   off renders in software
#   --headroom on|off   Rhizo's CPU headroom
#   --guest-kernel stock|steal   steal: guest kernel with steal accounting
#   --dry-run      print, run nothing
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 2
ROOT="$PWD"
RHIZO_SCRIPT=rhizo_batch
. benchmark/host/settings.conf
. benchmark/run/rhizo/lib.sh

RHIZO="${RHIZO_TREE:-}" DRY=0 TEARDOWN=0 GPU=on HEADROOM=off GUEST_KERNEL=stock
while [ $# -gt 0 ]; do
    case "$1" in
        --rhizo) (( $# >= 2 )) || die "--rhizo needs a tree"; RHIZO="$2"; shift 2;;
        --gpu) (( $# >= 2 )) || die "--gpu needs on or off"; GPU="$2"; shift 2;;
        --headroom) (( $# >= 2 )) || die "--headroom needs on or off"; HEADROOM="$2"; shift 2;;
        --guest-kernel) (( $# >= 2 )) || die "--guest-kernel needs stock or steal"; GUEST_KERNEL="$2"; shift 2;;
        --dry-run) DRY=1; shift;;
        --teardown) TEARDOWN=1; shift;;
        --) shift; break;;
        -h|--help) sed -n '2,10p' "$0"; exit 0;;
        *) die "unknown argument '$1'";;
    esac
done
[[ "$GPU" == on || "$GPU" == off ]] || die "--gpu is '$GPU', not on or off"
[[ "$HEADROOM" == on || "$HEADROOM" == off ]] || die "--headroom is '$HEADROOM', not on or off"
[[ "$GUEST_KERNEL" == stock || "$GUEST_KERNEL" == steal ]] || die "--guest-kernel is '$GUEST_KERNEL', not stock or steal"
IFS=, read -ra VCPUS <<<"$RHIZO_VCPU_CPUS"
NV=${#VCPUS[@]}
(( NV == 4 )) || die "RHIZO_VCPU_CPUS names $NV CPUs; the loop needs 4 vCPUs"
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

# run a Rhizo tool, or print it under --dry-run
T() { if (( DRY )); then show "tools>   $*"; else "$@" 3>&-; fi; }
# uninstall the loop's app, failing when Android still has it afterwards
uninstall_app() {
    adbx uninstall "$APP" >"$OUT/uninstall.txt" 2>&1
    (( DRY )) && return 0
    local left
    left="$(A "pm path $APP; echo PM_PATH_DONE" 2>&1 | tr -d '\r')"
    grep -q PM_PATH_DONE <<<"$left" && ! grep -q '^package:' <<<"$left"
}
# put the VM back and release it
teardown() {
    trap - EXIT
    phase "teardown: the vCPUs' prior affinity, the staged files and the app, the display's own timeout, the VM's own policy.conf, then dev.sh down"
    if (( PINNED )); then
        if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py restore --state $OUT/vcpus.json"
        elif [ -f "$OUT/vcpus.json" ]; then
            if python3 benchmark/run/rhizo/host.py restore --state "$OUT/vcpus.json"; then rm -f "$OUT/vcpus.json"
            else say "the vCPUs' prior affinity was not put back"; TD_FAIL=1; fi
        fi
    fi
    if (( STAGED )); then
        A "rm -rf $RDIR" >/dev/null 2>&1
        uninstall_app || { say "the loop's app $APP is still installed ($OUT/uninstall.txt)"; TD_FAIL=1; }
        G "rm -f $GUEST_CPU_LOAD" >/dev/null 2>&1
    fi
    if (( AWAKE )); then
        if (( DRY )); then show "tools>   android_release_stay_awake <init>"
        else android_release_stay_awake "$INIT" >"$OUT/release-awake.txt" 2>&1 \
            || { say "Android's display did not get its own timeout back ($OUT/release-awake.txt)"; TD_FAIL=1; }; fi
    fi
    if (( POLICY_WRITTEN )); then G "$(policy_restore_cmd "$POLICY_CONF" "$POLICY_SAVED")" || { say "the VM's policy.conf was not put back"; TD_FAIL=1; }; fi
    if (( UP && ! DRY )) && [ -n "$INIT" ] && declare -F android_sh >/dev/null; then
        mkdir -p "$ROOT/build/rhizo_vm"; A 'logcat -b crash -d' >"$ROOT/build/rhizo_vm/logcat-crash-gpu-$GPU.txt" 2>&1
        say "android crash buffer: $(grep -c . "$ROOT/build/rhizo_vm/logcat-crash-gpu-$GPU.txt" 2>/dev/null) lines"
    fi
    if (( UP )); then T "$DEV" down || { say "dev.sh down failed"; TD_FAIL=1; }; fi
    if (( DRY )); then rm -rf "$OUT"
    elif (( TD_FAIL )); then say "a step failed: $STATE is kept for $0 --teardown"
    else rm -f "$STATE"; fi
    if (( TD_FAIL )); then exit 2; fi
}
# write the batch state file with the values known so far
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
[ -n "$RHIZO" ] || die "no Rhizo tree: pass --rhizo TREE or set RHIZO_TREE"
[ -d "$RHIZO" ] || die "no such Rhizo tree: $RHIZO"
RHIZO="$(cd "$RHIZO" && pwd)"
rhizo_tools || exit 2
if (( DRY )); then OUT="$(mktemp -d)"
else
    OUT="$STATE_DIR"; mkdir -p "$OUT" || die "cannot create $OUT"
    [ ! -e "$STATE" ] || die "a batch is already set up ($STATE): put it back with $0 --teardown"
    [ -f "$APK" ] && [ -x "$CPU_LOAD_BIN" ] || die "build first: make android-app rhizo-vm-cpu-load"
    make -q build/android/tempo-loop.apk build/rhizo_vm/cpu_load build/inject >/dev/null 2>&1 \
        || die "build/android/tempo-loop.apk, build/rhizo_vm/cpu_load or build/inject is older than its sources: make android-app rhizo-vm-cpu-load build/inject NDK=<ndk root> SDK=<android sdk root>"
    if [ -n "${TEMPO_RHIZO_INJECT:-}" ]; then
        [[ "$TEMPO_RHIZO_INJECT" == */build/inject ]] && make -q -C "${TEMPO_RHIZO_INJECT%/build/inject}" build/inject >/dev/null 2>&1 \
            || die "TEMPO_RHIZO_INJECT=$TEMPO_RHIZO_INJECT is not a build/inject as new as its tree's sources"
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
    [[ "$RHIZO_VM" =~ ^[0-9]+$ ]] || die "dev.sh up left no vm held"
    rhizo_source
    write_state
fi

phase "policy: rt.budgets and trace.host_delay on, the VM's own policy.conf kept beside it, then dev.sh push"
POLICY_WRITTEN=1
G "$(policy_save_cmd "$POLICY_CONF" "$POLICY_SAVED")" || die "could not keep the VM's policy.conf"
G "printf '$POLICY' > $POLICY_CONF" || die "could not write $POLICY_CONF"
T "$DEV" push >"$OUT/push.log" 2>&1 || die "dev.sh push failed ($OUT/push.log)"

# read the power HAL's last rhizo-headroomd link marker
headroom_link() { HEADROOM_LINK="$(GT=10 G "dmesg | grep -o 'RHIZO_MARKER power_headroom=[a-z]*' | tail -1" 2>/dev/null | tr -d '\r')"; HEADROOM_LINK="${HEADROOM_LINK##*=}"; [ "$HEADROOM_LINK" = on ]; }
if [ "$HEADROOM" = on ]; then
    if (( DRY )); then show "guest>   dmesg | grep 'RHIZO_MARKER power_headroom='   # the last must be on, 60 s; else dev.sh push once more and check again"
    elif ! poll_until 60 2 headroom_link; then
        say "the power HAL did not link to rhizo-headroomd, last marker power_headroom=${HEADROOM_LINK:-none}; pushing once more"
        T "$DEV" push >"$OUT/push-again.log" 2>&1 || die "dev.sh push failed ($OUT/push-again.log)"
        poll_until 60 2 headroom_link || die "the power HAL has no link to rhizo-headroomd after a second boot, last marker power_headroom=${HEADROOM_LINK:-none}, so Android would answer headroom unsupported"
    fi
fi

phase "check: qemu on CPUs $RHIZO_VM_CPUSET, $NV guest CPUs, rhizo-rtd and rhizo-delayd active, Android told of host delays"
# check dumpsys rhizo.host_delay for connected=yes
hd_connected() { POLL_READ="$(A "dumpsys rhizo.host_delay" 2>&1 | tr -d '\r')"; grep -q 'connected=yes' <<<"$POLL_READ"; }
if (( DRY )); then
    QPID="<qemu pid>" INIT="<init>"
    show "# qemu's affinity is $RHIZO_VM_CPUSET; nproc is $NV; uname -v $([ "$GUEST_KERNEL" = steal ] && echo "names +steal" || echo "does not name +steal"); systemctl is-active rhizo-rtd rhizo-delayd; poll dumpsys rhizo.host_delay for connected=yes, 90 s"
else
    QPID="$(cat "$VM_PIDFILE" 2>/dev/null)"
    [[ "$QPID" =~ ^[0-9]+$ ]] || die "no qemu pid in $VM_PIDFILE"
    python3 -c 'import os, sys; sys.exit(sorted(os.sched_getaffinity(int(sys.argv[1]))) != sorted(int(c) for c in sys.argv[2].split(",")))' \
        "$QPID" "$RHIZO_VM_CPUSET" || die "qemu $QPID does not run on $RHIZO_VM_CPUSET: run $DEV down, then this again"
    n="$(ssh_vm -t 10 nproc | tr -d '\r')"; [ "$n" = "$NV" ] || die "the guest has '$n' CPUs, not $NV"
    uv="$(ssh_vm -t 10 'uname -r; uname -v' | tr -d '\r' | tr '\n' ' ')"; echo "$uv" >"$OUT/guest-kernel.txt"
    KERNEL_SHA="$(sha256sum "$RHIZO/var/$([ "$GUEST_KERNEL" = steal ] && echo vmlinuz-steal || echo vmlinuz)" 2>/dev/null | cut -d' ' -f1)"
    INITRD_SHA="$(sha256sum "$RHIZO/var/initrd.img" 2>/dev/null | cut -d' ' -f1)"
    [[ "$KERNEL_SHA" =~ ^[0-9a-f]{64}$ && "$INITRD_SHA" =~ ^[0-9a-f]{64}$ ]] || die "cannot read the sha256 of the guest kernel and initrd under $RHIZO/var"
    if [ "$GUEST_KERNEL" = steal ]; then [[ "$uv" == *"+steal "* ]] || die "the guest booted '$uv', not the steal-accounting kernel"
    else [[ "$uv" != *"+steal "* ]] || die "the guest booted the steal-accounting kernel ('$uv') with --guest-kernel stock"; fi
    st="$(ssh_vm -t 15 "systemctl is-active rhizo-rtd.service rhizo-delayd.service" | tr -d '\r' | tr '\n' ' ')"
    [ "$st" = "active active " ] || die "rhizo-rtd and rhizo-delayd are not both active: $st"
    if [ "$HEADROOM" = on ]; then
        st="$(ssh_vm -t 15 "systemctl is-active rhizo-headroomd.service" | tr -d '\r\n')"
        [ "$st" = active ] || die "rhizo-headroomd is not active: $st"
    fi
    INIT="$(guest_init_pid 30)"; [ -n "$INIT" ] || die "no Android init after the push"
    poll_until 90 2 hd_connected || die "dumpsys rhizo.host_delay never said connected=yes"
fi

# read SurfaceFlinger's GLES renderer
sf_renderer() { RENDERER="$(A 'dumpsys SurfaceFlinger' | tr -d '\r' | gles_renderer)"; [ -n "$RENDERER" ]; }
# check SurfaceFlinger's GLES renderer is the one this --gpu setting asks for
check_renderer() {
    if (( DRY )); then show "android> dumpsys SurfaceFlinger | grep '^GLES: '   # renderer must be $RENDERER_WANT"; RENDERER="$RENDERER_WANT"; return 0; fi
    poll_until 30 2 sf_renderer
    [ "$RENDERER" = "$RENDERER_WANT" ] || die "SurfaceFlinger renders on '${RENDERER:-nothing}', not $RENDERER_WANT (--gpu $GPU)"
}
check_renderer

# read the power HAL's last rhizo-rtd link marker
power_link() { POWER="$(GT=10 G "dmesg | grep -o 'RHIZO_MARKER power_sessions=[a-z]*' | tail -1" 2>/dev/null | tr -d '\r')"; POWER="${POWER##*=}"; [ "$POWER" = on ]; }
# check the power HAL has a link to rhizo-rtd
check_power_sessions() {
    if (( DRY )); then show "guest>   dmesg | grep 'RHIZO_MARKER power_sessions='   # the last must be on, 60 s"; return 0; fi
    poll_until 60 2 power_link || die "the power HAL has no link to rhizo-rtd, last marker power_sessions=${POWER:-none}, so every ADPF hint session this boot would fail"
}
check_power_sessions

phase "awake: Android's display kept on for the whole batch by Rhizo's android_stay_awake, so the loop's app stays in the foreground"
AWAKE=1
if (( DRY )); then show "tools>   android_stay_awake <init>"
else
    write_state
    android_stay_awake "$INIT" >"$OUT/stay-awake.txt" 2>&1 || die "android_stay_awake failed ($OUT/stay-awake.txt)"
    grep -q 'mWakefulness=Awake' "$OUT/stay-awake.txt" || die "Android is not awake after android_stay_awake ($OUT/stay-awake.txt)"
fi

phase "pin: vCPU i alone on CPU i of $RHIZO_VCPU_CPUS, qemu's other threads on $RHIZO_OTHER_CPU"
PINNED=1
rm -f "$OUT/vcpus.json"
if (( DRY )); then show "local>   python3 benchmark/run/rhizo/host.py pin --qmp <qmp socket> --pid <qemu pid> --cpus $RHIZO_VCPU_CPUS --other $RHIZO_OTHER_CPU --state $OUT/vcpus.json"
else
    python3 benchmark/run/rhizo/host.py pin --qmp "$QMP_SOCK" --pid "$QPID" --cpus "$RHIZO_VCPU_CPUS" --other "$RHIZO_OTHER_CPU" \
        --state "$OUT/vcpus.json" >"$OUT/pin.txt" 2>&1 || die "could not pin the vCPUs ($OUT/pin.txt)"
fi

phase "stage: the loop's app $APP into Android over adb, a folder for the trace, the load into the guest"
STAGED=1
T "$TUNNEL" up >"$OUT/adb-tunnel.log" 2>&1 || die "adb-tunnel.sh up failed ($OUT/adb-tunnel.log)"
if (( DRY )); then ADB=adb SERIAL="127.0.0.1:<adb port>"
else ADB="$(command -v adb)" || die "no adb on PATH"; SERIAL="127.0.0.1:$ADB_PORT"; fi
(( DRY )) || write_state
A "rm -rf $RDIR; mkdir -p $RDIR" || die "could not make $RDIR"
uninstall_app || die "a tempo.loop already in Android could not be removed before the install ($OUT/uninstall.txt)"
adbx install -r "$APK" >"$OUT/install.txt" 2>&1 || die "adb install failed ($OUT/install.txt)"
(( DRY )) || grep -q Success "$OUT/install.txt" || die "adb install did not say Success ($OUT/install.txt)"
if (( DRY )); then show "guest>   cat > $GUEST_CPU_LOAD && chmod 755 $GUEST_CPU_LOAD   # from $CPU_LOAD_BIN"
else G "cat > $GUEST_CPU_LOAD && chmod 755 $GUEST_CPU_LOAD" <"$CPU_LOAD_BIN" || die "could not stage the load in the guest"; fi

# print the guest's busy CPU percent over 10 s
guest_busy() { GT=30 G "a=\$(grep '^cpu ' /proc/stat); sleep 10; b=\$(grep '^cpu ' /proc/stat); echo \$a; echo \$b" | tr -d '\r' \
    | awk 'NR == 1 { for (i = 2; i <= 9; i++) a[i] = $i } NR == 2 { t = 0; for (i = 2; i <= 9; i++) { d[i] = $i - a[i]; t += d[i] } if (t > 0) printf "%d", 100 * (d[2] + d[3] + d[4]) / t }'; }
if [ "$HEADROOM" = on ]; then
    phase "settle: the guest's own CPU use under 10% of its vCPUs over 10 s, 300 s at most, before the first run"
    if (( DRY )); then show "guest>   two /proc/stat cpu lines 10 s apart until user, nice and system are under 10% of the total, 300 s at most"
    else
        settle_end=$(( SECONDS + 300 )) busy=""
        while :; do
            busy="$(guest_busy)"
            [[ "$busy" =~ ^[0-9]+$ ]] && (( busy < 10 )) && break
            (( SECONDS >= settle_end )) && break
            [[ "$busy" =~ ^[0-9]+$ ]] || sleep 1
        done
        echo "guest_busy_pct=${busy:-unread} waited_s=$(( SECONDS - settle_end + 300 ))" >"$OUT/guest-settle.txt"
        say "the guest's own CPU use before the first run: ${busy:-unread}% of its vCPUs ($(cat "$OUT/guest-settle.txt"))"
    fi
fi

(( DRY )) || write_state
phase "run: $*"
if (( DRY )); then show "local>   $*"; rc=0; else "$@" 3>&-; rc=$?; fi
exit "$rc"
