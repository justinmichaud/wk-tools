# Boot images: where they live, and what makes one publishable. An image
# is an artifact keyed by its content, not a cache of a fact, so it
# belongs in the store alongside the base snapshots (CLAUDE.md,
# "smallest possible state").
#   cache/images/<file>   a fetched base, verified by the spec's sha256
#   images/<id>/disk.img  the built image
#   images/<id>/manifest  what it is, written LAST: the publication gate --
#                         a build killed anywhere leaves no manifest, which
#                         every reader ignores and a re-run remakes (rule 2).

# Where images live: not simply $WK_STORE. On a macOS host that's
# /var/lib/wk right inside the podman VM, a path the Mac cannot create --
# the same correction targets/vm.sh and targets/remote.sh make for their
# own host-side state.
_image_root() {
    if [ "$(uname -s)" = Darwin ] && [ -z "${WK_IN_VM:-}" ]; then
        wk_state_dir
    else
        printf '%s' "$WK_STORE"
    fi
}
# Every place a build can leave an image, declared in one list: there is no
# image store (`wk help`), output lives wherever its builder put it, and a
# list that is *declared* is the only kind `wk gc` can search exhaustively
# -- a glob that guesses is a glob that misses the newest builder's
# multi-gigabyte output. **A new builder adds a line here in the same
# change that adds the builder**; `wk selftest` checks every IMG_BUILDER in
# image/profiles.sh has one. Paths, one per line; absent is not an error --
# a machine that never ran a given builder simply has none of its output.
image_build_locations() {
    # builder: buildroot -- one tree per profile; output/ is the expensive
    # part (tens of GB), with the finished images in output/images.
    printf '%s\n' "$WK_STORE/cache/buildroot"
    # builder: yocto -- DL_DIR and sstate. The images come out inside the
    # build workspace itself, reclaimed by removing the workspace.
    printf '%s\n' "$WK_STORE/cache/yocto"
    # builder: fetch -- downloaded bases, keyed by checksum. An input, kept
    # deliberately, listed so nothing is a memory instead of this list.
    printf '%s\n' "$(image_cache_dir)"
    # builder: pmos -- lives on the pmos build host, not here; gc_pmos
    # prunes it there. Listed for completeness.
    # builder: none -- the retiring image store itself, while it still exists.
    printf '%s\n' "$(image_store_dir)"
}

image_store_dir() { echo "$(_image_root)/images"; }

image_cache_dir() { echo "$(_image_root)/cache/images"; }
image_dir()       { echo "$(image_store_dir)/$1"; }
image_disk()      { echo "$(image_store_dir)/$1/disk.img"; }
image_manifest()  { echo "$(image_store_dir)/$1/manifest"; }

image_complete() { [ -f "$(image_manifest "$1")" ]; }  # not disk.img: half-written

image_ids() {
    local d id
    [ -d "$(image_store_dir)" ] || return 0
    for d in "$(image_store_dir)"/*; do
        [ -d "$d" ] || continue
        id=$(basename "$d")
        image_complete "$id" && echo "$id"
    done
    return 0  # else a store whose newest directory is rubble "fails" this
}

# Directories with no manifest: rubble from an interrupted build, named
# separately so `wk sysimage ls` can report rather than hide it, and a
# re-build can delete it without a heuristic.
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

# Newest complete image of a profile; ids sort lexically into build order
# (UTC timestamps), the same property `current_base` relies on.
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

# The image is what the manifest says. Cheap to check before every write,
# far cheaper than discovering it on the far side of a flash.
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

# The byte offset of the boot partition, from its own table. Factored
# out: three readers want it, each a chance to disagree which is the boot one.
image_boot_offset() {
    local disk="$1" offset
    offset=$(sfdisk -J "$disk" 2>/dev/null \
        | sed -n 's/.*"start": *\([0-9]*\).*/\1/p' | head -1 | awk '{print $1 * 512}')
    [ -n "$offset" ] && [ "$offset" -gt 0 ] || return 1
    echo "$offset"
}

# What the kernel command line says the root filesystem is: read via
# mtools at a byte offset, no mount, no privilege. Ubuntu's raspi images
# keep cmdline.txt under the os_prefix directory the firmware selects, so
# both places are tried. Prints the raw `root=` value, or nothing.
image_root_spec() {
    local id="$1" disk offset p
    disk=$(image_disk "$id"); [ -f "$disk" ] || return 0
    offset=$(image_boot_offset "$disk") || return 0
    for p in ::/current/cmdline.txt ::cmdline.txt; do
        MTOOLS_SKIP_CHECK=1 mtype -i "$disk@@$offset" "$p" 2>/dev/null \
            | tr ' ' '\n' | kv_get root && return 0
    done
    return 0
}

