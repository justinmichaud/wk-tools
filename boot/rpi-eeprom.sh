# Writing a Pi 4's bootloader EEPROM from a board that has no eeprom tooling.
#
# `wk pi boot-order` was written against Raspberry Pi OS, where
# `rpi-eeprom-config --apply` does the whole job on the board. The fleet's rpi4
# does not run Raspberry Pi OS -- it runs the WebKit Dev@CI Yocto image, whose
# 668-package manifest carries `vcgencmd` and nothing else from the rpi-eeprom
# package: no rpi-eeprom-config, no rpi-eeprom-update, no flashrom. So on the
# fleet's own rpi4, the writer refused.
#
# It refused for a good reason and the reason is worth keeping: it is a check
# that the machine is a Pi whose firmware configuration can be written *from
# the running system*. What was wrong is treating that as the only way in.
#
# The way in that needs nothing on the board:
#
#   the ROM runs recovery.bin from the boot partition before it runs anything
#   else, and recovery.bin flashes pieeprom.upd (checked against pieeprom.sig)
#   and then deletes itself.
#
# Every part of that is a *file*, so the work moves to the machine driving the
# operation -- which already has python3 and a network -- and the board's side
# is three files and a reboot. This is what `rpi-eeprom-update` does on Pi OS;
# the only thing lifted out is that it does it locally.
#
# Two consequences to be honest about, both surfaced at the confirm prompt:
#
# - **The whole EEPROM image is replaced, not just its config section.** There
#   is no way to modify the running EEPROM's own image without reading it back,
#   and reading it back is the part that needs the tooling this board lacks. So
#   the bootloader is *upgraded* to the pinned release below and the board's
#   existing configuration is carried across on top of it. On the fleet's rpi4
#   that took it from 2023-01-11 to 2026-05-17.
# - **It takes effect on the next boot, not now.** recovery.bin runs from the
#   ROM, so nothing has changed until the board has been through a power cycle
#   or a reboot.
#
# The failure mode is the mild one: recovery.bin verifies pieeprom.upd against
# the SHA-256 in pieeprom.sig before it writes anything, so a corrupt transfer
# flashes nothing and the board boots as it did before.
#
# Sourced by cmd/pi, and it uses that command's `rsh` and `$HOST` rather than
# opening its own connections: `wk pi` is where the ssh destination is resolved
# and refused, and a second opinion about how to reach the board is a second
# place for it to be wrong.

# The pinned upstream artefacts.
#
# Three files rather than the repository, and not the release tarball either:
# GitHub's tarball of this commit is 106 MB, of which this needs 660 KB. Each
# file is fetched by commit-addressed raw URL and pinned by sha256 -- the same
# contract as `image_fetch_base` in lib/image.sh, for the same reason.
#
# firmware-2711 is the Pi 4 / CM4 / Pi 400 tree (BCM2711) and is the only one
# fetched, which is why eeprom_check_soc refuses anything else rather than
# quietly flashing a Pi 5 with a Pi 4 bootloader.
#
# `default` rather than `latest`: it is the channel `rpi-eeprom-update` installs
# when nobody asks for anything, so it is the image with the most boards behind
# it. Bumping the pin means bumping all three values together -- the image
# filename carries its own date, so a stale pair fails the checksum rather than
# mixing releases.
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

# Read the running configuration without rpi-eeprom-config.
#
# `vcgencmd bootloader_config` asks the firmware for the config section it
# actually booted with, which is the same text rpi-eeprom-config prints and is
# available on any Pi with the VideoCore tools. It is a read, so it is the half
# of the job that was never the problem.
eeprom_read_config() {
    local out
    out=$(rsh 'vcgencmd bootloader_config 2>/dev/null || sudo vcgencmd bootloader_config' | tr -d '\r')
    [ -n "$out" ] || die "could not read $HOST's bootloader configuration.
    'vcgencmd bootloader_config' returned nothing, which on a Pi 4 means either
    the VideoCore tools are missing or this user cannot reach /dev/vcio."
    printf '%s\n' "$out"
}

# Where the firmware reads files from, on the board.
#
# Not hardcoded: it is /boot on the Yocto image and /boot/firmware on Ubuntu's,
# and getting it wrong means staging an update the ROM never looks at. Asked of
# /proc/mounts rather than findmnt, which buildroot images do not all have, and
# confirmed by the presence of the Pi 4 second-stage firmware so that some
# unrelated FAT volume cannot win.
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

# Remove a consumed update from the boot partition.
#
# recovery.bin renames itself to RECOVERY.000 once it has flashed, but nothing
# clears the pieeprom.upd and pieeprom.sig it read -- rpi-eeprom-update does
# that itself on the next run, and there is no rpi-eeprom-update here. Left
# behind they are inert (the bootloader compares versions and skips an update
# it already has) but not harmless to read: a board carrying a staged update it
# has already applied looks exactly like a board with one still pending.
#
# Called only when the running firmware already reports what was asked for,
# which is the one moment it is certain the staged files have been consumed.
eeprom_clear_staged() {
    local bootfs
    bootfs=$(eeprom_bootfs 2>/dev/null) || return 0
    rsh "sudo rm -f '$bootfs/pieeprom.upd' '$bootfs/pieeprom.sig' \
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

    # pieeprom.sig is what makes a half-written transfer harmless: recovery.bin
    # hashes the image and refuses it on a mismatch. Three lines, generated here
    # rather than by the upstream rpi-eeprom-digest shell script, because that
    # is all rpi-eeprom-digest writes for an unsigned update and shipping a
    # fourth pinned file to run `sha256sum` would be silly.
    {
        sha256sum "$work/pieeprom.upd" | cut -d' ' -f1
        echo "ts: $(date -u +%s)"
        echo "target-soc: 2711"
    } > "$work/pieeprom.sig"

    bootfs=$(eeprom_bootfs)
    info "staging the EEPROM update in $bootfs on $HOST"

    # recovery.bin last, deliberately. It is the trigger: the ROM runs whatever
    # recovery.bin it finds, so it must not be findable before the image and
    # the signature it will look for are both already there. A power cut
    # partway through then leaves a board that boots normally.
    scp -q -o BatchMode=yes "$work/pieeprom.upd" "$work/pieeprom.sig" "$HOST:$bootfs/" \
        || die "could not copy the EEPROM update to $HOST:$bootfs"

    for f in pieeprom.upd pieeprom.sig; do
        sum_local=$(sha256sum "$work/$f" | cut -d' ' -f1)
        sum_remote=$(rsh "sha256sum '$bootfs/$f'" | cut -d' ' -f1 | tr -d '\r')
        [ "$sum_local" = "$sum_remote" ] || die "$f did not survive the copy to $HOST
    ($sum_local here, $sum_remote there). Nothing has been armed: recovery.bin
    was not copied, so the board still boots as it did."
    done

    scp -q -o BatchMode=yes "$dir/recovery.bin" "$HOST:$bootfs/" \
        || die "could not copy recovery.bin to $HOST:$bootfs"
    rsh 'sync'
    rm -rf "$work"
}
