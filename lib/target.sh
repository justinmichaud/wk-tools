# Loading a target driver, and the defaults every driver inherits.

# Commands under cmd/ call only the contract below, never podman, tart or ssh
# directly, so adding a target means adding one file under targets/.

# Required of a driver:
#   t_create <name> [base]   bring the environment into existence
#   t_exec   <name> <cmd..>  run a command in it
#   t_enter  <name>          interactive shell
#   t_destroy <name>         remove it and everything it created
#   t_info   <name>          the lifecycle in one word: "absent", "creating",
#                            the driver's word for an existing environment
#                            ("running"/"exited"/"present"), or "unreachable"
#   t_list                   "<name>\t<state>" per line

# Optional, defaulted here. A driver overrides only what is genuinely
# different about it; re-stating a default is how the two copies drift apart.
#   t_src <name>       where the WebKit checkout is *inside* the target
#   t_arch <name>      the architecture the target runs natively
#   t_tools <name>     where wk-tools is inside the target
#   t_sync_tools <n>   push wk-tools in (no-op when it is bind-mounted)
#   t_sync             refresh this target's own furniture: its tooling copy,
#                      and its mirror and snapshot when it keeps its own
#   t_ssh_host <name>  ssh destination, for Zed and the generated alias
#   t_ssh_user <name>  the account inside the workspace an editor logs into
#   t_ssh_proxy <name> what to run *on this machine* to speak ssh to the
#                      workspace, for one with no address to connect to
#   t_needs_base       0 if `wk new` must resolve a base snapshot first
#   t_start <name>     bring a stopped workspace's environment back (see below)
#   t_stop <name>      stop a workspace's environment, leaving it on disk (see below)
#   t_cores/t_mem_mb   the resources the target actually has
#   t_load             the load already on the target, for the polite sizing
#   t_ccache_dir       where ccache keeps its cache inside the target
#   t_store_init       create the host-side directories this target needs
#   t_created <name>   is creation's completion marker there? (see .wk-ready)
#   t_ready            block until a new workspace finished initialising
#   t_exec_build       t_exec, for a build specifically (see below)
#   t_status_put       write the build status where the target's own side is
#   t_has_wk / t_wk    run `wk` on the target's own machine, if that is one
#   t_delegates        must a command about a workspace here run on that
#                      machine rather than this one? (`wk`, delegate_target)

# targets/local.sh is the degenerate case: the target is the machine already
# running the command (see "am I a workspace?" below).

# WK_READY_TIMEOUT/WK_READY_WAIT (below, t_ready/wait_ready): how long to
# poll a workspace that is still being created before giving up, overridable
# for a target genuinely slower than these assume.
# WK_MARKER/WK_REMOTE_MARKER (below, wk_marker/wk_remote_marker): the marker
# file paths that say "this machine IS a workspace" / "IS a remote target's
# host" -- overridable so a test can run marker-gated code (in_workspace,
# in_remote_host) without writing into this machine's real $HOME.
# WK_NO_DELEGATE (below, walk_targets): set by one machine answering another
# workstation's fleet-wide `wk ls`/`wk status`, so it reports only what it
# holds instead of delegating to every machine *it* knows too.
# WK_TARGET_KIND: set by load_target from the target name; never hand-set.

t_src()        { echo "/src/WebKit"; }
t_arch()       { echo native; }      # only the container driver differs; see lib/arch.sh
t_tools()      { echo "/opt/wk-tools"; }
t_ccache_dir() { echo "/ccache"; }   # inert on the Apple ports (no ccache)
t_sync_tools() { :; }

t_sync()       { :; }               # bring this target's own furniture up to date: its
                                    # tooling copy, and its store when it keeps one
t_prefetch()   { :; }                # ask this target whatever a report will need

# t_wiring_args -- how this target's checkouts are wired, for whoever
# re-asserts it: three lines, any of which may be empty --
#   <name of the local-copy remote>   (mirror, shared)
#   <its url, on the target>
#   <an ssh config the checkout must use, if not ~/.ssh/config>
t_wiring_args() { printf '\n\n\n'; }
t_ssh_host()   { echo "wk-$1"; }

t_ssh_prepare() { :; }   # point an editor at this target over ssh; nothing for one already an ssh destination

# The two halves of an editor's route that only the driver knows. Both refuse
# rather than guess: a target reached by address has no proxy to run, and one
# with no ssh account of its own has no editor route at all.
t_ssh_user()   { return 1; }
t_ssh_proxy()  { return 1; }
t_needs_base() { return 0; }

