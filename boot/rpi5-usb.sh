# Boot driver: Raspberry Pi 5, one-shot USB boot via `set_reboot_order` through the firmware mailbox (`vcmailbox 0x0003808b 4 4 <order>`), a register the firmware clears after one use. Nibbles are tried lowest first: 4=USB, 6=NVMe, f=restart.

BOOT_ARMING=one-shot   # the intent lives in the machine's own firmware, not a server record

BOOT_ORDER_IMAGE=0xf64     # USB -> NVMe -> restart
BOOT_ORDER_NORMAL=0xf461   # the EEPROM's own order: SD -> NVMe -> USB -> restart

B_SYSTEM_PARTS="1 3"

# The firmware's own A/B, and the only selector this board has: `boot_partition=<pair>` under `[all]` on the stick's first boot partition, written before each arm. Not the firmware's tryboot flag -- this board's tryboot belongs to flash-kernel's staging on its NVMe (boot/machines/rpi5.conf), and a pair selected with it does not boot at all: dark, no kernel, no panic, where the same pair selected here runs to userspace (measured, 2026-09-05).
RPI5_AUTOBOOT=autoboot.txt

b_medium_selects_by_partition() { return 0; }

b_arm() {
    local order="$1" reply word2 pair

    boot_priv_require

    case "${ARM_SYS_PART:-}" in
        ""|*[!0-9]1|*1) pair=1 ;;
        *[!0-9]3|*3)    pair=3 ;;
        *) die "'$ARM_SYS_PART' is not a boot partition this stick selects between
    (partition 1 or 3, the two pairs of a dedicated bench medium)" ;;
    esac
    rpi5_select_pair "$pair"

    reply=$(boot_priv order "$order") \
        || die "the firmware mailbox call failed on $NODE_NAME"

    word2=$(printf '%s\n' "$reply" | tr ' ' '\n' | sed -n '2p')
    [ "$word2" = "0x80000000" ] \
        || die "the firmware refused the boot order ($word2, wanted 0x80000000)
    reply: $reply"
    debug "mailbox reply: $reply"
}

rpi5_select_pair() { # <pair>
    local want="$1" out
    disk_unmount "$NODE_DEVICE"
    card_priv autoboot "$NODE_DEVICE" "$want" >/dev/null \
        || die "could not write $NODE_DEVICE's pair selector on $NODE_NAME"
    out=$(b_medium_read "$(disk_part "$NODE_DEVICE" 1)" "$RPI5_AUTOBOOT") || out=""
    case "$out" in
        *"boot_partition=$want"*) return 0 ;;
    esac
    die "$NODE_DEVICE on $NODE_NAME does not select pair $want after being told to,
    so the board would boot the other system and it would be measured under this
    one's name. Its card helper is older than the pair argument and ignores it.
    The remedy, from a terminal on $NODE_NAME:  ./setup --stage quiesce
    what its $RPI5_AUTOBOOT says now:
$(printf '%s' "$out" | sed 's/^/      /')"
}

b_reboot() {
    boot_priv reboot >/dev/null
}

# The one-shot order is write-only from userspace (no get_reboot_order tag), so the EEPROM's persistent order is the only evidence.
b_evidence() {
    r_ssh "rpi-eeprom-config 2>/dev/null | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p'" || true
}

b_media() {
    local id order
    case "${MODE:-}" in
        bench*) printf 'booted from its USB stick (system %s); NVMe untouched' "${MODE#bench }"; return 0 ;;
        base*)  printf 'booted %s -- a wk system on the medium that is never armed (%s)' \
                    "${NODE_ROOT:-its base medium}" "${MODE#base }"; return 0 ;;
        host)   ;;
        *)      printf 'USB stick %s: state unknown (board unreachable)' "$NODE_DEVICE"; return 0 ;;
    esac
    id=$(b_device_image 2>/dev/null || true)
    order=$(b_evidence 2>/dev/null | kv_get eeprom_boot_order)
    printf 'USB stick %s holds %s; NVMe workstation untouched%s' \
        "$NODE_DEVICE" "${id:-no wk system (wk sysimage write puts one there)}" \
        "${order:+ (eeprom $order)}"
}

b_reprovision() {
    cat <<REPROV
wk sysimage build $NODE_PROFILE
    in a workspace; hours
wk sysimage write --from <path> --disk $NODE_NAME:$NODE_DEVICE
wk sysimage write --from <path> --disk $NODE_NAME:$NODE_DEVICE@second
    optional: a second system beside the first, for an A/B across two images.
    Making it also writes the firmware's selector (autoboot.txt) onto the
    medium, which is what lets 'wk boot $NODE_NAME --system <id>' choose
wk boot $NODE_NAME
    one shot; it reverts by itself
REPROV
}
