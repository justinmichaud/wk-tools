#!/usr/bin/env bash
# On the benchmark install, as root: join the tailnet. Shell, since the first boot joins before it has a python3.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"

TS_BIN=/usr/local/bin
TS_SOCK=/var/run/tailscaled.socket
TS_KEY=/etc/wk/tailscale-authkey
TS_CONF=/etc/wk/tailnet.conf
DAEMON_LABEL=com.wk.tailscaled
LAUNCHD=/Library/LaunchDaemons

cmd_join() {
    [ "$(id -u)" = 0 ] || die "tailscaled needs root to open a utun, so this needs root too"
    [ -x "$TS_BIN/tailscaled" ] || die "no $TS_BIN/tailscaled on this install: it was never staged.
    Remedy, from the host install:  wk boot mbp --repair"

    launchctl print "system/$DAEMON_LABEL" >/dev/null 2>&1 \
        || launchctl bootstrap system "$LAUNCHD/$DAEMON_LABEL.plist" \
        || die "launchd refused $LAUNCHD/$DAEMON_LABEL.plist, so there is no daemon to join with"

    local i=0
    while [ "$i" -lt 30 ] && [ ! -S "$TS_SOCK" ]; do i=$((i + 1)); sleep 1; done
    [ -S "$TS_SOCK" ] || die "tailscaled did not open $TS_SOCK within 30s, so there is nothing
    to join with. Its output is in /var/log/wk-tailscaled.log."

    local ip
    if ip=$("$TS_BIN/tailscale" ip -4 2>/dev/null | head -1) && [ -n "$ip" ]; then
        info "already on the tailnet as $ip"
        rm -f "$TS_KEY"
        return 0
    fi

    [ -r "$TS_KEY" ] || die "no auth key at $TS_KEY and no tailnet identity in the state file,
    so this install cannot join and cannot be watched while it measures.
    Remedy, from the host install:  wk boot mbp --repair"

    local name tag
    name=$(sed -n 's/^hostname=//p' "$TS_CONF" | head -1)
    tag=$(sed -n 's/^tag=//p' "$TS_CONF" | head -1)
    [ -n "$name" ] && [ -n "$tag" ] \
        || die "$TS_CONF names no hostname and tag, so this install does not know
    which node it is. Remedy, from the host install:  wk boot mbp --repair"

    info "joining the tailnet as $name ($tag)"
    # --timeout, because without one `up` waits for the backend to reach Running for as long as that takes, and the caller is an unattended boot with a benchmark to run: an install whose Wi-Fi did not come up would sit here rather than measure. Failing is fine -- the key is kept and the run goes on unobserved.
    "$TS_BIN/tailscale" up --timeout=90s --auth-key "file:$TS_KEY" --advertise-tags="$tag" \
        --hostname="$name" --accept-dns=false \
        || die "tailscale up did not reach Running inside 90s. The key is kept so the
    next boot retries; a key that is single-use, untagged or expired fails exactly
    here, and so does an install with no route out."
    ip=$("$TS_BIN/tailscale" ip -4 2>/dev/null | head -1) || ip=""
    [ -n "$ip" ] || die "tailscale up returned success and this node still has no address.
    The key is kept so the next boot retries."
    info "up as $name at $ip"
    rm -f "$TS_KEY"
}

case "${1:-}" in
    join)    cmd_join ;;
    *)       die "usage: mac-tailnet.sh join" ;;
esac
