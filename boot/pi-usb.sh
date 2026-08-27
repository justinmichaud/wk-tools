# Boot driver: a Pi that boots an image from its USB stick, SD card behind
# it as the rescue role -- the rpi4 has neither the rpi5's firmware mailbox
# one-shot nor a server-side arming interface, so arming lives on the boot
# medium: BOOT_ORDER is permanently USB -> SD (`wk pi boot-order <host>
# usb-first`), and arming is whether the stick presents a bootable partition
# (one byte of the MBR). Disarming flips that byte (0x0c FAT32 LBA -> 0x83
# Linux) rather than renaming start4.elf, since firmware that finds a FAT
# partition present but incomplete *halts* rather than falls through, with
# nothing over the wire to recover it. Written with dd at a fixed offset
# rather than sfdisk, since the self-disarm runs *from* this disk with
# partition 2 mounted as root and rewriting the table asks the kernel to
# re-read it. Still a one-shot: the image disarms itself on first boot
# (b_self_disarm_sh below), so any later reboot falls through to the SD.
#
# The failure modes, all of which end somewhere reachable:
#   stick not armed          -> no FAT on the device, firmware skips it, SD boots
#   image kernel missing     -> refused before writing (image_check_boot_files)
#   image cannot find root   -> panic=10, reboot, and it disarmed itself, so SD
#   image hangs after boot   -> watchdog reboots it, and again SD

# Arming lives on the boot medium here, readable and writable from the other role.
BOOT_ARMING=medium

# Unused: the boot order here is permanent, not per-boot; `wk pi boot-order` writes the real one.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# MBR partition table: offset 446, 16-byte entries, type byte fifth (450).
PIUSB_PART_SUFFIX=1
PIUSB_TYPE_OFFSET=450
PIUSB_TYPE_ARMED=0c      # FAT32 LBA -- what `wk sysimage write` leaves behind
PIUSB_TYPE_DISARMED=83   # Linux -- no boot filesystem, as far as firmware sees

# Not `$MACH_DEVICE`, a kernel name: "the USB" survives the stick being
# swapped (disk_resolve_own, boot/disk.sh). Resolved once per process, so a
# read-back cannot confirm a byte on a stick nobody wrote to.
_PIUSB_DEV=""
piusb_dev() {
    [ -n "$_PIUSB_DEV" ] || _PIUSB_DEV=$(disk_own_or_declared)
    [ -n "$_PIUSB_DEV" ] || die "cannot tell which disk on $MACH_NAME is its boot stick.
    Its conf says ${MACH_DEVICE:-nothing}, and the board does not agree or could not be
    asked. Refusing to write a partition type byte to a disk chosen by name:
    on this board that byte decides whether it comes back at all."
    printf '%s' "$_PIUSB_DEV"
}

_piusb_type() {
    m_ssh "sudo dd if=$(piusb_dev) bs=1 skip=$PIUSB_TYPE_OFFSET count=1 status=none | od -An -tx1" \
        2>/dev/null | tr -d ' \r\n'
}

# printf's octal escape, so exactly one byte leaves the shell; conv=notrunc
# since dd would otherwise truncate the whole device. Read back, since a
# silent no-op write looks exactly like one that worked.
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

# Armed, disarmed, or neither -- from the stick, not a record. "neither" is
# real: the stick holds something this driver did not put there.
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

# Safe at any time, including a stick already disarmed -- the common case.
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

# The half that runs *inside* the image, as an early systemd unit
# (`wk sysimage build`) that disarms before the benchmark starts. No
# systemd calls in here: the unit is something systemd *orders*, and
# calling into it here is the deadlock in cmd/image's bootcmd comment.
b_self_disarm_sh() {
    # The disk this runs from, derived at run time: the image is not told
    # which device it was written to.
    # **No single quote may appear in what this returns**: interpolated
    # into a systemd `ExecStart=/bin/sh -c '...'`, one would hand systemd
    # three fragments instead of one command. wk selftest asserts this
    # against a built image.
    printf "%s" "b=/boot/firmware; [ -d \$b ] || b=/boot; \
p=\$(findmnt -no SOURCE \$b) && \
d=/dev/\$(lsblk -no PKNAME \"\$p\") && \
printf \"\\$(printf '%03o' 0x$PIUSB_TYPE_DISARMED)\" \
| dd of=\"\$d\" bs=1 seek=$PIUSB_TYPE_OFFSET count=1 conv=notrunc status=none && sync"
}

# The boot order proves the fallback is still in place; the stick's state
# is the arming.
b_evidence() {
    m_ssh "$EEPROM_CONFIG_CMD" \
        | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p' || true

    # Captured first, then judged: `$(cmd || echo unreadable)` *appends* the
    # fallback, so a call that answered and then exited nonzero reports both.
    local state
    state=$(piusb_state 2>/dev/null) || true
    printf 'usb_stick=%s\n' "${state:-unreadable}"
}

# The wk-managed media, one line, for `wk status`'s fleet block.
b_media() {
    local id state
    case "${MODE:-}" in
        bench*) printf 'booted from its USB stick (system %s); SD card is the rescue' "${MODE#bench }"; return 0 ;;
        # Reachable on its SD card: report the stick too, rather than looking unreachable.
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

# The rescue goes on first: a bench system with no rescue behind it needs a
# person the first time a write goes badly.
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
