# git/WebKit.git is the only thing ever fetched into: base/<id>/WebKit is a live overlay's lower layer, and changes under a mounted overlay are undefined.
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

# What this machine writes for itself and opens again as files: a seeded benchmark payload, an exported runner tree, a downloaded profiler, a long-running command's task record, a bench task's directory. The store holds them, except the one store no host command can write -- the podman VM's, which is what a macOS workstation's default resolves to, root-owned from this side (`mkdir /var/lib/wk/bench`: Permission denied). Only that one is diverted: a store this machine was pointed at, a target's own or a test's scratch, is where its own records belong, and a command forwarded into the VM answers this from in there.
wk_record_dir() {
    if [ -z "${WK_IN_VM:-}" ] && [ "$(uname -s)" = Darwin ] \
       && [ "$WK_STORE" = "$(_wk_default_store)" ]; then
        wk_state_dir
    else
        printf '%s' "$WK_STORE"
    fi
}

wk_artifact_dir() { printf '%s/cache' "$(wk_record_dir)"; }
wk_bench_dir()    { printf '%s/bench' "$(wk_record_dir)"; }   # the tasks a benchmarking command records, named here so cmd/status and cmd/doctor spell it the way lib/bench.sh does

WK_CCACHE_MAXSIZE="${WK_CCACHE_MAXSIZE:-40G}"   # shared by every workspace here

ccache_conf_render() { printf 'max_size = %s\n' "$WK_CCACHE_MAXSIZE"; }
ccache_conf_write() { # <path to ccache.conf>
    [ -f "$1" ] && return 0
    if [ -n "${WK_DRY_RUN:-}" ]; then
        changed "would write $1 ($(ccache_conf_render))"
        return 0
    fi
    ensure_dir "$(dirname "$1")" >/dev/null
    ccache_conf_render > "$1"
}

wk_mirror() {   # one per machine, written where `wk sync` runs: a macOS host's is its own, and the podman VM (/var/lib/wk/git) and every tart guest (the `mirror` share) mount it read-only
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then echo "$(wk_state_dir)/git/WebKit.git"
    else echo "$WK_STORE/git/WebKit.git"; fi
}
mirror_init()    { ensure_dir "$(dirname "$(wk_mirror)")"; }
wk_ws_dir()   { echo "$WK_STORE/ws/$1"; }

wk_push_forks() { _wk_py wk.secrets forks; }   # <remote> <owner/repo> <ssh-host-alias>: lib/wk/secrets.py's FORKS

# The wiring, the snapshots and the PR fetch are lib/wk/{git,store,pr}.py; these are their bash callers' names for them.
_wk_py() { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" WK_STORE="$WK_STORE" WK_MIRROR_BRANCHES="${WK_MIRROR_BRANCHES:-}" python3 -m "$@"; }

wk_remotes()                { _wk_py wk.git remotes; }
wk_mirror_branches()        { _wk_py wk.git mirror-branches; }
mirror_refresh_script()     { _wk_py wk.git mirror-refresh-script "$@"; }
wk_wiring_script()          { _wk_py wk.git wiring-script "$@"; }
wk_gitwebkit_setup_script() { _wk_py wk.git gitwebkit-setup-script "$@"; }
base_verify()               { _wk_py wk.store base-verify "$@"; }
current_base()              { _wk_py wk.store current-base; }
list_workspaces()           { _wk_py wk.store list-workspaces; }

wk_claude_cli_script() {
    cat <<'EOF'
[ ! -r "$HOME/.wk-egress" ] || . "$HOME/.wk-egress"
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    echo claude=present
    exit 0
fi
# stdout is the installer's progress, stderr the one report of why it stopped: the caller logs it.
curl -fsSL https://claude.ai/install.sh | bash >/dev/null || { echo claude=failed; exit 1; }
echo claude=installed
EOF
}

wk_ssh_alias_blocks() { _wk_py wk.secrets alias-blocks "$@"; }   # <dir> [<prefix> [<agent-sock> [<ProxyCommand>]]]

wk_machine_store() { printf '%s' "${WK_STORE_DEFAULT:-$WK_STORE}"; }

wk_secrets_dir() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        wk_host_secrets
    else
        printf '%s/secrets' "$(wk_machine_store)"
    fi
}

# The other half of the line above: inside the podman VM that directory is the macOS host's own ~/.config/wk/secrets, bind-mounted read-only, so the host publishes into it and the VM only reads it.
wk_secrets_owned_here() { [ -z "${WK_IN_VM:-}" ]; }

wk_push_held_dir() { printf '%s/push-keys' "$(dirname "$(wk_secrets_dir)")"; }

wk_ntfy_topic_path() { printf '%s/notify/ntfy-topic' "$(dirname "$(wk_secrets_dir)")"; }

