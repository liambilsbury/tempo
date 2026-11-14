#!/usr/bin/env bash
set -uo pipefail
[ $# -ge 5 ] || { echo "usage: benchmark/batches/move_interrupted_batch.sh <results> <name> <stamp> <n> <label...>" >&2; exit 2; }
RES="$1" NAME="$2" STAMP="$3" N="$4"; shift 4
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if python3 "$HERE/per_run_checks.py" --attempted "$RES/$NAME.json" "$N"; then
    exit 0
fi
MOVE=()
for f in "$RES/$NAME.json" "$RES/$NAME.checks.txt" "$RES/$NAME.score.txt" "$RES/$NAME.selection"; do [ -e "$f" ] && MOVE+=("$f"); done
for l in "$@"; do for d in "$RES/$l"-r[0-9]*; do [ -d "$d" ] && MOVE+=("$d"); done; done
[ "${#MOVE[@]}" -gt 0 ] || { exit 0; }
DIR="$RES/$NAME.partial-$STAMP"
mkdir -p "$DIR" || exit 1
for f in "${MOVE[@]}"; do mv -- "$f" "$DIR/" || exit 1; done
echo "move_interrupted_batch: -> $DIR"
