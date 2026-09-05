# Boot driver: Raspberry Pi 5, one-shot USB boot via `set_reboot_order` through the firmware mailbox (`vcmailbox 0x0003808b 4 4 <order>`), a register the firmware clears after one use. Nibbles are tried lowest first: 4=USB, 6=NVMe, f=restart.

BOOT_ARMING=one-shot   # the intent lives in the machine's own firmware, not a server record

BOOT_ORDER_IMAGE=0xf64     # USB -> NVMe -> restart
BOOT_ORDER_NORMAL=0xf461   # the EEPROM's own order: SD -> NVMe -> USB -> restart

B_SYSTEM_PARTS="1 3"

# The firmware's own A/B: on the stick's first boot partition it sets `boot_partition=1` under `[all]` and `=3` under `[tryboot]`, so pair 3 is one `reboot "0 tryboot"` away.
RPI5_AUTOBOOT=autoboot.txt

b_medium_selects_by_partition() { return 0; }

RPI5_TRYBOOT=""

b_arm() {
    local order="$1" reply word2

    boot_priv_require

    RPI5_TRYBOOT=""
    case "${ARM_SYS_PART:-}" in
        "") ;;                                  # no selection: pair 1, the [all] default
        *[!0-9]1|*1) ;;                         # pair 1 is what every plain boot lands on
        *[!0-9]3|*3) rpi5_check_autoboot; RPI5_TRYBOOT=1 ;;
        *) die "'$ARM_SYS_PART' is not a boot partition this stick selects between
    (partition 1 or 3, the two pairs of a dedicated bench medium)" ;;
    esac

    reply=$(boot_priv order "$order") \
        || die "the firmware mailbox call failed on $NODE_NAME"

    word2=$(printf '%s\n' "$reply" | tr ' ' '\n' | sed -n '2p')
    [ "$word2" = "0x80000000" ] \
        || die "the firmware refused the boot order ($word2, wanted 0x80000000)
    reply: $reply"
    debug "mailbox reply: $reply"
}

rpi5_check_autoboot() {
    local out
    out=$(b_medium_read "$(disk_part "$NODE_DEVICE" 1)" "$RPI5_AUTOBOOT") || out=""
    case "$out" in
        *boot_partition=3*) return 0 ;;
    esac
    die "$NODE_DEVICE on $NODE_NAME has no [tryboot] boot_partition=3 in its
    $RPI5_AUTOBOOT, so the firmware has no way to boot the second pair: the flag
    would be ignored and pair 1 would boot instead -- the wrong system, with
    nothing to say so. The file is written when the second pair is made, so
    rewrite it:
        wk sysimage write --from <path> --disk $NODE_NAME:$NODE_DEVICE@second"
}

b_reboot() {
    if [ -n "$RPI5_TRYBOOT" ]; then
        b_reboot_tryboot
    else
        boot_priv reboot >/dev/null
    fi
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
