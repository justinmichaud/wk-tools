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
# /var/lib/wk wins if it is ours, so a machine already holding one keeps working
# without moving a hundred gigabytes.
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

# Is the store on *this* machine, or somewhere this machine only drives?
#
# On a Linux workstation and inside the podman VM it is here. On a macOS
# workstation it is not: $WK_STORE is the VM's /var/lib/wk, a path the Mac can
# neither create nor read -- so a command that acts on the store has to be
# forwarded rather than attempted. `mkdir: /var/lib/wk: Permission denied` is
# what attempting it looks like, and it says nothing about which machine to run
# on instead.
store_is_local() {
    [ -n "${WK_IN_VM:-}" ] && return 0
    [ "$(uname -s)" != Darwin ] && return 0
    # A Mac with a real /var/lib/wk it owns is possible and is still local.
    [ -d "$WK_STORE" ] && [ -w "$WK_STORE" ]
}

# This device's own state, on the host, whatever $WK_STORE happens to be.
#
# Answers the question this file exists for: where does something live when
# $WK_STORE is somebody else's path. Two users are that shape: the workspace
# registry (target-dependent $WK_STORE cannot say which target a workspace is
# on) and the image store (on a macOS host $WK_STORE is the podman VM's
# /var/lib/wk, which the Mac cannot create).
#
# `wk_state_dir` itself lives in lib/common.sh, not here or in lib/target.sh:
# a helper reachable only through a higher-sourced file silently disappears
# for any command that sources a lower one without it -- `wk` sources
# target.sh without store.sh, so anything defined only here is missing from
# target_all() on a macOS host, and common.sh is the floor every such file
# sources.

# ccache ceiling, shared by every workspace.
#
# Measured: a full WPE release build plus two JSC release builds came to
# 364 MB across ~6,200 cached objects -- release objects compress well, far
# lower than intuition suggests.
#
# Debug builds are the real driver: unstripped objects with full DWARF run
# roughly 8-10x release, so a full debug WebKit build is on the order of
# 3-4 GB of cache. 40 GB holds well over two full builds of any
# configuration, with room for several ports side by side, and still leaves
# most of the 200 GB disk for snapshots and workspaces.
WK_CCACHE_MAXSIZE="${WK_CCACHE_MAXSIZE:-40G}"

# The ccache ceiling, written the same way everywhere.
#
# ccache's own default is 5 GB, which a couple of WebKit builds blow through,
# so every cache this repo creates records the real limit in its own config
# as well as receiving it in the environment -- otherwise `ccache -s` on the
# machine reports 5 GB and the next reader concludes the cache is
# misconfigured.
#
# One function rather than two spellings of the same policy, which is how a
# store cache and a remote target's cache would silently drift to different
# sizes.
#
# Writes the value in, never a path out: the caller says where.
ccache_conf_render() { printf 'max_size = %s\n' "$WK_CCACHE_MAXSIZE"; }
ccache_conf_write() { # <path to ccache.conf>
    [ -f "$1" ] || ccache_conf_render > "$1"
}

wk_mirror()   { echo "$WK_STORE/git/WebKit.git"; }
wk_base_dir() { echo "$WK_STORE/base"; }
wk_ws_dir()   { echo "$WK_STORE/ws/$1"; }

