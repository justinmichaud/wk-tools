#!/usr/bin/env bash
#
# wk bench mac -- the macOS benchmark lane, end to end, driven from anywhere.
#
#   wk bench mac <workspace> [--plan P] [--config C] [--host H]
#   wk bench mac <workspace> --status | --reset | --preflight
#
# Runs the five automated steps of the six-step flow
# (docs/HANDOFF-mac-perf-mode.md, "The flow") and resumes across the reboot in
# the middle. Refuses to run on the Mac it measures: phase 4 reboots that
# machine.
#
# Every phase asserts the mode it needs from /etc/wk-image before acting, and
# bench mode has its own ssh alias and HostKeyAlias (`tolken-bench`,
# dotfiles/ssh/config), so a phase aimed at the wrong install fails on the host
# key instead of running against the wrong computer.
#
# The state file records the phase reached; `--status` reads it without
# touching the machine, `--reset` forgets it.
#
# Environment overrides, each defaulted below where it is read and beaten by
# its flag where one exists. WK_MAC_MACHINE is the only way to point this lane
# at benchvm; WK_MAC_BENCH_SSH is needed when the bench install is not on the
# tailnet (docs/HANDOFF-mac-perf-mode.md); WK_MAC_DETACH works around WK_STORE
# naming a path inside the podman VM that a vm workspace on this host does not
# have (phase_build).

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/boot/machines.sh"

# The fleet name of the machine to bench; boot/machines/mbp.conf owns its
# profile. benchvm is the rehearsal you ask for by name.
MACHINE="${WK_MAC_MACHINE:-mbp}"  # static

# Which shape of bench mode this machine has, read from its own conf.
#
#   volume  (mbp, driver mac-volume)  a second macOS install, reached over ssh
#           at the same address with a different host key.
#   guest   (benchvm, driver mac-guest) a macOS VM, reached through the host
#           (`wk enter <guest> -- ...`) because its address is assigned at boot
#           and known only to the vm driver.
lane_shape() {
    local d
    d=$( . "$WK_ROOT/boot/machines.sh" >/dev/null 2>&1
         machine_load "$MACHINE" >/dev/null 2>&1 && printf '%s' "${NODE_DRIVER:-}" )
    case "$d" in
        mac-guest)  echo guest ;;
        mac-volume) echo volume ;;
        '')  die "no such machine: $MACHINE (wk boot --list)" ;;
        *)   die "$MACHINE is driven by '$d', which this lane does not know how to run.
  It handles mac-volume (a second install) and mac-guest (a VM rehearsal)." ;;
    esac
}
SHAPE=""

lane_guest() {
    ( . "$WK_ROOT/boot/machines.sh" >/dev/null 2>&1
      machine_load "$MACHINE" >/dev/null 2>&1
      . "$WK_ROOT/boot/mac-guest.sh" >/dev/null 2>&1
      printf '%s' "${NODE_GUEST:-}" )
}

# ssh destinations, one per install. Default to MACHINE's own conf (NODE_SSH /
# NODE_BENCH_SSH, boot/machines/<machine>.conf) once MACHINE is final (below,
# after argument parsing); --host/--bench-host or WK_MAC_SSH/WK_MAC_BENCH_SSH
# still win outright.
HOST="${WK_MAC_SSH:-}"
BENCH_HOST="${WK_MAC_BENCH_SSH:-}"

TOOLS="${WK_MAC_TOOLS:-}"
BENCH_TOOLS="${WK_MAC_BENCH_TOOLS:-}"

PLAN="${WK_MAC_PLAN:-speedometer3.0}"
CONFIG="${WK_MAC_CONFIG:-mac-release}"
COUNT=""
PAYLOAD=""
DRY=""
WS=""

# docs/HANDOFF-mac-perf-mode.md: "give the first run after a stage a generous
# --timeout".
TIMEOUT="${WK_MAC_TIMEOUT:-2700}"

# Long: the wait is somebody walking to the machine, shutting it down, holding
# the power button and authenticating.
BOOT_WAIT="${WK_MAC_BOOT_WAIT:-3600}"

