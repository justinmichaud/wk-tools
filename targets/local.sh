# Target driver: the workspace is this machine.
#
# Every other driver reaches into an environment from outside -- podman exec,
# tart plus ssh, plain ssh. This one is the degenerate case: the environment is
# the machine the command is already running on. That is what a workspace looks
# like from the inside, and it is the only thing available in there -- a
# container has no podman machine to talk to, and a macOS guest has no nested
# virtualisation (kern.hv_support = 0), so it can never host a workspace either.
#
# It exists because Claude only ever runs inside a workspace. That is the whole
# sandbox model, and it only works if `wk build`, `wk run` and `wk test` work
# from in there. Selected automatically by the `wk` entrypoint when the
# workspace marker is present; see "am I a workspace?" in lib/target.sh.

# Nothing here is sandboxing anything: the boundary was drawn by whoever created
# this workspace, and from in here it is simply the machine. Named so that
# anything reporting the sandbox model does not silently claim otherwise.
WK_SANDBOX=self

# Read once, at load: a driver that re-derived these per call would go looking
# for the marker again from inside a build, where $HOME may not be what it was.
_local_name=$(wk_marker_field name)
_local_src=$(wk_marker_field src)
_local_arch=$(wk_marker_field arch)
[ -n "$_local_name" ] || die "$(wk_marker) does not name the workspace (name=)"
[ -n "$_local_src" ] || die "$(wk_marker) does not name a checkout (src=)"

# Build logs and status files.
#
# Not the host's store: it is not visible from in here. A container gets the
# checkout, the caches and the tooling bind-mounted and nothing else, and a
# macOS guest has no host filesystem at all -- by design, in both cases. So the
# workspace keeps its own, which is enough for `wk status` and `wk logs` inside
# the workspace to read back what `wk build` inside it wrote.
#
# XDG state rather than data: build logs and status files are exactly that.
WK_STORE="${XDG_STATE_HOME:-$HOME/.local/state}/wk"
mkdir -p "$WK_STORE/ws/$_local_name"

t_src()   { echo "$_local_src"; }

# From the marker, because nothing in here can work it out: the kernel is the
# host's, so an armhf container reports aarch64 from uname. Defaulted for
# workspaces created before the field existed, which are all native.
t_arch()  { echo "${_local_arch:-native}"; }

# The wk being run is the one in the workspace, so there is nothing to locate
# and nothing to push: $WK_ROOT is already the in-workspace tooling tree.
t_tools() { echo "$WK_ROOT"; }

t_list()  { printf '%s\trunning\n' "$_local_name"; }

# One workspace exists here and it is this machine. Anything else named on the
# command line is genuinely absent, and saying so matters: reporting every name
# as running would make `wk build otherws jsc-release` build *this* workspace
# while announcing it built that one.
t_info()  { [ "$1" = "$_local_name" ] && echo running || echo absent; }

# Through a login shell, for the same reason the vm driver runs its commands
# that way: the environment a command needs is in the profile, not in this
# process. The proxy variables and the PATH additions are written there by
# provisioning (container/firstrun.sh, vm/provision-base.sh), and a plain fork
# of the current shell inherits only whatever `wk` itself was started with --
# which for an agent's `bash -c`, a hook or an ssh command is very little.
#
# bash, not $SHELL: the macOS guest's login shell is zsh, and the profile
# fragments provisioning writes cover both, so picking one keeps the two hosts
# on one code path.
t_exec() {
    shift
    bash -lc "exec $(sh_quote "$@")"
}

# t_exec_tty is the inherited default, and correct here: a local process already
# has whatever terminal the caller had.

t_create() {
    die "a workspace cannot create a workspace -- run 'wk new $1' on the host"
}

# Refused, not implemented. `wk rm` deletes the overlay upper layer this
# process's filesystem is made of, and the container it is running in; the host
# owns both. A workspace destroying itself would be a process deleting the
# ground it stands on and reporting success from a shell that no longer exists.
t_destroy() {
    die "a workspace cannot destroy itself -- run 'wk rm $_local_name' on the host"
}

t_enter() { die "already inside workspace '$_local_name'"; }

t_ssh_host() { die "a workspace has no ssh route to itself"; }

# Nothing to resolve: creation refuses, so there is never a base to pin.
t_needs_base() { return 1; }

# Only the one directory, and not store_init's caches and snapshot tree: those
# are the host's, and creating empty copies of them in a workspace's home would
# advertise a store that holds nothing and can hold nothing.
t_store_init() { mkdir -p "$WK_STORE/ws/$_local_name"; }

# What this machine actually has, which for a container is its cgroup limit and
# not what the kernel reports.
#
# `nproc` inside a container is the wrong answer twice over: --cpus sets a
# cpu.max quota, which does not restrict the affinity mask nproc reads, so a
# container capped at 7 CPUs on this machine reports 80. cmd/build passes these
# to build_jobs as WK_CGROUP_CORES/WK_CGROUP_MB, so getting them wrong here is
# how a build ends up with a job count its own cgroup will OOM-kill.
#
# lib/resources.sh is not sourced by every command that loads a target (cmd/run
# does not need it), so pull it in on demand rather than assuming it is there.
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
    # No cgroup, or no limit in it: a macOS guest, or a container created
    # without --cpus. Either way the workspace is the only workload on this
    # machine and the host already subtracted its own reserve when it sized it.
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
