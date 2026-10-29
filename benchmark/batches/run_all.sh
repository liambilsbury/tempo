#!/usr/bin/env bash
# run every pre-registered single-level batch
#   bash benchmark/batches/run_all.sh [--dry-run] [--one-round | --remeasure] [--no-idle] [--foreground] [--detach] [--only r1,r2,r3-single,r3-nested,r4,levels,levels2,r4b,r5,r6,r7,rhizo,rhizo-nogpu,rhizo-drift,rhizo-drift-nogpu,rhizo-headroom]
#   bash benchmark/batches/run_all.sh --stop [<summary dir>]
#   bash benchmark/batches/run_all.sh --check
#   --one-round    a single round
#   --remeasure    re-measure with the code at HEAD
#   --no-idle     skip host idling
#   --foreground   run in this terminal
#   --detach       return at once
#   --stop         stop the recorded run
#   --check        status of the latest run
set -uo pipefail
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
cd "$(dirname "$SELF")/../.." || exit
ROOT="$PWD"
. benchmark/host/settings.conf

# print the usage lines from this file's header and exit 2
usage() { grep '^#   bash benchmark/batches/run_all.sh' "$SELF" | sed 's/^# *//' >&2; exit 2; }
DRY=0; ONE_ROUND=0; REMEASURE=0; ONLY=""; FG=0; SUMDIR=""; STOP=0; STOPDIR=""; IDLE=1; CHECK=0; DETACH=0
ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift;;
        --one-round) ONE_ROUND=1; shift;;
        --remeasure) REMEASURE=1; shift;;
        --no-idle) IDLE=0; shift;;
        --only) [ $# -ge 2 ] || usage; ONLY="$2"; shift 2;;
        --foreground) FG=1; shift;;
        --detach) DETACH=1; shift;;
        --summary-dir) [ $# -ge 2 ] || usage; SUMDIR="$2"; FG=1; shift 2;;
        --stop) STOP=1; shift; [ $# -gt 0 ] && [ "${1#--}" = "$1" ] && { STOPDIR="$1"; shift; };;
        --check) CHECK=1; shift;;
        -h|--help) sed -n '2,/^set -uo pipefail/p' "$SELF" | sed '$d'; exit 0;;
        *) echo "run_batches: unknown argument '$1'" >&2; usage;;
    esac
done
[ "$REMEASURE" = 1 ] && [ "$ONE_ROUND" = 1 ] && { echo "run_batches: --remeasure and --one-round do not combine" >&2; usage; }
if [ "$ONE_ROUND" = 1 ] || [ "$REMEASURE" = 1 ]; then CODECHECK=0; else CODECHECK=1; fi
ATTACH=0; [ "$ONE_ROUND" = 1 ] && [ "$DETACH" = 0 ] && [ "$FG" = 0 ] && ATTACH=1
ALL=(r1 r2 r3-single r3-nested)
KNOWN=("${ALL[@]}" r4 levels levels2 r4b r5 r6 r7 rhizo rhizo-nogpu rhizo-drift rhizo-drift-nogpu rhizo-headroom)
PLAN=()
if [ -z "$ONLY" ]; then PLAN=("${ALL[@]}")
else
    IFS=, read -ra want <<<"$ONLY"
    for w in "${want[@]}"; do [[ " ${KNOWN[*]} " == *" $w "* ]] || { echo "run_batches: unknown batch '$w'" >&2; usage; }; done
    for b in "${KNOWN[@]}"; do
        [[ ",$ONLY," == *",$b,"* ]] || continue
        if [ "$b" = r4 ]; then PLAN+=(r4-pilot r4)
        elif [ "$b" = r4b ]; then PLAN+=(r4b-pilot r4b)
        elif [ "$b" = r5 ]; then PLAN+=(r5-pilot r5)
        elif [ "$b" = r6 ]; then PLAN+=(r6-nested r6-single)
        elif [ "$b" = r7 ]; then PLAN+=(r7-a r7-b)
        elif [ "$b" = levels ]; then PLAN+=(ladder-pilot ladder-l0 ladder-l1 ladder-l2)
        elif [ "$b" = levels2 ]; then PLAN+=(ladder2-pilot ladder2-l0 ladder2-l1 ladder2-l2)
        else PLAN+=("$b"); fi
    done
fi
# test whether a batch is in the plan
planned() { [[ " ${PLAN[*]} " == *" $1 "* ]]; }
# test whether a revision 1 levels batch is planned
levels1_planned() { planned ladder-pilot || planned ladder-l0 || planned ladder-l1 || planned ladder-l2; }
# test whether a revision 2 levels batch is planned
levels2_planned() { planned ladder2-pilot || planned ladder2-l0 || planned ladder2-l1 || planned ladder2-l2; }
# test whether a levels batch is planned
levels_planned() { levels1_planned || levels2_planned; }
# test whether an R4b batch is planned
r4b_planned() { planned r4b-pilot || planned r4b; }
R4B_EST="estimated duration: about 8.5-9 h; about 2.8 h if the pilot selects no load A"
# test whether an R5 batch is planned
r5_planned() { planned r5-pilot || planned r5; }
R5_EST="estimated duration: about 4.8 h (215 nested runs at pcn-lb2's 80 s each), at most 7.1 h; about 3 h if the pilot selects no duty"
# test whether an R6 batch is planned
r6_planned() { planned r6-nested || planned r6-single; }
R6_EST="estimated duration: about 6.0 h (96 nested runs at pcn-r5's 78 s each, 96 host runs at pc-r4b's 146 s each)"
# test whether an R7 batch is planned
r7_planned() { planned r7-a || planned r7-b; }
# test whether r7-a already attempted all its rounds
r7a_done() { python3 benchmark/batches/per_run_checks.py --attempted "$RES/pcn-r7a.json" "$REPEATS" >/dev/null 2>&1; }
R7_EST="estimated duration: about 4.3 h (192 nested runs at pcn-r6's 79 s each)"
R7B_EST="estimated duration: about 2.2 h, r7-b only (96 nested runs at pcn-r6's 79 s each); about 4.4 h if the L1 VM crashes and the batch is retried"
RHIZO_ALL=(rhizo rhizo-nogpu rhizo-drift rhizo-drift-nogpu rhizo-headroom)
# test whether a name is one of the Rhizo batches
is_rhizo() { case "$1" in rhizo|rhizo-nogpu|rhizo-drift|rhizo-drift-nogpu|rhizo-headroom) return 0;; esac; return 1; }
# test whether a Rhizo batch is planned
rhizo_planned() { local b; for b in "${RHIZO_ALL[@]}"; do planned "$b" && return 0; done; return 1; }
# test whether every batch in the plan is a Rhizo batch
rhizo_only() { local b; [ "${#PLAN[@]}" -gt 0 ] || return 1; for b in "${PLAN[@]}"; do is_rhizo "$b" || return 1; done; return 0; }
RHIZO_N=0 RHIZO_NAMES="" RHIZO_HEADROOM_N=0
for b in "${RHIZO_ALL[@]}"; do
    planned "$b" || continue
    if [ "$b" = rhizo-headroom ]; then RHIZO_HEADROOM_N=1; else RHIZO_N=$((RHIZO_N + 1)); fi
    RHIZO_NAMES+="${RHIZO_NAMES:+, then }$b"
done
RHIZO_H="$(awk -v n="$RHIZO_N" -v m="$RHIZO_HEADROOM_N" 'BEGIN { h = 0.5 + 2 * n + m; if (h == int(h)) printf "%d", h; else printf "%.1f", h }')"
RHIZO_EST="estimated duration: about $RHIZO_H h for $RHIZO_NAMES"
LEVELS2_EST="estimated duration: about 5.5-6 h; about 1.5 h if the pilot selects no duty"
# set the LV* names of a levels revision
lv() {
    case "$1" in
        levels2|ladder2-pilot|ladder2-l0|ladder2-l1|ladder2-l2) LVK=levels2; LVS="--variant levels2"; LVP=pc-lb-pilot; LVR="pc-lb0 pc-lb1 pcn-lb2"
                  LVD=preregistrations/levels2.txt; LVM="ladder2-harness:"; LVO=pc-ladder2 ;;
        *) LVK=levels; LVS=""; LVP=pc-ld-pilot; LVR="pc-ld0 pc-ld1 pcn-ld2"
           LVD=preregistrations/levels.txt; LVM="ladder-harness:"; LVO=pc-ladder ;;
    esac
}

TESTRUN=0; BAD_STUB=""
if [ -n "${TEMPO_BATCHES_CMD:-}" ]; then
    real_exp="$(realpath -m results/experiments)"
    stub_res="$([ -n "${TEMPO_RUN_BATCHES_RESULTS:-}" ] && realpath -m -- "$TEMPO_RUN_BATCHES_RESULTS")"
    if [[ "$TEMPO_BATCHES_CMD" == *batches/run_one.sh* ]]; then BAD_STUB="it names benchmark/batches/run_one.sh"
    elif [ -z "$stub_res" ]; then BAD_STUB="TEMPO_RUN_BATCHES_RESULTS is not set"
    elif [ "$stub_res" = "$real_exp" ] || [[ "$stub_res" == "$real_exp"/* ]] || [[ "$real_exp" == "$stub_res"/* ]]; then
        BAD_STUB="TEMPO_RUN_BATCHES_RESULTS ($stub_res) overlaps results/experiments"
    else TESTRUN=1; fi
fi
DRYRES=0
if [ -n "${TEMPO_RUN_BATCHES_DRY_RESULTS:-}" ] && [ "$DRY" = 1 ] && [ "$TESTRUN" = 0 ] \
   && [ "$CHECK" = 0 ] && [ "$STOP" = 0 ]; then
    real_exp="$(realpath -m results/experiments)"; dry_res="$(realpath -m -- "$TEMPO_RUN_BATCHES_DRY_RESULTS")"
    if [ "$dry_res" != "$real_exp" ] && [[ "$dry_res" != "$real_exp"/* ]] && [[ "$real_exp" != "$dry_res"/* ]]; then
        DRYRES=1
    fi
fi
RES=results/experiments
[ "$TESTRUN" = 1 ] && RES="$TEMPO_RUN_BATCHES_RESULTS"
[ "$DRYRES" = 1 ] && RES="$TEMPO_RUN_BATCHES_DRY_RESULTS"
# print the old name benchmark/renamed.tsv gives for a new name
renamed_old() { awk -F'\t' -v n="$1" '$2 == n { print $1 }' benchmark/renamed.tsv 2>/dev/null; }
OLD_RUN="$(renamed_old 'run_batches-<stamp>')"; OLD_RUN="${OLD_RUN%<stamp>}"
# shellcheck disable=SC2207
ONE_ROUND_PFXS=(one-round- $(renamed_old 'one-round-*' | sed 's/[*]$//'))
OLD_WORD="$(renamed_old run_batches)"; OLD_OUT="$(renamed_old 'run_batches:')"
OLD_SH="$(renamed_old benchmark/batches/run_all.sh | grep /)"
OUTCOME_RE="^(run_batches:${OLD_OUT:+|$(sed 's/[][\.^$*+?(){}|]/\\&/g' <<<"$OLD_OUT")}) (all batches complete|[0-9]+ batch\(es\) need attention)"
# print a run dir's run_batches.pid or .log
run_file() {
    if [ -n "$OLD_WORD" ] && [ ! -e "$1/run_batches.$2" ] && [ -e "$1/$OLD_WORD.$2" ]; then echo "$1/$OLD_WORD.$2"
    else echo "$1/run_batches.$2"; fi
}
# print every summary directory under $RES, old names included
run_dirs() {
    local d
    for d in "$RES"/run_batches-*/ ${OLD_RUN:+"$RES"/"$OLD_RUN"*/}; do [ -d "$d" ] && echo "${d%/}"; done
}
# print "one-round <stamp>" or "real <stamp>" for a summary dir
run_stamp() {
    local rest p
    case "$1" in run_batches-*) rest="${1#run_batches-}";; *) rest="${1#"$OLD_RUN"}";; esac
    for p in "${ONE_ROUND_PFXS[@]}"; do
        [[ "$rest" == "$p"* ]] && { echo "one-round ${rest#"$p"}"; return; }
    done
    echo "real $rest"
}
# test whether a run dir holds an idle state whose last restore line is not OK
unrestored() {
    local f
    [ -f "$1/idle/system.state" ] || return 1
    for f in restore.txt restore-reboot.txt; do
        tail -1 "$1/idle/$f" 2>/dev/null | grep -q '^host restore (.*): OK -- ' && return 1
    done
    return 0
}
# test whether a recorded pid is still a run_all.sh --foreground process
run_alive() {
    local cmd
    [ -n "$1" ] && [ -d "/proc/$1" ] || return 1
    cmd="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)"
    [[ "$cmd" == *benchmark/batches/run_all.sh*--foreground* ]] && return 0
    local old
    while read -r old; do
        [ -n "$old" ] && [[ "$cmd" == *"$old"*--foreground* ]] && return 0
    done <<<"$OLD_SH"
    return 1
}
# format seconds as a short duration
dur() {
    local t="$1"
    if [ "$t" -ge 3600 ]; then echo "$((t / 3600))h $((t % 3600 / 60))m"
    elif [ "$t" -ge 60 ]; then echo "$((t / 60))m $((t % 60))s"
    else echo "${t}s"; fi
}
HOOKS_OK=" "
[ "$DRYRES" = 1 ] && HOOKS_OK+="TEMPO_RUN_BATCHES_DRY_RESULTS "
[ "$TESTRUN" = 1 ] && HOOKS_OK+="TEMPO_BATCHES_CMD TEMPO_L1_CMD TEMPO_L1_DIR TEMPO_L1_SSH TEMPO_L1_SCP TEMPO_L1_BOOT TEMPO_L1_POLL_S TEMPO_L1_DOWN_S TEMPO_R7_AMENDMENT_DOC TEMPO_NO_INHIBIT TEMPO_RUN_BATCHES_SKIP_PREFLIGHT TEMPO_RUN_BATCHES_RESULTS TEMPO_IDLE_SUDO TEMPO_IDLE_SYSTEMCTL TEMPO_R4_PREREGISTRATION_DOC TEMPO_R4B_PREREGISTRATION_DOC TEMPO_R5_PREREGISTRATION_DOC TEMPO_R6_PREREGISTRATION_DOC TEMPO_R7_PREREGISTRATION_DOC TEMPO_LEVELS_PREREGISTRATION_DOC TEMPO_RHIZO_PREREGISTRATION_DOC TEMPO_RHIZO_DRIFT_PREREGISTRATION_DOC TEMPO_RHIZO_HEADROOM_PREREGISTRATION_DOC TEMPO_REALTIME_FILE "
# print a refusal line per leftover TEMPO_* or IDLE_* variable
hook_refusals() {
    local v
    [ -n "$BAD_STUB" ] && echo "TEMPO_BATCHES_CMD is refused: $BAD_STUB; unset it"
    while IFS= read -r v; do
        [ -z "$v" ] && continue
        [[ "$HOOKS_OK" == *" $v "* ]] && continue
        case "$v" in
            TEMPO_BATCHES_CMD|TEMPO_INHIBITED) ;;
            TEMPO_BATCHES_*) echo "$v is set; unset it" ;;
            TEMPO_RUN_BATCHES_DRY_RESULTS) echo "$v is a test hook for --dry-run outside results/experiments only; unset it" ;;
            TEMPO_L1_CMD|TEMPO_L1_*|TEMPO_NO_INHIBIT|TEMPO_RUN_BATCHES_*|TEMPO_IDLE_*|TEMPO_R4_*|TEMPO_R4B_*|TEMPO_R5_*|TEMPO_R6_*|TEMPO_R7_*|TEMPO_LEVELS_*|TEMPO_RHIZO_PREREGISTRATION_DOC|TEMPO_RHIZO_DRIFT_PREREGISTRATION_DOC|TEMPO_RHIZO_HEADROOM_PREREGISTRATION_DOC|TEMPO_REALTIME_*) echo "$v is a test hook for TEMPO_BATCHES_CMD only; unset it" ;;
            IDLE_*) [ "$TESTRUN" = 1 ] || echo "$v is a test hook for TEMPO_BATCHES_CMD only; unset it" ;;
            *) echo "$v is set; unset it" ;;
        esac
    done < <(compgen -e | grep -E '^(TEMPO_|IDLE_)')
}
IDLE_PINS="$HOST_PINS"; IDLE_VM="$VM_HOST_CORES"
AUDIO_UNITS=(pipewire.socket pipewire-pulse.socket pipewire.service wireplumber.service pipewire-pulse.service)
QSUDO=(sudo); USC=(systemctl)
if [ "$TESTRUN" = 1 ]; then
    [ -n "${TEMPO_IDLE_SUDO:-}" ] && read -ra QSUDO <<<"$TEMPO_IDLE_SUDO"
    [ -n "${TEMPO_IDLE_SYSTEMCTL:-}" ] && read -ra USC <<<"$TEMPO_IDLE_SYSTEMCTL"
fi
LAUNCH=0; [ -z "$SUMDIR" ] && LAUNCH=1

if [ "$STOP" = 1 ] || [ "$CHECK" = 1 ]; then
    BAD=0
    while IFS= read -r r; do [ -n "$r" ] && { echo "run_batches: refusing -- $r" >&2; BAD=1; }; done < <(hook_refusals)
    [ "$BAD" = 1 ] && exit 3
fi

