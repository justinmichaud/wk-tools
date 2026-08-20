# Boot images: where they live, and what makes one publishable.
#
# An image is an artifact keyed by its content, not a cache of a fact, so it
# belongs in the store alongside the base snapshots and the seeded benchmark
# payloads (docs/HANDOFF-workspace-state.md, rule 1: "could a read recompute
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

image_store_dir() { echo "$WK_STORE/images"; }
# The compressed original and its block map, when the builder kept them. Only
# the yocto builder has them (bitbake's wic output); the distro builder edits a
# raw image in place and has nothing to map. Both optional -- their absence
# picks the dd path in boot/disk.sh, it is not an error.
image_wic()       { echo "$WK_STORE/images/$1/disk.wic.xz"; }
image_bmap()      { echo "$WK_STORE/images/$1/disk.bmap"; }
image_cache_dir() { echo "$WK_STORE/cache/images"; }
image_dir()       { echo "$WK_STORE/images/$1"; }
image_disk()      { echo "$WK_STORE/images/$1/disk.img"; }
image_manifest()  { echo "$WK_STORE/images/$1/manifest"; }

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
# separately from image_ids so `wk image ls` can report them as what they are
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

# What the image's kernel command line says its root filesystem is.
#
# Read out of the image's own boot partition with mtools at a byte offset -- no
# mount, no privilege, the same trick the rest of this file relies on. Ubuntu's
# raspi images keep cmdline.txt under the os_prefix directory the firmware
# selects, so both places are tried.
#
# Prints the raw `root=` value, or nothing if there is no cmdline to read.
image_root_spec() {
    local id="$1" disk offset p
    disk=$(image_disk "$id"); [ -f "$disk" ] || return 0
    offset=$(sfdisk -J "$disk" 2>/dev/null \
        | sed -n 's/.*"start": *\([0-9]*\).*/\1/p' | head -1 | awk '{print $1 * 512}')
    [ -n "$offset" ] && [ "$offset" -gt 0 ] || return 0
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
# This is the same class of check as `check_root_is_reachable` in cmd/serve,
# which refuses to *netboot* an image whose cmdline names a local root. The
# failure it prevents is expensive in the same way: nothing here fails, the
# write succeeds, and the discovery happens on a headless board that fetched a
# kernel and then could not find `/`.
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

image_root_word() {
    case "$1" in
        mmc)  echo "an SD card (/dev/mmcblk*)" ;;
        usb)  echo "a USB or SCSI disk (/dev/sd*)" ;;
        nvme) echo "an NVMe disk" ;;
        *)    echo "an unrecognised kind of device" ;;
    esac
}

# One lock per mutated resource (rule 4). The image store is one resource: two
# concurrent builds would race on the same rubble-cleanup and on the shared
# base download. flock dies with its holder, so a killed build leaves no lock
# to clear -- which is the property that makes rule 2 possible at all.
#
# lib/common.sh is about to grow a general `with_lock` (the macOS lane owns
# that file this week); when it lands, this collapses into a call to it.
image_lock() {
    local d
    d=$(image_store_dir); mkdir -p "$d"
    exec 9>"$d/.lock"
    flock -w "${WK_LOCK_WAIT:-300}" 9 \
        || die "another 'wk image' is holding the image store lock"
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
