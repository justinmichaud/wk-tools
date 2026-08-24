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
# On the machine, over ssh, where sudo is passwordless. The workstation
# deliberately has no privileged component ("no root, and no firewall"),
# and the disk is over there anyway. This end decides and reports.
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
        # Provenance before speed. A compressed copy that predates the last
        # edit to disk.img would write a board that boots without the fleet
        # integration or the retargeted root, and would do it silently -- see
        # image_fast_path_ok.
        if ! image_fast_path_ok "$id"; then
            warn "$id's compressed copy does not record which disk.img it came from,
  so it may not be the image beside it. Writing the raw image instead (slower,
  and verified by read-back). 'wk sysimage retarget $id' re-derives it." >&2
            echo dd; return 0
        fi
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

    info "sending $(basename "$wic") ($(human_bytes "$(file_bytes "$wic")")) and its block map to $MACH_NAME"
    # Registered rather than trapped, and the path goes in a global for it to
    # read: this command holds the image-store lock, and a `trap ... EXIT` here
    # would replace the release with the staging cleanup (lib/common.sh,
    # wk_atexit). Self-cancelling -- the normal path clears the variable.
    DISK_STAGING="$remote"
    wk_atexit _disk_staging_cleanup
    m_ssh "cat > $remote/disk.wic.xz" < "$wic" || die "could not copy the image to $MACH_NAME"
    m_ssh "cat > $remote/disk.bmap"   < "$bmap" || die "could not copy the block map to $MACH_NAME"

    # Zero the image's extent first, and this is a correctness step rather than
    # hygiene.
    #
    # bmaptool writes only the mapped blocks; the unmapped ones keep whatever the
    # destination already held. The note on disk_verify_dd below says so, and the
    # note in cmd/sysimage's refresh_fast_path argues it is safe because a hole is
    # "filesystem free space that was never written". That argument holds for a
    # blank card and fails for a reused one, because free space is not
    # don't-care: on FAT, a free directory slot and a free FAT entry are *defined*
    # as zeros, so leaving a previous filesystem's bytes there does not leave
    # unused space, it leaves entries. Measured 2026-08-22 writing an rpi3 image
    # onto a card that had held a PinePhone system: 100 MB of the 130 MB boot
    # partition went unwritten, the root directory region among it, and the
    # result mounted with garbage entries beside the real ones, an I/O error on
    # readdir, and fsck reporting files with start clusters past the end of the
    # partition. Every block bmaptool did write was correct and checksummed --
    # which is exactly why nothing caught it. The map's checksums cover what was
    # written, never what was not, and the bmap path has no read-back check.
    #
    # Only the image's own extent, not the whole card: what lies past it is not
    # this image's business, and on a 64 GB card holding a 3.3 GB image, zeroing
    # the rest would cost far more than the write.
    local bytes
    bytes=$(file_bytes "$(image_disk "$id")")
    info "zeroing the first $(human_bytes "$bytes") of $dev (bmaptool skips holes; a reused disk keeps its old bytes there)"
    m_ssh "sudo blkdiscard -z --offset 0 --length $bytes $(sh_quote "$dev") 2>/dev/null \
           || sudo dd if=/dev/zero of=$(sh_quote "$dev") bs=4M count=$(( (bytes + 4194303) / 4194304 )) conv=fsync status=none" \
        || die "could not zero $dev on $MACH_NAME before writing"

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
    bytes=$(file_bytes "$img")
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
disk_verify_dd() {
    local img="$1" dev="$2" bytes local_sha remote_sha
    bytes=$(file_bytes "$img")
    info "verifying by reading it back"
    local_sha=$(sha256sum "$img" | cut -d' ' -f1)
    remote_sha=$(m_ssh "sudo head -c $bytes $(sh_quote "$dev") | sha256sum" | cut -d' ' -f1)
    [ "$local_sha" = "$remote_sha" ] || die "read-back mismatch on $dev
    wrote  $local_sha
    read   $remote_sha
    The disk did not store what was sent. Try another disk or another port."
    info "read-back matches"
}