BACK_WAIT="${WK_MAC_BACK_WAIT:-600}"

usage() {
    sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//' >&2
    cat >&2 <<'EOF'

  <workspace>         the macOS workspace whose build is measured
  --plan <name>       which benchmark (default: speedometer3.0)
  --config <name>     which build (default: mac-release)
  --host <dest>       ssh destination for host mode (default: the machine's NODE_SSH)
  --count <n>         iterations, passed to the runner
  --payload <dir>     a pinned benchmark checkout, passed to the stage
  --timeout <s>       how long one iteration may take
  --preflight         report what the lane needs and stop
  --status            where this lane got to, without touching the machine
  --reset             forget the recorded state and start over
  --dry-run           print the remote commands instead of running them

  The lane pauses exactly once, for the startup manager. It says so loudly,
  then waits for the machine to come back in bench mode and carries on.
EOF
    exit 1
}

# --- state -------------------------------------------------------------------
# key=value and nothing else: a script greps this file for `^key=`, so a
# comment written into it would be silent prose, not a comment.

lane_state_dir() { echo "$(wk_state_dir)/mac-lane"; }
# Keyed by machine too: mbp and benchvm can share one ssh destination.
lane_state()     { echo "$(lane_state_dir)/${HOST}-${MACHINE}.state"; }

# `|| true` is load-bearing: under `set -o pipefail`, `sed` on a state file
# that does not exist yet fails the pipeline and `set -e` kills the script.
state_get() { sed -n "s/^$1=//p" "$(lane_state)" 2>/dev/null | tail -1 || true; }

state_set() {
    # A dry run must not move the lane's recorded progress.
    [ -n "$DRY" ] && return 0
    local f; f="$(lane_state)"
    ensure_dir "$(lane_state_dir)" >/dev/null
    [ -f "$f" ] || : > "$f"
    local tmp="$f.tmp"
    grep -v "^$1=" "$f" 2>/dev/null > "$tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    mv "$tmp" "$f"
}


ssh_to() {
    mac_ssh "$@"
}

rwk() {
    local mode="$1"; shift
    local dest cmd tools
    # A guest's bench mode is reached through host mode (no second ssh alias).
    # `wk enter` execs its argument directly rather than through a shell, so a
    # bare `cd x && y` fails, and `wk` is not on the guest's PATH.
    if [ "$mode" = bench ] && [ "$SHAPE" = guest ]; then
        local guest inner
        guest=$(lane_guest)
        [ -n "$guest" ] || die "$MACHINE names no guest (NODE_GUEST)"
        [ -n "$TOOLS" ] || [ -n "$DRY" ] || discover_tools host \
            || die "no wk-tools on $HOST to reach the guest through"
        inner="cd ~/wk-tools && ./wk"
        for a in "$@"; do inner="$inner $(sh_quote "$a")"; done
        cmd="cd $(sh_quote "${TOOLS:-<host wk-tools>}") && ./wk enter $(sh_quote "$guest") bash -lc $(sh_quote "$inner")"
        if [ -n "$DRY" ]; then log "  would run [bench/guest] $cmd"; return 0; fi
        debug "[bench/guest] $cmd"
        ssh_to "$HOST" "$cmd"
        return $?
    fi
    case "$mode" in
        host)  dest="$HOST";       tools="$TOOLS" ;;
        bench) dest="$BENCH_HOST"; tools="$BENCH_TOOLS" ;;
        *) die "internal: unknown mode '$mode'" ;;
    esac
    if [ -z "$tools" ]; then
        if [ -n "$DRY" ]; then
            tools="<${mode}-mode wk-tools, discovered at first use>"
        else
            discover_tools "$mode" || die "no wk-tools checkout found on the $mode install ($dest).
  \`wk bench staged\` runs from it, so the benchmark install needs this
  repository even though it must never carry a WebKit checkout
  (docs/HANDOFF-mac-perf-mode.md). Clone it there, or set WK_MAC_BENCH_TOOLS."
            case "$mode" in
                host)  tools="$TOOLS" ;;
                bench) tools="$BENCH_TOOLS" ;;
            esac
        fi
    fi
    cmd="cd $(sh_quote "$tools") && ./wk"
    local a
    for a in "$@"; do cmd="$cmd $(sh_quote "$a")"; done
    if [ -n "$DRY" ]; then
        log "  would run [$mode] $cmd"
        return 0
    fi
    debug "[$mode] $cmd"
    ssh_to "$dest" "$cmd"
}

# A raw shell command in bench mode, for the questions that are not wk verbs:
# rwk always prefixes `./wk`, so `rwk bench sh -c ...` becomes `wk sh -c ...`.
rbench_sh() {
    local script="$1" guest
    if [ "$SHAPE" = guest ]; then
        guest=$(lane_guest)
        [ -n "$TOOLS" ] || discover_tools host || return 1
        ssh_to "$HOST" "cd $(sh_quote "$TOOLS") && ./wk enter $(sh_quote "$guest") bash -lc $(sh_quote "$script")"
    else
        ssh_to "$BENCH_HOST" "bash -lc $(sh_quote "$script")"
    fi
}

# Prints host | bench | unreachable, and never fails, so callers can branch.
probe_mode() {
    local out
    # For a guest, ask its driver: there is no ssh alias to sniff /etc/wk-image
    # over. Test bench first -- the "off" answer contains the words "host mode"
    # and would misread a stopped guest as host mode.
    if [ "$SHAPE" = guest ]; then
        out=$(rwk host boot "$MACHINE" --status 2>&1) || true
        case "$out" in
            *"bench mode -- system "*)
                echo "bench:$(printf '%s' "$out" | sed -n 's/.*bench mode -- system \([^ ]*\).*/\1/p')" ;;
            *"host mode"*|*) echo host ;;
        esac
        return 0
    fi
    if out=$(ssh_to "$HOST" 'test -f /etc/wk-image && sed -n "s/^id=//p" /etc/wk-image || echo HOSTMODE' 2>/dev/null); then
        case "$out" in
            HOSTMODE) echo host ;;
            "")       echo bench-unmarked ;;
            *)        echo "bench:$out" ;;
        esac
        return 0
    fi
    # Try the bench identity at the same address before concluding it is off.
    if out=$(ssh_to "$BENCH_HOST" 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null || true' 2>/dev/null); then
        [ -n "$out" ] && { echo "bench:$out"; return 0; }
        echo bench-unmarked; return 0
    fi
    echo unreachable
}

# kern.boottime answers "has it rebooted yet" (boot/mac-volume.sh).
probe_boottime() {
    local dest="${1:-$HOST}"
    ssh_to "$dest" 'sysctl -n kern.boottime' 2>/dev/null | sed -n 's/.*sec = \([0-9]*\).*/\1/p'
}

# --- preflight ---------------------------------------------------------------
# These matter now because the expensive failures are only discoverable after
# the reboot.

# Sets TOOLS or BENCH_TOOLS and leaves the other alone; the two installs are
# different machines as far as paths go.
discover_tools() {
    local mode="$1" dest cur found="" c
    case "$mode" in
        host)  dest="$HOST";       cur="$TOOLS" ;;
        bench) dest="$BENCH_HOST"; cur="$BENCH_TOOLS" ;;
        *) die "internal: unknown mode '$mode'" ;;
    esac
    [ -n "$cur" ] && return 0
    for c in Development/wk-tools wk-tools wk/tools Development/wk/tools; do
        # $HOME stays unexpanded: it belongs to the remote shell, not this one.
        found=$(ssh_to "$dest" "cd \$HOME/$c 2>/dev/null && test -x ./wk && pwd" 2>/dev/null) || continue
        [ -n "$found" ] && break
    done
    [ -n "$found" ] || return 1
    case "$mode" in
        host)  TOOLS="$found" ;;
        bench) BENCH_TOOLS="$found" ;;
    esac
    debug "wk-tools on $dest ($mode): $found"
    return 0
}

preflight() {
    local fail=0 mode
    info "preflight: the macOS benchmark lane"

    mode=$(probe_mode)
    case "$mode" in
        unreachable)
            warn "$HOST does not answer ssh"
            log  "  macOS ships Remote Login off. Turn it on:"
            log  "    System Settings -> General -> Sharing -> Remote Login"
            log  "  or, at the keyboard on that machine:"
            log  "    sudo systemsetup -setremotelogin on"
            log  "  then authorise this machine's key:"
            log  "    $(cat "$HOME/.ssh/id_ed25519.pub" 2>/dev/null || echo '(no key at ~/.ssh/id_ed25519.pub)')"
            return 1 ;;
        host)  log "  mode: host mode -- correct for building and staging" ;;
        bench:*)
            warn "  mode: bench mode (${mode#bench:})"
            log  "  the lane builds in host mode. Reboot back first: wk bench mac --resume covers it" ;;
        bench-unmarked)
            warn "  a marker file exists but has no id= line, so the mode is undecidable"
            log  "  /etc/wk-image needs at least 'id=<something>' -- docs/HANDOFF-mac-perf-mode.md"
            fail=1 ;;
    esac

    if ! discover_tools host; then
        warn "  no wk-tools checkout found on $HOST"
        log  "  looked for ~/Development/wk-tools/wk and three other spellings;"
        log  "  set WK_MAC_TOOLS to its path, or clone it there and run ./setup"
        return 1
    fi
    log "  wk-tools: $TOOLS"

    # The commit, not just a checkout: a stale copy on the far side fails as
    # `unknown option` from a command that works fine here.
    local there_ver here_ver there here
    there_ver=$(rwk host version 2>/dev/null || true)
    here_ver=$("$WK_ROOT/cmd/version" 2>/dev/null || true)
    there=$(kv_get sha <<<"$there_ver")$([ "$(kv_get dirty <<<"$there_ver")" = yes ] && printf '+dirty')
    here=$(kv_get sha <<<"$here_ver")$([ "$(kv_get dirty <<<"$here_ver")" = yes ] && printf '+dirty')
    if [ -n "$there" ] && [ -n "$here" ] && [ "$there" != "$here" ]; then
        warn "  wk-tools on $HOST is a different commit than this one"
        log  "  here $here, there $there -- 'wk sync --tools' or a git pull over there"
        log  "  (not fatal: the lane only uses long-standing verbs)"
    fi

    # An empty formatted disk with the right name mounts perfectly and boots
    # nothing, which `wk boot mbp --status` tells apart.
    local bootstat rc=0
    bootstat=$(rwk host boot "$MACHINE" --status 2>&1) || rc=$?
    printf '%s\n' "$bootstat" | sed 's/^/    /' >&2
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ] && [ "$rc" -ne 3 ]; then
        warn "  wk boot $MACHINE --status exited $rc"
        fail=1
    fi
    # Matched on the positive signal, not a list of ways to be absent.
    if [ "$SHAPE" = guest ]; then
        info "preflight: ready (guest rehearsal -- proves every phase but the number)"
        return 0
    fi
    if ! printf '%s\n' "$bootstat" | grep -qE '^[[:space:]]*benchmark_volume=.*\(attached at '; then
        # "not attached" covers mounted-but-not-yet-macOS and absent alike.
        warn "  the benchmark volume is not usable yet: either absent, or present"
        warn "  but not a macOS system volume (an empty one mounts fine and boots nothing)"
        log  "  it is a second macOS install, made *from* that machine, named '$(printf '%s\n' "$bootstat" | sed -n 's/^[[:space:]]*benchmark_volume=\([^(]*\)(.*/\1/p' | sed 's/ *$//')'."
        log  "  Nothing can produce it over the wire. On the Mac:"
        log  "    wk bench mac-volume            # what it would do"
        log  "    wk bench mac-volume --create   # then --fetch, --install, --provision"
        fail=1
    fi

    [ "$fail" -eq 0 ] && info "preflight: ready" || warn "preflight: not ready"
    return "$fail"
}

# --- waiting for a mode ------------------------------------------------------
# Polls a mode, not reachability: both installs answer ssh.

wait_for_mode() {
    local want="$1" limit="$2" why="$3"
    local start now mode last=""
    if [ -n "$DRY" ]; then
        log "  would wait up to ${limit}s for $want mode ($why)"
        return 0
    fi
    start=$(date +%s)
    while :; do
        mode=$(probe_mode)
        case "$want:$mode" in
            host:host)    info "  $HOST is in host mode"; return 0 ;;
            bench:bench:*) info "  $HOST is in bench mode (${mode#bench:})"; return 0 ;;
        esac
        now=$(date +%s)
        if [ $((now - start)) -ge "$limit" ]; then
            warn "  gave up after ${limit}s waiting for $want mode ($why)"
            log  "  last seen: $mode. Re-run to resume -- nothing is lost."
            return 1
        fi
        [ "$mode" != "$last" ] && { log "  waiting for $want mode; now: $mode"; last="$mode"; }
        sleep 10
    done
}


# The build workspace is a macOS guest that neither `wk build` nor `wk bench
# stage` will start for you. Started here in both phases, so a lane resumed at
# the stage works; starting a running guest just returns its address.
#
# Two running macOS VMs is the hard ceiling Virtualization.framework enforces
# (a third fails with VZErrorDomain code 6), and this lane needs exactly two at
# its widest -- the build guest and the bench guest, both up during the stage.
stop_build_guest() {
    [ -n "$DRY" ] && { log "  would stop the build guest '$WS'"; return 0; }
    info "  stopping the build guest '$WS'"
    rwk host vm stop "$WS" >/dev/null 2>&1 || true
}

