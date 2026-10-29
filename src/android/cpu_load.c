// pin a spinner to each cpu and log its windows in CLOCK_BOOTTIME
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_CPUS 64

// a spinner's CPU and recorded windows
struct cpu_load {
    int cpu;
    int ran_cpu_first, ran_cpu_last;
    uint64_t held_ns;
    size_t n, cap;
    uint64_t *win;
};

static uint64_t epoch_ns, until_ns, slot_ns, on_ns;
static int rt_prio;

// convert a timespec to ns
static uint64_t ts_ns(struct timespec ts)
{
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

// return CLOCK_BOOTTIME in ns
static uint64_t boottime_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_BOOTTIME, &ts);
    return ts_ns(ts);
}

// sleep until an absolute CLOCK_BOOTTIME time
static void sleep_until(uint64_t t)
{
    struct timespec ts = {.tv_sec = (time_t)(t / 1000000000ull), .tv_nsec = (long)(t % 1000000000ull)};
    while (clock_nanosleep(CLOCK_BOOTTIME, TIMER_ABSTIME, &ts, NULL) == EINTR)
        ;
}

// print the errno message and exit 2
static void die(const char *what)
{
    fprintf(stderr, "cpu_load: %s: %s\n", what, strerror(errno));
    exit(2);
}

// run a cpu at duty percent of every slot, recording each window
static void *spin(void *v)
{
    struct cpu_load *h = v;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(h->cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0)
        die("sched_setaffinity");
    if (rt_prio) {
        struct sched_param sp = {.sched_priority = rt_prio};
        if (sched_setscheduler(0, SCHED_FIFO, &sp) != 0)
            die("sched_setscheduler");
    }
    h->ran_cpu_first = sched_getcpu();
    for (uint64_t slot = epoch_ns; slot < until_ns; slot += slot_ns) {
        sleep_until(slot);
        uint64_t a = boottime_ns();
        uint64_t end = slot + on_ns;
        uint64_t b = a;
        while (b < end)
            b = boottime_ns();
        if (h->n && h->win[2 * h->n - 1] == a)
            h->win[2 * h->n - 1] = b;
        else if (h->n < h->cap) {
            h->win[2 * h->n] = a;
            h->win[2 * h->n + 1] = b;
            h->n++;
        } else {
            fprintf(stderr, "cpu_load: cpu %d ran out of window slots at %zu\n", h->cpu, h->n);
            exit(2);
        }
    }
    h->ran_cpu_last = sched_getcpu();
    struct timespec ct;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ct);
    h->held_ns = ts_ns(ct);
    return NULL;
}

// print usage and exit 2
static void usage(void)
{
    fprintf(stderr, "usage: cpu_load --cpus N[,N...] --log PATH [--duration-s N] [--duty PCT] "
                    "[--slot-us N] [--rt-prio N]\n");
    exit(2);
}

// parse a flag's integer value, at least lo
static long num(const char *flag, const char *s, long lo)
{
    char *end;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || *end || v < lo) {
        fprintf(stderr, "cpu_load: bad %s value: %s\n", flag, s);
        exit(2);
    }
    return v;
}

// parse flags, run the spinners and write their windows to the log
int main(int argc, char **argv)
{
    const char *cpus = NULL, *logpath = NULL;
    long duration = 30, duty = 100, slot_us = 1000;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!v)
            usage();
        i++;
        if (!strcmp(a, "--cpus")) cpus = v;
        else if (!strcmp(a, "--log")) logpath = v;
        else if (!strcmp(a, "--duration-s")) duration = num(a, v, 1);
        else if (!strcmp(a, "--duty")) duty = num(a, v, 1);
        else if (!strcmp(a, "--slot-us")) slot_us = num(a, v, 1);
        else if (!strcmp(a, "--rt-prio")) rt_prio = (int)num(a, v, 0);
        else usage();
    }
    if (!cpus || !logpath || duty > 100)
        usage();

    struct cpu_load hs[MAX_CPUS];
    int n = 0;
    char *list = strdup(cpus);
    if (!list)
        die("strdup");
    for (char *t = strtok(list, ","); t; t = strtok(NULL, ",")) {
        if (n == MAX_CPUS) {
            fprintf(stderr, "cpu_load: more than %d cpus\n", MAX_CPUS);
            return 2;
        }
        memset(&hs[n], 0, sizeof(hs[n]));
        hs[n++].cpu = (int)num("--cpus", t, 0);
    }
    if (!n)
        usage();

    slot_ns = (uint64_t)slot_us * 1000ull;
    on_ns = slot_ns * (uint64_t)duty / 100;
    size_t cap = (size_t)((uint64_t)duration * 1000000000ull / slot_ns) + 2;
    for (int i = 0; i < n; i++) {
        hs[i].cap = cap;
        hs[i].win = calloc(cap, 2 * sizeof(uint64_t));
        if (!hs[i].win)
            die("calloc");
    }

    FILE *lf = fopen(logpath, "w");
    if (!lf)
        die("fopen log");

    uint64_t start = boottime_ns();
    epoch_ns = start + 100000000ull;
    until_ns = epoch_ns + (uint64_t)duration * 1000000000ull;
    fprintf(lf, "CPU_LOAD pid=%d cpus=%s duration_s=%ld duty=%ld slot_us=%ld rt_prio=%d clock=boottime\n",
            (int)getpid(), cpus, duration, duty, slot_us, rt_prio);
    fprintf(lf, "CPU_LOAD start_ns=%" PRIu64 "\n", start);
    fflush(lf);

    pthread_t th[MAX_CPUS];
    for (int i = 0; i < n; i++)
        if (pthread_create(&th[i], NULL, spin, &hs[i]) != 0)
            die("pthread_create");
    for (int i = 0; i < n; i++)
        pthread_join(th[i], NULL);

    for (int i = 0; i < n; i++) {
        fprintf(lf, "CPU_LOAD cpu=%d ran_cpu_first=%d ran_cpu_last=%d windows=%zu held_ns=%" PRIu64 "\n",
                hs[i].cpu, hs[i].ran_cpu_first, hs[i].ran_cpu_last, hs[i].n, hs[i].held_ns);
        for (size_t w = 0; w < hs[i].n; w++)
            fprintf(lf, "WIN cpu=%d start_ns=%" PRIu64 " end_ns=%" PRIu64 "\n",
                    hs[i].cpu, hs[i].win[2 * w], hs[i].win[2 * w + 1]);
    }
    fprintf(lf, "CPU_LOAD end_ns=%" PRIu64 "\nCPU_LOAD done\n", boottime_ns());
    if (fclose(lf) != 0)
        die("fclose log");
    return 0;
}