# A machine this target only *reaches* (remote) is always reachable, so
# there is nothing to start by default; container.sh, vm.sh and
# targets/local.sh override with something real to do.
t_start() { info "'$WK_TARGET' has no notion of starting a single workspace -- nothing to bring up for '$1'"; }

# Refuses loudly rather than silently doing nothing, which would read as
# "stopped" about a workspace left running.
t_stop() { die "the '$WK_TARGET' target has no notion of stopping a single workspace -- '$1' is left running"; }
t_store_init() { store_init; }

t_home()       { echo "$HOME"; }   # the workspace user's home dir, as seen from inside it

# t_pull <name> <src-in-target> <dest-on-host> -- copy one file out, byte for
# byte. `t_exec <ws> cat <file>` into a redirect silently corrupts binary
# data; anything over a network overrides this (`podman cp`, `scp`).
t_pull() {
    local name="$1" src="$2" dest="$3"
    cp -f "$src" "$dest"
}

# t_pull_dir <name> <src-dir-in-target> <dest-dir-on-host> [--exclude PAT]...
# A separate hook rather than a loop over t_pull: a per-file round trip over
# ssh is minutes of latency. *Contents* are replaced, not merged, so a
# re-stage can't leave last build's binaries behind; a driver that cannot
# honour an exclude refuses rather than copying everything.
_t_pull_dir_excludes() {
    _T_PULL_EXCLUDES=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --exclude) _T_PULL_EXCLUDES+=("--exclude" "${2:-}"); shift 2 ;;
            *) die "t_pull_dir: unknown option $1" ;;
        esac
    done
}

t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    _t_pull_dir_excludes "$@"
    mkdir -p "$dest"
    rsync -a --delete ${_T_PULL_EXCLUDES[@]+"${_T_PULL_EXCLUDES[@]}"} "$src/" "$dest/"
}

# _ssh_opts_base <connect-timeout> -- never interactive, bounded connect; the
# rest of each driver's ssh options genuinely differs and stays with it.
_ssh_opts_base() {
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=${1:-10}"
}

# t_spawn <name> <log> <pidfile> <cmd...> -- start a command detached from
# *this* process and return at once; `log`/`pidfile` are paths inside the
# target. Over ssh, `nohup cmd &` survives the connection closing; under
# `podman exec` it does not, so the container driver overrides this. A
# process nobody forked can't be waited for, so a caller decides "finished"
# from what it left behind -- why yocto's wrapper prints a marker.
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    t_exec "$name" bash -lc "setsid nohup $(sh_quote "$@") \
        > $(sh_quote "$log") 2>&1 < /dev/null & echo \$! > $(sh_quote "$pidf")"
}

# This workspace's checkout branch, or `-` when unknowable without starting
# something. targets/container.sh overrides since an overlay's HEAD is a
# file on the host.
t_branch() {
    local out
    # Anything not present and answering is `-`: exec'ing into a container
    # still provisioning, or a connection error, would die mid-listing.
    case "$(t_info "$1")" in
        absent|creating|broken|unreachable) echo -; return 0 ;;
    esac
    out=$(t_exec "$1" git -C "$(t_src "$1")" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '\r') || out=""
    printf '%s' "${out:--}"
}

# --- creation's completion marker -------------------------------------------
# `.wk-ready`, written as the *last* act of creating a workspace, by every
# driver: present means creation finished, absent means still going or
# killed. Next to the artifact, never a record on the driving side. vm is
# the deviation -- a freshly cloned guest is visible only from this host, so
# its marker lives in the host-side workspace directory instead.
WK_READY_MARKER=".wk-ready"

# The interesting case t_info alone can't see: a marker present with the
# environment *not*, a workspace removed by hand (rule 5). Defaults to yes,
# so a target with no marker mechanism reads as finished.
t_created() { return 0; }

# Waits until a freshly created workspace is usable, polling the driver's
# own lifecycle answer. A driver with something better to say overrides it.
t_ready() {
    local name="$1" i=0 max="${WK_READY_TIMEOUT:-300}"
    while [ "$i" -lt "$max" ]; do
        case "$(t_info "$name")" in
            creating) ;;
            # Neither gets better by waiting five minutes.
            absent|unreachable) return 1 ;;
            *) return 0 ;;
        esac
        sleep 1; i=$((i + 1))
    done
    return 1
}
# t_cores/t_mem_mb differ from lib/resources.sh: a vm target must be sized
# from the *guest*'s memory, not the host's.
t_cores()      { envelope_cores; }   # t_cores <name>
t_mem_mb()     { envelope_mem_mb; }  # t_mem_mb <name>

