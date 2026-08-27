# Writes an image onto a disk attached to a machine: `wk sysimage write
# <image> --disk <machine>:<device>`, spelled like `scp`'s host:path because
# the disk is *at* the machine, not the machine itself. A machine's own
# system disk is always refused; writing a disk does not make anything boot
# it (that is `wk boot`, one-shot).
#
# The work happens on the machine, over ssh, through one privileged helper
# (`admin/wk-card-priv`, invoked as `sudo -n`) -- the only way in, since a
# BatchMode ssh has no terminal for sudo to prompt on. Requires
# machine_load() to have run, so MACH_SSH/MACH_ROOT/MACH_DEVICE are set.

# --- the privileged half ------------------------------------------------------
# One spelling for every privileged step: `sudo -n`, a fixed path, a fixed
# verb, arguments the helper checks itself rather than trusting this end. No
# inline sudo, no fallback if the helper is missing. stdin passes straight
# through, which is how the image reaches `write` and the tailscale auth key
# reaches `tailnet` without touching a command line.
CARD_PRIV=/usr/local/libexec/wk-card-priv

card_priv() { # <verb> [args...]
    m_ssh "sudo -n $CARD_PRIV $(sh_quote "$@")"
}

# Asked once, before the first refusal that would otherwise blame the disk
# for a missing helper: without this, "command not found" arrives indented
# inside "rpi5 will not write /dev/mmcblk0" and names no remedy.
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

# Split `<machine>:<device>`, tolerating a bare `<machine>`. Sets
# DISK_MACHINE/DISK_DEV rather than printing them, since a caller capturing
# two values via command substitution would only have to split them again.
disk_parse() {
    local spec="$1"
    case "$spec" in
        *:*) DISK_MACHINE="${spec%%:*}"; DISK_DEV="${spec#*:}" ;;
        *)   DISK_MACHINE="$spec"; DISK_DEV="" ;;
    esac
    [ -n "$DISK_MACHINE" ] || die "--disk needs a machine: --disk <machine>:<device>"
}

# Every disk on the machine that could plausibly be written. RM=1 catches
# card readers and most sticks; TRAN catches non-removable usb/mmc, normal
# for USB SSDs and built-in SD slots. Whole disks only, since an image
# carries its own partition table.
disk_candidates() {
    m_ssh "lsblk -dpno NAME,SIZE,TRAN,RM,TYPE,MODEL" 2>/dev/null | awk '
        $5 == "disk" && ($4 == 1 || $3 == "usb" || $3 == "mmc") { print }'
}

# Kernel names like sda/sdb are enumeration order, not identity, and swap
# across a reboot, so which disk holds a wk system is read from the disk's
# identity marker (/etc/wk-image, `machine=`), not from a filesystem label
# (a yocto image labels boot/root like any other) or a declared serial in a
# conf (a second copy of a fact the first swapped stick makes a lie). Empty
# means a blank disk, one somebody else wrote, or a helper too old to have
# the verb -- kept apart from "this end cannot even look" (disk_can_identify).
_WK_WHOSE_CAP=""
disk_can_identify() {
    if [ -z "$_WK_WHOSE_CAP" ]; then
        case "$(card_priv whose 2>&1 </dev/null)" in
            *'usage: wk-card-priv'*) _WK_WHOSE_CAP=no ;;
            *) _WK_WHOSE_CAP=yes ;;
        esac
    fi
    [ "$_WK_WHOSE_CAP" = yes ]
}

# A mount per disk, read-only, on the machine holding it: cached for the
# process because a listing asks about every disk and a probe is not free.
_WK_WHOSE=""
disk_image_machine() { # <device>
    local dev="$1" hit out
    disk_can_identify || return 1

    hit=$(printf '%s\n' "$_WK_WHOSE" | awk -v d="$dev" '$1 == d { print $2; exit }')
    [ -z "$hit" ] || { [ "$hit" = "-" ] || printf '%s' "$hit"; return 0; }

    out=$(card_priv whose "$dev" </dev/null 2>/dev/null) || out=""
    hit=$(kv_get machine <<<"$out")
    _WK_WHOSE="$_WK_WHOSE
$dev ${hit:--}"
    [ -z "$hit" ] || printf '%s' "$hit"
}

