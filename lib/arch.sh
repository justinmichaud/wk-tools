# The architecture a workspace *is*, and the flags that follow from it.
#
# Three different things on this machine could all be called "building 32-bit",
# and conflating any two of them produces a build that looks right and is not.
# They get three different words, here and everywhere else:
#
#   arch      the workspace's own userland. `wk new <name> --arch armhf` gives
#             an armhf container: armhf clang, armhf libraries, armhf ninja,
#             executing natively on this Neoverse-N1 (AArch32 at EL0, which is
#             why this is a Linux-only capability -- Apple Silicon has none).
#             Everything built in it is native. There is no sysroot, no
#             emulation and no target triple to get wrong, so the configs mean
#             what they always meant: `wk build jsc-release` in an armhf
#             workspace is an armhf JSC.
#
#   sysroot   a *cross* build: an aarch64 workspace, an aarch64 clang, and
#             another architecture's libraries mounted in to link against.
#             That is the wkdev-sysroot images (`_arm`, `_riscv64`) and the
#             multiarch `24.04_arm32_arm64` image, and it is a property of the
#             build rather than of the workspace -- one aarch64 workspace can
#             cross-build several ways. Reserved here, not implemented: see
#             docs/Nice to have/HANDOFF-cross-compile.md, and `wk build --sysroot`, which
#             refuses with that pointer rather than doing something plausible.
#
#   target    another machine entirely -- an rpi5, a remote box, a device
#             booted off a benchmark image. `wk new --target`, not --arch.
#
# The distinction is not pedantry. A native armhf build gets its ABI from the
# image and needs no -m32 at all; the cross build needs -m32, an explicit
# --sysroot, CMAKE_LIBRARY_ARCHITECTURE and CMAKE_PREFIX_PATH (see
# webkitdirs.pm:2925-2941, the `--32-bit` path, which is written for the cross
# case and is wrong for this one). Two mechanisms, one name, would mean every
# build flag is a guess about which one you meant.

# The canonical names. `native` is not a synonym for aarch64: it means "the
# same architecture as the host", which is what makes it the right default on
# a machine whose port this all gets carried to later.
WK_ARCHES="native armhf"

# The image an armhf workspace is created from. Pinned to a tag rather than
# resolved: `wkdev-create --arch` picks the image by appending an arch suffix
# to whatever version it would otherwise have used, which on this machine finds
# the aarch64 image of the current SDK version and hands podman an arm64 image
# with --arch=arm. Overridable, because the arm32 image is published far less
# often than the aarch64 one and pinning the wrong one is a five-minute pull.
#
# 24.04_arm32 is the *native* armhf image: dpkg architecture armhf, and
# /usr/local/bin/clang is an armhf clang targeting arm-unknown-linux-gnueabihf.
# Do not "upgrade" this to 24.04_arm32_arm64, which despite the name is an
# arm64 image with armhf as a foreign multiarch architecture -- that is the
# sysroot mechanism above, and it belongs to --sysroot when that lands.
WK_IMAGE_ARMHF="${WK_IMAGE_ARMHF:-ghcr.io/igalia/wkdev-sdk:24.04_arm32}"

# arch_canon <word> -- the canonical name, or die naming the alternatives.
#
# Aliases are accepted because "32" is what the wiki pages and five years of
# muscle memory say, but only the canonical name is ever stored or printed:
# the whole point of the vocabulary is that one workspace cannot be described
# two ways in two places.
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

# What to pass to `wkdev-create --arch`, which passes it to podman. podman's
# name for 32-bit ARM is "arm"; ours is armhf, because "arm" on an aarch64 host
# is ambiguous in exactly the way this file exists to prevent.
arch_podman() {
    case "$1" in
        armhf) echo arm ;;
        *)     echo "" ;;
    esac
}

# The image to create the workspace from, empty for native (where the SDK's own
# version resolution is right and should be left alone).
arch_image() {
    case "$1" in
        armhf) echo "$WK_IMAGE_ARMHF" ;;
        *)     echo "" ;;
    esac
}

