// the Android build's own flags
#ifndef TEMPO_ANDROID_ARGS_H
#define TEMPO_ANDROID_ARGS_H
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct android_args {
    const char *stage;
    long period_ns;
    long frames;
    int adpf;
    int adpf_telemetry;
    const char *record;
};

// read a positive count into *out
static int android_count(const char *flag, const char *v, long *out, char *err, size_t errlen)
{
    char *end;
    errno = 0;
    long n = strtol(v, &end, 10);
    if (errno || *end || n <= 0) {
        snprintf(err, errlen, "bad %s value: %s", flag, v);
        return 2;
    }
    *out = n;
    return 0;
}

// read the Android flags, removing the ones the pipeline does not take
static int android_args(int *argc, char **argv, struct android_args *a, char *err, size_t errlen)
{
    int out = 1;
    a->stage = NULL;
    a->period_ns = 0;
    a->frames = 0;
    a->adpf = 1;
    a->adpf_telemetry = 0;
    a->record = NULL;
    for (int i = 1; i < *argc; i++) {
        const char *v = i + 1 < *argc ? argv[i + 1] : NULL;
        if (!strcmp(argv[i], "--record")) {
            if (!v || !*v) {
                snprintf(err, errlen, "--record needs a path");
                return 2;
            }
            a->record = v;
            i++;
            continue;
        }
        if (!strcmp(argv[i], "--adpf")) {
            if (v && !strcmp(v, "on"))
                a->adpf = 1;
            else if (v && !strcmp(v, "off"))
                a->adpf = 0;
            else {
                snprintf(err, errlen, "bad --adpf value: %s", v ? v : "(none)");
                return 2;
            }
            i++;
            continue;
        }
        if (!strcmp(argv[i], "--adpf-telemetry")) {
            if (v && !strcmp(v, "on"))
                a->adpf_telemetry = 1;
            else if (v && !strcmp(v, "off"))
                a->adpf_telemetry = 0;
            else {
                snprintf(err, errlen, "bad --adpf-telemetry value: %s", v ? v : "(none)");
                return 2;
            }
            i++;
            continue;
        }
        if (!strcmp(argv[i], "--stage") && v) {
            a->stage = v;
        } else if (!strcmp(argv[i], "--period-ns") && v) {
            if (android_count("--period-ns", v, &a->period_ns, err, errlen))
                return 2;
        } else if (!strcmp(argv[i], "--frames") && v) {
            if (android_count("--frames", v, &a->frames, err, errlen))
                return 2;
        }
        argv[out++] = argv[i];
    }
    argv[out] = NULL;
    *argc = out;
    if (a->record && !a->frames) {
        snprintf(err, errlen, "--record needs --frames to size the record");
        return 2;
    }
    return 0;
}
#endif
