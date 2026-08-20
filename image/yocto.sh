# The yocto image builder: `wk image build <a yocto profile>`.
#
# Why this lives under `wk image` and not in a command of its own
# ---------------------------------------------------------------
# What comes out of a Yocto build is the same *kind* of thing that comes out of
# the distro builder: one partitioned disk image for one machine, whose
# identity has to be recorded and whose bytes have to reach a boot device. Every
# consumer downstream of that -- `wk image ls`, `wk image show`, `wk image
# flash`, the SD-card path in docs/HANDOFF-sdcard.md, and (once it can serve a
# network root) `wk serve` -- is about a disk image and has nothing to say about
# how it was made. Giving Yocto its own command would have meant a second image
# store, a second manifest format and a second flashing path, and
# docs/HANDOFF-yocto.md asks for the opposite in as many words: consume the
# SD-card path rather than building a separate copy-to-host path here.
#
# So the mechanism is a *field* of a profile (IMG_BUILDER) rather than a
# separate verb, and the two builders share the store, the manifest and
# everything after the manifest -- and nothing before it.
#
# What is genuinely different
# ---------------------------
#   the host does not build it.  The distro builder runs entirely on the
#   workstation: it downloads a pinned image and edits it with mtools and
#   debugfs, unprivileged, in about two minutes. A Yocto build is hours of
#   compilation and needs a toolchain, a WebKit checkout and 100 GB of scratch
#   -- which is the definition of a workspace, and the workstation is
#   deliberately not that (the "host stays boring" rule in README.md). So this
#   builder *drives* a workspace and imports the result.
#
#   the spec is not in this repo.  The distro profiles carry their own package
#   lists and config.txt fragments. Here the spec is WebKit's own Tools/yocto
#   on the release branch: its manifest.xml pins poky, meta-openembedded,
#   meta-raspberrypi, meta-webkit, meta-clang and meta-browser by commit. That
#   is a better pin than anything this repo could restate, and it has the
#   property that matters -- it is the *same* pin as the WebKit that will run on
#   the board. So the profile names a branch and a cross-target, and nothing
#   about layers or recipes.
#
#   it survives the workspace.  DL_DIR and SSTATE_DIR are store-backed cache
#   mounts (targets/container.sh), so `wk rm` on the build workspace throws away
#   the 30-90 GB of TMPDIR and keeps the part that makes the next build fast.
#   That is docs/HANDOFF-yocto.md's "Yocto cache should be preserved even if
#   target is destroyed", and it needed no new mechanism -- only for the build
#   to actually be told to use it, which bitbake's environment filtering
#   otherwise prevents (see image/yocto-build.sh).
#
# The detach model
# ----------------
# This build is hours, so nothing may depend on the driving process staying
# alive -- the same rule the top of claude/CLAUDE.md states for `wk build`. That
# needed a driver primitive rather than a `nohup` written here: under `podman
# exec`, a detached process does not survive its client, `setsid` or not, which
# is measured and not assumed (see t_spawn in targets/container.sh). So the
# stage is started through `t_spawn`, with its output redirected to a file in
# the workspace's home directory -- which is a host bind mount, so the host
# follows the log with a plain `tail -f` and no podman in the way. This end can
# then be killed at any time and the build does not notice. `--detach` only
# decides whether we wait; `--stop` is how a detached one is ended.

# --- where the pieces are ----------------------------------------------------

# The build workspace's default name. Derived from the profile so that two
# profiles cannot land in one workspace: their WebKitBuild/CrossToolChains
# subdirectories would differ, but their checkouts are on different branches
# and only one branch can be checked out at a time.
yocto_ws_default() { echo "yocto-$1"; }

# Inside the workspace. The layout is cross-toolchain-helper's, not ours.
yocto_workdir()  { echo "/src/WebKit/WebKitBuild/CrossToolChains/$1"; }
yocto_image_dir() { echo "$(yocto_workdir "$1")/build/image"; }

# The log and the pid, in the workspace's home directory *because* that is a
# host bind mount (`--home $ws/home` in targets/container.sh): following the
# build then costs a `tail -f` on a file, with no exec into the container and
# nothing that breaks when the container is restarted.
yocto_log()    { echo "$(wk_ws_dir "$1")/home/yocto-$2.log"; }
yocto_pidfile() { echo "$(wk_ws_dir "$1")/home/yocto-$2.pid"; }
yocto_status() { echo "$(wk_ws_dir "$1")/yocto.status"; }

# --- the workspace image -----------------------------------------------------

