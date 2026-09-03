# Workspace storage: base-snapshot scheme built on a bare mirror.
# git/WebKit.git is the only thing ever fetched into. base/<id>/WebKit are
# immutable snapshots a workspace pins for life; ws/<name>/changes is that
# workspace's copy-on-write layer. A workspace's tree is the lower layer of
# a live overlay mount, and the kernel says changes to a mounted overlay's
# underlying filesystem are undefined -- so only the mirror is ever fetched
# into. A new snapshot is `cp -al` from the previous one (hardlinks,
# near-zero cost) and then fetched into; git creates and renames files
# rather than writing in place, so a workspace pinned to the previous
# snapshot is untouched.

# /var/lib/wk inside the macOS VM. On a Linux workstation, the user's own
# data directory: a store that needs root to create needs root to repair.
# An existing /var/lib/wk this user owns wins, so a machine already holding
# one keeps working without moving a hundred gigabytes.
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
# On a Linux workstation and inside the podman VM it is here. On a macOS
# workstation $WK_STORE is the VM's /var/lib/wk, a path the Mac can neither
# create nor read -- so a command that acts on the store has to be forwarded
# rather than attempted (`mkdir: /var/lib/wk: Permission denied` is what
# attempting it looks like).
store_is_local() {
    [ -n "${WK_IN_VM:-}" ] && return 0
    [ "$(uname -s)" != Darwin ] && return 0
    # A Mac with a real /var/lib/wk it owns is possible and is still local.
    [ -d "$WK_STORE" ] && [ -w "$WK_STORE" ]
}

# `wk_state_dir` (lib/common.sh) answers where host-side state (logs, keys,
# remote build status) lives when $WK_STORE is somebody else's path -- the
# podman VM's, on macOS. It lives in common.sh, not here: a helper reachable only through a
# higher-sourced file disappears for any command that sources a lower one
# without it, and common.sh is the floor every such file sources.

# ccache ceiling, shared by every workspace. Sized for several full debug
# builds side by side (the expensive case: unstripped objects with full
# DWARF), leaving most of the 200 GB disk for snapshots and workspaces.
# WK_CCACHE_MAXSIZE overrides the ceiling, for a store on a smaller disk.
WK_CCACHE_MAXSIZE="${WK_CCACHE_MAXSIZE:-40G}"

# ccache's own default is 5 GB, which a couple of WebKit builds blow
# through, so every cache this repo creates records the real limit in its
# own config as well as the environment -- otherwise `ccache -s` reports
# 5 GB and looks misconfigured. One function so a store cache and a remote
# target's cache cannot drift to different sizes. Writes the value in,
# never a path out: the caller says where.
ccache_conf_render() { printf 'max_size = %s\n' "$WK_CCACHE_MAXSIZE"; }
ccache_conf_write() { # <path to ccache.conf>
    [ -f "$1" ] || ccache_conf_render > "$1"
}

wk_mirror()   { echo "$WK_STORE/git/WebKit.git"; }
wk_base_dir() { echo "$WK_STORE/base"; }
wk_ws_dir()   { echo "$WK_STORE/ws/$1"; }

# Upstreams kept in the single mirror, so a workspace can fetch a branch
# from any of them without a second fetch. All HTTPS, including the forks:
# fetching is anonymous, so the mirror needs no credential; pushing is
# separate, per-workspace, via deploy key. This is also what a workspace's
# remotes are wired from (wk_wiring_script) -- the one place a project is
# added.
# The wiki's own WebKit checkout also wires `igalia`
# (ssh://git@gitlab.igalia.com:4429/...), deliberately absent here: the
# egress allowlist permits igalia.com on 80/443 only, not gitlab's ssh port
# 4429 (container/proxy/wk-proxy.py) -- a remote that cannot connect from
# where it is written fails at fetch time, in a workspace, with an ssh
# error. Add it here the day a workspace has a way to reach it.
wk_remotes() {
    cat <<'EOF'
origin   https://github.com/WebKit/WebKit.git
wpe      https://github.com/WebPlatformForEmbedded/WPEWebKit.git
fork     https://github.com/justinmichaud/WebKit.git
forkwpe  https://github.com/justinmichaud/WPEWebKit.git
EOF
}