if [ "$STOP" = 1 ]; then
    d="${STOPDIR:-$(ls -td "$RES"/run_batches-*/ 2>/dev/null | while read -r x; do [ -s "$x/run_batches.pid" ] && { echo "${x%/}"; break; }; done)}"
    [ -n "$d" ] && [ -s "$d/run_batches.pid" ] || { echo "run_batches: --stop: no run_batches.pid under ${d:-$RES/run_batches-*}" >&2; exit 1; }
    pid="$(tr -dc '0-9' < "$d/run_batches.pid")"
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
    run_alive "$pid" \
        || { echo "run_batches: --stop: pid $pid in $d/run_batches.pid is not a running run (${cmd:-gone}); nothing signalled" >&2; exit 1; }
    pgid="$(sed 's/^.*) //' "/proc/$pid/stat" | awk '{print $3}')"
    lead="$(tr '\0' ' ' < "/proc/$pgid/cmdline" 2>/dev/null)"
    mine="$(sed 's/^.*) //' /proc/$$/stat | awk '{print $3}')"
    if [ "$pgid" = "$mine" ] || { [ "$pgid" != "$pid" ] && [[ "$lead" != *--who=tempo* ]]; }; then
        echo "run_batches: --stop: pid $pid's process group $pgid is not the run's own, leader ${lead:-gone}; nothing signalled" >&2; exit 1
    fi
    echo "run_batches: stopping the run in $d: SIGTERM to pid $pid's process group $pgid"
    [ -s "$d/idle/helper.pid" ] && echo "run_batches: root helper pid $(cat "$d/idle/helper.pid") restores the host after the run exits"
    echo "run_batches: this can take up to 3 min if the L1 VM is up"
    kill -TERM -- "-$pgid"
    for i in $(seq 400); do [ -d "/proc/$pid" ] || break; sleep 0.5; done
    if [ -d "/proc/$pid" ]; then echo "run_batches: pid $pid is still shutting down; see $d/run_batches.log"
    else echo "run_batches: stopped; the partial summary is $d/summary.txt"; fi
    exit 0
fi

# compare an idle dir's recorded values with sysfs
check_governor() {
    local qd="$1" kind a b f label cur
    MISMATCH=(); CHECKED=0
    [ -f "$qd/system.state" ] || return 1
    while IFS=$'\t' read -r kind a b; do
        case "$kind" in
            gov) f="${IDLE_SYS:-/sys}/devices/system/cpu/cpu$a/cpufreq/scaling_governor"; label=governor ;;
            epp) f="${IDLE_SYS:-/sys}/devices/system/cpu/cpu$a/cpufreq/energy_performance_preference"; label=epp ;;
            online) f="${IDLE_SYS:-/sys}/devices/system/cpu/cpu$a/online"; label=online ;;
            *) continue ;;
        esac
        CHECKED=$((CHECKED + 1))
        if [ ! -r "$f" ]; then
            MISMATCH+=("cpu$a is offline or unreadable (recorded $label was $b before the run)")
        else
            cur="$(cat "$f" 2>/dev/null)"
            [ "$cur" = "$b" ] || MISMATCH+=("cpu$a $label is $cur, was $b before the run")
        fi
    done < "$qd/system.state"
    return 0
}

boot_id_now() { cat "${IDLE_PROC:-/proc}/sys/kernel/random/boot_id" 2>/dev/null; }
boot_time_now() { awk '$1 == "btime" { print $2 }' "${IDLE_PROC:-/proc}/stat" 2>/dev/null; }
EARLIER_OLD=""
# test whether an idle state was applied in an earlier boot
state_from_earlier_boot() {
    local st="$1/system.state" rec now t bt
    EARLIER_OLD=""
    [ -f "$st" ] || return 1
    now="$(boot_id_now)"
    rec="$(sed -n 's/^# boot_id=//p' "$st" | head -1)"
    if [ -n "$rec" ] && [ "$rec" != unknown ]; then
        [ -n "$now" ] && [ "$rec" != "$now" ] || return 1
        EARLIER_OLD="$rec"; return 0
    fi
    t="$(sed -n '1s/^# idle state v1, applied \(.*\) by pid .*$/\1/p' "$st")"
    [ -n "$t" ] && t="$(date -d "$t" +%s 2>/dev/null)"
    bt="$(boot_time_now)"
    [[ "$t" =~ ^[0-9]+$ && "$bt" =~ ^[0-9]+$ ]] && [ "$t" -lt "$bt" ] || return 1
    EARLIER_OLD="not recorded, applied $(date -d "@$t" '+%F %T %z') before this boot began $(date -d "@$bt" '+%F %T %z')"
    return 0
}
REBOOT_NOTE=""; RB_NOWRITE=0
# record an earlier boot's idle state as restored when its values all match sysfs
reboot_restored() {
    local qd="$1/idle" out line
    REBOOT_NOTE=""
    state_from_earlier_boot "$qd" || return 1
    check_governor "$qd"
    [ "${#MISMATCH[@]}" -eq 0 ] || return 2
    line="host restore ($(date '+%F %T %z'), after reboot): OK -- the PC was restarted since (boot $EARLIER_OLD -> $(boot_id_now)); nothing idle mode changes survives a restart; $CHECKED values verified"
    if [ "$DRY" = 1 ] || [ "$RB_NOWRITE" = 1 ]; then
        REBOOT_NOTE="$qd: restored by a reboot, $CHECKED value(s) match; dry run: not recorded"
        return 0
    fi
    out="$qd/restore.txt"
    if { [ -e "$out" ] && [ ! -w "$out" ]; } || { [ ! -e "$out" ] && [ ! -w "$qd" ]; }; then out="$qd/restore-reboot.txt"; fi
    { echo "$line" >> "$out"; } 2>/dev/null || return 1
    REBOOT_NOTE="$qd: restored by a reboot, $CHECKED value(s) match; recorded in $out"
    return 0
}
# print MISMATCH[] joined with "; "
mismatch_text() { local IFS=';'; echo "${MISMATCH[*]}" | sed 's/;/; /g'; }
if [ "$CHECK" = 1 ]; then
    live=""
    mapfile -t ODS < <(run_dirs)
    for od in "${ODS[@]}"; do
        pf="$(run_file "$od" pid)"; [ -f "$pf" ] || continue
        run_alive "$(tr -dc '0-9' < "$pf")" && live="$od"
    done
    if [ -n "$live" ]; then
        read -r kind stamp <<<"$(run_stamp "$(basename "$live")")"; [ "$kind" = real ] && kind=run
        pf="$(run_file "$live" pid)"; lf="$(run_file "$live" log)"
        stage="$(sed -n 's/^== \(.*\) ==$/\1/p' "$lf" 2>/dev/null | tail -1)"
        el=$(( $(date +%s) - $(stat -c %Y "$pf") ))
        echo "run_batches: check: run in progress: $live, pid $(tr -dc '0-9' < "$pf")"
        echo "run_batches: check: its last log lines ($lf):"
        tail -3 "$lf" 2>/dev/null | sed 's/^/    /'
        echo "check: RUNNING -- $kind $stamp, stage ${stage:-starting}, running for $(dur "$el")"
        exit 2
    fi
    UNRES=(); RBAD=()
    mapfile -t ODS < <(run_dirs)
    for od in "${ODS[@]}"; do
        unrestored "$od" || continue
        reboot_restored "$od"; rr=$?
        if [ "$rr" = 0 ]; then echo "run_batches: check: $REBOOT_NOTE"; continue; fi
        UNRES+=("$od")
        [ "$rr" = 2 ] && RBAD+=("$od is from an earlier boot but does not match now: $(mismatch_text)")
    done
    f="$(ls -t "$RES"/run_batches-*/summary.txt "$RES"/run_batches-*/run_batches.pid ${OLD_RUN:+"$RES"/"$OLD_RUN"*/summary.txt} \
        ${OLD_RUN:+${OLD_WORD:+"$RES"/"$OLD_RUN"*/"$OLD_WORD".pid}} 2>/dev/null | head -1)"
    if [ -z "$f" ]; then
        echo "run_batches: check: no run_batches run found under $RES"
        echo "check: NOT OK -- no run_batches run found under $RES"
        exit 1
    fi
    d="$(dirname "$f")"; base="$(basename "$d")"
    read -r kind stamp <<<"$(run_stamp "$base")"
    echo "run_batches: check: most recent run: $kind, $stamp ($d)"
    if [ -f "$d/summary.txt" ]; then
        echo "run_batches: check: last lines of $d/summary.txt:"
        tail -6 "$d/summary.txt" | sed 's/^/    /'
    else
        echo "run_batches: check: $d has no summary.txt"
    fi
    rbl="$(cat "$d/idle/restore.txt" "$d/idle/restore-reboot.txt" 2>/dev/null | grep '^host restore (.*, after reboot): OK -- ' | tail -1)"
    [ -n "$rbl" ] && echo "run_batches: check: host restored by a reboot: $rbl"
    if check_governor "$d/idle"; then
        if [ "${#MISMATCH[@]}" -eq 0 ]; then
            echo "run_batches: check: $CHECKED recorded value(s) match sysfs ($d/idle/system.state)"
        else
            echo "run_batches: check: host does NOT match $d/idle/system.state:"
            for m in "${MISMATCH[@]}"; do echo "    $m"; done
        fi
    else
        gov="$(cat "${IDLE_SYS:-/sys}/devices/system/cpu/cpu0/cpufreq/scaling_governor" 2>/dev/null || echo unknown)"
        echo "run_batches: check: CPU0 governor: $gov; no idle state recorded for this run"
    fi
    if [ "${#UNRES[@]}" -gt 0 ]; then
        echo "run_batches: check: host idle state NOT restored under: ${UNRES[*]}"
        for m in "${RBAD[@]}"; do echo "    $m"; done
    else
        echo "run_batches: check: no unrestored host idle state"
    fi
    outcome="$(grep -E "$OUTCOME_RE" "$d/summary.txt" 2>/dev/null | tail -1)"
    started="$(stat -c %Y "$(run_file "$d" pid)" 2>/dev/null || stat -c %Y "$d/header.txt" 2>/dev/null)"; bt="$(boot_time_now)"
    BYRESTART=0
    if [[ "$started" =~ ^[0-9]+$ && "$bt" =~ ^[0-9]+$ ]] && [ "$started" -lt "$bt" ] \
       && { [ -z "$outcome" ] || grep -q '^INTERRUPTED (exit' "$d/summary.txt" 2>/dev/null; }; then BYRESTART=1; fi
    if [ "${#MISMATCH[@]}" -gt 0 ]; then
        first="${MISMATCH[0]}"; rest=$((${#MISMATCH[@]} - 1))
        [ "$rest" -gt 0 ] && first="$first, and $rest more"
        echo "check: NOT OK -- $first"
        exit 1
    elif [ "${#UNRES[@]}" -gt 0 ]; then
        if [ "${#RBAD[@]}" -gt 0 ]; then echo "check: NOT OK -- host idle state not restored under: ${UNRES[*]} (${RBAD[0]}); run sudo bash benchmark/host/idle_machine.sh restore, or reboot"
        else echo "check: NOT OK -- host idle state not restored under: ${UNRES[*]}; run sudo bash benchmark/host/idle_machine.sh restore, or reboot"; fi
        exit 1
    elif [ "$BYRESTART" = 1 ] && [ "$kind" = one-round ]; then
        echo "check: OK -- one-round run $stamp was cut off by a restart; run bash benchmark/batches/run_all.sh --one-round again"
        exit 0
    elif [ "$BYRESTART" = 1 ]; then
        echo "check: NOT OK -- run $stamp was cut off by a restart; run bash benchmark/batches/run_all.sh again to finish it"
        exit 1
    elif [ -z "$outcome" ] || [[ "$outcome" != *"all batches complete"* ]]; then
        echo "check: NOT OK -- $base: ${outcome:-no outcome line in $d/summary.txt}"
        exit 1
    else
        echo "check: OK -- $base finished and the host is back to normal"
        exit 0
    fi
fi

INHIBIT=(systemd-inhibit --what=sleep:idle:handle-lid-switch --who=tempo "--why=Tempo run_batches batches")
# test whether this process runs under the inhibitor
inhibited() { [ -n "${TEMPO_INHIBITED:-}" ] && [ "$(cat "/proc/$PPID/comm" 2>/dev/null)" = "${INHIBIT[0]}" ]; }
if [ "$DRY" = 1 ] && [ "$FG" = 1 ]; then
    shown=(); for a in "${ARGS[@]}"; do [ "$a" = --dry-run ] || shown+=("$a"); done
    echo "+ exec env TEMPO_INHIBITED=1 ${INHIBIT[*]} bash $SELF ${shown[*]}$([ "${TEMPO_NO_INHIBIT:-}" = 1 ] && echo "   -- not taken: TEMPO_NO_INHIBIT=1")"
elif [ "$DRY" = 0 ] && [ "$FG" = 1 ] && [ "${TEMPO_NO_INHIBIT:-}" != 1 ] && ! inhibited; then
    command -v "${INHIBIT[0]}" >/dev/null && "${INHIBIT[@]}" true \
        || { echo "run_batches: refusing -- cannot take the sleep inhibitor (${INHIBIT[*]})" >&2; exit 3; }
    exec env TEMPO_INHIBITED=1 "${INHIBIT[@]}" bash "$SELF" "${ARGS[@]}"
    echo "run_batches: refusing -- could not re-execute under the sleep inhibitor" >&2; exit 3
fi

PFX=""; REPEATS=16; PILOT_REPEATS=3; R4B_PILOT_REPEATS=5; R5_PILOT_REPEATS=5
ONE_ROUND_ENV=()
if [ "$ONE_ROUND" = 1 ]; then
    PFX=one-round-; REPEATS=1; PILOT_REPEATS=1; R4B_PILOT_REPEATS=1; R5_PILOT_REPEATS=1
    ONE_ROUND_ENV=(TEMPO_BATCHES_REPEATS=1 TEMPO_BATCHES_FRAMES=1000 TEMPO_BATCHES_INJECT_S=1 TEMPO_BATCHES_SETTLE_S=0 TEMPO_BATCHES_PREFIX=one-round-)
fi
RB_RF=""; RB_LR=""
if [ "$REMEASURE" = 1 ]; then
    RM_TREE="$(git ls-tree HEAD -- benchmark src tools/build Makefile 2>/dev/null | sha256sum | cut -c1-40)"
    PFX="remeasure-${RM_TREE:0:12}-"
    ONE_ROUND_ENV=(TEMPO_BATCHES_PREFIX="$PFX" TEMPO_BATCHES_REMEASURE=1)
fi
if [ "$ONE_ROUND" = 1 ]; then RB_RF="--one-round-fallback"; RB_LR="--range 3000-4000"; fi
declare -A NAME=([r1]=pc-kern16 [r2]=pc-adapt [r3-single]=pc-r3 [r3-nested]=pcn-r3 [r4-pilot]=pc-r4-pilot [r4]=pc-r4
                 [ladder-pilot]=pc-ld-pilot [ladder-l0]=pc-ld0 [ladder-l1]=pc-ld1 [ladder-l2]=pcn-ld2
                 [ladder2-pilot]=pc-lb-pilot [ladder2-l0]=pc-lb0 [ladder2-l1]=pc-lb1 [ladder2-l2]=pcn-lb2
                 [r4b-pilot]=pc-r4b-pilot [r4b]=pc-r4b [r5-pilot]=pcn-r5-pilot [r5]=pcn-r5 [r6-nested]=pcn-r6 [r6-single]=pc-r6 [r7-a]=pcn-r7a [r7-b]=pcn-r7b [rhizo]=rhizo [rhizo-nogpu]=rhizo-nogpu [rhizo-drift]=rhizo-drift [rhizo-drift-nogpu]=rhizo-drift-nogpu [rhizo-headroom]=rhizo-headroom)
declare -A LABELS=([r1]="pc-kern16-nonrt pc-kern16-rt" [r2]="fixed-hi fixed-hi-probe adaptive fixed-lo"
                   [r3-single]="pc-r3-control pc-r3-hog" [r3-nested]="pcn-r3-control pcn-r3-hog"
                   [r4-pilot]="pc-r4p-control $(python3 -c "import sys; sys.path.insert(0, '.'); from benchmark.score.single_level import r4 as r4_select; print(*('pc-r4p-probe-d%d' % d for d in r4_select.GRID))")"
                   [r4]="fixed-hi fixed-hi-probe adaptive fixed-lo pc-r4-control"
                   [ladder-pilot]="pc-ldp-control $(python3 benchmark/score/single_level/levels_select.py --grid | awk '{print $2}' | tr '\n' ' ')"
                   [ladder-l0]="pc-ld0-control pc-ld0-hog" [ladder-l1]="pc-ld1-control pc-ld1-hog" [ladder-l2]="pcn-ld2-control pcn-ld2-hog"
                   [ladder2-pilot]="pc-lbp-control $(python3 benchmark/score/single_level/levels_select.py --grid --variant levels2 | awk '{print $2}' | tr '\n' ' ')"
                   [ladder2-l0]="pc-lb0-control pc-lb0-hog" [ladder2-l1]="pc-lb1-control pc-lb1-hog" [ladder2-l2]="pcn-lb2-control pcn-lb2-hog"
                   [r4b-pilot]="pc-r4bp-control $(python3 benchmark/score/single_level/r4b.py --grid | awk '{print $2}' | tr '\n' ' ')"
                   [r4b]="a-fixed-hi a-fixed-hi-probe a-adaptive a-fixed-lo b-fixed-hi b-fixed-hi-probe b-adaptive b-fixed-lo pc-r4b-control"
                   [r5-pilot]="pcn-r5p-control $(python3 benchmark/score/single_level/r5.py --grid | awk '{print $2}' | tr '\n' ' ')"
                   [r5]="pcn-r5-fixed-hi pcn-r5-fixed-hi-probe pcn-r5-adaptive pcn-r5-fixed-lo pcn-r5-control"
                   [r6-nested]="pcn-r6-fixed-hi pcn-r6-fixed-hi-probe pcn-r6-adaptive pcn-r6-adaptive-rs pcn-r6-fixed-lo pcn-r6-control"
                   [r6-single]="pc-r6-fixed-hi pc-r6-fixed-hi-probe pc-r6-adaptive pc-r6-adaptive-rs pc-r6-fixed-lo pc-r6-control"
                   [r7-a]="pcn-r7a-fixed-hi pcn-r7a-fixed-hi-probe pcn-r7a-adaptive pcn-r7a-adaptive-rs pcn-r7a-fixed-lo pcn-r7a-control"
                   [r7-b]="pcn-r7b-fixed-hi pcn-r7b-fixed-hi-probe pcn-r7b-adaptive pcn-r7b-adaptive-rs pcn-r7b-fixed-lo pcn-r7b-control"
                   [rhizo]="rhizo-noload-noadpf rhizo-noload-adpf rhizo-vmload-noadpf rhizo-vmload-adpf rhizo-pcload-noadpf rhizo-pcload-adpf"
                   [rhizo-nogpu]="nogpu-rhizo-noload-noadpf nogpu-rhizo-noload-adpf nogpu-rhizo-vmload-noadpf nogpu-rhizo-vmload-adpf nogpu-rhizo-pcload-noadpf nogpu-rhizo-pcload-adpf"
                   [rhizo-drift]="drift-rhizo-noload-noadpf drift-rhizo-noload-adpf drift-rhizo-vmload-noadpf drift-rhizo-vmload-adpf drift-rhizo-pcload-noadpf drift-rhizo-pcload-adpf"
                   [rhizo-drift-nogpu]="drift-nogpu-rhizo-noload-noadpf drift-nogpu-rhizo-noload-adpf drift-nogpu-rhizo-vmload-noadpf drift-nogpu-rhizo-vmload-adpf drift-nogpu-rhizo-pcload-noadpf drift-nogpu-rhizo-pcload-adpf"
                   [rhizo-headroom]="headroom-rhizo-noload headroom-rhizo-vmload headroom-rhizo-pcload")
# drop r4b's b-* arms when its pilot selected no load B
r4b_labels() {
    if [ "$(sed -n 's/^selected_duty_b=//p' "$RES/${PFX}pc-r4b-pilot.selection" 2>/dev/null | tail -1)" = none ]; then
        LABELS[r4b]="a-fixed-hi a-fixed-hi-probe a-adaptive a-fixed-lo pc-r4b-control"
    fi
}
# print the rounds a batch runs
reps() { case "$1" in r4-pilot|ladder-pilot|ladder2-pilot) echo "$PILOT_REPEATS";; r4b-pilot) echo "$R4B_PILOT_REPEATS";; r5-pilot) echo "$R5_PILOT_REPEATS";; *) echo "$REPEATS";; esac; }
# print a batch's labels with the prefix
labels() { local l out=(); for l in ${LABELS[$1]}; do out+=("$PFX$l"); done; echo "${out[*]}"; }
if [ "$TESTRUN" = 1 ]; then read -ra PCB <<<"$TEMPO_BATCHES_CMD"; else PCB=(bash benchmark/batches/run_one.sh); fi
STAMP="$(date +%Y%m%d-%H%M)"
if [ -n "$SUMDIR" ]; then SUM="$SUMDIR"
else SUM="$RES/run_batches-$PFX$STAMP"; [ -e "$SUM" ] && SUM="$SUM-$(date +%S)-$$"; fi
L1D=build/vm
SSH=(ssh -n -p "$VM_SSH_PORT" -o "UserKnownHostsFile=$L1D/known_hosts" -o StrictHostKeyChecking=accept-new
     -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 tempo@127.0.0.1)
