// native activity that forks a process for each of the loop's four stages
#define _GNU_SOURCE
#include <android/log.h>
#include <android/window.h>
#include <android_native_app_glue.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "run_args.h"

#define TAG "TempoLoop"
#define STAGES 4
#define MAX_ARGS 64
#define MAX_ENV 512
#define ARGS_BYTES 8192
#define HEADROOM_ARGS_BYTES 128
#define HEADROOM_TYPE_AVERAGE 1

extern char **environ;

static pthread_mutex_t run_lock = PTHREAD_MUTEX_INITIALIZER;
static ANativeActivity *current_activity;
static int run_going;
static char files_dir[PATH_MAX], exe_path[PATH_MAX], shm_env[PATH_MAX + 16];
static char args_text[ARGS_BYTES];
static char *stage_argv[STAGES][MAX_ARGS + 2];
static const char *stage_name[STAGES];
static char log_path[STAGES][PATH_MAX];
static char *stage_env[MAX_ENV + 2];
static pid_t stage_pid[STAGES];

typedef void *(*headroom_create_fn)(void);
typedef void (*headroom_destroy_fn)(void *);
typedef void (*headroom_set_fn)(void *, int);
typedef int (*headroom_get_fn)(const void *, float *);
static int headroom_on, headroom_interval_ms, headroom_window_ms, headroom_spin_ms;
static int headroom_gen, headroom_going, headroom_exited;
static pthread_t headroom_thread;

// write text to files/<name>
static int write_file(const char *name, const char *text)
{
    char path[PATH_MAX], tmp[PATH_MAX];
    snprintf(path, sizeof(path), "%s/%s", files_dir, name);
    snprintf(tmp, sizeof(tmp), "%s/%s.tmp", files_dir, name);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0)
        return -1;
    size_t len = strlen(text), done = 0;
    while (done < len) {
        ssize_t w = write(fd, text + done, len - done);
        if (w < 0 && errno == EINTR)
            continue;
        if (w <= 0) {
            close(fd);
            return -1;
        }
        done += (size_t)w;
    }
    if (close(fd) != 0)
        return -1;
    return rename(tmp, path);
}

// remove files/<name>
static void remove_file(const char *name)
{
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/%s", files_dir, name);
    unlink(path);
}

// write an RC error line naming the reason and log it
static void write_error(const char *reason)
{
    char line[128];
    snprintf(line, sizeof(line), "RC error=%s\n", reason);
    __android_log_print(ANDROID_LOG_ERROR, TAG, "%s", line);
    if (write_file("rc", line) != 0)
        __android_log_print(ANDROID_LOG_ERROR, TAG, "cannot write %s/rc: %s", files_dir, strerror(errno));
}

// read files/run.args into args_text
static const char *read_args(void)
{
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/run.args", files_dir);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return "no_run_args";
    size_t got = 0;
    for (;;) {
        ssize_t r = read(fd, args_text + got, sizeof(args_text) - 1 - got);
        if (r < 0 && errno == EINTR)
            continue;
        if (r < 0) {
            close(fd);
            return "run_args_unreadable";
        }
        if (r == 0)
            break;
        got += (size_t)r;
        if (got == sizeof(args_text) - 1) {
            close(fd);
            return "run_args_too_long";
        }
    }
    close(fd);
    args_text[got] = '\0';
    return NULL;
}

// split the stage lines into argv, log paths and environment
static const char *build_stages(void)
{
    char *lines[STAGES];
    if (run_args_split(args_text, lines, STAGES) != STAGES)
        return "not_four_lines";
    for (int i = 0; i < STAGES; i++) {
        stage_argv[i][0] = exe_path;
        if (run_args_argv(lines[i], stage_argv[i], MAX_ARGS, &stage_name[i]) < 0)
            return "bad_line";
        if (strchr(stage_name[i], '/') || !strcmp(stage_name[i], ".") || !strcmp(stage_name[i], ".."))
            return "bad_stage_name";
        snprintf(log_path[i], sizeof(log_path[i]), "%s/log/%s.log", files_dir, stage_name[i]);
    }
    int n = 0;
    for (char **e = environ; e && *e; e++) {
        if (!strncmp(*e, "MILK_SHM_DIR=", 13))
            continue;
        if (n == MAX_ENV)
            return "environment_too_large";
        stage_env[n++] = *e;
    }
    snprintf(shm_env, sizeof(shm_env), "MILK_SHM_DIR=%s/shm", files_dir);
    stage_env[n++] = shm_env;
    stage_env[n] = NULL;
    return NULL;
}