# The base the workspace image is built on.
#
# A plain Ubuntu, not the wkdev SDK image, and the whole argument for that is in
# container/yocto/Containerfile: 24.04 *is* a supported Yocto scarthgap build
# host, and the SDK image is three releases past one. Overridable for the day
# the pinned poky moves to a release whose supported host is newer.
YOCTO_BASE_IMAGE="${WK_YOCTO_BASE:-docker.io/library/ubuntu:24.04}"

# The workspace image for a Yocto build, built if it is not already there.
#
# One layer on the base, adding Yocto's host tooling and the few things WebKit's
# driving scripts need. Tagged with the base's tag *and* a digest of the
# Containerfile, so changing either builds a new image rather than reusing a
# layer that no longer matches its spec.
#
# Sets WK_SDK_IMAGE, which t_create reads. An environment variable rather than a
# flag on `wk new`: the caller here *is* this file, the value is derived rather
# than chosen, and a flag would invite pointing a workspace at an arbitrary
# image by hand -- a bigger door than this needs.
yocto_ensure_image() {
    local base="$YOCTO_BASE_IMAGE" derived spec
    spec="$WK_ROOT/container/yocto/Containerfile"

    # The tag carries the base's tag *and* a digest of the Containerfile, so
    # editing the spec builds a new image instead of silently reusing the old
    # one. Learned the hard way: a marker file was added to the Containerfile,
    # the tag did not change, `podman image exists` said yes, and every
    # workspace made from it went on lacking the marker -- which looked exactly
    # like the change not working. Same reasoning as `IMG_BASE_SHA256` in the
    # distro profiles: name a build product by what went into it.
    derived="localhost/wk-yocto-host:${base##*:}-$(sha256sum "$spec" | cut -c1-8)"

    if podman image exists "$derived" 2>/dev/null; then
        debug "workspace image $derived already built"
    else
        info "building the Yocto workspace image $derived (one layer on $base)"
        log  "  a supported Yocto build host: GCC 13, Python 3.12, glibc 2.39."
        log  "  container/yocto/Containerfile says why that matters more than"
        log  "  sharing layers with the WebKit SDK image."
        podman build --build-arg "BASE=$base" \
            -t "$derived" -f "$spec" "$WK_ROOT/container/yocto" \
            || die "could not build $derived.
    This runs on the host, where there is a network; if apt or the pull failed,
    that is a host-side problem and not the workspace boundary."
    fi

    WK_SDK_IMAGE="$derived"
    export WK_SDK_IMAGE
}

# --- the workspace -----------------------------------------------------------

# Make sure there is a workspace, on the right branch, ready to build in.
#
# Created rather than demanded: a profile names a branch, and "the workspace
# for this profile" is derivable, so requiring the user to have made it first
# would be asking them to retype something already written down. An existing
# one is reused -- that is the whole point of the sstate cache having a
# workspace to sit next to.
yocto_ensure_ws() {
    local ws="$1" branch="$2"

    # Unconditionally, and *before* the existence check. Doing it only on
    # creation is a trap this walked straight into: an edit to the Containerfile
    # changed the wanted tag, an existing workspace meant the build never asked
    # for the image, and every run went on using a container made from the old
    # one -- which looked exactly like the edit having no effect. `podman image
    # exists` makes the current case free.
    yocto_ensure_image

    if [ "$(t_info "$ws")" = absent ]; then
        info "creating workspace '$ws' for the Yocto build"
        "$WK_ROOT/wk" new "$ws" || die "could not create workspace '$ws'"
    else
        # An existing workspace is pinned to whatever image it was created
        # from; nothing can migrate a running container to a new one. So the
        # mismatch is reported rather than ignored or silently worked around --
        # the alternative is a build whose host packages are not the ones the
        # spec now describes.
        local was
        was=$(podman container inspect "wk-$ws" --format '{{.ImageName}}' 2>/dev/null) || was=""
        if [ -n "$was" ] && [ "$was" != "$WK_SDK_IMAGE" ]; then
            die "workspace '$ws' was made from $was, and the spec now wants
    $WK_SDK_IMAGE. A container cannot be moved between images, so this build
    would use host packages that container/yocto/Containerfile no longer
    describes. Remake it -- the Yocto caches are in the store and survive:
        wk rm $ws && wk image build $IMG_PROFILE${stage:+ --stage $stage}"
        fi
    fi

    # The branch is the version pin, so it is checked rather than assumed:
    # a workspace left on a different branch would build a different
    # distribution under this profile's name and nothing would say so.
    local at
    # tail -1: a container still running its firstrun hook prints that hook's
    # output through wkdev-enter, ahead of the command's own. Taking the last
    # line is what keeps a branch name from arriving as three paragraphs of
    # initialisation log -- the same class of trap the `--quiet` note on t_exec
    # describes.
    at=$(t_exec "$ws" bash -c "cd /src/WebKit && git rev-parse --abbrev-ref HEAD" 2>/dev/null | tr -d '\r' | tail -1) || at=""
    if [ "$at" != "$branch" ]; then
        info "checking out '$branch' in '$ws' (was ${at:-unknown})"
        # The mirror carries main only (lib/store.sh, wk_mirror_branches), so a
        # release branch is fetched from GitHub on demand -- which the egress
        # policy already permits and which lib/store.sh names as the intended
        # path for exactly this.
        t_exec "$ws" bash -c "cd /src/WebKit && {
            git checkout -q $(sh_quote "$branch") 2>/dev/null ||
            { git fetch -q origin $(sh_quote "$branch:$branch") &&
              git checkout -q $(sh_quote "$branch"); }; }" \
            || die "could not check out '$branch' in '$ws'.
    It is fetched from GitHub on demand, so this is usually egress. Try:
        wk enter $ws  and then  git fetch origin $branch"
    fi
}

