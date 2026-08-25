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
#   these disks *once*, by a firmware one-shot, and returns to host mode by
#   itself (`wk boot`).
#
# So there is one verb, and it says what it does to what:
#
#   wk sysimage write <id> --disk <machine>:<device>
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
# On the machine, over ssh, and through one privileged helper --
# `admin/wk-card-priv`, invoked as `sudo -n`. Not "where sudo is passwordless",
# which is what this said and was never true of a workstation: sudo there wants
# a password and a terminal, and there is no terminal down a BatchMode ssh.
# There is deliberately no second, inline-sudo way in. The workstation this runs
# *from* still has no privileged component ("no root, and no firewall"), and the
# disk is over there anyway. This end decides and reports.
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

# May this device be written? Asked of the helper, which is the machine that
# will do the writing and the only implementation of the rule.
#
# It used to be a second copy of the same checks, here -- whole disk, removable
# or usb/mmc, not the machine's own root. Two implementations of "is this safe"
# is one that can drift into permitting what the other refuses, and the one that
# matters is the one holding the privilege. So this asks, and adds the single
# question the helper cannot answer: does the image fit.
disk_refuse_unless_safe() {
    local dev="$1" bytes="$2" out dev_bytes size

    out=$(card_priv check "$dev" 2>&1) || die "$MACH_NAME will not write $dev:
$(printf '%s\n' "$out" | sed 's/^/    /')
    Disks there:
$(disk_list)"
    debug "$out"

    dev_bytes=$(m_ssh "lsblk -bdno SIZE $(sh_quote "$dev")" 2>/dev/null | tr -dc '0-9')
    size=$(m_ssh "lsblk -dno SIZE $(sh_quote "$dev")" 2>/dev/null | tr -d ' \r')
    [ -n "$dev_bytes" ] && [ "$dev_bytes" -ge "$bytes" ] \
        || die "$dev on $MACH_NAME is ${size:-unknown}, smaller than the image ($(human_bytes "$bytes"))"
}

disk_mounted() {
    m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$1")" 2>/dev/null \
        | awk 'NF > 1 { print "/dev/" $1 " on " $2 }'
}

# Unmount, after the confirmation and never before: the caller has agreed to
# erase the disk, and a desktop automounter having grabbed the card is not a
# reason to send someone back to a hand-typed umount.
disk_unmount() {
    local dev="$1"
    # Through the helper, like every other privileged step here. It is the one
    # verb allowed to see mounted filesystems -- unmounting them is its purpose
    # -- and it still refuses a disk this machine is running from.
    card_priv unmount "$dev" >/dev/null \
        || die "could not unmount what is on $dev on $MACH_NAME.
    Something is using it:
$(m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$dev")" 2>/dev/null | awk 'NF > 1 { print "    /dev/" $1 " at " $2 }')"
}

# There is one way to put an image on a card, and that is the point.
#
# There used to be two: a bmaptool path that sent the compressed image plus a
# block map and wrote only the mapped blocks, and a dd path for machines without
# bmaptool. Two writers is two code paths that can only be tested with hardware
# in hand, for one behaviour -- and the fast one existed to consume `disk.wic.xz`
# and `disk.bmap`, which were *store* artifacts. With no store there is nothing
# to feed it (wk help images), so it went with the store rather than being kept
# as a second thing to keep working. What is left streams the image through the
# privileged helper, which is also the only route to a raw device here.
#
# If a write ever becomes too slow to bear, the answer is to make the one path
# faster, not to add a second one back.

disk_write_stream() { # <device>   -- image bytes on stdin
    local dev="$1" remote_zstd=no
    m_ssh 'command -v zstd >/dev/null' && remote_zstd=yes
    info "writing to $dev on $MACH_NAME (streamed, zstd=$remote_zstd)"
    # The decompression runs unprivileged on the far side; only the plain stream
    # reaches the privileged writer, so the verb stays "write these bytes to
    # that device" and nothing more.
    if [ "$remote_zstd" = yes ] && have zstd; then
        zstd -3 -c | m_ssh "zstd -dc | sudo -n $CARD_PRIV write $(sh_quote "$dev")"
    else
        card_priv write "$dev"
    fi
}

