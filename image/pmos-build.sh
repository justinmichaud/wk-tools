#!/bin/sh
# Build a postmarketOS system with pmbootstrap. Runs on a Linux aarch64
# machine, over ssh from image/pmos.sh, which copies this file there --
# pmbootstrap is Linux-only and needs root (loop devices, chroots, kpartx),
# and the workstation driving this is a Mac, the same split `wk sysimage
# write` already lives with.
#
# Native, not emulated: both phones are aarch64, so an aarch64 build host
# runs the target's own architecture and pmbootstrap never starts qemu. On
# anything else this refuses rather than quietly taking ten times as long.
#
# Inputs, all from the environment (image/pmos.sh sets every one):
#
#   PMO_ID          the image id, for the log and the result block
#   PMO_DEVICE      the pmOS device codename, e.g. pine64-pinephone
#   PMO_UI          phosh -- and it is deliberate, see image/profiles.sh
#   PMO_CHANNEL     the pmOS release channel, e.g. v25.12
#   PMO_PMB_VERSION the pmbootstrap git tag to run
#   PMO_USER        the account the phone's own screen logs into
#   PMO_PASSWORD    that account's password. Plain text, on purpose: see below
#   PMO_PACKAGES    comma-separated extras baked in
#   PMO_EXTRA_SPACE MB of slack in the image beyond the rootfs
#   PMO_HOSTNAME    what the system calls itself
#   PMO_KEYFILE     a public key file on this host, put there by the caller
#   PMO_ROOT        where the venv, the pmbootstrap work folder and the output go
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

