# Writing an image onto a disk that is attached to a machine.
#
# The vocabulary
# --------------
# One verb, and it says what it does to what:
#
#   wk sysimage write <image> --disk <machine>:<device>
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
# On the machine, over ssh, through one privileged helper -- `admin/wk-card-priv`,
# invoked as `sudo -n`. sudo on a workstation wants a password and a terminal,
# and a BatchMode ssh has no terminal to offer one on, so the helper is the only
# way privilege is reached here: there is no inline-sudo path beside it. The
# workstation this runs *from* has no privileged component ("no root, and no
# firewall"), and the disk is over there anyway. This end decides and reports.
#
# Requires machine_load() to have run, so MACH_SSH/MACH_ROOT/MACH_DEVICE are set.

# --- the privileged half ------------------------------------------------------
#
# One spelling for every privileged step on the machine the disk is attached to:
# `sudo -n`, a fixed path, a fixed verb, and arguments the helper checks against
# what they are allowed to be rather than trusting this end. There is no second
# way in -- no inline sudo, no "run it directly if the helper is missing". A
# fallback here is the path that runs on the machine nobody tested, on the day
# something is already wrong (CLAUDE.md, "One path, not two").
#
# stdin passes straight through, which is how the image reaches `write` and the
# tailscale auth key reaches `tailnet` without either touching a command line.
CARD_PRIV=/usr/local/libexec/wk-card-priv

card_priv() { # <verb> [args...]
    m_ssh "sudo -n $CARD_PRIV $(sh_quote "$@")"
}

