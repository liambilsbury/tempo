// sample the nanosecond and tick steal counters over a run
#define _GNU_SOURCE
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "steal_read.h"

// a sample of the two steal counters
struct sample {
    uint64_t mono_ns;
    uint64_t ns_steal;
    uint64_t tick_steal;
};

// return CLOCK_MONOTONIC in ns
static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

// return the steal ticks from /proc/stat's cpu line
static uint64_t tick_steal(void)
{
    FILE *f = fopen("/proc/stat", "r");
    if (!f)
        return 0;
    char buf[1024];
    uint64_t v[10] = {0};
    uint64_t out = 0;
    if (fgets(buf, sizeof(buf), f)) {
        if (sscanf(buf, "cpu %llu %llu %llu %llu %llu %llu %llu %llu %llu %llu",
                   (unsigned long long *)&v[0], (unsigned long long *)&v[1],
                   (unsigned long long *)&v[2], (unsigned long long *)&v[3],
                   (unsigned long long *)&v[4], (unsigned long long *)&v[5],
                   (unsigned long long *)&v[6], (unsigned long long *)&v[7],
                   (unsigned long long *)&v[8], (unsigned long long *)&v[9]) >= 8)
            out = v[7];
    }
    fclose(f);
    return out;
}

// sleep, then spin until the deadline
static void wait_until(uint64_t deadline)
{
    for (;;) {
        uint64_t n = now_ns();
        if (n >= deadline)
            return;
        uint64_t left = deadline - n;
        if (left > 200000) {
            struct timespec ts = {.tv_sec = 0, .tv_nsec = (long)(left - 100000)};
            nanosleep(&ts, NULL);
        }
    }
}

// sample both steal counters and print how far the tick counter lags
int main(int argc, char **argv)
{
    int cpu = 0;
    long samples = 5000, period = 1000000;
    const char *out = NULL;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(a, "--cpu") && v) { cpu = atoi(v); i++; }
        else if (!strcmp(a, "--samples") && v) { samples = atol(v); i++; }
        else if (!strcmp(a, "--period-ns") && v) { period = atol(v); i++; }
        else if (!strcmp(a, "--out") && v) { out = v; i++; }
        else {
            fprintf(stderr, "steal_probe: unknown argument '%s'\n", a);
            return 2;
        }
    }

    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set)) {
        fprintf(stderr, "steal_probe: cannot pin to cpu %d\n", cpu);
        return 2;
    }

    struct steal_counter s;
    if (steal_counter_open(&s, cpu)) {
        fprintf(stderr, "steal_probe: NO NANOSECOND COUNTER on cpu %d -- the module is not "
                        "loaded, or paravirt steal time is off. Refusing to report zeros.\n",
                cpu);
        return 3;
    }

    uint64_t t0 = now_ns();
    volatile uint64_t sink = 0;
    for (int i = 0; i < 100000; i++)
        sink += steal_counter_read(&s);
    uint64_t per_read = (now_ns() - t0) / 100000;
    (void)sink;

    struct sample *rec = calloc((size_t)samples, sizeof(*rec));
    if (!rec) {
        steal_counter_close(&s);
        return 2;
    }

    uint64_t next = now_ns() + (uint64_t)period;
    for (long i = 0; i < samples; i++) {
        rec[i].mono_ns = now_ns();
        rec[i].ns_steal = steal_counter_read(&s);
        rec[i].tick_steal = tick_steal();
        wait_until(next);
        next += (uint64_t)period;
    }

    uint64_t d_ns = rec[samples - 1].ns_steal - rec[0].ns_steal;
    uint64_t d_tick = rec[samples - 1].tick_steal - rec[0].tick_steal;

    uint64_t worst_lag = 0;
    for (long i = 0; i < samples; i++) {
        uint64_t ns = rec[i].ns_steal - rec[0].ns_steal;
        uint64_t tk = (rec[i].tick_steal - rec[0].tick_steal) * 10000000ull;
        if (ns > tk && ns - tk > worst_lag)
            worst_lag = ns - tk;
    }

    printf("STEAL samples=%ld period_ns=%ld cpu=%d read_cost_ns=%llu\n",
           samples, period, cpu, (unsigned long long)per_read);
    printf("STEAL window_ns=%llu\n",
           (unsigned long long)(rec[samples - 1].mono_ns - rec[0].mono_ns));
    printf("STEAL ns_steal_window_ns=%llu\n", (unsigned long long)d_ns);
    printf("STEAL tick_steal_window_ticks=%llu\n", (unsigned long long)d_tick);
    printf("STEAL tick_steal_window_ns=%llu\n", (unsigned long long)(d_tick * 10000000ull));
    printf("STEAL worst_lag_ns=%llu\n", (unsigned long long)worst_lag);
    printf("STEAL periods_of_lag=%llu\n", (unsigned long long)(worst_lag / (uint64_t)period));

    if (out) {
        FILE *f = fopen(out, "wb");
        if (f) {
            fwrite(rec, sizeof(*rec), (size_t)samples, f);
            fclose(f);
            printf("STEAL records=%s bytes=%zu\n", out, (size_t)samples * sizeof(*rec));
        }
    }
    free(rec);
    steal_counter_close(&s);
    return 0;
}
