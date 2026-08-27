# Target driver: podman container.
#
# Implements the driver contract (t_create, t_exec, t_destroy, t_info, t_list)
# on top of the webkit-container-sdk. Commands under cmd/ call only these
# functions, never podman directly, so adding a new kind of environment later
# means adding a file here and nothing else.
#
# One sandbox model, on both hosts: rootless podman, --userns keep-id, and
# --network none. The workspace has no network interface at all and reaches the
# outside only through the unix socket of an egress proxy running as the user.
# Nothing here needs a privilege. (Why not a packet filter: see the WK_SANDBOX
# comment below.)

if [ -n "${WK_IN_VM:-}" ]; then
    WK_SDK="${WK_SDK:-/opt/webkit-container-sdk}"
else
    WK_SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"
fi

# Every SDK script checks this on line 5 and aborts without it. Normally set by
# register-sdk-on-host.sh, which is a shell-rc thing we deliberately do not use.
export WKDEV_SDK="$WK_SDK"

# One sandbox model, both hosts: rootless podman has no filterable forward
# path (its network helper re-emits container traffic from a random cgroup
# scope, with no stable selector), which forces a choice between rootful
# podman with nftables -- where a container escape becomes a root escape -- and
# removing the interface entirely. This removes it.
WK_SANDBOX=rootless-proxy

# Rootless, on both hosts. `sudo` appears nowhere in the daily path, and that
# is the property to preserve: if a wk command ever needs a password, it has
# stopped being something an unattended agent can use.
_sdk() { "$@"; }
_podman() { podman "$@"; }

# keep-id maps the invoking user through unchanged, so the container user is
# simply this user. No id derivation, no mismatch between the checkout, the
# caches and the home directory -- and none of the SDK's in-image user creation,
# which existed only because rootful podman has no keep-id.
export WKDEV_CONTAINER_UID="$(id -u)"
export WKDEV_CONTAINER_GID="$(id -g)"
export WKDEV_CONTAINER_USER="${WK_CONTAINER_USER:-$(id -un)}"

# wkdev-create defaults --shell to ${SHELL}, the *host* user's shell. That path
# need not exist in the image -- zsh does not -- and when it does not, the
# firstrun hook silently fails with "su: failed to execute /usr/bin/zsh" and the
# workspace comes up without its git identity, push key or Claude install.
export WKDEV_CONTAINER_SHELL=/bin/bash

# The architecture vocabulary: what --arch may say, which image each answer
# means, and the build flags that follow. Loaded on demand rather than assumed,
# because not every command that loads a target sources it.
command -v arch_canon >/dev/null 2>&1 || . "$WK_ROOT/lib/arch.sh"

# GPU policy lives with the rest of the Linux host support, because what has to
# be injected depends entirely on the host's driver stack.
if [ -f "$WK_ROOT/host/linux/gpu.sh" ]; then
    . "$WK_ROOT/host/linux/gpu.sh"
else
    gpu_flags() { :; }
fi

# Where the host keeps the sockets a workspace is allowed to see: the egress
# proxy's, and the benchmark compositor's. One directory, mounted at /run/wk,
# rather than the whole runtime directory -- which is what upstream does and
# which would hand over the session D-Bus socket, and with it the machine.
_wk_runtime() { echo "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk"; }

# In the VM the tooling is rsynced to /opt/wk-tools and the repo is not present;
# on a workstation it is simply the checkout this script came from.
[ -n "${WK_IN_VM:-}" ] && WK_TOOLS_SRC=/opt/wk-tools

_ctr() { echo "wk-$1"; }

# `wk sync` against the container target: pushes wk-tools itself, not the
# store. On a workstation the mount source is this checkout, so there is
# nothing to do; on macOS it is a copy inside the podman VM, refreshed by
# rsync (`./setup --stage vmtools` is the other route). The mirror and base
# snapshots are a separate, forwarded `wk sync`, so the two forms never
# duplicate each other's work.
t_sync() {
    if ! is_macos; then
        info "containers here bind-mount this checkout ($WK_ROOT), so the tooling is never stale"
        log  "  the mirror and snapshots are this machine's store:  wk sync"
        return 0
    fi
    ( WK_VMTOOLS_ONLY=tools . "$WK_ROOT/host/macos/vmtools.sh" ) \
        || die "could not push wk-tools into the podman VM"
    log "  the mirror and snapshots in there are a plain 'wk sync --machine' away"
}

# The architecture this workspace was created with, recorded at creation
# because nothing else can recover it: the container reports the *kernel's*
# architecture (the host's, since it shares it), so `uname -m` in an armhf
# workspace answers aarch64 and would call it native. Absent for every
# workspace made before --arch existed, which is what the default is for.
t_arch() {
    local f; f="$(wk_ws_dir "$1")/arch"
    [ -f "$f" ] && cat "$f" || echo native
}

