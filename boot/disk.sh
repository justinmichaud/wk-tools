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
# The model of a Pi's boot tree the helper runs as root (boot/check-boot-files.py,
# installed beside it under the name the helper knows). Named here because the
# two are installed as a pair and `wk pi helper` puts that pair on a board.
CARD_CHECKER=/usr/local/libexec/wk-check-boot-files.py

card_priv() { # <verb> [args...]
    m_ssh "sudo -n $CARD_PRIV $(sh_quote "$@")"
}

# `wk sysimage write --dry-run` runs the same sequence of steps as the write
# it is reporting; every step that would change the card asks this first, so
# what a dry run says is what the write does rather than a second description
# of it that can drift. Set by the caller for the length of one command.
DISK_DRY=""
disk_would() { # <what this step would do>
    [ -n "$DISK_DRY" ] || return 1
    log "  would $1"
    return 0
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
# The inverse: /dev/sda2 -> /dev/sda, /dev/mmcblk0p2 -> /dev/mmcblk0, /dev/nvme0n1p2 -> /dev/nvme0n1.
disk_of_part() {
    case "$1" in
        *[0-9]p[0-9]*) echo "${1%p[0-9]*}" ;;
        *)             echo "${1%[0-9]*}" ;;
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
# Sets DISK_WHOSE_MACHINE (the machine name, or empty) and DISK_WHOSE_BOOTED
# (set when the disk is the one this machine is currently running from)
# rather than printing a result: a caller reading it back via $(...) would
# run this in a subshell, and a global set there is gone once the subshell
# exits -- disk_list needs both answers together. Called as a statement,
# never as $(...), the way admin/wk-card-priv's `gate` sets GATED_DEV for
# the same reason.
#
# The booted case: admin/wk-card-priv's `gate` refuses to mount the disk
# this machine is running from at all, correctly, and its refusal already
# names the reason (it names booted_disks' finding) -- so this reads that
# refusal instead of asking booted_disks again over a second ssh round trip.
_WK_WHOSE=""
DISK_WHOSE_MACHINE=""
DISK_WHOSE_BOOTED=""
disk_image_machine() { # <device>
    local dev="$1" hit out
    DISK_WHOSE_MACHINE=""
    DISK_WHOSE_BOOTED=""
    disk_can_identify || return 1

    hit=$(printf '%s\n' "$_WK_WHOSE" | awk -v d="$dev" '$1 == d { print $2; exit }')
    if [ -n "$hit" ]; then
        case "$hit" in
            -)      ;;
            booted) DISK_WHOSE_BOOTED=1 ;;
            *)      DISK_WHOSE_MACHINE="$hit" ;;
        esac
        return 0
    fi

    # `|| true`, not `|| out=""`: on a refusal the message that matters is
    # in $out (stderr, merged in above) and a failed command substitution
    # still assigns it -- only a trailing `|| out=""` would have thrown it
    # away.
    out=$(card_priv whose "$dev" </dev/null 2>&1) || true
    case "$out" in
        *'is a disk this machine is running from'*) DISK_WHOSE_BOOTED=1 ;;
    esac
    hit=$(kv_get machine <<<"$out")
    if [ -n "$hit" ]; then
        _WK_WHOSE="$_WK_WHOSE
$dev $hit"
        DISK_WHOSE_MACHINE="$hit"
    elif [ -n "$DISK_WHOSE_BOOTED" ]; then
        _WK_WHOSE="$_WK_WHOSE
$dev booted"
    else
        _WK_WHOSE="$_WK_WHOSE
$dev -"
    fi
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
        disk_image_machine "$dev" || true
        owner="$DISK_WHOSE_MACHINE"
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
        disk_image_machine "$dev" || true
        [ "$DISK_WHOSE_MACHINE" = "$want" ] && { printf '%s' "$dev"; return 0; }
    done
    return 1
}

