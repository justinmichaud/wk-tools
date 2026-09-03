#!/usr/bin/env bash
#
# The in-workspace half of a buildroot image build, driven by
# image/buildroot.sh via t_spawn from /opt/wk-tools (the read-only repo mount).
#
# Follows the wiki recipe "Building WPEWebKit for 32-bit Raspberry Pi 3
# (Buildroot DRM config)" -- the only version known to produce a booting
# image -- plus this repo's tailnet/wifi overlays, BR2_EXTERNAL libffi fix,
# and store-backed caches. Runs detached: nothing here may fail silently,
# since the log is the only account of what happened.

set -euo pipefail

# This is wk's own build: the build wall (container/bin/wk-build-wall) lets
# ninja/cmake/make through for it and refuses them to an agent's shell.
export WK_BUILD=1

SRC=/src/WebKit
TREE_URL=""; TREE_BRANCH=""; TREE_COMMIT=""; DEFCONFIG=""; EXTERNAL=0
IMAGE=""; JOBS=""; OVERLAY_ARCH=""; OVERLAY_WIFI=0; NAME=""; ROOTFS_SIZE=""
KERNEL_TAR=""; KERNEL_RELEASE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --name)        NAME="${2:-}"; shift 2 ;;
        --tree-url)    TREE_URL="${2:-}"; shift 2 ;;
        --tree-branch) TREE_BRANCH="${2:-}"; shift 2 ;;
        --tree-commit) TREE_COMMIT="${2:-}"; shift 2 ;;
        --defconfig)   DEFCONFIG="${2:-}"; shift 2 ;;
        --external)    EXTERNAL="${2:-0}"; shift 2 ;;
        --image)       IMAGE="${2:-}"; shift 2 ;;
        --jobs)        JOBS="${2:-}"; shift 2 ;;
        --kernel-tar)     KERNEL_TAR="${2:-}"; shift 2 ;;
        --kernel-release) KERNEL_RELEASE="${2:-}"; shift 2 ;;
        --overlay-arch) OVERLAY_ARCH="${2:-}"; shift 2 ;;
        --overlay-wifi) OVERLAY_WIFI="${2:-0}"; shift 2 ;;
        --rootfs-size) ROOTFS_SIZE="${2:-}"; shift 2 ;;
        *) echo "buildroot-build.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf 'wk-buildroot: %s\n' "$*"; }
fail() { printf 'wk-buildroot: error: %s\n' "$*" >&2; exit 1; }

# make exits 0 on a tree with nothing left to do, so success alone does not
# mean a fresh image -- the named image's mtime must be at least as new as
# this build's own start (portable stat: GNU's -c, BSD/macOS's -f). Same
# shape as image/yocto-build.sh's verify_image_freshness, same reason.
verify_image_freshness() { # <path to the named image> <build start epoch seconds>
    local img="$1" start="$2" mtime
    [ -f "$img" ] || fail "the configuration names '$(basename "$img")' and make
    reported success, but $img does not exist. A cog defconfig whose
    filesystem output is tar-only builds no card image, so genimage has
    nothing to assemble -- that is a defconfig question (BR2_TARGET_ROOTFS_EXT2
    above should have prevented it here), not something this stage can call
    done."
    mtime=$(stat -c %Y "$img" 2>/dev/null || stat -f %m "$img" 2>/dev/null) \
        || fail "could not read the mtime of $img"
    [ "$mtime" -ge "$start" ] || fail "make reported success but $img is older
    than this build started. That should be impossible for a defconfig
    producing a fresh genimage run every time -- if this fires, something
    upstream of genimage started skipping work it used to always do, the same
    trap verify_image_freshness (image/yocto-build.sh) guards against for
    bitbake."
}

[ -n "$NAME" ]      || fail "--name is required"
[ -n "$TREE_URL" ]  || fail "--tree-url is required"
[ -n "$DEFCONFIG" ] || fail "--defconfig is required"
[ -n "$JOBS" ]      || JOBS=$(nproc 2>/dev/null || echo 4)