# Upstreams kept in the single mirror, so a workspace can check out a branch
# from any of them without another fetch.
#
# All HTTPS, including the forks: these are public repositories and fetching
# is anonymous, so the mirror needs no credential at all. Pushing is a
# separate concern handled per-workspace by the deploy key, scoped to the
# fork -- SSH here would make `wk sync` fail on any machine without that key.
# This list is also what a workspace's remotes are wired from
# (wk_wiring_script), so it is the one place a project is added.
#
# The wiki's own set for a WebKit checkout also has `igalia`
# (ssh://git@gitlab.igalia.com:4429/...), deliberately absent here: a
# workspace holds deploy keys for the two forks and no personal key, and the
# egress allowlist permits igalia.com on 80 and 443 only, not gitlab's ssh
# port 4429 (container/proxy/wk-proxy.py). A remote that cannot authenticate
# or connect from where it is written is worse than no remote -- it fails at
# fetch time, in a workspace, with an ssh error. Add it here the day a
# workspace has a way to reach it.
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
# One authority for three questions:
#
#   origin is WebKit/WebKit. Always, on every target. A workspace whose
#   origin is a machine-local mirror answers `git log origin/main` with
#   whatever that mirror last fetched, and `git push origin` with something
#   even worse. Closeness is what a second remote is for; the name `origin`
#   means upstream.
#
#   Pushing to origin fails immediately rather than after an auth round trip.
#   There is no write access to WebKit/WebKit and never will be.
#
#   Every fork in wk_push_forks is present, with an https fetch URL and an
#   ssh push URL through its own host alias -- one deploy key per fork, the
#   only way GitHub allows two. Whether the key is *there* is a separate
#   question, and a switch: see `wk push`.
#
# A snippet rather than a shell function because the checkout is very often
# not on this machine: the base snapshot is here, a remote workspace's
# checkout is at the far end of an ssh, and a guest's is inside a VM. The
# words have to travel; the list of forks must not be retyped to make that
# happen.
#
#   wk_wiring_script <src> [<extra-remote-name> <extra-remote-url> [<ssh-config>]]
#
# The optional extra remote is a local, fast copy of the same history -- the
# build machine's shared clone, or our own mirror. Fetch-only by nature.
#
# The optional ssh config is for a machine where the aliases cannot live in
# ~/.ssh/config: a shared build box, whose home directory is the user's own
# and often several machines'. There the checkout carries `core.sshCommand`
# instead, so the push URLs resolve through a file wk owns and nothing
# outside the wk root is edited.
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
    # The other upstreams, fetch-only, from the same list the mirror carries.
    # `wpe` matters here: without it a workspace cannot `git fetch wpe` at
    # all, so a WPEWebKit branch cannot be looked at even though `wk pr`
    # accepts PRs from that project and the mirror carries its objects.
    #
    # Pushing to them is refused for exactly the reason origin is: we never
    # push to an upstream, always to a fork.
    _forks=$(wk_push_forks | awk 'NF {printf " %s", $1}')
    wk_remotes | while read -r name url; do
        [ -n "$name" ] || continue
        [ "$name" = origin ] && continue
        case "$_forks " in *" $name "*) continue ;; esac
        printf 'git remote add %s %s 2>/dev/null || git remote set-url %s %s
' \
            "$name" "$(sh_quote "$url")" "$name" "$(sh_quote "$url")"
        printf 'git remote set-url --push %s no-push://use-a-fork-remote
' "$name"
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

