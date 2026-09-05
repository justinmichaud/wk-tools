WK_ARCHES="native armhf"

# Pinned: `wkdev-create --arch` otherwise resolves the aarch64 image and hands
# podman that with --arch=arm. This tag is arm64 with armhf multiarch.
WK_IMAGE_ARMHF="ghcr.io/igalia/wkdev-sdk:24.04_arm32"

arch_canon() {
    case "${1:-}" in
        ''|native|host|arm64|aarch64|64) echo native ;;
        armhf|arm32|armv7|arm|32)        echo armhf ;;
        riscv64|riscv)  # a cross target, not an arch: docs/Nice to have/HANDOFF-cross-compile.md
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

arch_wrapper() {  # CMake takes CMAKE_SYSTEM_PROCESSOR from uname, which without linux32 reports the host's aarch64 kernel
    case "$1" in
        armhf) echo linux32 ;;
        *)     echo "" ;;
    esac
}

arch_cflags() {  # JSC's ARMv7 JIT requires VFP; WTF's vectorize pragmas fail on ARMv7 regardless of -mfpu (docs/HANDOFF-linux-arm32.md)
    case "$1" in
        armhf) echo "-mthumb -march=armv7-a+fp -Wno-pass-failed" ;;
        *)     echo "" ;;
    esac
}

arch_ldflags() {  # a 32-bit ld has under 4 GB of address space whatever the machine's RAM: gold with mapping off fits, lld mmaps the world and OOMs
    case "$1" in
        armhf) echo "-mthumb -march=armv7-a+fp -fuse-ld=gold -Wl,--no-map-whole-files -Wl,--no-keep-memory -Wl,--no-keep-files-mapped -Wl,--no-mmap-output-file" ;;
        *)     echo "" ;;
    esac
}

arch_cmake() {  # USE_LD_LLD=OFF: WebKit appends its own -fuse-ld=lld, and the last flag wins over the gold above
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
