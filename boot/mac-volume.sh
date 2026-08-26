# Boot driver: this Mac, into a benchmark macOS install on another volume.
#
# Apple Silicon cannot be handed an image over the wire or told from software
# which volume to boot: boot volume selection goes through a LocalPolicy in
# the machine's own secure storage, changed only by an authenticated user
# action (Startup Disk, or the startup manager reached by holding the power
# button). `bless --setBoot` no longer works for this (its `folder` option
# survives only for external media). docs/HANDOFF-boot.md records this as
# tier 2.
#
# Consequences, compared with the Pi drivers:
#
#   * no image *file* -- what boots is an installed, personalised macOS
#     volume. `wk sysimage write` must never be pointed at this machine, and
#     the install is maintained per Mac even when contents are identical.
#   * arming is a person: this driver checks what it can, records the
#     intent, and prints the ritual; the reboot is a human act.
#   * the machine drives itself, since this is the only Apple Silicon machine
#     here (MACH_LOCAL in boot/machines.sh) -- the shell that arms the
#     transition is about to be rebooted out from under itself.
#
# The rest is not manual: the build happens in a macOS guest, the payload is
# staged onto the benchmark volume from here while it is merely mounted, the
# record says which build is over there, and results come back with
# `bench_host=image` on them. What a person does is choose a disk twice.
#
# TODO: unverified against hardware -- with no benchmark volume on this
# machine, only the states that exist without one (probe, status, evidence,
# refusals) are exercised. What needs a volume is marked where it appears.

# Another arming model, alongside `one-shot` (rpi5), `medium` (rpi4) and
# `server` (rpi3): the intent is recorded and the act is a person's; cmd/boot
# branches on it rather than assuming a firmware call it can make.
BOOT_ARMING=hands-on

# No boot order to write; named like other drivers so a diff shows a
# difference rather than an omission.
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
# marker, the host install does not -- but both modes are reached the same
# way, since both are this machine: MODE_CHANNEL is always `host`.
b_probe() {
    local id
    # Refuse before the first macOS-only command: run elsewhere, `diskutil`/
    # `bless`/`sysctl` fail partway through printing a status instead of
    # cleanly saying "not here".
    if ! is_macos; then
        MODE_CHANNEL=none; MODE=unreachable
        return 0
    fi
    MODE_CHANNEL=host
    id=$(wk_image_id)
    if [ -n "$id" ]; then MODE="bench $id"; else MODE=host; fi
    return 0
}

# No /proc on macOS: kern.boottime stands in for both facts the shared code
# needs. It changes every boot, serving as boot_id, and gives boot time
# directly, avoiding `uptime`'s local-time output (see boot/machines.sh).
#
# The brace in the pattern is load-bearing: the value reads
# `{ sec = 1786800736, usec = 451078 } Sat Aug 15 ...`, and a pattern anchored
# only on `sec = ` matches greedily to the last one, returning usec -- a
# plausible but wrong number.
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
    # Accurate only when run on this Mac: elsewhere `df /` would report the
    # driving machine's own root volume as if it were this Mac's.
    if ! is_macos; then
        echo "booted_volume=unknown (this is not that Mac; MACH_LOCAL machines answer only for themselves)"
        echo "benchmark_volume=$MACH_VOLUME (cannot be seen from here)"
        return 0
    fi

    local root; root=$(python3 "$WK_ROOT/lib/wkmac.py" volume-name / 2>/dev/null)
    [ -n "$root" ] || root=$(df / | awk 'NR==2 {print $1}')
    echo "booted_volume=$root"
    if mac_volume_present; then
        echo "benchmark_volume=$MACH_VOLUME (attached at $(mac_volume_path))"
    else
        echo "benchmark_volume=$MACH_VOLUME (not attached)"
    fi
    echo "firmware_default=$(mac_firmware_default)"
}

# Which install the firmware says it will boot next -- the one fact that
# decides whether a benchmark cycle costs a person nothing or one trip to
# the keyboard.
#
# The firmware publishes its choice as `boot-volume` in IODeviceTree:/options,
# three colon-separated UUIDs of which only the last identifies anything on
# this disk: the APFS volume group. `diskutil` gives the same UUID per
# install, so the two are simply compared.
#
# It is evidence, not a promise: the startup manager boots a volume *once*
# without updating this variable, so a machine last started that way reports
# a default it is not currently running -- it names the *next plain reboot*,
# which is the question a lane actually asks.
#
# It cannot be written, even as root with SIP disabled: `nvram
# boot-volume=<other group>` exits 0 but the value is discarded, `bless
# --setBoot` is unsupported on Apple Silicon, and `systemsetup
# -getstartupdisk` answers "(null)". The variable is firmware-owned, so this
# driver reports and never sets it.
mac_volume_group() {  # $1 = mount point
    python3 "$WK_ROOT/lib/wkmac.py" volume-group "$1" 2>/dev/null
}

mac_firmware_default() {
    local bv grp host_grp bench_grp
    bv=$(python3 "$WK_ROOT/lib/wkmac.py" boot-volume 2>/dev/null)
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
# Lives in this user's state directory, not /var/lib/wk: on a board the
# record belongs to the machine and passwordless sudo writes it, but here
# every `sudo` is a password prompt (docs/HANDOFF-sandboxing.md), and this
# machine has exactly one person driving it, so the machine's record and the
# user's are the same record.
#
# Lives on the *normal* role's disk, like every other arming record: the
# benchmark volume has its own home directory and cannot see this one.
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
    docs/HANDOFF-mac-perf-mode.md for what to turn off on it.
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
# A path on *this* machine: staging a build is a copy onto a mounted volume,
# not a network transfer like the Pi images (docs/HANDOFF-boot.md,
# "Storage"). A driver whose other role is only reachable over the network
# leaves this unset, and `wk bench stage` refuses it by name.
#
# Fails when the disk is not attached rather than printing a path under an
# unmounted mount point: that path is a creatable directory on the
# *internal* disk, and a payload staged there is invisible to both the role
# it was staged for and whoever staged it.
#
# The *Data* volume, not the system volume: under APFS the system volume is
# sealed and read-only (`csrutil authenticated-root`), so nothing can be
# created on it.
#
# `/var` does not exist on the Data volume by that name: it is a firmlink on
# the *running* system, and mounts outside it as `private/var`. So the same
# bytes are `/var/wk` to the booted bench install and
# `/Volumes/<name> - Data/private/var/wk` to host-mode staging.
mac_volume_data_path() {
    local d="/Volumes/$MACH_VOLUME - Data"
    [ -d "$d" ] && { printf '%s' "$d"; return 0; }
    # No data volume: a plain volume rather than a group. The system volume
    # is the only place there is; its writability is the caller's to report.
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
# (the same trap b_evidence above avoids).
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

# How this Mac's benchmark install is made from nothing: an APFS volume in
# its own container, made and populated on the Mac itself. The last step is
# always a person holding the power button (LocalPolicy, see file header).
b_reprovision() {
    cat <<REPROV
wk bench mac-volume --create
    a second APFS volume in its own container, on the Mac
wk bench mac-volume --install
wk bench mac-volume --provision
hold the power button and pick the volume
    hands-on, always: firmware owns this choice, not the OS
REPROV
}
