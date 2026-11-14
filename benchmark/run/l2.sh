#!/usr/bin/env bash
usage() {
    cat <<'EOF2'
usage: benchmark/run/l2.sh [--label NAME] [--frames N] [--period-ns N] [--slopes N] [--acts N]
           [--sched P] [--prio N] [--vcpus N] [--mem MB] [--pin CPUS] [--kernel PATH]
           [--guest-args S] [--outdir DIR] [--timeout S] [--trigger timer|irq] [--reconstruct M]
           [--tier N] [--modes N] [--alpha X] [--quality N] [--adapt M] [--block N] [--block-ns N]
EOF2
}
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ROOT="$PWD"

LABEL=unlabelled FRAMES=2000 PERIOD=1000000 SLOPES=800 ACTS=200 SCHED=fifo PRIO=80
VCPUS=4 MEM=1024 PIN="" KERNEL="" GUEST_ARGS="" OUTDIR="" TIMEOUT=0
RECONSTRUCT=dense TIER=0 MODES=0 ALPHA=1.0 QUALITY=0 ADAPT=off BLOCK=16 BLOCKNS=0
TRIGGER=timer

while [ $# -gt 0 ]; do
    case "$1" in
        --label) LABEL="$2"; shift 2;;
        --frames) FRAMES="$2"; shift 2;;
        --period-ns) PERIOD="$2"; shift 2;;
        --slopes) SLOPES="$2"; shift 2;;
        --acts) ACTS="$2"; shift 2;;
        --sched) SCHED="$2"; shift 2;;
        --prio) PRIO="$2"; shift 2;;
        --vcpus) VCPUS="$2"; shift 2;;
        --mem) MEM="$2"; shift 2;;
        --pin) PIN="$2"; shift 2;;
        --kernel) KERNEL="$2"; shift 2;;
        --guest-args) GUEST_ARGS="$2"; shift 2;;
        --outdir) OUTDIR="$2"; shift 2;;
        --timeout) TIMEOUT="$2"; shift 2;;
        --reconstruct) RECONSTRUCT="$2"; shift 2;;
        --tier) TIER="$2"; shift 2;;
        --modes) MODES="$2"; shift 2;;
        --alpha) ALPHA="$2"; shift 2;;
        --quality) QUALITY="$2"; shift 2;;
        --adapt) ADAPT="$2"; shift 2;;
        --block) BLOCK="$2"; shift 2;;
        --block-ns) BLOCKNS="$2"; shift 2;;
        --trigger) TRIGGER="$2"; shift 2;;
        -h|--help) usage; exit 0;;
        *) echo "run_l2: unknown argument '$1'" >&2; exit 2;;
    esac
done

INITRAMFS="$ROOT/build/l2-initramfs.cpio.gz"
[ -f "$INITRAMFS" ] || bash tools/build/make_image.sh >/dev/null

image_version() {
    python3 - "$1" <<'PY' 2>/dev/null || echo "- -"
import re, struct, sys
b = open(sys.argv[1], "rb").read(0x10000)
if b[0x202:0x206] != b"HdrS":
    print("- -"); sys.exit()
off = struct.unpack_from("<H", b, 0x20E)[0] + 0x200
s = b[off:off + 256].split(b"\0")[0].decode(errors="replace")
m = re.search(r"(PREEMPT_RT|PREEMPT_DYNAMIC|PREEMPT|VOLUNTARY)", s)
print((s.split() or ["-"])[0], m.group(1) if m else "-")
PY
}

KERNEL_ARM=given KERNEL_SRC="$KERNEL"
if [ -z "$KERNEL" ]; then
    KERNEL_ARM=stock-host
    REL="$(uname -r)"
    CACHE="$ROOT/build/l2-vmlinuz"
    if [ -r "/usr/lib/modules/$REL/vmlinuz" ]; then
        KERNEL="/usr/lib/modules/$REL/vmlinuz"
        KERNEL_SRC="$KERNEL"
    elif [ -f "$CACHE" ] && [ "$(image_version "$CACHE" | cut -d' ' -f1)" = "$REL" ]; then
        KERNEL="$CACHE"
        KERNEL_SRC="/boot/vmlinuz-$REL"
    elif [ -e "/boot/vmlinuz-$REL" ]; then
        KERNEL="$CACHE"
        KERNEL_SRC="/boot/vmlinuz-$REL"
        mkdir -p "$ROOT/build"
        sudo cp "/boot/vmlinuz-$REL" "$KERNEL"
        sudo chmod 644 "$KERNEL"
        sudo chown "$(id -u):$(id -g)" "$KERNEL"
    else
        {
            echo "run_l2: no image $REL; installed:"
            ls /usr/lib/modules 2>/dev/null | sed 's/^/run_l2:   /'
        } >&2
        exit 2
    fi
fi
[ -r "$KERNEL" ] || { echo "run_l2: $KERNEL unreadable" >&2; exit 2; }

OUTDIR="${OUTDIR:-$ROOT/build/l2-runs/$LABEL}"
mkdir -p "$OUTDIR"

read -r IMG_REL IMG_PREEMPT <<<"$(image_version "$KERNEL")"
{
    echo "kernel_arm=$KERNEL_ARM kernel_path=$KERNEL kernel_src=${KERNEL_SRC:-$KERNEL}"
    echo "kernel_image_release=$IMG_REL kernel_image_preempt=$IMG_PREEMPT host_release=$(uname -r)"
    echo "kernel_sha256=$(sha256sum "$KERNEL" | cut -d' ' -f1)"
} > "$OUTDIR/kernel.meta"
DISK="$OUTDIR/results.img"
CONSOLE="$OUTDIR/console.log"

