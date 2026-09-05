# Writes an image onto a disk attached to a machine: `wk sysimage write <image> --disk <machine>:<device>`. The work happens on
# that machine, over ssh, through one privileged helper invoked as `sudo -n`, a BatchMode ssh having no terminal for sudo to prompt
# on. Requires machine_load() to have run.

# stdin passes straight through, which is how the image reaches `write` and the tailscale auth key reaches `tailnet` without touching a command line.
CARD_PRIV=/usr/local/libexec/wk-card-priv
# boot/check-boot-files.py, run as root beside the helper; `wk pi helper` puts the pair on a board.
CARD_CHECKER=/usr/local/libexec/wk-check-boot-files.py

card_priv() { # <verb> [args...]
    r_sudo "$CARD_PRIV $(sh_quote "$@")"
}

# `--dry-run` runs the same steps as the write it reports, and every step that would change the card asks this first.
DISK_DRY=""
disk_would() { # <what this step would do>
    [ -n "$DISK_DRY" ] || return 1
    log "  would $1"
    return 0
}

card_priv_require() {
    card_priv status >/dev/null 2>&1 && return 0
    die "$NODE_NAME cannot write a disk: its card helper is missing, or its
    sudoers rule is not in force. Everything privileged here goes through it and
    there is deliberately no second way in.
    What fails:  sudo -n $CARD_PRIV status
    The remedy, from a terminal on $NODE_NAME:  ./setup --stage quiesce"
}

# /dev/sda -> /dev/sda2, but /dev/mmcblk0 -> /dev/mmcblk0p2, and likewise for nvme and loop.
disk_part() {
    case "$1" in
        *[0-9]) echo "$1p$2" ;;
        *)      echo "$1$2" ;;
    esac
}
disk_of_part() {
    case "$1" in
        *[0-9]p[0-9]*) echo "${1%p[0-9]*}" ;;
        *)             echo "${1%[0-9]*}" ;;
    esac
}
disk_partno() {
    case "$1" in
        *[0-9]p[0-9]*) echo "${1##*p}" ;;
        *)             echo "${1##*[!0-9]}" ;;
    esac
}

disk_parse() {
    local spec="$1"
    case "$spec" in
        *:*) DISK_MACHINE="${spec%%:*}"; DISK_DEV="${spec#*:}" ;;
        *)   DISK_MACHINE="$spec"; DISK_DEV="" ;;
    esac
    [ -n "$DISK_MACHINE" ] || die "--disk needs a machine: --disk <machine>:<device>"
}

# RM=1 catches card readers and most sticks; TRAN catches the non-removable usb/mmc normal for USB SSDs and built-in SD slots. Whole disks only, an image carrying its own partition table.
disk_candidates() {
    m_ssh "lsblk -dpno NAME,SIZE,TRAN,RM,TYPE,MODEL" 2>/dev/null | awk '
        $5 == "disk" && ($4 == 1 || $3 == "usb" || $3 == "mmc") { print }'
}

# Kernel names like sda/sdb are enumeration order and swap across a reboot, so which disk holds a wk system is read from its identity marker (/etc/wk-image, `machine=`) rather than a filesystem label, which a yocto image sets like any other.
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

# Sets DISK_WHOSE_MACHINE and DISK_WHOSE_BOOTED rather than printing, so it must be called as a statement and never as $(...), where a subshell would lose both.
# The helper's `gate` refuses to mount the disk this machine is running from and names that finding in its refusal, so the booted case is read out of the refusal rather than asked again.
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

    # `|| true`, not `|| out=""`: on a refusal the message that matters is the stderr a failed command substitution still assigns.
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

disk_tran_of_name() {
    case "$1" in
        /dev/sd*)     printf usb ;;
        /dev/mmcblk*) printf mmc ;;
        /dev/nvme*)   printf nvme ;;
    esac
}

