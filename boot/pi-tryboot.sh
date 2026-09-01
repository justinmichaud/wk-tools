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

# Whether this board can be armed while it is running a *bench* system.
#
# Yes, here: staging is files copied onto the SD boot partition as root over
# ssh, which either system can do -- no privileged helper, nothing that only a
# rescue carries. So a leg switch is one reboot rather than two, and it does not
# depend on a rescue this board may not be able to reach at all.
B_ARM_FROM_BENCH=yes

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

# Where the firmware reads files from: the SD's boot partition, named from a
# fact this machine's conf declares (partition 1 of the disk the *rescue* root
# is on) rather than found by content.
#
# Content was the earlier answer -- "the mounted vfat holding start4.elf" -- and
# it is right on the rescue and wrong on a bench system, whose own
# /boot/firmware is the *medium's* FAT and carries start4.elf too. A stage or a
# disarm run from the bench side would edit the wrong filesystem and report
# success. One name, from the conf, correct from either side.
#
# Emits shell defining `wk_sd_boot` (prints the mountpoint, mounting the
# partition if nothing has) and `wk_sd_drop` (undoes only a mount it made).
# **No single quote and no `%`**: this is spliced into b_self_disarm_sh, which
# systemd puts inside an `ExecStart=/bin/sh -c '...'`. wk selftest asserts both.
_tryboot_sd_sh() {
    local part
    part=$(disk_part "$(disk_of_part "$MACH_ROOT")" 1)
    printf '%s' "wk_sd_own=; \
wk_sd_boot() { \
wk_sd_m=\$(grep -m1 \"^$part \" /proc/mounts | cut -d\" \" -f2); \
if [ -z \"\$wk_sd_m\" ]; then wk_sd_m=\$(mktemp -d); mount -t vfat \${1:-} $part \"\$wk_sd_m\" || return 1; wk_sd_own=1; fi; \
echo \"\$wk_sd_m\"; }; \
wk_sd_drop() { if [ -n \"\$wk_sd_own\" ]; then sync; umount \"\$wk_sd_m\"; rmdir \"\$wk_sd_m\"; wk_sd_own=; fi; }; "
}

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
#
# Which file is the kernel is the firmware's own name resolution, the rule
# boot/check-boot-files.py already models: a `kernel=` line names it, and
# without one the firmware picks from its defaults by what is present -- the
# WebKit Dev@CI yocto images ship a bare config.txt beside one `kernel8.img`.
# So this stages the single default-named kernel there is; with several, which
# one the firmware picks follows from arm_64bit and the board, and staging a
# different one would boot a kernel nobody asked for, so it refuses and asks
# the profile's config.txt.append to name one.
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
# os_prefix leads the file, ahead of every line the image itself carries: the
# firmware resolves a filename when it reads the directive that asks for one,
# so a prefix declared after a dtoverlay line never reaches that overlay's
# .dtbo, and a prefix appended at the end lands inside whichever conditional
# section the image happens to close with. Either way the arm boots the right
# kernel and leaves the image's display stack on the floor.
#
# arm_64bit, stated explicitly from the kernel's own magic (zImage:
# 0x016f2818 at offset 36; ARM64 Image: 'ARM\x64' at 56): the SD's modern
# firmware defaults to 64-bit, and a config.txt that only says kernel=zImage
# -- the fork's do -- makes it jump into a 32-bit zImage as if it were an
# arm64 Image: a silent hang before any kernel code, no diag, no watchdog.
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

# The half that runs *inside* the bench system, first thing after its root is
# up. It exists because this board does not consume the tryboot flag: measured
# 2026-09-01, a plain reboot and a cold power cycle both read `tryboot.txt`
# again and came back as the bench system, with `/proc/cmdline` carrying
# `second/cmdline.txt`s own `panic=10` and the SD `config.txt` holding no
# `os_prefix`. So "one boot" has to be made true by the boot that spends it:
# the staging goes away as the system it selected comes up, and every later
# reboot reads config.txt and lands on the rescue.
#
# Without this the board cannot leave the bench system at all -- and `b_disarm`
# cannot help, because it is addressed to a rescue that this same staging
# stops the board from ever booting.
#
# The SD's boot partition is partition 1 of the disk the *rescue* root is on
# (MACH_ROOT), which is a fact this machine's conf already states.
# **No single quote and no `%` may appear in what this returns** --
# interpolated into a systemd `ExecStart=/bin/sh -c '...'`, for the reasons
# boot/pi-sd.sh's copy states. wk selftest asserts both.
b_self_disarm_sh() {
    printf "%s" "$(_tryboot_sd_sh)boot=\$(wk_sd_boot) || exit 0; \
if [ -f \"\$boot/tryboot.txt\" ]; then rm -f \"\$boot/tryboot.txt\"; rm -rf \"\$boot/second\"; \
echo \"wk-self-disarm: this boot spent the tryboot staging; the rescue boots next\"; fi; \
wk_sd_drop"
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
    r_ssh "$(tryboot_stage_sh "$ARM_SYS_PART" "$MACH_DTB")" \
        || die "could not stage the tryboot files on $MACH_NAME.
    Arming copies the selected system's kernel out of $MACH_DEVICE's boot
    partition onto the SD, so the board has to answer -- as its rescue or as a
    bench system, either will do -- and both media have to be readable there."
    TRYBOOT_ARMED=1
}

