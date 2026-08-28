# Target driver: a shared, multi-user build machine.
#
# No containers: these are other people's build machines. A workspace is a
# plain checkout under your own home directory -- isolation is gone as a
# result, so `wk claude` and `wk verify` refuse to run against a remote target.
#
# Every build is sized from the *remote* machine's load and free memory,
# niced to the floor, and serialised against other wk builds by the same user.
#
# The only target you can have several of, so one is named after the machine:
#
#   wk new bug-238 --target devbox-arm64-2
#
# with targets/hosts/devbox-arm64-2.conf holding whatever differs from the
# defaults below (target_registry_dir, lib/target.sh):
#
#   WK_REMOTE_HOST=devbox-arm64-2  # ssh destination; defaults to the target name
#   WK_REMOTE_ROOT=/home/you/wk    # defaults to ~/wk on the box
#   WK_TARGET_CMAKE=-DFOO=ON       # extra CMake flags for builds on this machine
#   WK_REMOTE_REFERENCE=/var/...   # a shared checkout to clone from; see below
#   WK_REMOTE_LOCAL=1              # this *is* the machine; run without ssh
#   WK_REMOTE_PEER=1               # a workstation of its own, not a build box
#   WK_REMOTE_TOOLS=Development/…  # its wk-tools, if not $root/tools
#
# --- peers -------------------------------------------------------------------
#
# WK_REMOTE_PEER marks the one case that is not a build box: another
# workstation, which owns its own workspaces. A peer is *asked*, not driven:
# t_has_wk is true without the marker, so `wk status`/`wk ls` delegate to
# its own wk; no tooling is pushed, and creation/destruction are refused --
# its workspaces are containers and guests, which this driver would replace
# with a plain checkout. `wk remote setup <target>` needs no root.

# Defaults to the target's own name: a reachable machine already has an
# ~/.ssh/config entry. `remote` is excluded: it names the driver, not a host.
if [ -z "${WK_REMOTE_HOST:-}" ] && [ "${WK_TARGET:-remote}" != remote ]; then
    WK_REMOTE_HOST="$WK_TARGET"
fi
WK_REMOTE_HOST="${WK_REMOTE_HOST:-}"

# Left empty: the default is $HOME/wk on the remote box, unknown here until
# _remote_probe resolves it.
WK_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"

# --- am I the machine this target names? -------------------------------------
#
# Computed from ~/.wk-remote (`wk remote setup`): if it names this target,
# this process is running *on* the machine, so drop the ssh step and take
# the root from the marker rather than a second conf file that could drift.
if [ -z "${WK_REMOTE_LOCAL:-}" ] && in_remote_host \
   && [ "$(wk_remote_field target)" = "${WK_TARGET:-remote}" ]; then
    WK_REMOTE_LOCAL=1
    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$(wk_remote_field root)"
fi

# No fixed job ceiling: derived per build from what the machine has free
# (_remote_probe). WK_MAX_JOBS still overrides.

# $WK_STORE defaults to /var/lib/wk, wrong here, and is per target since two
# remote machines can each have a workspace of the same name. Only the build
# log and status live here -- checkout, build tree and ccache are on the far end.
# WK_REMOTE_STORE overrides that per-target default; tests/test_remote.py
# points it at a scratch directory so a test never writes under a real $HOME.
if [ -n "${WK_REMOTE_LOCAL:-}" ] && [ -n "${WK_REMOTE_ROOT:-}" ]; then
    WK_STORE="${WK_REMOTE_STORE:-$WK_REMOTE_ROOT}"
else
    WK_STORE="${WK_REMOTE_STORE:-$(wk_state_dir)/remote/${WK_TARGET:-remote}}"
fi

# One code path drives the same workspaces from the workstation over ssh or
# from a shell on the box with no ssh, so the two can never answer differently.
_remote_is_local() { [ -n "${WK_REMOTE_LOCAL:-}" ]; }

_remote_peer() { [ -n "${WK_REMOTE_PEER:-}" ]; }

