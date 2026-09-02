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

# Which pair a boot lands on is the *firmware's* own A/B, not something this
# end puts back afterwards: a static `autoboot.txt` on the stick's first boot
# partition says `boot_partition=1` under `[all]` and `boot_partition=3` under
# `[tryboot]`, so pair 3 is one `reboot "0 tryboot"` away and pair 1 is where
# every other boot lands. Two one-shots, both firmware-reverting, and nothing
# on the medium changes when a leg is armed -- unlike pi-sd, which edits a
# config.txt and needs the bench system to put it back.
#
# The file is written when the second pair is made (`wk sysimage write`), not
# at arm time: it never varies, and a file that never varies is not state to
# keep in step.
RPI5_AUTOBOOT=autoboot.txt

# Asked of this driver by `wk sysimage write` when it makes a second pair on
# this board's medium (_medium_autoboot_for, cmd/sysimage). Declared rather than
# assumed of every dedicated medium: the rpi4's stick is one too, and an
# autoboot.txt there would make its tryboot flag boot the *stick's* second pair
# instead of the staged kernel on its SD -- a working lane broken by a file
# written for another board.
b_medium_selects_by_partition() { return 0; }

# Set by b_arm when the selected pair is the tryboot one, read by b_reboot in
# the same breath -- the same shape pi-tryboot uses, for the same reason: only
# the reboot that follows an arming carries the flag.
RPI5_TRYBOOT=""

# Re-arming with the EEPROM's current order is a safe no-op that still
# proves the mailbox call works -- also how arming is *cancelled*.
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
        || die "the firmware mailbox call failed on $MACH_NAME"

    # 0x80000000 in the second word means success; checked rather than
    # assumed, since a silent no-op call looks like one that worked.
    word2=$(printf '%s\n' "$reply" | tr ' ' '\n' | sed -n '2p')
    [ "$word2" = "0x80000000" ] \
        || die "the firmware refused the boot order ($word2, wanted 0x80000000)
    reply: $reply"
    debug "mailbox reply: $reply"
}

# Selecting pair 3 needs the firmware to have been told how, and the file that
# tells it is on the medium rather than in a record here. Absent, the flag is
# ignored and the board boots pair 1 -- the wrong system, silently, which is the
# one outcome an A/B must never produce.
rpi5_check_autoboot() {
    local out
    out=$(r_sudo "m=\$(mktemp -d)
        p=\$(awk -v d=$(sh_quote "$(disk_part "$MACH_DEVICE" 1)") '\$1 == d { print \$2; exit }' /proc/mounts)
        own=
        if [ -z \"\$p\" ]; then mount -t vfat -o ro $(sh_quote "$(disk_part "$MACH_DEVICE" 1)") \"\$m\" 2>/dev/null || exit 0; p=\$m; own=1; fi
        grep -c 'boot_partition=3' \"\$p/$RPI5_AUTOBOOT\" 2>/dev/null || echo 0
        [ -n \"\$own\" ] && umount \"\$m\"; rmdir \"\$m\" 2>/dev/null; true" \
        2>/dev/null | tr -d '\r' | head -1)
    case "$out" in
        ''|0) die "$MACH_DEVICE on $MACH_NAME has no [tryboot] boot_partition=3 in its
    $RPI5_AUTOBOOT, so the firmware has no way to boot the second pair: the flag
    would be ignored and pair 1 would boot instead -- the wrong system, with
    nothing to say so. The file is written when the second pair is made, so
    rewrite it:
        wk sysimage write --from <path> --disk $MACH_NAME:$MACH_DEVICE@second" ;;
    esac
}

# Only an arming for the second pair carries the flag; every other reboot is
# plain, and a plain boot lands on pair 1 by the autoboot.txt [all] section.
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
wk sysimage write --from <path> --disk $MACH_NAME:$MACH_DEVICE
wk sysimage write --from <path> --disk $MACH_NAME:$MACH_DEVICE@second
    optional: a second system beside the first, for an A/B across two images.
    Making it also writes the firmware's selector (autoboot.txt) onto the
    medium, which is what lets 'wk boot $MACH_NAME --system <id>' choose
wk boot $MACH_NAME
    one shot; it reverts by itself
REPROV
}