disk_contents() {
    m_ssh "lsblk -rno NAME,TYPE,LABEL,PKNAME 2>/dev/null" 2>/dev/null | awk '
        $2 == "part" && $4 != "" {
            lab = ($3 == "" ? "-" : $3)
            labels["/dev/" $4] = labels["/dev/" $4] (labels["/dev/" $4] ? "," : "") lab
        }
        END {
            for (d in labels)
                print d "\t" "labels: " labels[d]
        }'
}

disk_list() {
    local line name n=0 contents desc owner tran booted
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
        disk_image_machine "$name" || true
        owner="$DISK_WHOSE_MACHINE"
        booted="$DISK_WHOSE_BOOTED"

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
        elif [ -n "$booted" ]; then
            # The helper's `whose` refuses to mount this one -- it is the
            # disk the machine is running from -- and its refusal is the
            # strongest evidence there is that this is the machine's own
            # system, not an empty answer.
            printf "        %s  --  this machine's own system (booted)\n" "$desc"
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
# refuses. Whether the image *fits* is not asked here: the image is a stream
# read once and written as it arrives, so its size is not known until it has
# gone past; a card too small for it runs out of space part-written, which is
# what disk_write_source reports.
disk_refuse_unless_safe() {
    local dev="$1" out

    card_priv_require
    # A helper from before @second answers `check <disk>@second` for the
    # whole disk, and its `write` would take the whole disk too -- over the
    # rescue it may be running from. Asked before anything else.
    if disk_is_second "$dev"; then
        card_priv status 2>/dev/null | grep -q 'second=yes' \
            || die "$MACH_NAME's card helper predates second systems (@second), so it
    would write the whole disk. Update it first: on a workstation,
    ./setup --stage quiesce from a terminal there; on a rescue, rebuild the
    rescue image and write it again."
        case "$dev" in (*@third)
            card_priv status 2>/dev/null | grep -q 'third=yes' \
                || die "$MACH_NAME's card helper predates third systems (@third).
    Update it first: on a workstation, ./setup --stage quiesce from a terminal
    there; on a rescue, rebuild the rescue image and write it again." ;;
        esac
    fi
    out=$(card_priv check "$dev" 2>&1) || die "$MACH_NAME will not write $dev:
$(printf '%s\n' "$out" | sed 's/^/    /')
    Disks there:
$(disk_list)"
    debug "$out"
}

# How big the disk is, in bytes and as the machine words it, for a message
# about a write that did not fit.
disk_size() { # <device>
    m_ssh "lsblk -dno SIZE $(sh_quote "$1")" 2>/dev/null | tr -d ' \r'
}

# Unmount, after the confirmation and never before: the caller has agreed to
# erase the disk, and a desktop automounter having grabbed the card is not a
# reason to send someone back to a hand-typed umount.
disk_unmount() {
    local dev="$1"
    disk_would "unmount whatever is mounted from $dev on $MACH_NAME" && return 0
    # Through the helper, like every other privileged step: it still refuses
    # a disk this machine is running from.
    card_priv unmount "$dev" >/dev/null \
        || die "could not unmount what is on $dev on $MACH_NAME.
    Something is using it:
$(m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$dev")" 2>/dev/null | awk 'NF > 1 { print "    /dev/" $1 " at " $2 }')"
}

# A system beside the first (`<device>@second`, `<device>@third`,
# admin/wk-card-priv): the image goes into that system's own partition pair
# and the rest of the card stays as it is -- which pair is the helper's to
# resolve from the card's shape. Every step here passes the spec through;
# the steps that differ ask this.
disk_is_second() { case "$1" in *@second|*@third) return 0 ;; *) return 1 ;; esac; }

# The card machine decompresses, meters and writes in one pipeline: the image
# is never decompressed here, so a source that is compressed crosses the
# network compressed and this machine needs none of the tools its format
# wants. `exec 3>&1` hands the meter a fd onto the ssh session's stdout,
# where the helper's own report goes: its two lines are read out of the
# report by disk_write_source, and the decompressor's stdout stays the
# writer's stdin.
disk_write_stream() { # <device> <decompressor>  -- source bytes on stdin; the far side's report on stdout
    local dev="$1" filter="$2"
    info "writing to $dev on $MACH_NAME (streamed; decompressed there with $filter)"
    m_ssh "exec 3>&1; $filter | python3 -c $(sh_quote "$(disk_stream_meter_py)") | sudo -n $CARD_PRIV write $(sh_quote "$dev")"
}

