# Boot driver: a board whose only boot medium is its SD card, hands-on today.
#
# This driver exists because nothing here can arm the rpi3 *yet* -- not because
# the board cannot be armed. The arrangement it is heading for (one card holding
# a rescue and a bench system, selected by `root=`, armed in stage 2) is in
# `wk help hardware`; what is left to build is in docs/HANDOFF-boot.md.
#
# Until that lands, the lane is a system on local media, written by
# `wk sysimage write` and booted by putting the card in the slot -- a person's
# job, which is what `hands-on` means here: `--status` works from the shared
# probe, and arming refuses with the actual instructions rather than pretending a
# register exists to write.
BOOT_ARMING=hands-on

b_arm() {
    die "$MACH_NAME boots only its SD card, and swapping what the card holds is
    hands-on today. Write the system to a card and move the card:

        wk sysimage write <id> --disk <machine>:<device>   (from another machine
                                                            with a reader)

    Whether that stays true is an open question rather than a property of the
    board: it turns on the USB-boot OTP fuse, which this repo assumed unburned
    and which may already be blown. On a board that is up:

        cat /proc/device-tree/model
        vcgencmd otp_dump | grep '^17:'

    docs/HANDOFF-boot.md, 'The rpi3', has what follows from each answer."
}

b_evidence() {
    # The lane, not a guess about the fuse: this board is armed in stage 2, so
    # the USB-boot OTP bit governs nothing here (`wk help hardware`). Nothing has
    # ever read it on this board, and a line of evidence that is really a belief
    # is the worst kind to print beside real ones.
    echo "lane=local SD media, both systems on one card (stage-2 arming)"
    return 0
}

# The wk-managed media, in one line, for the fleet block in `wk status`.
b_media() {
    printf 'SD card %s holds both systems by design (wk help hardware); card swaps hands-on until stage-2 arming lands' "$MACH_DEVICE"
}
