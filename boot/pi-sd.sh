# Boot driver: a board with one medium, its SD card, holding both systems --
# the rescue on partitions 1-2 and the bench system on 3-4 (`<device>@second`,
# admin/wk-card-priv). The rpi3's Ethernet is a USB device, so a bench root on
# a stick would share a bus with the traffic the benchmark generates; one card
# it is, and the two systems are told apart by partition (b_system_kind:
# MACH_ROOT is the rescue's root, anything else on MACH_DEVICE is the bench).
#
# The firmware boots the first FAT partition and only that one, so arming is
# an edit of the rescue's boot partition: the bench system's kernel, device
# trees and cmdline are copied into `second/` there and selected with one
# `os_prefix=second/` line in config.txt, the rescue's own config.txt kept as
# config.txt.rescue (`second-arm`). One boot: the bench system's self-disarm
# (b_self_disarm_sh, staged onto the card by `wk sysimage write`) moves the
# rescue's config.txt back as the first thing it does, and `second-disarm`
# from the rescue does the same. Both run through the card helper on the
# rescue, so the rescue has to be up to arm -- which is the state a board is
# in whenever it is not being measured.
#
# The failure modes, and where each ends:
#   bench kernel cannot find its root  -> panic; a power cycle boots the same
#                                          os_prefix again until the rescue's
#                                          config.txt is put back by hand
#                                          (TODO: docs/HANDOFF-boot.md, the
#                                          stage-2 revert)
#   bench system hangs after init      -> the self-return watchdog reboots it;
#                                          the self-disarm already ran, so
#                                          that boot is the rescue's
#   rescue unreachable                 -> nothing to arm from; `wk boot` says so
BOOT_ARMING=medium

# Unused: nothing here is a per-boot firmware order.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# The card, as the helper addresses the bench system on it.
pisd_second() { printf '%s@second' "$MACH_DEVICE"; }

# The one place the rescue is asked about the arming: armed=yes|no from
# whether its config.txt has stepped aside, present=yes|no from whether a
# fourth partition exists at all.
pisd_state() {
    card_priv second-state "$(pisd_second)" 2>/dev/null | tr -d '\r'
}

b_arm() {
    local state
    state=$(pisd_state) || die "could not read $MACH_DEVICE's arming on $MACH_NAME.
    Arming this board is an edit of its rescue's boot partition, made by the
    card helper on the rescue, so the rescue has to be up and carry the helper."
    case "$state" in
        *present=yes*) ;;
        *) die "$MACH_DEVICE on $MACH_NAME holds no bench system beside the rescue.
    Write one first:  wk sysimage write --from <path> --disk <reader>:$MACH_DEVICE@second" ;;
    esac
    case "$state" in
        *armed=yes*) debug "$MACH_NAME is already armed"; return 0 ;;
    esac
    card_priv second-arm "$(pisd_second)" >/dev/null \
        || die "could not arm the bench system on $MACH_NAME"
}

# Safe at any time, including a board not armed -- the common case.
b_disarm() {
    local state
    state=$(pisd_state) || return 0
    case "$state" in *armed=yes*) ;; *) return 0 ;; esac
    card_priv second-disarm "$(pisd_second)" >/dev/null \
        || die "could not disarm the bench system on $MACH_NAME"
}

b_disarm_note() {
    log "  the rescue's config.txt is back on $MACH_DEVICE, so the firmware boots the"
    log "  rescue's kernel again. 'wk boot $MACH_NAME' arms the bench system once more."
}

# The half that runs *inside* the bench system, first thing after its root is
# up, as a systemd unit or a BusyBox init script (`wk sysimage write` stages
# whichever the image runs). The boot partition is partition 1 of the disk
# this root is on; the root is named by PARTUUID (disk_retarget_root), which
# is why this waits for nothing but udev's /dev/disk links.
# **No single quote and no `%` may appear in what this returns**:
# interpolated into a systemd `ExecStart=/bin/sh -c '...'`, a quote would
# hand systemd three fragments instead of one command, and systemd expands
# `%` specifiers before it parses quotes. wk selftest asserts both.
b_self_disarm_sh() {
    printf "%s" "r=\$(sed -n \"/root=PARTUUID=/{s/.*root=PARTUUID=//;s/ .*//;p;}\" /proc/cmdline); \
[ -n \"\$r\" ] || { echo \"wk-self-disarm: root is not named by PARTUUID; cannot find the boot partition\"; exit 0; }; \
d=\$(readlink -f /dev/disk/by-partuuid/\$r); b=\$(echo \"\$d\" | sed \"s/p[0-9]*\$/p1/\"); \
m=\$(mktemp -d); if mount -t vfat \"\$b\" \"\$m\"; then \
[ -f \"\$m/config.txt.rescue\" ] && mv -f \"\$m/config.txt.rescue\" \"\$m/config.txt\" && echo \"wk-self-disarm: this boot was the one; the rescue boots next\"; \
sync; umount \"\$m\"; fi; rmdir \"\$m\""
}

# Evidence from the card, not the record: is the rescue's config.txt stepped
# aside, and is there a bench system to step aside for.
b_evidence() {
    local state
    state=$(pisd_state) || { echo "arming=unreadable (the rescue did not answer)"; return 0; }
    printf 'lane=one SD card, rescue on partitions 1-2, bench system on 3-4 (os_prefix arming)\n'
    printf '%s\n' "$state" | sed -n 's/^wk-card-priv: \(armed=.*\)$/\1/p; s/^wk-card-priv: \(present=.*\)$/bench_system=\1/p' \
        | sed 's/bench_system=present=/bench_system_present=/'
    return 0
}

# The bench system's boot partition is the third on the card; its identity
# (wk-image.id) and diagnostics are read there.
b_boot_part() { disk_part "$MACH_DEVICE" 3; }

# The wk-managed media, in one line, for the fleet block in `wk status`.
b_media() {
    printf 'SD card %s holds both systems: rescue on p1-p2, bench system on p3-p4 (wk boot %s arms it for one boot)' \
        "$MACH_DEVICE" "$MACH_NAME"
}

# How this board is made from nothing, derived rather than written down.
#
# Every line is composed from what this machine's conf already declares -- its
# profile, its device, its driver -- so there is no second copy to go stale when
# one of them changes.
#
# One medium, so the two systems share it and the rescue is written without
# growing: what is left of the card is where the bench system goes. Both writes
# can be made from a reader on any machine with the card helper; once the
# rescue is on the board, every later bench write is made from the rescue
# itself (`--disk $MACH_NAME:$MACH_DEVICE@second`).
b_reprovision() {
    cat <<REPROV
wk sysimage build $MACH_PROFILE
    in a workspace; hours
wk sysimage write --from <path> --disk <reader>:$MACH_DEVICE --rescue --profile $MACH_PROFILE
    no --grow: the rest of the card is where the bench system goes
wk sysimage write --from <path> --disk <reader>:$MACH_DEVICE@second --profile <bench profile>
    the bench system, into partitions 3 and 4 beside the rescue
    then carry the card to $MACH_NAME and power it on
wk boot $MACH_NAME
REPROV
}
