# Target driver: podman container on the webkit-container-sdk -- rootless, --userns
# keep-id, --network none, so egress is only through the proxy's unix socket.

if [ -n "${WK_IN_VM:-}" ]; then
    WK_SDK="${WK_SDK:-/opt/webkit-container-sdk}"
else
    WK_SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"
fi

export WKDEV_SDK="$WK_SDK"

WK_SANDBOX=rootless-proxy

_sdk() { "$@"; }

export WKDEV_CONTAINER_UID="$(id -u)"
export WKDEV_CONTAINER_GID="$(id -g)"
export WKDEV_CONTAINER_USER="${WK_CONTAINER_USER:-$(id -un)}"

export WKDEV_CONTAINER_SHELL=/bin/bash

command -v arch_canon >/dev/null 2>&1 || . "$WK_ROOT/lib/arch.sh"

if [ -f "$WK_ROOT/host/linux/gpu.sh" ]; then
    . "$WK_ROOT/host/linux/gpu.sh"
else
    gpu_flags() { :; }
fi

# /run/wk alone: the whole runtime directory would hand over D-Bus.
_wk_runtime() { echo "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk"; }

t_agent_sock() { echo /run/wk/ssh-agent.sock; }

[ -n "${WK_IN_VM:-}" ] && WK_TOOLS_SRC=/opt/wk-tools

_ctr() { echo "wk-$1"; }

t_sync() {
    info "nothing to copy: a container bind-mounts this checkout ($WK_ROOT) at
  /opt/wk-tools, so the tooling in one is never stale. What the VM has
  installed rather than mounted -- the proxy and injector units, the skills,
  its packages -- comes from:  ./setup --stage vmtools"
}

# Recorded at creation: `uname -m` in an armhf container answers the host's aarch64.
t_arch() {
    local f; f="$(wk_ws_dir "$1")/arch"
    [ -f "$f" ] && cat "$f" || echo native
}

t_list() {
    _hpodman ps -a --filter 'name=^wk-' --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
        | sed 's/^wk-//'
}

# TODO: drop the `.wk-firstrun-complete` clause once no pre-marker workspace is left.
t_created() {
    local h; h="$(wk_ws_dir "$1")/home"
    [ -f "$h/$WK_READY_MARKER" ] || [ -f "$h/.wk-firstrun-complete" ]
}

t_info() {
    local c st
    c=$(_ctr "$1")
    st=$(_hpodman inspect "$c" --format '{{.State.Status}}' 2>/dev/null) || st=absent
    [ -n "$st" ] || st=absent
    [ "$st" = absent ] && { echo absent; return 0; }
    t_created "$1" || { echo creating; return 0; }
    echo "$st"
}

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

# Not --additional-flags: podman rejects --isolated there, and a second --network wins.
_sdk_opts() {
    printf '%s\n' --network none --isolated
}

_sandbox_flags() {
    local arch="${1:-native}"
    local rt; rt=$(_wk_runtime)
    mkdir -p "$rt"

    # NVIDIA userspace matches only the host kernel driver, so no GPU cross-arch.
    local gpu=""
    arch_has_gpu "$arch" && gpu=$(gpu_flags)

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

t_create() {
    local name="$1" base_id="$2" arch="${3:-native}"
    local c ws base
    c=$(_ctr "$name")
    ws=$(wk_ws_dir "$name")
    base=$(base_path "$base_id")

    [ -d "$base" ] || die "base snapshot $base_id not found; run 'wk sync' first"
    _hpodman container exists "$c" 2>/dev/null && die "workspace '$name' already exists"

    ensure_dir "$ws"
    ensure_dir "$ws/changes"
    ensure_dir "$ws/overlay-work"
    ensure_dir "$ws/home"
    ensure_dir "$ws/build"

    printf '%s\n' "$arch" > "$ws/arch"

    # A bare :O gives an ephemeral upper podman discards on container stop.
    local overlay="$base:/src/WebKit:O,upperdir=$ws/changes,workdir=$ws/overlay-work"

    local build_mount="$ws/build:/src/WebKit/WebKitBuild"

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
         --volume $WK_STORE/agent-rw:/agent-rw
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

    # Without --image, `--arch arm` would ask the aarch64 image for a 32-bit container.
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

    install -m 0755 "$WK_ROOT/container/firstrun.sh" "$ws/home/.wkdev-firstrun"

    # Written last: base-id is the completion marker for a killed, re-run `wk new`.
    printf '%s\n' "$base_id" > "$ws/base-id"
}

t_ready() {
    local name="$1" c i=0
    c=$(_ctr "$name")
    while [ "$i" -lt "${WK_READY_TIMEOUT:-300}" ]; do
        t_created "$name" && return 0
        _hpodman container exists "$c" >/dev/null 2>&1 || break
        sleep 1; i=$((i + 1))
    done

    warn "initialisation did not complete; last output from the container:"
    _hpodman logs "$c" 2>&1 | grep -vE "^\\s*$" | tail -8 | sed "s/^/    /" >&2 || true
    return 1
}

_wrap_cmd() {
    printf '%s\n' /opt/wk-tools/container/proxy/ensure-bridge.sh
}

# --quiet: without it wkdev-enter's banner mixes SDK chatter into the output.
t_exec() {
    local name="$1"; shift
    local c; c=$(_ctr "$name")
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --quiet --name "$c" --exec -- $(_wrap_cmd) "$@"
}

# podman's seccomp allow-list lacks personality(ADDR_NO_RANDOMIZE); stop-on-exec would hijack `wk gui --lldb ui`.
t_lldb_opts() {
    printf '%s' "-O 'settings set target.disable-aslr false'"
    printf '%s' " -O 'settings set target.process.stop-on-exec false'"
}

t_home() { echo "/home/$WKDEV_CONTAINER_USER"; }

t_mirror_dir() { mirror_in_container; }

# `podman cp`: wkdev-enter is a shell wrapper, not a byte pipe -- a 1396-byte file arrived as 1399, and xz refused it.
t_pull() {
    local name="$1" src="$2" dest="$3"
    _hpodman cp "$(_ctr "$name"):$src" "$dest"
}

t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    [ $# -eq 0 ] || die "t_pull_dir: the container driver cannot exclude paths ($*).
    Copy the whole tree, or make the selection inside the workspace first."
    rm -rf "$dest"; mkdir -p "$dest"
    _hpodman cp "$(_ctr "$name"):$src/." "$dest"
}

t_push() {
    local name="$1" src="$2" dest="$3"
    _hpodman cp "$src" "$(_ctr "$name"):$dest"
}

t_push_dir() {
    local name="$1" src="$2" dest="$3" u
    u=$(_ctr_user "$name") \
        || die "workspace '$name' has no container to reach (podman does not know it)"
    _hpodman exec --user "$u" "$(_ctr "$name")" /bin/sh -c \
        "rm -rf $(sh_quote "$dest") && mkdir -p $(sh_quote "$dest")"
    _hpodman cp "$src/." "$(_ctr "$name"):$dest"
}

t_path_kind() {
    local name="$1" p="$2" u
    u=$(_ctr_user "$name") \
        || die "workspace '$name' has no container to reach (podman does not know it)"
    _hpodman exec --user "$u" "$(_ctr "$name")" /bin/sh -c \
        "if [ -d $(sh_quote "$p") ]; then echo dir
         elif [ -e $(sh_quote "$p") ]; then echo file
         else echo absent; fi" 2>/dev/null | tr -d '\r'
}

# podman's own detached exec: `setsid nohup` (lib/target.sh) does not survive here.
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    local c u; c=$(_ctr "$name"); u="$WKDEV_CONTAINER_USER"
    _hpodman exec -d --user "$u" "$c" \
        /opt/wk-tools/container/proxy/ensure-bridge.sh \
        /usr/bin/env "USER=$u" "HOME=/home/$u" bash --login -c \
        "echo \$\$ > $(sh_quote "$pidf"); exec $(sh_quote "$@") > $(sh_quote "$log") 2>&1 < /dev/null" \
        >/dev/null
}

