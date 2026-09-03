# Boot driver: a Pi with two media, armed by one byte of a partition table.
#
# NODE_DEVICE is the bench medium and the one the firmware tries first
# (`wk pi boot-order <machine>` writes BOOT_ORDER from it); the rescue is on
# the disk NODE_ROOT is a partition of, tried second. The rpi4 has neither the
# rpi5's firmware mailbox one-shot nor a server-side arming interface, so
# arming is whether the bench medium presents a bootable partition: its first
# partition's MBR type byte, 0x0c (FAT32 LBA, what `wk sysimage write` leaves)
# or 0x83 (Linux). Firmware that finds no FAT partition steps over the medium
# to the rescue; firmware that finds a FAT partition present but incomplete
# *halts*, which is why the byte and not start4.elf is what moves. Written with
# dd at a fixed offset rather than sfdisk: the self-disarm runs *from* this
# disk with partition 2 mounted as root, and rewriting the table would ask the
# kernel to re-read it. Still a one-shot: the image disarms itself on first
# boot (b_self_disarm_sh), so any later reboot falls through to the rescue.
#
# The failure modes, all of which end somewhere reachable:
#   medium not armed         -> no FAT on it, firmware skips it, the rescue boots
#   image kernel missing     -> refused after the write, on the card and before
#                               anything arms it (disk_check_boot_files)
#   image cannot find root   -> panic=10, reboot, and it disarmed itself, so the rescue
#   image hangs after boot   -> the self-return watchdog reboots it, and again the rescue

BOOT_ARMING=medium

# Unused: the boot order here is permanent, not per-boot; `wk pi boot-order` writes the real one.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# One system on the medium: partition 1 is the only candidate (b_systems).
B_SYSTEM_PARTS="1"

# MBR partition table: offset 446, 16-byte entries, type byte fifth (450).
PIMBR_PART_SUFFIX=1
PIMBR_TYPE_OFFSET=450
PIMBR_TYPE_ARMED=0c
PIMBR_TYPE_DISARMED=83

# Not `$NODE_DEVICE`, a kernel name: "the SD card" survives another card
# taking the name (disk_resolve_own, boot/disk.sh). Resolved once per
# process, so a read-back cannot confirm a byte on a disk nobody wrote to.
_PIMBR_DEV=""
pimbr_dev() {
    [ -n "$_PIMBR_DEV" ] || _PIMBR_DEV=$(disk_own_or_declared)
    [ -n "$_PIMBR_DEV" ] || die "cannot tell which disk on $NODE_NAME is its bench medium.
    Its conf says ${NODE_DEVICE:-nothing}, and the board does not agree or could not be
    asked. Refusing to write a partition type byte to a disk chosen by name:
    on this board that byte decides whether it comes back at all."
    printf '%s' "$_PIMBR_DEV"
}

# "SD card" or "USB stick", from the device name (disk_tran_of_name, boot/disk.sh).
_pimbr_word() {
    case "$(disk_tran_of_name "$1")" in
        mmc) printf 'SD card' ;;
        usb) printf 'USB stick' ;;
        *)   printf '%s' "$1" ;;
    esac
}
pimbr_rescue_disk() { disk_of_part "$NODE_ROOT"; }

# The bench system's boot partition is on the medium as the board has it now,
# not as the conf names it: an empty enclosure enumerating first makes the
# stick sdb, and wk-image.id is read from wherever the marker says it is.
b_boot_part() { disk_part "$(pimbr_dev)" 1; }

_pimbr_type() {
    r_sudo "dd if=$(pimbr_dev) bs=1 skip=$PIMBR_TYPE_OFFSET count=1 status=none | od -An -tx1" \
        2>/dev/null | tr -d ' \r\n'
}

# printf's octal escape, so exactly one byte leaves the shell; conv=notrunc
# since dd would otherwise truncate the whole device. Read back, since a
# silent no-op write looks exactly like one that worked.
_pimbr_set_type() {
    local hex="$1" got
    r_sudo "printf '\\$(printf '%03o' 0x$hex)' \
        | sudo dd of=$(pimbr_dev) bs=1 seek=$PIMBR_TYPE_OFFSET count=1 conv=notrunc status=none \
        && sync" || return 1
    got=$(_pimbr_type)
    [ "$got" = "$hex" ] || die "$NODE_DEVICE's partition type on $NODE_NAME still reads
    0x${got:-unreadable} after writing 0x$hex. This byte is what decides whether the board's
    firmware boots the $(_pimbr_word "$NODE_DEVICE") or steps over it to the rescue, so a write
    that did not take is not something to continue past.

    The board has not been rebooted; it is still in whatever role it was in."
}

# Armed, disarmed, or neither -- from the medium, not a record. "neither" is
# real: the medium holds something this driver did not put there.
pimbr_state() {
    case "$(_pimbr_type)" in
        "$PIMBR_TYPE_ARMED")    echo armed ;;
        "$PIMBR_TYPE_DISARMED") echo disarmed ;;
        "")                     return 1 ;;
        *)                      echo foreign ;;
    esac
}