# --- running it --------------------------------------------------------------

# Is the build for <ws>/<stage> still running?
#
# Evidence, not the status file: the pid is read from the workspace and tested
# in the workspace, because that is the only namespace the number means
# anything in (docs/HANDOFF-workspace-state.md -- "status files are claims").
# Does the branch actually have a section for the target this profile names?
#
# Asked here rather than left to bitbake, because the failure is otherwise a
# config-parse error inside a spawned build whose log the caller has not been
# told to read yet -- and because the fix is eight lines that the caller can
# see from the message.
#
# The rpi5 is why this exists. `Tools/yocto/rpi/local-rpi5-64bits-mesa.conf` is
# shipped on this branch and the pinned meta-raspberrypi carries
# `raspberrypi5.conf`, so everything a Pi 5 build needs is present except the
# stanza that names them together.
yocto_check_target() {
    local ws="$1" conf=/src/WebKit/Tools/yocto/targets.conf have
    have=$(t_exec "$ws" bash -c "grep -c '^\[$YOC_TARGET\]' $conf 2>/dev/null || echo 0" \
        2>/dev/null | tr -d '\r' | tail -1)
    [ "${have:-0}" != 0 ] && return 0

    die "$YOC_BRANCH has no [$YOC_TARGET] section in Tools/yocto/targets.conf,
    so there is nothing for bitbake to configure from.

    Everything else that target needs is already on the branch -- its
    local.conf (Tools/yocto/rpi/local-$YOC_TARGET.conf) and its MACHINE in the
    pinned meta-raspberrypi. Only the stanza tying them together is missing.
    Add it upstream, beside [rpi4-64bits-mesa]:

        [$YOC_TARGET]
        repo_manifest_path = rpi/manifest.xml
        conf_bblayers_path = rpi/bblayers.conf
        conf_local_path = rpi/local-$YOC_TARGET.conf
        image_basename = $YOC_IMAGE
        image_types = tar.xz wic.xz wic.bmap
        patch_file_path = meta-openembedded_and_meta-webkit.patch

    The sections that do exist here are:
$(t_exec "$ws" bash -c "sed -n 's/^\[\(.*\)\]/      \1/p' $conf" 2>/dev/null | tr -d '\r')"
}

yocto_running() {
    local ws="$1" stage="$2" pid
    pid=$(cat "$(yocto_pidfile "$ws" "$stage")" 2>/dev/null | tr -dc '0-9') || true
    [ -n "$pid" ] || return 1
    t_exec "$ws" kill -0 "$pid" >/dev/null 2>&1
}

# Every stage, not just the one being asked for. Two bitbakes in one build
# directory is the thing bitbake's own lock exists to prevent, and the stages
# share a build directory -- so "is *anything* running in here" is the question,
# and asking it per stage is what let a `--stage fetch` start on top of a live
# `--stage image` and get as far as two cookers before anyone noticed.
#
# Prints the stage it found, so the refusal can name it.
YOCTO_STAGES="layers fetch image toolchain webkit"
yocto_any_running() {
    local ws="$1" s
    for s in $YOCTO_STAGES; do
        if yocto_running "$ws" "$s"; then printf '%s' "$s"; return 0; fi
    done
    return 1
}

