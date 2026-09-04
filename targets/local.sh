# Target driver: the workspace is this machine -- the only thing reachable from
# inside one (no nested podman machine; a macOS guest has kern.hv_support = 0).

# Not a sandbox boundary: that was drawn by whoever created this workspace.
WK_SANDBOX=self

# Read once: inside a build $HOME may no longer point at the marker.
_local_name=$(wk_marker_field name)
_local_src=$(wk_marker_field src)
_local_arch=$(wk_marker_field arch)
[ -n "$_local_name" ] || die "$(wk_marker) does not name the workspace (name=)"
[ -n "$_local_src" ] || die "$(wk_marker) does not name a checkout (src=)"

# WK_LOCAL_STORE is the container exception: the workspace directory bind-mounted
# in at the host's own path, so build.status here is what `wk status` reads out there.
WK_STORE="${WK_LOCAL_STORE:-${XDG_STATE_HOME:-$HOME/.local/state}/wk}"
mkdir -p "$WK_STORE/ws/$_local_name"

t_src()   { echo "$_local_src"; }

# The kernel is the host's, so an armhf container reports aarch64 from uname.
t_arch()  { echo "${_local_arch:-native}"; }

t_os()    { wk_os; }

t_tools() { echo "$WK_ROOT"; }

t_mirror_dir() {
    case "$(wk_os)" in
        macos) mirror_beside_checkout "$_local_src" ;;
        *)     mirror_in_container ;;
    esac
}

t_list()  { printf '%s\trunning\n' "$_local_name"; }

t_info()  { [ "$1" = "$_local_name" ] && echo running || echo absent; }

# A login shell: the proxy vars and PATH provisioning writes live in the profile.
# bash, not $SHELL: the guest's login shell is zsh, and the profile covers both.
t_exec() {
    shift
    bash -lc "exec $(sh_quote "$@")"
}

t_create() {
    die "a workspace cannot create a workspace -- run 'wk new $1' on the host"
}

t_destroy() {
    die "a workspace cannot destroy itself -- run 'wk rm $_local_name' on the host"
}

t_enter() { die "already inside workspace '$_local_name'"; }

t_stop() { die "a workspace cannot stop itself -- run 'wk stop $_local_name' on the host"; }

t_start() { die "a workspace cannot start itself -- run 'wk start $_local_name' on the host"; }

t_ssh_host() { die "a workspace has no ssh route to itself"; }

t_needs_base() { return 1; }

t_store_init() { mkdir -p "$WK_STORE/ws/$_local_name"; }

# `nproc` is wrong inside a container: --cpus sets a cpu.max quota that does not
# restrict the affinity mask nproc reads, so a 7-CPU container reports 80.
command -v host_cores >/dev/null 2>&1 || . "$WK_ROOT/lib/resources.sh"

t_cores() {
    local quota period
    if [ -r /sys/fs/cgroup/cpu.max ]; then
        read -r quota period < /sys/fs/cgroup/cpu.max || true
        if [ "${quota:-max}" != max ] && [ -n "${period:-}" ]; then
            echo $(( quota / period ))
            return 0
        fi
    fi
    host_cores
}

t_mem_mb() {
    local limit
    if [ -r /sys/fs/cgroup/memory.max ]; then
        limit=$(cat /sys/fs/cgroup/memory.max)
        if [ "$limit" != max ]; then
            echo $(( limit / 1024 / 1024 ))
            return 0
        fi
    fi
    host_mem_mb
}
