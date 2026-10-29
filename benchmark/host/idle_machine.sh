#!/usr/bin/env bash
# idle the host for a batch run, then restore what it changed
#   sudo bash benchmark/host/idle_machine.sh start <idle dir> <run pidfile> [--pins L] [--vm-cores L] [--summary F] [--log F]
#   sudo bash benchmark/host/idle_machine.sh restore [<idle dir>/system.state]
#   bash benchmark/host/idle_machine.sh plan [--pins L] [--vm-cores L]
set -uo pipefail
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SYS="${IDLE_SYS:-/sys}"; PROC="${IDLE_PROC:-/proc}"
read -ra SC <<<"${IDLE_SYSTEMCTL:-systemctl}"
POLL="${IDLE_POLL_S:-5}"; PID_WAIT="${IDLE_PID_WAIT_S:-900}"; APPLY_WAIT="${IDLE_APPLY_WAIT_S:-600}"
CPU="$SYS/devices/system/cpu"
ROOT="$(cd "$(dirname "$SELF")/../.." && pwd)"
RESD="${IDLE_RESULTS:-$ROOT/results/experiments}"
# print the old name benchmark/renamed.tsv gives for a new name
renamed_old() { awk -F'\t' -v n="$1" '$2 == n { print $1 }' "$ROOT/benchmark/renamed.tsv" 2>/dev/null; }
OLD_RUN="$(renamed_old 'run_batches-<stamp>')"; OLD_RUN="${OLD_RUN%<stamp>}"
# shellcheck disable=SC2207
ONE_ROUND_PFXS=(one-round- $(renamed_old 'one-round-*' | sed 's/[*]$//'))

# print the usage lines from this file's header and exit 2
usage() { grep '^#   .*benchmark/host/idle_machine.sh' "$SELF" | sed 's/^# *//' >&2; exit 2; }
CMD="${1:-}"; [ $# -gt 0 ] && shift
# read a CPU list for a key from benchmark/host/settings.conf
host_conf() { sed -n "s/^$1=\([0-9,-]*\)\$/\1/p" "$ROOT/benchmark/host/settings.conf" 2>/dev/null | tail -1; }
POS=(); PINS="$(host_conf HOST_PINS)"; VMC="$(host_conf VM_HOST_CORES)"; SUMMARY=""; LOGF=""
while [ $# -gt 0 ]; do
    case "$1" in
        --pins) [ $# -ge 2 ] || usage; PINS="$2"; shift 2;;
        --vm-cores) [ $# -ge 2 ] || usage; VMC="$2"; shift 2;;
        --summary) [ $# -ge 2 ] || usage; SUMMARY="$2"; shift 2;;
        --log) [ $# -ge 2 ] || usage; LOGF="$2"; shift 2;;
        -*) usage;;
        *) POS+=("$1"); shift;;
    esac
done
[[ "$PINS" =~ ^[0-9,-]+$ && "$VMC" =~ ^[0-9,-]+$ ]] || usage

