# Boot driver: a board whose only boot medium is its SD card, hands-on today.
#
# The rpi3 cannot boot USB or the network without burning a one-way OTP bit,
# and the netboot root was decided against outright (2026-08-21, the netboot
# handoff) -- so its bench lane is the same as everyone else's: a system on
# local media, written by `wk sysimage write` (or, once it exists, `wk
# sysimage flash --reader`) and booted by putting the card in the slot. That
# transition is a person's until the board is provisioned, which is why the
# arming model is hands-on: `--status` works from the shared probe, and
# arming refuses with the actual instructions rather than pretending a
# register exists to write.
BOOT_ARMING=hands-on

b_arm() {
    die "$MACH_NAME boots only its SD card, and swapping what the card holds is
    hands-on today. Write the system to a card and move the card:

        wk sysimage write <id> --disk <machine>:<device>   (from another machine
                                                            with a reader)

    The board is also not provisioned yet -- docs/HANDOFF-netboot.md, 'The
    rpi3', has the order of operations, and the OTP door stays shut."
}

b_evidence() {
    echo "lane=local SD media (netboot dropped 2026-08-21; OTP unburned)"
    return 0
}

# The wk-managed media, in one line, for the fleet block in `wk status`.
b_media() {
    printf 'SD card %s is the only medium (hands-on card swap; OTP unburned)' "$MACH_DEVICE"
}
