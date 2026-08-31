# Boot driver: a Pi 4 whose bench medium the bootloader will not boot, armed
# through the firmware's tryboot one-shot instead.
#
# The firmware's USB-MSD boot rejects some devices outright (the rpi4's stick:
# armed, correct files, enumerates in 1.5 s under Linux -- skipped on either
# bus, warm or cold). The kernel has no such problem, so the two jobs are
# split: the *firmware* loads the kernel from the rescue's SD card, which it
# provably boots, and the *kernel* mounts the bench root on MACH_DEVICE by
# PARTUUID. The bench medium stays the measured system's storage; only the
# few MB the firmware reads leave it.
#
# Arming stages those files -- the bench system's kernel, MACH_DTB, overlays
# and cmdline into `second/` on the SD's boot partition, and `tryboot.txt`
# (its config.txt plus one `os_prefix=second/` line) beside the rescue's
# untouched config.txt -- and the one shot is the reboot itself:
# `reboot "0 tryboot"` makes the firmware read tryboot.txt exactly once and
# clear the flag, whatever happens next. That is a *firmware-enforced*
# fall-through, so unlike pi-sd's os_prefix edit there is no state to put
# back and no self-disarm to stage: any reboot, panic (the staged cmdline
# carries panic=10) or watchdog return boots the rescue's own config.txt.
#
# The failure modes, all of which end somewhere reachable:
#   medium not armed        -> no tryboot flag; the SD boots the rescue
#   image kernel missing    -> refused after the write, before anything arms it
#   image kernel panics     -> panic=10 reboots; the flag is spent, so the rescue
#   image hangs after boot  -> the self-return watchdog reboots it; the rescue
#   staged files stale      -> b_arm re-stages from the medium on every arm
#
# The rescue must run systemd: the reboot argument travels as systemd's
# reboot parameter (/run/systemd/reboot-param, what `systemctl reboot ARG`
# itself writes -- but written directly, since the positional form is
# deprecated and drops the argument silently on some versions), and
# systemd-shutdown hands it to the reboot syscall. The fleet's yocto rescues
# run systemd.

BOOT_ARMING=medium

# Unused: the boot order here is permanent, not per-boot. The SD card is the
# boot authority, so the right EEPROM order for this driver is sd-first
# (`wk pi boot-order`); usb-first merely wastes the discovery window on a
# medium the firmware rejects.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# The bench medium's own boot partition still holds the system's identity
# (wk-image.id) and diagnostics; only the *reading* of the kernel moved.
# b_boot_part's default (partition 1 of MACH_DEVICE) is already that.

# The medium can hold a second system on partitions 3-4 (`wk sysimage write
# --disk <machine>:$MACH_DEVICE@second`, the same shape as the rpi3's card):
# two releases with two library stacks, resident side by side, so an A/B
# across *images* is an arming choice rather than a rewrite. Arming stages
# from the selected system's own boot partition -- its kernel, its cmdline,
# its root PARTUUID -- so which system boots is decided entirely by
# `wk boot <machine> --system <id>`.
B_SYSTEM_PARTS="1 3"

# Appends panic=10 to a staged cmdline that sets no panic of its own: without
# it a panicking kernel hangs instead of rebooting, and the hang is the one
# failure tryboot's firmware revert cannot see past.
TRYBOOT_CMDLINE_SED='/ panic=[0-9]/!s/[[:space:]]*$/ panic=10/'

# Where the firmware reads files from on the rescue: the mounted FAT holding
# the Pi 4 second-stage firmware, asked of /proc/mounts (eeprom_bootfs makes
# the same probe for the same reason -- an unrelated FAT volume must not win).
_tryboot_bootfs_sh='for m in $(awk "\$3 == \"vfat\" { print \$2 }" /proc/mounts); do [ -f "$m/start4.elf" ] && printf %s "$m" && exit 0; done; exit 9'

