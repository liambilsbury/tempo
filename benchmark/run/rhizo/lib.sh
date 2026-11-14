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
say() { echo "[$(date +%H:%M:%S)] $*"; }
show() { local t="$*"; echo "${t//${OUT:-<none>}/<out>}" >&3; }
phase() { if (( DRY )); then show "# $*"; else say "$*"; fi; }
G() { if (( DRY )); then show "guest>   $*"; else ssh_vm -t "${GT:-60}" "$@"; fi; }
A() { if (( DRY )); then show "android> $*"; else ANDROID_SH_TIMEOUT="${AT:-60}" android_sh "$INIT" "$*"; fi; }
adbx() { if (( DRY )); then show "adb>     $*"; else timeout 120 "$ADB" -s "$SERIAL" "$@" 3>&-; fi; }

rhizo_tool() {
    local hits
    hits="$(git -C "$RHIZO" ls-files -- "$1/*" | awk -F/ -v n="$2" '$NF == n')"
    [ -n "$hits" ] || { echo "$RHIZO_SCRIPT: no $1/$2" >&2; return 1; }
    [ "$(wc -l <<<"$hits")" = 1 ] || { echo "$RHIZO_SCRIPT: more than one $2:" $hits >&2; return 1; }
    echo "$RHIZO/$hits"
}
rhizo_tools() {
    # shellcheck disable=SC2034
    DEV="$(rhizo_tool test/vm dev.sh)" && CLAIM="$(rhizo_tool test/vm claim.sh)" && COMMON="$(rhizo_tool test/vm common.sh)" \
        && TUNNEL="$(rhizo_tool test/vm adb-tunnel.sh)" && SYSLIB="$(rhizo_tool test/system lib.sh)" \
        && INSTALL_LIB="$(rhizo_tool test/system install-lib.sh)"
}
rhizo_source() {
    export RHIZO_VM
    local here="$PWD"
    cd "$(dirname "$COMMON")" || die "cd $(dirname "$COMMON") failed"
    # shellcheck disable=SC1090
    source "$COMMON"
    # shellcheck disable=SC1090
    source "$SYSLIB"
    # shellcheck disable=SC1090
    source "$INSTALL_LIB"
    cd "$here" || exit 2
}
read_state() {
    [ -f "$STATE" ] || die "no $STATE"
    local k v
    while IFS='=' read -r k v; do
        case "$k" in
            rhizo|rhizo_sha|vm|session|qemu_pid|serial|adb|vcpu_cpus|other_cpu|awake|apk_sha256|gpu|renderer|headroom|guest_kernel|guest_kernel_sha256|guest_initrd_sha256) printf -v "BATCH_${k^^}" '%s' "$v";;
        esac
    done <"$STATE"
}
held_vm_is() { "$CLAIM" --renew >/dev/null 2>&1 && [ "$("$CLAIM" --held 2>/dev/null | head -1)" = "$1" ]; }
policy_save_cmd() { echo "mkdir -p $(dirname "$1"); [ -e $2 ] || [ -e $2.none ] || { if [ -e $1 ]; then cp -a $1 $2; else : > $2.none; fi; }"; }
policy_restore_cmd() { echo "if [ -e $2 ]; then mv -f $2 $1; elif [ -e $2.none ]; then rm -f $1 $2.none; fi"; }
gles_renderer() { grep -m1 '^[[:space:]]*GLES: ' | grep -o 'llvmpipe\|virgl' | head -1; }
