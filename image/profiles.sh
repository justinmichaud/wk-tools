# Named system profiles -- the spec that `wk sysimage build` executes.
#
# The name carries the category (docs/HANDOFF-vocabulary.md, "Systems, named
# by what they are for"): perf-<distro>-<device> for measurement-grade
# systems seeded from a general-purpose distribution, downstream-<builder>-
# <release>-<device>[-width] for the public embedded images. Adding a
# category later is adding a profile.
#
# The rule this file exists to keep (docs/HANDOFF-boot.md, "One-command
# reproducible, everywhere"): the spec lives in the repo, not on a machine.
# Package lists, the image's config.txt, the units, the network shape -- all
# under version control, applied by a verb. A machine is then disposable in the
# same sense a workspace is.
#
# A profile sets:
#
#   IMG_BUILDER      which mechanism builds it:
#                      distro -- download a pinned distro image and seed it
#                        (cloud-init, minutes, unprivileged, done on the host)
#                      yocto  -- bitbake a whole distribution from source in a
#                        workspace (hours, tens of gigabytes; image/yocto.sh)
#                    The two share only the store and the flashing path. Every
#                    field below marked (distro) is meaningless to the yocto
#                    builder and is not set by a yocto profile -- a profile that
#                    set them anyway would be describing a seeding step that
#                    never runs.
#   IMG_MACHINE      the fleet machine it is built for (boot/machines.sh)
#   IMG_ARCH         the image's architecture
#   IMG_BASE_URL     (distro) distro base, pinned
#   IMG_BASE_SHA256  (distro) ... and pinned by content, not by name
#   IMG_BASE_KIND    (distro) which seeding dialect the base speaks
#   IMG_HOSTNAME     what it calls itself once booted
#   IMG_WATCHDOG     seconds before the self-return reboot, unless kept. Every
#                    profile that can be *booted as a bench system* wants one --
#                    it is what hands the machine back when a run wedges it.
#   IMG_GROW         (distro) whether cloud-init may grow the root partition
#   IMG_PACKAGES     (distro) packages installed on first boot (needs egress)
#   IMG_NETWORK      (distro) how the image gets on the network:
#                      wifi-from-machine -- copy the credential off the target
#                        board itself, so the PSK never travels through a log
#                        or an agent's context. Requires the board to be up,
#                        which is the price of not handling the secret here.
#                      wired -- DHCP on eth0, no secret at all, so the image
#                        builds with the target powered off. This is why a
#                        cabled test device is easier to serve than the rpi5.
#   IMG_LABEL_ROOT   (distro) the image's own root filesystem label
#   IMG_LABEL_BOOT   (distro) ... and its boot filesystem label
#   IMG_SPEC_DIR     the profile's own files
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
# The two labels are not cosmetic and not optional. A distro image and an
# install made from the same distro image carry the *same* filesystem labels --
# `writable` and `system-boot` on Ubuntu -- and this image is written to a stick
# that stays plugged into a machine whose workstation install came from the same
# family. Booted with both attached, `root=LABEL=writable` in the image's
# cmdline is ambiguous, and which disk wins is a property of enumeration order.
# Worse, `/boot/firmware` mounted by label could put the image's kernel updates
# on the workstation's firmware partition -- the exact separation this whole
# design exists to preserve. So the image is relabelled at build time, and its
# cmdline and fstab are rewritten to match.
#
# Why a whole distro image rather than a rootfs built from a package list: the
# one thing that has to work on first boot is the network, and the last three
# attempts at this failed there and nowhere else. Ubuntu on this board, associating with this AP on
# channel 52, is the one configuration that is *known* to work -- so the image
# is that configuration, modified as little as possible. A slim
# built-from-scratch rootfs is the next increment if one is ever wanted;
# building it first would have meant debugging a new rootfs and a new radio
# setup at the same time.

