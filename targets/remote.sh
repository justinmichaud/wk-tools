# Target driver: a shared, multi-user build machine -- other people's, so no containers: a workspace is a plain checkout under your own home directory. WK_REMOTE_PEER marks a workstation instead, which owns its own workspaces and is asked, not driven.
# machines/<name>.conf (KIND=build or peer) holds whatever differs: WK_REMOTE_HOST (ssh destination, default the target name), WK_REMOTE_ROOT (~/wk there), WK_REMOTE_REFERENCE (a shared checkout to clone from), WK_REMOTE_LOCAL, WK_REMOTE_PEER, WK_REMOTE_TOOLS, WK_TARGET_CMAKE, WK_TARGET_LIBCXX, WK_TARGET_WPE.
if [ -z "${WK_REMOTE_HOST:-}" ] && [ "${WK_TARGET:-remote}" != remote ]; then
    WK_REMOTE_HOST="$WK_TARGET"
fi
WK_REMOTE_HOST="${WK_REMOTE_HOST:-}"

WK_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"

# This host being the target means this process runs *on* the machine: no ssh step.
if [ -z "${WK_REMOTE_LOCAL:-}" ] && in_remote_host \
   && [ "$(wk_fleet self 2>/dev/null)" = "${WK_TARGET:-remote}" ]; then
    WK_REMOTE_LOCAL=1
    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$(wk_remote_field root)"
fi

if [ -n "${WK_REMOTE_LOCAL:-}" ] && [ -n "${WK_REMOTE_ROOT:-}" ]; then
    WK_STORE="${WK_REMOTE_STORE:-$WK_REMOTE_ROOT}"
else
    WK_STORE="${WK_REMOTE_STORE:-$(wk_state_dir)/remote/${WK_TARGET:-remote}}"
fi

_remote_is_local() { [ -n "${WK_REMOTE_LOCAL:-}" ]; }

_remote_peer() { [ -n "${WK_REMOTE_PEER:-}" ]; }

_remote_require() {
    _remote_is_local && return 0
    [ -n "$WK_REMOTE_HOST" ] || die "target '${WK_TARGET:-remote}' has no host to reach.
    Set WK_REMOTE_HOST in $(target_registry_conf "${WK_TARGET:-remote}"), or
    name the target after a machine your ~/.ssh/config already knows:
        wk new <name> --target devbox-arm64-2"
}

# ServerAliveInterval/CountMax because ConnectTimeout covers the TCP connect and nothing after it: a machine that accepts the connection and then stops answering -- a wedged sshd, a box deep in swap -- held `wk status <ws>` and `wk logs <ws>` past a 300s wait with no bound of their own (measured 2026-09-17, with moose down). Four missed keepalives at 15s is a session given up inside a minute, and a healthy long build answers them at the protocol level however busy the box is.
_ssh_opts() {
    local d; d="$(wk_state_dir)/ssh"
    mkdir -p "$d" 2>/dev/null || true
    printf '%s' "$(_ssh_opts_base "$(wk_ssh_timeout)") -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ControlMaster=auto -o ControlPath=$d/%h-%p-%r -o ControlPersist=60"
}

_rsh() {
    _remote_require
    if _remote_is_local; then
        bash -c "$*"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

# -n: these run in command substitutions, whose stdin ssh would otherwise drink
_rsh_q() {
    _remote_require
    if _remote_is_local; then
        bash -c "$*" </dev/null
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -n $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

_remote_probe_cmd() {
    printf '%s' '
        echo "$HOME"
        u=$(uname -s)
        echo "$u"
        if [ "$u" = Linux ]; then
            nproc
            cat /proc/loadavg
            echo "===MEM==="
            cat /proc/meminfo
        else
            sysctl -n hw.ncpu
            sysctl -n vm.loadavg
            echo "===MEM==="
            vm_stat
        fi
        echo "===IONICE==="
        command -v ionice >/dev/null 2>&1 && echo yes || echo no'
}

# `sysctl -n vm.loadavg` puts the load average second where /proc/loadavg puts it first, and `vm_stat` reports pages where /proc/meminfo has MemAvailable in kB.
_remote_probe_parse() {
    local uname cores section=head load_raw="" mem_raw="" ionice=no line
    { read -r uname; read -r cores; } || return 1

    while IFS= read -r line; do
        case "$line" in
            '===MEM===')    section=mem;    continue ;;
            '===IONICE===') section=ionice; continue ;;
        esac
        case "$section" in
            head)   load_raw="$load_raw$line
