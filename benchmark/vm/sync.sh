#!/usr/bin/env bash
# check out a commit from a git bundle in the L1 VM's repo

#   bash sync.sh <bundle> <sha> <repo dir>
set -uo pipefail
[ $# = 3 ] || { sed -n '4p' "$0" | sed 's/^# *//' >&2; exit 2; }
BUNDLE="$1" SHA="$2" DIR="$3"
if [ -d "$DIR/.git" ]; then
    dirty="$(git -C "$DIR" status --porcelain --untracked-files=no)"
    if [ -n "$dirty" ]; then
        echo "vm_sync: refusing -- $DIR has tracked edits:" >&2
        echo "$dirty" >&2; exit 4
    fi
    git -C "$DIR" fetch -q "$BUNDLE" HEAD || { echo "vm_sync: fetch from $BUNDLE failed" >&2; exit 1; }
else
    git clone -q "$BUNDLE" "$DIR" 2>/dev/null || { echo "vm_sync: clone of $BUNDLE into $DIR failed" >&2; exit 1; }
fi
git -C "$DIR" -c advice.detachedHead=false checkout -q --detach "$SHA" || { echo "vm_sync: checkout $SHA failed" >&2; exit 1; }
[ "$(git -C "$DIR" rev-parse HEAD)" = "$SHA" ] || { echo "vm_sync: HEAD is not $SHA" >&2; exit 1; }
echo "vm_sync: $DIR at $SHA"
