#!/usr/bin/env bash
# fetch, verify, configure and build the pinned L2 guest kernel, rt or nonrt
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ROOT="$PWD"

KVER="6.12.103"
KSHA256="f143aaade8877ba5616e788b4482576db28481bcf557ef537f4fcc3938fc3176"
KURL="https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${KVER}.tar.xz"

SRC_DIR="$ROOT/build/kernel-src"
TARBALL="$SRC_DIR/linux-${KVER}.tar.xz"

LOAD_CEILING=4.0
DISK_FLOOR_GB=10

SHARE=0 VARIANT=rt DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --share)   SHARE=1; shift;;
        --variant) VARIANT="$2"; shift 2;;
        --dry-run) DRY=1; shift;;
        *) echo "build_kernel: unknown argument $1" >&2; exit 2;;
    esac
done
case "$VARIANT" in rt|nonrt) ;; *) echo "build_kernel: --variant is rt or nonrt, not $VARIANT" >&2; exit 2;; esac
if [ "$VARIANT" = rt ]; then SRC_TREE="$SRC_DIR/linux-${KVER}"; OUT_DIR="$ROOT/build/kernel-rt"
else                          SRC_TREE="$SRC_DIR/linux-${KVER}-nonrt"; OUT_DIR="$ROOT/build/kernel-nonrt"; fi

# print the scripts/config arguments that set the preemption model
preempt_deltas() {
    echo "--enable EXPERT"
    echo "--disable PREEMPT_NONE"
    echo "--disable PREEMPT_VOLUNTARY"
    if [ "$VARIANT" = rt ]; then
        echo "--disable PREEMPT"
        echo "--enable PREEMPT_RT"
    else
        echo "--disable PREEMPT_RT"
        echo "--enable PREEMPT"
        echo "--enable PREEMPT_DYNAMIC"
    fi
}

# print every scripts/config argument pair this build applies
deltas() {
    preempt_deltas

    echo "--enable VIRTIO"
    echo "--enable VIRTIO_PCI"
    echo "--enable VIRTIO_BLK"
    echo "--enable VIRTIO_CONSOLE"

    echo "--enable SERIAL_8250"
    echo "--enable SERIAL_8250_CONSOLE"

    echo "--enable BLK_DEV_INITRD"
    echo "--enable DEVTMPFS"
    echo "--enable TMPFS"
    echo "--enable PROC_FS"
    echo "--enable SYSFS"

    echo "--enable HIGH_RES_TIMERS"
    echo "--disable HZ_PERIODIC"
    echo "--disable NO_HZ_IDLE"
    echo "--enable NO_HZ_FULL"
    echo "--enable RCU_NOCB_CPU"
    echo "--enable CPU_ISOLATION"

    echo "--enable SMP"
    echo "--enable HYPERVISOR_GUEST"
    echo "--enable KVM_GUEST"
    echo "--enable PARAVIRT"

    echo "--enable FTRACE"
    echo "--enable OSNOISE_TRACER"
    echo "--enable TIMERLAT_TRACER"
}

if [ "$VARIANT" = rt ]; then preempt_required=(PREEMPT_RT); forbidden=()
else                          preempt_required=(PREEMPT PREEMPT_DYNAMIC); forbidden=(PREEMPT_RT); fi
required=("${preempt_required[@]}" EXPERT VIRTIO VIRTIO_PCI VIRTIO_BLK VIRTIO_CONSOLE SERIAL_8250
          SERIAL_8250_CONSOLE BLK_DEV_INITRD DEVTMPFS TMPFS PROC_FS SYSFS HIGH_RES_TIMERS
          NO_HZ_FULL RCU_NOCB_CPU CPU_ISOLATION SMP HYPERVISOR_GUEST KVM_GUEST PARAVIRT
          FTRACE OSNOISE_TRACER TIMERLAT_TRACER)

[ "$DRY" = 1 ] && { echo "variant: $VARIANT"; deltas; echo "required: ${required[*]}"; echo "forbidden: ${forbidden[*]}"; exit 0; }

