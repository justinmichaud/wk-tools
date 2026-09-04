# Writes a Pi 4's bootloader EEPROM with nothing but `vcgencmd` on the board:
# the ROM runs recovery.bin from the boot partition before anything else, and
# recovery.bin flashes pieeprom.upd (checked against pieeprom.sig) and then
# deletes itself. Staging those three files is done from the machine driving
# this, over ssh, so the board needs no eeprom tooling of its own.
#
# Two consequences, both surfaced at the confirm prompt: the whole EEPROM image
# is replaced rather than just its config section (the running image cannot be
# read back without tooling the board lacks, so the bootloader is upgraded to
# the pinned release below with the board's configuration carried across), and
# it takes effect on the next boot, since recovery.bin only runs from the ROM.
#
# recovery.bin verifies pieeprom.upd's SHA-256 against pieeprom.sig before
# writing anything, so a corrupt transfer flashes nothing.
#
# Sourced by cmd/pi, using that command's `rsh` and `$HOST`.

# Three files fetched by commit-addressed raw URL and pinned by sha256, rather
# than cloning the 106 MB tree for 660 KB of files. firmware-2711 is the
# BCM2711 (Pi 4 / CM4 / Pi 400) tree; eeprom_check_soc refuses any other SoC.
# `default` is the channel `rpi-eeprom-update` installs when nothing is asked
# for. Bump all three pins together.
EEPROM_COMMIT=86759b04b22173e10186139ac3ae4debcd0d7252
EEPROM_IMAGE=pieeprom-2026-05-17.bin

eeprom_pins() {
    cat <<EOF
rpi-eeprom-config rpi-eeprom-config 39895792eb724afe5a4ed39e5798db844292efcca4317228aa790c580ddbb70f
$EEPROM_IMAGE firmware-2711/default/$EEPROM_IMAGE f1da1bda48c8f19d6eccd94f160a2c8da48fd0acdbfa47f0ac58353208d188c3
recovery.bin firmware-2711/default/recovery.bin 9ec8816886f3938d962a837347d65ccc1d03e811a84bf1ad4b608906b288d995
EOF
}

eeprom_cache_dir() { echo "$WK_STORE/rpi-eeprom/$EEPROM_COMMIT"; }

# Fetch the pinned set, once. Echoes the directory holding it.
eeprom_fetch() {
    local dir name path sha url
    dir=$(eeprom_cache_dir)
    mkdir -p "$dir"

    while read -r name path sha; do
        [ -n "$name" ] || continue
        if [ -f "$dir/$name" ] && [ "$(sha256sum "$dir/$name" | cut -d' ' -f1)" = "$sha" ]; then
            continue
        fi
        url="https://raw.githubusercontent.com/raspberrypi/rpi-eeprom/$EEPROM_COMMIT/$path"
        info "fetching $name from rpi-eeprom@${EEPROM_COMMIT:0:7}"
        curl -fL --retry 5 -o "$dir/$name" "$url" >&2 || die "could not fetch $url"
        [ "$(sha256sum "$dir/$name" | cut -d' ' -f1)" = "$sha" ] \
            || die "checksum mismatch on $dir/$name
    expected $sha
    Delete it and re-run; if it mismatches again the pin in boot/rpi-eeprom.sh
    is stale."
    done <<EOF
$(eeprom_pins)
EOF

    echo "$dir"
}

# The pinned image is BCM2711 firmware, so anything else is refused here rather
# than discovered by a board that will not boot. /proc/device-tree/compatible is
# NUL-separated, hence the tr.
eeprom_check_soc() {
    local compat
    compat=$(rsh 'tr "\0" "\n" < /proc/device-tree/compatible 2>/dev/null' | tr -d '\r')
    case "$compat" in
        *brcm,bcm2711*) return 0 ;;
        '') die "could not read $HOST's SoC from /proc/device-tree/compatible, so
    there is no way to tell whether the pinned bootloader image is the right
    one for it. Refusing to flash an EEPROM on a guess." ;;
        *) die "$HOST is not a BCM2711 (Pi 4 / CM4 / Pi 400); it reports:
$(printf '%s\n' "$compat" | sed 's/^/      /')
    boot/rpi-eeprom.sh pins the firmware-2711 bootloader only, and flashing it
    to another SoC would brick the board." ;;
    esac
}