# The same wiring, as a *check* rather than an assertion.
#
# `wk_wiring_script` is the authority on what a checkout's remotes should be.
# They drift for two reasons: a workspace made before the wiring existed
# keeps whatever `git clone` left it with -- `origin` pointing at the local
# mirror it was cloned from, the exact failure the wiring script prevents --
# and anyone can run `git remote set-url` in a checkout afterwards.
#
# The shape this catches, seen on a real build box: `origin` =
# /home/…/wk/mirror with pushing *enabled* to it, `fork` with an https push
# URL (so no deploy key can ever be offered, whatever `wk push` says), and no
# `core.sshCommand`. Nothing else reports any of it.
#
# Prints one `problem: …` line per fault and exits 1 if there were any, so a
# caller can relay it without parsing. A snippet for the same reason as the
# wiring: the checkout is usually on another machine.
#
# A non-empty second argument skips the checks that are about the
# *environment* rather than the tree -- whether ssh can resolve the fork's
# host alias from here. A base snapshot is a template nobody pushes from, and
# lives in the podman VM, which has no alias config and needs none: the
# aliases are written into each workspace (container/firstrun.sh) and into a
# build machine's own ssh config. Asking the environment question of a
# snapshot means reporting a fault that no re-wiring can ever clear.
#
#   wk_wiring_check_script <src> [<skip-env>]
wk_wiring_check_script() {
    local src="$1" skip_env="${2:-}"
    printf 'cd %s || exit 2
' "$(sh_quote "$src")"
    printf 'bad=0
'
    # origin is upstream, fetch-only. Both halves matter: a local path here is
    # what makes `git log origin/main` answer for a stale mirror, and a
    # *pushable* origin is a push at the wrong repository waiting to happen.
    printf 'u=$(git remote get-url origin 2>/dev/null || echo "")
'
    printf 'p=$(git remote get-url --push origin 2>/dev/null || echo "")
'
    printf 'case "$u" in
  https://github.com/WebKit/WebKit.git) ;;
  "") echo "problem: no origin remote at all"; bad=1 ;;
  *)  echo "problem: origin is $u -- origin must be upstream (WebKit/WebKit); a local copy is what a second remote is for"; bad=1 ;;
esac
'
    # Only when the remote is there: "origin accepts a push ()" about a remote
    # that does not exist is a second complaint about the first fault.
    printf 'if [ -n "$u" ]; then case "$p" in
  no-push://*) ;;
  *) echo "problem: origin accepts a push ($p) -- there is no write access to upstream, and this is how a push goes to the wrong repository"; bad=1 ;;
esac
fi
'
    # The other upstreams the wiring adds (`wpe`), by the same two rules: the
    # url the mirror uses, and no push -- we never push to an upstream.
    _forks=$(wk_push_forks | awk 'NF {printf " %s", $1}')
    wk_remotes | while read -r name url; do
        [ -n "$name" ] || continue
        [ "$name" = origin ] && continue
        case "$_forks " in *" $name "*) continue ;; esac
        printf 'u=$(git remote get-url %s 2>/dev/null || echo "")
' "$name"
        printf 'p=$(git remote get-url --push %s 2>/dev/null || echo "")
' "$name"
        printf 'case "$u" in
  %s) ;;
  "") echo "problem: no %s remote (upstream %s), so its branches cannot be fetched at all"; bad=1 ;;
  *)  echo "problem: %s is $u, not %s"; bad=1 ;;
esac
' "$url" "$name" "$url" "$name" "$url"
        printf 'if [ -n "$u" ]; then case "$p" in
  no-push://*) ;;
  *) echo "problem: %s accepts a push ($p) -- we never push to an upstream"; bad=1 ;;
esac
fi
' "$name"
    done
    wk_push_forks | while read -r remote repo alias; do
        [ -n "$remote" ] || continue
        printf 'u=$(git remote get-url %s 2>/dev/null || echo "")
' "$remote"
        printf 'p=$(git remote get-url --push %s 2>/dev/null || echo "")
' "$remote"
        printf 'case "$u" in
  https://github.com/%s.git) ;;
  "") echo "problem: no %s remote (the fork)"; bad=1 ;;
  *)  echo "problem: %s fetches from $u, not https://github.com/%s.git"; bad=1 ;;
esac
' "$repo" "$remote" "$remote" "$repo"
        # The push URL is the whole point of the switch: the deploy key is
        # selected by the ssh host alias, so an https push URL means git never
        # asks ssh, never offers a key, and `wk push on` cannot help.
        printf 'if [ -n "$u" ]; then case "$p" in
  git@%s:%s.git) ;;
  *) echo "problem: %s pushes to $p, not git@%s:%s.git -- the deploy key is chosen by that ssh alias, so no key is offered at all"; bad=1 ;;
