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