# wk_atexit is lib/common.sh's single EXIT trap; a second `trap ... EXIT` here
# would silently replace it.
_BUILD_GUEST_STARTED=""
_lane_cleanup() {
    [ -n "$_BUILD_GUEST_STARTED" ] || return 0
    [ -n "$DRY" ] && return 0
    rwk host vm stop "$WS" >/dev/null 2>&1 || true
}

ensure_build_guest() {
    # The benchmark install must never gain a toolchain.
    rwk host vm start "$WS" >/dev/null 2>&1 || rwk host vm start "$WS"
    if [ -z "$_BUILD_GUEST_STARTED" ]; then
        _BUILD_GUEST_STARTED=1
        wk_atexit _lane_cleanup
    fi
}

phase_build() {
    info "build: $CONFIG in the macOS guest"
    ensure_build_guest
    # Foreground by default: `wk build <vm-workspace> <config> --detach` does
    # not work on a macOS host, because WK_STORE is `/var/lib/wk` on Darwin
    # (the path inside the podman VM) but a vm workspace is a Tart guest on the
    # host, where that path does not exist -- detach_run's status file cannot
    # be written and the wrapper dies right after printing a pid. Polling `wk
    # status` right after a detach can also read the previous build's `ok`.
    if [ -n "${WK_MAC_DETACH:-}" ]; then
        local before
        before=$(rwk host status "$WS" 2>&1 || true)
        rwk host build "$WS" "$CONFIG" --detach
        [ -n "$DRY" ] && return 0
        local rc out waited=0 step=30
        while :; do
            out=$(rwk host status "$WS" 2>&1); rc=$? || true
            if [ "$out" != "$before" ] && [ "$rc" != 2 ]; then
                case "$rc" in
                    0) info "  build ok"; return 0 ;;
                    *) printf '%s\n' "$out" >&2
                       die "build did not finish (wk status exit $rc) -- 'wk logs $WS' on $HOST" ;;
                esac
            fi
            sleep "$step"; waited=$((waited + step))
            if [ "$waited" -ge "${WK_MAC_BUILD_WAIT:-14400}" ]; then
                printf '%s\n' "$out" >&2
                die "no build result after ${waited}s -- 'wk logs $WS -f' on $HOST"
            fi
        done
    fi

    rwk host build "$WS" "$CONFIG"
    [ -n "$DRY" ] || info "  build ok"
}

