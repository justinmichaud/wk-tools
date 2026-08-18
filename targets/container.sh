# Target driver: podman container.
#
# Implements the driver contract (t_create, t_exec, t_destroy, t_info, t_list)
# on top of the webkit-container-sdk. Commands under cmd/ call only these
# functions, never podman directly, so adding a new kind of environment later
# means adding a file here and nothing else.
#
# Two sandbox modes, because the two hosts differ in what they can enforce:
#
#   rootless-proxy   Linux workstation. Rootless podman, --userns keep-id, and
#                    --network none: the workspace has no network interface at
#                    all and reaches the outside only through the unix socket of
#                    an egress proxy running as the user. Nothing here needs a
#                    privilege.
#
#   vm-nftables      Inside the macOS podman machine. Rootful podman on a bridge
#                    network, with the egress policy in nftables' forward chain.
#
# The VM path is not a preference either way -- it is what was built first and
# what macOS is verified against. The Linux path deliberately does not copy it:
# rootless podman has no filterable forward path (its network helper re-emits
# traffic from a randomly named cgroup scope in the init namespace), so keeping
# nftables would have meant keeping rootful podman, and rootful podman means a
# container escape is a root escape. Removing the interface removes the need for
# a filter. See docs/HANDOFF-macos-proxy.md for moving the VM to the same model.

if [ -n "${WK_IN_VM:-}" ]; then
    WK_SDK="${WK_SDK:-/opt/webkit-container-sdk}"
else
    WK_SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"
fi

# Every SDK script checks this on line 5 and aborts without it. Normally set by
# register-sdk-on-host.sh, which is a shell-rc thing we deliberately do not use.
export WKDEV_SDK="$WK_SDK"

if [ -n "${WK_IN_VM:-}" ]; then
    WK_SANDBOX=vm-nftables
else
    WK_SANDBOX=rootless-proxy
fi

if [ "$WK_SANDBOX" = vm-nftables ]; then
    WK_NETWORK="${WK_NETWORK:-wk}"

    # Rootful, because the egress policy is nftables in the VM's forward chain
    # and rootless pasta traffic never reaches it.
    _sdk() { sudo -E "$@"; }
    _podman() { sudo podman "$@"; }

    # Under rootful podman there is no --userns keep-id, so the user is created
    # inside the image by the patched .wkdev-init, with ids taken from whoever
    # owns the storage -- they must match or the container cannot write to its
    # own home, the caches, or the checkout.
    export WKDEV_CONTAINER_UID="$(stat -c %u "$WK_STORE" 2>/dev/null || echo 1000)"
    export WKDEV_CONTAINER_GID="$(stat -c %g "$WK_STORE" 2>/dev/null || echo 1000)"
    # Must be an account that exists on the VM host: argsparse type-checks
    # --user/--group with `getent`, so a name that exists only inside the image
    # fails validation before podman is ever invoked.
    export WKDEV_CONTAINER_USER="${WK_CONTAINER_USER:-core}"
else
    # Rootless. `sudo` appears nowhere in this file's Linux path, and that is
    # the property to preserve: if a wk command ever needs a password, it has
    # stopped being something an unattended agent can use.
    _sdk() { "$@"; }
    _podman() { podman "$@"; }

    # keep-id maps the invoking user through unchanged, so the container user is
    # simply this user. No id derivation, no mismatch between the checkout, the
    # caches and the home directory.
    export WKDEV_CONTAINER_UID="$(id -u)"
    export WKDEV_CONTAINER_GID="$(id -g)"
    export WKDEV_CONTAINER_USER="${WK_CONTAINER_USER:-$(id -un)}"
fi
# wkdev-create defaults --shell to ${SHELL}, the *host* user's shell. That path
# need not exist in the image -- zsh does not -- and when it does not, the
# firstrun hook silently fails with "su: failed to execute /usr/bin/zsh" and the
# workspace comes up without its git identity, push key or Claude install.
export WKDEV_CONTAINER_SHELL=/bin/bash