# Asked here rather than beside the pipeline below, which runs in a command
# substitution: a `die` in there would exit that subshell and leave the write
# to fail a second time, blaming the disk. `< /dev/null`, like every other
# probe here: ssh forwards its own stdin to the far side, and once the write
# starts that stdin is the image -- bytes read to ask a yes/no question are
# bytes the card never sees.
disk_filter_require() { # <decompressor>
    local tool="${1%% *}"
    [ "$1" = cat ] && return 0
    m_ssh "command -v $(sh_quote "$tool") >/dev/null" < /dev/null && return 0
    die "$MACH_NAME has no $tool, and the image being sent to it is compressed
    with it -- the card machine is what decompresses the stream, so this end
    never has to have the tool for a format it is only passing through.
    Remedy: install $tool on $MACH_NAME (apt spells xz 'xz-utils')."
}

# The meter, as it runs on the card machine: stdin to stdout unchanged, with
# the byte count and sha256 of everything that went past written to fd 3 at
# EOF. A filter in the pipeline rather than `tee` into a process
# substitution, whose completion no shell here can wait for: the numbers have
# to be final by the time the write returns. One write call, so the two lines
# cannot interleave with the helper's on the fd they share.
disk_stream_meter_py() {
    cat <<'PY'
import hashlib, os, sys
out, digest, n = sys.stdout.buffer, hashlib.sha256(), 0
while True:
    chunk = sys.stdin.buffer.read(1 << 20)
    if not chunk:
        break
    n += len(chunk)
    digest.update(chunk)
    out.write(chunk)
out.flush()
os.write(3, ("stream_bytes=%d\nstream_sha=%s\n" % (n, digest.hexdigest())).encode())
PY
}

# The image's own bytes, from wherever the source is onto the card, in one
# pass: nothing is materialized on this machine, which is what lets a driving
# machine with no Linux tooling write a card -- every edit the image needs is
# made afterwards, on the card (admin/wk-card-priv). The stream is metered on
# the card machine as it goes past, since the read-back below has to compare
# the disk against *something* and there is no local copy to re-read. The
# meta file's first line is "<bytes> <sha>" of the stream; under @second a
# second line carries what the helper reports it split the stream into -- the
# two partitions' own sizes and hashes, which is what the read-back compares
# there.
#   disk_write_source <device> <reader command> <decompressor> <meta file>
disk_write_source() {
    local dev="$1" reader="$2" filter="$3" meta="$4" report
    disk_would "stream the image onto $dev on $MACH_NAME, and read it back to verify" && return 0
    disk_filter_require "$filter"
    report=$(eval "$reader" | disk_write_stream "$dev" "$filter" | tr -d '\r') \
        || die "could not write the image onto $dev on $MACH_NAME.
    It was read through:  $reader
    $dev is $(disk_size "${dev%@*}"); an image larger than that runs out of space
    part-written, and the read that fed it can fail on its own account."
    printf '%s\n' "$report" | sed 's/^/    /' >&2
    printf '%s %s\n' \
        "$(kv_get stream_bytes <<<"$report")" "$(kv_get stream_sha <<<"$report")" > "$meta"
    disk_is_second "$dev" || return 0
    printf '%s %s %s %s\n' \
        "$(kv_get boot_bytes <<<"$report")" "$(kv_get boot_sha <<<"$report")" \
        "$(kv_get root_bytes <<<"$report")" "$(kv_get root_sha <<<"$report")" >> "$meta"
}

