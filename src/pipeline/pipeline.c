// run a stage of the sensor, compute, actuator, telemetry pipeline over ImageStreamIO
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "ImageStreamIO.h"
#include "ImageStruct.h"
#include "frame.h"
#include "reconstruct.h"
#include "reserve.h"

#ifndef telemetry_frame_written
#define telemetry_frame_written() ((void)0)
#endif

// attributes for sched_setattr
struct sched_attr_ {
    uint32_t size, sched_policy;
    uint64_t sched_flags;
    int32_t sched_nice;
    uint32_t sched_priority;
    uint64_t sched_runtime, sched_deadline, sched_period;
};

// call sched_setattr directly
static int sched_setattr_(pid_t pid, struct sched_attr_ *a, unsigned int flags)
{
    return (int)syscall(__NR_sched_setattr, pid, a, flags);
}


// command-line options of a stage
struct opt {
    const char *stage;
    const char *tag;
    const char *out;
    long slopes, acts;
    long period_ns;
    long frames;
    int cpu;
    const char *policy;
    int prio;
    long runtime_ns;
    const char *reconstruct;
    long modes;
    int tier;
    double alpha;
    long stim;
    const char *adapt;
    long block;
    long block_ns;
    long gap_threshold_ns;
    long reserve_ns;
    int curve;
    const char *trigger;
    const char *irq_dev;
};

// print the errno message and exit 2
static void die(const char *what)
{
    fprintf(stderr, "pipeline: %s: %s\n", what, strerror(errno));
    exit(2);
}


// apply the CPU pin and scheduling policy and lock memory
static void apply_sched(const struct opt *o)
{
    if (o->cpu >= 0) {
        cpu_set_t set;
        CPU_ZERO(&set);
        CPU_SET(o->cpu, &set);
        if (sched_setaffinity(0, sizeof(set), &set) != 0)
            die("sched_setaffinity");
    }

    if (strcmp(o->policy, "fifo") == 0) {
        struct sched_param p = {.sched_priority = o->prio};
        if (sched_setscheduler(0, SCHED_FIFO, &p) != 0)
            die("sched_setscheduler(SCHED_FIFO)");
    } else if (strcmp(o->policy, "deadline") == 0) {
        struct sched_attr_ a;
        memset(&a, 0, sizeof(a));
        a.size = sizeof(a);
        a.sched_policy = 6;
        a.sched_runtime = o->runtime_ns ? (uint64_t)o->runtime_ns : (uint64_t)o->period_ns / 2;
        a.sched_deadline = (uint64_t)o->period_ns;
        a.sched_period = (uint64_t)o->period_ns;
        if (sched_setattr_(0, &a, 0) != 0)
            die("sched_setattr(SCHED_DEADLINE)");
    } else if (strcmp(o->policy, "other") != 0) {
        fprintf(stderr, "pipeline: unknown --sched '%s'\n", o->policy);
        exit(2);
    }

    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
        fprintf(stderr, "pipeline: mlockall failed (%s) -- page faults will pollute the tail\n",
                strerror(errno));

    printf("OBS policy=%s\nOBS prio=%d\nOBS cpu=%d\nOBS period_ns=%ld\n",
           o->policy, o->prio, o->cpu, o->period_ns);
    fflush(stdout);
}


// build a stream name from the run tag
static void stream_name(char *dst, size_t n, const char *tag, const char *which)
{
    snprintf(dst, n, "tempo_%s_%s", tag, which);
}

// create a stream of the header plus payload floats
static void create_stream(IMAGE *im, const struct opt *o, const char *which, long payload)
{
    char name[128];
    uint32_t size[1];
    stream_name(name, sizeof(name), o->tag, which);
    size[0] = (uint32_t)(HDR_FLOATS + payload);
    if (ImageStreamIO_createIm(im, name, 1, size, _DATATYPE_FLOAT, 1, 10, 0) != IMAGESTREAMIO_SUCCESS) {
        fprintf(stderr, "pipeline: could not create stream %s\n", name);
        exit(2);
    }
    printf("OBS created=%s\n", name);
    fflush(stdout);
}

