# Writing an image onto a disk that is attached to a machine.
#
# The vocabulary, because the old one actively misled
# ---------------------------------------------------
# This used to be two commands, `wk image flash <machine>` and `wk pi flash
# <machine> --device`, and both names were wrong in the same way:
#
#   "flash <machine>" reads as *reflash that machine* -- replace its OS, lose
#   what is on it. That is not what happens and never was. The machine's own
#   system disk is refused outright; what gets written is a removable disk that
#   happens to be plugged into it.
#
#   "flash" also implies permanence, and there is none. A machine boots one of
#   these disks *once*, by a firmware one-shot or by what a netboot server is
#   holding, and returns to its normal role by itself (`wk boot`).
#
# So there is one verb, and it says what it does to what:
#
#   wk image write <id> --disk <machine>:<device>
#
# `<machine>:<device>` rather than a bare machine, because the containment is
# the thing people get wrong: the disk is *at* the machine, the machine is not
# the target. It is spelled like `scp`'s host:path for the same reason -- it
# reads as "over there, that one" without being explained.
#
# Two facts the help text states outright, because both surprise people and
# neither is guessable:
#
#   1. a machine's own system disk can never be written -- it is refused, by
#      several independent checks;
#   2. writing a disk does not make anything boot it. That is `wk boot`, it is
#      one-shot, and the machine reverts on its own.
#
# Where the work happens
# ----------------------
# On the machine, over ssh, where sudo is passwordless. The workstation
# deliberately has no privileged component (docs/HANDOFF-linux.md, "no root, and
# no firewall"), and the disk is over there anyway. This end decides and reports.
#
# Requires machine_load() to have run, so MACH_SSH/MACH_ROOT/MACH_DEVICE are set.

# The partition device for a disk, which is not a suffix you can assume:
# /dev/sda -> /dev/sda2, but /dev/mmcblk0 -> /dev/mmcblk0p2, and likewise for
# nvme and loop. Getting this wrong grows the wrong filesystem, or none.
disk_part() {
    case "$1" in
        *[0-9]) echo "$1p$2" ;;
        *)      echo "$1$2" ;;
    esac
}

# Split `<machine>:<device>`, tolerating a bare `<machine>`.
#
# Sets DISK_MACHINE and DISK_DEV rather than printing them: two values, and a
# caller that captured them through a command substitution would have to split
# them again.
disk_parse() {
    local spec="$1"
    case "$spec" in
        *:*) DISK_MACHINE="${spec%%:*}"; DISK_DEV="${spec#*:}" ;;
        *)   DISK_MACHINE="$spec"; DISK_DEV="" ;;
    esac
    [ -n "$DISK_MACHINE" ] || die "--disk needs a machine: --disk <machine>:<device>"
}

# Every disk on the machine that could plausibly be written.
#
# RM=1 catches card readers and most sticks; TRAN catches the ones reporting
# themselves as non-removable usb or mmc, which is normal for USB SSDs and
# built-in SD slots. Whole disks only: an image carries its own partition table,
# so it is written to a disk and never to a partition.
disk_candidates() {
    m_ssh "lsblk -dpno NAME,SIZE,TRAN,RM,TYPE,MODEL" 2>/dev/null | awk '
        $5 == "disk" && ($4 == 1 || $3 == "usb" || $3 == "mmc") { print }'
}

# The listing, with the one annotation that saves a question: which of these the
# machine is actually set up to boot from. Without it, `wk boot` looks like it
# takes a disk argument it does not take.
disk_list() {
    local line name n=0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        n=$((n + 1))
        name=${line%% *}
        if [ "$name" = "${MACH_DEVICE:-}" ]; then
            printf '    %s   <- %s is configured to boot from this one (wk boot %s)\n' \
                "$line" "$MACH_NAME" "$MACH_NAME"
        else
            printf '    %s\n' "$line"
        fi
    done <<EOF
$(disk_candidates)
EOF
    [ "$n" -gt 0 ] || printf '    (none -- no removable disk is attached to %s)\n' "$MACH_NAME"
}

