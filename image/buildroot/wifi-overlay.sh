#!/usr/bin/env bash
#
# Assemble the rootfs overlay that lets a buildroot image bring up WiFi from a
# credential seeded onto the card.
#
#   image/buildroot/wifi-overlay.sh <staging-dir>
#
# Simpler than tailnet-overlay.sh, and deliberately so: there is no binary to
# fetch here. wpa_supplicant is a package this board's defconfig selects
# (BR2_PACKAGE_WPA_SUPPLICANT, image/buildroot/external/configs) and is
# compiled by the ordinary build, so this overlay only ever carries the script
# and the init line that spend a credential the *card* provides -- the same
# division tailnet-overlay.sh draws for tailscaled versus wk-tailnet-join.
#
# What it must NOT contain is a credential: the SSID and passphrase arrive
# with the card (disk_seed_wifi, boot/disk.sh), never in the image, for the
# reason tailnet-overlay.sh gives -- an image is stored, copied and kept after
# it is superseded, and a credential inside one is a credential in every copy.
#
# wk-wifi-join itself is not duplicated here: this installs the one copy the
# Yocto layer carries, the same way tailnet-overlay.sh installs
# meta-wk-tailnet's wk-tailnet-join rather than writing a second one.
set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$WK_ROOT/lib/common.sh"

STAGE="${1:-}"
[ -n "$STAGE" ] || die "usage: wifi-overlay.sh <staging-dir>"

info "assembling the wifi overlay in $STAGE"
rm -rf "$STAGE"
# Regular, world-readable files and nothing else -- the same rule
# tailnet-overlay.sh follows, for the same reason: rsync at target-finalize
# runs as the build user, and a mode-0700 directory here fails the build with
# rsync error 23.
mkdir -p "$STAGE/usr/sbin" "$STAGE/etc/init.d"

install -m 0755 \
    "$WK_ROOT/image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join" \
    "$STAGE/usr/sbin/wk-wifi-join"
install -m 0755 "$WK_ROOT/image/buildroot/overlay/etc/init.d/S41wifi" \
    "$STAGE/etc/init.d/S41wifi"

info "overlay ready: $(du -sh "$STAGE" | cut -f1)"
log  "  point the build at it, alongside the tailnet overlay if both are wanted:"
log  "  BR2_ROOTFS_OVERLAY=\"<tailnet-staging> $STAGE\""
