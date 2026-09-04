# Target driver: a shared, multi-user build machine.
#
# No containers: these are other people's build machines. A workspace is a plain
# checkout under your own home directory, so `wk ai claude` and `wk verify` refuse
# to run against a remote target. WK_REMOTE_PEER marks the one machine that is not
# a build box: another workstation, which owns its own workspaces and is asked,
# not driven -- creation and destruction are refused there.
#
# targets/hosts/<name>.conf (target_registry_dir, lib/target.sh) holds whatever
# differs: WK_REMOTE_HOST (ssh destination, default the target name), WK_REMOTE_ROOT
# (~/wk there), WK_REMOTE_REFERENCE (a shared checkout to clone from), WK_REMOTE_LOCAL,
# WK_REMOTE_PEER, WK_REMOTE_TOOLS, WK_TARGET_CMAKE, WK_TARGET_LIBCXX, WK_TARGET_WPE.

# A reachable machine already has an ~/.ssh/config entry; `remote` names the driver.
if [ -z "${WK_REMOTE_HOST:-}" ] && [ "${WK_TARGET:-remote}" != remote ]; then
    WK_REMOTE_HOST="$WK_TARGET"
fi
WK_REMOTE_HOST="${WK_REMOTE_HOST:-}"

# The default is $HOME/wk on the box, unknown here until _remote_probe resolves it.
WK_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"

# Computed from ~/.wk-remote (`wk remote setup`): if it names this target, this
# process runs *on* the machine, so drop the ssh step and take the root from it.
if [ -z "${WK_REMOTE_LOCAL:-}" ] && in_remote_host \
   && [ "$(wk_remote_field target)" = "${WK_TARGET:-remote}" ]; then
    WK_REMOTE_LOCAL=1
    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$(wk_remote_field root)"
fi

# Per target: two remote machines can each have a workspace of the same name.
if [ -n "${WK_REMOTE_LOCAL:-}" ] && [ -n "${WK_REMOTE_ROOT:-}" ]; then
    # WK_REMOTE_STORE overrides the per-target default.
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