_remote_require() {
    _remote_is_local && return 0
    [ -n "$WK_REMOTE_HOST" ] || die "target '${WK_TARGET:-remote}' has no host to reach.
    Set WK_REMOTE_HOST in $(target_registry_conf "${WK_TARGET:-remote}"), or
    name the target after a machine your ~/.ssh/config already knows:
        wk new <name> --target devbox-arm64-2"
}

# Multiplexed and never interactive: `t_info`/`t_src`/`t_tools`/the capacity
# probe reach this several times per command, each a handshake through a
# jump host without a shared connection. ConnectTimeout/BatchMode are
# _ssh_opts_base (lib/target.sh); this adds ControlMaster muxing.
_ssh_opts() {
    local d; d="$(wk_state_dir)/ssh"
    mkdir -p "$d" 2>/dev/null || true
    printf '%s' "$(_ssh_opts_base "$(wk_ssh_timeout)") -o ControlMaster=auto -o ControlPath=$d/%h-%p-%r -o ControlPersist=60"
}

_rsh() {
    _remote_require
    # On the machine itself there is nothing to connect to; same shell, same script text.
    if _remote_is_local; then
        bash -c "$*"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

# ssh for *questions*, not work: `-n`, since these run inside command
# substitutions that inherit stdin, and ssh would otherwise drink it.
# t_exec/t_exec_tty/t_wk/t_status_put don't use it -- a backgrounded build
# reaching for the terminal earns a SIGTTIN.
_rsh_q() {
    _remote_require
    if _remote_is_local; then
        bash -c "$*" </dev/null
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -n $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

# One round trip, cached for the process: the remote home, core count, load
# and memory free -- everything that sizes a build, not lib/resources.sh,
# which measures the wrong machine here. `wk status` must never die over an
# unreachable machine, so the answer (or lack of one) is cached too.
#
# Raw form (nproc/loadavg/meminfo on Linux, sysctl/vm_stat on Darwin), parsed
# back here by _remote_probe_parse rather than an inline remote awk, which
# could not be unit-tested on a captured sample.
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

# No ssh involved, so a captured sample exercises exactly what a live probe
# parses. `sysctl -n vm.loadavg`'s second field is the load average where
# /proc/loadavg has it first; `vm_stat` reports pages where /proc/meminfo
# has MemAvailable in kB directly.
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
            # A trailing blank line (a here-string's own) must not overwrite
            # the real answer that came before it.
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

    printf '%s\n%s\n%s\n%s\n' "${cores:-1}" "${load:-0}" "${mem:-0}" "$ionice"
}

# A file, not a variable: the prefetch (prefetch_targets, lib/target.sh)
# runs in a subshell per target, which cannot hand a value to its parent.
_remote_probe_file() {
    [ -n "${WK_PREFETCH_DIR:-}" ] || return 0
    printf '%s/%s.probe' "$WK_PREFETCH_DIR" "${WK_TARGET:-remote}"
}

# An *empty* file means asked-and-did-not-answer, the ten-second answer
# worth prefetching.
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
    # A prefetched answer if one was taken for this command, ssh otherwise.
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

    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$_WK_REMOTE_HOME/wk"
    _WK_REMOTE_PROBED=1
}

_remote_probe() {
    _remote_probe_try || die "cannot reach '$WK_REMOTE_HOST' over ssh ($(wk_ssh_timeout)s).
    This target has no way in but ssh, and it is not interactive: the key,
    the ProxyJump and the host entry all have to work non-interactively.
    Try:  ssh -o BatchMode=yes $WK_REMOTE_HOST true"
}

