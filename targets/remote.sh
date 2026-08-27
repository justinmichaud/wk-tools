# Target driver: a shared, multi-user build machine.
#
# No containers: these are other people's build machines too, with no
# rootless podman, no overlay support, and no appetite for a stranger's
# storage tree. A workspace is a plain checkout under your own home
# directory.
#
# Isolation is gone as a result -- no firewall, no read-only base, no
# disposable layer. Anything running here is trusted, and `wk claude` and
# `wk verify` refuse to run against a remote target.
#
# Every build here is sized from the *remote* machine's load and free
# memory, niced to the floor, and serialised against other wk builds by the
# same user.
#
# The only target you can have several of, so one is named after the machine
# rather than the driver:
#
#   wk new bug-238 --target devbox-arm64-2
#
# with targets/hosts/devbox-arm64-2.conf holding whatever differs from the
# defaults below:
#
#   WK_REMOTE_HOST=devbox-arm64-2  # ssh destination; defaults to the target name
#   WK_REMOTE_ROOT=/home/you/wk    # defaults to ~/wk on the box
#   WK_TARGET_CMAKE=-DFOO=ON       # extra CMake flags for builds on this machine
#   WK_REMOTE_REFERENCE=/var/...   # a shared checkout to clone from; see below
#   WK_REMOTE_LOCAL=1              # this *is* the machine; run without ssh
#   WK_REMOTE_PEER=1               # a workstation of its own, not a build box
#   WK_REMOTE_TOOLS=Development/…  # its wk-tools, if not $root/tools
#
# See target_registry_dir in lib/target.sh for why there is no per-device
# config file.
#
# --- peers -------------------------------------------------------------------
#
# WK_REMOTE_PEER marks the one case that is not a build box: another
# workstation, which owns its own workspaces (containers, guests, a store, a
# mirror) rather than being the far end of this one. Provisioning it as a
# build box (`wk remote setup`) would make it refuse `wk sync`, `wk gc` and
# `wk new` on itself (`wk` dispatch, in_remote_host) -- a workstation cannot
# be told it is somebody's build box without ceasing to be a workstation.
#
# A peer is a target that can be *asked* and not driven:
#
#   asked      t_has_wk is true without the marker, so `wk status` and
#              `wk ls` delegate to the machine's own wk and report what it
#              says.
#   not driven no tooling is pushed (it keeps its own checkout, by git), and
#              creation and destruction are refused -- a workstation's
#              workspaces are containers and guests, which this driver knows
#              nothing about and would replace with a plain checkout.
#
# The bare name `remote` still works for a one-off, with WK_REMOTE_HOST from
# the environment -- there is no machine name to infer it from.
#
# `wk remote setup <target>` provisions a machine for all of this and needs
# no root on it: a shared box is somebody else's, and a tool that wants sudo
# on it is a tool that does not get used.

# The ssh destination defaults to the target's own name, because a machine you
# can already reach is a machine that already has an entry in ~/.ssh/config --
# with whatever ProxyJump, user and key it needs. Re-stating that here would be
# a second place to keep it right. `remote` is excluded: it names the driver,
# not a host.
if [ -z "${WK_REMOTE_HOST:-}" ] && [ "${WK_TARGET:-remote}" != remote ]; then
    WK_REMOTE_HOST="$WK_TARGET"
fi
WK_REMOTE_HOST="${WK_REMOTE_HOST:-}"

# Left empty on purpose: the default is $HOME/wk *on the remote box*, and this
# side does not know what that expands to. _remote_probe resolves it, once.
WK_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"

# --- am I the machine this target names? -------------------------------------
#
# Computed from ~/.wk-remote, written by `wk remote setup` and recording
# `target=` and `root=`. If it names this target, this process is running
# *on* the machine: the driver drops the ssh step (WK_REMOTE_LOCAL) and takes
# the root from the marker rather than probing itself.
#
# Not a second conf file: restating WK_TARGET_KIND/WK_REMOTE_LOCAL/
# WK_REMOTE_ROOT there would duplicate what the marker already says
# (CLAUDE.md, "State and lifecycle" 1) and could drift if the target were
# renamed in the registry.
if [ -z "${WK_REMOTE_LOCAL:-}" ] && in_remote_host \
   && [ "$(wk_remote_field target)" = "${WK_TARGET:-remote}" ]; then
    WK_REMOTE_LOCAL=1
    [ -n "$WK_REMOTE_ROOT" ] || WK_REMOTE_ROOT="$(wk_remote_field root)"
