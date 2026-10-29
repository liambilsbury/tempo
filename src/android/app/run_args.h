// the Android app's stage argument file reader
#ifndef TEMPO_RUN_ARGS_H
#define TEMPO_RUN_ARGS_H
#include <string.h>

// split text into its non-empty lines in place
static int run_args_split(char *text, char **lines, int max_lines)
{
    int n = 0;
    char *p = text;
    while (*p) {
        char *end = strchr(p, '\n');
        if (end)
            *end = '\0';
        if (*p) {
            if (n == max_lines)
                return -1;
            lines[n++] = p;
        }
        if (!end)
            break;
        p = end + 1;
    }
    return n;
}

// split a line on spaces in place into argv
static int run_args_argv(char *line, char **argv, int max_args, const char **stage)
{
    int argc = 1;
    const char *found = NULL;
    char *p = line;
    while (*p) {
        char *end = strchr(p, ' ');
        if (end)
            *end = '\0';
        if (*p) {
            if (argc > max_args)
                return -1;
            argv[argc++] = p;
        }
        if (!end)
            break;
        p = end + 1;
    }
    argv[argc] = NULL;
    for (int i = 1; i + 1 < argc; i++)
        if (!strcmp(argv[i], "--stage"))
            found = argv[i + 1];
    if (!found)
        return -1;
    *stage = found;
    return argc;
}

// read interval_ms, window_ms and spin_ms from the headroom argument file
static int run_headroom_args(const char *text, int *interval_ms, int *window_ms, int *spin_ms)
{
    int interval = -1, window = -1, spin = -1;
    const char *p = text;
    while (*p) {
        while (*p == ' ' || *p == '\n')
            p++;
        if (!*p)
            break;
        int *slot;
        if (!strncmp(p, "interval_ms=", 12)) {
            slot = &interval;
            p += 12;
        } else if (!strncmp(p, "window_ms=", 10)) {
            slot = &window;
            p += 10;
        } else if (!strncmp(p, "spin_ms=", 8)) {
            slot = &spin;
            p += 8;
        } else {
            return -1;
        }
        long v = 0;
        int digits = 0;
        while (*p >= '0' && *p <= '9' && digits < 6) {
            v = v * 10 + (*p++ - '0');
            digits++;
        }
        if (!digits || (*p && *p != ' ' && *p != '\n'))
            return -1;
        if (slot == &spin ? v > 500 : (v < 100 || v > 10000))
            return -1;
        *slot = (int)v;
    }
    if (interval < 0 || window < 0 || spin < 0)
        return -1;
    *interval_ms = interval;
    *window_ms = window;
    *spin_ms = spin;
    return 0;
}
#endif