image_profile_list() {
    cat <<'EOF'
perf-linux-rpi5
            Ubuntu 26.04 server, aarch64, for the rpi5's USB one-shot: no
            sandbox, perf_event_paranoid ours to set, WiFi baked in
perf-linux-rpi4
            the same, cabled: DHCP on eth0, no credential, so it builds with
            the board switched off
perf-linux-rpi3
            refused, and says why: Ubuntu ships no armhf raspi image, and a
            32-bit run wants a 32-bit system rather than an arm64 one

downstream-wpe-2.46-rpi4
            the known-good pairing: WPE 2.46 from the downstream
            WebPlatformForEmbedded/WPEWebKit repo. The image is the branch's
            own, unmodified; the build needs the pseudo bump this host's kernel
            requires (--no-local-layer to re-test that).
downstream-yocto-wpe-2.48-rpi4
            WPE WebKit 2.48's own Yocto image for the rpi4 (aarch64,
            scarthgap, weston): the runtime the 2.48 release branch pins,
            bitbaked from source in a workspace. Hours, not minutes. Needs
            image/yocto/meta-wk to build at all -- see the pseudo story in
            docs/HANDOFF-yocto.md.
downstream-yocto-wpe-2.48-rpi4-32
            the same distribution, 32-bit: a 32-bit kernel and userspace, which
            is what a 32-bit perf run has to measure -- not a 32-bit process on
            a 64-bit kernel's compat layer
downstream-yocto-wpe-2.48-rpi3-32
            and for the rpi3, whose native width this is
downstream-yocto-wpe-2.48-rpi3-64
            the rpi3 as aarch64. Marginal at 931 MB, and there so that "slower,
            or just out of memory" is answerable
