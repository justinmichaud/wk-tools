# The fleet: which machines can be booted into an image, and how.
#
# One verb (`wk boot`) with one driver per machine: entering bench mode is not
# one mechanism (Pi 5 firmware one-shot over SSH, rpi4 arms its own stick,
# moose BMC virtual media, MBP not remotely bootable -- Apple Silicon's boot
# volume selection is a LocalPolicy in its own secure storage). See
# docs/HANDOFF-boot.md.
# Sourced here since five callers (cmd/boot, cmd/sysimage, cmd/pi, cmd/bench,
# cmd/doctor) load this file and not lib/reach.sh. Guarded so loading both is
# order-safe.
command -v reach_tailnet >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

# A machine sets:
#
#   MACH_SSH      ssh destination in host mode
#   MACH_DRIVER   boot/<driver>.sh
#   MACH_DEVICE   the block device the image is written to, on the machine
#   MACH_ROOT     the root device of its host mode -- never written to
#   MACH_PROFILE  its default system profile
#   MACH_NOTE     one line, for the listing
#   MACH_DTB      the device tree its firmware boots (image_dtb_for,
#                 lib/image.sh); empty for a machine with no board firmware
#                 to ask -- only image_check_boot_files reads it, and only
#                 for a machine an image is actually built for
#   MACH_BENCH_SSH  the ssh destination for a *second*, same-address install
#                 sharing this machine's hardware (bench/mac-lane.sh's
#                 tolken/tolken-bench split); empty for a machine with only
#                 one install
#   MACH_NET      wifi|ethernet -- how this machine's rescue/bench images
#                 reach the network (_image_wants_wifi, boot/disk.sh), a
#                 hardware fact about the board rather than something to
#                 infer from its name

# vcgencmd is on every Pi image; rpi-eeprom-config, the fleet's own rpi4
# lacks, so it is tried second. See boot/rpi-eeprom.sh for the writing half.
EEPROM_CONFIG_CMD='vcgencmd bootloader_config 2>/dev/null || sudo rpi-eeprom-config 2>/dev/null || true'

# One conf per machine, the same shape as targets/hosts/*.conf; each conf
# opens with its device's from-nothing recipe.
machines_dir() { echo "$WK_ROOT/boot/machines"; }