fi

# No fixed job ceiling: a number that fits a 250 GB build box is wrong for a
# small one, and goes stale the moment either machine changes. The job count
# is derived per build from what the machine has free *at the time*
# (_remote_probe reads MemAvailable and the load average on every
# invocation). WK_MAX_JOBS is still honoured if the environment sets it.

# Host-side state, per machine. $WK_STORE defaults to /var/lib/wk, which is
# right inside the podman VM but wrong on a workstation driving a build
# elsewhere, and has to be per target since two remote machines can each
# have a workspace of the same name.
#
# What lives here is only what this side produces: the build log and build
# status. The checkout, build tree and ccache are all on the far end.
#
# On the machine itself the state *is* the workspace directory:
# `$root/ws/<name>` already holds the checkout, so putting build.log and
# build.status beside it keeps one copy canonical regardless of which end
# started the build.
if [ -n "${WK_REMOTE_LOCAL:-}" ] && [ -n "${WK_REMOTE_ROOT:-}" ]; then
    WK_STORE="${WK_REMOTE_STORE:-$WK_REMOTE_ROOT}"
else
    WK_STORE="${WK_REMOTE_STORE:-$(wk_state_dir)/remote/${WK_TARGET:-remote}}"
fi

# Whether this process is running *on* the target -- set just above from
# ~/.wk-remote. The same driver drives the same workspaces from either end:
# from the workstation over ssh, and from a shell on the box with no ssh at
# all. One code path, so the two can never answer differently about where a
# checkout is or how many jobs a build gets.
_remote_is_local() { [ -n "${WK_REMOTE_LOCAL:-}" ]; }

# Another workstation rather than a build box -- see "peers" at the top.
_remote_peer() { [ -n "${WK_REMOTE_PEER:-}" ]; }

_remote_require() {
    _remote_is_local && return 0
    [ -n "$WK_REMOTE_HOST" ] || die "target '${WK_TARGET:-remote}' has no host to reach.
    Set WK_REMOTE_HOST in $(target_registry_conf "${WK_TARGET:-remote}"), or
    name the target after a machine your ~/.ssh/config already knows:
        wk new <name> --target devbox-arm64-2"
}

# ssh, multiplexed and never interactive. A remote target answers `t_info`,
# `t_src`, `t_tools` and the capacity probe through this, several times per
# command, each a full handshake through a jump host without a shared
# connection. The socket expires by itself, so nothing has to clean it up.
#
# ConnectTimeout: `wk status` walks every target it knows, and an off machine
# must cost ten seconds, not a TCP timeout. BatchMode: none of this may stop
# to ask for a passphrase -- a prompt mid-`wk build` is a hang with a cursor.
# _ssh_opts_base (lib/target.sh) supplies those two; the rest here is
# ControlMaster muxing, worth it for several questions per command.
_ssh_opts() {
    local d; d="$(wk_state_dir)/ssh"
    mkdir -p "$d" 2>/dev/null || true
    printf '%s' "$(_ssh_opts_base "${WK_SSH_TIMEOUT:-10}") -o ControlMaster=auto -o ControlPath=$d/%h-%p-%r -o ControlPersist=60"
}

_rsh() {
    _remote_require
    # On the machine itself there is nothing to connect to, and trying would
    # need an sshd loop and a key to itself. Same shell, same script text.
    if _remote_is_local; then
        bash -c "$*"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_REMOTE_HOST" "$@"
}

# ssh for *questions* -- everything that asks the machine something rather
# than handing it work. `-n`, because ssh reads stdin eagerly and forwards it
# whether or not the remote command wants it, and these run inside command
# substitutions that inherit whatever stdin their caller had -- without `-n` a
# question run in the same pipeline as something reading stdin can drink that
# input instead of answering.
#
# t_exec, t_exec_tty, t_wk and t_status_put deliberately do *not* use it: a
# command run in the workspace may legitimately be fed something. A *build*
# is not -- it reads nothing, and `run_watched` backgrounds it, where an ssh
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

