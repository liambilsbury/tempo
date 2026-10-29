# record and signal the bpftrace probes by their recorded pids
# shellcheck shell=bash
: "${SUDO=sudo}"
# escape a string as an exact regex
exact_re() { printf '%s' "$1" | sed 's/[][\\.*^$+?(){}|]/\\&/g'; }
# count the processes whose whole cmdline matches RE
count_matching() { pgrep -f -x "$1" 2>/dev/null | wc -l; }
# write the pid matching RE to PIDFILE
record_exact_pid() {
    local pids; pids="$(pgrep -f -x "$1" 2>/dev/null)"
    rm -f "$2"
    [ -n "$pids" ] && [ "$(printf '%s\n' "$pids" | wc -l)" -eq 1 ] || return 1
    echo "$pids" > "$2"
}
# SIGINT the pid in PIDFILE while its cmdline still matches RE
stop_exact() {
    [ -s "$1" ] || { echo "stop_exact: no recorded pid in $1" >&2; return 1; }
    $SUDO pkill -INT -F "$1" -f -x "$2"
}
