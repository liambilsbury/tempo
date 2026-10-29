# shared helpers of rhizo/batch.sh and rhizo/run.sh
# shellcheck shell=bash
STATE_DIR="$ROOT/build/rhizo_vm"
STATE="$STATE_DIR/batch.state"
# shellcheck disable=SC2034
RDIR=/data/local/tmp/tempo
# shellcheck disable=SC2034
APP=tempo.loop
# shellcheck disable=SC2034
GUEST_CPU_LOAD=/usr/local/bin/tempo-cpu-load
# shellcheck disable=SC2034
POLICY_CONF=/etc/rhizo/policy.conf
# shellcheck disable=SC2034
POLICY_SAVED=/etc/rhizo/policy.conf.tempo-batch

die() { echo "$RHIZO_SCRIPT: $*" >&2; exit 2; }
# print a timestamped line
say() { echo "[$(date +%H:%M:%S)] $*"; }
# print a dry-run line on fd 3 with the output directory shown as <out>
show() { local t="$*"; echo "${t//${OUT:-<none>}/<out>}" >&3; }
# print a phase heading
phase() { if (( DRY )); then show "# $*"; else say "$*"; fi; }
# run a command as root in the VM's guest, or print it under --dry-run
G() { if (( DRY )); then show "guest>   $*"; else ssh_vm -t "${GT:-60}" "$@"; fi; }
# run a shell body in Android, or print it under --dry-run
A() { if (( DRY )); then show "android> $*"; else ANDROID_SH_TIMEOUT="${AT:-60}" android_sh "$INIT" "$*"; fi; }
# run an adb command against the VM, or print it under --dry-run
adbx() { if (( DRY )); then show "adb>     $*"; else timeout 120 "$ADB" -s "$SERIAL" "$@" 3>&-; fi; }

# print the tracked file named $2 under the Rhizo tree's folder $1, or fail
rhizo_tool() {
    local hits
    hits="$(git -C "$RHIZO" ls-files -- "$1/*" | awk -F/ -v n="$2" '$NF == n')"
    [ -n "$hits" ] || { echo "$RHIZO_SCRIPT: no file named $2 under $RHIZO/$1" >&2; return 1; }
    [ "$(wc -l <<<"$hits")" = 1 ] || { echo "$RHIZO_SCRIPT: more than one $2 under $RHIZO/$1:" $hits >&2; return 1; }
    echo "$RHIZO/$hits"
}
# set the paths of the Rhizo tools a batch uses
rhizo_tools() {
    # shellcheck disable=SC2034
    DEV="$(rhizo_tool test/vm dev.sh)" && CLAIM="$(rhizo_tool test/vm claim.sh)" && COMMON="$(rhizo_tool test/vm common.sh)" \
        && TUNNEL="$(rhizo_tool test/vm adb-tunnel.sh)" && SYSLIB="$(rhizo_tool test/system lib.sh)" \
        && INSTALL_LIB="$(rhizo_tool test/system install-lib.sh)"
}
# source Rhizo's vm and system-test libraries for the vm in RHIZO_VM
rhizo_source() {
    export RHIZO_VM
    local here="$PWD"
    cd "$(dirname "$COMMON")" || die "cannot enter $(dirname "$COMMON")"
    # shellcheck disable=SC1090
    source "$COMMON"
    # shellcheck disable=SC1090
    source "$SYSLIB"
    # shellcheck disable=SC1090
    source "$INSTALL_LIB"
    cd "$here" || exit 2
}
# read the batch state file into BATCH_* variables, or exit when no batch is set up
read_state() {
    [ -f "$STATE" ] || die "no batch is set up, $STATE is missing: run benchmark/run/rhizo/batch.sh"
    local k v
    while IFS='=' read -r k v; do
        case "$k" in
            rhizo|rhizo_sha|vm|session|qemu_pid|serial|adb|vcpu_cpus|other_cpu|awake|apk_sha256|gpu|renderer|headroom|guest_kernel|guest_kernel_sha256|guest_initrd_sha256) printf -v "BATCH_${k^^}" '%s' "$v";;
        esac
    done <"$STATE"
}
# renew this batch's claim and check it still holds the vm
held_vm_is() { "$CLAIM" --renew >/dev/null 2>&1 && [ "$("$CLAIM" --held 2>/dev/null | head -1)" = "$1" ]; }
# print the guest command that keeps a copy of the VM's own policy.conf
policy_save_cmd() { echo "mkdir -p $(dirname "$1"); [ -e $2 ] || [ -e $2.none ] || { if [ -e $1 ]; then cp -a $1 $2; else : > $2.none; fi; }"; }
# print the guest command that puts the VM's own policy.conf back
policy_restore_cmd() { echo "if [ -e $2 ]; then mv -f $2 $1; elif [ -e $2.none ]; then rm -f $1 $2.none; fi"; }
# print the GLES renderer from dumpsys SurfaceFlinger on stdin
gles_renderer() { grep -m1 '^[[:space:]]*GLES: ' | grep -o 'llvmpipe\|virgl' | head -1; }
