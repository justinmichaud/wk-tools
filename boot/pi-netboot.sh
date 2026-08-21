# Boot driver: a Raspberry Pi that netboots, armed from the server side.
#
# The Pi 4 and Pi 3 have no one-shot. `set_reboot_order` through the firmware
# mailbox -- the whole basis of the rpi5's driver -- is Raspberry Pi 5 only.
# That sounds like a downgrade and is not, because these boards are *dedicated
# test devices* rather than workstations, so they do not need one-shot
# semantics at all:
#
#   BOOT_ORDER has network FIRST, permanently (wk pi netboot-enable writes it),
#   and arming becomes an act of the server:
#
#     serve the image  -> the board's next boot is the image
#     empty the root   -> the board falls through to its local disk
#
# Which is strictly better than what the rpi5 gets: no access to the device is
# needed to arm a run, so a board that is wedged, unreachable, or simply off
# can still be pointed at a different image. The failure mode is benign too --
# the firmware's network attempt times out and the local image boots, which is
# the same fall-through the rpi5's one-shot relies on.
#
# UNVERIFIED against hardware as of 2026-08-19: the rpi4 is powered off. Two
# facts have to be checked on the board before this is believed -- its EEPROM
# date (network and USB boot both want a reasonably recent bootloader) and
# whether it has a spare boot device at all. `b_arm` checks what it can.

BOOT_ARMING=server

# Not used by this driver -- the boot order is permanent here, not per-boot --
# but named so that a reader comparing the two drivers sees the difference
# rather than an omission.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

b_arm() {
    local served
    served=$(sed -n 's/^image=//p' "$WK_STORE/serve/serve.status" 2>/dev/null | head -1)

    [ -n "$served" ] || die "nothing is being served from this machine, so there is
    nothing for $MACH_NAME to boot: its arming *is* the server's content.
    Start it first:  wk serve --system <id>"

    [ "$served" = "$IMAGE" ] || die "this machine is serving $served, not $IMAGE.
    A netboot client boots what the server holds, so change the server rather
    than the client:  wk serve --system $IMAGE"

    # The firmware has to be pointed at a server, and that is EEPROM state
    # rather than something this command can set per-boot.
    #
    # Read with vcgencmd, not rpi-eeprom-config: the tool is a Raspberry Pi OS
    # package and these boards do not run one, so asking it returned nothing on
    # the fleet's rpi4 and fell into the warn branch below -- which is the arm
    # reporting "assuming it netboots" about the very board whose BOOT_ORDER it
    # exists to check. vcgencmd is firmware, so it answers on any Pi.
    local order
    order=$(m_ssh "$EEPROM_CONFIG_CMD" | sed -n 's/^BOOT_ORDER=//p' | head -1) || order=""
    case "$order" in
        *2*) ;;
        '')  warn "could not read $MACH_NAME's BOOT_ORDER; assuming it netboots" ;;
        *)   die "$MACH_NAME's BOOT_ORDER is $order, which has no network entry, so
    it will never ask this server for anything.
    Fix it once:  wk pi netboot-enable $MACH_NAME" ;;
    esac
}

# Disarming is emptying the server, not touching the board. Deliberately not
# `wk serve --stop`: another machine may be booting from the same server, and
# one client's disarm must not take the service down for the rest.
b_disarm_note() {
    log "  $MACH_NAME netboots whatever this machine serves; 'wk serve --stop'"
    log "  is what makes it fall through to its local disk."
}

b_evidence() {
    m_ssh "$EEPROM_CONFIG_CMD" \
        | sed -n 's/^BOOT_ORDER=/eeprom_boot_order=/p;s/^TFTP_IP=/eeprom_tftp_ip=/p' || true
}