# Three attempts, because the build host is on WiFi: it roams between two
# APs on one SSID, and a blip of a few seconds at minute one once killed a
# build that would otherwise have taken twenty ("Failed to connect ... after
# 30 ms", with the same fetch succeeding immediately after). Anything that
# reaches the network here gets wrapped.
retry() {
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

# Checked, and each name is the one that installs it. pmbootstrap's own error
# for a missing program names the program and not the package, and `kpartx` is
# the one where those differ enough to matter.
missing=""
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

# --- pmbootstrap -------------------------------------------------------------
#
# From a pinned git tag, in a venv of its own -- not from PyPI, where every
# release is yanked (1.0.1 through 2.1.0, all of them: `pip install
# pmbootstrap` fails with "no matching distribution"), and not from apt,
# which Ubuntu does not carry.
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

# --- the work folder ---------------------------------------------------------
#
# Prepared here rather than by `pmbootstrap init`, which cannot be driven
# non-interactively (it prompts for the work path before reading anything,
# and `-y` only suppresses "are you sure" -- a script of answers would be a
# guess about question order that breaks on any upstream edit). What init
# does that matters is exactly two things -- a version file and a pmaports
# clone -- both done here, with the version pinned above.
step "Work folder"
WORK_VERSION=8
if [ ! -d "$WORK" ]; then
    mkdir -p "$WORK"
    printf '%s\n' "$WORK_VERSION" > "$WORK/version"
    info "created $WORK (version $WORK_VERSION)"
elif [ ! -f "$WORK/version" ]; then
    # A work folder with no version file is one pmbootstrap refuses to migrate
    # ("we can't migrate that automatically") -- and an empty directory left
    # by a failed run looks exactly like that. Stamp it rather than making a
    # person delete it.
    printf '%s\n' "$WORK_VERSION" > "$WORK/version"
    info "stamped $WORK with version $WORK_VERSION"
fi

# pmaports, on the channel's branch -- and `origin/master` fetched as well.
# Two things fail in a way that names the wrong problem: pmbootstrap reads
# channels.cfg from `origin/master`, but pmaports' default branch is `main`,
# so a single-branch clone can't resolve any channel at all ("Failed to read
# channels.cfg from 'origin/master'"); and the channel has to be one
# channels.cfg actually lists -- `v26.06` exists as a branch, is not a
# released channel, and picking it gets a KeyError rather than a sentence.
step "pmaports ($PMO_CHANNEL)"
if [ ! -d "$APORTS/.git" ]; then
    mkdir -p "$(dirname "$APORTS")"
    retry git clone -q --depth 1 https://gitlab.postmarketos.org/postmarketOS/pmaports.git "$APORTS" \
        || die "could not clone pmaports"
fi
cd "$APORTS"
retry git fetch -q --depth 1 origin "+refs/heads/master:refs/remotes/origin/master" \
    || die "could not fetch pmaports' master ref (channels.cfg lives there)"
# Into the tracking ref, then `checkout -B` off it. Fetching straight into
# refs/heads/<channel> is refused by git the moment that branch is the one
# checked out -- which it is on every run after the first ("refusing to fetch
# into branch ... checked out at ...").
retry git fetch -q --depth 1 origin "+refs/heads/$PMO_CHANNEL:refs/remotes/origin/$PMO_CHANNEL" \
    || die "could not fetch the $PMO_CHANNEL branch of pmaports"
git checkout -q -B "$PMO_CHANNEL" "refs/remotes/origin/$PMO_CHANNEL"
cd - >/dev/null
# Refuse a channel the release metadata does not know, here, where the answer
# is one file read -- rather than inside pmbootstrap, where it is a traceback.
(cd "$APORTS" && git show origin/master:channels.cfg) | grep -q "^\[$PMO_CHANNEL\]" \
    || die "'$PMO_CHANNEL' is not a channel in pmaports' channels.cfg.
    Channels are the released ones:
$( (cd "$APORTS" && git show origin/master:channels.cfg) | sed -n 's/^\[\(v[0-9.]*\|edge\)\]$/      \1/p')"
APORTS_REV=$(cd "$APORTS" && git rev-parse --short HEAD)
info "pmaports $PMO_CHANNEL @ $APORTS_REV"

# --- the config --------------------------------------------------------------
#
# `aports` is set explicitly and that is not redundant: it does not follow
# `work`. Left out, pmbootstrap looks for pmaports under the *default* work
# folder (~/.local/var/pmbootstrap) and reports "pmaports dir not found" about
# a path nothing was ever put in.
#
# systemd = never: the bridge role is written in OpenRC service files, and
# `wk bridge setup` refuses a systemd image rather than half-applying itself.
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

# --- install -----------------------------------------------------------------
#
# --no-firewall because the bridge role owns the firewall (its own nftables
# table, packaged service disabled) -- two things writing one ruleset is how
# tailscale's chains get flushed by accident. --password is pmbootstrap's
# documented automation dummy, handled in plain text and logged -- exactly
# what it is here: the console password on the phone's own screen, useless
# over the network since bridge/provision.sh turns password auth off.
# --no-split gives one combined image file, which is what a card wants.
#
# Shut down whatever the last run left behind first: pmbootstrap leaves its
# chroots bind-mounted and the previous image on a loop device without
# cleaning up ("chroot is still active"), and the next install then fails at
# `mkfs.ext4 ... /dev/installp2` on a mapping that still belongs to the old
# image. Placed after Config, not next to the pmaports checkout it edits: the
# checksum run below is `pmbootstrap`, which needs the config file Config wrote.
# --- the kernel config delta -------------------------------------------------
#
# A profile may declare kernel options its device needs and pmOS does not set
# (PMO_KCONFIG, PMO_KERNEL_APORT in image/profiles.sh) -- applied here rather
# than as a patch file, since the change is one line of a generated defconfig
# and a patch would conflict on every pmaports bump, and applied *after* the
# checkout above, since `git checkout -B` resets the tree every run.
#
# Three things have to happen together or the build fails in a way that
# reads like something else:
#
#   the option     replaced in place whether pmOS spells it `# ... is not
#                  set` or assigns it something else, appended if absent.
#   the checksums  the APKBUILD's sha512sums cover the config file, so
#                  editing it makes abuild stop with "Use 'abuild checksum'".
#   pkgrel         bumped by a lot, or pmbootstrap reuses the previous
#                  build's identical-version apk and silently skips the
#                  change. +100 keeps a patched kernel distinguishable from
#                  stock by version alone.
if [ -n "${PMO_KCONFIG:-}" ]; then
    step "Kernel config delta ($PMO_KERNEL_APORT)"
    [ -n "${PMO_KERNEL_APORT:-}" ] \
        || die "the profile sets PMO_KCONFIG but no PMO_KERNEL_APORT to apply it to"
    KDIR="$APORTS/$PMO_KERNEL_APORT"
    [ -d "$KDIR" ] || die "no such aport in pmaports $PMO_CHANNEL: $PMO_KERNEL_APORT"

    # The config fragment for the architecture being built, named by the
    # aport's own $CARCH convention -- globbed directly rather than `ls |
    # head -1`. This runs `set -eu` without pipefail so that would not
    # actually misfire here, but `head` closing a pipe early is the shape
    # that bit cmd/selftest 32 times, and there is exactly one such config
    # per architecture anyway.
    KCFG=""
    for f in "$KDIR"/config-*.aarch64; do
        [ -f "$f" ] && { KCFG="$f"; break; }
    done
    [ -n "$KCFG" ] || die "no config-*.aarch64 in $KDIR"

    for opt in $PMO_KCONFIG; do
        name=${opt%%=*}
        # Read *before* editing, or what is printed as the old value is the
        # one that was just written.
        was=$(sed -n "s/^$name=/=/p;s/^# $name is not set\$/unset/p" "$KCFG" | tr '\n' ' ')
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

    # pkgrel, then the checksums -- in that order, because the checksum run
    # reads the APKBUILD.
    KREL=$(sed -n 's/^pkgrel=\([0-9]*\)$/\1/p' "$KDIR/APKBUILD" | tail -1)
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
# The artifacts, not the directory: $OUT is where this run's *log* lives (the
# caller redirected into it before this script started), so `rm -rf "$OUT"`
# would unlink the log mid-run -- the shell keeps writing to a file nobody
# can open, and the failure that follows looks like "the build never started".
mkdir -p "$OUT"
rm -f "$OUT/disk.wic.xz" "$OUT/disk.bmap" "$OUT/result"
add=""
[ -n "$PMO_PACKAGES" ] && add="--add $PMO_PACKAGES"
# shellcheck disable=SC2086
"$PMB" -c "$CFG" -y -E "$PMO_EXTRA_SPACE" --details-to-stdout install \
    --no-split --no-firewall --password "$PMO_PASSWORD" $add \
    || {
        # pmbootstrap's real error is in its own log, and the traceback it
        # prints to stdout names the failed command without its output. Print
        # the tail here so a detached build's log holds the reason rather than
        # a path on another machine.
        warn "pmbootstrap install failed; the last 40 lines of its own log:"
        tail -40 "$WORK/log.txt" 2>/dev/null | sed 's/^/    | /'
        die "pmbootstrap install failed (its log: $WORK/log.txt on this host)"
    }

# Unmount and detach before touching the image ourselves: the loop device the
# install used is still attached to it, and mounting the same file twice is how
# you get two writers to one filesystem.
step "Shutting down the chroot"
"$PMB" -c "$CFG" -y shutdown >/dev/null 2>&1 || warn "pmbootstrap shutdown reported a problem; continuing"

img=$(find "$WORK/chroot_native/home/pmos/rootfs" -maxdepth 1 -name "$PMO_DEVICE.img" 2>/dev/null | head -1)
[ -n "$img" ] || img=$(find "$WORK" -maxdepth 6 -name "$PMO_DEVICE.img" 2>/dev/null | head -1)
[ -n "$img" ] || die "pmbootstrap reported success but no $PMO_DEVICE.img is under $WORK"
info "image: $img ($(du -h "$img" | cut -f1))"

# --- the bootloader, which pmbootstrap has already written --------------------
#
# Nothing to do here: pmbootstrap embeds each device's firmware into the
# image at the offset the device declares -- its own log says so ("Embed
# firmware u-boot/... at offset 8 with step size 1024"), in units of 1024
# bytes, not sectors.
#
# What is left is the check, which is cheap: read back the byte the boot ROM
# will read and refuse to ship an image whose firmware is not there.
step "Checking the bootloader pmbootstrap embedded"
DEVICEINFO=$(find "$APORTS/device" -name deviceinfo -path "*device-$PMO_DEVICE/*" | head -1)
[ -n "$DEVICEINFO" ] || die "no deviceinfo for $PMO_DEVICE under $APORTS/device"
embed=$(sed -n 's/^deviceinfo_sd_embed_firmware="\(.*\)"$/\1/p' "$DEVICEINFO")
fw_step=$(sed -n 's/^deviceinfo_sd_embed_firmware_step_size="\{0,1\}\([0-9]*\).*/\1/p' "$DEVICEINFO")
case "$fw_step" in ''|*[!0-9]*) fw_step=1024 ;; esac