# Does a workspace of this architecture get the GPU?
#
# No, for armhf, and this is not a policy choice: the NVIDIA userspace
# libraries must match the host kernel driver exactly and are published for
# aarch64 only, so there is nothing to inject. wkdev-create already skips its
# own nvidia handling whenever --arch is set, with a comment saying so; this is
# the same fact on our side, where the derived injection lives (host/linux/gpu.sh).
#
# The consequence belongs in the benchmark preflight, not here: an armhf
# workspace is a software-rendering workspace, which is fine for JSC and for
# CPU-class benchmarks and meaningless for MotionMark.
arch_has_gpu() { [ "${1:-native}" != armhf ]; }

# --- build flags -------------------------------------------------------------
# Everything below is a property of the architecture, not of the config. That
# is why it lives here and reaches the build through WK_ARCH_* rather than
# through CFG_ARGS: `jsc-release` names a port and a build type, and it must go
# on meaning that in every workspace.

# The wrapper the whole build runs under, if any.
#
# linux32 is load-bearing for armhf and its absence is silent. The kernel is
# the host's aarch64 kernel, so `uname -m` in an armhf container answers
# aarch64 (measured: `podman run --arch arm ... uname -m` -> aarch64, and under
# linux32 -> armv8l). CMake reads CMAKE_SYSTEM_PROCESSOR from uname, and
# WebKitCommon.cmake:167-172 turns aarch64 into WTF_CPU_ARM64 -- so without
# this, a 32-bit compiler builds a tree configured for 64-bit ARM, and the
# first thing that notices is the assembler. With it, armv8l matches the arm
# branch and CMAKE_SYSTEM_PROCESSOR is forced to armv7l, which is correct and
# needs no FORCE_32BIT at all.
arch_wrapper() {
    case "$1" in
        armhf) echo linux32 ;;
        *)     echo "" ;;
    esac
}

# Compiler flags. Prepended to whatever the build already sets, never replacing.
#
# The image's clang already targets arm-unknown-linux-gnueabihf and already
# defaults to __ARM_ARCH 7 with hard float, so this is not what makes the build
# 32-bit -- being armhf is. It pins the two things Ubuntu's armhf baseline
# leaves lower than WebKit wants: an FPU (JSC's ARMv7 JIT requires VFP) and
# Thumb-2 (smaller code, and the tier of the ISA the 32-bit backend is written
# and tested against).
#
# -Wno-pass-failed is not in the invocation the wiki pages carry, and without it
# a current checkout does not build at all. WTF asks clang to vectorize several
# loops with `#pragma clang loop vectorize(enable)` -- SHA1::addUTF8Bytes,
# StringImpl, URLParser, the simdutf paths -- and on ARMv7 clang cannot form
# them, which is a -Wpass-failed=transform-warning, which -Werror turns into
# nine failed translation units in WTF alone. Measured here: the pragmas fail
# identically with -mfpu=neon and -mfpu=neon-vfpv4, so this is not a missing
# NEON and cannot be fixed by asking for a wider machine -- the diagnostic is
# simply not actionable on this target. Silenced narrowly rather than by
# passing --no-fatal-warnings, which would drop -Werror for the whole build and
# hide every other warning a 32-bit build is worth having.
# There is no -mno-unaligned-access here, and that is a measured decision
# rather than an omission. JSC as it stands crashes on this machine --
# `JSC::OpEnumeratorPutByVal::decode` faults on `ldmia r5, {r2, r3, r5}` with
# r5 three bytes out of alignment, killing anything that runs a for-in loop
# inside a function -- and the obvious flag does not prevent it: clang emits
# that ldmia because the source promises the pointer is aligned, so telling the
# compiler that unaligned access is unavailable changes nothing. Verified by
# building both ways. It is a WebKit bug (see docs/HANDOFF-linux-arm32.md), not
# a flag to compensate for, and adding a flag that silently does not work would
# be worse than the crash.
arch_cflags() {
    case "$1" in
        armhf) echo "-mthumb -march=armv7-a+fp -Wno-pass-failed" ;;
        *)     echo "" ;;
    esac
}

# Linker flags. This is the block that decides whether a 32-bit WebKit link
# finishes at all.
#
# A link of libWebKit is the one step in the build whose peak memory is not
# divisible by the job count, and a 32-bit ld has under 4 GB of address space
# to do it in regardless of the 512 GB on the machine. gold with the mapping
# and caching turned off trades a slower link for one that fits; the default
# (lld, mmap the world) exits with an out-of-memory error that reads as a
# machine problem and is not.
arch_ldflags() {
    case "$1" in
        armhf) echo "-mthumb -march=armv7-a+fp -fuse-ld=gold -Wl,--no-map-whole-files -Wl,--no-keep-memory -Wl,--no-keep-files-mapped -Wl,--no-mmap-output-file" ;;
        *)     echo "" ;;
    esac
}