// exec the pipeline for stage i in the forked process
static void exec_stage(int i, const sigset_t *empty, const struct sigaction *dfl)
{
    for (int s = 1; s < NSIG; s++)
        sigaction(s, dfl, NULL);
    sigprocmask(SIG_SETMASK, empty, NULL);
    if (chdir(files_dir) != 0)
        _exit(126);
    int fd = open(log_path[i], O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        _exit(126);
    if (dup2(fd, 1) < 0 || dup2(fd, 2) < 0)
        _exit(126);
    if (fd > 2)
        close(fd);
    execve(exe_path, stage_argv[i], stage_env);
    static const char msg[] = "loop_app: execve of the pipeline failed\n";
    write(2, msg, sizeof(msg) - 1);
    _exit(127);
}

// wait for a stage process and return its exit status
static int wait_stage(pid_t pid)
{
    int st;
    pid_t r;
    do
        r = waitpid(pid, &st, 0);
    while (r < 0 && errno == EINTR);
    if (r != pid)
        return -1;
    if (WIFEXITED(st))
        return WEXITSTATUS(st);
    if (WIFSIGNALED(st))
        return 128 + WTERMSIG(st);
    return -1;
}

// read the sampler's interval and window from files/headroom.args
static const char *read_headroom_args(void)
{
    char path[PATH_MAX], text[HEADROOM_ARGS_BYTES];
    headroom_on = 0;
    snprintf(path, sizeof(path), "%s/headroom.args", files_dir);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return errno == ENOENT ? NULL : "headroom_args_unreadable";
    ssize_t r;
    do
        r = read(fd, text, sizeof(text) - 1);
    while (r < 0 && errno == EINTR);
    close(fd);
    if (r < 0 || r == (ssize_t)sizeof(text) - 1)
        return "headroom_args_unreadable";
    text[r] = '\0';
    if (run_headroom_args(text, &headroom_interval_ms, &headroom_window_ms, &headroom_spin_ms) != 0)
        return "bad_headroom_args";
    headroom_on = 1;
    return NULL;
}

// return CLOCK_BOOTTIME in ns
static long long boottime_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_BOOTTIME, &t);
    return (long long)t.tv_sec * 1000000000LL + t.tv_nsec;
}

// return this thread's CPU time in ns
static long long thread_cpu_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t);
    return (long long)t.tv_sec * 1000000000LL + t.tv_nsec;
}

// spend ms of this thread's CPU time
static void spin_cpu_ms(int ms)
{
    long long until = thread_cpu_ns() + (long long)ms * 1000000LL;
    volatile unsigned long x = 0;
    while (thread_cpu_ns() < until)
        for (int i = 0; i < 1000; i++)
            x += (unsigned long)i;
}

