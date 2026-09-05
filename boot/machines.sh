# The fleet: which machines can be booted into an image, and how.
#
# One verb (`wk boot`) with one driver per machine: entering bench mode is not
# one mechanism (Pi 5 firmware one-shot over SSH, rpi4 arms its own stick,
# moose BMC virtual media, MBP not remotely bootable -- Apple Silicon's boot
# volume selection is a LocalPolicy in its own secure storage).
command -v reach_tailnet >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"
command -v disk_part >/dev/null 2>&1 || . "$WK_ROOT/boot/disk.sh"

# The fields a machine sets are documented in README.md ("Add a new fleet
# device"). NODE_DTB is empty for a machine with no board firmware to ask;
# NODE_BENCH_SSH is the tailnet name the card was seeded to join as.

# vcgencmd is on every Pi image; rpi4 lacks rpi-eeprom-config, tried second.
EEPROM_CONFIG_CMD='vcgencmd bootloader_config 2>/dev/null || sudo rpi-eeprom-config 2>/dev/null || true'

# WK_MACHINES_DIR, read only here, drives this against a fake fleet
# (tests/test_fleet_walk.py).
machines_dir() { echo "${WK_MACHINES_DIR:-$WK_ROOT/boot/machines}"; }

machine_list() {
    local f n
    for f in "$(machines_dir)"/*.conf; do
        [ -f "$f" ] || continue
        n=$(basename "$f" .conf)
        machine_load "$n" 2>/dev/null || continue
        printf '%-8s%s\n' "$n" "$NODE_NOTE"
    done
}

# The other fleet devices that share this one's way onto the network, and how
# many are quiet: every device on one network going silent at once says the
# carrier is down, not the board. A subshell, because machine_load overwrites
# the caller's NODE_* as it walks. Prints "<quiet> <total>".
machine_quiet_siblings() { # <this machine> <net> <bridge>
    local me="$1" net="$2" bridge="$3"
    (
        local peers quiet=0 total=0 f n name up
        peers=$(wk_tailscale_peers 2>/dev/null) || peers=""
        [ -n "$peers" ] || { printf '0 0'; exit 0; }
        for f in "$(machines_dir)"/*.conf; do
            [ -f "$f" ] || continue
            n=$(basename "$f" .conf)
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

# Tab-separated fields, not prose, for anything that has to *decide*
# (container/broker/wk-broker.py refuses non-bench-device).
machine_declare() {
    local f n
    for f in "$(machines_dir)"/*.conf; do
        [ -f "$f" ] || continue
        n=$(basename "$f" .conf)
        machine_load "$n" 2>/dev/null || continue
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$n" "${NODE_ROLE:-workstation}" "${NODE_OS:-any}" "${NODE_PROFILE:-}" "$NODE_NOTE"
    done
}

machine_load() {
    local f
    f="$(machines_dir)/$1.conf"
    [ -f "$f" ] || return 1
    NODE_NAME="$1"
    # So a second load cannot inherit the first machine's answers.
    NODE_SSH=""; NODE_DRIVER=""; NODE_DEVICE=""; NODE_ROOT=""; NODE_PROFILE=""
    NODE_NOTE=""; NODE_MAC=""; NODE_LOCAL=""; NODE_VOLUME=""; NODE_DTB=""
    NODE_BENCH_SSH=""; NODE_NET=""
    NODE_BRIDGE=""  # declared, not discovered: readable when unreachable
    NODE_ROLE=workstation
    NODE_OS=any  # or an OS name for a machine that answers only for itself
    # shellcheck disable=SC1090
    . "$f"
    [ -n "$NODE_DRIVER" ] && [ -n "$NODE_NOTE" ] || return 1
}

# The ssh name and machine name can differ (rpi4's ssh name is rpi4-test);
# machine names win.
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

# BatchMode so an unreachable machine fails immediately instead of prompting
# into a script. NODE_LOCAL (the MBP) runs locally so every driver shares one
# spelling.
m_ssh() {
    if [ -n "${NODE_LOCAL:-}" ]; then
        bash -c "$*"
        return $?
    fi
    # shellcheck disable=SC2086
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" \
        $(m_ssh_opts) "$NODE_SSH" "$@"
}

# -l root: the driving key is in root's authorized_keys. A bench-device's
# system is replaced on demand and boots with its own fresh host key each time
# (_unpinned_host_key_opts).
#
# Host mode is what this answers for, so the role is the right question: a
# bench-device's host mode is its rescue, driven as root; a workstation's is a
# person. The bench system is a different channel and asks i_ssh_opts.
m_ssh_opts() {
    [ "${NODE_ROLE:-}" = bench-device ] || return 0
    printf '%s' "-l root $(_unpinned_host_key_opts)"
}

# The bench system, which is a wk image whatever the machine is in host mode:
# the driving key is in root's authorized_keys (disk_install_fleet) and it
# boots with a fresh host key every time it is written. So this asks nothing
# about NODE_ROLE. Reading the role here instead left every board whose host
# mode is a workstation unreachable in bench mode -- `wk boot --keep`,
# `wk pi deploy` and `wk pi bench` all sshed as the driving user and were
# refused (rpi5, 2026-09-04).
i_ssh_opts() {
    printf '%s' "-l root $(_unpinned_host_key_opts)"
}

m_reachable() { m_ssh true >/dev/null 2>&1; }

# --- reaching the bench system -----------------------------------------------
# By the tailnet name; falls back to a MAC sweep (`reach_enumerate`,
# lib/reach.sh). No mDNS. WK_IMAGE_HOST overrides all.
image_addr() {
    local a=''
    [ -n "${WK_IMAGE_HOST:-}" ] && { printf '%s' "$WK_IMAGE_HOST"; return 0; }

    a=$(reach_tailnet "${NODE_BENCH_SSH:-${NODE_SSH:-$NODE_NAME}}" 2>/dev/null | awk '{print $1}')
    [ -n "$a" ] && { printf '%s' "$a"; return 0; }

    [ -n "${NODE_MAC:-}" ] || { printf '%s' "${NODE_SSH:-$NODE_NAME}"; return 0; }
    a=$(reach_enumerate "$NODE_MAC" 2>/dev/null | awk '{print $1}')
    printf '%s' "${a:-${NODE_SSH:-$NODE_NAME}}"
}

# Different installs at the same address would trip a man-in-the-middle warning
# in a shared known_hosts (_unpinned_host_key_opts).
i_ssh() {
    # shellcheck disable=SC2046
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" \
        $(i_ssh_opts) \
        "$(image_addr)" "$@"
}

# Whether the channel that answered is one wk is already root on. Two ways to
# be: a *bench system* is a wk image driven as root whatever the machine is in
# host mode (i_ssh_opts), and a bench-device's host mode is its rescue, also
# root. Only a workstation in host mode is driven as a person.
#
# The channel and not NODE_ROLE, which describes host mode alone: asking the
# role sent `wk boot --back` at a workstation-role board's bench system looking
# for a helper only its host mode has, and it answered "wk-boot-priv: command
# not found" with the board stuck in bench mode (rpi5, 2026-09-04).
r_is_root() {
    [ "${MODE_CHANNEL:-}" = bench ] && return 0
    [ "${NODE_ROLE:-}" = bench-device ]
}

# Privileged, on whichever channel answered: already root where r_is_root says
# so, which a BusyBox system has no sudo for anyway; this user's sudo on a
# workstation. `sudo -n` and never a bare `sudo`: every channel here is a
# BatchMode ssh with no terminal, so a sudo that prompts cannot be answered.
r_sudo() { # <command string>
    if r_is_root; then r_ssh "$@"; else r_ssh "sudo -n $*"; fi
}

# The privileged half of arming a board that boots itself: one firmware
# mailbox call and the reboot that spends it. A bench-device is driven as root
# and does them directly; a workstation is driven as a person over a BatchMode
# ssh with no terminal, where the only password-free root is a named helper
# (CLAUDE.md) -- `sudo -n vcmailbox` there answers "interactive authentication
# is required" and the arming died before the mailbox call (rpi5, 2026-09-03).
#
# One spelling for both, so a driver names the operation and never the
# privilege. Fixed verbs, and the only argument any of them takes is a boot
# order the helper checks for itself.
BOOT_PRIV=/usr/local/libexec/wk-boot-priv
boot_priv() { # <verb> [order]
    if r_is_root; then
        case "$1" in
            order)          r_ssh "vcmailbox 0x0003808b 4 4 $(sh_quote "$2")" ;;
            reboot)         r_ssh "setsid sh -c 'sleep 3; reboot' </dev/null >/dev/null 2>&1 &" ;;
            reboot-tryboot) r_ssh "setsid sh -c 'sleep 3; printf \"0 tryboot\" > /run/systemd/reboot-param && systemctl reboot' </dev/null >/dev/null 2>&1 &" ;;
            status)         return 0 ;;
        esac
        return $?
    fi
    r_sudo "$BOOT_PRIV $(sh_quote "$@")"
}

# Asked before the first refusal that would otherwise blame the firmware for a
# missing helper, the same shape card_priv_require has (boot/disk.sh).
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

# Read one of the fixed files a wk system leaves on a boot partition of this
# machine's medium; three readers differ only in the filename.
#
# The privilege differs by role: a bench-device is already root and its medium
# is often the disk the board runs from, which the card helper refuses by
# design, so it mounts and reads directly. On a workstation only the card
# helper runs without a password, and a plain `mount` answers "interactive
# authentication is required", which reads as an empty medium.
#
# A partition holding no system prints nothing and is not an error; non-zero
# means the medium could not be read at all.
b_medium_read() { # <boot partition> <fixed file name>
    local part="$1" name="$2"
    if [ "${NODE_ROLE:-}" = bench-device ]; then
        # Read the partition where an automounter already has it, and mount
        # only when nothing has. /proc/mounts and not findmnt: a BusyBox bench
        # system carries neither findmnt nor sudo.
        r_sudo "at=\$(awk -v p='$part' '\$1 == p { print \$2; exit }' /proc/mounts)
            if [ -n \"\$at\" ]; then cat \"\$at/$name\" 2>/dev/null; exit 0; fi
            mkdir -p /mnt/wk-read
            mount -o ro '$part' /mnt/wk-read 2>/dev/null || exit 0
            cat /mnt/wk-read/$name 2>/dev/null
            umount /mnt/wk-read" 2>/dev/null || return 1
        return 0
    fi
    card_priv boot-read "$(disk_of_part "$part")" "$(disk_partno "$part")" "$name" && return 0
    # Not fatal: `wk status` reads a medium too and never dies.
    warn "$NODE_NAME could not read $name off $part.
    Its card helper is older than this verb, or its sudoers rule is not in
    force; a workstation has no second way to reach the medium.
    What fails:  sudo -n $CARD_PRIV boot-read ...
    The remedy, from a terminal on $NODE_NAME:  ./setup --stage quiesce"
    return 1
}

# No unpinned-host-key handling: stable macOS installs, not a regenerating
# board.
mac_ssh() {
    local dest="$1"; shift
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" "$dest" "$@"
}

# --- the arming record -------------------------------------------------------
# No probe can derive `wk boot`'s intent half: once armed, the firmware
# register and running system look unchanged. So arming leaves one record of
# intent on the machine it describes.
NODE_RECORD=/var/lib/wk/boot/armed

# Stamped with its own boot id, so "has this arming been spent" compares two
# values from one kernel rather than two machines' wall clocks.
record_write() {
    # The record lives on the host install's root, so a board armed from its
    # bench system has nowhere to put one. Not an error: a medium-armed board's
    # arming is on the medium, where b_evidence reads it.
    [ "${MODE_CHANNEL:-host}" = host ] || { debug "$NODE_NAME answered as its bench system; the arming is on its medium and no record is written"; return 0; }
    m_ssh "mkdir -p $(dirname $NODE_RECORD) && cat > $NODE_RECORD <<EOF
image=$1
profile=$2
device=$3
order=$4
armed_by=$(hostname)
armed_at=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
armed_boot_id=$(b_boot_id)
EOF"
}

# Only host mode: a bench system does not mount the host install's root.
record_read() {
    [ "${MODE_CHANNEL:-host}" = host ] || return 0
    m_ssh "cat $NODE_RECORD 2>/dev/null" || true
}
# Nothing to clear on a machine answering as its bench system, and silence
# rather than failure: a medium-armed board answers that way precisely when its
# arming needs disarming, and spent-ness is computed from boot ids anyway.
record_clear() {
    [ "${MODE_CHANNEL:-host}" = host ] || { debug "$NODE_NAME answered as its bench system; its arming record is on the host install and stays"; return 0; }
    m_ssh "rm -f $NODE_RECORD"
}

# Refuse to mutate a machine between `wk boot` and the reboot it asked for: in
# that window the filesystem answering ssh is not the one about to run. Checked
# against evidence -- a record whose boot id is not the current one has been
# spent -- or this would refuse every command after every bench run.
machine_armed_barrier() { # <what this command would do>
    local what="$1" rec img armed_boot now_boot
    b_probe >/dev/null 2>&1 || true
    [ "${MODE:-}" = host ] || return 0
    rec=$(record_read 2>/dev/null || true)
    img=$(kv_get image <<<"$rec")
    [ -n "$img" ] || return 0
    armed_boot=$(kv_get armed_boot_id <<<"$rec")
    now_boot=$(b_boot_id 2>/dev/null || true)
    if [ -n "$armed_boot" ] && [ -n "$now_boot" ] && [ "$armed_boot" != "$now_boot" ]; then
        return 0    # spent: the machine has rebooted since it was armed
    fi
    barrier "$NODE_NAME is armed for system '$img' and has not rebooted yet.
    $what
    Disarm it first:   wk boot $NODE_NAME --disarm
    Or see the state:  wk boot $NODE_NAME --status"
}

# --- shared driver parts -----------------------------------------------------
# Only the arming mechanism differs per machine; everything else is shared here.

# Which mode is answering, and on which channel. Sets MODE and MODE_CHANNEL
# rather than printing, since a channel found in a command substitution is gone
# by the time anyone acts on it. Every `case` pattern below opens with `(`:
# bash 3.2, the bash macOS ships, cannot parse a `case` inside a `$( … )`
# without both-sided parens and fails at *load* time.
_b_probe_sh=$(cat <<'EOS'
cat /etc/wk-image 2>/dev/null
rd=$(findmnt -no SOURCE / 2>/dev/null || true)
[ -n "$rd" ] || rd=$(awk '$2 == "/" { print $1; exit }' /proc/mounts 2>/dev/null)
case "$rd" in
  (/dev/root|"") rd=$(sed -n 's/.*[ ]root=\([^ ]*\).*/\1/p' /proc/cmdline 2>/dev/null | head -1) ;;
esac
case "$rd" in
  (PARTUUID=*) rd=$(readlink -f "/dev/disk/by-partuuid/${rd#PARTUUID=}" 2>/dev/null || echo "$rd") ;;
  (UUID=*)     rd=$(readlink -f "/dev/disk/by-uuid/${rd#UUID=}" 2>/dev/null || echo "$rd") ;;
esac
printf 'rootdev=%s\n' "$rd"
EOS
)

