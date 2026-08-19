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

# Where everything lives. /var/lib/wk inside the macOS VM, which is provisioned
# for it and has no other user; under the user's own data directory on a Linux
# workstation, because nothing here needs to be system-owned and a store that
# needs root to create is a store that needs root to repair. An existing
# /var/lib/wk still wins if it is ours, so a machine set up before this change
# keeps working without moving a hundred gigabytes.
_wk_default_store() {
    if [ -n "${WK_IN_VM:-}" ] || [ "$(uname -s)" = Darwin ]; then
        echo /var/lib/wk
    elif [ -d /var/lib/wk ] && [ -w /var/lib/wk ]; then
        echo /var/lib/wk
    else
        echo "${XDG_DATA_HOME:-$HOME/.local/share}/wk"
    fi
}

WK_STORE="${WK_STORE:-$(_wk_default_store)}"

# ccache ceiling, shared by every workspace.
#
# Measured: a full WPE release build plus two JSC release builds came to 364 MB
# across ~6,200 cached objects. Release objects compress well, so the number is
# far lower than intuition suggests.
#
# Debug builds are the real driver -- unstripped objects with full DWARF run
# roughly 8-10x release, so a full debug WebKit build is on the order of 3-4 GB
# of cache. 40 GB therefore holds well over two full builds of any
# configuration, with room for several ports side by side, and still leaves
# most of the 200 GB disk for snapshots and workspaces.
WK_CCACHE_MAXSIZE="${WK_CCACHE_MAXSIZE:-40G}"

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

# The repositories a PR branch can live in, by bare name.
#
# Derived from wk_remotes rather than listed again: the upstreams this tooling
# knows are WebKit/WebKit and WebPlatformForEmbedded/WPEWebKit, and a fork of
# either keeps the repository name. `wk pr <user>:<branch>` tries each in turn,
# because the branch name alone does not say which project it belongs to and
# asking is worse than looking.
wk_pr_repos() {
    wk_remotes | awk '{ print $2 }' \
        | sed -E 's#(\.git)?$##; s#.*/##' \
        | awk '!seen[$0]++'
}

# Forks that workspaces may push to.
#
#   <remote>  <owner/repo>  <ssh-host-alias>
#
# One deploy key per fork, because GitHub deploy keys are scoped to a single
# repository and it refuses to accept the same key on a second one. That
# restriction is the feature: each key can write to exactly one repo, so a
# compromised workspace can push to these and nowhere else. A personal access
# token would be one credential for both, but a broader one.
#
# Since both forks are on github.com, the keys are selected by ssh host alias
# (github-webkit, github-wpe) rather than by hostname.
wk_push_forks() {
    cat <<'EOF'
fork     justinmichaud/WebKit      github-webkit
forkwpe  justinmichaud/WPEWebKit   github-wpe
EOF
}