# One round trip, cached for the life of this process: the remote home (what
# $WK_REMOTE_ROOT defaults to), core count, load average and memory actually
# free. Everything that sizes a build comes from this and nothing local --
# lib/resources.sh measures the machine it runs on, which for every other
# target is right and for this one never is.
#
# Reporting and working want different answers to "the machine did not
# reply": a build cannot proceed and says so with the ssh command to try;
# `wk status` must never die over an unreachable machine, only report it and
# carry on with the rest of the fleet. Both outcomes are cached for the
# process, since `wk status` asks per workspace and a ConnectTimeout on each
# would turn a listing into a minute of waiting.
#
# The questions are one shell snippet because two callers ask them -- the
# probe below and the parallel prefetch further down -- and a second copy
# would parse differently from the prefetch's cache.
#
# Cores, load and memory are asked for in raw form (nproc/loadavg/meminfo on
# Linux; sysctl/vm_stat on Darwin and the BSDs close enough to it) and parsed
# back on this side by _remote_probe_parse, rather than computed remotely with
# an inline awk -- a function that only exists as a string baked into an ssh
# command line cannot be unit-tested on a captured sample. Selected once by
# the remote's own `uname -s`, not by which tools this side happens to know:
# a machine answers for itself.
#
# ionice is asked for the same way (present or not), since it is util-linux
# and t_exec_build must not try it on a machine that has no notion of I/O
# scheduling classes at all.
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

# The pure half of the probe: cores/load/memory/ionice out of the raw text
# _remote_probe_cmd gathers (everything after its $HOME line), with no ssh
# involved -- so a captured sample exercises exactly what a live probe would
# parse:
#   bash -c '. targets/remote.sh; _remote_probe_parse <<<"$sample"'
#
# Cores need no branch -- both `nproc` and `sysctl -n hw.ncpu` already print a
# bare integer. Load and memory do: `sysctl -n vm.loadavg` prints
# "{ 1.23 1.45 1.67 }", the load average as its second field, where
# /proc/loadavg has it as the first; and `vm_stat` reports free memory in
# pages, with the page size in its own header line, where /proc/meminfo has
# MemAvailable in kB directly. Free + inactive + speculative pages is what
# Activity Monitor calls "available" on Darwin.
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
            # A trailing blank line (a sample's own newline plus the one a
            # here-string adds) must not overwrite the real answer that came
            # before it.
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

# Where a prefetched answer for *this* target would be, if a command asked
# for one (prefetch_targets, lib/target.sh). Empty when nothing did.
#
# A file, not an inherited variable: the prefetch runs in a subshell per
# target (that is what makes it parallel), and a subshell cannot hand a
# value back to its parent. It lives and dies with one command, the same
# per-process memo _WK_REMOTE_PROBED already is, not a cache that outlives
# the asking.
_remote_probe_file() {
    [ -n "${WK_PREFETCH_DIR:-}" ] || return 0
    printf '%s/%s.probe' "$WK_PREFETCH_DIR" "${WK_TARGET:-remote}"
}

# Asks the machine, in parallel with every other target, and writes what it
# says. An *empty* file means asked-and-did-not-answer, which is a real answer
# and the one worth prefetching: it is the ten-second one.
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
    # A prefetched answer if one was taken for this command, and the ssh
    # otherwise. Same question, same parsing; the only difference is who waited.
    f=$(_remote_probe_file) || f=""
    if [ -n "$f" ] && [ -f "$f" ]; then
        out=$(cat "$f")
        if [ -z "$out" ]; then
            _WK_REMOTE_DOWN=1
            return 1
        fi
    elif ! out=$(_rsh_q "$(_remote_probe_cmd)"); then
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
    _remote_probe_try || die "cannot reach '$WK_REMOTE_HOST' over ssh (${WK_SSH_TIMEOUT:-10}s).
    This target has no way in but ssh, and it is not interactive: the key,
    the ProxyJump and the host entry all have to work non-interactively.
    Try:  ssh -o BatchMode=yes $WK_REMOTE_HOST true"
}

