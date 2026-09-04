# Workspace storage: a base-snapshot scheme built on a bare mirror.
# git/WebKit.git is the only thing ever fetched into; base/<id>/WebKit are
# immutable snapshots a workspace pins for life, and ws/<name>/changes is that
# workspace's copy-on-write layer. Only the mirror is fetched into because a
# workspace's tree is the lower layer of a live overlay mount, and the kernel
# says changes underneath a mounted overlay are undefined.

# /var/lib/wk inside the macOS VM; elsewhere the user's own data directory,
# since a store that needs root to create needs root to repair.
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

# On a macOS workstation $WK_STORE is the podman VM's /var/lib/wk, which the
# Mac can neither create nor read, so such a command has to be forwarded.
store_is_local() {
    [ -n "${WK_IN_VM:-}" ] && return 0
    [ "$(uname -s)" != Darwin ] && return 0
    # A Mac with a real /var/lib/wk it owns is possible and is still local.
    [ -d "$WK_STORE" ] && [ -w "$WK_STORE" ]
}


# ccache ceiling, shared by every workspace: several full debug builds.
# WK_CCACHE_MAXSIZE overrides the ceiling, for a store on a smaller disk.
WK_CCACHE_MAXSIZE="${WK_CCACHE_MAXSIZE:-40G}"

# ccache's own default is 5 GB, which a couple of WebKit builds blow through,
# so the real limit goes in each cache's config too, or `ccache -s` says 5 GB.
ccache_conf_render() { printf 'max_size = %s\n' "$WK_CCACHE_MAXSIZE"; }
ccache_conf_write() { # <path to ccache.conf>
    [ -f "$1" ] || ccache_conf_render > "$1"
}

wk_mirror()   { echo "$WK_STORE/git/WebKit.git"; }
wk_base_dir() { echo "$WK_STORE/base"; }
wk_ws_dir()   { echo "$WK_STORE/ws/$1"; }

# Upstreams kept in the single mirror. All HTTPS, forks included: fetching is
# anonymous, so the mirror needs no credential. `igalia` is absent because the
# egress allowlist permits igalia.com on 80/443 only, not gitlab's ssh 4429.
wk_remotes() {
    cat <<'EOF'
origin   https://github.com/WebKit/WebKit.git
wpe      https://github.com/WebPlatformForEmbedded/WPEWebKit.git
fork     https://github.com/justinmichaud/WebKit.git
forkwpe  https://github.com/justinmichaud/WPEWebKit.git
EOF
}

# Repositories a PR branch can live in: `wk pr <user>:<branch>` tries each.
wk_pr_repos() {
    wk_remotes | awk '{ print $2 }' \
        | sed -E 's#(\.git)?$##; s#.*/##' \
        | awk '!seen[$0]++'
}

# <remote> <owner/repo> <ssh-host-alias>. GitHub scopes a deploy key to one
# repository, and both forks are on github.com, so the key is picked by alias.
wk_push_forks() {
    cat <<'EOF'
fork     justinmichaud/WebKit      github-webkit
forkwpe  justinmichaud/WPEWebKit   github-wpe
EOF
}

# The remote wiring every WebKit checkout gets, as a portable `sh` snippet: the
# checkout is often not on this machine.
#   wk_wiring_script <src> [<extra-remote-name> <extra-remote-url> [<ssh-config>]]
# The extra remote is a local, fetch-only copy of the same history; the ssh
# config is for a machine whose aliases cannot live in ~/.ssh/config.
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
    # The other upstreams (`wpe`), fetch-only: we always push to a fork.
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

# The same wiring as a *check*: a workspace made before the wiring existed keeps
# whatever `git clone` left it. One `problem: ...` line per fault, exit 1 if any.
# A non-empty second argument skips the environment checks (can ssh resolve a
# fork's alias from here), which a base snapshot in the podman VM needs none of.
#   wk_wiring_check_script <src> [<skip-env>]
wk_wiring_check_script() {
    local src="$1" skip_env="${2:-}"
    printf 'cd %s || exit 2
' "$(sh_quote "$src")"
    printf 'bad=0
'
    # origin is fetch-only: a local path here is a stale mirror `git log
    # origin/main` silently answers for.
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
    printf 'if [ -n "$u" ]; then case "$p" in
  no-push://*) ;;
  *) echo "problem: origin accepts a push ($p) -- there is no write access to upstream, and this is how a push goes to the wrong repository"; bad=1 ;;
