#!/usr/bin/env bash
#
# Assemble the rootfs overlay that puts a buildroot image on the tailnet.
#
#   image/buildroot/tailnet-overlay.sh <tailscale-arch> <staging-dir>
#
# BR2_ROOTFS_OVERLAY is buildroot's one supported way to add files without
# writing a package, and it is copied in at `target-finalize`, so this can be
# added to a tree most of the way through a build. A package would mean a patch
# against the WebPlatformForEmbedded/buildroot fork's vendor branch, rebased
# every time it moves.
#
# It must contain no credential: the auth key and fleet name arrive with the
# *card* (disk_seed_tailnet, boot/disk.sh), one boot before wk-tailnet-join
# spends and deletes the key.
#
# Version and sha256 come from
# image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc,
# the one file that declares them for all three installers.
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

# sha256, spelled for whichever machine this runs on: macOS has `shasum -a 256`
# and a Linux build container has `sha256sum`. A missing checker must not read
# as a checksum that passed.
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
# Regular, world-readable files and nothing else: an overlay is copied into the
# target tree by rsync running as the build user, so a mode-0700 directory here
# dies at `target-finalize` with rsync error 23. Restrictive permissions are
# applied at boot by S99tailscale instead.
mkdir -p "$STAGE/usr/bin" "$STAGE/usr/sbin" "$STAGE/etc/init.d"

tar xzf "$TGZ" -C "$STAGE/usr/bin"  --strip-components=1 "tailscale_${VER}_${TS_ARCH}/tailscale"
tar xzf "$TGZ" -C "$STAGE/usr/sbin" --strip-components=1 "tailscale_${VER}_${TS_ARCH}/tailscaled"
chmod 0755 "$STAGE/usr/bin/tailscale" "$STAGE/usr/sbin/tailscaled"

# The same join script the Yocto images run, from the same file. It reads
# /etc/wk/tailnet.conf and /etc/wk/tailscale-authkey, which arrive with the card.
install -m 0755 \
    "$WK_ROOT/image/yocto/meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join" \
    "$STAGE/usr/sbin/wk-tailnet-join"
install -m 0755 "$WK_ROOT/image/buildroot/overlay/etc/init.d/S99tailscale" \
    "$STAGE/etc/init.d/S99tailscale"

info "overlay ready: $(du -sh "$STAGE" | cut -f1), tailscale $VER ($TS_ARCH)"
log  "  point the build at it:  BR2_ROOTFS_OVERLAY=\"$STAGE\""
