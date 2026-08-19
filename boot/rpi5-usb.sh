# Boot driver: Raspberry Pi 5, one-shot boot from the attached USB device.
#
# The primitive is `set_reboot_order` through the firmware mailbox:
#
#   vcmailbox 0x0003808b 4 4 0xf64
#
# which passes a BOOT_ORDER to the bootloader through a reset-safe register.
# "As with tryboot, this is a one-time setting and is automatically cleared
# after use" (config_txt/boot.adoc). The order reads lowest-nibble-first:
# 4=USB, 6=NVMe, f=restart -- so USB first, the workstation's NVMe next, then
# loop. Three properties follow, and together they are the reason this is the
# mechanism rather than an EEPROM change:
#
#   * it is one-shot, so any later reboot is a normal NVMe boot and the board
#     is a workstation again without anyone having to remember to put it back;
#   * it fails back rather than hanging, because the NVMe is next in the same
#     order -- so a wedged image recovers by itself;
#   * undo is a command, not a trip to the device.
#
# Confirmed live on this board 2026-08-19, and the return path was confirmed
# both ways (a power cycle and a self-reboot each land back on the NVMe).

BOOT_ORDER_IMAGE=0xf64     # USB -> NVMe -> restart
BOOT_ORDER_NORMAL=0xf461   # the EEPROM's own order: SD -> NVMe -> USB -> restart

# Arming with an order is also how arming is *cancelled*: re-arming with the
# EEPROM's current order is a safe no-op that still proves the mailbox call
# works. That is how the mechanism was first tested without changing anything.
b_arm() {
    local order="$1" reply word2
    reply=$(m_ssh "sudo vcmailbox 0x0003808b 4 4 $order") \
        || die "the firmware mailbox call failed on $MACH_NAME"

    # The reply is the confirmation: 0x80000000 in the second word means the
    # request succeeded. Checked rather than assumed, because a mailbox call
    # that silently did nothing looks exactly like one that worked until the
    # machine fails to come back.
    word2=$(printf '%s\n' "$reply" | tr ' ' '\n' | sed -n '2p')
    [ "$word2" = "0x80000000" ] \
        || die "the firmware refused the boot order ($word2, wanted 0x80000000)
    reply: $reply"
    debug "mailbox reply: $reply"
}

# What the firmware itself can be asked. The one-shot register is write-only
# from userspace -- there is no get_reboot_order tag -- so the persistent
# BOOT_ORDER is the only firmware evidence there is, and it is still worth
# reading: it proves the fallback the whole design leans on is still in place.
b_evidence() {
    m_ssh "rpi-eeprom-config 2>/dev/null | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p'" || true
}

b_reboot() {
    # Detached, and after a delay: the reboot kills the ssh session, and an
    # ssh that is killed mid-command exits nonzero, which under `set -e` would
    # report a successful arming as a failure.
    r_ssh "sudo systemd-run --on-active=3 --unit=wk-boot-reboot /sbin/reboot" >/dev/null
}

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
