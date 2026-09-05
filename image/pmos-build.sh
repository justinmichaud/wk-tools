#!/bin/sh
# Build a postmarketOS system with pmbootstrap, on the machine image/pmos.sh copies this to: pmbootstrap is Linux-only, needs root (loop devices, chroots, kpartx), and on aarch64 never starts qemu.
set -eu

: "${PMO_ID:?}"; : "${PMO_DEVICE:?}"; : "${PMO_CHANNEL:?}"; : "${PMO_PMB_VERSION:?}"
: "${PMO_HOSTNAME:?}"; : "${PMO_KEYFILE:?}"
PMO_UI="${PMO_UI:-phosh}"
PMO_USER="${PMO_USER:-user}"
PMO_PASSWORD="${PMO_PASSWORD:-147147}"
PMO_PACKAGES="${PMO_PACKAGES:-}"
PMO_EXTRA_SPACE="${PMO_EXTRA_SPACE:-512}"
PMO_ROOT="${PMO_ROOT:-$HOME/wk-pmos}"

VENV="$PMO_ROOT/venv"
WORK="$PMO_ROOT/work"
APORTS="$WORK/cache_git/pmaports"
CFG="$PMO_ROOT/$PMO_DEVICE.cfg"
OUT="$PMO_ROOT/out/$PMO_ID"
PMB="$VENV/bin/pmbootstrap"

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARN: %s\n' "$*"; }
die()  { printf '    ERROR: %s\n' "$*" >&2; exit 1; }

retry() {  # the build host roams between two APs on one SSID, and a brief drop fails a fetch hours in
    n=0
    while :; do
        "$@" && return 0
        n=$((n + 1))
        [ "$n" -ge 3 ] && return 1
        warn "failed: $* -- retrying in $((n * 5))s"
        sleep $((n * 5))
    done
}

step "Preflight"
[ "$(uname -s)" = Linux ] || die "this is not Linux; pmbootstrap cannot run here"
arch=$(uname -m)
[ "$arch" = aarch64 ] || die "this host is $arch and the phones are aarch64.
    pmbootstrap would emulate the whole build with qemu -- hours instead of
    minutes -- so this refuses. Build on an aarch64 machine."

missing=""  # pmbootstrap names the missing program, not its package; for kpartx those differ
for t in python3 git xz; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
command -v kpartx    >/dev/null 2>&1 || missing="$missing kpartx(multipath-tools)"
command -v losetup   >/dev/null 2>&1 || missing="$missing losetup(util-linux)"
python3 -c 'import ensurepip' 2>/dev/null || missing="$missing python3-venv"
python3 -c 'import yaml' 2>/dev/null || missing="$missing python3-yaml"
[ -z "$missing" ] || die "missing on this host:$missing
    sudo apt install -y multipath-tools python3-venv python3-yaml xz-utils git"
sudo -n true 2>/dev/null || die "sudo needs a password here.
    pmbootstrap mounts chroots and loop devices; it cannot do that
    non-interactively without passwordless sudo."
[ -r "$PMO_KEYFILE" ] || die "no public key at $PMO_KEYFILE"

# A pinned git tag in a venv of its own: every PyPI release is yanked (1.0.1 through 2.1.0) and Ubuntu has no package.
step "pmbootstrap $PMO_PMB_VERSION"
have_ver=""
[ -x "$PMB" ] && have_ver=$("$PMB" --version 2>/dev/null || true)
if [ "$have_ver" = "$PMO_PMB_VERSION" ]; then
    info "already installed"
else
    [ -d "$VENV" ] || python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q \
        "git+https://gitlab.postmarketos.org/postmarketOS/pmbootstrap.git@$PMO_PMB_VERSION" \
        || die "could not install pmbootstrap $PMO_PMB_VERSION"
    info "installed $("$PMB" --version)"
fi

step "Work folder"
WORK_VERSION=8
if [ ! -d "$WORK" ]; then
    mkdir -p "$WORK"
    printf '%s\n' "$WORK_VERSION" > "$WORK/version"
    info "created $WORK (version $WORK_VERSION)"
