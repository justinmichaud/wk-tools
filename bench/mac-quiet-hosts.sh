#!/usr/bin/env bash
# Denies the hosts macOS software update scans, in /etc/hosts, on a bench install only.
# mesu and gdmf still answer OTA/DDM check-ins with automatic checking off, so the whole scan path is denied: catalogs, both CDNs, xp.apple.com.

WK_BENCH_HOSTS_LIST="swscan.apple.com swdist.apple.com swcdn.apple.com swdownload.apple.com mesu.apple.com gdmf.apple.com updates.cdn-apple.com updates-http.cdn-apple.com xp.apple.com"

WK_BENCH_HOSTS_BEGIN="# wk-bench: software update denial -- begin"
WK_BENCH_HOSTS_END="# wk-bench: software update denial -- end"

_wk_bench_hosts_say() { printf '%s\n' "$*" >&2; }

wk_bench_hosts_lines() {
    local h
    for h in $WK_BENCH_HOSTS_LIST; do
        printf '0.0.0.0 %s\n' "$h"
    done
}

wk_bench_hosts_block() {
    printf '%s\n' "$WK_BENCH_HOSTS_BEGIN"
    wk_bench_hosts_lines
    printf '%s\n' "$WK_BENCH_HOSTS_END"
}

wk_bench_hosts_present() {
    local hosts="${1:-/etc/hosts}"
    [ -r "$hosts" ] || return 1
    local got want
    got=$(awk -v b="$WK_BENCH_HOSTS_BEGIN" -v e="$WK_BENCH_HOSTS_END" '
        $0 == b { on=1; next }
        $0 == e { on=0 }
        on { print }
    ' "$hosts" 2>/dev/null)
    want=$(wk_bench_hosts_lines)
    [ "$got" = "$want" ]
}

# A direct write, not a rename over a temp file: a rename needs only the directory writable and would hide a read-only $hosts.
wk_bench_hosts_apply() {
    local hosts="${1:-/etc/hosts}" dry="${2:-}"

    if [ -n "$dry" ]; then
        _wk_bench_hosts_say "  would write this block into $hosts:"
        wk_bench_hosts_block | sed 's/^/    /' >&2
        return 0
    fi

    local tmp
    tmp=$(mktemp "${TMPDIR:-/tmp}/wk-bench-hosts.XXXXXX") || {
        _wk_bench_hosts_say "  hosts: could not make a temp file"
        return 1
    }
    if [ -f "$hosts" ]; then
        awk -v b="$WK_BENCH_HOSTS_BEGIN" -v e="$WK_BENCH_HOSTS_END" '
            $0 == b { skip=1; next }
            $0 == e { skip=0; next }
            !skip { print }
        ' "$hosts" > "$tmp"
    fi
    wk_bench_hosts_block >> "$tmp"

    if ! cat "$tmp" > "$hosts" 2>/dev/null; then
        rm -f "$tmp"
        _wk_bench_hosts_say "  hosts: could not write $hosts (permission denied?)"
        return 1
    fi
    rm -f "$tmp"

    if wk_bench_hosts_present "$hosts"; then
        return 0
    fi
    _wk_bench_hosts_say "  hosts: wrote $hosts but it did not read back correctly"
    return 1
}