// wait for a stream to exist and open it
static void open_stream(IMAGE *im, const struct opt *o, const char *which, long expect)
{
    char name[128], path[512];
    stream_name(name, sizeof(name), o->tag, which);
    if (ImageStreamIO_filename(path, sizeof(path), name) != IMAGESTREAMIO_SUCCESS) {
        fprintf(stderr, "pipeline: could not resolve a path for stream %s\n", name);
        exit(2);
    }

    int ok = 0;
    for (int i = 0; i < 500; i++) {
        struct stat st;
        if (stat(path, &st) == 0 && st.st_size > (off_t)sizeof(IMAGE_METADATA)) {
            char ver[32] = {0};
            FILE *vf = fopen(path, "rb");
            if (vf) {
                size_t got = fread(ver, 1, sizeof(ver) - 1, vf);
                fclose(vf);
                if (got == sizeof(ver) - 1 && ver[0] != '\0') {
                    ok = 1;
                    break;
                }
            }
        }
        usleep(20000);
    }
    if (!ok) {
        fprintf(stderr, "pipeline: stream %s (%s) never appeared\n", name, path);
        exit(2);
    }
    int opened = 0;
    for (int i = 0; i < 25; i++) {
        if (ImageStreamIO_openIm(im, name) == IMAGESTREAMIO_SUCCESS && im->md
            && im->md->nelement) {
            opened = 1;
            break;
        }
        usleep(20000);
    }
    if (!opened) {
        fprintf(stderr, "pipeline: stream %s exists but would not open\n", name);
        exit(2);
    }

    uint64_t want = (uint64_t)(HDR_FLOATS + expect);
    if (im->md->nelement != want) {
        fprintf(stderr,
                "pipeline: stream %s carries %llu floats, this stage expects %llu; "
                "--slopes/--acts disagree between stages\n",
                name, (unsigned long long)im->md->nelement, (unsigned long long)want);
        exit(2);
    }
    if (im->md->datatype != _DATATYPE_FLOAT) {
        fprintf(stderr, "pipeline: stream %s is datatype %d, expected FLOAT\n",
                name, (int)im->md->datatype);
        exit(2);
    }
    printf("OBS opened=%s\nOBS %s_nelement=%llu\n",
           name, which, (unsigned long long)im->md->nelement);
    fflush(stdout);
}

// publish a stream's new frame
static void publish(IMAGE *im)
{
    ImageStreamIO_UpdateIm(im);
}


// build the path of a stage's ready marker
static void ready_path(char *dst, size_t n, const struct opt *o, const char *stage)
{
    char sample[512];
    ImageStreamIO_filename(sample, sizeof(sample), "x");
    char *slash = strrchr(sample, '/');
    if (slash) *slash = '\0'; else strcpy(sample, "/tmp");
    snprintf(dst, n, "%s/tempo_%s_%s.ready", sample, o->tag, stage);
}

// create a stage's ready marker
static void mark_ready(const struct opt *o, const char *stage)
{
    char p[640];
    ready_path(p, sizeof(p), o, stage);
    FILE *fp = fopen(p, "w");
    if (!fp) die("mark_ready");
    fclose(fp);
    printf("OBS ready=%s\n", stage);
    fflush(stdout);
}

// wait up to 30 s for each stage's ready marker
static void wait_ready(const struct opt *o, const char *const *stages, int n)
{
    for (int i = 0; i < n; i++) {
        char p[640];
        struct stat st;
        ready_path(p, sizeof(p), o, stages[i]);
        int ok = 0;
        for (int t = 0; t < 1500 && !ok; t++) {
            if (stat(p, &st) == 0) ok = 1;
            else usleep(20000);
        }
        if (!ok) {
            fprintf(stderr, "pipeline: %s never became ready (%s)\n", stages[i], p);
            exit(2);
        }
    }
    printf("OBS downstream_ready=1\n");
    fflush(stdout);
}

// remove a stage's ready marker
static void clear_ready(const struct opt *o, const char *stage)
{
    char p[640];
    ready_path(p, sizeof(p), o, stage);
    unlink(p);
}


#define STALL_SECONDS 5

// wait for the stream counter to advance, return the advance or -1 on a stall
static int wait_frame(IMAGE *im, int sem, uint64_t *last, uint64_t *spurious)
{
    for (;;) {
        struct timespec to;
        clock_gettime(CLOCK_REALTIME, &to);
        to.tv_sec += STALL_SECONDS;
        if (ImageStreamIO_semtimedwait(im, sem, &to) != 0)
            return -1;
        uint64_t c = im->md->cnt0;
        if (c != *last) {
            uint64_t advance = c - *last;
            *last = c;
            return (int)(advance > INT32_MAX ? INT32_MAX : advance);
        }
        (*spurious)++;
    }
}