elif [ ! -f "$WORK/version" ]; then
    # pmbootstrap refuses to migrate a work folder with no version file, which a failed run's empty directory looks like.
    printf '%s\n' "$WORK_VERSION" > "$WORK/version"
    info "stamped $WORK with version $WORK_VERSION"
fi

# pmbootstrap reads channels.cfg from `origin/master` while pmaports' default branch is `main`, so a single-branch clone resolves no channel; and the channel must be one channels.cfg lists, not merely a branch.
step "pmaports ($PMO_CHANNEL)"
if [ ! -d "$APORTS/.git" ]; then
    mkdir -p "$(dirname "$APORTS")"
    retry git clone -q --depth 1 https://gitlab.postmarketos.org/postmarketOS/pmaports.git "$APORTS" \
        || die "could not clone pmaports"
fi
cd "$APORTS"
retry git fetch -q --depth 1 origin "+refs/heads/master:refs/remotes/origin/master" \
    || die "could not fetch pmaports' master ref (channels.cfg lives there)"
# Into the tracking ref, then `checkout -B`: git refuses to fetch straight into refs/heads/<channel> while it is checked out.
retry git fetch -q --depth 1 origin "+refs/heads/$PMO_CHANNEL:refs/remotes/origin/$PMO_CHANNEL" \
    || die "could not fetch the $PMO_CHANNEL branch of pmaports"
git checkout -q -B "$PMO_CHANNEL" "refs/remotes/origin/$PMO_CHANNEL"
cd - >/dev/null
(cd "$APORTS" && git show origin/master:channels.cfg) | grep -q "^\[$PMO_CHANNEL\]" \
    || die "'$PMO_CHANNEL' is not a channel in pmaports' channels.cfg.
    Channels are the released ones:
$( (cd "$APORTS" && git show origin/master:channels.cfg) | sed -n 's/^\[\(v[0-9.]*\|edge\)\]$/      \1/p')"
APORTS_REV=$(cd "$APORTS" && git rev-parse --short HEAD)
info "pmaports $PMO_CHANNEL @ $APORTS_REV"

# `aports` does not follow `work`: left out, pmbootstrap looks under ~/.local/var/pmbootstrap. systemd = never, the bridge role being OpenRC.
step "Config"
cat > "$CFG" <<EOF
# Written by image/pmos-build.sh for $PMO_ID. Edit the profile, not this.
[pmbootstrap]
device = $PMO_DEVICE
ui = $PMO_UI
user = $PMO_USER
hostname = $PMO_HOSTNAME
work = $WORK
aports = $APORTS
ssh_keys = True
ssh_key_glob = $PMO_KEYFILE
systemd = never
EOF
info "$CFG"

# --password is pmbootstrap's automation dummy, logged in plain text: it is the console password on the phone's own screen, and bridge/provision.sh turns password auth off.

