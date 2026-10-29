// read the guest's nanosecond steal counter
#ifndef TEMPO_STEAL_READ_H
#define TEMPO_STEAL_READ_H

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

// layout of KVM's steal time record
struct tempo_kvm_steal_time {
    uint64_t steal;
    uint32_t version;
    uint32_t flags;
    uint8_t  preempted;
    uint8_t  pad0[3];
    uint32_t pad[11];
};

// mapped steal counter for a CPU
struct steal_counter {
    const volatile struct tempo_kvm_steal_time *st;
    void    *map;
    long     page;
    int      cpu;
};

// read a CPU's offset within its page from /proc/tempo_steal
static inline long steal_counter_offset(int cpu)
{
    FILE *f = fopen("/proc/tempo_steal", "r");
    if (!f)
        return -1;
    char line[256];
    long off = -1;
    while (fgets(line, sizeof(line), f)) {
        int c;
        unsigned long long gpa, o, ns;
        if (sscanf(line, "%d %llu %llu %llu", &c, &gpa, &o, &ns) == 4 && c == cpu) {
            off = (long)o;
            break;
        }
    }
    fclose(f);
    return off;
}

// map a CPU's steal page, return 0 on success
static inline int steal_counter_open(struct steal_counter *s, int cpu)
{
    memset(s, 0, sizeof(*s));
    s->page = sysconf(_SC_PAGESIZE);
    long off = steal_counter_offset(cpu);
    if (off < 0)
        return -1;
    int fd = open("/dev/tempo_steal", O_RDONLY);
    if (fd < 0)
        return -1;
    void *m = mmap(NULL, (size_t)s->page, PROT_READ, MAP_SHARED, fd,
                   (off_t)cpu * s->page);
    close(fd);
    if (m == MAP_FAILED)
        return -1;
    s->map = m;
    s->cpu = cpu;
    s->st = (const volatile struct tempo_kvm_steal_time *)((char *)m + off);
    return 0;
}

// read the steal value under the version seqlock
static inline uint64_t steal_counter_read(const struct steal_counter *s)
{
    uint32_t v1, v2;
    uint64_t steal;
    do {
        v1 = s->st->version;
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        steal = s->st->steal;
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        v2 = s->st->version;
    } while ((v1 & 1) || v1 != v2);
    return steal;
}

// unmap the steal page
static inline void steal_counter_close(struct steal_counter *s)
{
    if (s->map)
        munmap(s->map, (size_t)s->page);
    s->map = NULL;
    s->st = NULL;
}

#endif
