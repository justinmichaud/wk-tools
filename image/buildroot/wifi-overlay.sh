#!/usr/bin/env bash
#
# Assemble the rootfs overlay that lets a buildroot image bring up WiFi from a
# credential seeded onto the card.
#
#   image/buildroot/wifi-overlay.sh <staging-dir>
#
# No binary to fetch: wpa_supplicant is a package this board's defconfig
# selects (BR2_PACKAGE_WPA_SUPPLICANT, image/buildroot/external/configs), so
# the overlay carries only the script and init line.
#
# It must contain no credential: the SSID and passphrase arrive with the card
# (disk_seed_wifi, boot/disk.sh). wk-wifi-join is installed from the Yocto
# layer's one copy rather than duplicated here.
set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$WK_ROOT/lib/common.sh"

STAGE="${1:-}"
[ -n "$STAGE" ] || die "usage: wifi-overlay.sh <staging-dir>"

info "assembling the wifi overlay in $STAGE"
rm -rf "$STAGE"
# Regular, world-readable files and nothing else: rsync at target-finalize runs
# as the build user, and a mode-0700 directory here fails the build with rsync
# error 23.
mkdir -p "$STAGE/usr/sbin" "$STAGE/etc/init.d"

install -m 0755 \
    "$WK_ROOT/image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join" \
    "$STAGE/usr/sbin/wk-wifi-join"
install -m 0755 "$WK_ROOT/image/buildroot/overlay/etc/init.d/S41wifi" \
    "$STAGE/etc/init.d/S41wifi"

info "overlay ready: $(du -sh "$STAGE" | cut -f1)"
log  "  point the build at it, alongside the tailnet overlay if both are wanted:"
log  "  BR2_ROOTFS_OVERLAY=\"<tailnet-staging> $STAGE\""
