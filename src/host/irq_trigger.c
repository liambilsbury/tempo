// write a byte to L2's second UART socket at a fixed period, from L1
#define _GNU_SOURCE
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

// return CLOCK_MONOTONIC in ns
static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

// sleep until an absolute CLOCK_MONOTONIC deadline
static void wait_until(uint64_t deadline_ns)
{
    struct timespec ts = {.tv_sec = (time_t)(deadline_ns / 1000000000ull),
                          .tv_nsec = (long)(deadline_ns % 1000000000ull)};
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR)
        ;
}

// connect to the UNIX socket, retrying for 30 s
static int connect_sock(const char *path)
{
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) { perror("irq_trigger: socket"); exit(2); }
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(a.sun_path)) {
        fprintf(stderr, "irq_trigger: socket path too long: %s\n", path);
        exit(2);
    }
    strcpy(a.sun_path, path);
    for (int i = 0; i < 60; i++) {
        if (connect(fd, (struct sockaddr *)&a, sizeof(a)) == 0)
            return fd;
        struct timespec ts = {.tv_sec = 0, .tv_nsec = 500000000L};
        nanosleep(&ts, NULL);
    }
    fprintf(stderr, "irq_trigger: could not connect to %s: %s\n", path, strerror(errno));
    exit(2);
}

// connect, write a byte per period for the duration and print the counts
int main(int argc, char **argv)
{
    const char *sock_path = NULL;
    long period_ns = 1000000, duration_s = 30, start_delay_s = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
#define NEED() do { if (!v) goto usage; i++; } while (0)
        if (!strcmp(a, "--sock")) { NEED(); sock_path = v; }
        else if (!strcmp(a, "--period-ns")) { NEED(); period_ns = atol(v); }
        else if (!strcmp(a, "--duration-s")) { NEED(); duration_s = atol(v); }
        else if (!strcmp(a, "--start-delay-s")) { NEED(); start_delay_s = atol(v); }
        else goto usage;
#undef NEED
    }
    if (!sock_path || period_ns <= 0) {
usage:
        fprintf(stderr, "usage: irq_trigger --sock PATH [--period-ns N] [--duration-s N] "
                        "[--start-delay-s N]\n");
        return 2;
    }

    int fd = connect_sock(sock_path);

    if (start_delay_s > 0) {
        struct timespec ts = {.tv_sec = start_delay_s, .tv_nsec = 0};
        nanosleep(&ts, NULL);
    }

    uint64_t start = now_ns();
    uint64_t end = start + (uint64_t)duration_s * 1000000000ull;
    uint64_t next = start + (uint64_t)period_ns;
    long sent = 0, failed = 0;

    uint8_t byte = 0;
    while (now_ns() < end) {
        wait_until(next);
        ssize_t n = write(fd, &byte, 1);
        if (n == 1)
            sent++;
        else
            failed++;
        next += (uint64_t)period_ns;
    }

    printf("IRQ_TRIGGER sock=%s period_ns=%ld duration_s=%ld\n", sock_path, period_ns, duration_s);
    printf("IRQ_TRIGGER start_ns=%llu\n", (unsigned long long)start);
    printf("IRQ_TRIGGER end_ns=%llu\n", (unsigned long long)now_ns());
    printf("IRQ_TRIGGER sent=%ld failed=%ld\n", sent, failed);
    return failed ? 1 : 0;
}
