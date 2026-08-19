# Loading a target driver, and the defaults every driver inherits.
#
# Commands under cmd/ never talk to podman, tart or ssh directly -- they call
# the contract below and nothing else. That is what makes "add a target" mean
# "add one file under targets/" rather than "audit every command".
#
# Required of a driver:
#
#   t_create <name> [base]   bring the environment into existence
#   t_exec   <name> <cmd..>  run a command in it
#   t_enter  <name>          interactive shell
#   t_destroy <name>         remove it and everything it created
#   t_info   <name>          one word of state, or "absent"
#   t_list                   "<name>\t<state>" per line
#
# Optional, defaulted here. A driver overrides only what is genuinely
# different about it; re-stating a default is how the two copies drift apart.
#
#   t_src <name>       where the WebKit checkout is *inside* the target
#   t_arch <name>      the architecture the target runs natively
#   t_tools <name>     where wk-tools is inside the target
#   t_sync_tools <n>   push wk-tools in (no-op when it is bind-mounted)
#   t_ssh_host <name>  ssh destination, for Zed and the generated alias
#   t_needs_base       0 if `wk new` must resolve a base snapshot first
#   t_start / t_stop   lifecycle, for targets that have one
#   t_cores/t_mem_mb   the resources the target actually has
#   t_load             the load already on the target, for the polite sizing
#   t_ccache_dir       where ccache keeps its cache inside the target
#   t_store_init       create the host-side directories this target needs
#   t_ready            block until a new workspace finished initialising
#   t_exec_build       t_exec, for a build specifically (see below)
#
# One driver is the degenerate case of all this: targets/local.sh, where the
# target is the machine the command is already running on -- a workspace acting
# on itself. See "am I a workspace?" below for how that is detected.
#
# t_cores/t_mem_mb are separate from lib/resources.sh on purpose. A build has
# to be sized from the memory of the machine it runs on, and for a vm target
# that is the *guest*, not the host -- sizing a macOS VM build from the host's
# 32 GB when the guest was booted with 12 would pick a job count the guest
# cannot honour, which is the same class of mistake as sizing a container from
# MemAvailable instead of its cgroup limit.

t_src()        { echo "/src/WebKit"; }

# The architecture a workspace runs natively -- `native` unless something said
# otherwise at creation. Only the container driver can currently answer
# anything else, because only a container can be a different architecture from
# the host it runs on; a VM guest and a remote box are whatever they are.
#
# This is deliberately not "what would I build for": that question has a second
# answer once cross builds exist, and the two must not share a word. See
# lib/arch.sh.
t_arch()       { echo native; }
t_tools()      { echo "/opt/wk-tools"; }
# The bind-mounted store cache in a container, and inert on the Apple ports,
# which do not use ccache at all. A target that is somebody else's machine has
# to answer with a directory of its own -- see targets/remote.sh.
t_ccache_dir() { echo "/ccache"; }
t_sync_tools() { :; }
t_ssh_host()   { echo "wk-$1"; }
t_needs_base() { return 0; }
t_start()      { :; }
t_stop()       { :; }
t_store_init() { store_init; }

# Wait until a freshly created workspace is actually usable, and fail if it
# never gets there. Defaults to "immediately ready", which is true for targets
# whose creation is synchronous.
t_ready()      { :; }
t_cores()      { envelope_cores; }   # t_cores <name>
t_mem_mb()     { envelope_mem_mb; }  # t_mem_mb <name>

# Interactive exec. Anything with a full-screen UI -- Claude, an editor, a
# pager -- needs a pty, and ssh does not allocate one for a command unless it
# is asked to. Defaults to the plain exec, which is right wherever the driver
# already gives you a terminal.
t_exec_tty()   { t_exec "$@"; }

# The exec `wk build` uses, which is the same one everywhere except where a
# build specifically has to be serialised. On a shared machine two of your own
# builds must not stack, and the lock that guarantees it belongs on the build
# alone: applied to t_exec it would also make `wk run`, `wk status` and every
# small probe block for up to an hour behind a build, which is a hang with no
# explanation attached.
t_exec_build() { t_exec "$@"; }

# The load already on the target, in whole cores, as `build_jobs polite`
# subtracts it. Defaults to this machine's own -- correct for a container,
# which shares the host's kernel and therefore its load average, and for the
# degenerate local target, which *is* this machine. A driver whose target is
# another machine has to answer for that machine instead.
t_load() { awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0; }

