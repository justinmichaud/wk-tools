# Boot driver: this Mac, into a benchmark macOS install on another volume.
#
# The odd one out in the fleet, in three ways that all follow from one fact:
# **Apple Silicon cannot be handed an image over the wire, and cannot be told
# from software which volume to boot.** Boot volume selection goes through a
# LocalPolicy held in the machine's own secure storage, changed only by an
# authenticated user action -- System Settings' Startup Disk, or the startup
# manager you reach by holding the power button. `bless --setBoot` is
# superseded for this purpose (its `folder` option survives on Apple Silicon
# only for external media). Established 2026-08-19 and recorded in
# docs/HANDOFF-boot.md as tier 2; nothing here tries to work around it,
# because an automation that cannot exist is worse than a documented ritual.
#
# Re-tested 2026-08-23 with SIP *disabled* and root, because that is the obvious
# reason to expect a different answer: `nvram boot-volume=<other group>` exits 0
# and changes nothing, `bless --setBoot` refuses by documentation, and
# `systemsetup -getstartupdisk` answers "(null)". SIP is not the gate. What the
# test did buy is that the firmware's *own* choice is readable, which is what
# mac_firmware_default() below reports and what decides which side of a
# benchmark cycle costs a person anything.
#
# So, compared with the Pi drivers:
#
#   * there is no image *file*. What boots is an installed, personalised macOS
#     volume -- `wk sysimage write` cannot produce one and must never be pointed
#     at this machine. An install per Mac, maintained per Mac, even when the
#     contents are identical.
#   * arming is a person. This driver checks what it can, records the intent,
#     and prints the ritual; the reboot into the other role is a human act.
#   * the machine drives itself. There is one Apple Silicon machine here, so
#     the transition is arranged from inside the role being left (MACH_LOCAL
#     in boot/machines.sh), and the shell that arms it is a shell that is about
#     to be rebooted out from under itself.
#
# Why bother at all, when the run needs someone in the room: because the *rest*
# of it is exactly the same problem the Pi has, and none of it is manual. The
# build happens in a macOS guest, the payload is staged onto the benchmark
# volume from here while it is merely mounted, the record says which build is
# over there, and the numbers come back with `bench_host=image` on them. What a
# person does is choose a disk twice.
#
# UNVERIFIED against hardware as of 2026-08-20: there is no benchmark volume on
# this machine yet, so everything below has been exercised only in the states
# that exist without one -- probe, status, evidence, and the refusals. What
# needs a volume is marked where it appears.

# Another arming model, next to `one-shot` (rpi5), `medium` (rpi4) and
# `server` (rpi3):
# the intent is recorded and the act is a person's. cmd/boot branches on it
# rather than assuming a firmware call it can make.
BOOT_ARMING=hands-on

# Not used: there is no boot order to write. Named, as other drivers do,
# so that a reader comparing drivers sees a difference rather than an omission.
BOOT_ORDER_IMAGE=""
BOOT_ORDER_NORMAL=""

# --- where the other role lives ----------------------------------------------
#
# By volume name rather than by disk identifier: `disk5s2` is assigned at
# attach time and changes with which port the SSD is in, while the name is what
# the Startup Disk pane and the startup manager show -- so the name is both the
# stable identifier and the thing a person is told to click.
mac_volume_path() { printf '/Volumes/%s' "$MACH_VOLUME"; }

# Mounted *and* a macOS system volume. The second half matters: an empty
# formatted disk with the right name mounts perfectly and boots nothing, which
# would otherwise be discovered at the startup manager with the machine already
# shut down.
mac_volume_present() {
    local v; v=$(mac_volume_path)
    [ -d "$v/System/Library/CoreServices" ]
}

# --- which mode is answering --------------------------------------------------
#
# The same rule as every other driver -- the bench system writes an identity
# marker and the host install does not -- with the one difference that both
# modes are
# reached the same way, because both are this machine. There is no channel to
# discover, so MODE_CHANNEL is always `host` and r_ssh runs locally.
b_probe() {
    local id
    # This driver's every question is asked of the machine it is running on --
    # `diskutil`, `bless`, `sysctl kern.boottime` -- because MACH_LOCAL says
    # there is no other way to reach an Apple Silicon machine. Which means that
    # from anywhere else, the honest answer is "not here", and that has to be
    # said *before* the first macOS-only command rather than discovered by it:
    # run from Linux, this exited 127 partway through printing a status, which
    # is worse than either answering or refusing.
    if ! is_macos; then
        MODE_CHANNEL=none; MODE=unreachable
        return 0
    fi
    MODE_CHANNEL=host
    id=$(wk_image_id)
    if [ -n "$id" ]; then MODE="bench $id"; else MODE=host; fi
    return 0
}