t_list() {
    _podman ps -a --filter 'name=^wk-' --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
        | sed 's/^wk-//'
}

# The container's own home is a directory on this machine (workspace = overlay
# + home under the store), so the readiness marker is a file here and reading
# it costs nothing.
#
# Two names accepted: `.wk-firstrun-complete` (written before the marker
# became the contract) means the same as `.wk-ready`, so both count.
# TODO: this clause can go once no pre-marker workspace is left anywhere.
t_created() {
    local h; h="$(wk_ws_dir "$1")/home"
    [ -f "$h/$WK_READY_MARKER" ] || [ -f "$h/.wk-firstrun-complete" ]
}

# Two questions in one word: existing vs. running needs the marker, because
# creation is asynchronous (wkdev-create returns once the container starts;
# .wkdev-init runs firstrun afterwards) -- callers that gate on readiness need
# to see this window (ws_state, wait_ready in lib/target.sh).
#
# `_hpodman`, not `_podman`: every other caller of this only ever reaches it
# forwarded into the podman VM, where the two are identical -- but `wk stop`
# declares `forward=no` and asks it directly from a macOS host, where they are
# not, and the plain connection sees an empty store.
t_info() {
    local c st
    c=$(_ctr "$1")
    st=$(_hpodman inspect "$c" --format '{{.State.Status}}' 2>/dev/null) || st=absent
    [ -n "$st" ] || st=absent
    [ "$st" = absent ] && { echo absent; return 0; }
    t_created "$1" || { echo creating; return 0; }
    echo "$st"
}

# Read, not exec'd. The checkout is an overlay whose upper layer is a directory
# on this machine, so HEAD is a file here: `changes/.git/HEAD` once the
# workspace has checked anything out, and the base snapshot's own HEAD before
# that. A container exec per workspace would be a second of latency each in a
# command that must stay quick and must start nothing.
t_branch() {
    local ws head ref base
    ws=$(wk_ws_dir "$1")
    head="$ws/changes/.git/HEAD"
    if [ ! -f "$head" ]; then
        base=$(ws_base_id "$1" 2>/dev/null) || { echo -; return 0; }
        head="$(base_path "$base")/.git/HEAD"
    fi
    [ -f "$head" ] || { echo -; return 0; }
    ref=$(cat "$head" 2>/dev/null) || { echo -; return 0; }
    case "$ref" in
        "ref: refs/heads/"*) printf '%s' "${ref#ref: refs/heads/}" ;;
        "ref: "*)            printf '%s' "${ref#ref: }" ;;
        # A fresh workspace is detached at the snapshot's commit, which is the
        # honest answer rather than a branch name it is not on.
        *)                   printf 'detached %s' "$(printf '%s' "$ref" | cut -c1-10)" ;;
    esac
}

# Flags that differ between the two sandbox modes, split into two lists because
# they are consumed differently: wkdev-create's own options (--network,
# --isolated) vs. everything passed through verbatim inside --additional-flags.
# Mixing them fails -- podman rejects --isolated as unknown, and a --network in
# both places gives the container two.
_sdk_opts() {
    # --network none is the boundary itself, not a filtered interface --
    # loopback still exists, which is all run-benchmark's local server needs.
    #
    # --isolated drops the host session entirely: D-Bus, keyring, dconf, X11,
    # host home, shared runtime dir. The session bus alone is a complete host
    # escape unrelated to networking, so no firewall would have caught it.
    printf '%s\n' --network none --isolated
}

# _sandbox_flags <arch>
_sandbox_flags() {
    local arch="${1:-native}"
    local rt; rt=$(_wk_runtime)
    mkdir -p "$rt"

    # No GPU for a non-native workspace: NVIDIA userspace libraries must match
    # the host kernel driver and are published for aarch64 only, so on armhf
    # there is nothing to inject and asking anyway fails the container start.
    # See arch_has_gpu in lib/arch.sh.
    local gpu=""
    arch_has_gpu "$arch" && gpu=$(gpu_flags)

    # One directory holds every host socket a workspace may see: the egress
    # proxy's, and the benchmark compositor's. The compositor's socket comes
    # and goes with `wk session`, and mounting the directory rather than the
    # socket is what lets that happen without recreating containers.
    printf '%s' "--volume $rt:/run/wk
         --env WK_PROXY_SOCKET=/run/wk/proxy.sock
         --env http_proxy=http://127.0.0.1:3128
         --env https_proxy=http://127.0.0.1:3128
         --env HTTP_PROXY=http://127.0.0.1:3128
         --env HTTPS_PROXY=http://127.0.0.1:3128
         --env no_proxy=localhost,127.0.0.1,::1
         --env NO_PROXY=localhost,127.0.0.1,::1
         --env WAYLAND_DISPLAY=/run/wk/display/wayland-0
         $gpu"
}

