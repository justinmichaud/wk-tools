# Not simply $WK_STORE: on a macOS host that is /var/lib/wk inside the podman VM, a path the Mac cannot create.
_image_root() {
    if [ "$(uname -s)" = Darwin ] && [ -z "${WK_IN_VM:-}" ]; then
        wk_state_dir
    else
        printf '%s' "$WK_STORE"
    fi
}
image_build_locations() {  # every place a build can leave bytes, so `wk gc` reclaims each; a new builder adds a line, and `wk selftest` checks every IMG_BUILDER has one
    # builder: buildroot
    printf '%s\n' "$WK_STORE/cache/buildroot"
    # builder: yocto
    printf '%s\n' "$WK_STORE/cache/yocto"
    # builder: fetch
    printf '%s\n' "$(image_cache_dir)"
    # builder: pmos -- on the pmos build host, pruned there by gc_pmos
}

image_cache_dir() { echo "$(_image_root)/cache/images"; }

image_root_class() {  # which *kind* of device the spec names, not the path: a card written in one reader is often booted in another, and mmc-vs-usb is the mistake worth catching
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

image_check_root() {  # <root-spec> <device> <what-it-is>: refuse a card whose system cannot boot from the device it is on, or a headless board discovers it; WK_ANY_ROOT=1 warns instead
    local spec="$1" dev="$2" what="$3" class want
    class=$(image_root_class "$spec")
    want=$(device_class "$dev")

    case "$class" in
        portable|network|unknown) return 0 ;;
    esac
    [ "$class" = "$want" ] && return 0
    if [ -n "${WK_ANY_ROOT:-}" ]; then
        warn "this system expects $(image_root_word "$class") and $dev is $(image_root_word "$want");
  left as written (WK_ANY_ROOT). It will not boot -- this proves the transfer only."
        return 0
    fi

    die "the system on $dev expects to boot from $(image_root_word "$class"), and $dev is
    $(image_root_word "$want").

    Its kernel command line says \`root=$spec\`. The firmware would load the
    kernel from $dev and the kernel would then look for its root filesystem on
    $(image_root_word "$class") -- which is either absent or somebody else's
    disk. Nothing about the write failed; the board would.

    Either write it to $(image_root_word "$class") on $what, or rebuild the
    image for this device -- a wic image's root device comes from the recipe's
    wks file, not from anything this repo sets. Set WK_ANY_ROOT=1 to write it
    anyway (for testing the transfer, which is all it can prove)."
}

image_dtb_for() {  # the device tree a board's firmware wants; missing it is a conf bug
    . "$WK_ROOT/boot/machines.sh"
    machine_load "$1" 2>/dev/null || die "image_dtb_for: unknown machine '$1'"
    [ -n "$NODE_DTB" ] || die "image_dtb_for: '$1' (boot/machines/$1.conf) sets no NODE_DTB"
    printf '%s' "$NODE_DTB"
}

image_root_word() {  # every class image_root_class returns needs a word here
    case "$1" in
        mmc)      echo "an SD card (/dev/mmcblk*)" ;;
        usb)      echo "a USB or SCSI disk (/dev/sd*)" ;;
        nvme)     echo "an NVMe disk" ;;
        portable) echo "any device it is written to" ;;
        network)  echo "a network root, not a local device" ;;
        *)        echo "an unrecognised kind of device" ;;
    esac
}

# A rebuild clears its image directory, so a workspace whose glob matches nothing gets a placeholder row rather than reading as "not an image workspace".
image_workspace_scan() {  # one line per image: builder, workspace, path, bytes, mtime, tab-separated. Both builders write under the build directory targets/container.sh bind-mounts, so that is the whole scan
    local ws name f found

    [ -d "$WK_STORE/ws" ] || return 0

    for ws in "$WK_STORE/ws"/*; do
        [ -d "$ws" ] || continue
        name=$(basename "$ws")

        found=0
        for f in "$ws"/build/CrossToolChains/*/build/image/*.wic.xz; do
            [ -f "$f" ] || continue
            found=1
            printf 'yocto\t%s\t%s\t%s\t%s\n' "$name" "$f" \
                "$(file_bytes "$f")" "$(_scan_mtime "$f")"
        done
        case "$name" in
            yocto-*) [ "$found" = 1 ] || printf 'yocto\t%s\t-\t0\t-\n' "$name" ;;
        esac

        found=0
        for f in "$ws"/build/buildroot/*/output/images/*.img; do
            [ -f "$f" ] || continue
            found=1
            printf 'buildroot\t%s\t%s\t%s\t%s\n' "$name" "$f" \
                "$(file_bytes "$f")" "$(_scan_mtime "$f")"
        done
        case "$name" in
            buildroot-*) [ "$found" = 1 ] || printf 'buildroot\t%s\t-\t0\t-\n' "$name" ;;
        esac
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

# A fetched base, pinned by sha256 and resumable: this is gigabytes over WiFi.
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

image_slot_dir() { # <profile> <slot> -- host path. A slot is one built WebKit beside an image in the image's workspace; where it lives is a fact of the builder, so the caller loads the profile and IMG_BUILDER decides
    case "${IMG_BUILDER:-}" in
        buildroot) echo "$(wk_ws_dir "buildroot-$1")/build/buildroot/$1/output/wk-slots/$2" ;;
        yocto)     echo "$(wk_ws_dir "yocto-$1")/build/wk-slots/$2" ;;
        *) return 1 ;;
    esac
}

image_check_slot_name() {
    case "${1:-}" in
        ''|*[!a-zA-Z0-9_.-]*|-*|.*) die "slot '${1:-}' is not usable: letters, digits, '_', '.' and '-',
    not starting with '-' or '.'. It names a directory here and on the board." ;;
    esac
}
