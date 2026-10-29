#!/usr/bin/env bash
# make the Ubuntu 24.04 L1 VM disk and its cloud-init seed
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
IMG_URL="https://cloud-images.ubuntu.com/releases/noble/release-20260911/ubuntu-24.04-server-cloudimg-amd64.img"
IMG_SHA="612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354"
D=build/vm; mkdir -p "$D"
FORCE=0; [ "${1:-}" = "--force" ] && FORCE=1
if [ -f "$D/l1.qcow2" ] && [ "$FORCE" -ne 1 ]; then
    echo "make_vm: $D/l1.qcow2 already exists; pass --force to rebuild it"
    exit 0
fi
command -v qemu-img >/dev/null || { echo "make_vm: qemu-img missing: sudo pacman -S qemu-img" >&2; exit 3; }
[ -f "$D/base.img" ] || curl -fL -o "$D/base.img" "$IMG_URL"
echo "$IMG_SHA  $D/base.img" | sha256sum -c - >/dev/null || { echo "make_vm: base image sha mismatch" >&2; exit 1; }
qemu-img create -q -f qcow2 -F qcow2 -b base.img "$D/l1.qcow2" 40G
rm -f "$D/known_hosts"
sed "s|__SSH_PUBKEY__|$(cat ~/.ssh/id_ed25519.pub)|" benchmark/vm/user-data > "$D/user-data"
printf 'instance-id: tempo-l1\nlocal-hostname: tempo-l1\n' > "$D/meta-data"
rm -f "$D/seed.img"; mkfs.vfat -n CIDATA -C "$D/seed.img" 2048 >/dev/null
mcopy -i "$D/seed.img" "$D/user-data" "$D/meta-data" ::
echo "make_vm: $D/l1.qcow2 ready"