// sample Android's CPU headroom every interval into files/log/headroom.log
static void *headroom_sampler(void *arg)
{
    int gen = (int)(intptr_t)arg;
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/log/headroom.log", files_dir);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) {
        __android_log_print(ANDROID_LOG_ERROR, TAG, "cannot write %s: %s", path, strerror(errno));
        return NULL;
    }
    void *lib = dlopen("libandroid.so", RTLD_NOW);
    headroom_create_fn create = lib ? (headroom_create_fn)dlsym(lib, "ACpuHeadroomParams_create") : NULL;
    headroom_destroy_fn destroy = lib ? (headroom_destroy_fn)dlsym(lib, "ACpuHeadroomParams_destroy") : NULL;
    headroom_set_fn set_window = lib ? (headroom_set_fn)dlsym(lib, "ACpuHeadroomParams_setCalculationWindowMillis") : NULL;
    headroom_set_fn set_type = lib ? (headroom_set_fn)dlsym(lib, "ACpuHeadroomParams_setCalculationType") : NULL;
    headroom_get_fn get = lib ? (headroom_get_fn)dlsym(lib, "ASystemHealth_getCpuHeadroom") : NULL;
    if (!create || !destroy || !set_window || !set_type || !get) {
        dprintf(fd, "HEADROOM error=no_symbol\n");
        close(fd);
        return NULL;
    }
    void *params = create();
    set_type(params, HEADROOM_TYPE_AVERAGE);
    set_window(params, headroom_window_ms);
    dprintf(fd, "HEADROOM interval_ms=%d window_ms=%d spin_ms=%d type=average clock=boottime\n", headroom_interval_ms,
            headroom_window_ms, headroom_spin_ms);
    struct timespec gap = {headroom_interval_ms / 1000, (long)(headroom_interval_ms % 1000) * 1000000L};
    while (__atomic_load_n(&headroom_going, __ATOMIC_ACQUIRE) == gen) {
        float value = NAN;
        spin_cpu_ms(headroom_spin_ms);
        long long t = boottime_ns();
        int rc = get(params, &value);
        long long took_us = (boottime_ns() - t) / 1000;
        if (rc == 0 && !isnan(value))
            dprintf(fd, "H t_ns=%lld rc=0 value=%.2f call_us=%lld\n", t, value, took_us);
        else
            dprintf(fd, "H t_ns=%lld rc=%d value=nan call_us=%lld\n", t, rc, took_us);
        nanosleep(&gap, NULL);
    }
    destroy(params);
    dprintf(fd, "HEADROOM done\n");
    close(fd);
    __atomic_store_n(&headroom_exited, gen, __ATOMIC_RELEASE);
    return NULL;
}

// stop the headroom sampler, waiting at most 2 s for it
static void stop_headroom(void)
{
    int gen = __atomic_load_n(&headroom_going, __ATOMIC_ACQUIRE);
    if (!gen)
        return;
    __atomic_store_n(&headroom_going, 0, __ATOMIC_RELEASE);
    struct timespec tick = {0, 10 * 1000000L};
    for (int i = 0; i < 200 && __atomic_load_n(&headroom_exited, __ATOMIC_ACQUIRE) != gen; i++)
        nanosleep(&tick, NULL);
    if (__atomic_load_n(&headroom_exited, __ATOMIC_ACQUIRE) == gen) {
        pthread_join(headroom_thread, NULL);
    } else {
        __android_log_print(ANDROID_LOG_WARN, TAG, "the headroom sampler is still in a call after 2 s; leaving it");
        pthread_detach(headroom_thread);
    }
}

// start the four stages, wait for them and write their exit codes
static void run_stages(void)
{
    remove_file("rc");
    remove_file("pids");
    const char *reason = read_args();
    if (!reason)
        reason = build_stages();
    if (!reason)
        reason = read_headroom_args();
    if (reason) {
        write_error(reason);
        return;
    }
    if (headroom_on) {
        headroom_gen++;
        __atomic_store_n(&headroom_going, headroom_gen, __ATOMIC_RELEASE);
        if (pthread_create(&headroom_thread, NULL, headroom_sampler, (void *)(intptr_t)headroom_gen) != 0) {
            __atomic_store_n(&headroom_going, 0, __ATOMIC_RELEASE);
            write_error("headroom_thread_failed");
            return;
        }
    }
    sigset_t empty;
    sigemptyset(&empty);
    struct sigaction dfl;
    memset(&dfl, 0, sizeof(dfl));
    dfl.sa_handler = SIG_DFL;
    int started = 0;
    for (; started < STAGES; started++) {
        pid_t pid = fork();
        if (pid == 0)
            exec_stage(started, &empty, &dfl);
        if (pid < 0)
            break;
        stage_pid[started] = pid;
    }
    char pids[STAGES * 16] = "";
    for (int i = 0; i < started; i++)
        snprintf(pids + strlen(pids), sizeof(pids) - strlen(pids), "%d\n", (int)stage_pid[i]);
    if (write_file("pids", pids) != 0)
        __android_log_print(ANDROID_LOG_ERROR, TAG, "cannot write %s/pids: %s", files_dir, strerror(errno));
    if (started < STAGES) {
        for (int i = 0; i < started; i++)
            kill(stage_pid[i], SIGTERM);
        for (int i = 0; i < started; i++)
            wait_stage(stage_pid[i]);
        stop_headroom();
        write_error("fork_failed");
        return;
    }
    char line[256] = "RC";
    for (int i = 0; i < STAGES; i++) {
        int rc = wait_stage(stage_pid[i]);
        snprintf(line + strlen(line), sizeof(line) - strlen(line), " %s=%d", stage_name[i], rc);
    }
    strcat(line, "\n");
    stop_headroom();
    __android_log_print(ANDROID_LOG_INFO, TAG, "%s", line);
    if (write_file("rc", line) != 0)
        __android_log_print(ANDROID_LOG_ERROR, TAG, "cannot write %s/rc: %s", files_dir, strerror(errno));
}

