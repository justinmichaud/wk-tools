# The fleet: which machines can be booted into an image, and how.
#
# One verb (`wk boot`) with one driver per machine: entering bench mode is
# not one mechanism (Pi 5 firmware one-shot over SSH, rpi4 arms its own
# stick, moose BMC virtual media, MBP not remotely bootable -- Apple
# Silicon's boot volume selection is a LocalPolicy in its own secure storage).
#
# Sourced here, guarded, since five callers load this file and not lib/reach.sh;
# boot/disk.sh likewise, for the drivers that read their medium through it.
command -v reach_tailnet >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"
command -v disk_part >/dev/null 2>&1 || . "$WK_ROOT/boot/disk.sh"

# The fields a machine sets are documented in README.md ("Add a new fleet
# device"). NODE_DTB is empty for a machine with no board firmware to ask.
# NODE_BENCH_SSH is the tailnet name of the system `wk sysimage write` puts on
# this machine's medium -- what the card is seeded to join as (`<name>-bench`)
# and the ssh destination the bench lanes reach it at -- so a workstation
# (rpi5, tolken) keeps its own name in both roles.

# vcgencmd is on every Pi image; rpi4 lacks rpi-eeprom-config, tried second.
EEPROM_CONFIG_CMD='vcgencmd bootloader_config 2>/dev/null || sudo rpi-eeprom-config 2>/dev/null || true'

# One conf per machine, the same shape as targets/hosts/*.conf. WK_MACHINES_DIR
# overrides this, read only here: tests/test_fleet_walk.py's way to drive
# this against a fake fleet.
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
# many of them are quiet right now. A board is unreachable for two very
# different reasons -- the board, or the thing that carries it -- and when
# every device on one network goes silent at once the second is overwhelmingly
# the likely one. `wk boot --status` already says this for a board behind a
# bridge; a board on wifi has exactly the same failure and needs the same
# line, or an access point going down reads as a dead board.
#
# Evidence from the tailnet's own view of its peers, never a record, and a
# subshell because machine_load overwrites the caller's NODE_* as it walks.
# Prints "<quiet> <total>"; a machine with no siblings prints "0 0" and the
# caller says nothing.
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
    # So a second load in one process cannot inherit the first machine's
    # answers (same rule as image_profile_load).
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

# The reverse lookup: an ssh destination -> the machine it reaches. The ssh
# name and machine name can differ (rpi4's ssh name is rpi4-test); machine
# names win.
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
# into a script; ConnectTimeout because every fleet probe is bounded.
# NODE_LOCAL (the MBP: reached by rebooting this shell out from under itself)
# runs locally so every driver shares one spelling.
m_ssh() {
    if [ -n "${NODE_LOCAL:-}" ]; then
        bash -c "$*"
        return $?
    fi
    # shellcheck disable=SC2086
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" \
        $(m_ssh_opts) "$NODE_SSH" "$@"
}

# -l root: the driving key is in root's authorized_keys. Keyed on NODE_ROLE:
# a bench-device's system is replaced on demand and boots with its own
# fresh host key each time (_unpinned_host_key_opts, shared with i_ssh).
m_ssh_opts() {
    [ "${NODE_ROLE:-}" = bench-device ] || return 0
    printf '%s' "-l root $(_unpinned_host_key_opts)"
}

m_reachable() { m_ssh true >/dev/null 2>&1; }

# --- reaching the bench system -----------------------------------------------
# By the tailnet name; falls back to a MAC sweep (`reach_enumerate`,
# lib/reach.sh) when it can't. No mDNS. WK_IMAGE_HOST overrides all.
image_addr() {
    local a=''
    [ -n "${WK_IMAGE_HOST:-}" ] && { printf '%s' "$WK_IMAGE_HOST"; return 0; }

    # NODE_BENCH_SSH is the name the card was seeded to join under
    # (_tailnet_name_for); empty on a machine whose bench system shares its name.
    a=$(reach_tailnet "${NODE_BENCH_SSH:-${NODE_SSH:-$NODE_NAME}}" 2>/dev/null | awk '{print $1}')
    [ -n "$a" ] && { printf '%s' "$a"; return 0; }

    [ -n "${NODE_MAC:-}" ] || { printf '%s' "${NODE_SSH:-$NODE_NAME}"; return 0; }
    a=$(reach_enumerate "$NODE_MAC" 2>/dev/null | awk '{print $1}')
    # A failed sweep still returns the name, to fail resolving it honestly.
    printf '%s' "${a:-${NODE_SSH:-$NODE_NAME}}"
}