phase_stage() {
    if [ "$SHAPE" = guest ]; then
        info "stage: the products into the running guest, over its own ssh"
    else
        info "stage: the products onto the benchmark volume, while it is merely mounted"
    fi
    ensure_build_guest
    local args=(bench stage "$WS" --to "$MACHINE" --config "$CONFIG" --plan "$PLAN")
    [ -n "$PAYLOAD" ] && args+=(--payload "$PAYLOAD")
    rwk host "${args[@]}"

    stop_build_guest
}

phase_arm() {
    info "arm: recording the intent and printing the ritual"
    rwk host boot "$MACHINE" || true
}

# Shut down rather than reboot: the startup manager is reached by holding the
# power button from off, and a reboot lands back in the sticky default.
#
# osascript before sudo: a logged-in user can shut their own Mac down without a
# password, and the host install has no passwordless sudo. The bench install
# does, so the sudo path covers the other direction.
mac_power_off() {
    local mode="$1" dest
    case "$mode" in
        host)  dest="$HOST" ;;
        bench) dest="$BENCH_HOST" ;;
    esac
    ssh_to "$dest" 'osascript -e "tell application \"System Events\" to shut down" >/dev/null 2>&1 \
        || sudo -n shutdown -h now >/dev/null 2>&1' >/dev/null 2>&1 || true
}