# `vcgencmd bootloader_config` asks the firmware for the config section it
# booted with -- the same text rpi-eeprom-config prints, available on any Pi
# with the VideoCore tools.
eeprom_read_config() {
    local out
    # r_sudo, not `vcgencmd || sudo vcgencmd`: a BusyBox bench system has no
    # sudo at all, so that pair reports `sudo: not found` where the real fault
    # is a missing vcgencmd.
    out=$(r_sudo "vcgencmd bootloader_config" 2>/dev/null | tr -d '\r')
    if [ -z "$out" ]; then
        if ! r_ssh "command -v vcgencmd >/dev/null 2>&1"; then
            die "$NODE_NAME is running a system with no vcgencmd, so its EEPROM cannot be
    read from here at all -- the buildroot bench images carry no VideoCore
    tools. Its *rescue* does: boot that and re-run.
        wk boot $NODE_NAME --back
    If the firmware keeps landing on the bench medium instead (which is the
    very thing a boot order change fixes), take that medium out for one boot."
        fi
        die "could not read $NODE_NAME's bootloader configuration: vcgencmd is there but
    'bootloader_config' returned nothing, so this user cannot reach /dev/vcio."
    fi
    printf '%s\n' "$out"
}

# Where the firmware reads files from: /boot on the Yocto image, /boot/firmware
# on Ubuntu's. Asked of /proc/mounts rather than findmnt, which buildroot images
# do not all have, and confirmed by the presence of the Pi 4 second-stage
# firmware so that some unrelated FAT volume cannot win.
eeprom_bootfs() {
    local d found=""
    for d in $(rsh 'awk "\$3 == \"vfat\" { print \$2 }" /proc/mounts' | tr -d '\r'); do
        if rsh "[ -f '$d/start4.elf' ]"; then found="$d"; break; fi
    done
    [ -n "$found" ] || die "found no mounted FAT boot partition on $HOST holding start4.elf.
    recovery.bin has to be staged where the firmware reads it, and this cannot
    tell where that is. Mount the board's boot partition and re-run."
    echo "$found"
}

# recovery.bin renames itself to RECOVERY.000 but leaves pieeprom.upd and
# pieeprom.sig behind, and an applied update then looks identical to a pending
# one. Called only once the running firmware reports what was asked for.
eeprom_clear_staged() {
    local bootfs
    bootfs=$(eeprom_bootfs 2>/dev/null) || return 0
    r_sudo "rm -f '$bootfs/pieeprom.upd' '$bootfs/pieeprom.sig' \
                  '$bootfs/recovery.bin' '$bootfs'/RECOVERY.0* '$bootfs'/recovery.0* && sync" \
        >/dev/null 2>&1 || return 0
}

# Build the update image and stage it. $1 is the configuration text.
eeprom_stage_recovery() {
    local config="$1" dir work bootfs sum_local sum_remote f
    dir=$(eeprom_fetch)

    work=$(mktemp -d)
    printf '%s\n' "$config" > "$work/boot.conf"

    python3 "$dir/rpi-eeprom-config" \
        --config "$work/boot.conf" --out "$work/pieeprom.upd" "$dir/$EEPROM_IMAGE" \
        || die "rpi-eeprom-config could not apply that configuration to $EEPROM_IMAGE"

    # recovery.bin hashes the image against this and refuses it on a mismatch.
    # Written here rather than via upstream's rpi-eeprom-digest, which would be
    # a fourth pinned file.
    {
        sha256sum "$work/pieeprom.upd" | cut -d' ' -f1
        echo "ts: $(date -u +%s)"
        echo "target-soc: 2711"
    } > "$work/pieeprom.sig"

    bootfs=$(eeprom_bootfs)
    info "staging the EEPROM update in $bootfs on $HOST"

    # recovery.bin last: it is the trigger, and the ROM runs whatever
    # recovery.bin it finds, so a power cut partway through must leave a board
    # that still boots normally.
    # shellcheck disable=SC2046
    scp -q -o BatchMode=yes $(_unpinned_host_key_opts) "$work/pieeprom.upd" "$work/pieeprom.sig" "$(rsh_dest):$bootfs/" \
        || die "could not copy the EEPROM update to $HOST:$bootfs"

    for f in pieeprom.upd pieeprom.sig; do
        sum_local=$(sha256sum "$work/$f" | cut -d' ' -f1)
        sum_remote=$(rsh "sha256sum '$bootfs/$f'" | cut -d' ' -f1 | tr -d '\r')
        [ "$sum_local" = "$sum_remote" ] || die "$f did not survive the copy to $HOST
    ($sum_local here, $sum_remote there). Nothing has been armed: recovery.bin
    was not copied, so the board still boots as it did."
    done

    # shellcheck disable=SC2046
    scp -q -o BatchMode=yes $(_unpinned_host_key_opts) "$dir/recovery.bin" "$(rsh_dest):$bootfs/" \
        || die "could not copy recovery.bin to $HOST:$bootfs"
    rsh 'sync'
    rm -rf "$work"
}
