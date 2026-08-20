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
rpi4   Raspberry Pi 4 (2 GB), on the LAN. Netboots; armed from the server side.
rpi3   Raspberry Pi 3, on a direct cable. Netboots; needs real DHCP with option 43.
mbp    This Mac. Boots a benchmark volume; the selection is hands-on (Apple Silicon).
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
        MACH_ROLE=workstation
        MACH_NOTE="Raspberry Pi 5, USB one-shot"
        ;;
    rpi4)
        # Not `rpi4`: a hand-written `Host rpi4` in ~/.ssh/config used to point
        # at rpi4-compilers-0, a shared build box behind a ProxyJump, and a
        # `wk image write` aimed at this test device would have landed on that
        # machine's disk. host/dotfiles.sh now drops any hand-written stanza
        # naming a fleet machine, so the collision cannot come back -- and the
        # names stay apart on purpose, so that `Host rpi4` reads as the mistake
        # it is rather than as an alternative spelling of this one.
        MACH_SSH="${WK_RPI4_SSH:-rpi4-test}"
        MACH_DRIVER=pi-netboot
        MACH_DEVICE=/dev/sda
        MACH_ROOT=/dev/mmcblk0p2
        MACH_PROFILE=rpi4-perf
        # A dedicated test device, not a workstation: that is what licenses a
        # permanent network-first BOOT_ORDER, and cmd/pi reads it from here.
        MACH_ROLE=test-device
        MACH_NOTE="Raspberry Pi 4 (2 GB), netboot"
        ;;
    rpi3)
        MACH_SSH="${WK_RPI3_SSH:-rpi3}"
        MACH_DRIVER=pi-netboot
        MACH_DEVICE=/dev/sda
        MACH_ROOT=/dev/mmcblk0p2
        MACH_PROFILE=rpi3-perf
        MACH_ROLE=test-device
        MACH_NOTE="Raspberry Pi 3, netboot over a direct cable"
        ;;
    mbp)
        # The machine this is running on, and the only one in the fleet that
        # cannot be driven from anywhere else: there is one Apple Silicon
        # machine here, so its transition is arranged from inside the role it
        # is leaving. See boot/mac-volume.sh for what that costs.
        MACH_LOCAL=1
        MACH_SSH=localhost
        MACH_DRIVER=mac-volume
        # Not a block device: what is booted is an *installed volume*,
        # personalised for this Mac (an image copied onto a disk does not boot
        # -- the boot policy lives in this machine's own secure storage).
        # MACH_DEVICE is left empty so that anything reaching for it fails
        # loudly rather than writing to a disk on a machine that has no image
        # to write.
        MACH_DEVICE=""
        MACH_ROOT=""
        MACH_VOLUME="${WK_BENCH_VOLUME:-WK Bench}"
        MACH_PROFILE=mac-bench
        MACH_ROLE=workstation
        MACH_NOTE="this Mac, booting its benchmark volume (hands-on)"
        ;;
    *)  return 1 ;;
    esac
}

# The reverse lookup: an ssh destination -> the machine it reaches.
#
# `wk pi` takes an ssh host, because putting a device on the tailnet is a thing
# you do to a device rather than to a fleet entry, and it long predates the
# fleet. But its EEPROM half needs MACH_ROLE -- a test device wants network
# *first* in BOOT_ORDER and a workstation wants it last -- and looking the
# argument up with machine_load only works when the two names coincide. They do
# not for the rpi4, whose ssh name is rpi4-test on purpose, so the one board
# that must have network first was silently getting the workstation default.
#
# Machine names win over ssh names: `wk pi netboot-enable rpi4` should mean the
# fleet's rpi4 even if some host is called that.
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

