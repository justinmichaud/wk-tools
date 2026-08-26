# Named system profiles -- the spec that `wk sysimage build` executes.
#
# A configuration is named for what it is -- project, release, builder, board,
# width (docs/HANDOFF-vocabulary.md, "Systems, named by what they are for") --
# and lives in image/configs as a file. The phones and the fetched service
# image are the exceptions, being one of a kind rather than points in a matrix.
#
# The rule this file exists to keep (docs/HANDOFF-boot.md, "One-command
# reproducible, everywhere"): the spec lives in the repo, not on a machine.
# Package lists, the image's config.txt, the units, the network shape -- all
# under version control, applied by a verb. A machine is then disposable in the
# same sense a workspace is.
#
# A profile sets:
#
#   IMG_BUILDER      which mechanism builds it, and there are four:
#                      yocto     -- bitbake a whole distribution from source in
#                        a workspace (hours, tens of gigabytes; image/yocto.sh)
#                      buildroot -- the WPE fork's release-pinned cog
#                        defconfigs, in a workspace (image/buildroot.sh)
#                      pmos      -- postmarketOS for a phone, by pmbootstrap on
#                        a Linux aarch64 machine over ssh (image/pmos.sh)
#                      fetch     -- downloaded and pinned by content rather than
#                        built; the only builder whose output is an input
#                        (image/fetch.sh)
#                    They share the flashing path and nothing else. A field one
#                    builder reads is not set by a profile for another -- it
#                    would be describing a step that never runs.
#   IMG_MACHINE      the fleet machine it is built for (boot/machines.sh)
#   IMG_ARCH         the image's architecture
#   IMG_HOSTNAME     what it calls itself once booted
#   IMG_WATCHDOG     seconds before the self-return reboot, unless kept. Every
#                    profile wants one -- it is what hands the machine back when
#                    a run wedges it. On a card written as a rescue the unit is
#                    installed and stays inert, so this value is what a bench
#                    system does and a rescue simply never reaches it.
#   IMG_SPEC_DIR     the profile's own files
#
# There is deliberately no role field. A rescue system and the bench system
# beside it are the same distribution, built once from the same profile; the
# only difference is a marker on the card, written by `wk sysimage write
# --rescue` and read by the units that hand a machine back
# (`ConditionPathExists=!/etc/wk/rescue`). A profile field would be a second
# way to say one thing, and the expensive kind: it would mean two bitbakes and
# two multi-gigabyte artifacts of identical bytes.
#
# Which system is *running* is a third question again, and answered by evidence
# rather than by either of them (b_system_kind, boot/machines.sh): the medium
# the running root is on.
#
# A yocto profile sets these instead (image/yocto.sh reads them):
#
#   YOC_BRANCH       the WebKit branch whose Tools/yocto config is the spec.
#                    This is the whole version pin: that branch's manifest.xml
#                    names every layer by commit, so "which Yocto, which
#                    meta-webkit, which kernel" is answered by the release
#                    branch rather than by anything written here.
#   YOC_TARGET       the cross-target section in Tools/yocto/targets.conf
#   YOC_IMAGE        the bitbake image recipe (targets.conf's image_basename)
#   YOC_RM_WORK      1 to inherit rm_work -- see image/yocto.sh for the
#                    disk-space argument, which is the reason it defaults on
#   YOC_CHROMIUM     1 to leave Chromium in the image, 0 to drop it. The
#                    branch's own local.conf adds it ("to be able to compare
#                    WPE/Chromium performance"), and it is about half the build
#                    -- see the profile for the numbers.
#
# Identity is stamped per disk at write time rather than carried by the image,
# and that is not cosmetic. Two cards written from one image are twins in every
# namespace -- same MBR signature, same filesystem labels -- so `root=LABEL=` or
# a `PARTUUID=` from the image resolves to whichever disk the firmware
# enumerated first. `disk_unique_identity` (boot/disk.sh) gives each copy its
# own and rewrites the cmdline and fstab to match.