# Read the card back and compare with what was sent -- privileged, hence a
# verb rather than a second way in, and unconditional over the whole span: a
# checksum from whatever did the writing only claims the bytes it chose to
# write arrived, and says nothing about the ones it skipped. The size and hash
# are the stream's own, metered on the card machine as it went past
# (disk_write_source), there being no local copy; under @second they are the
# two partitions' (disk_write_source's second line), read back from
# partitions 3 and 4.
disk_verify_stream() { # <device> <meta file>
    local dev="$1" meta="$2" bytes want got b_bytes b_sha r_bytes r_sha
    disk_would "read $dev back and compare it with the image streamed to it" && return 0
    info "verifying $dev against the image that was streamed to it"
    if disk_is_second "$dev"; then
        read -r b_bytes b_sha r_bytes r_sha <<<"$(sed -n 2p "$meta")"
        [ -n "$r_sha" ] || die "the write onto $dev did not report what it split the image into,
    so there is nothing to read the card back against."
        card_priv verify "$dev" "$b_bytes" "$b_sha" "$r_bytes" "$r_sha" >/dev/null \
            || die "$dev does not read back as the image's boot and root."
        debug "verified $b_bytes + $r_bytes bytes"
        return 0
    fi
    read -r bytes want < "$meta"
    got=$(card_priv verify "$dev" "$bytes" | tr -d '\r' | tail -1)
    [ -n "$got" ] || die "could not read $dev back on $MACH_NAME"
    [ "$want" = "$got" ] \
        || die "$dev does not match the image that was streamed to it
    image: $want
    disk:  $got"
    debug "verified $bytes bytes"
}

# --- the edits an image needs, made on the card -------------------------------
# Each is a verb of the card helper, applied to the card after the image's own
# bytes are on it: the driving machine opens no filesystem inside an image and
# needs no tooling for one.

# A card that took an image has a partition table. One that took a stream
# with a shell banner ahead of it -- a container exec, an ssh session with
# something to say -- hashes perfectly against what was sent and has none.
disk_parts_present() { # <device>
    local dev="$1" out
    disk_would "check that $dev came out of this with a partition table" && return 0
    out=$(card_priv parts "$dev" 2>&1) \
        || die "$dev has no readable partition table after the write:
$(printf '%s\n' "$out" | sed 's/^/    /')
    Something was written ahead of the image bytes on the way out, or the
    source is not a disk image at all."
    debug "$out"
}

# The `root=` on the card's own kernel command line, read off the card: what
# the board will look for when it boots this disk, rather than what the image
# it came from said before anything edited it.
disk_root_spec() { # <device>
    local dev="$1"
    disk_would "read the root= on $dev's kernel command line" && return 0
    card_priv root-spec "$dev" 2>/dev/null | tr -d '\r' | kv_get root
}

# Give the card a root reference that survives the kind of device it was
# written to (`retarget`, admin/wk-card-priv). Which cards need it is decided
# here, by the one classifier there is (image_root_class, lib/image.sh); a
# card whose root is already portable is left alone.
disk_retarget_root() { # <device>
    local dev="$1" spec class
    disk_would "retarget $dev's root= to a PARTUUID of $dev, so it boots from any device" && return 0
    spec=$(disk_root_spec "$dev")
    [ -n "$spec" ] || die "$dev has no cmdline.txt to read a root from, and this image was
    written as one that boots by firmware and cmdline.txt."
    class=$(image_root_class "$spec")
    case "$class" in
        portable) debug "$dev's root is already portable ($spec); nothing to retarget"; return 0 ;;
        network)  die "$dev names a network root ($spec). Nothing here boots that way." ;;
    esac
    info "retargeting $dev's root: $spec -> a PARTUUID of this disk (boots from any device)"
    card_priv retarget "$dev" \
        || die "could not retarget $dev's root.
    The image is written; its kernel command line still says root=$spec, which
    is a promise about a device rather than about a filesystem -- in another
    reader it names somebody else's disk."
}

