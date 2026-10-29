// inject interference at the L1 cores an L2 guest's vCPUs are pinned to
#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <dirent.h>
#include <time.h>
#include <unistd.h>

#define TEMPO_MAX_VCPU 8

static atomic_int running = 1;
static long rate_hz = 1000;
static double duty = 100;
static long slot_us = 1000;
static size_t map_bytes = 2u << 20;
static int rt_prio = 0;
static uint64_t epoch;
static char watch_comm[64] = "qemu";

// CPU figures of the watched process and its vCPU threads
struct wstat {
    long     pid;
    uint64_t cpu_ns;
    uint64_t guest_ns;
    uint64_t runtime_ns;
    uint64_t rundelay_ns;
    uint64_t vcpu_rundelay_ns;
    int      threads;
    int      vcpus;
    uint64_t per_runtime_ns[TEMPO_MAX_VCPU];
    uint64_t per_rundelay_ns[TEMPO_MAX_VCPU];
};

// sum each thread's runtime and run delay from schedstat
static void task_sched_ns(const char *pid, struct wstat *w)
{
    char dirp[320];
    snprintf(dirp, sizeof(dirp), "/proc/%s/task", pid);
    DIR *td = opendir(dirp);
    if (!td)
        return;
    struct dirent *te;
    while ((te = readdir(td))) {
        if (te->d_name[0] < '0' || te->d_name[0] > '9')
            continue;
        char path[700], buf[256];
        snprintf(path, sizeof(path), "%s/%s/schedstat", dirp, te->d_name);
        FILE *f = fopen(path, "r");
        if (!f)
            continue;
        buf[0] = 0;
        int got = 0;
        unsigned long long rt = 0, rd = 0;
        if (fgets(buf, sizeof(buf), f))
            got = sscanf(buf, "%llu %llu", &rt, &rd);
        fclose(f);
        if (got < 1)
            continue;
        w->runtime_ns += rt;
        w->rundelay_ns += rd;
        w->threads++;

        snprintf(path, sizeof(path), "%s/%s/comm", dirp, te->d_name);
        f = fopen(path, "r");
        if (!f)
            continue;
        buf[0] = 0;
        if (fgets(buf, sizeof(buf), f) && !strncmp(buf, "CPU ", 4) && strstr(buf, "/KVM")) {
            w->vcpu_rundelay_ns += rd;
            w->vcpus++;
            int idx = atoi(buf + 4);
            if (idx >= 0 && idx < TEMPO_MAX_VCPU) {
                w->per_runtime_ns[idx] = rt;
                w->per_rundelay_ns[idx] = rd;
            }
        }
        fclose(f);
    }
    closedir(td);
}

// read the Nth field after the comm in /proc/<pid>/stat
static int stat_field(const char *after_paren, int want, unsigned long long *out)
{
    const char *p = after_paren;
    for (int idx = 3; *p; idx++) {
        while (*p == ' ') p++;
        if (!*p) break;
        if (idx == want) {
            char *end;
            unsigned long long v = strtoull(p, &end, 10);
            if (end == p)
                return 0;
            *out = v;
            return 1;
        }
        while (*p && *p != ' ') p++;
    }
    return 0;
}

// read the CPU figures of the busiest process whose comm matches --watch
static struct wstat watched_stat(void)
{
    struct wstat best = {0};
    char best_name[NAME_MAX + 1] = "";
    DIR *d = opendir("/proc");
    if (!d)
        return best;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9')
            continue;
        char path[320], buf[2048];
        snprintf(path, sizeof(path), "/proc/%s/comm", e->d_name);
        FILE *f = fopen(path, "r");
        if (!f)
            continue;
        buf[0] = 0;
        if (!fgets(buf, sizeof(buf), f)) { fclose(f); continue; }
        fclose(f);
        if (!strstr(buf, watch_comm))
            continue;
        snprintf(path, sizeof(path), "/proc/%s/stat", e->d_name);
        f = fopen(path, "r");
        if (!f)
            continue;
        size_t n = fread(buf, 1, sizeof(buf) - 1, f);
        fclose(f);
        buf[n] = 0;
        char *close_paren = strrchr(buf, ')');
        if (!close_paren)
            continue;
        unsigned long long ut = 0, st = 0, gt = 0;
        if (!stat_field(close_paren + 1, 14, &ut) || !stat_field(close_paren + 1, 15, &st))
            continue;
        (void)stat_field(close_paren + 1, 43, &gt);
        uint64_t v = (ut + st) * (1000000000ull / 100);
        if (v > best.cpu_ns) {
            best.cpu_ns = v;
            best.guest_ns = gt * (1000000000ull / 100);
            best.pid = atol(e->d_name);
            snprintf(best_name, sizeof(best_name), "%s", e->d_name);
        }
    }
    closedir(d);
    if (best.pid)
        task_sched_ns(best_name, &best);
    return best;
}

