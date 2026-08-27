# Target driver: podman container, on top of the webkit-container-sdk.
#
# rootless podman, --userns keep-id, --network none: no network interface,
# egress only through the proxy's unix socket, no privilege needed anywhere.

if [ -n "${WK_IN_VM:-}" ]; then
    WK_SDK="${WK_SDK:-/opt/webkit-container-sdk}"
else
    WK_SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"
fi

# Every SDK script checks this and aborts without it (register-sdk-on-host.sh
# normally sets it -- a shell-rc mechanism this deliberately bypasses).
export WKDEV_SDK="$WK_SDK"

# rootless podman has no filterable forward path; the alternative, rootful
# podman with nftables, turns a container escape into a root escape.
WK_SANDBOX=rootless-proxy

# No sudo anywhere in the daily path, which is what keeps this usable by an
# unattended agent.
_sdk() { "$@"; }
_podman() { podman "$@"; }

# keep-id maps the invoking user through unchanged: no id derivation, no
# mismatch between checkout/caches/home.
export WKDEV_CONTAINER_UID="$(id -u)"
export WKDEV_CONTAINER_GID="$(id -g)"
export WKDEV_CONTAINER_USER="${WK_CONTAINER_USER:-$(id -un)}"

# wkdev-create defaults --shell to the host's $SHELL, which need not exist in
# the image (zsh does not) -- firstrun then fails silently with no git
# identity, push key or Claude install.
export WKDEV_CONTAINER_SHELL=/bin/bash

command -v arch_canon >/dev/null 2>&1 || . "$WK_ROOT/lib/arch.sh"

# GPU policy depends on the host's driver stack, so it lives with the Linux
# host support.
if [ -f "$WK_ROOT/host/linux/gpu.sh" ]; then
    . "$WK_ROOT/host/linux/gpu.sh"
else
    gpu_flags() { :; }
fi

# One directory of sockets a workspace may see, mounted at /run/wk rather
# than the whole runtime directory -- which would hand over the D-Bus socket too.
_wk_runtime() { echo "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk"; }

# In the VM the tooling is rsynced to /opt/wk-tools; on a workstation it is
# the checkout this script came from.
[ -n "${WK_IN_VM:-}" ] && WK_TOOLS_SRC=/opt/wk-tools

_ctr() { echo "wk-$1"; }

# Pushes wk-tools itself, not the store: on a workstation the mount source is
# this checkout (nothing to do); on macOS it's a copy inside the podman VM,
# refreshed by rsync. The mirror/snapshots are a separate, forwarded `wk sync`.
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

# Recorded at creation: the container reports the kernel's (host's)
# architecture, so `uname -m` in an armhf workspace answers aarch64.
t_arch() {
    local f; f="$(wk_ws_dir "$1")/arch"
    [ -f "$f" ] && cat "$f" || echo native
}

t_list() {
    _podman ps -a --filter 'name=^wk-' --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
        | sed 's/^wk-//'
}

# The container's home is a directory on this machine, so the readiness
# marker is a file here. `.wk-firstrun-complete` predates the marker contract.
# TODO: drop that clause once no pre-marker workspace is left anywhere.
t_created() {
    local h; h="$(wk_ws_dir "$1")/home"
    [ -f "$h/$WK_READY_MARKER" ] || [ -f "$h/.wk-firstrun-complete" ]
}

# Existing vs. running needs the marker: creation is asynchronous. `_hpodman`,
# not `_podman`: `wk stop` runs directly on a macOS host with `forward=no`.
t_info() {
    local c st
    c=$(_ctr "$1")
    st=$(_hpodman inspect "$c" --format '{{.State.Status}}' 2>/dev/null) || st=absent
    [ -n "$st" ] || st=absent
    [ "$st" = absent ] && { echo absent; return 0; }
    t_created "$1" || { echo creating; return 0; }
    echo "$st"
}

# Read, not exec'd: HEAD is a file on this machine (overlay upper layer), so
# reading it avoids a container exec's latency.
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
        *)                   printf 'detached %s' "$(printf '%s' "$ref" | cut -c1-10)" ;;
    esac
}

# wkdev-create's own options, kept apart from --additional-flags: podman
# rejects --isolated as unknown there, and --network in both places gives
# the container two.
_sdk_opts() {
    # --network none is the boundary; loopback still exists for
    # run-benchmark's local server. --isolated drops the host session
    # entirely -- D-Bus, keyring, dconf, X11, host home, shared runtime dir.
    printf '%s\n' --network none --isolated
}