esac
fi
' "$alias" "$repo" "$remote" "$alias" "$repo"
        # ...and the alias has to resolve. Either the user's own ssh config
        # knows it, or the checkout carries a core.sshCommand pointing at a
        # config that does -- which is the arrangement on a shared machine,
        # where wk owns a config file under its own root instead of editing
        # ~/.ssh/config.
        [ -z "$skip_env" ] || continue
        printf 'c=$(git config core.sshCommand 2>/dev/null || echo "")
'
        printf 'f=${c#*-F }; f=${f%%%% *}
'
        printf 'if ! grep -qs "^[[:space:]]*Host[[:space:]].*%s" "$HOME/.ssh/config" "$HOME/.ssh/config.d/"* 2>/dev/null; then
  if [ -z "$c" ]; then
    echo "problem: nothing resolves the ssh alias %s (no Host entry, no core.sshCommand)"; bad=1
  elif ! grep -qs "^[[:space:]]*Host[[:space:]].*%s" "$f" 2>/dev/null; then
    echo "problem: core.sshCommand uses $f, which has no Host entry for %s"; bad=1
  fi
fi
' "$alias" "$alias" "$alias" "$alias"
    done
    # Which repository the *branch* points at, which is the same separation
    # one level up -- and a fault rather than a note, because the rule is
    # absolute: we never push to origin, always to the fork. A working branch
    # tracking origin/<x> therefore cannot be pushed by a bare `git push` at
    # all -- and where origin is a local mirror (the fault above) that
    # tracking ref names a different repository than it does once the wiring
    # is correct, so a branch tracks origin/eng/... for a branch that exists
    # on the fork and nowhere upstream.
    #
    # Following an upstream's own branches is the exception -- nobody pushes
    # those either, so tracking one is not a push waiting to fail.
    local keep='""' _u _r _f
    for _u in $(wk_remotes | awk 'NF {print $1}'); do
        case "$_forks " in *" $_u "*) continue ;; esac
        for _b in $(wk_mirror_branches); do keep="$keep|$_u/$_b"; done
    done
    printf 'b=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")
if [ -n "$b" ]; then
  up=$(git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>/dev/null || echo "")
  case "$up" in
    %s) ;;
' "$keep"
    # One arm per upstream, naming *that project's* fork: a WPEWebKit branch
    # belongs to forkwpe, not to fork. Paired by repository name, the same way
    # wk_pr_repos derives its list, so adding a project to wk_remotes and
    # wk_push_forks is all it takes.
    wk_remotes | while read -r _u _url; do
        [ -n "$_u" ] || continue
        # Upstreams only. A branch tracking its *own* fork is the arrangement
        # this is checking for, not a fault.
        case "$_forks " in *" $_u "*) continue ;; esac
        _r=$(printf '%s' "$_url" | sed -E 's#(\.git)?$##; s#.*/##')
        _f=$(wk_push_forks | awk -v r="$_r" 'NF && $2 ~ "/" r "$" { print $1; exit }')
        [ -n "$_f" ] || continue
        printf '    %s/*) echo "problem: branch $b tracks $up, and we never push to %s -- it belongs to the fork: %s/$b"; bad=1 ;;
' "$_u" "$_u" "$_f"
    done
    printf '  esac
fi
'
    printf 'exit $bad
'
}

# Point the current branch at the fork it can actually be pushed to.
#
# The rule is the one above, one level down: we never push to an upstream, so a
# working branch tracking origin/<x> or wpe/<x> can never be pushed by a bare
# `git push`. This is the only part of the wiring that touches a *branch*, which
# is why it is a separate snippet run only by `wk remotes --fix` rather than by
# every creation: it is git config (branch.<b>.remote and .merge), not the
# checkout, but it is still somebody's branch.
#
# The fetch is needed and is one ref: `git branch -u` refuses an upstream whose
# remote-tracking ref it has never seen. A branch that is not on the fork yet
# gets said so rather than silently left -- pushing it is the answer, and only
# a person can decide to.
#
#   wk_branch_upstream_fix_script <src>
wk_branch_upstream_fix_script() {
    local src="$1" _u _url _r _f
    printf 'cd %s || exit 2
' "$(sh_quote "$src")"
    printf 'b=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")
[ -n "$b" ] || exit 0
up=$(git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>/dev/null || echo "")
f=""
case "$up" in
'
    _forks=$(wk_push_forks | awk 'NF {printf " %s", $1}')
    for _b in $(wk_mirror_branches); do
        wk_remotes | while read -r _u _url; do
            [ -n "$_u" ] || continue
            case "$_forks " in *" $_u "*) continue ;; esac
            printf '  %s/%s) exit 0 ;;
