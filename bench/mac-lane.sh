#!/usr/bin/env bash
#
# wk bench mac -- the macOS benchmark lane, end to end, driven from anywhere.
#
#   wk bench mac <workspace> [--plan P] [--config C] [--host H]
#   wk bench mac <workspace> --status | --reset | --preflight
#
# The lane is six wk commands (docs/HANDOFF-mac-perf-mode.md, "The flow"), of
# which five are automatic and one is a person holding the power button. This
# script is the thing that runs the five, knows which of the six it is on, and
# survives the reboot in the middle -- because the reboot is the whole reason
# the flow was a printed list of commands rather than a command.
#
# WHY IT RUNS HERE AND NOT ON THE MAC
#
# Every phase acts *on* tolken, so the obvious place for the driver is tolken.
# It cannot live there: phase 4 reboots the machine into a different macOS
# install, and a driver running in host mode is a process on a volume that is
# no longer running. It would have to re-establish itself as a launchd job in
# the other install, in which case the state lives on the machine being
# measured and a benchmark install grows a scheduler -- which is the same
# mistake as giving it a checkout.
#
# So the driver runs on any *other* machine, holds the state, and reaches in
# over ssh once per phase. The reboot then costs it nothing: the connection was
# never meant to survive, only the state file was. That is also why this is the
# one wk verb about the Mac that refuses to run *on* the Mac.
#
# WHAT IS AUTOMATED, AND WHAT CANNOT BE
#
# Apple Silicon takes its boot volume from a LocalPolicy in the machine's own
# secure storage, changed only by an authenticated user action -- so choosing
# the benchmark volume is a person at the keyboard, once per run, and no
# argument to this script can stand in for them (boot/mac-volume.sh has the
# long version). Everything either side of that is here, including the two
# things that were previously left to the operator's memory and are the easiest
# to get wrong:
#
#   * waiting for the right mode. "Is it back?" is not the question -- the
#     machine answers ssh in *both* modes, and a `wk bench staged` aimed at
#     host mode is refused, while a `wk build` aimed at bench mode would start
#     turning the benchmark install into a workstation. So the wait is for a
#     mode, established from /etc/wk-image, and every phase asserts the mode it
#     needs before it does anything.
#
#   * not confusing the two installs. One address, two macOS installs, two host
#     keys: see the `tolken-bench` stanza in dotfiles/ssh/config. Bench mode is
#     reached by its own ssh alias with its own HostKeyAlias, so a phase aimed
#     at the wrong mode fails on the host key instead of succeeding against the
#     wrong computer.
#
# RESUMABILITY
#
# The state file records the phase reached and the boot generation it was
# reached in, so re-running continues rather than restarting: an interrupted
# lane is resumed by repeating the command, and a lane whose build is already
# staged does not rebuild. `--status` reads it without touching the machine and
# `--reset` forgets it. Nothing here is idempotent by accident -- a phase that
# would repeat expensive work checks for the work first.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"

# The fleet name of the machine, and the profile its bench install must claim.
# Both come from boot/machines/mbp.conf rather than being repeated here, so a
# renamed volume or profile stays a one-line edit in the machine's conf.
MACHINE="${WK_MAC_MACHINE:-mbp}"

# Which shape of bench mode this machine has, read from its own conf rather
# than decided here -- boot/machines.sh is the registry and duplicating its
# answer is how the two drift.
#
#   volume  (mbp, driver mac-volume)  a second macOS install on this Mac.
#           Bench mode is reached over ssh at the same address with a different
#           host key, hence the tolken-bench alias.
#   guest   (benchvm, driver mac-guest) a macOS VM standing in for one. Bench
#           mode is reached *through the host*, because the guest's address is
#           assigned at boot and only the vm driver knows it -- so commands go
#           `wk enter <guest> -- ...` from host mode rather than over a second
#           ssh alias, and the mode is read from `wk boot <machine> --status`
#           for the same reason.
#
# The guest shape is the rehearsal: it proves every phase except the number,
# and it needs nobody at the keyboard (docs/HANDOFF-benchmarking.md).
lane_shape() {
    local d
    d=$( . "$WK_ROOT/boot/machines.sh" >/dev/null 2>&1
         machine_load "$MACHINE" >/dev/null 2>&1 && printf '%s' "${MACH_DRIVER:-}" )
    case "$d" in
        mac-guest)  echo guest ;;
        mac-volume) echo volume ;;
        '')  die "no such machine: $MACHINE (wk boot --list)" ;;
        *)   die "$MACHINE is driven by '$d', which this lane does not know how to run.
  It handles mac-volume (a second install) and mac-guest (a VM rehearsal)." ;;
    esac
}
SHAPE=""