machine_list() {
    local f n
    for f in "$(machines_dir)"/*.conf; do
        [ -f "$f" ] || continue
        n=$(basename "$f" .conf)
        machine_load "$n" 2>/dev/null || continue
        printf '%-8s%s\n' "$n" "$MACH_NOTE"
    done
}

# Fields, not prose, for anything that has to *decide* (container/broker/
# wk-broker.py refuses non-bench-device). Tab-separated: name/role/os/profile/note.
machine_declare() {
    local f n
    for f in "$(machines_dir)"/*.conf; do
        [ -f "$f" ] || continue
        n=$(basename "$f" .conf)
        machine_load "$n" 2>/dev/null || continue
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$n" "${MACH_ROLE:-workstation}" "${MACH_OS:-any}" "${MACH_PROFILE:-}" "$MACH_NOTE"
    done
}

machine_load() {
    local f
    f="$(machines_dir)/$1.conf"
    [ -f "$f" ] || return 1
    MACH_NAME="$1"
    # Reset every field, so a second load in one process cannot inherit the
    # first machine's answers (same rule as image_profile_load).
    MACH_SSH=""; MACH_DRIVER=""; MACH_DEVICE=""; MACH_ROOT=""; MACH_PROFILE=""
    MACH_NOTE=""; MACH_MAC=""; MACH_LOCAL=""; MACH_VOLUME=""; MACH_DTB=""
    MACH_BENCH_SSH=""; MACH_NET=""
    # Declared, not discovered: readable when the machine is unreachable.
    MACH_BRIDGE=""
    MACH_ROLE=workstation
    # `any`, or an OS name for a machine that answers only for itself (mbp)
    # or needs host-only tooling (tart).
    MACH_OS=any
    # shellcheck disable=SC1090
    . "$f"
    [ -n "$MACH_DRIVER" ] && [ -n "$MACH_NOTE" ] || return 1
}

# The reverse lookup: an ssh destination -> the machine it reaches. Needed
# because the ssh name and machine name can differ (rpi4's ssh name is
# rpi4-test); machine names win over ssh names.
machine_by_ssh() {
    local want="$1" m
    machine_load "$want" 2>/dev/null && return 0
    for m in $(machine_list | awk '{print $1}'); do
        machine_load "$m" || continue
        [ "$MACH_SSH" = "$want" ] && return 0
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
# MACH_LOCAL is the MBP's shape: bench mode is reached by rebooting this
# shell out from under itself, run locally so every driver shares one spelling.
m_ssh() {
    if [ -n "${MACH_LOCAL:-}" ]; then
        bash -c "$*"
        return $?
    fi
    # shellcheck disable=SC2086
    ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" \
        $(m_ssh_opts) "$MACH_SSH" "$@"
}

# In code, not an ssh config stanza: a node this repo owns is on the tailnet,
# so storing one there is a second copy of a fact.
#   -l root    the driving key is in root's authorized_keys.
#   host key   each image boots with its own fresh key
#              (_unpinned_host_key_opts, shared with i_ssh).
# Keyed on MACH_ROLE: a bench-device's system is replaced on demand; a
# workstation's key stays checked.
m_ssh_opts() {
    [ "${MACH_ROLE:-}" = bench-device ] || return 0
    printf '%s' "-l root $(_unpinned_host_key_opts)"
}

# _unpinned_host_key_opts is in lib/reach.sh, already in scope via the source at top.

m_reachable() { m_ssh true >/dev/null 2>&1; }

# --- reaching the bench system -----------------------------------------------
# By the tailnet name; falls back to a MAC sweep (`reach_enumerate`,
# lib/reach.sh) when it can't. No mDNS. WK_IMAGE_HOST overrides all.
image_addr() {
    local a=''
    [ -n "${WK_IMAGE_HOST:-}" ] && { printf '%s' "$WK_IMAGE_HOST"; return 0; }

    # MACH_SSH is the name the card was seeded to join under (_tailnet_name_for).
    a=$(reach_tailnet "${MACH_SSH:-$MACH_NAME}" 2>/dev/null | awk '{print $1}')
    [ -n "$a" ] && { printf '%s' "$a"; return 0; }

    [ -n "${MACH_MAC:-}" ] || { printf '%s' "${MACH_SSH:-$MACH_NAME}"; return 0; }
    a=$(reach_enumerate "$MACH_MAC" 2>/dev/null | awk '{print $1}')
    # A failed sweep still returns the name, to fail resolving it honestly.
    printf '%s' "${a:-${MACH_SSH:-$MACH_NAME}}"
}

image_hostname() {
    image_profile_load "$MACH_PROFILE" >/dev/null 2>&1 && printf '%s' "$IMG_HOSTNAME" \
        || printf '%s' "$MACH_NAME"
}

# Different installs at the same address would trip a man-in-the-middle
# warning in a shared known_hosts (_unpinned_host_key_opts, shared with m_ssh_opts).
i_ssh() {
    # shellcheck disable=SC2046
    ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" \
        $(_unpinned_host_key_opts) \
        "$(id -un)@$(image_addr)" "$@"
}

# By ssh alias -- host or bench mode, whichever `dest` names. Shared by
# bench/mac-lane.sh and bench/mac-ab.sh so both halves agree on the timeout.
# No unpinned-host-key handling: stable macOS installs, not a regenerating board.
mac_ssh() {
    local dest="$1"; shift
    ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" "$dest" "$@"
}

# --- the arming record -------------------------------------------------------
# `wk boot` is a mode transition, and no probe can derive the *intent* half:
# once armed, the firmware register and running system look unchanged. So
# arming leaves one record of intent on the machine it describes -- a second
# copy on the driving workstation would be two records of one fact (rule 1).
MACH_RECORD=/var/lib/wk/boot-armed

# Stamped with its own boot id: "has this arming been spent" is then a
# comparison of two values from one kernel, not two machines' wall clocks.
record_write() {
    m_ssh "sudo mkdir -p $(dirname $MACH_RECORD) && sudo tee $MACH_RECORD >/dev/null <<EOF
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
    m_ssh "sudo cat $MACH_RECORD 2>/dev/null" || true
}
record_clear() { m_ssh "sudo rm -f $MACH_RECORD"; }

# Refuse to mutate a machine between `wk boot` and the reboot it asked for.
# Load the machine first; this probes it. In that window the filesystem
# answering ssh is not the one about to run, so writing or deploying lands on
# the disk about to be left -- both look like they worked and fail silently.
# Evidence, not the record alone: a record whose boot id is not the current
# one has been *spent* (else this would refuse every command after every
# bench run).
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
    barrier "$MACH_NAME is armed for system '$img' and has not rebooted yet.
    $what
    Disarm it first:   wk boot $MACH_NAME --disarm
    Or see the state:  wk boot $MACH_NAME --status"
}

# --- shared driver parts -----------------------------------------------------
# Only the *arming* mechanism differs per machine (Pi 5 firmware one-shot,
# Pi 4 server-side, moose BMC virtual media); everything else is shared here.

# Which mode is answering, and on which channel. Evidence, never the record:
# a bench system writes an identity marker the host install does not. Sets
# MODE and MODE_CHANNEL rather than printing, since a channel found in a
# command substitution is gone by the time anyone acts on it.
#
# Every `case` pattern below opens with `(`: bash 3.2 -- the bash macOS ships
# -- cannot parse a `case` inside a `$( … )` without both-sided parens, or its
# parser reads the closing `)` as the end of the substitution and finds a
# `;;` it has no grammar for. The failure is at *load* time: `.
# boot/machines.sh` aborts, so `wk boot`, `wk pi` and `wk status` all die on
# a stock Mac.
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
# /etc/wk-image alone, since every image carries that marker. MACH_ROOT is
# tested first: on a one-medium board (rpi3) both patterns match.
b_system_kind() { # <root device>
    local rd="${1:-}"
    [ -n "$rd" ] || { printf 'unknown'; return 0; }
    case "$rd" in "${MACH_ROOT:-@none@}"*) printf 'base'; return 0 ;; esac
    case "$rd" in "${MACH_DEVICE:-@none@}"*) printf 'bench'; return 0 ;; esac
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

# Mac drivers override this: a MACH_LOCAL machine answers only for itself.
b_probeable() { :; }

# One line: the wk-managed media on this machine and what is on it now.
# Every driver overrides this; this default exists so one without one still answers.
b_media() {
    [ -n "${MACH_DEVICE:-}" ] || { printf 'no wk-managed media declared'; return 0; }
    printf 'media %s (this driver says nothing more about it)' "$MACH_DEVICE"
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

# Read off the boot partition from host mode: the image writes the dump
# there 75 s in, readable even if the image was never reachable.
b_diag() {
    local part="${MACH_DEVICE}1"
    # An automounter usually has the partition already; read it where it is,
    # and only mount when nothing else has.
    m_ssh "set -e
        at=\$(findmnt -rno TARGET '$part' | head -1)
        if [ -n \"\$at\" ]; then
            cat \"\$at/wk-diag.txt\" 2>/dev/null \
                || echo '(no wk-diag.txt -- the image did not get that far)'
            exit 0
        fi
        sudo mkdir -p /mnt/wk-diag
        sudo mount -o ro '$part' /mnt/wk-diag || { echo 'cannot mount $part' >&2; exit 1; }
        cat /mnt/wk-diag/wk-diag.txt 2>/dev/null \
            || echo '(no wk-diag.txt -- the image did not get that far)'
        sudo umount /mnt/wk-diag"
}

b_reboot() {
    # Detached, and after a delay: the reboot kills the ssh session, which
    # would otherwise exit nonzero and, under `set -e`, read as a failure.
    r_ssh "sudo systemd-run --on-active=3 --unit=wk-boot-reboot /sbin/reboot" >/dev/null
}

# Read off the FAT boot partition rather than by hashing the device: a
# booted image writes to its own boot partition, so the bytes stop matching.
b_device_image() {
    local part="${MACH_DEVICE}1"
    m_ssh "at=\$(findmnt -rno TARGET '$part' | head -1)
        if [ -n \"\$at\" ]; then cat \"\$at/wk-image.id\" 2>/dev/null; exit 0; fi
        sudo mkdir -p /mnt/wk-id
        sudo mount -o ro '$part' /mnt/wk-id 2>/dev/null || exit 0
        cat /mnt/wk-id/wk-image.id 2>/dev/null
        sudo umount /mnt/wk-id" 2>/dev/null | tr -d '\r\n ' || true
}