# The transport a declared device name implies. Derived rather than declared:
# a conf that named both the device and its transport would be stating one fact
# twice, and the two could disagree.
disk_tran_of_name() {
    case "$1" in
        /dev/sd*)     printf usb ;;
        /dev/mmcblk*) printf mmc ;;
        /dev/nvme*)   printf nvme ;;
    esac
}

# The machine's own medium, resolved from the machine rather than trusted
# from its conf: `MACH_DEVICE` is a kernel name, and on a board that doubles
# as the fleet's card reader another board's stick can take that name --
# the arming path would then write a partition type byte straight to it.
# Exclusion, not recognition: start with disks of the expected transport,
# drop the ones marked for another machine; what is left needs no marker of
# its own. Prints nothing when more than one survives, so the caller refuses
# rather than picks.
disk_resolve_own() {
    local want same n dev owner left=""
    want=$(disk_tran_of_name "${MACH_DEVICE:-}")
    [ -n "$want" ] || return 1

    same=$(disk_candidates | awk -v t="$want" '$3 == t { print $1 }')
    n=$(printf '%s\n' "$same" | grep -c . || true)
    [ "$n" = 0 ] && return 1
    if [ "$n" = 1 ]; then printf '%s' "$same"; return 0; fi

    for dev in $same; do
        owner=$(disk_image_machine "$dev" || true)
        # Another machine's system: definitely not this machine's medium.
        [ -n "$owner" ] && [ "$owner" != "$MACH_NAME" ] && continue
        # This machine's own system named outright ends it -- no need to keep
        # looking, and it is the strongest evidence available.
        [ "$owner" = "$MACH_NAME" ] && { printf '%s' "$dev"; return 0; }
        left="$left $dev"
    done

    set -- $left
    [ "$#" = 1 ] || return 1
    printf '%s' "$1"
}

# The device the machine's own medium is at right now. Falls back to the
# declared name only when the machine could not be asked, and says so.
disk_own_or_declared() {
    local got
    got=$(disk_resolve_own 2>/dev/null) || got=""
    if [ -z "$got" ]; then
        debug "could not resolve $MACH_NAME's own medium from the machine; using ${MACH_DEVICE:-none} as declared"
        printf '%s' "${MACH_DEVICE:-}"
        return 0
    fi
    if [ "$got" != "${MACH_DEVICE:-}" ]; then
        warn "$MACH_NAME's conf says $MACH_DEVICE, but its own medium is $got right now.
  Kernel names move; this is using $got, which is what the machine says."
    fi
    printf '%s' "$got"
}

# disk_for_machine <machine> -- the disk on the machine m_ssh is aimed at
# that holds <machine>'s system, or nothing. A blank disk matches nothing:
# the first write to a fresh stick has to name it; every write after does not.
disk_for_machine() { # <machine>
    local want="$1" dev
    [ -n "$want" ] || return 1
    for dev in $(disk_candidates | awk '{print $1}'); do
        [ "$(disk_image_machine "$dev")" = "$want" ] && { printf '%s' "$dev"; return 0; }
    done
    return 1
}

disk_contents() {
    m_ssh "lsblk -rno NAME,TYPE,LABEL,PKNAME 2>/dev/null" 2>/dev/null | awk '
        $2 == "part" && $4 != "" {
            lab = ($3 == "" ? "-" : $3)
            labels["/dev/" $4] = labels["/dev/" $4] (labels["/dev/" $4] ? "," : "") lab
            if (lab == "WK-IMG-BOOT" || lab == "wk-image-root") wk["/dev/" $4] = 1
        }
        END {
            for (d in labels)
                print d "\t" "labels: " labels[d] (wk[d] ? "  (written by the distro path)" : "")
        }'
}