# The guest's name, for the `wk enter` that reaches it. Same source as above.
lane_guest() {
    ( . "$WK_ROOT/boot/machines.sh" >/dev/null 2>&1
      machine_load "$MACHINE" >/dev/null 2>&1
      . "$WK_ROOT/boot/mac-guest.sh" >/dev/null 2>&1
      printf '%s' "${MACH_GUEST:-}" )
}

# ssh destinations. Two, because there are two installs -- see the header.
# Overridable because the bench install's network identity is its own: it has
# its own tailscale state, or none, in which case it needs a .local name.
HOST="${WK_MAC_SSH:-tolken}"
BENCH_HOST="${WK_MAC_BENCH_SSH:-tolken-bench}"

# Where this repository lives on the Mac -- and there are *two* answers, which
# is the whole reason this is not one variable.
#
# The two installs have different users (`justinmichaud` on the host today,
# `bench` on the benchmark install -- docs/HANDOFF-reprovision.md), so they have
# different home directories and therefore different wk-tools paths. Discovering
# the path in host mode and reusing it in bench mode would have sent every
# post-reboot phase to `/Users/justinmichaud/Development/wk-tools` on a machine
# where that user does not exist: `wk bench staged` would have failed as "no
# such file or directory", after the reboot, with the operator already up.
#
# Both discovered rather than assumed, and the bench one only once bench mode is
# actually up -- there is nothing to ask before then.
TOOLS="${WK_MAC_TOOLS:-}"
BENCH_TOOLS="${WK_MAC_BENCH_TOOLS:-}"

PLAN="${WK_MAC_PLAN:-speedometer3.0}"
CONFIG="${WK_MAC_CONFIG:-mac-release}"
COUNT=""
PAYLOAD=""
DRY=""
WS=""

# The first run after a stage is the slow one, and the reason is recorded in
# docs/HANDOFF-mac-perf-mode.md: an identical JetStream2.2 run timed out at
# 900 s and then finished in about five minutes, with a first-launch scan of
# 1.5 GB of freshly copied binaries the obvious suspect. So the default here is
# deliberately generous rather than tuned -- a lane that dies on the timeout
# costs a whole trip to the keyboard, and an over-long timeout costs nothing on
# a healthy run.
TIMEOUT="${WK_MAC_TIMEOUT:-2700}"

# How long to wait for a person. Long, because the wait is somebody walking to
# the machine, shutting it down, holding the power button and authenticating --
# and because the alternative to waiting is making them re-run the command.
BOOT_WAIT="${WK_MAC_BOOT_WAIT:-1800}"

# How long to wait for a reboot that needs nobody: back into host mode, which
# is a plain reboot the machine does by itself.
BACK_WAIT="${WK_MAC_BACK_WAIT:-600}"

