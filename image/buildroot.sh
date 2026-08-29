# The buildroot builder: sourced by cmd/sysimage, dispatched on IMG_BUILDER.
# The same host/worker split the yocto lane uses: this file drives a
# container workspace and `image/buildroot-build.sh` runs inside it, from
# /opt/wk-tools so the two halves cannot skew.
#
# --- the recipe, and why each piece is what it is -----------------------------
#
#   tree        WebPlatformForEmbedded/buildroot, branch `wpe`, pinned 2020.02.
#               A fork, not upstream buildroot: the release-pinned `cog`
#               defconfigs only exist there.
#   host        Ubuntu 22.04, as its own workspace image (WK_SDK_IMAGE, same
#               mechanism image/yocto.sh uses): buildroot 2020.02 builds
#               2009-era tarballs, and a newer host stops compiling them.
#   downloads   BR2_DL_DIR/BR2_CCACHE_DIR under $WK_STORE/cache/buildroot
#               (lib/store.sh reserves it, targets/container.sh mounts it);
#               buildroot-build.sh reads them from its own environment, not
#               a command-line path -- a host path names nothing in the
#               container it runs in.
#   external    BR_EXTERNAL -> image/buildroot/external, a BR2_EXTERNAL tree
#               this repo owns. It carries the fix an arm64 build host needs:
#               host-python-2.7's bundled libffi cannot assemble
#               aarch64/sysv.S, applied from outside (external.mk) rather
#               than patching the vendor branch.
#   overlay     BR2_ROOTFS_OVERLAY, assembled by
#               image/buildroot/tailnet-overlay.sh <arch> <staging>. Only
#               world-readable regular files: the overlay is rsynced at
#               target-finalize as the build user, and a 0700 directory in
#               it fails the build with rsync error 23.
#   wifi        image/buildroot/wifi-overlay.sh <staging>, the same shape:
#               wk-wifi-join and the S-init line that spends the credential
#               the card seeds. TODO: whether the rpi3 defconfig compiles
#               wpa_supplicant at all is unverified until a build runs.
#   output      genimage -> output/images/$BR_IMAGE. The cog defconfigs'
#               filesystem output is tar-only, so buildroot-build.sh adds
#               BR2_TARGET_ROOTFS_EXT2 whenever the profile names a `.img`.
#               The completion line requires that file's mtime newer than
#               the run's own start, since a `make` with nothing to do can
#               report success too.
#   init        BusyBox, so no systemd units: install_units reads the init
#               out of the rootfs and refuses rather than writing units
#               nothing will start. The self-disarm and self-return are init
#               scripts generated from the board's driver at build time
#               (image/buildroot/fleet-overlay.sh); S99tailscale is the
#               tailnet join's.
#
# Owed work: docs/HANDOFF-ab-bench.md #3.

# WK_BUILDROOT_BASE overrides the build host image, pinned at 22.04 because
# buildroot 2020.02 needs it (above); overriding it without also repinning
# buildroot is a build that stops compiling, so this exists for testing a
# newer host ahead of that repin, not for routine use.
BUILDROOT_BASE_IMAGE="${WK_BUILDROOT_BASE:-docker.io/library/ubuntu:22.04}"

buildroot_workdir()  { echo "/src/WebKit/WebKitBuild/buildroot/$1"; }
buildroot_log()      { echo "$(wk_ws_dir "$1")/home/buildroot-$2.log"; }
buildroot_pidfile()  { echo "$(wk_ws_dir "$1")/home/buildroot-$2.pid"; }

# Tagged with the base's tag *and* a digest of the Containerfile, so
# editing the spec builds a new image rather than reusing a stale layer.
buildroot_ensure_image() {
    local base="$BUILDROOT_BASE_IMAGE" derived spec
    spec="$WK_ROOT/container/buildroot/Containerfile"
    derived="localhost/wk-buildroot-host:${base##*:}-$(sha256sum "$spec" | cut -c1-8)"

    if podman image exists "$derived" 2>/dev/null; then
        debug "workspace image $derived already built"
    else
        info "building the buildroot workspace image $derived (one layer on $base)"
        log  "  22.04 is the host the wiki recipe was driven on; a 2020 buildroot"
        log  "  does not survive a much newer one (container/buildroot/Containerfile)."
        podman build --build-arg "BASE=$base" \
            -t "$derived" -f "$spec" "$WK_ROOT/container/buildroot" \
            || die "could not build $derived.
    This runs on the host, where there is a network; if apt or the pull failed,
    that is a host-side problem and not the workspace boundary."
    fi

    WK_SDK_IMAGE="$derived"
    export WK_SDK_IMAGE
}

