# Named system profiles -- the spec `wk sysimage build` executes, fields in README.md. Rescue and bench are one distribution, told apart by a card marker.

image_config_dir()  { echo "$WK_ROOT/image/configs"; }
image_config_file() { echo "$(image_config_dir)/$1.conf"; }

image_config_names() {
    local f
    for f in "$(image_config_dir)"/*.conf; do
        [ -f "$f" ] || continue
        basename "$f" .conf
    done
}

image_config_list() {
    local n f blurb needs
    for n in $(image_config_names); do
        f=$(image_config_file "$n")
        # From the header line, not a line number: a positional read is emptied by any comment edit above it.
        blurb=$(sed -n '1s/^# [^ ]* -- //p' "$f")
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

    IMG_BUILDER=""  # `wk boot --list` loops over profiles: a field left set would describe a nonexistent image
    IMG_MACHINE=""; IMG_ARCH=""; IMG_HOSTNAME=""
    # Seconds before a wedged system hands the board back by rebooting out of
    # bench mode; inert until the card is written --rescue. Override to differ.
    IMG_WATCHDOG=300
    YOC_BRANCH=""; YOC_TARGET=""; YOC_IMAGE=""; YOC_RM_WORK=""
    YOC_CHROMIUM=1; YOC_REMOTE=origin; YOC_LOCAL_LAYER=1
    YOC_PORT_TARGET_FROM=""; YOC_MACHINE=""
    YOC_MULTILIB=""; YOC_MULTILIB_TUNE=""
    CFG_PROJECT=""; CFG_RELEASE=""; CFG_BRANCH=""; CFG_REMOTE=""; CFG_NEEDS=""
    BR_TREE_URL=""; BR_TREE_BRANCH=""; BR_TREE_COMMIT=""; BR_DEFCONFIG=""
    BR_OVERLAY_TAILSCALE=""; BR_EXTERNAL=""; BR_IMAGE=""
    BR_KERNEL_DEB_URL=""; BR_KERNEL_DEB_SHA256=""; BR_KERNEL_RELEASE=""  # a kernel declared, not built (image/buildroot/kernel-pin.sh)
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
    perf-linux-rpi3|perf-linux-rpi4|perf-linux-rpi5)
        die "there is no '$1'. A perf system is built by yocto or buildroot, or
    it is macOS (wk help). For this board:

        perf-linux-rpi3  -> webkit-2.52-yocto-rpi3-32   (32-bit, its native width)
        perf-linux-rpi4  -> webkit-2.52-yocto-rpi4-64
        perf-linux-rpi5  -> webkit-2.52-yocto-rpi5-64"
        ;;

    recovery-pinephone)
        IMG_BUILDER=fetch
        IMG_ARCH=aarch64
        IMG_MACHINE=""
        FET_DEVICE=pine64-pinephone
        FET_URL=https://github.com/dreemurrs-embedded/Jumpdrive/releases/download/0.8/pine64-pinephone.img.xz
        FET_SHA256=a8c9e0252e070e1737c14ca7c8cac515d3196148ded1af98c2bf8e9350d970be  # upstream publishes none; this is what we verified
        FET_XZ=1
        FET_NOTE="Jumpdrive 0.8: boots from a card and exports the phone's eMMC over USB"
        ;;

    bridge-pinephone|bridge-librem5)  # both phones, one provisioner (bridge/provision.sh)
        IMG_BUILDER=pmos
        IMG_ARCH=aarch64
        IMG_MACHINE=""  # empty, not absent: tells "not for this machine" from "does not say"

        PMO_UI=phosh  # the last way in if WiFi and ssh both fail

        PMO_CHANNEL=v25.12  # pmaports' "Recommended for best stability" channel

        PMO_PMB_VERSION=3.9.0  # a git tag: every PyPI release of pmbootstrap is yanked, and prompts differ by release

        PMO_USER=user
        PMO_PASSWORD=147147  # the known password is the recovery path (bridge/provision.sh turns off network auth)

        PMO_PACKAGES="openssh,nftables,dnsmasq,chrony,tailscale,jq,iw,ethtool,logrotate,zram-init,v4l-utils,networkmanager,avahi,nmap"  # avahi reaches the phone before `tailscale up`; nmap because no other machine is on this segment
        PMO_EXTRA_SPACE=512

        PMO_BUILD_HOST=rpi5  # its own WiFi credential is the uplink one
        case "$1" in
            bridge-pinephone)
                PMO_DEVICE=pine64-pinephone
                PMO_BRIDGE=tailnet-bridge-generic
                IMG_HOSTNAME=tailnet-bridge-generic

                # pmOS leaves CONFIG_SUNXI_WATCHDOG unset though the A64 DT declares
                # the watchdog (0x1c20ca0); `=m`, so a bad driver is not a reflash.
                PMO_KERNEL_APORT=device/community/linux-postmarketos-allwinner
                PMO_KCONFIG="CONFIG_SUNXI_WATCHDOG=m"
                PMO_WIFI_BANDS="2.4"  # the RTL8723CS is single-band, so a 5 GHz PSK would be unusable
                ;;
            bridge-librem5)
                PMO_DEVICE=purism-librem5
                PMO_BRIDGE=tailnet-bridge-moose-bmc
                IMG_HOSTNAME=tailnet-bridge-moose-bmc
                PMO_WIFI_BANDS="2.4 5"  # RS9116, 802.11abgn
                PMO_PACKAGES="$PMO_PACKAGES,ffmpeg"  # the camera stream is this bridge's job
                ;;
        esac
        ;;
    *)  return 1 ;;
    esac
}