esac
fi
'
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
        # The deploy key is selected by the ssh host alias, so an https push URL means
        # git never asks ssh, never offers a key, and `wk push on` cannot help.
        printf 'if [ -n "$u" ]; then case "$p" in
  git@%s:%s.git) ;;
  *) echo "problem: %s pushes to $p, not git@%s:%s.git -- the deploy key is chosen by that ssh alias, so no key is offered at all"; bad=1 ;;
esac
fi
' "$alias" "$repo" "$remote" "$alias" "$repo"
        # ...and the alias has to resolve, in ~/.ssh/config or via core.sshCommand.
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
    # Which repository the *branch* points at: a working branch tracking origin/<x>
    # can never be pushed by a bare `git push`. Upstream branches are the exception.
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
    # One arm per upstream: a WPEWebKit branch belongs to forkwpe, not fork.
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

# Point the current branch at the fork it can actually be pushed to -- the only
# part of the wiring that touches a *branch*. `git branch -u` refuses an
# upstream whose remote-tracking ref it has never seen, hence the fetch.
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

# The ssh aliases that select a deploy key per fork, as an ssh_config body: one
# key per fork and both on github.com, so an alias per fork is the only way ssh
# will offer the right one.
#   wk_ssh_alias_blocks <dir> <prefix> <suffix> <agent-sock> [<ProxyCommand>]
# The identity is <dir>/<prefix><remote><suffix>: with an agent socket, the
# *public* half, `IdentitiesOnly` keeping ssh to that one while the agent
# outside signs; without one, the private half and no IdentityAgent line. The
# ProxyCommand is for a machine whose only route to github.com is an HTTP
# CONNECT proxy; a container leaves it empty, its `Host *` block already
# carrying one and ssh taking the first value it sees for a keyword.
wk_ssh_alias_blocks() {
    local dir="$1" prefix="${2:-build_key_}" suffix="${3:-}" agent="${4:-}" proxy="${5:-}"
    wk_push_forks | while read -r remote repo alias; do
        [ -n "$remote" ] || continue
        cat <<EOF

Host $alias
    HostName github.com
    User git
    IdentityFile $dir/$prefix$remote$suffix
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
        [ -z "$agent" ] || printf '    IdentityAgent %s\n' "$agent"
        [ -z "$proxy" ] || printf '    ProxyCommand %s\n' "$proxy"
    done
}

# --- delivering a deploy key to a target that cannot mount the store ---------
# The switch `wk push` throws is *where the key is*. secrets/ is a read-only
# mount the container only links into, so moving the key moves it for every
# container at once; a macOS guest mounts nothing of ours and holds a copy.

# A loaded target driver may point $WK_STORE at its own state; load_target
# records the machine's own store in WK_STORE_DEFAULT first.
wk_machine_store() { printf '%s' "${WK_STORE_DEFAULT:-$WK_STORE}"; }

# Two spellings, one set of bytes: on macOS the credentials are on the host and
# mounted into the podman machine at $WK_STORE/secrets.
wk_secrets_dir() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        wk_host_secrets
    else
        printf '%s/secrets' "$(wk_machine_store)"
    fi
}

# Beside the secrets directory and never inside it, since that one is mounted
# into every workspace. `wk push` moves the agent's contents, not a file.
wk_push_held_dir() { printf '%s/push-keys' "$(dirname "$(wk_secrets_dir)")"; }

# One fork's private key, read only by push_agent_load, which streams it into
# `ssh-add -`: never logged, never an argument, never on another machine.
wk_push_key() { # <fork>
    _wk_secret_read "$(wk_push_held_dir)/build_key_$1"
}

