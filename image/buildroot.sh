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
#   host        an `ubuntu:20.04` container. buildroot 2020.02 does not build on
#               a current distro.
#
#               **This is the blocker, and it is environmental rather than
#               unwritten.** The scouting run nested that container inside a
#               container workspace, by hand, outside `wk`. A workspace cannot
#               do it: the SDK image is Ubuntu 24.04 and carries no container
#               runtime at all -- no podman, no docker, no buildah -- and it is
#               also missing `rsync` and `bc`, which buildroot needs of its
#               host. Measured, not assumed.
#
#               So there are three ways out, and picking one is a design
#               decision rather than a detail:
#                 1. give the buildroot lane its own container on the host,
#                    beside the workspaces rather than inside one. Closest to
#                    what a workspace already is, and the one that keeps "a
#                    build runs in a container wk manages".
#                 2. carry a container runtime in the workspace image. The SDK
#                    image is not this repository's, so this is somebody else's
#                    tree to change.
#                 3. make buildroot 2020.02 build on 24.04. The arm64 libffi
#                    wall is already handled (BR_EXTERNAL below); what is
#                    unmeasured is everything after it, and finding out costs a
#                    long build that may fail late.
#               TODO: none of them is chosen. Until one is, this refuses.
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
#      defconfig, write the overlay, build in an ubuntu:20.04 container in a
#      workspace, and hand output/images/$BR_IMAGE to the same manifest step the
#      yocto builder uses. Container-only, and refused elsewhere for the reasons
#      yocto_build gives.
#   2. the ten configurations whose defconfig does not exist upstream -- every
#      board but the rpi3, and every WebKit/WebKit release. Each says what it
#      needs in its own CFG_NEEDS, so they refuse individually and correctly
#      already; deriving those defconfigs is a separate piece of work per board.

buildroot_build() {
    local profile="$1"; shift
    # Parsed before refusing, so that a caller who typed a flag wrong learns
    # that too rather than only the first of two problems.
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run|--detach|--stop|--keep-work|--no-import|--no-tailnet|--tailnet) ;;
            --workspace) shift ;;
            --stage)     shift ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done

    die "the buildroot builder is not written, so '$profile' cannot be built.

    The configuration is real and complete -- $(image_config_file "$profile") --
    and its defconfig (${BR_DEFCONFIG:-none}) exists in
    ${BR_TREE_URL:-the fork} on branch ${BR_TREE_BRANCH:-wpe}. The BR2_EXTERNAL
    tree it needs on an arm64 build host is written
    ($WK_ROOT/image/buildroot/external), and so is the tailnet overlay
    ($WK_ROOT/image/buildroot/tailnet-overlay.sh, verified: it stages the pinned
    tailscale binaries, the join and the BusyBox S99 script).

    What blocks it is the build host, not the code. buildroot 2020.02 needs an
    ubuntu:20.04 userspace, and a workspace cannot provide one: the SDK image is
    Ubuntu 24.04 with no container runtime in it -- no podman, docker or buildah
    -- and without rsync or bc either. image/buildroot.sh, 'what the scouting
    run established', has the three ways out and says that none is chosen.

    Choosing one is the next step, and it is a design decision."
}
