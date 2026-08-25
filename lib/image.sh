# Boot images: where they live, and what makes one publishable.
#
# An image is an artifact keyed by its content, not a cache of a fact, so it
# belongs in the store alongside the base snapshots and the seeded benchmark
# payloads (CLAUDE.md, "smallest possible state": "could a read recompute
# this value, or only re-download/rebuild it?" -- only rebuild).
#
#   cache/images/<file>       the distro base, downloaded once, verified by
#                             the sha256 the spec pins. Re-downloadable, so a
#                             cache in the honest sense.
#   images/<id>/disk.img      the built image
#   images/<id>/manifest      what it is, written LAST -- this is the gate
#
# The manifest being written last is the whole publishing protocol, and it is
# rule 2 (crash-only) applied here: a build killed at any point leaves a
# directory with no manifest, which every reader ignores and a re-run destroys
# and remakes. There is no half-built image to recognise, and therefore no
# repair path to get wrong -- rule 3, wipe over repair.

# Where images live, and it is not simply $WK_STORE on every machine.
#
# $WK_STORE is /var/lib/wk on a macOS host: right inside the podman VM, and a
# path the Mac itself cannot even create. targets/vm.sh and targets/remote.sh
# already make this same correction for their own host-side state, with the same
# reasoning -- "right inside the podman VM and wrong on a macOS workstation" --
# and an image store is host-side state of exactly that kind. `wk sysimage` is a
# host command that is never forwarded, so on a Mac it was resolving every path
# below into a directory that does not exist, and the first thing to notice was
# an import failing with "mkdir: /var/lib/wk: Permission denied".
#
# Deterministic rather than "wherever is writable": a store whose location
# depends on what happens to exist is a store where yesterday's images become
# invisible.
_image_root() {
    if [ "$(uname -s)" = Darwin ] && [ -z "${WK_IN_VM:-}" ]; then
        wk_state_dir
    else
        printf '%s' "$WK_STORE"
    fi
}
# Every place a build can leave an image, declared in one list.
#
# This exists because there is no image store (`wk help images`): output lives
# wherever the builder that made it put it, so "where are the images" stops
# being "one directory" and becomes a question with as many answers as there are
# builders. A list that is *declared* is the only kind `wk gc` can search
# exhaustively -- a search that guesses is a search that misses the newest
# builder, and the thing it misses is tens of gigabytes.
#
# The rule that goes with it: **a new builder adds a line here in the same
# change that adds the builder.** `wk selftest` checks that every IMG_BUILDER in
# image/profiles.sh has one, so a builder without a location is a failure rather
# than a slow leak.
#
# Paths, one per line, and it is not an error for one to be absent -- a machine
# that has never run a given builder simply has none of its output.
image_build_locations() {
    # Each line is annotated with the builder it belongs to, and `wk selftest`
    # reads those annotations: a builder in image/profiles.sh with no line here
    # is a failure, not a slow leak.
    # builder: buildroot -- one tree per profile, and the whole output/ of each
    # is the expensive part (tens of GB), with the finished images in
    # output/images.
    printf '%s\n' "$WK_STORE/cache/buildroot"
    # builder: yocto -- DL_DIR and sstate. The images themselves come out inside
    # the build workspace, which is the workspace's own disk and is reclaimed by
    # removing the workspace.
    printf '%s\n' "$WK_STORE/cache/yocto"
    # builder: distro, fetch -- downloaded bases, keyed by checksum. An input
    # rather than an output, kept deliberately, and listed so that "everywhere a
    # build leaves bytes" is one list rather than a memory.
    printf '%s\n' "$(image_cache_dir)"
    # builder: pmos -- the phone images' build output lives on the pmos build
    # host, not here; gc_pmos prunes it there. Listed for completeness so that
    # the builder is accounted for rather than silently absent.
    # builder: none -- the retiring image store itself, while it still exists.
    printf '%s\n' "$(image_store_dir)"
}

image_store_dir() { echo "$(_image_root)/images"; }

# The compressed original and its block map, when the builder kept them. The
# yocto and pmos builders have them (bitbake's wic output; xz plus `bmaptool
# create`); the distro builder edits a raw image in place and has nothing to
# map. Both optional -- their absence picks the dd path in boot/disk.sh, it is
# not an error.
image_wic()       { echo "$(image_store_dir)/$1/disk.wic.xz"; }
image_bmap()      { echo "$(image_store_dir)/$1/disk.bmap"; }
image_cache_dir() { echo "$(_image_root)/cache/images"; }
image_dir()       { echo "$(image_store_dir)/$1"; }
image_disk()      { echo "$(image_store_dir)/$1/disk.img"; }
image_manifest()  { echo "$(image_store_dir)/$1/manifest"; }

