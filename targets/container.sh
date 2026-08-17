# Target driver: podman container.
#
# Implements the driver contract (t_create, t_exec, t_destroy, t_info, t_list)
# on top of the webkit-container-sdk. Commands under cmd/ call only these
# functions, never podman directly, so adding a new kind of environment later
# means adding a file here and nothing else.

WK_NETWORK="${WK_NETWORK:-wk}"
WK_SDK="${WK_SDK:-/opt/webkit-container-sdk}"

# Every SDK script checks this on line 5 and aborts without it. Normally set by
# register-sdk-on-host.sh, which is a shell-rc thing we deliberately do not use.
export WKDEV_SDK="$WK_SDK"

# Everything runs under rootful podman, and that is not a convenience.
#
# Rootless podman uses pasta, which terminates container traffic and re-emits it
# as ordinary sockets from the init namespace. Such traffic reaches the output
# and postrouting chains but never `forward`, so there is nothing for the
# workspace egress policy to filter -- the firewall would load, look correct,
# and enforce nothing. Rootful gives a real bridge, and with it a real boundary.
#
# The workspace network and the container user both live in root's podman
# storage as a result; rootless podman cannot see either.
_sdk() { sudo -E "$@"; }
_podman() { sudo podman "$@"; }

# The container's own account. Under rootful podman there is no --userns
# keep-id, so the user is created inside the image by the patched .wkdev-init.
#
# The ids are derived from whoever owns the storage, not hardcoded: rootful
# podman maps uids 1:1, so if the in-container user's uid did not match the
# owner of the bind-mounted ccache, skills and home directories, it would be
# unable to write to any of them -- and the failure would surface much later as
# a confusing permission error in the middle of a build.
export WKDEV_CONTAINER_UID="$(stat -c %u "$WK_STORE" 2>/dev/null || echo 1000)"
export WKDEV_CONTAINER_GID="$(stat -c %g "$WK_STORE" 2>/dev/null || echo 1000)"
# Must be an account that exists on the VM host: argsparse type-checks
# --user/--group with `getent`, so a name that exists only inside the image
# fails validation before podman is ever invoked. `core` is the VM's own user
# and is uid 1000, which lines up with the account .wkdev-init creates.
export WKDEV_CONTAINER_USER="${WK_CONTAINER_USER:-core}"
export WKDEV_CONTAINER_SHELL=/bin/bash

_ctr() { echo "wk-$1"; }

t_list() {
    _podman ps -a --filter 'name=^wk-' --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
        | sed 's/^wk-//'
}

t_info() {
    local c; c=$(_ctr "$1")
    _podman inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo absent
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

    local -a flags
    flags=(
        --additional-flags
        "--volume /opt/wk-tools:/opt/wk-tools:ro
         --volume $overlay
         --volume $WK_STORE/cache/ccache:/ccache
         --volume $WK_STORE/cache/yocto:/cache/yocto
         --volume $WK_STORE/cache/buildroot:/cache/buildroot
         --volume $WK_STORE/skills:/skills
         --volume $WK_STORE/secrets:/secrets:ro
         --memory $(envelope_mem_mb)m
         --cpus $(envelope_cores)
         --env CCACHE_DIR=/ccache
         --env CCACHE_MAXSIZE=${WK_CCACHE_MAXSIZE:-50G}
         --env DL_DIR=/cache/yocto/downloads
         --env SSTATE_DIR=/cache/yocto/sstate
         --env BR2_DL_DIR=/cache/buildroot/dl
         --env BR2_CCACHE_DIR=/cache/buildroot/ccache
         --env WK_WORKSPACE=$name
         --env WKDEV_OFFLINE=1"
    )

    info "creating workspace '$name' from base $base_id"
    _sdk "$WK_SDK/scripts/host-only/wkdev-create" \
        --name "$c" \
        --network "$WK_NETWORK" \
        --user "$WKDEV_CONTAINER_USER" \
        --group "$WKDEV_CONTAINER_USER" \
        --home "$ws/home" \
        "${flags[@]}"

    # Drop the firstrun hook where .wkdev-init will pick it up on first start.
    install -m 0755 "$WK_ROOT/container/firstrun.sh" "$ws/home/.wkdev-firstrun"
}

t_exec() {
    local name="$1"; shift
    local c; c=$(_ctr "$name")
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --name "$c" --exec -- "$@"
}

t_enter() {
    local name="$1"
    local c; c=$(_ctr "$name")
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
    if [ -d "$ws" ]; then
        rm -rf "$ws"
        info "removed $ws"
    fi
}