# Repositories a PR branch can live in, by bare name. Derived from
# wk_remotes rather than listed again: `wk pr <user>:<branch>` tries each in
# turn, because the branch name alone does not say which project it belongs to.
wk_pr_repos() {
    wk_remotes | awk '{ print $2 }' \
        | sed -E 's#(\.git)?$##; s#.*/##' \
        | awk '!seen[$0]++'
}

# Forks that workspaces may push to: <remote> <owner/repo> <ssh-host-alias>.
# One deploy key per fork -- GitHub scopes a deploy key to a single
# repository -- so a compromised workspace can push only to these, never as
# broadly as a personal access token would allow. Both forks are on
# github.com, so the key is selected by ssh host alias (github-webkit,
# github-wpe), not by hostname.
wk_push_forks() {
    cat <<'EOF'
fork     justinmichaud/WebKit      github-webkit
forkwpe  justinmichaud/WPEWebKit   github-wpe
EOF
}

# The remote wiring every WebKit checkout gets, as a portable `sh` snippet
# (not a shell function: the checkout is often not on this machine -- a
# base snapshot here, a remote workspace's over ssh, a guest's inside a VM).
# origin is always WebKit/WebKit, fetch-only, never a machine-local mirror.
# Every fork in wk_push_forks gets an https fetch URL and an ssh push URL
# through its own host alias (see `wk push` for whether the key is there).
#   wk_wiring_script <src> [<extra-remote-name> <extra-remote-url> [<ssh-config>]]
# The optional extra remote is a local, fetch-only copy of the same history.
# The optional ssh config is for a machine whose aliases cannot live in
# ~/.ssh/config: the checkout carries `core.sshCommand` instead, pointing
# at a file wk owns.
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
    # The other upstreams (`wpe`), fetch-only: we always push to a fork,
    # never an upstream.
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