# From the device the running root is on, not /etc/wk-image alone, since every
# image carries that marker. NODE_ROOT is tested first: on a one-medium board
# (rpi3) both patterns match.
b_system_kind() { # <root device>
    local rd="${1:-}"
    [ -n "$rd" ] || { printf 'unknown'; return 0; }
    case "$rd" in "${NODE_ROOT:-@none@}"*) printf 'base'; return 0 ;; esac
    case "$rd" in "${NODE_DEVICE:-@none@}"*) printf 'bench'; return 0 ;; esac
    printf 'unknown'
}

b_probe() {
    local out id role rootdev kind
    if m_ssh true >/dev/null 2>&1; then
        MODE_CHANNEL=host
        out=$(m_ssh "$_b_probe_sh" 2>/dev/null || true)
    elif out=$(i_ssh "$_b_probe_sh" 2>/dev/null) && printf '%s' "$out" | grep -q '^id='; then
        MODE_CHANNEL=bench
    else
        MODE_CHANNEL=none; MODE=unreachable; return 0
    fi

    id=$(     kv_get id      <<<"$out")
    role=$(   kv_get role    <<<"$out")
    rootdev=$(kv_get rootdev <<<"$out")

    if [ -z "$id" ]; then MODE="host"; return 0; fi

    kind=$(b_system_kind "$rootdev")
    case "$kind" in
        base)  MODE="base $id" ;;
        bench) MODE="bench $id" ;;
        *)     case "$role" in
                   rescue) MODE="base $id" ;;
                   *)      MODE="bench $id" ;;
               esac ;;
    esac
    return 0
}

