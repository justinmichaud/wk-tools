#!/usr/bin/env bash
#
# The in-workspace half of a buildroot image build. Driven by image/buildroot.sh
# via t_spawn, and run from /opt/wk-tools -- the read-only mount of this repo --
# so the two halves cannot skew.
#
# Everything here is the recipe from the wiki page "Building WPEWebKit for
# 32-bit Raspberry Pi 3 (Buildroot DRM config)", which is the one version of
# this that has produced a booting image, plus what this repository adds: the
# tailnet and wifi overlays, the BR2_EXTERNAL libffi fix, and caches that live
# in the store so a rebuild is cheap.
#
# Nothing here may fail silently: this runs detached and its log is the only
# account of what happened.

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

# The image stage's completion evidence -- a function of its own, the same
# shape as image/yocto-build.sh's verify_image_freshness, for the same reason:
# `make` exits 0 on a tree it decided had nothing left to do (a second run
# against an unchanged .config, or one whose only change never reached the
# genimage step), and a "done" printed from that exit code would be exactly
# the staleness that function exists to catch on the Yocto side -- a build
# that reports success and hands back yesterday's card image. So the named
# image's mtime has to be at least as new as this build's own start, checked
# with a portable `stat` (GNU's -c, BSD/macOS's -f).
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

# BR2_DL_DIR and BR2_CCACHE_DIR arrive as environment variables -- the
# container's store-backed cache mount (targets/container.sh) sets them --
# rather than as flags on this script. The same reasoning image/yocto-build.sh
# gives for DL_DIR/SSTATE_DIR: a host path handed in from image/buildroot.sh
# would name nothing inside this container, and the container's own
# environment is already the one true copy of where the mount landed. Their
# absence means the store mount itself is missing, not that the cache is
# merely unset -- so it fails here rather than quietly building inside the
# workspace, where `wk rm` would throw the downloads away with it.
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

# --- the tree ----------------------------------------------------------------
#
# Pinned to a tag when the configuration names one, which every WPE
# configuration here does: the fork's WPE branches are pinned to 2020.02 and the
# release-pinned cog defconfigs only make sense against it.
#
# Re-used when it is already there and already at the pin. A buildroot tree with
# an output/ directory is tens of gigabytes of work, and the point of it living
# in the workspace is that a second run continues rather than starts again.
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
    # A commit, not a tag. The fork's `2020.02` tag is the buildroot version its
    # WPE branch is based on, and the release-pinned cog defconfigs were added to
    # the branch after it -- so standing the tree at that tag removes the
    # configuration the profile names. Fetched by sha because a shallow clone or
    # an older fetch may not have it yet.
    git -C "$WORKDIR" fetch origin "$TREE_COMMIT" 2>/dev/null || true
    git -C "$WORKDIR" checkout --detach "$TREE_COMMIT" \
        || fail "$TREE_URL has no commit $TREE_COMMIT.
    The configuration pins one deliberately: a branch moves and the 2020.02 tag
    predates the cog defconfigs."
    say "pinned at $(git -C "$WORKDIR" rev-parse --short HEAD)"
fi

cd "$WORKDIR"

# --- the overlay -------------------------------------------------------------
#
# BR2_ROOTFS_OVERLAY is buildroot's one supported way to add files to an image
# without writing a package, and it is copied in at target-finalize -- the very
# end -- so this can be assembled now and costs only the last step if it
# changes. Both scripts are this repository's and carry no credential: the
# tailnet key and the wifi passphrase arrive with the card, not the image.
# BR2_ROOTFS_OVERLAY takes a space-separated list, so both overlays sit side by
# side when both are wanted rather than needing to be merged into one staging
# directory.
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

# --- the configuration -------------------------------------------------------
#
# BR2_EXTERNAL is passed on every make, not just the first: buildroot records it
# in output/.br-external.mk, and a later make without it fails on the recorded
# path rather than quietly dropping the tree.
BR_EXT=""
[ "$EXTERNAL" = 1 ] && BR_EXT="BR2_EXTERNAL=/opt/wk-tools/image/buildroot/external"

say "applying $DEFCONFIG"
# shellcheck disable=SC2086
make $BR_EXT "$DEFCONFIG" || fail "no such defconfig: $DEFCONFIG
    'make list-defconfigs' in $WORKDIR shows what the tree has, and the external
    tree adds this repository's own (image/buildroot/external/configs)."

# The fragments this repository adds on top of the defconfig, appended and then
# resolved by olddefconfig so that a setting which implies others gets them.
#
# Written to the .config rather than passed as make variables because that is
# what survives the recursive makes buildroot does internally.
{
    echo ""
    echo "# --- added by wk (image/buildroot-build.sh) ---"
    # One host for almost every fetch, the same trick the yocto lane uses with
    # the OpenEmbedded mirror: buildroot mirrors every package it knows about at
    # sources.buildroot.net, so the build stops depending on a hundred upstreams
    # still being up and serving the same bytes -- and, because a workspace
    # reaches the network only through a hostname allowlist, the set of names
    # that has to be allowed collapses to something a person can read.
    #
    # Not PRIMARY_SITE_ONLY: a 2020 tree pins versions the mirror does not
    # always carry, and forbidding the upstream fallback outright would fail
    # those. Fallbacks that hit the egress allowlist show up in the proxy log
    # as DENY lines, naming the host to add.
    # The filesystem genimage assembles the card out of.
    #
    # The release-pinned cog defconfigs emit a *tarball only* -- no rootfs.ext4 --
    # so the board's own genimage config has nothing to assemble and the build
    # ends with a kernel, some dtbs and a tar. That is not a failure and it is
    # not a card either: the wiki recipe stops there and copies files by hand.
    # A configuration that names an .img wants the image, so the filesystem it
    # is made from is added here.
    #
    # Sized from the configuration rather than from the content: buildroot needs
    # the number before the rootfs exists, so it cannot be derived, and one too
    # small fails at image time after the whole build.
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

# --- the build ---------------------------------------------------------------
#
# FORCE_UNSAFE_CONFIGURE=1 because the workspace user is uid 0 in some
# configurations and several 2009-era configure scripts refuse to run as root;
# the wiki recipe carries the same flag for the same reason.
#
# The start time is taken right here, not at the top of the script: cloning
# and pinning the tree can legitimately take minutes and touches nothing under
# output/, so it must not count against the freshness check below -- only the
# build that is about to run should have to have produced the evidence.
build_start=$(date +%s)
say "building (this is hours, and the log below is the whole account of it)"
# shellcheck disable=SC2086
FORCE_UNSAFE_CONFIGURE=1 make $BR_EXT -j"$JOBS" \
    || fail "buildroot failed. The last lines above are the failing package."

# --- what came out -----------------------------------------------------------
#
# Left where buildroot put it. There is no store to import it into (wk help
# images): the workspace that built an image is its name, and `wk sysimage ls`
# finds it by looking here.
OUT="$WORKDIR/output/images"
[ -d "$OUT" ] || fail "the build reported success but produced no $OUT"
say "images built at:"
ls -1 "$OUT" | sed 's/^/  -   /'

# See verify_image_freshness above: only now, with the artifact checked
# against this run's own start time, may the stage call itself done.
if [ -n "$IMAGE" ]; then
    verify_image_freshness "$OUT/$IMAGE" "$build_start"
    say "$IMAGE is fresh"
fi
say "stage 'image' done"