disk_list() {
    local line name n=0 contents desc owner tran
    contents=$(disk_contents)

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        n=$((n + 1))
        name=${line%% *}
        tran=$(printf '%s' "$line" | awk '{print $3}')
        desc=$(printf '%s\n' "$contents" | awk -F'\t' -v d="$name" '$1 == d { print $2; exit }')
        [ -n "$desc" ] || desc="empty -- no partition table"

        # `|| true`: an unavailable answer is a line of the listing, not the
        # end of it.
        owner=$(disk_image_machine "$name" || true)

        if [ "$name" = "${MACH_DEVICE:-}" ]; then
            printf '    %s   <- %s is configured to boot from this one (wk boot %s)\n' \
                "$line" "$MACH_NAME" "$MACH_NAME"
        else
            printf '    %s\n' "$line"
        fi
        if [ -n "$owner" ]; then
            printf '        %s  --  holds %s\n' "$desc" \
                "$([ "$owner" = "$MACH_NAME" ] && printf "this machine's own system" \
                                               || printf "a system for %s" "$owner")"
        elif disk_can_identify; then
            printf '        %s  --  no wk system on it\n' "$desc"
        else
            printf '        %s\n' "$desc"
        fi
    done <<EOF
$(disk_candidates)
EOF
    [ "$n" -gt 0 ] || printf '    (none -- no removable disk is attached to %s)\n' "$MACH_NAME"

    disk_can_identify || {
        warn "cannot tell which system is on any of these: $MACH_NAME's card helper is
  older than this checkout and has no 'whose' verb. The disks are listed by what
  their filesystems are labelled, which is not the same question.
  Remedy, from a terminal on $MACH_NAME:  ./setup --stage quiesce"
    }

    # The machine wins when the record and it disagree (CLAUDE.md). `sd*`
    # names are enumeration order, and this board doubles as the fleet's
    # card reader, so other boards' media sit beside its own.
    local mine
    mine=$(disk_for_machine "$MACH_NAME" 2>/dev/null) || mine=""
    if [ -n "${MACH_DEVICE:-}" ] && [ -n "$mine" ] && [ "$mine" != "$MACH_DEVICE" ]; then
        warn "$MACH_NAME's conf says it boots $MACH_DEVICE, but the disk holding
  $MACH_NAME's own system is $mine. Kernel names are assigned in enumeration
  order and these have moved. Trust the marker, not the name."
    fi
}

# May this device be written? Asked of the helper, the only implementation
# of the rule -- a second copy can drift into permitting what the other
# refuses. Adds the one question the helper cannot answer: does the image fit.
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
    # Through the helper, like every other privileged step: it still refuses
    # a disk this machine is running from.
    card_priv unmount "$dev" >/dev/null \
        || die "could not unmount what is on $dev on $MACH_NAME.
    Something is using it:
$(m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$dev")" 2>/dev/null | awk 'NF > 1 { print "    /dev/" $1 " at " $2 }')"
}

disk_write_stream() { # <device>   -- image bytes on stdin
    local dev="$1" remote_zstd=no
    m_ssh 'command -v zstd >/dev/null' && remote_zstd=yes
    info "writing to $dev on $MACH_NAME (streamed, zstd=$remote_zstd)"
    # Decompression runs unprivileged on the far side; only the plain stream
    # reaches the privileged writer.
    if [ "$remote_zstd" = yes ] && have zstd; then
        zstd -3 -c | m_ssh "zstd -dc | sudo -n $CARD_PRIV write $(sh_quote "$dev")"
    else
        card_priv write "$dev"
    fi
}

# Writing a file is writing a stream with the file on stdin -- one writer,
# not a second one to test on hardware.
disk_write_dd() {
    local img="$1" dev="$2" bytes
    bytes=$(file_bytes "$img")
    info "writing $(basename "$img") ($((bytes / 1024 / 1024)) MB)"
    disk_write_stream "$dev" < "$img"
}

# Read the card back and compare with what was sent -- privileged, hence a
# verb rather than a second way in, and unconditional over the whole span: a
# checksum from whatever did the writing only claims the bytes it chose to
# write arrived, and says nothing about the ones it skipped.
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