# BR2_DL_DIR/BR2_CCACHE_DIR come from the container's store-backed cache mount
# (targets/container.sh), not as flags: a host path from image/buildroot.sh
# would name nothing inside this container. Their absence means the mount is
# missing, so this fails rather than building inside the workspace, where
# `wk rm` would discard the downloads with it.
for _d in "${BR2_DL_DIR:-}" "${BR2_CCACHE_DIR:-}"; do
    [ -n "$_d" ] || fail "BR2_DL_DIR/BR2_CCACHE_DIR are not set in this workspace.
    They come from the container's store-backed cache mount
    (targets/container.sh); without them buildroot's download and ccache
    caches would land in the workspace and die with it."
    mkdir -p "$_d" || fail "cannot create $_d"
    [ -w "$_d" ] || fail "$_d is not writable"
done

WORKDIR="$SRC/WebKitBuild/buildroot/$NAME"
say "tree        $TREE_URL @ ${TREE_COMMIT:-${TREE_BRANCH:-HEAD}}"
say "defconfig   $DEFCONFIG"
say "workdir     $WORKDIR"
say "jobs        -j$JOBS"
say "host        $( . /etc/os-release; echo "$PRETTY_NAME"), gcc $(gcc -dumpversion 2>/dev/null || echo '?')"

# --- the tree ------------------------------------------------------------
# Every WPE configuration here pins to 2020.02, since the release-pinned cog
# defconfigs only make sense against it. Re-used when already at the pin: an
# output/ tree is tens of gigabytes, so a second run continues rather than
# re-cloning.
if [ -d "$WORKDIR/.git" ]; then
    say "tree already present; fetching the pin"
    git -C "$WORKDIR" fetch --tags origin "${TREE_BRANCH:-HEAD}" \
        || fail "could not fetch $TREE_URL in $WORKDIR"
else
    mkdir -p "$(dirname "$WORKDIR")"
    say "cloning (this is somebody else's vendor branch; shallow would lose the tag)"
    git clone ${TREE_BRANCH:+--branch "$TREE_BRANCH"} "$TREE_URL" "$WORKDIR" \
        || fail "could not clone $TREE_URL"
fi
if [ -n "$TREE_COMMIT" ]; then
    # Not the tag: the cog defconfigs were added to the branch after 2020.02,
    # so standing at the tag alone would remove the configuration the profile
    # names. Fetched by sha since a shallow clone may not have it yet.
    git -C "$WORKDIR" fetch origin "$TREE_COMMIT" 2>/dev/null || true
    git -C "$WORKDIR" checkout --detach "$TREE_COMMIT" \
        || fail "$TREE_URL has no commit $TREE_COMMIT.
    The configuration pins one deliberately: a branch moves and the 2020.02 tag
    predates the cog defconfigs."
    say "pinned at $(git -C "$WORKDIR" rev-parse --short HEAD)"
fi

