#!/usr/bin/env bash
set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$WK_ROOT/lib/common.sh"

STAGE="${1:-}"
[ -n "$STAGE" ] || die "usage: wifi-overlay.sh <staging-dir>"

info "assembling the wifi overlay in $STAGE"
rm -rf "$STAGE"
# World-readable only: rsync at target-finalize runs as the build user, and a mode-0700 directory fails it with error 23.
mkdir -p "$STAGE/usr/sbin" "$STAGE/etc/init.d"

install -m 0755 \
    "$WK_ROOT/image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join" \
    "$STAGE/usr/sbin/wk-wifi-join"
install -m 0755 "$WK_ROOT/image/buildroot/overlay/etc/init.d/S41wifi" \
    "$STAGE/etc/init.d/S41wifi"

info "overlay ready: $(du -sh "$STAGE" | cut -f1)"
log  "  point the build at it, alongside the tailnet overlay if both are wanted:"
log  "  BR2_ROOTFS_OVERLAY=\"<tailnet-staging> $STAGE\""
