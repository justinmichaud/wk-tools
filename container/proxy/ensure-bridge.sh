#!/bin/bash
#
# Start the loopback bridge if it is not already running, then exec the real
# command, so `wk run` and `wk build` stay one container exec.

set -euo pipefail

# Errexit, because a bridge that half-started must not look like one that
# started. The two steps that write into the temp directory `warn` instead: a
# full or read-only /tmp would abort every exec in the workspace over a pidfile.
warn() { printf 'wk: %s\n' "$*" >&2; }

# ${TMPDIR:-/tmp}: the same path on every exec, and a test can point it at a
# directory it can make unwritable.
WK_TMP="${TMPDIR:-/tmp}"
PIDFILE="$WK_TMP/.wk-bridge.pid"
BRIDGE=/opt/wk-tools/container/proxy/bridge.py

# PIDs are recycled, and a match on an unrelated process leaves the workspace
# with no egress while looking healthy.
bridge_alive() {
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && grep -qa "bridge.py" "/proc/$pid/cmdline" 2>/dev/null
}

if ! bridge_alive; then
    setsid python3 "$BRIDGE" >"$WK_TMP/.wk-bridge.log" 2>&1 &
    # Without the pid the next exec starts another bridge rather than leaving
    # the workspace with no egress.
    echo $! > "$PIDFILE" || warn "could not write $PIDFILE ($WK_TMP full?);
    the next command in this workspace will start a second bridge"
    # The bridge binds before serving. bridge.py reads WK_PROXY_PORT too.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        (exec 3<>/dev/tcp/127.0.0.1/${WK_PROXY_PORT:-3128}) 2>/dev/null && break
        sleep 0.1
    done
fi

# --- talking to GitHub's API without holding the credential that does it ------
# api.github.com is the one host whose TLS is terminated outside this workspace
# (INJECTED_HOSTS, container/proxy/wk-proxy.py): the injector replaces the
# Authorization header with the real token, so a workspace needs only a
# certificate to trust and a placeholder to send. The bundle is the system's
# plus that CA and never that CA alone, since these variables replace the trust
# store outright.
WK_CA_SRC=/run/wk/wk-github-ca.pem
WK_CA_BUNDLE="$WK_TMP/.wk-ca-bundle.pem"
if [ -r "$WK_CA_SRC" ]; then
    if [ ! -s "$WK_CA_BUNDLE" ] || [ "$WK_CA_SRC" -nt "$WK_CA_BUNDLE" ]; then
        if ! { cat /etc/ssl/certs/ca-certificates.crt "$WK_CA_SRC" > "$WK_CA_BUNDLE.$$" \
               && mv "$WK_CA_BUNDLE.$$" "$WK_CA_BUNDLE"; }; then
            # Left behind, it accumulates one file per exec.
            rm -f "$WK_CA_BUNDLE.$$"
            warn "could not build $WK_CA_BUNDLE ($WK_TMP full?);
    api.github.com will fail to verify in this workspace"
        fi
    fi
    # Naming a file that is not there fails every HTTPS request.
    if [ -s "$WK_CA_BUNDLE" ]; then
        export REQUESTS_CA_BUNDLE="$WK_CA_BUNDLE"
        export CURL_CA_BUNDLE="$WK_CA_BUNDLE"
        export GIT_SSL_CAINFO="$WK_CA_BUNDLE"
        # `gh` is Go: crypto/x509 reads none of the three above, only these
        # two -- and it sends an Authorization header only when it has a token,
        # so it gets the same placeholder for the injector to replace.
        export SSL_CERT_FILE="$WK_CA_BUNDLE"
        export SSL_CERT_DIR=/etc/ssl/certs
        export GH_TOKEN=wk-injects-this
    fi
fi

# What `git-webkit pr` authenticates with: webkitcorepy reads these two, and the
# keyring is unusable in a container. A placeholder token, so an agent can open
# a pull request without ever reading the credential that did it.
if [ -r /secrets/github-user ]; then
    GITHUB_COM_USERNAME=$(cat /secrets/github-user)
    export GITHUB_COM_USERNAME
    export GITHUB_COM_TOKEN=wk-injects-this
fi

# The workspace's own tools on PATH: a login shell gets this from ~/.bashrc and
# nothing else does. Here rather than in the container's environment because
# this file is read from /opt/wk-tools, so an existing workspace gets it too.
WK_TOOLS_DIR=/opt/wk-tools
. "$WK_TOOLS_DIR/shell/path.sh"

exec "$@"