rm -f "$DISK"
truncate -s 64M "$DISK"

APPEND="console=ttyS0 panic=1 quiet"
APPEND="$APPEND tempo.frames=$FRAMES tempo.period=$PERIOD tempo.slopes=$SLOPES tempo.acts=$ACTS"
APPEND="$APPEND tempo.sched=$SCHED tempo.prio=$PRIO tempo.label=$LABEL"
APPEND="$APPEND tempo.reconstruct=$RECONSTRUCT tempo.tier=$TIER tempo.modes=$MODES tempo.alpha=$ALPHA"
APPEND="$APPEND tempo.adapt=$ADAPT tempo.block=$BLOCK tempo.blockns=$BLOCKNS"
[ "$QUALITY" != 0 ] && APPEND="$APPEND tempo.quality=$QUALITY"
[ "$VCPUS" -ge 4 ] && APPEND="$APPEND tempo.cpus=1"
TRIGSOCK="$OUTDIR/irq_trigger.sock"
if [ "$TRIGGER" = irq ]; then
    APPEND="$APPEND tempo.trigger=irq tempo.irqdev=/dev/ttyS1"
fi
[ -n "$GUEST_ARGS" ] && APPEND="$APPEND $GUEST_ARGS"

[ "$TIMEOUT" -eq 0 ] && TIMEOUT=$(( FRAMES * PERIOD / 1000000000 + 180 ))

QEMU=(qemu-system-x86_64
      -accel kvm -cpu host -smp "$VCPUS" -m "$MEM"
      -name "guest=tempo-$LABEL,debug-threads=on"
      -nographic -no-reboot
      -kernel "$KERNEL" -initrd "$INITRAMFS" -append "$APPEND"
      -drive "file=$DISK,format=raw,if=virtio"
      -serial mon:stdio)
[ "$TRIGGER" = irq ] && QEMU+=(
      -chardev "socket,id=irq_trigger,path=$TRIGSOCK,server=on,wait=off"
      -device "isa-serial,chardev=irq_trigger")

echo "run_l2: $LABEL  vcpus=$VCPUS pin=${PIN:-none} frames=$FRAMES sched=$SCHED"
echo "run_l2: kernel $KERNEL_ARM $IMG_REL $IMG_PREEMPT ($KERNEL)"

qemu_cpu_ns() {
    local pid="$1" t
    t=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null) || return 1
    echo $(( t * 10000000 ))     # clock ticks at 100 Hz -> ns
}

if [ -n "$PIN" ]; then
    timeout "$TIMEOUT" taskset -c "$PIN" "${QEMU[@]}" >"$CONSOLE" 2>&1 &
else
    timeout "$TIMEOUT" "${QEMU[@]}" >"$CONSOLE" 2>&1 &
fi
QPID=$!

find_qemu() { ps -eo pid,comm | awk '$2 ~ /qemu/ {print $1; exit}'; }
qcpu_now() {
    local p; p=$(find_qemu); [ -n "$p" ] || { echo 0; return; }
    qemu_cpu_ns "$p" 2>/dev/null || echo 0
}

SAMPLES="$OUTDIR/.cpu_samples"
: > "$SAMPLES"
( while kill -0 "$QPID" 2>/dev/null; do
      ready=0
      if grep -q 'downstream_ready=1' "$CONSOLE" 2>/dev/null; then ready=1; fi
      echo "$ready $(qcpu_now) $(date +%s)" >> "$SAMPLES"
      sleep 0.2
  done ) &
SAMPLER=$!

wait "$QPID" 2>/dev/null || true
kill "$SAMPLER" 2>/dev/null; wait "$SAMPLER" 2>/dev/null || true

QCPU_START=$(awk '$1==1 && $2>0 {print $2; exit}' "$SAMPLES" 2>/dev/null || echo 0)
QCPU_END=$(awk '{if ($2+0 > m) m=$2+0} END{print m+0}' "$SAMPLES" 2>/dev/null || echo 0)
QCPU_START=${QCPU_START:-0}; QCPU_END=${QCPU_END:-0}
cp "$SAMPLES" "$OUTDIR/cpu_samples.txt" 2>/dev/null || true
rm -f "$SAMPLES"
{
  echo "guest_cpu_ns=$QCPU_END"
  echo "guest_cpu_at_ready_ns=$QCPU_START"
  echo "guest_cpu_loop_ns=$(( QCPU_END > QCPU_START ? QCPU_END - QCPU_START : 0 ))"
} > "$OUTDIR/guest_cpu.txt"

head -c 4096 "$DISK" | tr -d '\0' > "$OUTDIR/header.txt"
BYTES="$(sed -n 's/^bytes=//p' "$OUTDIR/header.txt" | head -1)"
BYTES="${BYTES:-0}"
if [ "$BYTES" -gt 0 ]; then
    dd if="$DISK" of="$OUTDIR/telemetry.bin" bs=4096 skip=1 count=$(( (BYTES + 4095) / 4096 )) \
       status=none
    truncate -s "$BYTES" "$OUTDIR/telemetry.bin"
fi

if [ "$BYTES" -gt 0 ]; then
    echo "run_l2: frames=$((BYTES / 72))"
elif grep -q '^mode=quality$' "$OUTDIR/header.txt" 2>/dev/null; then
    echo "run_l2: tiers=$(grep -c '^tier[0-9]_' "$OUTDIR/header.txt")"
    grep '^tier[0-9]_' "$OUTDIR/header.txt" || true
else
    echo "run_l2: NO RECORDS" >&2
    exit 1
fi
