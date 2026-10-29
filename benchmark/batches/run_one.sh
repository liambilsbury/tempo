#!/usr/bin/env bash
# run a pre-registered single-level batch, or print it with --dry-run
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
DRY=0; [ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
. benchmark/host/settings.conf
case "${1:-}" in
    r3-nested|ladder-l2|ladder2-l2|r5-pilot|r5|r6-nested|r7-a|r7-b) PINS="$VM_PINS"; PINS_PAIR="$VM_PINS_PAIR";;
    *) PINS="$HOST_PINS"; PINS_PAIR="$HOST_PINS_PAIR";;
esac
KDIR="${TEMPO_BATCHES_KERNEL_DIR:-build}"
# print a value, or exit 2 when it is not a whole number
num() { [[ "$2" =~ ^[0-9]+$ ]] || { echo "batches: refusing -- $1='$2' is not a whole number" >&2; exit 2; }; echo "$2"; }
REPEATS="$(num TEMPO_BATCHES_REPEATS "${TEMPO_BATCHES_REPEATS:-16}")" || exit 2
F13="$(num TEMPO_BATCHES_FRAMES "${TEMPO_BATCHES_FRAMES:-3000}")" || exit 2
F2="$(num TEMPO_BATCHES_FRAMES "${TEMPO_BATCHES_FRAMES:-20000}")" || exit 2
FR="$(num TEMPO_BATCHES_FRAMES "${TEMPO_BATCHES_FRAMES:-6000}")" || exit 2
INJ="$(num TEMPO_BATCHES_INJECT_S "${TEMPO_BATCHES_INJECT_S:-12}")" || exit 2
SETTLE_S="$(num TEMPO_BATCHES_SETTLE_S "${TEMPO_BATCHES_SETTLE_S:-900}")" || exit 2
PILOT_REPEATS=3; [ -n "${TEMPO_BATCHES_REPEATS:-}" ] && PILOT_REPEATS="$REPEATS"
FORCED_RE="^(one_round_forced$(awk -F'\t' '$2 == "one_round_forced" { printf "|%s", $1 }' benchmark/renamed.tsv 2>/dev/null))=1"
# print R4's duty grid
r4_grid() { python3 -c "import sys; sys.path.insert(0, '.'); from benchmark.score.single_level import r4 as r4_select; print(*r4_select.GRID)"; }
PFX="${TEMPO_BATCHES_PREFIX:-}"
[[ "$PFX" =~ ^[a-z0-9-]*$ ]] || { echo "batches: refusing -- TEMPO_BATCHES_PREFIX='$PFX' is not [a-z0-9-]*" >&2; exit 2; }
ONE_ROUND=""; [ -n "$PFX" ] && [ -z "${TEMPO_BATCHES_REMEASURE:-}" ] && ONE_ROUND=1
# turn a dry re-measurement's missing pilot selection into a note
remeasure_note() {
    if [ -n "$bad" ] && [ "$DRY" = 1 ] && [ -n "${TEMPO_BATCHES_REMEASURE:-}" ] && [[ "$bad" == "no selection at "* ]]; then
        echo "batches: note -- $bad; the plan's pilot stage writes it" >&2; bad=""; D="<duty>"
    fi
}
SETTLE="TEMPO_SETTLE_S=$SETTLE_S TEMPO_SETTLE_LOAD=0.3"
# refuse a missing kernel, a running L1 VM or missing sudo rules
need() {
    for k in "$@"; do
        [ -f "$KDIR/$k/vmlinuz" ] && continue
        echo "batches: refusing -- $KDIR/$k/vmlinuz missing; run bash tools/build/guest_kernel.sh --variant ${k#kernel-}" >&2
        [ "$DRY" = 1 ] || exit 3
    done
    if [ "${SKIP_VM_CHECK:-0}" != 1 ] && pgrep -f -- '-name tempo-l1' >/dev/null; then
        echo "batches: refusing -- the L1 VM is running on cores $VM_HOST_CORES; power it off first" >&2
        [ "$DRY" = 1 ] || exit 3
    fi
    [ "$DRY" = 1 ] && return 0
    sudo -n bpftrace --version >/dev/null 2>&1 || { echo "batches: refusing -- no NOPASSWD sudo for bpftrace in /etc/sudoers.d/50-tempo" >&2; exit 3; }
    sudo -n -l "$PWD/build/inject" >/dev/null 2>&1 || { echo "batches: refusing -- no NOPASSWD sudo for $PWD/build/inject in /etc/sudoers.d/50-tempo" >&2; exit 3; }
}
# refuse when the host topology is not the one given
want_topology() {
    [ "$DRY" = 1 ] && return 0
    t="$(python3 -c "import sys; sys.path.insert(0, '.'); from benchmark import lib; print(lib.host_topology()['topology'])")"
    [ "$t" = "$1" ] || { echo "batches: refusing -- host topology is $t, this batch needs $1" >&2; exit 3; }
}
# print a command and run it unless this is a dry run
run() { echo "+ $*"; [ "$DRY" = 1 ] || bash -c "$*"; }
# refuse a host that is not PREEMPT_RT
rt_host() {
    local f=/sys/kernel/realtime
    if [ -n "${TEMPO_BATCHES_REALTIME:-}" ]; then
        [ "$DRY" = 1 ] || { echo "batches: refusing -- TEMPO_BATCHES_REALTIME works only with --dry-run" >&2; exit 3; }
        f="$TEMPO_BATCHES_REALTIME"
    fi
    [ "$(cat "$f" 2>/dev/null)" = 1 ] && return 0
    echo "batches: refusing -- not a PREEMPT_RT host: $f is not 1, kernel $(uname -r)" >&2
    [ "$DRY" = 1 ] || exit 3
}
case "${1:-}" in
  r1)
    need kernel-rt kernel-nonrt; want_topology single-level
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS --pin $PINS_PAIR --frames $F13 --slopes 3200 --acts 500 --condition ${PFX}pc-kern16-nonrt:cpu-load:50:TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz --condition ${PFX}pc-kern16-rt:cpu-load:50:TEMPO_KERNEL=$KDIR/kernel-rt/vmlinuz --out results/experiments/${PFX}pc-kern16.json"
    ;;
  r2)
    need kernel-nonrt; want_topology single-level
    E="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz"
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS --frames $F2 --inject-s $INJ --slopes 2400 --acts 600 --pin $PINS --metric over_deadline_frac --out results/experiments/${PFX}pc-adapt.json --condition ${PFX}fixed-hi:cpu-load:20:$E,TEMPO_TIER=0,TEMPO_ADAPT=off --condition ${PFX}fixed-hi-probe:cpu-load:20:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe --condition ${PFX}adaptive:cpu-load:20:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce --condition ${PFX}fixed-lo:cpu-load:20:$E,TEMPO_TIER=3,TEMPO_ADAPT=off"
    prc=$?
    if [ "$prc" = 0 ]; then
        run "python3 benchmark/score/nested/quality_shedding.py --paired results/experiments/${PFX}pc-adapt.json"
    else
        echo "batches: paired.py exited $prc; batch not scored" >&2
    fi
    exit "$prc"
    ;;
  r3-single|r3-nested)
    [ "$1" = r3-nested ] && SKIP_VM_CHECK=1
    need kernel-nonrt
    if [ "$1" = r3-single ]; then want_topology single-level; P=${PFX}pc-r3; else want_topology nested; P=${PFX}pcn-r3; fi
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS --pin $PINS_PAIR --frames $F13 --slopes 3200 --acts 500 --condition $P-control:none:0:TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz --condition $P-hog:cpu-load:50:TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz --out results/experiments/$P.json"
    ;;
  r4-pilot)
    need kernel-nonrt; want_topology single-level
    E="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz,TEMPO_SLOT_US=125,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    C="--condition ${PFX}pc-r4p-control:none:0:$E"
    for d in $(r4_grid); do C+=" --condition ${PFX}pc-r4p-probe-d$d:cpu-load:$d:$E"; done
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $PILOT_REPEATS --frames $F2 --inject-s $INJ --slopes 2400 --acts 600 --pin $PINS --metric over_deadline_frac --out results/experiments/${PFX}pc-r4-pilot.json $C"
    prc=$?
    SEL="${PFX:+ --prefix $PFX}${ONE_ROUND:+ --one-round-fallback}"
    if [ "$prc" = 0 ]; then
        run "python3 benchmark/score/single_level/r4.py --select results/experiments/${PFX}pc-r4-pilot.json --record results/experiments/${PFX}pc-r4-pilot.selection$SEL"
    else
        echo "batches: paired.py exited $prc; pilot not scored, no duty selected" >&2
    fi
    exit "$prc"
    ;;
  r4)
    SELF="${TEMPO_BATCHES_R4_SELECTION:-results/experiments/${PFX}pc-r4-pilot.selection}"
    D="$(sed -n 's/^selected_duty=//p' "$SELF" 2>/dev/null | tail -1)"
    bad=""
    if [ ! -f "$SELF" ]; then bad="no selection at $SELF; run r4-pilot first"
    elif [ "$D" = none ]; then bad="the selection in $SELF chose no duty; R4 is not run"
    elif [[ ! " $(r4_grid) " == *" $D "* ]]; then bad="the selection in $SELF names duty '$D', not in the grid $(r4_grid | tr ' ' ,)"
    elif [ -z "$ONE_ROUND" ] && grep -qE "$FORCED_RE" "$SELF"; then bad="the selection at $SELF was forced for a one-round run"
    elif [ "$DRY" = 0 ] && [ -n "${TEMPO_BATCHES_R4_SELECTION:-}" ]; then bad="TEMPO_BATCHES_R4_SELECTION works only with --dry-run"
    elif [ "$DRY" = 0 ] && [ -z "$ONE_ROUND" ]; then
        again="$(python3 benchmark/score/single_level/r4.py --select "results/experiments/${PFX}pc-r4-pilot.json" ${PFX:+--prefix $PFX} 2>/dev/null | sed -n 's/^selected_duty=//p')"
        [ "$again" = "$D" ] || bad="duty $D in $SELF does not match the pilot re-scored: ${again:-unreadable}"
    fi
    remeasure_note
    if [ -n "$bad" ]; then
        echo "batches: refusing r4 -- $bad" >&2
        [ "$DRY" = 1 ] || exit 3
        D="<duty>"
    fi
    need kernel-nonrt; want_topology single-level
    E="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz,TEMPO_SLOT_US=125"
    if [ -z "$PFX" ]; then run "python3 benchmark/score/single_level/r4.py --keep-r2-runs results/experiments" || exit 3; fi
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS --frames $F2 --inject-s $INJ --slopes 2400 --acts 600 --pin $PINS --metric over_deadline_frac --out results/experiments/${PFX}pc-r4.json --condition ${PFX}fixed-hi:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=off --condition ${PFX}fixed-hi-probe:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe --condition ${PFX}adaptive:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce --condition ${PFX}fixed-lo:cpu-load:$D:$E,TEMPO_TIER=3,TEMPO_ADAPT=off --condition ${PFX}pc-r4-control:none:0:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    prc=$?
    if [ "$prc" = 0 ]; then
        R="results/experiments/${PFX}pc-r4"
        run "python3 benchmark/score/single_level/r4.py --arrival $R.json $R.arrived.json${PFX:+ --prefix $PFX} && python3 benchmark/score/nested/quality_shedding.py --paired $R.arrived.json && python3 benchmark/score/single_level/r4.py --ratio $R.arrived.json"
    else
        echo "batches: paired.py exited $prc; batch not scored" >&2
    fi
    exit "$prc"
    ;;
  ladder-pilot|ladder-l0|ladder-l1|ladder-l2|ladder2-pilot|ladder2-l0|ladder2-l1|ladder2-l2)
    VF=""; LP=pc-ld-pilot; LA=pc-ldp; R0=pc-ld0; R1=pc-ld1; R2=pcn-ld2; ST="${1##*-}"
    case "$1" in ladder2-pilot|ladder2-l0|ladder2-l1|ladder2-l2) VF=" --variant levels2"; LP=pc-lb-pilot; LA=pc-lbp; R0=pc-lb0; R1=pc-lb1; R2=pcn-lb2;; esac
    LF=$F2; LI=$INJ; LRANGE=2000-9000
    if [ -n "$ONE_ROUND" ]; then LF=6000; LI=8; LRANGE=3000-4000; fi
    G1="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz,TEMPO_SLOT_US=125,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_LATE_RANGE=$LRANGE"
    LSHAPE="--frames $LF --inject-s $LI --slopes 2400 --acts 600 --pin $PINS --metric max_ns"
    LSEL="results/experiments/${PFX}$LP.selection"
    if [ "$ST" = pilot ]; then
        rt_host; need kernel-nonrt; want_topology single-level
        C="--condition ${PFX}$LA-control:none:0:$G1"
        while read -r d lab; do C+=" --condition ${PFX}$lab:cpu-load:$d:$G1"; done < <(python3 benchmark/score/single_level/levels_select.py --grid$VF)
        run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $PILOT_REPEATS $LSHAPE --out results/experiments/${PFX}$LP.json $C"
        prc=$?
        if [ "$prc" = 0 ]; then
            run "python3 benchmark/score/single_level/levels_select.py --select results/experiments/${PFX}$LP.json --record $LSEL$VF${PFX:+ --prefix $PFX}${ONE_ROUND:+ --one-round-fallback --range $LRANGE}"
        else
            echo "batches: paired.py exited $prc; pilot not scored, no duty selected" >&2
        fi
        exit "$prc"
    fi
    SELF="${TEMPO_BATCHES_LEVELS_SELECTION:-$LSEL}"
    D="$(sed -n 's/^selected_duty=//p' "$SELF" 2>/dev/null | tail -1)"
    GRIDS=" $(python3 benchmark/score/single_level/levels_select.py --grid$VF | awk '{print $1}' | tr '\n' ' ')"
    bad=""
    if [ "$DRY" = 0 ] && [ -n "${TEMPO_BATCHES_LEVELS_SELECTION:-}" ]; then bad="TEMPO_BATCHES_LEVELS_SELECTION works only with --dry-run"
    elif [ ! -f "$SELF" ]; then bad="no selection at $SELF; run ${1%-*}-pilot first"
    elif [ "$D" = none ]; then bad="the selection in $SELF chose no duty; the levels comparison is not run"
    elif [[ "$GRIDS" != *" $D "* ]]; then bad="the selection in $SELF names duty '$D', not in the grid$GRIDS"
    elif [ -z "$ONE_ROUND" ] && grep -qE "$FORCED_RE" "$SELF"; then bad="the selection at $SELF was forced for a one-round run"
    elif [ "$DRY" = 0 ] && [ -z "$ONE_ROUND" ]; then
        again="$(python3 benchmark/score/single_level/levels_select.py --select results/experiments/${PFX}$LP.json$VF${PFX:+ --prefix $PFX} 2>/dev/null | sed -n 's/^selected_duty=//p')"
        [ "$again" = "$D" ] || bad="duty $D in $SELF does not match the pilot re-scored: ${again:-unreadable}"
    fi
    remeasure_note
    if [ -n "$bad" ]; then
        echo "batches: refusing $1 -- $bad" >&2
        [ "$DRY" = 1 ] || exit 3
        D="<duty>"
    fi
    case "$ST" in
      l0) rt_host; need; want_topology single-level; P=${PFX}$R0
                 E="TEMPO_LEVEL=host,TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=125,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_LATE_RANGE=$LRANGE" ;;
      l1) rt_host; need kernel-nonrt; want_topology single-level; P=${PFX}$R1; E="$G1" ;;
      l2) SKIP_VM_CHECK=1; need kernel-nonrt; want_topology nested; P=${PFX}$R2; E="$G1" ;;
    esac
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS $LSHAPE --out results/experiments/$P.json --condition $P-control:none:0:$E --condition $P-hog:cpu-load:$D:$E"
    ;;
  r4b-pilot|r4b)
    E="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz,TEMPO_SLOT_US=125"
    BSHAPE="--frames $F2 --inject-s $INJ --slopes 2400 --acts 600 --pin $PINS --metric over_deadline_frac"
    BSEL="results/experiments/${PFX}pc-r4b-pilot.selection"
    if [ "$1" = r4b-pilot ]; then
        rt_host; need kernel-nonrt; want_topology single-level
        PR=5; [ -n "${TEMPO_BATCHES_REPEATS:-}" ] && PR="$REPEATS"
        C="--condition ${PFX}pc-r4bp-control:none:0:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
        while read -r d lab; do C+=" --condition ${PFX}$lab:cpu-load:$d:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"; done < <(python3 benchmark/score/single_level/r4b.py --grid)
        run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $PR $BSHAPE --out results/experiments/${PFX}pc-r4b-pilot.json $C"
        prc=$?
        if [ "$prc" = 0 ]; then
            run "python3 benchmark/score/single_level/r4b.py --select results/experiments/${PFX}pc-r4b-pilot.json --record $BSEL${PFX:+ --prefix $PFX}${ONE_ROUND:+ --one-round-fallback}"
        else
            echo "batches: paired.py exited $prc; pilot not scored, no duty selected" >&2
        fi
        exit "$prc"
    fi
    SELF="${TEMPO_BATCHES_R4B_SELECTION:-$BSEL}"
    DA="$(sed -n 's/^selected_duty_a=//p' "$SELF" 2>/dev/null | tail -1)"
    DB="$(sed -n 's/^selected_duty_b=//p' "$SELF" 2>/dev/null | tail -1)"
    GRIDS=" $(python3 benchmark/score/single_level/r4b.py --grid | awk '{print $1}' | tr '\n' ' ')"
    bad=""
    if [ "$DRY" = 0 ] && [ -n "${TEMPO_BATCHES_R4B_SELECTION:-}" ]; then bad="TEMPO_BATCHES_R4B_SELECTION works only with --dry-run"
    elif [ ! -f "$SELF" ]; then bad="no selection at $SELF; run r4b-pilot first"
    elif [ "$DA" = none ]; then bad="the pilot chose no load A in $SELF; R4b is not run"
    elif [[ "$GRIDS" != *" $DA "* ]] || { [ "$DB" != none ] && [[ "$GRIDS" != *" $DB "* ]]; }; then bad="the selection in $SELF names duty '$DA'/'$DB', not in the grid$GRIDS"
    elif [ -z "$ONE_ROUND" ] && grep -qE "$FORCED_RE" "$SELF"; then bad="the selection at $SELF was forced for a one-round run"
    elif [ "$DRY" = 0 ] && [ -z "$ONE_ROUND" ]; then
        again="$(python3 benchmark/score/single_level/r4b.py --select results/experiments/${PFX}pc-r4b-pilot.json ${PFX:+--prefix $PFX} 2>/dev/null | sed -n 's/^selected_duty_[ab]=//p' | tr '\n' ' ')"
        [ "$again" = "$DA $DB " ] || bad="duties $DA $DB in $SELF do not match the pilot re-scored: ${again:-unreadable}"
    fi
    remeasure_note
    if [ -n "$bad" ]; then
        echo "batches: refusing r4b -- $bad" >&2
        [ "$DRY" = 1 ] || exit 3
        DA="<duty-a>"; DB="<duty-b>"
    fi
    rt_host; need kernel-nonrt; want_topology single-level
    LOADS="a"; [ "$DB" != none ] && LOADS="a b"
    C=""
    for l in $LOADS; do
        D="$DA"; [ "$l" = b ] && D="$DB"
        C+=" --condition ${PFX}$l-fixed-hi:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=off --condition ${PFX}$l-fixed-hi-probe:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
        C+=" --condition ${PFX}$l-adaptive:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce --condition ${PFX}$l-fixed-lo:cpu-load:$D:$E,TEMPO_TIER=3,TEMPO_ADAPT=off"
    done
    C+=" --condition ${PFX}pc-r4b-control:none:0:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS $BSHAPE --out results/experiments/${PFX}pc-r4b.json$C"
    prc=$?
    if [ "$prc" = 0 ]; then
        R="results/experiments/${PFX}pc-r4b"
        for l in $LOADS; do
            run "python3 benchmark/score/single_level/r4b.py --split $R.json $l $R-$l.arrived.json${PFX:+ --prefix $PFX} && python3 benchmark/score/nested/quality_shedding.py --paired $R-$l.arrived.json && python3 benchmark/score/single_level/r4b.py --ratio $l $R-$l.arrived.json"
        done
    else
        echo "batches: paired.py exited $prc; batch not scored" >&2
    fi
    exit "$prc"
    ;;
  r5-pilot|r5)
    SKIP_VM_CHECK=1
    E="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz,TEMPO_SLOT_US=125"
    BSHAPE="--frames $F2 --inject-s $INJ --slopes 2400 --acts 600 --pin $PINS --metric over_deadline_frac"
    BSEL="results/experiments/${PFX}pcn-r5-pilot.selection"
    if [ "$1" = r5-pilot ]; then
        need kernel-nonrt; want_topology nested
        PR=5; [ -n "${TEMPO_BATCHES_REPEATS:-}" ] && PR="$REPEATS"
        C="--condition ${PFX}pcn-r5p-control:none:0:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
        while read -r d lab; do C+=" --condition ${PFX}$lab:cpu-load:$d:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"; done < <(python3 benchmark/score/single_level/r5.py --grid)
        run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $PR $BSHAPE --out results/experiments/${PFX}pcn-r5-pilot.json $C"
        prc=$?
        if [ "$prc" = 0 ]; then
            run "python3 benchmark/score/single_level/r5.py --select results/experiments/${PFX}pcn-r5-pilot.json --record $BSEL${PFX:+ --prefix $PFX}${ONE_ROUND:+ --one-round-fallback}"
        else
            echo "batches: paired.py exited $prc; pilot not scored, no duty selected" >&2
        fi
        exit "$prc"
    fi
    SELF="${TEMPO_BATCHES_R5_SELECTION:-$BSEL}"
    D="$(sed -n 's/^selected_duty=//p' "$SELF" 2>/dev/null | tail -1)"
    GRIDS=" $(python3 benchmark/score/single_level/r5.py --grid | awk '{print $1}' | tr '\n' ' ')"
    bad=""
    if [ "$DRY" = 0 ] && [ -n "${TEMPO_BATCHES_R5_SELECTION:-}" ]; then bad="TEMPO_BATCHES_R5_SELECTION works only with --dry-run"
    elif [ ! -f "$SELF" ]; then bad="no selection at $SELF; run r5-pilot first"
    elif [ "$D" = none ]; then bad="the selection in $SELF chose no duty; R5 is not run"
    elif [[ "$GRIDS" != *" $D "* ]]; then bad="the selection in $SELF names duty '$D', not in the grid$GRIDS"
    elif [ -z "$ONE_ROUND" ] && grep -qE "$FORCED_RE" "$SELF"; then bad="the selection at $SELF was forced for a one-round run"
    elif [ "$DRY" = 0 ] && [ -z "$ONE_ROUND" ]; then
        again="$(python3 benchmark/score/single_level/r5.py --select results/experiments/${PFX}pcn-r5-pilot.json ${PFX:+--prefix $PFX} 2>/dev/null | sed -n 's/^selected_duty=//p')"
        [ "$again" = "$D" ] || bad="duty $D in $SELF does not match the pilot re-scored: ${again:-unreadable}"
    fi
    remeasure_note
    if [ -n "$bad" ]; then
        echo "batches: refusing r5 -- $bad" >&2
        [ "$DRY" = 1 ] || exit 3
        D="<duty>"
    fi
    need kernel-nonrt; want_topology nested
    P="${PFX}pcn-r5"
    C=" --condition $P-fixed-hi:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=off --condition $P-fixed-hi-probe:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    C+=" --condition $P-adaptive:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce --condition $P-fixed-lo:cpu-load:$D:$E,TEMPO_TIER=3,TEMPO_ADAPT=off"
    C+=" --condition $P-control:none:0:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS $BSHAPE --out results/experiments/$P.json$C"
    prc=$?
    if [ "$prc" = 0 ]; then
        R="results/experiments/$P"
        run "python3 benchmark/score/single_level/r5.py --split $R.json $R.arrived.json${PFX:+ --prefix $PFX} && python3 benchmark/score/nested/quality_shedding.py --paired $R.arrived.json && python3 benchmark/score/single_level/r5.py --ratio $R.arrived.json"
    else
        echo "batches: paired.py exited $prc; batch not scored" >&2
    fi
    exit "$prc"
    ;;
  r6-nested|r6-single)
    E="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz,TEMPO_SLOT_US=125"
    BSHAPE="--frames $F2 --inject-s $INJ --slopes 2400 --acts 600 --pin $PINS --metric over_deadline_frac"
    LV=single; P="${PFX}pc-r6"
    if [ "$1" = r6-nested ]; then
        SKIP_VM_CHECK=1; LV=nested; P="${PFX}pcn-r6"
    fi
    D="$(python3 benchmark/score/single_level/r6.py --duty $LV 2>/dev/null)"
    if [ -z "$D" ]; then
        echo "batches: refusing $1 -- no $LV duty from benchmark/score/single_level/r6.py --duty $LV" >&2
        [ "$DRY" = 1 ] || exit 3
        D="<duty>"
    fi
    if [ "$LV" = nested ]; then need kernel-nonrt; want_topology nested
    else rt_host; need kernel-nonrt; want_topology single-level; fi
    C=" --condition $P-fixed-hi:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=off --condition $P-fixed-hi-probe:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    C+=" --condition $P-adaptive:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce --condition $P-adaptive-rs:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce-rs"
    C+=" --condition $P-fixed-lo:cpu-load:$D:$E,TEMPO_TIER=3,TEMPO_ADAPT=off --condition $P-control:none:0:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS $BSHAPE --out results/experiments/$P.json$C"
    prc=$?
    if [ "$prc" = 0 ]; then
        R="results/experiments/$P"
        run "python3 benchmark/score/single_level/r6.py --split $R.json $LV adaptive-rs $R-rs.arrived.json${PFX:+ --prefix $PFX} && python3 benchmark/score/nested/quality_shedding.py --paired $R-rs.arrived.json && python3 benchmark/score/single_level/r6.py --ratio $LV adaptive-rs $R-rs.arrived.json"
    else
        echo "batches: paired.py exited $prc; batch not scored" >&2
    fi
    exit "$prc"
    ;;
  r7-a|r7-b)
    SKIP_VM_CHECK=1
    E="TEMPO_RECONSTRUCT=modal,TEMPO_BLOCK_NS=23136,TEMPO_KERNEL=$KDIR/kernel-nonrt/vmlinuz,TEMPO_SLOT_US=125"
    BSHAPE="--frames $F2 --inject-s $INJ --slopes 2400 --acts 600 --pin $PINS --metric over_deadline_frac"
    LV="${1#r7-}"; P="${PFX}pcn-r7$LV"
    D="$(python3 benchmark/score/single_level/r7.py --duty $LV 2>/dev/null)"
    if [ -z "$D" ]; then
        echo "batches: refusing $1 -- no duty from benchmark/score/single_level/r7.py --duty $LV" >&2
        [ "$DRY" = 1 ] || exit 3
        D="<duty>"
    fi
    need kernel-nonrt; want_topology nested
    C=" --condition $P-fixed-hi:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=off --condition $P-fixed-hi-probe:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    C+=" --condition $P-adaptive:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce --condition $P-adaptive-rs:cpu-load:$D:$E,TEMPO_TIER=0,TEMPO_ADAPT=enforce-rs"
    C+=" --condition $P-fixed-lo:cpu-load:$D:$E,TEMPO_TIER=3,TEMPO_ADAPT=off --condition $P-control:none:0:$E,TEMPO_TIER=0,TEMPO_ADAPT=observe"
    run "TEMPO_NO_EXIT_PROBE=1 $SETTLE python3 benchmark/run/paired.py --repeats $REPEATS $BSHAPE --out results/experiments/$P.json$C"
    prc=$?
    if [ "$prc" = 0 ]; then
        R="results/experiments/$P"
        run "python3 benchmark/score/single_level/r7.py --split $R.json $LV adaptive-rs $R-rs.arrived.json${PFX:+ --prefix $PFX} && python3 benchmark/score/nested/quality_shedding.py --paired $R-rs.arrived.json && python3 benchmark/score/single_level/r7.py --ratio $LV adaptive-rs $R-rs.arrived.json"
    else
        echo "batches: paired.py exited $prc; batch not scored" >&2
    fi
    exit "$prc"
    ;;
  rhizo|rhizo-nogpu|rhizo-drift|rhizo-drift-nogpu|rhizo-headroom)
    want_topology single-level
    RT="${RHIZO_TREE:-}"
    if [ -z "$RT" ]; then
        echo "batches: refusing -- RHIZO_TREE is not set: the Rhizo checkout whose VM runs the loop" >&2
        [ "$DRY" = 1 ] || exit 3
        RT="<RHIZO_TREE>"
    fi
    case "$1" in
        rhizo) H="${PFX}rhizo" GPU=on OUTN=rhizo E="";;
        rhizo-nogpu) H="${PFX}nogpu-rhizo" GPU=off OUTN=rhizo-nogpu E="";;
        rhizo-drift) H="${PFX}drift-rhizo" GPU=on OUTN=rhizo-drift E=",TEMPO_RHIZO_SLOT_US=97000,TEMPO_RHIZO_TELEMETRY_ADPF=on,TEMPO_RHIZO_CPU_COUNTERS=on";;
        rhizo-drift-nogpu) H="${PFX}drift-nogpu-rhizo" GPU=off OUTN=rhizo-drift-nogpu E=",TEMPO_RHIZO_SLOT_US=97000,TEMPO_RHIZO_TELEMETRY_ADPF=on,TEMPO_RHIZO_CPU_COUNTERS=on";;
        rhizo-headroom) H="${PFX}headroom-rhizo" GPU=on OUTN=rhizo-headroom E=",TEMPO_RHIZO_SLOT_US=97000,TEMPO_RHIZO_CPU_COUNTERS=on,TEMPO_RHIZO_HEADROOM=on";;
    esac
    if [ "$1" = rhizo-headroom ]; then
        HR=on
        C="--condition $H-noload:none:0:TEMPO_RHIZO_ADPF=off$E --condition $H-vmload:cpu-load:30:TEMPO_RHIZO_LEVEL=vm,TEMPO_RHIZO_ADPF=off$E"
        C+=" --condition $H-pcload:cpu-load:30:TEMPO_RHIZO_LEVEL=pc,TEMPO_RHIZO_ADPF=off$E"
    else
        HR=off
        C="--condition $H-noload-noadpf:none:0:TEMPO_RHIZO_ADPF=off$E --condition $H-noload-adpf:none:0:TEMPO_RHIZO_ADPF=on$E"
        C+=" --condition $H-vmload-noadpf:cpu-load:30:TEMPO_RHIZO_LEVEL=vm,TEMPO_RHIZO_ADPF=off$E --condition $H-vmload-adpf:cpu-load:30:TEMPO_RHIZO_LEVEL=vm,TEMPO_RHIZO_ADPF=on$E"
        C+=" --condition $H-pcload-noadpf:cpu-load:30:TEMPO_RHIZO_LEVEL=pc,TEMPO_RHIZO_ADPF=off$E --condition $H-pcload-adpf:cpu-load:30:TEMPO_RHIZO_LEVEL=pc,TEMPO_RHIZO_ADPF=on$E"
    fi
    GK=stock; [[ "$1" == rhizo-drift* || "$1" == rhizo-headroom ]] && GK=steal
    run "bash benchmark/run/rhizo/batch.sh --rhizo $RT --gpu $GPU$([ "$HR" = on ] && echo " --headroom on")$([ "$GK" = steal ] && echo " --guest-kernel steal") -- python3 benchmark/run/paired.py --runner benchmark/run/rhizo/run.sh --repeats $REPEATS --frames $FR --period-ns 10000000 --slopes 1600 --acts 400 --pin $RHIZO_VCPU_CPUS --metric missed_frac --out results/experiments/${PFX}${OUTN}.json $C"
    ;;
  *) echo "usage: benchmark/batches/run_one.sh [--dry-run] r1|r2|r3-single|r3-nested|r4-pilot|r4|ladder-pilot|ladder-l0|ladder-l1|ladder-l2|ladder2-pilot|ladder2-l0|ladder2-l1|ladder2-l2|r4b-pilot|r4b|r5-pilot|r5|r6-nested|r6-single|r7-a|r7-b|rhizo|rhizo-nogpu|rhizo-drift|rhizo-drift-nogpu|rhizo-headroom" >&2; exit 2;;
esac
