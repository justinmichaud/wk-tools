# git/WebKit.git is the only thing ever fetched into: base/<id>/WebKit is a live
# overlay's lower layer, and changes under a mounted overlay are undefined.
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

store_is_local() { # on macOS $WK_STORE is the VM's, and such a command is forwarded
    [ -n "${WK_IN_VM:-}" ] && return 0
    [ "$(uname -s)" != Darwin ] && return 0
    [ -d "$WK_STORE" ] && [ -w "$WK_STORE" ]
}


WK_CCACHE_MAXSIZE="${WK_CCACHE_MAXSIZE:-40G}"   # shared by every workspace here

ccache_conf_render() { printf 'max_size = %s\n' "$WK_CCACHE_MAXSIZE"; }
ccache_conf_write() { # <path to ccache.conf>
    [ -f "$1" ] || ccache_conf_render > "$1"
}

wk_mirror()   { echo "$WK_STORE/git/WebKit.git"; }
wk_base_dir() { echo "$WK_STORE/base"; }
wk_ws_dir()   { echo "$WK_STORE/ws/$1"; }

# `igalia` is absent: the egress allowlist permits it on 80/443, not ssh 4429.
wk_remotes() {
    cat <<'EOF'
origin   https://github.com/WebKit/WebKit.git
wpe      https://github.com/WebPlatformForEmbedded/WPEWebKit.git
fork     https://github.com/justinmichaud/WebKit.git
forkwpe  https://github.com/justinmichaud/WPEWebKit.git
EOF
}

wk_pr_repos() { # repositories `wk pr <user>:<branch>` tries in turn
    wk_remotes | awk '{ print $2 }' \
        | sed -E 's#(\.git)?$##; s#.*/##' \
        | awk '!seen[$0]++'
}

wk_push_forks() { # <remote> <owner/repo> <ssh-host-alias>
    cat <<'EOF'
fork     justinmichaud/WebKit      github-webkit
forkwpe  justinmichaud/WPEWebKit   github-wpe
EOF
}

wk_wiring_script() { # <src> [<extra-name> <extra-url> [<ssh-config>]]
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

wk_wiring_check_script() { # <src> [<skip-env>] -- a `problem:` line per fault, exit 1
    local src="$1" skip_env="${2:-}"
    printf 'cd %s || exit 2
' "$(sh_quote "$src")"
    printf 'bad=0
'
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
        printf 'if [ -n "$u" ]; then case "$p" in
  git@%s:%s.git) ;;
  *) echo "problem: %s pushes to $p, not git@%s:%s.git -- the deploy key is chosen by that ssh alias, so no key is offered at all"; bad=1 ;;
esac
fi
' "$alias" "$repo" "$remote" "$alias" "$repo"
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

