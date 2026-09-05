# Boot driver: a Pi 4 whose bench medium the bootloader will not boot (this stick: armed, correct files, enumerating in 1.5 s under Linux, skipped on either bus warm or cold). The bench system's kernel, NODE_DTB, overlays and cmdline are staged into `second/` on the rescue's SD beside a `tryboot.txt` that `reboot "0 tryboot"` makes the firmware read, and the kernel mounts the bench root on NODE_DEVICE by PARTUUID.
# The flag travels as systemd's reboot parameter (/run/systemd/reboot-param; the positional `systemctl reboot ARG` form is deprecated and silently drops it), so the rescue must run systemd.

BOOT_ARMING=medium

B_ARM_FROM_BENCH=no

BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

B_SYSTEM_PARTS="1 3"

TRYBOOT_CMDLINE_SED='/ panic=[0-9]/!s/[[:space:]]*$/ panic=10/'

# **No single quote and no `%`**: spliced into b_self_disarm_sh, which systemd puts inside an `ExecStart=/bin/sh -c '...'`.
_tryboot_sd_sh() {
    local part
    part=$(disk_part "$(disk_of_part "$NODE_ROOT")" 1)
    printf '%s' "wk_sd_own=; \
wk_sd_boot() { \
wk_sd_m=\$(grep -m1 \"^$part \" /proc/mounts | cut -d\" \" -f2); \
if [ -z \"\$wk_sd_m\" ]; then wk_sd_m=\$(mktemp -d); mount -t vfat \${1:-} $part \"\$wk_sd_m\" || return 1; wk_sd_own=1; fi; \
echo \"\$wk_sd_m\"; }; \
wk_sd_drop() { if [ -n \"\$wk_sd_own\" ]; then sync; umount \"\$wk_sd_m\"; rmdir \"\$wk_sd_m\"; wk_sd_own=; fi; }; "
}