t_exec_tty()   { t_exec "$@"; }     # interactive exec: a full-screen UI needs a pty, ssh doesn't allocate one unasked
t_lldb_opts()  { :; }               # lldb options a target needs before it can launch anything

# Same exec as everywhere except where a build must be serialised: applied
# to t_exec, the lock would also make `wk run`/`wk status` block for no reason.
t_exec_build() { t_exec "$@"; }

# Build state, in exactly one place: here, unless the target is a *machine
# of its own*, which overrides it and writes there instead (over ssh) since
# a second copy on the driving side would go stale.
t_status_put() { local n="$1" ws; ws="$(wk_ws_dir "$n")"; cat > "$ws/build.status"; }
t_has_wk()    { return 1; }         # is there a far side that can answer?

# Is this side able to act on a workspace here at all? A target whose
# workspaces belong to another machine says no, and `wk` hands the whole
# command over rather than each command deciding for itself.
t_delegates() { return 1; }
t_far_side()  { echo none; }        # answering | unreachable | no-wk | none (not a machine of its own)
t_wk()        { return 1; }         # t_wk <args...>, its exit status is the answer
t_wk_tty()    { t_wk "$@"; }        # t_wk with a terminal, for far-side commands that prompt a human

t_load() { awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0; }   # whole cores; build_jobs polite subtracts it

# Which target a *named* workspace lives on, so `wk build`, `wk pr` and the
# macOS dispatcher cannot reach three different machines for one name. No
# registry: every fact is recomputed from evidence -- an explicit WK_TARGET
# overrides everything; otherwise every target is asked directly (below).
#
# ws_on_target <target> <name> -- three tests, because each sees what the
# others cannot: the workspace directory (only where the store is on this
# machine), t_info ("unreachable" is not evidence the workspace exists), and,
# while `wk new` is still creating it, the status file it wrote, trusted only
# while the process writing it is alive.
ws_on_target() {
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      command -v detach_alive >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
      load_target "$1" >/dev/null 2>&1 || exit 1
      [ -d "$(wk_ws_dir "$2")" ] && exit 0
      if command -v t_info >/dev/null 2>&1; then
          case "$(t_info "$2" 2>/dev/null)" in
              absent|unreachable|"") ;;
              *) exit 0 ;;
          esac
      fi
      sf=$(ws_status_file "$2")
      [ -f "$sf" ] && detach_alive "$sf" )
}

ws_exists() { # <name>
    local name="$1" t
    if [ -n "${WK_TARGET:-}" ]; then
        ws_on_target "$WK_TARGET" "$name" && return 0
        # ws_on_target says no for two reasons a caller can't tell apart from
        # its exit status alone: the target answered and doesn't have it, or
        # it didn't answer. Only the first is evidence of absence; let the
        # second through and let ws_state report honestly.
        ( load_target "$WK_TARGET" >/dev/null 2>&1
          [ "$(t_info "$name" 2>/dev/null)" = unreachable ] )
        return $?
    fi
    for t in $(target_all 2>/dev/null); do
        ws_on_target "$t" "$name" && return 0
    done
    return 1
}

ws_target() { # <name>
    local name="$1" t hits=""
    if [ -n "${WK_TARGET:-}" ]; then printf '%s' "$WK_TARGET"; return 0; fi
    for t in $(target_all 2>/dev/null); do
        ws_on_target "$t" "$name" && hits="$hits $t"
    done
    # shellcheck disable=SC2086 -- deliberate word splitting of the collected hits.
    set -- $hits
    case "$#" in
        # Nothing here knows this name. On a macOS host a real container
        # workspace whose VM is stopped looks the same, so this is "not
        # visible from here", not "does not exist" -- default_target's
        # "container" answer forwards the command into the VM to resolve.
        0) default_target; return 0 ;;
        1) printf '%s' "$1"; return 0 ;;
    esac
    die "workspace '$name' exists on targets:$hits -- this cannot be
    resolved; remove one, or set WK_TARGET"
}