# _sandbox_flags <arch>
_sandbox_flags() {
    local arch="${1:-native}"
    local rt; rt=$(_wk_runtime)
    mkdir -p "$rt"

    # No GPU for a non-native workspace: NVIDIA userspace libraries match
    # only the host kernel driver and ship for aarch64 (arch_has_gpu, lib/arch.sh).
    local gpu=""
    arch_has_gpu "$arch" && gpu=$(gpu_flags)

    # Mounting the directory, not the socket: the compositor's socket comes
    # and goes with `wk session` without recreating containers.
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

    # Before creation, not after: everything downstream resolves architecture
    # from this file.
    printf '%s\n' "$arch" > "$ws/arch"

    # Explicit upperdir/workdir: a bare :O gives an ephemeral upper podman
    # discards on container stop. podman never deletes a user-managed
    # upperdir on removal -- that is t_destroy's job.
    local overlay="$base:/src/WebKit:O,upperdir=$ws/changes,workdir=$ws/overlay-work"

    # Bind-mounted over WebKitBuild rather than left in the overlay: every
    # object a build writes would otherwise pay overlayfs's copy-up cost on
    # first write. A plain bind mount also makes WebKitBuild a mount point
    # git and an editor see, not tracked-repo churn.
    local build_mount="$ws/build:/src/WebKit/WebKitBuild"

    # $ws itself, at the same absolute path the host resolves for it: a
    # `wk build` typed inside the workspace uses the `local` driver, which
    # without this mount would resolve $WK_STORE to a path nothing outside
    # the container can see -- a build.status `wk status` never finds.
    # WK_LOCAL_STORE is what targets/local.sh reads to find this shared path.
    #
    # ccache settings: WebKit's CMake sets them on Apple only, so Linux
    # skips the precompiled header without them.
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

    # Explicit image and podman arch, neither inferred: without --image,
    # `--arch arm` would ask the aarch64 image for a 32-bit container.
    # WK_SDK_IMAGE overrides it, for the Yocto builder (image/yocto.sh).
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

    # .wkdev-init picks this up on first start; it writes `.wk-ready` as its
    # last act, so completion evidence lives next to the workspace.
    install -m 0755 "$WK_ROOT/container/firstrun.sh" "$ws/home/.wkdev-firstrun"

    # Last, deliberately: base-id is the completion marker (ws_state,
    # lib/target.sh). Written first, a killed `wk new` would re-pin it over an
    # already-started overlay on re-run.
    printf '%s\n' "$base_id" > "$ws/base-id"
}

# .wkdev-init runs firstrun (push keys, Claude CLI, helix, lldb config, shell
# rc) after the container starts, without checking its exit -- this waits for
# firstrun's marker and reports honestly when it never appears.
t_ready() {
    local name="$1" c i=0
    c=$(_ctr "$name")
    while [ "$i" -lt "${WK_READY_TIMEOUT:-300}" ]; do
        t_created "$name" && return 0
        # No process left to write the marker if the container is gone.
        _podman container exists "$c" >/dev/null 2>&1 || break
        sleep 1; i=$((i + 1))
    done

    warn "initialisation did not complete; last output from the container:"
    _podman logs "$c" 2>&1 | grep -vE "^\\s*$" | tail -8 | sed "s/^/    /" >&2 || true
    return 1
}

# Starts the loopback-to-proxy forwarder if not already running; wrapping
# keeps this to one container exec rather than a second round trip.
_wrap_cmd() {
    printf '%s\n' /opt/wk-tools/container/proxy/ensure-bridge.sh
}

# --quiet is not cosmetic: without it wkdev-enter prints its banner on stdout
# ahead of the command's output, mixing SDK chatter into the result (wk
# verify's interface list, wk bench's renderer string).
t_exec() {
    local name="$1"; shift
    local c; c=$(_ctr "$name")
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --quiet --name "$c" --exec -- $(_wrap_cmd) "$@"
}

# ASLR: podman's seccomp allow-list lacks personality(ADDR_NO_RANDOMIZE), so
# lldb reads "no personality()" with nothing pointing at the real cause.
# stop-on-exec: lldb's default stops the session at each child exec,
# hijacking `wk gui --lldb ui` away from the breakpoints set for `run`.
t_lldb_opts() {
    printf '%s' "-O 'settings set target.disable-aslr false'"
    printf '%s' " -O 'settings set target.process.stop-on-exec false'"
}

# Bind-mounted from `home/` in t_create, so naming it here lets the host
# follow a detached build's log with a plain `tail -f`.
t_home() { echo "/home/$WKDEV_CONTAINER_USER"; }

# `podman cp`, not the generic `t_exec ... cat`: wkdev-enter is an
# interactive-shell wrapper, not a byte pipe (a 1396-byte file arrived as
# 1399 bytes over it, and xz refused it).
t_pull() {
    local name="$1" src="$2" dest="$3"
    _podman cp "$(_ctr "$name"):$src" "$dest"
}

# One `podman cp`: the generic rsync implementation needs rsync inside the
# container and a transport to run it over. Trailing `/.` copies contents,
# not the directory itself.
t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    # `podman cp` has no filter: refused rather than silently over-copying.
    [ $# -eq 0 ] || die "t_pull_dir: the container driver cannot exclude paths ($*).
    Copy the whole tree, or make the selection inside the workspace first."
    rm -rf "$dest"; mkdir -p "$dest"
    _podman cp "$(_ctr "$name"):$src/." "$dest"
}