static struct wstat wstat_last;
static uint64_t wstat_last_ns;
static int wstat_gone;

static uint64_t now_ns(void);

// sample the watched process until the deadline
static void wait_and_watch(uint64_t until, struct wstat before)
{
    wstat_last = before;
    wstat_last_ns = now_ns();
    while (now_ns() < until) {
        usleep(50000);
        struct wstat s = watched_stat();
        if (s.pid && (!before.pid || s.pid == before.pid) && s.cpu_ns >= wstat_last.cpu_ns) {
            wstat_last = s;
            wstat_last_ns = now_ns();
        } else if (!s.pid && before.pid) {
            wstat_gone = 1;
        }
    }
}

// print every window figure for the watched process
static void report_window(struct wstat before, uint64_t win_start_ns)
{
    struct wstat after = wstat_last;
    uint64_t cpu = after.cpu_ns > before.cpu_ns ? after.cpu_ns - before.cpu_ns : 0;
    uint64_t guest = after.guest_ns > before.guest_ns ? after.guest_ns - before.guest_ns : 0;
    printf("INJECT watched_wall_ns=%llu\n",
           (unsigned long long)(wstat_last_ns > win_start_ns ? wstat_last_ns - win_start_ns : 0));
    printf("INJECT watched_window_start_ns=%llu\n", (unsigned long long)win_start_ns);
    printf("INJECT watched_window_end_ns=%llu\n", (unsigned long long)wstat_last_ns);
    printf("INJECT watched_gone=%d\n", wstat_gone);
    printf("INJECT watched_pid=%ld\n", after.pid ? after.pid : before.pid);
    printf("INJECT watched_cpu_before_ns=%llu\n", (unsigned long long)before.cpu_ns);
    printf("INJECT watched_cpu_after_ns=%llu\n", (unsigned long long)after.cpu_ns);
    printf("INJECT watched_cpu_window_ns=%llu\n", (unsigned long long)cpu);
    printf("INJECT watched_guest_ticksampled_window_ns=%llu\n", (unsigned long long)guest);
    printf("INJECT watched_runtime_before_ns=%llu\n", (unsigned long long)before.runtime_ns);
    printf("INJECT watched_runtime_after_ns=%llu\n", (unsigned long long)after.runtime_ns);
    printf("INJECT watched_runtime_window_ns=%llu\n",
           (unsigned long long)(after.runtime_ns > before.runtime_ns
                                ? after.runtime_ns - before.runtime_ns : 0));
    printf("INJECT watched_threads=%d\n", after.threads ? after.threads : before.threads);
    printf("INJECT watched_vcpus=%d\n", after.vcpus ? after.vcpus : before.vcpus);
    printf("INJECT watched_vcpu_rundelay_window_ns=%llu\n",
           (unsigned long long)(after.vcpu_rundelay_ns > before.vcpu_rundelay_ns
                                ? after.vcpu_rundelay_ns - before.vcpu_rundelay_ns : 0));
    printf("INJECT watched_rundelay_window_ns=%llu\n",
           (unsigned long long)(after.rundelay_ns > before.rundelay_ns
                                ? after.rundelay_ns - before.rundelay_ns : 0));
    for (int i = 0; i < TEMPO_MAX_VCPU; i++) {
        if (!after.per_runtime_ns[i] && !before.per_runtime_ns[i])
            continue;
        printf("INJECT watched_vcpu%d_runtime_window_ns=%llu\n", i,
               (unsigned long long)(after.per_runtime_ns[i] > before.per_runtime_ns[i]
                                    ? after.per_runtime_ns[i] - before.per_runtime_ns[i] : 0));
        printf("INJECT watched_vcpu%d_rundelay_window_ns=%llu\n", i,
               (unsigned long long)(after.per_rundelay_ns[i] > before.per_rundelay_ns[i]
                                    ? after.per_rundelay_ns[i] - before.per_rundelay_ns[i] : 0));
    }
}
static void *shared_map;