# os_prefix applies to the second-stage firmware pair too and a prefix with incomplete files is silently ignored, so start4.elf and fixup4.dat are staged from the SD's own root -- an image's own pair can predate the board (the 2.38 fork's 2020-01 rpi-firmware hangs a Pi 4 rev 1.5 before the kernel).
tryboot_stage_sh() { # <bench boot partition> <dtb name>
    printf '%s' "set -e
$(_tryboot_sd_sh)
boot=\$(wk_sd_boot) || { echo 'cannot reach the SD boot partition to stage onto' >&2; exit 9; }
src=\$(mktemp -d)
mount -o ro $(sh_quote "$1") \"\$src\"
kernel=\$(sed -n 's/^kernel=//p' \"\$src/config.txt\" | tail -1)
if [ -z \"\$kernel\" ]; then
for k in kernel8.img kernel7l.img kernel7.img kernel.img; do
[ -f \"\$src/\$k\" ] || continue
[ -z \"\$kernel\" ] || { echo \"the bench system names no kernel= and its boot partition holds \$kernel and \$k: which one the firmware picks is not this staging's guess to make. Name it with kernel= in the profile's config.txt.append.\" >&2; umount \"\$src\"; exit 9; }
kernel=\$k
done
fi
[ -n \"\$kernel\" ] || { echo 'the bench system config.txt names no kernel= and its boot partition holds none of kernel8.img kernel7l.img kernel7.img kernel.img' >&2; umount \"\$src\"; exit 9; }
rm -rf \"\$boot/second.new\" \"\$boot/second\"
mkdir \"\$boot/second.new\"
cp \"\$src/\$kernel\" \"\$boot/second.new/\$kernel\"
cp \"\$boot/start4.elf\" \"\$boot/fixup4.dat\" \"\$boot/second.new/\"
cp \"\$src/$2\" \"\$boot/second.new/\"
[ -d \"\$src/overlays\" ] && cp -r \"\$src/overlays\" \"\$boot/second.new/overlays\"
sed $(sh_quote "$TRYBOOT_CMDLINE_SED") \"\$src/cmdline.txt\" > \"\$boot/second.new/cmdline.txt\"
# os_prefix leads the file: the firmware resolves a filename as it reads the directive asking for one, so a prefix after a dtoverlay line never reaches that overlay's .dtbo.
bits=''
case \"\$(od -An -tx4 -j36 -N4 \"\$src/\$kernel\" | tr -d ' ')\" in (016f2818) bits=0 ;; esac
[ -n \"\$bits\" ] || case \"\$(od -An -tx4 -j56 -N4 \"\$src/\$kernel\" | tr -d ' ')\" in (644d5241) bits=1 ;; esac
{ echo 'os_prefix=second/'; [ -z \"\$bits\" ] || echo \"arm_64bit=\$bits\"; sed '/^os_prefix=/d; /^arm_64bit=/d' \"\$src/config.txt\"; } > \"\$boot/tryboot.txt.new\"
umount \"\$src\"; rmdir \"\$src\"
mv \"\$boot/second.new\" \"\$boot/second\"
mv \"\$boot/tryboot.txt.new\" \"\$boot/tryboot.txt\"
sync
wk_sd_drop"
}

# This board does not consume the tryboot flag -- a plain reboot and a cold power cycle both read `tryboot.txt` again -- so the boot that spends the staging removes it.
b_self_disarm_sh() {
    printf "%s" "$(_tryboot_sd_sh)boot=\$(wk_sd_boot) || exit 0; \
if [ -f \"\$boot/tryboot.txt\" ]; then rm -f \"\$boot/tryboot.txt\"; rm -rf \"\$boot/second\"; \
echo \"wk-self-disarm: this boot spent the tryboot staging; the rescue boots next\"; fi; \
wk_sd_drop"
}

TRYBOOT_ARMED=""

b_arm() {
    [ -n "${ARM_SYS_PART:-}" ] \
        || die "b_arm needs the selected boot partition (machine_select_system, cmd/boot)"
    r_ssh "$(tryboot_stage_sh "$ARM_SYS_PART" "$NODE_DTB")" \
        || die "could not stage the tryboot files on $NODE_NAME.
    Arming copies the selected system's kernel out of $NODE_DEVICE's boot
    partition onto the SD, so the board has to answer -- as its rescue or as a
    bench system, either will do -- and both media have to be readable there."
    TRYBOOT_ARMED=1
}

b_disarm() {
    r_ssh "$(_tryboot_sd_sh)
    boot=\$(wk_sd_boot) || exit 0
    rm -rf \"\$boot/second\" \"\$boot/tryboot.txt\"
    wk_sd_drop" >/dev/null 2>&1 || return 0
}

b_disarm_note() {
    log "  the staged second/ and tryboot.txt are gone from the SD's boot partition;"
    log "  the tryboot flag itself is the firmware's and any boot clears it."
}

b_reboot() {
    if [ -n "$TRYBOOT_ARMED" ]; then
        b_reboot_tryboot
    else
            boot_priv reboot >/dev/null
    fi
}

b_evidence() {
    printf 'lane=kernel from the SD via tryboot (one boot, firmware-reverting); bench root on %s\n' "$NODE_DEVICE"
    local staged systems source
    staged=$(r_ssh "$(_tryboot_sd_sh)
        boot=\$(wk_sd_boot \"-o ro\") || exit 0
        if [ -f \"\$boot/tryboot.txt\" ] && [ -d \"\$boot/second\" ]; then echo yes; else echo no; fi
        wk_sd_drop" 2>/dev/null | tr -d '\r' | head -1) || staged=""

    source=$(r_ssh "$(_tryboot_sd_sh)
        wk_root_of() { tr \" \" \"\\n\" < \"\$1\" | grep \"^root=\" | head -1; }
        r=\$(wk_root_of /proc/cmdline)
        boot=\$(wk_sd_boot \"-o ro\") || { echo unknown; exit 0; }
        sd=\$(wk_root_of \"\$boot/cmdline.txt\" 2>/dev/null)
        st=\$(wk_root_of \"\$boot/second/cmdline.txt\" 2>/dev/null)
        if [ -n \"\$st\" ] && [ \"\$r\" = \"\$st\" ]; then echo staging
        elif [ -n \"\$sd\" ] && [ \"\$r\" = \"\$sd\" ]; then echo sd-config
        else echo unknown; fi
        wk_sd_drop" 2>/dev/null | tr -d '\r' | head -1) || source=""
    case "${source:-}" in
        staging)   printf 'boot_source=the tryboot staging now on the SD (second/), so this boot spent this arming\n' ;;
        sd-config) printf 'boot_source=the SD config.txt, the plain path\n' ;;
        unknown)   printf 'boot_source=neither the SD config.txt nor the staging now on the SD -- this
  boot came from an earlier staging, so the last arming either did not reboot
  the board or the firmware did not consume its flag. What is staged now is
  what the next boot would use, not what is running.\n' ;;
        *)         printf 'boot_source=unreadable (the board did not answer the probe)\n' ;;
    esac
    printf 'tryboot_staged=%s\n' "${staged:-unreadable}"
    systems=$(b_systems 2>/dev/null) || systems=""
    [ -z "$systems" ] || printf '%s\n' "$systems" | awk '{ printf "system=%s (on %s)\n", $2, $1 }'
}

b_media() {
    printf '%s holds the bench system(s): root on 1-2 and, when written, a second on 3-4; the armed kernel is tryboot-staged onto the SD, which also carries the rescue' \
        "$NODE_DEVICE"
}

b_reprovision() {
    cat <<REPROV
wk sysimage build $NODE_PROFILE
    in a workspace; hours
wk sysimage write --from <path> --disk <reader>:$(disk_of_part "$NODE_ROOT") --rescue --profile $NODE_PROFILE
    the SD card -- the system this board falls back to, and the firmware's boot medium
wk pi boot-order $NODE_NAME sd-first
    the SD first: the bench medium is mounted by the kernel, never firmware-booted
wk sysimage write --from <path> --disk $NODE_NAME:$NODE_DEVICE --profile <bench profile>
    the bench system's root medium, written from the rescue
wk sysimage write --from <path> --disk $NODE_NAME:$NODE_DEVICE@second --profile <bench profile>
    optional: a second system beside the first (partitions 3-4), for an
    A/B across images; 'wk boot $NODE_NAME --system <id>' picks one
wk boot $NODE_NAME
    one shot; the firmware reverts by itself
REPROV
}