# Kernel command-line additions a profile declares. base64 on the way, like
# the identity marker: the helper checks the value against a character set
# before anything decodes it.
disk_cmdline_append() { # <device> <text>
    local dev="$1" add="$2" b64
    [ -n "$add" ] || return 0
    disk_would "append to $dev's kernel command line: $add" && return 0
    b64=$(printf '%s' "$add" | base64 | tr -d '\n\r ')
    card_priv cmdline-append "$dev" "$b64" \
        || die "could not append to $dev's kernel command line ($add).
    The image is written; the board would boot without what this profile asks
    its kernel for."
}

# Firmware settings a profile declares, appended to the config.txt a Pi's
# firmware reads whatever the OS is. A setting that fails to land fails
# nothing and makes every number worse, so this is a refusal, not a warning.
disk_config_append() { # <device> <block>
    local dev="$1" block="$2" b64
    [ -n "$block" ] || return 0
    disk_would "append this profile's firmware block to $dev's config.txt" && return 0
    b64=$(printf '%s' "$block" | base64 | tr -d '\n\r ')
    card_priv config-append "$dev" "$b64" \
        || die "could not append the firmware block to $dev's config.txt.
    The image is written; the board would come up at whatever clock it felt
    like, and nothing later would say so."
}

# The card helper, onto a rescue: the board that boots it writes its own bench
# media, and the code it does that with should be this checkout's, not whatever
# the image baked in months ago. Copied by the helper from its own installed
# file, so the bytes are the ones the machine holding the reader just ran.
#
# A refusal is fatal: a rescue that cannot write a card cannot do the one job
# that makes it a rescue, and finding that out later costs a trip to the board.
disk_install_rescue_helper() { # <device>
    local dev="$1" out rc=0
    disk_would "put this machine's card helper on $dev's rescue" && return 0
    out=$(card_priv rescue-helper "$dev" 2>&1) || rc=$?
    [ "$rc" -eq 0 ] && { log "  $(printf '%s' "$out" | sed -n 's/^wk-card-priv: helper: //p')"; return 0; }
    case "$out" in
        *'usage: wk-card-priv'*)
            die "$MACH_NAME's card helper is older than this checkout: it has no
    'rescue-helper' verb, so the rescue being written would carry the helper its
    image was built with, and a fix made here would never reach the board.
    The image is written; the helper is not.
    Remedy, from a terminal on $MACH_NAME (its sudo asks for a password, which
    is why this end cannot do it): update its wk-tools checkout, then
        ./setup --stage quiesce" ;;
    esac
    die "could not put the card helper on $dev's rescue:
$out"
}

# Which system this card holds, on the boot partition, where `wk boot` reads
# it off a disk that is not running.
disk_boot_id() { # <device> <id>
    local dev="$1" id="$2"
    disk_would "name the system on $dev's boot partition '$id'" && return 0
    card_priv boot-id "$dev" "$id" >/dev/null \
        || die "could not write the identity onto $dev's boot partition.
    The image is written, but 'wk boot' refuses a disk it cannot name."
}

# The units that make a booted image a fleet system, composed by the caller
# (their bodies come from the profile) and installed here as a tar the helper
# unpacks into two fixed directories under /etc.
disk_install_units() { # <device> <staging directory>
    local dev="$1" dir="$2" out rc=0
    disk_would "install the fleet units and the profiling knobs into $dev's rootfs" && return 0
    out=$(tar -cf - -C "$dir" systemd sysctl.d init.d | card_priv units "$dev" 2>&1) || rc=$?
    [ "$rc" -eq 0 ] || die "could not install the fleet units on $dev:
$(printf '%s\n' "$out" | sed 's/^/    /')
    The image is written; a run that wedges the board would not hand it back."
    case "$out" in
        *'neither systemd nor /etc/init.d'*)
            warn "this image has neither systemd nor a BusyBox init, so the self-return
  watchdog and the self-disarm were NOT installed. The card carries its identity
  marker and the driving key and nothing else: a run that wedges the board will
  not hand it back, and on a medium-armed machine the medium stays armed until
  something disarms it." ;;
        *'no systemd on this disk; nothing installed'*)
            die "$MACH_NAME's card helper predates BusyBox init scripts, so this image got
    neither its self-disarm nor its self-return: a board booted into it would not
    hand itself back. The image is written. Update the helper (on a workstation,
    ./setup --stage quiesce from a terminal there; on a rescue, rebuild the
    rescue image) and write the card again." ;;
        *) debug "$out" ;;
    esac
}