# By exclusion -- disks of the expected transport, less the ones marked for another machine -- so a blank medium needs no marker of its own. Prints nothing when more than one survives, leaving the caller to refuse rather than pick.
disk_resolve_own() {
    local want same n dev owner left=""
    want=$(disk_tran_of_name "${NODE_DEVICE:-}")
    [ -n "$want" ] || return 1

    same=$(disk_candidates | awk -v t="$want" '$3 == t { print $1 }')
    n=$(printf '%s\n' "$same" | grep -c . || true)
    [ "$n" = 0 ] && return 1
    if [ "$n" = 1 ]; then printf '%s' "$same"; return 0; fi

    for dev in $same; do
        disk_image_machine "$dev" || true
        owner="$DISK_WHOSE_MACHINE"
        [ -n "$owner" ] && [ "$owner" != "$NODE_NAME" ] && continue
        [ "$owner" = "$NODE_NAME" ] && { printf '%s' "$dev"; return 0; }
        left="$left $dev"
    done

    set -- $left
    [ "$#" = 1 ] || return 1
    printf '%s' "$1"
}

disk_own_or_declared() {
    local got
    got=$(disk_resolve_own 2>/dev/null) || got=""
    if [ -z "$got" ]; then
        debug "could not resolve $NODE_NAME's own medium from the machine; using ${NODE_DEVICE:-none} as declared"
        printf '%s' "${NODE_DEVICE:-}"
        return 0
    fi
    if [ "$got" != "${NODE_DEVICE:-}" ]; then
        warn "$NODE_NAME's conf says $NODE_DEVICE, but its own medium is $got right now.
  Kernel names move; this is using $got, which is what the machine says."
    fi
    printf '%s' "$got"
}

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

        disk_image_machine "$name" || true
        owner="$DISK_WHOSE_MACHINE"
        booted="$DISK_WHOSE_BOOTED"

        if [ "$name" = "${NODE_DEVICE:-}" ]; then
            printf '    %s   <- %s is configured to boot from this one (wk boot %s)\n' \
                "$line" "$NODE_NAME" "$NODE_NAME"
        else
            printf '    %s\n' "$line"
        fi
        if [ -n "$owner" ]; then
            printf '        %s  --  holds %s\n' "$desc" \
                "$([ "$owner" = "$NODE_NAME" ] && printf "this machine's own system" \
                                               || printf "a system for %s" "$owner")"
        elif [ -n "$booted" ]; then
            printf "        %s  --  this machine's own system (booted)\n" "$desc"
        elif disk_can_identify; then
            printf '        %s  --  no wk system on it\n' "$desc"
        else
            printf '        %s\n' "$desc"
        fi
    done <<EOF
$(disk_candidates)
EOF
    [ "$n" -gt 0 ] || printf '    (none -- no removable disk is attached to %s)\n' "$NODE_NAME"

    disk_can_identify || {
        warn "cannot tell which system is on any of these: $NODE_NAME's card helper is
  older than this checkout and has no 'whose' verb. The disks are listed by what
  their filesystems are labelled, which is not the same question.
  Remedy, from a terminal on $NODE_NAME:  ./setup --stage quiesce"
    }

    local mine
    mine=$(disk_for_machine "$NODE_NAME" 2>/dev/null) || mine=""
    if [ -n "${NODE_DEVICE:-}" ] && [ -n "$mine" ] && [ "$mine" != "$NODE_DEVICE" ]; then
        warn "$NODE_NAME's conf says it boots $NODE_DEVICE, but the disk holding
  $NODE_NAME's own system is $mine. Kernel names are assigned in enumeration
  order and these have moved. Trust the marker, not the name."
    fi
}