# Start one stage, detached inside the workspace, and return immediately.
yocto_spawn() {
    local ws="$1" stage="$2"; shift 2
    local log pid_host
    log=$(yocto_log "$ws" "$stage"); pid_host=$(yocto_pidfile "$ws" "$stage")

    local live
    if live=$(yocto_any_running "$ws"); then
        die "a '$live' build is already running in '$ws', and the stages share one
    bitbake build directory -- two cookers in it is what bitbake's own lock
    exists to prevent.
    Follow it:  tail -f $(yocto_log "$ws" "$live")
    Stop it:    wk image build $IMG_PROFILE --stage $live --stop"
    fi

    # The log is truncated, not unlinked, and the pid file is unlinked. The
    # difference matters to whoever is watching: `tail -f` follows an inode, so
    # deleting the log leaves every existing follower staring at a file nobody
    # writes to any more -- silently, which is the worst way to lose a watch.
    # The pid file has no followers and a stale one is worse than none.
    : > "$log"
    rm -f "$pid_host"

    # Detached by the driver, not by a `setsid` written here: under `podman
    # exec` a detached process does not survive its client, which is measured
    # rather than assumed (see t_spawn in targets/container.sh). The wrapper
    # itself is at /opt/wk-tools -- the read-only mount of this repo -- so there
    # is nothing to copy in and no version skew between the two halves.
    #
    # The same file has two names, and both are correct: `$(t_home)/yocto-*.log`
    # inside the workspace and `$(yocto_log ...)` out here, because a
    # container's home *is* the workspace's `home/` directory on the host. That
    # is what makes following the build a plain `tail -f` with no podman in the
    # way.
    t_spawn "$ws" "$(t_home "$ws")/$(basename "$log")" \
                  "$(t_home "$ws")/$(basename "$pid_host")" \
        "$(t_tools "$ws")/image/yocto-build.sh" "$@" \
        || die "could not start the build in '$ws'"

    # A pid file that never appears means the exec itself failed, and reporting
    # "started" then would leave a watcher waiting on nothing.
    local i=0
    while [ ! -s "$pid_host" ]; do
        i=$((i + 1)); [ "$i" -gt 50 ] && die "the build did not start in '$ws' (no pid after 5s)
$(sed 's/^/    /' "$log" 2>/dev/null | tail -5)"
        sleep 0.1
    done
    debug "stage $stage running as pid $(cat "$pid_host") inside '$ws'"
}