// open the irq device in raw mode
static int open_irq_dev(const char *path)
{
    int fd = open(path, O_RDONLY | O_NOCTTY);
    if (fd < 0)
        die("open irq-dev");
    struct termios t;
    if (tcgetattr(fd, &t) != 0)
        die("tcgetattr irq-dev");
    cfmakeraw(&t);
    t.c_cflag |= CLOCAL;
    t.c_cc[VMIN] = 1;
    t.c_cc[VTIME] = 0;
    if (tcsetattr(fd, TCSANOW, &t) != 0)
        die("tcsetattr irq-dev");
    return fd;
}

// generate a frame each period or each irq byte and publish it on s0
static int stage_sensor(const struct opt *o)
{
    IMAGE s0;
    clear_ready(o, "compute");
    clear_ready(o, "actuator");
    clear_ready(o, "telemetry");
    create_stream(&s0, o, STREAM_S0, o->slopes);
    apply_sched(o);

    static const char *const consumers[] = {"compute", "actuator", "telemetry"};
    wait_ready(o, consumers, 3);

    int irq_fd = !strcmp(o->trigger, "irq") ? open_irq_dev(o->irq_dev) : -1;

    {
        struct timespec m0, rt, m1;
        clock_gettime(CLOCK_MONOTONIC, &m0);
        clock_gettime(CLOCK_REALTIME, &rt);
        clock_gettime(CLOCK_MONOTONIC, &m1);
        uint64_t a = (uint64_t)m0.tv_sec * 1000000000ull + (uint64_t)m0.tv_nsec;
        uint64_t b = (uint64_t)m1.tv_sec * 1000000000ull + (uint64_t)m1.tv_nsec;
        printf("OBS clock_pair_real_ns=%llu\nOBS clock_pair_mono_ns=%llu\n",
               (unsigned long long)((uint64_t)rt.tv_sec * 1000000000ull + (uint64_t)rt.tv_nsec),
               (unsigned long long)(a + (b - a) / 2));
        fflush(stdout);
    }

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);
    next.tv_nsec += o->period_ns;
    while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }

    for (long f = 0; f < o->frames; f++) {
        if (irq_fd >= 0) {
            uint8_t b;
            ssize_t n;
            while ((n = read(irq_fd, &b, 1)) < 0 && errno == EINTR)
                ;
            if (n <= 0)
                die("read irq-dev");
        } else {
            while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL) == EINTR)
                ;
        }

        struct frame_hdr h;
        memset(&h, 0, sizeof(h));
        h.seq = (uint64_t)f;
        h.cpu_sensor = (uint32_t)sched_getcpu();
        reconstruct_stimulus(s0.array.F + HDR_FLOATS, o->slopes, h.seq);
        h.t_sensor = now_ns();
        h.t_sensor_join = now_ns_join();
        hdr_put(s0.array.F, &h);
        publish(&s0);
        ImageStreamIO_sempost(&s0, -1);

        next.tv_nsec += o->period_ns;
        while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
    }
    printf("DONE frames=%ld\n", o->frames);
    return 0;
}