# t_create <name> <base-id> [arch]
t_create() {
    local name="$1" base_id="$2" arch="${3:-native}"
    local c ws base
    c=$(_ctr "$name")
    ws=$(wk_ws_dir "$name")
    base=$(base_path "$base_id")

    [ -d "$base" ] || die "base snapshot $base_id not found; run 'wk sync' first"
    _podman container exists "$c" 2>/dev/null && die "workspace '$name' already exists"

    ensure_dir "$ws"
    ensure_dir "$ws/changes"
    ensure_dir "$ws/overlay-work"
    ensure_dir "$ws/home"
    ensure_dir "$ws/build"

    # Recorded before creation, not after: everything downstream -- the build
    # flags, the benchmark preflight, `wk ls` -- resolves the architecture from
    # here, and a workspace that exists without this file would be reported as
    # native and then built as native.
    printf '%s\n' "$arch" > "$ws/arch"

    # Explicit upperdir/workdir: a bare :O gives an ephemeral upper podman
    # discards on container stop, silently losing a day's work on restart.
    # podman also never deletes a user-managed upperdir on removal -- that is
    # t_destroy's job, or the workspace leaks forever.
    local overlay="$base:/src/WebKit:O,upperdir=$ws/changes,workdir=$ws/overlay-work"

    # $ws/build, bind-mounted over the checkout's WebKitBuild: without this,
    # every object a build writes lands on the overlay's upperdir
    # ($ws/changes) and pays overlayfs's copy-up on the first write of each
    # file it touches -- there is nothing to copy up from for a build
    # directory that never existed in the base snapshot, but the overlay
    # still walks the lower layer to find that out before creating the upper
    # one. A plain bind mount is a normal directory with none of that, and is
    # what makes WebKitBuild a mount point `git status` and an editor see
    # instead of tracked-repo churn -- WebKit's own .gitignore already excludes
    # it, so nothing here needs a second ignore rule. A workspace created
    # before this mount existed keeps its build tree inside the overlay until
    # it is recreated.
    local build_mount="$ws/build:/src/WebKit/WebKitBuild"

    # The mirror, read-only, at /mirror: the one copy of every upstream this
    # machine has, so `wk sync` refreshes a workspace from disk instead of
    # fetching WebKit again through the proxy. Read-only because it is what
    # every base snapshot clones from -- a writable mirror could change what
    # every future workspace builds. The directory above the bare repo is
    # mounted, not the repo itself, so a second mirror needs no second mount.
    #
    # $ws itself, at the same absolute path the host resolves for it
    # (/var/lib/wk/ws/<name>): this is what makes `wk build`/`wk status` agree
    # with each other regardless of which side of the sandbox ran `wk build`.
    # Claude runs only inside the workspace (CLAUDE.md), so a `wk build` typed
    # in there uses the `local` driver, which without this mount would resolve
    # $WK_STORE to a directory nothing outside the container can see -- a
    # build.status the host's own `wk status` would never find, reporting the
    # workspace as running with no build at all. WK_LOCAL_STORE is what
    # targets/local.sh reads to know a shared path exists here; a workspace
    # created before this mount existed keeps its own private one until it is
    # recreated.
    #
    # The ccache settings are not decoration: WebKit's CMake sets them on Apple
    # only (Source/cmake/WebKitCCache.cmake), so on Linux the precompiled header
    # is skipped without them -- measured at 371 of 752 calls on a JSC build.
    # BASEDIR and NOHASHDIR are what let one cache serve every workspace.
    local -a flags
    flags=(
        --additional-flags
        "--volume ${WK_TOOLS_SRC:-$WK_ROOT}:/opt/wk-tools:ro
         --volume $(dirname "$(wk_mirror)"):/mirror:ro
         --volume $overlay
         --volume $build_mount
         --volume $ws:/var/lib/wk/ws/$name
         --volume $WK_STORE/cache/ccache:/ccache
         --volume $WK_STORE/cache/yocto:/cache/yocto
         --volume $WK_STORE/cache/buildroot:/cache/buildroot
         --volume $WK_STORE/cache/bench:/cache/bench
         --volume $WK_STORE/bench:/bench
         --volume $WK_STORE/skills:/skills
         --volume $WK_STORE/secrets:/secrets:ro
         --memory $(envelope_mem_mb)m
         --cpus $(envelope_cores)
         --env CCACHE_DIR=/ccache
         --env CCACHE_MAXSIZE=$WK_CCACHE_MAXSIZE
         --env CCACHE_BASEDIR=/src/WebKit
         --env CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime,include_file_ctime
         --env CCACHE_PCH_EXTSUM=true
         --env CCACHE_DEPEND=true
         --env CCACHE_NOHASHDIR=true
         --env DL_DIR=/cache/yocto/downloads
         --env SSTATE_DIR=/cache/yocto/sstate
         --env BR2_DL_DIR=/cache/buildroot/dl
         --env BR2_CCACHE_DIR=/cache/buildroot/ccache
         --env WK_WORKSPACE=$name
         --env WK_ARCH=$arch
         --env WKDEV_OFFLINE=1
         --env WK_LOCAL_STORE=/var/lib/wk
         $(_sandbox_flags "$arch")"
    )

    # Explicit image and podman arch, both from lib/arch.sh, neither inferred:
    # without --image (a wk addition, sdk-patches/apply.sh section 12), `--arch
    # arm` would ask the aarch64 image of the current SDK for a 32-bit container.
    #
    # WK_SDK_IMAGE overrides the image -- set only by the Yocto builder
    # (image/yocto.sh), which needs Yocto's host tooling -- and wins because it
    # is the more specific of the two. Image and arch are chosen separately:
    # the architecture stays whatever it was regardless of which image is used.
    local -a archopts=()
    local image="${WK_SDK_IMAGE:-}"
    arch_is_native "$arch" || archopts+=(--arch "$(arch_podman "$arch")")
    [ -n "$image" ] || arch_is_native "$arch" || image=$(arch_image "$arch")
    [ -z "$image" ] || archopts+=(--image "$image")
    [ -z "${WK_SDK_IMAGE:-}" ] || info "using workspace image $WK_SDK_IMAGE (WK_SDK_IMAGE)"

    info "creating workspace '$name' from base $base_id ($WK_SANDBOX${arch:+, $arch})"
    # shellcheck disable=SC2046 -- _sdk_opts is a deliberate list of words.
    _sdk "$WK_SDK/scripts/host-only/wkdev-create" \
        $(_sdk_opts) \
        ${archopts[@]+"${archopts[@]}"} \
        --name "$c" \
        --shell "$WKDEV_CONTAINER_SHELL" \
        --user "$WKDEV_CONTAINER_USER" \
        --group "$WKDEV_CONTAINER_USER" \
        --home "$ws/home" \
        "${flags[@]}"

    # Drop the firstrun hook where .wkdev-init will pick it up on first start.
    # It writes `.wk-ready` in this home directory as its own last act, which is
    # what makes creation's completion evidence live next to the workspace
    # rather than on whichever machine happened to drive it.
    install -m 0755 "$WK_ROOT/container/firstrun.sh" "$ws/home/.wkdev-firstrun"

    # Last, deliberately: base-id is the completion marker (ws_state in
    # lib/target.sh). Written first, a `wk new` killed mid-create would leave a
    # workspace that looks finished with no container, and a re-run would
    # re-pin base-id over an already-started `changes/` overlay -- undefined
    # behaviour. It is also `wk gc`'s refcount, safe only because gc and new
    # take the store lock (lib/common.sh).
    printf '%s\n' "$base_id" > "$ws/base-id"
}