# macOS has no /proc, so the two facts the shared implementations read out of it
# come from sysctl instead.
#
# kern.boottime is the only per-boot value there is here, and it does the same
# job as Linux's random boot_id for the one question that matters -- "has this
# machine rebooted since it was armed" -- because it changes on every boot. It
# is a clock reading rather than a random token, so it is *also* the answer to
# "when did it boot", and both are taken from it rather than from `uptime`,
# whose output is local time and reparses as UTC (the trap boot/machines.sh
# records for the Linux side).
# The brace in the pattern is load-bearing: the value reads
# `{ sec = 1786800736, usec = 451078 } Sat Aug 15 ...`, and a pattern that only
# asks for `sec = ` matches the *last* one -- greedy -- and returns the
# microseconds. That is a plausible-looking number, which is how it came back
# as a 1970 boot date the first time.
_mac_boottime() { sysctl -n kern.boottime 2>/dev/null | sed -n 's/.*{ *sec *= *\([0-9][0-9]*\).*/\1/p'; }

# `|| true` for the same reason the Linux driver has it: a boot id that cannot
# be read is a missing fact, not a failure, and `set -e` should not turn the
# one into the other at `BOOT_ID=$(b_boot_id)`.
b_boot_id() { _mac_boottime || true; }

b_booted_at() {
    local sec; sec=$(_mac_boottime)
    [ -n "$sec" ] || return 0
    epoch_to_utc "$sec"
}

# What can be asked of the machine itself. Not a firmware register -- there is
# nothing readable there -- but the two things that decide whether a transition
# is even possible: which volume is booted right now, and whether the other one
# is attached.
b_evidence() {
    # Both facts below are read off the machine this is running on, which is
    # only the right machine when that is this Mac. Said plainly instead when
    # it is not: the `df /` fallback answered with the *driving* machine's root
    # volume, so `wk boot mbp --status` from Linux reported an Ubuntu LVM path
    # as the Mac's booted volume -- a confident answer to a question that was
    # never asked of the right computer.
    if ! is_macos; then
        echo "booted_volume=unknown (this is not that Mac; MACH_LOCAL machines answer only for themselves)"
        echo "benchmark_volume=$MACH_VOLUME (cannot be seen from here)"
        return 0
    fi

    # `diskutil info /`'s own labelled output rather than its plist: the plist
    # puts key and value on separate lines, so reading it with sed is a
    # two-line state machine to get a string that the plain form prints once.
    local root; root=$(diskutil info / 2>/dev/null | sed -n 's/^ *Volume Name: *//p' | head -1)
    [ -n "$root" ] || root=$(df / | awk 'NR==2 {print $1}')
    echo "booted_volume=$root"
    if mac_volume_present; then
        echo "benchmark_volume=$MACH_VOLUME (attached at $(mac_volume_path))"
    else
        echo "benchmark_volume=$MACH_VOLUME (not attached)"
    fi
    echo "firmware_default=$(mac_firmware_default)"
}

# Which install the firmware says it will boot next, and what that is worth.
#
# This is the one fact that decides whether a benchmark cycle on this machine
# costs a person nothing or costs them one trip to the keyboard, and until
# 2026-08-23 nothing here reported it -- so "hands-on" was stated as a property
# of the machine when it is really a property of *which way round the default
# currently points*.
#
# The firmware publishes its choice as `boot-volume` in IODeviceTree:/options,
# three colon-separated UUIDs of which only the last identifies anything on this
# disk: the APFS *volume group*. `diskutil` gives the same UUID for each install,
# so the two can simply be compared.
#
# Two things it is honest about rather than quiet about:
#
#   it is evidence, not a promise. The startup manager boots a volume *once*
#   without updating this variable, so a machine that was last started that way
#   reports a default it is not currently running -- which is exactly the state
#   this Mac was found in (booted Macintosh HD, boot-volume naming WK Bench).
#   That is a feature of the reading: it says where the *next plain reboot*
#   goes, which is the question a lane actually asks.
#
#   it cannot be written. Tested 2026-08-23 in a macOS guest with SIP disabled
#   and root: `nvram boot-volume=<other group>` exits 0 and changes nothing --
#   the value lands in the 7C436110-… namespace and is discarded, while
#   IODeviceTree:/options keeps the firmware's own across a reboot.
#   `bless --setBoot` is documented as unsupported on Apple Silicon and
#   `systemsetup -getstartupdisk` answers "(null)". SIP is not the gate; the
#   variable is firmware-owned. So this driver reports and never sets.
mac_volume_group() {  # $1 = mount point
    diskutil info "$1" 2>/dev/null | sed -n 's/^ *APFS Volume Group: *//p' | head -1
}