wk_branch_upstream_fix_script() { # point HEAD at the fork it can be pushed to
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

# One deploy key per repository, both forks on github.com: only an alias per
# fork makes ssh offer the right one.
wk_ssh_alias_blocks() { # <dir> <prefix> <suffix> <agent-sock> [<ProxyCommand>]
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

wk_machine_store() { printf '%s' "${WK_STORE_DEFAULT:-$WK_STORE}"; }

wk_secrets_dir() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        wk_host_secrets
    else
        printf '%s/secrets' "$(wk_machine_store)"
    fi
}

wk_push_held_dir() { printf '%s/push-keys' "$(dirname "$(wk_secrets_dir)")"; }

wk_push_key() { # <fork> -- read only by push_agent_load, into `ssh-add -`
    _wk_secret_read "$(wk_push_held_dir)/build_key_$1"
}

# lib/secretfile.py holds the rule "this is a file, and it is ours": agent-rw is
# mounted read-write beside the deploy keys, so a link planted there could
# publish a credential.
_wk_secret_read() { # <path> -- absent is not an error
    python3 "$WK_ROOT/lib/secretfile.py" read "$1"
}

# One switch over both halves a publish needs: an ssh-agent holding the private
# keys, and the token file the api.github.com TLS injector reads. Each function
# below takes an exec function -- a command line run on the machine holding
# those, stdin passed through -- and a path as that machine spells it, so a
# shell word to expand there: /run/user/501 is not on macOS.
push_agent_machine_sock() {
    if [ -n "${WK_PUSH_AGENT_SOCK:-}" ]; then
        printf '%s' "$WK_PUSH_AGENT_SOCK"
    else
        printf '%s' '${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/ssh-agent.sock'
    fi
}

push_agent_machine_pat() {
    printf '%s' "${WK_PUSH_PAT_FILE:-${WK_STORE:-/var/lib/wk}/push-github-pat}"
}

push_agent_machine_read_pat() {
    printf '%s' "${WK_PUSH_READ_PAT_FILE:-${WK_STORE:-/var/lib/wk}/read-github-pat}"
}

push_agent_exec() { # <shell command line>
    if store_is_local; then
        sh -c "$1"
    else
        podman machine ssh "${WK_MACHINE:-wk}" -- "$1"
    fi
}

# `ssh-add -l` exits 1 for "no identities", 2 for "no connection".
push_agent_ensure() { # <execfn> <sock>
    local rc
    rc=$("$1" "SSH_AUTH_SOCK=$2 ssh-add -l >/dev/null 2>&1; echo \$?" \
             </dev/null 2>/dev/null | tr -dc '0-9')
    [ "$rc" = 0 ] || [ "$rc" = 1 ]
}

push_agent_list() { # <execfn> <sock>
    local out
    out=$("$1" "SSH_AUTH_SOCK=$2 ssh-add -l 2>/dev/null" </dev/null 2>/dev/null) || out=""
    printf '%s' "$out" | tr -d '\r' | grep -v 'has no identities' || true
}

push_agent_load() { # <execfn> <sock> -- one `<fork> loaded|no-key|FAILED` line each
    local execfn="$1" sock="$2" remote key
    for remote in $(wk_push_forks | awk 'NF {print $1}'); do
        key=$(wk_push_key "$remote")
        if [ -z "$key" ]; then
            printf '%s no-key\n' "$remote"
            continue
        fi
        # ssh-add refuses a key not ending in the newline `$( )` stripped.
        if printf '%s\n' "$key" \
            | "$execfn" "SSH_AUTH_SOCK=$sock ssh-add - >/dev/null 2>&1"; then
            printf '%s loaded\n' "$remote"
        else
            printf '%s FAILED\n' "$remote"
        fi
    done
}

push_agent_clear() { # <execfn> <sock>
    "$1" "SSH_AUTH_SOCK=$2 ssh-add -D >/dev/null 2>&1" </dev/null
}

wk_github_pat_path() { printf '%s/github-pat' "$(wk_push_held_dir)"; }
wk_github_pat() { _wk_secret_read "$(wk_github_pat_path)" | sed -n '1p'; }

wk_github_user() { wk_push_forks | awk 'NF {print $2; exit}' | cut -d/ -f1; }

push_agent_pat_write() { # <execfn> <path>
    local pat; pat=$(wk_github_pat)
    [ -n "$pat" ] || return 1
    printf '%s\n' "$pat" | "$1" "umask 077 && cat > $(sh_quote "$2")"
}

push_agent_pat_clear() { # <execfn> <path>
    "$1" "rm -f $(sh_quote "$2")" </dev/null
}

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

push_agent_publish_config() { # <dir> is this machine's spelling; paths inside are /secrets
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

    printf '%s\n' "$(wk_github_user)" > "$dir/github-user.new" || return 1
    chmod 0644 "$dir/github-user.new"
    mv "$dir/github-user.new" "$dir/github-user"
}

# WebKit/WebKit has ~920 branches, tens of gigabytes to mirror, so only main.
wk_mirror_branches() {
    echo "${WK_MIRROR_BRANCHES:-main}"
}

wk_mirror_default_remotes() { wk_remotes | awk 'NF {printf "%s%s", sep, $1; sep=" "} END {print ""}'; }

# The layout every mirror shares, so a workspace can fetch from any (see
# mirror_refspecs): origin's branches as the mirror's OWN refs/heads, the other
# upstreams under refs/remotes/<remote>/. gc.auto 0 or a repack breaks the
# `--shared` clones borrowing these objects.
mirror_refresh_script() { # <mirror-dir>, as portable `sh` -- two of three are remote
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
    printf 'for r in %s; do\n' "$(wk_mirror_default_remotes)"
    printf '    if git -C "$M" fetch --prune -q "$r" 2>/dev/null; then\n'
    printf '        echo "mirror-fetch $r ok"\n'
    printf '    else echo "mirror-fetch $r FAILED"\n    fi\n'
    printf 'done\n'
    # After the fetches: `git fetch` in a bare repository overwrites HEAD with
    # that remote's default branch, whatever fetch.followRemoteHEAD says
    # (measured, git 2.48.1).
    # TODO: upstream -- report the bare-repository HEAD overwrite to git.
    printf 'git -C "$M" symbolic-ref HEAD %s\n' \
        "$(sh_quote "refs/heads/$(wk_mirror_branches | awk '{print $1}')")"
}

origin_branch_fetch_step() { # <branch> <mirror-dir>; mirror first, empty asks origin
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

wk_pr_refname()   { printf '%s/%s/%s' "$1" "$2" "$3"; }  # <user> <repo> <branch>
wk_pull_refname() { printf '%s/%s' "$1" "$2"; }          # <remote> <n>

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

_mirror_fetch_into() {  # <src-url-or-remote> <src-refspec> <dest-ref>
    local src="$1" srcspec="$2" dest="$3"
    store_is_local || die "the mirror is not this machine's (\$WK_STORE is $WK_STORE); a workspace here cannot read it"
    store_init
    with_lock store -- _mirror_fetch_do "$srcspec" "$dest" "$src"
}

mirror_fetch_pr() {  # <url> <branch> <refname>
    _mirror_fetch_into "$1" "refs/heads/$2" "refs/remotes/pr/$3"
}

mirror_fetch_pull() {  # <remote> <n>
    local remote="$1" n="$2" url
    url=$(wk_remotes | awk -v r="$remote" '$1 == r {print $2; exit}')
    [ -n "$url" ] || die "no such upstream remote '$remote' to fetch a pull request from"
    _mirror_fetch_into "$url" "refs/pull/$n/head" "refs/remotes/pr/$(wk_pull_refname "$remote" "$n")"
}

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

wk_pr_checkout() {  # <name> <spec> -- fetch it into the mirror once, check it out
    local name="$1" spec="$2"
    local src repo url branch remote head_sha local_sha dirty reset ahead
    local probe found n mirror_ok mirror_dir mirror_ref add_remote="" src_ref net_refspec fetch_step

    pr_parse_spec "$spec"
    src=$(t_src "$name")
    mirror_dir=$(t_mirror_dir "$name")
    mirror_ok=""
    [ "${WK_TARGET_KIND:-}" = container ] && store_is_local && mirror_ok=1

    case "$PR_KIND" in
    user)
        probe=$(t_exec "$name" bash -c "
            cd $(sh_quote "$src") || exit 1
            for r in $(wk_pr_repos | tr '\n' ' '); do
                u=https://github.com/$(sh_quote "$PR_USER")/\$r.git
                sha=\$(git ls-remote \"\$u\" refs/heads/$(sh_quote "$PR_BRANCH") 2>/dev/null | awk '{print \$1}')
                [ -n \"\$sha\" ] && echo \"found=\$r \$u \$sha\"
            done
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

    if [ "${dirty:-0}" -gt 0 ] 2>/dev/null; then
        warn "'$name' has $dirty uncommitted change(s); the checkout carries them across"
    fi

    reset=""
    if [ -n "$local_sha" ] && [ "$local_sha" != "$head_sha" ]; then
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

    net_refspec="$src_ref:refs/remotes/$remote/$branch"
    fetch_step="git fetch --quiet $(sh_quote "$remote") $(sh_quote "$net_refspec")"
    if [ -n "$add_remote" ]; then
        fetch_step="git remote get-url $(sh_quote "$remote") >/dev/null 2>&1 || git remote add $(sh_quote "$remote") $(sh_quote "$url")
        git remote set-url $(sh_quote "$remote") $(sh_quote "$url")
        $fetch_step"
    fi
    if [ -n "$mirror_ok" ]; then
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
    # --no-pager: the exec gives this a terminal, so `git log` would page.
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
    ensure_dir "$WK_STORE/bench"
    ensure_dir "$WK_STORE/skills"
    ensure_dir "$(wk_secrets_dir)" 0700
    ensure_dir "$(wk_agent_rw_dir)" 0700
    secrets_publish || warn "could not publish $(wk_secrets_dir)/ssh_config and github-user,
    so a workspace here gets no fork alias and no GITHUB_COM_TOKEN"
}

# /secrets is what every workspace on this machine reads. What goes in it -- the fork aliases and the account name -- is public and identical whether push is on or off, so it is published with the directory. Left to `wk push`, a machine nobody had switched yet gave every workspace an empty /secrets: no fork alias, and no placeholder for the injector to replace, so `git-webkit` sent no Authorization header at all.
secrets_publish() {
    local sock; sock=$(t_agent_sock 2>/dev/null) || sock=""
    push_agent_publish_config "$(wk_secrets_dir)" "$sock"
}

base_path() { echo "$(wk_base_dir)/$1/WebKit"; }

# `wk sync` publishes into a hardlinked copy of the last snapshot, so a kill
# mid-publish leaves a newer directory than any good one; the sha lands last.
base_sha_file() { echo "$(wk_base_dir)/$1/sha"; }

base_complete() { [ -s "$(base_sha_file "$1")" ]; }

base_recorded_sha() { cat "$(base_sha_file "$1")" 2>/dev/null || true; }
base_tree_sha()     { git -C "$(base_path "$1")" rev-parse HEAD 2>/dev/null || true; }

base_verify() { # <id> -- 0 if publishable and untampered; else prints why not
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

current_base() {
    local d
    for d in $(ls -1 "$(wk_base_dir)" 2>/dev/null | sort -r); do
        base_complete "$d" || continue
        echo "$d"
        return 0
    done
    return 1
}

ws_base_id() {
    local f="$(wk_ws_dir "$1")/base-id"
    [ -f "$f" ] && cat "$f" || return 1
}

list_workspaces() {
    [ -d "$WK_STORE/ws" ] || return 0
    ls -1 "$WK_STORE/ws" 2>/dev/null || true
}

unpinned_workspaces() {
    local ws
    for ws in $(list_workspaces); do
        ws_base_id "$ws" >/dev/null 2>&1 || echo "$ws"
    done
}

unreferenced_bases() {
    local base used ws id
    [ -z "$(unpinned_workspaces)" ] || return 0
    used=""
    for ws in $(list_workspaces); do
        id=$(ws_base_id "$ws" 2>/dev/null) || continue
        used="$used $id"
    done

    local keep; keep=$(current_base 2>/dev/null || true)

    for base in $(ls -1 "$(wk_base_dir)" 2>/dev/null); do
        [ "$base" = "$keep" ] && continue
        case " $used " in
            *" $base "*) continue ;;
        esac
        echo "$base"
    done
}

# A closed set: each is delivered and read by name (container/firstrun.sh,
# shell/bashrc, cmd/key; bound back here by tests/test_pi_agent.py). A `value`
# row is one line, exported into its variable; a `file` row the agent rewrites
# in place, so it lives in wk_agent_rw_dir and goes only to a workspace seeing
# these same bytes.  <name> <file here> <file in the home> <variable> <kind>
wk_agent_secrets() {
    cat <<'EOF'
claude        claude-token        .wk-agent-token             CLAUDE_CODE_OAUTH_TOKEN  value
litellm       litellm-key         .wk-litellm-key             LITELLM_API_KEY          value
claude-login  .credentials.json   .claude/.credentials.json   -                        file
EOF
}

# The Claude CLI rotates the refresh token in place, so every holder here
# shares these bytes and one lock.
wk_agent_rw_dir() { printf '%s/agent-rw' "$(dirname "$(wk_secrets_dir)")"; }

wk_agent_secret_names() { wk_agent_secrets | awk 'NF { print $1 }'; }

wk_agent_secret_field() { # <name> <column>; empty for a name not in the table
    wk_agent_secrets | awk -v n="$1" -v c="$2" '$1 == n { print $c; exit }'
}
wk_agent_secret_known() { [ -n "$(wk_agent_secret_field "$1" 1)" ]; }
wk_agent_secret_kind() { wk_agent_secret_field "$1" 5; }

wk_agent_secret_path() { # <name>
    local f; f=$(wk_agent_secret_field "$1" 2)
    [ -n "$f" ] || return 1
    if [ "$(wk_agent_secret_kind "$1")" = file ]; then
        printf '%s/%s' "$(wk_agent_rw_dir)" "$f"
    else
        printf '%s/%s' "$(wk_secrets_dir)" "$f"
    fi
}

wk_agent_secret() { # <name> -- its first line; a file row is read whole by wk_cred_read
    local p; p=$(wk_agent_secret_path "$1") || return 0
    _wk_secret_read "$p" | sed -n '1p'
}

wk_agent_secret_present() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 1
    python3 "$WK_ROOT/lib/secretfile.py" present "$p"
}

wk_agent_secret_store() { # <name> -- from stdin: an argument is visible in `ps`
    local p; p=$(wk_agent_secret_path "$1") || return 1
    ensure_dir "$(dirname "$p")" 0700
    python3 "$WK_ROOT/lib/secretfile.py" write "$p"
}

wk_agent_secret_clear() { # <name>
    local p; p=$(wk_agent_secret_path "$1") || return 1
    rm -f "$p"
}

wk_cred_path() { # <name> -- where this machine keeps it
    case "$1" in
        github-pat)  wk_github_pat_path ;;
        tailnet)     wk_tailscale_authkey_path ;;
        tailnet-api) wk_tailscale_api_path ;;
        *)           wk_agent_secret_path "$1" ;;
    esac
}

wk_cred_names() { python3 "$WK_ROOT/lib/credcheck.py" names; }

wk_cred_read() { # <name> -- every byte of it, nothing when it is absent
    _wk_secret_read "$(wk_cred_path "$1")"
}

# `--stored` judges what this machine holds, else the value comes on stdin,
# before anything has written it.
wk_cred_check() { # <name> [--stored] [...] -> <absent|ok|wide|bad|unverified><TAB><detail>
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
