# The yocto image builder: `wk sysimage build <a yocto profile>`. The spec is WebKit's own Tools/yocto on
# the release branch, so an image pins the same commits as the WebKit that runs on the board. Under
# `podman exec` a detached process does not survive its client even under `setsid`, hence `t_spawn`.

yocto_ws_default() { echo "yocto-$1"; }  # per profile: two branches cannot both be checked out in one workspace

yocto_workdir()  { echo "/src/WebKit/WebKitBuild/CrossToolChains/$1"; }  # cross-toolchain-helper's layout, not ours
yocto_image_dir() { echo "$(yocto_workdir "$1")/build/image"; }

yocto_log()    { echo "$(wk_ws_dir "$1")/home/yocto-$2.log"; }  # a host bind mount, so `tail -f` needs no exec into the container
yocto_pidfile() { echo "$(wk_ws_dir "$1")/home/yocto-$2.pid"; }
yocto_status() { echo "$(wk_ws_dir "$1")/yocto.status"; }


YOCTO_BASE_IMAGE="${WK_YOCTO_BASE:-docker.io/library/ubuntu:24.04}"  # a supported scarthgap build host, unlike the wkdev SDK image; WK_YOCTO_BASE overrides

yocto_ensure_image() {  # tagged with a digest of the Containerfile, so editing the spec builds a new image
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


yocto_ensure_ws() {  # created rather than demanded: the name is derivable from the profile
    local ws="$1" branch="$2"

    yocto_ensure_image  # before the existence check, so a Containerfile change is re-asked for on every run

    if [ "$(t_info "$ws")" = absent ]; then
        info "creating workspace '$ws' for the Yocto build"
        "$WK_ROOT/wk" new "$ws" || die "could not create workspace '$ws'"
    else
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

    local at  # the branch is the version pin, checked rather than assumed
    # tail -1: a container still in its firstrun hook prints that hook's output through wkdev-enter first.
    at=$(t_exec "$ws" bash -c "cd /src/WebKit && git rev-parse --abbrev-ref HEAD" 2>/dev/null | tr -d '\r' | tail -1) || at=""
    if [ "$at" != "$branch" ]; then
        info "checking out '$branch' in '$ws' (was ${at:-unknown})"
        # The mirror carries main only, so a release branch is fetched on demand, from the profile's remote.
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


yocto_target_note() { # <workspace>; the real check needs a branch a dry run has usually not checked out
    local ws="$1" have
    [ "$(t_info "$ws")" = absent ] && {
        printf '%s' "  (not verified: '$ws' does not exist yet, so this branch's
               Tools/yocto/targets.conf has not been read; the build refuses if
               it has no [$YOC_TARGET] section)"
        return 0
    }
    have=$(t_exec "$ws" bash -c "grep -c '^\\[$YOC_TARGET\\]' /src/WebKit/Tools/yocto/targets.conf 2>/dev/null || echo 0" \
        2>/dev/null | tr -d '\r' | tail -1)
    case "$have" in
        ''|*[!0-9]*) printf '%s' "  (not verified: could not read the branch's targets.conf)" ;;
        0)           printf '%s' "  ** $YOC_BRANCH has no [$YOC_TARGET] section: this build would refuse **" ;;
        *)           printf '%s' "  (verified on $YOC_BRANCH)" ;;
    esac
}

yocto_check_target() {  # asked here: bitbake's own version of it is a config-parse error hours into a spawned build
    local ws="$1" conf=/src/WebKit/Tools/yocto/targets.conf have
    if [ -n "${YOC_PORT_TARGET_FROM:-}" ]; then
        info "$YOC_BRANCH has no [$YOC_TARGET]; the build derives one from [$YOC_PORT_TARGET_FROM]"
        return 0
    fi
    have=$(t_exec "$ws" bash -c "grep -c '^\[$YOC_TARGET\]' $conf 2>/dev/null || echo 0" \
        2>/dev/null | tr -d '\r' | tail -1)
    # Only a count is an answer: `!= 0` would read a failed t_exec as "the section is there".
    case "$have" in
        ''|*[!0-9]*)
            die "could not read $conf in '$ws', so whether $YOC_BRANCH has a
    [$YOC_TARGET] section is unknown -- and a build configured from a section
    that is not there fails inside bitbake, hours later. Check the workspace is
    up:  wk status $ws" ;;
    esac
    [ "$have" != 0 ] && return 0

    die "$YOC_BRANCH has no [$YOC_TARGET] section in Tools/yocto/targets.conf,
    so there is nothing for bitbake to configure from.

    Two files are missing on a branch that predates the target, not one: the
    section, and the local.conf it points at (Tools/yocto/rpi/local-$YOC_TARGET.conf
    -- absent on wpe-2.46, webkitglib/2.46 and webkitglib/2.48). What is
    *not* missing is the machine: the meta-raspberrypi
    revision these manifests pin already carries its conf/machine entry, so this
    is WebKit's own glue and nothing deeper.

    Two ways on, and the first is the real one:

      - add the section and its local.conf upstream, beside the target it
        would be derived from;
      - or have the profile declare what to derive them from, and this build
        writes both into the checkout every run and says that it did:
            YOC_PORT_TARGET_FROM=<a target this branch does have>
            YOC_MACHINE=<the yocto MACHINE the new target selects>
        (image/configs/wpewebkit-2.46-yocto-rpi5-64.conf is the worked example.)

    The sections that do exist here are:
$(t_exec "$ws" bash -c "sed -n 's/^\[\(.*\)\]/      \1/p' $conf" 2>/dev/null | tr -d '\r')"
}

yocto_running() {  # the pid means something only in the workspace's own namespace
    local ws="$1" stage="$2" pid
    pid=$(cat "$(yocto_pidfile "$ws" "$stage")" 2>/dev/null | tr -dc '0-9') || true
    [ -n "$pid" ] || return 1
    t_exec "$ws" kill -0 "$pid" >/dev/null 2>&1
}

# The stages share one build directory, so `--stage fetch` on top of a live `--stage image` reaches two cookers.
YOCTO_STAGES="layers fetch image toolchain webkit"
yocto_any_running() {
    local ws="$1" s
    for s in $YOCTO_STAGES; do
        if yocto_running "$ws" "$s"; then printf '%s' "$s"; return 0; fi
    done
    return 1
}

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

    build_admit "the $stage build" "$(build_jobs)"
    build_record "wk sysimage $stage $ws" "$(envelope_cores)" "$(envelope_mem_mb)" "ws:$ws:yocto-$stage.pid"

    : > "$log"  # truncated, not unlinked: `tail -f` follows an inode
    rm -f "$pid_host"

    t_spawn "$ws" "$(t_home "$ws")/$(basename "$log")" \
                  "$(t_home "$ws")/$(basename "$pid_host")" \
        "$(t_tools "$ws")/image/yocto-build.sh" "$@" \
        || die "could not start the build in '$ws'"

    local i=0  # a pid file that never appears means the exec itself failed
    while [ ! -s "$pid_host" ]; do
        i=$((i + 1)); [ "$i" -gt 50 ] && die "the build did not start in '$ws' (no pid after 5s)
$(sed 's/^/    /' "$log" 2>/dev/null | tail -5)"
        sleep 0.1
    done
    debug "stage $stage running as pid $(cat "$pid_host") inside '$ws'"
}