// reconstruct each s0 frame into actuator commands on s1
static int stage_compute(const struct opt *o)
{
    IMAGE s0, s1;
    open_stream(&s0, o, STREAM_S0, o->slopes);
    create_stream(&s1, o, STREAM_S1, o->acts);
    apply_sched(o);

    const int modal = strcmp(o->reconstruct, "modal") == 0;
    float *mat = NULL;
    struct reconstruct rec;
    memset(&rec, 0, sizeof(rec));

    struct reconstruct_budget bud;
    memset(&bud, 0, sizeof(bud));
    const int watching = strcmp(o->adapt, "off") != 0;
    const int feedback = strcmp(o->adapt, "feedback") == 0;
    long shed_frames = 0;
    const int learned = strcmp(o->adapt, "enforce-rs") == 0;
    static struct rs rs;
    IMAGE s2;
    rs_init(&rs);
    bud.enforce = strcmp(o->adapt, "enforce") == 0 || learned;
    bud.block = o->block;
    bud.first_block_ns = (uint64_t)o->block_ns;
    bud.threshold_ns = (uint64_t)o->gap_threshold_ns;
    const long reserve = o->reserve_ns > 0 ? o->reserve_ns : o->period_ns / 10;

    if (modal) {
        if (reconstruct_init(&rec, o->slopes, o->acts, o->modes, o->alpha) != 0)
            die("reconstruct_init");
        printf("OBS recon=modal\nOBS modes=%ld\nOBS tier=%d\nOBS tier_modes=%ld\n"
               "OBS alpha=%.3f\nOBS macs=%ld\nOBS adapt=%s\nOBS block=%ld\n"
               "OBS block_ns=%ld\nOBS gap_threshold_ns=%ld\nOBS reserve_ns=%ld\n",
               rec.modes, o->tier, reconstruct_tier_modes(&rec, o->tier), o->alpha,
               reconstruct_tier_macs(&rec, o->tier), o->adapt, o->block, o->block_ns,
               o->gap_threshold_ns, reserve);
    } else {
        mat = aligned_alloc(64, (size_t)o->acts * (size_t)o->slopes * sizeof(float));
        if (!mat) die("aligned_alloc");
        for (long i = 0; i < o->acts * o->slopes; i++)
            mat[i] = (float)(i % 97) * 1e-4f;
        printf("OBS recon=dense\nOBS macs=%ld\n", o->acts * o->slopes);
    }
    fflush(stdout);

    int sem = ImageStreamIO_getsemwaitindex(&s0, 0);
    ImageStreamIO_semflush(&s0, sem);
    if (learned)
        open_stream(&s2, o, STREAM_S2, 1);
    mark_ready(o, "compute");

    uint64_t last = s0.md->cnt0, spurious = 0, dropped = 0;
    long seen = 0;
    for (;;) {
        int adv = wait_frame(&s0, sem, &last, &spurious);
        if (adv < 0) {
            fprintf(stderr, "pipeline: compute stalled after %ld of %ld frames\n",
                    seen, o->frames);
            break;
        }
        dropped += (uint64_t)(adv - 1);
        seen++;
        uint64_t t_in = now_ns();

        struct frame_hdr h;
        hdr_get(s0.array.F, &h);
        h.t_compute_in = t_in;
        h.cpu_compute = (uint32_t)sched_getcpu();

        const float *slopes = s0.array.F + HDR_FLOATS;
        float *cmd = s1.array.F + HDR_FLOATS;
        if (modal) {
            bud.deadline_ns = h.t_sensor + (uint64_t)o->period_ns - frame_reserve(learned ? &rs : NULL, learned, (uint64_t)reserve);
            if (learned) {
                uint64_t lr = frame_reserve(&rs, 1, (uint64_t)reserve);
                if (lr > (uint64_t)reserve) rs.frames_learned++; else rs.frames_floor++;
                if (lr > rs.reserve_max) rs.reserve_max = lr;
            }

            long m_max = reconstruct_tier_modes(&rec, o->tier);

            if (feedback) {
                uint64_t ref = (bud.frame_blocks > 0 && bud.frame_ns > 0)
                    ? bud.frame_ns / (uint64_t)bud.frame_blocks : bud.first_block_ns;
                if (ref == 0)
                    ref = bud.first_block_ns;
                long blocks = bud.deadline_ns > t_in
                    ? (long)((bud.deadline_ns - t_in) / ref) : 0;
                long m = blocks * o->block;
                if (m < o->block)
                    m = o->block;
                if (m < m_max)
                    m_max = m;
                if (m_max < reconstruct_tier_modes(&rec, o->tier))
                    shed_frames++;
            }

            h.modes_done = (uint32_t)reconstruct_apply_blocks(
                &rec, m_max, o->block, slopes, cmd,
                watching ? &bud : NULL);
        } else {
            for (long a = 0; a < o->acts; a++) {
                const float *row = mat + a * o->slopes;
                float acc = 0.0f;
                for (long s = 0; s < o->slopes; s++)
                    acc += row[s] * slopes[s];
                cmd[a] = acc;
            }
        }

        h.t_compute_out = now_ns();
        hdr_put(s1.array.F, &h);
        publish(&s1);
        ImageStreamIO_sempost(&s1, -1);
        if (learned) {
            struct frame_hdr a;
            rs_note_out(&rs, h.seq, h.t_compute_out);
            hdr_get(s2.array.F, &a);
            rs_sample(&rs, a.seq, a.t_actuator, (uint64_t)o->period_ns);
        }
        if (h.seq + 1 >= (uint64_t)o->frames)
            break;
    }
    if (modal && watching)
        printf("OBS det_gap_sum_ns=%llu\nOBS det_gap_count=%ld\nOBS det_gap_max_ns=%llu\n"
               "OBS det_observed_ns=%llu\nOBS det_blocks=%ld\nOBS det_stopped_frames=%ld\n"
               "OBS det_shed_frames=%ld\n",
               (unsigned long long)bud.gap_sum, bud.gap_n, (unsigned long long)bud.gap_max,
               (unsigned long long)bud.observed_ns, bud.blocks, bud.stopped_frames,
               shed_frames);
    if (modal && learned)
        printf("OBS det_rs_accepted=%ld\nOBS det_rs_rejected=%ld\nOBS det_rs_held=%ld\n"
               "OBS det_rs_frames_learned=%ld\nOBS det_rs_frames_floor=%ld\n"
               "OBS det_rs_reserve_final_ns=%llu\nOBS det_rs_reserve_max_ns=%llu\n",
               rs.accepted, rs.rejected, rs_held(&rs), rs.frames_learned, rs.frames_floor,
               (unsigned long long)rs_reserve(&rs, (uint64_t)reserve),
               (unsigned long long)rs.reserve_max);
    printf("OBS spurious_wakes=%llu\nOBS dropped=%llu\nOBS seen=%ld\nDONE frames=%ld\n",
           (unsigned long long)spurious, (unsigned long long)dropped, seen, o->frames);
    if (modal)
        reconstruct_free(&rec);
    free(mat);
    return 0;
}

