command -v reach_tailnet >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"
command -v disk_part >/dev/null 2>&1 || . "$WK_ROOT/boot/disk.sh"
command -v tools_push >/dev/null 2>&1 || . "$WK_ROOT/lib/tools.sh"

machine_names() { wk_fleet list --kind board --kind mac --kind guest; }

machine_list() {
    local n
    for n in $(machine_names); do
        machine_load "$n" 2>/dev/null || continue
        printf '%-8s%s\n' "$n" "$NODE_NOTE"
    done
}

machine_quiet_siblings() { # <this machine> <net> <bridge>; a subshell, machine_load overwriting NODE_* as it walks
    local me="$1" net="$2" bridge="$3"
    (
        local peers quiet=0 total=0 n name up
        peers=$(wk_tailscale_peers 2>/dev/null) || peers=""
        [ -n "$peers" ] || { printf '0 0'; exit 0; }
        for n in $(machine_names); do
            [ "$n" != "$me" ] || continue
            machine_load "$n" 2>/dev/null || continue
            [ "${NODE_NET:-}" = "$net" ] || continue
            [ "${NODE_BRIDGE:-}" = "$bridge" ] || continue
            total=$((total + 1))
            up=""
            for name in "${NODE_SSH:-}" "${NODE_BENCH_SSH:-}"; do
                [ -n "$name" ] || continue
                printf '%s\n' "$peers" \
                    | awk -F'\t' -v n="$name" '$1 == n && $3 == "up" { found = 1 } END { exit !found }' \
                    && up=1
            done
            [ -n "$up" ] || quiet=$((quiet + 1))
        done
        printf '%s %s' "$quiet" "$total"
    )
}

machine_declare() {
    local n
    for n in $(machine_names); do
        machine_load "$n" 2>/dev/null || continue
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$n" "${NODE_ROLE:-workstation}" "${NODE_OS:-any}" "${NODE_PROFILE:-}" "$NODE_NOTE"
    done
}

machine_load() {
    local out
    out=$(wk_fleet load "$1" --kind board --kind mac --kind guest) || return 1
    NODE_NAME="$1"
    NODE_SSH=""; NODE_DRIVER=""; NODE_DEVICE=""; NODE_ROOT=""; NODE_PROFILE=""
    NODE_NOTE=""; NODE_MAC=""; NODE_VOLUME=""; NODE_DTB=""
    NODE_BENCH_SSH=""; NODE_NET=""; NODE_DISPLAY=""
    NODE_BRIDGE=""  # declared, not discovered: readable when unreachable
    NODE_ROLE=workstation
    NODE_OS=any  # or an OS name for a machine that answers only for itself
    eval "$out"
    [ -n "$NODE_DRIVER" ] && [ -n "$NODE_NOTE" ] || return 1
}

fleet_tailnet() { # <machine> -- each of its tailnet names with its address, or `not a node`
    ( machine_load "$1" >/dev/null 2>&1 || exit 0
      local n a out=""
      for n in "${NODE_SSH:-$1}" "${NODE_BENCH_SSH:-}"; do
          [ -n "$n" ] || continue
          a=$(reach_tailnet "$n")
          out="${out:+$out; }$n ${a:-not a node}"
      done
      printf '%s' "$out" )
}

machine_by_ssh() {
    local want="$1" m
    machine_load "$want" 2>/dev/null && return 0
    for m in $(machine_list | awk '{print $1}'); do
        machine_load "$m" || continue
        [ "$NODE_SSH" = "$want" ] && return 0
    done
    return 1
}

load_driver() {
    local d="$WK_ROOT/boot/$1.sh"
    [ -f "$d" ] || die "no boot driver '$1'"
    # shellcheck disable=SC1090
    . "$d"
}