# One reader for everything in the secrets directory; absent is not an error.
# Through lib/secretfile.py, which holds the rule "this is a file, and it is
# ours": `agent-rw` is mounted read-write into every container and is a sibling
# of the directory holding the private deploy keys, so a link planted in the
# writable one would turn this into a read of a publishing credential.
_wk_secret_read() { # <path>
    python3 "$WK_ROOT/lib/secretfile.py" read "$1"
}

# --- the switch: two credentials, neither ever inside a workspace ------------
# `wk push on|off` throws one switch over both things a publish needs:
#
#   ssh    an ssh-agent on the machine that runs the workspaces holds the
#          private halves. A container gets one unix socket and a guest gets it
#          forwarded; the key bytes stay in wk_push_held_dir on this machine.
#   api    the injector that terminates TLS for api.github.com reads the token
#          from a file on that same machine and puts it in the Authorization
#          header. The workspace holds a placeholder.
#
# Every function below takes an *exec function* -- one shell command line run on
# the machine holding the agent, stdin passed through -- and a path as that
# machine spells it. There is no state file: `ssh-add -l` is the evidence.

# The socket the machine running the containers keeps its agent on: `%t/wk` in
# wk-ssh-agent.service, the directory every container bind-mounts at /run/wk. A
# shell word for the far side to expand: /run/user/501 is not on macOS.
push_agent_machine_sock() {
    # WK_PUSH_AGENT_SOCK: tests point this at an ssh-agent of their own.
    if [ -n "${WK_PUSH_AGENT_SOCK:-}" ]; then
        printf '%s' "$WK_PUSH_AGENT_SOCK"
    else
        printf '%s' '${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/ssh-agent.sock'
    fi
}

# The file the injector reads the token from, on that same machine. Under the
# store root and not $XDG_RUNTIME_DIR/wk: that directory is the one containers
# bind-mount. A resolved path, unlike the socket above, since $WK_STORE is
# already that machine's spelling and this reaches `wk push status` as text.
push_agent_machine_pat() {
    # WK_PUSH_PAT_FILE: tests point this at a file of their own.
    printf '%s' "${WK_PUSH_PAT_FILE:-${WK_STORE:-/var/lib/wk}/push-github-pat}"
}

# The standing read token, beside it on the same machine. The switch is over
# writing, so `wk push` never touches this one: push_agent_pat_sync does.
push_agent_machine_read_pat() {
    # WK_PUSH_READ_PAT_FILE: tests point this at a file of their own.
    printf '%s' "${WK_PUSH_READ_PAT_FILE:-${WK_STORE:-/var/lib/wk}/read-github-pat}"
}

push_agent_exec() { # <shell command line>
    if store_is_local; then
        sh -c "$1"
    else
        podman machine ssh "${WK_MACHINE:-wk}" -- "$1"
    fi
}

# `ssh-add -l` exits 1 for "the agent has no identities" and 2 for "could not
# open a connection" -- off versus no switch, which must not be conflated.
push_agent_ensure() { # <execfn> <sock>
    local rc
    rc=$("$1" "SSH_AUTH_SOCK=$2 ssh-add -l >/dev/null 2>&1; echo \$?" \
             </dev/null 2>/dev/null | tr -dc '0-9')
    [ "$rc" = 0 ] || [ "$rc" = 1 ]
}

# What the agent holds, one `ssh-add -l` line each -- public information.
push_agent_list() { # <execfn> <sock>
    local out
    out=$("$1" "SSH_AUTH_SOCK=$2 ssh-add -l 2>/dev/null" </dev/null 2>/dev/null) || out=""
    printf '%s' "$out" | tr -d '\r' | grep -v 'has no identities' || true
}

# One `<fork> loaded|no-key|FAILED` line each. `ssh-add -` reads the key from
# stdin: the bytes are never an argument (`ps` shows those to everyone).
push_agent_load() { # <execfn> <sock>
    local execfn="$1" sock="$2" remote key
    for remote in $(wk_push_forks | awk 'NF {print $1}'); do
        key=$(wk_push_key "$remote")
        if [ -z "$key" ]; then
            printf '%s no-key\n' "$remote"
            continue
        fi
        # The trailing newline the command substitution stripped: ssh-add
        # refuses a key that does not end in one.
        if printf '%s\n' "$key" \
            | "$execfn" "SSH_AUTH_SOCK=$sock ssh-add - >/dev/null 2>&1"; then
            printf '%s loaded\n' "$remote"
        else
            printf '%s FAILED\n' "$remote"
        fi
    done
}

