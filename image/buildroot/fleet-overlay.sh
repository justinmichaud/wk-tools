#!/usr/bin/env bash
#
# Assemble the rootfs overlay that makes a buildroot image hand its board
# back: the BusyBox init scripts for what the yocto images do with
# wk-self-disarm.service and wk-self-return.service (stage_units, cmd/sysimage).
#
#   image/buildroot/fleet-overlay.sh <profile> <staging-dir>
#
# Generated here rather than written by `wk sysimage write` because the card
# helper installs systemd units and nothing else (admin/wk-card-priv `units`),
# and a helper on a board's rescue is not upgraded in place. The disarm
# command is the machine's driver's own (b_self_disarm_sh) -- one string, two
# init systems. Both scripts are inert on a rescue: /etc/wk/rescue is the
# marker `wk sysimage write --rescue` leaves, the same gate the units use.
set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/image/profiles.sh"
. "$WK_ROOT/boot/machines.sh"
. "$WK_ROOT/boot/disk.sh"

PROFILE="${1:-}"
STAGE="${2:-}"
[ -n "$PROFILE" ] && [ -n "$STAGE" ] || die "usage: fleet-overlay.sh <profile> <staging-dir>"
image_profile_load "$PROFILE" >/dev/null 2>&1 || die "'$PROFILE' is not an image profile"
[ -n "${IMG_MACHINE:-}" ] || die "$PROFILE names no IMG_MACHINE, so there is no driver to take the disarm from"
machine_load "$IMG_MACHINE" || die "no fleet device '$IMG_MACHINE' (boot/machines/)"
load_driver "$MACH_DRIVER"

info "assembling the fleet overlay in $STAGE"
rm -rf "$STAGE"
# World-readable regular files, for the reason tailnet-overlay.sh gives.
mkdir -p "$STAGE/etc/init.d"

if command -v b_self_disarm_sh >/dev/null 2>&1; then
    cat > "$STAGE/etc/init.d/S00wk-self-disarm" <<EOF
#!/bin/sh
# Park the medium this system booted from, so the next boot is the rescue's.
# From boot/$MACH_DRIVER.sh (b_self_disarm_sh), through image/buildroot/fleet-overlay.sh.
[ "\$1" = start ] || exit 0
[ -f /etc/wk/rescue ] && exit 0
$(b_self_disarm_sh) && echo "wk-self-disarm: the medium is parked; the next boot is the rescue"
exit 0
EOF
    chmod 0755 "$STAGE/etc/init.d/S00wk-self-disarm"
    sh -n "$STAGE/etc/init.d/S00wk-self-disarm" || die "the generated self-disarm does not parse"
    log "  S00wk-self-disarm  from boot/$MACH_DRIVER.sh"
else
    log "  no self-disarm: $IMG_MACHINE's card swap is hands-on ($MACH_DRIVER)"
fi

if [ -n "${IMG_WATCHDOG:-}" ]; then
    cat > "$STAGE/etc/init.d/S01wk-self-return" <<EOF
#!/bin/sh
# Reboot $IMG_WATCHDOG s after boot unless claimed (/run/wk-keep-running: wk boot --keep, wk pi bench).
[ "\$1" = start ] || exit 0
[ -f /etc/wk/rescue ] && exit 0
( sleep $IMG_WATCHDOG; [ -f /run/wk-keep-running ] || reboot ) >/dev/null 2>&1 &
exit 0
EOF
    chmod 0755 "$STAGE/etc/init.d/S01wk-self-return"
    log "  S01wk-self-return  $IMG_WATCHDOG s"
else
    warn "$PROFILE sets no IMG_WATCHDOG, so this image will not hand its machine back"
fi

info "overlay ready: $(du -sh "$STAGE" | cut -f1)"