# The same wiring, as a *check* rather than an assertion: a workspace made
# before the wiring existed keeps whatever `git clone` left it with, and
# anyone can `git remote set-url` afterwards.
# Prints one `problem: …` line per fault and exits 1 if there were any, so a
# caller can relay it without parsing.
# A non-empty second argument skips the checks against the *environment*
# (whether ssh can resolve the fork's host alias from here): a base
# snapshot lives in the podman VM, which has no alias config and needs
# none, since aliases are written into each workspace and build machine,
# not into a snapshot nobody pushes from.
#   wk_wiring_check_script <src> [<skip-env>]
wk_wiring_check_script() {
    local src="$1" skip_env="${2:-}"
    printf 'cd %s || exit 2
' "$(sh_quote "$src")"
    printf 'bad=0
'
    # origin is upstream, fetch-only: a local path here is a stale mirror
    # `git log origin/main` will silently answer for, and a pushable origin
    # is a push at the wrong repository waiting to happen.
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
    # Only when the remote is there, else it's a second complaint about the first fault.
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
        # ...and the alias has to resolve, either in the user's own ssh
        # config or via a core.sshCommand pointing at one wk owns.
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
    # Which repository the *branch* points at: a working branch tracking
    # origin/<x> can never be pushed by a bare `git push`. Following an
    # upstream's own branches is the exception -- nobody pushes those either.
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
    # belongs to forkwpe, not to fork.
    wk_remotes | while read -r _u _url; do
        [ -n "$_u" ] || continue
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

# Point the current branch at the fork it can actually be pushed to. The
# only part of the wiring that touches a *branch*, so it runs only by
# `wk remotes --fix`, not on every creation. `git branch -u` refuses an
# upstream whose remote-tracking ref it has never seen, hence the fetch.
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
# One key per fork and both forks on github.com, so an alias per fork is
# the only way ssh will offer the right one. Needed in three places (a
# container's ~/.ssh/config, a build machine's config, a macOS guest's), so
# it lives here with wk_push_forks rather than being retyped at each.
#
#   wk_ssh_alias_blocks <dir> [<identity prefix>] [<ProxyCommand>]
#
# The identity is <dir>/<prefix><remote>. The prefix is the store's own
# `build_key_` by default, because a machine that reads the keys where they
# live names them that; a workspace that holds copies or links instead names
# them `id_<remote>` in its own ~/.ssh.
#
# The ProxyCommand is for a machine whose only route to github.com is an HTTP
# CONNECT proxy. A container leaves it empty: its catch-all `Host *` block
# already carries one, and ssh takes the first value it sees for a keyword. A
# macOS guest has no such block and takes it per alias.
wk_ssh_alias_blocks() {
    local dir="$1" prefix="${2:-build_key_}" proxy="${3:-}"
    wk_push_forks | while read -r remote repo alias; do
        [ -n "$remote" ] || continue
        cat <<EOF

Host $alias
    HostName github.com
    User git
    IdentityFile $dir/$prefix$remote
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
        [ -z "$proxy" ] || printf '    ProxyCommand %s\n' "$proxy"
    done
}

# --- delivering a deploy key to a target that cannot mount the store ---------
# The switch `wk push` throws is *where the key is*, and for a container that
# is the whole of it: secrets/ is a read-only mount and the workspace only
# links into it, so moving the key moves it for every container at once. A
# macOS guest mounts nothing of ours, so it holds a copy, written from the
# host on every start and taken away again the same way -- the arrangement
# the agent credentials below already have, for the same reason.

# The store belonging to this *machine*, which is where both the deploy keys
# and the agent credentials below live. A loaded target driver may have pointed
# $WK_STORE at its own state (targets/vm.sh does, at the host's XDG state
# directory), and they are not there: they are where `wk key register` and `wk
# key set` put them. load_target records the machine's own store in
# WK_STORE_DEFAULT before overriding.
wk_machine_store() { printf '%s' "${WK_STORE_DEFAULT:-$WK_STORE}"; }

# Is that store on this machine? store_is_local asks about $WK_STORE, so ask
# it about the right one. Both halves of the store's secrets ask this, so the
# forwarding decision is made in one place.
wk_push_store_local() ( WK_STORE=$(wk_machine_store); store_is_local )

# One fork's private key, or nothing at all. Three different things read as
# nothing -- push is off, no key was ever registered, or the store is not
# readable from here (a stopped podman machine) -- and a guest must not hold
# a key in any of them, so they are not told apart here. Which one it was is
# `wk push status`'s question.
#
# On a macOS workstation the keys are inside the podman machine, a path this
# host cannot read at all, so the read is forwarded there. Never logged and
# never an argument: the caller streams it on stdin.
wk_push_key() { # <fork>
    _wk_secret_read "$(wk_machine_store)/secrets/build_key_$1"
}

# One reader for everything under the machine store's secrets/ -- deploy keys
# and agent credentials alike. On a macOS workstation the store is inside the
# podman machine, a path this host cannot read at all, so the read is
# forwarded there; absent is not an error, and every caller reports its own
# absence in its own words.
_wk_secret_read() { # <path>
    if wk_push_store_local; then
        [ -r "$1" ] && cat "$1"
    else
        podman machine ssh "${WK_MACHINE:-wk}" -- \
            "cat $(sh_quote "$1") 2>/dev/null" </dev/null 2>/dev/null
    fi
    return 0
}

# Which branches the mirror actually carries. WebKit/WebKit has ~920
# branches; mirroring all of them costs tens of gigabytes for histories
# nobody checks out, so the mirror carries only main -- anything else is
# still reachable directly from GitHub. Set WK_MIRROR_BRANCHES to carry more.
wk_mirror_branches() {
    echo "${WK_MIRROR_BRANCHES:-main}"
}

# What a plain `wk sync` fetches: all of them, not `origin` with `--all`
# for the rest -- both upstreams matter here (board images are built from
# the WPE release branches) and both forks are where this fleet's own work
# lives, so a remote fetched only when somebody remembers a flag is one
# that is missing exactly when it is wanted.
wk_mirror_default_remotes() { wk_remotes | awk 'NF {printf "%s%s", sep, $1; sep=" "} END {print ""}'; }

# --- PR / pull-request refs, fetched once into the mirror -------------------
# `wk pr` and `wk new --pr` want a fork's branch, or an upstream's numbered
# pull request, available to every workspace without a fetch per re-run --
# the same idea wk_mirror_default_remotes serves for origin/wpe/the forks,
# for the one ref a workspace actually wants. Fetched under
# refs/remotes/pr/... so it's never mistaken for one of the mirror's own
# branches. A workspace then fetches this one ref from /mirror, the same
# local, instant path `wk sync` gives it for main. See wk_pr_checkout below.

# The path a PR ref lands under, in the mirror and in a workspace's own
# remote-tracking namespace alike -- one name so mirror_fetch_pr/
# mirror_fetch_pull and the workspace-side fetch never disagree about it.
wk_pr_refname()   { printf '%s/%s/%s' "$1" "$2" "$3"; }  # <user> <repo> <branch>
wk_pull_refname() { printf '%s/%s' "$1" "$2"; }          # <remote> <n>

# Under the store lock the way `wk sync` takes it (rule 4, one lock per
# mutated resource) -- this mutates the same bare mirror, so it serialises
# against a publish exactly as a second `wk sync` would.
_mirror_fetch_do() {  # <src-refspec> <dest-ref> <src-url-or-remote>
    local srcspec="$1" dest="$2" src="$3" mirror
    mirror=$(wk_mirror)
    if [ ! -d "$mirror" ]; then
        info "creating bare mirror (first run: this clones all of WebKit)"
        git init --bare "$mirror"
        git -C "$mirror" config gc.auto 0
    fi
    git -C "$mirror" fetch --quiet "$src" "+$srcspec:$dest"
}

# One fetch underneath mirror_fetch_pr and mirror_fetch_pull. Only called for
# a workspace that reads this machine's mirror (wk_pr_checkout decides that up
# front); a fetch that fails is a refusal, never a detour to GitHub.
_mirror_fetch_into() {  # <src-url-or-remote> <src-refspec> <dest-ref>
    local src="$1" srcspec="$2" dest="$3"
    store_is_local || die "the mirror is not this machine's (\$WK_STORE is $WK_STORE); a workspace here cannot read it"
    store_init
    with_lock store -- _mirror_fetch_do "$srcspec" "$dest" "$src"
}

# mirror_fetch_pr <url> <branch> <refname>
# Fetches <branch> from a fork's <url> into refs/remotes/pr/<refname>.
mirror_fetch_pr() {  # <url> <branch> <refname>
    _mirror_fetch_into "$1" "refs/heads/$2" "refs/remotes/pr/$3"
}

# mirror_fetch_pull <remote> <n>
# Fetches refs/pull/<n>/head into refs/remotes/pr/<remote>/<n> -- no fork
# to discover first, since GitHub serves every PR's head under the base repo.
mirror_fetch_pull() {  # <remote> <n>
    local remote="$1" n="$2" url
    url=$(wk_remotes | awk -v r="$remote" '$1 == r {print $2; exit}')
    [ -n "$url" ] || die "no such upstream remote '$remote' to fetch a pull request from"
    _mirror_fetch_into "$url" "refs/pull/$n/head" "refs/remotes/pr/$(wk_pull_refname "$remote" "$n")"
}

# --- the spec, parsed once ---------------------------------------------------
# `wk pr` and `wk new --pr` accept the same spec, parsed here once so the two
# commands and the tests all agree what it means:
#   user:branch   a fork's branch, found by asking each of wk_pr_repos in
#                 turn which one has it
#   <n>           WebKit/WebKit pull request #n (refs/pull/<n>/head) -- no
#                 fork discovery, GitHub serves the head under the base repo
#   wpe:<n>       WPEWebKit's pull request #n, the same way
# Sets PR_KIND (user|pull) and either PR_USER/PR_BRANCH or PR_REMOTE/PR_N.
# Not `local` to any function: they are this call's result, read by the
# caller immediately after.
pr_parse_spec() {  # <spec>
    local spec="$1"
    PR_KIND="" PR_USER="" PR_BRANCH="" PR_REMOTE="" PR_N=""
    case "$spec" in
        [0-9]*)
            case "$spec" in *[!0-9]*) die "'$spec' is not a pull request number (digits only)" ;; esac
            PR_KIND=pull; PR_REMOTE=origin; PR_N="$spec" ;;
        wpe:[0-9]*)
            PR_N="${spec#wpe:}"
            case "$PR_N" in *[!0-9]*) die "'$spec' is not a pull request number (digits only)" ;; esac
            PR_KIND=pull; PR_REMOTE=wpe ;;
        *:*)
            PR_USER="${spec%%:*}"; PR_BRANCH="${spec#*:}"
            [ -n "$PR_USER" ] && [ -n "$PR_BRANCH" ] \
                || die "expected <user>:<branch>, got '$spec'"
            PR_KIND=user ;;
        *)
            die "'$spec' is not a PR spec: <user>:<branch>, a pull request number, or wpe:<number>" ;;
    esac
}

