#!/bin/bash
#
# Start the loopback bridge if it is not already running, then exec the real
# command. Wrapping rather than running as a separate step keeps `wk run` and
# `wk build` to a single container exec -- a second round trip on every command
# would be paid forever for something that is almost always already true.

set -euo pipefail

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
    # WK_PROXY_PORT overrides the port to probe; bridge.py (started above)
    # reads the same variable, with the same default, to pick what it binds.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        (exec 3<>/dev/tcp/127.0.0.1/${WK_PROXY_PORT:-3128}) 2>/dev/null && break
        sleep 0.1
    done
fi

# --- talking to GitHub's API without holding the credential that does it ------
# api.github.com is the one host whose TLS is terminated outside this workspace
# (INJECTED_HOSTS, container/proxy/wk-proxy.py): the injector replaces the
# Authorization header with the real token, which stays on the machine. So the
# two things a workspace needs are a certificate to trust and a placeholder to
# send, and neither is a secret.
#
# Here rather than in the container's environment for the same reason PATH is:
# this file is the one wrapper every exec path goes through and it is read from
# /opt/wk-tools, so every workspace that already exists gets it without being
# recreated.
#
# The bundle is the system's *plus* that CA and never that CA alone: these
# variables replace the trust store outright, so a bundle holding one
# certificate would fail every other HTTPS request in the workspace.
WK_CA_SRC=/run/wk/wk-github-ca.pem
WK_CA_BUNDLE=/tmp/.wk-ca-bundle.pem
if [ -r "$WK_CA_SRC" ]; then
    if [ ! -s "$WK_CA_BUNDLE" ] || [ "$WK_CA_SRC" -nt "$WK_CA_BUNDLE" ]; then
        cat /etc/ssl/certs/ca-certificates.crt "$WK_CA_SRC" > "$WK_CA_BUNDLE.$$" \
            && mv "$WK_CA_BUNDLE.$$" "$WK_CA_BUNDLE"
    fi
    export REQUESTS_CA_BUNDLE="$WK_CA_BUNDLE"
    export CURL_CA_BUNDLE="$WK_CA_BUNDLE"
    export GIT_SSL_CAINFO="$WK_CA_BUNDLE"
fi

# What `git-webkit pr` authenticates with (webkitcorepy reads these two; the
# keyring is unusable in a container). The token is a placeholder on purpose:
# an agent in here can open a pull request while `wk push` is on and can never
# read, copy or reuse the credential that did it. The account name is public,
# and comes from the same file the fork aliases do.
if [ -r /secrets/github-user ]; then
    GITHUB_COM_USERNAME=$(cat /secrets/github-user)
    export GITHUB_COM_USERNAME
    export GITHUB_COM_TOKEN=wk-injects-this
fi

# The workspace's own tools, on PATH, for every command that comes through here.
#
# A login shell gets this from ~/.bashrc, and *nothing that is not a login
# shell does* -- so `wk enter <ws> claude` answered "not found" about a CLI
# that was installed and working two directories away. Here rather than in the
# container's environment because this file is the one wrapper every exec path
# already goes through, and it is read from /opt/wk-tools, so every workspace
# that already exists gets it without being recreated.
WK_TOOLS_DIR=/opt/wk-tools
. "$WK_TOOLS_DIR/shell/path.sh"

exec "$@"