# The streamed write has no local file to re-read, so the caller passes the
# size and hash it already computed instead of a path. Otherwise the same
# claim as disk_verify_dd: read the disk back rather than trust a checksum
# from whatever wrote it.
disk_verify_stream() { # <device> <bytes> <sha256>
    local dev="$1" bytes="$2" want="$3" got
    info "verifying $dev against the image that was streamed to it"
    got=$(card_priv verify "$dev" "$bytes" | tr -d '\r' | tail -1)
    [ -n "$got" ] || die "could not read $dev back on $MACH_NAME"
    [ "$want" = "$got" ] \
        || die "$dev does not match the image that was streamed to it
    image: $want
    disk:  $got"
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

    # Read back rather than trusted: a stamp that silently failed would leave
    # two disks sharing an identity, which is exactly the bug this exists to
    # prevent.
    m_ssh "lsblk -no PARTUUID $(sh_quote "$(disk_part "$dev" 2)")" 2>/dev/null | tr -d '\r ' \
        | grep -qx "$new-02" \
        || die "$dev did not take the new identity; refusing to leave it ambiguous"
}

# What makes a written disk part of the fleet: the identity marker every
# wk-written system carries (/etc/wk-image, read by `b_probe`) and the
# driving machine's ssh key in root's authorized_keys -- a Yocto image ships
# `PermitRootLogin yes` with an empty root password, usable by a person but
# not by `ssh -o BatchMode=yes`. Both values travel as base64 on the command
# line, so the helper can check them against a character set before decoding.
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

# The tailnet identity, onto the card just written, not baked into the
# image: an image is stored, copied and kept after it's superseded, so a
# baked-in key would be in every copy, revocable only for the whole fleet.
# On a card it exists for one boot -- wk-tailnet-join deletes it once spent.
# Seeded only for an image that asked for it (the card is asked, not
# guessed): one built with --no-tailnet has none, and writing the key there
# would put a credential on a disk with nothing to spend it.
disk_seed_tailnet() { # <device> <tailnet hostname>
    local dev="$1" name="$2" keyfile joins tag="${WK_TAILNET_TAG:-tag:wk}"

    # Three outcomes: yes, no, and "the question was not answered" -- a grep
    # finding nothing looks like a no, and a no here means the board boots
    # with no tailnet identity, the state this rule exists to end.
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

    # The name and the key are both guaranteed present here -- the write
    # preflight (_tailnet_key_preflight, cmd/sysimage) refuses the write
    # outright, with no --force, before anything is erased.
    keyfile=$(wk_tailscale_authkey) || die "the tailnet auth key present moments ago at the write preflight is
    gone now, and $dev is already erased. Set one and retry:  wk key tailnet"

    info "seeding the tailnet identity onto $dev -- it joins as '$name' ($tag) on first boot"
    # Goes in over the same ssh and never onto a command line; the helper
    # reads it off stdin.
    card_priv tailnet "$dev" "$name" "$tag" < "$keyfile" >/dev/null \
        || die "could not seed the tailnet identity onto $dev.
    The image is written; it would boot with no tailnet identity and be
    reachable only over whatever LAN it lands on."
}

# Does this board's rescue/bench system bring up WiFi? A hardware fact, so
# a declared field (MACH_NET=wifi|ethernet, boot/machines.sh), not computed.
# Loaded in a subshell: machine_load sets MACH_* directly, and every caller
# here already has its own machine loaded (DISK_MACHINE) -- loading a second
# one in place would clobber it.
_image_wants_wifi() { # <IMG_MACHINE, which is boot/machines/<name>.conf's name>
    [ -n "${1:-}" ] || return 1
    ( machine_load "$1" >/dev/null 2>&1 && [ "${MACH_NET:-}" = wifi ] )
}

# One file for the fleet's WiFi identity, same shape as
# wk_tailscale_authkey_path (lib/common.sh). Not $WK_STORE/secrets/wifi: on
# macOS $WK_STORE resolves inside the podman VM, unwritable from this host
# command.
disk_wifi_creds_path() { printf '%s' "${WK_WIFI_CREDS:-$HOME/.config/wk/wifi}"; }

# No prompting, no side effects: a read-only report must never be the thing
# that asks for a credential.
disk_wifi_creds_present() {
    local p; p=$(disk_wifi_creds_path)
    [ -s "$p" ] || return 1
    grep -q '^ssid=' "$p" && grep -q '^psk=' "$p"
}