# CMake flags that follow from the architecture. USE_LD_LLD=OFF is not
# redundant with -fuse-ld=gold above: WebKit probes for lld and adds its own
# -fuse-ld=lld, and the last one on the command line wins, so leaving this on
# quietly puts the link back on the linker the flags above were chosen to
# avoid.
#
# There is deliberately no attempt here to turn the JIT on, and that is worth
# stating because the opposite looks obviously right: an armhf `jsc-release` is
# a CLoop interpreter build, with no JIT, no Wasm and no sampling profiler, and
# a config name that means "JIT build" on every other architecture quietly
# means "interpreter" here.
#
# On trunk it cannot mean anything else. There is no 32-bit ARM JIT there any
# more: `Source/JavaScriptCore/assembler/` has ARM64, ARM64E, X86_64 and RISCV64
# assemblers and no ARMv7 one; offlineasm's BACKENDS list
# (offlineasm/backends.rb:36) is the same four plus C_LOOP, so
# `OFFLINE_ASM_BACKEND` is simply unset for WTF_CPU_ARM and the LLInt generator
# fails with "undefined method `split' for nil" if ENABLE_JIT is forced on; and
# `PlatformEnable.h:724-727` settles it at compile time regardless of CMake --
# `#if !CPU(ADDRESS64)` undefines ENABLE_JIT and defines it to 0.
#
# It is a statement about the checkout, not about armhf. 2.48 still has the
# ARMv7 backend and still works, and a workspace can be put on that branch --
# which is exactly why nothing here forces the pair either way. Whatever the
# tree supports is what gets built, and `wk build --dry-run` shows which it
# was. See docs/HANDOFF-linux-arm32.md.
# arch_cmake <arch> [port]
#
# The port matters for one flag and only on armhf, which is why it is passed in
# rather than assumed: USE_VULKAN is a WPE/GTK option and naming it for a
# JSCOnly build would make CMake warn about an unused variable on every build.
arch_cmake() {
    local flags=""
    case "$1" in
        armhf)
            flags="-DUSE_LD_LLD=OFF"
            # Three features off for the browser ports, and they are one
            # problem wearing three hats: the published arm32 image
            # (24.04_arm32) is eight months old and trunk has moved past its
            # dependency set. The registry has no newer one -- the only
            # arm32-ish tags are 24.04_arm32, _arm32_amd64 and _arm32_arm64,
            # all from the same build -- so this is not a "pull a newer image"
            # fix. Each was found by hitting it: configure fails, disable, hit
            # the next.
            #
            #   USE_VULKAN         volk is required for it (OptionsWPE.cmake:270)
            #                      and the image has no volk. No loss: an armhf
            #                      workspace has no GPU, so its Vulkan path
            #                      would be llvmpipe pretending.
            #   ENABLE_WEB_RTC     libwebrtc's CMake wants a `vpx` target the
            #                      image does not produce ("Cannot specify
            #                      include directories for target vpx which is
            #                      not built by this project").
            #   ENABLE_WPE_QT_API  the image's Qt6 is broken, not missing:
            #                      Qt::QuickPrivate points at
            #                      /usr/include/arm-linux-gnueabihf/qt6/QtQuick/6.4.2,
            #                      which does not exist, and CMake fails at the
            #                      generate step rather than while configuring.
            #
            # These are properties of the image rather than of 32-bit ARM, and
            # they are keyed on the architecture only because the architecture
            # is what picks the image. Delete them when a current arm32 image
            # exists -- or when building a branch of WebKit contemporary with
            # this one, which is the other way the mismatch goes away.
            case "${2:-}" in
                --wpe|--gtk)
                    flags="$flags -DUSE_VULKAN=OFF -DENABLE_WEB_RTC=OFF -DENABLE_WPE_QT_API=OFF" ;;
            esac
            ;;
    esac
    echo "$flags"
}

# How an architecture is named in output. Native says nothing -- it is the
# unremarkable case and a workspace should not have to explain itself -- so
# this is empty there and callers print it with a leading separator.
arch_label() {
    case "${1:-native}" in
        native) echo "" ;;
        *)      echo "$1" ;;
    esac
}
