#!/usr/bin/env bash
# build the static L2 initramfs
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ROOT="$PWD"
OUT="$ROOT/build/l2"
IMAGESTREAMIO="$ROOT/third_party/ImageStreamIO"

[ -f "$IMAGESTREAMIO/ImageStreamIO.c" ] || { echo "make_image: run tools/fetch/dependencies.sh first" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT"/{bin,proc,sys,dev,tmp}

cc -O2 -g -std=gnu11 -static -Wall -Wextra -Wno-unused-parameter \
   -I"$IMAGESTREAMIO" -I"$IMAGESTREAMIO/build" -Isrc \
   -o "$OUT/bin/pipeline" src/pipeline/pipeline.c "$IMAGESTREAMIO/ImageStreamIO.c" \
   -lpthread -lrt -lm

for prog in steal_probe gap_detector; do
    cc -O2 -g -std=gnu11 -static -Wall -Wextra -Wno-unused-parameter \
       -Isrc/guest -o "$OUT/bin/$prog" "src/guest/$prog.c"
done

cc -O2 -g -std=gnu11 -static -Wall -Wextra -Wno-unused-parameter \
   -o "$OUT/bin/drain" "src/guest/drain.c"

if [ -f src/guest/tempo_steal.ko ]; then
    mkdir -p "$OUT/lib"
    cp src/guest/tempo_steal.ko "$OUT/lib/"
fi

busybox="$(command -v busybox)"
file "$busybox" | grep -q 'statically linked' || {
    echo "make_image: $busybox is not static; the initramfs would need a loader" >&2; exit 1; }
cp "$busybox" "$OUT/bin/busybox"

cp src/guest/init "$OUT/init"
chmod +x "$OUT/init" "$OUT/bin/pipeline" "$OUT/bin/busybox" \
         "$OUT/bin/steal_probe" "$OUT/bin/gap_detector" "$OUT/bin/drain"

( cd "$OUT" && find . -print0 | cpio --null -o --format=newc 2>/dev/null | gzip -9 ) \
    > "$ROOT/build/l2-initramfs.cpio.gz"

echo "l2 initramfs: $ROOT/build/l2-initramfs.cpio.gz ($(stat -c %s "$ROOT/build/l2-initramfs.cpio.gz") bytes)"
echo "  pipeline: $(file -b "$OUT/bin/pipeline" | cut -d, -f1-2)"