# Complete = has a manifest. Nothing else is looked at: a reader that inferred
# completeness from the presence of disk.img would accept a half-written one.
image_complete() { [ -f "$(image_manifest "$1")" ]; }

image_ids() {
    local d id
    [ -d "$(image_store_dir)" ] || return 0
    for d in "$(image_store_dir)"/*; do
        [ -d "$d" ] || continue
        id=$(basename "$d")
        image_complete "$id" && echo "$id"
    done
    # The loop's status is the last iteration's test, so without this a store
    # whose newest directory is rubble makes this function "fail".
    return 0
}

# Directories with no manifest: rubble from an interrupted build. Named
# separately from image_ids so `wk sysimage ls` can report them as what they are
# rather than hiding them, and so a re-build can delete them without a
# heuristic.
image_rubble() {
    local d id
    [ -d "$(image_store_dir)" ] || return 0
    for d in "$(image_store_dir)"/*; do
        [ -d "$d" ] || continue
        id=$(basename "$d")
        image_complete "$id" || echo "$id"
    done
    return 0
}

# Newest complete image of a profile. Ids carry a UTC timestamp, so they sort
# lexically into build order -- the same property `current_base` relies on.
image_latest() {
    local profile="$1" id last=''
    for id in $(image_ids | sort); do
        [ "$(manifest_get "$id" profile)" = "$profile" ] && last="$id"
    done
    [ -n "$last" ] || return 1
    echo "$last"
}

manifest_get() {
    local id="$1" key="$2" f
    f=$(image_manifest "$id")
    [ -f "$f" ] || return 1
    sed -n "s/^$key=//p" "$f" | head -1
}

# The image really is what the manifest says. Cheap enough to be worth doing
# whenever an image is about to be written to a device, and far cheaper than
# discovering it on the far side of a flash.
image_verify() {
    local id="$1" want have
    image_complete "$id" || { warn "image '$id' has no manifest -- it is rubble from an interrupted build"; return 1; }
    want=$(manifest_get "$id" disk_sha256)
    [ -n "$want" ] || { warn "image '$id' records no disk_sha256"; return 1; }
    have=$(sha256sum "$(image_disk "$id")" | cut -d' ' -f1)
    [ "$want" = "$have" ] && return 0
    warn "image '$id' does not match its manifest
    manifest: $want
    on disk:  $have"
    return 1
}

# Whether the compressed copy still describes the image lying beside it.
#
# They are two files and only one of them is what the builder produced. The
# yocto import decompresses bitbake's wic into disk.img and *then* edits it --
# the fleet integration, the root retarget -- so disk.wic.xz can easily
# describe a system that no longer exists. The bmap write path reads only the
# compressed copy and never disk.img, so writing from a stale one succeeds,
# reports success, and puts a disk on a board with none of that work on it.
#
# So the fast path is opt-in by provenance rather than by existence: `wic_of`
# records the disk.img sha256 the compressed copy was derived from, and only an
# exact match takes it. An image built before this field existed has no
# `wic_of` and takes the slow path -- the safe direction to be wrong in.
image_fast_path_ok() {
    local id="$1" of want
    of=$(manifest_get "$id" wic_of 2>/dev/null) || return 1
    [ -n "$of" ] || return 1
    want=$(manifest_get "$id" disk_sha256) || return 1
    [ "$of" = "$want" ]
}

# What the image's kernel command line says its root filesystem is.
#
# Read out of the image's own boot partition with mtools at a byte offset -- no
# mount, no privilege, the same trick the rest of this file relies on. Ubuntu's
# raspi images keep cmdline.txt under the os_prefix directory the firmware
# selects, so both places are tried.
#
# Prints the raw `root=` value, or nothing if there is no cmdline to read.
# The byte offset of the image's boot partition, from its own partition table.
# Factored out because three readers now want it and each one computing it again
# is three chances to disagree about which partition is the boot one.
image_boot_offset() {
    local disk="$1" offset
    offset=$(sfdisk -J "$disk" 2>/dev/null \
        | sed -n 's/.*"start": *\([0-9]*\).*/\1/p' | head -1 | awk '{print $1 * 512}')
    [ -n "$offset" ] && [ "$offset" -gt 0 ] || return 1
    echo "$offset"
}