# Make the written disk's identity its own.
#
# A raw image write copies the MBR disk signature along with everything else,
# and `PARTUUID=` *is* that signature plus a partition number. So every disk
# ever written from one image answers to the same `root=`, and which one the
# kernel picks when two are attached is enumeration order.
#
# That is not a hypothetical. The rpi4's SD rescue and its bench stick were
# written from the same Yocto wic; both carried `0x076c4a2a`; the board loaded
# the *stick's* kernel and mounted the *card's* root filesystem, and came up as
# a system that was neither of the two -- with the stick's fleet integration
# missing, because the running rootfs was the card's. `/proc/cmdline` said
# `root=PARTUUID=076c4a2a-02` and `findmnt /` said `/dev/mmcblk0p2`, which is
# the whole bug in two lines. It is the same ambiguity image/profiles.sh
# records for filesystem labels, reached through the partition table instead,
# and it survives every check on the writing side because nothing about the
# write is wrong.
#
# So uniqueness is stamped per *disk*, here, where the ambiguity actually
# lives. The image keeps a portable root spec -- that is what lets it be
# written to a card or a stick at all -- and this makes the copy on this disk
# name itself. Both readers have to agree: `root=` in cmdline.txt, which the
# kernel resolves, and `/boot` in /etc/fstab, which mount(8) resolves later.
#
# After the read-back verification, necessarily: this changes the disk on
# purpose, so the bytes stop matching the image here and not before.
disk_unique_identity() {
    local dev="$1" spec="$2" old new p1 p2
    case "$spec" in PARTUUID=*) ;; *) return 0 ;; esac
    old=${spec#PARTUUID=}; old=${old%-*}
    new=$(od -An -tx4 -N4 /dev/urandom | tr -d ' \n')
    [ -n "$new" ] || { warn "could not generate a disk identity; $dev keeps $old"; return 0; }
    p1=$(disk_part "$dev" 1); p2=$(disk_part "$dev" 2)

    info "stamping a unique identity on $dev (0x$old -> 0x$new), so it cannot be confused with another copy"
    m_ssh "set -e
        sudo sfdisk --disk-id $(sh_quote "$dev") 0x$new >/dev/null
        m=\$(mktemp -d)
        sudo mount $(sh_quote "$p1") \$m
        sudo sed -i 's/PARTUUID=$old-/PARTUUID=$new-/g' \$m/cmdline.txt
        sudo umount \$m
        sudo mount $(sh_quote "$p2") \$m
        [ -f \$m/etc/fstab ] && sudo sed -i 's/PARTUUID=$old-/PARTUUID=$new-/g' \$m/etc/fstab
        sudo umount \$m
        rmdir \$m
        sync" >/dev/null 2>&1 \
        || die "could not stamp a unique identity on $dev.
    The image is written and verified, but its root is still PARTUUID=$old-2 --
    the same as any other disk written from this image. Booted next to one of
    them, the kernel may mount the wrong root."

    # Read back rather than trusted: this is the check whose absence let the
    # original confusion reach a board.
    m_ssh "sudo blkid -o value -s PARTUUID $(sh_quote "$p2")" 2>/dev/null | tr -d '\r\n ' \
        | grep -qx "$new-02" \
        || die "$dev did not take the new identity; refusing to leave it ambiguous"
}