# Refuse before writing, in the order in which a mistake would cost most. Every
# branch names the disk and the reason: "permission denied", or a silent success
# on the wrong disk, are the two outcomes worth spending lines to avoid.
disk_refuse_unless_safe() {
    local dev="$1" bytes="$2" type tran rm size dev_bytes root_src

    m_ssh "test -b $(sh_quote "$dev")" 2>/dev/null || die "$dev is not a block device on $MACH_NAME.
    Disks there:
$(disk_list)"

    read -r type tran rm size <<EOF
$(m_ssh "lsblk -dno TYPE,TRAN,RM,SIZE $(sh_quote "$dev")" 2>/dev/null)
EOF
    [ "$type" = disk ] || die "$dev on $MACH_NAME is a '$type', not a whole disk.
    An image carries its own partition table, so it goes to the disk --
    /dev/sdb, not /dev/sdb1."

    # The mistake this whole file exists to make impossible.
    if [ "${rm:-0}" != 1 ] && [ "$tran" != usb ] && [ "$tran" != mmc ]; then
        die "refusing to write $dev on $MACH_NAME: it is not removable (RM=${rm:-?}) and
    its transport is '${tran:-unknown}', not usb or mmc. That is the signature
    of a fixed disk -- a machine's own system disk is never a target here.
    Disks there:
$(disk_list)"
    fi

    # And the checks removability cannot make: a genuinely removable disk that
    # genuinely holds the machine's system.
    case "$dev" in
        "${MACH_ROOT%%[0-9p]*}"*) die "refusing to write $dev: that is $MACH_NAME's own root disk ($MACH_ROOT)" ;;
    esac
    root_src=$(m_ssh "findmnt -no SOURCE /" 2>/dev/null || true)
    case "$root_src" in
        "$dev"*) die "refusing to write $dev: $MACH_NAME's root filesystem is on it ($root_src)" ;;
    esac

    dev_bytes=$(m_ssh "lsblk -bdno SIZE $(sh_quote "$dev")" 2>/dev/null | tr -dc '0-9')
    [ -n "$dev_bytes" ] && [ "$dev_bytes" -ge "$bytes" ] \
        || die "$dev on $MACH_NAME is ${size:-unknown}, smaller than the image ($(numfmt --to=iec "$bytes"))"
}

disk_mounted() {
    m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$1")" 2>/dev/null \
        | awk 'NF > 1 { print "/dev/" $1 " on " $2 }'
}

# Unmount, after the confirmation and never before: the caller has agreed to
# erase the disk, and a desktop automounter having grabbed the card is not a
# reason to send someone back to a hand-typed umount.
disk_unmount() {
    local dev="$1" p
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        info "unmounting $p on $MACH_NAME"
        m_ssh "udisksctl unmount -b $(sh_quote "$p") >/dev/null 2>&1 || sudo umount $(sh_quote "$p")" \
            || die "could not unmount $p on $MACH_NAME"
    done <<EOF
$(m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$dev")" 2>/dev/null | awk 'NF > 1 { print "/dev/" $1 }')
EOF
    return 0
}

# Which write method this image and this machine can use.
#
#   bmap  -- the image has a block map and a compressed original, and the
#            machine has bmaptool. Sends the *compressed* image (573 MB for a
#            4 GB wic) and writes only the blocks the map says are in use,
#            checksumming each one as it goes.
#   dd    -- everything else: stream the raw image through zstd into dd.
disk_method() {
    local id="$1"
    if [ -f "$(image_bmap "$id")" ] && [ -f "$(image_wic "$id")" ]; then
        if m_ssh 'command -v bmaptool >/dev/null' 2>/dev/null; then
            echo bmap; return 0
        fi
        # Said out loud rather than fallen back from quietly. The image *has* a
        # block map, so the slow path here is a missing package on one machine,
        # not a property of the image -- and the difference is sending 4 GB
        # instead of 573 MB and writing every byte instead of the used ones.
        warn "$MACH_NAME has no bmaptool, so this falls back to streaming the whole
  raw image. The image has a block map; installing the package makes writes
  much faster:  ssh $MACH_SSH sudo apt install -y bmap-tools" >&2
    fi
    echo dd
}

# bmaptool needs a file it can seek in, so the image has to land on the machine
# before it can be written -- it cannot be streamed. That is why the *compressed*
# wic is what gets copied: 573 MB instead of 4 GB, and bmaptool reads .xz
# directly. The temp copy is removed even if the write fails.
DISK_STAGING=""
_disk_staging_cleanup() {
    [ -n "${DISK_STAGING:-}" ] || return 0
    m_ssh "rm -rf $DISK_STAGING" >/dev/null 2>&1 || true
    DISK_STAGING=""
    return 0
}

