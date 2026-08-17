#!/usr/bin/env bash
#
# Ensure a graphical session exists on the attached monitor, so workspaces can
# get real GPU acceleration for benchmarks.
#
# Benchmarks that matter -- Speedometer, MotionMark, anything touching WebGL --
# are meaningless under llvmpipe, so this has to be a real session on real
# hardware, not Xvfb. Two cases:
#
#   Someone is logged in at the machine. Nothing to do; `wk enter` passes the
#   existing Wayland socket and /dev/dri through.
#
#   Nobody is logged in. Start a minimal compositor (cage) on the seat so there
#   is a real DRM session to render into. This is what makes an unattended
#   benchmark run possible.
#
# Display integration and network isolation are independent: a workspace keeps
# the GPU and the compositor socket while still being unable to reach the LAN.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$WK_ROOT/lib/common.sh"

is_linux || die "session.sh is Linux-only"

if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    info "wayland session already present ($WAYLAND_DISPLAY)"
    exit 0
fi

if [ -n "${DISPLAY:-}" ]; then
    info "X11 session already present ($DISPLAY)"
    exit 0
fi

# Is a graphical session running on the seat under someone else's login?
if have loginctl && loginctl list-sessions --no-legend 2>/dev/null | grep -q seat0; then
    sid=$(loginctl list-sessions --no-legend | awk '/seat0/ {print $1; exit}')
    typ=$(loginctl show-session "$sid" -p Type --value 2>/dev/null || echo)
    if [ "$typ" = wayland ] || [ "$typ" = x11 ]; then
        info "graphical session $sid ($typ) is active on seat0"
        log  "run 'wk enter' from inside that session so the compositor socket is inherited"
        exit 0
    fi
fi

have cage || die "cage is not installed (apt install cage) and no graphical session is running"

[ -e /dev/dri/renderD128 ] || die "no DRM render node at /dev/dri/renderD128; GPU acceleration is unavailable"

info "starting a headless-but-real compositor on seat0"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
ensure_dir "$XDG_RUNTIME_DIR" 0700

# cage runs one client fullscreen on the real DRM device, so rendering goes
# through the GPU exactly as it would in a desktop session.
cage -d -- "${@:-sleep infinity}" &
sleep 2

if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
    info "compositor up; WAYLAND_DISPLAY=wayland-0"
else
    die "compositor did not start; check that this user is on seat0 and has DRM access"
fi
