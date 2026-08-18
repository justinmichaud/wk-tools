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
#   t_tools <name>     where wk-tools is inside the target
#   t_sync_tools <n>   push wk-tools in (no-op when it is bind-mounted)
#   t_ssh_host <name>  ssh destination, for Zed and the generated alias
#   t_needs_base       0 if `wk new` must resolve a base snapshot first
#   t_start / t_stop   lifecycle, for targets that have one
#   t_cores/t_mem_mb   the resources the target actually has
#   t_store_init       create the host-side directories this target needs
#   t_ready            block until a new workspace finished initialising
#
# t_cores/t_mem_mb are separate from lib/resources.sh on purpose. A build has
# to be sized from the memory of the machine it runs on, and for a vm target
# that is the *guest*, not the host -- sizing a macOS VM build from the host's
# 32 GB when the guest was booted with 12 would pick a job count the guest
# cannot honour, which is the same class of mistake as sizing a container from
# MemAvailable instead of its cgroup limit.

t_src()        { echo "/src/WebKit"; }
t_tools()      { echo "/opt/wk-tools"; }
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
    d="$(wk_state_dir)/targets"
    [ -d "$d" ] || return 0
    for f in "$d"/*; do
        [ -f "$f" ] || continue
        t=$(cat "$f")
        case "$seen" in *" $t "*) continue ;; esac
        seen="$seen$t "
        echo "$t"
    done
}

load_target() {
    local t="${1:-container}"

    # $WK_STORE is target-dependent -- a vm workspace keeps its state under
    # XDG state on the host, a container's lives in /var/lib/wk -- and drivers
    # set it when they are sourced. Reset it first so loading a second target
    # in one process does not inherit the first one's store.
    : "${WK_STORE_DEFAULT:=${WK_STORE:-/var/lib/wk}}"
    WK_STORE="$WK_STORE_DEFAULT"
    case "$t" in
        container|vm|remote) ;;
        *) die "unknown target '$t' (container, vm, remote)" ;;
    esac
    # shellcheck disable=SC1090
    . "$WK_ROOT/targets/$t.sh"
    WK_TARGET="$t"
    export WK_TARGET
}