disk_write_bmap() {
    local id="$1" dev="$2" wic bmap remote
    wic=$(image_wic "$id"); bmap=$(image_bmap "$id")
    remote=$(m_ssh 'mktemp -d /var/tmp/wk-write.XXXXXX' | tr -d '\r\n')
    [ -n "$remote" ] || die "could not make a staging directory on $MACH_NAME"

    info "sending $(basename "$wic") ($(numfmt --to=iec "$(stat -c %s "$wic")")) and its block map to $MACH_NAME"
    # Registered rather than trapped, and the path goes in a global for it to
    # read: this command holds the image-store lock, and a `trap ... EXIT` here
    # would replace the release with the staging cleanup (lib/common.sh,
    # wk_atexit). Self-cancelling -- the normal path clears the variable.
    DISK_STAGING="$remote"
    wk_atexit _disk_staging_cleanup
    m_ssh "cat > $remote/disk.wic.xz" < "$wic" || die "could not copy the image to $MACH_NAME"
    m_ssh "cat > $remote/disk.bmap"   < "$bmap" || die "could not copy the block map to $MACH_NAME"

    info "writing with bmaptool -- mapped blocks only, each checksummed against the map"
    m_ssh "sudo bmaptool copy --bmap $remote/disk.bmap $remote/disk.wic.xz $(sh_quote "$dev")" \
        || die "bmaptool failed writing to $dev on $MACH_NAME"
    m_ssh "sync"
    m_ssh "rm -rf $remote" >/dev/null 2>&1 || true
    DISK_STAGING=""
}

# Stream the raw image over ssh into dd. zstd on both ends when both have it --
# a wic image is mostly unallocated space and text, and this goes over a tailnet.
disk_write_dd() {
    local img="$1" dev="$2" bytes remote_zstd=no
    bytes=$(stat -c %s "$img")
    m_ssh 'command -v zstd >/dev/null' && remote_zstd=yes
    info "writing $(basename "$img") to $dev on $MACH_NAME ($((bytes / 1024 / 1024)) MB, zstd=$remote_zstd)"
    if [ "$remote_zstd" = yes ] && have zstd; then
        zstd -3 -c "$img" | m_ssh "zstd -dc | sudo dd of=$(sh_quote "$dev") bs=4M conv=fsync status=none"
    else
        m_ssh "sudo dd of=$(sh_quote "$dev") bs=4M conv=fsync status=none" < "$img"
    fi
    m_ssh "sync"
}

# Read back what was written and compare hashes.
#
# Only valid after a dd write. After a bmap write it would *always* fail, and
# that is not a bug to work around: bmaptool deliberately does not write the
# unmapped blocks, so those regions of the disk still hold whatever they held
# before, while the image has zeroes there. Comparing the whole span compares
# bytes nobody wrote. bmaptool checksums every block it does write, against the
# map, as it writes -- which is a stronger check than this one, not a weaker.
disk_verify_dd() {
    local img="$1" dev="$2" bytes local_sha remote_sha
    bytes=$(stat -c %s "$img")
    info "verifying by reading it back"
    local_sha=$(sha256sum "$img" | cut -d' ' -f1)
    remote_sha=$(m_ssh "sudo head -c $bytes $(sh_quote "$dev") | sha256sum" | cut -d' ' -f1)
    [ "$local_sha" = "$remote_sha" ] || die "read-back mismatch on $dev
    wrote  $local_sha
    read   $remote_sha
    The disk did not store what was sent. Try another disk or another port."
    info "read-back matches"
}

# Grow the last partition to fill the disk.
#
# An image is sized to its contents, so a 4 GB image on a 64 GB card leaves most
# of it unreachable -- and what then fails is a build or a benchmark run out of
# disk, hours later and a long way from this decision.
disk_grow() {
    local dev="$1" part; part=$(disk_part "$dev" 2)
    m_ssh 'command -v growpart >/dev/null' \
        || { warn "growpart is not installed on $MACH_NAME; the root partition stays the image's size"; return 0; }
    info "growing $part to fill the disk"
    m_ssh "sudo growpart $(sh_quote "$dev") 2" >/dev/null 2>&1 \
        || { log "  (nothing to grow -- it already fills the disk)"; return 0; }
    m_ssh "sudo e2fsck -fy $(sh_quote "$part") >/dev/null 2>&1; true"
    m_ssh "sudo resize2fs $(sh_quote "$part")" >/dev/null 2>&1 \
        || warn "resize2fs failed on $part; the filesystem is still the image's size"
    m_ssh "sync"
}

# Cut power so the disk is safe to pull. Best-effort: it needs udisks, and a
# synced disk is already safe to remove in practice.
disk_eject() {
    local dev="$1"
    m_ssh "command -v udisksctl >/dev/null" || return 0
    m_ssh "udisksctl power-off -b $(sh_quote "$dev")" >/dev/null 2>&1 \
        && info "powered off $dev -- safe to remove" \
        || log "  (could not power off $dev; it is synced, so it is safe to pull anyway)"
    return 0
}