# Writing a file is writing a stream with the file on stdin, and it is spelled
# that way rather than reimplemented: two ways to put bytes on a card is two
# code paths to test, on hardware, for one behaviour.
disk_write_dd() {
    local img="$1" dev="$2" bytes
    bytes=$(file_bytes "$img")
    info "writing $(basename "$img") ($((bytes / 1024 / 1024)) MB)"
    disk_write_stream "$dev" < "$img"
}

# Read back what was written and compare hashes.
#
# Only valid after a dd write, and the reason is worth stating carefully now
# that disk_write_bmap zeros first.
#
# It used to be that a bmap write left the unmapped regions holding whatever
# they held before, so comparing the whole span compared bytes nobody wrote.
# That is what the zeroing fixed. What has not changed is that bmaptool's
# checksums cover only the blocks it writes -- a strictly weaker claim than a
# read-back, not a stronger one, and believing otherwise is what let a corrupt
# card through on 2026-08-22. A read-back over the bmap path is now meaningful
# whenever the image's unmapped regions really are holes; that is true of every
# image refresh_fast_path re-derived, and not guaranteed of a map bitbake wrote
# from free-space data, so it is not switched on here blindly.
# Read the card back and compare it with what was sent. The read needs
# privilege, so it is a verb rather than a second way in.
disk_verify_dd() {
    local img="$1" dev="$2" bytes local_sha remote_sha
    bytes=$(file_bytes "$img")
    info "verifying $dev against $(basename "$img")"
    local_sha=$(head -c "$bytes" "$img" | shasum -a 256 2>/dev/null | cut -d' ' -f1)
    remote_sha=$(card_priv verify "$dev" "$bytes" | tr -d '\r' | tail -1)
    [ -n "$remote_sha" ] || die "could not read $dev back on $MACH_NAME"
    [ "$local_sha" = "$remote_sha" ] \
        || die "$dev does not match the image that was written to it
    image: $local_sha
    disk:  $remote_sha"
    debug "verified $bytes bytes"
}

disk_unique_identity() {
    local dev="$1" spec="$2" old new
    case "$spec" in PARTUUID=*) ;; *) return 0 ;; esac
    old=${spec#PARTUUID=}; old=${old%-*}
    new=$(od -An -tx4 -N4 /dev/urandom | tr -d ' \n')
    [ -n "$new" ] || { warn "could not generate a disk identity; $dev keeps $old"; return 0; }

    info "stamping a unique identity on $dev (0x$old -> 0x$new), so it cannot be confused with another copy"
    card_priv identity "$dev" "$old" "$new" \
        || die "could not stamp a unique identity on $dev.
    The image is written and verified, but its root is still PARTUUID=$old-2 --
    the same as any other disk written from this image. Booted next to one of
    them, the kernel may mount the wrong root."

    # Read back rather than trusted: this is the check whose absence let the
    # original confusion reach a board.
    m_ssh "lsblk -no PARTUUID $(sh_quote "$(disk_part "$dev" 2)")" 2>/dev/null | tr -d '\r ' \
        | grep -qx "$new-02" \
        || die "$dev did not take the new identity; refusing to leave it ambiguous"
}

# Grow the last partition to fill the disk.
#
# An image is sized to its contents, so a 4 GB image on a 64 GB card leaves most
# of it unreachable -- and what then fails is a build or a benchmark run out of
# disk, hours later and a long way from this decision.
disk_grow() {
    local dev="$1"
    info "growing the last partition to fill $dev"
    card_priv grow "$dev" >/dev/null \
        || die "could not grow the root partition on $dev"
}

disk_eject() {
    local dev="$1"
    m_ssh "command -v udisksctl >/dev/null" || return 0
    m_ssh "udisksctl power-off -b $(sh_quote "$dev")" >/dev/null 2>&1 \
        && info "powered off $dev -- safe to remove" \
        || log "  (could not power off $dev; it is synced, so it is safe to pull anyway)"
    return 0
}