// stamp each s1 frame as sent and forward it on s2
static int stage_actuator(const struct opt *o)
{
    IMAGE s1, s2;
    open_stream(&s1, o, STREAM_S1, o->acts);
    create_stream(&s2, o, STREAM_S2, 1);
    apply_sched(o);

    int sem = ImageStreamIO_getsemwaitindex(&s1, 0);
    ImageStreamIO_semflush(&s1, sem);
    mark_ready(o, "actuator");

    uint64_t last = s1.md->cnt0, spurious = 0, dropped = 0;
    long seen = 0;
    for (;;) {
        int adv = wait_frame(&s1, sem, &last, &spurious);
        if (adv < 0) {
            fprintf(stderr, "pipeline: actuator stalled after %ld of %ld frames\n",
                    seen, o->frames);
            break;
        }
        dropped += (uint64_t)(adv - 1);
        seen++;

        struct frame_hdr h;
        hdr_get(s1.array.F, &h);
        h.cpu_actuator = (uint32_t)sched_getcpu();
        h.t_actuator = now_ns();
        h.t_actuator_join = now_ns_join();

        hdr_put(s2.array.F, &h);
        publish(&s2);
        ImageStreamIO_sempost(&s2, -1);
        if (h.seq + 1 >= (uint64_t)o->frames)
            break;
    }
    printf("OBS spurious_wakes=%llu\nOBS dropped=%llu\nOBS seen=%ld\nDONE frames=%ld\n",
           (unsigned long long)spurious, (unsigned long long)dropped, seen, o->frames);
    return 0;
}

// write each s2 frame header to the telemetry file
static int stage_telemetry(const struct opt *o)
{
    IMAGE s2;
    open_stream(&s2, o, STREAM_S2, 1);
    apply_sched(o);

    FILE *fp = fopen(o->out, "wb");
    if (!fp) die("fopen telemetry");

    int sem = ImageStreamIO_getsemwaitindex(&s2, 0);
    ImageStreamIO_semflush(&s2, sem);
    mark_ready(o, "telemetry");

    long got = 0;
    uint64_t last = s2.md->cnt0, spurious = 0, dropped = 0;
    for (;;) {
        int adv = wait_frame(&s2, sem, &last, &spurious);
        if (adv < 0) {
            fprintf(stderr, "pipeline: telemetry stalled after %ld of %ld frames\n",
                    got, o->frames);
            break;
        }
        dropped += (uint64_t)(adv - 1);
        struct frame_hdr h;
        hdr_get(s2.array.F, &h);
        if (fwrite(&h, sizeof(h), 1, fp) != 1) die("fwrite telemetry");
        telemetry_frame_written();
        got++;
        if (h.seq + 1 >= (uint64_t)o->frames)
            break;
    }
    fclose(fp);
    printf("OBS records=%ld\nOBS spurious_wakes=%llu\nOBS dropped=%llu\nDONE frames=%ld\n",
           got, (unsigned long long)spurious, (unsigned long long)dropped, got);
    return 0;
}


// read the aggregate steal ticks from /proc/stat
static long read_steal(void)
{
    FILE *f = fopen("/proc/stat", "r");
    if (!f)
        return -1;
    char buf[512];
    long v = -1;
    if (fgets(buf, sizeof(buf), f)) {
        long u, ni, sy, id, io, irq, sirq, st;
        if (sscanf(buf, "cpu %ld %ld %ld %ld %ld %ld %ld %ld",
                   &u, &ni, &sy, &id, &io, &irq, &sirq, &st) == 8)
            v = st;
    }
    fclose(f);
    return v;
}

