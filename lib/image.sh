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

# Refuse to leave a card whose system cannot boot from the device it is on:
# without this the discovery happens on a headless board that loaded a kernel
# and then couldn't find `/`. The spec is the one the card itself names
# (disk_root_spec, boot/disk.sh), read after every edit that could change it.
#   image_check_root <root-spec> <device> <what-it-is>
# `portable`, `network` and `unknown` all pass: the first works anywhere.
image_check_root() {
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
# both builders write under /src/WebKit/WebKitBuild, which is ws/<name>/build
# on this side (targets/container.sh bind-mounts it there, out of the
# overlay), so the scan is that directory and nothing else.
image_workspace_scan() {
    local ws name f

    [ -d "$WK_STORE/ws" ] || return 0

    for ws in "$WK_STORE/ws"/*; do
        [ -d "$ws" ] || continue
        name=$(basename "$ws")

        # builder: yocto -- bitbake writes the wic beside the rootfs tarball
        # (yocto_image_dir, image/yocto.sh); globbed, not profile-derived.
        for f in "$ws"/build/CrossToolChains/*/build/image/*.wic.xz; do
            [ -f "$f" ] || continue
            printf 'yocto\t%s\t%s\t%s\t%s\n' "$name" "$f" \
                "$(file_bytes "$f")" "$(_scan_mtime "$f")"
        done

        # builder: buildroot -- genimage assembles into output/images
        # (BR_IMAGE names the file).
        for f in "$ws"/build/buildroot/*/output/images/*.img; do
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

# --- WebKit slots ------------------------------------------------------------
# A slot is one built WebKit beside an image, in the image's workspace, built
# the way that builder builds WebKit (buildroot: its own package from an
# override source; yocto: WebKit's build-webkit --cross-target) and deployed
# to a board by `wk pi deploy`. Where it lives is a fact of the builder, kept
# here so cmd/pi, cmd/ab and `wk sysimage ls` never spell it twice. The
# profile is loaded by the caller (IMG_BUILDER is what decides).
image_slot_dir() { # <profile> <slot> -- host path
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