usage() {
    sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//' >&2
    cat >&2 <<'EOF'

  <workspace>         the macOS workspace whose build is measured
  --plan <name>       which benchmark (default: speedometer3.0)
  --config <name>     which build (default: mac-release)
  --host <dest>       ssh destination for host mode (default: tolken)
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
#
# key=value and nothing else. A `#` line in a manifest heredoc is not a
# comment, it is prose written into the record where a reader greps for `^key=`
# and finds nothing -- which is how three lines of explanation ended up inside
# a real manifest (docs/TESTING.md). The explanation belongs in the shell.

lane_state_dir() { echo "$(wk_state_dir)/mac-lane"; }
# Keyed by machine as well as host, and that is not cosmetic: `mbp` and
# `benchvm` are two different machines sharing one ssh destination, so a single
# ${HOST}.state made the rehearsal and the real run write the *same* file.
# Having just finished a benchvm lane, an `mbp` lane would have read
# `done_through=collect` and skipped build, stage, arm and run -- reporting a
# complete lane for a benchmark volume it had never touched. Found 2026-08-22,
# before it could do that.
lane_state()     { echo "$(lane_state_dir)/${HOST}-${MACHINE}.state"; }

# `|| true` is load-bearing, not defensive habit. Under `set -o pipefail` a
# `sed` on a state file that does not exist yet fails the whole pipeline, and
# `set -e` then kills the script at the *first* read -- which is every fresh
# lane. The symptom was a run that printed the preflight and then simply
# stopped, with no error and exit 0, because the failure was in a command
# substitution being assigned. A missing state file is the normal starting
# state, so it has to read as empty rather than as an error.
state_get() { sed -n "s/^$1=//p" "$(lane_state)" 2>/dev/null | tail -1 || true; }

state_set() {
    # A dry run must not move the lane. Without this it recorded `done_through`
    # as it walked the phases, so the *next* real run skipped build and stage
    # and went looking for products nobody had staged -- a dry run that changes
    # what a later real run does is worse than no dry run.
    [ -n "$DRY" ] && return 0
    local f; f="$(lane_state)"
    ensure_dir "$(lane_state_dir)" >/dev/null
    [ -f "$f" ] || : > "$f"
    local tmp="$f.tmp"
    grep -v "^$1=" "$f" 2>/dev/null > "$tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    mv "$tmp" "$f"
}

# --- reaching the machine ----------------------------------------------------

ssh_to() {
    local dest="$1"; shift
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$dest" "$@"
}

# A wk command over there, in a named mode. The mode is not decoration: it
# selects the ssh alias, and therefore the host key, so a command that would
# have run against the wrong install fails to connect instead.
rwk() {
    local mode="$1"; shift
    local dest cmd tools
    # A guest's bench mode is reached *through* host mode, not beside it: the
    # guest's address is assigned at boot and only the vm driver knows it, so
    # there is no second ssh alias to aim at. `wk enter <guest> bash -lc ...`
    # is the form -- `wk enter` execs its argument directly rather than through
    # a shell (so `cd x && y` fails as "No such file or directory"), and `wk`
    # is not on the guest's PATH (so a bare `wk` fails as "command not found").
    # Both established by trying them, 2026-08-22.
    if [ "$mode" = bench ] && [ "$SHAPE" = guest ]; then
        local guest inner
        guest=$(lane_guest)
        [ -n "$guest" ] || die "$MACHINE names no guest (MACH_GUEST)"
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
    # Bench mode's path cannot be known before bench mode exists, so it is
    # discovered at first use rather than in the preflight.
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

# A raw shell command in bench mode, for the questions that are not wk verbs.
#
# rwk always prefixes `./wk`, so `rwk bench sh -c ...` becomes `wk sh -c ...`,
# which is not a command -- the pre-run screen check silently degraded to its
# "could not ask" branch because of exactly that. A separate helper rather than
# a flag on rwk, because the two have different shapes: this one takes a script,
# not an argument list.
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

# Which mode is the machine in *right now*? The marker file is the only
# answer -- `wk bench staged` refuses without it and is right to, because
# nothing else distinguishes the two installs from the outside. Prints
# host | bench | unreachable, and never fails, so callers can branch.
probe_mode() {
    local out
    # For a guest, ask its driver rather than sniffing /etc/wk-image over an
    # ssh alias that does not exist. Note the parse order: the "off" answer is
    # `benchvm: unreachable over ssh in host mode`, which contains the words
    # "host mode" -- so bench has to be tested first or a stopped guest reads
    # as host mode by accident. And for a guest shape, "not in bench mode" *is*
    # host mode: tolken itself is always there to build and stage on.
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
    # Host mode's key did not match or did not answer. In bench mode the
    # address is the same and only the key differs, so try that identity
    # before concluding the machine is off.
    if out=$(ssh_to "$BENCH_HOST" 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null || true' 2>/dev/null); then
        [ -n "$out" ] && { echo "bench:$out"; return 0; }
        echo bench-unmarked; return 0
    fi
    echo unreachable
}

# The machine's own idea of when it booted, which is what makes "has it
# rebooted yet" answerable rather than guessed. kern.boottime does for macOS
# what Linux's random boot_id does, and is a clock reading as well
# (boot/mac-volume.sh).
probe_boottime() {
    local dest="${1:-$HOST}"
    ssh_to "$dest" 'sysctl -n kern.boottime' 2>/dev/null | sed -n 's/.*sec = \([0-9]*\).*/\1/p'
}

# --- preflight ---------------------------------------------------------------
#
# Every one of these was either a documented failure or a refusal already built
# into `wk bench staged`, and the point of asking now is that the expensive ones
# are only discoverable *after* the reboot. A lane that is going to fail on a
# missing python should fail before somebody walks to the machine.

# $1 is the mode, because the two installs are different machines as far as
# paths are concerned. Sets TOOLS or BENCH_TOOLS and leaves the other alone.
discover_tools() {
    local mode="$1" dest cur found="" c
    case "$mode" in
        host)  dest="$HOST";       cur="$TOOLS" ;;
        bench) dest="$BENCH_HOST"; cur="$BENCH_TOOLS" ;;
        *) die "internal: unknown mode '$mode'" ;;
    esac
    [ -n "$cur" ] && return 0
    for c in Development/wk-tools wk-tools wk/tools Development/wk/tools; do
        # $HOME stays unexpanded on purpose: it belongs to the remote shell,
        # and resolving it here would resolve this machine's home -- which is
        # the same mistake, one level down, as sharing one path between the two
        # installs. `pwd` on the far side turns whichever spelling matched into
        # an absolute path, so every later phase quotes one known-good directory.
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

    # The tree hash, not just the sha: a stale copy of this repository on the
    # far side fails as `unknown option` from a command that works perfectly
    # here, which cmd/status exists partly to catch.
    local there here
    there=$(rwk host version --tree 2>/dev/null || true)
    here=$("$WK_ROOT/cmd/version" --tree 2>/dev/null || true)
    if [ -n "$there" ] && [ -n "$here" ] && [ "$there" != "$here" ]; then
        warn "  wk-tools on $HOST is a different tree than this one"
        log  "  here $here, there $there -- 'wk sync --target' or a git pull over there"
        log  "  (not fatal: the lane only uses verbs both copies have had for a while)"
    fi

    # The volume. `wk boot mbp --status` is the authority and already
    # distinguishes "mounted" from "mounted and actually a macOS system
    # volume" -- an empty formatted disk with the right name mounts perfectly
    # and boots nothing.
    local bootstat rc=0
    bootstat=$(rwk host boot "$MACHINE" --status 2>&1) || rc=$?
    printf '%s\n' "$bootstat" | sed 's/^/    /' >&2
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ] && [ "$rc" -ne 3 ]; then
        warn "  wk boot $MACHINE --status exited $rc"
        fail=1
    fi
    # Matched on the *positive* signal, not on a list of ways it can be absent.
    # The first version of this grepped for "not attached" and friends, and
    # passed a machine whose status said `benchmark_volume=WK Bench (not
    # attached)` because that spelling was not in the list -- a preflight that
    # reports ready and then fails at the stage, which is the exact failure the
    # preflight exists to prevent. boot/mac-volume.sh emits
    # `(attached at <path>)` and nothing else means attached, so that is what
    # is required. A new spelling on the driver's side then fails closed.
    # `[[:space:]]*` is not defensive noise: the driver indents these lines by
    # two spaces under the machine's own heading, so a `^`-anchored match never
    # fired -- and because this fails closed, it would have blocked the lane
    # even once the volume was attached. Verified against real output with
    # `cat -A` rather than read off the echo in boot/mac-volume.sh, which is
    # where the indentation is added by the caller and so is invisible.
    if [ "$SHAPE" = guest ]; then
        # Nothing to check: the guest either exists and carries a marker or
        # b_arm refuses, and that refusal is better than a copy of it here.
        # `--status` above already printed the guest and its state.
        info "preflight: ready (guest rehearsal -- proves every phase but the number)"
        return 0
    fi
    if ! printf '%s\n' "$bootstat" | grep -qE '^[[:space:]]*benchmark_volume=.*\(attached at '; then
        # "not attached" from the driver covers two different states, because
        # mac_volume_present means mounted *and* a macOS system volume. Saying
        # only "not there" about a volume that is sitting mounted in /Volumes is
        # how somebody goes looking for a disk problem instead of running the
        # install -- which is exactly the state this Mac was in on 2026-08-22,
        # with an empty 'WK Bench' created and no macOS on it yet.
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
#
# The one place the lane is allowed to block for a long time. It polls a mode
# rather than reachability, because both installs answer ssh and only one of
# them is the one that was asked for.

