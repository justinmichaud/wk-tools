# Boot driver: this Mac, into a benchmark macOS install on another volume.
#
# Apple Silicon has no volume-boot primitive over software or the wire:
# selection goes through a LocalPolicy in the machine's own secure storage,
# changed only by an authenticated user action (docs/HANDOFF-boot.md, tier 2).
# So `wk sysimage write` is never pointed at this machine, arming is a person,
# and the machine drives itself (NODE_LOCAL, boot/machines.sh).
#
# TODO: unverified against hardware -- no benchmark volume exists here yet.

BOOT_ARMING=hands-on   # the intent is recorded and the act is a person's; cmd/boot branches on it

BOOT_ORDER_IMAGE=""    # no order to write; named so a diff shows a difference, not an omission
BOOT_ORDER_NORMAL=""

# --- where the other role lives ----------------------------------------------
# By volume name: `disk5s2` changes with the port, the name doesn't.
mac_volume_path() { printf '/Volumes/%s' "$NODE_VOLUME"; }

# Mounted and a macOS system volume: an empty formatted disk with the right
# name mounts perfectly and boots nothing.
mac_volume_present() {
    local v; v=$(mac_volume_path)
    [ -d "$v/System/Library/CoreServices" ]
}

# --- which mode is answering --------------------------------------------------
# The bench system writes an identity marker the host install does not, but
# both modes are this same machine: MODE_CHANNEL is always `host`.
b_probe() {
    local id
    # Refuse before the first macOS-only command, or diskutil/bless/sysctl
    # fail partway through printing a status.
    if ! is_macos; then
        MODE_CHANNEL=none; MODE=unreachable
        return 0
    fi
    MODE_CHANNEL=host
    id=$(wk_image_id)
    if [ -n "$id" ]; then MODE="bench $id"; else MODE=host; fi
    return 0
}

# No /proc on macOS: kern.boottime stands in for boot_id and boot time. The
# brace in the pattern is load-bearing -- `{ sec = 1786800736, usec = 451078
# } Sat Aug 15 ...` -- since a pattern anchored only on `sec = ` matches
# greedily to the last one, returning usec, a plausible but wrong number.
_mac_boottime() { sysctl -n kern.boottime 2>/dev/null | sed -n 's/.*{ *sec *= *\([0-9][0-9]*\).*/\1/p'; }

# `|| true`: a boot id that cannot be read is a missing fact, not a failure,
# and `set -e` should not turn the one into the other.
b_boot_id() { _mac_boottime || true; }

b_booted_at() {
    local sec; sec=$(_mac_boottime)
    [ -n "$sec" ] || return 0
    epoch_to_utc "$sec"
}

# Not a firmware register -- nothing readable there -- but the two facts that
# decide whether a transition is possible.
b_evidence() {
    # Accurate only on this Mac: elsewhere `df /` reports the driving
    # machine's own root volume as if it were this Mac's.
    if ! is_macos; then
        echo "booted_volume=unknown (this is not that Mac; NODE_LOCAL machines answer only for themselves)"
        echo "benchmark_volume=$NODE_VOLUME (cannot be seen from here)"
        return 0
    fi

    local root; root=$(python3 "$WK_ROOT/lib/wkmac.py" volume-name / 2>/dev/null)
    [ -n "$root" ] || root=$(df / | awk 'NR==2 {print $1}')
    echo "booted_volume=$root"
    if mac_volume_present; then
        echo "benchmark_volume=$NODE_VOLUME (attached at $(mac_volume_path))"
    else
        echo "benchmark_volume=$NODE_VOLUME (not attached)"
    fi
    echo "firmware_default=$(mac_firmware_default)"
}

# Which install the firmware will boot next: `boot-volume` in
# IODeviceTree:/options, whose last UUID is the APFS volume group `diskutil`
# also reports. It names the next plain reboot and cannot be written
# (`nvram boot-volume=...` discards the value).
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
        printf "%s ('%s' -- a plain reboot is expected to enter bench mode)" "$grp" "$NODE_VOLUME"
    elif [ -n "$host_grp" ] && [ "$grp" = "$host_grp" ]; then
        printf '%s (the host install -- a plain reboot stays in host mode)' "$grp"
    else
        printf '%s (matches neither install on this disk)' "$grp"
    fi
}

