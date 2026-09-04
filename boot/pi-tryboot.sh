# Boot driver: a Pi 4 whose bench medium the bootloader will not boot, armed
# through the firmware's tryboot one-shot instead. The firmware's USB-MSD boot
# rejects some devices outright (the rpi4's stick: armed, correct files,
# enumerates in 1.5 s under Linux -- skipped on either bus, warm or cold), so
# the firmware loads the kernel from the rescue's SD card and the kernel mounts
# the bench root on NODE_DEVICE by PARTUUID.
#
# Arming stages the bench system's kernel, NODE_DTB, overlays and cmdline into
# `second/` on the SD's boot partition and writes `tryboot.txt` (that system's
# config.txt plus one `os_prefix=second/` line) beside the rescue's untouched
# config.txt; `reboot "0 tryboot"` makes the firmware read tryboot.txt exactly
# once and clear the flag, whatever happens next, so the fall-through is
# firmware-enforced and there is no state to put back. The argument travels as
# systemd's reboot parameter (/run/systemd/reboot-param, written directly --
# the positional `systemctl reboot ARG` form is deprecated and drops it
# silently on some versions), so the rescue must run systemd.

BOOT_ARMING=medium

# A BusyBox bench system can copy the staging but cannot pass the flag, so it
# would stage perfectly and reboot without it, losing the leg.
B_ARM_FROM_BENCH=no

# Unused: the boot order here is permanent, not per-boot. sd-first is the right
# EEPROM order (`wk pi boot-order`); usb-first wastes the discovery window on a
# medium the firmware rejects.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# The medium can hold a second system on partitions 3-4 (`wk sysimage write
# --disk <machine>:$NODE_DEVICE@second`). Arming stages from the selected
# system's own boot partition -- its kernel, its cmdline, its root PARTUUID.
B_SYSTEM_PARTS="1 3"

# Appends panic=10 to a staged cmdline that sets no panic of its own: without
# it a panicking kernel hangs instead of rebooting, and the hang is the one
# failure tryboot's firmware revert cannot see past.
TRYBOOT_CMDLINE_SED='/ panic=[0-9]/!s/[[:space:]]*$/ panic=10/'

# `wk_sd_boot` prints the SD boot partition's mountpoint, mounting it if
# nothing has; `wk_sd_drop` undoes only a mount it made. The partition is named
# from the conf (partition 1 of the rescue root's disk), not found by content:
# a bench system's own /boot/firmware is the medium's FAT and carries
# start4.elf too.
# **No single quote and no `%`**: spliced into b_self_disarm_sh, which systemd
# puts inside an `ExecStart=/bin/sh -c '...'`. wk selftest asserts both.
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

# The staging, run on the rescue as one script. The second-stage firmware pair
# (start4.elf, fixup4.dat) has to sit under the prefix too -- os_prefix applies
# to it, and a prefix with incomplete files is silently ignored -- but is
# copied from the *SD's own root*: an image's own pair can predate the board
# (the 2.38 fork pins rpi-firmware from 2020-01, which cannot bring up a Pi 4
# rev 1.5 -- it hangs before the kernel, the one failure the tryboot revert
# cannot see past). Built into `second.new` and renamed, so a kill mid-arm
# leaves the previous staging whole.
#
# Which file is the kernel is the firmware's own name resolution: a `kernel=`
# line names it, and without one the firmware picks from its defaults by what
# is present. With several the choice follows from arm_64bit and the board, so
# this refuses instead.
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
# os_prefix leads the file: the firmware resolves a filename when it reads the
# directive that asks for one, so a prefix after a dtoverlay line never reaches
# that overlay's .dtbo, and one appended at the end lands inside whichever
# conditional section the image closes with.
#
# arm_64bit, stated explicitly from the kernel's own magic (zImage: 0x016f2818
# at offset 36; ARM64 Image: 'ARM\x64' at 56): the SD's firmware defaults to
# 64-bit, and jumping into a 32-bit zImage as if it were an arm64 Image is a
# silent hang before any kernel code, no diag, no watchdog.
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

# Runs inside the bench system, first thing after its root is up. This board
# does not consume the tryboot flag -- a plain reboot and a cold power cycle
# both read `tryboot.txt` again -- so "one boot" is made true by the boot that
# spends it removing the staging.
# **No single quote and no `%` may appear in what this returns**: interpolated
# into a systemd `ExecStart=/bin/sh -c '...'`. wk selftest asserts both.
b_self_disarm_sh() {
    printf "%s" "$(_tryboot_sd_sh)boot=\$(wk_sd_boot) || exit 0; \
if [ -f \"\$boot/tryboot.txt\" ]; then rm -f \"\$boot/tryboot.txt\"; rm -rf \"\$boot/second\"; \
echo \"wk-self-disarm: this boot spent the tryboot staging; the rescue boots next\"; fi; \
wk_sd_drop"
}

# Set by b_arm and read by b_reboot in the same cmd_arm breath: only the reboot
# that follows an arming carries the tryboot flag.
TRYBOOT_ARMED=""

# ARM_SYS_PART is required rather than defaulted: an arm that guessed the
# partition could boot the system beside the one recorded.
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

# Nothing here can clear the flag; removing the staging is the disarm. Over
# whichever channel answered rather than the rescue's alone: while the staging
# is in force this board boots the *bench* system.
# /proc/mounts and not findmnt: a BusyBox bench system may carry no findmnt.
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

# A plain reboot carries no flag, reads the rescue's config.txt, and so is
# itself the disarm.
b_reboot() {
    if [ -n "$TRYBOOT_ARMED" ]; then
        b_reboot_tryboot
    else
            boot_priv reboot >/dev/null
    fi
}

# The flag itself is write-only and spent by any boot, so it is never evidence.
b_evidence() {
    printf 'lane=kernel from the SD via tryboot (one boot, firmware-reverting); bench root on %s\n' "$NODE_DEVICE"
    local staged systems source
    staged=$(r_ssh "$(_tryboot_sd_sh)
        boot=\$(wk_sd_boot \"-o ro\") || exit 0
        if [ -f \"\$boot/tryboot.txt\" ] && [ -d \"\$boot/second\" ]; then echo yes; else echo no; fi
        wk_sd_drop" 2>/dev/null | tr -d '\r' | head -1) || staged=""

    # Which config the running boot came from: the running root= against the
    # two cmdlines on the SD. `tr` and `grep`, not sed: a sed script's
    # backslashes would have to survive this string, the ssh command line and
    # the remote shell all at once.
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
    # "unknown": the running cmdline is neither the SD's nor what is staged
    # now, so the board runs a staging that has since been replaced.
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