# Different installs at the same address would trip a man-in-the-middle
# warning in a shared known_hosts (_unpinned_host_key_opts, shared with
# m_ssh_opts). The login is m_ssh's: root on a bench-device, whose written
# systems accept the driving key in root's authorized_keys and have no other
# user; this user on a workstation.
i_ssh() {
    # shellcheck disable=SC2046
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" \
        $(m_ssh_opts) $(_unpinned_host_key_opts) \
        "$(image_addr)" "$@"
}

# Privileged, on whichever channel answered: root already on a bench-device
# (m_ssh_opts), so no sudo there -- a BusyBox bench system has none -- and
# this user's sudo on a workstation.
# `sudo -n` and never a bare `sudo`: every channel here is a BatchMode ssh with
# no terminal, so a sudo that decides to prompt cannot be answered -- it hangs
# or fails with a password prompt nobody sees. Failing fast says which.
r_sudo() { # <command string>
    if [ "${NODE_ROLE:-}" = bench-device ]; then r_ssh "$@"; else r_ssh "sudo -n $*"; fi
}

# Read one of the fixed files a wk system leaves on a boot partition of this
# machine's medium. One implementation, because there are three readers --
# the system id (b_device_image), the boot dump (b_diag) and the firmware's
# pair selector (rpi5_check_autoboot) -- differing only in the filename.
#
# The privilege differs by role, and only by role, for the same reason r_sudo
# above it does:
#
#   bench-device  already root, and its medium is often the very disk the
#                 board runs from -- which the card helper refuses by design.
#                 So it mounts and reads directly.
#   workstation   r_sudo is `sudo -n`, and the only thing that runs there
#                 without a password is the card helper (CLAUDE.md). A plain
#                 `mount` answers "interactive authentication is required",
#                 which every reader above saw as an empty medium.
#
# Prints nothing when the file is absent: a partition holding no system is not
# an error. Returns non-zero only when the medium could not be read at all.
b_medium_read() { # <boot partition> <fixed file name>
    local part="$1" name="$2"
    if [ "${NODE_ROLE:-}" = bench-device ]; then
        # An automounter usually has the partition already; read it where it
        # is, and only mount when nothing else has. /proc/mounts and not
        # findmnt: a BusyBox bench system carries neither findmnt nor sudo.
        r_sudo "at=\$(awk -v p='$part' '\$1 == p { print \$2; exit }' /proc/mounts)
            if [ -n \"\$at\" ]; then cat \"\$at/$name\" 2>/dev/null; exit 0; fi
            mkdir -p /mnt/wk-read
            mount -o ro '$part' /mnt/wk-read 2>/dev/null || exit 0
            cat /mnt/wk-read/$name 2>/dev/null
            umount /mnt/wk-read" 2>/dev/null || return 1
        return 0
    fi
    card_priv boot-read "$(disk_of_part "$part")" "$(disk_partno "$part")" "$name" && return 0
    # Not fatal here: `wk status` reads a medium too and a reporting command
    # never dies. The remedy goes to stderr, which status discards and every
    # other caller shows.
    warn "$NODE_NAME could not read $name off $part.
    Its card helper is older than this verb, or its sudoers rule is not in
    force; a workstation has no second way to reach the medium.
    What fails:  sudo -n $CARD_PRIV boot-read ...
    The remedy, from a terminal on $NODE_NAME:  ./setup --stage quiesce"
    return 1
}

# By ssh alias -- host or bench mode, whichever `dest` names. No
# unpinned-host-key handling: stable macOS installs, not a regenerating board.
mac_ssh() {
    local dest="$1"; shift
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" "$dest" "$@"
}

# --- the arming record -------------------------------------------------------
# No probe can derive `wk boot`'s *intent* half: once armed, the firmware
# register and running system look unchanged. So arming leaves one record of
# intent on the machine it describes, not a second copy on the workstation.
NODE_RECORD=/var/lib/wk/boot-armed

# Stamped with its own boot id: "has this arming been spent" is then a
# comparison of two values from one kernel, not two machines' wall clocks.
record_write() {
    # The same guard record_read and record_clear carry: the record lives on the
    # *host* install's root, so a board armed from its bench system has nowhere
    # to put one. Not an error -- for a medium-armed board the arming itself is
    # on the medium and `wk boot --status` reads it there (b_evidence), which is
    # evidence rather than a record and is what decides the next boot anyway.
    [ "${MODE_CHANNEL:-host}" = host ] || { debug "$NODE_NAME answered as its bench system; the arming is on its medium and no record is written"; return 0; }
    m_ssh "sudo mkdir -p $(dirname $NODE_RECORD) && sudo tee $NODE_RECORD >/dev/null <<EOF
image=$1
profile=$2
device=$3
order=$4
armed_by=$(hostname)
armed_at=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
armed_boot_id=$(b_boot_id)
EOF"
}

# Only host mode: the record lives on the host install's root, which a bench
# system does not mount. Returns nothing rather than a lying "no record".
record_read() {
    [ "${MODE_CHANNEL:-host}" = host ] || return 0
    m_ssh "sudo cat $NODE_RECORD 2>/dev/null" || true
}
# The same guard record_read carries, and for the same reason: the record lives
# on the *host* install's root, so on a machine answering as its bench system
# there is nothing here to clear. Silence rather than failure -- a medium-armed
# board answers as its bench system precisely when its arming is what needs
# disarming (pi-tryboot), and the record's own spent-ness is computed from
# boot ids, so an uncleared one misleads nobody.
record_clear() {
    [ "${MODE_CHANNEL:-host}" = host ] || { debug "$NODE_NAME answered as its bench system; its arming record is on the host install and stays"; return 0; }
    m_ssh "sudo rm -f $NODE_RECORD"
}

# Refuse to mutate a machine between `wk boot` and the reboot it asked for:
# in that window the filesystem answering ssh is not the one about to run,
# so writing or deploying lands on the disk about to be left, and both look
# like they worked. Checked against evidence: a record whose boot id is not
# the current one has been *spent*, or this would refuse every command
# after every bench run.
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
# Only the *arming* mechanism differs per machine (Pi 5 firmware one-shot,
# Pi 4 server-side, moose BMC virtual media); everything else is shared here.

# Which mode is answering, and on which channel. Evidence, never the record:
# a bench system writes an identity marker the host install does not. Sets
# MODE and MODE_CHANNEL rather than printing, since a channel found in a
# command substitution is gone by the time anyone acts on it. Every `case`
# pattern below opens with `(`: bash 3.2, the bash macOS ships, cannot parse
# a `case` inside a `$( … )` without both-sided parens, and fails at *load*
# time -- `wk boot`, `wk pi` and `wk status` all die on a stock Mac without it.
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

# base | bench | unknown, from the device the running root is on -- not from
# /etc/wk-image alone, since every image carries that marker. NODE_ROOT is
# tested first: on a one-medium board (rpi3) both patterns match.
b_system_kind() { # <root device>
    local rd="${1:-}"
    [ -n "$rd" ] || { printf 'unknown'; return 0; }
    case "$rd" in "${NODE_ROOT:-@none@}"*) printf 'base'; return 0 ;; esac
    case "$rd" in "${NODE_DEVICE:-@none@}"*) printf 'bench'; return 0 ;; esac
    printf 'unknown'
}