# podman's own detached exec: `setsid nohup` (lib/target.sh) does not
# survive here -- podman tears the session down when `podman exec` exits.
# Bypassing wkdev-enter means reproducing the bridge and login shell it
# would otherwise provide (`podman exec` defaults to root, no login shell).
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    local c u; c=$(_ctr "$name"); u="$WKDEV_CONTAINER_USER"
    # $$ is written before exec so the recorded pid is the command's own.
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

# --- an ssh route into the container, for Zed --------------------------------
#
# A container has no network interface, so there is no address for Zed's
# `ssh` to reach. `podman exec` substitutes: sshd's inetd mode (`-i`) makes
# one exec a complete transport (`ProxyCommand container/ssh-transport.sh <name>`).

# How the host reaches podman: local inside the VM and on Linux; from macOS,
# the rootless connection named explicitly, since podman's default here is
# the rootful one (`wk-root`).
_hpodman() {
    if [ -n "${WK_IN_VM:-}" ] || ! is_macos; then
        podman "$@"
    else
        podman -c "${WK_MACHINE:-wk}" "$@"
    fi
}

# Asked of the container, not assumed: WKDEV_CONTAINER_USER is the invoking
# user, wrong on macOS (the container was created inside the podman VM as
# `core`). Its working directory (home) is the one place both hosts agree.
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

# The sshd command line, in one place: this file installs what it names,
# container/ssh-transport.sh runs it.
#
#   -i             inetd mode: protocol on stdin/stdout, no listener.
#   AllowUsers     without it, root login (this exec's own user) would be
#                  permitted by a server whose job is one account.
#   SetEnv         one option with every assignment: ssh config keywords take
#                  only the first value, so six `-o SetEnv=` flags would set
#                  exactly one variable.
#   internal-sftp  not sftp-server, which runs through the login shell where
#                  ~/.bashrc `cd`s to /src/WebKit, breaking `~`-relative paths.
t_ssh_sshd_cmd() {
    local name="$1" u h
    u=$(_ctr_user "$name") || return 1
    h="/home/$u"
    # SetEnv is why this differs from bare sshd -i: without it there is no
    # other route out (`wk enter` worked, Zed's terminal had no DNS/github).
    # ensure-bridge wraps this because 127.0.0.1:3128 only listens once
    # something started the forwarder.
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

# /run/sshd is created here, not at install time: /run is a tmpfs podman
# remakes on every container start. No `exec`: _hpodman is a shell function.
t_ssh_exec() {
    local name="$1" cmd
    cmd=$(t_ssh_sshd_cmd "$name") \
        || die "workspace '$name' has no container to reach (podman does not know it)"
    _hpodman exec -i "$(_ctr "$name")" /bin/sh -c "$cmd"
}

# Idempotent -- every step tests itself, so this resumes a `wk zed`
# interrupted half-way. sshd is installed on first use (the SDK image carries
# only the client); the host key lives in the workspace's home.
#
# t_ssh_prepare <name>
t_ssh_prepare() {
    local name="$1" c u h pub waited=0 said=""
    c=$(_ctr "$name")

    # Failing to find the user honestly answers "is this a workspace at all"
    # (unanswerable from the store on macOS).
    u=$(_ctr_user "$name") \
        || die "no container workspace called '$name' on this machine.
    'wk ls' lists the ones there are, and 'wk start' brings the podman machine up
    if it is stopped. (An editor reaches a container over podman from here: the
    workspace has no network interface, so there is no other route in.)"
    h="/home/$u"

    # Asked of the container, not the store: on macOS the store is inside the
    # podman VM, so ws_state cannot be computed out here. Path spelled out,
    # not `~`: these execs run as root, where `~` is /root. A wait, not a
    # refusal: creation is asynchronous.
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
        # Drops apt's "configured multiple times" warnings (armhf+native sources).
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

    # As the workspace user, not root: a root-owned host key is undeletable by it.
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

    # Rewritten every time: the alias derives from the user and checkout
    # path, both of which can change under it.
    ssh_alias_set "$name" "wk-$name.container.invalid" "$u" "$(zed_key)" \
        "ProxyCommand $WK_ROOT/container/ssh-transport.sh $name"
}

# This container cannot start inside a podman machine that is not running,
# and `wk start` (bare) already brings the machine up for its own sweep.
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

# One container, not the bulk sweep `wk stop` (bare) does. `_hpodman`:
# cmd/stop runs directly on the host with `forward=no` -- on macOS that has
# to be the connection that reaches across, the same one t_ssh_exec uses.
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

    # Podman creates and deletes nothing for a user-managed overlay
    # upper/work dir. Rootless needs `podman unshare`: with --userns keep-id
    # the container's root is a subordinate host uid, so anything
    # .wkdev-init wrote as root is undeletable from outside the namespace.
    if [ -d "$ws" ]; then
        podman unshare rm -rf "$ws" 2>/dev/null || rm -rf "$ws"
        [ -d "$ws" ] && warn "could not fully remove $ws" || info "removed $ws"
    fi
}