' "$_u" "$_b"
        done
    done
    wk_remotes | while read -r _u _url; do
        [ -n "$_u" ] || continue
        case "$_forks " in *" $_u "*) continue ;; esac
        _r=$(printf '%s' "$_url" | sed -E 's#(\.git)?$##; s#.*/##')
        _f=$(wk_push_forks | awk -v r="$_r" 'NF && $2 ~ "/" r "$" { print $1; exit }')
        [ -n "$_f" ] || continue
        printf '  %s/*) f=%s ;;
' "$_u" "$_f"
    done
    printf '  *) exit 0 ;;
esac
git fetch -q "$f" "$b" 2>/dev/null || true
if git rev-parse --verify -q "refs/remotes/$f/$b" >/dev/null; then
    git branch -u "$f/$b" >/dev/null 2>&1 && echo "retargeted: $b now tracks $f/$b"
else
    echo "left alone: $b is not on $f yet -- push it first:  git push $f $b"
fi
'
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

# What a plain `wk sync` fetches: all of them.
#
# Not `origin` by default with a `--all` for the rest: the economy is not worth
# what it costs. Both upstreams are upstreams *here* (the
# board images are built from the WPE release branches), and both forks are
# where this fleet's own work lives -- a fork branch that is in the mirror is a
# branch `wk pr` can check out into a fresh workspace without going to GitHub at
# all. A remote that is fetched only when somebody remembers a flag is a remote
# that is missing exactly when it is wanted.
wk_mirror_default_remotes() { wk_remotes | awk 'NF {printf "%s%s", sep, $1; sep=" "} END {print ""}'; }

store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/git"
    ensure_dir "$WK_STORE/base"
    ensure_dir "$WK_STORE/ws"
    ensure_dir "$WK_STORE/cache/ccache"
    ccache_conf_write "$WK_STORE/cache/ccache/ccache.conf"
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
# `wk sync` publishes by hardlinking the previous snapshot, fetching into it
# and checking it out -- minutes of work, at the end of which the directory
# becomes a usable base. Killed anywhere in the middle it leaves a directory
# newer than every good one, so `current_base` cannot simply be `ls | tail
# -1`: that pins the rubble to the next `wk new` and hardlinks the next
# `wk sync` from it.
#
# So the recorded sha -- which existed already, as a cache nothing read -- is
# written last and becomes the publication gate. Present means published;
# absent means "still being made, or was being made when something killed
# it", and every reader ignores it. The same pattern as the firstrun marker,
# and the one the workspace lifecycle uses.
#
# It is also the tamper evidence: a by-hand `git fetch` or checkout inside a
# published snapshot moves HEAD away from the recorded sha, and a snapshot
# whose tree no longer matches what was published is refused by name rather
# than silently handed to a workspace.
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
    # costs minutes. Here rather than in `wk gc`: with the exception only over
    # there, `wk ls` reports as reclaimable exactly the snapshot gc refuses to
    # remove.
    local keep; keep=$(current_base 2>/dev/null || true)

    for base in $(ls -1 "$(wk_base_dir)" 2>/dev/null); do
        [ "$base" = "$keep" ] && continue
        case " $used " in
            *" $base "*) continue ;;
        esac
        echo "$base"
    done
}