// return CLOCK_MONOTONIC in ns
static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

// run this thread at SCHED_FIFO --rt-prio
static void make_rt(void)
{
    if (!rt_prio)
        return;
    struct sched_param sp = {.sched_priority = rt_prio};
    if (sched_setscheduler(0, SCHED_FIFO, &sp) != 0) {
        fprintf(stderr, "inject: SCHED_FIFO prio %d: %s (run under sudo, or the injection "
                        "will not preempt anything)\n", rt_prio, strerror(errno));
        exit(2);
    }
}

static void pin(int cpu)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        fprintf(stderr, "inject: pin to cpu %d: %s\n", cpu, strerror(errno));
        exit(2);
    }
}

// sleep until an absolute CLOCK_MONOTONIC deadline
static void wait_until(uint64_t deadline_ns)
{
    struct timespec ts = {.tv_sec = (time_t)(deadline_ns / 1000000000ull),
                          .tv_nsec = (long)(deadline_ns % 1000000000ull)};
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR)
        ;
}

// per-thread injector arguments
struct arg {
    int cpu;
    int partner;
    uint64_t until;
};

static pthread_mutex_t wake_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t wake_cond = PTHREAD_COND_INITIALIZER;
static atomic_long wake_seq;

// wait on the condition variable, pinned to a target core
static void *wake_target(void *v)
{
    struct arg *a = v;
    pin(a->cpu);
    long seen = 0;
    pthread_mutex_lock(&wake_lock);
    while (atomic_load(&running)) {
        while (atomic_load(&wake_seq) == seen && atomic_load(&running))
            pthread_cond_wait(&wake_cond, &wake_lock);
        seen = atomic_load(&wake_seq);
    }
    pthread_mutex_unlock(&wake_lock);
    return NULL;
}

// wake the target threads at --rate-hz from the partner core
static void *wake_source(void *v)
{
    struct arg *a = v;
    pin(a->partner);
    uint64_t period = rate_hz > 0 ? 1000000000ull / (uint64_t)rate_hz : 1000000ull;
    uint64_t next = now_ns() + period;
    while (atomic_load(&running) && now_ns() < a->until) {
        pthread_mutex_lock(&wake_lock);
        atomic_fetch_add(&wake_seq, 1);
        pthread_cond_broadcast(&wake_cond);
        pthread_mutex_unlock(&wake_lock);
        wait_until(next);
        next += period;
    }
    return NULL;
}

// read every page of the shared mapping in a tight loop on a target core
static void *tlb_toucher(void *v)
{
    struct arg *a = v;
    pin(a->cpu);
    volatile char *p = shared_map;
    while (atomic_load(&running) && now_ns() < a->until) {
        for (size_t i = 0; i < map_bytes; i += 4096)
            (void)p[i];
    }
    return NULL;
}

// mprotect the shared mapping at --rate-hz
static void *tlb_source(void *v)
{
    struct arg *a = v;
    pin(a->partner);
    uint64_t period = rate_hz > 0 ? 1000000000ull / (uint64_t)rate_hz : 1000000ull;
    uint64_t next = now_ns() + period;
    while (atomic_load(&running) && now_ns() < a->until) {
        mprotect(shared_map, map_bytes, PROT_READ);
        mprotect(shared_map, map_bytes, PROT_READ | PROT_WRITE);
        wait_until(next);
        next += period;
    }
    return NULL;
}

// read every page of the shared mapping once per period, then sleep
static void *tlbq_toucher(void *v)
{
    struct arg *a = v;
    pin(a->cpu);
    volatile char *p = shared_map;
    uint64_t period = rate_hz > 0 ? 1000000000ull / (uint64_t)rate_hz : 1000000ull;
    uint64_t next = now_ns() + period;
    while (atomic_load(&running) && now_ns() < a->until) {
        for (size_t i = 0; i < map_bytes; i += 4096)
            (void)p[i];
        wait_until(next);
        next += period;
    }
    return NULL;
}

