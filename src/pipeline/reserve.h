// learn the frame reserve for --adapt enforce-rs
#ifndef TEMPO_RESERVE_H
#define TEMPO_RESERVE_H

#include <stdint.h>
#include <string.h>

#define RS_WINDOW 1000
#define RS_WARMUP 100
#define RS_RING   64

// learned reserve window and counters
struct rs {
    uint32_t order[RS_WINDOW];
    uint32_t sorted[RS_WINDOW];
    long n, head;
    uint64_t ring_seq[RS_RING], ring_out[RS_RING];
    int ring_ok[RS_RING];
    uint64_t last_seq;
    int have_last;
    long accepted, rejected;
    uint64_t reserve_max;
    long frames_learned, frames_floor;
};

// zero a learner
static inline void rs_init(struct rs *r)
{
    memset(r, 0, sizeof(*r));
}

// return the number of samples held
static inline long rs_held(const struct rs *r)
{
    return r->n;
}

// note the compute-out stamp of a frame just published
static inline void rs_note_out(struct rs *r, uint64_t seq, uint64_t t_out)
{
    int i = (int)(seq % RS_RING);
    r->ring_seq[i] = seq;
    r->ring_out[i] = t_out;
    r->ring_ok[i] = 1;
}

// add a sample to the sorted window
static inline void rs_push(struct rs *r, uint32_t v)
{
    if (r->n == RS_WINDOW) {
        uint32_t old = r->order[r->head];
        long lo = 0, hi = r->n - 1;
        while (lo < hi) {
            long mid = (lo + hi) / 2;
            if (r->sorted[mid] < old) lo = mid + 1; else hi = mid;
        }
        memmove(&r->sorted[lo], &r->sorted[lo + 1], (size_t)(r->n - lo - 1) * sizeof(uint32_t));
        r->n--;
    }
    long lo = 0, hi = r->n;
    while (lo < hi) {
        long mid = (lo + hi) / 2;
        if (r->sorted[mid] <= v) lo = mid + 1; else hi = mid;
    }
    memmove(&r->sorted[lo + 1], &r->sorted[lo], (size_t)(r->n - lo) * sizeof(uint32_t));
    r->sorted[lo] = v;
    r->n++;
    r->order[r->head] = v;
    r->head = (r->head + 1) % RS_WINDOW;
}

// take a sample
static inline int rs_sample(struct rs *r, uint64_t seq, uint64_t t_act, uint64_t period)
{
    if (r->have_last && seq == r->last_seq)
        return 0;
    int i = (int)(seq % RS_RING);
    if (!r->ring_ok[i] || r->ring_seq[i] != seq)
        return 0;
    r->last_seq = seq;
    r->have_last = 1;
    uint64_t out = r->ring_out[i];
    if (t_act < out || t_act - out >= period) {
        r->rejected++;
        return -1;
    }
    rs_push(r, (uint32_t)(t_act - out));
    r->accepted++;
    return 1;
}

// return the nearest-rank p99, 0 when empty
static inline uint64_t rs_p99(const struct rs *r)
{
    if (r->n == 0)
        return 0;
    long rank = (99 * r->n + 99) / 100;
    return r->sorted[rank - 1];
}

// return the p99, or the floor during warm-up or when the p99 is below it
static inline uint64_t rs_reserve(const struct rs *r, uint64_t floor_ns)
{
    if (r->n < RS_WARMUP)
        return floor_ns;
    uint64_t p = rs_p99(r);
    return p > floor_ns ? p : floor_ns;
}

// return the reserve a frame budgets with
static inline uint64_t frame_reserve(const struct rs *r, int learned, uint64_t fixed_ns)
{
    if (!learned || !r)
        return fixed_ns;
    return rs_reserve(r, fixed_ns);
}

#endif