wait_for_mode() {
    local want="$1" limit="$2" why="$3"
    local start now mode last=""
    # The wait is the longest thing the lane does and a dry run must not do it:
    # `--dry-run` sat here polling for a reboot that was never going to happen,
    # which looked exactly like a hang and is the opposite of what the flag is
    # for.
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

# --- the phases --------------------------------------------------------------

# The build workspace is a macOS guest, and neither `wk build` nor
# `wk bench stage` will start it for you -- `wk build` refuses outright with
# "'<ws>' is not running (wk vm start <ws>)", and the stage reaches into it over
# its own ssh. So starting it is part of both phases rather than an assumption
# either of them makes, which also makes a *resumed* lane work: a lane picked up
# at the stage has a stopped build guest, and staging out of a stopped guest is
# the same failure one phase later.
#
# Idempotent by the vm driver's own contract: t_start on a running guest returns
# its address and succeeds.
#
# Two running macOS VMs is the hard ceiling Virtualization.framework enforces
# (a third fails with VZErrorDomain code 6), and this lane needs exactly two at
# its widest -- the build guest and the bench guest, both up during the stage.
# That is why the build guest is not left running past the collect.
# Stopping is idempotent and never fatal: this runs on the failure path too,
# where the interesting error is the one that got us here, not a tidy-up.
stop_build_guest() {
    [ -n "$DRY" ] && { log "  would stop the build guest '$WS'"; return 0; }
    info "  stopping the build guest '$WS'"
    rwk host vm stop "$WS" >/dev/null 2>&1 || true
}

# Registered once the guest has been started, so a lane that dies at any later
# phase does not leave a macOS VM running on somebody's workstation. wk_atexit
# is lib/common.sh's single EXIT trap -- a second `trap ... EXIT` here would
# silently replace whatever it had registered.
_BUILD_GUEST_STARTED=""
_lane_cleanup() {
    [ -n "$_BUILD_GUEST_STARTED" ] || return 0
    [ -n "$DRY" ] && return 0
    rwk host vm stop "$WS" >/dev/null 2>&1 || true
}

ensure_build_guest() {
    # Both shapes, and the guard that used to be here ("guest only") was a
    # confusion between the two guests in play. The *bench* target differs by
    # shape -- a VM for the rehearsal, a volume for the real thing -- but the
    # *build* always happens in a macOS vm workspace on tolken, because that is
    # where builds belong and the benchmark install must never gain a toolchain.
    # So the build guest needs starting either way, and skipping it for the
    # volume shape failed the real lane on its first run with
    # "'bench-build' is not running".
    rwk host vm start "$WS" >/dev/null 2>&1 || rwk host vm start "$WS"
    if [ -z "$_BUILD_GUEST_STARTED" ]; then
        _BUILD_GUEST_STARTED=1
        wk_atexit _lane_cleanup
    fi
}

phase_build() {
    info "build: $CONFIG in the macOS guest"
    ensure_build_guest
    # --detach, because the build outlives this connection by design: a link
    # that drops during a 40-minute build must not take the build with it, and
    # the state to poll is on the machine that is doing the work.
    # Foreground, and --detach only on request. That is the opposite of the
    # obvious choice, so the reason is worth recording:
    #
    # `wk build <vm-workspace> <config> --detach` does not work on a macOS host.
    # It returns 0, prints a pid, and the pid is gone immediately having built
    # nothing. The cause is the store: on Darwin `WK_STORE` is `/var/lib/wk` by
    # design, because that is the path *inside* the podman VM and container
    # commands are forwarded into it -- but a vm workspace is a Tart guest, so
    # `wk build` is not forwarded, runs on the host, and `/var/lib/wk` does not
    # exist there. detach_run's status file cannot be written and the wrapper
    # dies. `wk status` and `wk logs` still answer, because the vm driver reads
    # those from the guest, which is what made this look like a working build.
    # (docs/TESTING.md records the same $WK_STORE trap for the image store.)
    #
    # Measured 2026-08-22. Two failure modes came out of it, and the second is
    # the dangerous one: polling `wk status` straight after a detach reads the
    # *previous* build's `ok`, so the lane reported success in seconds, staged a
    # two-day-old build, and would have published a number for a tree nobody had
    # just compiled -- it printed `8m1s`, the figure recorded for the build of
    # 2026-08-20.
    #
    # In the foreground the exit status is the build's own, there is nothing to
    # poll and nothing stale to believe, and the output streams where it can be
    # read. The connection has to survive the build, which is what the
    # ServerAliveInterval on this host's ssh entry is for -- and the lane is
    # resumable, so a dropped one costs a rebuild rather than the run.
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

    # The build guest has done its job. Stopping it here rather than later is
    # the difference between a machine with one VM on it and a machine with a
    # spare macOS guest burning cores next to a benchmark.
    #
    # It used to be stopped only for the guest shape, on the reasoning that the
    # volume shape reboots and takes every host-side guest with it. That is true
    # only if the lane reaches the reboot: this one died at the stage and left
    # `bench-build` running for three quarters of an hour, which a person
    # noticed and the tooling did not.
    stop_build_guest
}

phase_arm() {
    info "arm: recording the intent and printing the ritual"
    # Prints the ritual and reboots nothing. The intent is recorded over there
    # because that is where the machine's own boot identity is readable.
    rwk host boot "$MACHINE" || true
}

# Put the machine where a person can act on it, then tell them what to do --
# on *this* screen, which is the one they are looking at.
#
# The alternative, and what this used to do, was print a ritual into a terminal
# on the machine that was about to be rebooted out from under it and then wait.
# That asks somebody to read instructions on a screen that is going away, and to
# remember them across a shutdown. Shutting the machine down *first* means the
# only instruction left is which disk to pick, and it is on the driving machine
# where it stays readable.
#
# Shut down rather than reboot, because the startup manager is reached by
# holding the power button *from off*. A reboot would land back in whatever the
# sticky default is, which is exactly the wrong thing when the point is to
# choose.
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

# What to pick, printed where it can be read.
say_which_disk() {
    local want="$1" disk
    case "$want" in
        bench) disk="$(printf '%s' "${MACH_VOLUME_NAME:-WK Bench}")" ;;
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
    # The whole point of the guest shape: arming it started it, so there is
    # nobody to interrupt and nothing to say.
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

    # Nothing else may be running on the machine while it is being measured, and
    # for the guest shape that is not automatic: the build guest was started for
    # the build and the stage, and leaving it up means a second macOS VM
    # competing for the same CPUs *during the measurement*. Found 2026-08-22 by
    # someone looking at the screen and seeing two windows -- an earlier comment
    # here claimed the build guest "is not left running past the collect", which
    # was true of the intent and false of the code.
    #
    # Not for the volume shape: there the machine reboots into the bench install,
    # so every guest on the host side is gone by construction.
    if [ "$SHAPE" = guest ]; then
        info "  stopping the build guest so nothing shares the CPU with the run"
        rwk host vm stop "$WS" >/dev/null 2>&1 || warn "  could not stop '$WS'; the run would share the machine"
    fi

    if [ "$SHAPE" = volume ]; then
        # Quiet the machine first. `wk bench staged` checks quietness and
        # refuses without it -- it does not do the quieting, and saying so in
        # its preflight ("'wk quiesce on' first") makes that the caller's job.
        # This was missing, and the run refused on it.
        info "  quiescing the bench install"
        rwk bench quiesce on || warn "  quiesce reported a problem; the run will judge it"
    else
        # The guest needs --force, and the interesting part is what has to be
        # true before forcing is honest.
        #
        # `wk quiesce` cannot run here at all: the bench guest is also a
        # workspace (`wk new` made it), and quiesce refuses in a workspace --
        # the only machine in the fleet that is both. And one quiet check cannot
        # pass in a guest regardless: `softwareupdate --schedule off` reports
        # "on" afterwards no matter what is written to AutomaticCheckEnabled.
        # That one really is a guest quirk, recorded in
        # docs/HANDOFF-mac-perf-mode.md and confirmed here 2026-08-22 -- it
        # persists with Setup Assistant dismissed, so it is not a pane holding
        # the decision open, which is what this code first assumed.
        #
        # But --force is all-or-nothing: it forces *every* preflight failure,
        # so using it for the benign one also forces past a real one. That is
        # not hypothetical -- it is what happened. An unanswered Setup Assistant
        # owned the front window, MiniBrowser never became active, Speedometer
        # was throttled in the background and run-benchmark timed out at exit
        # 124, while the only complaint on screen was the update schedule.
        #
        # So the lane asserts the dangerous condition itself before forcing the
        # harmless one. `wk bench staged` now has a "the screen is free" check
        # of its own; this is the same question asked *before* --force can hide
        # the answer.
        # Asked through lib/quiet.sh's screen_blocker, the same function
        # `wk bench staged` uses -- not a second list of apps here. A narrower
        # copy of this check is what let `Software Update` through after
        # `Setup Assistant` was dismissed: the runner failed it correctly and
        # this forced past it, because "the dangerous condition" had been
        # spelled out in two places and only one of them was right.
        local front
        front=$(rbench_sh 'cd ~/wk-tools && . lib/common.sh && . lib/quiet.sh && screen_blocker' 2>/dev/null | tr -d "\r") || front="?"
        if [ -n "$front" ] && [ "$front" != "?" ]; then
            die "'$front' owns the guest's screen.
  A benchmark in a background window is throttled by the browser, so the run
  makes no progress and times out with no error -- exit 124, measured
  2026-08-22. --force would hide this, which is why it is checked first.
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
        # Stopping a guest is a host-mode act, and for a guest leaving the role
        # *is* leaving the machine -- there is nothing to reboot.
        info "back: stopping the guest; the result stays in it"
        rwk host boot "$MACHINE" --back || true
        return 0
    fi
    # Not "a plain reboot returns it to host mode" -- that assumption is false
    # after `startosinstall`, which makes the benchmark volume the *sticky*
    # default. A reboot from bench mode then lands back in bench mode, and the
    # lane waits for a host mode that is never coming. Shut down and say which
    # disk instead: correct whichever way the default happens to point.
    info "back: shutting down; the result stays on the volume"
    rwk bench boot "$MACHINE" --back >/dev/null 2>&1 || true
    mac_power_off bench
    say_which_disk host
}