# A shared WebKit repository, to clone workspaces from -- advertised in the
# MOTD, or WK_REMOTE_REFERENCE for one that does not announce it. A local
# clone hardlinks `.git/objects`, costing almost nothing next to a 13 GB
# mirror of our own. Verified before use: a MOTD can outlive the repo it names.
_remote_reference() {
    [ -n "${_WK_REMOTE_REF_PROBED:-}" ] && { printf '%s' "$WK_REMOTE_REFERENCE"; return 0; }
    _WK_REMOTE_REF_PROBED=1

    if [ -n "${WK_REMOTE_REFERENCE:-}" ]; then
        printf '%s' "$WK_REMOTE_REFERENCE"
        return 0
    fi

    # Static MOTD files only: running somebody else's scripts to read a hint
    # is not a trade worth making.
    WK_REMOTE_REFERENCE=$(_rsh_q '
        cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null \
        | grep -oE "/[A-Za-z0-9._/-]*[Ww]eb[Kk]it(\.git)?" | sort -u \
        | while read -r p; do
              git -C "$p" rev-parse --verify -q refs/heads/main >/dev/null 2>&1 || continue
              echo "$p"; break
          done' 2>/dev/null) || WK_REMOTE_REFERENCE=""

    printf '%s' "$WK_REMOTE_REFERENCE"
}

# The mirror this driver keeps when the machine has no shared repository:
# `t_create` clones from it, `t_sync` refreshes it. Only main is mirrored
# (wk_mirror_branches); gc.auto is off since workspaces borrow its objects
# through --shared, which a repack underneath a live clone would break.
_remote_mirror_update() {
    local root="$1"
    info "updating the WebKit mirror on $WK_REMOTE_HOST (first run clones it)"
    _rsh_q "set -e
        mkdir -p $(sh_quote "$root/ws") $(sh_quote "$root/cache/ccache")
        M=$(sh_quote "$root/mirror")
        if [ ! -d \"\$M\" ]; then
            git init --bare -q \"\$M\"
            git -C \"\$M\" config gc.auto 0
            git -C \"\$M\" remote add origin https://github.com/WebKit/WebKit.git
            git -C \"\$M\" config --add remote.origin.fetch '+refs/heads/main:refs/heads/main'
        fi
        git -C \"\$M\" fetch --prune -q origin" \
        || die "could not update the WebKit mirror on $WK_REMOTE_HOST"
}

# Every path function goes through this, not the variable: the default is
# only known after the probe.
_remote_root() { _remote_probe; printf '%s' "$WK_REMOTE_ROOT"; }

_remote_ws()   { echo "$(_remote_root)/ws/$1"; }

# --- peers -------------------------------------------------------------------
# A peer owns its workspaces: they are its containers and its guests, in its
# own store, and nothing on this side has a path to one. So every question
# about them is asked of its own `wk`, and every command about one is handed
# over whole (`wk`'s delegation, which t_delegates below decides).

# Its listing, as "<name>\t<state>" lines. One round trip per process,
# memoised the way the capacity probe is; `wk ls --json` because a document
# is the off-the-shelf machine-readable answer and a table is not.
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

# What it takes to reach a workspace the peer owns, from the only machine
# that can say: which account, which checkout, and what to run *there* to
# speak ssh to a container that has no address of its own. The same round
# trip installs that workspace's sshd and authorises this machine's key over
# there (WK_ZED_PUBKEY), so an editor here needs nothing further.
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

# --- contract ----------------------------------------------------------------

# A peer's checkout is inside the workspace, not under the remote root, so
# only the peer can say where -- and it says so as part of the route, which
# `wk zed` has already asked for by the time this is called.
t_src() {
    _remote_peer && [ -n "${1:-}" ] \
        && { _peer_route "$1"; printf '%s' "$_WK_PEER_ROUTE_SRC"; return 0; }
    echo "$(_remote_ws "$1")/WebKit"
}

# The remote's own ccache, under the remote root -- not a shared one on the
# box: a cache you do not administer is one you can poison for other people.
t_ccache_dir() { echo "$(_remote_root)/cache/ccache"; }

_remote_home() { _remote_probe; printf '%s' "$_WK_REMOTE_HOME"; }

# Pushed to the remote root, not per workspace: same tree for all of them.
# WK_REMOTE_TOOLS overrides for a peer's own git checkout; a relative path
# is relative to the *remote* home, since the conf is sourced on this side.
t_tools() {
    case "${WK_REMOTE_TOOLS:-}" in
        "") echo "$(_remote_root)/tools" ;;
        /*) printf '%s' "$WK_REMOTE_TOOLS" ;;
        *)  printf '%s/%s' "$(_remote_home)" "$WK_REMOTE_TOOLS" ;;
    esac
}

# No base snapshots here: the equivalent is the git mirror, which t_create maintains.
t_needs_base() { return 1; }

# The ssh destination already configured, not a generated alias (which could
# not carry the real entry's ProxyJump). On the machine itself there is no
# route and never will be -- that would be an ssh loop back to the host you are typing on.
#
# A peer's *workspace* is the exception: it is not on the peer's filesystem
# at all, so it is reached through the alias t_ssh_prepare writes here. The
# machine itself -- the empty name, which is what `wk zed --tools` asks about
# -- still answers as itself.
t_ssh_host() {
    _remote_is_local && return 1
    _remote_require
    if _remote_peer && [ -n "${1:-}" ]; then echo "wk-$1"; return 0; fi
    echo "$WK_REMOTE_HOST"
}

# One hop more than the peer needs itself: its own transport, run over ssh to
# it, is a complete transport from here -- the container still has no network
# interface and nothing new listens anywhere.
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
    # `|| true`: no ws directory yet is not an error, and pipefail in the
    # caller would fail the whole listing on a non-zero ls.
    { _rsh_q "ls -1 $(sh_quote "$(_remote_root)/ws") 2>/dev/null" 2>/dev/null || true; } \
        | while read -r n; do [ -n "$n" ] && printf '%s\tpresent\n' "$n"; done
}

# One round trip, since every extra one is a handshake through a jump host:
#
#   no workspace directory          absent
#   directory, no `.wk-ready`       creating -- clone unfinished or ssh cut mid-way
#   `.wk-ready`                     present
#   the machine did not answer      unreachable, never absent
#
# Without the marker a half-cloned workspace reads as present, `wk new`
# refuses it as "already exists", and `wk build` builds the rubble.
t_info() {
    local ws out
    _remote_probe_try || { echo unreachable; return 0; }

    # A peer's own word for a workspace it owns. Only the four words above
    # survive the trip: everything else its listing can say (running, exited,
    # broken) means the same thing from here -- it is there, and the peer is
    # what reports on it, since every command about it is handed over.
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

    # A half-made workspace is not refused: `wk new` already destroyed it
    # before getting here (rule 3, wipe over repair), so anything but absent
    # here means the record and the machine disagree.
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
        # A plain local clone (hardlinks) rather than --shared, so the
        # workspace does not depend on a repository the sysadmins repack.
        info "cloning from $ref (this machine's shared WebKit, hardlinked)"
        _rsh_q "set -e
            mkdir -p $(sh_quote "$root/ws") $(sh_quote "$root/cache/ccache")
            git clone --quiet -b main $(sh_quote "$ref") $(sh_quote "$ws/WebKit")" \
            || die "could not clone $ref on $WK_REMOTE_HOST"
        _remote_wire "$ws/WebKit"
    else
        # No shared repository: keep one of our own and clone --shared.
        # _remote_mirror_update is both the keeping and the fetching;
        # `t_sync` is its other caller.
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

    # Last, on the far side, so it survives this end going away: an ssh cut
    # mid-clone leaves no marker, and the workspace reads `creating`.
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

# Only the build is serialised: locking t_exec instead would block every
# one-line probe behind an hour-long build. The lock is taken on the machine
# that builds, by lib/lockrun.sh -- not here, since a detached build
# outlives the ssh session by hours. Not `flock`: its descriptor is
# inherited, so anything the build leaves running would hold it (lib/common.sh).
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
    # Stdin closed before the pipeline: the lookup can itself reach the
    # machine, and an ssh in a command substitution reads inherited stdin.
    ws=$(_remote_ws "$name" </dev/null)
    # log= is rewritten to the machine's own log path, or `wk status` loses
    # its liveness check. No `|| true`: a silent failure leaves `wk status`
    # reporting stale state forever, so warn instead.
    sed "s|^log=.*|log=$ws/build.log|" \
        | _rsh "cat > $(sh_quote "$ws/build.status")" \
        || warn "could not record '$name's build state on $WK_REMOTE_HOST -- 'wk status $name'
    may show stale information until it answers again"
}

# `wk`, run on the machine itself: it answers with its own store, where the
# canonical build state lives. Refused unless provisioned (`wk remote setup`).
t_has_wk() {
    _remote_is_local && return 1
    # Before resolving the remote root: on a machine that is off, that dies
    # inside a command substitution with a connection error mid-listing.
    _remote_probe_try || return 1
    # A peer has no marker and must not be given one: it is what makes a
    # machine refuse the host-only commands on itself, which a peer needs.
    if _remote_peer; then
        _rsh_q "test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
        return $?
    fi
    _rsh_q "test -f \$HOME/.wk-remote && test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
}

# A peer's workspaces are its own and no path here reaches them, answered
# or not -- so a peer delegates whether or not it is up, and t_far_side (in
# `wk`) is what turns a peer that is down into an honest refusal rather than
# a command quietly acting on the wrong thing. A build box delegates once it
# has a `wk` of its own: the machine doing the building drives the build.
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

# What the far side is asked to run, built in one place so a pty changes the
# transport and nothing else -- a delegated command that loses --force over
# the hop is a barrier the user already crossed, refusing again over there.
# The dispatcher's global flags and the driver's own three travel as
# environment, not arguments: an unknown argument is fatal on an old copy (a
# peer's own git checkout), an unknown variable is silently ignored.
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

# The same, with a pty: `wk sudo setup` over there prompts for a password,
# and sudo refuses to read one without a terminal.
t_wk_tty() {
    if _remote_is_local; then
        t_wk "$@"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" "$(_remote_wk_cmd "$@")"
}

# A pty, for anything with a full-screen UI: `wk run --lldb` without one is
# a debugger prompt that prints, accepts nothing, and dies on the first ctrl-c.
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

# .git excluded because the remote copy is a deployment, not a checkout to work in.
t_sync_tools() {
    local name="$1" dest
    dest=$(t_tools "$name")

    # A peer keeps its own copy under git: --delete would throw away its uncommitted work.
    if _remote_peer; then
        debug "not pushing wk-tools to $WK_REMOTE_HOST: it is a workstation with its own checkout"
        return 0
    fi

    if _remote_is_local; then
        [ "$WK_ROOT" = "$dest" ] || warn "running $WK_ROOT/wk, but this target's tooling is $dest"
        return 0
    fi

    debug "syncing wk-tools -> $WK_REMOTE_HOST"
    # rsync makes no missing parent; a fresh machine gets here before t_create's mkdir has run.
    _rsh_q "mkdir -p $(sh_quote "$dest")"
    rsync -az --delete --exclude '.git/' \
        -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/" "$WK_REMOTE_HOST:$dest/"
}

# See t_wiring_args (lib/target.sh): the shared WebKit or our mirror, plus
# the ssh config under the wk root -- makes fork push URLs resolve on a box
# whose ~/.ssh is not ours to edit.
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
        printf 'mirror\n%s\n%s\n' "$root/mirror" "$root/ssh/config"
    fi
}

# Why a peer's `git pull` could not reach this machine's tree: the answer is
# always on this side -- a peer pulls from the shared upstream, so anything
# this machine hasn't published is invisible to it. Named precisely, because
# the three cases have three different fixes.
_peer_why_behind() {
    local dirty="" ahead="" branch="" up=""
    git -C "$WK_ROOT" rev-parse --git-dir >/dev/null 2>&1 || {
        printf 'this copy of wk-tools is not a git checkout, so nothing can pull from it'
        return 0; }
    [ -n "$(git -C "$WK_ROOT" status --porcelain 2>/dev/null)" ] && dirty=1
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
    else
        printf 'both are at %s, so the difference is untracked or excluded files rather than commits' "$up"
    fi
}

# This target's furniture (`wk sync --tools`): its copy of wk-tools, and the
# WebKit objects its workspaces clone from. A bare `wk sync` is refused on
# the machine itself (is_host_only), so this is how either is refreshed.
t_sync() {
    local ref rc=0 mine theirs tools
    _remote_probe
    tools=$(t_tools "")

    # A peer is a workstation running its own checkout under git: rsyncing
    # over it would throw away uncommitted work, so it pulls instead
    # (--ff-only: a merge on somebody else's machine is not this command's to
    # make). Then checked, because a dirty tree there lets the pull succeed
    # while converging nothing.
    if _remote_peer; then
        _rsh_q "cd $(sh_quote "$tools") && git pull --ff-only" >&2 \
            || { printf '  %-24s %s\n' "$WK_TARGET" "git pull --ff-only failed there" >&2; return 1; }
        mine=$("$WK_ROOT/cmd/version" --tree 2>/dev/null || true)
        theirs=$(_rsh_q "$(sh_quote "$tools")/cmd/version --tree" 2>/dev/null || true)
        if [ -z "$mine" ] || [ "$mine" != "$theirs" ]; then
            printf '  %-24s %s\n' "$WK_TARGET" "pulled, still DIFFERS ($theirs, this machine has $mine)" >&2
            printf '  %-24s %s\n' "" "$(_peer_why_behind)" >&2
            # Nothing more is asked of a copy that is not this one: what a
            # scope word means is that copy's to decide, and an older one
            # spells them differently -- which is how a command naming one
            # machine reaches machines it never named. Its mirror and its
            # snapshot wait for the pull to converge.
            return 1
        fi
        printf '  %-24s pulled, in sync\n' "$WK_TARGET" >&2

        # Its mirror and its snapshot are its own: publishing one on somebody
        # else's workstation happens only when this run named it
        # (WK_SYNC_NAMED), never as part of a sweep over every target.
        if [ -z "${WK_SYNC_NAMED:-}" ]; then
            info "$WK_REMOTE_HOST keeps a store of its own -- its mirror and snapshot untouched"
            log  "  name it for those:  wk sync --tools $WK_TARGET"
            return "$rc"
        fi
        info "running 'wk sync --tools' on $WK_REMOTE_HOST -- its mirror, its snapshot"
        WK_NO_DELEGATE=1 t_wk sync --tools || rc=1
        return "$rc"
    fi

    t_sync_tools "" && printf '  %-24s pushed\n' "$WK_TARGET" >&2
    ref=$(_remote_reference)
    if [ -n "$ref" ]; then
        info "workspaces here clone from $ref, which this machine's admins keep up to date"
        log  "  nothing of ours to fetch: no mirror is kept on $WK_REMOTE_HOST"
        return 0
    fi
    _remote_mirror_update "$(_remote_root)"
    changed "the WebKit mirror on $WK_REMOTE_HOST is up to date"
}

t_destroy() {
    local name="$1"
    _remote_peer && die "'$WK_REMOTE_HOST' is a workstation: its workspaces are removed there,
    by the machine that made them.  ssh $WK_REMOTE_HOST wk rm $name"
    _rsh_q "rm -rf $(sh_quote "$(_remote_ws "$name")")"
    rm -rf "$(wk_ws_dir "$name")"
    info "removed remote workspace '$name' from $WK_REMOTE_HOST"
}

# --- capacity ----------------------------------------------------------------
# All three answer for the remote machine: lib/resources.sh's job-count
# calculation needs the far end's numbers, not this one's.

t_cores()  { _remote_probe; echo "${_WK_REMOTE_CORES:-1}"; }
t_load()   { _remote_probe; echo "${_WK_REMOTE_LOAD:-0}"; }
t_mem_mb() { _remote_probe; echo "${_WK_REMOTE_MEM:-1024}"; }