# Mac drivers override this: a NODE_LOCAL machine answers only for itself.
b_probeable() { :; }

b_media() {
    [ -n "${NODE_DEVICE:-}" ] || { printf 'no wk-managed media declared'; return 0; }
    printf 'media %s (this driver says nothing more about it)' "$NODE_DEVICE"
}

r_ssh() {
    case "${MODE_CHANNEL:-none}" in
        host)  m_ssh "$@" ;;
        bench) i_ssh "$@" ;;
        *) return 1 ;;
    esac
}

# From /proc/stat's btime, epoch seconds with no timezone to get wrong;
# `uptime -s` prints *local* time and re-reading it as UTC misreports silently.
b_booted_at() {
    local btime
    btime=$(r_ssh 'sed -n "s/^btime //p" /proc/stat' 2>/dev/null | tr -dc '0-9') || true
    [ -n "$btime" ] || return 0
    # BSD date has no `-d`.
    epoch_to_utc "$btime"
}

# A fresh random value per boot. Linux only; macOS derives one from
# kern.boottime.
b_boot_id() { r_ssh 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true; }

# Unless the bench system shares its medium with the rescue (pi-sd overrides).
b_boot_part() { disk_part "$NODE_DEVICE" 1; }

# Drivers with more than one system override this.
B_SYSTEM_PARTS="1"