// compare two u64 values for qsort
static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return x < y ? -1 : x > y;
}

// measure each periodic timer wakeup's lateness and the steal ticks over the run
static int stage_detector(const struct opt *o)
{
    apply_sched(o);
    long steal0 = read_steal();

    long n = o->frames;
    uint64_t *late = malloc((size_t)n * sizeof(*late));
    if (!late) die("malloc detector");

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);
    uint64_t total_late = 0, worst = 0;
    for (long i = 0; i < n; i++) {
        next.tv_nsec += o->period_ns;
        while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL) == EINTR)
            ;
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        uint64_t want = (uint64_t)next.tv_sec * 1000000000ull + (uint64_t)next.tv_nsec;
        uint64_t got = (uint64_t)now.tv_sec * 1000000000ull + (uint64_t)now.tv_nsec;
        uint64_t d = got > want ? got - want : 0;
        late[i] = d;
        total_late += d;
        if (d > worst) worst = d;
    }

    qsort(late, (size_t)n, sizeof(*late), cmp_u64);
    long steal1 = read_steal();
    printf("OBS det_samples=%ld\n", n);
    printf("OBS det_p50_ns=%llu\nOBS det_p99_ns=%llu\nOBS det_p999_ns=%llu\nOBS det_max_ns=%llu\n",
           (unsigned long long)late[n / 2],
           (unsigned long long)late[(long)(n * 0.99)],
           (unsigned long long)late[(long)(n * 0.999)],
           (unsigned long long)worst);
    printf("OBS det_sum_late_ns=%llu\n", (unsigned long long)total_late);
    printf("OBS det_steal_ticks=%ld\n", steal1 >= 0 && steal0 >= 0 ? steal1 - steal0 : -1);
    printf("OBS det_window_ns=%llu\n", (unsigned long long)((uint64_t)n * (uint64_t)o->period_ns));
    printf("DONE frames=%ld\n", n);
    free(late);
    return 0;
}

// time each tier's reconstruction and compute its error against tier 0
static int stage_quality(const struct opt *o)
{
    struct reconstruct rec;
    memset(&rec, 0, sizeof(rec));
    if (reconstruct_init(&rec, o->slopes, o->acts, o->modes, o->alpha) != 0)
        die("reconstruct_init");

    float *x = aligned_alloc(64, ((size_t)o->slopes * sizeof(float) + 63) / 64 * 64);
    float *ref = aligned_alloc(64, ((size_t)o->acts * sizeof(float) + 63) / 64 * 64);
    float *got = aligned_alloc(64, ((size_t)o->acts * sizeof(float) + 63) / 64 * 64);
    long reps = o->frames;
    uint64_t *ns = malloc((size_t)reps * sizeof(*ns));
    if (!x || !ref || !got || !ns)
        die("aligned_alloc quality");

    printf("OBS quality_slopes=%ld\nOBS quality_acts=%ld\nOBS quality_modes=%ld\n"
           "OBS quality_alpha=%.6f\nOBS quality_reps=%ld\nOBS quality_stim=%ld\n"
           "OBS quality_dense_macs=%ld\n",
           o->slopes, o->acts, rec.modes, o->alpha, reps, o->stim,
           o->acts * o->slopes);

    {
        const long n = 200000;
        uint64_t sink = 0;
        uint64_t a = now_ns();
        for (long i = 0; i < n; i++)
            sink += now_ns();
        uint64_t b = now_ns();
        double raw = (double)(b - a) / (double)n;

        a = now_ns();
        for (long i = 0; i < n; i++)
            sink += now_ns_join();
        b = now_ns();
        double join = (double)(b - a) / (double)n;

        printf("OBS clock_raw_ns=%.2f\nOBS clock_join_ns=%.2f\nOBS clock_read_ns=%.2f\n"
               "OBS clock_reads=%ld\nOBS clock_sink=%llu\n",
               raw, join, raw, n, (unsigned long long)(sink & 1));
        fflush(stdout);
    }

    double t0_median = 0.0;
    for (int tier = 0; tier < RECONSTRUCT_TIERS; tier++) {
        reconstruct_stimulus(x, o->slopes, 0);
        for (long i = 0; i < reps; i++) {
            uint64_t a = now_ns();
            reconstruct_apply(&rec, tier, o->block, x, got);
            ns[i] = now_ns() - a;
        }
        qsort(ns, (size_t)reps, sizeof(*ns), cmp_u64);
        double median = (double)ns[reps / 2];
        if (tier == 0)
            t0_median = median;

        double relerr = reconstruct_tier_relerr(&rec, tier, o->stim, o->block, x, ref, got);

        printf("OBS tier%d_modes=%ld tier%d_macs=%ld tier%d_min_ns=%llu tier%d_p50_ns=%llu "
               "tier%d_p99_ns=%llu tier%d_cost_ratio=%.4f tier%d_relerr=%.6f\n",
               tier, reconstruct_tier_modes(&rec, tier),
               tier, reconstruct_tier_macs(&rec, tier),
               tier, (unsigned long long)ns[0],
               tier, (unsigned long long)ns[reps / 2],
               tier, (unsigned long long)ns[(long)(reps * 0.99)],
               tier, t0_median > 0.0 ? median / t0_median : 0.0,
               tier, relerr);
        fflush(stdout);
    }

    if (o->curve) {
        float *prefix = malloc((size_t)rec.modes * (size_t)o->acts * sizeof(float));
        double *num = malloc((size_t)(rec.modes + 1) * sizeof(double));
        double *curve = malloc((size_t)(rec.modes + 1) * sizeof(double));
        if (!prefix || !num || !curve)
            die("malloc curve");
        reconstruct_relerr_curve(&rec, o->stim, x, prefix, num, curve);
        for (long m = 0; m <= rec.modes; m++)
            printf("CURVE %ld %.9f\n", m, curve[m]);
        free(curve); free(num); free(prefix);
    }

    printf("DONE frames=%ld\n", reps);
    free(ns); free(got); free(ref); free(x);
    reconstruct_free(&rec);
    return 0;
}


