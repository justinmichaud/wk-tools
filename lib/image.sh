command -v image_config_names >/dev/null 2>&1 || . "$WK_ROOT/image/profiles.sh"

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

image_spec_profile() { printf '%s' "${1%%@*}"; }                            # <profile>[@<machine>]: what is built
image_spec_machine() { case "$1" in *@?*) printf '%s' "${1#*@}" ;; esac; }  # ...and which machine holds the lane, for one nothing holds yet

image_spec_target() { # <machine> -- the target its lanes live on; this machine's own are on its default
    if [ "$1" = "$(wk_machine_name)" ]; then default_target; else printf '%s' "$1"; fi
}

# A lane is the image workspace, and everything a build leaves is under it: the checkout, the build directory, the slots and the collections. Every path below is derived from the lane's name and none from the profile's, so a second lane of one profile -- named with `--workspace` -- keeps its own of each.
image_lane_ws() { # <profile>[@<machine>] -- the workspace, nothing when the builder has no lane
    local profile builder
    profile=$(image_spec_profile "$1")
    builder=$( image_profile_load "$profile" >/dev/null 2>&1 && printf '%s' "${IMG_BUILDER:-}" ) || builder=""   # a name no configuration answers to is a question with no lane in it: image_profile_load dies on one, and that die is this subshell's
    case "$builder" in
        yocto|buildroot) printf '%s-%s' "$builder" "$profile" ;;
    esac
}

image_lane_profile() { # <workspace> -- the profile its lane builds, by longest match, so a lane carrying an arm's suffix still names it
    local rest name best=""
    case "$1" in
        yocto-*)     rest="${1#yocto-}" ;;
        buildroot-*) rest="${1#buildroot-}" ;;
        *) return 1 ;;
    esac
    for name in $(image_config_names); do
        case "$rest" in
            "$name"|"$name"-*) [ "${#name}" -gt "${#best}" ] && best="$name" ;;
        esac
    done
    [ -n "$best" ] || return 1
    printf '%s' "$best"
}

# What a command's own arguments say the lane is: `--workspace <name>` when one is given, the profile's own lane otherwise. Every command that acts on a lane asks this, so `--workspace` means the same thing in each and the dispatcher routes them all alike (`name=derived`, wk).
image_lane_arg() { # <profile>[@<machine>] <the rest of the arguments> -- nothing when they name no lane
    local profile="${1:-}" a prev=""
    shift 2>/dev/null || return 0
    case "$profile" in ''|-*) return 0 ;; esac
    for a in ${@+"$@"}; do
        case "$prev" in --workspace) printf '%s' "$a"; return 0 ;; esac
        case "$a" in --workspace=?*) printf '%s' "${a#*=}"; return 0 ;; esac
        prev="$a"
    done
    image_lane_ws "$profile"
}

image_slot_dir() { # <lane workspace> <slot> -- host path. A slot is one built WebKit beside an image in the lane that built it; where it sits under the lane is a fact of the builder, which the lane's name says
    local profile
    profile=$(image_lane_profile "$1") || return 1
    case "$1" in
        buildroot-*) echo "$(wk_ws_dir "$1")/build/buildroot/$profile/output/wk-slots/$2" ;;
        yocto-*)     echo "$(wk_ws_dir "$1")/build/wk-slots/$2" ;;
        *) return 1 ;;
    esac
}

IMAGE_PGO_SUBDIR=wk-pgo   # the collection the next build reads, beside the slots and in the same bind mount: the host writes it (`wk pi bench --pgo`) and the builder reads it back through the cross toolchain (image/yocto-build.sh's pgo-mix)
image_pgo_dir()    { echo "$(wk_ws_dir "$1")/build/$IMAGE_PGO_SUBDIR/$2"; }              # <lane workspace> <slot>, host side
image_pgo_dir_in() { echo "/src/WebKit/WebKitBuild/$IMAGE_PGO_SUBDIR/$1"; }              # <slot>, as the builder sees it (targets/container.sh binds the one at the other)

IMAGE_PGO_INSTR_SUFFIX=-instr   # the instrumented slot of a cycle is the measured slot's name and this, named here because image/pgo.sh builds and deploys it and cmd/pi collects into the measured slot's directory
image_pgo_instr_slot() { printf '%s%s' "$1" "$IMAGE_PGO_INSTR_SUFFIX"; }   # <slot>
image_pgo_measured_slot() { printf '%s' "${1%$IMAGE_PGO_INSTR_SUFFIX}"; }  # <instrumented slot>

image_lane_machine() { # <lane workspace> <machine, when a spec named one> -- which machine holds the lane
    if [ -n "${2:-}" ]; then printf '%s' "$2"; return 0; fi
    local t; t=$(ws_target "$1") || return $?
    case "$t" in
        container|local) wk_machine_name ;;
        *) printf '%s' "$t" ;;
    esac
}

image_build_resource() { printf 'machine:%s' "$1"; }   # <machine> -- what the scheduler serialises a build by. One machine builds one thing at a time, whichever lane it is for, so the machine is the whole resource; two machines build at once (lib/sched.py, and build_admit for the builds no one plan knows about)

image_holds_predicate() { # <profile>[@<machine>] <lane workspace> [the rest of `holds`'s options] -- how a step asks whether it is done, as the shell snippet lib/sched.py runs. `wk sysimage holds` is routed to the machine holding the lane, so the question reaches the machine that would do the work rather than the one that drew the graph
    local spec="$1" lane="$2"; shift 2
    printf '[ "$(wk sysimage holds %s --workspace %s%s)" = yes ]' \
        "$spec" "$lane" "${*:+ $*}"
}

image_lane_here() { # <profile>[@<machine>] -- refuse when the spec named another machine and the command is still running on this one, which means the dispatcher found no wk over there to hand it to
    local on; on=$(image_spec_machine "$1")
    [ -n "$on" ] && [ "$on" != "$(wk_machine_name)" ] || return 0
    die "'$1' names machine '$on' and this is $(wk_machine_name). The command was not
    handed over, so nothing there answered for a store of its own:
        wk remote setup $on
    'wk status' names the machines this fleet has."
}

# Which of the builds a slot is, in the words a reader needs. On a 2.52 profile the same command produces an instrumented slot and a measured one, and a number taken from the first is not this engine's (image/pgo.sh) -- so a build that is running says which it is, not only that one is running.
image_config_word() { # <cross config>
    case "${1:-}" in
        wpe-cross-pgo-collect) printf 'instrumented, to collect a profile from -- not a measurement' ;;
        wpe-cross-pgo-use)     printf 'the measured build, against the mixed profile' ;;
        wpe-cross)             printf 'built without a profile' ;;
        '')                    printf 'the image itself' ;;
        *)                     printf '%s' "$1" ;;
    esac
}

image_build_subject() { # <lane workspace> <stage> <slot> <commit> <cross config> -- one line saying what this build is of, recorded on its task and printed by `wk status`
    case "${2:-}" in
        webkit)  printf 'slot %s in %s at %.12s -- %s' "$3" "$1" "$4" "$(image_config_word "$5")" ;;
        pgo-mix) printf "mixing slot %s's collection in %s" "$3" "$1" ;;
        *)       printf '%s stage of %s' "${2:-build}" "$1" ;;
    esac
}

image_check_slot_name() {
    case "${1:-}" in
        ''|*[!a-zA-Z0-9_.-]*|-*|.*) die "slot '${1:-}' is not usable: letters, digits, '_', '.' and '-',
    not starting with '-' or '.'. It names a directory here and on the board." ;;
    esac
}