t_enter() {
    local name="$1"
    local c; c=$(_ctr "$name")
    _hpodman exec -d "$c" /opt/wk-tools/container/proxy/ensure-bridge.sh true 2>/dev/null || true
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --name "$c"
}

# From macOS the rootless connection is named explicitly: the default there is rootful.
_hpodman() {
    if [ -n "${WK_IN_VM:-}" ] || ! is_macos; then
        podman "$@"
    else
        podman -c "${WK_MACHINE:-wk}" "$@"
    fi
}

WK_SDK_REPO="ghcr.io/igalia/wkdev-sdk"

t_sdk_local() {
    local img created
    img=$(_hpodman images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
          | grep "^$WK_SDK_REPO:" | head -1) || img=""
    [ -n "$img" ] || return 1
    created=$(_hpodman image inspect "$img" --format '{{.Created}}' 2>/dev/null | cut -c1-10)
    printf 'image=%s\ncreated=%s\n' "$img" "$created"
}

t_sdk_upstream() {
    _hpodman search --list-tags "$WK_SDK_REPO" --limit 100 2>/dev/null \
        | awk 'NR > 1 {print $2}'
}

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

# -i is inetd mode (no listener); one SetEnv carries every assignment because the keyword takes only its first; internal-sftp avoids the login shell's `cd`.
t_ssh_sshd_cmd() {
    local name="$1" u h
    u=$(_ctr_user "$name") || return 1
    h="/home/$u"
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

t_ssh_user()  { _ctr_user "$1"; }
t_ssh_proxy() { printf '%s %s' "$WK_ROOT/container/ssh-transport.sh" "$1"; }

t_ssh_exec() {
    local name="$1" cmd
    cmd=$(t_ssh_sshd_cmd "$name") \
        || die "workspace '$name' has no container to reach (podman does not know it)"
    _hpodman exec -i "$(_ctr "$name")" /bin/sh -c "$cmd"
}

t_ssh_prepare() {
    local name="$1" c u h pub waited=0 said=""
    c=$(_ctr "$name")

    u=$(_ctr_user "$name") \
        || die "no container workspace called '$name' on this machine.
    'wk ls' lists the ones there are, and 'wk start' brings the podman machine up
    if it is stopped. (An editor reaches a container over podman from here: the
    workspace has no network interface, so there is no other route in.)"
    h="/home/$u"

    # Asked of the container (on macOS the store is inside the podman VM), path spelled out because these execs run as root.
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
            || die "could not authorise the editor's key in '$name'"
        changed "authorised the editor's key in '$name'"
    fi

    ssh_alias_set "$name" "wk-$name.container.invalid" "$u" "$(zed_key)" \
        "ProxyCommand $(t_ssh_proxy "$name")"
}

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

    if _hpodman container exists "$c" 2>/dev/null; then
        _hpodman rm -f "$c" >/dev/null
        info "removed container $c"
    fi

    # `podman unshare` under keep-id: root's files inside are a subordinate uid.
    if [ -d "$ws" ]; then
        podman unshare rm -rf "$ws" 2>/dev/null || rm -rf "$ws"
        [ -d "$ws" ] && warn "could not fully remove $ws" || info "removed $ws"
    fi
}