// print usage and exit 2
static void usage(void)
{
    fprintf(stderr,
            "usage: pipeline --stage sensor|compute|actuator|telemetry|detector|quality --tag ID\n"
            "                [--slopes N] [--acts N] [--period-ns N] [--frames N]\n"
            "                [--cpu N] [--sched other|fifo|deadline] [--prio N]\n"
            "                [--runtime-ns N] [--out PATH]\n"
            "                [--reconstruct dense|modal] [--modes N] [--tier N] [--alpha F]\n"
            "                [--stim N] [--adapt off|observe|enforce|feedback|enforce-rs] [--block N]\n"
            "                [--block-ns N] [--gap-threshold-ns N] [--reserve-ns N]\n"
            "                [--curve] [--trigger timer|irq] [--irq-dev PATH]\n");
    exit(2);
}

// parse flags, check them and run the chosen stage
int main(int argc, char **argv)
{
    struct opt o = {
        .stage = NULL, .tag = NULL, .out = "telemetry.bin",
        .slopes = 1600, .acts = 400,
        .period_ns = 1000000, .frames = 10000,
        .cpu = -1, .policy = "other", .prio = 80, .runtime_ns = 0,
        .reconstruct = "dense", .modes = 0, .tier = 0, .alpha = RECONSTRUCT_ALPHA_DEFAULT,
        .stim = RECONSTRUCT_STIM_PERIOD,
        .adapt = "off", .block = 16, .block_ns = 0, .gap_threshold_ns = 2000, .reserve_ns = 0,
        .curve = 0,
        .trigger = "timer", .irq_dev = NULL,
    };

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
#define NEED() do { if (!v) usage(); i++; } while (0)
        if (!strcmp(a, "--stage")) { NEED(); o.stage = v; }
        else if (!strcmp(a, "--tag")) { NEED(); o.tag = v; }
        else if (!strcmp(a, "--out")) { NEED(); o.out = v; }
        else if (!strcmp(a, "--slopes")) { NEED(); o.slopes = atol(v); }
        else if (!strcmp(a, "--acts")) { NEED(); o.acts = atol(v); }
        else if (!strcmp(a, "--period-ns")) { NEED(); o.period_ns = atol(v); }
        else if (!strcmp(a, "--frames")) { NEED(); o.frames = atol(v); }
        else if (!strcmp(a, "--cpu")) { NEED(); o.cpu = atoi(v); }
        else if (!strcmp(a, "--sched")) { NEED(); o.policy = v; }
        else if (!strcmp(a, "--prio")) { NEED(); o.prio = atoi(v); }
        else if (!strcmp(a, "--runtime-ns")) { NEED(); o.runtime_ns = atol(v); }
        else if (!strcmp(a, "--reconstruct")) { NEED(); o.reconstruct = v; }
        else if (!strcmp(a, "--modes")) { NEED(); o.modes = atol(v); }
        else if (!strcmp(a, "--tier")) { NEED(); o.tier = atoi(v); }
        else if (!strcmp(a, "--alpha")) { NEED(); o.alpha = atof(v); }
        else if (!strcmp(a, "--stim")) { NEED(); o.stim = atol(v); }
        else if (!strcmp(a, "--adapt")) { NEED(); o.adapt = v; }
        else if (!strcmp(a, "--block")) { NEED(); o.block = atol(v); }
        else if (!strcmp(a, "--block-ns")) { NEED(); o.block_ns = atol(v); }
        else if (!strcmp(a, "--gap-threshold-ns")) { NEED(); o.gap_threshold_ns = atol(v); }
        else if (!strcmp(a, "--reserve-ns")) { NEED(); o.reserve_ns = atol(v); }
        else if (!strcmp(a, "--curve")) { o.curve = 1; }
        else if (!strcmp(a, "--trigger")) { NEED(); o.trigger = v; }
        else if (!strcmp(a, "--irq-dev")) { NEED(); o.irq_dev = v; }
        else usage();
#undef NEED
    }
    if (!o.stage || !o.tag) usage();
    if (o.slopes <= 0 || o.acts <= 0 || o.period_ns <= 0 || o.frames <= 0) usage();
    if (strcmp(o.reconstruct, "dense") && strcmp(o.reconstruct, "modal")) {
        fprintf(stderr, "pipeline: unknown --reconstruct '%s'\n", o.reconstruct);
        exit(2);
    }
    if (o.tier < 0 || o.tier >= RECONSTRUCT_TIERS) {
        fprintf(stderr, "pipeline: --tier %d outside 0..%d\n", o.tier, RECONSTRUCT_TIERS - 1);
        exit(2);
    }
    if (o.alpha <= 0.0) {
        fprintf(stderr, "pipeline: --alpha %g must be positive\n", o.alpha);
        exit(2);
    }
    if (o.stim <= 0) {
        fprintf(stderr, "pipeline: --stim %ld must be positive\n", o.stim);
        exit(2);
    }
    if (strcmp(o.adapt, "off") && strcmp(o.adapt, "observe") && strcmp(o.adapt, "enforce")
        && strcmp(o.adapt, "feedback") && strcmp(o.adapt, "enforce-rs")) {
        fprintf(stderr, "pipeline: unknown --adapt '%s'\n", o.adapt);
        exit(2);
    }
    if (o.block < 1) {
        fprintf(stderr, "pipeline: --block %ld must be at least 1\n", o.block);
        exit(2);
    }
    if (strcmp(o.adapt, "off") && o.block_ns <= 0) {
        fprintf(stderr, "pipeline: --adapt %s needs --block-ns, the declared cost of a block "
                        "of %ld modes, see results/earlier_phase/frozen/quality-frozen.json\n", o.adapt, o.block);
        exit(2);
    }
    if (strcmp(o.adapt, "off") && strcmp(o.reconstruct, "modal")) {
        fprintf(stderr, "pipeline: --adapt %s needs --reconstruct modal; the dense reconstruction has "
                        "nothing to stop early\n", o.adapt);
        exit(2);
    }
    if (strcmp(o.trigger, "timer") && strcmp(o.trigger, "irq")) {
        fprintf(stderr, "pipeline: unknown --trigger '%s'\n", o.trigger);
        exit(2);
    }
    if (!strcmp(o.trigger, "irq") && !o.irq_dev) {
        fprintf(stderr, "pipeline: --trigger irq needs --irq-dev, the device the sensor blocks "
                        "on for a frame start\n");
        exit(2);
    }

    printf("OBS stage=%s\nOBS tag=%s\nOBS slopes=%ld\nOBS acts=%ld\nOBS frames=%ld\n",
           o.stage, o.tag, o.slopes, o.acts, o.frames);
    fflush(stdout);

    if (!strcmp(o.stage, "sensor")) return stage_sensor(&o);
    if (!strcmp(o.stage, "compute")) return stage_compute(&o);
    if (!strcmp(o.stage, "actuator")) return stage_actuator(&o);
    if (!strcmp(o.stage, "telemetry")) return stage_telemetry(&o);
    if (!strcmp(o.stage, "detector")) return stage_detector(&o);
    if (!strcmp(o.stage, "quality")) return stage_quality(&o);
    usage();
    return 2;
}