# SIGTERM, not SIGKILL: bitbake writes its state and sstate as it goes, and closing them cleanly is the difference between resumable and corrupt.
yocto_stop() {
    local ws="$1" stage="$2" pid
    pid=$(cat "$(yocto_pidfile "$ws" "$stage")" 2>/dev/null | tr -dc '0-9') || true
    if ! yocto_running "$ws" "$stage"; then
        log "no '$stage' build is running in '$ws'"
        return 0
    fi
    info "stopping the '$stage' build in '$ws' (pid $pid)"
    # A wkdev container shares the host's PID namespace, so the pattern is this workspace's build directory, not "bitbake".
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

yocto_wait() {
    local ws="$1" stage="$2"
    local log start now idle warned=0 last_beat
    log=$(yocto_log "$ws" "$stage")
    command -v log_age >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    start=$(date +%s); last_beat=$start

    while yocto_running "$ws" "$stage"; do
        sleep "${WK_YOCTO_POLL_SECONDS:-30}"  # each check is a container exec, so slower than run_watched
        now=$(date +%s)
        idle=$(log_age "$log" 2>/dev/null) || idle=0
        if [ "$idle" -lt "${WK_STALL_SECONDS:-900}" ]; then  # 900, not lib/watchdog.sh's 300 default: see that file
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

    # A process this end did not fork cannot be waited for, so the wrapper's marker line is the verdict.
    grep -q "^wk-yocto: stage '$stage' done" "$log" 2>/dev/null && return 0
    return 1
}


yocto_dry_run() {
    local ws="$1" stage="$2"
    local at="not created"
    [ "$(t_info "$ws")" = absent ] || at=$(t_exec "$ws" bash -c "cd /src/WebKit && git rev-parse --abbrev-ref HEAD" 2>/dev/null | tr -d '\r' | tail -1)
    cat >&2 <<EOF
would build image $IMG_PROFILE (builder: yocto)
  for machine $IMG_MACHINE ($IMG_ARCH)
  branch      $YOC_BRANCH  (from the '${YOC_REMOTE:-origin}' remote)
  cross-target $YOC_TARGET$(yocto_target_note "$ws")
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

yocto_build() { # <profile> <args...>
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
            --no-local-layer) YOC_LOCAL_LAYER=0 ;;  # a flag, not a profile field: "does this build unmodified?" is asked of a build
            --local-layer)    YOC_LOCAL_LAYER=1 ;;
            --no-tailnet)     YOC_TAILNET=0 ;;  # for measurements comparing against fleet numbers taken without tailscale
            --tailnet)        YOC_TAILNET=1 ;;
            --commit)         commit="${2:-}"; shift ;;
            --slot)           slot="${2:-}"; image_check_slot_name "$slot"; shift ;;
            *) die "usage: wk sysimage build $profile [--dry-run|--workspace <name>|--stage <name>|
    --detach|--stop|--keep-work|--chromium|--no-local-layer|--no-tailnet]; unknown option: $1
    'wk sysimage webkit $profile' adds --commit and --slot." ;;
        esac
        shift
    done

    # YOC_CHROMIUM=0 in a config drops it: measured here, chromium-ozone-wayland and gn-native are 21 GB of TMPDIR *each* and roughly half of the 13,379 tasks.
    [ -n "$chromium" ] || chromium="${YOC_CHROMIUM:-1}"

    [ -n "$ws" ] || ws=$(yocto_ws_default "$profile")
    require_name "$ws"
    [ -n "$keep_work" ] && YOC_RM_WORK=0

    # One stage per invocation: the wrapper syncs layers and writes local.conf before whatever it was asked for.
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

    local kind="$WK_TARGET_KIND"
    [ "$kind" = container ] || die "the Yocto builder needs a container workspace, and this target is '$kind'.
    A remote target is a shared machine -- 100 GB of scratch and days of CPU
    are not ours to take there -- and a macOS VM workspace has no store-backed
    Yocto cache to build against (targets/vm.sh)."

    if [ -n "$dry" ]; then
        yocto_dry_run "$ws" "$stage"
        [ -z "$slot" ] || {
            log "  commit      $commit"
            log "  slot        $slot -> $(image_slot_dir "$profile" "$slot")"
            log "              WebKit's build-webkit --cross-target of that commit, packed as a slot"
        }
        return 0
    fi

    # Before the disk check and before anything is created: stopping must work when the build has gone wrong.
    if [ -n "$stop" ]; then
        [ "$(t_info "$ws")" = absent ] && die "no workspace '$ws', so nothing is building"
        yocto_stop "$ws" "$stage"
        return $?
    fi

    # Measured thresholds; TMPDIR lands in the workspace's overlay, on the store's filesystem. Per stage: an
    # image build unpacks every recipe in the distribution, the webkit stage is one ninja tree.
    local need_gb what
    case "$stage" in
        webkit)
            need_gb="$WK_BUILD_DISK_GB"
            what="this WebKit cross build" ;;
        *)
            need_gb=120
            [ "$chromium" = 1 ] || need_gb=60
            [ "${YOC_RM_WORK:-0}" = 1 ] || need_gb=$((need_gb + 60))
            what="this image build (TMPDIR${YOC_RM_WORK:+ with rm_work on}, plus the sstate
    and download caches)" ;;
    esac
    disk_admit "$what" "$need_gb"

    yocto_ensure_ws "$ws" "$YOC_BRANCH"

    local built id
    built=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    id="$profile-$(date -u +%Y%m%dT%H%M%SZ)"

    hold_lock "ws-$ws" -w "${WK_BUILD_LOCK_WAIT:-3600}"  # two builds in one checkout corrupt both

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
    # YOC_IMAGE names targets.conf's image_basename in the config too, so a renamed or missing section fails here rather than four hours into bitbake.
    yocto_spawn "$ws" "$stage" \
        --target "$YOC_TARGET" --image "$YOC_IMAGE" --stage "$stage" \
        --jobs "$(envelope_cores)" --rm-work "${YOC_RM_WORK:-0}" \
        ${YOC_PORT_TARGET_FROM:+--port-target-from "$YOC_PORT_TARGET_FROM"} \
        ${YOC_MACHINE:+--port-machine "$YOC_MACHINE"} \
        ${IMG_MACHINE:+--board "$IMG_MACHINE"} \
        ${YOC_MULTILIB:+--multilib "$YOC_MULTILIB"} \
        ${YOC_MULTILIB_TUNE:+--multilib-tune "$YOC_MULTILIB_TUNE"} \
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

    # No import: the image stays where bitbake left it, and 'wk sysimage write --from' applies the fleet integration.
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