# Stop a detached build.
#
# Needed because `--detach` is the normal way to run this: a six-hour build that
# can be started and not stopped is a six-hour build you have to wait out, or
# hunt for by pid. The bitbake processes are killed as well as the wrapper --
# the wrapper is a shell waiting on cross-toolchain-helper, and killing only it
# leaves bitbake building happily with nothing recording that it is.
#
# SIGTERM, not SIGKILL: bitbake writes its own state and its sstate cache as it
# goes, and letting it close them is the difference between a resumable build
# and a corrupt cache.
yocto_stop() {
    local ws="$1" stage="$2" pid
    pid=$(cat "$(yocto_pidfile "$ws" "$stage")" 2>/dev/null | tr -dc '0-9') || true
    if ! yocto_running "$ws" "$stage"; then
        log "no '$stage' build is running in '$ws'"
        return 0
    fi
    info "stopping the '$stage' build in '$ws' (pid $pid)"
    # The pattern is this workspace's own build directory, not the word
    # "bitbake". A wkdev container shares the host's PID namespace, so a bare
    # `pkill -f bitbake` from inside one reaches every bitbake on the machine --
    # including another workspace's build, and including anything the user is
    # running by hand. The build directory is unique to this workspace and
    # appears in every bitbake process's command line, so it is both the
    # narrowest pattern available and an exact one.
    local pat; pat=$(yocto_workdir "$YOC_TARGET")
    t_exec "$ws" bash -c "kill -TERM $pid 2>/dev/null
        pkill -TERM -f $(sh_quote "$pat") 2>/dev/null
        true" >/dev/null 2>&1 || true
    local i=0
    while yocto_running "$ws" "$stage"; do
        i=$((i + 1))
        [ "$i" -gt 24 ] && { warn "it is still running after 2 minutes; bitbake shuts down slowly.
  Look:  wk enter $ws  and then  pgrep -af bitbake"; return 1; }
        sleep 5
    done
    sed -i 's/^state=running/state=stopped/' "$(yocto_status "$ws")" 2>/dev/null || true
    info "stopped. sstate is intact, so restarting resumes rather than starts over."
}

# Wait for a stage, reporting progress, and return its exit status.
#
# Written rather than reusing run_watched() because that function watches a
# *child* -- and the whole point of the detach above is that this build is not
# one. What is shared is the policy: warn after WK_STALL_SECONDS of silence,
# and say something every WK_HEARTBEAT_SECONDS so a watcher never has to guess
# whether to keep waiting.
#
# It does NOT kill a stalled build. run_watched aborts at WK_ABORT_SECONDS
# because a stalled compile is dead; a bitbake task can legitimately be silent
# for a long time (a kernel compile behind one recipe's serial task, a large
# fetch with no progress output), and killing one costs hours of work that
# sstate cannot always give back. So a stall here is reported and left alone.
yocto_wait() {
    local ws="$1" stage="$2"
    local log start last_size=0 last_change now size idle warned=0 last_beat
    log=$(yocto_log "$ws" "$stage")
    start=$(date +%s); last_change=$start; last_beat=$start

    while yocto_running "$ws" "$stage"; do
        # A slower poll than run_watched's: each check is a container exec,
        # and over a six-hour build a ten-second interval is two thousand of
        # them for a question whose answer changes once.
        sleep "${WK_YOCTO_POLL_SECONDS:-30}"
        size=$(stat -c %s "$log" 2>/dev/null || echo 0); now=$(date +%s)
        if [ "$size" != "$last_size" ]; then
            last_size=$size; last_change=$now; warned=0
        fi
        idle=$(( now - last_change ))
        if [ "$idle" -ge "${WK_STALL_SECONDS:-900}" ] && [ "$warned" -eq 0 ]; then
            warn "no output for ${idle}s in '$ws' -- not killing it; a bitbake task
  can be silent for a long time. Look:  tail -f $log"
            warned=1
        fi
        if [ $(( now - last_beat )) -ge "${WK_HEARTBEAT_SECONDS:-300}" ]; then
            log "  ... $(tail -1 "$log" 2>/dev/null | tr -d '\r' | cut -c1-100) ($(( (now - start) / 60 ))m)"
            last_beat=$now
        fi
    done

    # The exit status of a process we did not fork cannot be waited for, so the
    # wrapper's own last line is the verdict. That is why image/yocto-build.sh
    # ends with a marker line rather than just exiting zero: a build whose
    # container was killed leaves a log with no marker, which is exactly the
    # "did not finish" answer we need and could not otherwise get.
    grep -q "^wk-yocto: stage '$stage' done" "$log" 2>/dev/null && return 0
    return 1
}

# --- importing the result ----------------------------------------------------

# Bring the built image out of the workspace and into the store.
#
# Copied with the driver's own file-copy primitive rather than read out of the
# overlay's upperdir on the host. The upperdir path is real and would be
# faster, but it is an implementation detail of one target -- and this same
# import has to work the day the build runs in the macOS podman VM.
#
# Not `t_exec <ws> cat` either, which was the first attempt: that goes through
# an interactive-shell wrapper and is not a byte pipe. A 1396-byte test image
# arrived as 1399 bytes and xz refused it, which is why t_pull exists.
yocto_import() {
    local ws="$1" target="$2" recipe="$3" id="$4"
    local dir src_dir wic
    dir=$(image_dir "$id"); src_dir=$(yocto_image_dir "$target")

    wic="$src_dir/$recipe.wic.xz"
    t_exec "$ws" test -f "$wic" \
        || die "no image at $wic in '$ws'.
    The bitbake run reported success but produced nothing under that name --
    which usually means targets.conf's image_types and the recipe's
    IMAGE_FSTYPES disagree. 'wk enter $ws' and look in $src_dir."

    mkdir -p "$dir"

    # Decompressed on the way in, so what the store holds is what `wk image
    # flash` writes and what `image_verify` hashes: one artifact, one hash, no
    # "which of these two files is the image" question anywhere downstream.
    info "importing $recipe.wic.xz from '$ws' (decompressing)"
    t_pull "$ws" "$wic" "$dir/image.wic.xz" \
        || die "could not copy the image out of '$ws'"
    xz -dc "$dir/image.wic.xz" > "$dir/disk.img" \
        || die "the image copied out of '$ws' is not valid xz"
    rm -f "$dir/image.wic.xz"

    # The compressed wic and its block map, which is what makes writing a card
    # fast: bmaptool sends the 573 MB compressed image instead of the 4 GB raw
    # one and writes only the blocks the map says are in use, checksumming each
    # against the map as it goes. It needs a seekable file, so it cannot work on
    # a stream -- which is exactly why the compressed original has to be kept
    # rather than regenerated. boot/disk.sh picks the path.
    local wic_xz="$src_dir/$recipe.wic.xz" bmap="$src_dir/$recipe.wic.bmap"
    if t_exec "$ws" test -f "$bmap"; then
        info "importing $recipe.wic.xz and its block map (the fast write path)"
        t_pull "$ws" "$wic_xz" "$dir/disk.wic.xz" || die "could not import the compressed image"
        t_pull "$ws" "$bmap" "$dir/disk.bmap" || die "could not import the block map"
    fi

    # The rootfs tarball as well. Not needed to flash a card, and kept anyway:
    # a network root is what the rpi4's netboot loop needs (cmd/serve refuses
    # to serve an image whose cmdline names a local root, correctly), and a
    # tarball is what fills an NFS root. Importing it now costs ~600 MB and
    # means the day that lands, the image is already in the store complete.
    local tar="$src_dir/$recipe.tar.xz"
    if t_exec "$ws" test -f "$tar"; then
        info "importing $recipe.tar.xz (the rootfs, for a future network root)"
        t_pull "$ws" "$tar" "$dir/rootfs.tar.xz" \
            || die "could not import the rootfs tarball"
    fi

    # The identity the *board* can be asked for. cross-toolchain-helper hashes
    # every file that can change the build into this string and installs it in
    # the image at /usr/share/cross-target-info-version, so "is the board
    # running this image" is answerable by comparing two strings rather than by
    # trusting a record. Worth carrying into the manifest for that reason
    # alone.
    local crossver
    crossver=$(t_exec "$ws" cat "$(yocto_workdir "$target")/.target-info-version" 2>/dev/null | tr -d '\r\n') || crossver=""
    printf '%s' "$crossver"
}

# --- the build ---------------------------------------------------------------

yocto_dry_run() {
    local ws="$1" stage="$2"
    local at="not created"
    [ "$(t_info "$ws")" = absent ] || at=$(t_exec "$ws" bash -c "cd /src/WebKit && git rev-parse --abbrev-ref HEAD" 2>/dev/null | tr -d '\r' | tail -1)
    cat >&2 <<EOF
would build image $IMG_PROFILE (builder: yocto)
  for machine $IMG_MACHINE ($IMG_ARCH)
  branch      $YOC_BRANCH
  cross-target $YOC_TARGET
  recipe      $YOC_IMAGE
  stage       $stage (it includes the ones before it)
  workspace   $ws ($at)
  jobs        $(envelope_cores) cores, $(envelope_mem_mb) MB envelope
  DL_DIR      $WK_STORE/cache/yocto/downloads ($(du -sh "$WK_STORE/cache/yocto/downloads" 2>/dev/null | cut -f1))
  SSTATE_DIR  $WK_STORE/cache/yocto/sstate ($(du -sh "$WK_STORE/cache/yocto/sstate" 2>/dev/null | cut -f1))
  rm_work     $([ "${YOC_RM_WORK:-0}" = 1 ] && echo "on (--keep-work turns it off; still peaked at 79 GB here)" || echo off)
  chromium    $([ "$chromium" = 0 ] && echo "dropped (about half the build; --chromium puts it back)" || echo "in the image (--chromium)")
  disk free   $(df -h --output=avail "$WK_STORE" 2>/dev/null | tail -1 | tr -d ' ')
  into        $(image_dir "<profile>-<stamp>")
EOF
    log "dry run -- nothing was built."
}

# yocto_build <profile> <args...>
yocto_build() {
    local profile="$1"; shift
    local dry="" ws="" stage="" detach="" keep_work="" no_import="" stop=""
    local chromium=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)   dry=1 ;;
            --stop)      stop=1 ;;
            --workspace) ws="${2:-}"; [ -n "$ws" ] || die "--workspace needs a name"; shift ;;
            --stage)     stage="${2:-}"; [ -n "$stage" ] || die "--stage needs a name"; shift ;;
            --detach)    detach=1 ;;
            --keep-work) keep_work=1 ;;
            --chromium)  chromium=1 ;;
            --no-import) no_import=1 ;;
            *) die "unknown option: $1
    'wk image build $profile' takes --dry-run, --workspace, --stage,
    --detach, --stop, --keep-work, --chromium and --no-import." ;;
        esac
        shift
    done

    # The profile's value unless the flag overrode it: the expensive choice is
    # written down in one place rather than typed per invocation.
    [ -n "$chromium" ] || chromium="${YOC_CHROMIUM:-1}"

    [ -n "$ws" ] || ws=$(yocto_ws_default "$profile")
    require_name "$ws"
    [ -n "$keep_work" ] && YOC_RM_WORK=0

    # One stage per invocation, and each one is self-contained: the wrapper
    # syncs the layers and writes local.conf before whatever it was asked for,
    # and `webkit` builds the toolchain on the way through (build-webkit's
    # --cross-target does that itself). So the stages are a *depth*, not a
    # pipeline this end has to sequence -- which is what makes `--detach`
    # honest. An earlier version spawned a list of stages and returned after
    # the first, so a detached `--stage image` would have synced the layers and
    # stopped.
    stage="${stage:-image}"
    case "$stage" in
        layers|fetch|image|toolchain|webkit) ;;
        *) die "unknown stage '$stage'. One of, each including the ones above it:
      layers     sync the Yocto layers only (minutes; the network-bound part)
      fetch      ... and fetch every source, without building -- one pass that
                 names every host the egress allowlist is still missing, instead
                 of one halted build per host
      image      bitbake the image -- rootfs, kernel, wic  (the default; hours)
      toolchain  bitbake populate_sdk, the cross toolchain (hours)
      webkit     cross-build WebKit against that toolchain
    They are separate commands rather than one because they fail differently:
    'layers' is egress, the rest is compilation, and a run that mixed them
    would report every network failure as a build failure." ;;
    esac

    # Container only, and refused rather than attempted elsewhere. A remote
    # target is somebody else's machine and this build is 100 GB and days of
    # CPU; a macOS VM workspace has no store-backed Yocto cache at all
    # (targets/vm.sh says so in as many words).
    local kind="${WK_TARGET_KIND:-container}"
    [ "$kind" = container ] || die "the Yocto builder needs a container workspace, and this target is '$kind'.
    A remote target is a shared machine -- 100 GB of scratch and days of CPU
    are not ours to take there -- and a macOS VM workspace has no store-backed
    Yocto cache to build against (targets/vm.sh)."

    if [ -n "$dry" ]; then
        yocto_dry_run "$ws" "$stage"
        return 0
    fi

    # Before the disk check and before anything is created: stopping is the one
    # thing that must work when the build has already gone wrong.
    if [ -n "$stop" ]; then
        [ "$(t_info "$ws")" = absent ] && die "no workspace '$ws', so nothing is building"
        yocto_stop "$ws" "$stage"
        return $?
    fi

    # Free space, before anything is created. TMPDIR is the big one and it
    # lands in the workspace's overlay, i.e. on the store's filesystem.
    # Measured rather than estimated. With Chromium in, TMPDIR reached 79 GB
    # with rm_work *on* -- most of it Chromium and gn at 21 GB each. Without it,
    # 13 GB at the same point in the build. Plus ~25 GB of DL_DIR and a growing
    # sstate either way.
    local avail_gb need_gb=120
    [ "$chromium" = 1 ] || need_gb=60
    [ "${YOC_RM_WORK:-0}" = 1 ] || need_gb=$((need_gb + 60))
    avail_gb=$(df -B1G --output=avail "$WK_STORE" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ -n "$avail_gb" ] && [ "$avail_gb" -lt "$need_gb" ]; then
        warn "${avail_gb} GB free on $WK_STORE's filesystem; this build wants about ${need_gb} GB
  (TMPDIR${YOC_RM_WORK:+ with rm_work on}, plus the sstate and download caches). It will halt
  rather than fill the disk -- BB_DISKMON_DIRS is set for that -- but it will
  halt hours in. 'wk gc' first, or free space."
        confirm "start anyway?" || die "not started"
    fi

    yocto_ensure_ws "$ws" "$YOC_BRANCH"

    local built id commit
    built=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    id="$profile-$(date -u +%Y%m%dT%H%M%SZ)"
    commit=$(t_exec "$ws" bash -c "cd /src/WebKit && git rev-parse HEAD" 2>/dev/null | tr -d '\r' | tail -1) || commit=""

    # The workspace lock, for the whole build. Not the image-store lock: that
    # one is taken for the seconds of the import, because holding it for six
    # hours would stop every other `wk image` in the meantime, and nothing this
    # build does touches the store until then. What must not happen is a `wk
    # build` in the same workspace at the same time -- two builds in one
    # checkout corrupt both -- and that is what this lock is.
    hold_lock "ws-$ws" -w "${WK_BUILD_LOCK_WAIT:-3600}"

    cat > "$(yocto_status "$ws")" <<EOF