SCP=(scp -q -P "$VM_SSH_PORT" -o "UserKnownHostsFile=$L1D/known_hosts" -o StrictHostKeyChecking=accept-new
     -o BatchMode=yes -o ConnectTimeout=10)
if [ "$ONE_ROUND" = 1 ]; then L1_BATCH_S=3600; else L1_BATCH_S=18000; fi
L1_POLL_S=30; L1_DOWN_S=600
L1BOOT=(bash benchmark/vm/run.sh --background)
if [ "$TESTRUN" = 1 ]; then
    [ -n "${TEMPO_L1_DIR:-}" ] && L1D="$TEMPO_L1_DIR"
    [ -n "${TEMPO_L1_SSH:-}" ] && read -ra SSH <<<"$TEMPO_L1_SSH"
    [ -n "${TEMPO_L1_SCP:-}" ] && read -ra SCP <<<"$TEMPO_L1_SCP"
    [ -n "${TEMPO_L1_BOOT:-}" ] && read -ra L1BOOT <<<"$TEMPO_L1_BOOT"
    [ -n "${TEMPO_L1_POLL_S:-}" ] && L1_POLL_S="$TEMPO_L1_POLL_S"
    [ -n "${TEMPO_L1_DOWN_S:-}" ] && L1_DOWN_S="$TEMPO_L1_DOWN_S"
fi

# print a line, and log it unless the run is dry or detached
say() { echo "$*"; [ "$DRY" = 1 ] || [ -n "$SUMDIR" ] || echo "$*" >> "$SUM/run_batches.log"; }
# print the batch runner's dry-run plan for a batch
batches_dry() {
    local out l
    out="$("${CMDPFX[@]}" "${PCB[@]}" --dry-run "$@" 2>&1)"
    printf '%s\n' "$out" | sed 's/^/    | /'
    [ "$REMEASURE" = 1 ] || return 0
    while IFS= read -r l; do
        case "$l" in *"batches: refusing"*) grep -qE "$HOSTPAT" <<<"$l" || REPO_REFUSED=1;; esac
    done <<<"$out"
    return 0
}
# print, log and run a command unless dry-running
step() {
    local log="$1"; shift
    say "[$(date +%T)] + $*"
    [ "$DRY" = 1 ] && return 0
    echo "[$(date +%T)] + $*" >> "$log"
    "$@" 9>&- 2>&1 | tee -a "$log"
    local rc="${PIPESTATUS[0]}"
    [ "$rc" = 0 ] || say "    exit $rc"
    return "$rc"
}
# run a command like step, with stdout written to a file
step_to() {
    local file="$1" log="$2"; shift 2
    say "[$(date +%T)] + $* > $file"
    [ "$DRY" = 1 ] && return 0
    echo "[$(date +%T)] + $* > $file" >> "$log"
    "$@" 9>&- > "$file.tmp" 2>> "$log"
    local rc=$?
    mv -f "$file.tmp" "$file"; cat "$file" >> "$log"
    [ "$rc" = 0 ] || say "    exit $rc"
    return "$rc"
}

REFUSE=()
while IFS= read -r r; do [ -n "$r" ] && REFUSE+=("$r"); done < <(hook_refusals)
if [ "$IDLE" = 1 ] && [ "$LAUNCH" = 1 ]; then
    gui=""; [ -n "${WAYLAND_DISPLAY:-}" ] && gui+=" WAYLAND_DISPLAY=$WAYLAND_DISPLAY"; [ -n "${DISPLAY:-}" ] && gui+=" DISPLAY=$DISPLAY"
    case "${XDG_SESSION_TYPE:-}" in wayland|x11) gui+=" XDG_SESSION_TYPE=$XDG_SESSION_TYPE";; esac
    [ -n "$gui" ] && REFUSE+=("this terminal is in graphical session $gui; run from the text console on ctrl+alt+F3, or pass --no-idle")
    [ "$TESTRUN" = 1 ] && [ -z "${TEMPO_IDLE_SUDO:-}" ] && REFUSE+=("TEMPO_IDLE_SUDO is not set; set it to a stub, or pass --no-idle")
    [ -f benchmark/host/idle_machine.sh ] || REFUSE+=("benchmark/host/idle_machine.sh is missing; pass --no-idle to skip idling")
fi
RB_PENDING=(); RB_NOWRITE=1
mapfile -t ODS < <(run_dirs)
for d in "${ODS[@]}"; do
    [ "$d" = "$SUM" ] && continue
    unrestored "$d" || continue
    reboot_restored "$d"; rr=$?
    if [ "$rr" = 0 ]; then RB_PENDING+=("$d"); [ "$DRY" = 1 ] && echo "run_batches: $REBOOT_NOTE"; continue; fi
    if [ "$rr" = 2 ]; then
        REFUSE+=("$d/idle: host idle state from an earlier boot does not match the host: $(mismatch_text); run sudo bash benchmark/host/idle_machine.sh restore $d/idle/system.state")
        continue
    fi
    REFUSE+=("$d/idle: host idle state not restored OK: $(tail -1 "$d/idle/restore.txt" 2>/dev/null || echo "no restore.txt"); run sudo bash benchmark/host/idle_machine.sh restore $d/idle/system.state, or reboot")
done
if [ "$REMEASURE" = 1 ]; then
    for f in benchmark/batches/run_one.sh benchmark/run/paired.py benchmark/batches/per_run_checks.py benchmark/batches/move_interrupted_batch.sh benchmark/host/idle_machine.sh \
             benchmark/lib.py benchmark/host/settings.conf benchmark/run/experiment.sh benchmark/score/nested/quality_shedding.py benchmark/score/single_level/r1_r3.py \
             benchmark/score/single_level/r4.py benchmark/score/single_level/r4b.py benchmark/score/single_level/r5.py benchmark/score/single_level/r6.py benchmark/score/single_level/r7.py \
             benchmark/score/single_level/levels_select.py benchmark/score/single_level/levels.py benchmark/vm/run.sh benchmark/vm/sync.sh benchmark/vm/make.sh \
             benchmark/run/rhizo/batch.sh benchmark/run/rhizo/run.sh benchmark/run/rhizo/lib.sh benchmark/run/rhizo/host.py benchmark/score/rhizo/frames.py benchmark/score/rhizo/trace.py benchmark/score/rhizo/score.py benchmark/score/rhizo/headroom.py src/android/pipeline.c src/android/args.h src/android/cpu_load.c \
             src/android/app/loop.c src/android/app/run_args.h src/android/app/AndroidManifest.xml tools/build/android_app.sh \
             src/pipeline/pipeline.c tools/build/guest_kernel.sh; do
        [ -e "$f" ] || REFUSE+=("$f is missing, which a re-measurement names")
    done
    if r6_planned || r7_planned; then
        for f in results/single_level/r5/pcn-r5-pilot.json results/single_level/r4b/pc-r4b-a.json; do
            [ -e "$f" ] || REFUSE+=("$f is missing, which re-measuring R6 and R7 needs")
        done
    fi
    git rev-parse --verify -q HEAD >/dev/null || REFUSE+=("no commit at HEAD to re-measure at")
fi
# refuse a batch whose pre-registration document is not in the tree
doc_found() { [ -f "$1" ] || { REFUSE+=("$1 not found"); return 1; }; }
SKIP_PF=0; [ "$TESTRUN" = 1 ] && [ "${TEMPO_RUN_BATCHES_SKIP_PREFLIGHT:-}" = 1 ] && SKIP_PF=1
if planned r4 && [ "$CODECHECK" = 1 ]; then
    R4DOC=preregistrations/r4.txt; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_R4_PREREGISTRATION_DOC:-}" ] && R4DOC="$TEMPO_R4_PREREGISTRATION_DOC"
    doc_found "$R4DOC" && { hmsg="$(python3 benchmark/score/single_level/r4.py --measurement-code "$R4DOC" 2>&1)" \
        || REFUSE+=("R4's measurement code check failed: ${hmsg#r4_select: refusing -- }"); }
fi
if r4b_planned; then
    RTF=/sys/kernel/realtime; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_REALTIME_FILE:-}" ] && RTF="$TEMPO_REALTIME_FILE"
    [ "$(cat "$RTF" 2>/dev/null)" = 1 ] \
        || REFUSE+=("R4b needs a PREEMPT_RT host kernel; $RTF is not 1 on $(uname -r)")
    if [ "$CODECHECK" = 1 ]; then
        BDOC=preregistrations/r4b.txt; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_R4B_PREREGISTRATION_DOC:-}" ] && BDOC="$TEMPO_R4B_PREREGISTRATION_DOC"
        doc_found "$BDOC" && { hmsg="$(python3 benchmark/score/single_level/levels.py --measurement-code "$BDOC" --mark "r4b-harness:" 2>&1)" \
            || REFUSE+=("R4b's measurement code check failed: ${hmsg#levels_score: refusing -- }"); }
    fi
fi
if r5_planned; then
    RTF=/sys/kernel/realtime; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_REALTIME_FILE:-}" ] && RTF="$TEMPO_REALTIME_FILE"
    [ "$(cat "$RTF" 2>/dev/null)" = 1 ] \
        || REFUSE+=("R5 needs a PREEMPT_RT host kernel; $RTF is not 1 on $(uname -r)")
    if [ "$CODECHECK" = 1 ]; then
        FDOC=preregistrations/r5.txt; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_R5_PREREGISTRATION_DOC:-}" ] && FDOC="$TEMPO_R5_PREREGISTRATION_DOC"
        doc_found "$FDOC" && { hmsg="$(python3 benchmark/score/single_level/levels.py --measurement-code "$FDOC" --mark "r5-harness:" 2>&1)" \
            || REFUSE+=("R5's measurement code check failed: ${hmsg#levels_score: refusing -- }"); }
    fi
fi
if r6_planned; then
    RTF=/sys/kernel/realtime; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_REALTIME_FILE:-}" ] && RTF="$TEMPO_REALTIME_FILE"
    [ "$(cat "$RTF" 2>/dev/null)" = 1 ] \
        || REFUSE+=("R6 needs a PREEMPT_RT host kernel; $RTF is not 1 on $(uname -r)")
    if [ "$CODECHECK" = 1 ]; then
        SDOC=preregistrations/r6.txt; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_R6_PREREGISTRATION_DOC:-}" ] && SDOC="$TEMPO_R6_PREREGISTRATION_DOC"
        doc_found "$SDOC" && { hmsg="$(python3 benchmark/score/single_level/levels.py --measurement-code "$SDOC" --mark "r6-harness:" 2>&1)" \
            || REFUSE+=("R6's measurement code check failed: ${hmsg#levels_score: refusing -- }"); }
    fi
fi
if r7_planned; then
    RTF=/sys/kernel/realtime; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_REALTIME_FILE:-}" ] && RTF="$TEMPO_REALTIME_FILE"
    [ "$(cat "$RTF" 2>/dev/null)" = 1 ] \
        || REFUSE+=("R7 needs a PREEMPT_RT host kernel; $RTF is not 1 on $(uname -r)")
    if [ "$CODECHECK" = 1 ] && ! r7a_done; then
        UDOC=preregistrations/r7.txt; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_R7_PREREGISTRATION_DOC:-}" ] && UDOC="$TEMPO_R7_PREREGISTRATION_DOC"
        doc_found "$UDOC" && { hmsg="$(python3 benchmark/score/single_level/levels.py --measurement-code "$UDOC" --mark "r7-harness:" 2>&1)" \
            || REFUSE+=("R7's measurement code check failed: ${hmsg#levels_score: refusing -- }"); }
    elif [ "$CODECHECK" = 1 ]; then
        ADOC=preregistrations/r7_amendment.txt; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_R7_AMENDMENT_DOC:-}" ] && ADOC="$TEMPO_R7_AMENDMENT_DOC"
        doc_found "$ADOC" && { hmsg="$(python3 benchmark/score/single_level/levels.py --measurement-code "$ADOC" --mark "r7b-harness:" 2>&1)" \
            || REFUSE+=("R7's amendment measurement code check failed: ${hmsg#levels_score: refusing -- }"); }
        am="$(sed -n 's/.*<!-- r7b-harness: \([0-9a-f]*\) -->.*/\1/p' "$ADOC" 2>/dev/null | head -1)"
        if [ "$TESTRUN" = 0 ]; then
            allowed="$(sed -n 's/.*<!-- r7b-allowed: \(.*\) -->.*/\1/p' "$ADOC" 2>/dev/null)"
            if [ -z "$allowed" ]; then extra="no allowed file list in $ADOC"
            else extra="$(git diff --name-only 6c82617 "${am:-HEAD}" -- bench guest src Makefile 2>&1 \
                | grep -vxF -f <(printf '%s\n' $allowed))"; fi
            [ -z "$extra" ] || REFUSE+=("measurement code differs from 6c82617: $(echo $extra)")
        fi
    fi
fi
# refuse a Rhizo family that does not match its pre-registration
rhizo_marks() {
    local doc="$1" mark="$2" who="$3" hmsg rtree ksha got
    [ -f "$doc" ] || { REFUSE+=("$doc is missing, which $who needs"); return; }
    hmsg="$(python3 benchmark/score/single_level/levels.py --measurement-code "$doc" --mark "$mark" 2>&1)" \
        || REFUSE+=("$who's measurement code check failed: ${hmsg#levels_score: refusing -- }")
    rtree="$(sed -n 's/.*<!-- rhizo-tree: \([0-9a-f]*\) -->.*/\1/p' "$doc" 2>/dev/null | head -1)"
    if [ -z "$rtree" ]; then REFUSE+=("$doc names no Rhizo commit (<!-- rhizo-tree: <sha> -->)")
    else
        git -C "$RT" diff --quiet "$rtree" HEAD -- src debian 2>/dev/null || REFUSE+=("Rhizo's src/ and debian/ in $RT differ from the pre-registered $rtree")
        [[ "$(git -C "$RT" rev-parse HEAD 2>/dev/null)" == "$rtree"* ]] || REFUSE+=("the Rhizo checkout $RT is not at the pre-registered commit $rtree, which every run records and the freezer requires")
    fi
    ksha="$(sed -n 's/.*<!-- guest-kernel-sha256: \([0-9a-f]*\) -->.*/\1/p' "$doc" 2>/dev/null | head -1)"
    if [ -n "$ksha" ]; then
        got="$(sha256sum "$RT/var/vmlinuz-steal" 2>/dev/null | cut -d' ' -f1)"
        [ "$got" = "$ksha" ] || REFUSE+=("$RT/var/vmlinuz-steal is ${got:-missing}, not the pre-registered steal-accounting guest kernel $ksha")
    fi
}
if rhizo_planned; then
    RT="${RHIZO_TREE:-}"
    if [ -z "$RT" ] || [ ! -d "$RT" ]; then
        REFUSE+=("the Rhizo batch needs RHIZO_TREE set to a Rhizo checkout (now '$RT')")
    elif [ "$CODECHECK" = 1 ]; then
        if planned rhizo || planned rhizo-nogpu; then
            HDOC=preregistrations/rhizo.txt; [ -f preregistrations/rhizo_amendment.txt ] && HDOC=preregistrations/rhizo_amendment.txt
            [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_RHIZO_PREREGISTRATION_DOC:-}" ] && HDOC="$TEMPO_RHIZO_PREREGISTRATION_DOC"
            rhizo_marks "$HDOC" "rhizo-harness:" "the Rhizo batch"
        fi
        if planned rhizo-drift || planned rhizo-drift-nogpu; then
            DDOC=preregistrations/rhizo_drift.txt
            [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_RHIZO_DRIFT_PREREGISTRATION_DOC:-}" ] && DDOC="$TEMPO_RHIZO_DRIFT_PREREGISTRATION_DOC"
            rhizo_marks "$DDOC" "rhizo-drift-harness:" "the drift batch"
        fi
        if planned rhizo-headroom; then
            RDOC=preregistrations/rhizo_headroom.txt
            [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_RHIZO_HEADROOM_PREREGISTRATION_DOC:-}" ] && RDOC="$TEMPO_RHIZO_HEADROOM_PREREGISTRATION_DOC"
            rhizo_marks "$RDOC" "rhizo-headroom-harness:" "the headroom batch"
        fi
    fi
