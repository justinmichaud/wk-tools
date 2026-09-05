# Downloads a prebuilt image a profile pins by FET_URL and FET_SHA256 (FET_XZ marks xz, FET_NOTE describes it).

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