# Ensures the image *before* the existence check, so an edited Containerfile
# changes the wanted tag on every run rather than only on creation.
buildroot_ensure_ws() {
    local ws="$1"
    buildroot_ensure_image

    if [ "$(t_info "$ws")" = absent ]; then
        info "creating workspace '$ws' for the buildroot build"
        "$WK_ROOT/wk" new "$ws" || die "could not create workspace '$ws'"
    else
        local was
        was=$(podman container inspect "wk-$ws" --format '{{.ImageName}}' 2>/dev/null) || was=""
        if [ -n "$was" ] && [ "$was" != "$WK_SDK_IMAGE" ]; then
            die "workspace '$ws' was made from $was, and the spec now wants
    $WK_SDK_IMAGE. A container cannot be moved between images, so this build
    would use host packages container/buildroot/Containerfile no longer
    describes. Remake it -- the buildroot downloads are in the store and
    survive:
        wk rm $ws && wk sysimage build $IMG_PROFILE"
        fi
    fi
}

buildroot_build() {
    local profile="$1"; shift
    local detach="" stop="" dry="" ws=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --detach)    detach=1 ;;
            --stop)      stop=1 ;;
            --dry-run)   dry=1 ;;
            --keep-work|--no-import|--no-tailnet|--tailnet) ;;
            --workspace) ws="${2:-}"; shift ;;
            --stage)     shift ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done

    [ -n "${BR_DEFCONFIG:-}" ] || die "'$profile' names no defconfig, so there is nothing to
    build. Its configuration is $(image_config_file "$profile")."

    ws="${ws:-buildroot-$profile}"

    if [ -n "$stop" ]; then
        local pf; pf=$(buildroot_pidfile "$ws" image)
        [ -s "$pf" ] || die "no buildroot build is running in '$ws'"
        t_exec "$ws" "kill $(cat "$pf")" >/dev/null 2>&1 || true
        info "asked the build in '$ws' to stop"
        return 0
    fi

    # Memory-sized, then capped near the wiki recipe's own `-j 8`: a WPE
    # compile here runs roughly 2 GB a job, and 2009-era tarballs are where
    # broken parallel rules live.
    local jobs overlay_arch overlay_wifi dl cc
    jobs=$(WK_MB_PER_JOB=2048 WK_MAX_JOBS=16 build_jobs)
    overlay_arch="${BR_OVERLAY_TAILSCALE:-}"
    overlay_wifi=""
    _image_wants_wifi "${IMG_MACHINE:-}" && overlay_wifi=1
    # Display only, for the dry run; not passed to buildroot-build.sh, whose
    # own (container-side) environment already has the real path.
    dl="$WK_STORE/cache/buildroot/dl"
    cc="$WK_STORE/cache/buildroot/ccache"

    if [ -n "$dry" ]; then
        cat >&2 <<EOF
would build image $profile (builder: buildroot)
  for machine  ${IMG_MACHINE:-none} (${IMG_ARCH:-?})
  tree         $BR_TREE_URL @ ${BR_TREE_COMMIT:-${BR_TREE_BRANCH:-HEAD}}
  defconfig    $BR_DEFCONFIG$([ "${BR_EXTERNAL:-0}" = 1 ] && printf " (plus this repo's BR2_EXTERNAL)")
  workspace    $ws ($(t_info "$ws" 2>/dev/null || echo absent))
  host image   $BUILDROOT_BASE_IMAGE + container/buildroot/Containerfile
  jobs         -j$jobs (memory-sized at 2048 MB/job)
  overlay      ${overlay_arch:-none -- this image would join no tailnet}
  wifi overlay $([ -n "$overlay_wifi" ] && echo "wk-wifi-join (image/buildroot/wifi-overlay.sh); the card carries the credential" || echo "none -- ${IMG_MACHINE:-this board} has a cable")
  DL_DIR       $dl ($(du -sh "$dl" 2>/dev/null | cut -f1 || echo "not created yet")) -- BR2_DL_DIR in the container
  CCACHE_DIR   $cc ($(du -sh "$cc" 2>/dev/null | cut -f1 || echo "not created yet")) -- BR2_CCACHE_DIR in the container
  output       in the workspace, under output/images (there is no store)