fi
if levels_planned; then
    RTF=/sys/kernel/realtime; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_REALTIME_FILE:-}" ] && RTF="$TEMPO_REALTIME_FILE"
    [ "$(cat "$RTF" 2>/dev/null)" = 1 ] \
        || REFUSE+=("the levels comparison needs a PREEMPT_RT host kernel; $RTF is not 1 on $(uname -r)")
    for lk in levels levels2; do
        { [ "$lk" = levels ] && levels1_planned; } || { [ "$lk" = levels2 ] && levels2_planned; } || continue
        lv "$lk"
        if [ "$CODECHECK" = 1 ]; then
            LDOC="$LVD"; [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_LEVELS_PREREGISTRATION_DOC:-}" ] && LDOC="$TEMPO_LEVELS_PREREGISTRATION_DOC"
            doc_found "$LDOC" && { hmsg="$(python3 benchmark/score/single_level/levels.py --measurement-code "$LDOC" --mark "$LVM" 2>&1)" \
                || REFUSE+=("the $lk measurement code check failed: ${hmsg#levels_score: refusing -- }"); }
        fi
        if [ "$lk" = levels2 ] && [ "$REMEASURE" = 0 ]; then
            vmsg="$(python3 benchmark/score/single_level/levels_select.py --verify-frozen "$RES/pc-ld-pilot.selection" results/single_level/levels/pc-ld-pilot.json 2>&1)" \
                || REFUSE+=("revision 1 of the levels comparison is not as frozen: ${vmsg#levels_select: refusing -- }")
        fi
    done
fi
HOST_BATCH=0; for b in r1 r2 r3-single r4 r4b-pilot r4b r6-single ladder-pilot ladder-l0 ladder-l1 ladder2-pilot ladder2-l0 ladder2-l1; do planned "$b" && HOST_BATCH=1; done
if [ "$SKIP_PF" = 0 ]; then
    [ -z "$(git status --porcelain 2>&1)" ] || REFUSE+=("the git tree is dirty; commit or stash first")
    need_k=(); planned r1 && need_k+=(kernel-rt)
    { planned r1 || planned r2 || planned r3-single || planned r3-nested || planned r4 || levels_planned || r4b_planned || r5_planned || r6_planned || r7_planned; } && need_k+=(kernel-nonrt)
    for k in "${need_k[@]}"; do
        [ -f "build/$k/vmlinuz" ] || REFUSE+=("build/$k/vmlinuz is missing; build it with bash tools/build/guest_kernel.sh --variant ${k#kernel-}")
    done
    if [ "$HOST_BATCH" = 1 ]; then
        for c in /usr/bin/bpftrace /usr/bin/pkill "$PWD/build/inject"; do
            sudo -n -l "$c" >/dev/null 2>&1 || REFUSE+=("no NOPASSWD sudo for $c (/etc/sudoers.d/50-tempo)")
        done
    fi
    pgrep -f -- '-name tempo-l1' >/dev/null && REFUSE+=("the L1 VM is running; stop it first: bash benchmark/vm/stop.sh")
    for nb in r3-nested ladder-l2 ladder2-l2 r5-pilot r5 r6-nested r7-a r7-b; do
        planned "$nb" || continue
        [ -f "$L1D/l1.qcow2" ] || REFUSE+=("$L1D/l1.qcow2 is missing, which $nb needs; make it with bash benchmark/vm/make.sh")
        [ -f "$L1D/seed.img" ] || REFUSE+=("$L1D/seed.img is missing, which $nb needs; make it with bash benchmark/vm/make.sh")
        command -v qemu-system-x86_64 >/dev/null || REFUSE+=("qemu-system-x86_64 is not installed, which $nb needs")
    done
    if rhizo_planned && [ -d "${RHIZO_TREE:-/nonexistent}" ]; then
        [ -z "$(git -C "$RHIZO_TREE" status --porcelain --untracked-files=no 2>&1)" ] || REFUSE+=("the Rhizo checkout $RHIZO_TREE has uncommitted changes")
        (cd "$RHIZO_TREE" && python3 tools/build/stamps.py deb-require) >/dev/null 2>&1 || REFUSE+=("the Rhizo .deb in $RHIZO_TREE does not match its tree; run make env-deb there")
        [ -x third_party/perfetto/trace_processor_shell ] || REFUSE+=("no trace_processor; fetch it with bash tools/fetch/trace_processor.sh")
        [ -f build/android/tempo-loop.apk ] || REFUSE+=("build/android/tempo-loop.apk is missing; build it with make android-app rhizo-vm-cpu-load NDK=<ndk root> SDK=<android sdk root>")
        [ -x build/rhizo_vm/cpu_load ] || REFUSE+=("build/rhizo_vm/cpu_load is missing; build it with make android-app rhizo-vm-cpu-load NDK=<ndk root> SDK=<android sdk root>")
        make -q build/android/tempo-loop.apk build/rhizo_vm/cpu_load build/inject >/dev/null 2>&1 \
            || REFUSE+=("build/android/tempo-loop.apk, build/rhizo_vm/cpu_load or build/inject is older than its sources; rebuild with make android-app rhizo-vm-cpu-load build/inject NDK=<ndk root> SDK=<android sdk root>")
        sudo -n -l "$PWD/build/inject" >/dev/null 2>&1 || REFUSE+=("no NOPASSWD sudo for $PWD/build/inject (/etc/sudoers.d/50-tempo)")
        pgrep -f '^[^ ]*qemu-system-' >/dev/null && REFUSE+=("a VM is running; the Rhizo batch boots its own and needs the PC to itself")
        [ -e build/rhizo_vm/batch.state ] && REFUSE+=("a Rhizo batch is still set up: bash benchmark/run/rhizo/batch.sh --teardown")
    fi
fi
if [ "$DRY" = 0 ]; then
    mkdir -p "$RES" || REFUSE+=("cannot create $RES")
    exec 9>>"$RES/.run_batches.lock" && flock -n 9 || REFUSE+=("another run_batches.sh holds $RES/.run_batches.lock")
    [ "$FG" = 0 ] || [ "${TEMPO_NO_INHIBIT:-}" = 1 ] || inhibited || REFUSE+=("not running under the sleep inhibitor")
fi
HOSTPAT='PREEMPT_RT host|build/kernel-|NOPASSWD sudo|build/vm/|qemu-system|L1 VM is running|graphical session|git tree is dirty|sleep inhibitor|idle state|host idle'
HOSTPAT+='|RHIZO_TREE|Rhizo checkout|Rhizo [.]deb|trace_processor|build/android/|build/rhizo_vm/|a VM is running|Rhizo batch is still set up|older than its sources'
REPO_REFUSED=0
if [ "${#REFUSE[@]}" -gt 0 ]; then
    for r in "${REFUSE[@]}"; do
        if [ "$DRY" = 1 ]; then
            if [ "$REMEASURE" = 1 ] && grep -qE "$HOSTPAT" <<<"$r"; then echo "run_batches: host prerequisite you supply -- $r"
            else echo "run_batches: preflight would refuse -- $r"; [ "$REMEASURE" = 1 ] && REPO_REFUSED=1; fi
        else echo "run_batches: refusing -- $r" >&2; fi
    done
    [ "$DRY" = 1 ] || exit 3
fi
RB_NOWRITE=0
if [ "$DRY" = 0 ]; then
    for d in "${RB_PENDING[@]}"; do
        if reboot_restored "$d"; then echo "run_batches: $REBOOT_NOTE"
        else echo "run_batches: refusing -- cannot record $d/idle as restored: ${MISMATCH[*]:-restore.txt not writable}; run sudo bash benchmark/host/idle_machine.sh restore $d/idle/system.state" >&2; exit 3; fi
    done
fi

if [ "$IDLE" = 1 ] && [ "$LAUNCH" = 1 ]; then
    echo "run_batches: host idling stops the display manager; run from a text console (ctrl+alt+F3)"
    if [ "$DRY" = 1 ]; then
        echo "+ ${QSUDO[*]} -v"
    else
        "${QSUDO[@]}" -v || { echo "run_batches: refusing -- sudo was refused or cancelled; nothing changed. Retry, or pass --no-idle" >&2; exit 3; }
    fi
fi

if [ "$DRY" = 1 ]; then echo "run_batches: summary at $SUM (dry run: nothing is written or run)"
else mkdir -p "$SUM" || { echo "run_batches: refusing -- cannot create $SUM" >&2; exit 3; }; echo "run_batches: summary at $SUM"; fi
if [ "$ONE_ROUND" = 1 ] && [ -z "$SUMDIR" ]; then
    for x in "$RES"/one-round-* "$RES"/run_batches-one-round-*; do
        [ -e "$x" ] && [ "$x" != "$SUM" ] || continue
        if unrestored "$x"; then echo "run_batches: keeping $x: its host idle state was not restored OK"; continue; fi
        if [ "$DRY" = 1 ]; then echo "run_batches: would remove earlier one-round run output $x"
        else echo "run_batches: removing earlier one-round run output $x"; rm -rf -- "$x"; fi
    done
fi

QD="$SUM/idle"; USER_RESTORED=0; USER_RESTORE_MSG=""
# print the user's active timers
user_timers() { "${USC[@]}" --user list-timers --no-legend --plain 2>/dev/null | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /\.timer$/) { print $i; break } }'; }
# test whether a user unit is active
user_active() { [ "$("${USC[@]}" --user is-active "$1" 2>/dev/null)" = active ]; }
# stop the user's active timers and audio, each recorded first
user_idle() {
    local us="$QD/user.state" u stopped=() bad=()
    printf 'user\t%s\n' "$(id -un)" > "$us"
    for u in $(user_timers) "${AUDIO_UNITS[@]}"; do
        user_active "$u" || continue
        printf 'uunit\t%s\n' "$u" >> "$us"
        if "${USC[@]}" --user stop "$u" >/dev/null 2>&1; then stopped+=("$u"); else printf 'failed\tuunit\t%s\n' "$u" >> "$us"; bad+=("$u"); fi
    done
    echo "run_batches: stopped as $(id -un): ${stopped[*]:-nothing}${bad[*]:+; could not stop: ${bad[*]}}"
}
# restart the user's units that user_idle stopped
user_restore() {
    [ "$USER_RESTORED" = 0 ] && [ -r "$QD/user.state" ] || return 0
    USER_RESTORED=1
    local kind a b ok=() bad=()
    while IFS=$'\t' read -r kind a b; do
        [ "$kind" = uunit ] || continue
        grep -qxF "$(printf 'failed\tuunit\t%s' "$a")" "$QD/user.state" && continue
        "${USC[@]}" --user start "$a" >/dev/null 2>&1; if user_active "$a"; then ok+=("$a"); else bad+=("$a"); fi
    done < "$QD/user.state"
    if [ "${#bad[@]}" = 0 ]; then USER_RESTORE_MSG="user units restarted by the run ($(date +%T)): OK -- ${ok[*]:-none to restart}"
    else USER_RESTORE_MSG="user units restarted by the run ($(date +%T)): FAILED for ${bad[*]} (${#ok[@]} ok); the root helper tries again"; fi
    echo "$USER_RESTORE_MSG" > "$QD/user-restore.txt"
    say "run_batches: $USER_RESTORE_MSG"
}
if [ "$IDLE" = 1 ] && [ "$LAUNCH" = 1 ]; then
    QCMD=(bash "$ROOT/benchmark/host/idle_machine.sh" start "$QD" "$SUM/run_batches.pid" --pins "$IDLE_PINS" --vm-cores "$IDLE_VM"
          --summary "$SUM/summary.txt" --log "$SUM/run_batches.log")
    if [ "$DRY" = 1 ]; then
        echo "+ ${QSUDO[*]} -n ${QCMD[*]}"
        bash benchmark/host/idle_machine.sh plan --pins "$IDLE_PINS" --vm-cores "$IDLE_VM" 2>&1 | sed 's/^/    /'
        uu=(); for u in $(user_timers) "${AUDIO_UNITS[@]}"; do user_active "$u" && uu+=("$u"); done
        echo "+ ${USC[*]} --user stop ${uu[*]:-<no active user timers or audio units>}"
    else
        mkdir -p "$QD" || { echo "run_batches: refusing -- cannot create $QD" >&2; exit 3; }
        trap 'echo "run_batches: interrupted before the run started; asking the root helper to restore the host ($QD/abort)" >&2; : > "$QD/abort"; user_restore; exit 130' INT TERM HUP
        "${QSUDO[@]}" -n "${QCMD[@]}" 9>&-; qrc=$?
        if [ "$qrc" != 0 ]; then
            echo "run_batches: refusing -- the host could not be idled; see $QD/helper.log, or pass --no-idle" >&2
            exit 3
        fi
        user_idle
        echo "run_batches: restore by hand: sudo bash benchmark/host/idle_machine.sh restore $QD/system.state, or reboot"
    fi
elif [ "$LAUNCH" = 1 ]; then
    echo "run_batches: host idling: skipped (--no-idle)"
fi

# print a detached one-round run's stages as they progress
follow_one_round() {
    local np="$1" log="$SUM/run_batches.log" n=0 total cur="" t0=$SECONDS tc=$SECONDS line stop_sent=0 i
    STOPREQ=0
    trap 'STOPREQ=1' INT
    echo "run_batches: one-round run is running as pid $np; ctrl+c stops it"
    while :; do
        local alive=0; run_alive "$np" && alive=1
        total="$(wc -l < "$log" 2>/dev/null || echo 0)"
        if [ "$total" -gt "$n" ]; then
            while IFS= read -r line; do
                case "$line" in
                    "== "*" ==")
                        [ -n "$cur" ] && echo "[$(date +%T)] one-round run: $cur finished after $(dur $((SECONDS - tc)))"
                        cur="${line#== }"; cur="${cur% ==}"; tc=$SECONDS
                        echo "[$(date +%T)] one-round run: $cur started" ;;
                    "    exit "[0-9]*) echo "[$(date +%T)] one-round run: ${cur:-preflight}: a step failed (${line#    }; see $log)" ;;
                    "run_batches: refusing"*|"run_batches: interrupted"*) echo "[$(date +%T)] one-round run: $line" ;;
                esac
            done < <(sed -n "$((n + 1)),${total}p" "$log")
            n="$total"
        fi
        [ "$alive" = 1 ] || break
        if [ "$STOPREQ" = 1 ] && [ "$stop_sent" = 0 ]; then
            stop_sent=1; echo; echo "[$(date +%T)] one-round run: ctrl+c -- stopping the one-round run with bash benchmark/batches/run_all.sh --stop $SUM"
            bash "$SELF" --stop "$SUM" 2>&1 | sed 's/^/    /' &
        fi
        sleep 1
    done
    trap - INT
    [ -n "$cur" ] && echo "[$(date +%T)] one-round run: $cur finished after $(dur $((SECONDS - tc)))"
    echo "[$(date +%T)] one-round run: the one-round run ended after $(dur $((SECONDS - t0)))$(o="$(grep -E '^run_batches: (all batches complete|[0-9]+ batch\(es\) need attention)' "$SUM/summary.txt" 2>/dev/null | tail -1)"; [ -n "$o" ] && echo " -- ${o#run_batches: }")"
    if [ "$(cat "$QD/apply.status" 2>/dev/null)" = applied ]; then
        echo "[$(date +%T)] one-round run: waiting for the root helper to put the host back ($QD/restore.txt)"
        for i in $(seq 600); do grep -q '^host restore' "$QD/restore.txt" 2>/dev/null && break; sleep 0.5; done
        tail -1 "$QD/restore.txt" 2>/dev/null | sed 's/^/    /'
    fi
    wait 2>/dev/null
    echo
    bash "$SELF" --check
}

