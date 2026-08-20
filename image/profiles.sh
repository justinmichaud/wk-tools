# Named boot-image profiles -- the spec that `wk image build` executes.
#
# The rule this file exists to keep (docs/HANDOFF-netboot.md, "One-command
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
#   IMG_WATCHDOG     (distro) seconds before the self-return reboot, unless kept
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
# attempts at this failed there and nowhere else (see the netboot handoff's
# "Attempts 2 and 3"). Ubuntu on this board, associating with this AP on
# channel 52, is the one configuration that is *known* to work -- so the image
# is that configuration, modified as little as possible. The slim
# built-from-scratch rootfs the size table in the netboot handoff sizes is the
# next increment, and it is what the RAM-root benchmarking phase needs; it is
# not what the profiling phase needs, and building it first would have meant
# debugging a new rootfs and a new radio setup at the same time.

image_profile_list() {
    cat <<'EOF'
rpi5-perf   Ubuntu 26.04 server, aarch64, for the rpi5's USB one-shot: no
            sandbox, perf_event_paranoid ours to set, WiFi baked in
rpi4-perf   the same, cabled: DHCP on eth0, no credential, so it builds with
            the board switched off
rpi3-perf   likewise, for the rpi3 on its direct cable

rpi4-wpe-2.48
            WPE WebKit 2.48's own Yocto image for the rpi4 (aarch64,
            scarthgap, weston): the runtime the 2.48 release branch pins,
            bitbaked from source in a workspace. Hours, not minutes.
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
    YOC_CHROMIUM=1

    case "$1" in
    rpi5-perf)
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
    rpi3-perf)
        # Refused rather than built, because the obvious thing here is wrong.
        # The rpi3 is the fleet's only 32-bit board -- armv7l, a buildroot/WPE
        # rig with 931 MB and no swap -- and it is that deliberately: this repo
        # carries a whole armhf story (`wk new --arch armhf`) and the rpi3 is
        # where 32-bit gets exercised on real hardware. Handing it the arm64
        # base the other profiles use would boot (the Cortex-A53 is 64-bit
        # capable) and would quietly convert the one device that tests 32-bit
        # into another 64-bit one.
        #
        # So this waits on a decision rather than guessing at it: either a
        # 32-bit base for this profile, or an explicit "the rpi3 becomes
        # arm64". See docs/HANDOFF-netboot.md's rpi3 section.
        die "no rpi3 image profile yet, and this is deliberate.

    The rpi3 is the fleet's only 32-bit board (armv7l, 931 MB, no swap), and
    the base every other profile uses is arm64. Building that here would boot
    -- and would silently turn the one device that exercises 32-bit into a
    64-bit one.

    Decide first: a 32-bit base for the rpi3, or the rpi3 becomes arm64."
        ;;
    rpi4-perf)
        # Cabled, so no credential is involved and the image builds with the
        # board switched off -- which is the whole difference between these and
        # the rpi5's profile.
        IMG_NETWORK=wired
        IMG_MACHINE="${1%-perf}"
        IMG_ARCH=arm64
        IMG_BASE_KIND=ubuntu-raspi
        IMG_BASE_URL=https://cdimage.ubuntu.com/releases/26.04/release/ubuntu-26.04-preinstalled-server-arm64+raspi.img.xz
        IMG_BASE_SHA256=10604098a0c4eeb7359e58e12b01badbce8c74b0d53b414e633ba0b047b512cd
        IMG_HOSTNAME="${1%-perf}-perf"
        IMG_WATCHDOG=900
        IMG_GROW=off
        IMG_PACKAGES="linux-tools-raspi avahi-daemon"
        IMG_LABEL_ROOT=wk-image-root
        IMG_LABEL_BOOT=WK-IMG-BOOT
        ;;
    # --- yocto ------------------------------------------------------------
    #
    # A different mechanism entirely, and the profile says so in one field
    # rather than by which other fields happen to be set. Nothing below is a
    # distro base, a cloud-init seed, or a filesystem relabel: bitbake builds
    # the whole distribution, partitions it with wic, and the result enters the
    # store as an image like any other -- which is the entire reason to put it
    # here rather than in a command of its own. `wk image write`, `wk image
    # show` and the SD-card path then work on it unchanged.
    rpi4-wpe-2.48)
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
        # What the board calls itself. Yocto takes it from MACHINE and there is
        # no cloud-init here to override it, so this is recorded rather than
        # applied -- it is what `wk image show` should be able to answer.
        IMG_HOSTNAME=raspberrypi4-64
        ;;
    *)  return 1 ;;
    esac
}