# Evidence, never the record: which medium the running root is on.
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
    # The device wins wherever conclusive; the image's own word is the
    # fallback only.
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

# One line: the wk-managed media on this machine and what is on it now.
# Every driver overrides this; this default exists so one without one still answers.
b_media() {
    [ -n "${NODE_DEVICE:-}" ] || { printf 'no wk-managed media declared'; return 0; }
    printf 'media %s (this driver says nothing more about it)' "$NODE_DEVICE"
}

# ssh over whichever channel answered.
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

# A fresh random value per boot, so "has it rebooted" needs no clock
# comparison. Linux only; macOS derives one from kern.boottime instead.
b_boot_id() { r_ssh 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true; }

# The bench system's boot partition on NODE_DEVICE: the first one, unless
# the system shares its medium with the rescue (pi-sd overrides this with
# the second system's).
b_boot_part() { disk_part "$NODE_DEVICE" 1; }

# Which partitions of NODE_DEVICE can hold a system's boot files: the first,
# unless the driver says otherwise (pi-sd: the bench system is on 3-4 beside
# the rescue; pi-tryboot: 1-2 and, when written, a second system on 3-4).
B_SYSTEM_PARTS="1"

# Partition <n> of the bench medium, as the enumeration below addresses it.
# One hook so a driver that *resolves* its medium instead of declaring it
# (pi-mbr) overrides the device half once, not the enumeration.
b_system_part() { disk_part "$NODE_DEVICE" "$1"; }