if [ "$FG" = 0 ]; then
    RUN_CMD=(bash "$SELF" --foreground --summary-dir "$SUM" "${ARGS[@]}")
    WRAP=(env TEMPO_INHIBITED=1 "${INHIBIT[@]}")
    [ "$TESTRUN" = 1 ] && [ "${TEMPO_NO_INHIBIT:-}" = 1 ] && WRAP=()
    for i in "${!RUN_CMD[@]}"; do [ "${RUN_CMD[$i]}" = --dry-run ] && unset 'RUN_CMD[i]'; done
    if [ "$DRY" = 1 ]; then
        echo "+ setsid nohup ${WRAP[*]:+${WRAP[*]} }${RUN_CMD[*]} >> $SUM/run_batches.log 2>&1 < /dev/null &"
        echo "  then: watch with tail -f $SUM/run_batches.log; stop with bash benchmark/batches/run_all.sh --stop"
        echo "  the detached run then runs:"
    else
        exec 9>&-
        setsid nohup "${WRAP[@]}" "${RUN_CMD[@]}" >> "$SUM/run_batches.log" 2>&1 < /dev/null &
        for i in $(seq 60); do
            [ -s "$SUM/run_batches.pid" ] && break
            grep -q "^run_batches: refusing" "$SUM/run_batches.log" 2>/dev/null && break
            sleep 0.5
        done
        if [ ! -s "$SUM/run_batches.pid" ]; then
            echo "run_batches: the detached run did not start:" >&2; tail -20 "$SUM/run_batches.log" >&2
            if [ "$IDLE" = 1 ] && [ -d "$QD" ]; then
                : > "$QD/abort"; user_restore
                echo "run_batches: asked the root helper to restore the host ($QD/abort); else: sudo bash benchmark/host/idle_machine.sh restore $QD/system.state" >&2
            fi
            exit 3
        fi
        trap - INT TERM HUP
        NP="$(cat "$SUM/run_batches.pid")"
        if [ "$ATTACH" = 1 ]; then follow_one_round "$NP"; exit $?; fi
        echo "run_batches: running detached as pid $NP$([ "${#WRAP[@]}" -gt 0 ] && echo " under the sleep inhibitor")"
        echo "run_batches: watch it:  tail -f $SUM/run_batches.log"
        echo "run_batches: stop it:   bash benchmark/batches/run_all.sh --stop"
        echo "run_batches: summary:   $SUM/summary.txt"
        [ "$IDLE" = 1 ] && echo "run_batches: host:      idled; restore by hand: sudo bash benchmark/host/idle_machine.sh restore $QD/system.state"
        if [ "$ONE_ROUND" = 1 ]; then echo "run_batches: expected: about 20-60 min, mostly L1 VM setup"
        elif [ "${PLAN[*]}" = "r4-pilot r4" ]; then echo "run_batches: expected: about 3-4 h"
        elif levels2_planned; then echo "run_batches: expected: $LEVELS2_EST"
        elif r4b_planned; then echo "run_batches: expected: $R4B_EST"
        elif [ "${PLAN[*]}" = "ladder-pilot ladder-l0 ladder-l1 ladder-l2" ]; then echo "run_batches: expected: about 4.5-5.5 h"
        elif rhizo_only; then echo "run_batches: expected: $RHIZO_EST"
        else echo "run_batches: expected: about 4-5 h"; fi
        [ "$TESTRUN" = 1 ] && echo "run_batches: TEST RUN: batch runner '${PCB[*]}', results under $RES"
        if [ "$ONE_ROUND" = 0 ]; then
            echo "=================================================================="
            echo "TEMPO BATCHES ARE RUNNING in the background (started $(date +%H:%M); about $([ "${PLAN[*]}" = "r4-pilot r4" ] && echo 3-4 || { levels2_planned && echo 5.5-6; } || { r4b_planned && echo 8.5-9; } || { rhizo_only && echo "$RHIZO_H"; } || echo 4-5) h)"
            echo "Do NOT reboot or shut down. Leave this console logged in."
            echo "\`bash benchmark/batches/run_all.sh --check\` shows progress; it says OK only when everything is done."
            echo "=================================================================="
        fi
        exit 0
    fi
fi
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo -)"
HEAD_SHORT="$(git rev-parse --short HEAD 2>/dev/null || echo -)"
HDR=("mode: $([ "$ONE_ROUND" = 1 ] && echo "ONE-ROUND (1 round, tiny, one-round- names; verdicts meaningless)" || { [ "$REMEASURE" = 1 ] && echo "RE-MEASUREMENT at commit $HEAD_SHORT (the whole batch, $PFX names; not the pre-registered run)" || echo "real (pre-registered)"; })"
     "batches: ${PLAN[*]}"
     "commit: $HEAD_SHA"
     "kernel: $(uname -r)"
     "started: $(date '+%F %T %z')")
if [ "$REMEASURE" = 1 ]; then
    HDR+=("re-measurement: commit $HEAD_SHA, measured-code tree hash $RM_TREE; not the pre-registered run"
          "re-measurement outputs: $RES/$PFX*")
fi
IDLED=0; [ "$(cat "$QD/apply.status" 2>/dev/null)" = applied ] && IDLED=1
if [ "$IDLE" = 0 ]; then HDR+=("host: NOT idled (--no-idle)")
elif [ "$DRY" = 1 ]; then HDR+=("host: idled (benchmark/host/idle_machine.sh, above)")
elif [ "$IDLED" = 1 ]; then HDR+=("host: idled before the run (see 'host idling' below)")
else HDR+=("host: NOT idled ($QD/apply.status does not say applied)"); fi
[ "$SKIP_PF" = 1 ] && HDR+=("preflight: SKIPPED (test hook)")
[ "$TESTRUN" = 1 ] && HDR+=("TEST RUN: batch runner is '${PCB[*]}'$([ -n "${TEMPO_L1_CMD:-}" ] && echo "; L1 steps are '$TEMPO_L1_CMD'")")
if [ "$DRY" = 0 ]; then
    if [ "${TEMPO_NO_INHIBIT:-}" = 1 ]; then HDR+=("inhibitor: NONE (TEMPO_NO_INHIBIT=1, test run)")
    else HDR+=("inhibitor: held by pid $PPID ($(cat /proc/$PPID/comm 2>/dev/null))"); fi
    { printf '%s\n' "${HDR[@]}"; echo "top CPU consumers at start:"; ps -eo pcpu,comm --sort=-pcpu | head -6 | sed 's/^/  /'; } > "$SUM/header.txt"
fi
levels2_planned && [ "$ONE_ROUND" = 0 ] && HDR+=("$LEVELS2_EST")
r4b_planned && [ "$ONE_ROUND" = 0 ] && HDR+=("$R4B_EST")
r5_planned && [ "$ONE_ROUND" = 0 ] && HDR+=("$R5_EST")
r6_planned && [ "$ONE_ROUND" = 0 ] && HDR+=("$R6_EST")
if r7_planned && [ "$ONE_ROUND" = 0 ]; then
    if r7a_done; then HDR+=("$R7B_EST"); else HDR+=("$R7_EST"); fi
fi
if rhizo_planned && [ "$ONE_ROUND" = 0 ]; then HDR+=("$RHIZO_EST"); fi
for h in "${HDR[@]}"; do say "run_batches: $h"; done
[ "$DRY" = 1 ] || echo "$$" > "$SUM/run_batches.pid"
[ "$DRY" = 1 ] || [ "$REMEASURE" = 0 ] || printf '%s\n' "commit $HEAD_SHA" "measured-code tree $RM_TREE" "re-measurement, not the pre-registered run" > "$SUM/remeasure.txt"
[ "${#ONE_ROUND_ENV[@]}" -gt 0 ] && export "${ONE_ROUND_ENV[@]}"

declare -A STATUS=() LOGOF=() ATTN=() SCORED=() ASIDE=() ABORT1=() NORUN=() CRASHNOTE=()
SELBAD=0
R4BSELBAD=0
R5SELBAD=0
declare -A LSELBAD=([levels]=0 [levels2]=0)
R1_ABORT="FAILED (exit 5: paired.py stopped the batch after round 1 because a run in round 1 failed)"
# set a batch's STATUS, ATTN and ABORT1 from its exit code
batch_status() {
    if [ "$2" = 0 ]; then STATUS[$1]="DONE"
    elif [ "$2" = 5 ]; then STATUS[$1]="$R1_ABORT"; ABORT1[$1]=1; ATTN[$1]=1
    else STATUS[$1]="FAILED (exit $2${L1_WHY:+: $L1_WHY})"; ATTN[$1]=1; fi
}
# test whether a batch's JSON attempted all its rounds
finished() { python3 benchmark/batches/per_run_checks.py --attempted "$1" "${2:-$REPEATS}" >/dev/null 2>&1; }
CMDPFX=(); [ "${#ONE_ROUND_ENV[@]}" -gt 0 ] && CMDPFX=(env "${ONE_ROUND_ENV[@]}")

# move an interrupted batch and its checks and score aside
move_interrupted_batch() {
    local b="$1" log="$2" name="$PFX${NAME[$1]}" n; n="$(reps "$1")"
    local dir="$RES/$name.partial-$STAMP"
    # shellcheck disable=SC2046
    step "$log" bash benchmark/batches/move_interrupted_batch.sh "$RES" "$name" "$STAMP" "$n" $(labels "$b") || return 1
    if [ "$DRY" = 0 ] && [ -d "$dir" ]; then
        ASIDE[$b]="$dir"
        say "    set aside the interrupted earlier batch under $dir; running it from round 1, n=$n"
    fi
}

# run a batch on the host
host_batch() {
    local b="$1" name="$PFX${NAME[$1]}" lab n; lab="$(labels "$1")"; n="$(reps "$1")"
    local json="$RES/$name.json" log="$SUM/$b.log"
    LOGOF[$b]="$log"
    say ""; say "== $b ($name) =="
    [ "$DRY" = 1 ] || : > "$log"
    say "[$(date +%T)] + python3 benchmark/batches/per_run_checks.py --attempted $json $n"
    if finished "$json" "$n"; then
        STATUS[$b]="SKIPPED (finished earlier: all $n rounds attempted; never re-run)"
        say "    finished earlier, $([ "$DRY" = 1 ] && echo "would skip" || echo skipping): ${CMDPFX[*]:+${CMDPFX[*]} }${PCB[*]} $b"
    elif ! move_interrupted_batch "$b" "$log"; then
        STATUS[$b]="FAILED (could not set the interrupted earlier batch aside; not run, see $log)"; ATTN[$b]=1
        say "    set-aside failed: $b not run"
        return 0
    else
        step "$log" "${CMDPFX[@]}" "${PCB[@]}" "$b"; local rc=$?
        if [ "$DRY" = 1 ] && [ "$TESTRUN" = 0 ]; then
            batches_dry "$b"
        fi
        batch_status "$b" "$rc"
    fi
    step_to "$RES/$name.checks.txt" "$log" python3 benchmark/batches/per_run_checks.py "$json" $lab
    score_host "$b" "$json" "$log"
}

# write the scoring commit as the score file's first line
stamp_score() { [ "$DRY" = 1 ] || [ ! -e "$1" ] || sed -i "1i scored at $HEAD_SHORT" "$1"; }