mac_firmware_default() {
    local bv grp host_grp bench_grp
    bv=$(nvram -p 2>/dev/null | awk -F'\t' '$1 == "boot-volume" { print $2 }')
    [ -n "$bv" ] || { printf 'unknown (the firmware publishes no boot-volume)'; return 0; }
    grp="${bv##*:}"
    host_grp=$(mac_volume_group /)
    bench_grp=""
    mac_volume_present && bench_grp=$(mac_volume_group "$(mac_volume_path)")
    if [ -n "$bench_grp" ] && [ "$grp" = "$bench_grp" ]; then
        printf "%s ('%s' -- a plain reboot is expected to enter bench mode)" "$grp" "$MACH_VOLUME"
    elif [ -n "$host_grp" ] && [ "$grp" = "$host_grp" ]; then
        printf '%s (the host install -- a plain reboot stays in host mode)' "$grp"
    else
        printf '%s (matches neither install on this disk)' "$grp"
    fi
}

# --- the record ---------------------------------------------------------------
#
# In this user's own state directory rather than /var/lib/wk, and that is a
# deliberate difference from the Pi drivers: on a board the record belongs to
# the machine and passwordless sudo is there to write it, while here every
# `sudo` is a password prompt (docs/HANDOFF-sandboxing.md keeps it that way).
# A record of intent is not worth a password, and this machine has exactly one
# person driving it -- so the machine's record and the user's are the same
# record. It stays one copy, which is the property rule 1 asks for.
#
# It has to live on the *normal* role's disk, like every other arming record:
# the benchmark volume has its own home directory and cannot see this one.
MACH_RECORD="$(wk_state_dir)/boot-armed"

record_write() {
    mkdir -p "$(dirname "$MACH_RECORD")"
    cat > "$MACH_RECORD" <<EOF
image=$1
profile=$2
device=$3
order=$4
armed_by=$(hostname)
armed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
armed_boot_id=$(b_boot_id)
EOF
}

record_read() {
    [ "${MODE_CHANNEL:-host}" = host ] || return 0
    cat "$MACH_RECORD" 2>/dev/null || true
}

record_clear() { rm -f "$MACH_RECORD"; }

# --- arming -------------------------------------------------------------------
#
# Everything that can be checked, then the ritual. The checks are the point: a
# person who has shut the machine down to hold the power button cannot be told
# at that moment that the volume was never attached.
#
# NEEDS A VOLUME to have ever succeeded.
b_arm() {
    mac_volume_present || die "'$MACH_VOLUME' is not attached, or is not a macOS system volume.
    What has to exist is a full macOS *install* on another volume, personalised
    for this Mac -- an image copied onto a disk will not boot (the boot policy
    lives in this machine's secure storage). Install it from Recovery or with
    the macOS installer app, name the volume '$MACH_VOLUME', and see
    docs/HANDOFF-benchmarking.md for what to turn off on it.
    A different name:  WK_BENCH_VOLUME='...' wk boot $MACH_NAME"

    cat >&2 <<EOF

  This is the hands-on half, and it is two clicks:

    the one-shot way (preferred)
      shut down, then hold the power button until "Loading startup options",
      pick "$MACH_VOLUME", and press Return. This boots it *once* and leaves
      the default alone, which is what makes the way back a plain reboot.

    the sticky way
      System Settings -> General -> Startup Disk -> "$MACH_VOLUME" -> Restart.
      This changes the default, so the machine keeps booting the benchmark
      volume until the pane is used again. Only worth it for a long session.

EOF
}

# There is no one-shot register to cancel, so disarming is only the record --
# and if the sticky route was taken, a person has to undo that too. Said out
# loud, because a disarm that silently did half the job is worse than one that
# explains which half it could do.
b_disarm_note() {
    log "  nothing in firmware was changed, so there is nothing there to cancel."
    log "  If you used System Settings -> Startup Disk (the sticky route), set it"
    log "  back to the internal volume there; the startup-manager route needs no"
    log "  undo -- the next reboot is a normal one by itself."
}

# The benchmark volume's own account of its last boot, read from the normal
# role while the volume is merely mounted. The offline channel the Pi images
# have, arrived at from the other direction: there, the workstation mounts the
# image's boot partition; here, the volume is already mounted and the file is
# simply on it.
b_diag() {
    local v; v=$(mac_volume_path)
    mac_volume_present || die "'$MACH_VOLUME' is not attached, so there is nothing to read."
    cat "$v/var/log/wk-diag.txt" 2>/dev/null \
        || echo "(no var/log/wk-diag.txt on '$MACH_VOLUME' -- it has not been provisioned, or has never booted)"
}

# A plain reboot, which lands in the *default* startup disk -- the internal
# volume, unless somebody used the Startup Disk pane. That is the way back from
# bench mode and it needs no ritual, which is the whole reason the
# startup-manager route is the recommended one above.
b_reboot() {
    sudo shutdown -r +1 "wk boot: returning to host mode" >/dev/null 2>&1 \
        || die "could not schedule a reboot (this one needs sudo, and it is the
    only part of a transition that does)."
}

