# Boot driver: one SD card holds every system, the rescue on partitions 1-2. The firmware boots the first FAT
# partition and only that one, so arming is an `os_prefix=second/` line the card helper writes into the rescue's config.txt.

BOOT_ARMING=medium

B_ARM_FROM_BENCH=yes

BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

B_SYSTEM_PARTS="3 5 7"   # 3: the one-system layout's pair; 5 and 7: the pairs a shared layout keeps inside an extended partition 3.

pisd_addr() { # <boot partition>
    case "$1" in
        *[!0-9]3|*[!0-9]5) printf '%s@second' "$NODE_DEVICE" ;;
        *[!0-9]7)          printf '%s@third'  "$NODE_DEVICE" ;;
        *) die "'$1' is not a bench system's boot partition on $NODE_DEVICE" ;;
    esac
}

pisd_state() { # <address>
    card_priv second-state "$1" 2>/dev/null | tr -d '\r'
}

b_arm() {
    local addr state
    [ -n "${ARM_SYS_PART:-}" ] \
        || die "b_arm needs the selected boot partition (machine_select_system, cmd/boot)"
    addr=$(pisd_addr "$ARM_SYS_PART")
    state=$(pisd_state "$addr") || die "could not read $NODE_DEVICE's arming on $NODE_NAME.
    Arming this board is an edit of its rescue's boot partition, made by the
    card helper on the rescue, so the rescue has to be up and carry the helper."
    case "$state" in
        *present=yes*) ;;
        *) die "$NODE_DEVICE on $NODE_NAME holds no bench system at ${addr##*/}.
    Write one first:  wk sysimage write --from <path> --disk <reader>:$addr" ;;
    esac
    case "$state" in
        *"armed_prefix=${addr##*@}"*) debug "$NODE_NAME is already armed for ${addr##*@}"; return 0 ;;
    esac
    card_priv second-arm "$addr" >/dev/null \
        || die "could not arm the ${addr##*@} system on $NODE_NAME"
}

b_disarm() {
    local state
    state=$(pisd_state "$NODE_DEVICE@second") || return 0
    case "$state" in *armed=yes*) ;; *) return 0 ;; esac
    card_priv second-disarm "$NODE_DEVICE@second" >/dev/null \
        || die "could not disarm the bench system on $NODE_NAME"
}

b_disarm_note() {
    log "  the rescue's config.txt is back on $NODE_DEVICE, so the firmware boots the"
    log "  rescue's kernel again. 'wk boot $NODE_NAME' arms the bench system once more."
}

# Interpolated into a systemd `ExecStart=/bin/sh -c '...'`: no single quote (systemd would read three fragments), no `%` (expanded before quotes are).
b_self_disarm_sh() {
    printf "%s" "r=\$(sed -n \"/root=PARTUUID=/{s/.*root=PARTUUID=//;s/ .*//;p;}\" /proc/cmdline); \
[ -n \"\$r\" ] || { echo \"wk-self-disarm: root is not named by PARTUUID; cannot find the boot partition\"; exit 0; }; \
d=\$(readlink -f /dev/disk/by-partuuid/\$r); b=\$(echo \"\$d\" | sed \"s/p[0-9]*\$/p1/\"); \
m=\$(mktemp -d); if mount -t vfat \"\$b\" \"\$m\"; then \
[ -f \"\$m/config.txt.rescue\" ] && mv -f \"\$m/config.txt.rescue\" \"\$m/config.txt\" && echo \"wk-self-disarm: this boot was the one; the rescue boots next\"; \
sync; umount \"\$m\"; fi; rmdir \"\$m\""
}

b_evidence() {
    local state systems
    state=$(pisd_state "$NODE_DEVICE@second") || { echo "arming=unreadable (the rescue did not answer)"; return 0; }
    printf 'lane=one SD card, rescue on partitions 1-2, bench system(s) beside it (os_prefix arming)\n'
    printf '%s\n' "$state" | sed -n 's/^wk-card-priv: \(armed=.*\)$/\1/p'
    systems=$(b_systems 2>/dev/null) || systems=""
    [ -z "$systems" ] || printf '%s\n' "$systems" | awk '{ printf "system=%s (on %s)\n", $2, $1 }'
    return 0
}

b_boot_part() { disk_part "$NODE_DEVICE" 3; }

b_media() {
    printf 'SD card %s holds every system: rescue on p1-p2, bench system(s) beside it -- p3-p4, or pairs 5-6 and 7-8 in an extended p3 (wk boot %s --system <id> arms one for one boot)' \
        "$NODE_DEVICE" "$NODE_NAME"
}

b_reprovision() {
    cat <<REPROV
wk sysimage build $NODE_PROFILE
    in a workspace; hours
wk sysimage write --from <path> --disk <reader>:$NODE_DEVICE --rescue --profile $NODE_PROFILE
    no --grow: the rest of the card is where the bench system goes
wk sysimage write --from <path> --disk <reader>:$NODE_DEVICE@second --profile <bench profile>
    the first bench system beside the rescue
wk sysimage write --from <path> --disk <reader>:$NODE_DEVICE@third --profile <bench profile>
    optional: a second bench system (the shared layout holds two), for an
    A/B across images; 'wk boot $NODE_NAME --system <id>' picks one
    then carry the card to $NODE_NAME and power it on
wk boot $NODE_NAME
REPROV
}
