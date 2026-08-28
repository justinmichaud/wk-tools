# The yocto image builder: `wk sysimage build <a yocto profile>`.
#
# A field of a profile (IMG_BUILDER=yocto), not a separate command: what
# comes out is the same disk image `wk sysimage ls`/`write` handle for every
# builder, so the two share the scan and the write path and nothing before it.
#
# Differs from the distro builder in three ways: it drives a workspace
# instead of running on the host, since hours of compilation need a
# toolchain and 100 GB of scratch, which a workspace is for and the
# workstation deliberately is not (the "host stays boring" rule in
# README.md); its spec is WebKit's own Tools/yocto on the release branch,
# not a profile in this repo, so it pins the same commits as the WebKit that
# will run on the board; and DL_DIR/SSTATE_DIR are store-backed cache mounts
# (targets/container.sh), so `wk rm` on the build workspace throws away
# TMPDIR but keeps what makes the next build fast.
#
# The build is hours, so nothing may depend on the driving process staying
# alive: under `podman exec` a detached process does not survive its client
# even under `setsid`, measured rather than assumed (t_spawn,
# targets/container.sh). The stage runs through `t_spawn` with its output in
# a host bind mount, so `tail -f` follows it with no podman in the way, and
# this end can be killed without the build noticing. `--detach` only decides
# whether we wait; `--stop` ends a detached one.

# --- where the pieces are ----------------------------------------------------

# The build workspace's default name. Derived from the profile so that two
# profiles cannot land in one workspace: their WebKitBuild/CrossToolChains
# subdirectories would differ, but their checkouts are on different branches
# and only one branch can be checked out at a time.
yocto_ws_default() { echo "yocto-$1"; }

# Inside the workspace. The layout is cross-toolchain-helper's, not ours.
yocto_workdir()  { echo "/src/WebKit/WebKitBuild/CrossToolChains/$1"; }
yocto_image_dir() { echo "$(yocto_workdir "$1")/build/image"; }

# In the workspace's home directory, a host bind mount, so following the
# build costs a plain `tail -f` with no exec into the container.
yocto_log()    { echo "$(wk_ws_dir "$1")/home/yocto-$2.log"; }
yocto_pidfile() { echo "$(wk_ws_dir "$1")/home/yocto-$2.pid"; }
yocto_status() { echo "$(wk_ws_dir "$1")/yocto.status"; }

# --- the workspace image -----------------------------------------------------

# A plain Ubuntu, not the wkdev SDK image: 24.04 is a supported Yocto
# scarthgap build host, and the SDK image is three releases past one
# (container/yocto/Containerfile). WK_YOCTO_BASE overrides it, for the day
# the pinned poky moves to a release whose supported host is newer.
YOCTO_BASE_IMAGE="${WK_YOCTO_BASE:-docker.io/library/ubuntu:24.04}"

