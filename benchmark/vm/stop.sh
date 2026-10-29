#!/usr/bin/env bash
# stop the L1 VM at the recorded pid
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
PIDFILE="${1:-build/vm/qemu.pid}"; WAIT_S="${2:-120}"
PAT='-name tempo-l1'
# check that the recorded pid is still the L1 VM
is_l1() { [ -n "${PID:-}" ] && [ -r "/proc/$PID/cmdline" ] && tr '\0' ' ' < "/proc/$PID/cmdline" | grep -qF -- "$PAT"; }
PID="$(tr -dc '0-9' < "$PIDFILE" 2>/dev/null)"
if [ -z "$PID" ]; then echo "stop_vm: no recorded pid in $PIDFILE; nothing to stop"; exit 0; fi
for ((i = 0; i < WAIT_S; i++)); do is_l1 || { echo "stop_vm: pid $PID has exited"; exit 0; }; sleep 1; done
for sig in TERM KILL; do
    is_l1 || { echo "stop_vm: pid $PID has exited"; exit 0; }
    echo "stop_vm: pid $PID still the L1 VM after ${WAIT_S}s; sending SIG$sig"
    pkill "-$sig" -F "$PIDFILE" -f -- "$PAT"
    for ((i = 0; i < 20; i++)); do is_l1 || break; sleep 0.5; done
done
is_l1 && { echo "stop_vm: pid $PID survived SIGKILL" >&2; exit 1; }
echo "stop_vm: pid $PID stopped"; exit 0