EOF
        log "dry run -- nothing was built."
        return 0
    fi

    buildroot_ensure_ws "$ws"
    buildroot_refuse_busy "$ws"

    info "building $profile in '$ws'"
    _buildroot_run "$ws" image "$detach" "hours" "$jobs" -- \
        "$(t_tools "$ws")/image/buildroot-build.sh" \
            --name "$profile" \
            --tree-url "$BR_TREE_URL" \
            ${BR_TREE_BRANCH:+--tree-branch "$BR_TREE_BRANCH"} \
            ${BR_TREE_COMMIT:+--tree-commit "$BR_TREE_COMMIT"} \
            --defconfig "$BR_DEFCONFIG" \
            --external "${BR_EXTERNAL:-0}" \
            ${BR_IMAGE:+--image "$BR_IMAGE"} \
            --jobs "$jobs" \
            ${overlay_arch:+--overlay-arch "$overlay_arch"} \
            ${overlay_wifi:+--overlay-wifi 1} \
            ${BR_ROOTFS_SIZE:+--rootfs-size "$BR_ROOTFS_SIZE"} \
        || return 1
    [ -n "$detach" ] || info "built $profile in '$ws'"
}

# One job per workspace: an image build and a slot build both move the
# checkout and the tree's output under them. (What the machine as a whole
# can fit is build_admit's question, lib/resources.sh.) Evidence is the pid
# a detached stage leaves, asked of the workspace (ws_busy_reason); a dead
# pid is a finished job, whatever its log says.
buildroot_refuse_busy() {
    local ws="$1" busy
    busy=$(ws_busy_reason "$ws") || return 0
    [ -n "$busy" ] || return 0
    die "a build is still running in '$ws': $busy.
    One job per workspace: both move the checkout and the tree's output.
    Follow it:  tail -f $(wk_ws_dir "$ws")/home/${busy%% *}.log
    Stop it:    wk sysimage build $IMG_PROFILE --stop"
}

# Start a stage's script in the workspace and, unless detached, wait for its
# completion line. `<stage>` names the log and pid files (buildroot-<stage>.*
# in the workspace's home); the script ends with "stage '<stage>' done" or it
# did not finish, whatever its exit status says.
_buildroot_run() { # <ws> <stage> <detach> <how long> <jobs> -- <script> <args...>
    local ws="$1" stage="$2" detach="$3" howlong="$4" jobs="$5"; shift 5
    [ "${1:-}" = -- ] && shift
    local log pid
    log=$(buildroot_log "$ws" "$stage"); pid=$(buildroot_pidfile "$ws" "$stage")
    # Sized against the machine's other builds, and on its books while it
    # runs (lib/resources.sh): the pid it leaves is the workspace's own.
    build_admit "the $stage build" "$jobs"
    build_record "wk sysimage $stage $ws" "$jobs" "$(( jobs * 2048 ))" "ws:$ws:buildroot-$stage.pid"
    # Truncated, not unlinked: `tail -f` follows an inode, so deleting the
    # log leaves an existing follower watching a file nobody writes to.
    : > "$log"; rm -f "$pid"
    log "  log: $log"

    t_spawn "$ws" "$(t_home "$ws")/$(basename "$log")" \
                  "$(t_home "$ws")/$(basename "$pid")" "$@" \
        || die "could not start the $stage build in '$ws'"

    local i=0
    while [ ! -s "$pid" ]; do
        i=$((i + 1))
        [ "$i" -gt 50 ] && die "the $stage build did not start in '$ws' (no pid after 5s)
$(sed 's/^/    /' "$log" 2>/dev/null | tail -5)"
        sleep 0.1
    done

    if [ -n "$detach" ]; then
        info "running detached in '$ws' -- this end can go away"
        log  "  follow:  tail -f $log"
        log  "  it stays in the workspace that built it (wk help), and"
        log  "  'wk sysimage ls' finds it once it is there"
        return 0
    fi

    info "waiting for the $stage build ($howlong; --detach returns instead)"
    # The pid is the workspace's (its own pid namespace); asked of the
    # workspace, never of this host.
    while t_exec "$ws" kill -0 "$(cat "$pid" 2>/dev/null)" >/dev/null 2>&1; do sleep 10; done
    grep -q "stage '$stage' done" "$log" \
        || die "the $stage build in '$ws' failed. Last lines:
$(tail -20 "$log" | sed 's/^/    /')"
    return 0
}