# One hook so a driver that resolves its medium instead of declaring it
# (pi-mbr) overrides the device half once, not the enumeration.
b_system_part() { disk_part "$NODE_DEVICE" "$1"; }

# The systems the bench medium holds, one `<boot partition> <image id>` line
# each. A partition that is absent or carries no wk-image.id is not a system
# and not an error; an unreachable machine is.
b_systems() {
    local p part id
    for p in $B_SYSTEM_PARTS; do
        part=$(b_system_part "$p")
        id=$(b_device_image "$part") || return 1
        [ -n "$id" ] && printf '%s %s\n' "$part" "$id"
    done
    return 0
}

# The system named, or the only one there is; prints its
# `<boot partition> <image id>` line.
machine_select_system() { # <requested id, or empty for the sole system>
    local want="$1" systems count
    systems=$(b_systems) \
        || die "could not read $NODE_DEVICE on $NODE_NAME to see what it holds"
    count=$(printf '%s' "$systems" | grep -c . || true)
    if [ "$count" -eq 0 ]; then
        die "$NODE_DEVICE on $NODE_NAME holds no wk system yet.
    Write one first:  wk sysimage write --from <path> --disk $NODE_NAME:$NODE_DEVICE
    ('wk sysimage ls' lists what a workspace here has built)"
    fi
    if [ -z "$want" ]; then
        [ "$count" -eq 1 ] || die "$NODE_DEVICE on $NODE_NAME holds $count systems:
$(printf '%s\n' "$systems" | awk '{ printf "        %s  (on %s)\n", $2, $1 }')
    Name the one to boot:  wk boot $NODE_NAME --system <id>"
        printf '%s\n' "$systems"
        return 0
    fi
    local line
    line=$(printf '%s\n' "$systems" | awk -v id="$want" '$2 == id { print; exit }')
    [ -n "$line" ] || die "$NODE_DEVICE on $NODE_NAME holds:
$(printf '%s\n' "$systems" | awk '{ printf "        %s  (on %s)\n", $2, $1 }')
    not '$want'. Write it first:  wk sysimage write --from <path> --disk $NODE_NAME:$NODE_DEVICE
    (a medium already holding a system takes a second one at ...:$NODE_DEVICE@second)"
    printf '%s\n' "$line"
}

