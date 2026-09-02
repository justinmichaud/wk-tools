# Named system profiles -- the spec that `wk sysimage build` executes.
# A configuration lives in image/configs as a file (README.md has the naming
# scheme). The phones and the fetched service image are exceptions, being
# one of a kind rather than points in a matrix.
#
# A profile sets:
#   IMG_BUILDER      which mechanism builds it: yocto (bitbake from source,
#                    image/yocto.sh), buildroot (WPE fork's cog defconfigs,
#                    image/buildroot.sh), pmos (postmarketOS via pmbootstrap,
#                    image/pmos.sh), or fetch (downloaded, pinned by content,
#                    image/fetch.sh)
#   IMG_MACHINE      the fleet machine it is built for (boot/machines.sh)
#   IMG_ARCH         the image's architecture
#   IMG_HOSTNAME     what it calls itself once booted
#   IMG_WATCHDOG     seconds before the self-return reboot after a wedged
#                    run; inert until `wk sysimage write --rescue` marks the card
#   IMG_SPEC_DIR     the profile's own files
# No role field: rescue and bench are the same distribution, distinguished
# only by a card marker (`wk sysimage write --rescue`; b_system_kind).
#
# A yocto profile sets these instead (image/yocto.sh reads them):
#   YOC_BRANCH       the WebKit branch whose Tools/yocto config is the spec
#   YOC_TARGET       the cross-target section in Tools/yocto/targets.conf
#   YOC_IMAGE        the bitbake image recipe (targets.conf's image_basename)
#   YOC_RM_WORK      1 to inherit rm_work (disk space; image/yocto.sh)
#   YOC_CHROMIUM     1 to leave Chromium in the image, 0 to drop it
#   YOC_PORT_TARGET_FROM  a target this branch does have, to derive YOC_TARGET
#                    from when the branch has no such section (port-target.py)
#   YOC_MACHINE      the yocto MACHINE that derived target selects
#   YOC_MULTILIB     a multilib variant to build the userspace as ('lib32'),
#                    leaving the machine -- and so the kernel -- alone
#   YOC_MULTILIB_TUNE  the tune that variant builds at
# Identity is stamped per disk at write time: two cards from one image share
# an MBR signature, so `root=LABEL=` would resolve to whichever disk the
# firmware enumerated first. `disk_unique_identity` (boot/disk.sh) fixes it.

image_config_dir()  { echo "$WK_ROOT/image/configs"; }
image_config_file() { echo "$(image_config_dir)/$1.conf"; }