# --- where a staged payload goes ----------------------------------------------
#
# A path on *this* machine, because that is the whole reason this transition is
# cheap: the other role's disk is merely a mounted volume while we are in the
# host mode, so staging a build onto it is a copy and not a transfer.
#
# The Pi images have an equivalent and it is not this: their payload is pushed
# over ssh to a machine that is already running the image, onto a partition
# that exists for it (docs/HANDOFF-boot.md, "Storage"). A driver whose other
# role is only reachable over the network leaves this unset, and `wk bench
# stage` refuses it by name rather than inventing a transport.
# Fails when the disk is not attached, rather than printing a path under a
# mount point that is not mounted: /Volumes/<name>/var/wk on an absent volume
# is a perfectly creatable directory on the *internal* disk, and a payload
# staged there would be invisible to the role it was staged for and invisible
# to whoever staged it.
# The *Data* volume, not the system volume, and this is not a detail: since APFS
# volume groups the system volume is sealed and read-only (`csrutil
# authenticated-root` is enabled), so `/Volumes/WK Bench/var/wk` cannot be
# created at all -- staging died on `mkdir: /Volumes/WK Bench/var/wk:
# Permission denied` the first time this driver ever met a real volume, having
# been written and reviewed against one that did not exist yet.
#
# `/var` does not exist on the data volume either: on the *running* system it is
# a firmlink, and from outside the pair is mounted as two volumes with the real
# directory at `private/var`. So the same bytes are `/var/wk` to the booted
# bench install and `/Volumes/<name> - Data/private/var/wk` to host mode staging
# them, and both spellings have to appear because both are correct from where
# they are said.
mac_volume_data_path() {
    local d="/Volumes/$MACH_VOLUME - Data"
    [ -d "$d" ] && { printf '%s' "$d"; return 0; }
    # No data volume: a plain volume rather than a group. Then the system
    # volume is the only place there is, and its writability is the caller's
    # problem to report rather than this function's to hide.
    printf '%s' "$(mac_volume_path)"
}

b_bench_root() {
    mac_volume_present || return 1
    local d; d=$(mac_volume_data_path)
    case "$d" in
        *" - Data") printf '%s/private/var/wk' "$d" ;;
        *)          printf '%s/var/wk' "$d" ;;
    esac
}

# A MACH_LOCAL machine answers only for itself: probing it from anywhere else
# reads the *driving* machine's marker and reports a confident wrong answer
# (the b_evidence comment above records the df / version of that mistake).
b_probeable() { is_macos; }

# The wk-managed media, in one line, for the fleet block in `wk status`.
b_media() {
    if ! is_macos; then
        printf "bench volume '%s' (visible only on that Mac)" "$MACH_VOLUME"
        return 0
    fi
    if mac_volume_present; then
        printf "bench volume '%s' attached at %s" "$MACH_VOLUME" "$(mac_volume_path)"
    else
        printf "bench volume '%s' MISSING -- docs/HANDOFF-mac-perf-mode.md creates it" "$MACH_VOLUME"
    fi
}
