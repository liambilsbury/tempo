#!/usr/bin/env bash
# print and run the launch and scorer commands of an earlier-phase batch
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
DRY=0; [ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
B="${1:-}"
O=results/experiments/earlier-
BATCHES="fixed kernels classes class-predictions probe-overhead exit-counter-overhead steal-gap gap-detector anchor adapt feedback classes-sweep frequency-sweep in-loop slack guarantee budget budget-class trigger trigger-completion quality calibration"
[ "$B" = "--list" ] && { echo $BATCHES; exit 0; }
usage() { echo "usage: bash benchmark/batches/nested.sh [--dry-run] <batch> | --list" >&2; exit 2; }
[[ " $BATCHES " == *" $B "* ]] || usage
RC=0
topo="$(python3 -c "import sys; sys.path.insert(0, '.'); from benchmark import lib; print(lib.host_topology()['topology'])")"
[ "$topo" = nested ] || echo "earlier_phase: host prerequisite you supply -- this host is $topo, not nested; results go to results/experiments/ as $topo"
# print and run a launch command
P() { echo "+ $*"; if [ "$DRY" = 1 ]; then "$@" --dry-run || RC=1; else "$@" || RC=1; fi; }
# print and run a scorer command
S() {
    echo "+ $*"
    local f; f="$(grep -oE '(benchmark|tools)/[a-z_0-9/]*\.py' <<<"$*" | head -1)"
    if [[ "$f" == tools/results/* ]] && [ ! -f "$f" ]; then echo "earlier_phase: skipping $f, which is not in this tree"; return; fi
    if [ "$DRY" = 1 ]; then
        [ -f "$f" ] || { echo "earlier_phase: $f is missing" >&2; RC=1; }
    else "$@" || RC=1; fi
}
case "$B" in
fixed)
    P python3 benchmark/run/paired.py --repeats 8 --frames 3000 --pin 8,9 --slopes 3200 --acts 500 \
        --out ${O}paired-fixed.json \
        --condition control:none --condition hogsync:cpu-load:50
    S python3 benchmark/analysis/recall/recall.py --paired
    ;;
kernels)
    P python3 benchmark/run/paired.py --repeats 8 --frames 3000 --slopes 3200 --acts 500 --pin 8,9 \
        --out ${O}paired-kernels.json \
        --condition stock:cpu-load:50 \
        --condition rt:cpu-load:50:TEMPO_KERNEL=build/kernel-rt/vmlinuz \
        --condition 'rt-isol:cpu-load:50:TEMPO_KERNEL=build/kernel-rt/vmlinuz,TEMPO_GUEST_ARGS=isolcpus=1-3 nohz_full=1-3 rcu_nocbs=1-3'
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 8 --frames 3000 --slopes 3200 --acts 500 --pin 8,9 \
        --out ${O}paired-kernels-more.json \
        --condition stock:cpu-load:50 \
        --condition rt:cpu-load:50:TEMPO_KERNEL=build/kernel-rt/vmlinuz \
        --condition 'rt-isol:cpu-load:50:TEMPO_KERNEL=build/kernel-rt/vmlinuz,TEMPO_GUEST_ARGS=isolcpus=1-3 nohz_full=1-3 rcu_nocbs=1-3'
    S python3 tools/results/verify_claims.py
    ;;
classes)
    P python3 benchmark/run/paired.py --repeats 8 --frames 3000 --slopes 3200 --acts 500 --pin 8,9 \
        --out ${O}paired-classes.json \
        --condition control:none \
        --condition wake5k:wake:5000 \
        --condition tlbstorm:tlb:2000
    S python3 benchmark/analysis/exits/classes.py --paired ${O}paired-classes.json
    ;;
class-predictions)
    P python3 benchmark/run/paired.py --repeats 8 --frames 30000 --inject-s 20 \
        --slopes 3200 --acts 500 --pin 8,9,10,11 --out ${O}paired-prereg.json \
        --condition control:none \
        --condition tlb:tlb:500 \
        --condition tlbq:tlbq:500 \
        --condition cache:cache:0 \
        --condition wake5k:wake:5000 \
        --condition wake20k:wake:20000 \
        --condition hog20:cpu-load:20
    S python3 benchmark/score/nested/exit_rule.py --paired ${O}paired-prereg.json --live
    ;;
probe-overhead)
    P python3 benchmark/run/paired.py --repeats 8 --frames 4000 --slopes 3200 --acts 500 --pin 8,9,10,11 \
        --out ${O}paired-probe-overhead.json \
        --condition probe-on:none \
        --condition probe-off:none:0:TEMPO_NO_PROBE=1
    S python3 tools/results/verify_claims.py
    ;;
exit-counter-overhead)
    P python3 benchmark/run/paired.py --repeats 8 --frames 30000 --inject-s 20 \
        --slopes 3200 --acts 500 --pin 8,9,10,11 --out ${O}paired-exitprobe-overhead.json \
        --condition exitprobe-on:none \
        --condition exitprobe-off:none:0:TEMPO_NO_EXIT_PROBE=1
    S python3 tools/results/verify_claims.py
    ;;
steal-gap)
    P python3 benchmark/run/paired.py --repeats 8 --frames 30000 --inject-s 20 \
        --slopes 3200 --acts 500 --pin 8,9,10,11 --out ${O}paired-stealgap.json \
        --condition control:none \
        --condition hog20:cpu-load:20 \
        --condition tlb:tlb:500 \
        --condition tlbq:tlbq:500 \
        --condition wake20k:wake:20000
    S python3 benchmark/analysis/recall/steal_gap.py --paired ${O}paired-stealgap.json --live
    ;;
gap-detector)
    P env TEMPO_GUEST_ARGS="tempo.gap_detector=12 tempo.gapcpu=4 tempo.gapdelay=${GAPDELAY:-4}" \
        python3 benchmark/run/paired.py --repeats 8 --frames 30000 --inject-s 20 \
        --slopes 3200 --acts 500 --pin 8,9,10,11,12 --vcpus 5 --out ${O}paired-gapdet.json \
        --condition control:none \
        --condition hog20:cpu-load:20 \
        --condition tlb:tlb:500 \
        --condition wake20k:wake:20000
    S python3 benchmark/analysis/recall/gap_detector.py --paired ${O}paired-gapdet.json --live
    ;;
anchor)
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 20 --frames 20000 --inject-s 12 \
        --slopes 2400 --acts 600 --pin 8,9,10,11 --metric p999_ns \
        --out ${O}paired-anchor.json \
        --condition fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition feedback:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=feedback,TEMPO_BLOCK_NS=23136
    S python3 benchmark/score/nested/anchor.py --paired ${O}paired-anchor.json
    ;;
adapt)
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 16 --frames 20000 --inject-s 12 \
        --slopes 2400 --acts 600 --pin 8,9,10,11 --metric over_deadline_frac \
        --out ${O}paired-adapt.json \
        --condition fixed-hi:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=off,TEMPO_BLOCK_NS=23136 \
        --condition fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition fixed-lo:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=3,TEMPO_ADAPT=off,TEMPO_BLOCK_NS=23136
    S python3 benchmark/score/nested/quality_shedding.py --paired ${O}paired-adapt.json
    ;;
feedback)
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 16 --frames 20000 --inject-s 12 \
        --slopes 2400 --acts 600 --pin 8,9,10,11 --metric over_deadline_frac \
        --out ${O}paired-feedback.json \
        --condition fixed-hi:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=off,TEMPO_BLOCK_NS=23136 \
        --condition fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition feedback:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=feedback,TEMPO_BLOCK_NS=23136 \
        --condition fixed-lo:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=3,TEMPO_ADAPT=off,TEMPO_BLOCK_NS=23136
    S python3 benchmark/score/nested/feedback.py --paired ${O}paired-feedback.json
    ;;
classes-sweep)
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 16 --frames 20000 --inject-s 12 \
        --slopes 2400 --acts 600 --pin 8,9,10,11 --metric over_deadline_frac \
        --out ${O}paired-classes-sweep.json \
        --condition wake20k-fixed-hi-probe:wake:20000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_BLOCK_NS=23136,TEMPO_ADAPT=observe \
        --condition wake20k-adaptive:wake:20000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_BLOCK_NS=23136,TEMPO_ADAPT=enforce \
        --condition wake20k-feedback:wake:20000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_BLOCK_NS=23136,TEMPO_ADAPT=feedback \
        --condition wake20k-fixed-lo:wake:20000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=3,TEMPO_ADAPT=off,TEMPO_BLOCK_NS=23136 \
        --condition tlb-fixed-hi-probe:tlb:2000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_BLOCK_NS=23136,TEMPO_ADAPT=observe \
        --condition tlb-adaptive:tlb:2000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_BLOCK_NS=23136,TEMPO_ADAPT=enforce \
        --condition tlb-feedback:tlb:2000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_BLOCK_NS=23136,TEMPO_ADAPT=feedback \
        --condition tlb-fixed-lo:tlb:2000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=3,TEMPO_ADAPT=off,TEMPO_BLOCK_NS=23136
    S python3 benchmark/score/nested/classes.py --paired ${O}paired-classes-sweep.json
    ;;
frequency-sweep)
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 16 --frames 20000 --inject-s 12 \
        --slopes 2400 --acts 600 --pin 8,9,10,11 --metric p999_ns \
        --out ${O}paired-freqsweep.json \
        --condition s125-fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=125 \
        --condition s125-adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=125 \
        --condition s125-feedback:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=feedback,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=125 \
        --condition s500-fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=500 \
        --condition s500-adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=500 \
        --condition s500-feedback:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=feedback,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=500 \
        --condition s1000-fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=1000 \
        --condition s1000-adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=1000 \
        --condition s1000-feedback:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=feedback,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=1000 \
        --condition s2000-fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=2000 \
        --condition s2000-adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=2000 \
        --condition s2000-feedback:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=feedback,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=2000 \
        --condition s4000-fixed-hi-probe:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=4000 \
        --condition s4000-adaptive:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=4000 \
        --condition s4000-feedback:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=feedback,TEMPO_BLOCK_NS=23136,TEMPO_SLOT_US=4000
    S python3 benchmark/score/nested/frequency_sweep.py --paired ${O}paired-freqsweep.json
    ;;
in-loop)
    P python3 benchmark/run/paired.py --repeats 8 --frames 13000 --inject-s 12 --slopes 2400 --acts 600 \
        --pin 8,9,10,11 --out ${O}paired-inloop.json \
        --condition control:none:0:TEMPO_RECONSTRUCT=modal,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition hog20:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136
    S python3 benchmark/analysis/recall/in_loop.py --paired ${O}paired-inloop.json
    ;;
slack)
    for g in paired-inloop-slack-hi.json:800:200 paired-inloop-slack-mid.json:2400:600 paired-inloop-slack-lo.json:3200:500; do
        out="${O}${g%%:*}"; name="${g%%:*}"; name="${name%.json}"; name="${name##*-}"; rest="${g#*:}"; slopes="${rest%%:*}"; acts="${rest#*:}"
        P python3 benchmark/run/paired.py --repeats 8 --frames 13000 --inject-s 12 \
            --slopes "$slopes" --acts "$acts" --pin 8,9,10,11 \
            --out "$out" \
            --condition "$name-control:none:0:TEMPO_RECONSTRUCT=modal,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136" \
            --condition "$name-hog20:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136" \
            --condition "$name-wake20k:wake:20000:TEMPO_RECONSTRUCT=modal,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136" \
            --condition "$name-tlb:tlb:2000:TEMPO_RECONSTRUCT=modal,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136"
        S python3 benchmark/analysis/recall/in_loop.py --paired "$out" --control "$name-control"
    done
    ;;
guarantee)
    P env TEMPO_CHECK_DEDICATED=1 TEMPO_KERNEL=build/kernel-rt/vmlinuz \
    TEMPO_GUEST_ARGS="isolcpus=1-4 nohz_full=1-4 rcu_nocbs=1-4 tempo.gap_detector=6 tempo.gapcpu=4" \
    TEMPO_RECONSTRUCT=modal TEMPO_ADAPT=observe TEMPO_BLOCK_NS=23136 \
    python3 benchmark/run/paired.py --repeats 20 --pin 8,9,10,11,12 --vcpus 5 --frames 6000 --period-ns 1000000 \
        --inject-s 6 --slopes 2400 --acts 600 --metric over_deadline_frac \
        --out ${O}paired-guarantee.json \
        --condition guarantee-control:none \
        --condition guarantee-violate-tlbq:tlbq:1 \
        --condition guarantee-violate-wake:wake:1
    S python3 benchmark/score/nested/violation.py --paired ${O}paired-guarantee.json --live
    ;;
budget)
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 16 --frames 20000 --inject-s 12 \
        --slopes 2400 --acts 600 --pin 8,9,10,11 --metric over_deadline_frac \
        --out ${O}paired-budget.json \
        --condition control:none \
        --condition adaptive-d20:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition adaptive-d50:cpu-load:50:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition adaptive-d80:cpu-load:80:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition adaptive-d95:cpu-load:95:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition probe-d20:cpu-load:20:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition probe-d50:cpu-load:50:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition probe-d80:cpu-load:80:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition probe-d95:cpu-load:95:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136
    S python3 benchmark/score/nested/budget.py --paired ${O}paired-budget.json
    ;;
budget-class)
    P env TEMPO_NO_EXIT_PROBE=1 python3 benchmark/run/paired.py --repeats 16 --frames 20000 --inject-s 12 \
        --slopes 2400 --acts 600 --pin 8,9,10,11 --metric over_deadline_frac \
        --out ${O}paired-budget-class.json \
        --condition control:none \
        --condition adaptive-r1000:tlb:1000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition adaptive-r2000:tlb:2000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition adaptive-r4000:tlb:4000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition adaptive-r8000:tlb:8000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=enforce,TEMPO_BLOCK_NS=23136 \
        --condition probe-r1000:tlb:1000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition probe-r2000:tlb:2000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition probe-r4000:tlb:4000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136 \
        --condition probe-r8000:tlb:8000:TEMPO_RECONSTRUCT=modal,TEMPO_TIER=0,TEMPO_ADAPT=observe,TEMPO_BLOCK_NS=23136
    S python3 benchmark/score/nested/budget_class.py --paired ${O}paired-budget-class.json
    ;;
trigger)
    P python3 benchmark/run/paired.py --repeats 16 --pin 8,9,10,11 --vcpus 4 --frames 300 --period-ns 8000000 \
        --inject-s 6 --slopes 800 --acts 200 --metric p999_ns \
        --out ${O}paired-trigger.json \
        --condition trigger-timer:none \
        --condition trigger-timer-hog:cpu-load:80:TEMPO_SLOT_US=8000 \
        --condition trigger-irq:none:0:TEMPO_TRIGGER=irq \
        --condition trigger-irq-hog:cpu-load:80:TEMPO_TRIGGER=irq,TEMPO_SLOT_US=8000
    S python3 benchmark/score/nested/trigger.py --paired ${O}paired-trigger.json
    ;;
trigger-completion)
    P python3 benchmark/run/paired.py --repeats 16 --pin 8,9,10,11 --vcpus 4 --frames 300 --period-ns 8000000 \
        --inject-s 6 --slopes 800 --acts 200 --metric p999_ns \
        --out ${O}paired-trigger-completion.json \
        --condition trigger-timer:none \
        --condition trigger-timer-hog:cpu-load:40:TEMPO_SLOT_US=8000 \
        --condition trigger-irq:none:0:TEMPO_TRIGGER=irq \
        --condition trigger-irq-hog:cpu-load:40:TEMPO_TRIGGER=irq,TEMPO_SLOT_US=8000
    S python3 benchmark/score/nested/trigger_completion.py --paired ${O}paired-trigger-completion.json
    ;;
quality)
    P python3 benchmark/analysis/quality/quality.py --slopes 2400 --acts 600 --reps 3000
    ;;
calibration)
    P python3 benchmark/run/paired.py --repeats 1 --frames 3000 --slopes 3200 --acts 500 --pin 8,9 \
        --out ${O}calibration.json \
        --condition calibration:wake:2000
    ;;
esac
[ "$DRY" = 1 ] && { [ "$RC" = 0 ] && echo "earlier_phase: plan ok -- nothing was run" || echo "earlier_phase: plan FAILS" >&2; }
exit "$RC"
