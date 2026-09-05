#!/usr/bin/env bash
#
# The in-workspace half of a buildroot image build, spawned by image/buildroot.sh.
# Follows the wiki recipe "Building WPEWebKit for 32-bit Raspberry Pi 3 (Buildroot DRM config)", the only one known to boot.

set -euo pipefail

export WK_BUILD=1  # the build wall (container/bin/wk-build-wall) passes ninja/cmake/make for a wk build

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

# make exits 0 on a tree with nothing left to do, so the image must be newer.
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

if [ -d "$WORKDIR/.git" ]; then  # an output/ tree is tens of gigabytes: a second run at the pin continues rather than re-cloning
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
    git -C "$WORKDIR" fetch origin "$TREE_COMMIT" 2>/dev/null || true
    git -C "$WORKDIR" checkout --detach "$TREE_COMMIT" \
        || fail "$TREE_URL has no commit $TREE_COMMIT.
    The configuration pins one deliberately: a branch moves and the 2020.02 tag
    predates the cog defconfigs."
    say "pinned at $(git -C "$WORKDIR" rev-parse --short HEAD)"
fi

for p in /opt/wk-tools/image/buildroot/tree-patches/*.patch; do  # the fork pins versions it never built: wpebackend-fdo 1.14 is meson, its package calls cmake
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

# BR2_ROOTFS_OVERLAY copies files in at target-finalize, space-separated; no overlay carries a credential -- the key and passphrase arrive with the card.
OVERLAY=""
if [ -n "$OVERLAY_ARCH" ]; then
    OVERLAY="$WORKDIR/wk-overlay-tailnet"
    say "assembling the tailnet overlay ($OVERLAY_ARCH)"
    /opt/wk-tools/image/buildroot/tailnet-overlay.sh "$OVERLAY_ARCH" "$OVERLAY" \
        || fail "could not assemble the tailnet overlay"
fi
KERNEL_STAGE=""  # one Debian package holds kernel, modules, device trees and overlays, so the four cannot disagree
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

    KERNEL_OVERLAY="$WORKDIR/wk-overlay-kernel"  # modules arrive decompressed, modules.dep rebuilt (image/buildroot/kernel-pin.sh)
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
# BR2_EXTERNAL goes on every make: buildroot records it in output/.br-external.mk, and a later make without it fails on the recorded path.
BR_EXT=""
[ "$EXTERNAL" = 1 ] && BR_EXT="BR2_EXTERNAL=/opt/wk-tools/image/buildroot/external"

say "applying $DEFCONFIG"
# shellcheck disable=SC2086
make $BR_EXT "$DEFCONFIG" || fail "no such defconfig: $DEFCONFIG
    'make list-defconfigs' in $WORKDIR shows what the tree has, and the external
    tree adds this repository's own (image/buildroot/external/configs)."

WK_POST_IMAGE=""; POST_IMAGE_ORIG=""
if [ -n "$KERNEL_STAGE" ]; then
    POST_IMAGE_ORIG=$(sed -n 's/^BR2_ROOTFS_POST_IMAGE_SCRIPT="\(.*\)"$/\1/p' .config | tail -1)
    NODE_DTB_NAME=$(sed -n 's/^BR2_LINUX_KERNEL_INTREE_DTS_NAME="\(.*\)"$/\1/p' .config | tail -1).dtb
    [ "$NODE_DTB_NAME" != ".dtb" ] \
        || fail "$DEFCONFIG names no BR2_LINUX_KERNEL_INTREE_DTS_NAME, so there is no
    device tree name to install the pinned kernel's copy of."
    [ -f "$KERNEL_STAGE/dtb/$NODE_DTB_NAME" ] \
        || fail "the pinned kernel carries no $NODE_DTB_NAME"
    WK_POST_IMAGE="$WORKDIR/wk-kernel-post-image.sh"
    cat > "$WK_POST_IMAGE" <<POSTIMG
#!/bin/sh
# Written by image/buildroot-build.sh. \$1 is BINARIES_DIR: what the board's
# genimage config assembles the boot partition from.
set -e
b="\$1"
cp "$KERNEL_STAGE/boot/zImage" "\$b/zImage"
cp "$KERNEL_STAGE/dtb/$NODE_DTB_NAME" "\$b/"
mkdir -p "\$b/rpi-firmware/overlays"
cp -f "$KERNEL_STAGE/dtb/overlays/"*.dtbo "\$b/rpi-firmware/overlays/"
echo "wk: installed the pinned kernel $KERNEL_RELEASE and its device trees into \$b"
POSTIMG
    chmod +x "$WK_POST_IMAGE"
fi

# Appended then resolved by olddefconfig; in .config because only .config survives buildroot's recursive makes.
{
    echo ""
    echo "# --- added by wk (image/buildroot-build.sh) ---"
    case "${IMAGE:-}" in  # cog defconfigs emit a tarball only, and buildroot wants the size before the rootfs exists
        *.img)
            echo "BR2_TARGET_ROOTFS_EXT2=y"
            echo "BR2_TARGET_ROOTFS_EXT2_4=y"
            echo "BR2_TARGET_ROOTFS_EXT2_SIZE=\"${ROOTFS_SIZE:-1600M}\""
            ;;
    esac
    [ -z "$KERNEL_STAGE" ] || echo "BR2_ROOTFS_POST_IMAGE_SCRIPT=\"$WK_POST_IMAGE $POST_IMAGE_ORIG\""
    echo "BR2_PRIMARY_SITE=\"https://sources.buildroot.net\""
    echo "BR2_DL_DIR=\"$BR2_DL_DIR\""
    echo "BR2_CCACHE=y"
    echo "BR2_CCACHE_DIR=\"$BR2_CCACHE_DIR\""
    # BR2_JLEVEL is a kconfig symbol, so an environment value is inert; its default, nproc+1, is five times the 2 GB/job budget.
    echo "BR2_JLEVEL=$JOBS"
    echo "BR2_TARGET_LDFLAGS=\"-Wl,--build-id\""  # a build-id is how a WebKit slot is told apart in the running process (wk pi bench)
    [ -z "$OVERLAY" ]   || echo "BR2_ROOTFS_OVERLAY=\"$OVERLAY\""
} >> .config
# shellcheck disable=SC2086
make $BR_EXT olddefconfig >/dev/null || fail "the configuration would not resolve after wk's additions"

for v in BR2_DL_DIR BR2_CCACHE_DIR BR2_ROOTFS_OVERLAY; do
    grep -q "^$v=" .config && say "  $(grep "^$v=" .config)"
done

TCF="$WORKDIR/output/host/share/buildroot/toolchainfile.cmake"
if [ -f "$TCF" ] && ! grep -q -- '--build-id' "$TCF"; then
    say "the toolchain file predates BR2_TARGET_LDFLAGS; reinstalling the toolchain to regenerate it"
    # shellcheck disable=SC2086
    make $BR_EXT toolchain-reinstall >/dev/null || fail "toolchain-reinstall failed"
fi

if [ -f "$WORKDIR/local.mk" ]; then  # a slot build (image/buildroot-webkit.sh) points wpewebkit at the workspace checkout; an image is the pinned tarball and nothing else
    say "dropping local.mk (a WebKit slot's source override) and rebuilding wpewebkit from the pinned tarball"
    rm -f "$WORKDIR/local.mk"
    # shellcheck disable=SC2086
    make $BR_EXT wpewebkit-dirclean >/dev/null || fail "wpewebkit-dirclean failed"
fi

. /opt/wk-tools/build/guard.sh
# FORCE_UNSAFE_CONFIGURE=1: 2009-era configure scripts refuse to run as root.
build_start=$(date +%s)  # after the clone, which touches nothing under output/
say "building (this is hours, and the log below is the whole account of it)"
# shellcheck disable=SC2086
WK_MB_PER_JOB=2048 guard_run "$JOBS" -- env FORCE_UNSAFE_CONFIGURE=1 make $BR_EXT -j"$JOBS" \
    || fail "buildroot failed. The last lines above are the failing package."

OUT="$WORKDIR/output/images"  # left where buildroot put it; `wk sysimage ls` looks here
[ -d "$OUT" ] || fail "the build reported success but produced no $OUT"
say "images built at:"
ls -1 "$OUT" | sed 's/^/  -   /'

if [ -n "$IMAGE" ]; then
    verify_image_freshness "$OUT/$IMAGE" "$build_start"
    say "$IMAGE is fresh"
fi
say "stage 'image' done"