// run the stages on their own thread
static void *stage_thread(void *unused)
{
    (void)unused;
    run_stages();
    pthread_mutex_lock(&run_lock);
    run_going = 0;
    if (current_activity)
        ANativeActivity_finish(current_activity);
    pthread_mutex_unlock(&run_lock);
    return NULL;
}

// set files_dir and exe_path
static const char *find_paths(struct android_app *app)
{
    Dl_info info;
    char lib[PATH_MAX];
    if (!app->activity->internalDataPath)
        return "no_files_dir";
    snprintf(files_dir, sizeof(files_dir), "%s", app->activity->internalDataPath);
    if (!dladdr((void *)&android_main, &info) || !info.dli_fname)
        return "no_library_dir";
    snprintf(lib, sizeof(lib), "%s", info.dli_fname);
    const char *dir = dirname(lib);
    if (!dir)
        return "no_library_dir";
    snprintf(exe_path, sizeof(exe_path), "%s/libpipeline.so", dir);
    return NULL;
}

// start a run on the activity's first resume
static void on_app_cmd(struct android_app *app, int32_t cmd)
{
    int *resumed = app->userData;
    if (cmd != APP_CMD_RESUME || *resumed)
        return;
    *resumed = 1;
    pthread_mutex_lock(&run_lock);
    if (run_going) {
        pthread_mutex_unlock(&run_lock);
        __android_log_print(ANDROID_LOG_WARN, TAG, "a run is already going in this process; not starting another");
        return;
    }
    const char *reason = find_paths(app);
    if (reason) {
        if (files_dir[0])
            write_error(reason);
        else
            __android_log_print(ANDROID_LOG_ERROR, TAG, "%s", reason);
        ANativeActivity_finish(app->activity);
        pthread_mutex_unlock(&run_lock);
        return;
    }
    pthread_t t;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    if (pthread_create(&t, &attr, stage_thread, NULL) != 0) {
        write_error("thread_failed");
        ANativeActivity_finish(app->activity);
    } else {
        run_going = 1;
    }
    pthread_attr_destroy(&attr);
    pthread_mutex_unlock(&run_lock);
}

// poll the activity's looper until it is destroyed
void android_main(struct android_app *app)
{
    int resumed = 0;
    app->userData = &resumed;
    app->onAppCmd = on_app_cmd;
    ANativeActivity_setWindowFlags(app->activity, AWINDOW_FLAG_KEEP_SCREEN_ON, 0);
    pthread_mutex_lock(&run_lock);
    current_activity = app->activity;
    pthread_mutex_unlock(&run_lock);
    while (!app->destroyRequested) {
        int events;
        struct android_poll_source *source = NULL;
        if (ALooper_pollOnce(-1, NULL, &events, (void **)&source) >= 0 && source)
            source->process(app, source);
    }
    pthread_mutex_lock(&run_lock);
    if (current_activity == app->activity)
        current_activity = NULL;
    pthread_mutex_unlock(&run_lock);
}