# The remote wiring every WebKit checkout gets, as a portable `sh` snippet.
#
# One authority for three questions that were being answered separately, and
# differently, in four places:
#
#   origin is WebKit/WebKit. Always, on every target. A workspace whose origin
#   is a machine-local mirror answers `git log origin/main` with whatever that
#   mirror last fetched, and `git push origin` with something even worse -- and
#   the remote build machine's driver did exactly that, pointing origin at the
#   box's shared clone because it is closer. Closeness is what a second remote
#   is for; the name `origin` means upstream.
#
#   Pushing to origin fails immediately rather than after an auth round trip.
#   There is no write access to WebKit/WebKit and never will be.
#
#   Every fork in wk_push_forks is present, with an https fetch URL and an ssh
#   push URL through its own host alias -- one deploy key per fork, which is
#   the only way GitHub allows two. Whether the key is *there* is a separate
#   question, and a switch: see `wk push`.
#
# A snippet rather than a shell function because the checkout is very often not
# on this machine: the base snapshot is here, a remote workspace's checkout is
# at the far end of an ssh, and a guest's is inside a VM. The words have to
# travel; the list of forks must not be retyped to make that happen.
#
#   wk_wiring_script <src> [<extra-remote-name> <extra-remote-url> [<ssh-config>]]
#
# The optional extra remote is a local, fast copy of the same history -- the
# build machine's shared clone, or our own mirror. Fetch-only by nature.
#
# The optional ssh config is for a machine where the aliases cannot live in
# ~/.ssh/config: a shared build box, whose home directory is the user's own and
# often several machines'. There the checkout carries `core.sshCommand`
# instead, so the push URLs resolve through a file wk owns and nothing outside
# the wk root is edited.
wk_wiring_script() {
    local src="$1" extra_name="${2:-}" extra_url="${3:-}" ssh_config="${4:-}"
    printf 'set -e
'
    printf 'cd %s
' "$(sh_quote "$src")"
    printf 'git remote set-url origin https://github.com/WebKit/WebKit.git 2>/dev/null || git remote add origin https://github.com/WebKit/WebKit.git
'
    printf 'git remote set-url --push origin no-push://use-a-fork-remote
'
    wk_push_forks | while read -r remote repo alias; do
        [ -n "$remote" ] || continue
        printf 'git remote add %s https://github.com/%s.git 2>/dev/null || git remote set-url %s https://github.com/%s.git
' \
            "$remote" "$repo" "$remote" "$repo"
        printf 'git remote set-url --push %s git@%s:%s.git
' "$remote" "$alias" "$repo"
    done
    if [ -n "$ssh_config" ]; then
        printf 'git config core.sshCommand %s\n' "$(sh_quote "ssh -F $ssh_config")"
    fi
    if [ -n "$extra_name" ] && [ -n "$extra_url" ]; then
        printf 'git remote add %s %s 2>/dev/null || git remote set-url %s %s
' \
            "$extra_name" "$(sh_quote "$extra_url")" "$extra_name" "$(sh_quote "$extra_url")"
        printf 'git remote set-url --push %s no-push://%s-is-a-local-copy
' "$extra_name" "$extra_name"
    fi
}

# The ssh aliases that select a deploy key per fork, as an ssh_config body.
#
# One key per fork and both forks on github.com, so the key cannot be chosen by
# hostname -- an alias per fork is the only way ssh will offer the right one.
# The same three lines are needed in three places (a container's ~/.ssh/config,
# a build machine's own config file under the remote root, and any future
# guest), so the list lives here with wk_push_forks rather than being retyped
# next to each of them.
#
#   wk_ssh_alias_blocks <directory holding build_key_<remote>>
wk_ssh_alias_blocks() {
    local dir="$1"
    wk_push_forks | while read -r remote repo alias; do
        [ -n "$remote" ] || continue
        cat <<EOF

Host $alias
    HostName github.com
    User git
    IdentityFile $dir/build_key_$remote
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
    done
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
    # Recorded in the cache's own config as well as passed as an environment
    # variable, so `ccache -s` reports the real limit from the host too rather
    # than the 5 GB default.
    [ -f "$WK_STORE/cache/ccache/ccache.conf" ] || \
        printf 'max_size = %s\n' "$WK_CCACHE_MAXSIZE" > "$WK_STORE/cache/ccache/ccache.conf"
    ensure_dir "$WK_STORE/cache/yocto/downloads"
    ensure_dir "$WK_STORE/cache/yocto/sstate"
    ensure_dir "$WK_STORE/cache/buildroot/dl"
    ensure_dir "$WK_STORE/cache/buildroot/ccache"
    ensure_dir "$WK_STORE/cache/bench"
    # Benchmark results. Created here rather than by `wk bench`, because it is
    # bind-mounted at container creation and podman refuses to start a
    # container whose mount source does not exist.
    ensure_dir "$WK_STORE/bench"
    ensure_dir "$WK_STORE/skills"
    ensure_dir "$WK_STORE/secrets" 0700
}

base_path() { echo "$(wk_base_dir)/$1/WebKit"; }