# The staged files are inert without the flag, and the flag lives in the
# firmware for at most the seconds between b_arm and the reboot -- nothing
# here can clear it. Removing the staging is still the honest disarm: what
# `wk boot --status` reads as armed evidence goes away with it.
# Over whichever channel answered, not the rescue's alone: while the staging is
# in force this board boots the *bench* system (the flag is not consumed here),
# so a disarm addressed only to the rescue is one that can never run. `r_ssh`
# is the one implementation of "the channel this machine answered on"
# (boot/machines.sh), and the script below reaches the SD's boot filesystem
# from either side: mounted already on the rescue, mounted by name on a bench
# system, whose own /boot/firmware is the medium's rather than the SD's.
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

# Only an arming reboot carries the flag; the argument rides the reboot
# syscall, which systemd's reboot verb forwards. A bench system going --back
# reboots plain: with no flag the firmware reads the rescue's config.txt.
b_reboot() {
    if [ -n "$TRYBOOT_ARMED" ]; then
        r_sudo "setsid sh -c 'sleep 3; printf \"0 tryboot\" > /run/systemd/reboot-param && systemctl reboot' </dev/null >/dev/null 2>&1 &" >/dev/null
    else
        r_sudo "setsid sh -c 'sleep 3; reboot' </dev/null >/dev/null 2>&1 &" >/dev/null
    fi
}

# Evidence from the SD, not a record: whether the staging is in place. The
# flag itself is write-only and spent by any boot, so it is never evidence.
# The systems the medium holds are evidence too: which ids an arming can name.
b_evidence() {
    printf 'lane=kernel from the SD via tryboot (one boot, firmware-reverting); bench root on %s\n' "$MACH_DEVICE"
    local staged systems source
    staged=$(r_ssh "$(_tryboot_sd_sh)
        boot=\$(wk_sd_boot \"-o ro\") || exit 0
        if [ -f \"\$boot/tryboot.txt\" ] && [ -d \"\$boot/second\" ]; then echo yes; else echo no; fi
        wk_sd_drop" 2>/dev/null | tr -d '\r' | head -1) || staged=""

    # Which config the running boot came from, and not which one was staged.
    # `wk boot --status` used to answer "bench mode, system X", which is true
    # and says nothing about *how* the board got there -- so a board reading
    # tryboot.txt on a plain reboot (this firmware does not consume the flag)
    # looked exactly like a board that had been armed for it. Hours went into
    # that. The running root= against the two cmdlines on the SD tells them
    # apart in one read, so the status says it.
    source=$(r_ssh "$(_tryboot_sd_sh)
        r=\$(sed -n \"s/.*root=\\([^ ]*\\).*/\\1/p\" /proc/cmdline)
        boot=\$(wk_sd_boot \"-o ro\") || { echo unknown; exit 0; }
        sd=\$(sed -n \"s/.*root=\\([^ ]*\\).*/\\1/p\" \"\$boot/cmdline.txt\" 2>/dev/null)
        st=\$(sed -n \"s/.*root=\\([^ ]*\\).*/\\1/p\" \"\$boot/second/cmdline.txt\" 2>/dev/null)
        if [ -n \"\$st\" ] && [ \"\$r\" = \"\$st\" ]; then echo staging
        elif [ -n \"\$sd\" ] && [ \"\$r\" = \"\$sd\" ]; then echo sd-config
        else echo unknown; fi
        wk_sd_drop" 2>/dev/null | tr -d '\r' | head -1) || source=""
    case "${source:-}" in
        staging)   printf 'boot_source=the tryboot staging on the SD (second/), so this boot spent an arming -- or read it again because the firmware did not consume the flag\n' ;;
        sd-config) printf 'boot_source=the SD config.txt, the plain path\n' ;;
        *)         printf 'boot_source=unreadable\n' ;;
    esac
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
