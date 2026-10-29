// build and apply the modal reconstruction cmd = U diag(s) V^T x in tiers of modes
#ifndef TEMPO_RECONSTRUCT_H
#define TEMPO_RECONSTRUCT_H

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "frame.h"

#define RECONSTRUCT_TIERS 4
#define RECONSTRUCT_ALPHA_DEFAULT 1.0

// mix (tag, i, j) into 32 bits
static inline uint32_t reconstruct_hash(uint32_t tag, uint32_t i, uint32_t j)
{
    uint32_t x = tag * 0x9E3779B1u + i * 0x85EBCA77u + j * 0xC2B2AE3Du + 0x27D4EB2Fu;
    x ^= x >> 16;
    x *= 0x7FEB352Du;
    x ^= x >> 15;
    x *= 0x846CA68Bu;
    x ^= x >> 16;
    return x;
}

// map a hash to a value in [-1, 1)
static inline double reconstruct_val(uint32_t tag, uint32_t i, uint32_t j)
{
    return (double)reconstruct_hash(tag, i, j) * (2.0 / 4294967296.0) - 1.0;
}

#define RECONSTRUCT_TAG_V 1u
#define RECONSTRUCT_TAG_U 2u

// modal reconstruction operator
struct reconstruct {
    long slopes, acts, modes;
    double alpha;
    float *vt;
    float *u;
    float *s;
};

// return the full rank at which the modal and dense operators cost the same
static inline long reconstruct_auto_modes(long slopes, long acts)
{
    long m = (acts * slopes) / (slopes + acts);
    if (m < RECONSTRUCT_TIERS) m = RECONSTRUCT_TIERS;
    return m;
}

// return the modes a tier keeps
static inline long reconstruct_tier_modes(const struct reconstruct *r, int tier)
{
    long m = r->modes >> (tier < 0 ? 0 : tier);
    return m < 1 ? 1 : m;
}

// return a tier's multiply-accumulates
static inline long reconstruct_tier_macs(const struct reconstruct *r, int tier)
{
    return reconstruct_tier_modes(r, tier) * (r->slopes + r->acts);
}

// fill the operator from the hash
static inline int reconstruct_init(struct reconstruct *r, long slopes, long acts, long modes, double alpha)
{
    r->slopes = slopes;
    r->acts = acts;
    r->modes = modes > 0 ? modes : reconstruct_auto_modes(slopes, acts);
    r->alpha = alpha;

    r->vt = aligned_alloc(64, (size_t)r->modes * (size_t)slopes * sizeof(float));
    r->u = aligned_alloc(64, (size_t)r->modes * (size_t)acts * sizeof(float));
    r->s = aligned_alloc(64, ((size_t)r->modes * sizeof(float) + 63) / 64 * 64);
    if (!r->vt || !r->u || !r->s)
        return -1;

    for (long k = 0; k < r->modes; k++) {
        for (long i = 0; i < slopes; i++)
            r->vt[k * slopes + i] = (float)reconstruct_val(RECONSTRUCT_TAG_V, (uint32_t)k, (uint32_t)i);
        for (long a = 0; a < acts; a++)
            r->u[k * acts + a] = (float)reconstruct_val(RECONSTRUCT_TAG_U, (uint32_t)k, (uint32_t)a);
        r->s[k] = (float)pow((double)(k + 1), -alpha);
    }
    return 0;
}

// free the operator
static inline void reconstruct_free(struct reconstruct *r)
{
    free(r->vt); free(r->u); free(r->s);
    r->vt = r->u = r->s = NULL;
}

// per-frame deadline and run counters for the in-loop detector
struct reconstruct_budget {
    uint64_t deadline_ns;
    int enforce;
    uint64_t frame_ns;
    long frame_blocks;
    long block;
    uint64_t first_block_ns;
    uint64_t threshold_ns;
    uint64_t gap_sum, gap_max, observed_ns;
    long gap_n, blocks, stopped_frames;
};

