# Boot driver: a Pi that boots an image from its USB stick, with the SD card
# behind it as the rescue role.
#
# This exists because the rpi4 has neither of the other two mechanisms and
# needs the property both provide.
#
# `boot/rpi5-usb.sh` arms through the firmware mailbox (`set_reboot_order`),
# a genuine one-shot, Raspberry Pi 5 only. Here arming moves to the boot
# medium instead, since a board offered a stick with nothing bootable on it
# falls through to the next entry; firmware that gets as far as start4.elf
# and no further *halts* instead, with nothing over the wire to recover it.
#
#   BOOT_ORDER is USB -> SD -> restart (`wk pi boot-order <host> usb-first`),
#   permanently, and arming is whether the stick presents a *bootable
#   partition* for the firmware to find -- one byte of the MBR.
#
# Not the second-stage file: renaming `start4.elf` aside still halts the
# firmware when a valid FAT partition is present but incomplete, rather than
# skipping. Disarming instead flips partition 1's MBR type byte from 0x0c
# (FAT32 LBA) to 0x83 (Linux), so the firmware finds no boot filesystem at
# all and moves to the SD card -- nothing erased, the 4.6 GB on the stick
# untouched.
#
# Written with dd at a fixed offset rather than through sfdisk: the
# self-disarm runs *from* that disk with partition 2 mounted as root, and a
# tool that rewrites the table asks the kernel to re-read it, while a
# surgical write to offset 450 asks nothing of anybody.
#
# Still a one-shot: the image disarms *itself* on first boot, renaming its
# own start4.elf aside before the benchmark starts, so any later reboot
# falls through to the SD card -- the same "reverts by itself unless
# claimed" property the rpi5 gets from firmware, reached without the
# primitive the Pi 4 does not have. A run that wedges the board costs a
# reboot, not a journey.
#
# The failure modes, all of which end somewhere reachable:
#
#   stick not armed          -> no FAT on the device, firmware skips it, SD boots
#   image kernel missing     -> refused before writing (image_check_boot_files)
#   image cannot find root   -> panic=10, reboot, and it disarmed itself, so SD
#   image hangs after boot   -> watchdog reboots it, and again SD

# A third arming model. The other two put the intent in firmware (one-shot) or
# in the server's content (server); this puts it on the medium the machine
# boots from, which is the only one of the three that is both readable and
# writable from the other role.
BOOT_ARMING=medium

# Not used by this driver -- the boot order is permanent here, not per-boot --
# but named so that a reader comparing the drivers sees the difference rather
# than an omission. `wk pi boot-order <host> usb-first` writes the real one.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# The MBR byte that decides whether this device has a boot filesystem on it.
#
# An MBR partition table starts at offset 446; each entry is 16 bytes and its
# type byte is the fifth. So partition 1's type lives at 446 + 4 = 450, and
# nothing else in the sector moves when it changes.
PIUSB_PART_SUFFIX=1
PIUSB_TYPE_OFFSET=450
PIUSB_TYPE_ARMED=0c      # FAT32 LBA -- what `wk sysimage write` leaves behind
PIUSB_TYPE_DISARMED=83   # Linux -- no boot filesystem, as far as firmware sees

# The stick, as the board has it right now.
#
# Not `$MACH_DEVICE` directly: that is a kernel name from a conf, assigned in
# enumeration order, while "the USB" is unambiguous evidence that survives
# the stick being swapped for another (disk_resolve_own, boot/disk.sh).
#
# Resolved once per process: two resolutions could land on different disks,
# and the read-back would then confirm a byte on a stick nobody wrote to.
_PIUSB_DEV=""
piusb_dev() {
    [ -n "$_PIUSB_DEV" ] || _PIUSB_DEV=$(disk_own_or_declared)
    [ -n "$_PIUSB_DEV" ] || die "cannot tell which disk on $MACH_NAME is its boot stick.
    Its conf says ${MACH_DEVICE:-nothing}, and the board does not agree or could not be
    asked. Refusing to write a partition type byte to a disk chosen by name:
    on this board that byte decides whether it comes back at all."
    printf '%s' "$_PIUSB_DEV"
}