say_which_disk() {
    local want="$1" disk
    case "$want" in
        bench) disk="$(printf '%s' "${NODE_VOLUME_NAME:-WK Bench}")" ;;
        host)  disk="Macintosh HD" ;;
    esac
    cat >&2 <<EOF

  ------------------------------------------------------------------
   $MACHINE is shutting down. When it is off:

       hold the power button until "Loading startup options"
       pick   $disk
       click  Continue

   Nothing else. This lane picks up again by itself once the machine
   answers in $want mode.
  ------------------------------------------------------------------

EOF
}

phase_handoff() {
    [ -n "$DRY" ] && return 0
    [ "$SHAPE" = guest ] && return 0
    cat >&2 <<EOF

  ------------------------------------------------------------------
   YOU ARE NEEDED, ONCE. This is the step no software can do:
   Apple Silicon takes its boot volume from a LocalPolicy in this
   Mac's own secure storage, changed only by an authenticated user.

     1. shut tolken down  (not restart -- shut down)
     2. hold the power button until "Loading startup options"
     3. pick  WK Bench  and click Continue
     4. authenticate if it asks, and log in at the console
        (a browser driven over ssh with nobody at the screen has
         nowhere to draw, and the run would look like a hang)

   Use the startup manager, NOT System Settings -> Startup Disk:
   the startup manager boots it once and leaves the default alone,
   so the way back is a plain reboot. The pane is sticky.

   Waiting up to $((BOOT_WAIT / 60)) minutes. Nothing is lost if it expires --
   re-run this command and it picks up here.
  ------------------------------------------------------------------

EOF
}

