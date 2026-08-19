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

# One sandbox model, both hosts. The macOS VM used to run a second one --
# rootful podman on a bridge with the egress policy in nftables -- and a
# boundary implemented twice is a boundary understood once and verified never.
#
# The nftables model also forced rootful podman, because rootless podman has no
# filterable forward path: its network helper re-emits container traffic from
# the init namespace in a cgroup scope with a random name, so there is no
# stable selector to match on. Under rootful podman a container escape is a
# root escape, which is a far worse trade than a proxy that needs no privilege
# at all. Removing the interface removes the need for the filter.
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

t_info() {
    local c; c=$(_ctr "$1")
    _podman inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo absent
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

# Flags that differ between the two sandbox modes.
#
# Two lists, because they are consumed differently: wkdev-create has its own
# options (--network, --isolated), while everything else is passed through to
# podman verbatim inside --additional-flags. Mixing them is a confusing failure
# -- podman rejects --isolated as an unknown flag, and a --network in both
# places means the container gets two.
_sdk_opts() {
    # --network none is the boundary. Not a filtered interface: no interface.
    # Loopback still exists inside the namespace, which is all run-benchmark's
    # local web server needs.
    #
    # --isolated drops the host session: no D-Bus, no keyring, no dconf, no
    # journal, no X11, no host home, no shared runtime directory. The session
    # bus in particular is a complete host escape and has nothing to do with
    # the network, so no firewall would have caught it.
    printf '%s\n' --network none --isolated
}

# _sandbox_flags <arch>
_sandbox_flags() {
    local arch="${1:-native}"
    local rt; rt=$(_wk_runtime)
    mkdir -p "$rt"

    # No GPU for a non-native workspace, and not as a policy: the NVIDIA
    # userspace libraries have to match the host kernel driver exactly and are
    # published for aarch64 only, so on armhf there is nothing to inject and
    # asking for the device nodes anyway produces a container that fails to
    # start. wkdev-create makes the same call for its own nvidia handling
    # whenever --arch is set. See arch_has_gpu in lib/arch.sh.
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

    # Recorded before creation, not after: everything downstream -- the build
    # flags, the benchmark preflight, `wk ls` -- resolves the architecture from
    # here, and a workspace that exists without this file would be reported as
    # native and then built as native.
    printf '%s\n' "$arch" > "$ws/arch"

    # The overlay must be spelled with an explicit upperdir and workdir. A bare
    # :O gives an ephemeral upper that podman throws away when the container
    # stops, which would silently discard a day's work on the first restart.
    #
    # podman does NOT delete a user-managed upperdir on container removal --
    # that is t_destroy's job, or the workspace leaks forever.
    local overlay="$base:/src/WebKit:O,upperdir=$ws/changes,workdir=$ws/overlay-work"

    # The ccache settings are not decoration. WebKit's own CMake sets these on
    # Apple only (Source/cmake/WebKitCCache.cmake), so on Linux every compile
    # that uses the precompiled header is reported "Could not use precompiled
    # header" and skips the cache entirely -- measured here as 371 of 752 calls
    # on a JSC build, which is most of the expensive ones. BASEDIR and NOHASHDIR
    # are what let one cache serve every workspace rather than one per checkout.
    local -a flags
    flags=(
        --additional-flags
        "--volume ${WK_TOOLS_SRC:-$WK_ROOT}:/opt/wk-tools:ro
         --volume $overlay
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
         $(_sandbox_flags "$arch")"
    )

    # An explicit image and an explicit podman architecture, both from
    # lib/arch.sh and neither inferred. --image is a wk addition to the SDK
    # (sdk-patches/apply.sh section 12); without it `--arch arm` picks the
    # aarch64 image of the current SDK version and asks podman for a 32-bit
    # container from it.
    local -a archopts=()
    if ! arch_is_native "$arch"; then
        archopts=(--arch "$(arch_podman "$arch")" --image "$(arch_image "$arch")")
    fi

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
    install -m 0755 "$WK_ROOT/container/firstrun.sh" "$ws/home/.wkdev-firstrun"

    # Last, and that is the point: base-id is the completion marker this target
    # is read by (ws_state in lib/target.sh). Written first -- as it used to be
    # -- a `wk new` killed during wkdev-create left a workspace that pinned a
    # snapshot, looked finished to every command, and had no container; and a
    # re-run re-pinned base-id over the `changes/` layer the first attempt had
    # already started, which is undefined behaviour with the overlay.
    #
    # It is also what `wk gc` counts as a snapshot's refcount, so writing it
    # last is only safe because gc and new take the store lock (lib/common.sh).
    printf '%s\n' "$base_id" > "$ws/base-id"
}

# Creation is asynchronous: wkdev-create returns as soon as the container is
# started, and .wkdev-init runs the firstrun hook afterwards -- installing the
# push keys, the Claude CLI, helix, the lldb config and the shell rc.
#
# .wkdev-init does not check how that hook exited, so a failure part-way
# through is silent: the container is up, creation "succeeded", and the
# workspace is missing everything after the failing step. firstrun.sh writes a
# marker as its last act; this waits for it and reports honestly when it never
# appears.
t_ready() {
    local name="$1" ws c i=0
    ws=$(wk_ws_dir "$name"); c=$(_ctr "$name")
    while [ "$i" -lt 300 ]; do
        [ -f "$ws/home/.wk-firstrun-complete" ] && return 0
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

t_enter() {
    local name="$1"
    local c; c=$(_ctr "$name")
    # Interactive shells do not go through t_exec, so start the bridge first.
    _podman exec -d "$c" /opt/wk-tools/container/proxy/ensure-bridge.sh true 2>/dev/null || true
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --name "$c"
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