# Will the firmware find everything it needs to reach a kernel? Asked of the
# card, after every edit that changes its boot partition, because what the
# board will boot is what is on the disk. A kernel that cannot find its root
# panics and reboots (`panic=10`); firmware that cannot find a kernel
# **halts** -- no retry, no fall-through, no way back over the wire.
# Does the system on this card name a root it can find on the device it is
# on? The spec is read off the card; the comparison is image_check_root
# (lib/image.sh), the one classifier there is.
disk_check_root() { # <device> <what-it-is>
    local dev="$1" what="$2"
    disk_would "check that the system on $dev names a root it can find on $dev" && return 0
    image_check_root "$(disk_root_spec "$dev")" "${dev%@second}" "$what"
}

disk_check_boot_files() { # <device> <machine> <dtb>
    local dev="$1" machine="$2" dtb="$3" out rc=0
    disk_would "check that every file a $machine's firmware asks for resolves on $dev" && return 0
    out=$(card_priv boot-check "$dev" "$dtb" 2>&1) || rc=$?
    [ "$rc" -eq 0 ] && { debug "$out"; return 0; }
    # The question could not be asked, which is not an answer about the card:
    # the same state as a card written for another machine, and reported the
    # same way. A rescue's helper is its image's, so the remedy is a rebuild.
    case "$out" in
        *'no boot-file checker'*)
            warn "$dev's boot files were NOT checked: $MACH_NAME's card helper has no boot-file
  checker beside it. If the firmware cannot find a kernel it halts, and that costs
  a trip to the board. The checker is installed with the helper (./setup --stage
  quiesce on a workstation; a rebuilt rescue image carries it)."
            return 0 ;;
    esac
    die "$dev is missing files a $machine needs to reach its kernel:

$(printf '%s\n' "$out" | sed 's/^/      /')

    Firmware that cannot find a kernel halts. It does not move on to the next
    BOOT_ORDER entry and it does not come back, so booting this card would cost
    a trip to the board rather than a reboot.

    The image is the problem, not the disk: rebuild it, or check what its
    config.txt names against what its boot partition holds, and write again."
}