image_root_spec() {
    local id="$1" disk offset p
    disk=$(image_disk "$id"); [ -f "$disk" ] || return 0
    offset=$(image_boot_offset "$disk") || return 0
    for p in ::/current/cmdline.txt ::cmdline.txt; do
        MTOOLS_SKIP_CHECK=1 mtype -i "$disk@@$offset" "$p" 2>/dev/null \
            | tr ' ' '\n' | sed -n 's/^root=//p' | head -1 && return 0
    done
    return 0
}

# Which *kind* of device that root spec names.
#
# The kind is what can be checked, not the exact path: a card written in one
# machine's reader is very often booted in another, so `/dev/mmcblk0` on the
# writer and on the booter are different facts that happen to share a spelling.
# What does carry across is "this image expects an SD card" versus "this image
# expects a USB disk", and that is the mistake worth catching -- the firmware
# boots the kernel from whatever it was given and the kernel then looks for a
# root that is not there.
#
#   mmc | usb | nvme  -- a specific kind of device
#   portable          -- LABEL=/UUID=/PARTUUID=, so any device it is written to
#   network           -- an NFS root; not a local device at all
#   unknown           -- no cmdline, or a form not recognised
image_root_class() {
    case "${1:-}" in
        "")                    echo unknown ;;
        LABEL=*|UUID=*|PARTUUID=*) echo portable ;;
        /dev/nfs|*nfsroot*)    echo network ;;
        /dev/mmcblk*)          echo mmc ;;
        /dev/sd*)              echo usb ;;
        /dev/nvme*)            echo nvme ;;
        *)                     echo unknown ;;
    esac
}

# And the same classification for a device we are about to write.
device_class() {
    case "${1:-}" in
        /dev/mmcblk*) echo mmc ;;
        /dev/sd*)     echo usb ;;
        /dev/nvme*)   echo nvme ;;
        *)            echo unknown ;;
    esac
}