# The staging, run on the rescue as one script. The OS files -- kernel,
# device tree, overlays, cmdline -- are copied out of the bench medium's own
# boot partition, so the staged set cannot drift from what `wk sysimage
# write` put there; re-staged on every arm, never trusted from last time.
# The second-stage firmware pair (start4.elf, fixup4.dat) also has to sit
# under the prefix -- os_prefix applies to it, and a prefix with incomplete
# files is silently ignored, which boots the rescue and reads exactly like a
# failed arm -- but it is copied from the *SD's own root*, not the medium:
# the SD is the boot authority, its pair is the one proven on this exact
# board every day, and an image's own pair can predate the board (the 2.38
# fork pins rpi-firmware from 2020-01, which cannot bring up a Pi 4 rev 1.5
# -- it hangs before the kernel, the one failure the tryboot revert cannot
# see past). Built fresh into `second.new` and
# renamed into place, so a kill mid-arm leaves the previous staging whole.
tryboot_stage_sh() { # <bench boot partition> <dtb name>
    printf '%s' "set -e
boot=\$($_tryboot_bootfs_sh) || { echo 'no mounted FAT with start4.elf on the rescue' >&2; exit 9; }
src=\$(mktemp -d)
mount -o ro $(sh_quote "$1") \"\$src\"
kernel=\$(sed -n 's/^kernel=//p' \"\$src/config.txt\" | tail -1)
[ -n \"\$kernel\" ] || { echo 'the bench system config.txt names no kernel=' >&2; umount \"\$src\"; exit 9; }
rm -rf \"\$boot/second.new\" \"\$boot/second\"
mkdir \"\$boot/second.new\"
cp \"\$src/\$kernel\" \"\$boot/second.new/\$kernel\"
cp \"\$boot/start4.elf\" \"\$boot/fixup4.dat\" \"\$boot/second.new/\"
cp \"\$src/$2\" \"\$boot/second.new/\"
[ -d \"\$src/overlays\" ] && cp -r \"\$src/overlays\" \"\$boot/second.new/overlays\"
sed $(sh_quote "$TRYBOOT_CMDLINE_SED") \"\$src/cmdline.txt\" > \"\$boot/second.new/cmdline.txt\"
# arm_64bit, stated explicitly from the kernel's own magic (zImage:
# 0x016f2818 at offset 36; ARM64 Image: 'ARM\x64' at 56): the SD's modern
# firmware defaults to 64-bit, and a config.txt that only says kernel=zImage
# -- the fork's do -- makes it jump into a 32-bit zImage as if it were an
# arm64 Image: a silent hang before any kernel code, no diag, no watchdog.
bits=''
case \"\$(od -An -tx4 -j36 -N4 \"\$src/\$kernel\" | tr -d ' ')\" in (016f2818) bits=0 ;; esac
[ -n \"\$bits\" ] || case \"\$(od -An -tx4 -j56 -N4 \"\$src/\$kernel\" | tr -d ' ')\" in (644d5241) bits=1 ;; esac
{ sed '/^os_prefix=/d; /^arm_64bit=/d' \"\$src/config.txt\"; echo 'os_prefix=second/'; [ -n \"\$bits\" ] && echo \"arm_64bit=\$bits\"; true; } > \"\$boot/tryboot.txt.new\"
umount \"\$src\"; rmdir \"\$src\"
mv \"\$boot/second.new\" \"\$boot/second\"
mv \"\$boot/tryboot.txt.new\" \"\$boot/tryboot.txt\"
sync"
}

# Set by b_arm and read by b_reboot in the same cmd_arm breath: only the
# reboot that follows an arming carries the tryboot flag. `wk boot --back`
# and every other reboot stay plain, which on this driver *is* the disarm.
TRYBOOT_ARMED=""

# Stages from the *selected* system's boot partition: cmd/boot's arming sets
# ARM_SYS_PART from machine_select_system, so a medium holding two systems
# arms the one that was named. Required rather than defaulted -- an arm that
# guessed the partition could boot the system beside the one recorded.
b_arm() {
    [ -n "${ARM_SYS_PART:-}" ] \
        || die "b_arm needs the selected boot partition (machine_select_system, cmd/boot)"
    m_ssh "$(tryboot_stage_sh "$ARM_SYS_PART" "$MACH_DTB")" \
        || die "could not stage the tryboot files on $MACH_NAME's rescue.
    Arming copies the bench system's kernel out of $MACH_DEVICE's boot
    partition onto the SD, so the rescue has to be up and the medium readable."
    TRYBOOT_ARMED=1
}

# The staged files are inert without the flag, and the flag lives in the
# firmware for at most the seconds between b_arm and the reboot -- nothing
# here can clear it. Removing the staging is still the honest disarm: what
# `wk boot --status` reads as armed evidence goes away with it.
b_disarm() {
    m_ssh "boot=\$($_tryboot_bootfs_sh) && rm -rf \"\$boot/second\" \"\$boot/tryboot.txt\" && sync" \
        >/dev/null 2>&1 || return 0
}

b_disarm_note() {
    log "  the staged second/ and tryboot.txt are gone from the SD's boot partition;"
    log "  the tryboot flag itself is the firmware's and any boot clears it."
}

# Only an arming reboot carries the flag; the argument rides the reboot
# syscall, which systemd's reboot verb forwards. A bench system going --back
# reboots plain: with no flag the firmware reads the rescue's config.txt.
b_reboot() {
    if [ -n "$TRYBOOT_ARMED" ]; then
        m_ssh "setsid sh -c 'sleep 3; printf \"0 tryboot\" > /run/systemd/reboot-param && systemctl reboot' </dev/null >/dev/null 2>&1 &" >/dev/null
    else
        r_sudo "setsid sh -c 'sleep 3; reboot' </dev/null >/dev/null 2>&1 &" >/dev/null
    fi
}

# Evidence from the SD, not a record: whether the staging is in place. The
# flag itself is write-only and spent by any boot, so it is never evidence.
# The systems the medium holds are evidence too: which ids an arming can name.
b_evidence() {
    printf 'lane=kernel from the SD via tryboot (one boot, firmware-reverting); bench root on %s\n' "$MACH_DEVICE"
    local staged systems
    staged=$(m_ssh "boot=\$($_tryboot_bootfs_sh) && [ -f \"\$boot/tryboot.txt\" ] && [ -d \"\$boot/second\" ] && echo yes || echo no" \
        2>/dev/null | tr -d '\r') || staged=""
    printf 'tryboot_staged=%s\n' "${staged:-unreadable}"
    systems=$(b_systems 2>/dev/null) || systems=""
    [ -z "$systems" ] || printf '%s\n' "$systems" | awk '{ printf "system=%s (on %s)\n", $2, $1 }'
}

# The wk-managed media, in one line, for the fleet block in `wk status`.
b_media() {
    printf '%s holds the bench system(s): root on 1-2 and, when written, a second on 3-4; the armed kernel is tryboot-staged onto the SD, which also carries the rescue' \
        "$MACH_DEVICE"
}

# How this board is made from nothing, derived from its conf. sd-first: the
# SD is the boot authority for both roles here, and the bench medium is never
# firmware-booted at all.
b_reprovision() {
    cat <<REPROV
wk sysimage build $MACH_PROFILE
    in a workspace; hours
wk sysimage write --from <path> --disk <reader>:$(disk_of_part "$MACH_ROOT") --rescue --profile $MACH_PROFILE
    the SD card -- the system this board falls back to, and the firmware's boot medium
wk pi boot-order $MACH_NAME sd-first
    the SD first: the bench medium is mounted by the kernel, never firmware-booted
wk sysimage write --from <path> --disk $MACH_NAME:$MACH_DEVICE --profile <bench profile>
    the bench system's root medium, written from the rescue
wk sysimage write --from <path> --disk $MACH_NAME:$MACH_DEVICE@second --profile <bench profile>
    optional: a second system beside the first (partitions 3-4), for an
    A/B across images; 'wk boot $MACH_NAME --system <id>' picks one
wk boot $MACH_NAME
    one shot; the firmware reverts by itself
REPROV
}
