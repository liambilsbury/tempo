#!/usr/bin/env bash
# fetch Perfetto's trace_processor_shell into third_party/perfetto
set -euo pipefail
PERFETTO_TAG="v58.2"
PERFETTO_ZIP_SHA256="32c739f71b2d39721afd294c0b038f2499a44a71b5b6bcdd85b83faca0b240b9"
IN_ZIP="linux-amd64/trace_processor_shell"

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
DEST="$PWD/third_party/perfetto"
BIN="$DEST/trace_processor_shell"
if [ -x "$BIN" ] && [ "$(cat "$DEST/tag" 2>/dev/null)" = "$PERFETTO_TAG" ]; then
    echo "trace_processor: $PERFETTO_TAG already present: $BIN"; exit 0
fi
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fsSL -o "$TMP/p.zip" "https://github.com/google/perfetto/releases/download/$PERFETTO_TAG/linux-amd64.zip"
echo "$PERFETTO_ZIP_SHA256  $TMP/p.zip" | sha256sum -c --quiet - \
    || { echo "trace_processor: the zip does not match $PERFETTO_ZIP_SHA256" >&2; exit 1; }
mkdir -p "$DEST"
unzip -q -o -j "$TMP/p.zip" "$IN_ZIP" -d "$DEST"
chmod 755 "$BIN"
echo "$PERFETTO_TAG" > "$DEST/tag"
echo "trace_processor: $PERFETTO_TAG at $BIN"