# Which *kind* of device that root spec names, not the exact path: a card
# written in one reader is often booted in another, and "SD card" vs "USB
# disk" is the mistake worth catching, since firmware boots the kernel
# from whatever it was given and the kernel looks for a root that isn't there.
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

device_class() {  # same classification, for a device about to be written
    case "${1:-}" in
        /dev/mmcblk*) echo mmc ;;
        /dev/sd*)     echo usb ;;
        /dev/nvme*)   echo nvme ;;
        *)            echo unknown ;;
    esac
}

# Refuse a write whose image cannot boot from its device: nothing here
# fails, the write succeeds, and the discovery happens on a headless board
# that loaded a kernel and then couldn't find `/`.
#   image_check_root <id> <device> <what-it-is>
# `portable`, `network` and `unknown` all pass: the first works anywhere,
# the other two aren't this check's business.
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

# Refuse a write whose image cannot get as far as its kernel. image_check_root
# above asks whether the kernel will find its root; this asks whether the
# *firmware* will find the kernel. They fail differently: a kernel that
# can't find its root panics and (with `panic=10` in these images) reboots,
# so a board is at worst in a loop that stopping the arm ends. Firmware
# that can't find a kernel **halts** -- no retry, no fall-through, no way
# back over the wire -- the difference between an unattended lane and one
# needing a person in the room, so it's checked here rather than on a board.
#   image_check_boot_files <id> <machine>
image_check_boot_files() {
    local id="$1" machine="$2" disk offset dtb work out

    dtb=$(image_dtb_for "$machine")

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

# The device tree a board's firmware wants, the one thing
# image_check_boot_files can't read from the image. From the machine's own
# conf (MACH_DTB); missing it is a conf bug, so this dies rather than skip.
image_dtb_for() {
    . "$WK_ROOT/boot/machines.sh"
    machine_load "$1" 2>/dev/null || die "image_dtb_for: unknown machine '$1'"
    [ -n "$MACH_DTB" ] || die "image_dtb_for: '$1' (boot/machines/$1.conf) sets no MACH_DTB"
    printf '%s' "$MACH_DTB"
}

# Every class image_root_class returns needs a word here, `portable` and
# `network` included, or the commonest root (LABEL=) reads "unrecognised".
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

# One lock per mutated resource (rule 4): two concurrent builds would race
# on the rubble-cleanup and shared base download. The general lock
# (hold_lock, lib/common.sh), not a flock: macOS ships no flock(1), so the
# one Mac-runnable command here would lock nothing or die.

# --- images, where the builders left them ------------------------------------
# The model (`wk help`): a workspace produces an image, not imported,
# catalogued, or given a second name -- so "which images are there" is
# answered by looking, now, at the places builders leave output.
# One line per image, tab-separated: builder, workspace, path, bytes, mtime.
# Host-visible even in a container: a container workspace mounts
# /src/WebKit as an overlay whose upper layer is ws/<name>/changes
# (targets/container.sh), and a freshly built image is always a new file there.
image_workspace_scan() {
    local ws name f

    [ -d "$WK_STORE/ws" ] || return 0

    for ws in "$WK_STORE/ws"/*; do
        [ -d "$ws" ] || continue
        name=$(basename "$ws")

        # builder: yocto -- bitbake writes the wic beside the rootfs tarball
        # (yocto_image_dir, image/yocto.sh); globbed, not profile-derived.
        for f in "$ws"/changes/WebKitBuild/CrossToolChains/*/build/image/*.wic.xz; do
            [ -f "$f" ] || continue
            printf 'yocto\t%s\t%s\t%s\t%s\n' "$name" "$f" \
                "$(file_bytes "$f")" "$(_scan_mtime "$f")"
        done

        # builder: buildroot -- genimage assembles into output/images
        # (BR_IMAGE names the file). Globbed, same reason.
        for f in "$ws"/changes/WebKitBuild/buildroot/*/output/images/*.img; do
            [ -f "$f" ] || continue
            printf 'buildroot\t%s\t%s\t%s\t%s\n' "$name" "$f" \
                "$(file_bytes "$f")" "$(_scan_mtime "$f")"
        done
    done
    return 0
}

_scan_mtime() {  # ISO-8601 UTC; BSD stat takes -f, GNU stat takes -c.
    local e
    e=$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null) || return 0
    date -u -d "@$e" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -r "$e" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || printf '%s' "$e"
}

image_lock() {
    hold_lock image-store -w "${WK_LOCK_WAIT:-300}"
}

# A fetched base image, downloaded once, pinned by sha256. Resumable: this
# is gigabytes over WiFi, and restarting from scratch would make rule 2
# expensive; the checksum makes resuming safe.
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