# The old identity is read off the card, now, rather than taken from what
# anything computed before the write: every reference to it on the card
# (cmdline.txt, /etc/fstab) is rewritten from it, and a stale one would
# rewrite nothing while reporting success.
disk_unique_identity() { # <device>
    local dev="$1" spec old new
    if disk_is_second "$dev"; then
        # The identity is the disk's, and the disk is the rescue's: the
        # second system took its PARTUUIDs from it (disk_retarget_root).
        log "  $dev keeps the rescue disk's identity; the second system names its partitions by it"
        return 0
    fi
    disk_would "stamp a unique disk identity on $dev, so two cards written from one image cannot be confused" && return 0
    spec=$(disk_root_spec "$dev")
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
    disk_would "install the identity marker and the driving ssh key on $dev" && return 0
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
    disk_would "seed the tailnet identity on $dev (it would join as '$name', $tag)" && return 0

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

# Unlike disk_seed_tailnet this is a barrier, not a warning: rpi3/rpi4/rpi5
# have no cable at the bench, so a card with no WiFi credential seeded has no
# LAN at all -- no --force past that. There is no hand-made credential file:
# the card takes its credential from $DISK_MACHINE's own WiFi connection,
# read by the card helper as root (wifi-host/wifi-from-host,
# admin/wk-card-priv). _wifi_creds_preflight (cmd/sysimage) asks wifi-host
# before anything is erased; this assumes that already passed.
disk_seed_wifi() { # <device> <IMG_MACHINE>
    local dev="$1" mach="$2" joins
    disk_would "seed $MACH_NAME's own WiFi credential on $dev, for a board with no cable" && return 0

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

    info "seeding $MACH_NAME's own WiFi credential onto $dev"
    card_priv wifi-from-host "$dev" >/dev/null \
        || die "could not seed WiFi credentials onto $dev.
    The image is written; $mach has no cable at the bench, so it would boot with
    no way to reach a network at all."
}

# A second system's tailnet node survives its rewrite: tailscaled's state on
# partition 4 is kept aside by the helper before the split and put back after
# it, so the new system comes up as the node the old one was -- same name,
# same address, no join, and no collision with its own name. Prints yes or
# no; read-only on the card, so it runs in a dry run too.
# A helper from before this verb -- a rescue's is the one its image was built
# with, and a rescue cannot rewrite its own partitions -- keeps nothing, and
# says so: the rewrite then joins fresh under the name preflight, the path a
# first write takes.
disk_tailnet_save() { # <device>@second
    local dev="$1" out
    if ! card_priv status 2>/dev/null | grep -q 'tailnet-keep=yes'; then
        warn "$MACH_NAME's card helper cannot keep a node's tailnet identity across a rewrite,
  so the new system joins fresh; a stale node of the same name on the tailnet
  refuses the write. The helper is the rescue image's: a rebuilt rescue, written
  from a reader, has the current one."
        printf no
        return 0
    fi
    out=$(card_priv tailnet-save "$dev" 2>&1) || die "could not look for a tailnet identity on $dev's partition 4:
$(printf '%s\n' "$out" | sed 's/^/    /')"
    case "$out" in
        *kept=yes*)
            case "$out" in
                *adopted=remembered*)
                    info "this board remembers its bench tailnet node, so the card written now
  rejoins as that node rather than colliding with it -- nothing has to retire
  a leftover, and no credential that can administer the tailnet is needed" ;;
                *adopted=*)
                    info "taking the board's bench tailnet identity from the system beside this one:
  the two bench systems take turns being one node, so the new one is reachable
  under the name the board's bench role already holds" ;;
                *) info "keeping the node's tailnet identity aside: the rewritten system comes back as the same node" ;;
            esac
            printf yes ;;
        *kept=no*)
            debug "$dev holds no bench tailnet identity yet; the new system joins fresh"
            printf no ;;
        *) die "$MACH_NAME's card helper did not say whether $dev's partition 4 holds a
    tailnet identity (it said: ${out:-nothing}). Refusing to guess: a system that
    joins under a name it already holds comes up renamed and unreachable." ;;
    esac
}

disk_tailnet_restore() { # <device>@second
    local dev="$1"
    disk_would "put the kept tailnet identity back on $dev's partition 4" && return 0
    card_priv tailnet-restore "$dev" >/dev/null \
        || die "could not put the kept tailnet identity back on $dev.
    The image is written; booted, it would join as a new node under a name the
    old one still holds, and come up renamed."
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
    disk_would "mark $dev a $role system" && return 0

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
    is why this end cannot do it): update its wk-tools checkout, then
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
    disk_would "grow the last partition to fill $dev" && return 0
    info "growing the last partition to fill $dev"
    card_priv grow "$dev" >/dev/null \
        || die "could not grow the root partition on $dev"
}

disk_eject() {
    local dev="$1"
    disk_would "flush and power off $dev" && return 0
    m_ssh "command -v udisksctl >/dev/null" || {
        warn "$MACH_NAME has no udisksctl, so $dev is left powered on. The write is
  complete and the card is synced -- it is safe to pull. To have the card
  powered off instead, install udisks2 on $MACH_NAME ('./setup' does, on a wk host)."
        return 0
    }
    m_ssh "udisksctl power-off -b $(sh_quote "$dev")" >/dev/null 2>&1 \
        && info "powered off $dev -- safe to remove" \
        || log "  (could not power off $dev; it is synced, so it is safe to pull anyway)"
    return 0
}