m_here() { # am I the machine NODE_SSH names? Case-insensitively: a machine's own spelling of its name need not be the conf's
    local me
    [ -n "${NODE_SSH:-}" ] || return 1
    me=$(hostname -s 2>/dev/null) || return 1
    [ "$(printf '%s' "$me" | tr '[:upper:]' '[:lower:]')" \
        = "$(printf '%s' "$NODE_SSH" | tr '[:upper:]' '[:lower:]')" ]
}

m_ssh() { # the one way to run a command on a machine, whichever machine this is
    if m_here; then bash -c "$*"; return $?; fi   # standing on it, and not a flag a conf declares: a machine drives itself only from its own keyboard, and from anywhere else that answers about the wrong computer
    reach_offline "$NODE_SSH" && { warn "$REACH_WHY"; return 255; }   # 255 is ssh's own "could not connect", which every caller here already reads
    # shellcheck disable=SC2086
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" \
        $(m_ssh_opts) "$NODE_SSH" "$@"
}
# -l root on both channels: the driving key is in root's authorized_keys and a written system's host key regenerates every time. Host mode asks NODE_ROLE; the bench system is a wk image whatever the role says, and reading the role there left every workstation-role board unreachable in bench mode (rpi5, 2026-09-04).
m_ssh_opts() {
    [ "${NODE_ROLE:-}" = bench-device ] || return 0
    printf '%s' "-l root $(_unpinned_host_key_opts)"
}
i_ssh_opts() {
    printf '%s' "-l root $(_unpinned_host_key_opts)"
}

m_reachable() { m_ssh true >/dev/null 2>&1; }

image_addr() {
    local a=''
    [ -n "${WK_IMAGE_HOST:-}" ] && { printf '%s' "$WK_IMAGE_HOST"; return 0; }

    a=$(reach_tailnet "${NODE_BENCH_SSH:-${NODE_SSH:-$NODE_NAME}}" 2>/dev/null | awk '{print $1}')
    [ -n "$a" ] && { printf '%s' "$a"; return 0; }

    [ -n "${NODE_MAC:-}" ] || { printf '%s' "${NODE_SSH:-$NODE_NAME}"; return 0; }
    a=$(reach_enumerate "$NODE_MAC" 2>/dev/null | awk '{print $1}')
    printf '%s' "${a:-${NODE_SSH:-$NODE_NAME}}"
}

i_ssh() {
    reach_offline "${NODE_BENCH_SSH:-${NODE_SSH:-$NODE_NAME}}" && { warn "$REACH_WHY"; return 255; }
    # shellcheck disable=SC2046
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" \
        $(i_ssh_opts) \
        "$(image_addr)" "$@"
}
r_is_root() {
    [ "${MODE_CHANNEL:-}" = bench ] && return 0
    [ "${NODE_ROLE:-}" = bench-device ]
}
# Privilege follows the channel, not NODE_ROLE: asking the role sent `wk boot --back` at a bench system looking for a helper only its host mode has (2026-09-04). `sudo -n`, never a bare `sudo` -- BatchMode ssh has no terminal to prompt on.
r_sudo() { # <command string>
    if r_is_root; then r_ssh "$@"; else r_ssh "sudo -n $*"; fi
}

# On a workstation the only password-free root is the named helper: `sudo -n vcmailbox` there answers "interactive authentication is required", and the arming died before the mailbox call (rpi5, 2026-09-03). The detach is `nohup`, which is POSIX and on both platforms; `setsid` is util-linux, so on a Mac the verb exits 0 having rebooted nothing.
BOOT_PRIV=/usr/local/libexec/wk-boot-priv
boot_priv() { # <verb> [order]
    if r_is_root; then
        case "$1" in
            order)          r_ssh "vcmailbox 0x0003808b 4 4 $(sh_quote "$2")" ;;
            reboot)         r_ssh "nohup sh -c 'sleep 3; reboot' </dev/null >/dev/null 2>&1 &" ;;
            reboot-tryboot) r_ssh "nohup sh -c 'sleep 3; printf \"0 tryboot\" > /run/systemd/reboot-param && systemctl reboot' </dev/null >/dev/null 2>&1 &" ;;
            status)         return 0 ;;
        esac
        return $?
    fi
    r_sudo "$BOOT_PRIV $(sh_quote "$@")"
}

