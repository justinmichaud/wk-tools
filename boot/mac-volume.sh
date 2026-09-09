# Boot driver: a Mac, into a benchmark macOS install on another volume. Which install the firmware boots is signed into a LocalPolicy, and only `wk-boot-priv` blesses it; every read and the arming itself go through m_ssh (boot/machines.sh), so this drives that Mac from it or from anywhere else.

BOOT_ARMING=command    # cmd/boot branches on it: `wk boot mbp` tells the firmware and reboots

BOOT_HELPER=/usr/local/libexec/wk-boot-priv

BOOT_ORDER_IMAGE=""    # no order to write; named so a diff shows a difference, not an omission
BOOT_ORDER_NORMAL=""

mv_wkmac() { # <subcommand> [args...] -- wkmac.py travels on stdin, so nothing over there has to be kept in step
    local a q=""
    for a in "$@"; do q="$q $(sh_quote "$a")"; done
    m_ssh "python3 -$q" < "$WK_ROOT/lib/wkmac.py" 2>/dev/null
}

mac_volume_path() { printf '/Volumes/%s' "$NODE_VOLUME"; }

mac_volume_present() {
    m_ssh "test -d $(sh_quote "$(mac_volume_path)/System/Library/CoreServices")"
}

# `wk bench mac-ab` records each plant as a bench task whose name opens with the UTC stamp it was planted at, so the newest is the last the glob yields.
mv_planted_task() {
    local d newest=""
    for d in "${WK_STORE:-/nonexistent}/bench"/*-"$NODE_NAME"-mac-ab; do
        [ -f "$d/job.json" ] || continue
        newest="$d"
    done
    [ -n "$newest" ] || return 1
    printf '%s' "$newest"
}

mv_planted_stamp() { basename "$1" | cut -d- -f1; }

b_probe() {
    local id
    if id=$(m_ssh 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null; echo READY' 2>/dev/null); then  # both installs answer as NODE_SSH; the marker is how the bench one says which it is
        MODE_CHANNEL=host
        id=$(printf '%s' "$id" | tr -d '\r' | head -1)
        if [ "$id" = READY ]; then MODE=host; else MODE="bench $id"; fi
        return 0
    fi
    MODE_CHANNEL=none; MODE=unreachable
    return 0
}

# The brace is load-bearing -- `{ sec = 1786800736, usec = 451078 } Sat Aug 15 ...` -- since a pattern anchored on `sec = ` alone matches greedily to the last one and returns usec.
_mac_boottime() {
    m_ssh 'sysctl -n kern.boottime 2>/dev/null' 2>/dev/null \
        | sed -n 's/.*{ *sec *= *\([0-9][0-9]*\).*/\1/p'
}

b_boot_id() { _mac_boottime || true; }

b_booted_at() {
    local sec; sec=$(_mac_boottime)
    [ -n "$sec" ] || return 0
    epoch_to_utc "$sec"
}

b_evidence() {
    if ! m_reachable; then
        echo "booted_volume=unknown ($NODE_SSH does not answer)"
        echo "benchmark_volume=$NODE_VOLUME (on that Mac; nothing on it is readable while it is silent)"
        echo "firmware_default=unknown (nvram answers only from a running install)"
        echo "bench_display=${NODE_DISPLAY:-unpinned} (the install that is measured, not the one that answers here)"
        echo "planted_job=$(mv_job_evidence)"
        return 0
    fi

    local root; root=$(mv_wkmac volume-name / || true)
    echo "booted_volume=${root:-unknown (diskutil would not name it)}"
    if mac_volume_present; then
        echo "benchmark_volume=$NODE_VOLUME (attached at $(mac_volume_path))"
    else
        echo "benchmark_volume=$NODE_VOLUME (not attached)"
    fi
    echo "firmware_default=$(mac_firmware_default)"
    echo "bench_display=${NODE_DISPLAY:-unpinned} (the install that is measured, not the one that answers here)"
    echo "planted_job=$(mv_job_evidence)"
}