# Creation is asynchronous: wkdev-create returns once the container starts,
# and .wkdev-init runs the firstrun hook (push keys, Claude CLI, helix, lldb
# config, shell rc) afterwards -- without checking its exit, so a failure
# part-way is silent. firstrun.sh writes a marker as its last act; this waits
# for it and reports honestly when it never appears.
t_ready() {
    local name="$1" c i=0
    c=$(_ctr "$name")
    while [ "$i" -lt "${WK_READY_TIMEOUT:-300}" ]; do
        t_created "$name" && return 0
        # No point waiting for a marker no process is going to write: the
        # container the firstrun hook runs in is gone.
        _podman container exists "$c" >/dev/null 2>&1 || break
        sleep 1; i=$((i + 1))
    done

    # The driver knows where the evidence is, so it prints it rather than
    # telling the caller to go and find a command that differs per host.
    warn "initialisation did not complete; last output from the container:"
    _podman logs "$c" 2>&1 | grep -vE "^\\s*$" | tail -8 | sed "s/^/    /" >&2 || true
    return 1
}

# Every exec goes through ensure-bridge, which starts the loopback-to-proxy
# forwarder if it is not already running. Wrapping rather than checking
# separately keeps this to one container exec: a second round trip on every
# `wk run` would be paid forever for something almost always already true.
_wrap_cmd() {
    printf '%s\n' /opt/wk-tools/container/proxy/ensure-bridge.sh
}

