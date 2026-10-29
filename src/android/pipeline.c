// build src/pipeline/pipeline.c for android with an ADPF hint session
#define _GNU_SOURCE
#include <stdbool.h>
#include <android/performance_hint.h>
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include "ImageStreamIO.h"
#include "frame.h"
#include "args.h"

static APerformanceHintSession *hint_session;
static int hint_wanted;
static int64_t hint_target_ns = 1000000;
static uint64_t hint_frame_t0;
static uint64_t hint_reports, hint_errors;
static int hint_last_rc;

// return CLOCK_BOOTTIME in ns
static uint64_t boottime_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_BOOTTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

#define obs(...) (printf(__VA_ARGS__), fflush(stdout))

// print the hint counts and close the performance hint handle
static void hint_close(void)
{
    if (!hint_session)
        return;
    obs("OBS hint_reports=%" PRIu64 "\nOBS hint_report_errors=%" PRIu64 "\nOBS hint_last_rc=%d\n",
        hint_reports, hint_errors, hint_last_rc);
    APerformanceHint_closeSession(hint_session);
    hint_session = NULL;
}

// open a hint session on the calling thread
static void hint_open(void)
{
    if (!hint_wanted)
        return;
    APerformanceHintManager *mgr = APerformanceHint_getManager();
    if (!mgr) {
        obs("OBS hint_session=unsupported reason=no_manager\n");
        return;
    }
    int64_t rate = APerformanceHint_getPreferredUpdateRateNanos(mgr);
    int32_t tid = (int32_t)syscall(SYS_gettid);
    hint_session = APerformanceHint_createSession(mgr, &tid, 1, hint_target_ns);
    if (!hint_session) {
        obs("OBS hint_session=unsupported reason=create_failed preferred_rate_ns=%" PRId64 "\n", rate);
        return;
    }
    obs("OBS hint_session=open tids=%d target_ns=%" PRId64 " preferred_rate_ns=%" PRId64 "\n",
        (int)tid, hint_target_ns, rate);
    atexit(hint_close);
}

// print the stage's thread id and open the hint session
static int hint_mlockall(int flags)
{
    int rc = mlockall(flags);
    int saved = errno;
    obs("OBS tid=%d\n", (int)syscall(SYS_gettid));
    hint_open();
    errno = saved;
    return rc;
}

// start a frame's work when the stage wakes for it
static int hint_semtimedwait(IMAGE *im, int index, const struct timespec *ts)
{
    int rc = ImageStreamIO_semtimedwait(im, index, ts);
    if (rc == 0)
        hint_frame_t0 = boottime_ns();
    return rc;
}

// start the sensor's frame when its timer fires
static int hint_clock_nanosleep(clockid_t clk, int flags, const struct timespec *req, struct timespec *rem)
{
    int rc = clock_nanosleep(clk, flags, req, rem);
    if (rc == 0)
        hint_frame_t0 = boottime_ns();
    return rc;
}

// report a frame's work to the hint session
static void hint_report_frame(void)
{
    if (!hint_session || !hint_frame_t0)
        return;
    int r = APerformanceHint_reportActualWorkDuration(hint_session, (int64_t)(boottime_ns() - hint_frame_t0));
    hint_reports++;
    if (r != 0) {
        if (hint_errors++ == 0)
            fprintf(stderr, "pipeline: reportActualWorkDuration returned %d\n", r);
        hint_last_rc = r;
    }
    hint_frame_t0 = 0;
}

// end a frame's work as the stage hands the frame on
static long hint_sempost(IMAGE *im, long index)
{
    long rc = ImageStreamIO_sempost(im, index);
    hint_report_frame();
    return rc;
}

_Static_assert(sizeof(struct frame_hdr) == 72, "struct frame_hdr is not the 72 bytes the runner and frames.py read");

static struct frame_hdr *record_buf;
static long record_cap, record_count, record_over;

// copy a header into a stream buffer and append it to the stage's own record
static void record_hdr_put(float *buf, const struct frame_hdr *h)
{
    hdr_put(buf, h);
    if (!record_buf)
        return;
    if (record_count < record_cap)
        record_buf[record_count++] = *h;
    else
        record_over++;
}

// allocate and touch the record for frames headers, return 0 or -1
static int record_open(long frames)
{
    record_buf = malloc((size_t)frames * sizeof(*record_buf));
    if (!record_buf)
        return -1;
    memset(record_buf, 0, (size_t)frames * sizeof(*record_buf));
    record_cap = frames;
    return 0;
}

// write the record as 72-byte headers to path
static int record_write(const char *path)
{
    if (record_over) {
        fprintf(stderr, "pipeline: the stage put %ld headers past its record of %ld\n", record_over, record_cap);
        return 2;
    }
    FILE *fp = fopen(path, "wb");
    if (!fp) {
        fprintf(stderr, "pipeline: fopen %s: %s\n", path, strerror(errno));
        return 2;
    }
    size_t n = fwrite(record_buf, sizeof(*record_buf), (size_t)record_count, fp);
    if (fclose(fp) != 0 || n != (size_t)record_count) {
        fprintf(stderr, "pipeline: writing %s failed\n", path);
        return 2;
    }
    obs("OBS actuator_records=%ld\n", record_count);
    return 0;
}

#define mlockall(f) hint_mlockall(f)
#define ImageStreamIO_semtimedwait(im, i, ts) hint_semtimedwait(im, i, ts)
#define ImageStreamIO_sempost(im, i) hint_sempost(im, i)
#define clock_nanosleep(c, f, r, m) hint_clock_nanosleep(c, f, r, m)
#define now_ns_join() boottime_ns()
#define hdr_put(b, h) record_hdr_put(b, h)
#define telemetry_frame_written() hint_report_frame()

#define main pipeline_main
#include "../pipeline/pipeline.c"
#undef main

// read the Android flags, then run the pipeline
int main(int argc, char **argv)
{
    struct android_args a;
    char err[160];
    if (android_args(&argc, argv, &a, err, sizeof(err)) != 0) {
        fprintf(stderr, "pipeline: %s\n", err);
        return 2;
    }
    if (a.period_ns)
        hint_target_ns = a.period_ns;
    hint_wanted = a.adpf && a.stage && (!strcmp(a.stage, "sensor") || !strcmp(a.stage, "compute")
                                        || !strcmp(a.stage, "actuator")
                                        || (a.adpf_telemetry && !strcmp(a.stage, "telemetry")));
    obs("OBS join_clock=boottime\nOBS adpf=%s\nOBS adpf_telemetry=%s\n", a.adpf ? "on" : "off", a.adpf_telemetry ? "on" : "off");
    if (a.record && record_open(a.frames) != 0) {
        fprintf(stderr, "pipeline: cannot allocate a record of %ld frames\n", a.frames);
        return 2;
    }
    int rc = pipeline_main(argc, argv);
    if (rc == 0 && a.record)
        rc = record_write(a.record);
    return rc;
}