# `ssh-add -D` drops every identity at once, including one added by hand.
push_agent_clear() { # <execfn> <sock>
    "$1" "SSH_AUTH_SOCK=$2 ssh-add -D >/dev/null 2>&1" </dev/null
}

# In the same never-mounted directory as the private key halves.
wk_github_pat_path() { printf '%s/github-pat' "$(wk_push_held_dir)"; }
wk_github_pat() { _wk_secret_read "$(wk_github_pat_path)" | sed -n '1p'; }

# The account the injector authenticates as, taken from the fork this machine
# pushes to: a username kept separately could only disagree with the token.
wk_github_user() { wk_push_forks | awk 'NF {print $2; exit}' | cut -d/ -f1; }

# The api half: the token goes to the file the injector reads, 0600 and on
# stdin, or the file is removed -- the injector answers 401 on a missing file.
push_agent_pat_write() { # <execfn> <path>
    local pat; pat=$(wk_github_pat)
    [ -n "$pat" ] || return 1
    printf '%s\n' "$pat" | "$1" "umask 077 && cat > $(sh_quote "$2")"
}

push_agent_pat_clear() { # <execfn> <path>
    "$1" "rm -f $(sh_quote "$2")" </dev/null
}

# Write-or-clear: a token withdrawn on this device has to vanish from the
# machine on the next converging call, so absent is a removal and not a skip.
push_agent_pat_sync() { # <execfn> <path>
    if [ -n "$(wk_github_pat)" ]; then
        push_agent_pat_write "$1" "$2"
    else
        push_agent_pat_clear "$1" "$2"
    fi
}

push_agent_pat_present() { # <execfn> <path>
    local out
    out=$("$1" "test -s $(sh_quote "$2") && echo yes" </dev/null 2>/dev/null) || out=""
    [ "$out" = yes ]
}

# What a workspace needs in order to *use* the switch: the ssh config every
# container includes (`Include /secrets/ssh_config`) and the account name the
# injector authenticates as. Neither is a credential, so both are written
# whichever way the switch is set, and regenerated by `wk push on|off` so a
# rotated key reaches every existing workspace at once. <dir> is this machine's
# spelling; the paths inside the file are the container's, /secrets.
push_agent_publish_config() { # <dir> <agent-sock>
    local dir="$1" sock="$2"
    ensure_dir "$dir" 0700 >/dev/null
    { printf '%s\n' "# wk: written by 'wk push on|off' (push_agent_publish_config," \
                    "# lib/store.sh). One alias per fork, because GitHub takes one deploy" \
                    "# key per repository and both forks live on github.com. The identity is" \
                    "# a public half; the private one is in an ssh-agent outside this" \
                    "# workspace, and whether it is loaded there is what 'wk push' switches."
      wk_ssh_alias_blocks /secrets build_key_ .pub "$sock"
    } > "$dir/ssh_config.new" || return 1
    chmod 0644 "$dir/ssh_config.new"
    mv "$dir/ssh_config.new" "$dir/ssh_config"

    # Read by container/proxy/ensure-bridge.sh into $GITHUB_COM_USERNAME. A file
    # rather than a `bash -c` into lib/store.sh, which would run on every
    # single command a workspace executes.
    printf '%s\n' "$(wk_github_user)" > "$dir/github-user.new" || return 1
    chmod 0644 "$dir/github-user.new"
    mv "$dir/github-user.new" "$dir/github-user"
}

# WebKit/WebKit has ~920 branches and mirroring all of them costs tens of
# gigabytes, so the mirror carries only main. WK_MIRROR_BRANCHES carries more.
wk_mirror_branches() {
    echo "${WK_MIRROR_BRANCHES:-main}"
}