# Multiplexed and never interactive: several round trips per command, each a handshake.
_ssh_opts() {
    local d; d="$(wk_state_dir)/ssh"
    mkdir -p "$d" 2>/dev/null || true
    printf '%s' "$(_ssh_opts_base "$(wk_ssh_timeout)") -o ControlMaster=auto -o ControlPath=$d/%h-%p-%r -o ControlPersist=60"
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

# ssh for *questions*: `-n`, since these run in command substitutions whose stdin ssh drinks.
_rsh_q() {
    _remote_require
    if _remote_is_local; then
        bash -c "$*" </dev/null
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -n $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

# One round trip, memoised: lib/resources.sh measures the wrong machine here. The raw
# form is parsed here, so a captured sample exercises what a live probe parses.
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

# `sysctl -n vm.loadavg`'s second field is the load average where /proc/loadavg has
# it first; `vm_stat` reports pages where /proc/meminfo has MemAvailable in kB.
# `uname -s` is reported on as well as branched on: it is what t_os answers with.
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
            # A trailing blank line (a here-string's own) must not overwrite the real answer.
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

# A file, not a variable: the prefetch runs in a subshell per target (lib/target.sh).
_remote_probe_file() {
    [ -n "${WK_PREFETCH_DIR:-}" ] || return 0
    printf '%s/%s.probe' "$WK_PREFETCH_DIR" "${WK_TARGET:-remote}"
}

# An *empty* file means asked-and-did-not-answer.
t_prefetch() {
    local f out
    f=$(_remote_probe_file) || return 0
    [ -n "$f" ] || return 0
    _remote_is_local && return 0
    [ -n "${WK_REMOTE_HOST:-}" ] || return 0
    out=$(_rsh_q "$(_remote_probe_cmd)" 2>/dev/null) || out=""
    printf '%s' "$out" > "$f.tmp.$$" && mv "$f.tmp.$$" "$f"
}

_remote_probe_try() {
    [ -n "${_WK_REMOTE_PROBED:-}" ] && return 0
    [ -n "${_WK_REMOTE_DOWN:-}" ] && return 1
    _remote_require
    local out f parsed
    f=$(_remote_probe_file) || f=""
    if [ -n "$f" ] && [ -f "$f" ]; then
        out=$(cat "$f")
        if [ -z "$out" ]; then
            _WK_REMOTE_DOWN=1
            return 1
        fi
    elif ! out=$(_rsh_q "$(_remote_probe_cmd)" 2>/dev/null); then
        _WK_REMOTE_DOWN=1
        return 1
    fi

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
    _remote_probe_try || die "cannot reach '$WK_REMOTE_HOST' over ssh ($(wk_ssh_timeout)s).
    This target has no way in but ssh, and it is not interactive: the key,
    the ProxyJump and the host entry all have to work non-interactively.
    Try:  ssh -o BatchMode=yes $WK_REMOTE_HOST true"
}

# A shared WebKit repository to clone from, named by the MOTD or WK_REMOTE_REFERENCE.
# Verified before use: a MOTD can outlive the repo it names.
_remote_reference() {
    [ -n "${_WK_REMOTE_REF_PROBED:-}" ] && { printf '%s' "$WK_REMOTE_REFERENCE"; return 0; }
    _WK_REMOTE_REF_PROBED=1

    if [ -n "${WK_REMOTE_REFERENCE:-}" ]; then
        printf '%s' "$WK_REMOTE_REFERENCE"
        return 0
    fi

    # Static MOTD files only: running somebody else's scripts to read a hint is no trade.
    WK_REMOTE_REFERENCE=$(_rsh_q '
        cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null \
        | grep -oE "/[A-Za-z0-9._/-]*[Ww]eb[Kk]it(\.git)?" | sort -u \
        | while read -r p; do
              git -C "$p" rev-parse --verify -q refs/heads/main >/dev/null 2>&1 || continue
              echo "$p"; break
          done' 2>/dev/null) || WK_REMOTE_REFERENCE=""

    printf '%s' "$WK_REMOTE_REFERENCE"
}

# The mirror this driver keeps when the machine has no shared repository, made by
# mirror_refresh_script (lib/store.sh); every checkout there is a `--shared` clone.
_remote_mirror_update() {
    local root="$1"
    info "updating the WebKit mirror on $WK_REMOTE_HOST (first run clones it)"
    command -v mirror_refresh_script >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
    # An unreachable fork is not a failure of the mirror; its line comes back on stdout.
    _rsh_q "set -e
        mkdir -p $(sh_quote "$root/ws") $(sh_quote "$root/cache/ccache")
        $(mirror_refresh_script "$(t_mirror_dir)")" \
        | while read -r _tag _name _state; do
              [ "$_tag" = mirror-fetch ] || continue
              printf '  %-8s %s\n' "$_name" "$_state" >&2
          done \
        || die "could not update the WebKit mirror on $WK_REMOTE_HOST"
}

# Every path function goes through this: the default is only known after the probe.
_remote_root() { _remote_probe; printf '%s' "$WK_REMOTE_ROOT"; }

_remote_ws()   { echo "$(_remote_root)/ws/$1"; }

# A peer owns its workspaces: every question is asked of its own `wk` (t_delegates).

# One round trip per process, memoised; `wk ls --json` is the machine-readable form.
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

# The same round trip installs that workspace's sshd and authorises this machine's key.
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

# Not a shared ccache on the box: one you do not administer is one you can poison.
t_ccache_dir() { echo "$(_remote_root)/cache/ccache"; }

# The bare mirror on the box: every workspace there is a `--shared` clone of it.
t_mirror_dir() { echo "$(_remote_root)/mirror"; }

_remote_home() { _remote_probe; printf '%s' "$_WK_REMOTE_HOME"; }

# A relative WK_REMOTE_TOOLS is relative to the *remote* home: the conf is sourced here.
t_tools() {
    case "${WK_REMOTE_TOOLS:-}" in
        "") echo "$(_remote_root)/tools" ;;
        /*) printf '%s' "$WK_REMOTE_TOOLS" ;;
        *)  printf '%s/%s' "$(_remote_home)" "$WK_REMOTE_TOOLS" ;;
    esac
}

# No base snapshots here: the equivalent is the git mirror, which t_create maintains.
t_needs_base() { return 1; }

# The ssh destination already configured, not a generated alias (which could not carry
# the ProxyJump). A peer's *workspace* goes through the alias t_ssh_prepare writes.
t_ssh_host() {
    _remote_is_local && return 1
    _remote_require
    if _remote_peer && [ -n "${1:-}" ]; then echo "wk-$1"; return 0; fi
    echo "$WK_REMOTE_HOST"
}

# Its own transport, run over ssh to it, is a complete transport from here.
t_ssh_prepare() {
    local name="${1:-}"
    { _remote_peer && [ -n "$name" ]; } || return 0
    _peer_route "$name"
    [ -n "$_WK_PEER_ROUTE_PROXY" ] \
        || die "'$name' on $WK_REMOTE_HOST is reached at an address on that machine's
    own network, which is not this one's. Open it from $WK_REMOTE_HOST:
        ssh $WK_REMOTE_HOST wk zed $name"
    ssh_alias_set "$name" "wk-$name.$WK_TARGET.invalid" "$_WK_PEER_ROUTE_USER" "$(zed_key)" \
        "ProxyCommand ssh $WK_REMOTE_HOST $_WK_PEER_ROUTE_PROXY"
}

t_store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/ws"
}

t_list() {
    _remote_peer && { _peer_list; return 0; }
    # `|| true`: no ws directory yet is not an error, and pipefail would fail the listing.
    { _rsh_q "ls -1 $(sh_quote "$(_remote_root)/ws") 2>/dev/null" 2>/dev/null || true; } \
        | while read -r n; do [ -n "$n" ] && printf '%s\tpresent\n' "$n"; done
}

# One round trip, since every extra one is a handshake through a jump host:
#   no workspace directory          absent
#   directory, no `.wk-ready`       creating -- clone unfinished or ssh cut mid-way
#   `.wk-ready`                     present
#   the machine did not answer      unreachable, never absent
t_info() {
    local ws out
    _remote_probe_try || { echo unreachable; return 0; }

    # A peer's own word for it. Only the four words above survive the trip.
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

t_created() { [ "$(t_info "$1")" = present ]; }

t_create() {
    local name="$1" root ws ref
    _remote_peer && die "'$WK_REMOTE_HOST' is a workstation, not a build machine for this one.
    Its workspaces are its own -- containers or guests, from its own store --
    and this driver would make a plain checkout under ~/wk instead. Create it
    there:  ssh $WK_REMOTE_HOST wk new $name"
    _remote_probe
    root=$(_remote_root)
    ws=$(_remote_ws "$name")

    # `wk new` already destroyed a half-made workspace, so anything but absent here means
    # the record and the machine disagree.
    case "$(t_info "$name")" in
        absent) ;;
        creating) die "'$name' on $WK_REMOTE_HOST is a checkout that never finished being
    made, and destroying it did not take. Remove it by hand and try again:
        ssh $WK_REMOTE_HOST rm -rf $(sh_quote "$(_remote_ws "$name")")" ;;
        unreachable) die "cannot reach $WK_REMOTE_HOST to create '$name'" ;;
        *) die "workspace '$name' already exists on $WK_REMOTE_HOST" ;;
    esac

    ref=$(_remote_reference)

    if [ -n "$ref" ]; then
        # A plain local clone (hardlinks), not --shared: the sysadmins repack that repository.
        info "cloning from $ref (this machine's shared WebKit, hardlinked)"
        _rsh_q "set -e
            mkdir -p $(sh_quote "$root/ws") $(sh_quote "$root/cache/ccache")
            git clone --quiet -b main $(sh_quote "$ref") $(sh_quote "$ws/WebKit")" \
            || die "could not clone $ref on $WK_REMOTE_HOST"
        _remote_wire "$ws/WebKit"
    else
        # No shared repository: keep one of our own and clone --shared.
        _remote_mirror_update "$root"
        _rsh_q "git clone --quiet --shared -b main $(sh_quote "$root/mirror") \
                          $(sh_quote "$ws/WebKit")" \
            || die "could not create the checkout on $WK_REMOTE_HOST"
        _remote_wire "$ws/WebKit"
    fi

    # Only when absent, so hand-tuned ccache survives.
    command -v ccache_conf_render >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
    _rsh_q "[ -f $(sh_quote "$root/cache/ccache/ccache.conf") ] ||
            printf %s $(sh_quote "$(ccache_conf_render)") \
              > $(sh_quote "$root/cache/ccache/ccache.conf")" || true

    ensure_dir "$(wk_ws_dir "$name")"

    # Last, on the far side: an ssh cut mid-clone leaves no marker, and it reads creating.
    _rsh_q "touch $(sh_quote "$ws/$WK_READY_MARKER")" \
        || die "could not mark '$name' ready on $WK_REMOTE_HOST -- treat it as half-made
    and re-run 'wk new $name --target ${WK_TARGET:-remote}'"
    info "remote workspace '$name' created on $WK_REMOTE_HOST ($ws)"
}

t_exec() {
    local name="$1"; shift
    _rsh "cd $(sh_quote "$(t_src "$name")") && $(sh_quote "$@")"
}

t_pull() {
    local name="$1" src="$2" dest="$3"
    if _remote_is_local; then cp -f "$src" "$dest"; return; fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_REMOTE_HOST:$src" "$dest"
}

t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    _t_pull_dir_excludes "$@"
    mkdir -p "$dest"
    if _remote_is_local; then
        rsync -a --delete ${_T_PULL_EXCLUDES[@]+"${_T_PULL_EXCLUDES[@]}"} "$src/" "$dest/"; return
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    rsync -a --delete ${_T_PULL_EXCLUDES[@]+"${_T_PULL_EXCLUDES[@]}"} -e "ssh $(_ssh_opts)" \
        "$WK_REMOTE_HOST:$src/" "$dest/"
}

t_push() {
    local name="$1" src="$2" dest="$3"
    if _remote_is_local; then cp -f "$src" "$dest"; return; fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$src" "$WK_REMOTE_HOST:$dest"
}

t_push_dir() {
    local name="$1" src="$2" dest="$3"
    if _remote_is_local; then
        mkdir -p "$dest"
        rsync -a --delete "$src/" "$dest/"; return
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    rsync -a --delete -e "ssh $(_ssh_opts)" "$src/" "$WK_REMOTE_HOST:$dest/"
}

t_path_kind() {
    local name="$1" p="$2"
    _rsh_q "if [ -d $(sh_quote "$p") ]; then echo dir
         elif [ -e $(sh_quote "$p") ]; then echo file
         else echo absent; fi" 2>/dev/null | tr -d '\r'
}

# Only the build is serialised: locking t_exec would block every one-line probe behind an
# hour-long build. Not `flock`: its descriptor is inherited by whatever the build leaves.
t_exec_build() {
    local name="$1"; shift
    local log tee_to

    # _remote_ws forces the probe, populating _WK_REMOTE_IONICE for `prio` below.
    log="$(_remote_ws "$name")/build.log"

    # Nothing to tee into locally: run_watched already writes this file there.
    tee_to=" 2>&1 | tee $(sh_quote "$log")"
    _remote_is_local && tee_to=""

    local prio="nice -n 19"
    [ "${_WK_REMOTE_IONICE:-no}" = yes ] && prio="$prio ionice -c3"

    # pipefail with tee, or tee's exit status becomes the build's.
    _rsh_q "set -o pipefail
          cd $(sh_quote "$(t_src "$name")") && \
          $(sh_quote "$(t_tools "$name")/lib/lockrun.sh") remote-build -w 3600 -- \
          $prio $(sh_quote "$@")$tee_to"
}

# The status file's one copy, on the machine itself -- never a local shadow.
t_status_put() {
    local name="$1" ws
    if _remote_is_local; then
        ws="$(wk_ws_dir "$name")"
        cat > "$ws/build.status"
        return 0
    fi
    # Stdin closed before the pipeline: the lookup can itself reach the machine.
    ws=$(_remote_ws "$name" </dev/null)
    # log= is rewritten to the machine's own log path, or `wk status` loses its liveness check.
    sed "s|^log=.*|log=$ws/build.log|" \
        | _rsh "cat > $(sh_quote "$ws/build.status")" \
        || warn "could not record '$name's build state on $WK_REMOTE_HOST -- 'wk status $name'
    may show stale information until it answers again"
}

# `wk` on the machine itself answers with its own store. Refused unless provisioned.
t_has_wk() {
    _remote_is_local && return 1
    # Before resolving the remote root: on a machine that is off, that dies mid-listing.
    _remote_probe_try || return 1
    # A peer has no marker and must not be given one: it is what makes a machine refuse the
    # host-only commands on itself.
    if _remote_peer; then
        _rsh_q "test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
        return $?
    fi
    _rsh_q "test -f \$HOME/.wk-remote && test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
}

# `wk remote setup` records the hash of the inputs that provisioned the machine in
# ~/.wk-remote; this recomputes it here, so the two ends cannot hash differently.
remote_provision_inputs_hash() {
    cat "$WK_ROOT/remote/provision.sh" "$WK_ROOT/remote/deps.sh" \
        | python3 -c 'import hashlib,sys
print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])'
}

# Prints the reason and succeeds when stale: `if why=$(remote_provision_stale); then`.
remote_provision_stale() {
    local marker rec
    marker=$(_rsh_q "cat \"\$HOME/.wk-remote\" 2>/dev/null" 2>/dev/null) || true
    if [ -z "$marker" ]; then
        echo "no ~/.wk-remote there, so nothing has provisioned it"
        return 0
    fi
    rec=$(printf '%s\n' "$marker" | kv_get inputs)
    if [ -z "$rec" ]; then
        echo "provisioned before this record existed"
        return 0
    fi
    [ "$rec" = "$(remote_provision_inputs_hash)" ] && return 1
    echo "remote/provision.sh or remote/deps.sh has changed since it ran"
    return 0
}

# A peer delegates whether or not it is up -- no path here reaches its workspaces. A
# build box delegates once it has a `wk` of its own.
t_delegates() {
    _remote_is_local && return 1
    _remote_peer && return 0
    t_has_wk
}

t_far_side() {
    if _remote_is_local; then echo none
    elif ! _remote_probe_try; then echo unreachable
    elif t_has_wk; then echo answering
    else echo no-wk
    fi
}

# Built in one place so a pty changes the transport and nothing else. The flags travel as
# environment, not arguments: an unknown argument is fatal on an old copy.
_remote_wk_cmd() {
    printf 'cd $HOME && %s%s%s%s%s %s' \
        "$(wk_forwarded_env)" \
        "${WK_ROW_LABEL:+WK_ROW_LABEL=$(sh_quote "${WK_ROW_LABEL:-}") }" \
        "${WK_NO_DELEGATE:+WK_NO_DELEGATE=1 }" \
        "${WK_ZED_PUBKEY:+WK_ZED_PUBKEY=$(sh_quote "${WK_ZED_PUBKEY:-}") }" \
        "$(sh_quote "$(t_tools '')/wk")" "$(sh_quote "$@")"
}

t_wk() {
    _rsh "$(_remote_wk_cmd "$@")"
}

# With a pty: `wk sudo setup` prompts, and sudo refuses to read a password without one.
t_wk_tty() {
    if _remote_is_local; then
        t_wk "$@"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" "$(_remote_wk_cmd "$@")"
}

# A pty: `wk run --lldb` without one accepts nothing and dies on the first ctrl-c.
t_exec_tty() {
    local name="$1"; shift
    if _remote_is_local; then
        cd "$(t_src "$name")" && exec "$@"
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    exec ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" \
        "cd $(sh_quote "$(t_src "$name")") && $(sh_quote "$@")"
}

t_enter() {
    _remote_probe
    if _remote_is_local; then
        cd "$(t_src "$1")" && exec "${SHELL:-/bin/sh}" -l
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    exec ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" \
        "cd $(sh_quote "$(t_src "$1")") && exec \$SHELL -l"
}

command -v tools_push >/dev/null 2>&1 || . "$WK_ROOT/lib/tools.sh"

# A git bundle of this tree's HEAD (tools_push, lib/tools.sh): a machine holds a commit
# that exists, which is what `wk status` compares by sha.
t_sync_tools() {
    local name="$1" dest
    dest=$(t_tools "$name")

    # A peer keeps its own checkout, with its own uncommitted work in it: it pulls.
    if _remote_peer; then
        debug "not pushing wk-tools to $WK_REMOTE_HOST: it is a workstation with its own checkout"
        return 0
    fi

    if _remote_is_local; then
        [ "$WK_ROOT" = "$dest" ] || warn "running $WK_ROOT/wk, but this target's tooling is $dest"
        return 0
    fi

    tools_push "$dest" _rsh
}

# The shared WebKit or our mirror, plus the ssh config under the wk root, so fork push
# URLs resolve where ~/.ssh is not ours to edit.
_remote_wire() {
    local src="$1" n u c
    { read -r n; read -r u; read -r c; } <<EOF
$(t_wiring_args)
EOF
    _rsh_q "$(wk_wiring_script "$src" "$n" "$u" "$c")" \
        || warn "could not wire the remotes in $src"
}

t_wiring_args() {
    local ref root
    root=$(_remote_root)
    ref=$(_remote_reference)
    if [ -n "$ref" ]; then
        printf 'shared\n%s\n%s\n' "$ref" "$root/ssh/config"
    else
        printf 'mirror\n%s\n%s\n' "$(t_mirror_dir)" "$root/ssh/config"
    fi
}

# The answer is always on this side; named precisely, since the three cases differ.
_peer_why_behind() {
    local dirty="" ahead="" branch="" up=""
    git -C "$WK_ROOT" rev-parse --git-dir >/dev/null 2>&1 || {
        printf 'this copy of wk-tools is not a git checkout, so nothing can pull from it'
        return 0; }
    [ -n "$(git -C "$WK_ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ] && dirty=1
    branch=$(git -C "$WK_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    up=$(git -C "$WK_ROOT" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)
    if [ -n "$up" ]; then
        ahead=$(git -C "$WK_ROOT" rev-list --count "$up..HEAD" 2>/dev/null || echo 0)
    fi
    if [ -n "$dirty" ]; then
        printf 'this machine has uncommitted changes -- a peer pulls from %s, so commit and push them first' \
            "${up:-the upstream}"
    elif [ -n "$up" ] && [ "${ahead:-0}" -gt 0 ]; then
        printf 'this machine is %s commit(s) ahead of %s -- push them, then re-run' "$ahead" "$up"
    elif [ -z "$up" ]; then
        printf "branch '%s' has no upstream here, so there is nothing for a peer to pull from" "${branch:-HEAD}"
    fi
}

# This target's furniture: its copy of wk-tools, and the objects its workspaces clone from.
t_sync() {
    local ref rc=0 mine_ver theirs_ver mine_sha mine_dirty theirs_sha theirs_dirty tools
    _remote_probe
    tools=$(t_tools "")

    # A peer runs its own checkout under git: rsyncing over it would throw away uncommitted
    # work, so it pulls (--ff-only). Then checked: a dirty tree there converges nothing.
    if _remote_peer; then
        _rsh_q "cd $(sh_quote "$tools") && git pull --ff-only" >&2 \
            || { printf '  %-24s %s\n' "$WK_TARGET" "git pull --ff-only failed there" >&2; return 1; }
        mine_ver=$("$WK_ROOT/cmd/version" 2>/dev/null || true)
        theirs_ver=$(_rsh_q "$(sh_quote "$tools")/cmd/version" 2>/dev/null || true)
        mine_sha=$(kv_get sha <<<"$mine_ver");     mine_dirty=$(kv_get dirty <<<"$mine_ver")
        theirs_sha=$(kv_get sha <<<"$theirs_ver"); theirs_dirty=$(kv_get dirty <<<"$theirs_ver")
        # The commit plus dirtiness of tracked files: an untracked .DS_Store never differs.
        if [ -z "$mine_sha" ] || [ "$mine_sha" != "$theirs_sha" ] \
            || [ "$mine_dirty" != "$theirs_dirty" ]; then
            printf '  %-24s %s\n' "$WK_TARGET" "pulled, still DIFFERS ($(printf '%s' "$theirs_sha" | cut -c1-12)$([ "$theirs_dirty" = yes ] && printf '+dirty'), this machine has $(printf '%s' "$mine_sha" | cut -c1-12)$([ "$mine_dirty" = yes ] && printf '+dirty'))" >&2
            printf '  %-24s %s\n' "" "$(_peer_why_behind)" >&2
            return 1
        fi
        printf '  %-24s pulled, in sync\n' "$WK_TARGET" >&2

        # Only when this run named it (WK_SYNC_NAMED), never as part of a sweep over targets.
        if [ -z "${WK_SYNC_NAMED:-}" ]; then
            info "$WK_REMOTE_HOST keeps a store of its own -- its mirror and snapshot untouched"
            log  "  name it for those:  wk sync --tools $WK_TARGET"
            return "$rc"
        fi
        info "running 'wk sync --tools' on $WK_REMOTE_HOST -- its mirror, its snapshot"
        WK_NO_DELEGATE=1 t_wk sync --tools || rc=1
        return "$rc"
    fi

    # A refused push is this command's verdict, and the mirror is still fetched.
    if t_sync_tools ""; then printf '  %-24s pushed %s\n' "$WK_TARGET" "$(tools_head)" >&2
    else rc=1; fi
    ref=$(_remote_reference)
    if [ -n "$ref" ]; then
        info "workspaces here clone from $ref, which this machine's admins keep up to date"
        log  "  nothing of ours to fetch: no mirror is kept on $WK_REMOTE_HOST"
        return "$rc"
    fi
    _remote_mirror_update "$(_remote_root)"
    changed "the WebKit mirror on $WK_REMOTE_HOST is up to date"
    return "$rc"
}

t_destroy() {
    local name="$1"
    _remote_peer && die "'$WK_REMOTE_HOST' is a workstation: its workspaces are removed there,
    by the machine that made them.  ssh $WK_REMOTE_HOST wk rm $name"
    _rsh_q "rm -rf $(sh_quote "$(_remote_ws "$name")")"
    rm -rf "$(wk_ws_dir "$name")"
    info "removed remote workspace '$name' from $WK_REMOTE_HOST"
}

# All three answer for the remote machine: the job count needs the far end's numbers.

t_cores()  { _remote_probe; echo "${_WK_REMOTE_CORES:-1}"; }
t_os()     { _remote_probe; echo "${_WK_REMOTE_OS:-linux}"; }
t_load()   { _remote_probe; echo "${_WK_REMOTE_LOAD:-0}"; }
t_mem_mb() { _remote_probe; echo "${_WK_REMOTE_MEM:-1024}"; }