# `wk new` writes $WK_STORE/create/<name>.{status,log} as its first act, so a
# later command can tell a half-made workspace from a missing one. A record
# speaks for a workspace only while creation is genuinely in flight, checked
# against the process table, never a file that outlives it.
gc_creation_records() {
    local sf n t found
    [ -d "$(ws_state_dir)" ] || return 0
    for sf in "$(ws_state_dir)"/*.status; do
        [ -f "$sf" ] || continue
        n=$(basename "$sf" .status)
        detach_alive "$sf" && continue
        found=""
        for t in $(target_all 2>/dev/null); do
            if ( load_target "$t" >/dev/null 2>&1; [ -d "$(wk_ws_dir "$n")" ] ); then
                found=1; break
            fi
        done
        [ -z "$found" ] || continue
        info "removing orphaned creation record for '$n' (no live creation, no workspace)"
        rm -f "$sf" "$(ws_create_log "$n")"
    done
}

# --- am I a workspace? -------------------------------------------------------
# A workspace is a whole machine; the `wk` inside one acts on it rather than
# reaching one from outside. Provisioning writes a marker file saying so and
# naming the checkout (container/firstrun.sh, targets/vm.sh) -- a file, not
# an environment variable, since it must be true for an ssh command, a hook
# or a `bash -c` from an agent, not only an inheriting shell.
wk_marker() { echo "${WK_MARKER:-$HOME/.wk-workspace}"; }

in_workspace() { [ -f "$(wk_marker)" ]; }

marker_field() { kv_field "$1" "$2"; }   # one key=value field from a marker file

wk_marker_field() { marker_field "$(wk_marker)" "$1"; }

# --- am I a machine that *hosts* workspaces? ---------------------------------
# A shared build box holds several workspaces at once, so the marker above is
# the wrong shape. This one says "this machine is the far end of a remote
# target", and names which:
#   target=devbox-arm64-2
#   root=/home/you/wk
# Written by `wk remote setup`.
wk_remote_marker()   { echo "${WK_REMOTE_MARKER:-$HOME/.wk-remote}"; }
in_remote_host()     { [ -f "$(wk_remote_marker)" ]; }
wk_remote_field()    { marker_field "$(wk_remote_marker)" "$1"; }

wk_self() { wk_marker_field name; }   # the workspace this machine *is*, or empty on a host

# The target for a workspace ws_target could not find anywhere: `container`
# on a host, `local` inside one, since there's nothing else in there.
default_target() {
    if in_workspace; then echo local; return 0; fi
    local t; t=$(wk_remote_field target)
    if [ -n "$t" ]; then echo "$t"; return 0; fi
    echo container
}

# The config to build, run or test when the caller names none. jsc-release is
# right on a host and in a container, but meaningless in a macOS guest, which
# can build nothing but the Apple ports; the marker carries the answer.
default_config() {
    local c=""
    in_workspace && c=$(wk_marker_field config)
    printf '%s' "${c:-jsc-release}"
}

# The config this workspace was last *built* with, or empty when it has
# never been built here. Evidence, not a preference: `wk test <ws> --layout`
# needs the config just built, not the jsc-release default (--jsc-only, the
# wrong tree for layout tests).
last_built_config() {
    local name="$1"
    ( command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
      load_target "$(ws_target "$name")" >/dev/null 2>&1
      kv_field "$(wk_ws_dir "$name")/build.status" config 2>/dev/null ) || true
}

# --- generated ssh aliases ---------------------------------------------------
# Written under ~/.ssh/config.d, not ~/.ssh/config itself, so it can be
# rewritten freely without touching hand-maintained entries. Rewritten, not
# appended: a VM's address changes on every boot.

wk_ssh_conf() { echo "$HOME/.ssh/config.d/wk"; }

ssh_alias_remove() {
    local conf tmp
    conf=$(wk_ssh_conf)
    [ -f "$conf" ] || return 0
    grep -q "^Host wk-$1\$" "$conf" || return 0
    tmp=$(mktemp)
    awk -v h="Host wk-$1" '
        $0 == h { skip = 1; next }
        /^Host / { skip = 0 }
        !skip { print }
    ' "$conf" > "$tmp"
    mv "$tmp" "$conf"
}

# ssh_alias_set <name> <hostname> <user> [identity] [extra-line...]
# Extra lines are for a target not reached by address at all -- a container
# is reached by a ProxyCommand over `podman exec` -- passed in since which
# lines are needed is the driver's knowledge.
ssh_alias_set() {
    local name="$1" hostname="$2" user="$3" conf extra
    conf=$(wk_ssh_conf)
    ensure_dir "$(dirname "$conf")" 0700
    ssh_alias_remove "$name"

    # A per-target identity only when there is one: a macOS guest has a key
    # of its own; a container authorises this machine's zed key (below).
    extra=""
    [ -n "${4:-}" ] && extra="    IdentityFile $4
    IdentitiesOnly yes"
    shift 4 2>/dev/null || shift $#
    while [ $# -gt 0 ]; do extra="$extra
    $1"; shift; done

    cat >> "$conf" <<EOF

Host wk-$name
    # generated by wk -- removed by wk rm
    HostName $hostname
    User $user
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
$extra
EOF
}

# --- the zed key --------------------------------------------------------------
# One key per machine, for reaching a container workspace's sshd over
# `podman exec`. Not a deploy key ($WK_STORE/secrets are push-only GitHub
# credentials, read-only to a workspace): this machine authenticates to its
# own containers, so it is machine-local, generated on demand. Its own key
# rather than ~/.ssh/id_ed25519, which may be passphrase-protected or held
# in an agent -- not discoverable from an editor launch.
zed_key() { echo "$(wk_state_dir)/ssh/zed_ed25519"; }

# The key the editor will present -- which is not this machine's whenever
# this machine is preparing a workspace for the one that asked (WK_ZED_PUBKEY,
# set by the remote driver reaching into a peer's workspace).
zed_key_pub() {
    [ -z "${WK_ZED_PUBKEY:-}" ] || { printf '%s\n' "$WK_ZED_PUBKEY"; return 0; }
    local k; k=$(zed_key)
    if [ ! -f "$k" ]; then
        ensure_dir "$(dirname "$k")" 0700
        ssh-keygen -q -t ed25519 -N '' -C "wk zed key ($(hostname -s 2>/dev/null || echo host))" \
            -f "$k" || return 1
        changed "generated this machine's zed key ($k)"
    fi
    cat "$k.pub"
}

# Every target this machine could have a workspace on: container and vm
# unconditionally, the build box this machine is the far end of (if any), and
# every machine configured under targets/hosts/*.conf.
target_all() {
    local d f t seen=" container vm "
    # A missing podman or tart answers every later question with nothing,
    # not a loud failure (see t_list in each).
    echo container
    echo vm

    # On a shared build machine the workspaces were created from somewhere
    # else, so nothing here is configured -- without this a bare `wk status`
    # on the box would report nothing about the ones it has.
    t=$(wk_remote_field target)
    if [ -n "$t" ]; then seen="$seen$t "; echo "$t"; fi

    # Skipped on a machine that is the far end of a target: a delegated `wk
    # ls` there would otherwise pay an ssh timeout for every machine it has
    # no route, key or business reaching. A peer workstation, which does
    # drive things, keeps the whole registry.
    local me=""
    if ! in_remote_host && [ -z "${WK_IN_VM:-}" ]; then
        me=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')
        for d in "$(target_registry_dir)"; do
            [ -d "$d" ] || continue
            for f in "$d"/*.conf; do
                [ -f "$f" ] || continue
                t=$(basename "$f" .conf)
                case "$seen" in *" $t "*) continue ;; esac
                # ...and never itself: otherwise a bare `wk status` would ssh
                # to itself and report itself unreachable, with no sshd inside.
                [ -n "$me" ] && [ "$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')" = "$me" ] && continue
                seen="$seen$t "
                echo "$t"
            done
        done
    fi
}

# Call <fn> once per configured remote machine -- every target_all entry
# except container, vm and local. <fn> gets the target name as $1 plus any
# extra arguments; its exit status folds into the worst one seen.
for_each_machine() {
    local fn="$1"; shift
    local t rc worst=0
    for t in $(target_all); do
        case "$t" in container|vm|local) continue ;; esac
        rc=0; "$fn" "$t" "$@" || rc=$?
        [ "$rc" -gt "$worst" ] && worst=$rc
    done
    return "$worst"
}

# machine_answers <target> -- 0 when the target's own machine can run `wk`
# for it; otherwise prints the fan-out line (`wk push --all`, `wk sudo
# --all`) saying why not, and returns 1. Down and unprovisioned are told
# apart because their remedies differ.
machine_answers() {
    case "$(t_far_side)" in
        answering)   return 0 ;;
        unreachable) printf '%-22s %s\n' "$1" "unreachable over ssh ($(wk_ssh_timeout)s) -- off, or not on the tailnet" ;;
        no-wk)       printf '%-22s %s\n' "$1" "no wk-tools there yet -- 'wk remote setup $1'" ;;
        *)           printf '%-22s %s\n' "$1" "not a machine of its own" ;;
    esac
    return 1
}

# --- the podman machine (macOS) -----------------------------------------------
# Every workspace container lives inside one podman machine, so `wk
# start`/`wk stop` run a command inside it from the host.

# podman errors on an unknown machine name rather than reporting one, so
# "absent" has to be read back off the failure.
_machine_state() {
    podman machine inspect "$1" --format '{{.State}}' 2>/dev/null || echo absent
}

# Run a command inside the podman machine from the host, or in place already
# inside a workspace (WK_IN_VM) or on Linux. Named apart from _vm()
# (targets/vm.sh's Tart guest) so sourcing both does not clobber either.
# </dev/null is essential: a caller feeding this from a `while read` loop
# over a heredoc otherwise has its stdin read by podman-machine-ssh, and the
# first iteration swallows every line meant for the rest.
_in_machine() {
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        podman machine ssh "${WK_MACHINE:-wk}" "$@" </dev/null
    else
        bash -c "$*" </dev/null
    fi
}

# --- named targets -----------------------------------------------------------
# A target name is either one of the four built-in kinds -- container, vm,
# remote, local -- or a machine configured under targets/hosts/<name>.conf.
# `remote` is the one kind with several instances, since a shared build box
# isn't unique: the machine's own name is the target, and its conf says
# which driver to use and how to reach it. One conf per machine, pulled by
# every device (CLAUDE.md, "One path, not two"); a key or secret is
# per-device and does not live here, while the environment is per-invocation
# and still wins over one (_target_reset_vars).
target_registry_dir() { echo "$WK_ROOT/targets/hosts"; }
target_registry_conf(){ echo "$(target_registry_dir)/$1.conf"; }

# The driver a target name selects. Anything not a built-in kind must have a
# conf, and one that names no kind falls through to `remote`.
target_kind() {
    case "$1" in
        container|vm|remote|local) echo "$1"; return 0 ;;
    esac
    local c k=""
    c=$(target_registry_conf "$1")
    [ -f "$c" ] || return 1
    k=$(awk -F= '/^[[:space:]]*WK_TARGET_KIND[[:space:]]*=/ {
            gsub(/[ \t"'"'"']/, "", $2); print $2; exit }' "$c")
    echo "${k:-remote}"
}

# Per-target driver state, cleared before every load: `wk status` loads more
# than one target in a process, and without this the second remote machine
# would inherit the first one's host, root and measured capacity. The
# environment still wins over a conf, so what it supplied is snapshotted
# once and restored on each load.
_target_reset_vars() {
    if [ -z "${_WK_ENV_SEEDED:-}" ]; then
        _WK_ENV_REMOTE_HOST="${WK_REMOTE_HOST:-}"
        _WK_ENV_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"
        _WK_ENV_REMOTE_REFERENCE="${WK_REMOTE_REFERENCE:-}"
        _WK_ENV_SEEDED=1
    fi
    WK_REMOTE_HOST="$_WK_ENV_REMOTE_HOST"
    WK_REMOTE_ROOT="$_WK_ENV_REMOTE_ROOT"
    WK_REMOTE_REFERENCE="$_WK_ENV_REMOTE_REFERENCE"
    WK_REMOTE_LOCAL=""
    WK_MAX_JOBS=""
    # Per-machine CMake defaults come from the conf about to be sourced, so
    # the previous target's must not survive into this one.
    WK_TARGET_CMAKE=""
    unset _WK_REMOTE_PROBED _WK_REMOTE_HOME _WK_REMOTE_CORES \
          _WK_REMOTE_LOAD _WK_REMOTE_MEM _WK_REMOTE_REF_PROBED _WK_REMOTE_DOWN \
          _WK_PEER_LISTED _WK_PEER_ROWS \
          _WK_PEER_ROUTE_NAME _WK_PEER_ROUTE_USER _WK_PEER_ROUTE_SRC _WK_PEER_ROUTE_PROXY
}

# --- what state is this workspace in? ----------------------------------------
#   absent       nothing of it exists anywhere
#   creating     something of it exists, and creation never finished
#   present      it exists and creation finished
#   broken       creation finished and the environment is gone -- something
#                outside wk removed it (rule 5)
#   unreachable  the machine that holds it did not answer in time
#
# Derived on every call from evidence next to the artifacts, stored nowhere.
# `ws.status` is a claim by the creating process, never believed about a
# workspace that exists.
ws_state() {
    local name="$1" env ws
    env=$(t_info "$name" 2>/dev/null || echo absent)
    ws=$(wk_ws_dir "$name")

    case "$env" in
        creating|unreachable) echo "$env"; return 0 ;;
    esac

    if [ "$env" = absent ]; then
        [ -d "$ws" ] || { echo absent; return 0; }
        # A ws directory with no environment behind it. The marker decides
        # which: with it, something removed a finished environment by hand;
        # without it, it's rubble from an interrupted creation or `wk rm`
        # (rule 3 says remake).
        command -v status_field >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
        if t_created "$name" 2>/dev/null \
           || [ "$(status_field "$(ws_status_file "$name")" state)" = present ]; then
            echo broken
        else
            echo creating
        fi
        return 0
    fi

    if t_needs_base && [ ! -f "$ws/base-id" ]; then echo creating; return 0; fi
    echo present
}

ws_display_state() {   # the driver's own answer, except a workspace whose lifecycle is not `present` reports that instead
    local st; st=$(ws_state "$1")
    case "$st" in
        present) t_info "$1" ;;
        *)       echo "$st" ;;
    esac
}

# What the creating process claimed, and its transcript -- a claim, never
# the record (see ws_state). Beside the workspace directory, not inside it:
# a re-run of `wk new` over a half-made workspace *destroys* that directory
# first (rule 3), and the log the driver is writing to must not be the file
# it deletes.
ws_state_dir()   { echo "$WK_STORE/create"; }
ws_status_file() { echo "$(ws_state_dir)/$1.status"; }
ws_create_log()  { echo "$(ws_state_dir)/$1.log"; }

# `wk new` is a workstation command -- a machine that only *hosts* workspaces
# refuses it (is_lifecycle in `wk`) -- so printing it bare on a build box
# sends somebody straight to a refusal.
ws_remake_hint() {
    if in_remote_host; then
        printf 'from the workstation:  wk new %s --target %s' "$1" "$(wk_remote_field target)"
    else
        printf 'wk new %s%s' "$1" "${WK_TARGET:+ --target $WK_TARGET}"
    fi
}

# --- one gate: wait_ready ----------------------------------------------------
# Block until this workspace is usable, honest about every other outcome --
# the one place anything asks "may I act on this yet", so zed, a build and
# the babysitter cannot each answer it differently. Foreground by design: if
# this waiter is killed, only the waiting stops -- creation is detached and
# continues, and re-running the same command resumes.
wait_ready() {
    local name="$1" timeout="${2:-${WK_READY_WAIT:-1800}}"
    local st sf waited=0 said="" stage

    command -v detach_alive >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    sf=$(ws_status_file "$name")

    while :; do
        st=$(ws_state "$name")
        case "$st" in
        present)
            [ -z "$said" ] || info "'$name' is ready"
            return 0
            ;;
        absent)
            die "no such workspace: $name"
            ;;
        broken)
            die "'$name' exists as a record and not as a $WK_TARGET workspace: creation
    finished, and the environment is gone -- something outside wk removed it.
    Repair:  wk rm $name    (then 'wk new $name' if you still want it)"
            ;;
        unreachable)
            die "'$name' lives on a machine that did not answer ($(wk_ssh_timeout)s).
    Nothing is wrong with the workspace as far as this end can tell -- it
    cannot be reached to ask. Try again, or check the route:
        ssh -o BatchMode=yes ${WK_REMOTE_HOST:-the machine} true"
            ;;
        creating)
            if ! detach_alive "$sf"; then
                # A barrier, not a refusal (lib/common.sh's distinction):
                # a clone that finished and never got its marker looks
                # exactly like one cut in the middle.
                barrier "'$name' was never finished creating, and nothing is creating it now
    (the process that was is gone, with whatever connection started it).
    Usually there is nothing in one worth keeping, so remake it:
        $(ws_remake_hint "$name")
    --force uses it as it is, which is right when you can see that the
    checkout is complete and only the marker is missing."
                return 0
            fi
            if [ -z "$said" ]; then
                said=1
                stage=$(status_field "$sf" stage)
                info "waiting for '$name' to finish being created${stage:+ (at: $stage)}"
                log  "  follow it:  tail -f $(ws_create_log "$name")"
                log  "  this end can be killed; creation is detached and continues"
            fi
            ;;
        esac

        if [ "$waited" -ge "$timeout" ]; then
            die "'$name' was still $st after ${timeout}s.
    Creation is detached, so it may still be going: 'wk status $name' says
    whether the driver is alive, and $(ws_create_log "$name") says what it is doing."
        fi
        sleep 2; waited=$((waited + 2))
    done
}

# --- work a lock here cannot see --------------------------------------------
# A lock (hold_lock, lib/common.sh) dies with the process holding it -- right
# for a command that does its work and exits, but covers nothing for work
# *detached into the workspace* (`wk sysimage build --stage image --detach`
# starts bitbake through t_spawn and returns within the second). The
# interface instead: **a job detached into a workspace writes
# `$(t_home)/<job>.pid`, and removes nothing on the way out.**
ws_busy_reason() {
    local name="$1" ws p pid job
    ws=$(wk_ws_dir "$name")
    [ -d "$ws/home" ] || return 1
    for p in "$ws"/home/*.pid; do
        [ -f "$p" ] || continue
        pid=$(tr -dc '0-9' < "$p" 2>/dev/null) || true
        [ -n "$pid" ] || continue
        job=$(basename "$p" .pid)
        if t_exec "$name" kill -0 "$pid" >/dev/null 2>&1; then
            printf '%s (pid %s in the workspace)' "$job" "$pid"
            return 0
        fi
    done
    return 1
}

# --- one round of probes instead of N ----------------------------------------
# A bare `wk status` walks every target serially, and an ssh machine costs a
# full connect (WK_SSH_TIMEOUT) when off. One subshell per target asks its
# own machine and writes the answer where the serial pass will find it
# (t_prefetch, _remote_probe_file in targets/remote.sh); best-effort, a
# prefetch that fails leaves no file, and the walk asks the machine directly.
# WK_PREFETCH_DIR: not a setting -- this process sets it for itself (below)
# and every subshell it starts, to hand a probe's answer back without a
# variable a subshell cannot export to its parent. Plumbing, not a knob.
prefetch_targets() {
    local t
    [ $# -gt 0 ] || return 0
    command -v mktemp >/dev/null 2>&1 || return 0
    WK_PREFETCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/wk-probe.XXXXXX" 2>/dev/null) || return 0
    export WK_PREFETCH_DIR
    for t in "$@"; do
        # A subshell, so each target's driver load and globals stay its own.
        ( load_target "$t" >/dev/null 2>&1 && t_prefetch ) >/dev/null 2>&1 &
    done
    wait
}

prefetch_done() {
    [ -n "${WK_PREFETCH_DIR:-}" ] || return 0
    rm -rf "$WK_PREFETCH_DIR"
    unset WK_PREFETCH_DIR
}

walk_targets() {
    if [ -n "${WK_TARGET:-}" ]; then printf '%s\n' $WK_TARGET; return 0; fi
    if in_workspace; then default_target; return 0; fi

    # WK_NO_DELEGATE: one machine's contribution to somebody else's fleet
    # listing, so it reports what it holds and asks nobody -- without it, a
    # workstation asked by another workstation would ask every machine *it*
    # knows too, squaring the ssh work over the shared registry. A machine's
    # own target survives it (WK_REMOTE_LOCAL, set by `wk remote setup`),
    # decided from the conf so this costs no ssh.
    if [ -n "${WK_NO_DELEGATE:-}" ]; then
        local t
        for t in $(target_all); do
            case "$(target_kind "$t")" in
                remote) ( load_target "$t" >/dev/null 2>&1
                          [ -n "${WK_REMOTE_LOCAL:-}" ] ) || continue ;;
            esac
            printf '%s\n' "$t"
        done
        return 0
    fi

    target_all
}

# Every workspace a loaded target has, from both sides: the local store
# knows what this machine created, the driver knows what actually exists
# over there -- not always the same set, since a remote target's workspaces
# live on the far machine.
target_workspaces() {
    { list_workspaces 2>/dev/null || true
      t_list 2>/dev/null | cut -f1 || true
    } | grep -v '^[[:space:]]*$' | sort -u
}

load_target() {
    local t="${1:-container}" kind conf

    # $WK_STORE is target-dependent; reset first so a second load in one
    # process doesn't inherit the first.
    : "${WK_STORE_DEFAULT:=${WK_STORE:-/var/lib/wk}}"
    WK_STORE="$WK_STORE_DEFAULT"

    kind=$(target_kind "$t") || die "unknown target '$t'.
    The built-in ones are container, vm, remote and local. Anything else is a
    machine, and needs a conf -- in the registry, so every device gets it:

        $(target_registry_conf "$t")
            WK_REMOTE_HOST=$t      # an ssh destination that already works
            WK_REMOTE_ROOT=/home/you/wk

    'wk remote setup $t' writes it for you."

    # Set before either file is read: both are written in terms of the name.
    WK_TARGET="$t"
    WK_TARGET_KIND="$kind"
    export WK_TARGET WK_TARGET_KIND

    _target_reset_vars

    # And the *functions*: a definition outlives the load, so re-sourcing
    # restores every default before the driver overrides only its own.
    # shellcheck disable=SC1090
    . "$WK_ROOT/lib/target.sh"

    # Conf first, driver second, so the driver's ${VAR:-default} assignments
    # fill in only what the conf left unsaid.
    conf=$(target_registry_conf "$t")
    if [ -f "$conf" ]; then
        # shellcheck disable=SC1090
        . "$conf"
    fi
    # shellcheck disable=SC1090
    . "$WK_ROOT/targets/$kind.sh"
}
