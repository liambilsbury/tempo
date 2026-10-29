#!/usr/bin/env bash
# fetch and build ImageStreamIO
set -euo pipefail

IMAGESTREAMIO_URL="https://github.com/milk-org/ImageStreamIO.git"
IMAGESTREAMIO_SHA="47c1b261a57a35041a67960534754dbe5962d7d0"

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
THIRD="$PWD/third_party"

[ "${1:-}" = "--clean" ] && { rm -rf "$THIRD"; echo "removed $THIRD"; exit 0; }

mkdir -p "$THIRD"
if [ ! -d "$THIRD/ImageStreamIO/.git" ]; then
    git clone "$IMAGESTREAMIO_URL" "$THIRD/ImageStreamIO"
fi
if git -C "$THIRD/ImageStreamIO" cat-file -e "$IMAGESTREAMIO_SHA^{commit}" 2>/dev/null; then
    echo "dependencies: ImageStreamIO $IMAGESTREAMIO_SHA already present"
else
    git -C "$THIRD/ImageStreamIO" fetch --quiet origin
fi
git -C "$THIRD/ImageStreamIO" checkout --quiet "$IMAGESTREAMIO_SHA"

have="$(git -C "$THIRD/ImageStreamIO" rev-parse HEAD)"
[ "$have" = "$IMAGESTREAMIO_SHA" ] || { echo "dependencies: ImageStreamIO is at $have, expected $IMAGESTREAMIO_SHA" >&2; exit 1; }

cmake -S "$THIRD/ImageStreamIO" -B "$THIRD/ImageStreamIO/build" -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build "$THIRD/ImageStreamIO/build" -j"$(nproc)" >/dev/null

lib="$THIRD/ImageStreamIO/build/libImageStreamIO.so"
[ -f "$lib" ] || { echo "dependencies: $lib was not produced" >&2; exit 1; }
echo "ImageStreamIO $IMAGESTREAMIO_SHA built: $lib"
ldd "$lib" | sed 's/^/  /'