# --- the record ---------------------------------------------------------------
# In this user's state directory, since every `sudo` here is a password prompt
# (docs/HANDOFF-sandboxing.md). Kept on the normal role's disk: the benchmark
# volume has its own home directory and cannot see this one.
NODE_RECORD="$(wk_state_dir)/boot-armed"

record_write() {
    mkdir -p "$(dirname "$NODE_RECORD")"
    cat > "$NODE_RECORD" <<EOF
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
    cat "$NODE_RECORD" 2>/dev/null || true
}

record_clear() { rm -f "$NODE_RECORD"; }

# --- arming -------------------------------------------------------------------
# Everything checkable, then the ritual. NEEDS A VOLUME to have ever succeeded.
b_arm() {
    mac_volume_present || die "'$NODE_VOLUME' is not attached, or is not a macOS system volume.
    What has to exist is a full macOS *install* on another volume, personalised
    for this Mac -- an image copied onto a disk will not boot (the boot policy
    lives in this machine's secure storage). Install it from Recovery or with
    the macOS installer app, name the volume '$NODE_VOLUME', and see
    docs/HANDOFF-mac-perf-mode.md for what to turn off on it.
    A different name:  WK_BENCH_VOLUME='...' wk boot $NODE_NAME"

    cat >&2 <<EOF

  This is the hands-on half, and it is two clicks:

    the one-shot way (preferred)
      shut down, then hold the power button until "Loading startup options",
      pick "$NODE_VOLUME", and press Return. This boots it *once* and leaves
      the default alone, which is what makes the way back a plain reboot.

    the sticky way
      System Settings -> General -> Startup Disk -> "$NODE_VOLUME" -> Restart.
      This changes the default, so the machine keeps booting the benchmark
      volume until the pane is used again. Only worth it for a long session.

EOF
}

# No one-shot register to cancel, so disarming is only the record; the sticky
# route has to be undone by a person.
b_disarm_note() {
    log "  nothing in firmware was changed, so there is nothing there to cancel."
    log "  If you used System Settings -> Startup Disk (the sticky route), set it"
    log "  back to the internal volume there; the startup-manager route needs no"
    log "  undo -- the next reboot is a normal one by itself."
}

# The benchmark volume's own account of its last boot, read while merely
# mounted.
b_diag() {
    local v; v=$(mac_volume_path)
    mac_volume_present || die "'$NODE_VOLUME' is not attached, so there is nothing to read."
    cat "$v/var/log/wk-diag.txt" 2>/dev/null \
        || echo "(no var/log/wk-diag.txt on '$NODE_VOLUME' -- it has not been provisioned, or has never booted)"
}

# A plain reboot lands in the default startup disk -- the internal volume,
# unless Startup Disk was used -- which is the way back from bench mode.
b_reboot() {
    sudo shutdown -r +1 "wk boot: returning to host mode" >/dev/null 2>&1 \
        || die "could not schedule a reboot (this one needs sudo, and it is the
    only part of a transition that does)."
}

# --- where a staged payload goes ----------------------------------------------
# A path on this machine: staging is a copy onto a mounted volume, not a
# network transfer. Fails when the disk is not attached rather than naming a
# creatable directory on the internal disk, invisible to both roles. The *Data*
# volume, since the APFS system volume is sealed and read-only; `/var` is a
# firmlink there, mounting outside it as `private/var`, so the same bytes are
# `/var/wk` to the booted bench install and
# `/Volumes/<name> - Data/private/var/wk` here.
mac_volume_data_path() {
    local d="/Volumes/$NODE_VOLUME - Data"
    [ -d "$d" ] && { printf '%s' "$d"; return 0; }
    # No data volume: the system volume is the only place there is.
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

# A NODE_LOCAL machine answers only for itself.
b_probeable() { is_macos; }

b_media() {
    if ! is_macos; then
        printf "bench volume '%s' (visible only on that Mac)" "$NODE_VOLUME"
        return 0
    fi
    if mac_volume_present; then
        printf "bench volume '%s' attached at %s" "$NODE_VOLUME" "$(mac_volume_path)"
    else
        printf "bench volume '%s' MISSING -- docs/HANDOFF-mac-perf-mode.md creates it" "$NODE_VOLUME"
    fi
}

# An APFS volume in its own container, made and populated on the Mac itself;
# the last step is always a person holding the power button.
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