# The systems the bench medium holds, one `<boot partition> <image id>` line
# each, read off every candidate partition. A partition that is absent or
# carries no wk-image.id is not a system and not an error; an unreachable
# machine is (nothing here can tell what the medium holds).
b_systems() {
    local p part id
    for p in $B_SYSTEM_PARTS; do
        part=$(b_system_part "$p")
        id=$(b_device_image "$part") || return 1
        [ -n "$id" ] && printf '%s %s\n' "$part" "$id"
    done
    return 0
}

# Which system an arming boots, from evidence: the one named, or the only
# one there is. Prints its `<boot partition> <image id>` line; every refusal
# names the remedy. One selector for every arming (cmd/boot), so a medium
# that grows a second system changes no command.
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
# system the medium holds is read -- after a failed boot of the second
# system, the first one's dump is the stale one.
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
# one-shot *is* that flag (pi-tryboot's staged prefix, rpi5's autoboot.txt
# boot_partition). One implementation: the flag rides the reboot syscall and
# systemd is the only userspace here that passes it, so the refusal when it
# cannot -- a BusyBox bench system has neither systemctl nor /run/systemd --
# belongs beside the call rather than in each driver: staged perfectly but
# never rebooted loses every leg, silently, without it.
#
# Detached and after a delay, like every reboot here: it kills the ssh session
# that asked for it.
b_reboot_tryboot() {
    r_ssh "command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd ]" >/dev/null 2>&1 \
        || die "$NODE_NAME answered on a system with no systemd, which cannot pass the
    tryboot flag to the reboot it rides on (only 'systemctl reboot' with
    /run/systemd/reboot-param does). Arm from a system that can:
        wk boot $NODE_NAME --back"
    r_sudo "setsid sh -c 'sleep 3; printf \"0 tryboot\" > /run/systemd/reboot-param && systemctl reboot' </dev/null >/dev/null 2>&1 &" >/dev/null
}

b_reboot() {
    # Detached, and after a delay: the reboot kills the ssh session, which
    # would otherwise exit nonzero and, under `set -e`, read as a failure.
    # setsid rather than systemd-run, since the system answering may be a
    # BusyBox one; both inits have setsid and reboot.
    r_sudo "setsid sh -c 'sleep 3; reboot' </dev/null >/dev/null 2>&1 &" >/dev/null
}

# Read off the FAT boot partition rather than by hashing the device: a
# booted image writes to its own boot partition, so the bytes stop matching.
# Fails only when the machine cannot be asked; a partition with no system on
# it (or none at all) answers with nothing, which is a different fact.
# Which system a boot partition holds, read off the medium.
#
# Over `r_sudo`, the channel the machine answered on, because this is a fact
# about the *medium* and every caller needs it while the board is in whatever
# state it is in: `wk boot --system <id>` resolves an id through here, and a
# board with an arming in force answers as its bench system -- which is exactly
# when the next system has to be named (an A/B leg switch). Addressed to the
# rescue alone, the enumeration failed with "could not read <device> to see what
# it holds" the moment it was needed most.
#
b_device_image() { # [boot partition; default: b_boot_part]
    local part out
    part=${1:-$(b_boot_part)}
    out=$(b_medium_read "$part" wk-image.id) || return 1
    printf '%s' "$out" | tr -d '\r\n '
}
