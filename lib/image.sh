# Boot images: where they live. An image is an artifact a workspace (or a
# build host) produces, with no catalogue of them (`wk help`): `wk sysimage
# ls` scans where each builder leaves its output (image_workspace_scan,
# below), and `wk sysimage write --from <path>` writes straight from there.
#   cache/images/<file>   a fetched base, verified by the spec's sha256 --
#                         a re-fetchable input, keyed by content rather than
#                         a build's identity, so it is kept deliberately.

# Where the input cache lives: not simply $WK_STORE. On a macOS host that's
# /var/lib/wk inside the podman VM, a path the Mac cannot create.
_image_root() {
    if [ "$(uname -s)" = Darwin ] && [ -z "${WK_IN_VM:-}" ]; then
        wk_state_dir
    else
        printf '%s' "$WK_STORE"
    fi
}
# Every place a build can leave bytes, declared in one list, so `wk gc` can
# report and (with --purge-builds) reclaim each exhaustively. **A new builder
# adds a line here in the same change that adds the builder**; `wk selftest`
# checks every IMG_BUILDER in image/profiles.sh has one. Paths, one per line;
# absent is not an error -- a machine that never ran a builder has no output.
image_build_locations() {
    # builder: buildroot -- one tree per profile
    printf '%s\n' "$WK_STORE/cache/buildroot"
    # builder: yocto -- DL_DIR, sstate, the build
    printf '%s\n' "$WK_STORE/cache/yocto"
    # builder: fetch -- downloaded bases keyed by checksum, kept as input
    printf '%s\n' "$(image_cache_dir)"
    # builder: pmos -- lives on the pmos build host; gc_pmos prunes it there.
}

image_cache_dir() { echo "$(_image_root)/cache/images"; }

# The byte offset of the boot partition, from its own table. Factored out so
# three readers can't disagree on which partition is the boot one.
image_boot_offset() {
    local disk="$1" offset
    offset=$(sfdisk -J "$disk" 2>/dev/null \
        | sed -n 's/.*"start": *\([0-9]*\).*/\1/p' | head -1 | awk '{print $1 * 512}')
    [ -n "$offset" ] && [ "$offset" -gt 0 ] || return 1
    echo "$offset"
}

# What the kernel command line says the root filesystem is: read via mtools
# at a byte offset, no mount, no privilege. Ubuntu's raspi images keep
# cmdline.txt under the os_prefix directory the firmware selects, so both
# paths are tried. Prints the raw `root=` value, or nothing.
#   image_root_spec <disk-image-path>
image_root_spec() {
    local disk="$1" offset p
    [ -f "$disk" ] || return 0
    offset=$(image_boot_offset "$disk") || return 0
    for p in ::/current/cmdline.txt ::cmdline.txt; do
        MTOOLS_SKIP_CHECK=1 mtype -i "$disk@@$offset" "$p" 2>/dev/null \
            | tr ' ' '\n' | kv_get root && return 0
    done
    return 0
}

# Which *kind* of device that root spec names, not the exact path: a card
# written in one reader is often booted in another, and "SD card" vs "USB
# disk" is the mistake worth catching.
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

# Refuse a write whose image cannot boot from its device: without this,
# the write succeeds and the discovery happens on a headless board that
# loaded a kernel and then couldn't find `/`.
#   image_check_root <disk-image-path> <device> <what-it-is>
# `portable`, `network` and `unknown` all pass: the first works anywhere.
image_check_root() {
    local disk="$1" dev="$2" what="$3" spec class want
    spec=$(image_root_spec "$disk")
    class=$(image_root_class "$spec")
    want=$(device_class "$dev")

    case "$class" in
        portable|network|unknown) return 0 ;;
    esac
    [ "$class" = "$want" ] && return 0
    if [ -n "${WK_ANY_ROOT:-}" ]; then
        warn "this image expects $(image_root_word "$class") and $dev is $(image_root_word "$want");
  writing anyway (WK_ANY_ROOT). It will not boot -- this proves the transfer only."
        return 0
    fi

    die "this image expects to boot from $(image_root_word "$class"), and $dev is $(image_root_word "$want").

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
# *firmware* will find the kernel. A kernel that can't find its root panics
# and reboots (`panic=10`); firmware that can't find a kernel **halts** --
# no retry, no fall-through, no way back over the wire -- the difference
# between an unattended lane and one needing a person in the room.
#   image_check_boot_files <disk-image-path> <machine>
image_check_boot_files() {
    local disk="$1" machine="$2" offset dtb work out

    dtb=$(image_dtb_for "$machine")

    [ -f "$disk" ] || return 0
    offset=$(image_boot_offset "$disk") || return 0

    command -v mcopy >/dev/null 2>&1 || { debug "no mtools; not checking boot files"; return 0; }

    work=$(mktemp -d)
    if ! MTOOLS_SKIP_CHECK=1 mcopy -s -i "$disk@@$offset" "::*" "$work/" 2>/dev/null; then
        rm -rf "$work"
        debug "could not read this image's boot partition; not checking boot files"
        return 0
    fi

    out=$(python3 "$WK_ROOT/boot/check-boot-files.py" \
              --root "$work" --dtb "$dtb" 2>&1) && { rm -rf "$work"; return 0; }
    rm -rf "$work"

    die "this image's boot partition is missing files a $machine needs to reach its kernel:

$(printf '%s\n' "$out" | sed 's/^/      /')

    Firmware that cannot find a kernel halts. It does not move on to the next
    BOOT_ORDER entry and it does not come back, so writing this would cost a
    trip to the board rather than a reboot.

    Nothing has been written. The image is the problem, not the disk: rebuild
    it, or check what its config.txt names against what its boot partition
    holds."
}

# The device tree a board's firmware wants, from the machine's own conf
# (MACH_DTB); missing it is a conf bug, so this dies rather than skip.
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

# --- images, where the builders left them ------------------------------------
# The model (`wk help`): a workspace produces an image, not imported or
# catalogued, so "which images are there" is answered by looking, now, at
# the places builders leave output. One line per image, tab-separated:
# builder, workspace, path, bytes, mtime. Host-visible even in a container:
# a container workspace mounts /src/WebKit as an overlay whose upper layer
# is ws/<name>/changes (targets/container.sh), where a built image lands.
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
        # (BR_IMAGE names the file).
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

# A fetched base image, downloaded once, pinned by sha256. Resumable: this
# is gigabytes over WiFi, and the checksum makes resuming safe.
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
