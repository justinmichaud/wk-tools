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
#   IMG_MACHINE      the fleet machine it is built for (boot/machines.sh)
#   IMG_ARCH         the image's architecture
#   IMG_BASE_URL     distro base, pinned
#   IMG_BASE_SHA256  ... and pinned by content, not by name
#   IMG_BASE_KIND    which seeding dialect the base speaks
#   IMG_HOSTNAME     what it calls itself once booted
#   IMG_WATCHDOG     seconds before the self-return reboot, unless kept
#   IMG_GROW         whether cloud-init may grow the root partition
#   IMG_PACKAGES     packages installed on first boot (needs egress)
#   IMG_LABEL_ROOT   the image's own root filesystem label
#   IMG_LABEL_BOOT   ... and its boot filesystem label
#   IMG_SPEC_DIR     the profile's own files
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
EOF
}

image_profile_load() {
    IMG_PROFILE="$1"
    IMG_SPEC_DIR="$WK_ROOT/image/$1"

    case "$1" in
    rpi5-perf)
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
    *)  return 1 ;;
    esac
}
