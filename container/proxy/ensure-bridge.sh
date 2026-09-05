#!/bin/bash

set -euo pipefail

# The two steps writing into the temp directory warn rather than die: under errexit a full or read-only /tmp would abort every exec in the workspace over a pidfile.
warn() { printf 'wk: %s\n' "$*" >&2; }

WK_TMP="${TMPDIR:-/tmp}"
PIDFILE="$WK_TMP/.wk-bridge.pid"
BRIDGE=/opt/wk-tools/container/proxy/bridge.py

bridge_alive() {
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && grep -qa "bridge.py" "/proc/$pid/cmdline" 2>/dev/null
}

if ! bridge_alive; then
    setsid python3 "$BRIDGE" >"$WK_TMP/.wk-bridge.log" 2>&1 &
    echo $! > "$PIDFILE" || warn "could not write $PIDFILE ($WK_TMP full?);
    the next command in this workspace will start a second bridge"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        (exec 3<>/dev/tcp/127.0.0.1/${WK_PROXY_PORT:-3128}) 2>/dev/null && break
        sleep 0.1
    done
fi

# api.github.com's TLS terminates at the injector, which swaps the placeholder Authorization header for the real token. The bundle is the system store plus that CA and never that CA alone, since these variables replace the trust store outright; `gh` is Go and reads only SSL_CERT_FILE/SSL_CERT_DIR, webkitcorepy only GITHUB_COM_*.
WK_CA_SRC=/run/wk/wk-github-ca.pem
WK_CA_BUNDLE="$WK_TMP/.wk-ca-bundle.pem"
if [ -r "$WK_CA_SRC" ]; then
    if [ ! -s "$WK_CA_BUNDLE" ] || [ "$WK_CA_SRC" -nt "$WK_CA_BUNDLE" ]; then
        if ! { cat /etc/ssl/certs/ca-certificates.crt "$WK_CA_SRC" > "$WK_CA_BUNDLE.$$" \
               && mv "$WK_CA_BUNDLE.$$" "$WK_CA_BUNDLE"; }; then
            rm -f "$WK_CA_BUNDLE.$$"
            warn "could not build $WK_CA_BUNDLE ($WK_TMP full?);
    api.github.com will fail to verify in this workspace"
        fi
    fi
    if [ -s "$WK_CA_BUNDLE" ]; then
        export REQUESTS_CA_BUNDLE="$WK_CA_BUNDLE"
        export CURL_CA_BUNDLE="$WK_CA_BUNDLE"
        export GIT_SSL_CAINFO="$WK_CA_BUNDLE"
        export SSL_CERT_FILE="$WK_CA_BUNDLE"
        export SSL_CERT_DIR=/etc/ssl/certs
        export GH_TOKEN=wk-injects-this
    fi
fi

if [ -r /secrets/github-user ]; then
    GITHUB_COM_USERNAME=$(cat /secrets/github-user)
    export GITHUB_COM_USERNAME
    export GITHUB_COM_TOKEN=wk-injects-this
fi

WK_TOOLS_DIR=/opt/wk-tools
. "$WK_TOOLS_DIR/shell/path.sh"

exec "$@"