# wk_pr_checkout <name> <spec>
# Check out a PR head in the workspace <name>: resolve <spec> (pr_parse_spec
# above), fetch it into the mirror once, and check it out from there,
# falling back to GitHub directly when the mirror isn't reachable. Shared
# by `wk pr` and `wk new --pr` so the two never drift into two implementations.
# Assumes lib/target.sh is sourced and load_target has already resolved
# <name>'s target. WK_FORCE takes the PR head even when a local branch of
# the same name has commits it does not, discarding them.
wk_pr_checkout() {  # <name> <spec>
    local name="$1" spec="$2"
    local src repo url branch remote head_sha local_sha dirty reset ahead
    local probe found n mirror_ok mirror_ref add_remote="" src_ref net_refspec fetch_step

    pr_parse_spec "$spec"
    src=$(t_src "$name")
    # Where the workspace fetches from is a property of its target: a container
    # reads this machine's mirror (bind-mounted as /mirror), so the branch is
    # fetched into the mirror once and every workspace takes it from there; a
    # vm or remote workspace has no path to that mirror and fetches from GitHub.
    mirror_ok=""
    [ "${WK_TARGET_KIND:-}" = container ] && store_is_local && mirror_ok=1

    case "$PR_KIND" in
    user)
        # --- which project, from an anonymous ls-remote per candidate repo --
        # `git ls-remote` over HTTPS is anonymous, the same reason a fork
        # fetches while `wk push` is off.
        probe=$(t_exec "$name" bash -c "
            cd $(sh_quote "$src") || exit 1
            for r in $(wk_pr_repos | tr '\n' ' '); do
                u=https://github.com/$(sh_quote "$PR_USER")/\$r.git
                sha=\$(git ls-remote \"\$u\" refs/heads/$(sh_quote "$PR_BRANCH") 2>/dev/null | awk '{print \$1}')
                [ -n \"\$sha\" ] && echo \"found=\$r \$u \$sha\"
            done
            # A remote that already points at this user's copy of one of
            # them, so a second one is not added beside it under another name.
            for rr in \$(git remote); do
                uu=\$(git remote get-url \"\$rr\")
                case \"\$uu\" in
                    *[:/]$(sh_quote "$PR_USER")/*) echo \"remote=\$rr \$uu\" ;;
                esac
            done
            echo \"local=\$(git rev-parse --verify --quiet refs/heads/$(sh_quote "$PR_BRANCH") || true)\"
            echo \"dirty=\$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')\"
        " 2>/dev/null | tr -d '\r') || die "could not reach the checkout in '$name'"

        found=$(printf '%s\n' "$probe" | sed -n 's/^found=//p')
        n=$(printf '%s\n' "$found" | grep -c . || true)
        case "$n" in
        0) die "no branch '$PR_BRANCH' in $(wk_pr_repos | tr '\n' '/' | sed 's|/$||') under '$PR_USER'.
    Checked: $(wk_pr_repos | sed "s|^|https://github.com/$PR_USER/|;s|\$|.git|" | tr '\n' ' ')" ;;
        1) ;;
        *) die "'$PR_BRANCH' exists in more than one of $PR_USER's repositories:
$(printf '%s\n' "$found" | sed 's/^/    /')
    They are different projects; check the PR page for which one it is and
    fetch that remote by hand." ;;
        esac

        repo=$(printf '%s' "$found" | awk '{print $1}')
        url=$(printf '%s' "$found"  | awk '{print $2}')
        head_sha=$(printf '%s' "$found" | awk '{print $3}')
        local_sha=$(kv_get local <<<"$probe")
        dirty=$(kv_get dirty <<<"$probe")
        remote=$(printf '%s\n' "$probe" | sed -n 's/^remote=//p' | awk -v u="$url" '$2 == u {print $1; exit}')

        # The user's name, with the project appended when it is not the one
        # `origin` is -- two remotes cannot share a name.
        if [ -z "$remote" ]; then
            remote="$PR_USER"
            [ "$repo" = "$(wk_pr_repos | head -1)" ] || remote="$PR_USER-$(printf '%s' "$repo" | tr 'A-Z' 'a-z')"
        fi
        branch="$PR_BRANCH"
        add_remote=1
        src_ref="$branch"
        mirror_ref="refs/remotes/pr/$(wk_pr_refname "$PR_USER" "$repo" "$branch")"

        [ -z "$mirror_ok" ] || mirror_fetch_pr "$url" "$branch" "$(wk_pr_refname "$PR_USER" "$repo" "$branch")" \
            || die "could not fetch '$branch' from $url into the mirror; nothing was checked out"
        ;;

    pull)
        remote="$PR_REMOTE"
        url=$(wk_remotes | awk -v r="$remote" '$1 == r {print $2; exit}')
        [ -n "$url" ] || die "no such upstream remote '$remote'"
        repo=$(printf '%s' "$url" | sed -E 's#(\.git)?$##; s#.*/##')
        if [ "$remote" = origin ]; then branch="pr/$PR_N"; else branch="pr/$remote-$PR_N"; fi
        add_remote=""
        src_ref="refs/pull/$PR_N/head"
        mirror_ref="refs/remotes/pr/$(wk_pull_refname "$remote" "$PR_N")"

        probe=$(t_exec "$name" bash -c "
            cd $(sh_quote "$src") || exit 1
            echo \"local=\$(git rev-parse --verify --quiet refs/heads/$(sh_quote "$branch") || true)\"
            echo \"dirty=\$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')\"
        " 2>/dev/null | tr -d '\r') || die "could not reach the checkout in '$name'"
        local_sha=$(kv_get local <<<"$probe")
        dirty=$(kv_get dirty <<<"$probe")

        head_sha=$(t_exec "$name" bash -c "
            git ls-remote $(sh_quote "$url") $(sh_quote "$src_ref") 2>/dev/null | awk '{print \$1}'
        " 2>/dev/null | tr -d '\r')
        [ -n "$head_sha" ] || die "no pull request #$PR_N on $repo (checked $url)"

        [ -z "$mirror_ok" ] || mirror_fetch_pull "$remote" "$PR_N" \
            || die "could not fetch pull/$PR_N from $url into the mirror; nothing was checked out"
        ;;
    esac

    # --- what this would cost -------------------------------------------------
    if [ "${dirty:-0}" -gt 0 ] 2>/dev/null; then
        warn "'$name' has $dirty uncommitted change(s); the checkout carries them across"
    fi

    reset=""
    if [ -n "$local_sha" ] && [ "$local_sha" != "$head_sha" ]; then
        # Commits on the local branch the PR head doesn't have; none means
        # the branch is simply behind, and fast-forwarding it costs nothing.
        ahead=$(t_exec "$name" bash -c "
            cd $(sh_quote "$src") &&
            git fetch --quiet $(sh_quote "$url") $(sh_quote "$src_ref") 2>/dev/null &&
            git rev-list --count FETCH_HEAD..$(sh_quote "$branch") 2>/dev/null || echo unknown
        " 2>/dev/null | tr -d '\r' | tail -1)

        case "$ahead" in
            0)  reset=1 ;;   # behind or equal: taking the PR head loses nothing
            unknown)
                barrier "cannot tell whether '$branch' in '$name' has work the PR head does not.
    Checking it out will leave it as it is."
                ;;
            *)
                if [ -n "${WK_FORCE:-}" ]; then
                    barrier "discarding $ahead local commit(s) on '$branch' in '$name'."
                    reset=1
                else
                    warn "local '$branch' has $ahead commit(s) the PR head does not have"
                    log  "  it is checked out as it is; nothing is discarded."
                    log  "  to take the PR head instead and lose those commits:"
                    log  "    wk pr${name:+ $name} $spec --force"
                fi
                ;;
        esac
    fi

    # --- do it: from the mirror if it landed there this run, GitHub otherwise -
    net_refspec="$src_ref:refs/remotes/$remote/$branch"
    fetch_step="git fetch --quiet $(sh_quote "$remote") $(sh_quote "$net_refspec")"
    if [ -n "$add_remote" ]; then
        fetch_step="git remote get-url $(sh_quote "$remote") >/dev/null 2>&1 || git remote add $(sh_quote "$remote") $(sh_quote "$url")
        git remote set-url $(sh_quote "$remote") $(sh_quote "$url")
        $fetch_step"
    fi
    if [ -n "$mirror_ok" ]; then
        # A workspace with no /mirror, or one where this run's fetch into
        # the mirror didn't happen, falls through to the network path above.
        fetch_step="if [ -d /mirror/WebKit.git ] && git -C /mirror/WebKit.git rev-parse --verify --quiet $(sh_quote "$mirror_ref") >/dev/null 2>&1
        then git fetch --quiet /mirror/WebKit.git $(sh_quote "$mirror_ref:refs/remotes/$remote/$branch")
        else $fetch_step
        fi"
    fi

    t_exec "$name" bash -c "
        set -e
        cd $(sh_quote "$src")
        $fetch_step
        if git show-ref --verify --quiet refs/heads/$(sh_quote "$branch"); then
            git checkout --quiet $(sh_quote "$branch")
            ${reset:+git reset --hard --quiet refs/remotes/$(sh_quote "$remote")/$(sh_quote "$branch")}
        else
            git checkout --quiet -b $(sh_quote "$branch") --track refs/remotes/$(sh_quote "$remote")/$(sh_quote "$branch")
        fi
        git branch --quiet --set-upstream-to=refs/remotes/$(sh_quote "$remote")/$(sh_quote "$branch") $(sh_quote "$branch") 2>/dev/null || true
    " || die "could not check out '$branch' in '$name'"

    info "'$name' is on $branch ($repo, from $remote)"
    # --no-pager is not cosmetic: the driver's exec gives the command a
    # terminal, so `git log` would start a pager and wait for a keystroke
    # that never comes -- indistinguishable from a fetch that will not finish.
    log  "  $(t_exec "$name" bash -c "cd $(sh_quote "$src") && git --no-pager log --oneline -1" 2>/dev/null | tr -d '\r')"
}

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
    # Created here, not by `wk bench`: bind-mounted at container creation,
    # and podman refuses to start a container whose mount source is missing.
    ensure_dir "$WK_STORE/bench"
    ensure_dir "$WK_STORE/skills"
    ensure_dir "$WK_STORE/secrets" 0700
}

base_path() { echo "$(wk_base_dir)/$1/WebKit"; }

# --- the snapshot completion marker -----------------------------------------
# `wk sync` publishes by hardlinking the previous snapshot, fetching into
# it, and checking it out; killed anywhere in the middle it leaves a
# directory newer than every good one, so `current_base` cannot simply be
# `ls | tail -1` -- that pins the rubble to the next `wk new`.
# The recorded sha is written last and is the publication gate: present
# means published, absent means still being made or killed mid-make, and
# every reader ignores an absent one. It is also tamper evidence -- a
# by-hand fetch or checkout inside a published snapshot moves HEAD away
# from the recorded sha, and a mismatched snapshot is refused by name
# rather than silently handed to a workspace.
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

# The snapshot a new workspace gets: the newest *published* one. Ids sort
# lexically since they are UTC timestamps, but only complete ones are
# candidates -- what makes an interrupted sync invisible rather than the
# freshest thing here.
current_base() {
    local d
    for d in $(ls -1 "$(wk_base_dir)" 2>/dev/null | sort -r); do
        base_complete "$d" || continue
        echo "$d"
        return 0
    done
    return 1
}

# The refcount that keeps `wk gc` from deleting a snapshot still in use.
ws_base_id() {
    local f="$(wk_ws_dir "$1")/base-id"
    [ -f "$f" ] && cat "$f" || return 1
}

list_workspaces() {
    [ -d "$WK_STORE/ws" ] || return 0
    ls -1 "$WK_STORE/ws" 2>/dev/null || true
}

# Workspaces that exist without having recorded which snapshot they pin
# (creation in flight, or rubble left by one that was killed) -- a pin not
# yet written is still a reference, and one nothing can count.
unpinned_workspaces() {
    local ws
    for ws in $(list_workspaces); do
        ws_base_id "$ws" >/dev/null 2>&1 || echo "$ws"
    done
}

# Snapshots with no workspace pinning them, which `wk gc` prunes. Nothing
# at all while any workspace is unpinned: an uncountable reference makes
# "unreferenced" a guess, and acting on it deletes a live overlay's lower
# layer. One answer here, not a guard in `wk gc`, so `wk ls` never reports
# as reclaimable what gc will refuse to touch.
unreferenced_bases() {
    local base used ws id
    [ -z "$(unpinned_workspaces)" ] || return 0
    used=""
    for ws in $(list_workspaces); do
        id=$(ws_base_id "$ws" 2>/dev/null) || continue
        used="$used $id"
    done

    # The newest published snapshot is referenced by policy even unpinned:
    # it is what the next `wk new` gets, and re-fetching it costs minutes.
    local keep; keep=$(current_base 2>/dev/null || true)

    for base in $(ls -1 "$(wk_base_dir)" 2>/dev/null); do
        [ "$base" = "$keep" ] && continue
        case " $used " in
            *" $base "*) continue ;;
        esac
        echo "$base"
    done
}

# --- the agents' credentials --------------------------------------------------
# One secret per name per machine, in the store, serving every workspace on this
# machine. A container reads them live through the read-only /secrets mount, so
# rotating one takes effect in every container at once; a macOS guest and a
# shared build box are handed a copy when their workspace comes up, since
# neither can mount it.
#
# They are credentials, not facts about the machine, so a stored copy is the
# point rather than a smell -- the same trade `wk key tailnet` and the deploy
# keys make. `wk key set <name>` is what puts one here.
#
# The names are a closed set because each one is delivered and read *by name*:
# container/firstrun.sh links every row into the workspace, shell/bashrc exports
# each into the variable its agent reads, and cmd/key asks for it with wording
# of its own. A row with no reader in those files is a secret nothing can use,
# which is why this table is the one authority and tests/test_pi_agent.py binds
# the readers back to it.
#
#   <name> <file under secrets/> <file in the workspace's home> <the variable>
wk_agent_secrets() {
    cat <<'EOF'
claude   claude-token  .wk-agent-token  CLAUDE_CODE_OAUTH_TOKEN
litellm  litellm-key   .wk-litellm-key  LITELLM_API_KEY
EOF
}

wk_agent_secret_names() { wk_agent_secrets | awk 'NF { print $1 }'; }

# One field of one row, or nothing at all for a name that is not in the table.
# `wk key set` refuses on the empty answer rather than each caller re-deciding
# what the valid names are.
wk_agent_secret_field() { # <name> <column>
    wk_agent_secrets | awk -v n="$1" -v c="$2" '$1 == n { print $c; exit }'
}
wk_agent_secret_known() { [ -n "$(wk_agent_secret_field "$1" 1)" ]; }

# wk_machine_store, not $WK_STORE: there is one set of these per *machine*, and
# a loaded target driver may have pointed $WK_STORE at its own state (targets/
# vm.sh does, at the host's XDG state directory). Reading them from there would
# look in a directory `wk key set` never writes to and find nothing, in the one
# command -- `wk vm start` -- that most needs to find them.
wk_agent_secret_path() { # <name>
    local f; f=$(wk_agent_secret_field "$1" 2)
    [ -n "$f" ] || return 1
    printf '%s/secrets/%s' "$(wk_machine_store)" "$f"
}

# The secret itself, or nothing -- its first line, since a credential is one
# line and a trailing newline in the file is not part of it. Absent is not an
# error: a workspace with no Claude token asks the person to /login, which
# still works, and one with no LiteLLM key has an agent that cannot reach that
# endpoint.
wk_agent_secret() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 0
    _wk_secret_read "$p" | sed -n '1p'
}

# Store it, reading the value from stdin so it is never an argument -- an
# argument is visible in `ps` to everyone on the machine. Same two cases as
# the read: here, or forwarded into the VM that holds the store.
wk_agent_secret_store() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 1
    if wk_push_store_local; then
        mkdir -p "$(dirname "$p")" || return 1
        ( umask 077; cat > "$p" ) || return 1
        chmod 0600 "$p" 2>/dev/null || true
    else
        podman machine ssh "${WK_MACHINE:-wk}" -- \
            "mkdir -p $(sh_quote "$(dirname "$p")") && umask 077 && cat > $(sh_quote "$p") && chmod 0600 $(sh_quote "$p")" \
            || return 1
    fi
}

# Withdraw it. Every workspace loses it on its next start -- a container
# immediately, since its link points at this file.
wk_agent_secret_clear() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 1
    if wk_push_store_local; then
        rm -f "$p"
    else
        podman machine ssh "${WK_MACHINE:-wk}" -- "rm -f $(sh_quote "$p")" </dev/null
    fi
}