phase_run() {
    info "run: $PLAN, in bench mode"
    local args=(bench staged --plan "$PLAN" --timeout "$TIMEOUT")
    [ -n "$COUNT" ] && args+=(--count "$COUNT")

    # For the guest shape the build guest is still up from the stage and would
    # compete for the same CPUs during the run. The volume shape's reboot into
    # the bench install clears every host-side guest by construction.
    if [ "$SHAPE" = guest ]; then
        info "  stopping the build guest so nothing shares the CPU with the run"
        rwk host vm stop "$WS" >/dev/null 2>&1 || warn "  could not stop '$WS'; the run would share the machine"
    fi

    if [ "$SHAPE" = volume ]; then
        info "  quiescing the bench install"
        rwk bench quiesce on || warn "  quiesce reported a problem; the run will judge it"
    else
        # `wk quiesce` cannot run in a guest workspace and one check can never
        # pass there (docs/HANDOFF-mac-perf-mode.md), so the guest run always
        # needs --force -- which would also hide an unanswered Setup Assistant
        # or a MiniBrowser that never becomes active, both surfacing only as
        # run-benchmark timing out at exit 124. So the dangerous condition is
        # checked here first, through lib/quiet.sh's screen_blocker.
        local front
        front=$(rbench_sh 'cd ~/wk-tools && . lib/common.sh && . lib/quiet.sh && screen_blocker' 2>/dev/null | tr -d "\r") || front="?"
        if [ -n "$front" ] && [ "$front" != "?" ]; then
            die "'$front' owns the guest's screen.
  A benchmark in a background window is throttled by the browser, so the run
  makes no progress and times out with no error -- exit 124. --force would hide
  this, which is why it is checked first.
  Clear it:
    wk enter $(lane_guest) bash -lc 'sudo touch /var/db/.AppleSetupDone; sudo pkill -f \"$front.app\"'"
        elif [ "$front" = "?" ]; then
            warn "  could not ask the guest whether its screen is free -- continuing,"
            warn "  but a run that times out with no error is this and nothing else"
        else
            log "  the guest's screen is free (asked with the runner's own check)"
        fi
        warn "  --force: a guest cannot pass the quiet check (its update schedule"
        warn "  does not stick, and 'wk quiesce' refuses in a workspace). The result"
        warn "  records the failure; a rehearsal is not a measurement."
        args+=(--force)
    fi
    rwk bench "${args[@]}"
}

