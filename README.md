# Tempo

Tempo measures how much of a real-time control loop's deadline-miss tail comes from the system (interrupts, TLB shootdowns, preemption by other work) rather than the computation (the loop's own processing of each frame).

The loop is a four-stage pipeline (sensor, compute, actuator, telemetry), each stage its own process, passing frames over [ImageStreamIO](https://github.com/milk-org/ImageStreamIO) shared memory at a fixed period. A frame that finishes after its deadline is a miss.

### How it measures

The loop runs in a KVM guest, single-level or nested. An injector on the host loads the guest's cores with IPIs, TLB shootdowns, cache pressure or CPU load. Conditions run interleaved and are compared as ratios against a control.

Each run records what reached the guest: IPIs and VM exits counted on the host, and the guest's steal time, from `/proc/stat` or in ns from `tempo_steal`.

### On Android

Tempo can run the loop as an [Android app](src/android/app/) on a VM booted with [Rhizo](https://github.com/liambilsbury/rhizo), with the CPU load injected inside the VM or on the host.

## Build

```bash
bash tools/fetch/dependencies.sh  # -> third_party/ImageStreamIO/
make                              # -> build/pipeline build/inject build/irq_trigger
bash tools/build/guest_kernel.sh  # -> build/kernel-rt/
bash tools/build/make_image.sh    # -> build/l2-initramfs.cpio.gz
bash benchmark/vm/make.sh         # -> build/vm/l1.qcow2
make android-app NDK=... SDK=...  # -> build/android/tempo-loop.apk
```

## Usage

```bash
benchmark/run/experiment.sh --label control --inject none --pin 8,9,10,11  # -> results/experiments/control/
python3 benchmark/run/paired.py --condition control:none --condition hog80:cpu-load:80 --out rounds.json  # -> rounds.json
```

## License

`src/guest/tempo_steal.c` and `kernel/sched-deadline/` are GPL-2.0, see `LICENSES/GPL-2.0.txt`.