// stream reads through a 256 MiB buffer on a core the guest does not use
static void *cache_thrash(void *v)
{
    struct arg *a = v;
    pin(a->cpu);
    size_t n = 256u << 20;
    volatile unsigned char *buf = mmap(NULL, n, PROT_READ | PROT_WRITE,
                                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (buf == MAP_FAILED)
        return NULL;
    memset((void *)buf, 1, n);
    unsigned long acc = 0;
    while (atomic_load(&running) && now_ns() < a->until) {
        for (size_t i = 0; i < n; i += 64)
            acc += buf[i];
    }
    munmap((void *)buf, n);
    (void)acc;
    return NULL;
}

// return the on-time of a slot at --duty
static uint64_t hog_on_ns(uint64_t slot)
{
    return (uint64_t)((double)slot * duty / 100.0);
}

// spin for the on-time of each slot, aligned to the shared epoch
static void *cpu_load(void *v)
{
    struct arg *a = v;
    pin(a->cpu);
    make_rt();
    uint64_t slot = (uint64_t)slot_us * 1000ull;
    uint64_t on = hog_on_ns(slot);
    while (atomic_load(&running) && now_ns() < a->until) {
        uint64_t now = now_ns();
        uint64_t k = (now - epoch) / slot + 1;
        uint64_t start = epoch + k * slot;
        wait_until(start);
        uint64_t end = start + on;
        while (now_ns() < end && atomic_load(&running))
            __asm__ __volatile__("" ::: "memory");
    }
    return NULL;
}

// parse flags, start the threads for the mode and print the window figures
int main(int argc, char **argv)
{
    const char *mode = NULL;
    char *cpus = NULL;
    long duration = 30;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
#define NEED() do { if (!v) goto usage; i++; } while (0)
        if (!strcmp(a, "--mode")) { NEED(); mode = v; }
        else if (!strcmp(a, "--cpus")) { NEED(); cpus = strdup(v); }
        else if (!strcmp(a, "--duration-s")) { NEED(); duration = atol(v); }
        else if (!strcmp(a, "--rate-hz")) { NEED(); rate_hz = atol(v); }
        else if (!strcmp(a, "--duty")) { NEED(); duty = atof(v); }
        else if (!strcmp(a, "--slot-us")) { NEED(); slot_us = atol(v); }
        else if (!strcmp(a, "--rt-prio")) { NEED(); rt_prio = atoi(v); }
        else if (!strcmp(a, "--watch")) { NEED(); snprintf(watch_comm, sizeof(watch_comm), "%s", v); }
        else goto usage;
#undef NEED
    }
    if (!mode || !cpus) goto usage;
    if (strcmp(mode, "none") && strcmp(mode, "wake") && strcmp(mode, "tlb") && strcmp(mode, "tlbq")
        && strcmp(mode, "cache") && strcmp(mode, "cpu-load")) {
        fprintf(stderr, "inject: unknown --mode '%s'\n", mode);
        goto usage;
    }

    char *cpus_shown = strdup(cpus);
    int target[64], n = 0;
    for (char *t = strtok(cpus, ","); t && n < 64; t = strtok(NULL, ","))
        target[n++] = atoi(t);
    if (!n) goto usage;

    cpu_set_t usable;
    CPU_ZERO(&usable);
    int ncpu = CPU_SETSIZE;
    if (sched_getaffinity(0, sizeof(usable), &usable) != 0) {
        ncpu = (int)sysconf(_SC_NPROCESSORS_ONLN);
        for (int c = 0; c < ncpu; c++) CPU_SET(c, &usable);
    }
    int partner = -1;
    for (int c = ncpu - 1; c >= 0 && partner < 0; c--) {
        if (!CPU_ISSET(c, &usable)) continue;
        int used = 0;
        for (int i = 0; i < n; i++) if (target[i] == c) used = 1;
        if (!used) partner = c;
    }
    if (partner < 0) {
        fprintf(stderr, "inject: every core is a target; no core left to inject from\n");
        return 2;
    }

    epoch = now_ns() + 100000000ull;
    uint64_t until = now_ns() + (uint64_t)duration * 1000000000ull;
    printf("INJECT mode=%s cpus=%s partner=%d duration_s=%ld rate_hz=%ld duty=%g rt_prio=%d "
           "slot_us=%ld\n",
           mode, cpus_shown, partner, duration, rate_hz, duty, rt_prio, slot_us);
    if (!strcmp(mode, "cpu-load"))
        printf("INJECT hog_on_ns=%llu\n", (unsigned long long)hog_on_ns((uint64_t)slot_us * 1000ull));
    printf("INJECT start_ns=%llu\n", (unsigned long long)now_ns());
    fflush(stdout);

    struct wstat watched_before = watched_stat();
    uint64_t win_start = now_ns();

    if (!strcmp(mode, "none")) {
        wait_and_watch(until, watched_before);
        printf("INJECT end_ns=%llu\n", (unsigned long long)now_ns());
        printf("INJECT cpu_consumed_ns=0\n");
        report_window(watched_before, win_start);
        printf("INJECT done\n");
        return 0;
    }

    int is_tlb = !strcmp(mode, "tlb") || !strcmp(mode, "tlbq");
    if (is_tlb) {
        shared_map = mmap(NULL, map_bytes, PROT_READ | PROT_WRITE,
                          MAP_SHARED | MAP_ANONYMOUS, -1, 0);
        if (shared_map == MAP_FAILED) { perror("mmap"); return 2; }
        memset(shared_map, 1, map_bytes);
    }

    pthread_t th[130];
    struct arg args[130];
    int nt = 0;

    if (!strcmp(mode, "cache")) {
        int placed = 0;
        for (int c = ncpu - 1; c >= 0 && placed < n; c--) {
            if (!CPU_ISSET(c, &usable)) continue;
            int used = 0;
            for (int i = 0; i < n; i++) if (target[i] == c) used = 1;
            if (used)
                continue;
            args[nt] = (struct arg){.cpu = c, .partner = partner, .until = until};
            pthread_create(&th[nt], NULL, cache_thrash, &args[nt]);
            nt++;
            placed++;
        }
        if (!placed) {
            fprintf(stderr, "inject: no core outside the target set to put cache pressure on\n");
            return 2;
        }
        printf("INJECT cache_threads=%d\n", placed);
    } else {
        for (int i = 0; i < n; i++) {
            args[nt] = (struct arg){.cpu = target[i], .partner = partner, .until = until};
            if (!strcmp(mode, "wake")) pthread_create(&th[nt], NULL, wake_target, &args[nt]);
            else if (!strcmp(mode, "tlb")) pthread_create(&th[nt], NULL, tlb_toucher, &args[nt]);
            else if (!strcmp(mode, "tlbq")) pthread_create(&th[nt], NULL, tlbq_toucher, &args[nt]);
            else if (!strcmp(mode, "cpu-load")) pthread_create(&th[nt], NULL, cpu_load, &args[nt]);
            else goto usage;
            nt++;
        }
        if (!strcmp(mode, "wake") || is_tlb) {
            args[nt] = (struct arg){.cpu = partner, .partner = partner, .until = until};
            pthread_create(&th[nt], NULL,
                           !strcmp(mode, "wake") ? wake_source : tlb_source, &args[nt]);
            nt++;
        }
    }

    wait_and_watch(until, watched_before);
    atomic_store(&running, 0);
    pthread_mutex_lock(&wake_lock);
    pthread_cond_broadcast(&wake_cond);
    pthread_mutex_unlock(&wake_lock);
    for (int i = 0; i < nt; i++)
        pthread_join(th[i], NULL);

    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    uint64_t cpu_ns = ((uint64_t)ru.ru_utime.tv_sec + (uint64_t)ru.ru_stime.tv_sec) * 1000000000ull
                    + ((uint64_t)ru.ru_utime.tv_usec + (uint64_t)ru.ru_stime.tv_usec) * 1000ull;
    printf("INJECT end_ns=%llu\n", (unsigned long long)now_ns());
    printf("INJECT cpu_consumed_ns=%llu\n", (unsigned long long)cpu_ns);
    report_window(watched_before, win_start);
    printf("INJECT done\n");
    return 0;

usage:
    fprintf(stderr,
            "usage: inject --mode wake|tlb|tlbq|cache|cpu-load --cpus N[,N...] [--duration-s N]\n"
            "              [--rate-hz N] [--duty PCT] [--rt-prio N] [--slot-us N]\n"
            "  --rt-prio N   cpu-load threads at SCHED_FIFO priority N\n"
            "  --slot-us N   cpu-load on/off period in us, default 1000\n");
    return 2;
}