# ssh to a machine in its normal role. BatchMode so an unreachable machine
# fails immediately instead of prompting into a script, ConnectTimeout because
# every fleet probe is bounded (the reliability contract in
# docs/HANDOFF-workspace-state.md).
#
# MACH_LOCAL is the case where the machine under test *is* the machine driving
# -- which is not an edge case but the normal shape for the MBP: it is the only
# Apple Silicon machine here, so nothing else can drive its transition, and the
# benchmark role is reached by rebooting this very shell out from under itself.
# Running the same commands locally keeps every driver written against one
# spelling; what changes is only who executes them.
m_ssh() {
    if [ -n "${MACH_LOCAL:-}" ]; then
        bash -c "$*"
    else
        ssh -o BatchMode=yes -o ConnectTimeout="${WK_SSH_TIMEOUT:-10}" "$MACH_SSH" "$@"
    fi
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
#
# It comes through `b_boot_id` rather than out of /proc directly, because a
# boot id is a per-platform question -- Linux has one to read and macOS has to
# derive one from kern.boottime -- and a record whose id is written by one rule
# and read by another compares two different things and always disagrees.
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

# Only the normal role can be asked: the record lives on that role's root, and
# the image does not mount it. Answering "no record" from inside the image
# would be a lie, so the caller gets nothing and decides on evidence instead.
record_read() {
    [ "${ROLE_CHANNEL:-normal}" = normal ] || return 0
    m_ssh "sudo cat $MACH_RECORD 2>/dev/null" || true
}
record_clear() { m_ssh "sudo rm -f $MACH_RECORD"; }

# --- shared driver parts -----------------------------------------------------
#
# Machine-independent, so they live here rather than in any one driver: only
# the *arming* mechanism differs between a Pi 5 (a firmware one-shot), a Pi 4
# (server-side, because set_reboot_order is Pi 5 only) and moose (BMC virtual
# media). Everything else -- which role answered, when it booted, rebooting it,
# writing its boot device, reading its offline dump -- is the same work.

# Which role is answering, and on which channel. Evidence, never the record:
# the image writes an identity marker and the workstation does not, and the two
# are reached completely differently -- the workstation over the tailnet, the
# image over the LAN, because the image carries no tailscale and never will.
#
# Sets ROLE and ROLE_CHANNEL, rather than printing the role: everything
# downstream has to talk to whatever actually answered, and a probe whose
# channel is discovered in a command substitution loses it on the way out --
# the subshell that computed it is gone by the time anyone acts on it.
b_probe() {
    local id
    if m_ssh true >/dev/null 2>&1; then
        ROLE_CHANNEL=normal
        id=$(m_ssh 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null' 2>/dev/null || true)
        ROLE="workstation"
        [ -n "$id" ] && ROLE="image $id"
        return 0
    fi
    if id=$(i_ssh 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null' 2>/dev/null) && [ -n "$id" ]; then
        ROLE_CHANNEL=image; ROLE="image $id"; return 0
    fi
    ROLE_CHANNEL=none; ROLE=unreachable
}

# ssh over whichever channel answered.
r_ssh() {
    case "${ROLE_CHANNEL:-none}" in
        normal) m_ssh "$@" ;;
        image)  i_ssh "$@" ;;
        *) return 1 ;;
    esac
}

# From /proc/stat's btime, which is epoch seconds and therefore has no timezone
# to get wrong. `uptime -s` is the obvious source and the wrong one: it prints
# *local* time, and `date -u -d "<local string>"` re-reads that string as UTC --
# -u applies to parsing as well as printing -- so a machine six hours off UTC
# reports a boot six hours in the past, silently.
#
# The remote program contains no `$`, deliberately. An awk one-liner sent
# through ssh has three shells to survive and its `$2` did not: the value that
# came back was wrong in a way that looked plausible, which is the worst kind.
# sed needs no field reference, so there is nothing left to mis-expand, and the
# formatting happens here where the quoting is visible.
b_booted_at() {
    local btime
    btime=$(r_ssh 'sed -n "s/^btime //p" /proc/stat' 2>/dev/null | tr -dc '0-9') || true
    [ -n "$btime" ] || return 0
    date -u -d "@$btime" +%Y-%m-%dT%H:%M:%SZ
}

# The kernel's own identifier for this boot. This is what makes "has the
# machine rebooted since it was armed" answerable without comparing two
# machines' clocks -- it is a fresh random value per boot, and nothing about it
# can drift, skew or be misparsed.
b_boot_id() { r_ssh 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true; }

# The image's own account of the boot, read off its boot partition from the
# other role. This is the channel that matters when the thing that failed is
# the network: the image writes the dump to its FAT partition 75 s in, and the
# workstation can read it afterwards without the image ever having been
# reachable. Three blind attempts became one answer the day this existed.
#
# Mounted read-only and unmounted again. Nothing on the image is modified, and
# nothing about the workstation's own filesystems is touched.
b_diag() {
    local part="${MACH_DEVICE}1"
    # An automounter usually has the partition already -- the workstation's
    # desktop session mounts a stick the moment it appears. Read it where it
    # is rather than failing, and only mount when nothing else has.
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
    # Detached, and after a delay: the reboot kills the ssh session, and an
    # ssh that is killed mid-command exits nonzero, which under `set -e` would
    # report a successful arming as a failure.
    r_ssh "sudo systemd-run --on-active=3 --unit=wk-boot-reboot /sbin/reboot" >/dev/null
}