SRC=""
# set SRC to the JSON to score
scoring_copy() {
    SRC="$1"
    [ -n "$PFX" ] || return 0
    SRC="$SUM/stripped-$(basename "$1")"
    step "$2" python3 benchmark/batches/per_run_checks.py --strip-prefix "$PFX" "$1" "$SRC"
}
# score a finished batch under its pre-registered rules
score_host() {
    local b="$1" json="$2" log="$3" name="$PFX${NAME[$1]}" rc=0
    if [ "$DRY" = 0 ] && ! finished "$json" "$(reps "$b")"; then
        say "    no verdict: $json did not run to the end, so it is not scored"
        return 0
    fi
    case "$b" in
        r1)
            scoring_copy "$json" "$log"
            step_to "$RES/$name.score.txt" "$log" python3 benchmark/score/single_level/r1_r3.py r1 "$SRC" || rc=1 ;;
        r2)
            scoring_copy "$json" "$log"
            step_to "$RES/$name.score.txt" "$log" python3 benchmark/score/nested/quality_shedding.py --paired "$SRC" || rc=1
            step_to "$SUM/r2-ratio.txt" "$log" python3 benchmark/batches/per_run_checks.py --r2-ratio "$SRC" || rc=1
            [ "$DRY" = 1 ] || cat "$SUM/r2-ratio.txt" >> "$RES/$name.score.txt" ;;
        ladder-pilot|ladder2-pilot)
            lv "$b"
            local lsel="$RES/${PFX}$LVP.selection" lsrc
            step "$log" python3 benchmark/score/single_level/levels_select.py --select "$json" --record "$lsel" $LVS ${PFX:+--prefix "$PFX"} $RB_RF $RB_LR; lsrc=$?
            if [ "$lsrc" = 0 ]; then SCORED[$b]=1
            elif [ "$lsrc" = 3 ]; then LSELBAD[$LVK]=1; ATTN[$b]=1; say "    recorded selection $lsel does not match the pilot; the levels are refused, see $log"
            else ATTN[$b]=1; say "    the levels selection rule could not be applied, see $log"; fi
            return 0 ;;
        r4b-pilot)
            local bsel="$RES/${PFX}pc-r4b-pilot.selection" bsrc
            step "$log" python3 benchmark/score/single_level/r4b.py --select "$json" --record "$bsel" ${PFX:+--prefix "$PFX"} $RB_RF; bsrc=$?
            if [ "$bsrc" = 0 ]; then SCORED[$b]=1
            elif [ "$bsrc" = 3 ]; then R4BSELBAD=1; ATTN[$b]=1; say "    recorded selection $bsel does not match the pilot; R4b is refused, see $log"
            else ATTN[$b]=1; say "    R4b's selection rule could not be applied, see $log"; fi
            return 0 ;;
        r4b)
            local l lrc rcs=0 out="$RES/$name.score.txt" ld lfiles=()
            r4b_labels
            [ "$DRY" = 1 ] || : > "$out.tmp"
            for l in a b; do
                [ "$l" = b ] && [[ " ${LABELS[r4b]} " != *" b-fixed-hi "* ]] && { say "    load B was not selected: not run, not scored"; [ "$DRY" = 1 ] || echo "load B: not run ($(grep -m1 '^not run: ' "$RES/${PFX}pc-r4b-pilot.selection" 2>/dev/null))" >> "$out.tmp"; lfiles+=(/dev/null); continue; }
                ld="$RES/$name-$l.arrived.json"
                step_to "$SUM/r4b-$l-arrival.txt" "$log" python3 benchmark/score/single_level/r4b.py --split "$json" "$l" "$ld" ${PFX:+--prefix "$PFX"}; lrc=$?
                [ "$DRY" = 1 ] || cat "$SUM/r4b-$l-arrival.txt" >> "$out.tmp"
                if [ "$lrc" = 0 ]; then
                    step_to "$SUM/r4b-$l-adapt.txt" "$log" python3 benchmark/score/nested/quality_shedding.py --paired "$ld" || rcs=1
                    step_to "$SUM/r4b-$l-ratio.txt" "$log" python3 benchmark/score/single_level/r4b.py --ratio "$l" "$ld" || rcs=1
                    [ "$DRY" = 1 ] || { cat "$SUM/r4b-$l-adapt.txt" "$SUM/r4b-$l-ratio.txt" > "$SUM/r4b-$l-score.txt"; cat "$SUM/r4b-$l-score.txt" >> "$out.tmp"; }
                    lfiles+=("$SUM/r4b-$l-score.txt")
                elif [ "$lrc" = 4 ]; then
                    [ "$ONE_ROUND" = 1 ] || ATTN[$b]=1
                    say "    R4b load ${l^^} VOID: too few rounds pass the arrival rule and the hit rule; not scored$([ "$ONE_ROUND" = 1 ] && echo "; expected in a one-round run")"
                    lfiles+=(/dev/null)
                else rcs=1; lfiles+=(/dev/null); fi
            done
            step_to "$SUM/r4b-cross.txt" "$log" python3 benchmark/score/single_level/r4b.py --cross "${lfiles[@]}" || rcs=1
            if [ "$DRY" = 0 ]; then
                cat "$SUM/r4b-cross.txt" >> "$out.tmp"; mv -f "$out.tmp" "$out"
                [ "$ONE_ROUND" = 1 ] && sed -i "1i ONE-ROUND: scored $json through its one-round- prefix; one round, verdicts meaningless" "$out"
            fi
            stamp_score "$out"
            if [ "$rcs" = 0 ]; then SCORED[$b]=1; else ATTN[$b]=1; say "    scoring $b failed, see $log"; fi
            return 0 ;;
        r5-pilot)
            local fsel="$RES/${PFX}pcn-r5-pilot.selection" fsrc
            step "$log" python3 benchmark/score/single_level/r5.py --select "$json" --record "$fsel" ${PFX:+--prefix "$PFX"} $RB_RF; fsrc=$?
            if [ "$fsrc" = 0 ]; then SCORED[$b]=1
            elif [ "$fsrc" = 3 ]; then R5SELBAD=1; ATTN[$b]=1; say "    recorded selection $fsel does not match the pilot; R5 is refused, see $log"
            else ATTN[$b]=1; say "    R5's selection rule could not be applied, see $log"; fi
            return 0 ;;
        r5)
            local frc rcs=0 out="$RES/$name.score.txt" ld="$RES/$name.arrived.json" fscore=/dev/null
            [ "$DRY" = 1 ] || : > "$out.tmp"
            step_to "$SUM/r5-arrival.txt" "$log" python3 benchmark/score/single_level/r5.py --split "$json" "$ld" ${PFX:+--prefix "$PFX"}; frc=$?
            [ "$DRY" = 1 ] || cat "$SUM/r5-arrival.txt" >> "$out.tmp"
            if [ "$frc" = 0 ]; then
                step_to "$SUM/r5-adapt.txt" "$log" python3 benchmark/score/nested/quality_shedding.py --paired "$ld" || rcs=1
                step_to "$SUM/r5-ratio.txt" "$log" python3 benchmark/score/single_level/r5.py --ratio "$ld" || rcs=1
                [ "$DRY" = 1 ] || { cat "$SUM/r5-adapt.txt" "$SUM/r5-ratio.txt" > "$SUM/r5-score.txt"; cat "$SUM/r5-score.txt" >> "$out.tmp"; }
                fscore="$SUM/r5-score.txt"
            elif [ "$frc" = 4 ]; then
                [ "$ONE_ROUND" = 1 ] || ATTN[$b]=1
                say "    R5 VOID: too few rounds pass the arrival rule and the hit rule; not scored$([ "$ONE_ROUND" = 1 ] && echo "; expected in a one-round run")"
            else rcs=1; fi
            step_to "$SUM/r5-cross.txt" "$log" python3 benchmark/score/single_level/r5.py --cross-level "$fscore" || rcs=1
            if [ "$DRY" = 0 ]; then
                cat "$SUM/r5-cross.txt" >> "$out.tmp"; mv -f "$out.tmp" "$out"
                [ "$ONE_ROUND" = 1 ] && sed -i "1i ONE-ROUND: scored $json through its one-round- prefix; one round, verdicts meaningless" "$out"
            fi
            stamp_score "$out"
            if [ "$rcs" = 0 ]; then SCORED[$b]=1; else ATTN[$b]=1; say "    scoring $b failed, see $log"; fi
            return 0 ;;
        r6-nested|r6-single|r7-a|r7-b)
            local lv=single sel=benchmark/score/single_level/r6.py; [ "$b" = r6-nested ] && lv=nested
            case "$b" in r7-a) lv=a; sel=benchmark/score/single_level/r7.py;; r7-b) lv=b; sel=benchmark/score/single_level/r7.py;; esac
            local arm tag frc rcs=0 out="$RES/$name.score.txt" ld rsfile=/dev/null
            [ "$DRY" = 1 ] || : > "$out.tmp"
            for arm in adaptive-rs adaptive; do
                tag=rs; [ "$arm" = adaptive ] && tag=old
                ld="$RES/$name-$tag.arrived.json"
                [ "$DRY" = 1 ] || echo "== $arm ($([ "$arm" = adaptive-rs ] && echo "R6's arm, scored" || echo "R4's arm, re-measured, reported, not a prediction")) ==" >> "$out.tmp"
                step_to "$SUM/$b-$tag-arrival.txt" "$log" python3 "$sel" --split "$json" "$lv" "$arm" "$ld" ${PFX:+--prefix "$PFX"}; frc=$?
                [ "$DRY" = 1 ] || cat "$SUM/$b-$tag-arrival.txt" >> "$out.tmp"
                if [ "$frc" = 0 ]; then
                    step_to "$SUM/$b-$tag-adapt.txt" "$log" python3 benchmark/score/nested/quality_shedding.py --paired "$ld" || rcs=1
                    step_to "$SUM/$b-$tag-ratio.txt" "$log" python3 "$sel" --ratio "$lv" "$arm" "$ld" || rcs=1
                    step_to "$SUM/$b-$tag-vsfh.txt" "$log" python3 "$sel" --vs-fixed-hi "$lv" "$arm" "$ld" || rcs=1
                    [ "$DRY" = 1 ] || cat "$SUM/$b-$tag-adapt.txt" "$SUM/$b-$tag-ratio.txt" "$SUM/$b-$tag-vsfh.txt" >> "$out.tmp"
                    [ "$arm" = adaptive-rs ] && { rsfile="$SUM/$b-rs-score.txt"; [ "$DRY" = 1 ] || cat "$SUM/$b-$tag-adapt.txt" "$SUM/$b-$tag-ratio.txt" > "$rsfile"; }
                elif [ "$frc" = 4 ]; then
                    if [ "$arm" = adaptive-rs ]; then
                        [ "$ONE_ROUND" = 1 ] || ATTN[$b]=1
                        say "    $([[ "$b" == r7-* ]] && echo R7 || echo R6) $lv VOID: too few rounds pass the arrival rule and the hit rule; not scored$([ "$ONE_ROUND" = 1 ] && echo "; expected in a one-round run")"
                    fi
                else rcs=1; fi
            done
            step_to "$SUM/$b-verdict.txt" "$log" python3 "$sel" --verdict "$lv" "$rsfile" || rcs=1
            if [ "$DRY" = 0 ]; then
                cat "$SUM/$b-verdict.txt" >> "$out.tmp"; mv -f "$out.tmp" "$out"
                [ "$ONE_ROUND" = 1 ] && sed -i "1i ONE-ROUND: scored $json through its one-round- prefix; one round, verdicts meaningless" "$out"
            fi
            stamp_score "$out"
            if [ "$rcs" = 0 ]; then SCORED[$b]=1; else ATTN[$b]=1; say "    scoring $b failed, see $log"; fi
            return 0 ;;
        rhizo|rhizo-nogpu|rhizo-drift|rhizo-drift-nogpu|rhizo-headroom)
            local gpu=on drift=""; [[ "$b" == *-nogpu ]] && gpu=off; [[ "$b" == rhizo-drift* ]] && drift=--drift
            scoring_copy "$json" "$log"
            if [ "$b" = rhizo-headroom ]; then step_to "$RES/$name.score.txt" "$log" python3 benchmark/score/rhizo/headroom.py "$SRC"; rc=$?
            else step_to "$RES/$name.score.txt" "$log" python3 benchmark/score/rhizo/score.py "$SRC" --gpu "$gpu" $drift; rc=$?; fi
            if [ "$rc" = 4 ] && grep -q '^VOID: run ' "$RES/$name.score.txt" 2>/dev/null; then
                rc=0; ATTN[$b]=1
                say "    the Rhizo batch is VOID: a run did not render as --gpu $gpu says, or ran at another slot, on another guest kernel or without its headroom sampler; not scored, see $RES/$name.score.txt"
            elif [ "$rc" = 4 ]; then
                rc=0; [ "$ONE_ROUND" = 1 ] || ATTN[$b]=1
                say "    the Rhizo batch is VOID: fewer than 12 rounds kept; not scored$([ "$ONE_ROUND" = 1 ] && echo "; expected in a one-round run")"
            elif [ "$rc" != 0 ]; then rc=1; fi ;;
        r4-pilot)
            local sel="$RES/${PFX}pc-r4-pilot.selection" src
            step "$log" python3 benchmark/score/single_level/r4.py --select "$json" --record "$sel" ${PFX:+--prefix "$PFX"} $RB_RF; src=$?
            if [ "$src" = 0 ]; then SCORED[$b]=1
            elif [ "$src" = 3 ]; then SELBAD=1; ATTN[$b]=1; say "    recorded selection $sel does not match the pilot; R4 is refused, see $log"
            else ATTN[$b]=1; say "    the selection rule could not be applied, see $log"; fi
            return 0 ;;
        r4)
            local arr="$RES/$name.arrived.json" arrival_rc
            step_to "$SUM/r4-arrival.txt" "$log" python3 benchmark/score/single_level/r4.py --arrival "$json" "$arr" ${PFX:+--prefix "$PFX"}; arrival_rc=$?
            if [ "$arrival_rc" = 0 ]; then
                step_to "$SUM/r4-adapt.txt" "$log" python3 benchmark/score/nested/quality_shedding.py --paired "$arr" || rc=1
                step_to "$SUM/r4-ratio.txt" "$log" python3 benchmark/score/single_level/r4.py --ratio "$arr" || rc=1
                [ "$DRY" = 1 ] || cat "$SUM/r4-arrival.txt" "$SUM/r4-adapt.txt" "$SUM/r4-ratio.txt" > "$RES/$name.score.txt"
            elif [ "$arrival_rc" = 4 ]; then
                ATTN[$b]=1; say "    R4 VOID: too few rounds pass the arrival rule and the hit rule; not scored"
                [ "$DRY" = 1 ] || cp "$SUM/r4-arrival.txt" "$RES/$name.score.txt"
            else rc=1; fi
            [ "$DRY" = 1 ] || { [ "$ONE_ROUND" = 1 ] && sed -i "1i ONE-ROUND: scored $json through its one-round- prefix; one round, verdicts meaningless" "$RES/$name.score.txt"; }
            stamp_score "$RES/$name.score.txt"
            if [ "$rc" = 0 ]; then SCORED[$b]=1; else ATTN[$b]=1; say "    scoring $b failed, see $log"; fi
            return 0 ;;
        *) return 0 ;;
    esac
    [ "$DRY" = 1 ] || { [ "$ONE_ROUND" = 1 ] && sed -i "1i ONE-ROUND: scored a prefix-stripped copy of $json; one round, verdicts meaningless" "$RES/$name.score.txt"; }
    stamp_score "$RES/$name.score.txt"
    if [ "$rc" = 0 ]; then SCORED[$b]=1; else ATTN[$b]=1; say "    scoring $b failed, see $log"; fi
}

# print a checks file's counts
checks_line() {
    awk 'NR > 1 && $1 !~ /^complete_rounds=/ { n++; if ($2 == "1" && $3 == "1") ex++; if ($5 == "1") cov++; if ($6 != "0" && $6 != "-") lost++
                                               if ($10 != "on") off++; if ($11 ~ /^[0-9]+$/ && $11 > 0) leftover++ }
         /^complete_rounds=/ { split($1, a, "="); cr = a[2] }
         END { printf "complete rounds %s; exclusive at both ends %d/%d runs; probe covers injection %d/%d runs; probe lost events in %d; probe not on in %d/%d runs; leftover ipi probes in %d/%d runs", (cr == "" ? "-" : cr), ex, n, cov, n, lost, off, n, leftover, n }' "$1" 2>/dev/null
}
# test whether any run's probe was not on or saw probes left from an earlier run
probe_trouble() {
    awk 'NR > 1 && $1 !~ /^complete_rounds=/ && ($10 != "on" || ($11 ~ /^[0-9]+$/ && $11 > 0)) { bad = 1 }
         END { exit !bad }' "$1" 2>/dev/null
}
# print the lines of a score file worth reading
verdicts() {
    grep -E '^(guard P7/P8|guard P6|not run|scored at|selected at|ONE-ROUND|R1a|R1b|R3a|complete|injection_confirmed|rt_max_higher|p_max|p_p999|nested_over_single_max|p|why|topology|R[135]: VOID)[: ]|^P[5-8] |^  (HIT|MISS) |round\(s\) void|no usable rounds|no data yet|^R2 ratio|^R4[: ]|^(arrival|duty [0-9]+([.][0-9]+)?|reported, not a rule):|^kept by both rules|^(selected_duty|selected_duty_a|selected_duty_b|one_round_forced)=|^load [AB]: (arrival|kept)|^R4b load|^R5 ratio|^cross-load|^cross-level|^R6 [a-z-]+ (verdict|adaptive-rs ratio|adaptive ratio|adaptive-rs: (arrival|kept)|adaptive-rs: VOID)|^R6 [a-z-]+: |^R7 [ab] (verdict|interpretation|adaptive-rs ratio|adaptive ratio|adaptive-rs: (arrival|kept)|adaptive-rs: VOID)|^R7 [ab]: |^reported, not scored: R7|^== adaptive|^reported, not scored: R6|^load [AB] guard|^no duty in the grid|^P[1-4] (HIT|MISS):|^H[12] (HIT|MISS):|^rounds kept|^VOID:|^reported:|^interpretation:|^not kept:' "$1" 2>/dev/null | sed 's/^/    /'
}
# print what was idled and how it is put back
idle_section() {
    if [ "$IDLE" = 0 ]; then
        echo "host idling: NONE (--no-idle)"
        return
    fi
    if [ "$IDLED" = 0 ]; then echo "host idling: NOT APPLIED (see $QD)"; return; fi
    if [ -s "$QD/restore.txt" ]; then
        ATTN[idle]=1
        echo "host idling: APPLIED but PUT BACK BEFORE THE RUN ENDED; later batches ran without it. Restore record:"
        sed 's/^/  /' "$QD/restore.txt"
    else
        echo "host idling: APPLIED before the run, identically for every batch"
    fi
    sed 's/^/  /' "$QD/apply.txt"
    echo "  stopped as $(awk -F'\t' '$1 == "user" { print $2 }' "$QD/user.state" 2>/dev/null): $(awk -F'\t' '$1 == "uunit" { printf "%s ", $2 }' "$QD/user.state" 2>/dev/null)"
    [ -n "$USER_RESTORE_MSG" ] && echo "  $USER_RESTORE_MSG"
    echo "  restore: root helper pid $(cat "$QD/helper.pid" 2>/dev/null); by hand: sudo bash benchmark/host/idle_machine.sh restore $QD/system.state, or reboot"
}
# write summary.txt, with a note for an interrupted exit
write_summary() {
  SUMMARY_DONE=1
  {
    cat "$SUM/header.txt"
    [ -n "${1:-}" ] && echo "$1"
    echo
    idle_section
    echo
    for b in "${PLAN[@]}"; do
        name="$PFX${NAME[$b]}"; n="$(reps "$b")"
        if [ -n "${NORUN[$b]:-}" ]; then
            echo "$b  $name  ${STATUS[$b]}  log: ${LOGOF[$b]:--}"
            continue
        fi
        if [ -z "${STATUS[$b]:-}" ] && ! finished "$RES/$name.json" "$n"; then
            ATTN[$b]=1
            echo "$b  $name  not run: the run ended before it started; re-run the same command"
            continue
        fi
        echo "$b  $name  ${STATUS[$b]:-not run in this run (finished earlier)}  log: ${LOGOF[$b]:--}"
        [ -n "${CRASHNOTE[$b]:-}" ] && echo "    ${CRASHNOTE[$b]}"
        [ -n "${ASIDE[$b]:-}" ] && echo "    an interrupted earlier batch was set aside at ${ASIDE[$b]}; this one ran from round 1, n=$n"
        # shellcheck disable=SC2046
        st="$(python3 benchmark/batches/per_run_checks.py --status "$RES/$name.json" "$n" $(labels "$b") 2>/dev/null)"
        at="$(sed -n 's/.*attempted=\([0-9]*\).*/\1/p' <<<"$st")"; fr="$(sed -n 's/.*failed_runs=\([0-9]*\).*/\1/p' <<<"$st")"
        if [ -n "${ABORT1[$b]:-}" ]; then
            ATTN[$b]=1
            echo "    STOPPED AFTER ROUND 1, paired.py exit 5: a round-1 run failed; fix it from ${LOGOF[$b]:-its log}, then re-run the same command"
        elif [ "${at:-0}" -ge "$n" ] && { [ "$b" = r4-pilot ] || [ "$b" = ladder-pilot ] || [ "$b" = ladder2-pilot ] || [ "$b" = r4b-pilot ] || [ "$b" = r5-pilot ]; }; then
            echo "    ran to the end: all $n rounds attempted, ${fr:-0} failed run(s); calibration, not scored"
        elif [ "${at:-0}" -ge "$n" ]; then
            echo "    ran to the end: all $n rounds attempted, ${fr:-0} failed run(s)"
        elif [ -z "${STATUS[$b]:-}" ] || [[ "${STATUS[$b]}" != "NOT RUN"* ]]; then
            ATTN[$b]=1
            echo "    INTERRUPTED after ${at:-0} of $n rounds; re-run the same command to finish it"
        fi
        echo "    $(checks_line "$RES/$name.checks.txt")   ($RES/$name.checks.txt)"
        if [[ " ladder-pilot ladder-l0 ladder-l1 ladder-l2 ladder2-pilot ladder2-l0 ladder2-l1 ladder2-l2 " == *" $b "* ]] && [ -s "$RES/$name.json" ]; then
            # shellcheck disable=SC2046
            cov="$(python3 benchmark/score/single_level/levels_select.py --coverage "$RES/$name.json" $(labels "$b") 2>/dev/null)"
            echo "    $(head -1 <<<"${cov:-window coverage could not be read}")"
            ck="$(sed -n '1s/.* in \([0-9]*\)\/\([0-9]*\) runs$/\1 \2/p' <<<"$cov")"
            if [ "$ONE_ROUND" = 1 ]; then
                bad_arms="$(awk 'NR > 1 && $2 ~ /^0\/[1-9]/ { printf "%s%s", s, $1; s = ", " }' <<<"$cov")"
                if [ -z "$ck" ] || [ "${ck#* }" = 0 ]; then
                    ATTN[$b]=1
                    echo "    ONE-ROUND NOT OK -- $b's window coverage could not be read or counts no run (${ck:-unreadable})"
                elif [ -n "$bad_arms" ]; then
                    ATTN[$b]=1
                    echo "    ONE-ROUND NOT OK -- $b's injection window covered the range in 0 of its runs for $bad_arms"
                fi
            fi
        fi
        if ! is_rhizo "$b" && probe_trouble "$RES/$name.checks.txt"; then
            ATTN[$b]=1
            echo "    PROBES NEED ATTENTION: an IPI probe was lost, off or unrecorded, or leftover probes ran; see the probe and leftover_ipi_probes columns"
        fi
        if [ -z "${ABORT1[$b]:-}" ] && [ "${at:-0}" -ge "$n" ]; then
            case "$b" in
                r1|r2|r4|r4b|r5|r6-nested|r6-single|r7-a|r7-b|rhizo|rhizo-nogpu|rhizo-drift|rhizo-drift-nogpu|rhizo-headroom) verdicts "$RES/$name.score.txt"; echo "    score: $RES/$name.score.txt";;
                r4-pilot|r4b-pilot|r5-pilot|ladder-pilot|ladder2-pilot) verdicts "$RES/$name.selection"; echo "    selection, not scored as a prediction: $RES/$name.selection";;
            esac
        elif [ -n "${ABORT1[$b]:-}" ]; then
            case "$b" in r1|r2|r4|r4b|r5|r6-nested|r6-single|r7-a|r7-b|rhizo|rhizo-nogpu|rhizo-drift|rhizo-drift-nogpu|rhizo-headroom|r4-pilot|r4b-pilot|r5-pilot|ladder-pilot|ladder2-pilot) echo "    no verdict: stopped after round 1";; esac
        elif [[ "${STATUS[$b]:-}" != "NOT RUN"* ]]; then
            case "$b" in r1|r2|r4|r4b|r5|r6-nested|r6-single|r7-a|r7-b|rhizo|rhizo-nogpu|rhizo-drift|rhizo-drift-nogpu|rhizo-headroom|r4-pilot|r4b-pilot|r5-pilot|ladder-pilot|ladder2-pilot) echo "    no verdict: interrupted after ${at:-0}/$n rounds";; esac
        fi
    done
    if planned r3-single || planned r3-nested; then
        echo "R3  ${PFX}pc-r3 vs ${PFX}pcn-r3  $([ -n "${SCORED[r3]:-}" ] && echo scored || echo "NOT SCORED")"
        [ -n "${SCORED[r3]:-}" ] && verdicts "$RES/${PFX}pc-r3.score.txt"
    fi
    for lk in levels levels2; do
        { [ "$lk" = levels ] && levels1_planned; } || { [ "$lk" = levels2 ] && levels2_planned; } || continue
        lv "$lk"; read -ra lrn <<<"$LVR"
        echo "$([ "$lk" = levels ] && echo LEVELS || echo LEVELS2)  ${PFX}${lrn[0]}, ${PFX}${lrn[1]}, ${PFX}${lrn[2]}  $([ -n "${SCORED[$lk]:-}" ] && echo scored || echo "NOT SCORED: needs all three levels finished")"
        [ -n "${SCORED[$lk]:-}" ] && grep -E '^(scored at|ONE-ROUND|L[012] .*VOID|LAD[1-4] |  guard LAD|guard (load|saturation) |  L[012]: the injection window|verdicts  )' "$RES/${PFX}$LVO.score.txt" 2>/dev/null | sed 's/^/    /'
        [ -n "${SCORED[$lk]:-}" ] && echo "    score: $RES/${PFX}$LVO.score.txt"
    done
    echo
    k=0; for b in "${PLAN[@]}" r3 levels levels2 idle; do [ -n "${ATTN[$b]:-}" ] && k=$((k + 1)); done
    echo "finished: $(date '+%F %T %z')"
    if [ "$k" = 0 ] && [ -z "${1:-}" ]; then echo "run_batches: all batches complete"; else echo "run_batches: $((k > 0 ? k : 1)) batch(es) need attention"; fi
  } > "$SUM/summary.txt"
  say ""; cat "$SUM/summary.txt"
}
VM_UP=0; SUMMARY_DONE=0; L1_WHY=""; L1_CRASH=""; NESTED_B=r3-nested
# power off the L1 VM
vm_down() {
    [ "$VM_UP" = 1 ] || return 0
    local log="$1"
    step "$log" timeout --foreground -k 5 30 "${SSH[@]}" sudo poweroff
    step "$log" bash benchmark/vm/stop.sh "$L1D/qemu.pid" 120 || { say "    the L1 VM did not stop -- check $L1D/qemu.pid"; ATTN[$NESTED_B]=1; }
    VM_UP=0
}
# power off the VM, restart the user's units and write the summary on exit
on_exit() {
    local rc=$?
    trap '' INT TERM HUP
    [ "$VM_UP" = 1 ] && { say "run_batches: exiting with the L1 VM up -- powering it off"; vm_down "${LOGOF[$NESTED_B]:-$SUM/$NESTED_B.log}"; }
    [ "$DRY" = 0 ] && [ "$IDLED" = 1 ] && user_restore
    [ "$DRY" = 0 ] && [ "$SUMMARY_DONE" = 0 ] && write_summary "INTERRUPTED (exit $rc) before the run finished; re-run the same command to finish it"
    return "$rc"
}
trap on_exit EXIT
trap 'say "run_batches: interrupted"; exit 130' INT TERM HUP