# --quiet is not cosmetic. Without it wkdev-enter prints its own banner and
# host-integration notes on *stdout*, ahead of the command's output -- so
# anything that captures the result of a command (wk verify parsing interface
# lists, wk bench reading a renderer string) gets the SDK's chatter mixed into
# its data. The option exists upstream precisely for this.
t_exec() {
    local name="$1"; shift
    local c; c=$(_ctr "$name")
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --quiet --name "$c" --exec -- $(_wrap_cmd) "$@"
}

# ASLR: podman's seccomp allow-list lacks personality(ADDR_NO_RANDOMIZE), and a
# blocked syscall returns ENOSYS not EPERM, so lldb reads "no personality()" and
# refuses to launch ("personality set failed: Function not implemented") with
# nothing pointing at the real cause. disable-aslr false costs only that
# addresses move between runs -- not a sandbox weakening, a debugger convenience.
#
# stop-on-exec: WebKit's UI process spawns network, web and GPU child
# processes, and lldb's default stops the session at each child's exec --
# hijacking `wk gui --lldb ui` away from the process breakpoints were set in, a
# few seconds after `run`. `--lldb web` sets nothing here and is unaffected: it
# names the process it wants.
#
# Both belong here rather than in cmd/gui: they are what this target needs
# before lldb is usable at all.
t_lldb_opts() {
    printf '%s' "-O 'settings set target.disable-aslr false'"
    printf '%s' " -O 'settings set target.process.stop-on-exec false'"
}

# The container's home is the workspace's own `home/` directory on the host,
# bind-mounted (`--home $ws/home` in t_create). Naming it here is what lets the
# host follow a detached build's log with a plain `tail -f`.
t_home() { echo "/home/$WKDEV_CONTAINER_USER"; }

# `podman cp`, for the same reason t_spawn is `podman exec -d`: the generic
# implementation goes through `wkdev-enter`, and that is an interactive-shell
# wrapper rather than a byte pipe. A 1396-byte file read with `t_exec ... cat`
# arrived as 1399 bytes and xz refused it -- three bytes of difference that
# nothing at the call site could have seen.
t_pull() {
    local name="$1" src="$2" dest="$3"
    _podman cp "$(_ctr "$name"):$src" "$dest"
}

# A directory out of the container, in one `podman cp`: the generic rsync
# implementation would need rsync inside the container and a transport to run
# it over, and podman already has one.
#
# The trailing `/.` in `<ctr>:<dir>/.` copies the contents rather than nesting
# the directory inside the destination -- `cp -r`'s rule, and podman follows it.
t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    # `podman cp` copies a tree or nothing; it has no filter. Refused rather
    # than ignored, because a caller that asked for a product tree and got a
    # 39 GB build tree would not find out until the disk did.
    [ $# -eq 0 ] || die "t_pull_dir: the container driver cannot exclude paths ($*).
    Copy the whole tree, or make the selection inside the workspace first."
    rm -rf "$dest"; mkdir -p "$dest"
    _podman cp "$(_ctr "$name"):$src/." "$dest"
}

# podman's own detached exec: the generic `setsid nohup` in lib/target.sh does
# not survive here -- when the `podman exec` client exits, podman tears the
# session down regardless. Verified: a detached `sleep 3; echo` never wrote its
# file that way, but did under `podman exec -d`.
#
# Bypassing wkdev-enter means reproducing two things it would otherwise do: the
# bridge (ensure-bridge.sh, for egress -- a detached build fetching sources
# needs it too), and the user/login shell (`podman exec` defaults to root with
# no login shell, so the SDK's ~/.bash_profile PATH is missing).
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    local c u; c=$(_ctr "$name"); u="$WKDEV_CONTAINER_USER"
    # $$ is written before the exec, so the pid recorded is the pid the command
    # goes on to have. Redirected explicitly: under `exec -d` stdout is
    # podman's and goes nowhere.
    _podman exec -d --user "$u" "$c" \
        /opt/wk-tools/container/proxy/ensure-bridge.sh \
        /usr/bin/env "USER=$u" "HOME=/home/$u" bash --login -c \
        "echo \$\$ > $(sh_quote "$pidf"); exec $(sh_quote "$@") > $(sh_quote "$log") 2>&1 < /dev/null" \
        >/dev/null
}

t_enter() {
    local name="$1"
    local c; c=$(_ctr "$name")
    # Interactive shells do not go through t_exec, so start the bridge first.
    _podman exec -d "$c" /opt/wk-tools/container/proxy/ensure-bridge.sh true 2>/dev/null || true
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --name "$c"
}

