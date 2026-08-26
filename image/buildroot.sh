# The buildroot builder: sourced by cmd/sysimage, dispatched on IMG_BUILDER.
#
# Not written yet, and this file exists to say so in the first second of
# `wk sysimage build` rather than let a configuration that cannot be built look
# like one that can. The configurations are real -- image/configs holds twelve
# buildroot ones -- and two of them name a defconfig that exists upstream today;
# what is missing is the mechanism between the config file and an image.
#
# --- what the scouting run established ---------------------------------------
#
# A build done by hand outside `wk`, because buildroot 2020.02 on a modern host
# is the unknown and the lane should be written around what works. It produced
# a bootable `sdcard.img` for the rpi3, with tailscale in it. So the recipe below
# is recorded evidence, not a design sketch -- but it has never been run *through
# this file*, and until it has, claiming otherwise in code would be the worst of
# the three options.
#
#   tree        WebPlatformForEmbedded/buildroot, branch `wpe`, pinned 2020.02.
#               A fork, not upstream buildroot: the release-pinned `cog`
#               defconfigs only exist there.
#   host        Ubuntu 22.04, as its own workspace image. buildroot 2020.02
#               builds 2009-era tarballs and the further the host moves the more
#               of them stop compiling.
#
#               22.04 rather than a guess: it is the host the wiki recipe
#               ("Building WPEWebKit for 32-bit Raspberry Pi 3 (Buildroot DRM
#               config)") was driven on, and it produced a booting image. Its
#               four added packages -- file, cpio, bc, libncurses-dev -- are in
#               container/buildroot/Containerfile with the rest of buildroot's
#               documented host requirements.
#
#               An image builds in *its own workspace*, and a workspace may be
#               made from a different base than the WebKit SDK -- the mechanism
#               is WK_SDK_IMAGE and image/yocto.sh has used it since it was
#               written. So this needs no container inside a container and no
#               runtime in the SDK image.
#   jobs        -j6 measured right on a 19 GB VM: a WPE compile is ~2 GB.
#   downloads   BR2_DL_DIR and BR2_CCACHE_DIR under $WK_STORE/cache/buildroot,
#               which lib/store.sh already reserves and targets/container.sh
#               already exports. image_build_locations already declares it, so
#               `wk gc` already reclaims it.
#   external    BR_EXTERNAL -> image/buildroot/external, a BR2_EXTERNAL tree this
#               repository owns. It carries the one fix the scouting run needed: host-python-2.7's bundled 2013-era
#               libffi cannot assemble aarch64/sysv.S, so the build dies at
#               `sharedmods` on an arm64 build host. An architecture problem,
#               not an old-distro one -- on x86_64 that file is never compiled,
#               which is why this tree always built on moose. Buildroot ships
#               host-libffi and gives the *target* python --with-system-ffi, and
#               at the pin never joins the two for the host build; upstream does
#               join them in a later release, so the tree applies that change
#               from outside rather than inventing one. TODO: never run through
#               a build -- what is checked is the make semantics it depends on
#               (external.mk says which, and why an appended dependency is not
#               enough on its own).
#   overlay     BR2_ROOTFS_OVERLAY, assembled by
#               image/buildroot/tailnet-overlay.sh <arch> <staging>. This part is
#               written and verified. Only world-readable regular files: the
#               overlay is rsynced at target-finalize as the build user, and a
#               0700 directory in it failed the build with rsync error 23.
#   output      genimage -> output/images/sdcard.img. The cog defconfigs'
#               filesystem output is tar-only -- no rootfs.ext4 -- so the board's
#               own genimage config had nothing to assemble until that was fixed.
#   init        BusyBox, so no systemd units: install_units already reads the
#               init out of the rootfs and refuses rather than writing units
#               nothing will start. The S99tailscale script is the equivalent,
#               and the BusyBox halves of the self-return watchdog and the
#               self-disarm are still owed (docs/TESTING.md).
#
# --- what is owed, in order --------------------------------------------------
#
#   1. this function: clone-and-pin the tree per configuration, apply the
#      defconfig, write the overlay, build in the workspace, and leave
#      output/images/$BR_IMAGE where it lands -- there is no store to import it
#      into (wk help images). Container-only, and refused elsewhere for the
#      reasons yocto_build gives.
#   2. the ten configurations whose defconfig does not exist upstream -- every
#      board but the rpi3, and every WebKit/WebKit release. Each says what it
#      needs in its own CFG_NEEDS, so they refuse individually and correctly
#      already; deriving those defconfigs is a separate piece of work per board.