# Read partition 1's type byte, as two lowercase hex digits.
_piusb_type() {
    m_ssh "sudo dd if=$(piusb_dev) bs=1 skip=$PIUSB_TYPE_OFFSET count=1 status=none | od -An -tx1" \
        2>/dev/null | tr -d ' \r\n'
}

# Write it, then read it back.
#
# printf's octal escape rather than a here-doc, so exactly one byte leaves
# the shell; conv=notrunc because dd would otherwise truncate the whole
# device to one byte.
#
# The read-back is not ceremony: this byte decides whether the board comes
# back at all, and a write that silently did nothing (a full filesystem, a
# read-only stick, an sudo failure ssh swallowed) looks exactly like one
# that worked, right up until the machine does not come back.
_piusb_set_type() {
    local hex="$1" got
    m_ssh "printf '\\$(printf '%03o' 0x$hex)' \
        | sudo dd of=$(piusb_dev) bs=1 seek=$PIUSB_TYPE_OFFSET count=1 conv=notrunc status=none \
        && sync" || return 1
    got=$(_piusb_type)
    [ "$got" = "$hex" ] || die "$MACH_DEVICE's partition type on $MACH_NAME still reads
    0x${got:-unreadable} after writing 0x$hex. This byte is what decides whether the board's
    firmware boots the stick or steps over it to the SD card, so a write that
    did not take is not something to continue past.

    The board has not been rebooted; it is still in whatever role it was in."
}

# Armed, disarmed, or neither -- from the stick rather than from a record.
#
# "neither" is a real answer and not a failure: a type byte that is neither of
# ours means the stick holds something this driver did not put there, and
# arming it would be a guess about somebody else's disk.
piusb_state() {
    case "$(_piusb_type)" in
        "$PIUSB_TYPE_ARMED")    echo armed ;;
        "$PIUSB_TYPE_DISARMED") echo disarmed ;;
        "")                     return 1 ;;
        *)                      echo foreign ;;
    esac
}

b_arm() {
    local state
    state=$(piusb_state) || die "could not read $MACH_DEVICE's partition table on $MACH_NAME.
    Arming this machine means writing one byte of it, so the stick has to be
    attached and readable from the rescue role."

    case "$state" in
        armed)
            debug "$MACH_NAME's stick is already armed"
            return 0 ;;
        disarmed)
            _piusb_set_type "$PIUSB_TYPE_ARMED" \
                || die "could not arm the stick on $MACH_NAME" ;;
        *)
            die "$MACH_DEVICE on $MACH_NAME has partition type 0x$(_piusb_type) on partition
    $PIUSB_PART_SUFFIX, which is neither 0x$PIUSB_TYPE_ARMED nor 0x$PIUSB_TYPE_DISARMED. That is not a disk
    this driver put an image on, and arming it would be a guess.

    Write one first:  wk sysimage write <id> --disk $MACH_NAME:$MACH_DEVICE" ;;
    esac
}

# Safe to do at any time, including to a stick that has already disarmed
# itself -- which is the common case, because the image does it on the way up.
b_disarm() {
    local state
    state=$(piusb_state) || return 0
    [ "$state" = armed ] || return 0
    _piusb_set_type "$PIUSB_TYPE_DISARMED" \
        || die "could not disarm the stick on $MACH_NAME"
}

b_disarm_note() {
    log "  $MACH_DEVICE's partition $PIUSB_PART_SUFFIX is typed 0x$PIUSB_TYPE_DISARMED, so the firmware finds no"
    log "  boot filesystem there and $MACH_NAME boots its SD card. 'wk boot $MACH_NAME' puts it back."
}