" ;;
            mem)    mem_raw="$mem_raw$line
" ;;
            ionice) [ -n "$line" ] && ionice="$line" ;;
        esac
    done

    local load mem
    if [ "$uname" = Linux ]; then
        load=$(printf '%s' "$load_raw" | awk '{print int($1); exit}')
        mem=$(printf '%s' "$mem_raw"  | awk '/^MemAvailable:/ {print int($2/1024); exit}')
    else
        load=$(printf '%s' "$load_raw" | awk '{print int($2); exit}')
        mem=$(printf '%s' "$mem_raw" | awk '
            /page size of/        { match($0, /[0-9]+/); ps = substr($0, RSTART, RLENGTH) }
            /^Pages free:/        { gsub(/\./, "", $NF); free = $NF }
            /^Pages inactive:/    { gsub(/\./, "", $NF); inactive = $NF }
            /^Pages speculative:/ { gsub(/\./, "", $NF); spec = $NF }
            END { if (ps) printf "%d\n", (free + inactive + spec) * ps / 1024 / 1024 }')
    fi

    local os=linux
    [ "$uname" = Darwin ] && os=macos

    printf '%s\n%s\n%s\n%s\n%s\n' "${cores:-1}" "${load:-0}" "${mem:-0}" "$ionice" "$os"
}

# ssh's stderr is the measurement: "Host key verification failed" and "Connection timed out" call for different remedies, and neither is "off".
_remote_probe_ssh() { # <why-file> -- the probe's stdout; on failure the reason is left in <why-file>
    local why="$1" out rc=0
    # Under a ceiling of its own, the way wk_tailscale_peers reads the tailnet: every report of the fleet waits on this one round trip, and a machine that answers its TCP connect and then nothing has no timeout to offer.
    out=$(capped "${WK_PROBE_SECONDS:-20}" _rsh_q "$(_remote_probe_cmd)" 2>"$why") || rc=$?
    if [ "$rc" -ne 0 ]; then
        _ssh_last_word "$rc" < "$why" > "$why.tmp" && mv "$why.tmp" "$why"
        return 1
    fi
    rm -f "$why"
    printf '%s' "$out"
}

_ssh_last_word() { # <ssh exit status>, its stderr on stdin -- the one line a person acts on
    local line
    line=$(grep -v '^[[:space:]]*$' | tail -n 1 | sed -e 's/^ssh: //' -e 's/^kex_exchange_identification: //') || line=""
    printf '%s' "${line:-ssh exited $1 and said nothing}"
}

_remote_probe_try() {
    [ -n "${_WK_REMOTE_PROBED:-}" ] && return 0
    [ -n "${_WK_REMOTE_DOWN:-}" ] && return 1
    _remote_require
    local out parsed why
    why=$(mktemp "${TMPDIR:-/tmp}/wk-ssh-why.XXXXXX")
    if ! out=$(_remote_probe_ssh "$why"); then
        _WK_REMOTE_DOWN=1
        _WK_REMOTE_WHY=$(cat "$why" 2>/dev/null); rm -f "$why"
        return 1
    fi
    rm -f "$why"

    _WK_REMOTE_HOME=$(printf '%s\n' "$out" | sed -n 1p)
    parsed=$(printf '%s\n' "$out" | tail -n +2 | _remote_probe_parse)
    _WK_REMOTE_CORES=$(printf '%s\n' "$parsed" | sed -n 1p)
    _WK_REMOTE_LOAD=$(printf '%s\n' "$parsed" | sed -n 2p)
    _WK_REMOTE_MEM=$(printf '%s\n' "$parsed" | sed -n 3p)
    _WK_REMOTE_IONICE=$(printf '%s\n' "$parsed" | sed -n 4p)
    _WK_REMOTE_OS=$(printf '%s\n' "$parsed" | sed -n 5p)

    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$_WK_REMOTE_HOME/wk"
    _WK_REMOTE_PROBED=1
}