# Asked of the helper, the only implementation of the rule. Whether the image fits is not asked: it is a stream read once, so its size is unknown until it has gone past and a card too small runs out of space part-written.
disk_refuse_unless_safe() {
    local dev="$1" out

    card_priv_require
    # A helper without @second answers `check <disk>@second` for the whole disk, and its `write` would take the whole disk too -- over the rescue it may be running from.
    if disk_is_second "$dev"; then
        card_priv status 2>/dev/null | grep -q 'second=yes' \
            || die "$NODE_NAME's card helper predates second systems (@second), so it
    would write the whole disk. Update it first: on a workstation,
    ./setup --stage quiesce from a terminal there; on a rescue, rebuild the
    rescue image and write it again."
        case "$dev" in (*@third)
            card_priv status 2>/dev/null | grep -q 'third=yes' \
                || die "$NODE_NAME's card helper predates third systems (@third).
    Update it first: on a workstation, ./setup --stage quiesce from a terminal
    there; on a rescue, rebuild the rescue image and write it again." ;;
        esac
    fi
    out=$(card_priv check "$dev" 2>&1) || die "$NODE_NAME will not write $dev:
$(printf '%s\n' "$out" | sed 's/^/    /')
    Disks there:
$(disk_list)"
    debug "$out"
}

disk_size() { # <device>
    m_ssh "lsblk -dno SIZE $(sh_quote "$1")" 2>/dev/null | tr -d ' \r'
}

disk_unmount() {
    local dev="$1"
    disk_would "unmount whatever is mounted from $dev on $NODE_NAME" && return 0
    card_priv unmount "$dev" >/dev/null \
        || die "could not unmount what is on $dev on $NODE_NAME.
    Something is using it:
$(m_ssh "lsblk -lno NAME,MOUNTPOINT $(sh_quote "$dev")" 2>/dev/null | awk 'NF > 1 { print "    /dev/" $1 " at " $2 }')"
}

disk_is_second() { case "$1" in *@second|*@third) return 0 ;; *) return 1 ;; esac; }

# `exec 3>&1` hands the meter a fd onto the ssh session's stdout, where the helper's own report goes, leaving the decompressor's stdout as the writer's stdin.
disk_write_stream() { # <device> <decompressor>  -- source bytes on stdin; the far side's report on stdout
    local dev="$1" filter="$2"
    info "writing to $dev on $NODE_NAME (streamed; decompressed there with $filter)"
    m_ssh "exec 3>&1; $filter | python3 -c $(sh_quote "$(disk_stream_meter_py)") | sudo -n $CARD_PRIV write $(sh_quote "$dev")"
}

# `< /dev/null`, like every probe here: ssh forwards its own stdin to the far side, and once the write starts that stdin is the image. Asked outside the pipeline's command substitution, where a `die` would exit only the subshell.
disk_filter_require() { # <decompressor>
    local tool="${1%% *}"
    [ "$1" = cat ] && return 0
    m_ssh "command -v $(sh_quote "$tool") >/dev/null" < /dev/null && return 0
    die "$NODE_NAME has no $tool, and the image being sent to it is compressed
    with it -- the card machine is what decompresses the stream, so this end
    never has to have the tool for a format it is only passing through.
    Remedy: install $tool on $NODE_NAME (apt spells xz 'xz-utils')."
}

# stdin to stdout unchanged, the byte count and sha256 going to fd 3 at EOF in one write call, so the two lines cannot interleave with the helper's on the fd they share.
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

# The meta file's first line is "<bytes> <sha>" of the stream; under @second a second line carries the two partitions' own sizes and hashes, which is what the read-back compares there.
disk_write_source() { # <device> <reader command> <decompressor> <meta file>
    local dev="$1" reader="$2" filter="$3" meta="$4" report
    disk_would "stream the image onto $dev on $NODE_NAME, and read it back to verify" && return 0
    disk_filter_require "$filter"
    report=$(eval "$reader" | disk_write_stream "$dev" "$filter" | tr -d '\r') \
        || die "could not write the image onto $dev on $NODE_NAME.
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
    [ -n "$got" ] || die "could not read $dev back on $NODE_NAME"
    [ "$want" = "$got" ] \
        || die "$dev does not match the image that was streamed to it
    image: $want
    disk:  $got"
    debug "verified $bytes bytes"
}

# A card that took a stream with a shell banner ahead of it hashes perfectly against what was sent and has no partition table.
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