# Refuse a write whose image cannot boot from the device it is going to.
#
# The failure it prevents is expensive in a particular way: nothing here fails,
# the write succeeds, and the discovery happens on a headless board that loaded
# a kernel and then could not find `/`.
#
#   image_check_root <id> <device> <what-it-is>
#
# `portable`, `network` and `unknown` all pass: the first genuinely works
# anywhere, and the other two are not this check's business to judge.
image_check_root() {
    local id="$1" dev="$2" what="$3" spec class want
    spec=$(image_root_spec "$id")
    class=$(image_root_class "$spec")
    want=$(device_class "$dev")

    case "$class" in
        portable|network|unknown) return 0 ;;
    esac
    [ "$class" = "$want" ] && return 0
    if [ -n "${WK_ANY_ROOT:-}" ]; then
        warn "$id expects $(image_root_word "$class") and $dev is $(image_root_word "$want");
  writing anyway (WK_ANY_ROOT). It will not boot -- this proves the transfer only."
        return 0
    fi

    die "$id expects to boot from $(image_root_word "$class"), and $dev is $(image_root_word "$want").

    Its kernel command line says \`root=$spec\`. Written to $dev, the firmware
    would load the kernel from it and the kernel would then look for its root
    filesystem on $(image_root_word "$class") -- which is either absent or
    somebody else's disk. Nothing about the write would fail; the board would.

    Either write it to $(image_root_word "$class") on $what, or rebuild the
    image for this device -- a wic image's root device comes from the recipe's
    wks file, not from anything this repo sets. Set WK_ANY_ROOT=1 to write it
    anyway (for testing the transfer, which is all it can prove)."
}

# Refuse a write whose image cannot get as far as its kernel.
#
# image_check_root above asks whether the kernel will find its root. This asks
# the question before it: whether the *firmware* will find the kernel. They fail
# differently and that is why both exist -- a kernel that cannot find its root
# panics, and with `panic=10` in these images it reboots, so a board is at worst
# in a loop that stopping the arm ends. Firmware that cannot find a kernel
# **halts**: no retry, no fall-through to the next BOOT_ORDER entry, no way back
# over the wire.
#
# That is the difference between an unattended lane and one that needs a person
# in the room, so it is checked here -- where the whole boot filesystem is
# sitting in a file and can be read in a second -- rather than discovered on a
# board.
#
# It first happened on 2026-08-20 and cost the rpi4 two power cycles.
#
#   image_check_boot_files <id> <machine>
image_check_boot_files() {
    local id="$1" machine="$2" disk offset dtb work out

    dtb=$(image_dtb_for "$machine")
    [ -n "$dtb" ] || { debug "no device tree known for '$machine'; not checking boot files"; return 0; }

    disk=$(image_disk "$id"); [ -f "$disk" ] || return 0
    offset=$(image_boot_offset "$disk") || return 0

    command -v mcopy >/dev/null 2>&1 || { debug "no mtools; not checking boot files"; return 0; }

    work=$(mktemp -d)
    if ! MTOOLS_SKIP_CHECK=1 mcopy -s -i "$disk@@$offset" "::*" "$work/" 2>/dev/null; then
        rm -rf "$work"
        debug "could not read $id's boot partition; not checking boot files"
        return 0
    fi

    out=$(python3 "$WK_ROOT/boot/check-boot-files.py" \
              --root "$work" --dtb "$dtb" 2>&1) && { rm -rf "$work"; return 0; }
    rm -rf "$work"

    die "$id's boot partition is missing files a $machine needs to reach its kernel:

$(printf '%s\n' "$out" | sed 's/^/      /')

    Firmware that cannot find a kernel halts. It does not move on to the next
    BOOT_ORDER entry and it does not come back, so writing this would cost a
    trip to the board rather than a reboot.

    Nothing has been written. The image is the problem, not the disk: rebuild
    it, or check what its config.txt names against what its boot partition
    holds."
}

# The device tree a board asks its firmware for. A fact about the machine, and
# the one thing image_check_boot_files cannot read out of the image.
image_dtb_for() {
    case "$1" in
        rpi5) echo bcm2712-rpi-5-b.dtb ;;
        rpi4) echo bcm2711-rpi-4-b.dtb ;;
        rpi3) echo bcm2710-rpi-3-b.dtb ;;
        *)    echo "" ;;
    esac
}

# Every class image_root_class can return needs a word here. `portable` and
# `network` were missing, so `wk sysimage write --dry-run` described the commonest
# root of all -- a LABEL=, which is exactly the kind that works anywhere -- as
# "an unrecognised kind of device", while the check on the very next line was
# passing it deliberately.
image_root_word() {
    case "$1" in
        mmc)      echo "an SD card (/dev/mmcblk*)" ;;
        usb)      echo "a USB or SCSI disk (/dev/sd*)" ;;
        nvme)     echo "an NVMe disk" ;;
        portable) echo "any device it is written to" ;;
        network)  echo "a network root, not a local device" ;;
        *)        echo "an unrecognised kind of device" ;;
    esac
}

# One lock per mutated resource (rule 4). The image store is one resource: two
# concurrent builds would race on the same rubble-cleanup and on the shared
# base download.
#
# The general lock (hold_lock, lib/common.sh) rather than a flock of its own,
# which is what this used to be. Two reasons, and the first is not a tidiness
# argument: macOS ships no flock(1) at all, so the one command in here that a
# Mac can run -- writing a disk attached to a fleet machine -- was locking
# nothing, or dying, depending on what was installed. The second is that a
# flock is held by the file descriptor and therefore by every process that
# inherits it, which is the property that made it wrong for workspaces too.
image_lock() {
    hold_lock image-store -w "${WK_LOCK_WAIT:-300}"
}

# The base distro image, downloaded once and pinned by sha256.
#
# Resumable, because this is 1.3 GB over the workstation's WiFi and an
# interrupted download that had to start again would make rule 2 expensive
# rather than free. The checksum is what makes resuming safe.
image_fetch_base() {
    local url="$1" sha="$2" dest cache
    cache=$(image_cache_dir); mkdir -p "$cache"
    dest="$cache/$(basename "$url")"

    if [ -f "$dest" ] && [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$sha" ]; then
        debug "base image already fetched: $dest"
        echo "$dest"; return 0
    fi

    info "fetching base image $(basename "$url")" >&2
    curl -fL --retry 5 -C - -o "$dest" "$url" >&2 \
        || die "could not fetch $url"

    [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$sha" ] \
        || die "checksum mismatch on $dest
    expected $sha
    Delete it and re-run; if it mismatches again the spec's pin is stale."
    echo "$dest"
}
