#!/bin/bash
#
# Start the loopback bridge if it is not already running, then exec the real
# command. Wrapping rather than running as a separate step keeps `wk run` and
# `wk build` to a single container exec -- a second round trip on every command
# would be paid forever for something that is almost always already true.

PIDFILE=/tmp/.wk-bridge.pid
BRIDGE=/opt/wk-tools/container/proxy/bridge.py

# The PID alone is not proof: PIDs are recycled, and a match on an unrelated
# process leaves the workspace with no egress while looking healthy.
bridge_alive() {
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && grep -qa "bridge.py" "/proc/$pid/cmdline" 2>/dev/null
}

if ! bridge_alive; then
    setsid python3 "$BRIDGE" >/tmp/.wk-bridge.log 2>&1 &
    echo $! > "$PIDFILE"
    # The bridge binds before serving; a short wait avoids a spurious first
    # failure for whatever is about to use it.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        (exec 3<>/dev/tcp/127.0.0.1/${WK_PROXY_PORT:-3128}) 2>/dev/null && break
        sleep 0.1
    done
fi

exec "$@"