# The tailnet identity, onto the card that was just written.
#
# This is the half of "everything wk touches is on the tailnet" that cannot live
# in the image. The image carries tailscale and the join
# (image/yocto/meta-wk-tailnet); what it must not carry is the auth key or the
# name, and for different reasons:
#
#   the key   is a credential, and an image is an artifact that is stored,
#             compressed, copied between machines and kept after it is
#             superseded. A key baked into one is a key in every copy of it,
#             revocable only by revoking it for the whole fleet. On a card it
#             exists for one boot -- wk-tailnet-join deletes it once it has been
#             spent.
#   the name  is a property of the machine the card goes into, not of the image:
#             the same image is written for more than one board, and the name a
#             node has to answer to is the fleet's name for the machine. An
#             image that joined under its own hostname would be on the tailnet
#             under a name nothing else here uses -- which is a mapping, written
#             down somewhere, which is the whole thing the rule forbids.
#
# Only for an image that asked for it. The probe is the image's own join script:
# a card with no wk-tailnet-join is a bridge image, a rescue system or an older
# build, and seeding a key into one would be leaving a credential on a disk that
# has nothing to spend it.
disk_seed_tailnet() { # <device> <tailnet hostname>
    local dev="$1" name="$2" p2 keyfile tag="${WK_TAILNET_TAG:-tag:wk}"
    p2=$(disk_part "$dev" 2)

    m_ssh "set -e
        m=\$(mktemp -d)
        sudo mount $(sh_quote "$p2") \$m
        test -x \$m/usr/sbin/wk-tailnet-join && echo has-join || true
        sudo umount \$m; rmdir \$m" 2>/dev/null | grep -q has-join || {
        debug "$p2 carries no wk-tailnet-join, so there is nothing to seed"
        return 0
    }

    [ -n "$name" ] || barrier "this image joins the tailnet on first boot, and nothing here
    knows what name it should answer to -- the image records no machine, so the
    card would join under the image's own hostname and be reachable by a name
    the fleet does not use. Write it for a machine, or --force to let it pick."

    keyfile=$(wk_tailscale_authkey) || barrier "this image joins the tailnet on first boot and there is no auth
    key here to give it. The board would boot reachable only over whatever LAN
    it lands on, which is the state the fleet rule exists to end (CLAUDE.md,
    'Cattle, not pets')."
    [ -n "${keyfile:-}" ] || return 0   # forced past the barrier

    info "seeding the tailnet identity onto $p2 -- it joins as '$name' ($tag) on first boot"
    # The key goes in over the same ssh and never onto a command line, the same
    # rule as everywhere else it moves. `dd` rather than a redirect inside
    # `sudo sh -c`, so that nothing here has to nest three levels of quoting
    # around a secret.
    m_ssh "set -e
        m=\$(mktemp -d)
        sudo mount $(sh_quote "$p2") \$m
        sudo mkdir -p \$m/etc/wk
        sudo chmod 0755 \$m/etc/wk
        printf 'hostname=%s\ntag=%s\n' $(sh_quote "$name") $(sh_quote "$tag") \
            | sudo tee \$m/etc/wk/tailnet.conf >/dev/null
        sudo chmod 0644 \$m/etc/wk/tailnet.conf
        sudo dd of=\$m/etc/wk/tailscale-authkey status=none
        sudo chmod 0600 \$m/etc/wk/tailscale-authkey
        sudo chown 0:0 \$m/etc/wk/tailnet.conf \$m/etc/wk/tailscale-authkey
        sudo umount \$m; rmdir \$m
        sync" < "$keyfile" \
        || die "could not seed the tailnet identity onto $p2.
    The image is written; it will boot with no tailnet identity and be reachable
    only over whatever LAN it lands on."

    # Read back what can be read back. The key deliberately is not: its size and
    # mode answer 'did it land', and printing a credential to prove it arrived
    # would be the same mistake as putting it on a command line.
    local got
    got=$(m_ssh "set -e
        m=\$(mktemp -d)
        sudo mount -o ro $(sh_quote "$p2") \$m
        sudo sed -n 's/^hostname=//p' \$m/etc/wk/tailnet.conf | head -1
        sudo stat -c '%a %s' \$m/etc/wk/tailscale-authkey
        sudo umount \$m; rmdir \$m" 2>/dev/null | tr '\r' ' ')
    case "$got" in
        *"$name"*"600 "*) debug "tailnet identity verified on $p2" ;;
        *) die "the tailnet identity did not survive the write to $p2 (read back: ${got:-nothing}).
    Refusing to report a card as seeded when the board would come up with no
    tailnet identity and nothing to say so." ;;
    esac
}

# Grow the last partition to fill the disk.
#
# An image is sized to its contents, so a 4 GB image on a 64 GB card leaves most
# of it unreachable -- and what then fails is a build or a benchmark run out of
# disk, hours later and a long way from this decision.
disk_grow() {
    local dev="$1" part; part=$(disk_part "$dev" 2)
    info "growing $part to fill the disk"

    # Two ways, because "install growpart" is not advice every machine here can
    # take: cloud-utils is a Debian package and the rpi4's rescue system is a
    # Yocto image with no apt at all. It does ship sfdisk, partx and resize2fs,
    # which between them do the same job -- `, +` keeps partition 2's start and
    # takes it to the end of the disk, which is the whole of what growpart was
    # doing here.
    if m_ssh 'command -v growpart >/dev/null' 2>/dev/null; then
        m_ssh "sudo growpart $(sh_quote "$dev") 2" >/dev/null 2>&1 \
            || { log "  (nothing to grow -- it already fills the disk)"; return 0; }
    elif m_ssh 'command -v sfdisk >/dev/null' 2>/dev/null; then
        m_ssh "printf ', +\n' | sudo sfdisk -N 2 --no-reread --force $(sh_quote "$dev")" >/dev/null 2>&1 \
            || { log "  (nothing to grow -- it already fills the disk)"; return 0; }
        m_ssh "sudo partx -u $(sh_quote "$dev")" >/dev/null 2>&1 || true
    else
        warn "$MACH_NAME has neither growpart nor sfdisk; the root partition stays the image's size"
        return 0
    fi
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