_remote_probe() {
    _remote_probe_try || die "cannot reach '$WK_REMOTE_HOST' over ssh: $_WK_REMOTE_WHY
    This target has no way in but ssh, and it is not interactive: the key,
    the ProxyJump and the host entry all have to work non-interactively.
    What BatchMode refuses to ask -- a new host key, a passphrase -- one
    interactive  ssh $WK_REMOTE_HOST true  asks and settles."
}

t_answers() { # by exit status, so the one probe is not paid again down a command substitution
    WK_FAR_WHY=""
    _remote_is_local && return 0
    _remote_probe_try && return 0
    WK_FAR_WHY="$_WK_REMOTE_WHY"
    return 1
}

# Verified before use: a MOTD can outlive the WebKit repository it names.
_remote_reference() {
    [ -n "${_WK_REMOTE_REF_PROBED:-}" ] && { printf '%s' "$WK_REMOTE_REFERENCE"; return 0; }
    _WK_REMOTE_REF_PROBED=1

    if [ -n "${WK_REMOTE_REFERENCE:-}" ]; then
        printf '%s' "$WK_REMOTE_REFERENCE"
        return 0
    fi

    WK_REMOTE_REFERENCE=$(_rsh_q '
        cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null \
        | grep -oE "/[A-Za-z0-9._/-]*[Ww]eb[Kk]it(\.git)?" | sort -u \
        | while read -r p; do
              git -C "$p" rev-parse --verify -q refs/heads/main >/dev/null 2>&1 || continue
              echo "$p"; break
          done' 2>/dev/null) || WK_REMOTE_REFERENCE=""

    printf '%s' "$WK_REMOTE_REFERENCE"
}

_remote_root() { _remote_probe; printf '%s' "$WK_REMOTE_ROOT"; }

_remote_ws()   { echo "$(_remote_root)/ws/$1"; }

_peer_fetch() {
    local json
    json=$(WK_NO_DELEGATE=1 t_wk ls --json </dev/null 2>/dev/null) || return 1
    printf '%s' "$json" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
for w in doc.get("workspaces", []):
    print("%s\t%s" % (w.get("name", ""), w.get("state", "")))
'
}

_peer_list() {
    if [ -z "${_WK_PEER_LISTED:-}" ]; then
        _WK_PEER_LISTED=1
        _WK_PEER_ROWS=$(_peer_fetch) || _WK_PEER_ROWS=""
    fi
    [ -z "$_WK_PEER_ROWS" ] || printf '%s\n' "$_WK_PEER_ROWS"
}

_peer_route() { # <name>
    local name="$1" out
    [ "${_WK_PEER_ROUTE_NAME:-}" = "$name" ] && return 0
    out=$(WK_ZED_PUBKEY="$(zed_key_pub)" t_wk zed "$name" --route </dev/null) \
        || die "$WK_REMOTE_HOST could not open a route into '$name'; what it said is above.
    A copy of wk-tools that has never heard of 'wk zed --route' says so as a usage
    error -- that one is fixed by bringing the machine up to date:  wk sync --tools"
    _WK_PEER_ROUTE_USER=$( printf '%s\n' "$out" | kv_get user )
    _WK_PEER_ROUTE_SRC=$(  printf '%s\n' "$out" | kv_get src )
    _WK_PEER_ROUTE_PROXY=$(printf '%s\n' "$out" | kv_get proxy )
    [ -n "$_WK_PEER_ROUTE_USER" ] && [ -n "$_WK_PEER_ROUTE_SRC" ] \
        || die "$WK_REMOTE_HOST said nothing an editor can use about '$name'"
    _WK_PEER_ROUTE_NAME="$name"
}

# A peer's checkout is inside the workspace, so only the peer can say where.
t_src() {
    _remote_peer && [ -n "${1:-}" ] \
        && { _peer_route "$1"; printf '%s' "$_WK_PEER_ROUTE_SRC"; return 0; }
    echo "$(_remote_ws "$1")/WebKit"
}

# Empty when this machine keeps a reference of its own (_remote_reference): that is a plain WebKit clone its admins refresh, carrying origin's branches and none of the other upstreams, so it is a clone source and not a mirror to fetch from -- workspaces here ask the upstreams themselves.
t_mirror_dir() { [ -n "$(_remote_reference)" ] || printf '%s' "$(_remote_root)/mirror"; }

_remote_home() { _remote_probe; printf '%s' "$_WK_REMOTE_HOME"; }

t_home() { _remote_home; }

# A relative WK_REMOTE_TOOLS is relative to the *remote* home: the conf is sourced here.
t_tools() {
    case "${WK_REMOTE_TOOLS:-}" in
        "") echo "$(_remote_root)/tools" ;;
        /*) printf '%s' "$WK_REMOTE_TOOLS" ;;
        *)  printf '%s/%s' "$(_remote_home)" "$WK_REMOTE_TOOLS" ;;
    esac
}

t_list() {
    _remote_peer && { _peer_list; return 0; }
    { _rsh_q "ls -1 $(sh_quote "$(_remote_root)/ws") 2>/dev/null" 2>/dev/null || true; } \
        | while read -r n; do if [ -n "$n" ]; then printf '%s\tpresent\n' "$n"; fi; done
}

# One round trip, since every extra one is a handshake through a jump host: no directory is absent, no `.wk-ready` is creating, and no answer is unreachable, never absent.
t_info() {
    local ws out
    _remote_probe_try || { echo unreachable; return 0; }

    if _remote_peer; then
        out=$(_peer_list | awk -F'\t' -v n="$1" '$1 == n { print $2; exit }')
        case "$out" in
            "")                    echo absent ;;
            creating|unreachable)  echo "$out" ;;
            *)                     echo present ;;
        esac
        return 0
    fi

    ws=$(_remote_ws "$1")
    out=$(_rsh_q "if [ ! -d $(sh_quote "$ws") ]; then echo absent;
                  elif [ -f $(sh_quote "$ws/$WK_READY_MARKER") ]; then echo present;
                  else echo creating; fi" 2>/dev/null) || out=unreachable
    printf '%s\n' "${out:-unreachable}"
}

t_exec() {
    local name="$1"; shift
    _rsh "cd $(sh_quote "$(t_src "$name")") && $(sh_quote "$@")"
}

t_task_put() { # <name> <task dir> -- `wk status` asks the machine that runs the build (t_has_wk delegates), so every write is copied there, with `log` and `machine` that machine's own or it loses the liveness check and names the wrong host
    local name="$1" dir="$2" ws far
    _remote_is_local && return 0
    ws=$(_remote_ws "$name" </dev/null)
    far="$(_remote_root)/task/$(basename "$dir")"
    tar -C "$dir" -cf - . 2>/dev/null | _rsh "
        rm -rf $(sh_quote "$far.new") && mkdir -p $(sh_quote "$far.new") &&
        tar -C $(sh_quote "$far.new") -xf - &&
        printf '%s\n' $(sh_quote "$ws/build.log") > $(sh_quote "$far.new/log") &&
        printf '%s\n' $(sh_quote "$WK_REMOTE_HOST") > $(sh_quote "$far.new/machine") &&
        rm -rf $(sh_quote "$far") && mv $(sh_quote "$far.new") $(sh_quote "$far")" \
        || warn "could not record '$name's build state on $WK_REMOTE_HOST -- 'wk status $name'
    may show stale information until it answers again"
}

t_has_wk() {
    _remote_is_local && return 1
    _remote_probe_try || return 1
    if _remote_peer; then
        _rsh_q "test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
        return $?
    fi
    _rsh_q "test -f \$HOME/.wk-remote && test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
}

t_far_side() {
    if _remote_is_local; then echo none
    elif ! _remote_probe_try; then echo unreachable
    elif t_has_wk; then echo answering
    else echo no-wk
    fi
}

# Target.wk_cmd (lib/wk/targets.py), which spells the same far-side line for every target kind.
_remote_wk_cmd() {
    _ws_py wk-cmd "$WK_TARGET" "$@"
}

t_wk() {
    _rsh "$(_remote_wk_cmd "$@")"
}

# With a pty: `wk key sudo setup` prompts, and sudo refuses to read a password without one.
t_wk_tty() {
    if _remote_is_local; then
        t_wk "$@"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" "$(_remote_wk_cmd "$@")"
}

t_cores()  { _remote_probe; echo "${_WK_REMOTE_CORES:-1}"; }
t_os()     { _remote_probe; echo "${_WK_REMOTE_OS:-linux}"; }
t_load()   { _remote_probe; echo "${_WK_REMOTE_LOAD:-0}"; }
t_mem_mb() { _remote_probe; echo "${_WK_REMOTE_MEM:-1024}"; }
