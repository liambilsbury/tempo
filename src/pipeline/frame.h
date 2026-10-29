// define the frame header carried through every pipeline hop
#ifndef TEMPO_FRAME_H
#define TEMPO_FRAME_H

#include <stdint.h>
#include <string.h>
#include <time.h>

#define HDR_FLOATS 24

#define APSIS_CLOCK      CLOCK_MONOTONIC_RAW
#define APSIS_CLOCK_JOIN CLOCK_MONOTONIC

// frame header with per-stage timestamps in ns
struct frame_hdr {
    uint64_t seq;
    uint64_t t_sensor;
    uint64_t t_compute_in;
    uint64_t t_compute_out;
    uint64_t t_actuator;
    uint64_t t_sensor_join;
    uint64_t t_actuator_join;
    uint32_t cpu_sensor;
    uint32_t cpu_compute;
    uint32_t cpu_actuator;
    uint32_t modes_done;
};

_Static_assert(HDR_FLOATS * sizeof(float) >= sizeof(struct frame_hdr),
               "HDR_FLOATS does not reserve enough floats for struct frame_hdr");

// return the latency clock in ns
static inline uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(APSIS_CLOCK, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

// return the join clock in ns
static inline uint64_t now_ns_join(void)
{
    struct timespec ts;
    clock_gettime(APSIS_CLOCK_JOIN, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

// copy a header into a stream buffer
static inline void hdr_put(float *buf, const struct frame_hdr *h)
{
    memcpy(buf, h, sizeof(*h));
}

// copy a header out of a stream buffer
static inline void hdr_get(const float *buf, struct frame_hdr *h)
{
    memcpy(h, buf, sizeof(*h));
}

#define STREAM_S0 "s0"
#define STREAM_S1 "s1"
#define STREAM_S2 "s2"

#endif