# --- this repository's corrections to the vendor tree ----------------------
# The fork pins package versions it never built (wpebackend-fdo 1.14 is a
# meson project; the fork's package still calls cmake), so the pinned tree
# takes this repository's patches: input, not hand-edit -- a fresh clone
# converges to the same tree. Idempotent: an applied patch is skipped.
for p in /opt/wk-tools/image/buildroot/tree-patches/*.patch; do
    [ -e "$p" ] || continue
    if git -C "$WORKDIR" apply --reverse --check "$p" 2>/dev/null; then
        say "tree patch already applied: $(basename "$p")"
    else
        git -C "$WORKDIR" apply "$p" \
            || fail "tree patch does not apply: $(basename "$p")
    The pin moved out from under it (BR_TREE_COMMIT); rederive the patch."
        say "tree patch applied: $(basename "$p")"
    fi
done

cd "$WORKDIR"

# --- the overlay -----------------------------------------------------------
# BR2_ROOTFS_OVERLAY copies files in at target-finalize without needing a
# package; it takes a space-separated list, so both overlays sit side by side.
# Neither script carries a credential: the tailnet key and wifi passphrase
# arrive with the card, not the image.
OVERLAY=""
if [ -n "$OVERLAY_ARCH" ]; then
    OVERLAY="$WORKDIR/wk-overlay-tailnet"
    say "assembling the tailnet overlay ($OVERLAY_ARCH)"
    /opt/wk-tools/image/buildroot/tailnet-overlay.sh "$OVERLAY_ARCH" "$OVERLAY" \
        || fail "could not assemble the tailnet overlay"
fi
# --- the pinned kernel ------------------------------------------------------
# Declared by the profile, not built here (image/configs/<profile>.conf says
# why). One Debian package carries the whole set -- kernel, modules, device
# trees, overlays -- so the four cannot disagree about a version. It is
# unpacked once and read by two consumers below: an overlay that puts the
# modules in the root filesystem, and a post-image script that puts the boot
# files where genimage will pick them up.
KERNEL_STAGE=""
if [ -n "$KERNEL_TAR" ]; then
    [ -f "$KERNEL_TAR" ] || fail "the pinned kernel is not at $KERNEL_TAR.
    It is fetched and prepared on the driving machine and handed over through
    the download cache both sides share (image/buildroot.sh); this build does
    not fetch it itself."
    [ -n "$KERNEL_RELEASE" ] || fail "--kernel-tar needs --kernel-release"
    KERNEL_STAGE="$WORKDIR/wk-kernel"
    rm -rf "$KERNEL_STAGE"; mkdir -p "$KERNEL_STAGE"
    say "unpacking the pinned kernel $KERNEL_RELEASE"
    tar -C "$KERNEL_STAGE" -xf "$KERNEL_TAR" || fail "could not unpack $KERNEL_TAR"
    [ -f "$KERNEL_STAGE/boot/zImage" ] && [ -d "$KERNEL_STAGE/lib/modules/$KERNEL_RELEASE" ] \
        || fail "$KERNEL_TAR does not hold a kernel and modules for $KERNEL_RELEASE"

    # The modules go in as an overlay, the way every other file this repo adds
    # to a root filesystem does; they arrive decompressed with modules.dep
    # already rebuilt (image/buildroot/kernel-pin.sh).
    KERNEL_OVERLAY="$WORKDIR/wk-overlay-kernel"
    rm -rf "$KERNEL_OVERLAY"; mkdir -p "$KERNEL_OVERLAY"
    cp -a "$KERNEL_STAGE/lib" "$KERNEL_OVERLAY/lib"
    say "  modules      $(find "$KERNEL_OVERLAY/lib/modules/$KERNEL_RELEASE" -name '*.ko' | wc -l) in the overlay"
fi

if [ "$OVERLAY_WIFI" = 1 ]; then
    WIFI_OVERLAY="$WORKDIR/wk-overlay-wifi"
    say "assembling the wifi overlay"
    /opt/wk-tools/image/buildroot/wifi-overlay.sh "$WIFI_OVERLAY" \
        || fail "could not assemble the wifi overlay"
    OVERLAY="${OVERLAY:+$OVERLAY }$WIFI_OVERLAY"
fi
[ -z "${KERNEL_OVERLAY:-}" ] || OVERLAY="${OVERLAY:+$OVERLAY }$KERNEL_OVERLAY"
# --- the configuration -----------------------------------------------------
# BR2_EXTERNAL must be passed on every make: buildroot records it in
# output/.br-external.mk, and a later make without it fails on the recorded path.
BR_EXT=""
[ "$EXTERNAL" = 1 ] && BR_EXT="BR2_EXTERNAL=/opt/wk-tools/image/buildroot/external"

say "applying $DEFCONFIG"
# shellcheck disable=SC2086
make $BR_EXT "$DEFCONFIG" || fail "no such defconfig: $DEFCONFIG
    'make list-defconfigs' in $WORKDIR shows what the tree has, and the external
    tree adds this repository's own (image/buildroot/external/configs)."

# The pinned kernel's boot files go in through buildroot's own post-image
# hook rather than by editing output/ behind its back: the hook runs after
# the root filesystem is built and before the board script assembles the
# card, which is the one moment both are true. Written here because this is
# where the staged kernel is.
WK_POST_IMAGE=""; POST_IMAGE_ORIG=""
if [ -n "$KERNEL_STAGE" ]; then
    POST_IMAGE_ORIG=$(sed -n 's/^BR2_ROOTFS_POST_IMAGE_SCRIPT="\(.*\)"$/\1/p' .config | tail -1)
    # The device tree this board's firmware asks for, taken from the
    # configuration that already names it rather than repeated here.
    MACH_DTB_NAME=$(sed -n 's/^BR2_LINUX_KERNEL_INTREE_DTS_NAME="\(.*\)"$/\1/p' .config | tail -1).dtb
    [ "$MACH_DTB_NAME" != ".dtb" ] \
        || fail "$DEFCONFIG names no BR2_LINUX_KERNEL_INTREE_DTS_NAME, so there is no
    device tree name to install the pinned kernel's copy of."
    [ -f "$KERNEL_STAGE/dtb/$MACH_DTB_NAME" ] \
        || fail "the pinned kernel carries no $MACH_DTB_NAME"
    WK_POST_IMAGE="$WORKDIR/wk-kernel-post-image.sh"
    cat > "$WK_POST_IMAGE" <<POSTIMG
#!/bin/sh
# Written by image/buildroot-build.sh. \$1 is BINARIES_DIR: what the board's
# genimage config assembles the boot partition from.
set -e
b="\$1"
cp "$KERNEL_STAGE/boot/zImage" "\$b/zImage"
cp "$KERNEL_STAGE/dtb/$MACH_DTB_NAME" "\$b/"
mkdir -p "\$b/rpi-firmware/overlays"
cp -f "$KERNEL_STAGE/dtb/overlays/"*.dtbo "\$b/rpi-firmware/overlays/"
echo "wk: installed the pinned kernel $KERNEL_RELEASE and its device trees into \$b"
POSTIMG
    chmod +x "$WK_POST_IMAGE"
fi

# Fragments appended then resolved by olddefconfig, so a setting that implies
# others gets them. Written to .config, not make vars, since only .config
# survives buildroot's recursive makes.
{
    echo ""
    echo "# --- added by wk (image/buildroot-build.sh) ---"
    # sources.buildroot.net mirrors nearly every package, keeping the network
    # allowlist short; not PRIMARY_SITE_ONLY since a 2020 tree pins versions
    # the mirror may lack (a DENY in the proxy log names the host to add). The
    # cog defconfigs emit a tarball only, so a configuration naming an .img
    # needs the filesystem added here, sized from the configuration since
    # buildroot needs the number before the rootfs exists.
    case "${IMAGE:-}" in
        *.img)
            echo "BR2_TARGET_ROOTFS_EXT2=y"
            echo "BR2_TARGET_ROOTFS_EXT2_4=y"
            echo "BR2_TARGET_ROOTFS_EXT2_SIZE=\"${ROOTFS_SIZE:-1600M}\""
            ;;
    esac
    # The pinned kernel's boot files, installed by a post-image script that
    # runs *before* the board's own (which is what assembles the card image),
    # so genimage picks these up rather than the ones buildroot built. The
    # board's script keeps its arguments; ours takes none.
    [ -z "$KERNEL_STAGE" ] || echo "BR2_ROOTFS_POST_IMAGE_SCRIPT=\"$WK_POST_IMAGE $POST_IMAGE_ORIG\""
    echo "BR2_PRIMARY_SITE=\"https://sources.buildroot.net\""
    echo "BR2_DL_DIR=\"$BR2_DL_DIR\""
    echo "BR2_CCACHE=y"
    echo "BR2_CCACHE_DIR=\"$BR2_CCACHE_DIR\""
    # The memory-sized job count, in .config where buildroot's ninja packages
    # actually read it (BR2_JLEVEL; its default is nproc+1, five times what
    # the guard budgets at 2 GB/job -- wpewebkit's build was the casualty).
    # An environment BR2_JLEVEL is inert: kconfig symbols come from .config.
    echo "BR2_JLEVEL=$JOBS"
    # Every binary carries a build-id: the identifier a WebKit slot is told
    # apart by in the running process (wk pi bench). The toolchain file
    # cmake packages read it from is regenerated below when this is new.
    echo "BR2_TARGET_LDFLAGS=\"-Wl,--build-id\""
    [ -z "$OVERLAY" ]   || echo "BR2_ROOTFS_OVERLAY=\"$OVERLAY\""
} >> .config
# shellcheck disable=SC2086
make $BR_EXT olddefconfig >/dev/null || fail "the configuration would not resolve after wk's additions"

for v in BR2_DL_DIR BR2_CCACHE_DIR BR2_ROOTFS_OVERLAY; do
    grep -q "^$v=" .config && say "  $(grep "^$v=" .config)"
done

# The linker flag above reaches cmake packages through the exported
# toolchain file, written once at toolchain install; a tree built before the
# flag has one without it, and buildroot's own reinstall rewrites it.
TCF="$WORKDIR/output/host/share/buildroot/toolchainfile.cmake"
if [ -f "$TCF" ] && ! grep -q -- '--build-id' "$TCF"; then
    say "the toolchain file predates BR2_TARGET_LDFLAGS; reinstalling the toolchain to regenerate it"
    # shellcheck disable=SC2086
    make $BR_EXT toolchain-reinstall >/dev/null || fail "toolchain-reinstall failed"
fi

# The image is built from the pinned tarball and nothing else. A slot build
# (image/buildroot-webkit.sh) leaves a local.mk pointing the wpewebkit
# package at the workspace checkout instead; here that override is dropped
# and the package rebuilt from its own source, so the image never ships the
# last slot built (buildroot's own developer workflow, in reverse).
if [ -f "$WORKDIR/local.mk" ]; then
    say "dropping local.mk (a WebKit slot's source override) and rebuilding wpewebkit from the pinned tarball"
    rm -f "$WORKDIR/local.mk"
    # shellcheck disable=SC2086
    make $BR_EXT wpewebkit-dirclean >/dev/null || fail "wpewebkit-dirclean failed"
fi

# --- the build ---------------------------------------------------------
# Under the guard every heavy step in a target runs under (build/guard.sh).
. /opt/wk-tools/build/guard.sh
# FORCE_UNSAFE_CONFIGURE=1: the workspace user is uid 0 in some configs, and
# several 2009-era configure scripts refuse to run as root (the wiki recipe
# carries the same flag). Start time taken here, not at the top: cloning and
# pinning can take minutes and touches nothing under output/, so it must not
# count against the freshness check below.
build_start=$(date +%s)
say "building (this is hours, and the log below is the whole account of it)"
# shellcheck disable=SC2086
# BR2_JLEVEL is not set here: it is a kconfig symbol, so an environment value
# loses to the .config the build reads it from -- it is written there with
# the rest of wk's additions above.
WK_MB_PER_JOB=2048 guard_run "$JOBS" -- env FORCE_UNSAFE_CONFIGURE=1 make $BR_EXT -j"$JOBS" \
    || fail "buildroot failed. The last lines above are the failing package."

# --- what came out -----------------------------------------------------
# Left where buildroot put it; `wk sysimage ls` finds it by looking here.
OUT="$WORKDIR/output/images"
[ -d "$OUT" ] || fail "the build reported success but produced no $OUT"
say "images built at:"
ls -1 "$OUT" | sed 's/^/  -   /'

if [ -n "$IMAGE" ]; then
    verify_image_freshness "$OUT/$IMAGE" "$build_start"
    say "$IMAGE is fresh"
fi
say "stage 'image' done"