# --- an ssh route into the container, for Zed ---------------------------------
#
# Zed drives the system `ssh` binary and nothing else, so the route has to
# speak the ssh protocol -- but a container workspace has no network interface
# at all (`--network none`, see _sdk_opts), so there is no address to give it.
#
# `podman exec` is the substitute on both hosts: sshd's inetd mode (`-i`) is a
# protocol conversation on stdin/stdout, so one exec is a complete transport
# (`ProxyCommand container/ssh-transport.sh <name>`), at the same privilege
# `wk enter` already has. This is also what makes one mechanism serve both
# hosts: the ProxyCommand carries no address, so it is correct from wherever
# podman is reached, unlike a `HostName localhost` alias that pointed at the
# wrong filesystem on Linux and into a VM Zed cannot reach on macOS.

# How the *host* reaches podman. Inside the VM (and on Linux) podman is local;
# from a macOS host it is the machine's rootless connection, named explicitly
# because podman's default connection here is the *rootful* one (`wk-root`) and
# the workspaces are rootless -- a `podman exec` through the default sees a
# different set of containers and reports the workspace as absent.
_hpodman() {
    if [ -n "${WK_IN_VM:-}" ] || ! is_macos; then
        podman "$@"
    else
        podman -c "${WK_MACHINE:-wk}" "$@"
    fi
}

# The workspace user, asked of the container rather than assumed:
# `WKDEV_CONTAINER_USER` is the *invoking* user, which on macOS is wrong by
# construction -- the container was created by `wk new`'s forwarded half inside
# the podman VM (user `core`), not this Mac account. Its working directory
# (its home) is the one place both hosts agree.
_ctr_user() {
    local c home
    c=$(_ctr "$1")
    home=$(_hpodman inspect "$c" --format '{{.Config.WorkingDir}}' 2>/dev/null) || home=""
    case "$home" in
        /home/?*) printf '%s' "${home#/home/}" ;;
        *) return 1 ;;
    esac
}

_ctr_home() { echo "/home/$(_ctr_user "$1")"; }

# The sshd command line, in one place because two callers need to agree on it:
# this file, which installs what it names, and container/ssh-transport.sh,
# which runs it.
#
#   -i             inetd mode: the protocol on stdin/stdout, no listener at all.
#   -e -o LogLevel=ERROR  stderr is the only diagnostic channel here (no
#                  journal); ERROR avoids narrating every accepted key.
#   -f /dev/null   no config file, so nothing here can drift from what wk asked.
#   UsePAM no      no session manager to answer to, and PAM adds /etc/pam.d/sshd
#                  to the list of things that must be right.
#   AllowUsers     the exec that starts this is root's; without it a root login
#                  would be permitted by a server whose job is one account.
#   SetEnv         one option with every assignment, not one each: ssh config
#                  keywords take the first value and ignore the rest, so six
#                  `-o SetEnv=` flags would set exactly one variable (found here
#                  as `http_proxy` set and `https_proxy` missing).
#   internal-sftp  not sftp-server: an external subsystem runs through the login
#                  shell, and workspace ~/.bashrc `cd`s to /src/WebKit
#                  (firstrun.sh) -- so a `~`-relative upload (Zed's own server
#                  binary) failed "No such file or directory" in a directory
#                  that plainly exists. internal-sftp runs with no shell in the way.
t_ssh_sshd_cmd() {
    local name="$1" u h
    u=$(_ctr_user "$name") || return 1
    h="/home/$u"
    # SetEnv is why this differs from a bare sshd -i: without it, sshd hands the
    # login shell a sanitised environment and a container has no other route
    # out (`wk enter` worked, Zed's terminal had no DNS/github/Claude --
    # `http_proxy=[]` over ssh vs `http_proxy=[http://127.0.0.1:3128]`).
    #
    # ensure-bridge wraps this for the reason it wraps every exec path:
    # 127.0.0.1:3128 is only listening because something started the forwarder.
    printf '%s' "mkdir -p /run/sshd && exec /opt/wk-tools/container/proxy/ensure-bridge.sh \
/usr/sbin/sshd -i -e -f /dev/null \
-o HostKey=$h/.wk-ssh/ssh_host_ed25519_key \
-o AuthorizedKeysFile=.ssh/authorized_keys \
-o UsePAM=no -o PidFile=none -o PermitRootLogin=no -o AllowUsers=$u -o LogLevel=ERROR \
-o Subsystem='sftp internal-sftp' \
-o 'SetEnv=http_proxy=http://127.0.0.1:3128 https_proxy=http://127.0.0.1:3128 \
HTTP_PROXY=http://127.0.0.1:3128 HTTPS_PROXY=http://127.0.0.1:3128 \
no_proxy=localhost,127.0.0.1,::1 NO_PROXY=localhost,127.0.0.1,::1'"
}