# The workspace image for a Yocto build, built if it is not already there.
# Tagged with the base's tag *and* a digest of the Containerfile, so editing
# the spec builds a new image instead of `podman image exists` reusing a
# layer that no longer matches it.
yocto_ensure_image() {
    local base="$YOCTO_BASE_IMAGE" derived spec
    spec="$WK_ROOT/container/yocto/Containerfile"

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
# Created rather than demanded: it is derivable from the profile, so there
# is nothing to ask the user to type first. An existing one is reused --
# the sstate cache needs a workspace to sit next to.
yocto_ensure_ws() {
    local ws="$1" branch="$2"

    # Before the existence check: an existing workspace must re-ask for the
    # image after the Containerfile changes, not silently build against the
    # old one. `podman image exists` makes the common case free.
    yocto_ensure_image

    if [ "$(t_info "$ws")" = absent ]; then
        info "creating workspace '$ws' for the Yocto build"
        "$WK_ROOT/wk" new "$ws" || die "could not create workspace '$ws'"
    else
        # An existing workspace is pinned to whatever image it was created
        # from; nothing can migrate a running container to a new one, so a
        # mismatch is reported rather than silently building with stale
        # host packages.
        local was
        was=$(podman container inspect "wk-$ws" --format '{{.ImageName}}' 2>/dev/null) || was=""
        if [ -n "$was" ] && [ "$was" != "$WK_SDK_IMAGE" ]; then
            die "workspace '$ws' was made from $was, and the spec now wants
    $WK_SDK_IMAGE. A container cannot be moved between images, so this build
    would use host packages that container/yocto/Containerfile no longer
    describes. Remake it -- the Yocto caches are in the store and survive:
        wk rm $ws && wk sysimage build $IMG_PROFILE${stage:+ --stage $stage}"
        fi
    fi

    # The branch is the version pin, checked rather than assumed: a workspace
    # left on a different one would build a different distribution silently.
    local at
    # tail -1: a container still running its firstrun hook prints that hook's
    # output through wkdev-enter, ahead of the command's own.
    at=$(t_exec "$ws" bash -c "cd /src/WebKit && git rev-parse --abbrev-ref HEAD" 2>/dev/null | tr -d '\r' | tail -1) || at=""
    if [ "$at" != "$branch" ]; then
        info "checking out '$branch' in '$ws' (was ${at:-unknown})"
        # The mirror carries main only (lib/store.sh), so a release branch
        # is fetched on demand -- from the profile's remote, not always
        # origin: WPE's known-good configurations live in a different
        # repository, wired as `wpe` (lib/store.sh).
        local remote="${YOC_REMOTE:-origin}"
        t_exec "$ws" bash -c "cd /src/WebKit && {
            git checkout -q $(sh_quote "$branch") 2>/dev/null ||
            { git fetch -q $(sh_quote "$remote") $(sh_quote "$branch:$branch") &&
              git checkout -q $(sh_quote "$branch"); }; }" \
            || die "could not check out '$branch' from '$remote' in '$ws'.
    It is fetched from GitHub on demand, so this is usually egress -- or a
    missing remote, which 'wk remotes' reports and 'wk remotes --fix' repairs.
    Try:  wk enter $ws  and then  git fetch $remote $branch"
    fi
}

# --- running it --------------------------------------------------------------

# Does the branch have a section for the target this profile names?
#
# Asked here rather than left to bitbake: the failure is otherwise a
# config-parse error inside a spawned build whose log the caller has not
# been told to read yet, for a fix the caller can see from this message.
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
        image_types = tar.xz wic.xz
        patch_file_path = meta-openembedded_and_meta-webkit.patch

    The sections that do exist here are:
$(t_exec "$ws" bash -c "sed -n 's/^\[\(.*\)\]/      \1/p' $conf" 2>/dev/null | tr -d '\r')"
}

# Evidence, not the status file: the pid is read from the workspace and
# tested in the workspace, the only namespace the number means anything in.
# The wrapper outlives a failed command briefly, so a restart inside that
# window is refused as "already running" until it exits or `--stop`.
yocto_running() {
    local ws="$1" stage="$2" pid
    pid=$(cat "$(yocto_pidfile "$ws" "$stage")" 2>/dev/null | tr -dc '0-9') || true
    [ -n "$pid" ] || return 1
    t_exec "$ws" kill -0 "$pid" >/dev/null 2>&1
}

# Every stage, not just the one asked for: the stages share a build
# directory, so checking only the requested one would let `--stage fetch`
# start on top of a live `--stage image`, getting as far as two cookers.
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
    Stop it:    wk sysimage build $IMG_PROFILE --stage $live --stop"
    fi

    # Sized against the machine's other builds and on its books while it
    # runs (lib/resources.sh): a bitbake run takes the whole envelope.
    build_admit "the $stage build" "$(build_jobs)"
    build_record "wk sysimage $stage $ws" "$(envelope_cores)" "$(envelope_mem_mb)" "ws:$ws:yocto-$stage.pid"

    # Truncated, not unlinked: `tail -f` follows an inode, so deleting it
    # would leave a follower staring at a file nobody writes to any more.
    : > "$log"
    rm -f "$pid_host"

    # The wrapper runs from /opt/wk-tools, the read-only mount of this repo,
    # so there is no version skew between the two halves.
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