phase_back() {
    if [ "$SHAPE" = guest ]; then
        info "back: stopping the guest; the result stays in it"
        rwk host boot "$MACHINE" --back || true
        return 0
    fi
    # A plain reboot does not return this to host mode: `startosinstall` makes
    # the benchmark volume the sticky default.
    info "back: shutting down; the result stays on the volume"
    rwk bench boot "$MACHINE" --back >/dev/null 2>&1 || true
    mac_power_off bench
    say_which_disk host
}

phase_collect() {
    if [ "$SHAPE" = guest ]; then
        info "collect: listing the result inside the guest, before it is stopped"
        rwk bench bench staged --ls
        return 0
    fi
    info "collect: reading the result back from host mode"
    rwk host bench staged --ls
}


ACTION=run
while [ $# -gt 0 ]; do
    case "$1" in
        --plan)      PLAN="${2:-}"; shift 2 ;;
        --config)    CONFIG="${2:-}"; shift 2 ;;
        --machine)   MACHINE="${2:-}"; shift 2 ;;
        --host)      HOST="${2:-}"; shift 2 ;;
        --bench-host) BENCH_HOST="${2:-}"; shift 2 ;;
        --tools)     TOOLS="${2:-}"; shift 2 ;;
        --count)     COUNT="${2:-}"; shift 2 ;;
        --payload)   PAYLOAD="${2:-}"; shift 2 ;;
        --timeout)   TIMEOUT="${2:-}"; shift 2 ;;
        --preflight) ACTION=preflight; shift ;;
        --status)    ACTION=status; shift ;;
        --reset)     ACTION=reset; shift ;;
        --resume)    shift ;;   # the default; accepted because the docs say it
        --dry-run)   DRY=1; shift ;;
        -h|--help)   usage ;;
        -*)          die "unknown option: $1" ;;
        *)           [ -z "$WS" ] || die "one workspace at a time (got '$WS' and '$1')"
                     WS="$1"; shift ;;
    esac
done

SHAPE=$(lane_shape)

# Deferred until MACHINE is final, so `--machine benchvm` picks up benchvm's
# own conf rather than staying pinned to whatever MACHINE was at startup.
if [ -z "$HOST" ]; then
    machine_load "$MACHINE" >/dev/null 2>&1 || die "no such machine: $MACHINE (wk boot --list)"
    HOST="${NODE_SSH:-}"
    [ -n "$HOST" ] || die "$MACHINE (boot/machines/$MACHINE.conf) sets no NODE_SSH"
fi
# BENCH_HOST only exists for the volume shape, so a guest's empty
# NODE_BENCH_SSH is not a conf bug.
if [ "$SHAPE" = volume ] && [ -z "$BENCH_HOST" ]; then
    machine_load "$MACHINE" >/dev/null 2>&1
    BENCH_HOST="${NODE_BENCH_SSH:-}"
    [ -n "$BENCH_HOST" ] || die "$MACHINE (boot/machines/$MACHINE.conf) sets no NODE_BENCH_SSH -- needed to reach its bench-mode install"