# check for a running tempo measurement by process name
measuring() {
    ps -eo comm= | grep -qE '^(qemu-system|pipeline$)' && return 0
    ps -eo comm=,args= | awk '$1 ~ /^python3/ && /benchmark\/(paired|experiment)/ {found=1}
                              END {exit !found}' && return 0
    return 1
}
if measuring; then
    echo "build_kernel: refusing -- a tempo measurement is running right now" >&2
    exit 1
fi

load1="$(awk '{print $1}' /proc/loadavg)"
if awk -v l="$load1" -v c="$LOAD_CEILING" 'BEGIN{exit !(l>c)}'; then
    if [ "$SHARE" = 1 ]; then
        echo "build_kernel: load1=$load1 above $LOAD_CEILING, proceeding at lowest priority" \
             "for --share"
        renice -n 19 -p $$ >/dev/null 2>&1 || true
        ionice -c 3 -p $$ >/dev/null 2>&1 || true
    else
        echo "build_kernel: refusing -- load1=$load1 above ceiling $LOAD_CEILING" \
             "-- pass --share if it is only load" >&2
        exit 1
    fi
fi
free_gb="$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc '0-9')"
if [ "${free_gb:-0}" -lt "$DISK_FLOOR_GB" ]; then
    echo "build_kernel: refusing -- ${free_gb}G free below the ${DISK_FLOOR_GB}G floor" >&2
    exit 1
fi

mkdir -p "$SRC_DIR" "$OUT_DIR"
if [ ! -f "$TARBALL" ]; then
    echo "build_kernel: fetching linux-${KVER}.tar.xz"
    curl -sS -m 600 -o "$TARBALL.partial" "$KURL"
    mv "$TARBALL.partial" "$TARBALL"
fi
actual_sha="$(sha256sum "$TARBALL" | awk '{print $1}')"
if [ "$actual_sha" != "$KSHA256" ]; then
    echo "build_kernel: sha256 mismatch on $TARBALL" >&2
    echo "  expected $KSHA256" >&2
    echo "  got      $actual_sha" >&2
    exit 1
fi
echo "build_kernel: sha256 verified: $KSHA256"

if [ ! -d "$SRC_TREE" ]; then
    if [ "$VARIANT" = rt ]; then
        echo "build_kernel: extracting"
        tar -C "$SRC_DIR" -xf "$TARBALL"
    else
        stage="$SRC_DIR/.x-$$"
        mkdir -p "$stage"
        echo "build_kernel: extracting nonrt"
        tar -C "$stage" -xf "$TARBALL" || { rm -rf "$stage"; echo "build_kernel: extract failed" >&2; exit 1; }
        mv "$stage/linux-${KVER}" "$SRC_TREE"
        rmdir "$stage"
    fi
fi

( cd "$SRC_TREE" && make x86_64_defconfig )

( cd "$SRC_TREE" && deltas | xargs -n2 scripts/config )
( cd "$SRC_TREE" && make olddefconfig )

missing=()
for c in "${required[@]}"; do
    grep -qx "CONFIG_$c=y" "$SRC_TREE/.config" || missing+=("$c")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "build_kernel: refusing -- olddefconfig dropped: ${missing[*]}" >&2
    echo "  linux-${KVER}'s Kconfig no longer agrees with the scripts/config list above --" \
         "update it" >&2
    exit 1
fi

present=()
for c in "${forbidden[@]}"; do
    grep -qx "CONFIG_$c=y" "$SRC_TREE/.config" && present+=("$c")
done
if [ "${#present[@]}" -gt 0 ]; then
    echo "build_kernel: refusing -- olddefconfig set a forbidden option for variant $VARIANT:" \
         "${present[*]}" >&2
    exit 1
fi

echo "build_kernel: building bzImage -j$(nproc)"
( cd "$SRC_TREE" && make -j"$(nproc)" bzImage )

cp "$SRC_TREE/arch/x86/boot/bzImage" "$OUT_DIR/vmlinuz"
release="$(cd "$SRC_TREE" && make -s kernelrelease)"
echo "build_kernel: done -- $OUT_DIR/vmlinuz ($release)"
