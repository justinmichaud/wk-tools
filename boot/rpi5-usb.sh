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

# The stick can hold two systems, and both are candidates: a dedicated bench
# medium carries its second system on primaries 3-4
# (`wk sysimage write --disk rpi5:/dev/sda@second`), so an A/B across two
# images needs no second stick and no reflash between arms.
B_SYSTEM_PARTS="1 3"

# Which pair a boot lands on is the firmware's own A/B: a static `autoboot.txt`
# on the stick's first boot partition says `boot_partition=1` under `[all]` and
# `boot_partition=3` under `[tryboot]`, so pair 3 is one `reboot "0 tryboot"`
# away and pair 1 is where every other boot lands. The file is written when the
# second pair is made (`wk sysimage write`), not at arm time.
RPI5_AUTOBOOT=autoboot.txt

# Asked of this driver by `wk sysimage write` when it makes a second pair
# (_medium_autoboot_for, cmd/sysimage). Declared rather than assumed of every
# dedicated medium: an autoboot.txt on the rpi4's stick would make its tryboot
# flag boot that stick's second pair instead of the staged kernel on its SD.
b_medium_selects_by_partition() { return 0; }

# Set by b_arm when the selected pair is the tryboot one, read by b_reboot in
# the same breath: only the reboot that follows an arming carries the flag.
RPI5_TRYBOOT=""

# Re-arming with the EEPROM's current order is how arming is cancelled.
b_arm() {
    local order="$1" reply word2

    # Which pair, before the order: an arming that set the boot order and then
    # refused would leave a one-shot pointing at the stick with no say in which
    # system on it boots.
    RPI5_TRYBOOT=""
    case "${ARM_SYS_PART:-}" in
        "") ;;                                  # no selection: pair 1, the [all] default
        *[!0-9]1|*1) ;;                         # pair 1 is what every plain boot lands on
        *[!0-9]3|*3) rpi5_check_autoboot; RPI5_TRYBOOT=1 ;;
        *) die "'$ARM_SYS_PART' is not a boot partition this stick selects between
    (partition 1 or 3, the two pairs of a dedicated bench medium)" ;;
    esac

    reply=$(r_sudo "vcmailbox 0x0003808b 4 4 $order") \
        || die "the firmware mailbox call failed on $NODE_NAME"

    # 0x80000000 in the second word means success; a silent no-op call looks
    # like one that worked.
    word2=$(printf '%s\n' "$reply" | tr ' ' '\n' | sed -n '2p')
    [ "$word2" = "0x80000000" ] \
        || die "the firmware refused the boot order ($word2, wanted 0x80000000)
    reply: $reply"
    debug "mailbox reply: $reply"
}

# Without autoboot.txt the tryboot flag is ignored and the board boots pair 1 --
# the wrong system, silently.
rpi5_check_autoboot() {
    local out
    # Through b_medium_read (boot/machines.sh): this board is a workstation,
    # where only the card helper runs privileged, so a bare `sudo -n mount`
    # comes back empty and reads as "no autoboot.txt".
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

# A plain reboot lands on pair 1, by the autoboot.txt [all] section.
b_reboot() {
    if [ -n "$RPI5_TRYBOOT" ]; then
        b_reboot_tryboot
    else
        r_sudo "setsid sh -c 'sleep 3; reboot' </dev/null >/dev/null 2>&1 &" >/dev/null
    fi
}

# Write-only from userspace (no get_reboot_order tag), so the persistent
# BOOT_ORDER is the only evidence the fallback is still in place.
b_evidence() {
    r_ssh "rpi-eeprom-config 2>/dev/null | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p'" || true
}

b_media() {
    local id order
    case "${MODE:-}" in
        bench*) printf 'booted from its USB stick (system %s); NVMe untouched' "${MODE#bench }"; return 0 ;;
        # Not expected (reads as host mode); answered, not "unreachable".
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

# Only the stick: the NVMe workstation install is not wk's to write.
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
