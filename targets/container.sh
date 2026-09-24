# Target driver: podman container on the webkit-container-sdk -- rootless, --userns
# keep-id, --network none, so egress is only through the proxy's unix socket.

if [ -n "${WK_IN_VM:-}" ]; then
    WK_SDK="${WK_SDK:-/opt/webkit-container-sdk}"
else
    WK_SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"
fi

export WKDEV_SDK="$WK_SDK"

_sdk() { "$@"; }

export WKDEV_CONTAINER_UID="$(id -u)"
export WKDEV_CONTAINER_GID="$(id -g)"
export WKDEV_CONTAINER_USER="${WK_CONTAINER_USER:-$(id -un)}"

export WKDEV_CONTAINER_SHELL=/bin/bash

t_agent_sock() { echo /run/wk/ssh-agent.sock; }

_ctr() { echo "wk-$1"; }

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

_wrap_cmd() {
    printf '%s\n' /opt/wk-tools/container/proxy/ensure-bridge.sh
}

t_exec() {
    local name="$1"; shift
    local c tty=""; c=$(_ctr "$name")
    { [ -t 0 ] && [ -t 1 ]; } || tty=--no-tty   # no terminal here means none in there: git would page into it and wait for ever
    _sdk "$WK_SDK/scripts/host-only/wkdev-enter" --quiet --name "$c" $tty --exec -- $(_wrap_cmd) "$@"
}

t_home() { echo "/home/$WKDEV_CONTAINER_USER"; }

t_mirror_dir() { mirror_in_container; }

# podman's own detached exec: a job left behind by `podman exec` (the nohup and disown of lib/target.sh) dies with it.
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    local c u; c=$(_ctr "$name"); u="$WKDEV_CONTAINER_USER"
    _hpodman exec -d --user "$u" "$c" \
        /opt/wk-tools/container/proxy/ensure-bridge.sh \
        /usr/bin/env "USER=$u" "HOME=/home/$u" bash --login -c \
        "$(t_spawn_script "$log" "$pidf" "$@")" \
        >/dev/null
}

# From macOS the store and the containers are the podman machine's, so the machine answers for them in its own words.
t_far_side() {
    if [ -n "${WK_IN_VM:-}" ] || ! is_macos; then echo none
    elif [ "$(_machine_state "${WK_MACHINE:-wk}")" = running ]; then echo answering
    else echo stopped
    fi
}
t_has_wk() { [ "$(t_far_side)" = answering ]; }
t_wk()     { _in_machine "$(vm_wk_cmd "$@")"; }

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

# -i is inetd mode (no listener); internal-sftp avoids the login shell's `cd`. sshd builds a session's environment from scratch, so an editor's terminal gets what every other way in gets through the wrapper's own SetEnv line, in one option because the keyword takes only its first.
t_ssh_sshd_cmd() {
    local name="$1" u h
    u=$(_ctr_user "$name") || return 1
    h="/home/$u"
    printf '%s' "mkdir -p /run/sshd && exec /opt/wk-tools/container/proxy/ensure-bridge.sh \
/bin/sh -c 'exec /usr/sbin/sshd -i -e -f /dev/null \
-o HostKey=$h/.wk-ssh/ssh_host_ed25519_key \
-o AuthorizedKeysFile=.ssh/authorized_keys \
-o UsePAM=no -o PidFile=none -o PermitRootLogin=no -o AllowUsers=$u -o LogLevel=ERROR \
-o Subsystem=\"sftp internal-sftp\" \
-o SetEnv=\"\$WK_SSH_SETENV\"'"
}

t_ssh_exec() {
    local name="$1" cmd
    cmd=$(t_ssh_sshd_cmd "$name") \
        || die "workspace '$name' has no container to reach (podman does not know it)"
    _hpodman exec -i "$(_ctr "$name")" /bin/sh -c "$cmd"
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
    push_agent_pat_converge_machine
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