# PMO_KCONFIG's options are applied after the checkout, which resets the tree every run; the APKBUILD's sha512sums cover the config, and pkgrel jumps 100 or pmbootstrap reuses the stock apk.
if [ -n "${PMO_KCONFIG:-}" ]; then
    step "Kernel config delta ($PMO_KERNEL_APORT)"
    [ -n "${PMO_KERNEL_APORT:-}" ] \
        || die "the profile sets PMO_KCONFIG but no PMO_KERNEL_APORT to apply it to"
    KDIR="$APORTS/$PMO_KERNEL_APORT"
    [ -d "$KDIR" ] || die "no such aport in pmaports $PMO_CHANNEL: $PMO_KERNEL_APORT"

    # Globbed rather than `ls | head -1`, which SIGPIPEs under pipefail.
    KCFG=""
    for f in "$KDIR"/config-*.aarch64; do
        [ -f "$f" ] && { KCFG="$f"; break; }
    done
    [ -n "$KCFG" ] || die "no config-*.aarch64 in $KDIR"

    for opt in $PMO_KCONFIG; do
        name=${opt%%=*}
        was=$(sed -n "s/^$name=/=/p;s/^# $name is not set\$/unset/p" "$KCFG" | tr '\n' ' ')  # before editing, or the "old" value is the new one
        if grep -q "^$name=" "$KCFG" 2>/dev/null; then
            sed -i "s|^$name=.*|$opt|" "$KCFG"
            info "$opt (was ${was:-something else})"
        elif grep -q "^# $name is not set$" "$KCFG" 2>/dev/null; then
            sed -i "s|^# $name is not set\$|$opt|" "$KCFG"
            info "$opt (was unset)"
        else
            printf '%s\n' "$opt" >> "$KCFG"
            info "$opt (appended; the option was absent)"
        fi
        grep -q "^$opt\$" "$KCFG" \
            || die "could not apply $opt to $(basename "$KCFG")"
    done

    KREL=$(sed -n 's/^pkgrel=\([0-9]*\)$/\1/p' "$KDIR/APKBUILD" | tail -1)  # before the checksums: that run reads the APKBUILD
    [ -n "$KREL" ] || die "could not read pkgrel from $KDIR/APKBUILD"
    sed -i "s/^pkgrel=$KREL\$/pkgrel=$((KREL + 100))/" "$KDIR/APKBUILD"
    info "pkgrel $KREL -> $((KREL + 100)), so the cached stock package cannot win"

    "$PMB" -c "$CFG" checksum "$(basename "$PMO_KERNEL_APORT")" >/dev/null 2>&1 \
        || die "could not regenerate checksums for $(basename "$PMO_KERNEL_APORT")
    The config was edited, so abuild will refuse the build without this."
    info "checksums regenerated"
fi

step "Shutting down any previous chroot"
"$PMB" -c "$CFG" -y shutdown >/dev/null 2>&1 || warn "pmbootstrap shutdown reported a problem; continuing"

step "pmbootstrap install ($PMO_DEVICE, $PMO_UI, +${PMO_EXTRA_SPACE}M)"
mkdir -p "$OUT"  # the artifacts below, not the directory: $OUT holds this run's log
rm -f "$OUT/disk.wic.xz" "$OUT/disk.bmap" "$OUT/result"
add=""
[ -n "$PMO_PACKAGES" ] && add="--add $PMO_PACKAGES"
# shellcheck disable=SC2086
"$PMB" -c "$CFG" -y -E "$PMO_EXTRA_SPACE" --details-to-stdout install \
    --no-split --no-firewall --password "$PMO_PASSWORD" $add \
    || {  # pmbootstrap's traceback names the failed command without its output
        warn "pmbootstrap install failed; the last 40 lines of its own log:"
        tail -40 "$WORK/log.txt" 2>/dev/null | sed 's/^/    | /'
        die "pmbootstrap install failed (its log: $WORK/log.txt on this host)"
    }

step "Shutting down the chroot"  # the loop device the install used is still attached to the image
"$PMB" -c "$CFG" -y shutdown >/dev/null 2>&1 || warn "pmbootstrap shutdown reported a problem; continuing"

img=$(find "$WORK/chroot_native/home/pmos/rootfs" -maxdepth 1 -name "$PMO_DEVICE.img" 2>/dev/null | head -1)
[ -n "$img" ] || img=$(find "$WORK" -maxdepth 6 -name "$PMO_DEVICE.img" 2>/dev/null | head -1)
[ -n "$img" ] || die "pmbootstrap reported success but no $PMO_DEVICE.img is under $WORK"
info "image: $img ($(du -h "$img" | cut -f1))"

# pmbootstrap embeds each device's firmware at the offset the device declares, in units of 1024 bytes, not sectors.
step "Checking the bootloader pmbootstrap embedded"
DEVICEINFO=$(find "$APORTS/device" -name deviceinfo -path "*device-$PMO_DEVICE/*" | head -1)
[ -n "$DEVICEINFO" ] || die "no deviceinfo for $PMO_DEVICE under $APORTS/device"
embed=$(sed -n 's/^deviceinfo_sd_embed_firmware="\(.*\)"$/\1/p' "$DEVICEINFO")
fw_step=$(sed -n 's/^deviceinfo_sd_embed_firmware_step_size="\{0,1\}\([0-9]*\).*/\1/p' "$DEVICEINFO")
case "$fw_step" in ''|*[!0-9]*) fw_step=1024 ;; esac