image_config_names() {
    local f
    for f in "$(image_config_dir)"/*.conf; do
        [ -f "$f" ] || continue
        basename "$f" .conf
    done
}

# One line each: the blurb is the config file's third line.
image_config_list() {
    local n f blurb needs
    for n in $(image_config_names); do
        f=$(image_config_file "$n")
        blurb=$(sed -n '3s/^# //p' "$f")
        needs=$(grep -c '^CFG_NEEDS=' "$f" 2>/dev/null || true)
        printf '%s\n' "$n"
        printf '            %s\n' "$blurb"
        [ "${needs:-0}" -eq 0 ] || printf '            %s\n' \
            "-- not buildable yet; 'wk sysimage build $n' says what it needs"
    done
}

image_profile_list() {
    image_config_list
    cat <<'EOF'
bridge-pinephone
            postmarketOS for the PinePhone, as tailnet-bridge-generic: pmbootstrap
            on a Linux aarch64 machine, ssh key and WiFi baked in, the bridge
            role's packages preinstalled. Goes on a microSD, which both phones
            boot in preference to their own storage
bridge-librem5
            the same for the Librem 5, as tailnet-bridge-moose-bmc. Its u-boot is
            embedded in the card by pmbootstrap (deviceinfo_sd_embed_firmware),
            so the card boots without touching the eMMC install

recovery-pinephone
            Jumpdrive: the PinePhone's service image, downloaded and pinned
            rather than built. Boots from a card and exports the phone's
            internal storage over USB, which is how its eMMC gets written --
            and, being somebody else's known-good image, is what settles
            whether a card that does not boot is a bad image or a phone that
            will not boot cards
EOF
}

image_profile_load() {
    IMG_PROFILE="$1"
    IMG_SPEC_DIR="$WK_ROOT/image/$1"

    # Reset every field: `wk boot --list` loops over profiles, and a stale
    # field would describe a nonexistent image.
    IMG_BUILDER=""
    IMG_MACHINE=""; IMG_ARCH=""; IMG_HOSTNAME=""; IMG_WATCHDOG=""
    YOC_BRANCH=""; YOC_TARGET=""; YOC_IMAGE=""; YOC_RM_WORK=""
    YOC_CHROMIUM=1; YOC_REMOTE=origin; YOC_LOCAL_LAYER=1
    YOC_PORT_TARGET_FROM=""; YOC_MACHINE=""
    YOC_MULTILIB=""; YOC_MULTILIB_TUNE=""
    CFG_PROJECT=""; CFG_RELEASE=""; CFG_BRANCH=""; CFG_REMOTE=""; CFG_NEEDS=""
    BR_TREE_URL=""; BR_TREE_BRANCH=""; BR_TREE_COMMIT=""; BR_DEFCONFIG=""
    BR_OVERLAY_TAILSCALE=""; BR_EXTERNAL=""; BR_IMAGE=""
    # A kernel this profile declares instead of building: the package, its
    # hash and the release inside it (image/buildroot/kernel-pin.sh).
    BR_KERNEL_DEB_URL=""; BR_KERNEL_DEB_SHA256=""; BR_KERNEL_RELEASE=""
    FET_URL=""; FET_SHA256=""; FET_XZ=""; FET_NOTE=""; FET_DEVICE=""
    PMO_DEVICE=""; PMO_UI=""; PMO_CHANNEL=""; PMO_PMB_VERSION=""
    PMO_USER=""; PMO_PASSWORD=""; PMO_PACKAGES=""; PMO_EXTRA_SPACE=""
    PMO_BRIDGE=""; PMO_BUILD_HOST=""; PMO_WIFI_BANDS=""
    PMO_KERNEL_APORT=""; PMO_KCONFIG=""

    local _cfg
    _cfg=$(image_config_file "$1")
    if [ -f "$_cfg" ]; then
        # shellcheck disable=SC1090
        . "$_cfg"
        return 0
    fi

    case "$1" in
    # Tombstones: point at the current spelling rather than meaning something quietly.
    rpi5-perf|rpi4-perf|rpi3-perf|rpi4-wpe-2.48|rpi4-wpe-2.48-32|rpi3-wpe-2.48-32|rpi3-wpe-2.48-64|rpi5-wpe-2.48|mac-bench)
        die "there is no profile '$1'. Use:
    rpi5-perf        -> webkit-2.52-yocto-rpi5-64
    rpi4-perf        -> webkit-2.52-yocto-rpi4-64
    rpi3-perf        -> webkit-2.52-yocto-rpi3-32
    rpi4-wpe-2.48    -> webkit-2.52-yocto-rpi4-64
    rpi4-wpe-2.48-32 -> webkit-2.52-yocto-rpi4-32
    rpi3-wpe-2.48-32 -> webkit-2.52-yocto-rpi3-32
    rpi3-wpe-2.48-64 -> rpi3 is 32-bit here; use webkit-2.52-yocto-rpi3-32
    rpi5-wpe-2.48    -> webkit-2.52-yocto-rpi5-64
    mac-bench        -> perf-macos-tolken"
        ;;
    # Tombstones for the `downstream-` spellings: WPEWebKit has no wpe-2.48
    # release (its releases run 2.36, 2.38, 2.42, 2.46, 2.50).
    downstream-wpe-2.46-rpi4|downstream-yocto-wpe-2.48-rpi4|downstream-yocto-wpe-2.48-rpi4-32|downstream-yocto-wpe-2.48-rpi3-32|downstream-yocto-wpe-2.48-rpi3-64|downstream-yocto-wpe-2.48-rpi5)
        die "there is no profile '$1'. Configurations are named for the project
    they are built from -- WebKit or WPEWebKit -- and webkitglib/2.48 is a
    branch in WebKit/WebKit.

    WPEWebKit releases here are 2.38 and 2.46; WebKit's is 2.52. So:

        downstream-wpe-2.46-rpi4          -> wpewebkit-2.46-yocto-rpi4-64
        downstream-yocto-wpe-2.48-rpi4    -> webkit-2.52-yocto-rpi4-64
        downstream-yocto-wpe-2.48-rpi4-32 -> webkit-2.52-yocto-rpi4-32
        downstream-yocto-wpe-2.48-rpi3-32 -> webkit-2.52-yocto-rpi3-32
        downstream-yocto-wpe-2.48-rpi3-64 -> the rpi3 is 32-bit here
        downstream-yocto-wpe-2.48-rpi5    -> webkit-2.52-yocto-rpi5-64

    'wk sysimage --list' has all of them."
        ;;
    # Tombstone: refused by name, rather than failing as "unknown profile".
    perf-linux-rpi3|perf-linux-rpi4|perf-linux-rpi5)
        die "there is no '$1'. A perf system is built by yocto or buildroot, or
    it is macOS (wk help). For this board:

        perf-linux-rpi3  -> webkit-2.52-yocto-rpi3-32   (32-bit, its native width)
        perf-linux-rpi4  -> webkit-2.52-yocto-rpi4-64
        perf-linux-rpi5  -> webkit-2.52-yocto-rpi5-64"
        ;;

    # --- fetch: Jumpdrive turns the phone's internal storage into a disk --
    recovery-pinephone)
        IMG_BUILDER=fetch
        IMG_ARCH=aarch64
        IMG_MACHINE=""
        FET_DEVICE=pine64-pinephone
        FET_URL=https://github.com/dreemurrs-embedded/Jumpdrive/releases/download/0.8/pine64-pinephone.img.xz
        # Upstream publishes no checksum; this is "what we verified".
        FET_SHA256=a8c9e0252e070e1737c14ca7c8cac515d3196148ded1af98c2bf8e9350d970be
        FET_XZ=1
        FET_NOTE="Jumpdrive 0.8: boots from a card and exports the phone's eMMC over USB"
        ;;

    # --- pmos: both phones share one provisioner (bridge/provision.sh) -----
    bridge-pinephone|bridge-librem5)
        IMG_BUILDER=pmos
        IMG_ARCH=aarch64
        # Empty, not absent: lets `wk sysimage write` tell "not for the
        # machine holding the card" from "does not say".
        IMG_MACHINE=""

        # phosh: the last way in if WiFi and ssh both fail.
        PMO_UI=phosh

        # v25.12 is pmaports' "Recommended for best stability" channel.
        PMO_CHANNEL=v25.12

        # A git tag, not a PyPI version: every PyPI release of pmbootstrap is
        # yanked, and prompts/config format differ by release.
        PMO_PMB_VERSION=3.9.0

        # The known password is the recovery path (bridge/provision.sh turns off network auth).
        PMO_USER=user
        PMO_PASSWORD=147147

        # avahi: reaches the phone before `tailscale up`. nmap: no other machine on this segment.
        PMO_PACKAGES="openssh,nftables,dnsmasq,chrony,tailscale,jq,iw,ethtool,logrotate,zram-init,v4l-utils,networkmanager,avahi,nmap"
        PMO_EXTRA_SPACE=512

        # The uplink credential is copied from this host's own WiFi.
        PMO_BUILD_HOST=rpi5
        case "$1" in
            bridge-pinephone)
                PMO_DEVICE=pine64-pinephone
                PMO_BRIDGE=tailnet-bridge-generic
                IMG_HOSTNAME=tailnet-bridge-generic

                # pmOS ships this kernel with CONFIG_SUNXI_WATCHDOG unset
                # despite the A64 DT declaring the watchdog (0x1c20ca0), the
                # only recovery for a hung kernel on a device nobody can walk
                # up to. `=m` so a bad driver is a modules-file line, not a
                # reflash; not needed for the Librem 5.
                PMO_KERNEL_APORT=device/community/linux-postmarketos-allwinner
                PMO_KCONFIG="CONFIG_SUNXI_WATCHDOG=m"
                # 2.4 GHz only: the RTL8723CS is single-band, else the copied PSK would be unusable.
                PMO_WIFI_BANDS="2.4"
                ;;
            bridge-librem5)
                PMO_DEVICE=purism-librem5
                PMO_BRIDGE=tailnet-bridge-moose-bmc
                IMG_HOSTNAME=tailnet-bridge-moose-bmc
                # Dual band (RS9116, 802.11abgn); stated since "unknown" and "both" differ.
                PMO_WIFI_BANDS="2.4 5"
                # The camera stream is this bridge's job.
                PMO_PACKAGES="$PMO_PACKAGES,ffmpeg"
                ;;
        esac
        ;;
    # --- yocto and buildroot: not here, they're files in image/configs -----

    *)  return 1 ;;
    esac
}