b_arm() {
    local state
    state=$(pimbr_state) || die "could not read $NODE_DEVICE's partition table on $NODE_NAME.
    Arming this machine means writing one byte of it, so the bench medium has to be
    attached and readable from the rescue."

    case "$state" in
        armed)
            debug "$NODE_NAME's $(_pimbr_word "$NODE_DEVICE") is already armed"
            return 0 ;;
        disarmed)
            _pimbr_set_type "$PIMBR_TYPE_ARMED" \
                || die "could not arm the $(_pimbr_word "$NODE_DEVICE") on $NODE_NAME" ;;
        *)
            die "$NODE_DEVICE on $NODE_NAME has partition type 0x$(_pimbr_type) on partition
    $PIMBR_PART_SUFFIX, which is neither 0x$PIMBR_TYPE_ARMED nor 0x$PIMBR_TYPE_DISARMED. That is not a disk
    this driver put an image on, and arming it would be a guess.

    Write one first:  wk sysimage write <id> --disk $NODE_NAME:$NODE_DEVICE" ;;
    esac
}

# Safe at any time, including a medium already disarmed -- the common case.
b_disarm() {
    local state
    state=$(pimbr_state) || return 0
    [ "$state" = armed ] || return 0
    _pimbr_set_type "$PIMBR_TYPE_DISARMED" \
        || die "could not disarm the $(_pimbr_word "$NODE_DEVICE") on $NODE_NAME"
}

b_disarm_note() {
    log "  $NODE_DEVICE's partition $PIMBR_PART_SUFFIX is typed 0x$PIMBR_TYPE_DISARMED, so the firmware finds no"
    log "  boot filesystem there and $NODE_NAME boots its rescue on $(pimbr_rescue_disk). 'wk boot $NODE_NAME' puts it back."
}

# The half that runs *inside* the image on its first boot, before the
# benchmark starts: as a systemd unit on a yocto image and as a BusyBox init
# script on a buildroot one (stage_units, cmd/sysimage), from this one string.
#
# POSIX sh against /proc and /sys only -- a BusyBox image has neither findmnt
# nor lsblk. The disk this runs from is derived at run time: the image is not
# told which device it was written to. **No single quote may appear in what
# this returns** (it lands inside a `sh -c '...'`); wk selftest asserts it.
b_self_disarm_sh() {
    printf "%s" "while read -r id parent mm root mp rest; do [ \"\$mp\" = / ] && break; done < /proc/self/mountinfo; \
p=\$(readlink -f /sys/dev/block/\$mm) && \
d=/dev/\$(basename \"\$(dirname \"\$p\")\") && \
printf \"\\$(printf '%03o' 0x$PIMBR_TYPE_DISARMED)\" \
| dd of=\"\$d\" bs=1 seek=$PIMBR_TYPE_OFFSET count=1 conv=notrunc status=none && sync"
}

# The boot order proves the fall-through is still in place; the medium's
# state is the arming.
b_evidence() {
    r_ssh "$EEPROM_CONFIG_CMD" \
        | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p' || true

    # Captured first, then judged: `$(cmd || echo unreadable)` *appends* the
    # fallback, so a call that answered and then exited nonzero reports both.
    local state
    state=$(pimbr_state 2>/dev/null) || true
    printf 'bench_medium=%s\n' "${state:-unreadable}"
}

# The wk-managed media, one line, for `wk status`'s fleet block.
b_media() {
    local id state bench rescue
    bench=$(_pimbr_word "$NODE_DEVICE"); rescue=$(_pimbr_word "$(pimbr_rescue_disk)")
    case "${MODE:-}" in
        bench*) printf 'booted from its %s (system %s); the %s is the rescue' "$bench" "${MODE#bench }" "$rescue"; return 0 ;;
        # Reachable on its rescue: report the bench medium too, rather than looking unreachable.
        base*)  id=$(b_device_image 2>/dev/null || true)
                state=$(pimbr_state 2>/dev/null) || true
                printf 'booted its rescue on the %s (%s); %s %s holds %s, %s' \
                    "$rescue" "${MODE#base }" "$bench" "$NODE_DEVICE" \
                    "${id:-no wk system (wk sysimage write puts one there)}" "${state:-unreadable}"
                return 0 ;;
        host)   ;;
        *)      printf '%s %s: state unknown (board unreachable); the %s is the rescue' "$bench" "$NODE_DEVICE" "$rescue"; return 0 ;;
    esac
    id=$(b_device_image 2>/dev/null || true)
    state=$(pimbr_state 2>/dev/null) || true
    printf '%s %s holds %s, %s; the %s is the rescue' \
        "$bench" "$NODE_DEVICE" "${id:-no wk system (wk sysimage write puts one there)}" "${state:-unreadable}" "$rescue"
}

# The rescue goes on first: a bench system with no rescue behind it needs a
# person the first time a write goes badly.
b_reprovision() {
    cat <<REPROV
wk sysimage build $NODE_PROFILE
    in a workspace; hours
wk sysimage write <id> --disk <reader>:$(pimbr_rescue_disk) --rescue
    the $(_pimbr_word "$(pimbr_rescue_disk)") -- the system this board falls back to
wk pi boot-order $NODE_NAME
    the $(_pimbr_word "$NODE_DEVICE") first, the rescue behind it
wk sysimage write <id> --disk $NODE_NAME:$NODE_DEVICE
    the $(_pimbr_word "$NODE_DEVICE") -- the system it is measured on
wk boot $NODE_NAME
    one shot; it reverts by itself
REPROV
}
