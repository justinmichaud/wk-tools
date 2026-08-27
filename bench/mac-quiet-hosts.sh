#!/usr/bin/env bash
#
# Denies the hosts macOS software update scans, in /etc/hosts, on a bench
# install only. Sourced by mac-bench-volume.sh's do_provision (sudo, on the
# Mac, re-runnable) and by mac-bench-firstboot.sh (root, at first boot) --
# both call the functions here rather than either carrying its own copy of
# the merge/write logic. do_build_pkg and do_repair install this file next
# to the first-boot script so firstboot can source it from the live
# filesystem at the same relative path; nothing renders or concatenates it.
#
# mesu.apple.com and gdmf.apple.com still answer OTA/DDM check-ins with
# automatic checking off (the "scanner: softwareupdated is LOADED" warning
# in lib/quiet.sh); swscan/swdist/swcdn/swdownload.apple.com and the two
# updates.cdn-apple.com hosts serve catalogs and packages; xp.apple.com is
# the client provisioning endpoint a scan also reaches. Only these
# hostnames are touched here -- no resolver setting, no radio, nothing else
# in /etc/hosts.

WK_BENCH_HOSTS_LIST="swscan.apple.com swdist.apple.com swcdn.apple.com swdownload.apple.com mesu.apple.com gdmf.apple.com updates.cdn-apple.com updates-http.cdn-apple.com xp.apple.com"

WK_BENCH_HOSTS_BEGIN="# wk-bench: software update denial -- begin"
WK_BENCH_HOSTS_END="# wk-bench: software update denial -- end"

_wk_bench_hosts_say() { printf '%s\n' "$*" >&2; }

# One 0.0.0.0 line per host, computed fresh from WK_BENCH_HOSTS_LIST every
# time -- nothing about the list is stored anywhere else, so this is also
# the definition of "correct" that wk_bench_hosts_present checks against.
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

# True when $1 (default /etc/hosts) carries exactly this block between the
# markers -- a stale block from a shorter or differently-spelled list reads
# back false, same as no block at all. Read-only.
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

# Idempotent: strips any existing marker block (whatever its contents) and
# appends a freshly rendered one, so re-running -- with the same list or a
# changed one -- always converges on exactly one block. Every line outside
# the markers passes through the awk filter unchanged. $2 = 1 means dry:
# prints the block to stderr and touches nothing.
#
# The write is a direct `cat > $hosts`, not a rename over a temp file: a
# rename only needs the containing directory to be writable, which would
# hide a $hosts that is itself read-only. Every real write is read back
# with wk_bench_hosts_present before this returns 0, so a write that did
# not land -- permission denied, a read-only volume -- is caught here
# rather than trusted.
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
