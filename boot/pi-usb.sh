# Boot driver: a Pi that boots an image from its USB stick, with the SD card
# behind it as the rescue role.
#
# This exists because the rpi4 can have neither of the other two mechanisms and
# needs the property both of them provide.
#
# `boot/rpi5-usb.sh` uses `set_reboot_order` through the firmware mailbox --
# a genuine one-shot, and Raspberry Pi 5 only. `boot/pi-netboot.sh` moves the
# arming to the server instead, which is a good answer and the wrong one for a
# benchmark: a netboot that reaches start4.elf and no further *halts*, with no
# fall-through and nothing reachable over the wire, and a lane that runs
# unattended cannot have a state whose only exit is a hand on the power supply.
# It also puts the root filesystem on the network, which puts the network in
# the measurement. See docs/HANDOFF-benchmarking.md, "rpi4".
#
# So the arming moves to the boot medium:
#
#   BOOT_ORDER is USB -> SD -> restart (`wk pi boot-order <host> usb-first`),
#   permanently, and arming is whether the stick presents a *bootable
#   partition* for the firmware to find -- which is one byte of the MBR.
#
# **Not the second-stage file, and this distinction cost a power cycle.** The
# first version of this driver disarmed by renaming `start4.elf` aside, on the
# reasoning that a device the firmware cannot boot is a device it skips. That
# reasoning was drawn from the right observation and the wrong state: what was
# actually watched skipping, on this board on 2026-08-20, was a stick carrying
# a single **ext4** partition and no FAT at all. A stick with a perfectly valid
# FAT boot partition that happens to be missing `start4.elf` is a different
# thing entirely, and the firmware **halts** on it -- the same halt as netboot,
# reached from a new direction, and the board went silent mid-afternoon.
#
# So the disarm now reproduces the state that was observed to skip: partition
# 1's type byte in the MBR is flipped from 0x0c (FAT32 LBA) to 0x83 (Linux), so
# the firmware finds no boot filesystem on the device at all and moves to the
# SD card. Nothing is erased, no filesystem is touched, and the 4.6 GB on the
# stick stays exactly where it is -- the partition is still there and still
# mountable by anything that looks at the superblock rather than the table.
#
# One byte, written with dd at a fixed offset rather than through sfdisk,
# because the self-disarm runs *from* that disk with partition 2 mounted as
# root: a tool that rewrites the table asks the kernel to re-read it, and a
# surgical write to offset 450 asks nothing of anybody.
#
# **And it is still a one-shot**, which is the part worth reading twice. The
# image disarms *itself* on first boot -- it renames its own start4.elf aside
# before the benchmark starts -- so any later reboot falls through to the SD
# card. That is the same "reverts by itself unless claimed" property the rpi5
# gets from the firmware, reached without the primitive the Pi 4 does not have.
# A run that wedges the board costs a reboot, not a journey.
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

# Read partition 1's type byte, as two lowercase hex digits.
_piusb_type() {
    m_ssh "sudo dd if=$MACH_DEVICE bs=1 skip=$PIUSB_TYPE_OFFSET count=1 status=none | od -An -tx1" \
        2>/dev/null | tr -d ' \r\n'
}

# Write it, then read it back.
#
# printf's octal escape rather than a here-doc, so exactly one byte leaves the
# shell; conv=notrunc because dd would otherwise truncate the whole device to
# one byte, which on a boot medium is not a recoverable mistake.
#
# The read-back is not ceremony. This byte decides whether the board comes back
# at all: leave it 0x0c when it should be 0x83 and the next boot finds a FAT
# partition it cannot complete from, which halts the firmware and costs a trip
# to the device. A write that silently did nothing -- a full filesystem, a
# stick that went read-only, an sudo that failed in a way ssh swallowed --
# looks exactly like a write that worked, right up until the machine does not
# come back. So it is confirmed here, where saying so is free.
_piusb_set_type() {
    local hex="$1" got
    m_ssh "printf '\\$(printf '%03o' 0x$hex)' \
        | sudo dd of=$MACH_DEVICE bs=1 seek=$PIUSB_TYPE_OFFSET count=1 conv=notrunc status=none \
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
# `wk sysimage build` installs this as an early systemd unit, so a booted image
# takes its own start4.elf out of the firmware's way before it does anything
# else. From then on the stick is not bootable and any reboot -- clean, panic,
# watchdog or power cut -- lands on the SD card, reachable.
#
# The driver owns this rather than the image profile because it is the same
# mechanism as b_arm read backwards, and a second copy of the two filenames is
# a second place for them to disagree. Inside the image the stick's boot
# partition is its own /boot/firmware.
#
# No systemd calls in here. The unit is something systemd *orders*; a unit that
# asks systemd to do work while systemd is waiting for it is the deadlock
# recorded in cmd/image's bootcmd comment.
b_self_disarm_sh() {
    # The disk this is running from, derived at run time: the image cannot be
    # told which device it was written to, because the whole point is that it
    # is the same image whatever it was written to. /boot/firmware is its own
    # boot partition, so its parent is the disk whose table to edit.
    #
    # **No single quote may appear in what this returns.** It is interpolated
    # into a systemd `ExecStart=/bin/sh -c '...'`, which is single-quoted, so
    # one of its own would close that string early and hand systemd three
    # fragments instead of one command. The first version emitted
    # `printf '\203'` and did exactly that -- caught by reading the unit out of
    # a built image rather than by the board failing to come back, which is the
    # only other way it was going to be found. wk selftest asserts it now.
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

    # Captured first, then judged. `$(cmd || echo unreadable)` *appends* the
    # fallback to whatever the command already printed, so a call that answered
    # and then exited nonzero reported both -- a status line reading
    # "usb_stick=disarmed" followed by a bare "unreadable", which is two
    # answers to one question and no way to tell which is live.
    local state
    state=$(piusb_state 2>/dev/null) || true
    printf 'usb_stick=%s\n' "${state:-unreadable}"
}