BUILDROOT_BASE_IMAGE="${WK_BUILDROOT_BASE:-docker.io/library/ubuntu:22.04}"

buildroot_workdir()  { echo "/src/WebKit/WebKitBuild/buildroot/$1"; }
buildroot_log()      { echo "$(wk_ws_dir "$1")/home/buildroot-$2.log"; }
buildroot_pidfile()  { echo "$(wk_ws_dir "$1")/home/buildroot-$2.pid"; }

# The workspace image, built if it is not already there.
#
# The same shape as yocto_ensure_image and for the same reasons: one layer on a
# pinned base, tagged with the base's tag *and* a digest of the Containerfile so
# that editing the spec builds a new image rather than silently reusing a layer
# that no longer matches it.
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

# Make sure there is a workspace to build in, made from that image.
#
# Unconditionally ensures the image *before* the existence check, which is the
# trap yocto_ensure_ws records: doing it only on creation means an edited
# Containerfile changes the wanted tag while every run goes on using a container
# made from the old one.
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

    # Memory-sized rather than core-sized, then capped.
    #
    # The scouting run measured a WPE compile in this tree at roughly 2 GB a
    # job, which is what makes -j<cores> the wrong answer on a machine with
    # more cores than gigabytes-over-two -- the same reasoning the yocto lane
    # applies to WebKit.
    #
    # The cap is the wiki's number, near enough: that recipe drove `make -j 8`
    # and produced a booting image, and this is a 2020 buildroot building
    # 2009-era tarballs, which is where packages with broken parallel rules
    # live. Going far past the only value with evidence behind it risks losing
    # an hours-long build to a race in somebody else's Makefile, and the cores
    # are not the scarce thing here anyway.
    local jobs overlay_arch dl cc
    jobs=$(WK_MB_PER_JOB=2048 WK_MAX_JOBS=16 build_jobs)
    overlay_arch="${BR_OVERLAY_TAILSCALE:-}"
    dl="$WK_STORE/cache/buildroot/downloads"
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
  DL_DIR       $dl
  output       in the workspace, under output/images (there is no store)
EOF
        log "dry run -- nothing was built."
        return 0
    fi

    buildroot_ensure_ws "$ws"
    mkdir -p "$dl" "$cc"

    local log pid
    log=$(buildroot_log "$ws" image); pid=$(buildroot_pidfile "$ws" image)
    # Truncated, not unlinked: `tail -f` follows an inode, so deleting the log
    # leaves an existing follower watching a file nobody writes to.
    : > "$log"; rm -f "$pid"

    info "building $profile in '$ws'"
    log  "  log: $log"

    t_spawn "$ws" "$(t_home "$ws")/$(basename "$log")" \
                  "$(t_home "$ws")/$(basename "$pid")" \
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
            --dl-dir "$dl" \
            --ccache-dir "$cc" \
            ${BR_ROOTFS_SIZE:+--rootfs-size "$BR_ROOTFS_SIZE"} \
        || die "could not start the build in '$ws'"

    local i=0
    while [ ! -s "$pid" ]; do
        i=$((i + 1))
        [ "$i" -gt 50 ] && die "the build did not start in '$ws' (no pid after 5s)
$(sed 's/^/    /' "$log" 2>/dev/null | tail -5)"
        sleep 0.1
    done

    if [ -n "$detach" ]; then
        info "running detached in '$ws' -- this end can go away"
        log  "  follow:  tail -f $log"
        log  "  it stays in the workspace that built it (wk help images), and"
        log  "  'wk sysimage ls' finds it once it is there"
        return 0
    fi

    info "waiting for the build (hours; --detach returns instead)"
    while kill -0 "$(cat "$pid" 2>/dev/null)" 2>/dev/null; do sleep 10; done
    grep -q "stage 'image' done" "$log" \
        || die "the build in '$ws' failed. Last lines:
$(tail -20 "$log" | sed 's/^/    /')"
    info "built $profile in '$ws'"
}