state=running
profile=$profile
id=$id
target=$YOC_TARGET
branch=$YOC_BRANCH
stage=$stage
started=$built
EOF

    yocto_check_target "$ws"

    info "stage '$stage' for $profile in '$ws'"
    log  "  log: $(yocto_log "$ws" "$stage")"
    yocto_spawn "$ws" "$stage" \
        --target "$YOC_TARGET" --image "$YOC_IMAGE" --stage "$stage" \
        --jobs "$(envelope_cores)" --rm-work "${YOC_RM_WORK:-0}" \
        --chromium "$chromium" \
        --sstate-ns "$(printf '%s' "${WK_SDK_IMAGE##*/}" | tr ':/' '--')"

    if [ -n "$detach" ]; then
        info "running detached in '$ws' -- this end can go away"
        log  "  follow:  tail -f $(yocto_log "$ws" "$stage")"
        log  "  import:  wk image build $profile --stage $stage   (once it has finished)"
        return 0
    fi

    local rc
    set +e; yocto_wait "$ws" "$stage"; rc=$?; set -e
    if [ "$rc" != 0 ]; then
        sed -i 's/^state=running/state=failed/' "$(yocto_status "$ws")"
        warn "stage '$stage' failed for $profile in '$ws'"
        log "last lines:"
        tail -20 "$(yocto_log "$ws" "$stage")" 2>/dev/null | sed 's/^/  /' >&2
        die "  full log: $(yocto_log "$ws" "$stage")"
    fi
    info "stage '$stage' ok"

    if [ -n "$no_import" ]; then
        sed -i 's/^state=running/state=built/' "$(yocto_status "$ws")"
        info "built, not imported (--no-import). It is in '$ws' under $(yocto_image_dir "$YOC_TARGET")"
        return 0
    fi

    if [ "$stage" != image ]; then
        info "stage '$stage' builds no disk image, so there is nothing to import"
        sed -i 's/^state=running/state=built/' "$(yocto_status "$ws")"
        return 0
    fi

    # Only now does the store get touched, and only now is its lock needed.
    image_lock
    local r
    for r in $(image_rubble); do
        warn "destroying rubble from an interrupted import: $r"
        rm -rf "$(image_dir "$r")"
    done

    local crossver dir
    crossver=$(yocto_import "$ws" "$YOC_TARGET" "$YOC_IMAGE" "$id")
    dir=$(image_dir "$id")

    # Is what arrived actually a disk image? The transfer is `cat` over the
    # target driver, and the one thing that can quietly ruin it is a container
    # exec that printed something of its own ahead of the bytes -- an
    # initialisation log, a shell banner. A contaminated image would hash
    # perfectly and fail on the board, so it is checked here, where the answer
    # is one partition table read.
    sfdisk -J "$dir/disk.img" >/dev/null 2>&1 \
        || die "the imported $dir/disk.img has no readable partition table.
    Something was written ahead of the image bytes on the way out of '$ws'.
    'wk enter $ws' and check $(yocto_image_dir "$YOC_TARGET")."

    # The fleet integration, before the hash, because it modifies the image.
    #
    # Everything above this line produced a distribution; this makes it a
    # system *this repo can drive* -- the identity marker `b_probe` reads, the
    # self-return watchdog, and the self-disarm a medium-armed machine needs.
    # Without it a Yocto image boots perfectly and is invisible: `wk boot
    # --status` cannot tell a board running it from a board that never left its
    # host mode, and on the rpi4 the stick would stay armed for ever.
    #
    # The distro builder has done this all along, inside `relabel`, which is
    # why it took wanting a Yocto image on the rpi5 to notice that the two
    # builders shared nothing after the manifest.
    DISK="$dir/disk.img"
    SEED=$(mktemp -d)
    wk_atexit _seed_cleanup
    info "adding the fleet integration (identity marker, watchdog, self-disarm)"
    install_fleet_integration "$(part_offset "$DISK" 2)"
    rm -rf "$SEED"; SEED=""

    info "hashing the image"
    local sha; sha=$(sha256sum "$dir/disk.img" | cut -d' ' -f1)

    # Written last: this is the publishing gate for the yocto builder exactly
    # as it is for the distro one (lib/image.sh).
    {
        cat <<EOF
id=$id
profile=$IMG_PROFILE
builder=yocto
machine=$IMG_MACHINE
arch=$IMG_ARCH
hostname=$IMG_HOSTNAME
branch=$YOC_BRANCH
commit=$commit
cross_target=$YOC_TARGET
image_recipe=$YOC_IMAGE
cross_version=$crossver
workspace=$ws
built=$built
built_by=$(hostname)
wk_tools=$(git -C "$WK_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
disk_bytes=$(stat -c %s "$dir/disk.img")
disk_sha256=$sha
EOF
        if [ -f "$dir/disk.bmap" ]; then
            cat <<EOF
wic_xz=disk.wic.xz
bmap=disk.bmap
wic_xz_bytes=$(stat -c %s "$dir/disk.wic.xz")
EOF
        fi
        if [ -f "$dir/rootfs.tar.xz" ]; then
            cat <<EOF
rootfs_tar=rootfs.tar.xz
rootfs_tar_bytes=$(stat -c %s "$dir/rootfs.tar.xz")
rootfs_tar_sha256=$(sha256sum "$dir/rootfs.tar.xz" | cut -d' ' -f1)
EOF
        fi
    } > "$(image_manifest "$id")"

    sed -i 's/^state=running/state=ok/' "$(yocto_status "$ws")"
    info "built $id  ($(du -h "$dir/disk.img" | cut -f1))"
    log  "  next:  wk image write $id --disk <machine>:<device>"
    log  "         ('wk image disks <machine>' lists what is attached where)"
    log  "  the image carries no WebKit -- it is the runtime. The matching"
    log  "  build is:  wk image build $profile --stage webkit"
}