downstream-yocto-wpe-2.48-rpi5
            the rpi5 running what ships, on its USB stick, leaving the NVMe
            workstation alone. Needs one section added to targets.conf upstream
            -- the local.conf and the MACHINE are already there

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
    # previous profile's IMG_BASE_URL would describe an image that does not
    # exist.
    IMG_BUILDER=distro
    IMG_MACHINE=""; IMG_ARCH=""; IMG_BASE_KIND=""; IMG_BASE_URL=""
    IMG_BASE_SHA256=""; IMG_HOSTNAME=""; IMG_WATCHDOG=""; IMG_GROW=""
    IMG_PACKAGES=""; IMG_NETWORK=""; IMG_LABEL_ROOT=""; IMG_LABEL_BOOT=""
    YOC_BRANCH=""; YOC_TARGET=""; YOC_IMAGE=""; YOC_RM_WORK=""
    YOC_CHROMIUM=1; YOC_REMOTE=origin; YOC_LOCAL_LAYER=1
    FET_URL=""; FET_SHA256=""; FET_XZ=""; FET_NOTE=""; FET_DEVICE=""
    PMO_DEVICE=""; PMO_UI=""; PMO_CHANNEL=""; PMO_PMB_VERSION=""
    PMO_USER=""; PMO_PASSWORD=""; PMO_PACKAGES=""; PMO_EXTRA_SPACE=""
    PMO_BRIDGE=""; PMO_BUILD_HOST=""; PMO_WIFI_BANDS=""
    PMO_KERNEL_APORT=""; PMO_KCONFIG=""

    case "$1" in
    # The old names, refused by name: every profile was renamed on 2026-08-20
    # to carry its category (docs/HANDOFF-vocabulary.md). One spelling, so the
    # old one points at the new one rather than quietly meaning it.
    rpi5-perf|rpi4-perf|rpi3-perf|rpi4-wpe-2.48|rpi4-wpe-2.48-32|rpi3-wpe-2.48-32|rpi3-wpe-2.48-64|rpi5-wpe-2.48|mac-bench)
        die "profile '$1' was renamed (docs/HANDOFF-vocabulary.md, 'Systems'):
    rpi5-perf        -> perf-linux-rpi5
    rpi4-perf        -> perf-linux-rpi4
    rpi3-perf        -> perf-linux-rpi3
    rpi4-wpe-2.48    -> downstream-yocto-wpe-2.48-rpi4
    rpi4-wpe-2.48-32 -> downstream-yocto-wpe-2.48-rpi4-32
    rpi3-wpe-2.48-32 -> downstream-yocto-wpe-2.48-rpi3-32
    rpi3-wpe-2.48-64 -> downstream-yocto-wpe-2.48-rpi3-64
    rpi5-wpe-2.48    -> downstream-yocto-wpe-2.48-rpi5
    mac-bench        -> perf-macos-tolken"
        ;;
    perf-linux-rpi5)
        IMG_NETWORK=wifi-from-machine
        IMG_MACHINE=rpi5
        IMG_ARCH=arm64
        IMG_BASE_KIND=ubuntu-raspi
        # Ubuntu 26.04 LTS preinstalled server. The same base as the board's
        # own workstation install, deliberately: that install drives this radio
        # onto this AP today, which is the property three earlier attempts on a
        # Raspberry Pi OS base could not get.
        IMG_BASE_URL=https://cdimage.ubuntu.com/releases/26.04/release/ubuntu-26.04-preinstalled-server-arm64+raspi.img.xz
        IMG_BASE_SHA256=10604098a0c4eeb7359e58e12b01badbce8c74b0d53b414e633ba0b047b512cd
        # The hostname keeps the old short form: it is what the system calls
        # itself on mDNS, and the flashed stick already announces it.
        IMG_HOSTNAME=rpi5-perf
        # 15 minutes: long enough to ssh in and claim the board for a real
        # session, short enough that a wedged boot costs one coffee rather than
        # a trip to the device. `wk boot rpi5 --keep` is what claims it.
        IMG_WATCHDOG=900
        # Off, and this is the subtle one. A distro image that grows its root
        # and reboots itself spends the one-shot on the first boot, lands back
        # on the workstation, and the image never finishes coming up. The stick
        # is 29 GB against a ~4 GB image; the unused space stays unused, and
        # build products belong on a payload partition anyway, never in the
        # image.
        IMG_GROW=off
        # perf is the one tool the profiling consumer cannot supply itself:
        # `perf inject` is what turns a JIT dump into resolvable symbols. It
        # needs egress on first boot, which this image has once the radio is
        # up -- and if it fails, cloud-init records the failure and sshd is
        # still running, which is the failure mode to want.
        #
        # avahi-daemon earns its place for a different reason: the image has no
        # tailscale and never will, so it is reachable only over the LAN, and
        # `rpi5-perf.local` is the one name for it that does not depend on the
        # driving machine's ARP cache being warm. The workstation already
        # resolves .local (nss-mdns, avahi running), so this closes the loop
        # with one package.
        IMG_PACKAGES="linux-tools-raspi avahi-daemon"
        IMG_LABEL_ROOT=wk-image-root   # ext4, <= 16 chars
        IMG_LABEL_BOOT=WK-IMG-BOOT     # FAT, <= 11 chars, upper case
        ;;
    perf-linux-rpi3)
        # Refused rather than built, because the obvious thing here is wrong.
        # The rpi3 is the fleet's only 32-bit board -- armv7l, a buildroot/WPE
        # rig with 931 MB and no swap -- and it is that deliberately: this repo
        # carries a whole armhf story (`wk new --arch armhf`) and the rpi3 is
        # where 32-bit gets exercised on real hardware. Handing it the arm64
        # base the other profiles use would boot (the Cortex-A53 is 64-bit
        # capable) and would quietly convert the one device that tests 32-bit
        # into another 64-bit one.
        #
        # This used to wait on a decision -- "a 32-bit base for this profile, or
        # an explicit 'the rpi3 becomes arm64'". The decision was taken on
        # 2026-08-20 and it was neither, so the refusal stays but the reason
        # changed: there is no armhf Ubuntu raspi image to build this *from*
        # (26.04 publishes arm64 desktop and arm64 server, and nothing else),
        # and the rpi3's perf systems are Yocto builds instead -- which
        # targets.conf already had targets for in both widths.
        die "there is no perf-linux-rpi3, and there will not be one.

    'perf-linux-*' profiles are seeded from Ubuntu's preinstalled raspi image,
    and Ubuntu 26.04 publishes no armhf one -- arm64 desktop and arm64 server,
    and nothing else. There is nothing to seed a 32-bit rpi3 system from.

    A 32-bit perf run has to measure a 32-bit kernel and userspace rather than
    a 32-bit process on a 64-bit kernel, so the answer is not to build this
    board an arm64 system either. Use the Yocto profiles, which exist in both
    widths:

        wk sysimage build downstream-yocto-wpe-2.48-rpi3-32   its native width
        wk sysimage build downstream-yocto-wpe-2.48-rpi3-64   marginal, for comparison

    docs/HANDOFF-vocabulary.md, '32-bit and 64-bit', has the whole argument."
        ;;
    perf-linux-rpi4)
        # Cabled, so no credential is involved and the image builds with the
        # board switched off -- which is the whole difference between these and
        # the rpi5's profile.
        IMG_NETWORK=wired
        IMG_MACHINE=rpi4
        IMG_ARCH=arm64
        IMG_BASE_KIND=ubuntu-raspi
        IMG_BASE_URL=https://cdimage.ubuntu.com/releases/26.04/release/ubuntu-26.04-preinstalled-server-arm64+raspi.img.xz
        IMG_BASE_SHA256=10604098a0c4eeb7359e58e12b01badbce8c74b0d53b414e633ba0b047b512cd
        IMG_HOSTNAME=rpi4-perf
        IMG_WATCHDOG=900
        IMG_GROW=off
        IMG_PACKAGES="linux-tools-raspi avahi-daemon"
        IMG_LABEL_ROOT=wk-image-root
        IMG_LABEL_BOOT=WK-IMG-BOOT
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
        # Recorded from the artifact this repo actually fetched (2026-08-21).
        # Upstream publishes no checksum file, so the pin is "what we verified
        # once" rather than "what they said" -- which is still the property that
        # matters: it cannot change underneath us without this failing.
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
        PMO_PACKAGES="openssh,nftables,dnsmasq,chrony,tailscale,jq,iw,ethtool,logrotate,zram-init,v4l-utils,networkmanager,avahi"
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
                # driver that would bind them is simply not built -- measured on
                # the phone 2026-08-22. Nothing in userspace substitutes: this is
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
                # the phone's hardware cannot see. That happened here
                # (2026-08-21): rpi5 associated on channel 52, and the phone
                # came up showing every neighbour's 2.4 GHz network and not the
                # one it was built for. pmos_check_uplink_band refuses it now.
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
    # --- yocto ------------------------------------------------------------
    #
    # A different mechanism entirely, and the profile says so in one field
    # rather than by which other fields happen to be set. Nothing below is a
    # distro base, a cloud-init seed, or a filesystem relabel: bitbake builds
    # the whole distribution, partitions it with wic, and the result enters the
    # store as an image like any other -- which is the entire reason to put it
    # here rather than in a command of its own. `wk sysimage write`, `wk sysimage
    # show` and the SD-card path then work on it unchanged.
    downstream-wpe-2.46-rpi4)
        # The known-good configuration, and the reason it exists as a profile of
        # its own rather than as a flag on the 2.48 one: it is a different
        # *repository*, not just a different branch. `wpe-2.46` lives in
        # WebPlatformForEmbedded/WPEWebKit -- the downstream WPE repo, wired as
        # `wpe` (lib/store.sh) -- where 2.48's `webkitglib/2.48` is upstream
        # WebKit/WebKit. A `git fetch origin wpe-2.46` finds nothing at all,
        # which is why YOC_REMOTE had to exist before this profile could.
        #
        # Known-good means: it is the pairing that has actually built and run
        # (Ubuntu 24.04 build host + this branch), and the `rpi3` skill already
        # clones exactly this branch for the 32-bit board. So it is the profile
        # to reach for when the question is "is my change the problem, or is the
        # configuration?" -- and it carries **no local fixes** for that reason.
        IMG_BUILDER=yocto
        IMG_MACHINE=rpi4
        IMG_ARCH=arm64
        YOC_REMOTE=wpe
        YOC_BRANCH=wpe-2.46
        YOC_TARGET=rpi4-64bits-mesa
        YOC_IMAGE=webkit-dev-ci-tools
        YOC_RM_WORK=1
        YOC_CHROMIUM=0
        # Tested unmodified on 2026-08-21, and it does not build here. The
        # finding, because it is the useful half of this profile: with no local
        # layer the build dies in `do_package` on `update-rc.d` and `base-files`
        # with pseudo's own signature --
        #
        #   got *at() syscall for unknown directory, fd 4
        #   unknown base path for fd 4, path sbin
        #   tar: ./usr/sbin: Cannot mkdir: Bad address
        #
        # byte for byte what recipes-devtools/pseudo/pseudo_%.bbappend
        # documents. So the pseudo bug is a property of **pseudo plus this
        # host's kernel** (7.0.11 aarch64), not of the release branch: 2.46
        # pins poky 6879650b, whose pseudo is the same 1.9.0-era fakeroot 2.48
        # pins, and it fails identically. A branch cannot be known-good against
        # a kernel that postdates its pseudo.
        #
        # So the layer stays on, and that does *not* compromise "the image
        # works without changes": pseudo is a build-time fakeroot and is never
        # installed into the image. What it changes is how the image is built,
        # not what the image contains. `--no-local-layer` re-runs the
        # experiment on a host where it might pass.
        YOC_LOCAL_LAYER=1
        IMG_HOSTNAME=raspberrypi4-64
        IMG_WATCHDOG=900
        ;;
    downstream-yocto-wpe-2.48-rpi4)
        IMG_BUILDER=yocto
        IMG_MACHINE=rpi4
        IMG_ARCH=arm64
        # The version pin, and the only one. Tools/yocto on this branch names
        # poky, meta-openembedded, meta-raspberrypi, meta-webkit, meta-clang
        # and meta-browser by commit, so pinning the branch pins the whole
        # distribution -- and pins it to the same tree the WebKit that will run
        # on the board is built from, which is what makes the pair coherent.
        # `webkitglib/2.48` is the release branch for both GLib ports; there is
        # no separate `wpe-2.48` in WebKit/WebKit.
        YOC_BRANCH=webkitglib/2.48
        YOC_TARGET=rpi4-64bits-mesa
        # targets.conf's image_basename for that target. Named here as well so
        # a missing or renamed section fails with "that target is gone" rather
        # than with bitbake's own error four hours in.
        YOC_IMAGE=webkit-dev-ci-tools
        YOC_RM_WORK=1
        # Chromium out. The branch's own local-rpi4-64bits-mesa.conf adds it --
        # "Add chromium to image to be able to compare WPE/Chromium
        # performance" -- and that is a real reason on a fleet built for
        # comparative benchmarking. It is also, by a wide margin, the most
        # expensive thing in the build: measured here, chromium-ozone-wayland
        # and gn-native were 21 GB of TMPDIR *each*, with rust-native,
        # cargo-native, rust-llvm-native and mozjs-115 behind them, and roughly
        # half of the 13,379 tasks. This profile exists to get a WPE runtime
        # onto the rpi4, so it is off, and `--chromium` puts it back for the day
        # the comparison is the point.
        YOC_CHROMIUM=0
        IMG_WATCHDOG=900
        # What the board calls itself. Yocto takes it from MACHINE and there is
        # no cloud-init here to override it, so this is recorded rather than
        # applied -- it is what `wk sysimage show` should be able to answer.
        IMG_HOSTNAME=raspberrypi4-64
        ;;
    # The 32-bit half of the same distribution, and the reason 32-bit is not a
    # separate problem to solve.
    #
    # A 32-bit perf run measures a 32-bit *system* -- a 32-bit kernel and a
    # 32-bit userspace -- and not a 32-bit process borrowing a 64-bit kernel's
    # compat layer. The two differ in syscall path, page size and pointer
    # width in the kernel, so a number from one is not a number from the other,
    # and the armhf port that ships to customers runs the first.
    #
    # Which is why the base for this is Yocto rather than a distro image.
    # Ubuntu 26.04 publishes **no armhf raspi image at all** (checked
    # 2026-08-20: `arm64` desktop and server, and nothing else), so the obvious
    # route does not exist; and `Tools/yocto/targets.conf` on this branch
    # already carries `rpi3-32bits-mesa`, `rpi3-32bits-userland`,
    # `rpi4-32bits-mesa` and `rpi3-64bits-mesa` beside the 64-bit rpi4 target.
    # The 32-bit systems were already buildable; nothing had named them.
    #
    # This also answers the question the rpi3 entry above was waiting on. It
    # asked for "a 32-bit base for this profile, or an explicit 'the rpi3
    # becomes arm64'", and the answer is neither: the rpi3's perf system is a
    # Yocto build, in whichever width the run is measuring, and no distro base
    # is involved.
    downstream-yocto-wpe-2.48-rpi4-32|downstream-yocto-wpe-2.48-rpi3-32|downstream-yocto-wpe-2.48-rpi3-64|downstream-yocto-wpe-2.48-rpi5)
        IMG_BUILDER=yocto
        YOC_BRANCH=webkitglib/2.48
        YOC_IMAGE=webkit-dev-ci-tools
        YOC_RM_WORK=1
        YOC_CHROMIUM=0
        # Same as the distro profiles, and for the same reason: these images
        # are booted as bench systems, and a bench system with no self-return
        # is a board that stays borrowed until somebody notices.
        IMG_WATCHDOG=900
        case "$1" in
            downstream-yocto-wpe-2.48-rpi4-32)
                IMG_MACHINE=rpi4; IMG_ARCH=armhf
                YOC_TARGET=rpi4-32bits-mesa; IMG_HOSTNAME=raspberrypi4 ;;
            downstream-yocto-wpe-2.48-rpi3-32)
                IMG_MACHINE=rpi3; IMG_ARCH=armhf
                # `-mesa`, not `-userland`: targets.conf offers both for the
                # rpi3 and the userland one is the closed Broadcom stack. Mesa
                # is what the rpi4 targets use, so it is the one that keeps a
                # rpi3 number and a rpi4 number describing the same graphics
                # path. `-userland` is a deliberate second profile if it is
                # ever wanted, not a default.
                YOC_TARGET=rpi3-32bits-mesa; IMG_HOSTNAME=raspberrypi3 ;;
            downstream-yocto-wpe-2.48-rpi5)
                IMG_MACHINE=rpi5; IMG_ARCH=arm64
                # The rpi5 as a Yocto system, so that the board can be measured
                # running what ships rather than only a distro -- without
                # touching its NVMe workstation, which is untouched here for
                # the same reason it is untouched by perf-linux-rpi5: the image goes
                # on the USB stick and the firmware one-shot boots it.
                #
                # `targets.conf` has no [rpi5-64bits-mesa] section on this
                # branch, and everything it would need is already there:
                # `Tools/yocto/rpi/local-rpi5-64bits-mesa.conf` is shipped, and
                # the pinned meta-raspberrypi carries `raspberrypi5.conf`. The
                # section is the only missing piece, so yocto_build refuses
                # with the eight lines to add rather than failing inside
                # bitbake.
                YOC_TARGET=rpi5-64bits-mesa; IMG_HOSTNAME=raspberrypi5 ;;
            downstream-yocto-wpe-2.48-rpi3-64)
                IMG_MACHINE=rpi3; IMG_ARCH=arm64
                # Buildable, and marginal on this board: 931 MB of RAM, no
                # swap. It exists so that "is the 64-bit port slower here, or
                # just short of memory" is answerable, which is a question the
                # 32-bit-only rpi3 could never be asked.
                YOC_TARGET=rpi3-64bits-mesa; IMG_HOSTNAME=raspberrypi3-64 ;;
        esac
        ;;
    *)  return 1 ;;
    esac
}
