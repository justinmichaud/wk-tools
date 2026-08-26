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
# The return path holds both ways: a power cycle and a self-reboot each land
# back on the NVMe.

# How this machine is armed. The two models differ in where the intent lives:
# a one-shot writes it into the machine's firmware and a record beside it,
# while a server-armed machine's intent is simply what the server is holding.
BOOT_ARMING=one-shot

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

# The wk-managed media, in one line, for the fleet block in `wk status`.
b_media() {
    local id order
    case "${MODE:-}" in
        bench*) printf 'booted from its USB stick (system %s); NVMe untouched' "${MODE#bench }"; return 0 ;;
        # Not expected on this board -- its MACH_ROOT is the NVMe workstation
        # install, which carries no marker and therefore reads as host mode --
        # but answered rather than falling into "unreachable" below, because a
        # reachable machine reported unreachable is a wrong answer whichever
        # way it is arrived at.
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

# How this machine's bench lane is made from nothing.
#
# Only the stick. The NVMe workstation install is not wk's to write and is never
# touched (`wk help hardware`), so there is no rescue to make here -- the
# workstation *is* the fallback, and it is the one medium in the fleet this
# command must never name.
b_reprovision() {
    cat <<REPROV
wk sysimage build $MACH_PROFILE
    in a workspace; hours
wk sysimage write <id> --disk $MACH_NAME:$MACH_DEVICE
wk boot $MACH_NAME
    one shot; it reverts by itself
REPROV
}
