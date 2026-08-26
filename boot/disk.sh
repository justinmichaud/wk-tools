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
# What is on each removable disk, as "<disk> <TAB> <description>".
#
# A kernel name is not an identity. `sda` and `sdb` are assigned in enumeration
# order, so two sticks in one machine swap names across a reboot -- and the only
# thing marking one of them as the machine's own was `MACH_DEVICE`, which is a
# kernel name written down in a conf. Two sticks and a card in one reader is
# exactly when that matters, and picking the wrong one erases the wrong system.
#
# So the listing says what each disk *contains*, from the disk: its filesystem
# labels, in one ssh round trip, with nothing mounted -- lsblk reads the
# superblocks.
#
# The labels are reported rather than interpreted, and that is the correction
# worth keeping: `WK-IMG-BOOT`/`wk-image-root` mean a card came from the
# distro-seeding path, and a yocto image labels its partitions `boot` and `root`
# like any other. Reading the first as "holds a wk system" and the second as
# something else says the opposite of the truth about a card wk wrote a minute
# ago. What actually answers "whose is this" is the serial, below.
#
# Reading the identity marker instead would be the honest test -- every image wk
# writes carries /etc/wk-image -- but it is on an ext4 partition and would cost
# a privileged mount per disk, which a listing has no business doing.
# Which machine's system is on a disk, read from the disk.
#
# The identity is written *into* an image at write time (/etc/wk-image, with
# `machine=`), so the disk can be asked and nothing out here has to remember.
# That is the whole reason this replaced a declared serial per machine: a serial
# is a second copy of a fact, and the first swapped stick makes it a lie. A
# marker travels with the medium.
#
# Empty for a blank disk, for one somebody else wrote, and for a helper too old
# to have the verb -- all three are "this repo cannot say", which is the honest
# answer and is what makes the caller ask rather than guess.
#
# One mount per disk, read-only, on the machine holding it. Cached for the
# process because a listing asks about every disk and a probe is not free.
# Can the machine holding the disks answer the question at all?
#
# Asked once, and kept apart from the answer, because "this disk has no marker"
# and "this end cannot look" are different facts with different remedies -- and
# reporting the second as the first says a freshly written card is blank.
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

_WK_WHOSE=""
disk_image_machine() { # <device>
    local dev="$1" hit out
    disk_can_identify || return 1

    hit=$(printf '%s\n' "$_WK_WHOSE" | awk -v d="$dev" '$1 == d { print $2; exit }')
    [ -z "$hit" ] || { [ "$hit" = "-" ] || printf '%s' "$hit"; return 0; }

    out=$(card_priv whose "$dev" </dev/null 2>/dev/null) || out=""
    hit=$(printf '%s\n' "$out" | sed -n 's/^machine=//p' | head -1)
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

# The machine's own medium, resolved from the machine rather than trusted from
# its conf.
#
# `MACH_DEVICE` is a kernel name, and kernel names are assigned in enumeration
# order -- so on a board that is also the fleet's card reader, another board's
# stick can take the name the conf uses, and the arming path writes a partition
# type byte straight to it. That is the harm this exists to prevent.
#
# The rule is **exclusion**, not recognition, and that distinction is the whole
# of why this works:
#
#   start with the disks of the expected transport. One of them and there is
#   nothing to resolve -- the normal case, and what makes swapping a stick just
#   work, because a fresh unmarked stick is still the only USB.
#
#   then drop the ones that say they are somebody else's. A system written for
#   another board names that board in its marker, and that is a definite
#   statement about what a disk is *not*.
#
#   if one is left, it is this machine's.
#
# Recognition was tried first and is not enough: it asked for a disk whose marker
# names this machine, and this machine's own medium may carry no marker at all --
# rpi5's stick was written by a path that predates markers, so the disk that is
# obviously its own is the one that cannot prove it. Exclusion needs no marker on
# the disk it selects, only on the ones it rejects.
#
# Prints nothing when more than one survives, which leaves the caller to refuse
# rather than pick. Silence is "the machine did not say", never "use the conf".
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

# The device the machine's own medium is at right now, for a caller that is
# about to write to it. Falls back to the declared name only when the machine
# could not be asked, and says so -- a fallback that is silent is one that acts
# on a stale name without anybody knowing.
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

# disk_for_machine <machine> -- the disk on the machine m_ssh is aimed at that
# holds <machine>'s system, or nothing.
#
# Evidence only. A blank disk matches nothing, which is correct: the first write
# to a fresh stick has to name it, and every write after that does not, because
# by then the stick says what it is.
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

        # Whose system is on it, read off the disk. Not "whose medium is this" --
        # nothing here knows that, and nothing needs to: what a write has to
        # avoid is overwriting a system somebody wants, and the system says who
        # it is for.
        # `|| true`: this returns non-zero when the answer is unavailable, and
        # an unavailable answer is a line of the listing, not the end of it.
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

    # Rule 5 (CLAUDE.md): when the record and the machine disagree, the machine
    # wins and the command says so.
    #
    # The conf names a kernel name and the disk carries a marker. If this
    # machine's boot device is not the disk holding this machine's system, the
    # names have moved underneath the conf -- `sd*` is assigned in enumeration
    # order, and this board doubles as the fleet's card reader, so other boards'
    # media sit beside its own. Writing the one the conf names would erase
    # whichever disk happens to be there now.
    local mine
    mine=$(disk_for_machine "$MACH_NAME" 2>/dev/null) || mine=""
    if [ -n "${MACH_DEVICE:-}" ] && [ -n "$mine" ] && [ "$mine" != "$MACH_DEVICE" ]; then
        warn "$MACH_NAME's conf says it boots $MACH_DEVICE, but the disk holding
  $MACH_NAME's own system is $mine. Kernel names are assigned in enumeration
  order and these have moved. Trust the marker, not the name."
    fi
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
