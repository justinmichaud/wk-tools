# The architecture a workspace *is*: `native` (the host's own, whatever that is)
# or `armhf`. A cross build against another architecture's sysroot is reserved,
# not implemented (docs/Nice to have/HANDOFF-cross-compile.md).

WK_ARCHES="native armhf"

# Pinned: `wkdev-create --arch` otherwise resolves to the aarch64 image and hands
# podman that with --arch=arm. 24.04_arm32_arm64 is arm64 with armhf multiarch.
WK_IMAGE_ARMHF="ghcr.io/igalia/wkdev-sdk:24.04_arm32"

arch_canon() {
    case "${1:-}" in
        ''|native|host|arm64|aarch64|64) echo native ;;
        armhf|arm32|armv7|arm|32)        echo armhf ;;
        riscv64|riscv)
            die "riscv64 is a cross-build target, not a workspace architecture:
    this machine cannot execute riscv64 natively, so it needs a sysroot.
    See docs/Nice to have/HANDOFF-cross-compile.md; 'wk build --sysroot' is where it will go." ;;
        *) die "unknown architecture '$1' (one of: $WK_ARCHES)
    A workspace's --arch is what it runs *natively*. To build for something
    this machine cannot execute, that is a cross build -- see
    docs/Nice to have/HANDOFF-cross-compile.md." ;;
    esac
}

arch_is_native() { [ "${1:-native}" = native ]; }

arch_podman() {  # podman calls 32-bit ARM "arm"; ours is armhf since "arm" is ambiguous
    case "$1" in
        armhf) echo arm ;;
        *)     echo "" ;;
    esac
}

arch_image() {
    case "$1" in
        armhf) echo "$WK_IMAGE_ARMHF" ;;
        *)     echo "" ;;
    esac
}

arch_has_gpu() { [ "${1:-native}" != armhf ]; }  # NVIDIA userspace is aarch64-only (host/linux/gpu.sh)

# linux32 is load-bearing: an armhf container's kernel is the host's aarch64
# kernel, so `uname -m` reports aarch64 (armv8l under linux32), and CMake
# reads CMAKE_SYSTEM_PROCESSOR from uname (WebKitCommon.cmake:167-172).
arch_wrapper() {
    case "$1" in
        armhf) echo linux32 ;;
        *)     echo "" ;;
    esac
}

# Pins an FPU (JSC's ARMv7 JIT requires VFP) and Thumb-2. -Wno-pass-failed:
# WTF's vectorize pragmas fail on ARMv7 regardless of -mfpu. -mno-unaligned-access
# is absent because JSC crashes regardless (a WebKit bug). docs/HANDOFF-linux-arm32.md.
arch_cflags() {
    case "$1" in
        armhf) echo "-mthumb -march=armv7-a+fp -Wno-pass-failed" ;;
        *)     echo "" ;;
    esac
}

# A 32-bit ld has under 4 GB of address space regardless of the machine's RAM;
# gold with mapping/caching off fits, the default (lld, mmap the world) OOMs.
arch_ldflags() {
    case "$1" in
        armhf) echo "-mthumb -march=armv7-a+fp -fuse-ld=gold -Wl,--no-map-whole-files -Wl,--no-keep-memory -Wl,--no-keep-files-mapped -Wl,--no-mmap-output-file" ;;
        *)     echo "" ;;
    esac
}

# USE_LD_LLD=OFF: WebKit probes for lld and appends its own -fuse-ld=lld, and the
# last flag wins over -fuse-ld=gold above. port is omitted for JSCOnly, where
# naming USE_VULKAN would warn unused. No JIT: trunk has no ARMv7 assembler.
arch_cmake() {
    local flags=""
    case "$1" in
        armhf)
            flags="-DUSE_LD_LLD=OFF"
            case "${2:-}" in  # off: the arm32 image predates these deps (no volk/vpx, broken Qt6)
                --wpe|--gtk)
                    flags="$flags -DUSE_VULKAN=OFF -DENABLE_WEB_RTC=OFF -DENABLE_WPE_QT_API=OFF" ;;
            esac
            ;;
    esac
    echo "$flags"
}

arch_label() {
    case "${1:-native}" in
        native) echo "" ;;
        *)      echo "$1" ;;
    esac
}