L1_READY_S=1500
# wait for ssh and cloud-init's ready mark in the L1 VM
wait_ssh() {
    local log="$1" t0=$SECONDS
    say "[$(date +%T)] + wait up to ${L1_READY_S}s for: ${SSH[*]} test -e /var/lib/cloud/instance/tempo-ready"
    [ "$DRY" = 1 ] && return 0
    while [ $((SECONDS - t0)) -lt "$L1_READY_S" ]; do
        timeout --foreground -k 5 30 "${SSH[@]}" test -e /var/lib/cloud/instance/tempo-ready >>"$log" 2>&1 && { say "    ssh up after $((SECONDS - t0))s"; return 0; }
        pgrep -F "$L1D/qemu.pid" >/dev/null 2>&1 || { say "    the L1 VM exited while booting; see $L1D/console.log"; return 1; }
        sleep 5
    done
    say "    no ssh or no ready mark after ${L1_READY_S}s"; return 1
}

# remove the run directories the host already holds from the L1 VM
vm_remove_copied() {
    local b="$1" log="$2" name="$PFX${NAME[$1]}" lst="$SUM/$1.vm-copied-home.txt" files="$SUM/$1.vm-files.txt" nd na nk lrc=0
    local -a rmd rma
    local listcmd="cd ~/tempo && python3 benchmark/vm/copied_home.py list results/experiments /tmp/"
    say "[$(date +%T)] + ${SSH[*]} $listcmd > $files"
    if [ "$DRY" = 0 ]; then
        echo "[$(date +%T)] + ${SSH[*]} $listcmd > $files" >> "$log"
        timeout --foreground -k 5 1800 "${SSH[@]}" "$listcmd" 9>&- > "$files" 2>> "$log"; lrc=$?
        [ "$lrc" = 0 ] && echo "    listed $(awk -F'\t' '$1 == "dir" { n++ } END { print n + 0 }' "$files") run directories and $(awk -F'\t' '$1 == "archive" { n++ } END { print n + 0 }' "$files") archives in the L1 VM, each file's sha256 in $files" >> "$log"
    fi
    # shellcheck disable=SC2046
    { [ "$lrc" = 0 ] && step_to "$lst" "$log" python3 benchmark/vm/copied_home.py compare "$files" "$RES" "$name" $(labels "$b"); } \
        || { say "    could not list the L1 VM's run data against the host; nothing removed, $b still runs, see $log"; ATTN[$b]=1; return 0; }
    if [ "$DRY" = 1 ]; then
        say "[$(date +%T)] + ${SSH[*]} cd ~/tempo/results/experiments && rm -rf -- <each run directory $lst says to remove> && rm -f -- /tmp/<each archive it says to remove>"
        return 0
    fi
    mapfile -t rmd < <(sed -n 's/^remove dir \([^ ]*\)$/\1/p' "$lst")
    mapfile -t rma < <(sed -n 's/^remove archive \([^ ]*\) .*/\/tmp\/\1/p' "$lst")
    nd="${#rmd[@]}"; na="${#rma[@]}"; nk="$(grep -c '^keep ' "$lst")"
    if [ $((nd + na)) -gt 0 ]; then
        step "$log" timeout --foreground -k 5 600 "${SSH[@]}" "cd ~/tempo/results/experiments && rm -rf -- ${rmd[*]} && rm -f -- ${rma[*]}; rc=\$?; df -h ~ | tail -1; exit \$rc" \
            || { say "    removing the copied-home run data from the L1 VM failed, see $log"; ATTN[$b]=1; return 0; }
    fi
    say "    freed the L1 VM's disk before $b: removed $nd run director$([ "$nd" = 1 ] && echo y || echo ies) and $na archive$([ "$na" = 1 ] || echo s) already copied home, kept $nk; list: $lst"
}
# run a batch inside the L1 VM and copy its data home
l1_steps() {
    local b="$1" log="$2" name="$PFX${NAME[$1]}" lab n; lab="$(labels "$1")"; n="$(reps "$1")"
    local rj="results/experiments/$name.json" sha="$HEAD_SHA" rc
    local tag=r3n; [ "$b" = ladder-l2 ] && tag=ld2; [ "$b" = ladder2-l2 ] && tag=lb2; [ "$b" = r5-pilot ] && tag=r5p; [ "$b" = r5 ] && tag=r5; [ "$b" = r6-nested ] && tag=r6n; [ "$b" = r7-a ] && tag=r7a; [ "$b" = r7-b ] && tag=r7b
    local vmlog=build/run_batches-$tag.log vmrc=build/run_batches-$tag.rc envs="${ONE_ROUND_ENV[*]}"
    L1_WHY=""
    VM_UP=1
    step "$log" timeout --foreground -k 5 120 "${L1BOOT[@]}" || return 1
    wait_ssh "$log" || return 1
    step "$log" git bundle create "$SUM/l1-head.bundle" HEAD || return 1
    step "$log" timeout --foreground -k 5 600 "${SCP[@]}" "$SUM/l1-head.bundle" tempo@127.0.0.1:/tmp/tempo-head.bundle || return 1
    step "$log" timeout --foreground -k 5 600 "${SCP[@]}" benchmark/vm/sync.sh tempo@127.0.0.1:/tmp/vm_sync.sh || return 1
    step "$log" timeout --foreground -k 5 600 "${SSH[@]}" "bash /tmp/vm_sync.sh /tmp/tempo-head.bundle $sha ~/tempo" || return 1
    [ "$ONE_ROUND" = 1 ] && { step "$log" timeout --foreground -k 5 60 "${SSH[@]}" "cd ~/tempo && ls -d results/experiments/one-round-* 2>/dev/null; rm -rf results/experiments/one-round-*" || return 1; }
    vm_remove_copied "$b" "$log"
    if [ "$b" = ladder-l2 ] || [ "$b" = ladder2-l2 ]; then
        lv "$b"
        step "$log" timeout --foreground -k 5 60 "${SSH[@]}" "mkdir -p tempo/results/experiments" || return 1
        step "$log" timeout --foreground -k 5 600 "${SCP[@]}" "$RES/${PFX}$LVP.selection" "$RES/${PFX}$LVP.json" tempo@127.0.0.1:tempo/results/experiments/ || return 1
    fi
    if [ "$b" = r5 ]; then
        step "$log" timeout --foreground -k 5 60 "${SSH[@]}" "mkdir -p tempo/results/experiments" || return 1
        step "$log" timeout --foreground -k 5 600 "${SCP[@]}" "$RES/${PFX}pcn-r5-pilot.selection" "$RES/${PFX}pcn-r5-pilot.json" tempo@127.0.0.1:tempo/results/experiments/ || return 1
    fi
    step "$log" timeout --foreground -k 5 180 "${SSH[@]}" "sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service && sudo systemctl stop apt-daily.service apt-daily-upgrade.service; rc=\$?; echo \"active: \$(systemctl is-active apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service apt-daily.service apt-daily-upgrade.service | paste -sd' ' -)\"; exit \$rc" \
        || { say "    could not stop apt inside the L1 VM; the batch still runs, see $log"; ATTN[$b]=1; }
    local hk vk
    hk="$([ "$DRY" = 1 ] || sha256sum build/kernel-nonrt/vmlinuz | cut -d' ' -f1)"
    say "[$(date +%T)] + ${SSH[*]} sha256sum tempo/build/kernel-nonrt/vmlinuz"
    vk="$([ "$DRY" = 1 ] || timeout --foreground -k 5 60 "${SSH[@]}" sha256sum tempo/build/kernel-nonrt/vmlinuz 2>/dev/null | cut -d' ' -f1)"
    if [ "$DRY" = 1 ] || [ "$hk" != "$vk" ]; then
        step "$log" timeout --foreground -k 5 60 "${SSH[@]}" mkdir -p tempo/build/kernel-nonrt || return 1
        step "$log" timeout --foreground -k 5 600 "${SCP[@]}" build/kernel-nonrt/vmlinuz tempo@127.0.0.1:tempo/build/kernel-nonrt/vmlinuz || return 1
    fi
    step "$log" timeout --foreground -k 5 3600 "${SSH[@]}" "cd ~/tempo && nice bash tools/fetch/dependencies.sh && nice make && nice bash tools/build/make_image.sh" || return 1
    step "$log" timeout --foreground -k 5 120 "${SSH[@]}" "cd ~/tempo && bash benchmark/probes/probe_pid.sh" || { L1_WHY="benchmark/probes/probe_pid.sh failed inside the L1 VM; $b not run"; say "    $L1_WHY"; return 1; }
    local brc=0
    say "[$(date +%T)] + ${SSH[*]} cd ~/tempo && python3 benchmark/batches/per_run_checks.py --attempted $rj $n"
    if [ "$DRY" = 0 ] && timeout --foreground -k 5 60 "${SSH[@]}" "cd ~/tempo && python3 benchmark/batches/per_run_checks.py --attempted $rj $n" >>"$log" 2>&1; then
        say "    finished earlier inside the VM: not re-run, copying it home"
    else
        step "$log" timeout --foreground -k 5 60 "${SSH[@]}" "cd ~/tempo && bash benchmark/batches/move_interrupted_batch.sh results/experiments $name $STAMP-vm${3:-} $n $lab" || return 1
        step "$log" timeout --foreground -k 5 60 "${SSH[@]}" "cd ~/tempo && rm -f $vmrc && { setsid nohup bash -c '$envs bash benchmark/batches/run_one.sh $b > $vmlog 2>&1; echo \$? > $vmrc' </dev/null >/dev/null 2>&1 & }" || return 1
        say "[$(date +%T)] + poll every ${L1_POLL_S}s, up to ${L1_BATCH_S}s: ${SSH[*]} cat tempo/$vmrc"
        if [ "$DRY" = 0 ]; then
            local t0=$SECONDS got="" out lastok=$SECONDS
            while [ $((SECONDS - t0)) -lt "$L1_BATCH_S" ]; do
                sleep "$L1_POLL_S"
                out="$(timeout --foreground -k 5 60 "${SSH[@]}" "cat tempo/$vmrc 2>/dev/null; echo :reachable" 2>/dev/null)"
                if [[ "$out" == *:reachable* ]]; then lastok=$SECONDS; got="$(tr -dc '0-9' <<<"${out%%:reachable*}")"; else got=""; fi
                [ -n "$got" ] && break
                if ! pgrep -F "$L1D/qemu.pid" >/dev/null 2>&1; then L1_CRASH="its qemu process exited"
                elif grep -qE 'Kernel panic|Oops: ' "$L1D/console.log" 2>/dev/null; then L1_CRASH="its console shows a kernel panic or oops"
                elif [ $((SECONDS - lastok)) -gt "$L1_DOWN_S" ]; then L1_CRASH="ssh unreachable for more than ${L1_DOWN_S}s"
                fi
                if [ -n "$L1_CRASH" ]; then
                    say "    the L1 VM crashed during $b ($L1_CRASH) after $((SECONDS - t0))s"
                    return 75
                fi
            done
            if [ -n "$got" ]; then brc="$got"; say "    $b exited $brc inside the VM after $((SECONDS - t0))s"
            else brc=124; say "    $b did not finish: cap ${L1_BATCH_S}s reached or VM gone"; fi
            timeout --foreground -k 5 600 "${SCP[@]}" "tempo@127.0.0.1:tempo/$vmlog" "$SUM/$b.vm.log" >>"$log" 2>&1 \
                && { echo "---- VM-side batch log ----"; cat "$SUM/$b.vm.log"; } >> "$log"
        fi
    fi
    local globs=" $name.partial-*" l; for l in $lab; do globs+=" $l-r[0-9]*"; done
    step "$log" timeout --foreground -k 5 600 "${SSH[@]}" "cd ~/tempo && python3 benchmark/batches/per_run_checks.py $rj $lab > results/experiments/$name.checks.txt; cd results/experiments && tar --sparse -czf /tmp/$name-runs.tgz \$(ls -d $globs 2>/dev/null)" || { say "    packing the $b run directories inside the L1 VM failed, see $log"; ATTN[$b]=1; }
    step "$log" timeout --foreground -k 5 600 "${SCP[@]}" "tempo@127.0.0.1:tempo/$rj" "$SUM/$name.json.incoming" || return 1
    step "$log" mv "$SUM/$name.json.incoming" "$RES/$name.json" || return 1
    step "$log" timeout --foreground -k 5 600 "${SCP[@]}" "tempo@127.0.0.1:tempo/results/experiments/$name.checks.txt" "$RES/$name.checks.txt"
    if step "$log" timeout --foreground -k 5 1800 "${SCP[@]}" "tempo@127.0.0.1:/tmp/$name-runs.tgz" "$SUM/$name-runs.tgz"; then
        step "$log" tar -C "$RES" -xzf "$SUM/$name-runs.tgz" || { say "    unpacking the $b run directories failed, see $log"; ATTN[$b]=1; }
    else
        say "    copying the $b run directories home failed, see $log"; ATTN[$b]=1
    fi
    return "$brc"
}