# The other half of the one-shot, and the half that runs *inside* the image.
#
# `wk sysimage build` installs this as an early systemd unit, so a booted
# image takes its own start4.elf out of the firmware's way before it does
# anything else; from then on any reboot -- clean, panic, watchdog or power
# cut -- lands on the SD card.
#
# The driver owns this rather than the image profile: it is the same
# mechanism as b_arm read backwards, and a second copy of the two filenames
# is a second place for them to disagree. No systemd calls in here -- the
# unit is something systemd *orders*, and asking it to do work while it
# waits for this is the deadlock recorded in cmd/image's bootcmd comment.
b_self_disarm_sh() {
    # The disk this is running from, derived at run time: the image cannot
    # be told which device it was written to, since it is the same image
    # whatever it was written to. /boot/firmware is its own boot partition,
    # so its parent is the disk whose table to edit.
    #
    # **No single quote may appear in what this returns.** It is
    # interpolated into a systemd `ExecStart=/bin/sh -c '...'`, single-quoted,
    # so one of its own would close that string early and hand systemd three
    # fragments instead of one command -- `printf '\203'` does exactly that.
    # wk selftest asserts this against a built image, since a failure here
    # only otherwise shows up as a board that does not come back.
    printf "%s" "b=/boot/firmware; [ -d \$b ] || b=/boot; \
p=\$(findmnt -no SOURCE \$b) && \
d=/dev/\$(lsblk -no PKNAME \"\$p\") && \
printf \"\\$(printf '%03o' 0x$PIUSB_TYPE_DISARMED)\" \
| dd of=\"\$d\" bs=1 seek=$PIUSB_TYPE_OFFSET count=1 conv=notrunc status=none && sync"
}

# What can be asked of the board itself. The boot order proves the fallback the
# design leans on is still in place, and the stick's state is the arming --
# there is no write-only register here, so both halves are readable.
b_evidence() {
    m_ssh "$EEPROM_CONFIG_CMD" \
        | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p' || true

    # Captured first, then judged: `$(cmd || echo unreadable)` *appends* the
    # fallback to whatever the command already printed, so a call that
    # answered and then exited nonzero would report both -- two answers to
    # one question with no way to tell which is live.
    local state
    state=$(piusb_state 2>/dev/null) || true
    printf 'usb_stick=%s\n' "${state:-unreadable}"
}

# The wk-managed media, in one line, for the fleet block in `wk status`.
b_media() {
    local id state
    case "${MODE:-}" in
        bench*) printf 'booted from its USB stick (system %s); SD card is the rescue' "${MODE#bench }"; return 0 ;;
        # The board is on its SD card: the base image, reachable, and the
        # stick can be read from here like it can from host mode -- so this
        # reports what is on the stick rather than stopping at "not a bench
        # system", which would report a reachable board as unreachable.
        base*)  id=$(b_device_image 2>/dev/null || true)
                state=$(piusb_state 2>/dev/null) || true
                printf 'booted its SD card -- the base image (%s), not a bench system; USB stick %s holds %s, %s' \
                    "${MODE#base }" "$MACH_DEVICE" \
                    "${id:-no wk system (wk sysimage write puts one there)}" "${state:-unreadable}"
                return 0 ;;
        host)   ;;
        *)      printf 'USB stick %s: state unknown (board unreachable); SD card is the rescue' "$MACH_DEVICE"; return 0 ;;
    esac
    id=$(b_device_image 2>/dev/null || true)
    state=$(piusb_state 2>/dev/null) || true
    printf 'USB stick %s holds %s, %s; SD card is the rescue' \
        "$MACH_DEVICE" "${id:-no wk system (wk sysimage write puts one there)}" "${state:-unreadable}"
}

# How this board is made from nothing, composed from what its conf declares.
#
# Two media, two roles, and the order matters: the rescue goes on first, because
# it is what the board falls back to if anything about the stick is wrong. A
# board with a bench system and no rescue is a board that needs a person the
# first time a write goes badly.
b_reprovision() {
    cat <<REPROV
wk sysimage build $MACH_PROFILE
    in a workspace; hours
wk sysimage write <id> --disk <reader>:${MACH_ROOT%p[0-9]} --rescue
    the SD card -- the system this board falls back to
wk pi boot-order $MACH_NAME usb-first
wk sysimage write <id> --disk $MACH_NAME:$MACH_DEVICE
    the stick -- the system it is measured on
wk boot $MACH_NAME
    one shot; it reverts by itself
REPROV
}