if [ -z "$embed" ]; then
    # Some devices are flashed over fastboot or uuu instead, and for those an
    # image with no embedded firmware is correct. Said out loud, so that "the
    # card does not boot" is never a silent possibility.
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

# --- seeding the image -------------------------------------------------------
#
# Two things go in that pmbootstrap cannot put there, both about the phone
# being *reachable* the first time it boots -- the difference between a card
# provisioned over the wire and one that needs a person holding the phone:
# the WiFi credential (the uplink *is* WiFi, so an image without it comes up
# in isolation) and avahi enabled (until `wk bridge setup` runs `tailscale
# up` there is no tailnet name or knowable DHCP address, so <hostname>.local
# is the whole of first contact).
#
# The credential is read from this host's own connection, so the PSK never
# travels through a log, command line, or agent context -- the same rule
# `wifi-from-machine` follows for the Pis (image/profiles.sh). The bssid is
# deliberately not copied: this house has two APs on one SSID, and a pinned
# bssid turns a roam into an outage. One mount for both, here rather than on
# the workstation: this machine already needs root to build.
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
            # The hardware MAC, not a random one. NetworkManager randomises by
            # default, which is right for a phone roaming between networks it
            # does not own and wrong for infrastructure that lives on one
            # network for months: a stable MAC is what lets this hold a DHCP
            # reservation, appear as the same device in a router client list,
            # and be recognised by anything that filters or approves devices.
            # A randomised address arrives as a new unknown device on every
            # association: the phone comes up on a locally-administered
            # address and is reachable from nothing on the LAN despite holding
            # a lease.
            # (No apostrophes in here: this block is inside a single-quoted
            # shell string, and one apostrophe ends it.)
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
# Partition 2 is the root on both phones' layouts (boot is 1). Checked below
# rather than assumed: a wrong guess would silently seed the boot partition and
# the phone would come up with no uplink and no way in.
sudo -n mount "${loop}p2" "$OUT/mnt" || die "could not mount ${loop}p2"
[ -d "$OUT/mnt/etc/NetworkManager" ] \
    || die "${loop}p2 has no /etc/NetworkManager -- that is not the rootfs"