# --- the snapshot completion marker -----------------------------------------
#
# `wk sync` publishes by hardlinking the previous snapshot, fetching into it and
# checking it out -- minutes of work, at the end of which the directory becomes
# a usable base. Killed anywhere in the middle it leaves a directory that is
# newer than every good one, and `current_base` was `ls | tail -1`: the next
# `wk new` pinned the rubble, and the next `wk sync` hardlinked from it.
#
# So the recorded sha -- which existed already, as a cache nothing read -- is
# written last and becomes the publication gate. Present means published; absent
# means "still being made, or was being made when something killed it", and
# every reader ignores it. It is the same pattern as the firstrun marker, and
# the same one the workspace lifecycle uses.
#
# It is also the tamper evidence: a by-hand `git fetch` or checkout inside a
# published snapshot moves HEAD away from the recorded sha, and a snapshot whose
# tree no longer matches what was published is refused by name rather than
# silently handed to a workspace.
base_sha_file() { echo "$(wk_base_dir)/$1/sha"; }

base_complete() { [ -s "$(base_sha_file "$1")" ]; }

# The recorded sha, and the tree's own. Empty for either is "cannot tell",
# which base_verify reports rather than passing over.
base_recorded_sha() { cat "$(base_sha_file "$1")" 2>/dev/null || true; }
base_tree_sha()     { git -C "$(base_path "$1")" rev-parse HEAD 2>/dev/null || true; }

# base_verify <id> -- 0 if this snapshot is publishable and untampered.
# Prints the reason it is not, so callers can die with it.
base_verify() {
    local id="$1" want got
    [ -d "$(wk_base_dir)/$id" ] || { echo "snapshot $id does not exist"; return 1; }
    if ! base_complete "$id"; then
        echo "snapshot $id was never finished publishing (no completion marker).
    An interrupted 'wk sync' leaves one; the next 'wk gc' removes it."
        return 1
    fi
    [ -d "$(base_path "$id")/.git" ] || { echo "snapshot $id is not a git checkout"; return 1; }
    want=$(base_recorded_sha "$id")
    got=$(base_tree_sha "$id")
    [ -n "$got" ] || { echo "snapshot $id has no readable HEAD"; return 1; }
    [ "$want" = "$got" ] || {
        echo "snapshot $id no longer matches what was published:
    recorded $want
    tree     $got
    Something fetched or checked out inside a snapshot. Snapshots are
    immutable by design -- publish a new one with 'wk sync'."
        return 1
    }
}

# The snapshot a new workspace gets: the newest *published* one. Snapshot ids
# sort lexically because they are UTC timestamps, so the newest is the last --
# but only complete ones are candidates, which is what makes an interrupted
# sync invisible to every reader instead of being the freshest thing here.
current_base() {
    local d
    for d in $(ls -1 "$(wk_base_dir)" 2>/dev/null | sort -r); do
        base_complete "$d" || continue
        echo "$d"
        return 0
    done
    return 1
}

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

# Workspaces that exist without having recorded which snapshot they pin:
# creation in flight, or rubble left by one that was killed. A pin that is not
# written yet is still a reference -- the overlay's lower layer is in use --
# and it is one nothing can count.
unpinned_workspaces() {
    local ws
    for ws in $(list_workspaces); do
        ws_base_id "$ws" >/dev/null 2>&1 || echo "$ws"
    done
}

# Snapshots with no workspace pinning them. `wk gc` prunes these.
#
# Nothing at all while any workspace is unpinned: with a reference that cannot
# be counted, "unreferenced" is a guess, and acting on it deletes the lower
# layer of a live overlay. One answer here rather than a guard in `wk gc`, so
# that `wk ls` cannot report as reclaimable what gc will refuse to touch.
unreferenced_bases() {
    local base used ws id
    [ -z "$(unpinned_workspaces)" ] || return 0
    used=""
    for ws in $(list_workspaces); do
        id=$(ws_base_id "$ws" 2>/dev/null) || continue
        used="$used $id"
    done

    # The newest published snapshot is referenced by policy even when no
    # workspace pins it: it is what the next `wk new` gets, and re-fetching it
    # costs minutes. Here rather than in `wk gc`, which is where the exception
    # used to live -- `wk ls` then reported as reclaimable exactly the snapshot
    # gc would refuse to remove.
    local keep; keep=$(current_base 2>/dev/null || true)

    for base in $(ls -1 "$(wk_base_dir)" 2>/dev/null); do
        [ "$base" = "$keep" ] && continue
        case " $used " in
            *" $base "*) continue ;;
        esac
        echo "$base"
    done
}
