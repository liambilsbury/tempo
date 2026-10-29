#!/usr/bin/env bash
# build build/android/tempo-loop.apk from build/android/pipeline
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
NDK="${NDK:-}" SDK="${SDK:-}"
[ -n "$NDK" ] || { echo "android_app: set NDK to the NDK root" >&2; exit 2; }
[ -n "$SDK" ] || { echo "android_app: set SDK to the Android SDK root" >&2; exit 2; }
CLANG="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
GLUE="$NDK/sources/android/native_app_glue"
BT="$SDK/build-tools/34.0.0"
AAPT2="$BT/aapt2" ZIPALIGN="$BT/zipalign" APKSIGNER="$BT/apksigner"
ANDROID_JAR="$SDK/platforms/android-34/android.jar"
SRC=src/android/app
PIPELINE=build/android/pipeline
KS=build/android/debug.keystore
APK=build/android/tempo-loop.apk

# refuse a missing tool or input by name
need() { [ -e "$2" ] || { echo "android_app: no $1 at $2" >&2; exit 2; }; }
need "NDK clang" "$CLANG"
need "native_app_glue" "$GLUE/android_native_app_glue.c"
need aapt2 "$AAPT2"
need zipalign "$ZIPALIGN"
need apksigner "$APKSIGNER"
need android.jar "$ANDROID_JAR"
need "pipeline from make android" "$PIPELINE"
for t in keytool zip; do command -v "$t" >/dev/null || { echo "android_app: no $t on PATH" >&2; exit 2; }; done

mkdir -p build/android
if [ ! -f "$KS" ]; then
    echo "android_app: generating the debug keystore at $KS"
    keytool -genkeypair -keystore "$KS" -storepass android -keypass android -alias tempo -keyalg RSA -keysize 2048 \
        -validity 10000 -dname "CN=Tempo loop debug" >/dev/null
fi
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/lib/x86_64"
"$CLANG" --target=x86_64-linux-android33 -O2 -fPIC -shared -Wall -Wextra -I"$GLUE" -I"$SRC" \
    -o "$WORK/lib/x86_64/libtempoloop.so" "$GLUE/android_native_app_glue.c" "$SRC/loop.c" \
    -u ANativeActivity_onCreate -landroid -llog
cp "$PIPELINE" "$WORK/lib/x86_64/libpipeline.so"
"$AAPT2" link -o "$WORK/base.apk" --manifest "$SRC/AndroidManifest.xml" -I "$ANDROID_JAR" \
    --min-sdk-version 33 --target-sdk-version 34 --debug-mode
(cd "$WORK" && zip -q -0 base.apk lib/x86_64/libtempoloop.so lib/x86_64/libpipeline.so)
"$ZIPALIGN" -p -f 4 "$WORK/base.apk" "$WORK/aligned.apk"
"$APKSIGNER" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android --ks-key-alias tempo \
    --out "$WORK/signed.apk" "$WORK/aligned.apk"
mv -f "$WORK/signed.apk" "$APK"
echo "android_app: $APK ($(stat -c %s "$APK") bytes)"