# --- slots: one image, several WebKits ------------------------------------------
# `wk sysimage webkit <profile> --commit <sha> --slot <name>` builds one
# WebKit commit with the image's own wpewebkit package, into
# output/wk-slots/<name>/ in the image's workspace (image/buildroot-webkit.sh
# has the recipe; image_slot_dir, lib/image.sh, the location). A board
# running the image carries any number of slots beside the WebKit it shipped
# with, and `wk pi bench --ab` alternates between two of them with no reflash
# and no reboot.
buildroot_webkit() {
    local profile="$1"; shift
    local detach="" dry="" ws="" commit="" slot=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --detach)    detach=1 ;;
            --dry-run)   dry=1 ;;
            --workspace) ws="${2:-}"; shift ;;
            --commit)    commit="${2:-}"; shift ;;
            --slot)      slot="${2:-}"; shift ;;
            *) die "usage: wk sysimage webkit <profile> --commit <sha> --slot <name> [--detach] [--dry-run]; unknown option: $1" ;;
        esac
        shift
    done
    [ -n "$commit" ] && [ -n "$slot" ] \
        || die "usage: wk sysimage webkit <profile> --commit <sha> --slot <name> [--detach] [--dry-run]; see wk sysimage -h"
    image_check_slot_name "$slot"
    case "$commit" in *[!0-9a-f]*) die "--commit takes a full sha (40 hex digits), got '$commit'" ;; esac
    [ "${#commit}" -eq 40 ] || die "--commit takes a full sha (40 hex digits), got '$commit'.
    'git rev-parse' in the mirror or the workspace expands a short one."

    ws="${ws:-buildroot-$profile}"
    local image slotdir jobs
    image="$(wk_ws_dir "$ws")/build/buildroot/$profile/output/images/${BR_IMAGE:-sdcard.img}"
    slotdir=$(image_slot_dir "$profile" "$slot")
    # WebKit's own build parallelises well and links large: memory-sized at
    # 2 GB a job, capped where the link steps stop gaining.
    jobs=$(WK_MB_PER_JOB=2048 WK_MAX_JOBS=64 build_jobs)

    if [ -n "$dry" ]; then
        cat >&2 <<EOF
would build a WebKit slot for image $profile (builder: buildroot)
  commit       $commit
  slot         $slot -> $slotdir
  workspace    $ws ($(t_info "$ws" 2>/dev/null || echo absent))
  image        $([ -f "$image" ] && echo "$image" || echo "NOT BUILT -- the real run refuses here (wk sysimage build $profile)")
  toolchain    the image's own (output/host/share/buildroot/toolchainfile.cmake),
               options from the tree's WPEWEBKIT_CONF_OPTS, plus --build-id per slot
  jobs         -j$jobs (memory-sized at 2048 MB/job)
  existing     $([ -f "$slotdir/slot.json" ] \
                    && echo "slot '$slot' holds $(python3 "$WK_ROOT/lib/wkslot.py" get "$slotdir/slot.json" commit | cut -c1-12) -- rebuilt incrementally in the same build directory" \
                    || echo "none")
EOF
        log "dry run -- nothing was built."
        return 0
    fi

    [ "$(t_info "$ws")" != absent ] \
        || die "no workspace '$ws', so there is no image to build against.
    Build the image first:  wk sysimage build $profile"
    [ -f "$image" ] \
        || die "'$ws' has no finished image ($image).
    A slot is built against the image's toolchain, so the image comes first:
        wk sysimage build $profile"
    buildroot_refuse_busy "$ws"

    info "building WebKit $(printf '%s' "$commit" | cut -c1-12) into slot '$slot' of $profile (in '$ws')"
    _buildroot_run "$ws" "webkit-$slot" "$detach" "tens of minutes" "$jobs" -- \
        "$(t_tools "$ws")/image/buildroot-webkit.sh" \
            --name "$profile" --commit "$commit" --slot "$slot" --jobs "$jobs" \
        || return 1
    if [ -z "$detach" ]; then
        [ -f "$slotdir/slot.json" ] || die "the build reported done but left no $slotdir/slot.json"
        info "slot '$slot' of $profile holds $(printf '%s' "$commit" | cut -c1-12)"
        log  "  $slotdir"
        log  "  next:  wk pi deploy $profile <machine> --slot $slot"
    fi
}