phase_collect() {
    if [ "$SHAPE" = guest ]; then
        # From inside the guest, where the result was written. `--ls` in host
        # mode looks for a benchmark *volume*, which this Mac has none of, and
        # says so -- correctly, about a question that does not apply here.
        info "collect: listing the result inside the guest, before it is stopped"
        rwk bench bench staged --ls
        return 0
    fi
    info "collect: reading the result back from host mode"
    rwk host bench staged --ls
}

# --- argument parsing --------------------------------------------------------

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

# The Mac cannot drive its own lane: the driver has to outlive a reboot of the
# machine it is driving. Refused here rather than discovered at phase 4, with
# the machine already armed.
# Case-folded, and with `tr` rather than ${v,,} because macOS ships bash 3.2.
# This machine reports `Tolken` from `hostname -s` while every config here
# spells it `tolken`, so a case-sensitive comparison made the guard silently
# never fire -- which is worse than not having it, because the refusal it is
# supposed to produce would instead have been a reboot killing the driver.
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
#
# Phases are recorded as they complete and skipped when they already have, so
# the command is the same whether it is starting a lane or resuming one after
# the reboot. `done_through` is the last phase that finished; a phase is run
# when the lane has not got past it.

# --dry-run is a question about the plan, so it must be answerable on a machine
# that is not ready to run it -- which is exactly when the plan is worth
# reading. Before this, --dry-run died in the preflight and printed nothing:
# the one form of the command that touches nothing was the one form you could
# not use until everything already worked.
if ! preflight; then
    [ -n "$DRY" ] || die "preflight failed -- nothing has been changed on $HOST"
    warn "preflight failed; showing the plan anyway because this is --dry-run"
