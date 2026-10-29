#!/usr/bin/env bash
# check that the probe helpers signal the recorded pid and no other
set -uo pipefail
set -m
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
# shellcheck disable=SC2034
SUDO=""; . benchmark/probes/pid.sh
T="$(mktemp -d)"; fail=0
CMD="fakeprobe benchmark/probes/ipi_origin.bt -o $T/l1-events.txt 60"
RE="$(exact_re "$CMD")"
( exec python3 benchmark/probes/named_sleep.py "${CMD% 60}" 60 ) & A=$!
( exec python3 benchmark/probes/named_sleep.py "fakeprobe benchmark/probes/ipi_origin.bt -o $T/other/l1-events.txt" 60 ) & B=$!
sleep 0.3
record_exact_pid "$RE" "$T/pid" || { echo "FAIL record"; fail=1; }
[ "$(cat "$T/pid" 2>/dev/null)" = "$A" ]              || { echo "FAIL recorded wrong pid"; fail=1; }
[ "$(count_matching "$RE")" = 1 ]                      || { echo "FAIL count"; fail=1; }
stop_exact "$T/pid" "$RE"                               || { echo "FAIL stop"; fail=1; }
sleep 0.3
kill -0 "$A" 2>/dev/null                                && { echo "FAIL ours still running"; fail=1; }
kill -0 "$B" 2>/dev/null                                || { echo "FAIL sibling was signalled"; fail=1; }
echo "$B" > "$T/pid"
stop_exact "$T/pid" "$RE"                               && { echo "FAIL stopped a non-matching pid"; fail=1; }
kill -0 "$B" 2>/dev/null                                || { echo "FAIL sibling killed by a leftover pidfile"; fail=1; }
kill "$B" 2>/dev/null; rm -rf "$T"
T2="$(mktemp -d)"
CMD2="fakeprobe benchmark/probes/ipi_origin.bt -o $T2/l1-events.txt 60"
RE2="$(exact_re "$CMD2")"
( exec python3 benchmark/probes/named_sleep.py "${CMD2% 60}" 60 ) & E=$!
sleep 0.3
record_exact_pid "$RE2" "$T2/pid" || { echo "FAIL record2"; fail=1; }
( exec python3 benchmark/probes/named_sleep.py "${CMD2% 60}" 60 ) & F=$!
sleep 0.3
[ "$(count_matching "$RE2")" = 2 ]                      || { echo "FAIL duplicate did not appear"; fail=1; }
stop_exact "$T2/pid" "$RE2"                             || { echo "FAIL stop2"; fail=1; }
sleep 0.3
kill -0 "$E" 2>/dev/null                                && { echo "FAIL recorded pid survived its own stop"; fail=1; }
kill -0 "$F" 2>/dev/null                                || { echo "FAIL a sibling matching the pattern was also signalled"; fail=1; }
kill "$F" 2>/dev/null; rm -rf "$T2"
T3="$(mktemp -d)"; mkdir -p "$T3/bin"
REAL_PKILL="$(command -v pkill)"
cat > "$T3/bin/pkill" <<EOF
#!/bin/sh
for a; do case "\$a" in --pid|--pidf*) echo "pkill: pidfile not valid" >&2; exit 2;; esac; done
exec "$REAL_PKILL" "\$@"
EOF
chmod +x "$T3/bin/pkill"
CMD3="fakeprobe benchmark/probes/ipi_origin.bt -o $T3/l1-events.txt 60"
RE3="$(exact_re "$CMD3")"
( exec python3 benchmark/probes/named_sleep.py "${CMD3% 60}" 60 ) & G=$!
sleep 0.3
record_exact_pid "$RE3" "$T3/pid" || { echo "FAIL record3"; fail=1; }
PATH="$T3/bin:$PATH" stop_exact "$T3/pid" "$RE3" 2>"$T3/err" || { echo "FAIL stop under procps 4.0.4's options: $(cat "$T3/err")"; fail=1; }
sleep 0.3
kill -0 "$G" 2>/dev/null && { echo "FAIL recorded pid survived a stop under procps 4.0.4's options"; fail=1; }
kill "$G" 2>/dev/null
echo notapid > "$T3/badpid"
stop_exact "$T3/badpid" "$RE3" 2>"$T3/err2" && { echo "FAIL a bad pid file was accepted"; fail=1; }
[ -s "$T3/err2" ] || { echo "FAIL stop_exact hid its error"; fail=1; }
rm -rf "$T3"
[ "$fail" = 0 ] && echo "probe pid: ok"
exit "$fail"