# The transport itself: one exec, from whichever machine runs Zed.
#
# /run/sshd is created here, not at install time: /run is a tmpfs podman
# remakes on every container start, so a directory made once is gone after the
# first `wk stop` and sshd fails "Missing privilege separation directory" --
# which over a ProxyCommand reads as ssh closing for no reason.
#
# No `exec`: _hpodman is a shell function, and exec only replaces this process
# with a real program (same trap cmd/enter notes). Not worth a second spelling
# of the podman invocation to save one process on a connection Zed opens rarely.
t_ssh_exec() {
    local name="$1" cmd
    cmd=$(t_ssh_sshd_cmd "$name") \
        || die "workspace '$name' has no container to reach (podman does not know it)"
    _hpodman exec -i "$(_ctr "$name")" /bin/sh -c "$cmd"
}

# Everything the transport above needs, made true. Idempotent and cheap to
# re-run -- every step tests the thing itself, so this is the resume for a
# `wk zed` interrupted half-way (CLAUDE.md, crash-only).
#
#   sshd            installed on first use through the egress proxy (hence
#                   ports.ubuntu.com in the allowlist, container/proxy/
#                   wk-proxy.py) since the SDK image carries only the client.
#                   Wrapped in ensure-bridge.sh for the reason t_spawn is: this
#                   bypasses wkdev-enter, so there is no egress otherwise.
#   host key        in the workspace's home, so it survives a container restart
#                   without being state anybody has to manage.
#   authorized_keys this machine's zed key, generated here and never leaving
#                   it; the workspace holds only the public half.
#
# t_ssh_prepare <name>
t_ssh_prepare() {
    local name="$1" c u h pub waited=0 said=""
    c=$(_ctr "$name")

    # The user first: everything below is a path in that user's home, and
    # failing to find one is the honest answer to "is this a workspace at all"
    # -- which on a macOS host cannot be asked of the store (see below).
    u=$(_ctr_user "$name") \
        || die "no container workspace called '$name' on this machine.
    'wk ls' lists the ones there are, and 'wk start' brings the podman machine up
    if it is stopped. (An editor reaches a container over podman from here: the
    workspace has no network interface, so there is no other route in.)"
    h="/home/$u"

    # Readiness asked of the container, not the store: on macOS the store is
    # inside the podman VM, so ws_state (every other command's gate) cannot be
    # computed out here. Same marker, firstrun's last act; only how it is read
    # differs.
    #
    # Path spelled out, not `~`: these execs run as root, where `~` is /root,
    # so the test would never come true and the wait would end at the timeout.
    #
    # A wait rather than a refusal, for wait_ready's reason: creation is
    # asynchronous, and an editor opened on a checkout that is still being
    # cloned is a tree of missing files. `.wk-firstrun-complete` is accepted
    # alongside the marker for t_created's reason, and goes when that clause does.
    while ! _hpodman exec "$c" /bin/sh -c \
            "test -f '$h/$WK_READY_MARKER' || test -f '$h/.wk-firstrun-complete'" 2>/dev/null; do
        _hpodman container exists "$c" 2>/dev/null \
            || die "the container for '$name' is gone -- 'wk status $name' says what is left"
        [ -n "$said" ] || { info "waiting for '$name' to finish being created"; said=1; }
        [ "$waited" -lt "${WK_READY_WAIT:-1800}" ] \
            || die "'$name' is still being created after ${waited}s.
    It is detached, so it is still going -- 'wk status $name' says where."
        sleep 2; waited=$((waited + 2))
    done
    [ -z "$said" ] || info "'$name' is ready"

    if ! _hpodman exec "$c" test -x /usr/sbin/sshd 2>/dev/null; then
        info "installing openssh-server in '$name' (once per workspace; Zed needs an sshd to talk to)"
        # The grep drops apt's "configured multiple times" warnings, which the
        # SDK image produces by the screenful on every update (it carries an
        # armhf sources file alongside the native one) and which say nothing
        # about this install. Everything else apt has to say is kept.
        _hpodman exec "$c" /opt/wk-tools/container/proxy/ensure-bridge.sh \
            /bin/sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends openssh-server' \
            2>&1 | { grep -vE '^W: (Target|Skipping)' >&2 || true; } || die "could not install openssh-server in '$name', and Zed needs that one package.
    A refused fetch is logged by the egress proxy as 'DENY <host>:<port>' -- it
    runs as the wk-proxy user service on the machine that holds the containers
    (journalctl --user -u wk-proxy), and its allowlist is
    container/proxy/wk-proxy.py."
        _hpodman exec "$c" test -x /usr/sbin/sshd \
            || die "openssh-server installed in '$name' and there is still no /usr/sbin/sshd"
    fi

    # As the workspace user, not root: a host key root owns is a host key sshd
    # can read and the workspace cannot regenerate, and every file under this
    # home belongs to that user.
    _hpodman exec --user "$u" "$c" /bin/sh -c "
        set -e
        install -d -m 0700 '$h/.wk-ssh' '$h/.ssh'
        [ -f '$h/.wk-ssh/ssh_host_ed25519_key' ] ||
            ssh-keygen -q -t ed25519 -N '' -C 'wk-$name host key' \
                -f '$h/.wk-ssh/ssh_host_ed25519_key'
        touch '$h/.ssh/authorized_keys'
        chmod 0600 '$h/.ssh/authorized_keys'" \
        || die "could not prepare the ssh identity in '$name'"

    pub=$(zed_key_pub) || die "could not create this machine's zed key"
    if ! _hpodman exec --user "$u" "$c" grep -qsF "$pub" "$h/.ssh/authorized_keys"; then
        printf '%s\n' "$pub" | _hpodman exec -i --user "$u" "$c" \
            /bin/sh -c "cat >> '$h/.ssh/authorized_keys'" \
            || die "could not authorise this machine's zed key in '$name'"
        changed "authorised this machine's zed key in '$name'"
    fi

    # Rewritten every time rather than only when absent: the alias is derived
    # from the workspace's own user and from where this checkout is, and both
    # can change under it (a workspace recreated on the other host, a wk-tools
    # moved). Recomputing it costs nothing and cannot go stale.
    ssh_alias_set "$name" "wk-$name.container.invalid" "$u" "$(zed_key)" \
        "ProxyCommand $WK_ROOT/container/ssh-transport.sh $name"
}

