#!/usr/bin/env bash
set -uo pipefail
[ $# = 3 ] || { echo "usage: benchmark/vm/sync.sh <bundle> <sha> <dir>" >&2; exit 2; }
BUNDLE="$1" SHA="$2" DIR="$3"
if [ -d "$DIR/.git" ]; then
    dirty="$(git -C "$DIR" status --porcelain --untracked-files=no)"
    if [ -n "$dirty" ]; then
        echo "vm_sync: refusing -- $DIR dirty" >&2
        echo "$dirty" >&2; exit 4
    fi
    git -C "$DIR" fetch -q "$BUNDLE" HEAD || { echo "vm_sync: fetch failed" >&2; exit 1; }
else
    git clone -q "$BUNDLE" "$DIR" 2>/dev/null || { echo "vm_sync: clone failed" >&2; exit 1; }
fi
git -C "$DIR" -c advice.detachedHead=false checkout -q --detach "$SHA" || { echo "vm_sync: checkout $SHA failed" >&2; exit 1; }
[ "$(git -C "$DIR" rev-parse HEAD)" = "$SHA" ] || { echo "vm_sync: HEAD != $SHA" >&2; exit 1; }
echo "vm_sync: $DIR at $SHA"