fi

done_through="$(state_get done_through)"
state_set ws "$WS"
state_set plan "$PLAN"
state_set config "$CONFIG"

past() {
    # Is $1 at or before the recorded high-water mark?
    local p order
    # The order is the shape's, not a constant, because the guest stages *after*
    # entering bench mode and the volume stages before. A single hardcoded list
    # silently skipped the guest's stage phase: `mark reboot` sat past `stage`
    # in the list, so `past stage` answered true for work that had never run,
    # and the lane went from build straight to the benchmark with nothing
    # staged. A high-water mark only means anything against the order actually
    # walked.
    if [ "$SHAPE" = guest ]; then order="build arm reboot stage run collect back"
    else                          order="build stage arm reboot run back collect"
    fi
    [ -n "$done_through" ] || return 1
    # Walking in order, whichever is met first decides it: meeting $1 first
    # means it sits at or before the mark and is done; meeting the mark first
    # means the lane stopped short of $1.
    for p in $order; do
        [ "$p" = "$1" ] && return 0
        [ "$p" = "$done_through" ] && return 1
    done
    return 1
}

mark() { done_through="$1"; state_set done_through "$1"; state_set "at_$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; }

# Entering bench mode: arm, tell whoever has to act, wait for the mode.
# Factored out because the two shapes need it at different points in the
# sequence -- see below.
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
    # The guest is started *before* the stage, and this is a real difference
    # rather than a tidier ordering: staging into a guest reaches it over its
    # own ssh (b_bench_put dies with "'<guest>' is not running"), while staging
    # onto a volume only needs it mounted, which it already is in host mode.
    # Same phases, in the order this shape requires.
    enter_bench_mode
    past stage || { phase_stage; mark stage; }
else
    past stage || { phase_stage; mark stage; }
    enter_bench_mode
fi

past run || { phase_run; mark run; }

# Collect before or after leaving bench mode, and which one is not a style
# choice -- it is where the result physically is.
#
#   volume  the result is written onto the benchmark volume, which host mode can
#           still read once the machine has rebooted. Collecting afterwards is
#           the point: it proves the result survived the way back.
#   guest   the result is written *inside the guest*, and leaving the role means
#           stopping the guest. Collecting afterwards asked host mode to read a
#           benchmark volume this Mac does not have, and got exactly that error.
#
# So the guest reads its result while it is still running, and the volume reads
# its own after the reboot. Same phases, ordered by where the bytes live.
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