# A shared WebKit repository on the machine, to clone workspaces from.
#
# Igalia's build boxes advertise one in the MOTD (buildbox4: "clone the one
# from /var/git/WebKit.git", updated every 10 minutes) -- a local clone
# hardlinks `.git/objects`, so using it costs almost nothing next to a 13 GB
# mirror of our own on a machine whose MOTD asks everyone to keep disk use
# down.
#
# Advertised or configured, nothing else: a path that merely exists is
# somebody's checkout, not an invitation, while a path in the MOTD is the
# sysadmins telling every user to clone it. WK_REMOTE_REFERENCE overrides,
# for a machine that has one and does not announce it.
#
# Verified before use: a MOTD can outlive the repository it names, and an
# unchecked hint would turn every `wk new` into a confusing clone failure.
_remote_reference() {
    [ -n "${_WK_REMOTE_REF_PROBED:-}" ] && { printf '%s' "$WK_REMOTE_REFERENCE"; return 0; }
    _WK_REMOTE_REF_PROBED=1

    if [ -n "${WK_REMOTE_REFERENCE:-}" ]; then
        printf '%s' "$WK_REMOTE_REFERENCE"
        return 0
    fi

    # Static MOTD files only: /etc/update-motd.d holds scripts, and running
    # somebody else's scripts to read a hint is not a trade worth making.
    WK_REMOTE_REFERENCE=$(_rsh_q '
        cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null \
        | grep -oE "/[A-Za-z0-9._/-]*[Ww]eb[Kk]it(\.git)?" | sort -u \
        | while read -r p; do
              git -C "$p" rev-parse --verify -q refs/heads/main >/dev/null 2>&1 || continue
              echo "$p"; break
          done' 2>/dev/null) || WK_REMOTE_REFERENCE=""

    printf '%s' "$WK_REMOTE_REFERENCE"
}

# The mirror this driver keeps when the machine has no shared repository of
# its own, and the fetch into it. Both halves live here because both callers
# need both: `t_create` clones from it, `t_sync` refreshes it, and a sync
# that assumed the mirror already existed would fail on the machine most
# likely to sync first -- one that has never made one.
#
# Only main is mirrored, matching wk_mirror_branches: WebKit has ~920
# branches, and anything besides main is one `git fetch` away inside the
# workspace. gc.auto is off, as in the local mirror: workspaces borrow its
# objects through --shared, and a repack underneath a live clone breaks that.
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

# $WK_REMOTE_ROOT, resolved. Every path function goes through this rather than
# reading the variable, because the default is only known after the probe.
_remote_root() { _remote_probe; printf '%s' "$WK_REMOTE_ROOT"; }

_remote_ws()   { echo "$(_remote_root)/ws/$1"; }

# --- contract ----------------------------------------------------------------

t_src()   { echo "$(_remote_ws "$1")/WebKit"; }

# The remote's own ccache, under the remote root. Deliberately not a shared one
# somewhere on the box: a cache you do not administer is a cache you can poison
# for other people, and a good way to become unpopular.
#
# A plain '/'-joined path, which is the whole of what a POSIX remote needs --
# true of Linux, macOS and the BSDs alike; only a non-POSIX remote would need
# a second spelling here.
t_ccache_dir() { echo "$(_remote_root)/cache/ccache"; }

# The remote $HOME, from the probe that already asked for it.
_remote_home() { _remote_probe; printf '%s' "$_WK_REMOTE_HOME"; }

# wk-tools is pushed to the remote root rather than per workspace: it is the
# same tree for all of them, and `wk build` re-rsyncs it on every run.
#
# WK_REMOTE_TOOLS overrides, which is how a peer is reached: its wk-tools is
# a git checkout it maintains itself. A relative path is relative to the
# *remote* home -- the conf is sourced on this side, so writing $HOME in it
# would silently expand to the wrong machine's home.
t_tools() {
    case "${WK_REMOTE_TOOLS:-}" in
        "") echo "$(_remote_root)/tools" ;;
        /*) printf '%s' "$WK_REMOTE_TOOLS" ;;
        *)  printf '%s/%s' "$(_remote_home)" "$WK_REMOTE_TOOLS" ;;
    esac
}

# There are no base snapshots here. The overlay scheme is a local-store
# concept; the equivalent on the far end is the git mirror, which t_create
# maintains itself.
t_needs_base() { return 1; }

# The ssh destination is the one already configured, not a generated
# wk-<name> alias: a generated alias could not carry the real entry's
# ProxyJump, and a second entry for the same machine is a second thing to
# keep right.
#
# Non-zero rather than fatal when there is none, matching the vm driver's
# contract for a guest that is not running (`wk enter --zed` treats it as
# "no route"). On the machine itself there is no route and never will be --
# that would be an ssh loop back to the host you are typing on.
t_ssh_host() {
    _remote_is_local && return 1
    _remote_require; echo "$WK_REMOTE_HOST"
}

t_store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/ws"
}

t_list() {
    # `|| true`: no ws directory yet is not an error, and under `pipefail` in
    # the caller a non-zero ls would fail the whole listing.
    { _rsh_q "ls -1 $(sh_quote "$(_remote_root)/ws") 2>/dev/null" 2>/dev/null || true; } \
        | while read -r n; do [ -n "$n" ] && printf '%s\tpresent\n' "$n"; done
}

# The whole lifecycle in one round trip, because every extra one is a
# handshake through a jump host and `wk status` asks per workspace:
#
#   no workspace directory          absent
#   directory, no `.wk-ready`       creating -- a clone that never finished,
#                                   or one whose ssh was cut mid-way
#   `.wk-ready`                     present
#   the machine did not answer      unreachable, never absent
#
# The marker is the point: without it, a half-cloned workspace reads as
# present, `wk new` refuses it as "already exists", and `wk build` builds
# the rubble.
t_info() {
    local ws out
    _remote_probe_try || { echo unreachable; return 0; }
    ws=$(_remote_ws "$1")
    out=$(_rsh_q "if [ ! -d $(sh_quote "$ws") ]; then echo absent;
                  elif [ -f $(sh_quote "$ws/$WK_READY_MARKER") ]; then echo present;
                  else echo creating; fi" 2>/dev/null) || out=unreachable
    printf '%s\n' "${out:-unreachable}"
}

# The far side's marker, which is the only copy of this fact. Asked through
# t_info so there is one round trip and one place that knows where the marker
# lives.
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

    # A finished workspace is somebody's work and is refused by name. A
    # half-made one is not: `wk new` has already destroyed it before getting
    # here (rule 3, wipe over repair), so reaching this with anything but
    # absent means the record and the machine disagree about something this
    # driver cannot resolve on its own.
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
        # A plain local clone, which git makes with hardlinks -- costs the
        # working tree and essentially no object storage, and unlike
        # --shared does not leave the workspace depending on a repository
        # the sysadmins repack on a schedule.
        #
        # origin is then re-pointed at WebKit/WebKit like everywhere else;
        # the machine's copy stays as `shared`, since calling it `origin`
        # would make `git log origin/main` answer for that box's last fetch
        # rather than upstream.
        info "cloning from $ref (this machine's shared WebKit, hardlinked)"
        _rsh_q "set -e
            mkdir -p $(sh_quote "$root/ws") $(sh_quote "$root/cache/ccache")
            git clone --quiet -b main $(sh_quote "$ref") $(sh_quote "$ws/WebKit")" \
            || die "could not clone $ref on $WK_REMOTE_HOST"
        _remote_wire "$ws/WebKit"
    else
        # No shared repository, so we keep one of our own and clone from it
        # with --shared: a checkout and no objects at all. Both halves of that
        # -- keeping it and fetching into it -- are _remote_mirror_update, which
        # `t_sync` is the other caller of.
        _remote_mirror_update "$root"
        _rsh_q "git clone --quiet --shared -b main $(sh_quote "$root/mirror") \
                          $(sh_quote "$ws/WebKit")" \
            || die "could not create the checkout on $WK_REMOTE_HOST"
        # Same wiring as every other target: origin is WebKit/WebKit, the forks
        # are here, and the local mirror keeps a name of its own -- the
        # workspace borrows its objects through --shared either way.
        _remote_wire "$ws/WebKit"
    fi

    # Same ceiling and words as every other cache (ccache_conf_render,
    # lib/store.sh), rendered here and written there so the far machine needs
    # no wk-tools to get it right. Only when absent, so hand-tuned ccache
    # survives. Guarded because not every caller of this driver sources
    # lib/store.sh; unguarded, a `command not found` in a command
    # substitution would silently write an empty config.
    command -v ccache_conf_render >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
    _rsh_q "[ -f $(sh_quote "$root/cache/ccache/ccache.conf") ] ||
            printf %s $(sh_quote "$(ccache_conf_render)") \
              > $(sh_quote "$root/cache/ccache/ccache.conf")" || true

    ensure_dir "$(wk_ws_dir "$name")"

    # Last, on the far side, and that is the whole point: this file is what
    # says the clone, the wiring and the ccache config all happened. Written
    # over there rather than here so that it survives this end going away --
    # an ssh cut mid-clone leaves no marker and the workspace reads `creating`
    # from any machine that asks, including the box itself.
    _rsh_q "touch $(sh_quote "$ws/$WK_READY_MARKER")" \
        || die "could not mark '$name' ready on $WK_REMOTE_HOST -- treat it as half-made
    and re-run 'wk new $name --target ${WK_TARGET:-remote}'"
    info "remote workspace '$name' created on $WK_REMOTE_HOST ($ws)"
}

t_exec() {
    local name="$1"; shift
    _rsh "cd $(sh_quote "$(t_src "$name")") && $(sh_quote "$@")"
}

# One file out of the machine, byte for byte (lib/target.sh, t_pull).
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

# The build, and only the build, is serialised: two of your own builds must
# not stack on a machine already shared with other people, and locking
# t_exec instead would silently block every `wk run` and one-line probe
# behind an hour-long build.
#
# The lock is taken *on the machine that builds*, by lib/lockrun.sh in the
# copy of wk-tools t_sync_tools has just pushed there -- it cannot be taken
# here, since a lock dies with its holder and the holder that matters is the
# build, not the ssh session a detached build outlives by hours. Not a
# `flock`: its descriptor is inherited, so anything the build leaves running
# would hold the machine's lock for as long as it lives (lib/common.sh).
#
# nice and ionice are here as well as in build-in-target.sh: this end knows
# the target is shared, and the lock has to be outside them either way.
#
# ionice only when the probe found one: it is util-linux, so it buys nothing
# on Darwin or a BSD and there is no portable stand-in worth reaching for.
# Not a fallback -- the probe already asked the machine once
# (_remote_probe_parse's ionice line), and a machine without it gets `nice`
# alone rather than a second, untested scheduling path.
t_exec_build() {
    local name="$1"; shift
    local log tee_to

    # _remote_ws forces the probe, so _WK_REMOTE_IONICE is already populated
    # by the time `prio` reads it below.
    log="$(_remote_ws "$name")/build.log"

    # Nothing to tee into on the machine itself: $WK_STORE is the remote root
    # there, so run_watched is already writing this exact file. Two writers on
    # one log interleave, and the result reads like a corrupted build.
    tee_to=" 2>&1 | tee $(sh_quote "$log")"
    _remote_is_local && tee_to=""

    local prio="nice -n 19"
    [ "${_WK_REMOTE_IONICE:-no}" = yes ] && prio="$prio ionice -c3"

    # tee, so the canonical log is written *on the machine that is building*
    # while the same bytes stream back for the watchdog and the terminal here.
    # Without it a build started from the workstation would leave nothing
    # behind on the box, and `wk logs` in a shell there -- the shell most
    # likely to be watching -- would have nothing to show.
    #
    # pipefail with it, or tee's exit status becomes the build's and every
    # failure reads as success.
    _rsh_q "set -o pipefail
          cd $(sh_quote "$(t_src "$name")") && \
          $(sh_quote "$(t_tools "$name")/lib/lockrun.sh") remote-build -w 3600 -- \
          $prio $(sh_quote "$@")$tee_to"
}

# The status file's one copy, on the machine itself -- never a local shadow.
# In local mode $WK_STORE is already the remote root, so this is the same
# write the generic default (lib/target.sh) would do; written out explicitly
# rather than falling through, since the ssh branch below needs its own
# path anyway and one function reading both ways is easier to trust than a
# fallthrough plus an override.
t_status_put() {
    local name="$1" ws
    if _remote_is_local; then
        ws="$(wk_ws_dir "$name")"
        cat > "$ws/build.status"
        return 0
    fi
    # Resolved with stdin closed and *before* the pipeline below: the lookup
    # can itself reach the machine, and an ssh in a command substitution reads
    # the stdin it inherits.
    ws=$(_remote_ws "$name" </dev/null)
    # The log= field is rewritten on the way: the file it names is this side's
    # transcript, and on the machine the canonical log sits beside the
    # checkout. A status file pointing at a path that does not exist over there
    # would cost `wk status` its liveness check -- the one part that answers
    # "is it still moving".
    #
    # No `|| true`: a write that silently fails leaves `wk status` reporting
    # whatever was there before, forever, about a build that may have moved
    # on. Warn instead -- loud, but not fatal to a build that otherwise
    # succeeded over an ssh hiccup in the one write that records it.
    sed "s|^log=.*|log=$ws/build.log|" \
        | _rsh "cat > $(sh_quote "$ws/build.status")" \
        || warn "could not record '$name's build state on $WK_REMOTE_HOST -- 'wk status $name'
    may show stale information until it answers again"
}

# `wk`, run on the machine itself: it answers about its own workspaces with
# its own store, where the canonical build state lives, so `wk status` and
# `wk logs` ask it rather than reporting the half of the truth this side
# holds.
#
# Refused unless the machine has been provisioned: `wk remote setup` puts
# wk-tools and the marker there, and without them the command would either
# not exist or resolve a target it has never heard of.
t_has_wk() {
    _remote_is_local && return 1
    # Before resolving the remote root, which is what `t_tools` needs and which
    # only the capacity probe knows: on a machine that is off, resolving it dies
    # inside a command substitution and prints a connection error in the middle
    # of a listing that was about to say "unreachable" perfectly clearly.
    _remote_probe_try || return 1
    # A peer has no marker and must not be given one: the marker is what makes
    # a machine refuse the host-only commands on itself, and a workstation
    # needs those. Its own wk being there is the whole qualification.
    if _remote_peer; then
        _rsh_q "test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
        return $?
    fi
    _rsh_q "test -f \$HOME/.wk-remote && test -x $(sh_quote "$(t_tools '')/wk")" 2>/dev/null
}

# Two variables travel with a delegated command as environment, not as
# arguments: a *peer* runs its own checkout of this repository, kept by git,
# so the two sides are the same code only after both have pulled. An unknown
# argument is fatal on an old copy (`require_name --label` on a tree that
# predates `--label` kills the delegated status), while an unknown variable
# is silently ignored, so the old side answers as it always did.
t_wk() {
    _rsh "cd \$HOME && \
        ${WK_ROW_LABEL:+WK_ROW_LABEL=$(sh_quote "${WK_ROW_LABEL:-}") }\
        ${WK_NO_DELEGATE:+WK_NO_DELEGATE=1 }\
        $(sh_quote "$(t_tools '')/wk") $(sh_quote "$@")"
}

# Detached on the machine, so this end can go away. The redirections matter
# as much as nohup: ssh's own session ends when its channel closes, and a
# child still holding the tty or the pipe is killed with it. The log is not
# lost -- the far-side `wk build` writes build.log and build.status beside
# the checkout, which is where `wk logs` and `wk status` already look.
t_wk_detach() {
    _rsh_q "cd \$HOME && nohup $(sh_quote "$(t_tools '')/wk") $(sh_quote "$@") \
                >/dev/null 2>&1 </dev/null & echo \$!"
}

# The same, with a pty: `wk sudo setup` over there prompts for a password,
# and sudo refuses to read one without a terminal.
t_wk_tty() {
    if _remote_is_local; then
        t_wk "$@"
        return $?
    fi
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh -t $(_ssh_opts) "$WK_REMOTE_HOST" \
        "cd \$HOME && $(sh_quote "$(t_tools '')/wk") $(sh_quote "$@")"
}

# A pty, for anything with a full-screen UI. ssh gives a command no terminal
# unless it is asked to, and `wk run --lldb` without one is a debugger prompt
# that prints, accepts nothing, and dies on the first ctrl-c.
t_exec_tty() {
    local name="$1"; shift
    if _remote_is_local; then
        # Already on a terminal, if the caller had one.
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

# rsync rather than a mount or a git clone: the tooling is small, this is the
# same push the vm target does, and rsync makes it a no-op when nothing
# changed. --delete so a file removed here is removed there; excluding .git
# because the remote copy is a deployment, not a checkout to work in.
t_sync_tools() {
    local name="$1" dest
    dest=$(t_tools "$name")

    # A peer keeps its own copy, under git, and it is not ours to overwrite:
    # rsync --delete onto another workstation's checkout would throw away
    # whatever it had uncommitted -- which on a machine somebody works on is
    # the most valuable thing in the tree.
    if _remote_peer; then
        debug "not pushing wk-tools to $WK_REMOTE_HOST: it is a workstation with its own checkout"
        return 0
    fi

    # On the machine itself the tooling being run *is* the tooling: `wk` there
    # is $dest/wk, reached through the PATH entry the shell rc adds. Rsyncing a
    # tree onto itself mid-command would be, at best, pointless.
    if _remote_is_local; then
        [ "$WK_ROOT" = "$dest" ] || warn "running $WK_ROOT/wk, but this target's tooling is $dest"
        return 0
    fi

    debug "syncing wk-tools -> $WK_REMOTE_HOST"
    # rsync creates the last path element but not a missing parent, and on a
    # fresh machine `wk remote setup` gets here before anything has made the
    # remote root -- t_create's mkdir has never run.
    _rsh_q "mkdir -p $(sh_quote "$dest")"
    rsync -az --delete --exclude '.git/' \
        -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/" "$WK_REMOTE_HOST:$dest/"
}

# See t_wiring_args in lib/target.sh: the machine's own shared WebKit when
# it advertises one, our own mirror otherwise, and the ssh config under the
# wk root either way -- which is what makes fork push URLs resolve on a box
# whose ~/.ssh is not ours to edit.
#
# Wired from the three lines above so creation and `wk remotes --fix` cannot
# drift apart. `origin` stays upstream on every target: `git clone --shared
# <mirror>` leaves origin pointing at the mirror otherwise, and a workspace
# whose origin is a local copy answers `git log origin/main` for whatever
# that copy last fetched.
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

# `wk sync`, for a machine of its own.
#
# On a host, sync means the store: a bare mirror of every upstream plus the
# hardlinked base snapshots the overlay scheme is built on. None of that
# exists here, so a bare `wk sync` is refused on this machine (`wk`
# dispatch, is_host_only), and this is what takes its place: the two things
# that *do* go stale on the far end of a target.
#
# The tooling first, since it fails confusingly: every delegated command
# runs the machine's own copy of wk-tools, so a stale copy answers a
# question this side did not ask (`unknown option --quiet` from a command
# that works fine where it was typed). `wk status` names the drift; without
# this the only fix was a full `wk remote setup`.
#
# Then the WebKit objects, and which ones depends on where the machine's
# workspaces come from: our own mirror is ours to fetch, a shared repository
# in the MOTD is not -- it belongs to the sysadmins and fetching into it
# would be writing to somebody else's repository. Said rather than silently
# skipped, because "wk sync did nothing and reported success" is
# indistinguishable from a bug.
t_sync() {
    local ref
    _remote_probe

    # A peer's store is its own, and syncing it means running `wk sync` over
    # there -- 13 GB of fetch and a new base snapshot, on a machine somebody
    # else may be working on. So it happens only when that machine was asked
    # for by name: WK_SYNC_NAMED is set by cmd/sync for `--target <it>` and
    # unset when `--target all` merely walked onto it. Doing it either way
    # would mean one absent-minded `wk sync --target all` publishing snapshots
    # on every machine in the fleet.
    if _remote_peer; then
        if [ -z "${WK_SYNC_NAMED:-}" ]; then
            info "$WK_REMOTE_HOST is a workstation with a store of its own -- skipped"
            log  "  sync it by name:  wk sync --target $WK_TARGET"
            return 0
        fi
        info "running 'wk sync' on $WK_REMOTE_HOST -- its store, its snapshot"
        t_wk sync
        return $?
    fi

    t_sync_tools ""
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
# All three answer for the remote machine, which is the whole point: the polite
# calculation in lib/resources.sh subtracts load from cores and divides memory
# by the per-job working set, and every one of those numbers has to be the far
# end's or the result is a job count for the wrong computer.

t_cores()  { _remote_probe; echo "${_WK_REMOTE_CORES:-1}"; }
t_load()   { _remote_probe; echo "${_WK_REMOTE_LOAD:-0}"; }
t_mem_mb() { _remote_probe; echo "${_WK_REMOTE_MEM:-1024}"; }
