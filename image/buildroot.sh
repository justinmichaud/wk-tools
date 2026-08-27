# The buildroot builder: sourced by cmd/sysimage, dispatched on IMG_BUILDER.
#
# The same host/worker split the yocto lane uses (image/yocto.sh /
# image/yocto-build.sh): this file drives a container workspace and
# `image/buildroot-build.sh` is what runs inside it, from /opt/wk-tools so the
# two halves cannot skew. A configuration that cannot be built yet refuses in
# the first second (CFG_NEEDS, cmd_build) rather than hours into a compile.
#
# --- the recipe, and why each piece is what it is -----------------------------
#
# What follows was first run by hand outside `wk`, because buildroot 2020.02 on
# a modern host was the unknown and the lane was written around what works: it
# produced a bootable `sdcard.img` for the rpi3, with tailscale in it. The
# recipe below is that evidence, carried into this file and buildroot-build.sh.
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
#               already mounts and exports -- buildroot-build.sh reads them from
#               its own environment rather than being handed a path on the
#               command line, the same reason yocto-build.sh reads DL_DIR and
#               SSTATE_DIR that way: a host path hits nothing inside the
#               container it runs in, and the container's own environment is
#               already the one true copy of where the mount landed.
#               image_build_locations already declares the directory, so
#               `wk gc` already reclaims it.
#   external    BR_EXTERNAL -> image/buildroot/external, a BR2_EXTERNAL tree this
#               repository owns. It carries the one fix a build needs: host-
#               python-2.7's bundled 2013-era libffi cannot assemble
#               aarch64/sysv.S, so the build dies at `sharedmods` on an arm64
#               build host. An architecture problem, not an old-distro one --
#               on x86_64 that file is never compiled, which is why this tree
#               always built on moose. Buildroot ships host-libffi and gives the
#               *target* python --with-system-ffi, and at the pin never joins
#               the two for the host build; upstream does join them in a later
#               release, so the tree applies that change from outside
#               (external.mk) rather than patching somebody else's vendor
#               branch or sedding its Makefile.
#   overlay     BR2_ROOTFS_OVERLAY, assembled by
#               image/buildroot/tailnet-overlay.sh <arch> <staging>. Only
#               world-readable regular files: the overlay is rsynced at
#               target-finalize as the build user, and a 0700 directory in it
#               failed a build with rsync error 23.
#   wifi        image/buildroot/wifi-overlay.sh <staging>, the same shape. It
#               carries no binary (wpa_supplicant is a package the defconfig
#               selects, not a downloaded static build), only wk-wifi-join and
#               the S-init line that spends the credential the card seeds.
#               TODO: the rpi3 defconfig is the WPE fork's own; whether it
#               compiles wpa_supplicant at all is unverified until a build runs.
#   output      genimage -> output/images/$BR_IMAGE. The cog defconfigs'
#               filesystem output is tar-only -- no rootfs.ext4 -- so the
#               board's own genimage config has nothing to assemble unless
#               buildroot-build.sh adds BR2_TARGET_ROOTFS_EXT2, which it does
#               whenever the profile names a `.img`. The completion line is
#               conditioned on that file's mtime being newer than the run's own
#               start, the same rule yocto-build.sh applies to its image
#               directory: a helper (or here, a `make` with nothing to do)
#               reporting success proves nothing on its own.
#   init        BusyBox, so no systemd units: install_units already reads the
#               init out of the rootfs and refuses rather than writing units
#               nothing will start. The S99tailscale script is the equivalent.
#               TODO: the BusyBox halves of the self-return watchdog and the
#               self-disarm are still owed.
#
# --- what is owed, in order --------------------------------------------------
#
#   1. a real build, through this file: the mechanism below has never been run
#      end to end, only checked by dry run and by reading the make semantics
#      the external tree depends on.
#   2. the nine configurations whose defconfig does not exist upstream and is
#      not yet derived here either -- every board/release combination but
#      wpewebkit-2.38 and wpewebkit-2.46 on the rpi3, and wpewebkit-2.38 on the
#      rpi4 (image/buildroot/external/configs, itself unbuilt). Each says what
#      it needs in its own CFG_NEEDS, so they refuse individually and
#      correctly already; deriving each remaining defconfig is separate work.

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
    local jobs overlay_arch overlay_wifi dl cc
    jobs=$(WK_MB_PER_JOB=2048 WK_MAX_JOBS=16 build_jobs)
    overlay_arch="${BR_OVERLAY_TAILSCALE:-}"
    overlay_wifi=""
    _image_wants_wifi "${IMG_MACHINE:-}" && overlay_wifi=1
    # Display only, for the dry run's `du -sh` and to spell out what
    # lib/store.sh already reserves (store_init) and targets/container.sh
    # already mounts and exports as BR2_DL_DIR/BR2_CCACHE_DIR. Not passed to
    # buildroot-build.sh: a host path handed to a process running inside the
    # container would name nothing there, and the container already has the
    # right (container-side) path in its own environment -- passing one here
    # would be a second, disagreeing copy of the same fact.
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
            ${overlay_wifi:+--overlay-wifi 1} \
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
        log  "  it stays in the workspace that built it (wk help), and"
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
