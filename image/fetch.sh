# The fetch builder: downloads a pinned image somebody else built, verified
# against a pinned hash (image_fetch_base, lib/image.sh). Used for Jumpdrive,
# the PinePhone's service image, which boots from a card and exports the
# phone's eMMC as USB mass storage -- how the eMMC gets written at all.
# A fetch profile sets:
#
#   FET_URL       where it is published, pinned to a release rather than latest
#   FET_SHA256    pins it by content too, since a release tag can be moved
#   FET_XZ        1 if the artifact is xz-compressed (decompressed on write either way)
#   FET_NOTE      one line: what it does

fetch_build() {
    local profile="$1"; shift
    local dry=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1 ;;
            *) die "usage: wk sysimage build $profile [--dry-run]; unknown option: $1" ;;
        esac
        shift
    done

    if [ -n "$dry" ]; then
        cat >&2 <<EOF
would fetch image $IMG_PROFILE
  from        $FET_URL
              $([ -f "$(image_cache_dir)/$(basename "$FET_URL")" ] && echo "cached" || echo "not cached -- would download")
  pinned to   $FET_SHA256
  what it is  $FET_NOTE
EOF
        log "dry run -- nothing was fetched."
        return 0
    fi

    local src; src=$(image_fetch_base "$FET_URL" "$FET_SHA256")

    info "fetched $(basename "$src")  ($(human_bytes "$(file_bytes "$src")"))"
    log  "  write it:  wk sysimage write --from $src --disk <machine>:<device>"
}