# Stop a detached build. Kills the bitbake processes as well as the wrapper
# -- the wrapper is a shell waiting on cross-toolchain-helper, and killing
# only it leaves bitbake building with nothing recording that it is.
#
# SIGTERM, not SIGKILL: bitbake writes its own state and sstate cache as it
# goes, and letting it close them is the difference between a resumable
# build and a corrupt cache.
yocto_stop() {
    local ws="$1" stage="$2" pid
    pid=$(cat "$(yocto_pidfile "$ws" "$stage")" 2>/dev/null | tr -dc '0-9') || true
    if ! yocto_running "$ws" "$stage"; then
        log "no '$stage' build is running in '$ws'"
        return 0
    fi
    info "stopping the '$stage' build in '$ws' (pid $pid)"
    # The pattern is this workspace's own build directory, not the word
    # "bitbake": a wkdev container shares the host's PID namespace, so a bare
    # `pkill -f bitbake` would reach every bitbake on the machine, including
    # another workspace's build.
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
# Written rather than reusing run_watched(), which watches a *child* -- the
# whole point of the detach above is that this build is not one.
#
# Does NOT kill a stalled build, unlike run_watched: a bitbake task can be
# silent for a long time on legitimate work, and killing one costs hours
# sstate cannot always give back.
yocto_wait() {
    local ws="$1" stage="$2"
    local log start now idle warned=0 last_beat
    log=$(yocto_log "$ws" "$stage")
    command -v log_age >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    start=$(date +%s); last_beat=$start

    while yocto_running "$ws" "$stage"; do
        # Slower than run_watched's poll: each check is a container exec.
        # WK_YOCTO_POLL_SECONDS overrides the interval.
        sleep "${WK_YOCTO_POLL_SECONDS:-30}"
        now=$(date +%s)
        idle=$(log_age "$log" 2>/dev/null) || idle=0
        if [ "$idle" -lt "${WK_STALL_SECONDS:-900}" ]; then
            warned=0
        elif [ "$warned" -eq 0 ]; then
            warn "no output for ${idle}s in '$ws' -- not killing it; a bitbake task
  can be silent for a long time. Look:  tail -f $log"
            warned=1
        fi
        if [ $(( now - last_beat )) -ge "${WK_HEARTBEAT_SECONDS:-300}" ]; then
            log "  ... $(tail -1 "$log" 2>/dev/null | tr -d '\r' | cut -c1-100) ($(( (now - start) / 60 ))m)"
            last_beat=$now
        fi
    done

    # The exit status of a process we did not fork cannot be waited for, so
    # the wrapper's own marker line is the verdict: a build whose container
    # was killed leaves a log with no marker.
    grep -q "^wk-yocto: stage '$stage' done" "$log" 2>/dev/null && return 0
    return 1
}

# --- the build ---------------------------------------------------------------

yocto_dry_run() {
    local ws="$1" stage="$2"
    local at="not created"
    [ "$(t_info "$ws")" = absent ] || at=$(t_exec "$ws" bash -c "cd /src/WebKit && git rev-parse --abbrev-ref HEAD" 2>/dev/null | tr -d '\r' | tail -1)
    cat >&2 <<EOF
would build image $IMG_PROFILE (builder: yocto)
  for machine $IMG_MACHINE ($IMG_ARCH)
  branch      $YOC_BRANCH  (from the '${YOC_REMOTE:-origin}' remote)
  cross-target $YOC_TARGET
  recipe      $YOC_IMAGE
  stage       $stage (it includes the ones before it)
  workspace   $ws ($at)
  jobs        $(envelope_cores) cores, $(envelope_mem_mb) MB envelope
  DL_DIR      $WK_STORE/cache/yocto/downloads ($(du -sh "$WK_STORE/cache/yocto/downloads" 2>/dev/null | cut -f1))
  SSTATE_DIR  $WK_STORE/cache/yocto/sstate ($(du -sh "$WK_STORE/cache/yocto/sstate" 2>/dev/null | cut -f1))
  rm_work     $([ "${YOC_RM_WORK:-0}" = 1 ] && echo "on (--keep-work turns it off; still peaked at 79 GB here)" || echo off)
  chromium    $([ "$chromium" = 0 ] && echo "dropped (about half the build; --chromium puts it back)" || echo "in the image (--chromium)")
  webkit jobs $(WK_MB_PER_JOB=2560 build_jobs) (2560 MB/job -- WebCore's unified sources OOM'd at -j79)
  local fixes $([ "${YOC_LOCAL_LAYER:-1}" = 0 ] && echo "none -- the branch's own configuration, unmodified" || echo "image/yocto/meta-wk is added to bblayers (build-time only)")
  tailnet     $([ "${YOC_TAILNET:-1}" = 0 ] && echo "off -- the board is reachable only over whatever LAN it lands on" || echo "tailscale in the image (meta-wk-tailnet); the card carries the key")
  wifi        $(_image_wants_wifi "${IMG_MACHINE:-}" && echo "wk-wifi-join in the image (meta-wk-wifi); the card carries the credential" || echo "not needed -- $IMG_MACHINE has a cable")
  rescue      wk-card-priv in the image (meta-wk-rescue), so a board's rescue can write its bench medium
  disk free   $(df -h --output=avail "$WK_STORE" 2>/dev/null | tail -1 | tr -d ' ')
  into        $(yocto_image_dir "$YOC_TARGET")/$YOC_IMAGE.wic.xz -- no import; that is
              where 'wk sysimage ls' and 'wk sysimage write --from' read it
EOF
    log "dry run -- nothing was built."
}

# yocto_build <profile> <args...>
yocto_build() {
    local profile="$1"; shift
    local dry="" ws="" stage="" detach="" keep_work="" stop=""
    local chromium="" commit="" slot=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)   dry=1 ;;
            --stop)      stop=1 ;;
            --workspace) ws="${2:-}"; [ -n "$ws" ] || die "--workspace needs a name"; shift ;;
            --stage)     stage="${2:-}"; [ -n "$stage" ] || die "--stage needs a name"; shift ;;
            --detach)    detach=1 ;;
            --keep-work) keep_work=1 ;;
            --chromium)  chromium=1 ;;
            # A flag, not a profile field: "does this build unmodified?" is a
            # question asked of a build, not of a configuration.
            --no-local-layer) YOC_LOCAL_LAYER=0 ;;
            --local-layer)    YOC_LOCAL_LAYER=1 ;;
            # An image without tailscale, for measurements that must compare
            # against numbers taken without it in the fleet.
            --no-tailnet)     YOC_TAILNET=0 ;;
            --tailnet)        YOC_TAILNET=1 ;;
            # The webkit stage as a slot: WebKit's own build-webkit
            # --cross-target of one commit, packed beside the image
            # (`wk sysimage webkit`; image_slot_dir, lib/image.sh).
            --commit)         commit="${2:-}"; shift ;;
            --slot)           slot="${2:-}"; image_check_slot_name "$slot"; shift ;;
            *) die "unknown option: $1
    'wk sysimage build $profile' takes --dry-run, --workspace, --stage,
    --detach, --stop, --keep-work, --chromium, --no-local-layer and
    --no-tailnet; 'wk sysimage webkit $profile' adds --commit and --slot." ;;
        esac
        shift
    done

    # The profile's value unless the flag overrode it: the expensive choice is
    # written down in one place rather than typed per invocation.
    [ -n "$chromium" ] || chromium="${YOC_CHROMIUM:-1}"

    [ -n "$ws" ] || ws=$(yocto_ws_default "$profile")
    require_name "$ws"
    [ -n "$keep_work" ] && YOC_RM_WORK=0

    # One stage per invocation: the wrapper syncs layers and writes
    # local.conf before whatever it was asked for, so the stages are a
    # *depth*, not a pipeline this end has to sequence -- which is what
    # makes `--detach` honest.
    stage="${stage:-image}"
    if [ -n "$commit$slot" ]; then
        [ "$stage" = webkit ] || die "--commit/--slot belong to the webkit stage (wk sysimage webkit $profile)"
        [ -n "$commit" ] && [ -n "$slot" ] || die "a slot needs both --commit <sha> and --slot <name>"
        case "$commit" in *[!0-9a-f]*) die "--commit takes a full sha (40 hex digits), got '$commit'" ;; esac
        [ "${#commit}" -eq 40 ] || die "--commit takes a full sha (40 hex digits), got '$commit'"
    fi
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

    # Free space, before anything is created: TMPDIR lands in the workspace's
    # overlay, on the store's filesystem, and the thresholds below are
    # measured, not estimated.
    local avail_gb need_gb=120
    [ "$chromium" = 1 ] || need_gb=60
    [ "${YOC_RM_WORK:-0}" = 1 ] || need_gb=$((need_gb + 60))
    avail_gb=$(df -B1G --output=avail "$WK_STORE" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ -n "$avail_gb" ] && [ "$avail_gb" -lt "$need_gb" ]; then
        warn "${avail_gb} GB free on $WK_STORE's filesystem; this build wants about ${need_gb} GB
  (TMPDIR${YOC_RM_WORK:+ with rm_work on}, plus the sstate and download caches). It will halt
  rather than fill the disk -- BB_DISKMON_DIRS is set for that -- but it will
  halt hours in. 'wk gc' first, or free space."
        barrier "not started: ${avail_gb} GB free, about ${need_gb} GB needed"
    fi

    yocto_ensure_ws "$ws" "$YOC_BRANCH"

    local built id
    built=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    id="$profile-$(date -u +%Y%m%dT%H%M%SZ)"

    # The workspace lock, for the whole build: what must not happen is a `wk
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
        --local-layer "${YOC_LOCAL_LAYER:-1}" \
        --tailnet "${YOC_TAILNET:-1}" \
        --webkit-jobs "$(WK_MB_PER_JOB=2560 build_jobs)" \
        --sstate-ns "$(printf '%s' "${WK_SDK_IMAGE##*/}" | tr ':/' '--')" \
        ${commit:+--commit "$commit"} ${slot:+--slot "$slot" --profile "$profile"}

    if [ -n "$detach" ]; then
        info "running detached in '$ws' -- this end can go away"
        log  "  follow:  tail -f $(yocto_log "$ws" "$stage")"
        log  "  then:    wk sysimage build $profile --stage $stage   (once it has finished)"
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

    if [ "$stage" != image ]; then
        sed -i 's/^state=running/state=built/' "$(yocto_status "$ws")"
        info "stage '$stage' builds no disk image, so there is nothing more to report"
        return 0
    fi

    # No import: the image stays where bitbake left it (yocto_image_dir),
    # and 'wk sysimage ls' reads it there (image_workspace_scan, lib/image.sh).
    # The fleet integration -- identity marker, watchdog, driving key,
    # retargeted root -- is applied once, at write time, by
    # 'wk sysimage write --from' (cmd/sysimage).
    sed -i 's/^state=running/state=ok/' "$(yocto_status "$ws")"
    local wic; wic="$(yocto_image_dir "$YOC_TARGET")/$YOC_IMAGE.wic.xz"
    info "built $id  ($(du -h "$wic" 2>/dev/null | cut -f1))"
    log  "  $wic"
    log  "  next:  wk sysimage write --from <path above> --disk <machine>:<device>"
    log  "         ('wk sysimage ls' lists it with the exact path; 'wk sysimage disks"
    log  "         <machine>' lists what is attached where)"
    log  "  the image carries no WebKit -- it is the runtime. The matching"
    log  "  build is:  wk sysimage build $profile --stage webkit"
}
