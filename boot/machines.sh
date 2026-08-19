# The fleet: which machines can be booted into an image, and how.
#
# One verb (`wk boot`) with one driver per machine, which is the shape
# targets/*.sh already uses -- because "netboot" is not one mechanism. The Pis
# take a firmware one-shot over SSH; moose takes UEFI HTTP boot or BMC virtual
# media; the MBP cannot be booted remotely at all and its driver is hands-on by
# nature (Apple Silicon's boot volume selection goes through a LocalPolicy in
# the machine's own secure storage -- there is nothing to hand an image to over
# the wire). See docs/HANDOFF-netboot.md, "the headline".
#
# A machine sets:
#
#   MACH_SSH      ssh destination in its *normal* role
#   MACH_DRIVER   boot/<driver>.sh
#   MACH_DEVICE   the block device the image is written to, on the machine
#   MACH_ROOT     the root device of its normal role -- never written to
#   MACH_PROFILE  its default image profile
#   MACH_NOTE     one line, for the listing

machine_list() {
    cat <<'EOF'
rpi5   Raspberry Pi 5, WiFi only. USB one-shot; the NVMe workstation is untouched.
EOF
}

machine_load() {
    MACH_NAME="$1"
    case "$1" in
    rpi5)
        MACH_SSH=rpi5
        # Not netboot, and this is not a compromise. Netboot is wired-only on
        # every Pi and this board has no cable, but the same one-shot semantics
        # exist for a local device -- and the board's `tryboot` is already
        # taken by flash-kernel's kernel staging, so USB is also the option
        # that does not fight the existing configuration.
        MACH_DRIVER=rpi5-usb
        MACH_DEVICE=/dev/sda
        MACH_ROOT=/dev/nvme0n1p2
        MACH_PROFILE=rpi5-perf
        # The radio's own address, and the reason the image is findable at all.
        # The image boots the same hardware, so it presents the same MAC and
        # the AP hands it the same lease -- which makes "where is the image"
        # answerable from the driving machine's neighbour table rather than
        # from anything stored. Declared hardware identity, like MACH_DEVICE.
        MACH_MAC=88:a2:9e:07:1c:92
        MACH_NOTE="Raspberry Pi 5, USB one-shot"
        ;;
    *)  return 1 ;;
    esac
}

load_driver() {
    local d="$WK_ROOT/boot/$1.sh"
    [ -f "$d" ] || die "no boot driver '$1'"
    # shellcheck disable=SC1090
    . "$d"
}

# ssh to a machine in its normal role. BatchMode so an unreachable machine
# fails immediately instead of prompting into a script, ConnectTimeout because
# every fleet probe is bounded (the reliability contract in
# docs/HANDOFF-workspace-state.md).
m_ssh() {
    ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" "$MACH_SSH" "$@"
}

m_reachable() { m_ssh true >/dev/null 2>&1; }

# --- reaching the image ------------------------------------------------------
#
# The normal-role destination is no use here: this board is reached over
# Tailscale SSH, and the image carries no tailscale. So the image is found the
# way anything on a LAN is found -- by its hardware address in the driving
# machine's neighbour table, which is evidence read at the moment it is needed
# and stored nowhere. mDNS is the fallback for a cold ARP cache, and
# WK_IMAGE_HOST overrides both for a network where neither works.
image_addr() {
    local a=''
    [ -n "${WK_IMAGE_HOST:-}" ] && { printf '%s' "$WK_IMAGE_HOST"; return 0; }
    [ -n "${MACH_MAC:-}" ] && a=$(ip neigh show 2>/dev/null \
        | awk -v m="$MACH_MAC" 'tolower($5) == tolower(m) && $1 ~ /^[0-9]+\./ { print $1; exit }')
    [ -n "$a" ] || a="$(image_hostname).local"
    printf '%s' "$a"
}

image_hostname() {
    image_profile_load "$MACH_PROFILE" >/dev/null 2>&1 && printf '%s' "$IMG_HOSTNAME" \
        || printf '%s' "$MACH_NAME"
}

# The image's host key is not the workstation's, and never will be: they are
# different installs on the same hardware, and on the same address. A shared
# known_hosts would report that as the man-in-the-middle warning it looks
# exactly like, so images get their own file.
i_ssh() {
    ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="$WK_STORE/known_hosts-images" \
        "$(id -un)@$(image_addr)" "$@"
}

# --- the arming record -------------------------------------------------------
#
# `wk boot` is a role transition, and no probe can derive the *intent* half in
# full: once armed, the firmware register and the running system look exactly
# as they did a moment before. So arming leaves a small record of intent --
# and exactly one copy of it, on the machine it describes, next to the boot
# mechanism it describes. A second copy on the driving workstation would be
# two records of one fact, which rule 1 forbids even while they still agree.
#
# Everything else about the transition is derived from evidence: whether the
# machine has booted since the arming, and what it says it is when it answers.
MACH_RECORD=/var/lib/wk/boot-armed

# Written by the machine, on the machine, and stamped with the machine's own
# boot id and clock. Only `armed_by` comes from here, because only this end
# knows it. The boot id is the load-bearing field: "has this arming been
# spent" is then a comparison of two values from one kernel, not of two
# machines' wall clocks.
record_write() {
    m_ssh "sudo mkdir -p $(dirname $MACH_RECORD) && sudo tee $MACH_RECORD >/dev/null <<EOF
image=$1
profile=$2
device=$3
order=$4
armed_by=$(hostname)
armed_at=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
armed_boot_id=\$(cat /proc/sys/kernel/random/boot_id)
EOF"
}

# Only the normal role can be asked: the record lives on that role's root, and
# the image does not mount it. Answering "no record" from inside the image
# would be a lie, so the caller gets nothing and decides on evidence instead.
record_read() {
    [ "${ROLE_CHANNEL:-normal}" = normal ] || return 0
    m_ssh "sudo cat $MACH_RECORD 2>/dev/null" || true
}
record_clear() { m_ssh "sudo rm -f $MACH_RECORD"; }