# Read off each system's boot partition from host mode: the image writes the
# dump there 75 s in, readable even if the image was never reachable. Every
# system is read -- after a failed boot of the second, the first one's dump is
# the stale one.
b_diag() {
    local systems line part id
    systems=$(b_systems) || die "cannot read $NODE_DEVICE on $NODE_NAME"
    [ -n "$systems" ] || { echo "($NODE_DEVICE holds no wk system, so there is no dump to read)"; return 0; }
    local dump
    while read -r part id; do
        [ -n "$part" ] || continue
        printf '== %s (%s) ==\n' "$id" "$part"
        if ! dump=$(b_medium_read "$part" wk-diag.txt); then
            echo "(cannot read $part)"
        elif [ -z "$dump" ]; then
            echo "(no wk-diag.txt -- the image did not get that far)"
        else
            printf '%s\n' "$dump"
        fi
    done <<EOF
$systems
EOF
}

# The reboot that carries the firmware's tryboot flag, for the drivers whose
# one-shot is that flag. The flag rides the reboot syscall and systemd is the
# only userspace here that passes it, so the refusal when it cannot -- a
# BusyBox bench system has neither systemctl nor /run/systemd -- lives beside
# the call rather than in each driver.
b_reboot_tryboot() {
    r_ssh "command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd ]" >/dev/null 2>&1 \
        || die "$NODE_NAME answered on a system with no systemd, which cannot pass the
    tryboot flag to the reboot it rides on (only 'systemctl reboot' with
    /run/systemd/reboot-param does). Arm from a system that can:
        wk boot $NODE_NAME --back"
    boot_priv reboot-tryboot >/dev/null
}

b_reboot() {
    # Through boot_priv, like every reboot here: a bench-device is already root
    # and reboots directly, a workstation has no password-free root but the
    # helper. Detached and after a delay either way -- the reboot kills the ssh
    # session, which would otherwise exit nonzero and, under `set -e`, read as
    # a failure -- and setsid rather than systemd-run, since the system
    # answering may be a BusyBox one.
    boot_priv reboot >/dev/null
}

# Which system a boot partition holds, read off the FAT boot partition rather
# than by hashing the device: a booted image writes to its own boot partition,
# so the bytes stop matching. A partition with no system answers with nothing.
#
# Over `r_sudo`, the channel the machine answered on: a board with an arming in
# force answers as its bench system, which is exactly when the next system has
# to be named for an A/B leg switch.
#
b_device_image() { # [boot partition; default: b_boot_part]
    local part out
    part=${1:-$(b_boot_part)}
    out=$(b_medium_read "$part" wk-image.id) || return 1
    printf '%s' "$out" | tr -d '\r\n '
}
