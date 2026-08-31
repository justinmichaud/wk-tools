# Boot driver: Raspberry Pi 5, one-shot boot from the attached USB device,
# via `set_reboot_order` through the firmware mailbox
# (`vcmailbox 0x0003808b 4 4 0xf64`): a register the firmware clears after
# one use, unlike an EEPROM change. Lowest-nibble-first order (4=USB,
# 6=NVMe, f=restart) puts the workstation's NVMe as the fallback a wedged
# image or a plain power cycle lands on; undo is a command, not a trip to
# the device.

BOOT_ARMING=one-shot   # the intent lives in the machine's own firmware, not a server record

BOOT_ORDER_IMAGE=0xf64     # USB -> NVMe -> restart
BOOT_ORDER_NORMAL=0xf461   # the EEPROM's own order: SD -> NVMe -> USB -> restart

# One system on the medium: partition 1 is the only candidate (b_systems).
B_SYSTEM_PARTS="1"

# Re-arming with the EEPROM's current order is a safe no-op that still
# proves the mailbox call works -- also how arming is *cancelled*.
b_arm() {
    local order="$1" reply word2
    reply=$(m_ssh "sudo vcmailbox 0x0003808b 4 4 $order") \
        || die "the firmware mailbox call failed on $MACH_NAME"

    # 0x80000000 in the second word means success; checked rather than
    # assumed, since a silent no-op call looks like one that worked.
    word2=$(printf '%s\n' "$reply" | tr ' ' '\n' | sed -n '2p')
    [ "$word2" = "0x80000000" ] \
        || die "the firmware refused the boot order ($word2, wanted 0x80000000)
    reply: $reply"
    debug "mailbox reply: $reply"
}

# Write-only from userspace (no get_reboot_order tag), so the persistent
# BOOT_ORDER is the only evidence the fallback is still in place.
b_evidence() {
    m_ssh "rpi-eeprom-config 2>/dev/null | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p'" || true
}

# The wk-managed media, one line, for `wk status`'s fleet block.
b_media() {
    local id order
    case "${MODE:-}" in
        bench*) printf 'booted from its USB stick (system %s); NVMe untouched' "${MODE#bench }"; return 0 ;;
        # Not expected (reads as host mode); answered, not "unreachable".
        base*)  printf 'booted %s -- a wk system on the medium that is never armed (%s)' \
                    "${MACH_ROOT:-its base medium}" "${MODE#base }"; return 0 ;;
        host)   ;;
        *)      printf 'USB stick %s: state unknown (board unreachable)' "$MACH_DEVICE"; return 0 ;;
    esac
    id=$(b_device_image 2>/dev/null || true)
    order=$(b_evidence 2>/dev/null | kv_get eeprom_boot_order)
    printf 'USB stick %s holds %s; NVMe workstation untouched%s' \
        "$MACH_DEVICE" "${id:-no wk system (wk sysimage write puts one there)}" \
        "${order:+ (eeprom $order)}"
}

# Only the stick: the NVMe workstation install is not wk's to write, is
# never touched (`wk help`), and is the one medium this must never name.
b_reprovision() {
    cat <<REPROV
wk sysimage build $MACH_PROFILE
    in a workspace; hours
wk sysimage write <id> --disk $MACH_NAME:$MACH_DEVICE
wk boot $MACH_NAME
    one shot; it reverts by itself
REPROV
}
