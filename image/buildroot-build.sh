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

SRC=/src/WebKit
TREE_URL=""; TREE_BRANCH=""; TREE_COMMIT=""; DEFCONFIG=""; EXTERNAL=0
IMAGE=""; JOBS=""; OVERLAY_ARCH=""; OVERLAY_WIFI=0; NAME=""; ROOTFS_SIZE=""

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
if [ "$OVERLAY_WIFI" = 1 ]; then
    WIFI_OVERLAY="$WORKDIR/wk-overlay-wifi"
    say "assembling the wifi overlay"
    /opt/wk-tools/image/buildroot/wifi-overlay.sh "$WIFI_OVERLAY" \
        || fail "could not assemble the wifi overlay"
    OVERLAY="${OVERLAY:+$OVERLAY }$WIFI_OVERLAY"
fi

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
    echo "BR2_PRIMARY_SITE=\"https://sources.buildroot.net\""
    echo "BR2_DL_DIR=\"$BR2_DL_DIR\""
    echo "BR2_CCACHE=y"
    echo "BR2_CCACHE_DIR=\"$BR2_CCACHE_DIR\""
    [ -z "$OVERLAY" ]   || echo "BR2_ROOTFS_OVERLAY=\"$OVERLAY\""
} >> .config
# shellcheck disable=SC2086
make $BR_EXT olddefconfig >/dev/null || fail "the configuration would not resolve after wk's additions"

for v in BR2_DL_DIR BR2_CCACHE_DIR BR2_ROOTFS_OVERLAY; do
    grep -q "^$v=" .config && say "  $(grep "^$v=" .config)"
done

# --- the build ---------------------------------------------------------
# FORCE_UNSAFE_CONFIGURE=1: the workspace user is uid 0 in some configs, and
# several 2009-era configure scripts refuse to run as root (the wiki recipe
# carries the same flag). Start time taken here, not at the top: cloning and
# pinning can take minutes and touches nothing under output/, so it must not
# count against the freshness check below.
build_start=$(date +%s)
say "building (this is hours, and the log below is the whole account of it)"
# shellcheck disable=SC2086
FORCE_UNSAFE_CONFIGURE=1 make $BR_EXT -j"$JOBS" \
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