# The reverse of t_stop, and its mirror image: this container cannot start
# inside a podman machine that is not running, and `wk start` (bare) already
# knows to bring the machine up first for its own sweep -- narrowed to one
# workspace, this has to do the same thing itself rather than assume it.
t_start() {
    local name="$1" c mstate
    if is_macos && [ -z "${WK_IN_VM:-}" ]; then
        mstate=$(_machine_state "${WK_MACHINE:-wk}")
        if [ "$mstate" = absent ]; then
            die "no machine '${WK_MACHINE:-wk}' -- run ./setup"
        elif [ "$mstate" != running ]; then
            info "starting machine '${WK_MACHINE:-wk}'"
            podman machine start "${WK_MACHINE:-wk}" >/dev/null
        fi
    fi
    c=$(_ctr "$name")
    _hpodman container exists "$c" 2>/dev/null \
        || die "no container for '$name' -- 'wk status $name' says what is left"
    if [ "$(_hpodman inspect "$c" --format '{{.State.Status}}' 2>/dev/null)" = running ]; then
        info "'$name' is already running"
        return 0
    fi
    _hpodman start "$c" >/dev/null
    info "started '$name'"
}

# One container, not the bulk sweep `wk stop` (bare) does with `_in_machine`:
# `_hpodman`, because cmd/stop declares `forward=no` and so runs directly on
# the host, never inside the podman VM -- on macOS that has to be the
# connection that reaches across, the same one t_ssh_exec uses.
t_stop() {
    local name="$1" c
    c=$(_ctr "$name")
    if ! _hpodman container exists "$c" 2>/dev/null; then
        info "no container for '$name' -- nothing to stop"
        return 0
    fi
    if [ "$(_hpodman inspect "$c" --format '{{.State.Status}}' 2>/dev/null)" != running ]; then
        info "'$name' is not running"
        return 0
    fi
    _hpodman stop --time 30 "$c" >/dev/null
    info "stopped '$name'"
}

t_destroy() {
    local name="$1"
    local c ws
    c=$(_ctr "$name")
    ws=$(wk_ws_dir "$name")

    if _podman container exists "$c" 2>/dev/null; then
        _podman rm -f "$c" >/dev/null
        info "removed container $c"
    fi

    # The overlay upper and work directories are podman's blind spot: it
    # creates nothing and deletes nothing when they are user-managed. Removing
    # them here is what makes "deleting a workspace reclaims everything" true.
    #
    # Rootless needs `podman unshare` for it. With --userns keep-id the
    # container's root is a *subordinate* uid on the host (100000+), not this
    # user, so anything the container created as root -- everything .wkdev-init
    # writes -- is undeletable from outside the namespace. Plain rm -rf fails
    # partway and leaves a workspace that looks removed and is not.
    if [ -d "$ws" ]; then
        podman unshare rm -rf "$ws" 2>/dev/null || rm -rf "$ws"
        [ -d "$ws" ] && warn "could not fully remove $ws" || info "removed $ws"
    fi
}
