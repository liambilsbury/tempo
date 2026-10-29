// measure the time this vCPU was not executing from gaps in a clock loop
#define _GNU_SOURCE
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "steal_read.h"

// return CLOCK_MONOTONIC in ns
static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

// return the tick-based steal total from /proc/stat in ns
static uint64_t tick_steal_total(void)
{
    FILE *f = fopen("/proc/stat", "r");
    if (!f)
        return 0;
    char buf[1024];
    unsigned long long v[10] = {0};
    unsigned long long out = 0;
    if (fgets(buf, sizeof(buf), f)
        && sscanf(buf, "cpu %llu %llu %llu %llu %llu %llu %llu %llu",
                  &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7]) == 8)
        out = v[7];
    fclose(f);
    return out * 10000000ull;
}

// pin to a CPU, sum the clock gaps over the run and print them
int main(int argc, char **argv)
{
    int cpu = 1;
    long duration = 20, threshold = 2000;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(a, "--cpu") && v) { cpu = atoi(v); i++; }
        else if (!strcmp(a, "--duration-s") && v) { duration = atol(v); i++; }
        else if (!strcmp(a, "--threshold-ns") && v) { threshold = atol(v); i++; }
        else { fprintf(stderr, "gap_detector: unknown argument '%s'\n", a); return 2; }
    }

    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set)) {
        fprintf(stderr, "gap_detector: cannot pin to cpu %d\n", cpu);
        return 2;
    }
    struct sched_param sp = {.sched_priority = 90};
    if (sched_setscheduler(0, SCHED_FIFO, &sp))
        fprintf(stderr, "gap_detector: WARNING not real-time; guest-side preemption will "
                        "be counted as suspension\n");

    struct steal_counter s;
    int have_ns = (steal_counter_open(&s, cpu) == 0);
    uint64_t ns0 = have_ns ? steal_counter_read(&s) : 0;
    uint64_t tk0 = tick_steal_total();

    uint64_t cal_max = 0, prev = now_ns();
    for (int i = 0; i < 200000; i++) {
        uint64_t t = now_ns();
        uint64_t d = t - prev;
        if (d > cal_max)
            cal_max = d;
        prev = t;
    }

    uint64_t start = now_ns();
    uint64_t end = start + (uint64_t)duration * 1000000000ull;
    uint64_t gap_sum = 0, gap_max = 0, iters = 0;
    long gap_n = 0;
    prev = now_ns();
    while (prev < end) {
        uint64_t t = now_ns();
        uint64_t d = t - prev;
        prev = t;
        iters++;
        if (d > (uint64_t)threshold) {
            gap_sum += d;
            gap_n++;
            if (d > gap_max)
                gap_max = d;
        }
    }
    uint64_t wall = now_ns() - start;
    uint64_t ns1 = have_ns ? steal_counter_read(&s) : 0;
    uint64_t tk1 = tick_steal_total();

    printf("GAP_DETECTOR cpu=%d wall_ns=%llu iters=%llu threshold_ns=%ld cal_max_ns=%llu\n",
           cpu, (unsigned long long)wall, (unsigned long long)iters, threshold,
           (unsigned long long)cal_max);
    printf("GAP_DETECTOR gap_sum_ns=%llu gap_count=%ld gap_max_ns=%llu\n",
           (unsigned long long)gap_sum, gap_n, (unsigned long long)gap_max);
    printf("GAP_DETECTOR gap_fraction=%.6f\n", wall ? (double)gap_sum / (double)wall : 0.0);
    if (have_ns)
        printf("GAP_DETECTOR ns_steal_window_ns=%llu\n", (unsigned long long)(ns1 - ns0));
    else
        printf("GAP_DETECTOR ns_steal_window_ns=absent\n");
    printf("GAP_DETECTOR tick_steal_window_ns=%llu\n", (unsigned long long)(tk1 - tk0));
    if (have_ns)
        steal_counter_close(&s);
    return 0;
}