# root:root 0600, or NetworkManager ignores the file outright.
sudo -n install -o 0 -g 0 -m 0600 "$keyfile" \
    "$OUT/mnt/etc/NetworkManager/system-connections/wk-uplink.nmconnection"
info "uplink: $ssid (the PSK stayed on this host)"

# The same ssh key for root, not as a convenience: pmbootstrap's
# `ssh_keys`/`ssh_key_glob` install the key for the console user only, who
# cannot become root without a password -- doas prompts, and `wk bridge
# setup` is non-interactive over ssh, so the provisioner would die on its
# first privileged write on a phone that is otherwise reachable and healthy.
# Root by key rather than a passwordless doas rule: they are the same
# authority and only one says so out loud; sshd's `prohibit-password` here
# permits exactly the holder of this key, and bridge/provision.sh turns
# password auth off again explicitly.
#
# `sudo -n test`, not a bare test: pmbootstrap's account .ssh is mode 700
# owned by the image's own uid, so a plain `[ -f ... ]` comes back false on
# a file that is plainly there.
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

# The two settings a fresh phone must already have, or it is not reliably
# reachable and cannot be provisioned. Both are in bridge/provision.sh too,
# but the role applies them *over ssh* -- a phone that needs them to answer
# ssh can never receive them there.
#
#   power save    the RTL8723CS powers its RF side down when idle and misses
#                 frames aimed at it, though it can still *initiate* -- known-
#                 broken upstream, with distro patches whose only job is
#                 disabling it.
#   never sleep   an unprovisioned phone idles off the network in a few
#                 minutes, taking the uplink with it.
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

# A service is enabled by linking it into a runlevel -- that is all
# `rc-update add` does -- and installing avahi does not start it.
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

# --- the artifacts -----------------------------------------------------------
#
# A compressed image, because this comes back over WiFi and a phone image is
# mostly empty space: the difference between sending a few hundred megabytes and
# sending all of it. A *wire* format and nothing more -- the driving end
# decompresses it and deletes it.
step "Compression"
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