# Unlike disk_seed_tailnet this is a barrier, not a warning: rpi3/rpi4/rpi5
# have no cable at the bench, so a card with no WiFi credential seeded has
# no LAN at all -- no --force past that.
disk_seed_wifi() { # <device> <IMG_MACHINE>
    local dev="$1" mach="$2" creds joins

    _image_wants_wifi "$mach" || { debug "$mach has a cable, or is not one of this fleet's boards; nothing to seed"; return 0; }

    joins=$(card_priv wifi-joins "$dev" 2>&1) \
        || die "could not tell whether $dev brings up WiFi on first boot:
$(printf '%s\n' "$joins" | sed 's/^/    /')
    The image is written. Refusing to guess: $mach has no cable at the bench, so
    a card that brings up WiFi and was not seeded is a card with no network at
    all."
    case "$joins" in
        *'wifi-join: yes'*) ;;
        *'wifi-join: no'*)
            debug "$dev carries no wk-wifi-join, so there is nothing to seed"
            return 0 ;;
        *) die "$MACH_NAME's card helper did not say whether $dev brings up WiFi
    (it said: ${joins:-nothing}). Refusing to guess, for the same reason." ;;
    esac

    creds=$(disk_wifi_creds_path)
    disk_wifi_creds_present || die "$mach has no cable at the bench, and its rescue/bench images bring up
    WiFi from a credential seeded onto the card -- there is none at $creds.
    A board with no uplink is unreachable, which is worse than refusing, so this
    does not take --force.
    Set it:
        printf 'ssid=<network>\npsk=<passphrase>\n' > $creds"

    info "seeding the fleet's WiFi identity onto $dev"
    card_priv wifi "$dev" < "$creds" >/dev/null \
        || die "could not seed WiFi credentials onto $dev.
    The image is written; $mach has no cable at the bench, so it would boot with
    no way to reach a network at all."
}

# Which role the system on this card plays, stamped onto the card itself --
# the *only* difference between a rescue and a bench system, since one
# image artifact serves both and the board asks this on a boot when nothing
# else is reachable. Every image's units check
# `ConditionPathExists=!/etc/wk/rescue`, so a bench card is one with the
# marker absent, not a second flag. Called on every write, for both roles,
# so the final state is declared, not diffed.
disk_seed_role() { # <device> <bench|rescue>
    local dev="$1" role="$2"

    case "$role" in
        bench|rescue) ;;
        *) die "internal: '$role' is not a role" ;;
    esac

    if [ "$role" = rescue ]; then
        info "marking $dev a rescue system -- no self-return watchdog, no self-disarm"
        log  "  it is what a board falls back to, so there is nothing to hand it back to"
    else
        debug "marking $dev a bench system (the rescue marker is removed if it was there)"
    fi

    local out rc=0
    out=$(card_priv role "$dev" "$role" 2>&1) || rc=$?
    [ "$rc" -eq 0 ] && return 0

    # An older helper has no `role` verb at all, and its dispatch answers an
    # unknown one with its usage line. That is a provisioning fact about the
    # machine holding the reader, not a fault of this disk, and it has a
    # one-line remedy -- so it is worth telling apart from a refusal.
    case "$out" in
        *'usage: wk-card-priv'*)
            die "$MACH_NAME's card helper is older than this checkout: it has no 'role'
    verb, so the rescue marker cannot be written and this card would boot
    carrying a live self-return watchdog.
    The image is written; the role is not set.
    Remedy, from a terminal on $MACH_NAME (its sudo asks for a password, which
    is why this end cannot do it):
        wk sync --target $MACH_NAME
        ./setup --stage quiesce" ;;
    esac

    die "could not set the role on $dev:
$(printf '%s\n' "$out" | sed 's/^/    /')
    The image is written, and the role decides whether this system reboots
    itself every few minutes. Refusing to leave that unknown: a rescue that
    carries a live self-return watchdog reboots the helper in the middle of
    whatever card it is writing."
}

# Grow the last partition to fill the disk: an image is sized to its
# contents, so a 4 GB image on a 64 GB card leaves most of it unreachable --
# and what then fails is a build or benchmark run out of disk, hours later.
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
