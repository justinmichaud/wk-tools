#!/bin/bash
#
# Start the loopback bridge if it is not already running, then exec the real
# command. Wrapping rather than running as a separate step keeps `wk run` and
# `wk build` to a single container exec -- a second round trip on every command
# would be paid forever for something that is almost always already true.

PIDFILE=/tmp/.wk-bridge.pid
BRIDGE=/opt/wk-tools/container/proxy/bridge.py

if ! { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }; then
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