# lib/secretfile.py holds the rule "this is a file, and it is ours": a link planted in the read-write agent-rw could publish a credential.
_wk_secret_read() { # <path> -- absent is not an error
    python3 "$WK_ROOT/lib/secretfile.py" read "$1"
}

# The agent holding the keys and the injector's credential files: each function takes an exec function for the machine holding them, and a path as a shell word expanded there (/run/user/501 is not on macOS).

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

wk_github_pat_path() { printf '%s/github-pat' "$(wk_push_held_dir)"; }

wk_bugzilla_key_path() { printf '%s/bugzilla-api-key' "$(wk_push_held_dir)"; }

wk_github_user() { wk_push_forks | awk 'NF {print $2; exit}' | cut -d/ -f1; }

# WebKit's own record of who this GitHub account is on Bugzilla (webkitpy's Committer.bugzilla_email: the entry's first email), read from the mirror so no copy is kept; empty, exit 1, where there is no mirror or no entry.
wk_bugzilla_user() {
    git -C "$(wk_mirror)" cat-file -p main:metadata/contributors.json 2>/dev/null \
        | python3 "$WK_ROOT/lib/contributors.py" bugzilla-login "$(wk_github_user)"
}

push_agent_cred_write() { # <execfn> <path> <name> -- the first line of a stored credential, down a pipe
    local value; value=$(_wk_secret_read "$(wk_cred_path "$3")" | sed -n '1p')
    [ -n "$value" ] || return 1
    printf '%s\n' "$value" | "$1" "umask 077 && cat > $(sh_quote "$2")"
}

push_agent_cred_clear() { # <execfn> <path>
    "$1" "rm -f $(sh_quote "$2")" </dev/null
}

push_agent_cred_sync() { # <execfn> <path> <name>
    if [ -n "$(_wk_secret_read "$(wk_cred_path "$3")" | sed -n '1p')" ]; then
        push_agent_cred_write "$1" "$2" "$3"
    else
        push_agent_cred_clear "$1" "$2"
    fi
}

push_agent_pat_converge_machine() { # on every start of the podman machine, as a guest's `wk start` does for its injector: a token stored while it was down is otherwise a 401 from every container until './setup'
    push_agent_cred_sync push_agent_exec "$(push_agent_machine_read_pat)" github-pat \
        || warn "the injector in the podman machine did not take the read token; './setup' converges it"
}

push_agent_publish_config() { # <dir> is this machine's spelling; paths inside are /secrets
    local dir="$1" sock="$2"
    ensure_dir "$dir" 0700 >/dev/null
    { printf '%s\n' "# wk: written by 'wk push on|off' (push_agent_publish_config," \
                    "# lib/store.sh). One alias per fork, because GitHub takes one deploy" \
                    "# key per repository and both forks live on github.com. The identity is" \
                    "# a public half; the private one is in an ssh-agent outside this" \
                    "# workspace, and whether it is loaded there is what 'wk push' switches."
      wk_ssh_alias_blocks /secrets build_key_ "$sock"
    } > "$dir/ssh_config.new" || return 1
    chmod 0644 "$dir/ssh_config.new"
    mv "$dir/ssh_config.new" "$dir/ssh_config"

    printf '%s\n' "$(wk_github_user)" > "$dir/github-user.new" || return 1
    chmod 0644 "$dir/github-user.new"
    mv "$dir/github-user.new" "$dir/github-user"

    local bz
    if bz=$(wk_bugzilla_user) && [ -n "$bz" ]; then
        printf '%s\n' "$bz" > "$dir/bugzilla-user.new" || return 1
        chmod 0644 "$dir/bugzilla-user.new"
        mv "$dir/bugzilla-user.new" "$dir/bugzilla-user"
    else
        rm -f "$dir/bugzilla-user"
        warn "no Bugzilla login for $(wk_github_user): metadata/contributors.json in the
    mirror ($(wk_mirror)) has no entry for that account, or there is no mirror
    ('wk sync'). git-webkit in a workspace asks for one instead"
    fi
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
    if wk_secrets_owned_here; then
        secrets_publish || warn "could not publish $(wk_secrets_dir)/ssh_config and github-user,
    so a workspace here gets no fork alias and no GITHUB_COM_TOKEN"
    else
        secrets_require_published
    fi
}

secrets_require_published() { # what the owning machine put there, read in the VM
    local dir f; dir=$(wk_secrets_dir)
    for f in ssh_config github-user view/container/ssh_config; do
        [ -f "$dir/$f" ] && continue
        die "$dir/$f is not there, and nothing in here publishes it: $dir is the
    host's ~/.config/wk/secrets, mounted read-only. Publish it from the host:
        ./setup --stage vmtools"
    done
}

