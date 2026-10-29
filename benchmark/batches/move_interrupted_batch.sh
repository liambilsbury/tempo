#!/usr/bin/env bash
# move an interrupted batch's files into <name>.partial-<stamp>, leave a finished batch alone

#   bash benchmark/batches/move_interrupted_batch.sh <results dir> <name> <stamp> <n> <label...>
set -uo pipefail
[ $# -ge 5 ] || { sed -n '4p' "$0" | sed 's/^# *//' >&2; exit 2; }
RES="$1" NAME="$2" STAMP="$3" N="$4"; shift 4
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if python3 "$HERE/per_run_checks.py" --attempted "$RES/$NAME.json" "$N"; then
    echo "move_interrupted_batch: $RES/$NAME.json attempted all $N rounds; left in place"; exit 0
fi
MOVE=()
for f in "$RES/$NAME.json" "$RES/$NAME.checks.txt" "$RES/$NAME.score.txt" "$RES/$NAME.selection"; do [ -e "$f" ] && MOVE+=("$f"); done
for l in "$@"; do for d in "$RES/$l"-r[0-9]*; do [ -d "$d" ] && MOVE+=("$d"); done; done
[ "${#MOVE[@]}" -gt 0 ] || { echo "move_interrupted_batch: nothing of $NAME to move"; exit 0; }
DIR="$RES/$NAME.partial-$STAMP"
mkdir -p "$DIR" || exit 1
for f in "${MOVE[@]}"; do mv -- "$f" "$DIR/" && echo "move_interrupted_batch: $f -> $DIR/" || exit 1; done
echo "move_interrupted_batch: $NAME moved to $DIR"
