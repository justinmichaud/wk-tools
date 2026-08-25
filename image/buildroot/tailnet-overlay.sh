#!/usr/bin/env bash
#
# Assemble the rootfs overlay that puts a buildroot image on the tailnet.
#
#   image/buildroot/tailnet-overlay.sh <tailscale-arch> <staging-dir>
#
# BR2_ROOTFS_OVERLAY is buildroot's one supported way to add files to an image
# without writing a package, and it is copied in at `target-finalize` -- the very
# end -- so this can be added to a tree that is already most of the way through a
# build and cost only the last step again.
#
# Why an overlay and not a buildroot package: the tree is
# WebPlatformForEmbedded/buildroot at 2020.02, which is a *fork* pinned to a WPE
# release. Adding a package to it means carrying a patch against somebody else's
# vendor branch and rebasing it every time that branch moves; an overlay is a
# directory this repository owns outright, and it works identically against any
# of that fork's defconfigs.
#
# What it must NOT contain is a credential. The staging directory holds the
# binaries and the two scripts, and nothing else -- the auth key and the fleet
# name arrive with the *card* (disk_seed_tailnet, boot/disk.sh), one boot before
# wk-tailnet-join spends and deletes the key. An image is stored, compressed,
# copied between machines and kept after it is superseded; a key inside one is a
# key in every copy of it.
#
# The binaries are the same pinned bytes the Yocto layer installs and `wk pi
# setup` pushes -- version and sha256 from
# image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc,
# which is the one file that declares them. Three installers, one pin.
set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$WK_ROOT/lib/common.sh"

TS_ARCH="${1:-}"
STAGE="${2:-}"
[ -n "$TS_ARCH" ] && [ -n "$STAGE" ] \
    || die "usage: tailnet-overlay.sh <tailscale-arch: arm|arm64> <staging-dir>"

REL="$WK_ROOT/image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc"
rel_field() { sed -n "s/^$1 = \"\(.*\)\"\$/\1/p" "$REL" | head -1; }
VER=$(rel_field TS_VERSION)
SHA=$(rel_field "TS_SHA256_$TS_ARCH")
[ -n "$VER" ] || die "no TS_VERSION in $REL"
[ -n "$SHA" ] || die "$REL declares no TS_SHA256_$TS_ARCH"

TGZ="$STAGE/../tailscale_${VER}_${TS_ARCH}.tgz"
mkdir -p "$STAGE" "$(dirname "$TGZ")"

# Fetched once and kept beside the staging directory: this runs again on every
# rebuild, and re-downloading 30 MB to produce bytes that are pinned by checksum
# anyway is the kind of thing that makes a rebuild feel expensive.
# sha256, spelled for whichever machine this runs on: `shasum -a 256` is what
# macOS has and `sha256sum` is what a Linux build container has. This script runs
# on both -- the overlay is assembled wherever the buildroot tree lives -- and a
# missing checker must not read as a checksum that passed.
_sha256_check() { # <expected> <file>
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$1" "$2" | sha256sum -c - >/dev/null 2>&1
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s  %s\n' "$1" "$2" | shasum -a 256 -c - >/dev/null 2>&1
    else
        die "no sha256sum and no shasum here, so the tailscale release cannot be
    verified. Refusing to put unverified bytes in an image."
    fi
}

if [ ! -f "$TGZ" ] || ! _sha256_check "$SHA" "$TGZ"; then
    info "fetching tailscale $VER ($TS_ARCH)"
    curl -fsSL -o "$TGZ.part" \
        "https://pkgs.tailscale.com/stable/tailscale_${VER}_${TS_ARCH}.tgz" \
        || die "could not fetch the tailscale release"
    _sha256_check "$SHA" "$TGZ.part" \
        || { rm -f "$TGZ.part"; die "tailscale_${VER}_${TS_ARCH}.tgz does not match the pinned sha256.
    Refusing to put unverified bytes in an image -- $REL is what says which bytes are right."; }
    mv "$TGZ.part" "$TGZ"
fi

info "assembling the tailnet overlay in $STAGE"
rm -rf "$STAGE"
# Regular, world-readable files and nothing else. An overlay is copied into the
# target tree by rsync running as *the build user*, which is not the user that
# assembled it -- so a mode-0700 directory in here is a build that dies at
# `target-finalize` with rsync error 23. Anything that needs restrictive
# permissions is
# created at boot by S99tailscale, where the mode is the running system's
# business and no build user has to be able to read it.
mkdir -p "$STAGE/usr/bin" "$STAGE/usr/sbin" "$STAGE/etc/init.d"

tar xzf "$TGZ" -C "$STAGE/usr/bin"  --strip-components=1 "tailscale_${VER}_${TS_ARCH}/tailscale"
tar xzf "$TGZ" -C "$STAGE/usr/sbin" --strip-components=1 "tailscale_${VER}_${TS_ARCH}/tailscaled"
chmod 0755 "$STAGE/usr/bin/tailscale" "$STAGE/usr/sbin/tailscaled"

# The same join script the Yocto images run, from the same file. It is POSIX sh
# and reads /etc/wk/tailnet.conf and /etc/wk/tailscale-authkey, neither of which
# is in the image -- both arrive with the card.
install -m 0755 \
    "$WK_ROOT/image/yocto/meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join" \
    "$STAGE/usr/sbin/wk-tailnet-join"
install -m 0755 "$WK_ROOT/image/buildroot/overlay/etc/init.d/S99tailscale" \
    "$STAGE/etc/init.d/S99tailscale"

info "overlay ready: $(du -sh "$STAGE" | cut -f1), tailscale $VER ($TS_ARCH)"
log  "  point the build at it:  BR2_ROOTFS_OVERLAY=\"$STAGE\""
