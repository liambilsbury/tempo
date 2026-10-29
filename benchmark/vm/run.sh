#!/usr/bin/env bash
# boot the L1 VM
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. benchmark/host/settings.conf
D=build/vm
BG=(); [ "${1:-}" = "--background" ] && BG=(-daemonize -pidfile "$D/qemu.pid")
exec taskset -c "$VM_HOST_CORES" qemu-system-x86_64 -name tempo-l1 -accel kvm -cpu host -smp "$VM_CPUS" -m "$VM_MEMORY_MB" \
    -drive file="$D/l1.qcow2",if=virtio -drive file="$D/seed.img",if=virtio,format=raw \
    -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:"$VM_SSH_PORT"-:22 \
    -display none -serial file:"$D/console.log" "${BG[@]}"