# GPU policy lives with the rest of the Linux host support, because what has to
# be injected depends entirely on the host's driver stack.
if [ "$WK_SANDBOX" = rootless-proxy ] && [ -f "$WK_ROOT/host/linux/gpu.sh" ]; then
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

t_list() {
    _podman ps -a --filter 'name=^wk-' --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
        | sed 's/^wk-//'
}

t_info() {
    local c; c=$(_ctr "$1")
    _podman inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo absent
}

# Flags that differ between the two sandbox modes.
#
# Two lists, because they are consumed differently: wkdev-create has its own
# options (--network, --isolated), while everything else is passed through to
# podman verbatim inside --additional-flags. Mixing them is a confusing failure
# -- podman rejects --isolated as an unknown flag, and a --network in both
# places means the container gets two.
_sdk_opts() {
    if [ "$WK_SANDBOX" = vm-nftables ]; then
        printf '%s\n' --network "$WK_NETWORK"
    else
        # --network none is the boundary. Not a filtered interface: no
        # interface. Loopback still exists inside the namespace, which is all
        # run-benchmark's local web server needs.
        #
        # --isolated drops the host session: no D-Bus, no keyring, no dconf, no
        # journal, no X11, no host home, no shared runtime directory. The
        # session bus in particular is a complete host escape and has nothing
        # to do with the network, so no firewall would have caught it.
        printf '%s\n' --network none --isolated
    fi
}

_sandbox_flags() {
    [ "$WK_SANDBOX" = vm-nftables ] && return 0

    local rt; rt=$(_wk_runtime)
    mkdir -p "$rt"

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
         $(gpu_flags)"
}

t_create() {
    local name="$1" base_id="$2"
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
    printf '%s\n' "$base_id" > "$ws/base-id"

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
         --env CCACHE_MAXSIZE=${WK_CCACHE_MAXSIZE:-40G}
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
         --env WKDEV_OFFLINE=1
         --env WK_FORKS=$(wk_push_forks | awk '{printf "%s%s:%s:%s", sep, $1, $2, $3; sep=","}')
         $(_sandbox_flags)"
    )

    info "creating workspace '$name' from base $base_id ($WK_SANDBOX)"
    # shellcheck disable=SC2046 -- _sdk_opts is a deliberate list of words.
    _sdk "$WK_SDK/scripts/host-only/wkdev-create" \
        $(_sdk_opts) \
        --name "$c" \
        --shell "$WKDEV_CONTAINER_SHELL" \
        --user "$WKDEV_CONTAINER_USER" \
        --group "$WKDEV_CONTAINER_USER" \
        --home "$ws/home" \
        "${flags[@]}"

    # Drop the firstrun hook where .wkdev-init will pick it up on first start.
    install -m 0755 "$WK_ROOT/container/firstrun.sh" "$ws/home/.wkdev-firstrun"
}

# Every exec goes through ensure-bridge, which starts the loopback-to-proxy
# forwarder if it is not already running. Wrapping rather than checking
# separately keeps this to one container exec: a second round trip on every
# `wk run` would be paid forever for something almost always already true.
_wrap_cmd() {
    if [ "$WK_SANDBOX" = rootless-proxy ]; then
        printf '%s\n' /opt/wk-tools/container/proxy/ensure-bridge.sh
    fi
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
    [ "$WK_SANDBOX" = rootless-proxy ] && \
        _podman exec -d "$c" /opt/wk-tools/container/proxy/ensure-bridge.sh true 2>/dev/null
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
        if [ "$WK_SANDBOX" = rootless-proxy ]; then
            podman unshare rm -rf "$ws" 2>/dev/null || rm -rf "$ws"
        else
            rm -rf "$ws"
        fi
        [ -d "$ws" ] && warn "could not fully remove $ws" || info "removed $ws"
    fi
}
