# Workspace storage: the base-snapshot scheme and the paths built on it.
#
# The central problem this solves: a workspace's WebKit tree is the lower layer
# of a live overlay mount, and the kernel is explicit that "changes to the
# underlying filesystems while part of a mounted overlay filesystem are not
# allowed ... the behavior of the overlay is undefined". So `git fetch` must
# never touch a tree that a workspace is using.
#
# The scheme:
#
#   git/WebKit.git      a bare mirror. The only thing ever fetched into, and
#                       never a lower layer, so fetching is always safe.
#   base/<id>/WebKit    immutable snapshots. A workspace pins one for life.
#   ws/<name>/changes   that workspace's copy-on-write layer.
#
# A new snapshot is `cp -al` from the previous one -- hardlinks, one
# filesystem, so it costs near-zero time and space -- and is then fetched into.
# Git creates and renames files rather than writing in place, so the previous
# snapshot's inodes are untouched and workspaces pinned to it keep working.

WK_STORE="${WK_STORE:-/var/lib/wk}"

wk_mirror()   { echo "$WK_STORE/git/WebKit.git"; }
wk_base_dir() { echo "$WK_STORE/base"; }
wk_ws_dir()   { echo "$WK_STORE/ws/$1"; }

# Upstreams kept in the single mirror, so a workspace can check out a branch
# from any of them without another fetch.
#
# All HTTPS, including the forks: these are public repositories and fetching is
# anonymous, so the mirror needs no credential at all. Pushing is a separate
# concern handled per-workspace by the deploy key, which is scoped to the fork.
# Using SSH here would make `wk sync` fail on any machine without that key.
wk_remotes() {
    cat <<'EOF'
origin   https://github.com/WebKit/WebKit.git
wpe      https://github.com/WebPlatformForEmbedded/WPEWebKit.git
fork     https://github.com/justinmichaud/WebKit.git
forkwpe  https://github.com/justinmichaud/WPEWebKit.git
EOF
}

# Which branches the mirror actually carries.
#
# WebKit/WebKit has ~920 branches, and mirroring all of them costs tens of
# gigabytes and a very long first fetch for histories nobody checks out. The
# mirror exists to make snapshots cheap, and snapshots are built from main.
#
# Anything else is still reachable: workspaces can fetch a branch directly from
# GitHub on demand, which the egress policy permits. Set WK_MIRROR_BRANCHES to
# a space-separated list to carry more (e.g. a release branch you track).
wk_mirror_branches() {
    echo "${WK_MIRROR_BRANCHES:-main}"
}

store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/git"
    ensure_dir "$WK_STORE/base"
    ensure_dir "$WK_STORE/ws"
    ensure_dir "$WK_STORE/cache/ccache"
    ensure_dir "$WK_STORE/cache/yocto/downloads"
    ensure_dir "$WK_STORE/cache/yocto/sstate"
    ensure_dir "$WK_STORE/cache/buildroot/dl"
    ensure_dir "$WK_STORE/cache/buildroot/ccache"
    ensure_dir "$WK_STORE/skills"
    ensure_dir "$WK_STORE/secrets" 0700
}

# The snapshot a new workspace gets. Snapshot ids sort lexically because they
# are UTC timestamps, so the newest is simply the last.
current_base() {
    local d
    d=$(ls -1 "$(wk_base_dir)" 2>/dev/null | sort | tail -1)
    [ -n "$d" ] || return 1
    echo "$d"
}

base_path() { echo "$(wk_base_dir)/$1/WebKit"; }

# Which snapshot a workspace is pinned to. This is the refcount that keeps
# `wk gc` from deleting a snapshot still in use.
ws_base_id() {
    local f="$(wk_ws_dir "$1")/base-id"
    [ -f "$f" ] && cat "$f" || return 1
}

list_workspaces() {
    [ -d "$WK_STORE/ws" ] || return 0
    ls -1 "$WK_STORE/ws" 2>/dev/null || true
}

# Snapshots with no workspace pinning them. `wk gc` prunes these.
unreferenced_bases() {
    local base used ws id
    used=""
    for ws in $(list_workspaces); do
        id=$(ws_base_id "$ws" 2>/dev/null) || continue
        used="$used $id"
    done

    for base in $(ls -1 "$(wk_base_dir)" 2>/dev/null); do
        case " $used " in
            *" $base "*) continue ;;
        esac
        echo "$base"
    done
}