# keep the console of the boot just ended
save_console() {
    step "$2" cp "$L1D/console.log" "$SUM/$1.console$3.log" || say "    no console log to save for $1"
}
# run a batch inside the L1 VM
nested_batch() {
    local b="$1" name="$PFX${NAME[$1]}" lab n; lab="$(labels "$1")"; n="$(reps "$1")"
    local json="$RES/$name.json" log="$SUM/$1.log" rc
    NESTED_B="$b"
    LOGOF[$b]="$log"
    say ""; say "== $b ($name, inside the L1 VM) =="
    [ "$DRY" = 1 ] || : > "$log"
    [ "$DRY" = 1 ] && [ "$REMEASURE" = 1 ] && [ "$TESTRUN" = 0 ] && batches_dry "$b"
    say "[$(date +%T)] + python3 benchmark/batches/per_run_checks.py --attempted $json $n"
    if finished "$json" "$n"; then
        STATUS[$b]="SKIPPED (finished earlier: all $n rounds attempted; never re-run)"
        say "    finished earlier on the host, $([ "$DRY" = 1 ] && echo "would skip" || echo skipping): bash benchmark/vm/run.sh --background ... ${CMDPFX[*]:+${CMDPFX[*]} }bash benchmark/batches/run_one.sh $b ... sudo poweroff"
        return 0
    fi
    if ! move_interrupted_batch "$b" "$log"; then
        STATUS[$b]="FAILED (could not set the interrupted earlier batch aside; not run, see $log)"; ATTN[$b]=1
        say "    set-aside failed: $b not run"
        return 0
    fi
    if [ "$TESTRUN" = 1 ] && [ -n "${TEMPO_L1_CMD:-}" ]; then
        TEMPO_L1_BATCH="$b" step "$log" bash -c "$TEMPO_L1_CMD"; rc=$?
    else
        local att="" first=""
        while :; do
            L1_CRASH=""
            l1_steps "$b" "$log" "$att"; rc=$?
            if [ -n "$L1_CRASH" ]; then
                step "$log" bash benchmark/vm/stop.sh "$L1D/qemu.pid" 5 || { say "    the crashed L1 VM did not stop -- check $L1D/qemu.pid"; ATTN[$b]=1; }
                VM_UP=0
            else
                vm_down "$log"
            fi
            save_console "$b" "$log" "${att:+-retry}"
            [ -z "$L1_CRASH" ] && break
            if [ -z "$att" ]; then
                first="$L1_CRASH"; att="-retry"
                CRASHNOTE[$b]="the L1 VM crashed on the first attempt ($first; console: $SUM/$b.console.log); retried from round 1"
                say "    retrying $b once from round 1 in a new L1 VM boot"
                move_interrupted_batch "$b" "$log" || true
                continue
            fi
            STATUS[$b]="not run: the L1 VM crashed twice (first: $first; second: $L1_CRASH; consoles: $SUM/$b.console.log, $SUM/$b.console-retry.log)"
            ATTN[$b]=1; NORUN[$b]=1
            say "    $b not run: the L1 VM crashed twice"
            return 0
        done
    fi
    batch_status "$b" "$rc"
    if [ "$DRY" = 1 ] || [ ! -s "$RES/$name.checks.txt" ] || [ "$json" -nt "$RES/$name.checks.txt" ]; then
        step_to "$RES/$name.checks.txt" "$log" python3 benchmark/batches/per_run_checks.py "$json" $lab
    fi
}

# run R4 at the duty its pilot selected, or record why it is not run
r4_batch() {
    local b=r4 name="${PFX}pc-r4" lab; lab="$(labels r4)"
    local json="$RES/$name.json" log="$SUM/r4.log" pj="$RES/${PFX}pc-r4-pilot.json"
    local sel="$RES/${PFX}pc-r4-pilot.selection" d
    LOGOF[$b]="$log"
    say ""; say "== $b ($name, at the duty the pilot selected) =="
    [ "$DRY" = 1 ] || : > "$log"
    say "[$(date +%T)] + python3 benchmark/batches/per_run_checks.py --attempted $json $REPEATS"
    if finished "$json"; then
        STATUS[$b]="SKIPPED (finished earlier: all $REPEATS rounds attempted; never re-run)"
        say "    finished earlier, $([ "$DRY" = 1 ] && echo "would skip" || echo skipping): ${CMDPFX[*]:+${CMDPFX[*]} }${PCB[*]} $b"
    else
        if [ "$DRY" = 0 ]; then
            if ! finished "$pj" "$PILOT_REPEATS"; then
                STATUS[$b]="NOT RUN (pilot $pj did not finish); re-run the same command"; ATTN[$b]=1; NORUN[$b]=1
                say "    not run: the pilot did not run to the end"; return 0
            fi
            if [ "$SELBAD" = 1 ]; then
                STATUS[$b]="FAILED (recorded selection $sel does not match the pilot; R4 refused; see ${LOGOF[r4-pilot]:-the r4-pilot log})"; ATTN[$b]=1; NORUN[$b]=1
                say "    not run: the recorded selection does not match the pilot"; return 0
            fi
            d="$(sed -n 's/^selected_duty=//p' "$sel" 2>/dev/null | tail -1)"
            if [ "$d" = none ]; then
                why="$(grep -m1 '^not run: ' "$sel" 2>/dev/null)"
                STATUS[$b]="NOT RUN, as pre-registered: ${why#not run: } ($sel)"
                NORUN[$b]=1; say "    not run: the pilot selected no duty ($sel)"; return 0
            fi
            if [ -z "$d" ]; then
                STATUS[$b]="FAILED (no selection recorded at $sel; not run)"; ATTN[$b]=1; NORUN[$b]=1
                say "    not run: no selection at $sel"; return 0
            fi
            say "    the pilot selected duty $d ($sel)"
        fi
        if [ -z "$PFX" ] && ! step "$log" python3 benchmark/score/single_level/r4.py --keep-r2-runs "$RES"; then
            STATUS[$b]="FAILED (could not keep R2's run directories aside; not run, see $log)"; ATTN[$b]=1; NORUN[$b]=1
            say "    keeping R2's run directories failed: $b not run"; return 0
        fi
        if ! move_interrupted_batch "$b" "$log"; then
            STATUS[$b]="FAILED (could not set the interrupted earlier batch aside; not run, see $log)"; ATTN[$b]=1; NORUN[$b]=1
            say "    set-aside failed: $b not run"; return 0
        fi
        step "$log" "${CMDPFX[@]}" "${PCB[@]}" "$b"; local rc=$?
        if [ "$DRY" = 1 ] && [ "$TESTRUN" = 0 ]; then
            batches_dry "$b"
        fi
        batch_status "$b" "$rc"
    fi
    step_to "$RES/$name.checks.txt" "$log" python3 benchmark/batches/per_run_checks.py "$json" $lab
    score_host "$b" "$json" "$log"
}

# run R4b at the loads its pilot selected, or record why it is not run
r4b_batch() {
    local b=r4b name="${PFX}pc-r4b" lab
    local json="$RES/$name.json" log="$SUM/r4b.log" pj="$RES/${PFX}pc-r4b-pilot.json"
    local sel="$RES/${PFX}pc-r4b-pilot.selection" da db why
    LOGOF[$b]="$log"
    say ""; say "== $b ($name, at the loads the pilot selected) =="
    [ "$DRY" = 1 ] || : > "$log"
    say "[$(date +%T)] + python3 benchmark/batches/per_run_checks.py --attempted $json $REPEATS"
    if finished "$json"; then
        STATUS[$b]="SKIPPED (finished earlier: all $REPEATS rounds attempted; never re-run)"
        say "    finished earlier, $([ "$DRY" = 1 ] && echo "would skip" || echo skipping): ${CMDPFX[*]:+${CMDPFX[*]} }${PCB[*]} $b"
    else
        if [ "$DRY" = 0 ]; then
            if ! finished "$pj" "$R4B_PILOT_REPEATS"; then
                STATUS[$b]="NOT RUN (pilot $pj did not finish); re-run the same command"; ATTN[$b]=1; NORUN[$b]=1
                say "    not run: the pilot did not run to the end"; return 0
            fi
            if [ "$R4BSELBAD" = 1 ]; then
                STATUS[$b]="FAILED (recorded selection $sel does not match the pilot; R4b refused; see ${LOGOF[r4b-pilot]:-the r4b-pilot log})"; ATTN[$b]=1; NORUN[$b]=1
                say "    not run: the recorded selection does not match the pilot"; return 0
            fi
            da="$(sed -n 's/^selected_duty_a=//p' "$sel" 2>/dev/null | tail -1)"
            db="$(sed -n 's/^selected_duty_b=//p' "$sel" 2>/dev/null | tail -1)"
            if [ "$da" = none ]; then
                why="$(grep -m1 '^not run: ' "$sel" 2>/dev/null)"
                STATUS[$b]="NOT RUN, as pre-registered: ${why#not run: } ($sel)"
                NORUN[$b]=1; say "    not run: the pilot selected no load A ($sel)"; return 0
            fi
            if [ -z "$da" ]; then
                STATUS[$b]="FAILED (no selection recorded at $sel; not run)"; ATTN[$b]=1; NORUN[$b]=1
                say "    not run: no selection at $sel"; return 0
            fi
            say "    the pilot selected load A at duty $da and load B at ${db:-none} ($sel)"
        fi
        r4b_labels
        if ! move_interrupted_batch "$b" "$log"; then
            STATUS[$b]="FAILED (could not set the interrupted earlier batch aside; not run, see $log)"; ATTN[$b]=1; NORUN[$b]=1
            say "    set-aside failed: $b not run"; return 0
        fi
        step "$log" "${CMDPFX[@]}" "${PCB[@]}" "$b"; local rc=$?
        if [ "$DRY" = 1 ] && [ "$TESTRUN" = 0 ]; then
            batches_dry "$b"
        fi
        batch_status "$b" "$rc"
    fi
    r4b_labels; lab="$(labels r4b)"
    step_to "$RES/$name.checks.txt" "$log" python3 benchmark/batches/per_run_checks.py "$json" $lab
    score_host "$b" "$json" "$log"
}

# run R5's pilot inside the L1 VM and record its selection on the host
r5_pilot_batch() {
    nested_batch r5-pilot
    score_host r5-pilot "$RES/${PFX}pcn-r5-pilot.json" "${LOGOF[r5-pilot]}"
}
# run R5 at the duty its pilot selected, or record why it is not run
r5_batch() {
    local b=r5 name="${PFX}pcn-r5" pj="$RES/${PFX}pcn-r5-pilot.json"
    local sel="$RES/${PFX}pcn-r5-pilot.selection" d why
    if [ "$DRY" = 0 ] && ! finished "$RES/$name.json"; then
        if ! finished "$pj" "$R5_PILOT_REPEATS"; then
            STATUS[$b]="NOT RUN (pilot $pj did not finish); re-run the same command"; ATTN[$b]=1; NORUN[$b]=1
            say ""; say "== $b: not run -- the pilot did not run to the end =="; return 0
        fi
        if [ "$R5SELBAD" = 1 ]; then
            STATUS[$b]="FAILED (recorded selection $sel does not match the pilot; R5 refused; see ${LOGOF[r5-pilot]:-the r5-pilot log})"; ATTN[$b]=1; NORUN[$b]=1
            say ""; say "== $b: not run -- the recorded selection does not match the pilot =="; return 0
        fi
        d="$(sed -n 's/^selected_duty=//p' "$sel" 2>/dev/null | tail -1)"
        if [ "$d" = none ]; then
            why="$(grep -m1 '^not run: ' "$sel" 2>/dev/null)"
            STATUS[$b]="NOT RUN, as pre-registered: ${why#not run: } ($sel)"; NORUN[$b]=1
            say ""; say "== $b: not run, the pilot selected no duty =="; return 0
        fi
        if [ -z "$d" ]; then
            STATUS[$b]="FAILED (no selection recorded at $sel; not run)"; ATTN[$b]=1; NORUN[$b]=1
            say ""; say "== $b: not run -- no selection at $sel =="; return 0
        fi
    fi
    nested_batch r5
    score_host r5 "$RES/$name.json" "${LOGOF[r5]}"
}

# score R3 once both halves exist
score_r3() {
    local s="$RES/${PFX}pc-r3.json" n="$RES/${PFX}pcn-r3.json" log="$SUM/r3.log" ss sn
    say ""; say "== R3 score =="
    if [ "$DRY" = 0 ] && { ! finished "$s" || ! finished "$n"; }; then
        say "    R3 not scored: $(finished "$s" || echo "$s ")$(finished "$n" || echo "$n ")did not run to the end"
        { planned r3-single || planned r3-nested; } && ATTN[r3]=1
        return 0
    fi
    [ "$DRY" = 1 ] || : > "$log"
    scoring_copy "$s" "$log"; ss="$SRC"; scoring_copy "$n" "$log"; sn="$SRC"
    if step_to "$RES/${PFX}pc-r3.score.txt" "$log" python3 benchmark/score/single_level/r1_r3.py r3 "$ss" "$sn"; then
        SCORED[r3]=1
        [ "$DRY" = 1 ] || { [ "$ONE_ROUND" = 1 ] && sed -i "1i ONE-ROUND: scored prefix-stripped copies of $s and $n; one round, verdicts meaningless" "$RES/${PFX}pc-r3.score.txt"; }
        stamp_score "$RES/${PFX}pc-r3.score.txt"
    else ATTN[r3]=1; fi
}

# test whether a level may run at its pilot's recorded selection
levels_may_run() {
    lv "$1"
    local b="$1" pj="$RES/${PFX}$LVP.json" sel="$RES/${PFX}$LVP.selection" d why k="$LVK" doc="$LVD"
    [ "$DRY" = 1 ] && return 0
    finished "$RES/$PFX${NAME[$b]}.json" && return 0
    if ! finished "$pj" "$PILOT_REPEATS"; then
        STATUS[$b]="NOT RUN (pilot $pj did not finish); re-run the same command"; ATTN[$b]=1; NORUN[$b]=1
        say ""; say "== $b: not run -- the pilot did not run to the end =="; return 1
    fi
    if [ "${LSELBAD[$k]}" = 1 ]; then
        STATUS[$b]="FAILED (recorded selection $sel does not match the pilot; level refused; see ${LOGOF[$k-pilot]:-the $k-pilot log})"; ATTN[$b]=1; NORUN[$b]=1
        say ""; say "== $b: not run -- the recorded selection does not match the pilot =="; return 1
    fi
    d="$(sed -n 's/^selected_duty=//p' "$sel" 2>/dev/null | tail -1)"
    if [ "$d" = none ]; then
        why="$(grep -m1 '^not run: ' "$sel" 2>/dev/null)"
        STATUS[$b]="NOT RUN, as pre-registered: ${why#not run: } ($doc; $sel)"; NORUN[$b]=1
        say ""; say "== $b: not run, the pilot selected no duty =="; return 1
    fi
    if [ -z "$d" ]; then
        STATUS[$b]="FAILED (no selection recorded at $sel; not run)"; ATTN[$b]=1; NORUN[$b]=1
        say ""; say "== $b: not run -- no selection at $sel =="; return 1
    fi
    return 0
}

# score a levels comparison once all three levels ran to the end
score_levels() {
    local key="$1"; lv "$key"; set -- $LVR
    local a="$RES/${PFX}$1.json" b="$RES/${PFX}$2.json" c="$RES/${PFX}$3.json"
    local log="$SUM/$key.log" out="$RES/${PFX}$LVO.score.txt" rc d vf="$LVS"
    d="$(sed -n 's/^selected_duty=//p' "$RES/${PFX}$LVP.selection" 2>/dev/null | tail -1)"
    say ""; say "== $key score =="
    if [ "$DRY" = 0 ] && [ "$d" = none ]; then
        say "    levels not scored: the pilot selected no duty, so no level ran"
        return 0
    fi
    [ "$DRY" = 1 ] && [ -z "$d" ] && d="<duty>"
    if [ "$DRY" = 0 ] && { ! finished "$a" || ! finished "$b" || ! finished "$c"; }; then
        say "    levels not scored: $(for f in "$a" "$b" "$c"; do finished "$f" || printf '%s ' "$f"; done)did not run to the end"
        ATTN[$key]=1
        return 0
    fi
    [ "$DRY" = 1 ] || : > "$log"
    step_to "$out" "$log" python3 benchmark/score/single_level/levels.py "$a" "$b" "$c" --duty "$d" $vf ${PFX:+--prefix "$PFX"} $RB_LR; rc=$?
    if [ "$rc" = 0 ]; then SCORED[$key]=1
    elif [ "$rc" = 4 ] && [ "$ONE_ROUND" = 1 ]; then SCORED[$key]=1; say "    the one-round run's levels are VOID, as expected in a one-round run"
    elif [ "$rc" = 4 ]; then SCORED[$key]=1; ATTN[$key]=1; say "    a level is VOID: its predictions are not scored, see $out"
    else ATTN[$key]=1; say "    scoring the levels comparison failed, see $log"; fi
    [ "$DRY" = 1 ] || [ ! -e "$out" ] || { [ "$ONE_ROUND" = 1 ] && sed -i "1i ONE-ROUND: scored $a, $b and $c through their one-round- prefix; one round, verdicts meaningless" "$out"; }
    stamp_score "$out"
}

for b in "${PLAN[@]}"; do
    case "$b" in
        ladder-l0|ladder-l1|ladder2-l0|ladder2-l1) levels_may_run "$b" && host_batch "$b";;
        ladder-l2|ladder2-l2) levels_may_run "$b" && nested_batch "$b";;
        r3-nested) nested_batch "$b";;
        r4) r4_batch;;
        r4b) r4b_batch;;
        r5-pilot) r5_pilot_batch;;
        r6-nested) nested_batch r6-nested; score_host r6-nested "$RES/${PFX}pcn-r6.json" "${LOGOF[r6-nested]}";;
        r7-a|r7-b) nested_batch "$b"; score_host "$b" "$RES/$PFX${NAME[$b]}.json" "${LOGOF[$b]}";;
        r5) r5_batch;;
        *) host_batch "$b";;
    esac
done
{ planned r3-single || planned r3-nested; } && score_r3
levels1_planned && score_levels levels
levels2_planned && score_levels levels2

if [ "$DRY" = 1 ]; then
    say ""; say "run_batches: dry run -- the summary would be written to $SUM/summary.txt"
    if [ "$REMEASURE" = 1 ] && [ "$REPO_REFUSED" = 1 ]; then say "run_batches: re-measurement plan FAILS: the repository would refuse the run, see above"; exit 4; fi
    [ "$REMEASURE" = 0 ] || say "run_batches: re-measurement plan ok: nothing in the repository would refuse"
    exit 0
fi
if [ "$REMEASURE" = 1 ]; then
    for sf in "$RES/$PFX"*.score.txt; do
        [ -e "$sf" ] && ! grep -q "^REMEASUREMENT" "$sf" && sed -i "1i REMEASUREMENT at commit $HEAD_SHA, measured-code tree $RM_TREE: not the pre-registered run" "$sf"
    done
fi
[ "$IDLED" = 1 ] && user_restore
r4b_planned && r4b_labels
write_summary
tail -1 "$SUM/summary.txt" | grep -q "all batches complete" && exit 0
exit 1
