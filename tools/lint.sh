#!/usr/bin/env bash
# lint the tree
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
py=() sh=()
while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue
    case "$f" in
        *.py) py+=("$f") ;;
        *.sh|*.bash) sh+=("$f") ;;
        *) case "${f##*/}" in
               *.*) ;;
               *) head -1 "$f" | grep -qE '^#!.*[/ ](ba)?sh([[:space:]]|$)' && sh+=("$f") ;;
           esac ;;
    esac
done < <(git ls-files -z)
rc=0
[ "${#py[@]}" = 0 ] || pyflakes "${py[@]}" || rc=1
[ "${#sh[@]}" = 0 ] || shellcheck -S warning "${sh[@]}" || rc=1
exit "$rc"