# Per-workspace target registry.
#
# `wk build bug-238 ...` must reach the same place `wk new bug-238 --target vm`
# created, without the target being restated on every command -- and on macOS
# the dispatcher has to know the answer *before* it decides whether to forward
# into the podman VM. So the choice is recorded once, at creation.
#
# Kept on the host under XDG state rather than in $WK_STORE, because $WK_STORE
# is itself target-dependent and this is what resolves that.
wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

target_register() {
    local name="$1" target="$2" d
    d="$(wk_state_dir)/targets"
    mkdir -p "$d"
    printf '%s\n' "$target" > "$d/$name"
}

target_forget() { rm -f "$(wk_state_dir)/targets/$1"; }

# The target a workspace was created with, or empty if it is not registered.
# Container workspaces on Linux predate the registry and are the default, so
# "not registered" must resolve to container rather than to an error.
target_of() {
    local f="$(wk_state_dir)/targets/$1"
    [ -f "$f" ] && cat "$f" || return 1
}

# --- am I a workspace? -------------------------------------------------------
# A workspace is a whole machine -- a podman container or a macOS guest -- and
# the `wk` inside one has to act on that machine rather than try to reach one
# from outside. Claude only ever runs inside a workspace, so this is the path
# that has to work for it to build or test anything at all.
#
# Provisioning writes a marker file saying so and naming the checkout:
# container/firstrun.sh in a container, targets/vm.sh in a macOS guest. A file
# rather than an environment variable, because it has to be true for everything
# that reaches in -- an ssh command, a hook, a `bash -c` from an agent -- and
# not only for the shells that happened to inherit the right environment.
#
# When the marker is absent this machine is a host, and every path that reads it
# must behave exactly as it did before it existed.
wk_marker() { echo "${WK_MARKER:-$HOME/.wk-workspace}"; }

in_workspace() { [ -f "$(wk_marker)" ]; }

# One `key=value` field from a marker file; empty when the file or the key is
# missing. Comments and blank lines are ignored, so the file can explain itself.
marker_field() {
    local f="$1" k="$2"
    [ -f "$f" ] || return 0
    awk -F= -v k="$k" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$f"
}

wk_marker_field() { marker_field "$(wk_marker)" "$1"; }

# --- am I a machine that *hosts* workspaces? ---------------------------------
# A shared build box is neither a workstation nor a workspace. It holds several
# workspaces at once, so the marker above -- which says "this machine IS a
# workspace" and names one -- is the wrong shape for it. This one says "this
# machine is the far end of a remote target", and names which:
#
#   target=devbox-arm64-2
#   root=/home/you/wk
#
# Written by `wk remote setup`, and what makes `wk` usable *on* the box: with
# it, a bare `wk ls` or `wk build bug-238 jsc-release` there resolves to the
# same driver, the same paths and the same job policy as the workstation
# driving it -- with the ssh step dropped (WK_REMOTE_LOCAL, targets/remote.sh).
#
# A file rather than an exported variable, for the same reason as the workspace
# marker: it has to be true for `ssh box wk status` and for a cron line, not
# only for shells that sourced a profile.
wk_remote_marker()   { echo "${WK_REMOTE_MARKER:-$HOME/.wk-remote}"; }
in_remote_host()     { [ -f "$(wk_remote_marker)" ]; }
wk_remote_field()    { marker_field "$(wk_remote_marker)" "$1"; }

# The workspace this machine *is*, or empty on a host. This is what makes the
# workspace argument optional inside one: `wk build jsc-release` rather than
# `wk build <name> jsc-release`, which is the interface claude/CLAUDE.md
# documents and the only one available in here.
wk_self() { wk_marker_field name; }

# The target to assume for a workspace that is not in the registry. On a host
# that is `container` -- the default, and what every workspace predating the
# registry is. Inside a workspace it is `local`, because there is nothing else
# in there: no podman, no tart, and one workspace, which is this machine.
default_target() {
    if in_workspace; then echo local; return 0; fi
    local t; t=$(wk_remote_field target)
    if [ -n "$t" ]; then echo "$t"; return 0; fi
    echo container
}