# /secrets is what every workspace on this machine reads. What goes in it -- the fork aliases and the account name -- is public and identical whether push is on or off, so it is published with the directory. Left to `wk push`, a machine nobody had switched yet gave every workspace an empty /secrets: no fork alias, and no placeholder for the injector to replace, so `git-webkit` sent no Authorization header at all.
secrets_publish() {
    local sock   # the container target's own socket, not whichever target the caller had loaded: this file is what every container Includes, and built from a target naming none it silently loses the IdentityAgent line -- a workspace that cannot push even with the switch on
    sock=$( . "$WK_ROOT/lib/target.sh"; load_target container >/dev/null 2>&1 && t_agent_sock ) 2>/dev/null || sock=""
    push_agent_publish_config "$(wk_secrets_dir)" "$sock"
    secrets_publish_view container
}

# A container is handed its credentials by mounting a directory; every other kind is given its rows one file at a time (lib/wk/guest.py, targets/remote.sh) and mounts nothing. So the directory a container mounts holds exactly what wk_agent_secrets delivers to a container -- the store above it holds every row, a vm's and a build box's included.
wk_secrets_view_dir() { # <target kind>
    printf '%s/view/%s' "$(wk_secrets_dir)" "$1"
}

secrets_view_files() { # <target kind> -- what it may read, one name per line
    local kind="$1" name file home var vkind deliv f
    printf '%s\n' ssh_config github-user bugzilla-user
    for f in "$(wk_secrets_dir)"/build_key_*.pub; do
        [ -f "$f" ] && printf '%s\n' "${f##*/}"
    done
    wk_agent_secrets | while read -r name file home var vkind deliv; do
        [ -n "$name" ] || continue
        [ "$vkind" = value ] || continue      # a file row is rewritten in place: wk_agent_rw_dir
        wk_agent_secret_delivered "$name" "$kind" && printf '%s\n' "$file"
    done
    return 0
}

secrets_publish_view() { # <target kind> -- converge that view on the table
    local kind="$1" src dir f want
    wk_secrets_owned_here || return 0
    src=$(wk_secrets_dir)
    dir=$(wk_secrets_view_dir "$kind")
    ensure_dir "$dir" 0700 >/dev/null || return 1
    want=$(secrets_view_files "$kind")
    for f in $want; do
        if [ -f "$src/$f" ]; then
            cp -p "$src/$f" "$dir/$f.new" || return 1
            mv "$dir/$f.new" "$dir/$f" || return 1
        else
            rm -f "$dir/$f"
        fi
    done
    for f in $(ls -A "$dir" 2>/dev/null); do
        case "
$want
" in *"
$f
"*) ;; *) rm -f "$dir/$f" ;; esac
    done
}

wk_agent_secrets() { _wk_py wk.secrets agent-secrets; }   # <name> <file here> <file in the home> <variable> <value|file> <delivery>: lib/wk/secrets.py's AGENT_SECRETS

# The Claude CLI rotates the refresh token in place, so every holder here shares these bytes and one lock.
wk_agent_rw_dir() { printf '%s/agent-rw' "$(dirname "$(wk_secrets_dir)")"; }

wk_agent_secret_names() { wk_agent_secrets | awk 'NF { print $1 }'; }

wk_agent_secret_field() { # <name> <column>; empty for a name not in the table
    wk_agent_secrets | awk -v n="$1" -v c="$2" '$1 == n { print $c; exit }'
}
wk_agent_secret_kind() { wk_agent_secret_field "$1" 5; }

wk_agent_secret_delivered() { # <name> <target kind> -- 0 when this row reaches that kind
    case ",$(wk_agent_secret_field "$1" 6)," in
        *",$2,"*) return 0 ;;
    esac
    return 1
}

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
    wk_cred_present "$1"
}

wk_cred_path() { # <name> -- where this machine keeps it
    case "$1" in
        github-pat)  wk_github_pat_path ;;
        bugzilla-api-key) wk_bugzilla_key_path ;;
        tailnet)     wk_tailscale_authkey_path ;;
        tailnet-api) wk_tailscale_api_path ;;
        ntfy)        wk_ntfy_topic_path ;;
        *)           wk_agent_secret_path "$1" ;;
    esac
}

wk_cred_present() { # <name> -- is there one here at all; its rule judges what it can do
    local p; p=$(wk_cred_path "$1") || return 1
    python3 "$WK_ROOT/lib/secretfile.py" present "$p"
}

wk_cred_read() { WK_IN_VM="${WK_IN_VM:-}" WK_STORE_DEFAULT="${WK_STORE_DEFAULT:-}" WK_HOST_SECRETS="${WK_HOST_SECRETS:-}" _wk_py wk.secrets cred-read "$1"; }   # every byte of it, nothing when it is absent