// run a frame's reconstruction in blocks, return the modes completed
static inline long reconstruct_apply_blocks(struct reconstruct *r, long m_max, long blk, const float *x,
                                      float *cmd, struct reconstruct_budget *b)
{
    const long slopes = r->slopes, acts = r->acts;
    if (blk < 1)
        blk = m_max;

    for (long a = 0; a < acts; a++)
        cmd[a] = 0.0f;

    long done = 0, blocks_here = 0;
    uint64_t min_block = 0, t_prev = 0, t_start = 0;
    if (b)
        t_start = t_prev = now_ns();

    while (done < m_max) {
        long m = m_max - done;
        if (m > blk)
            m = blk;

        for (long k = done; k < done + m; k++) {
            const float *v = r->vt + k * slopes;
            float acc = 0.0f;
            for (long i = 0; i < slopes; i++)
                acc += v[i] * x[i];
            const float c = acc * r->s[k];
            const float *uu = r->u + k * acts;
            for (long a = 0; a < acts; a++)
                cmd[a] += c * uu[a];
        }
        done += m;

        if (!b)
            continue;

        const uint64_t t = now_ns();
        const uint64_t took = t - t_prev;
        t_prev = t;
        b->blocks++;
        blocks_here++;

        if (m == blk) {
            const uint64_t ref = min_block ? min_block : b->first_block_ns;
            if (took > ref + b->threshold_ns) {
                const uint64_t g = took - ref;
                b->gap_sum += g;
                b->gap_n++;
                if (g > b->gap_max)
                    b->gap_max = g;
            }
            if (!min_block || took < min_block)
                min_block = took;
        }

        if (b->enforce && done < m_max) {
            const uint64_t next = min_block ? min_block : b->first_block_ns;
            if (t + next > b->deadline_ns) {
                b->stopped_frames++;
                break;
            }
        }
    }
    if (b) {
        b->observed_ns += t_prev - t_start;
        b->frame_ns = t_prev - t_start;
        b->frame_blocks = blocks_here;
    }
    return done;
}

// run a frame's reconstruction at a fixed tier
static inline void reconstruct_apply(struct reconstruct *r, int tier, long blk, const float *x, float *cmd)
{
    reconstruct_apply_blocks(r, reconstruct_tier_modes(r, tier), blk, x, cmd, NULL);
}

// fill the deterministic stimulus for a frame
static inline void reconstruct_stimulus(float *payload, long n, uint64_t seq)
{
    for (long i = 0; i < n; i++)
        payload[i] = (float)((seq + (uint64_t)i) % 1024u) * 0.001f;
}

#define RECONSTRUCT_STIM_PERIOD 1024

// return a tier's relative L2 error against tier 0 over nstim stimuli
static inline double reconstruct_tier_relerr(struct reconstruct *r, int tier, long nstim, long blk,
                                       float *x, float *ref, float *got)
{
    double num = 0.0, den = 0.0;
    for (long f = 0; f < nstim; f++) {
        reconstruct_stimulus(x, r->slopes, (uint64_t)f);
        reconstruct_apply(r, 0, blk, x, ref);
        reconstruct_apply(r, tier, blk, x, got);
        for (long a = 0; a < r->acts; a++) {
            double d = (double)got[a] - (double)ref[a];
            num += d * d;
            den += (double)ref[a] * (double)ref[a];
        }
    }
    return den > 0.0 ? sqrt(num / den) : 0.0;
}

// fill curve[m] with the relative error of stopping after m modes
static inline void reconstruct_relerr_curve(struct reconstruct *r, long nstim, float *x, float *prefix,
                                      double *num, double *curve)
{
    const long slopes = r->slopes, acts = r->acts, modes = r->modes;
    double den = 0.0;

    for (long m = 0; m <= modes; m++)
        num[m] = 0.0;

    for (long f = 0; f < nstim; f++) {
        reconstruct_stimulus(x, slopes, (uint64_t)f);

        for (long k = 0; k < modes; k++) {
            const float *v = r->vt + k * slopes;
            float acc = 0.0f;
            for (long i = 0; i < slopes; i++)
                acc += v[i] * x[i];
            const float c = acc * r->s[k];
            const float *uu = r->u + k * acts;
            float *dst = prefix + k * acts;
            const float *src = k ? prefix + (k - 1) * acts : NULL;
            for (long a = 0; a < acts; a++)
                dst[a] = (src ? src[a] : 0.0f) + c * uu[a];
        }

        const float *full = prefix + (modes - 1) * acts;
        for (long a = 0; a < acts; a++)
            den += (double)full[a] * (double)full[a];
        for (long a = 0; a < acts; a++)
            num[0] += (double)full[a] * (double)full[a];
        for (long m = 1; m < modes; m++) {
            const float *p = prefix + (m - 1) * acts;
            for (long a = 0; a < acts; a++) {
                const double d = (double)p[a] - (double)full[a];
                num[m] += d * d;
            }
        }
    }
    for (long m = 0; m <= modes; m++)
        curve[m] = den > 0.0 ? sqrt(num[m] / den) : 0.0;
    curve[modes] = 0.0;
}

#endif