boot_priv_require() {
    r_is_root && return 0
    boot_priv status >/dev/null 2>&1 && return 0
    die "$NODE_NAME cannot be armed: its boot helper is missing, or its sudoers
    rule is not in force. A workstation is driven as a person and wk takes no
    passwordless sudo on one beyond its named helpers, so there is deliberately
    no second way in.
    What fails:  sudo -n $BOOT_PRIV status
    The remedy, from a terminal on $NODE_NAME:  ./setup --stage quiesce"
}

# A bench-device's medium is often the disk the board runs from, which the card helper refuses by design, so it mounts and reads directly. A partition holding no system prints nothing and is not an error.
b_medium_read() { # <boot partition> <fixed file name>
    local part="$1" name="$2"
    if [ "${NODE_ROLE:-}" = bench-device ]; then
        # /proc/mounts and not findmnt: a BusyBox bench system carries neither findmnt nor sudo.
        r_sudo "at=\$(awk -v p='$part' '\$1 == p { print \$2; exit }' /proc/mounts)
            if [ -n \"\$at\" ]; then cat \"\$at/$name\" 2>/dev/null; exit 0; fi
            mkdir -p /mnt/wk-read
            mount -o ro '$part' /mnt/wk-read 2>/dev/null || exit 0
            cat /mnt/wk-read/$name 2>/dev/null
            umount /mnt/wk-read" 2>/dev/null || return 1
        return 0
    fi
    card_priv boot-read "$(disk_of_part "$part")" "$(disk_partno "$part")" "$name" && return 0
    b_part_absent "$part" && return 0   # B_SYSTEM_PARTS names the pairs a medium *may* carry, so a slot it does not have is an empty slot rather than a medium that cannot be read (rpi5, 2026-09-10: `wk boot` refused a good card over an absent /dev/sda3). Asked after the read, so the common path costs nothing
    warn "$NODE_NAME could not read $name off $part.
    Its card helper is older than this verb, or its sudoers rule is not in
    force; a workstation has no second way to reach the medium.
    What fails:  sudo -n $CARD_PRIV boot-read ...
    The remedy, from a terminal on $NODE_NAME:  ./setup --stage quiesce"
    return 1
}

mac_ssh() {
    local dest="$1"; shift
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" "$dest" "$@"
}

# One declared path, not a search: the same spelling a peer's machines/<name>.conf gives WK_REMOTE_TOOLS. A machine carrying two clones otherwise has whichever a search reaches first driving the lane.
MACHINE_TOOLS=Development/wk-tools
machine_tools_dir() { printf '%s' "$MACHINE_TOOLS"; }

machine_tools_present() { # <ssh destination>
    mac_ssh "$1" "test -x $(sh_quote "$MACHINE_TOOLS/wk")" >/dev/null 2>&1
}

machine_tools_path() { # <ssh destination> -- MACHINE_TOOLS resolved against that machine's own home, which tools_push needs absolute
    local home
    home=$(mac_ssh "$1" 'printf "%s" "$HOME"' 2>/dev/null | tr -d '\r') || return 1
    [ -n "$home" ] || return 1
    printf '%s/%s' "$home" "$MACHINE_TOOLS"
}

machine_prepare() { # <ssh destination>
    local dest="$1" path
    path=$(machine_tools_path "$dest") \
        || { warn "could not read \$HOME on $dest, so there is nowhere to put this tree"; return 1; }
    info "pushing this tree to $dest:$path"
    tools_push "$path" mac_ssh "$dest" || return 1

    # Nothing can bootstrap the first authenticated sudo from a session with no terminal.
    [ -t 0 ] || { warn "the tree is in place on $dest. Installing its privileged helpers
    puts a NOPASSWD rule in /etc/sudoers.d, and that sudo authenticates once; this
    session has no terminal to answer on. From one:
      wk boot $NODE_NAME --prepare
    or on $dest itself, where Touch ID answers it if that Mac has it enabled:
      cd $path && ./setup --stage quiesce"; return 1; }

    info "installing the privileged helpers on $dest (it asks for a password once)"
    ssh -t "$dest" "cd $(sh_quote "$path") && ./setup --stage quiesce" || {
        warn "./setup --stage quiesce did not finish on $dest"
        return 1
    }
    return 0
}

# The driver interface is lib/wk/boot; every b_* here and in boot/pi-*.sh is its shim over this shell's NODE_* and MODE.
_wk_boot() { # <driver> <verb> [args]
    ( export NODE_NAME ${!NODE_*} MODE MODE_CHANNEL ARM_SYS_PART
      PYTHONPATH="$WK_ROOT/lib" exec python3 -m wk.boot "$@" )
}

boot_facts() { # <driver> -- BOOT_ARMING and the other facts a bash caller reads after load_driver
    local f
    f=$(_wk_boot "$1" facts) || { echo "boot driver $1: python3 -m wk.boot $1 facts failed" >&2; return 1; }
    eval "$f"
}

boot_bridge() { # [--driver] <function> [args] -- one transport call of this library, for lib/wk/boot's BashChannel
    if [ "$1" = --driver ]; then shift; load_driver "$NODE_DRIVER"; fi
    "$@"
}

_b_probe_sh=$(cat "$WK_ROOT/boot/onboard/probe.sh")

b_probe() {
    local _o
    _o=$(_wk_boot "${NODE_DRIVER:-}" probe) || _o="MODE=unreachable MODE_CHANNEL=none"
    eval "$_o"
}
b_probeable() { _wk_boot "${NODE_DRIVER:-}" probeable; }
b_system_kind() { _wk_boot "${NODE_DRIVER:-}" system-kind "${1:-}"; }
b_display() { _wk_boot "${NODE_DRIVER:-}" display; }
b_media() { _wk_boot "${NODE_DRIVER:-}" media; }
b_booted_at() { _wk_boot "${NODE_DRIVER:-}" booted-at; }
b_boot_id() { _wk_boot "${NODE_DRIVER:-}" boot-id; }
b_watchdog_present() { _wk_boot "${NODE_DRIVER:-}" watchdog-present; }
b_systems() { _wk_boot "${NODE_DRIVER:-}" systems; }
machine_select_system() { _wk_boot "${NODE_DRIVER:-}" select "${1:-}" || exit $?; }
b_diag() { _wk_boot "${NODE_DRIVER:-}" diag || exit $?; }
b_arm() { _wk_boot "${NODE_DRIVER:-}" arm "$@" || exit $?; B_ARMED=1; }
b_reboot() { _wk_boot "${NODE_DRIVER:-}" reboot ${B_ARMED:+--armed} || exit $?; }
b_evidence() { _wk_boot "${NODE_DRIVER:-}" evidence; }
b_reprovision() { _wk_boot "${NODE_DRIVER:-}" reprovision; }
record_write() { _wk_boot "${NODE_DRIVER:-}" record-write "$@"; }
record_read() { _wk_boot "${NODE_DRIVER:-}" record-read; }
record_clear() { _wk_boot "${NODE_DRIVER:-}" record-clear; }
machine_armed_barrier() { b_probe; _wk_boot "${NODE_DRIVER:-}" barrier "$1" || exit $?; }   # the probe sets this shell's channel for what follows

r_ssh() {
    case "${MODE_CHANNEL:-none}" in
        host)  m_ssh "$@" ;;
        bench) i_ssh "$@" ;;
        *) return 1 ;;
    esac
}