# expand a CPU list like 0-3,16 into single numbers
expand() {
    local p out=() i; local -a parts; IFS=, read -ra parts <<<"$1"
    for p in "${parts[@]}"; do
        [ -z "$p" ] && continue
        if [[ "$p" == *-* ]]; then for ((i = ${p%-*}; i <= ${p#*-}; i++)); do out+=("$i"); done
        else out+=("$p"); fi
    done
    echo "${out[*]}"
}
# test whether a space-separated list holds an item
has() { [[ " $1 " == *" $2 "* ]]; }
# join the arguments with commas
csv() { local IFS=,; echo "$*"; }
# print the arguments as {a,b}, or an argument bare
braces() { if [ $# -gt 1 ]; then echo "{$(csv "$@")}"; else echo "$1"; fi; }
# compress CPU numbers into ranges like 0-3,16
ranges() { printf '%s\n' "$@" | sort -n | awk 'NR == 1 { a = b = $1; next } $1 == b + 1 { b = $1; next }
    { printf "%s%s,", a, (a == b ? "" : "-" b); a = b = $1 } END { if (NR) printf "%s%s", a, (a == b ? "" : "-" b) }'; }
# count the input lines as "a x3, b x1"
tally() { sort | uniq -c | awk '{printf "%s%s x%d", (NR > 1 ? ", " : ""), $2, $1}'; }
# run systemctl with a timeout
sc() { timeout -k 5 180 "${SC[@]}" "$@"; }
# test whether a unit is active
active() { [ "$(sc is-active "$1" 2>/dev/null)" = active ]; }
# write a value to a path quietly
put() { { printf '%s\n' "$2" > "$1"; } 2>/dev/null; }
# print the current time
now() { date '+%F %T %z'; }
# test whether any of sysfs, procfs or systemctl is the real one
real_host() { [ "$SYS" = /sys ] || [ "$PROC" = /proc ] || [ "${SC[*]}" = systemctl ]; }
# give files back to the sudo user
own() { [ -n "${SUDO_UID:-}" ] && chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$@" 2>/dev/null; return 0; }

# read what apply would change
survey() {
    local c s p
    mapfile -t TIMERS < <(sc list-timers --no-legend --plain 2>/dev/null \
        | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /\.timer$/) { print $i; break } }')
    IRQB=""; active irqbalance.service && IRQB=irqbalance.service
    SNAPD=""; active snapperd.service && SNAPD=snapperd.service
    DM="$(sc show -p Id --value display-manager.service 2>/dev/null)"
    [ -n "$DM" ] && ! active "$DM" && DM=""
    ONLINE="$(expand "$(cat "$CPU/online" 2>/dev/null)")"
    GOVC=(); EPPC=(); GOVV=(); EPPV=()
    for c in $ONLINE; do
        [ -r "$CPU/cpu$c/cpufreq/scaling_governor" ] && { GOVC+=("$c"); GOVV+=("$(cat "$CPU/cpu$c/cpufreq/scaling_governor")"); }
        [ -r "$CPU/cpu$c/cpufreq/energy_performance_preference" ] && { EPPC+=("$c"); EPPV+=("$(cat "$CPU/cpu$c/cpufreq/energy_performance_preference")"); }
    done
    SIBS=(); SKIPPED=()
    for p in $(expand "$PINS"); do
        for s in $(expand "$(cat "$CPU/cpu$p/topology/thread_siblings_list" 2>/dev/null)"); do
            [ "$s" = "$p" ] && continue
            if [ "$s" = 0 ] || has "$(expand "$PINS")" "$s" || has "$(expand "$VMC")" "$s"; then SKIPPED+=("$s"); continue; fi
            has "$ONLINE" "$s" && ! has "${SIBS[*]}" "$s" && [ -e "$CPU/cpu$s/online" ] && SIBS+=("$s")
        done
    done
    local vm="" h=()
    for c in $(expand "$VMC"); do vm+=" $c $(expand "$(cat "$CPU/cpu$c/topology/thread_siblings_list" 2>/dev/null)")"; done
    for c in $ONLINE; do has "${SIBS[*]}" "$c" || has "$vm" "$c" || h+=("$c"); done
    HK="$(csv "${h[@]}")"
    IRQS=(); IRQV=()
    local d
    for d in "$PROC"/irq/[0-9]*; do
        [ -f "$d/smp_affinity_list" ] || continue
        IRQS+=("${d##*/}"); IRQV+=("$(cat "$d/smp_affinity_list")")
    done
}

plan() {
    survey
    echo "the root helper would, in order:"
    if [ "${#TIMERS[@]}" -gt 0 ]; then echo "+ systemctl stop ${TIMERS[*]}"
    else echo "  no active system timers"; fi
    if [ -n "$IRQB" ]; then echo "+ systemctl stop irqbalance.service"; else echo "  irqbalance.service is not active"; fi
    if [ -n "$SNAPD" ]; then echo "+ systemctl stop snapperd.service"; else echo "  (snapperd.service is not active)"; fi
    [ "${#GOVC[@]}" -gt 0 ] && echo "+ echo performance > $CPU/cpuN/cpufreq/scaling_governor for N in $(ranges "${GOVC[@]}")"
    [ "${#EPPC[@]}" -gt 0 ] && echo "+ echo performance > $CPU/cpuN/cpufreq/energy_performance_preference for N in $(ranges "${EPPC[@]}")"
    if [ "${#SIBS[@]}" -gt 0 ]; then echo "+ echo 0 > $CPU/cpu$(braces "${SIBS[@]}")/online"
    else echo "  no SMT sibling of pins $PINS to offline"; fi
    [ "${#SKIPPED[@]}" -gt 0 ] && echo "  kept online: ${SKIPPED[*]}"
    if [ -n "$HK" ] && [ "${#IRQS[@]}" -gt 0 ]; then
        echo "+ echo $HK > $PROC/irq/N/smp_affinity_list for N in $(ranges "${IRQS[@]}")"
    fi
    if [ -n "$DM" ]; then echo "+ systemctl stop $DM   -- ENDS THE GRAPHICAL SESSION"
    else echo "  no active display manager"; fi
    echo "restore, when the run exits or the helper is signalled:"
    [ "${#SIBS[@]}" -gt 0 ] && echo "+ echo 1 > $CPU/cpu$(braces "${SIBS[@]}")/online"
    [ "${#IRQS[@]}" -gt 0 ] && echo "+ each $PROC/irq/N/smp_affinity_list back to its recorded value"
    [ "${#GOVC[@]}" -gt 0 ] && echo "+ each CPU's scaling_governor, then its energy_performance_preference, back to its recorded value"
    # shellcheck disable=SC2206
    local up=($IRQB $SNAPD "${TIMERS[@]}"); [ "${#up[@]}" -gt 0 ] && echo "+ systemctl start ${up[*]}"
    echo "+ systemctl --user -M <you>@ start <the user units the launcher stopped>"
    [ -n "$DM" ] && echo "+ systemctl start $DM"
    echo "  by hand: sudo bash benchmark/host/idle_machine.sh restore <summary dir>/idle/system.state, or reboot"
}

QD=""; STATE=""
# append a tab-separated row to the state file
rec() { local IFS=$'\t'; echo "$*" >> "$STATE"; }
# record and apply the idle state
apply() {
    survey
    local i c t v ok=() failed=() moved=0 unmov=0 same=0
    { echo "# idle state v1, applied $(now) by pid $$"; echo "# pins=$PINS vm-cores=$VMC housekeeping=$HK"
      echo "# boot_id=$(cat "$PROC/sys/kernel/random/boot_id" 2>/dev/null || echo unknown)"; } > "$STATE"
    own "$STATE"
    for t in "${TIMERS[@]}"; do rec timer "$t"; sc stop "$t" >/dev/null 2>&1 && ok+=("$t") || { rec failed timer "$t"; failed+=("stop $t"); }; done
    if [ -n "$IRQB" ]; then rec unit "$IRQB"; sc stop "$IRQB" >/dev/null 2>&1 || { rec failed unit "$IRQB"; failed+=("stop $IRQB"); }; fi
    if [ -n "$SNAPD" ]; then rec unit "$SNAPD"; sc stop "$SNAPD" >/dev/null 2>&1 || { rec failed unit "$SNAPD"; failed+=("stop $SNAPD"); }; fi
    for i in "${!GOVC[@]}"; do
        c="${GOVC[$i]}"; rec gov "$c" "${GOVV[$i]}"
        put "$CPU/cpu$c/cpufreq/scaling_governor" performance || { rec failed gov "$c"; failed+=("governor cpu$c"); }
    done
    local eppfail=0
    for i in "${!EPPC[@]}"; do
        c="${EPPC[$i]}"; rec epp "$c" "${EPPV[$i]}"
        v="$(cat "$CPU/cpu$c/cpufreq/energy_performance_preference" 2>/dev/null)"
        [ "$v" = performance ] || put "$CPU/cpu$c/cpufreq/energy_performance_preference" performance || { rec failed epp "$c"; eppfail=$((eppfail + 1)); }
    done
    [ "$eppfail" -gt 0 ] && failed+=("EPP on $eppfail CPU(s)")
    local off=()
    for c in "${SIBS[@]}"; do
        rec online "$c" "$(cat "$CPU/cpu$c/online")"
        put "$CPU/cpu$c/online" 0 && off+=("$c") || { rec failed online "$c"; failed+=("offline cpu$c"); }
    done
    if [ -z "$HK" ]; then failed+=("IRQ steering: no housekeeping CPU")
    else
        for i in "${!IRQS[@]}"; do
            [ "${IRQV[$i]}" = "$HK" ] && { same=$((same + 1)); continue; }
            rec irq "${IRQS[$i]}" "${IRQV[$i]}"
            if put "$PROC/irq/${IRQS[$i]}/smp_affinity_list" "$HK"; then moved=$((moved + 1))
            else rec failed irq "${IRQS[$i]}"; unmov=$((unmov + 1)); fi
        done
    fi
    local dmok=1
    if [ -n "$DM" ]; then
        rec dm "$DM"
        sc stop "$DM" >/dev/null 2>&1; active "$DM" && { rec failed dm "$DM"; dmok=0; }
    fi
    {
        echo "host idling applied $(now) by root helper pid $$:"
        echo "  system timers stopped (${#ok[@]}): ${ok[*]:-none}"
        echo "  irqbalance: $([ -n "$IRQB" ] && echo stopped || echo "not active")"
        echo "  snapperd: $([ -n "$SNAPD" ] && echo stopped || echo "not active")"
        echo "  scaling_governor -> performance on ${#GOVC[@]} CPU(s), was: $(printf '%s\n' "${GOVV[@]}" | tally)"
        echo "  energy_performance_preference -> performance on ${#EPPC[@]} CPU(s), was: $(printf '%s\n' "${EPPV[@]}" | tally)"
        echo "  SMT siblings of pins $PINS offline: ${off[*]:-none}$([ "${#SKIPPED[@]}" -gt 0 ] && echo "; kept online: ${SKIPPED[*]}")"
        echo "  device IRQs -> housekeeping CPUs $HK: $moved moved, $unmov unmovable, $same already there"
        if [ -z "$DM" ]; then echo "  display manager: none active"
        elif [ "$dmok" = 1 ]; then echo "  display manager $DM stopped"
        else echo "  display manager $DM did NOT stop; restoring everything, launch refused"; fi
        [ "${#failed[@]}" -gt 0 ] && echo "  failed: $(csv "${failed[@]}" | sed 's/,/, /g')"
        echo "  state: $STATE"
    } > "$QD/apply.txt"
    own "$QD/apply.txt"
    [ "$dmok" = 1 ]
}

# restore the values in a state file
restore() {
    local st="$1" kind a b bad=() n=0 c
    local -A F=()
    if [ ! -r "$st" ]; then
        local msg
        msg="host restore ($(now), root helper pid $$${WHY:+, $WHY}): FAILED -- cannot read $st, nothing put back; reboot to restore"
        echo "$msg"
        if [ -n "$QD" ]; then echo "$msg" >> "$QD/restore.txt"; own "$QD/restore.txt"; fi
        return 1
    fi
    while IFS=$'\t' read -r kind a b; do [ "$kind" = failed ] && F["$a $b"]=1; done < "$st"
    rows() { awk -F'\t' -v k="$1" '$1 == k' "$st"; }
    while IFS=$'\t' read -r kind a b; do
        [ -n "${F[online $a]:-}" ] && continue
        put "$CPU/cpu$a/online" "$b"; [ "$(cat "$CPU/cpu$a/online" 2>/dev/null)" = "$b" ] && n=$((n + 1)) || bad+=("cpu$a online=$b")
    done < <(rows online)
    while IFS=$'\t' read -r kind a b; do
        [ -n "${F[irq $a]:-}" ] && continue
        [ -e "$PROC/irq/$a" ] || continue
        put "$PROC/irq/$a/smp_affinity_list" "$b"
        [ "$(cat "$PROC/irq/$a/smp_affinity_list" 2>/dev/null)" = "$b" ] && n=$((n + 1)) || bad+=("irq $a affinity=$b")
    done < <(rows irq)
    while IFS=$'\t' read -r kind a b; do
        put "$CPU/cpu$a/cpufreq/scaling_governor" "$b"
        [ "$(cat "$CPU/cpu$a/cpufreq/scaling_governor" 2>/dev/null)" = "$b" ] && n=$((n + 1)) || bad+=("cpu$a governor=$b")
    done < <(rows gov)
    while IFS=$'\t' read -r kind a b; do
        put "$CPU/cpu$a/cpufreq/energy_performance_preference" "$b"
        [ "$(cat "$CPU/cpu$a/cpufreq/energy_performance_preference" 2>/dev/null)" = "$b" ] && n=$((n + 1)) || bad+=("cpu$a epp=$b")
    done < <(rows epp)
    while IFS=$'\t' read -r kind a b; do
        sc start "$a" >/dev/null 2>&1; active "$a" && n=$((n + 1)) || bad+=("start $a")
    done < <(awk -F'\t' '$1 == "unit" || $1 == "timer"' "$st")
    local us="${st%/*}/user.state" who
    if [ -r "$us" ]; then
        who="$(awk -F'\t' '$1 == "user" { print $2; exit }' "$us")"
        declare -A UF=()
        while IFS=$'\t' read -r kind a b; do [ "$kind" = failed ] && UF["$b"]=1; done < "$us"
        while IFS=$'\t' read -r kind a b; do
            [ -n "${UF[$a]:-}" ] && continue
            sc --user -M "$who@" start "$a" >/dev/null 2>&1
            [ "$(sc --user -M "$who@" is-active "$a" 2>/dev/null)" = active ] && n=$((n + 1)) || bad+=("start $a as user $who")
        done < <(awk -F'\t' '$1 == "uunit"' "$us")
    fi
    while IFS=$'\t' read -r kind a b; do
        sc start "$a" >/dev/null 2>&1; active "$a" && n=$((n + 1)) || bad+=("start $a")
    done < <(rows dm)
    local out
    if [ "${#bad[@]}" = 0 ]; then
        out="host restore ($(now), root helper pid $$${WHY:+, $WHY}): OK -- all $n recorded change(s) put back from $st"
    else
        out="host restore ($(now), root helper pid $$${WHY:+, $WHY}): FAILED -- $n put back, NOT put back: $(csv "${bad[@]}" | sed 's/,/, /g'); run sudo bash benchmark/host/idle_machine.sh restore $st, or reboot"
    fi
    echo "$out"
    if [ -n "$QD" ]; then echo "$out" >> "$QD/restore.txt"; own "$QD/restore.txt"; fi
    [ "${#bad[@]}" = 0 ]
}

WHY=""; RESTORED=0
# restore once and exit
finish() {
    trap '' TERM INT HUP
    [ "$RESTORED" = 1 ] && exit 0
    RESTORED=1; WHY="$1"
    local out rc; out="$(restore "$STATE")"; rc=$?
    for f in "$SUMMARY" "$LOGF"; do [ -n "$f" ] && { { echo; echo "$out"; } >> "$f"; own "$f"; }; done
    exit "$rc"
}
# test whether a pid is still the recorded run_all.sh process
run_alive() {
    [ -d "/proc/$1" ] && [[ "$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)" == *benchmark/batches/run_all.sh*--foreground* ]]
}
# sleep a poll interval so a trap can run
nap() { sleep "$POLL" & wait $!; }
# restore once the run exits
watch_run() {
    local pf="$1" t0=$SECONDS pid=""
    while [ -z "$pid" ]; do
        [ -e "$QD/abort" ] && finish "the launcher gave up before the run started"
        [ -s "$pf" ] && pid="$(tr -dc '0-9' < "$pf")"
        [ -z "$pid" ] && [ $((SECONDS - t0)) -ge "$PID_WAIT" ] && finish "no run pid in $pf after ${PID_WAIT}s"
        [ -z "$pid" ] && nap
    done
    echo "idle: waiting for the run, pid $pid ($(now))"
    while run_alive "$pid"; do
        [ -e "$QD/abort" ] && finish "abort requested"
        nap
    done
    finish "the run (pid $pid) exited"
}

# print the newest unrestored system.state
latest_unrestored() {
    local st qd base stamp p newest="" newest_stamp=""
    while IFS= read -r st; do
        qd="$(dirname "$st")"
        tail -1 "$qd/restore.txt" 2>/dev/null | grep -q '^host restore (.*): OK -- ' && continue
        tail -1 "$qd/restore-reboot.txt" 2>/dev/null | grep -q '^host restore (.*): OK -- ' && continue
        base="$(basename "$(dirname "$qd")")"
        case "$base" in
            run_batches-*) stamp="${base#run_batches-}" ;;
            "$OLD_RUN"*) stamp="${base#"$OLD_RUN"}" ;;
            *) stamp="$base" ;;
        esac
        for p in "${ONE_ROUND_PFXS[@]}"; do [[ "$stamp" == "$p"* ]] && { stamp="${stamp#"$p"}"; break; }; done
        if [ -z "$newest_stamp" ] || [[ "$stamp" > "$newest_stamp" ]]; then newest_stamp="$stamp"; newest="$st"; fi
    done < <(find "$RESD" -maxdepth 3 \( -path '*/run_batches-*/idle/system.state' ${OLD_RUN:+-o -path "*/$OLD_RUN*/idle/system.state"} \) 2>/dev/null)
    echo "$newest"
}

case "$CMD" in
    plan)
        [ "${#POS[@]}" = 0 ] || usage
        plan ;;
    restore)
        [ "${#POS[@]}" -le 1 ] || usage
        if [ "${#POS[@]}" = 0 ]; then
            real_host && [ "$(id -u)" != 0 ] && { echo "idle: restore needs root: sudo bash benchmark/host/idle_machine.sh restore" >&2; exit 2; }
            AUTO="$(latest_unrestored)"
            [ -n "$AUTO" ] || { echo "idle: nothing to restore"; exit 0; }
            echo "idle: restoring the most recently changed unrestored state: $AUTO"
            POS=("$AUTO")
        else
            real_host && [ "$(id -u)" != 0 ] && { echo "idle: restore needs root: sudo bash benchmark/host/idle_machine.sh restore ${POS[0]}" >&2; exit 2; }
        fi
        QD="$(dirname "${POS[0]}")"; STATE="${POS[0]}"; WHY="by hand"
        [ -w "$QD" ] || QD=""
        restore "$STATE" ;;
    start)
        [ "${#POS[@]}" = 2 ] || usage
        real_host && [ "$(id -u)" != 0 ] && { echo "idle: start needs root" >&2; exit 2; }
        QD="${POS[0]}"; [ -d "$QD" ] || { echo "idle: no directory $QD" >&2; exit 2; }
        rm -f "$QD/apply.status" "$QD/abort" "$QD/helper.pid"
        A=(watch "$QD" "${POS[1]}" --pins "$PINS" --vm-cores "$VMC"); [ -n "$SUMMARY" ] && A+=(--summary "$SUMMARY"); [ -n "$LOGF" ] && A+=(--log "$LOGF")
        setsid env --default-signal=HUP,INT,TERM bash "$SELF" "${A[@]}" < /dev/null >> "$QD/helper.log" 2>&1 &
        for i in $(seq $((APPLY_WAIT * 10))); do [ -s "$QD/apply.status" ] && break; sleep 0.1; done
        own "$QD/helper.log"
        st="$(cat "$QD/apply.status" 2>/dev/null)"
        [ -s "$QD/apply.txt" ] && cat "$QD/apply.txt"
        case "$st" in
            applied) echo "idle: the helper, pid $(cat "$QD/helper.pid"), waits for the run in $(dirname "${POS[1]}"), then restores" ;;
            failed) echo "idle: the display manager did not stop; the helper restores everything and exits:" >&2
                    for i in $(seq 300); do [ -s "$QD/restore.txt" ] && break; sleep 0.1; done
                    cat "$QD/restore.txt" >&2 2>/dev/null; exit 1 ;;
            *) echo "idle: the helper did not report within ${APPLY_WAIT}s; asking it to restore, see $QD/helper.log" >&2
               : > "$QD/abort"; exit 1 ;;
        esac ;;
    watch)
        [ "${#POS[@]}" = 2 ] || usage
        QD="${POS[0]}"; STATE="$QD/system.state"
        echo $$ > "$QD/helper.pid"; own "$QD/helper.pid"
        trap 'finish "SIGTERM to the helper"' TERM
        trap 'finish "SIGINT to the helper"' INT
        trap 'finish "SIGHUP to the helper"' HUP
        echo "idle: helper pid $$ applying ($(now))"
        if apply; then echo applied > "$QD/apply.status"; own "$QD/apply.status"; cat "$QD/apply.txt"
        else
            cat "$QD/apply.txt"; echo failed > "$QD/apply.status"; own "$QD/apply.status"
            finish "the display manager did not stop"
        fi
        watch_run "${POS[1]}" ;;
    *) usage ;;
esac
