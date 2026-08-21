# The fetch builder: a pinned image somebody else built, brought into the store
# as it is.
#
# Sourced by cmd/sysimage. The fourth builder, and by far the smallest, because
# it does not build anything: it downloads a published artifact, checks it
# against a pinned hash, and stores it. What that buys is everything downstream
# -- `wk sysimage ls`, `show`, `write` with all its refusals, `image_verify`, the
# block-map write path, `wk gc` pruning superseded copies -- for an image this
# repo could not sensibly produce itself.
#
# What it is for today: Jumpdrive, the PinePhone's service image. It boots from a
# card and exports the phone's internal storage as USB mass storage, which is
# how the phone's eMMC gets written at all -- and once it is running, the eMMC
# *is* a removable disk attached to whatever the phone is plugged into, so
# flashing it is `wk sysimage write <bridge image> --disk <machine>:<device>`
# with nothing new involved.
#
# It is also the reference that settles an argument. A card written from a
# builder's own output that does not boot is either a bad image or a device that
# will not boot cards, and those are very different problems; a known-good image
# from the device's own community tells you which one you have.
#
# A fetch profile sets:
#
#   FET_URL       where it is published, pinned to a release rather than latest
#   FET_SHA256    ...and pinned by content, because a release tag can be moved
#   FET_XZ        1 if the artifact is xz-compressed and should be unpacked
#   FET_NOTE      one line: what it does, for `wk sysimage show`

fetch_dry_run() {
    local id="$1"
    cat >&2 <<EOF
would fetch image $id
  profile     $IMG_PROFILE (fetch builder -- downloaded, not built)
  from        $FET_URL
              $([ -f "$(image_cache_dir)/$(basename "$FET_URL")" ] && echo "cached" || echo "not cached -- would download")
  pinned to   $FET_SHA256
  what it is  $FET_NOTE
  into        $(image_dir "$id")
EOF
    log "dry run -- nothing was fetched."
}

fetch_build() {
    local profile="$1"; shift
    local dry=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1 ;;
            *) die "unknown option: $1
    The fetch builder takes --dry-run." ;;
        esac
        shift
    done

    BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local id="$profile-$(date -u +%Y%m%dT%H%M%SZ)"

    [ -n "$dry" ] && { fetch_dry_run "$id"; return 0; }

    image_lock

    local r
    for r in $(image_rubble); do
        warn "destroying rubble from an interrupted build: $r"
        rm -rf "$(image_dir "$r")"
    done

    # The same cache, and the same pin check, the distro builder's base download
    # uses: one place that knows how to fetch something by content.
    local src; src=$(image_fetch_base "$FET_URL" "$FET_SHA256")

    local dir; dir=$(image_dir "$id"); mkdir -p "$dir"
    if [ "${FET_XZ:-0}" = 1 ]; then
        info "unpacking $(basename "$src")"
        xz -dc "$src" > "$dir/disk.img" || die "$(basename "$src") is not valid xz"
    else
        cp "$src" "$dir/disk.img" || die "could not copy $(basename "$src") into the store"
    fi

    info "hashing the image"
    local sha; sha=$(sha256sum "$dir/disk.img" | cut -d' ' -f1)

    # Written last, which is the publishing gate for every builder here.
    cat > "$(image_manifest "$id")" <<EOF
id=$id
profile=$IMG_PROFILE
builder=fetch
device=${FET_DEVICE:-}
arch=$IMG_ARCH
source_url=$FET_URL
source_sha256=$FET_SHA256
note=$FET_NOTE
built=$BUILT
built_by=$(hostname)
wk_tools=$(git -C "$WK_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
disk_bytes=$(file_bytes "$dir/disk.img")
disk_sha256=$sha
EOF

    info "fetched $id  ($(human_bytes "$(file_bytes "$dir/disk.img")"))"
    log  "  write it:  wk sysimage write $id --disk <machine>:<device>"
}