# The config to build, run or test when the caller names none.
#
# jsc-release is right on a host and in a container, where JSCOnly is what gets
# built. It is meaningless in a macOS guest, which can build nothing but the
# Apple ports -- so a bare `wk run` in there went looking for a JSCOnly binary
# that cannot exist, and reported the path it could not find rather than the
# reason. The marker carries the answer, written by whoever wrote the marker:
# container/firstrun.sh knows the container builds JSCOnly, and targets/vm.sh
# knows the guest's tree is $WK_VM_BASE_PREBUILD.
default_config() {
    local c=""
    in_workspace && c=$(wk_marker_field config)
    printf '%s' "${c:-jsc-release}"
}

# --- generated ssh aliases ---------------------------------------------------
# Written to a file under ~/.ssh/config.d rather than to ~/.ssh/config itself,
# so it can be rewritten freely without ever touching hand-maintained entries.
#
# Rewritten rather than appended, because a VM's address changes on every boot:
# an alias that is only ever added would point at whatever the guest happened
# to get the first time, and Zed would sit there timing out against a stale IP.

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

# ssh_alias_set <name> <hostname> <user>
ssh_alias_set() {
    local name="$1" hostname="$2" user="$3" conf extra
    conf=$(wk_ssh_conf)
    ensure_dir "$(dirname "$conf")" 0700
    ssh_alias_remove "$name"

    # A per-target identity only when there is one. The container workspaces
    # authenticate as the invoking user through the podman machine and have no
    # key of their own.
    extra=""
    [ -n "${4:-}" ] && extra="    IdentityFile $4"

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

# Every registered target, plus container, which is the default and is what
# anything unregistered is. Used by commands that report on everything rather
# than acting on one named workspace.
target_all() {
    local d f t seen=" container "
    echo container

    # On a shared build machine the workspaces were created from somewhere
    # else, so nothing here is registered -- and without this a bare `wk
    # status` on the box would walk one target it does not have and report
    # nothing about the ones it does.
    t=$(wk_remote_field target)
    if [ -n "$t" ]; then seen="$seen$t "; echo "$t"; fi

    d="$(wk_state_dir)/targets"
    if [ -d "$d" ]; then
        for f in "$d"/*; do
            [ -f "$f" ] || continue
            t=$(cat "$f")
            case "$seen" in *" $t "*) continue ;; esac
            seen="$seen$t "
            echo "$t"
        done
    fi

    # Every machine that has been configured, whether or not a workspace is
    # currently pinned to it. A machine you have set up is a target you have,
    # and `wk status` should say what is on it -- including on the machine
    # itself, where nothing is registered at all because workspaces are created
    # from the workstation.
    d=$(target_conf_dir)
    [ -d "$d" ] || return 0
    for f in "$d"/*.conf; do
        [ -f "$f" ] || continue
        t=$(basename "$f" .conf)
        case "$seen" in *" $t "*) continue ;; esac
        seen="$seen$t "
        echo "$t"
    done
}

# --- named targets -----------------------------------------------------------
# A target name is either one of the four built-in kinds -- container, vm,
# remote, local -- or the name of a machine configured under
# ~/.config/wk/targets/<name>.conf.
#
# The distinction exists because `remote` is the one kind you can have several
# of. There is exactly one podman, one Tart and one local machine, so those
# names identify a driver *and* the thing it drives; a shared build box does
# not, and `wk new bug-238 --target remote` on a laptop with three of them
# configured would be a coin toss. So the machine's own name is the target:
#
#   wk new bug-238 --target devbox-arm64-2
#
# and the conf next to that name says which driver to use and how to reach it.
# Everything downstream keeps working unchanged because the registry already
# records a target per workspace as an opaque string.
target_conf_dir() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/wk/targets"; }
target_conf()     { echo "$(target_conf_dir)/$1.conf"; }

# The driver a target name selects. The built-in kinds are their own kind;
# anything else must have a conf, and a conf that does not say otherwise
# describes a remote machine -- the only kind there can be several of, and so
# the only kind worth naming.
target_kind() {
    case "$1" in
        container|vm|remote|local) echo "$1"; return 0 ;;
    esac
    local c k
    c=$(target_conf "$1")
    [ -f "$c" ] || return 1
    k=$(awk -F= '/^[[:space:]]*WK_TARGET_KIND[[:space:]]*=/ {
            gsub(/[ \t"'"'"']/, "", $2); print $2; exit }' "$c")
    echo "${k:-remote}"
}

# Per-target driver state, cleared before every load.
#
# A driver is a sourced file that sets globals, and more than one target can be
# loaded in a single process -- `wk status` with no argument walks every target
# there is. Without this the second remote machine would inherit the first
# one's host, root and measured capacity, and confidently report its workspaces
# as living somewhere else.
#
# The environment still wins over a conf, so what it supplied is snapshotted
# once, before any conf has been read, and restored on each load. The variables
# are named here rather than in the driver because the reset has to happen
# *before* the conf is sourced, and the conf is what would otherwise be undone.
_target_reset_vars() {
    if [ -z "${_WK_ENV_SEEDED:-}" ]; then
        _WK_ENV_REMOTE_HOST="${WK_REMOTE_HOST:-}"
        _WK_ENV_REMOTE_ROOT="${WK_REMOTE_ROOT:-}"
        _WK_ENV_REMOTE_MAX_JOBS="${WK_REMOTE_MAX_JOBS:-}"
        _WK_ENV_REMOTE_REFERENCE="${WK_REMOTE_REFERENCE:-}"
        _WK_ENV_SEEDED=1
    fi
    WK_REMOTE_HOST="$_WK_ENV_REMOTE_HOST"
    WK_REMOTE_ROOT="$_WK_ENV_REMOTE_ROOT"
    WK_REMOTE_MAX_JOBS="$_WK_ENV_REMOTE_MAX_JOBS"
    WK_REMOTE_REFERENCE="$_WK_ENV_REMOTE_REFERENCE"
    WK_REMOTE_LOCAL=""
    WK_MAX_JOBS=""
    unset _WK_REMOTE_PROBED _WK_REMOTE_HOME _WK_REMOTE_CORES \
          _WK_REMOTE_LOAD _WK_REMOTE_MEM _WK_REMOTE_REF_PROBED
}

# Every workspace a loaded target has, from both sides of it.
#
# The local store knows what this machine created -- what a workspace is pinned
# to, its build log -- and the driver knows what actually exists over there.
# Usually the same set, and not always: a remote target's workspaces live on
# the far machine, so `wk ls` run *on* that machine has an empty store and a
# full ws directory, and a workspace deleted out from under the store has the
# opposite. Listing only one side is how a machine ends up reporting "no
# workspaces" while holding six.
target_workspaces() {
    { list_workspaces 2>/dev/null || true
      t_list 2>/dev/null | cut -f1 || true
    } | grep -v '^[[:space:]]*$' | sort -u
}

load_target() {
    local t="${1:-container}" kind conf

    # $WK_STORE is target-dependent -- a vm workspace keeps its state under
    # XDG state on the host, a container's lives in /var/lib/wk -- and drivers
    # set it when they are sourced. Reset it first so loading a second target
    # in one process does not inherit the first one's store.
    : "${WK_STORE_DEFAULT:=${WK_STORE:-/var/lib/wk}}"
    WK_STORE="$WK_STORE_DEFAULT"

    kind=$(target_kind "$t") || die "unknown target '$t'.
    The built-in ones are container, vm, remote and local. Anything else is a
    machine, and needs $(target_conf "$t"):

        WK_REMOTE_HOST=$t          # an ssh destination that already works
        WK_REMOTE_ROOT=/home/you/wk
        WK_REMOTE_MAX_JOBS=16"

    # Set before either file is read: a driver that can have several instances
    # has to know which one it is, and both the conf and the driver's own
    # defaults are written in terms of the name.
    #
    # WK_TARGET is the name, WK_TARGET_KIND is the driver behind it. Commands
    # that branch on what kind of thing they are talking to must use the kind:
    # `[ "$TARGET" = remote ]` was true for every remote target while there was
    # only one possible name for one.
    WK_TARGET="$t"
    WK_TARGET_KIND="$kind"
    export WK_TARGET WK_TARGET_KIND

    _target_reset_vars

    # The conf first and the driver second, so that the driver's own
    # ${VAR:-default} assignments fill in only what the conf left unsaid.
    conf=$(target_conf "$t")
    if [ -f "$conf" ]; then
        # shellcheck disable=SC1090
        . "$conf"
    fi
    # shellcheck disable=SC1090
    . "$WK_ROOT/targets/$kind.sh"
}
