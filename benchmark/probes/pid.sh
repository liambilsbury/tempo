# shellcheck shell=bash
: "${SUDO=sudo}"
exact_re() { printf '%s' "$1" | sed 's/[][\\.*^$+?(){}|]/\\&/g'; }
count_matching() { pgrep -f -x "$1" 2>/dev/null | wc -l; }
record_exact_pid() {
    local pids; pids="$(pgrep -f -x "$1" 2>/dev/null)"
    rm -f "$2"
    [ -n "$pids" ] && [ "$(printf '%s\n' "$pids" | wc -l)" -eq 1 ] || return 1
    echo "$pids" > "$2"
}
stop_exact() {
    [ -s "$1" ] || { echo "stop_exact: no pid in $1" >&2; return 1; }
    $SUDO pkill -INT -F "$1" -f -x "$2"
}
