IMAGESTREAMIO   := third_party/ImageStreamIO
IMAGESTREAMIO_LIB := $(IMAGESTREAMIO)/build/libImageStreamIO.so

CC      ?= cc
CFLAGS  ?= -O2 -g -Wall -Wextra -Wno-unused-parameter -std=gnu11
CPPFLAGS := -I$(IMAGESTREAMIO) -I$(IMAGESTREAMIO)/build -Isrc/pipeline
LDFLAGS := -Wl,-rpath,$(abspath $(IMAGESTREAMIO)/build)
LDLIBS  := -L$(IMAGESTREAMIO)/build -lImageStreamIO -lpthread -lrt -lm

BIN := build/pipeline build/inject build/irq_trigger

.PHONY: all clean dependencies lint help android android-app rhizo-vm-cpu-load
all: $(BIN)

help:
	@echo "make                    build/pipeline build/inject build/irq_trigger"
	@echo "make dependencies       third_party/ImageStreamIO"
	@echo "make lint               pyflakes, shellcheck -S warning"
	@echo "make android            NDK="
	@echo "make android-app        build/android/tempo-loop.apk NDK= SDK="
	@echo "make rhizo-vm-cpu-load  static cpu_load"
	@echo "make clean"

lint:
	@bash tools/lint.sh

$(IMAGESTREAMIO_LIB):
	bash tools/fetch/dependencies.sh

build/pipeline: src/pipeline/pipeline.c src/pipeline/frame.h src/pipeline/reconstruct.h src/pipeline/reserve.h $(IMAGESTREAMIO_LIB) | build
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ src/pipeline/pipeline.c $(LDFLAGS) $(LDLIBS)

build/inject: src/host/inject.c | build
	$(CC) $(CFLAGS) -o $@ src/host/inject.c -lpthread

build/irq_trigger: src/host/irq_trigger.c | build
	$(CC) $(CFLAGS) -o $@ src/host/irq_trigger.c

build:
	mkdir -p build

dependencies: $(IMAGESTREAMIO_LIB)

clean:
	rm -f $(BIN)

NDK ?= $(ANDROID_NDK_HOME)
SDK ?= $(ANDROID_SDK_ROOT)
API ?= 33
ACLANG   := $(NDK)/toolchains/llvm/prebuilt/linux-x86_64/bin/clang
ACFLAGS  := --target=x86_64-linux-android$(API) -O2 -g -Wall -Wextra -Wno-unused-parameter -std=gnu11

android: build/android/pipeline
android-app: build/android/tempo-loop.apk
rhizo-vm-cpu-load: build/rhizo_vm/cpu_load

build/android/pipeline: src/android/pipeline.c src/android/args.h src/pipeline/pipeline.c src/pipeline/frame.h src/pipeline/reconstruct.h src/pipeline/reserve.h $(IMAGESTREAMIO_LIB)
	@test -x "$(ACLANG)" || { echo "android: no $(ACLANG)" >&2; exit 1; }
	mkdir -p $(@D)
	$(ACLANG) $(ACFLAGS) $(CPPFLAGS) -o $@ src/android/pipeline.c $(IMAGESTREAMIO)/ImageStreamIO.c -landroid -lm

build/android/tempo-loop.apk: build/android/pipeline $(wildcard src/android/app/*) tools/build/android_app.sh
	NDK="$(NDK)" SDK="$(SDK)" bash tools/build/android_app.sh

build/rhizo_vm/cpu_load: src/android/cpu_load.c
	mkdir -p $(@D)
	$(CC) $(CFLAGS) -static -o $@ src/android/cpu_load.c -lpthread

-include test/test.mk