disk_root_spec() { # <device>
    local dev="$1"
    disk_would "read the root= on $dev's kernel command line" && return 0
    card_priv root-spec "$dev" 2>/dev/null | tr -d '\r' | kv_get root
}

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

# base64 on the way, here and below, so the helper can check a value against a character set before decoding it.
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

# A firmware setting that fails to land fails nothing and makes every number worse, so this is a refusal, not a warning.
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

# Onto every system a write makes, rescue and bench alike, so a board whose arming is an edit to the card (pi-sd) can arm the next system where it stands.
disk_install_helper() { # <device>
    local dev="$1" out rc=0
    disk_would "put this machine's card helper on $dev" && return 0
    out=$(card_priv helper "$dev" 2>&1) || rc=$?
    [ "$rc" -eq 0 ] && { log "  $(printf '%s' "$out" | sed -n 's/^wk-card-priv: helper: //p')"; return 0; }
    case "$out" in
        *'usage: wk-card-priv'*)
            die "$NODE_NAME's card helper is older than this checkout: it has no
    'helper' verb, so the system being written would carry whatever its image
    was built with, and a fix made here would never reach the board.
    The image is written; the helper is not.
    Remedy, from a terminal on $NODE_NAME (its sudo asks for a password, which
    is why this end cannot do it): update its wk-tools checkout, then
        ./setup --stage quiesce" ;;
    esac
    die "could not put the card helper on $dev:
$out"
}

# The firmware's own selector, onto a medium that now holds two systems: without it the tryboot flag is ignored and the first pair boots.
disk_install_autoboot() { # <device>
    local dev="$1" out rc=0
    disk_would "write the firmware's two-system selector (autoboot.txt) onto $dev" && return 0
    out=$(card_priv autoboot "$dev" 2>&1) || rc=$?
    [ "$rc" -eq 0 ] && { debug "$out"; return 0; }
    case "$out" in
        *'usage: wk-card-priv'*)
            die "$NODE_NAME's card helper is older than this checkout: it has no 'autoboot'
    verb, so this medium would hold two systems with no way for the firmware to
    choose the second. Update its checkout, then:  ./setup --stage quiesce" ;;
    esac
    die "could not write the two-system selector onto $dev:
$out"
}

disk_boot_id() { # <device> <id>
    local dev="$1" id="$2"
    disk_would "name the system on $dev's boot partition '$id'" && return 0
    card_priv boot-id "$dev" "$id" >/dev/null \
        || die "could not write the identity onto $dev's boot partition.
    The image is written, but 'wk boot' refuses a disk it cannot name."
}

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
            die "$NODE_NAME's card helper predates BusyBox init scripts, so this image got
    neither its self-disarm nor its self-return: a board booted into it would not
    hand itself back. The image is written. Update the helper (on a workstation,
    ./setup --stage quiesce from a terminal there; on a rescue, rebuild the
    rescue image) and write the card again." ;;
        *) debug "$out" ;;
    esac
}

# A kernel that cannot find its root panics and reboots (`panic=10`), but firmware that cannot find a kernel **halts** -- no retry, no fall-through, no way back over the wire.
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
    case "$out" in
        *'no boot-file checker'*)
            warn "$dev's boot files were NOT checked: $NODE_NAME's card helper has no boot-file
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

# The old identity is read off the card now: every reference to it there (cmdline.txt, /etc/fstab) is rewritten from it, and a stale one rewrites nothing while reporting success.
disk_unique_identity() { # <device>
    local dev="$1" spec old new
    if disk_is_second "$dev"; then
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

    m_ssh "lsblk -no PARTUUID $(sh_quote "$(disk_part "$dev" 2)")" 2>/dev/null | tr -d '\r ' \
        | grep -qx "$new-02" \
        || die "$dev did not take the new identity; refusing to leave it ambiguous"
}

# The driving key goes into root's authorized_keys because a Yocto image ships `PermitRootLogin yes` with an empty root password, usable by a person but not by `ssh -o BatchMode=yes`.
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