# Where a WebKit-runtime configuration lives: one file per configuration, in
# this repository, named for what it is -- project, release, builder, board,
# width.
#
# Data rather than `case` arms in image_profile_load, which is the shape
# CLAUDE.md says is being replaced: a case statement naming a device. Six arms
# cover one release on four board/width combinations, three of the four sharing
# a single arm with a nested `case` to pull them apart -- so adding a release
# meant editing code, and the two facts that actually differ per configuration
# (which defconfig, which targets.conf section) were buried among the ones that
# do not.
#
# Named for the *project* -- WebKit or WPEWebKit -- and never "upstream" or
# "downstream". Those two words are actively wrong here: a configuration setting
# YOC_BRANCH=webkitglib/2.48 builds from a branch in WebKit/WebKit, so a
# `downstream-` prefix on it describes
# from upstream. A configuration named for the repository it comes from cannot
# make that mistake.
image_config_dir()  { echo "$WK_ROOT/image/configs"; }
image_config_file() { echo "$(image_config_dir)/$1.conf"; }

# Every configuration there is, from the files themselves rather than a list
# kept beside them.
image_config_names() {
    local f
    for f in "$(image_config_dir)"/*.conf; do
        [ -f "$f" ] || continue
        basename "$f" .conf
    done
}

# One line each: the blurb is the config file's third line, which is where the
# generator and every hand edit put it.
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

    # Reset every field a profile may set, so a second load in one process
    # cannot inherit the first profile's answers. `wk boot --list` loads one
    # profile per machine in a loop, and a yocto profile that silently kept the
    # previous profile's YOC_TARGET would describe an image that does not
    # exist.
    # No default: a profile that names no builder is a bug, and cmd_build
    # refuses an empty one by name.
    IMG_BUILDER=""
    IMG_MACHINE=""; IMG_ARCH=""; IMG_HOSTNAME=""; IMG_WATCHDOG=""
    YOC_BRANCH=""; YOC_TARGET=""; YOC_IMAGE=""; YOC_RM_WORK=""
    YOC_CHROMIUM=1; YOC_REMOTE=origin; YOC_LOCAL_LAYER=1
    CFG_PROJECT=""; CFG_RELEASE=""; CFG_BRANCH=""; CFG_REMOTE=""; CFG_NEEDS=""
    BR_TREE_URL=""; BR_TREE_BRANCH=""; BR_TREE_COMMIT=""; BR_DEFCONFIG=""
    BR_OVERLAY_TAILSCALE=""; BR_EXTERNAL=""; BR_IMAGE=""
    FET_URL=""; FET_SHA256=""; FET_XZ=""; FET_NOTE=""; FET_DEVICE=""
    PMO_DEVICE=""; PMO_UI=""; PMO_CHANNEL=""; PMO_PMB_VERSION=""
    PMO_USER=""; PMO_PASSWORD=""; PMO_PACKAGES=""; PMO_EXTRA_SPACE=""
    PMO_BRIDGE=""; PMO_BUILD_HOST=""; PMO_WIFI_BANDS=""
    PMO_KERNEL_APORT=""; PMO_KCONFIG=""

    # A WebKit-runtime configuration is a file, and it answers first: what is
    # true of one is data, not a branch. Everything else -- the two phones and
    # the fetched service image -- is still a case arm below, because each of
    # those is one of a kind rather than a point in a matrix.
    local _cfg
    _cfg=$(image_config_file "$1")
    if [ -f "$_cfg" ]; then
        # shellcheck disable=SC1090
        . "$_cfg"
        return 0
    fi

    case "$1" in
    # Tombstones: one spelling per configuration, so a name that is not it
    # points at the one that is rather than quietly meaning something.
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
    # Tombstones for the `downstream-` spellings. There is no wpe-2.48 in
    # WPEWebKit at all (its
    # releases run 2.36, 2.38, 2.42, 2.46, 2.50), which is why the old comment
    # had to explain that "webkitglib/2.48 is the release branch for both GLib
    # ports". The set is now three releases named for their project.
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

    'wk sysimage build --list' has all twenty."
        ;;
    # Tombstone: a name the tooling refuses so it points somewhere instead of
    # failing as "unknown profile".
    perf-linux-rpi3|perf-linux-rpi4|perf-linux-rpi5)
        die "there is no '$1'. A perf system is built by yocto or buildroot, or
    it is macOS (wk help images). For this board:

        perf-linux-rpi3  -> webkit-2.52-yocto-rpi3-32   (32-bit, its native width)
        perf-linux-rpi4  -> webkit-2.52-yocto-rpi4-64
        perf-linux-rpi5  -> webkit-2.52-yocto-rpi5-64"
        ;;

    # --- fetch --------------------------------------------------------------
    #
    # Not built here, and that is the point: Jumpdrive is the PinePhone
    # community's service image, and the reason it is in the store at all is
    # that it turns the phone's internal storage into a disk this repo's
    # ordinary write path can reach. Pinned by release *and* by content.
    recovery-pinephone)
        IMG_BUILDER=fetch
        IMG_ARCH=aarch64
        IMG_MACHINE=""
        FET_DEVICE=pine64-pinephone
        FET_URL=https://github.com/dreemurrs-embedded/Jumpdrive/releases/download/0.8/pine64-pinephone.img.xz
        # Recorded from the artifact this repo fetched. Upstream publishes no
        # checksum file, so the pin is "what we verified" rather than "what they
        # said" -- which is still the property that matters: it cannot change
        # underneath us without this failing.
        FET_SHA256=a8c9e0252e070e1737c14ca7c8cac515d3196148ded1af98c2bf8e9350d970be
        FET_XZ=1
        FET_NOTE="Jumpdrive 0.8: boots from a card and exports the phone's eMMC over USB"
        ;;

    # --- pmos ---------------------------------------------------------------
    #
    # A phone, and therefore not a machine. `IMG_MACHINE` stays empty on
    # purpose: the fleet is "machines wk can boot" (boot/machines.sh), `wk boot`
    # cannot boot a phone, and adding one to that table to satisfy a field would
    # be a claim this repo cannot honour. A pmos profile names a *device* -- the
    # postmarketOS codename -- and the card it lands on is written by whichever
    # machine holds the card reader.
    #
    # Both profiles are the same system with two device names and two bridges,
    # which is the whole point of moving the bridges to pmOS: one provisioner
    # (bridge/provision.sh) and now one builder for both phones.
    bridge-pinephone|bridge-librem5)
        IMG_BUILDER=pmos
        IMG_ARCH=aarch64
        # No machine: see above. Empty rather than absent so `wk sysimage write`
        # can tell "this image is not for the machine holding the card" from
        # "this image does not say", and skip the firmware check that only makes
        # sense for a board that is going to boot the disk in place.
        IMG_MACHINE=""

        # phosh, and the GUI is deliberate. A bridge keeps its screen: if WiFi
        # and ssh are both gone, the phone's own display and touchscreen are the
        # last way in, and that is worth more than the memory a headless image
        # would save. bridge/provision.sh says the same thing from the other end.
        PMO_UI=phosh

        # A released channel, not edge. `v25.12` is what pmaports' own
        # channels.cfg calls "Latest release / Recommended for best stability"
        # (Alpine 3.23), and stability is the entire requirement for a device
        # whose job is to still be answering in six months.
        PMO_CHANNEL=v25.12

        # Pinned, because pmbootstrap is the one moving part here: it prompts
        # differently, stores its config differently and expects a different
        # work-folder version from release to release, and image/pmos-build.sh
        # is written against exactly this one. Every PyPI release of it is
        # yanked, so this is a git tag.
        PMO_PMB_VERSION=3.9.0

        # The console account on the phone's own screen. The password is a
        # known one and that is not an oversight: it is the recovery path,
        # bridge/provision.sh turns off password authentication over the
        # network, and pmbootstrap handles this value in plain text by design
        # (it says so itself) -- so a secret here would be a secret in a log.
        PMO_USER=user
        PMO_PASSWORD=147147

        # The bridge role's own package list, baked in. Provisioning a phone
        # then needs no egress and no apk index that has moved on: the packages
        # are already there and `wk bridge setup` reports "all packages present
        # -- apk not contacted". Every name here was resolved against this
        # channel by an actual install, not from memory.
        # avahi is in there for one specific moment: the first boot. The phone
        # has no tailnet identity until `wk bridge setup` runs `tailscale up`,
        # so the name it is reached by *before* that has to come from somewhere
        # else, and mDNS is the only thing that works with no configuration on
        # either end. Enabled in the image by image/pmos-build.sh, because
        # installing a package does not enable its OpenRC service.
        # nmap is here for the one job a bridge has that nothing else can do:
        # its segment is a cable behind a phone, so this is the only machine in
        # the fleet that can see the boards on it. `wk find` sweeps from
        # whichever machine is on the segment, with nmap and nothing else --
        # there is deliberately no second sweeper -- so a bridge without it
        # makes every board behind it unfindable, which reads exactly like
        # every board behind it being dead.
        PMO_PACKAGES="openssh,nftables,dnsmasq,chrony,tailscale,jq,iw,ethtool,logrotate,zram-init,v4l-utils,networkmanager,avahi,nmap"
        PMO_EXTRA_SPACE=512

        # The build host, which has to be a machine on the WiFi the phone will
        # use: the uplink credential is copied from its own connection, on it,
        # so the PSK never travels. A phone has no cable, so this is not
        # optional -- an image without a credential comes up in isolation and
        # `wk bridge setup` has nothing to talk to.
        PMO_BUILD_HOST=rpi5

        # ...and the two lines that differ. Nested rather than two branches of
        # the outer case: everything above is shared, and a second branch would
        # be a second copy of it to keep in step.
        case "$1" in
            bridge-pinephone)
                PMO_DEVICE=pine64-pinephone
                PMO_BRIDGE=tailnet-bridge-generic
                IMG_HOSTNAME=tailnet-bridge-generic

                # One kernel option, and the only one this repo patches into
                # anybody's aport -- so it needs to earn it.
                #
                # A bridge's own documentation says "the hardware watchdog
                # recovers hangs", and on a PinePhone there was none: pmOS
                # ships `linux-postmarketos-allwinner` with
                # CONFIG_SUNXI_WATCHDOG unset, while the A64's device tree
                # declares the watchdog at 0x1c20ca0 (`allwinner,sun50i-a64-wdt`).
                # So the hardware is present, the DT node is present, and the
                # driver that would bind them is simply not built. Nothing in
                # userspace substitutes: this is
                # the only thing that recovers a kernel that has stopped
                # scheduling, on the one device here that cannot be walked up to.
                #
                # `=m` rather than `=y` deliberately. The kernel builds modules
                # already (431 of them), a module is loaded by the role rather
                # than by the image, and a driver that turns out to misbehave is
                # then a line in a modules file rather than a reflash.
                #
                # The Librem 5 gets no such entry: its watchdog situation is
                # unmeasured, and guessing at a second SoC's kconfig from this
                # one's is how a working image stops booting.
                PMO_KERNEL_APORT=device/community/linux-postmarketos-allwinner
                PMO_KCONFIG="CONFIG_SUNXI_WATCHDOG=m"
                # 2.4 GHz only, and this is load-bearing rather than trivia.
                # The PinePhone's radio is an RTL8723CS: 802.11 b/g/n, single
                # band, no 5 GHz at all. The uplink credential is copied from
                # the build host's own association, and a build host on a 5 GHz
                # SSID therefore produces an image that is *guaranteed* to boot
                # into isolation -- it has a perfectly good PSK for a network
                # the phone's hardware cannot see. A board associated on channel
                # 52 hands over a 5 GHz network, and the phone comes up showing
                # every neighbour's 2.4 GHz network and not the one it was built
                # for. pmos_check_uplink_band refuses that.
                PMO_WIFI_BANDS="2.4"
                ;;
            bridge-librem5)
                PMO_DEVICE=purism-librem5
                PMO_BRIDGE=tailnet-bridge-moose-bmc
                IMG_HOSTNAME=tailnet-bridge-moose-bmc
                # Dual band: the RS9116 is 802.11abgn, so either band works and
                # the check below has nothing to refuse. Stated rather than left
                # empty, because "unknown" and "both" are different answers and
                # only one of them should let a build through unquestioned.
                PMO_WIFI_BANDS="2.4 5"
                # The camera stream is this bridge's job, so its pipeline is in
                # the image rather than fetched on first provision.
                PMO_PACKAGES="$PMO_PACKAGES,ffmpeg"
                ;;
        esac
        ;;
    # --- yocto and buildroot ---------------------------------------------
    #
    # Not here. Every WebKit-runtime configuration -- both builders, three
    # releases, four board/width combinations -- is a file in image/configs,
    # sourced at the top of this function. As case arms they would disagree
    # about the branch, the targets.conf section and the hostname and nothing
    # else. See image_config_dir above for why that is data.

    *)  return 1 ;;
    esac
}