# Asked once, before the first refusal that would otherwise report it as a
# property of the disk.
#
# Without this the machine's own "command not found" arrives indented inside
# "rpi5 will not write /dev/mmcblk0", which reads as a disk that was rejected
# rather than a machine that was never provisioned -- and names no remedy. The
# helper is installed by `./setup` on the machines that hold card readers, so
# its absence is a provisioning fact and the fix is one line long.
card_priv_require() {
    card_priv status >/dev/null 2>&1 && return 0
    die "$MACH_NAME cannot write a disk: its card helper is missing, or its
    sudoers rule is not in force. Everything privileged here goes through it and
    there is deliberately no second way in.
    What fails:  sudo -n $CARD_PRIV status
    The remedy, from a terminal on $MACH_NAME:  ./setup --stage quiesce"
}

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
# One implementation, and it is the one holding the privilege: a second copy of
# "is this safe" is a copy that can drift into permitting what the other
# refuses. So this asks, and adds the single question the helper cannot answer:
# does the image fit.
disk_refuse_unless_safe() {
    local dev="$1" bytes="$2" out dev_bytes size

    card_priv_require
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

# One way to put an image on a card, and that is the point: a second writer is a
# second code path that can only be tested with hardware in hand, for one
# behaviour -- and the one that runs least often is the one that runs on the day
# something is already wrong.
#
# The image streams through the privileged helper, which is also the only route
# to a raw device here. If a write becomes too slow to bear, make this path
# faster.

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

# Read the card back and compare it with what was sent. The read needs
# privilege, so it is a verb rather than a second way in.
#
# Unconditional, over the whole span: every byte of the image was sent, so
# comparing every byte is a claim about the disk rather than about the writer.
# A checksum computed by whatever did the writing is a strictly weaker claim --
# it says the bytes it chose to write arrived, and says nothing about the ones
# it skipped.
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

# What makes a written disk part of the fleet, rather than a system that boots
# and is invisible: the identity marker every wk-written system carries
# (/etc/wk-image), which is what `b_probe` reads to tell a bench system from a
# machine that never left host mode, and the driving machine's ssh key in root's
# authorized_keys. A Yocto image ships `PermitRootLogin yes` with an empty root
# password -- something a person can use and `ssh -o BatchMode=yes` cannot -- so
# without the key the board comes up perfectly and nothing here can reach it.
#
# Both values go over as base64 on the command line. Neither is a secret (the
# marker is provenance, the key is a public key), both are multi-line, and the
# encoding is what lets the helper check them against a character set before it
# decodes anything -- a stronger position than escaping them through ssh into
# sudo. It is also the fix for the failure recorded in docs/TESTING.md: they
# were once split out of one stdin on a blank line, `sed` read the stream in
# buffered chunks, and every card came out with a perfect marker and an empty
# authorized_keys.
disk_install_fleet() { # <device> <marker> <ssh public key>
    local dev="$1" marker="$2" key="$3" m64 k64
    m64=$(printf '%s' "$marker" | base64 | tr -d '\n\r ')
    k64=$(printf '%s' "$key"    | base64 | tr -d '\n\r ')
    [ -n "$m64" ] && [ -n "$k64" ] \
        || die "refusing to install a fleet identity with an empty marker or key"

    info "installing the identity marker and the driving key on $dev"
    card_priv fleet "$dev" "$m64" "$k64" >/dev/null \
        || die "could not install the fleet integration on $dev.
    The image is written, but the board would boot with no /etc/wk-image and no
    key in root's authorized_keys: unreachable by anything here, and
    indistinguishable from the machine's host mode."
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
# Only for an image that asked for it, and the card is asked rather than
# guessed. A card with no wk-tailnet-join is a bench system (image/profiles.sh:
# "the image has no tailscale and never will"), a bridge image or a rescue, and
# for one of those this would resolve the fleet's auth key, send it to whichever
# machine holds the reader and write it to a file there -- a credential in one
# more place, for a disk that has nothing to spend it.
disk_seed_tailnet() { # <device> <tailnet hostname>
    local dev="$1" name="$2" keyfile joins tag="${WK_TAILNET_TAG:-tag:wk}"

    # Three outcomes, not two: yes, no, and "the question was not answered".
    # A grep that finds nothing looks exactly like a no, and a no here means the
    # board silently boots without a tailnet identity -- the state the fleet
    # rule exists to end, arrived at by a helper that failed rather than by a
    # decision anyone made.
    joins=$(card_priv joins "$dev" 2>&1) \
        || die "could not tell whether $dev joins the tailnet on first boot:
$(printf '%s\n' "$joins" | sed 's/^/    /')
    The image is written. Refusing to guess: a card that joins and was not
    seeded comes up with no tailnet identity, and a card that does not would be
    left holding a credential it cannot spend."
    case "$joins" in
        *'tailnet-join: yes'*) ;;
        *'tailnet-join: no'*)
            debug "$dev carries no wk-tailnet-join, so there is nothing to seed"
            return 0 ;;
        *) die "$MACH_NAME's card helper did not say whether $dev joins the tailnet
    (it said: ${joins:-nothing}). Refusing to guess, for the same reason." ;;
    esac

    [ -n "$name" ] || barrier "this image joins the tailnet on first boot, and nothing here
    knows what name it should answer to -- the image records no machine, so the
    card would join under the image's own hostname and be reachable by a name
    the fleet does not use. Write it for a machine, or --force to let it pick."

    keyfile=$(wk_tailscale_authkey) || barrier "this image joins the tailnet on first boot and there is no auth
    key here to give it. The board would boot reachable only over whatever LAN
    it lands on, which is the state the fleet rule exists to end (CLAUDE.md,
    'Cattle, not pets')."
    [ -n "${keyfile:-}" ] || return 0   # forced past the barrier

    info "seeding the tailnet identity onto $dev -- it joins as '$name' ($tag) on first boot"
    # The key goes in over the same ssh and never onto a command line, the same
    # rule as everywhere else it moves; the helper reads it off stdin. What was
    # written is read back before it is unmounted, on the far side, because the
    # only end that can see the card is the end holding the privilege.
    card_priv tailnet "$dev" "$name" "$tag" < "$keyfile" >/dev/null \
        || die "could not seed the tailnet identity onto $dev.
    The image is written; it would boot with no tailnet identity and be
    reachable only over whatever LAN it lands on."
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