# Which image is on the machine's boot device, by name.
#
# Read off the boot partition, which is FAT and therefore mountable anywhere,
# rather than by hashing the device against the image: a booted image writes to
# its own boot partition, so the bytes stop matching exactly when the image
# starts working. Reads an existing mount when the desktop's automounter
# already has the device, and mounts read-only only when nothing else does.
b_device_image() {
    local part="${MACH_DEVICE}1"
    m_ssh "at=\$(findmnt -rno TARGET '$part' | head -1)
        if [ -n \"\$at\" ]; then cat \"\$at/wk-image.id\" 2>/dev/null; exit 0; fi
        sudo mkdir -p /mnt/wk-id
        sudo mount -o ro '$part' /mnt/wk-id 2>/dev/null || exit 0
        cat /mnt/wk-id/wk-image.id 2>/dev/null
        sudo umount /mnt/wk-id" 2>/dev/null | tr -d '\r\n ' || true
}

# --- writing the image -------------------------------------------------------
#
# The write happens on the machine, not from here: the board has passwordless
# sudo and the workstation deliberately has no privileged component at all
# (docs/HANDOFF-linux.md, "no root, and no firewall"). So the image is streamed
# over ssh into a `dd` on the far side.
b_flash() {
    local img="$1" dev="$2" bytes tran remote_zstd sha_local sha_remote

    bytes=$(stat -c %s "$img")

    # Refusals, in the order that a mistake would be most expensive.
    #
    # The transport check is the load-bearing one: `removable` is 0 for a USB
    # SSD, so it cannot be the discriminator, while a device reached over USB
    # is by construction not this board's NVMe workstation disk.
    tran=$(m_ssh "lsblk -dno TRAN $dev 2>/dev/null" | tr -d ' ') \
        || die "$dev does not exist on $MACH_NAME"
    [ "$tran" = usb ] || die "refusing to write $dev on $MACH_NAME: transport is '${tran:-unknown}', not usb.
    This driver only writes the machine's attached USB device. The workstation
    install is on $MACH_ROOT and is never a target."

    case "$dev" in
        "$MACH_ROOT"*) die "refusing to write $dev: that is $MACH_NAME's own root device" ;;
    esac

    # Unmounted rather than refused. The caller has already agreed to erase
    # this device, and an automount or a leftover forensics mount is not a
    # reason to make the reproducible verb fall back to a hand-typed umount --
    # which is exactly the wiki recipe this whole step exists to delete. It is
    # still done here, after the confirmation, and never to anything that is
    # not the device being written.
    local mounted
    mounted=$(m_ssh "findmnt -rno SOURCE,TARGET | awk '\$1 ~ /^$(printf '%s' "$dev" | sed 's|/|\\/|g')/ { print \$2 }'" || true)
    if [ -n "$mounted" ]; then
        info "unmounting on $MACH_NAME: $(printf '%s' "$mounted" | tr '\n' ' ')"
        m_ssh "for m in $(printf '%s' "$mounted" | tr '\n' ' '); do sudo umount \"\$m\"; done" \
            || die "could not unmount everything on $dev"
    fi

    m_ssh "test \$(lsblk -bdno SIZE $dev) -ge $bytes" \
        || die "$dev is smaller than the image ($bytes bytes)"

    # zstd on both ends when both have it: this is a 4 GB write across the same
    # WiFi that everything else here shares, and an image is mostly zeroes and
    # text. Plain dd when either end lacks it, because a missing compressor is
    # not a reason to fail.
    remote_zstd=no
    m_ssh 'command -v zstd >/dev/null' && remote_zstd=yes

    info "writing $(basename "$img") to $dev on $MACH_NAME ($((bytes / 1024 / 1024)) MB, zstd=$remote_zstd)"
    if [ "$remote_zstd" = yes ] && have zstd; then
        zstd -3 -c "$img" | m_ssh "zstd -dc | sudo dd of=$dev bs=4M conv=fsync status=none"
    else
        m_ssh "sudo dd of=$dev bs=4M conv=fsync status=none" < "$img"
    fi

    # Read back exactly as many bytes as were written and compare hashes. The
    # alternative -- trusting dd's exit status -- cannot see a stick that
    # accepted the write and stored something else, which is the failure mode
    # that costs a boot attempt to discover.
    if [ -n "${WK_NO_VERIFY:-}" ]; then
        warn "skipping read-back verification (WK_NO_VERIFY)"
        return 0
    fi
    info "verifying the write by reading it back"
    sha_local=$(sha256sum "$img" | cut -d' ' -f1)
    sha_remote=$(m_ssh "sudo head -c $bytes $dev | sha256sum" | cut -d' ' -f1)
    [ "$sha_local" = "$sha_remote" ] || die "read-back mismatch on $dev
    wrote  $sha_local
    read   $sha_remote
    The device did not store what was sent. Try another port or another stick."
    info "read-back matches"
}
