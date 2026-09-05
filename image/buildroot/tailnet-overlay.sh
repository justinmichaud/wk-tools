#!/usr/bin/env bash
# Assembles a BR2_ROOTFS_OVERLAY carrying no credential: the key comes with the card.
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
# rsync copies an overlay in as the build user: a mode-0700 dir dies with rsync error 23.
mkdir -p "$STAGE/usr/bin" "$STAGE/usr/sbin" "$STAGE/etc/init.d"

tar xzf "$TGZ" -C "$STAGE/usr/bin"  --strip-components=1 "tailscale_${VER}_${TS_ARCH}/tailscale"
tar xzf "$TGZ" -C "$STAGE/usr/sbin" --strip-components=1 "tailscale_${VER}_${TS_ARCH}/tailscaled"
chmod 0755 "$STAGE/usr/bin/tailscale" "$STAGE/usr/sbin/tailscaled"

install -m 0755 \
    "$WK_ROOT/image/yocto/meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join" \
    "$STAGE/usr/sbin/wk-tailnet-join"
install -m 0755 "$WK_ROOT/image/buildroot/overlay/etc/init.d/S99tailscale" \
    "$STAGE/etc/init.d/S99tailscale"

info "overlay ready: $(du -sh "$STAGE" | cut -f1), tailscale $VER ($TS_ARCH)"
log  "  point the build at it:  BR2_ROOTFS_OVERLAY=\"$STAGE\""