# What a plain `wk sync` fetches: all of them, board images being built from
# the WPE release branches and both forks holding this fleet's own work.
wk_mirror_default_remotes() { wk_remotes | awk 'NF {printf "%s%s", sep, $1; sep=" "} END {print ""}'; }

# --- refreshing a mirror, wherever it is ------------------------------------
# Every mirror in the fleet is made and fetched by this one snippet: the one
# beside the snapshot here, the one on a build box and the one in a macOS guest.
# Portable `sh` because two of the three are on the far side of an ssh -- and
# because a workspace fetching from one must find the layout every other has
# (mirror_refspecs, cmd/sync): origin's branches as the mirror's OWN refs/heads,
# every other upstream under refs/remotes/<remote>/. --no-tags: a fetch follows
# every reachable tag unless told not to. gc.auto 0: workspaces borrow these
# objects through `--shared` clones, and a repack underneath one breaks it.
mirror_refresh_script() { # <mirror-dir>
    local name url b
    printf 'set -e\nM=%s\n' "$(sh_quote "$1")"
    printf 'if [ ! -d "$M" ]; then\n'
    printf '    git init --bare -q "$M"\n'
    printf '    git -C "$M" config gc.auto 0\n'
    printf 'fi\n'
    wk_remotes | while read -r name url; do
        [ -n "$name" ] || continue
        printf 'git -C "$M" remote set-url %s %s 2>/dev/null || git -C "$M" remote add %s %s\n' \
            "$name" "$(sh_quote "$url")" "$name" "$(sh_quote "$url")"
        printf 'git -C "$M" config remote.%s.tagOpt --no-tags\n' "$name"
        if [ "$name" = origin ]; then
            printf 'git -C "$M" config --unset-all remote.origin.fetch 2>/dev/null || true\n'
            for b in $(wk_mirror_branches); do
                printf 'git -C "$M" config --add remote.origin.fetch %s\n' \
                    "$(sh_quote "+refs/heads/$b:refs/heads/$b")"
            done
        else
            printf 'git -C "$M" config --replace-all remote.%s.fetch %s\n' \
                "$name" "$(sh_quote "+refs/heads/*:refs/remotes/$name/*")"
        fi
    done
    # One fetch per remote rather than `remote update`, so one unreachable
    # upstream leaves the others fetched. --prune, or a deleted branch stays.
    printf 'for r in %s; do\n' "$(wk_mirror_default_remotes)"
    printf '    if git -C "$M" fetch --prune -q "$r" 2>/dev/null; then\n'
    printf '        echo "mirror-fetch $r ok"\n'
    printf '    else echo "mirror-fetch $r FAILED"\n    fi\n'
    printf 'done\n'
    # After the fetches, not before: `git fetch` in a bare repository overwrites
    # HEAD with that remote's default branch, valid local HEAD or not and
    # `fetch.followRemoteHEAD=never` or not (measured, git 2.48.1). Left as the
    # last remote fetched, HEAD names a branch only that upstream has, and
    # cloning the mirror warns "remote HEAD refers to nonexistent ref" and
    # checks out nothing.
    # TODO: upstream -- report the bare-repository HEAD overwrite to git.
    printf 'git -C "$M" symbolic-ref HEAD %s\n' \
        "$(sh_quote "refs/heads/$(wk_mirror_branches | awk '{print $1}')")"
}

# `git fetch origin <branch>`, mirror first: a branch the target's mirror
# carries costs no network, and one it does not is asked of origin. The mirror's
# own refs/heads are origin's branches. Empty <mirror> asks origin for all.
origin_branch_fetch_step() { # <branch> <mirror-dir>
    local branch="$1" mirror="$2"
    local net; net="git fetch -q origin $(sh_quote "$branch")"
    if [ -z "$mirror" ]; then
        printf '%s' "$net"
        return 0
    fi
    printf 'if [ -d %s ] && git -C %s rev-parse --verify --quiet %s >/dev/null 2>&1
        then git fetch -q %s %s
        else %s
        fi' \
        "$(sh_quote "$mirror")" "$(sh_quote "$mirror")" \
        "$(sh_quote "refs/heads/$branch")" \
        "$(sh_quote "$mirror")" \
        "$(sh_quote "+refs/heads/$branch:refs/remotes/origin/$branch")" \
        "$net"
}

# --- PR / pull-request refs, fetched once into the mirror -------------------
# `wk pr` and `wk new --pr` want one ref without a fetch per re-run. Fetched
# under refs/remotes/pr/... so it is never taken for one of the mirror's own.

wk_pr_refname()   { printf '%s/%s/%s' "$1" "$2" "$3"; }  # <user> <repo> <branch>
wk_pull_refname() { printf '%s/%s' "$1" "$2"; }          # <remote> <n>

# Under the store lock: this mutates the same bare mirror a publish does.
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

# Only for a workspace that reads this machine's mirror; a fetch that fails is
# a refusal, not a detour to GitHub.
_mirror_fetch_into() {  # <src-url-or-remote> <src-refspec> <dest-ref>
    local src="$1" srcspec="$2" dest="$3"
    store_is_local || die "the mirror is not this machine's (\$WK_STORE is $WK_STORE); a workspace here cannot read it"
    store_init
    with_lock store -- _mirror_fetch_do "$srcspec" "$dest" "$src"
}

mirror_fetch_pr() {  # <url> <branch> <refname>
    _mirror_fetch_into "$1" "refs/heads/$2" "refs/remotes/pr/$3"
}

# No fork to discover first: GitHub serves every PR's head under the base repo.
mirror_fetch_pull() {  # <remote> <n>
    local remote="$1" n="$2" url
    url=$(wk_remotes | awk -v r="$remote" '$1 == r {print $2; exit}')
    [ -n "$url" ] || die "no such upstream remote '$remote' to fetch a pull request from"
    _mirror_fetch_into "$url" "refs/pull/$n/head" "refs/remotes/pr/$(wk_pull_refname "$remote" "$n")"
}

# --- the spec, parsed once ---------------------------------------------------
#   user:branch   a fork's branch, found by asking each of wk_pr_repos in turn
#   <n>           WebKit/WebKit pull request #n (refs/pull/<n>/head) -- no fork
#                 discovery, GitHub serves the head under the base repo
#   wpe:<n>       WPEWebKit's pull request #n, the same way
# Sets PR_KIND (user|pull) and either PR_USER/PR_BRANCH or PR_REMOTE/PR_N.
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
# Resolve <spec>, fetch it into the mirror once, and check it out from there,
# falling back to GitHub when the mirror is not reachable. WK_FORCE takes the PR
# head even when a local branch of the same name has commits it does not.
wk_pr_checkout() {  # <name> <spec>
    local name="$1" spec="$2"
    local src repo url branch remote head_sha local_sha dirty reset ahead
    local probe found n mirror_ok mirror_dir mirror_ref add_remote="" src_ref net_refspec fetch_step

    pr_parse_spec "$spec"
    src=$(t_src "$name")
    mirror_dir=$(t_mirror_dir "$name")
    # Whether this run can put the PR ref where the workspace will read it,
    # which is not the same as the target having a mirror: a container's is this
    # machine's own, a guest's and a build box's are on the far side.
    mirror_ok=""
    [ "${WK_TARGET_KIND:-}" = container ] && store_is_local && mirror_ok=1

    case "$PR_KIND" in
    user)
        # --- which project, from an anonymous ls-remote per candidate repo --
        # `git ls-remote` over HTTPS is anonymous, like a fetch with push off.
        probe=$(t_exec "$name" bash -c "
            cd $(sh_quote "$src") || exit 1
            for r in $(wk_pr_repos | tr '\n' ' '); do
                u=https://github.com/$(sh_quote "$PR_USER")/\$r.git
                sha=\$(git ls-remote \"\$u\" refs/heads/$(sh_quote "$PR_BRANCH") 2>/dev/null | awk '{print \$1}')
                [ -n \"\$sha\" ] && echo \"found=\$r \$u \$sha\"
            done
            # A remote already pointing at this user's copy, so a second one
            # is not added beside it under another name.
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

        # The project is appended when it is not origin's: two remotes cannot
        # share a name.
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
        # Commits the PR head does not have; none means simply behind.
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
        # A workspace whose mirror is not mounted after all falls through.
        fetch_step="if [ -d $(sh_quote "$mirror_dir") ] && git -C $(sh_quote "$mirror_dir") rev-parse --verify --quiet $(sh_quote "$mirror_ref") >/dev/null 2>&1
        then git fetch --quiet $(sh_quote "$mirror_dir") $(sh_quote "$mirror_ref:refs/remotes/$remote/$branch")
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
    # terminal, so `git log` would start a pager and wait for a keystroke.
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
    # This machine's own credentials on Linux; inside the podman machine both
    # are the host's, mounted. Bind-mounted at container creation, like bench/.
    ensure_dir "$(wk_secrets_dir)" 0700
    ensure_dir "$(wk_agent_rw_dir)" 0700
}

base_path() { echo "$(wk_base_dir)/$1/WebKit"; }

# --- the snapshot completion marker -----------------------------------------
# `wk sync` publishes by hardlinking the previous snapshot, fetching into it and
# checking it out; killed in the middle it leaves a directory newer than every
# good one, so `current_base` cannot be `ls | tail -1`. The recorded sha is
# written last and is the gate -- and is tamper evidence too.
base_sha_file() { echo "$(wk_base_dir)/$1/sha"; }

base_complete() { [ -s "$(base_sha_file "$1")" ]; }

base_recorded_sha() { cat "$(base_sha_file "$1")" 2>/dev/null || true; }
base_tree_sha()     { git -C "$(base_path "$1")" rev-parse HEAD 2>/dev/null || true; }

# base_verify <id> -- 0 if publishable and untampered; prints why it is not.
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

# The newest *published* snapshot. Ids sort lexically, being UTC timestamps.
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

# Workspaces that have not recorded which snapshot they pin: still a reference,
# and one nothing can count.
unpinned_workspaces() {
    local ws
    for ws in $(list_workspaces); do
        ws_base_id "$ws" >/dev/null 2>&1 || echo "$ws"
    done
}

# Snapshots no workspace pins, which `wk gc` prunes. Nothing at all while any
# workspace is unpinned: acting on a guess deletes a live overlay's lower layer.
unreferenced_bases() {
    local base used ws id
    [ -z "$(unpinned_workspaces)" ] || return 0
    used=""
    for ws in $(list_workspaces); do
        id=$(ws_base_id "$ws" 2>/dev/null) || continue
        used="$used $id"
    done

    # The newest published snapshot is referenced by policy even unpinned.
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
# One secret per name per machine, serving every workspace here. A container
# reads them live through a mount, so rotating one takes effect in every
# container at once; a macOS guest and a shared build box are handed a copy of
# each *value* row when their workspace comes up, neither being able to mount
# anything of ours. Neither is handed a file row. `wk key set <name>` puts one
# here.
#
# The names are a closed set because each is delivered and read *by name*:
# container/firstrun.sh links every value row into the workspace, shell/bashrc
# exports each into the variable its agent reads, and cmd/key asks for it with
# wording of its own. tests/test_pi_agent.py binds those readers back to here.
#
#   value  one line, in the read-only secrets directory, exported by
#          shell/bashrc into the named variable.
#   file   a file the agent's own tool *rewrites in place*, in the writable
#          directory (wk_agent_rw_dir below); the variable column is `-`.
#          Delivered only where the workspace sees the same bytes this machine
#          does -- a copy's first rewrite invalidates the original -- so a
#          container gets the mount and nothing else gets it at all; the home
#          column is then the path a target *removes*.
#
#   <name> <file in its directory> <file in the workspace's home> <the variable> <kind>
wk_agent_secrets() {
    cat <<'EOF'
claude        claude-token        .wk-agent-token             CLAUDE_CODE_OAUTH_TOKEN  value
litellm       litellm-key         .wk-litellm-key             LITELLM_API_KEY          value
claude-login  .credentials.json   .claude/.credentials.json   -                        file
EOF
}

# The one directory a workspace may write: the Claude CLI rotates the refresh
# token in its credentials file, so every holder on a machine must share one set
# of bytes and one `.storage-write` lock.
wk_agent_rw_dir() { printf '%s/agent-rw' "$(dirname "$(wk_secrets_dir)")"; }

wk_agent_secret_names() { wk_agent_secrets | awk 'NF { print $1 }'; }

# Nothing for a name not in the table, so `wk key set` refuses on that.
wk_agent_secret_field() { # <name> <column>
    wk_agent_secrets | awk -v n="$1" -v c="$2" '$1 == n { print $c; exit }'
}
wk_agent_secret_known() { [ -n "$(wk_agent_secret_field "$1" 1)" ]; }
wk_agent_secret_kind() { wk_agent_secret_field "$1" 5; }

# wk_secrets_dir and wk_agent_rw_dir, not $WK_STORE: there is one set per
# *machine*, and a driver may have pointed $WK_STORE where `wk key set` never
# writes. The row's kind picks which of the two directories.
wk_agent_secret_path() { # <name>
    local f; f=$(wk_agent_secret_field "$1" 2)
    [ -n "$f" ] || return 1
    if [ "$(wk_agent_secret_kind "$1")" = file ]; then
        printf '%s/%s' "$(wk_agent_rw_dir)" "$f"
    else
        printf '%s/%s' "$(wk_secrets_dir)" "$f"
    fi
}

# Its first line: a value credential is one line. A file row is not a value and
# is read whole, with wk_cred_read.
wk_agent_secret() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 0
    _wk_secret_read "$p" | sed -n '1p'
}

wk_agent_secret_present() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 1
    python3 "$WK_ROOT/lib/secretfile.py" present "$p"
}

# From stdin, so it is never an argument -- an argument is visible in `ps`.
wk_agent_secret_store() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 1
    ensure_dir "$(dirname "$p")" 0700
    python3 "$WK_ROOT/lib/secretfile.py" write "$p"
}

wk_agent_secret_clear() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 1
    rm -f "$p"
}

# --- what a credential is allowed to be ---------------------------------------
# lib/credcheck.py holds every rule; these four are the only way to ask it, and
# every reader asks at the moment it reports, so no verdict is written down.

# Where this machine keeps each credential the rules name.
wk_cred_path() { # <name>
    case "$1" in
        github-pat)  wk_github_pat_path ;;
        tailnet)     wk_tailscale_authkey_path ;;
        tailnet-api) wk_tailscale_api_path ;;
        *)           wk_agent_secret_path "$1" ;;
    esac
}

wk_cred_names() { python3 "$WK_ROOT/lib/credcheck.py" names; }

# Every byte of a stored credential, and the one way any of them is read: a link
# left in the writable directory would otherwise turn a read of the login into a
# read of the token beside the deploy keys. Nothing for one that is not there.
wk_cred_read() { # <name>
    _wk_secret_read "$(wk_cred_path "$1")"
}

# `<absent|ok|wide|bad|unverified><TAB><detail>`, always -- a path this cannot
# read is a verdict too, not a crash. `--stored` judges what this machine
# holds; without it the value comes on stdin, which is how one is judged before
# anything writes it.
wk_cred_check() { # <name> [--stored] [<credcheck args>...]
    local name="$1" repos value; shift
    repos=$(wk_push_forks | awk 'NF {printf "%s ", $2}')
    if [ "${1:-}" != --stored ]; then
        python3 "$WK_ROOT/lib/credcheck.py" check "$name" --repos "$repos" "$@"
        return
    fi
    shift
    if ! value=$(wk_cred_read "$name"); then
        printf 'bad\tthe file at %s could not be read; the refusal above says why\n' \
            "$(wk_cred_path "$name")"
        return 0
    fi
    printf '%s' "$value" \
        | python3 "$WK_ROOT/lib/credcheck.py" check "$name" \
              --repos "$repos" --path "$(wk_cred_path "$name")" "$@"
}

wk_cred_verdict() { printf '%s' "${1%%$'\t'*}"; }
wk_cred_detail()  { printf '%s' "${1#*$'\t'}"; }