if [ -z "$embed" ]; then  # a device flashed over fastboot or uuu is right to carry none
    warn "$PMO_DEVICE declares no sd_embed_firmware: this image carries no"
    warn "  bootloader of its own, and a card written from it boots only if the"
    warn "  device finds firmware somewhere else."
else
    for entry in $embed; do
        f="${entry%:*}"; off="${entry##*:}"
        byte=$((off * fw_step))
        nonzero=$(sudo -n dd if="$img" bs=1 skip="$byte" count=512 status=none \
                  | tr -d '\0' | wc -c | tr -d ' ')
        [ "${nonzero:-0}" -gt 0 ] \
            || die "$(basename "$f") should be at $((byte / 1024)) KiB and that byte range is
    all zeros. pmbootstrap did not embed it, and a card written from this image
    would not boot -- the phone would come up on its internal storage instead."
        info "$(basename "$f") is at $((byte / 1024)) KiB ($nonzero non-zero bytes in the first 512)"
    done
fi

# The WiFi credential and avahi are what pmbootstrap cannot put in, and until `wk bridge setup` runs `tailscale up` <hostname>.local is the whole of first contact. The PSK is read from this host's own connection, so it travels through no log; no bssid is copied, two APs sharing one SSID.
step "Seeding the image"
keyfile=$(mktemp)
chmod 600 "$keyfile"
sudo -n cat /etc/netplan/*.yaml 2>/dev/null | python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
for doc in docs:
    for _, dev in (doc.get("network", {}).get("wifis") or {}).items():
        for ssid, ap in (dev.get("access-points") or {}).items():
            psk = ((ap.get("auth") or {}).get("password")
                   or ap.get("password") or "")
            if not psk:
                continue
            print("[connection]")
            print("id=wk-uplink")
            print("type=wifi")
            print("autoconnect=true")
            print("autoconnect-retries=0")
            print("")
            print("[wifi]")
            print("mode=infrastructure")
            print("ssid=%s" % ssid)
            # A stable MAC holds a DHCP reservation. (No apostrophes in here: single-quoted shell string.)
            print("cloned-mac-address=permanent")
            print("")
            print("[wifi-security]")
            print("key-mgmt=wpa-psk")
            print("psk=%s" % psk)
            print("")
            print("[ipv4]")
            print("method=auto")
            print("")
            print("[ipv6]")
            print("method=auto")
            print("addr-gen-mode=default")
            sys.exit(0)
sys.exit(3)
' > "$keyfile" || { rm -f "$keyfile"; die "no WiFi credential found in this host's netplan.
    The image needs one: the phone has no cable, so an image without it boots
    into isolation. Build on a host that is on the WiFi the phone will use."; }
ssid=$(sed -n 's/^ssid=//p' "$keyfile")

loop=$(sudo -n losetup --show -P -f "$img")
trap 'sudo -n umount "$OUT/mnt" 2>/dev/null || true; sudo -n losetup -d "$loop" 2>/dev/null || true; rm -f "$keyfile"' EXIT INT TERM
mkdir -p "$OUT/mnt"
sudo -n mount "${loop}p2" "$OUT/mnt" || die "could not mount ${loop}p2"
[ -d "$OUT/mnt/etc/NetworkManager" ] \
    || die "${loop}p2 has no /etc/NetworkManager -- that is not the rootfs"

sudo -n install -o 0 -g 0 -m 0600 "$keyfile" \
    "$OUT/mnt/etc/NetworkManager/system-connections/wk-uplink.nmconnection"
info "uplink: $ssid (the PSK stayed on this host)"

# The same key for root: pmbootstrap installs it for the console user only, who needs a password to become root, and `wk bridge setup` is non-interactive. `sudo -n test` because that .ssh is 700 owned by the image.
if sudo -n test -f "$OUT/mnt/home/$PMO_USER/.ssh/authorized_keys"; then
    sudo -n install -d -o 0 -g 0 -m 0700 "$OUT/mnt/root/.ssh"
    sudo -n install -o 0 -g 0 -m 0600 \
        "$OUT/mnt/home/$PMO_USER/.ssh/authorized_keys" \
        "$OUT/mnt/root/.ssh/authorized_keys"
    info "root: the same ssh key, so provisioning needs no password on the phone"
else
    warn "no authorized_keys for '$PMO_USER' in the image -- root gets no key either."
    warn "  'wk bridge setup' will then fail on its first privileged write, because"
    warn "  the console user cannot become root without typing a password."
fi

# bridge/provision.sh applies these too, but over ssh -- which a phone needing them to answer ssh cannot receive. The RTL8723CS misses frames aimed at it when its RF side idles, and an unprovisioned phone sleeps.
sudo -n install -d -o 0 -g 0 -m 0755 "$OUT/mnt/etc/NetworkManager/conf.d"
printf '%s\n' \
    '# Written into the image by image/pmos-build.sh. bridge/provision.sh writes' \
    '# the same thing; a phone needs it before it can be provisioned at all.' \
    '[connection]' \
    'wifi.powersave=2' \
    '' \
    '[device]' \
    'wifi.scan-rand-mac-address=no' \
    | sudo -n tee "$OUT/mnt/etc/NetworkManager/conf.d/99-wk-bridge-reachable.conf" >/dev/null
info "power save off and scan MAC pinned, so the phone answers before it is provisioned"

if [ -d "$OUT/mnt/etc/elogind" ]; then
    sudo -n install -d -o 0 -g 0 -m 0755 "$OUT/mnt/etc/elogind/logind.conf.d"
    printf '%s\n' \
        '# Written into the image by image/pmos-build.sh: a phone that suspends' \
        '# before it is provisioned cannot be provisioned.' \
        '[Login]' \
        'HandlePowerKey=ignore' \
        'HandleSuspendKey=ignore' \
        'HandleHibernateKey=ignore' \
        'HandleLidSwitch=ignore' \
        'IdleAction=ignore' \
        | sudo -n tee "$OUT/mnt/etc/elogind/logind.conf.d/10-wk-bridge-nosleep.conf" >/dev/null
    info "suspend disabled in the image, so the first provision is not a race"
else
    warn "no /etc/elogind in the image -- cannot disable suspend before first boot"
fi

if [ -x "$OUT/mnt/etc/init.d/avahi-daemon" ] && [ -d "$OUT/mnt/etc/runlevels/default" ]; then
    if [ -e "$OUT/mnt/etc/runlevels/default/avahi-daemon" ]; then
        info "avahi-daemon already enabled in the image"
    else
        sudo -n ln -s /etc/init.d/avahi-daemon "$OUT/mnt/etc/runlevels/default/avahi-daemon"
        info "enabled avahi-daemon, so $PMO_HOSTNAME.local answers on the first boot"
    fi
else
    warn "no avahi-daemon in the image: first contact will need the phone's own"
    warn "  address, since it has no tailnet name until it is provisioned"
fi

sudo -n sync
sudo -n umount "$OUT/mnt"
sudo -n losetup -d "$loop"
rm -f "$keyfile"
trap - EXIT INT TERM
rmdir "$OUT/mnt"

step "Compression"  # it comes back over WiFi and a phone image is mostly empty space
raw_bytes=$(stat -c %s "$img")
raw_sha=$(sha256sum "$img" | cut -d' ' -f1)
xz -T0 -3 -c "$img" > "$OUT/disk.wic.xz" || die "could not compress $img"

cat > "$OUT/result" <<EOF
device=$PMO_DEVICE
ui=$PMO_UI
channel=$PMO_CHANNEL
pmaports_rev=$APORTS_REV
pmbootstrap=$("$PMB" --version)
user=$PMO_USER
hostname=$PMO_HOSTNAME
uplink_ssid=$ssid
raw_bytes=$raw_bytes
raw_sha256=$raw_sha
wic_xz_bytes=$(stat -c %s "$OUT/disk.wic.xz")
EOF

step "Done"
info "$OUT/disk.wic.xz  ($(du -h "$OUT/disk.wic.xz" | cut -f1) of $(du -h "$img" | cut -f1) raw)"
cat "$OUT/result"