fi

# The Mac cannot drive its own lane: the driver has to outlive a reboot of the
# machine it is driving.
# Case-folded with `tr`, not ${v,,} (macOS ships bash 3.2): this machine
# reports `Tolken` from `hostname -s` while config here spells it `tolken`.
_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
if [ "$SHAPE" = volume ] && is_macos && [ "$(_lc "$(hostname -s 2>/dev/null)")" = "$(_lc "$HOST")" ]; then
    die "this lane cannot be driven from the machine it measures -- the reboot
  in the middle would take the driver with it. Run it from another machine
  (rpi5, moose); it reaches this one over ssh."
fi

case "$ACTION" in
    status)
        f="$(lane_state)"
        [ -f "$f" ] || { log "no lane state for $HOST"; exit 0; }
        info "lane state for $HOST ($f)"
        sed 's/^/  /' "$f" >&2
        exit 0 ;;
    reset)
        rm -f "$(lane_state)"
        info "forgot the lane state for $HOST"
        exit 0 ;;
    preflight)
        preflight; exit $? ;;
esac

[ -n "$WS" ] || usage

# --- the lane ----------------------------------------------------------------
# Phases are recorded as they complete and skipped when they already have, so
# the command is the same starting a lane or resuming one after the reboot.
# `done_through` is the last phase that finished.

# --dry-run must be answerable even when preflight fails -- that is exactly
# when the plan is most worth reading.
if ! preflight; then
    [ -n "$DRY" ] || die "preflight failed -- nothing has been changed on $HOST"
    warn "preflight failed; showing the plan anyway because this is --dry-run"
fi

done_through="$(state_get done_through)"
state_set ws "$WS"
state_set plan "$PLAN"
state_set config "$CONFIG"

past() {
    local p order
    # The guest stages after entering bench mode, the volume stages before.
    if [ "$SHAPE" = guest ]; then order="build arm reboot stage run collect back"
    else                          order="build stage arm reboot run back collect"
    fi
    [ -n "$done_through" ] || return 1
    for p in $order; do
        [ "$p" = "$1" ] && return 0
        [ "$p" = "$done_through" ] && return 1
    done
    return 1
}

mark() { done_through="$1"; state_set done_through "$1"; state_set "at_$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; }

enter_bench_mode() {
    past reboot && return 0
    past arm || { phase_arm; state_set armed_boottime "$(probe_boottime "$HOST")"; mark arm; }
    phase_handoff
    if [ "$SHAPE" = volume ]; then
        info "  shutting $MACHINE down so the startup manager can be reached"
        mac_power_off host
        say_which_disk bench
    fi
    wait_for_mode bench "$BOOT_WAIT" \
        "$([ "$SHAPE" = guest ] && echo "the guest to come up and report its marker" \
                                || echo "somebody choosing WK Bench at the startup manager")" \
        || exit 75
    mark reboot
}

past build || { phase_build; mark build; }

if [ "$SHAPE" = guest ]; then
    # The guest must be started before the stage: staging into it reaches it
    # over its own ssh, while staging onto a volume only needs it mounted.
    enter_bench_mode
    past stage || { phase_stage; mark stage; }
else
    past stage || { phase_stage; mark stage; }
    enter_bench_mode
fi

past run || { phase_run; mark run; }

# Where the result physically is: on the volume, still readable by host mode
# after the reboot; inside the guest, gone once it is stopped.
if [ "$SHAPE" = guest ]; then
    past collect || { phase_collect; mark collect; }
    if ! past back; then
        phase_back
        wait_for_mode host "$BACK_WAIT" "the guest to stop" || exit 75
        mark back
    fi
else
    if ! past back; then
        phase_back
        wait_for_mode host "$BACK_WAIT" "a plain reboot back into host mode" || exit 75
        mark back
    fi
    past collect || { phase_collect; mark collect; }
fi

info "lane complete: $PLAN on $MACHINE, from $WS ($CONFIG)"
log  "  'wk bench compare' against a container run will warn on the host axis, which is the point"
log  "  'wk bench mac $WS --reset' before the next lane"
