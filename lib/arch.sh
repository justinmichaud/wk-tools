WK_ARCHES="native armhf"

# Pinned, arm64 with armhf multiarch: `wkdev-create --arch` would hand podman the aarch64 image with --arch=arm.
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

arch_has_gpu() { [ "${1:-native}" != armhf ]; }  # NVIDIA userspace is aarch64-only (host/linux/gpu.sh)
