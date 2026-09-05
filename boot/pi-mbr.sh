# Boot driver: a Pi with two media, armed by one byte -- the bench medium's partition 1 MBR type: 0x0c (FAT32 LBA) armed,
# 0x83 (Linux) disarmed. Firmware that finds no FAT partition steps over the medium to the rescue, while one that finds it
# present but incomplete *halts*, so the byte and not start4.elf moves; dd at a fixed offset, since the self-disarm runs from this disk.

BOOT_ARMING=medium

BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

B_SYSTEM_PARTS="1"

# MBR partition table: offset 446, 16-byte entries, type byte fifth (450).
PIMBR_PART_SUFFIX=1
PIMBR_TYPE_OFFSET=450
PIMBR_TYPE_ARMED=0c
PIMBR_TYPE_DISARMED=83

_PIMBR_DEV=""
pimbr_dev() {
    [ -n "$_PIMBR_DEV" ] || _PIMBR_DEV=$(disk_own_or_declared)
    [ -n "$_PIMBR_DEV" ] || die "cannot tell which disk on $NODE_NAME is its bench medium.
    Its conf says ${NODE_DEVICE:-nothing}, and the board does not agree or could not be
    asked. Refusing to write a partition type byte to a disk chosen by name:
    on this board that byte decides whether it comes back at all."
    printf '%s' "$_PIMBR_DEV"
}

_pimbr_word() {
    case "$(disk_tran_of_name "$1")" in
        mmc) printf 'SD card' ;;
        usb) printf 'USB stick' ;;
        *)   printf '%s' "$1" ;;
    esac
}
pimbr_rescue_disk() { disk_of_part "$NODE_ROOT"; }

b_boot_part() { disk_part "$(pimbr_dev)" 1; }

_pimbr_type() {
    r_sudo "dd if=$(pimbr_dev) bs=1 skip=$PIMBR_TYPE_OFFSET count=1 status=none | od -An -tx1" \
        2>/dev/null | tr -d ' \r\n'
}

# printf's octal escape, so exactly one byte leaves the shell; conv=notrunc, or dd truncates the whole device.
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

# POSIX sh against /proc and /sys only (a BusyBox image has neither findmnt nor lsblk), and no single quote: it lands inside a `sh -c '...'`.
b_self_disarm_sh() {
    printf "%s" "while read -r id parent mm root mp rest; do [ \"\$mp\" = / ] && break; done < /proc/self/mountinfo; \
p=\$(readlink -f /sys/dev/block/\$mm) && \
d=/dev/\$(basename \"\$(dirname \"\$p\")\") && \
printf \"\\$(printf '%03o' 0x$PIMBR_TYPE_DISARMED)\" \
| dd of=\"\$d\" bs=1 seek=$PIMBR_TYPE_OFFSET count=1 conv=notrunc status=none && sync"
}

b_evidence() {
    r_ssh "$EEPROM_CONFIG_CMD" \
        | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p' || true

    local state
    state=$(pimbr_state 2>/dev/null) || true
    printf 'bench_medium=%s\n' "${state:-unreadable}"
}

b_media() {
    local id state bench rescue
    bench=$(_pimbr_word "$NODE_DEVICE"); rescue=$(_pimbr_word "$(pimbr_rescue_disk)")
    case "${MODE:-}" in
        bench*) printf 'booted from its %s (system %s); the %s is the rescue' "$bench" "${MODE#bench }" "$rescue"; return 0 ;;
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