# Onto the card just written, never baked into the image: on a card the credential exists for one boot, wk-tailnet-join deleting it once spent.
disk_seed_tailnet() { # <device> <tailnet hostname>
    local dev="$1" name="$2" keyfile joins tag="${WK_TAILNET_TAG:-tag:wk}"
    disk_would "seed the tailnet identity on $dev (it would join as '$name', $tag)" && return 0

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
        *) die "$NODE_NAME's card helper did not say whether $dev joins the tailnet
    (it said: ${joins:-nothing}). Refusing to guess, for the same reason." ;;
    esac

    keyfile=$(wk_tailscale_authkey) || die "the tailnet auth key present moments ago at the write preflight is
    gone now, and $dev is already erased. Set one and retry:  wk key tailnet"

    info "seeding the tailnet identity onto $dev -- it joins as '$name' ($tag) on first boot"
    card_priv tailnet "$dev" "$name" "$tag" < "$keyfile" >/dev/null \
        || die "could not seed the tailnet identity onto $dev.
    The image is written; it would boot with no tailnet identity and be
    reachable only over whatever LAN it lands on."
}

# In a subshell: machine_load sets NODE_* directly and every caller here already has its own machine loaded.
_image_wants_wifi() { # <IMG_MACHINE, which is boot/machines/<name>.conf's name>
    [ -n "${1:-}" ] || return 1
    ( machine_load "$1" >/dev/null 2>&1 && [ "${NODE_NET:-}" = wifi ] )
}

# There is no hand-made credential file: the card takes its credential from $DISK_MACHINE's own WiFi connection, read by the card helper as root.
disk_seed_wifi() { # <device> <IMG_MACHINE>
    local dev="$1" mach="$2" joins
    disk_would "seed $NODE_NAME's own WiFi credential on $dev, for a board with no cable" && return 0

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
        *) die "$NODE_NAME's card helper did not say whether $dev brings up WiFi
    (it said: ${joins:-nothing}). Refusing to guess, for the same reason." ;;
    esac

    info "seeding $NODE_NAME's own WiFi credential onto $dev"
    card_priv wifi-from-host "$dev" >/dev/null \
        || die "could not seed WiFi credentials onto $dev.
    The image is written; $mach has no cable at the bench, so it would boot with
    no way to reach a network at all."
}

# tailscaled's state on partition 4 is kept aside by the helper before the split and put back after, so the new system comes up as the node the old one was. Prints yes or no, and is read-only on the card, so it runs in a dry run too.
disk_tailnet_save() { # <device>@second
    local dev="$1" out
    if ! card_priv status 2>/dev/null | grep -q 'tailnet-keep=yes'; then
        warn "$NODE_NAME's card helper cannot keep a node's tailnet identity across a rewrite,
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
        *) die "$NODE_NAME's card helper did not say whether $dev's partition 4 holds a
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

# The only difference between a rescue and a bench system, one image artifact serving both: every image's units check `ConditionPathExists=!/etc/wk/rescue`, so a bench card is one with the marker absent.
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

    case "$out" in
        *'usage: wk-card-priv'*)
            die "$NODE_NAME's card helper is older than this checkout: it has no 'role'
    verb, so the rescue marker cannot be written and this card would boot
    carrying a live self-return watchdog.
    The image is written; the role is not set.
    Remedy, from a terminal on $NODE_NAME (its sudo asks for a password, which
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
        warn "$NODE_NAME has no udisksctl, so $dev is left powered on. The write is
  complete and the card is synced -- it is safe to pull. To have the card
  powered off instead, install udisks2 on $NODE_NAME ('./setup' does, on a wk host)."
        return 0
    }
    m_ssh "udisksctl power-off -b $(sh_quote "$dev")" >/dev/null 2>&1 \
        && info "powered off $dev -- safe to remove" \
        || log "  (could not power off $dev; it is synced, so it is safe to pull anyway)"
    return 0
}