mv_job_evidence() {
    local d
    d=$(mv_planted_task) || { printf 'none in %s/bench' "${WK_STORE:-<no store>}"; return 0; }
    printf '%s (planted %s)' "$d" "$(mv_planted_stamp "$d")"
}

# The firmware publishes what it will boot next as `boot-volume` in IODeviceTree:/options, whose last UUID is the APFS volume group; it cannot be written (`nvram boot-volume=...` discards the value).
mac_volume_group() {  # $1 = mount point
    mv_wkmac volume-group "$1" || true
}

mac_firmware_default() {
    local bv grp host_grp bench_grp
    bv=$(mv_wkmac boot-volume || true)
    [ -n "$bv" ] || { printf 'unknown (the firmware publishes no boot-volume)'; return 0; }
    grp="${bv##*:}"
    host_grp=$(mac_volume_group / || true)
    bench_grp=""
    mac_volume_present && bench_grp=$(mac_volume_group "$(mac_volume_path)" || true)
    if [ -n "$bench_grp" ] && [ "$grp" = "$bench_grp" ]; then
        printf "%s ('%s' -- a plain reboot is expected to enter bench mode)" "$grp" "$NODE_VOLUME"
    elif [ -n "$host_grp" ] && [ "$grp" = "$host_grp" ]; then
        printf '%s (the host install -- a plain reboot stays in host mode)' "$grp"
    else
        printf '%s (matches neither install on this disk)' "$grp"
    fi
}

# On the host install's disk: the benchmark volume has its own home directory and cannot see this one. Expanded where it is read, since a machine driving the lane from elsewhere has a different home.
NODE_RECORD='"${XDG_STATE_HOME:-$HOME/.local/state}/wk/boot-armed"'

record_write() {
    m_ssh "mkdir -p \"\$(dirname $NODE_RECORD)\" && cat > $NODE_RECORD" <<EOF
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
    m_ssh "cat $NODE_RECORD 2>/dev/null" || true
}

record_clear() { m_ssh "rm -f $NODE_RECORD"; }

b_arm() {
    mac_volume_present || die "'$NODE_VOLUME' is not attached, or is not a macOS system volume.
    What has to exist is a full macOS *install* on another volume, personalised
    for this Mac -- an image copied onto a disk will not boot (the boot policy
    lives in this machine's secure storage). Install it from Recovery or with
    the macOS installer app, name the volume '$NODE_VOLUME', and see
    docs/HANDOFF-mac-perf-mode.md for what to turn off on it.
    A different name:  WK_BENCH_VOLUME='...' wk boot $NODE_NAME"

    m_ssh "test -x $(sh_quote "$BOOT_HELPER")" 2>/dev/null || die "the privileged boot helper is not installed on that Mac, so nothing here
    can tell the firmware which install to boot:  ./setup --stage quiesce
    Without it this is a person at the keyboard: shut down, hold the power
    button until 'Loading startup options', pick '$NODE_VOLUME', press Return."

    # The return first: --setBoot is sticky and Apple Silicon has no one-shot form, so an unproven way back is a machine that boots into bench mode forever. Blessing the running install changes nothing, and what bless answers to it is how this finds out whether the firmware takes the choice at all.
    local said
    said=$(mv_priv boot-host) || die "this Mac cannot be told to boot itself again, so it must not be told to
    boot '$NODE_VOLUME': the trip out is one way and the machine would come up
    in bench mode every time. What it answered:
${said:-(nothing -- $NODE_SSH did not answer)}
    Meanwhile the startup manager is the way: shut down, hold the power button
    until 'Loading startup options', pick '$NODE_VOLUME', press Return."
    log "$said"

    local back; back=$(mac_firmware_default)
    case "$back" in
        *"the host install"*) ;;
        *) die "bless blessed the running install and the firmware names: $back
    Nothing was armed: a return this cannot see is not a proven one, and the
    trip out to '$NODE_VOLUME' is one way." ;;
    esac

    said=$(mv_priv boot-volume) || die "the firmware would not take '$NODE_VOLUME', and nothing was changed.
    What it answered:
${said:-(nothing -- $NODE_SSH did not answer)}"
    log "$said"

    local now; now=$(mac_firmware_default)
    case "$now" in
        *"'$NODE_VOLUME'"*) info "the firmware will boot '$NODE_VOLUME' next" ;;
        *) die "bless reported success and the firmware still names: $now
    Nothing was rebooted; read that rather than working around it." ;;
    esac
}

b_disarm_note() {
    local said
    if said=$(mv_priv boot-host); then
        log "  the firmware is set back to this install; a plain reboot stays here."
        return 0
    fi
    log "  the firmware still names the benchmark volume, and this could not set it"
    log "  back:"
    log "${said:-  (nothing -- $NODE_SSH did not answer)}"
    log "  System Settings -> General -> Startup Disk is the other way."
}

b_diag() {
    local v; v=$(mac_volume_path)
    mac_volume_present || die "'$NODE_VOLUME' is not attached, so there is nothing to read."
    m_ssh "cat $(sh_quote "$v/var/log/wk-diag.txt") 2>/dev/null" \
        || echo "(no var/log/wk-diag.txt on '$NODE_VOLUME' -- it has not been provisioned, or has never booted)"
}

# Measured 2026-09-08: `«event aevtrrst»` is declined by any application that will not quit, so a graceful restart is refusable and only the helper's is unconditional.
mv_priv() { m_ssh "sudo -n $(sh_quote "$BOOT_HELPER") $1 2>&1"; }  # stderr merged: every refusal it makes is quoted back to the operator

# Answering is not being able: `status` said ok for as long as the reboot verb detached with `setsid`, which macOS does not ship, so it exited 0 having rebooted nothing. The verb names its own detach mechanism, and a helper too old to name one is a helper whose reboot cannot be trusted.
mv_reboot_ready() { mv_priv status 2>/dev/null | grep -q '^wk-boot-priv: detach='; }

b_reboot() {
    mv_priv reboot >/dev/null 2>&1 && return 0
    die "could not restart this Mac. The helper takes no password and is not
    installed there; plain sudo wants one, and an unattended transition has no
    terminal to answer it on. One command installs it:  wk boot $NODE_NAME --prepare"
}

# The *Data* volume, the APFS system volume being sealed and read-only. `/var` firmlinks out of it, so the same bytes are `/var/wk` to the booted bench install and `/Volumes/<name> - Data/private/var/wk` here.
mac_volume_data_path() {
    local d="/Volumes/$NODE_VOLUME - Data"
    m_ssh "test -d $(sh_quote "$d")" && { printf '%s' "$d"; return 0; }
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

b_media() {
    local what="bench volume '$NODE_VOLUME'"
    if mac_volume_present; then
        printf "%s attached at %s" "$what" "$(mac_volume_path)"
        return 0
    fi
    if m_reachable; then
        printf "%s MISSING on %s -- docs/HANDOFF-mac-perf-mode.md creates it" "$what" "$NODE_SSH"
        return 0
    fi
    local d
    if d=$(mv_planted_task); then
        # Two states, one silence: that install joins no network, so nothing here can tell them apart and neither may be reported as the answer.
        printf "%s: %s does not answer, with a job planted %s -- it is measuring, or it has finished and halted with the result on the volume" \
            "$what" "$NODE_SSH" "$(mv_planted_stamp "$d")"
        return 0
    fi
    printf "%s: %s does not answer and no job is planted, so this is a plain outage" "$what" "$NODE_SSH"
}

b_reprovision() {
    cat <<REPROV
wk bench mac-volume --create
    a second APFS volume in its own container, on the Mac
wk bench mac-volume --install
wk bench mac-volume --provision
hold the power button and pick the volume
    by command: wk boot mbp, which proves the way back before it arms
REPROV
}
