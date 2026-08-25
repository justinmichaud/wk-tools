#!/usr/bin/env bash
#
# wk bench mac-ab -- an interleaved A/B on this Mac's benchmark install, with
# nobody in the room.
#
#   wk bench mac-ab [<workspace>] [--plan P] [--rounds N] [--count N]
#                   [--a <staged-id>] [--b <staged-id>] [--stage]
#   wk bench mac-ab <workspace> --patch <ref|diff> [--base <ref>] [--rounds N]
#   wk bench mac-ab --preflight | --status | --collect | --dry-run
#
# The difference from `wk bench mac` is where the run is driven from, and it is
# forced by the machine rather than chosen: **the benchmark install has no
# network.** tolken is Wi-Fi only and that install joins nothing (an empty
# PreferredOrder in its own airport preferences), so the
# phase-by-phase ssh the lane in bench/mac-lane.sh depends on has nowhere to
# connect. Giving it credentials needs the System keychain and
# SystemConfiguration, both root, in the mode where root costs a password.
#
# So the job is *planted* instead of driven: everything the run needs is written
# onto the volume while it is merely mounted, a per-user LaunchAgent starts it
# when the bench account auto-logs in, and this driver's job after the reboot is
# only to wait and read.
#
# WHAT IS AND IS NOT UNATTENDED, TESTED RATHER THAN ASSUMED
#
# Everything here needs no person and no password:
#
#   staging          /var/wk on the bench volume is owned by uid 501, which is
#                    this account in host mode and the bench account over there.
#   planting         ~bench is uid 501 too, so its LaunchAgents directory is
#                    writable from here without sudo.
#   the reboot       `osascript … restart` reboots as the logged-in user. No
#                    sudo, no password.
#   the run          the bench account has NOPASSWD sudo on that install, so
#                    `wk quiesce` works with nobody to ask.
#   coming back      the autorun reboots when it finishes.
#
# One thing is not, and no flag can make it so: **which volume the firmware
# boots.** Even in a macOS guest with SIP disabled and root,
# `nvram boot-volume=<other group>` exits 0 and changes nothing (the value goes
# to the 7C436110-… namespace and is discarded; IODeviceTree:/options keeps the
# firmware's own value across a reboot), `bless --setBoot` says "not supported
# on Apple Silicon based systems" in its own man page, and
# `systemsetup -getstartupdisk` prints "(null)". SIP is not the gate: the
# variable is firmware-owned, not SIP-protected.
#
# So this driver does not pretend to switch the boot volume. It reboots, and
# then reports which mode came back:
#
#   the firmware default is Macintosh HD   the reboot lands in host mode. Then
#                                          entering bench mode is the one human
#                                          step, and everything after it --
#                                          including coming back -- is not.
#   the firmware default is WK Bench       the reboot lands in bench mode, the
#                                          A/B runs itself, and the machine
#                                          comes back or (if it lands here
#                                          again) halts. Zero human steps.
#
# Either way it is at most one human action per A/B, not per run: the planted
# job holds every round of every arm, so a longer experiment costs the same one
# action as a short one. And no path loops or hangs -- the autorun advances its
# state before it runs anything, counts its attempts, and carries a watchdog.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"

HOST="${WK_MAC_SSH:-tolken}"
MACHINE="${WK_MAC_MACHINE:-mbp}"
VOLUME="${WK_BENCH_VOLUME:-WK Bench}"
PLAN="${WK_MAC_PLAN:-speedometer3.0}"
CONFIG="${WK_MAC_CONFIG:-mac-release}"
ROUNDS=2
COUNT=""
TIMEOUT=1800
SETTLE=90
A_ID=""; B_ID=""
PATCH=""; BASE_REF=""; ARMS_PENDING=""
A_ARGS=""; B_ARGS=""
WS=""
DO_STAGE=""
ALLOW_FETCH=""
FORCE=""
AGENT_HOME=""
DRY=""
ACTION=run
BOOT_WAIT="${WK_MAC_BOOT_WAIT:-3600}"

usage() { sed -n '4,10p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# --- reaching the Mac --------------------------------------------------------
#
# One helper, so there is one place that decides how a command gets over there
# and one place a --dry-run can intercept. BatchMode: an unattended lane that
# stops at a password prompt is a hang, and it should read as a failure instead.
mac() {
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"
}
mac_sh() { mac bash -lc "$(sh_quote "$*")"; }

# The machine's hardware UUID, which is what macOS names ByHost preferences by.
# Read from host mode and used for the bench account's files: it identifies the
# *machine*, and the two installs are one machine.
mac_hw_uuid() {
    mac_sh 'ioreg -rd1 -c IOPlatformExpertDevice' 2>/dev/null \
        | awk -F'"' '/IOPlatformUUID/{print $4; exit}' | tr -d '\r'
}

# wk-tools over there, discovered rather than assumed: the two installs have
# different users and therefore different paths, and hardcoding this machine's
# answer is how a post-reboot phase ends up at a home directory that does not
# exist (bench/mac-lane.sh records that mistake).
TOOLS="${WK_MAC_TOOLS:-}"
host_tools() {
    [ -n "$TOOLS" ] && { printf '%s' "$TOOLS"; return 0; }
    TOOLS=$(mac_sh 'for d in ~/Development/wk-tools ~/wk-tools; do [ -x "$d/wk" ] && { echo "$d"; exit 0; }; done' 2>/dev/null | tr -d '\r' | head -1)
    [ -n "$TOOLS" ] || die "cannot find wk-tools on $HOST"
    printf '%s' "$TOOLS"
}
rwk() { mac_sh "cd $(sh_quote "$(host_tools)") && ./wk $*"; }

# The copy of wk-tools that was planted *onto the volume*, run from host mode.
#
# Not the host install's own checkout, and the difference matters for exactly one
# verb: `wk bench ab-summary` is new, so an older tree on the Mac would not have
# it -- and the tree that is guaranteed to be the same age as the job is the one
# the plant put there. It also keeps this lane from having to write into somebody
# else's working copy to do its own job.
bwk() { mac_sh "cd $(sh_quote "$(bench_root)/wk-tools") && ./wk $*"; }

# Getting files onto the volume, over ssh, without depending on how the far end
# spells a path with spaces in it.
#
# rsync was the obvious tool and it is the wrong one here. The benchmark volume
# is `/Volumes/WK Bench - Data/…` -- two spaces in the default name -- and how
# rsync sends that depends on both ends: GNU rsync 3.2.4+ transmits arguments in
# the protocol (secluded-args on by default) and the raw path works, while an
# escaped path arrives with its backslashes intact and fails with `open: No
# such file or directory` on a path containing literal backslashes. That is
# true of openrsync, which is what this Mac ships, and not true in general.
#
# tar over ssh has no such dependency: the remote path appears once, inside a
# command this side quotes, and every tar there has ever been extracts a
# relative archive into `-C`. So the transfers use that, and the fleet's other
# rsync users (t_pull_dir, which copies within one machine) are left alone.
put_file() {  # $1 = local file, $2 = remote path
    mac "cat > $(sh_quote "$2")" < "$1" || return 1
    # Byte count, both ends. A `cat >` that wrote nothing exits 0.
    local want got
    want=$(wc -c < "$1" | tr -d ' ')
    got=$(mac "wc -c < $(sh_quote "$2")" 2>/dev/null | tr -d ' \r')
    [ "$want" = "$got" ] || {
        warn "put_file: $2 is $got bytes, expected $want"
        return 1
    }
}

# $1 = local dir, $2 = remote dir -- replaced wholesale, then *verified*.
#
# The verification is the point. Without it this function reports success, the
# lane reboots the machine, and the tree on the volume is still the previous
# one -- so the benchmark install runs an older `lib/quiet.sh`, one that reads
# `softwareupdate --schedule`, which this repository documents as lying on
# macOS 26, and every arm fails its `quiet machine` check on a machine that is
# correctly configured, after the reboot, where nothing could say so.
#
# What was wrong underneath is less interesting than the shape of the mistake:
# a transfer whose success was inferred from an exit status rather than from the
# far end. docs/TESTING.md's first rule is "test the property, not the
# configuration", and this broke it. So the tree is now checked by asking the
# other side for a file that only the new tree has.
put_tree() {
    local src="$1" dst="$2"
    tar -cf - --exclude '.git' -C "$src" . \
        | mac "rm -rf $(sh_quote "$dst") && mkdir -p $(sh_quote "$dst") && tar -xf - -C $(sh_quote "$dst")" \
        || return 1

    # A sentinel that is part of *this* lane, so an older tree cannot satisfy it,
    # plus a byte count so a truncated copy cannot either.
    local probe="bench/mac-ab.sh" want got
    want=$(wc -c < "$src/$probe" | tr -d ' ')
    got=$(mac "wc -c < $(sh_quote "$dst/$probe") 2>/dev/null" 2>/dev/null | tr -d ' \r')
    if [ -z "$got" ]; then
        warn "put_tree: $dst/$probe is not there -- the tree did not land"
        return 1
    fi
    [ "$want" = "$got" ] || {
        warn "put_tree: $dst/$probe is $got bytes, expected $want"
        return 1
    }
    log "  verified: $dst carries this lane's own tree ($probe, $got bytes)"
}

# The bench volume's paths as host mode sees them. Asked of the machine's own
# boot driver rather than spelled here, because "where do staged builds live"
# is a property of the machine's boot arrangement (boot/mac-volume.sh).
BROOT=""
bench_root() {
    [ -n "$BROOT" ] && { printf '%s' "$BROOT"; return 0; }
    BROOT=$(mac_sh "cd $(sh_quote "$(host_tools)") && . lib/common.sh && . boot/machines.sh && machine_load $(sh_quote "$MACHINE") && load_driver \"\$MACH_DRIVER\" && b_bench_root" 2>/dev/null | tr -d '\r' | tail -1)
    # Two different situations produce this, and the message has to cover both,
    # because the second is the *normal* state to hit it in: the volume is only
    # `/Volumes/<name>` while host mode is running. Asked in bench mode, where
    # the same volume is `/`, the driver correctly answers "not attached" -- so
    # every verb here is a host-mode verb, and saying that is more useful than
    # repeating the driver's words.
    [ -n "$BROOT" ] || die "'$VOLUME' is not visible from $HOST right now.
    Either it is not attached, or $HOST is *in* bench mode -- that install's own
    root is the volume, so it is not mounted under /Volumes and every verb here
    is a host-mode verb. 'wk boot $MACHINE --status' over there says which.
    In bench mode the run drives itself; read it back once the machine returns."
    printf '%s' "$BROOT"
}

bench_home() {
    # ~bench as host mode sees it, from the volume rather than from a guess:
    # the account name is the install's, not this driver's.
    local d; d=$(dirname "$(bench_root)")          # …/private/var
    printf '%s' "$(dirname "$(dirname "$d")")/Users/bench"
}

# --- preflight ---------------------------------------------------------------
#
# Every check here is a thing that, if wrong, is discovered *after* the reboot
# on a machine that cannot be reached. That is the whole reason the list is
# this long: the cost of a late failure is a trip to the keyboard.
PF_FAIL=0
ck() {  # ck yes|no <label> <detail>
    if [ "$1" = yes ]; then printf '  \033[32mok\033[0m   %-24s %s\n' "$2" "$3" >&2
    else PF_FAIL=$((PF_FAIL + 1)); printf '  \033[31mFAIL\033[0m %-24s %s\n' "$2" "$3" >&2; fi
}

preflight() {
    info "preflight for an unattended A/B on $HOST"
    PF_FAIL=0

    local mode
    if mode=$(mac 'cat /etc/wk-image 2>/dev/null | sed -n "s/^id=//p"' 2>/dev/null); then
        mode=$(printf '%s' "$mode" | tr -d '\r')
        if [ -n "$mode" ]; then
            ck no "host mode" "$HOST is in BENCH mode ($mode) -- plant from host mode"
        else
            ck yes "host mode" "$HOST answers and carries no bench marker"
        fi
    else
        ck no "reachable" "$HOST does not answer ssh with a key"
        log "  everything below needs the machine, so nothing else was checked." >&2
        return 1
    fi

    local root; root=$(bench_root 2>/dev/null) || root=""
    if [ -n "$root" ]; then ck yes "bench volume" "$VOLUME at $root"
    else ck no "bench volume" "'$VOLUME' is not attached"; return 1; fi

    if mac "test -w $(sh_quote "$root")" 2>/dev/null; then
        ck yes "staging root" "writable without sudo"
    else
        ck no "staging root" "$root is not writable as this account -- staging would need sudo"
    fi

    local bh; bh=$(bench_home)
    if mac "test -d $(sh_quote "$bh") && test -w $(sh_quote "$bh/Library")" 2>/dev/null; then
        ck yes "bench home" "$bh (LaunchAgents installable without sudo)"
    else
        ck no "bench home" "$bh/Library is not writable -- the agent cannot be planted"
    fi

    # The autologin that gives the run a console session. Without it the browser
    # has nowhere to draw and the run looks like a hang -- and over there
    # nothing can tell us so.
    local alu
    alu=$(mac "defaults read $(sh_quote "$bh/../../Library/Preferences/com.apple.loginwindow") autoLoginUser 2>/dev/null" 2>/dev/null | tr -d '\r')
    if [ "$alu" = bench ]; then ck yes "autologin" "the bench account logs in at the console"
    else ck no "autologin" "autoLoginUser is '${alu:-unset}' -- the run would have no session"; fi

    # sshd being enabled on that install is not needed for the run, and is
    # reported rather than required: it is the difference between a failed run we
    # can read afterwards and one we can read *during*, if the install ever
    # gains a network.
    local sshd
    sshd=$(mac "/usr/libexec/PlistBuddy -c 'Print :com.openssh.sshd' $(sh_quote "$bh/../../private/var/db/com.apple.xpc.launchd/disabled.plist") 2>/dev/null" 2>/dev/null | tr -d '\r')
    log "  note remote login on the bench install: $([ "$sshd" = false ] && echo enabled || echo "disabled/unknown ($sshd)")" >&2

    # A python that can import objc, on the *bench* install, checked from here
    # by looking for the package rather than by running it.
    if mac "test -d $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages/objc")" 2>/dev/null; then
        ck yes "pyobjc over there" "in the bench account's site-packages"
    else
        ck no "pyobjc over there" "run-benchmark's prepare_env does a bare 'import objc'"
    fi

    if mac "test -d $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages/scipy")" 2>/dev/null; then
        ck yes "scipy over there" "the A/B can be compared on the volume"
    else
        log "  note scipy is not on the bench install; --plant installs it (needs this machine's network)" >&2
    fi

    # The *planted* copy, which is what the autorun runs. The install's own
    # ~bench/Development/wk-tools is not checked, deliberately: it is not used,
    # and provisioning rewrites it.
    if mac "test -x $(sh_quote "$root/wk-tools/wk")" 2>/dev/null; then
        ck yes "planted wk-tools" "$root/wk-tools"
    else
        log "  note nothing planted yet; --plant puts this lane's own tree at $root/wk-tools" >&2
    fi

    # Provisioning that should have finished. A first-boot daemon that cannot
    # remove itself reverts tooling and reboots the machine a minute into a run.
    # The autorun defuses it, and saying so here is the difference between a
    # cycle that looks inexplicable and one that is expected to need a first
    # pass.
    if mac "test -f $(sh_quote "$bh/../../Library/LaunchDaemons/com.wk.bench-firstboot.plist")" 2>/dev/null; then
        log "  note the first-boot daemon is still installed on this volume. It reverts" >&2
        log "       tooling and reboots the machine ~1 min into a boot; the autorun" >&2
        log "       cancels the reboot and removes it, so this cycle spends its first" >&2
        log "       boot defusing it. The cycle after this one is clean." >&2
    fi

    # What is staged, which decides whether this is an A/A control or a real A/B.
    local staged
    staged=$(mac "ls -1 $(sh_quote "$root/staged") 2>/dev/null" 2>/dev/null | tr -d '\r' | sort)
    if [ -n "$staged" ]; then
        ck yes "staged builds" "$(printf '%s' "$staged" | tr '\n' ' ')"
    elif [ -n "$DO_STAGE" ] || [ -n "$WS" ]; then
        log "  note nothing staged yet; --stage will put a build there" >&2
    else
        ck no "staged builds" "nothing on the volume, and no workspace given to stage from"
    fi

    # The one fact that decides how many human steps this costs. Reported, never
    # asserted: it cannot be changed from software (see the header) and it cannot
    # be read with certainty either -- the firmware's own variable is the best
    # evidence there is, and the startup manager can override it without
    # updating it.
    local bv grp
    bv=$(mac 'nvram -p 2>/dev/null | awk -F"\t" "\$1==\"boot-volume\"{print \$2}"' 2>/dev/null | tr -d '\r')
    grp="${bv##*:}"
    local bench_grp host_grp
    bench_grp=$(mac "diskutil info $(sh_quote "/Volumes/$VOLUME") 2>/dev/null | sed -n 's/.*APFS Volume Group: *//p'" 2>/dev/null | tr -d '\r' | head -1)
    host_grp=$(mac "diskutil info / 2>/dev/null | sed -n 's/.*APFS Volume Group: *//p'" 2>/dev/null | tr -d '\r' | head -1)
    log "" >&2
    log "  the firmware's boot-volume names:" >&2
    if [ -n "$grp" ] && [ "$grp" = "$bench_grp" ]; then
        log "    $grp  = '$VOLUME'" >&2
        log "    so a plain reboot is expected to land in BENCH mode, and this A/B" >&2
        log "    needs no human at all. If it lands in host mode instead, the" >&2
        log "    startup manager is the one step -- the job stays planted for it." >&2
    elif [ -n "$grp" ] && [ "$grp" = "$host_grp" ]; then
        log "    $grp  = the host install" >&2
        log "    so a plain reboot returns here, and entering bench mode is the one" >&2
        log "    human step: hold the power button, pick '$VOLUME'. Everything" >&2
        log "    after that, including coming back, is unattended." >&2
    else
        log "    ${grp:-<unreadable>}  (matches neither install)" >&2
    fi

    log "" >&2
    [ "$PF_FAIL" -eq 0 ] || { warn "$PF_FAIL preflight check(s) failed"; return 1; }
    info "preflight clean"
    return 0
}

# --- staging -----------------------------------------------------------------

phase_stage() {
    [ -n "$WS" ] || die "--stage needs a workspace to stage from"
    info "stage: $CONFIG from '$WS' onto $VOLUME"
    [ -n "$DRY" ] && { log "  would: wk vm start $WS; wk bench seed $WS $PLAN; wk bench stage $WS --to $MACHINE"; return 0; }
    rwk vm start "$WS" >/dev/null 2>&1 || true
    # The payload, pinned, and this is not optional here: the bench install has
    # no network, so a run-benchmark that wants to clone Speedometer has nowhere
    # to clone it from and the run dies over there where nobody can see it.
    # stdout only, and stderr left to flow to the terminal. `2>&1 | tail -1`
    # was here and it is a race: `wk bench seed` writes its progress to stderr
    # and the directory to stdout, and merging two differently-buffered streams
    # to take the last line sometimes takes the progress message instead of the
    # answer. The path is the whole point of the call, so it is read off the
    # stream that carries it.
    local payload
    payload=$(rwk bench seed "$WS" "$PLAN" | tr -d '\r' | tail -1) || payload=""
    case "$payload" in /*) ;; *) payload="" ;; esac
    # And confirmed on the machine that has to read it, not on the strength of
    # having been printed: a payload named but absent produces a run-benchmark
    # `--local-copy` pointing at nothing.
    if [ -n "$payload" ] && mac "test -d $(sh_quote "$payload")" 2>/dev/null; then
        log "  payload pinned: $payload"
    elif [ -n "$ALLOW_FETCH" ]; then
        payload=""
        warn "  no pinned payload, and --allow-network-fetch was given. Each run will
  clone $PLAN itself, so the benchmark install needs a working network *and*
  the two arms could in principle get different revisions of the benchmark."
    else
        # A refusal, not a warning.
        #
        # Staging with no pinned payload and saying so as a warning goes ahead
        # anyway. The benchmark install then has no route out -- its Wi-Fi is
        # its own and its remembered-networks list can be empty -- so every arm
        # dies
        # in run-benchmark's fetch, on a machine with no network to report it
        # over, and the whole cycle spent a reboot and half an hour to produce
        # nothing. By the time that is knowable the machine is gone.
        #
        # Which makes this exactly the class of thing a preflight is for: the
        # payload is cheap to pin *here*, where there is a network, and there is
        # no version of "we will find out over there" that is worth a cycle.
        die "the $PLAN payload could not be pinned, so nothing may be staged.

    Each run would clone the benchmark itself, over a network the benchmark
    install may not have -- and if it does not, every arm fails after the
    reboot, where nothing can say so.

    Pin it here, where the network is:
        wk bench seed $WS $PLAN
    then re-run. If that fails, its error is the thing to fix -- it reads the
    plan file out of '$WS', so the workspace has to be one that has the tree.

    To go ahead anyway (a benchmark install you know has a route out, and a
    revision of the benchmark you are content to have chosen for you):
        --allow-network-fetch"
    fi
    rwk bench stage "$WS" --to "$MACHINE" --config "$CONFIG" --plan "$PLAN" \
        ${payload:+--payload "$payload"}

    # And stop the guest again. Not tidiness: a running macOS VM is the single
    # largest thing on this machine's CPU (measured at 233% while the rehearsal
    # ran), and the next thing this lane does is reboot the host into the
    # install that is about to be measured. Leaving it up costs nothing if the
    # reboot lands in bench mode -- the guest is gone with the OS -- and costs a
    # whole measurement's worth of noise if the reboot lands back in host mode
    # and somebody runs the benchmark there instead. bench/mac-lane.sh had to
    # learn the same thing about its own two guests.
    info "  stopping the build guest"
    rwk vm stop "$WS" >/dev/null 2>&1 || warn "  could not stop '$WS'"
}

# --- building both arms ------------------------------------------------------
#
# `--patch` is the whole point of the lane in one command: a patch in, a number
# out. Without it, getting an A/B number meant ten commands and two by-hand git
# operations in a guest -- which is how the first real A/B on this machine was
# run, and it is not a procedure anyone should follow twice.
#
# Two builds, two stages, one plant. The baseline is built first and from the
# same tree, because an A/B whose arms came from different checkouts is not an
# A/B; and the tree is put back afterwards, because it is somebody's working
# copy and this command borrows it.
#
# `--patch` takes either a git ref (whose tree *is* the patched state) or a diff
# file on this machine, which is copied over and applied. A ref is the honest
# default for real work -- it is what a branch under review looks like.

# One command in the build guest, from here, two shells away.
#
# Written to a file and then run, rather than piped into `bash`: a script on
# stdin is consumed by the first thing inside it that reads stdin, which
# silently truncates the rest: `wk bench seed` eats the remainder of a staging
# script and the whole thing exits after its first line looking like a clean
# success.
guest_sh() {
    local b; b=$(printf '%s' "$1" | base64 | tr -d '\n')
    mac_sh "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new wk-$(sh_quote "$WS") 'printf %s $b | base64 -d > /tmp/wk-guest.sh && bash /tmp/wk-guest.sh'"
}

guest_src() {
    local d
    d=$(guest_sh 'for p in ~/WebKit ~/webkit; do [ -d "$p/.git" ] && { echo "$p"; exit 0; }; done' 2>/dev/null | tr -d '\r' | head -1)
    [ -n "$d" ] || die "no WebKit checkout found in the build guest '$WS'"
    printf '%s' "$d"
}

staged_ids() {
    mac "ls -1 $(sh_quote "$(bench_root)/staged") 2>/dev/null" 2>/dev/null | tr -d '\r' | sort
}

# Build the tree as it stands and stage it; print the staged id that appeared.
#
# The id is taken as the directory that is new since before the stage rather than
# parsed out of the log: `wk bench stage` prints a path, but a parse is a second
# thing to keep in step with its output and this comparison cannot drift.
build_and_stage() {
    local label="$1" before after id
    before=$(staged_ids)
    info "  building $label"
    rwk build "$WS" "$CONFIG" >&2 || die "the $label build failed"
    local payload
    payload=$(rwk bench seed "$WS" "$PLAN" | tr -d '\r' | tail -1) || payload=""
    case "$payload" in /*) ;; *) die "could not pin the $PLAN payload for $label" ;; esac
    info "  staging $label"
    rwk bench stage "$WS" --to "$MACHINE" --config "$CONFIG" --plan "$PLAN" \
        --payload "$payload" >&2 || die "staging $label failed"
    after=$(staged_ids)
    id=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | tail -1)
    [ -n "$id" ] || die "staging $label produced no new directory on $VOLUME"
    log "  $label staged as $id" >&2
    printf '%s' "$id"
}

phase_build_ab() {
    [ -n "$WS" ] || die "--patch needs a workspace to build in (wk bench mac-ab <ws> --patch ...)"
    [ -n "$DRY" ] && {
        log "  would build the baseline (${BASE_REF:-current HEAD}) in '$WS' and stage it"
        log "  would then apply '$PATCH' and stage that as the second arm"
        # Named rather than left empty. An empty A_ID falls through to the
        # plant's "default B to A" rule, which then reports a confident A/A
        # about two builds that do not exist yet -- a dry run describing a plan
        # that is not the plan.
        ARMS_PENDING=1
        A_ID="<baseline ${BASE_REF:-HEAD}, would be built>"
        B_ID="<patched $PATCH, would be built>"
        return 0
    }
    rwk vm start "$WS" >/dev/null 2>&1 || true
    local src; src=$(guest_src)
    log "  checkout: $src"

    # What to put back. A detached HEAD prints nothing for --abbrev-ref, so the
    # sha is the fallback and it is what actually restores the tree.
    local orig
    orig=$(guest_sh "git -C $src symbolic-ref --quiet --short HEAD 2>/dev/null || git -C $src rev-parse HEAD" | tr -d '\r' | head -1)
    [ -n "$orig" ] || die "could not read the guest checkout's current ref"
    log "  will restore '$orig' when done"

    local base="${BASE_REF:-$orig}"
    guest_sh "set -e; git -C $src checkout -q $base" >/dev/null \
        || die "could not check out the baseline '$base' in the guest"
    A_ID=$(build_and_stage "baseline ($base)")

    # The patched arm. A readable file here is a diff; anything else is a ref
    # over there. Applied on top of the baseline, never on top of whatever the
    # tree happened to be.
    if [ -f "$PATCH" ]; then
        log "  applying diff $PATCH"
        mac "cat > /tmp/wk-ab.patch" < "$PATCH" || die "could not copy the patch to $HOST"
        guest_sh "set -e; cd $src && git apply --index /tmp/wk-ab.patch" >/dev/null 2>&1 \
            || { guest_sh "git -C $src checkout -q $orig" >/dev/null 2>&1
                 die "the patch did not apply cleanly to '$base'; the tree has been put back"; }
    else
        log "  checking out patched ref $PATCH"
        guest_sh "set -e; git -C $src checkout -q $PATCH" >/dev/null \
            || { guest_sh "git -C $src checkout -q $orig" >/dev/null 2>&1
                 die "no such ref '$PATCH' in the guest checkout; the tree has been put back"; }
    fi
    B_ID=$(build_and_stage "patched ($PATCH)")

    # Put the tree back whatever happened above. It is a working copy this
    # command borrowed, and leaving it on a build branch is the kind of thing
    # that is discovered days later.
    guest_sh "git -C $src checkout -q $orig" >/dev/null 2>&1 \
        || warn "  could not restore '$orig' in the guest -- the tree is left on the patched ref"

    # Stopping the guest is not tidiness: a running macOS VM was measured at
    # 233% CPU, and the next thing this lane does is reboot into the install it
    # is about to measure.
    info "  stopping the build guest"
    rwk vm stop "$WS" >/dev/null 2>&1 || warn "  could not stop '$WS'"

    info "arms: A=$A_ID  B=$B_ID"
}

# --- planting ----------------------------------------------------------------
#
# Everything the run needs, written while the volume is merely mounted. Nothing
# here needs a password, which is the property that makes the whole lane
# possible; if any of it ever does, that is a bug rather than a prompt.
phase_plant() {
    local root bh stamp
    root=$(bench_root); bh=$(bench_home)
    stamp=$(date -u +%Y%m%dT%H%M%SZ)

    # Which builds the two arms run. Defaulting B to A is the control, and it is
    # said out loud rather than left to be discovered in the summary.
    # Skipped entirely when the arms are placeholders from a --patch dry run:
    # they name builds that do not exist yet, so validating them against the
    # volume would fail on exactly the run that is meant to change nothing.
    if [ -z "$ARMS_PENDING" ]; then
        local staged
        staged=$(mac "ls -1 $(sh_quote "$root/staged") 2>/dev/null" 2>/dev/null | tr -d '\r' | sort)
        [ -n "$staged" ] || die "nothing staged on $VOLUME -- pass a workspace with --stage, or --patch to build both arms"
        [ -n "$A_ID" ] || A_ID=$(printf '%s' "$staged" | tail -1)
        [ -n "$B_ID" ] || B_ID="$A_ID"
        printf '%s\n' "$staged" | grep -qx "$A_ID" || die "no staged build '$A_ID' on $VOLUME. There is:
$(printf '%s' "$staged" | sed 's/^/    /')"
        printf '%s\n' "$staged" | grep -qx "$B_ID" || die "no staged build '$B_ID' on $VOLUME"
    fi

    info "plant: $PLAN, $ROUNDS round(s), interleaved"
    log  "  arm A: $A_ID${A_ARGS:+  args: $A_ARGS}"
    log  "  arm B: $B_ID${B_ARGS:+  args: $B_ARGS}"
    [ "$A_ID" = "$B_ID" ] && [ "$A_ARGS" = "$B_ARGS" ] && \
        warn "  both arms are the same build with the same arguments: this is an A/A
  control. It measures this lane's noise floor, which is the thing you need
  before any real A/B means anything -- but it is not a comparison of two builds."

    if [ -n "$DRY" ]; then
        log "  would sync wk-tools to $root/wk-tools"
        log "  would install $root/bin/mac-bench-autorun.sh"
        log "  would write $root/job.json and reset $root/autorun.state"
        log "  would install $bh/Library/LaunchAgents/com.wk.bench-ab.plist"
        return 0
    fi

    # wk-tools into /var/wk, not into ~bench/Development.
    #
    # The autorun runs *this* copy, so a driver newer than the volume would
    # otherwise plant a job the runner does not understand. It went to
    # ~bench/Development/wk-tools -- where the install's own copy lives -- and
    # that turned out to be the one directory on the volume that something else
    # rewrites: bench/mac-bench-firstboot.sh `rsync --delete`s its payload copy
    # over it on every boot it runs, so a correctly planted tree is replaced by
    # an older one between the plant and the run. `put_tree` verifies the tree,
    # the verification is true when it is made, and the tree is gone by the time
    # the benchmark reads it.
    #
    # /var/wk is this lane's own directory. Nothing in provisioning writes into
    # it beyond creating it, so a tree here survives a boot that re-runs
    # provisioning, and the run does not depend on what the install happens to
    # carry.
    info "  syncing wk-tools onto the volume"
    put_tree "$WK_ROOT" "$root/wk-tools" \
        || die "could not sync wk-tools onto the bench volume"

    # scipy, so the verdict can be computed over there. Installed from here
    # because here is where the network is -- same machine, same python 3.9,
    # same architecture, so the wheel that fits this account fits that one.
    if ! mac "test -d $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages/scipy")" 2>/dev/null; then
        info "  installing scipy into the bench account's site-packages"
        mac "/usr/bin/python3 -m pip install --quiet --target $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages") scipy" \
            >/dev/null 2>&1 || warn "  scipy did not install; the A/B will be compared from host mode instead"
    fi

    # --- the screen lock, settled BEFORE the reboot --------------------------
    #
    # This is the governing rule of this lane applied to the class of failure
    # that had escaped it: anything discoverable only after the reboot has to be
    # refused, verified or defused before it. The screensaver and its lock were
    # neither: nothing in this repository touches them and a provisioning list
    # that asserts them anyway leaves the benchmark install free to lock itself.
    # A lock mid-run is the same "nowhere to draw" failure
    # as a stolen focus, and it is invisible to `screen_blocker`, which asks the
    # window server for the frontmost *application*.
    #
    # It can be settled from here, and that is the point. These are *per-user*
    # preferences, and ~bench is uid 501 -- this account in host mode -- so they
    # are writable while the volume is merely mounted, exactly like the staging
    # and the agent. Written to the file by absolute path, with no cfprefsd on
    # either side owning it (that volume is not running), then read back. The
    # ByHost UUID is the machine's, and both installs are the same machine.
    #
    # `wk quiesce` enforces the same two settings again at run time. That is not
    # redundancy for its own sake: this half can be *verified before anything
    # reboots*, which is the half that was missing, and the run-time half covers
    # a volume planted by an older tree.
    local sspath="$bh/Library/Preferences/ByHost/com.apple.screensaver.$(mac_hw_uuid)"
    mac "mkdir -p $(sh_quote "$bh/Library/Preferences/ByHost")"
    mac "defaults write $(sh_quote "$sspath") idleTime -int 0" >/dev/null 2>&1 || true
    mac "defaults write $(sh_quote "$bh/Library/Preferences/com.apple.screensaver") askForPassword -int 0" >/dev/null 2>&1 || true
    local ssv
    ssv=$(mac "defaults read $(sh_quote "$sspath") idleTime 2>/dev/null" 2>/dev/null | tr -d '\r ')
    if [ "$ssv" = 0 ]; then
        log "  screen lock: screensaver disabled on the volume (idleTime=0, verified)"
    elif [ -n "$FORCE" ]; then
        warn "  screen lock: could not disable the screensaver (idleTime reads '${ssv:-unreadable}').
  --force given, so planting anyway: the screen may lock during a run."
    else
        die "could not disable the screensaver on $VOLUME -- idleTime reads '${ssv:-unreadable}'.

    Refused here rather than discovered later. A benchmark makes no keyboard or
    mouse input, so the idle timer runs at full load exactly as it does on an
    abandoned machine; the lock behind it ends a run with the silent timeout that
    nothing over there can report.

    Nothing has been done to the machine yet. To plant anyway:  --force"
    fi

    info "  installing the autorun"
    mac "mkdir -p $(sh_quote "$root/bin") $(sh_quote "$bh/Library/LaunchAgents")"
    put_file "$WK_ROOT/bench/mac-bench-autorun.sh" "$root/bin/mac-bench-autorun.sh" \
        || die "could not install the autorun script"
    mac "chmod 0755 $(sh_quote "$root/bin/mac-bench-autorun.sh")"

    info "  writing the job"
    # The job, as json, written through python so that quoting is not a shell
    # problem: staged ids and browser arguments both reach this from a command
    # line and one of them can contain spaces.
    WK_JOB_PLAN="$PLAN" WK_JOB_ROUNDS="$ROUNDS" WK_JOB_TIMEOUT="$TIMEOUT" \
    WK_JOB_COUNT="$COUNT" WK_JOB_SETTLE="$SETTLE" \
    WK_JOB_A="$A_ID" WK_JOB_B="$B_ID" WK_JOB_AA="$A_ARGS" WK_JOB_BA="$B_ARGS" \
    WK_JOB_TOOLS="/var/wk/wk-tools" WK_JOB_BY="$(hostname)" \
    WK_JOB_STAMP="$stamp" WK_JOB_FORCE="$FORCE" \
    python3 - <<'PYEOF' > "$(wk_state_dir)/mac-ab-job.json"
import json, os
g = os.environ.get
arms = [{"label": "A", "id": g("WK_JOB_A"), "browser_args": g("WK_JOB_AA") or ""}]
if g("WK_JOB_B"):
    arms.append({"label": "B", "id": g("WK_JOB_B"), "browser_args": g("WK_JOB_BA") or ""})
print(json.dumps({
    "plan": g("WK_JOB_PLAN"),
    "rounds": int(g("WK_JOB_ROUNDS")),
    "timeout": int(g("WK_JOB_TIMEOUT")),
    "count": g("WK_JOB_COUNT") or "",
    "settle": int(g("WK_JOB_SETTLE")),
    "n_arms": len(arms),
    "arms": arms,
    "wk_tools": g("WK_JOB_TOOLS"),
    "created_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    "created_by": g("WK_JOB_BY"),
    "stamp": g("WK_JOB_STAMP"),
    "force": bool(g("WK_JOB_FORCE")),
}, indent=2))
PYEOF
    put_file "$(wk_state_dir)/mac-ab-job.json" "$root/job.json" \
        || die "could not write the job onto the volume"

    # The state file is reset here and nowhere else: the autorun only ever
    # advances it, so a fresh job needs a fresh state or the run would be
    # skipped as already done.
    mac "printf 'phase=planted\njob_stamp=%s\nattempts=0\nplanted_at=%s\n' \
        $(sh_quote "$stamp") $(sh_quote "$(date -u +%Y-%m-%dT%H:%M:%SZ)") > $(sh_quote "$root/autorun.state")"
    mac "mkdir -p $(sh_quote "$root/ab/$stamp")"

    info "  installing the launch agent"
    # RunAtLoad and nothing else: no KeepAlive, because a benchmark that has
    # finished must not be restarted, and no StartInterval, because this is a
    # one-shot job and not a schedule. The agent removes itself when the job it
    # was planted for is done.
    mac "cat > $(sh_quote "$bh/Library/LaunchAgents/com.wk.bench-ab.plist")" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wk.bench-ab</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/var/wk/bin/mac-bench-autorun.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>/var/wk/autorun.agent.log</string>
  <key>StandardErrorPath</key><string>/var/wk/autorun.agent.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>WK_AB_ROOT</key><string>/var/wk</string></dict>
</dict>
</plist>
PLIST
    mac "chmod 0644 $(sh_quote "$bh/Library/LaunchAgents/com.wk.bench-ab.plist")"

    info "planted: $stamp"
    log  "  job    $root/job.json"
    log  "  agent  $bh/Library/LaunchAgents/com.wk.bench-ab.plist"
    log  "  log    $root/autorun.log   (readable from host mode afterwards)"
    printf '%s' "$stamp"
}

# --- going, and coming back --------------------------------------------------

# The boot this lane started from, so "it came back" can be checked instead of
# assumed. Set by phase_go and read by phase_wait.
BOOT_BEFORE=""

mac_boottime() {
    mac_sh 'sysctl -n kern.boottime 2>/dev/null' 2>/dev/null \
        | tr -d '\r' | sed -n 's/.*sec *= *\([0-9]*\).*/\1/p' | head -1
}

phase_go() {
    [ -n "$DRY" ] && { log "  would reboot $HOST (loginwindow restart event, no sudo)"; return 0; }
    BOOT_BEFORE=$(mac_boottime)
    info "go: rebooting $HOST (boot before: ${BOOT_BEFORE:-unknown})"

    # HOW THIS MACHINE IS ACTUALLY REBOOTED, AND WHY THE OBVIOUS SPELLING IS A
    # SILENT NO-OP.
    #
    # `tell application "System Events" to restart`, with
    # `|| sudo -n shutdown -r now` behind it and both ends silenced, is the
    # third member of a family this repository keeps meeting: **it returns 0 and
    # does not reboot.** `lsappinfo front` is `loginwindow` -- host mode sits at
    # the login screen with nobody logged into the GUI -- so the restart has no
    # user session to be carried out in. rc=0, `kern.boottime` unchanged, and
    # the lane announces a reboot,
    # waited out its head start, found the machine answering, and reported
    # "back in HOST mode" about a machine that had never left. A planted job
    # went unconsumed and the whole cycle produced nothing, silently.
    #
    # (The `sudo -n` behind it could never have covered for it either: host mode
    # has no blanket NOPASSWD, only wk-quiesce-priv and wk-tftpd. Its failure
    # was invisible behind the first command's false success.)
    #
    # The **loginwindow restart Apple event** works here with no password, no
    # sudo and no GUI session, because loginwindow is running in every state
    # this machine is ever in -- it is what the login screen's own Restart
    # button uses. Written to a file with octal escapes rather than inlined:
    # the event code is guillemets, and those do not survive being passed
    # through two shells reliably. The escapes go in printf's *format*, not in
    # an argument to %s -- printf expands \\ooo in the format only, so the %s
    # spelling writes the literal text "\\302\\253" into the script and osascript
    # rejects it. Caught before it ran; it would have looked exactly like the
    # silent no-op this whole comment is about.
    #
    # Backgrounded and its exit status ignored on purpose. A successful reboot
    # kills the ssh connection carrying it, so "the command failed" and "the
    # command worked" look identical from here. Which is why the next thing
    # this function does is stop believing exit statuses altogether.
    mac_sh 'printf "tell application \"loginwindow\" to \302\253event aevtrrst\302\273\n" > /tmp/wk-restart.scpt
            (osascript /tmp/wk-restart.scpt >/dev/null 2>&1 &)
            exit 0' >/dev/null 2>&1 || true

    # THE REBOOT IS VERIFIED, NOT REPORTED. This is the whole lesson of the bug
    # above: every mechanism available here can claim success without acting, so
    # the only trustworthy signal is the machine actually going away. Nothing
    # downstream runs until it has.
    local waited=0
    while [ "$waited" -lt 150 ]; do
        mac_sh true >/dev/null 2>&1 || { info "  $HOST is going down (after ${waited}s)"; return 0; }
        sleep 5
        waited=$((waited + 5))
    done

    # Still answering after two and a half minutes, so the event was refused or
    # ignored. Try the privileged spelling once -- on a machine that *does* have
    # passwordless sudo it is the simplest thing that works -- and then give up
    # loudly rather than waiting an hour for a reboot that is not coming.
    warn "  the loginwindow restart event did not take; trying sudo -n"
    mac_sh 'sudo -n shutdown -r now >/dev/null 2>&1' >/dev/null 2>&1 || true
    waited=0
    while [ "$waited" -lt 60 ]; do
        mac_sh true >/dev/null 2>&1 || { info "  $HOST is going down (after sudo)"; return 0; }
        sleep 5
        waited=$((waited + 5))
    done

    die "could not reboot $HOST -- it is still answering, and \`kern.boottime\` is
    unchanged. Both mechanisms exit 0 without acting, so this is checked rather
    than trusted; see the comment in phase_go.

    Nothing has been lost: the job is planted, so rebooting the machine by hand
    -- or booting '$VOLUME' from the startup manager -- runs the A/B and needs
    nothing further from here."
}

phase_wait() {
    local limit="$1" start now mode last=""
    # A dry run has to *say* it did not wait, not return an empty answer: the
    # caller branches on this value, and an empty one fell through to "tolken is
    # not answering" -- a dry run reporting an outage.
    [ -n "$DRY" ] && { log "  would wait up to ${limit}s for $HOST to answer again"; printf 'dry'; return 0; }
    info "wait: up to $((limit / 60)) minutes for $HOST to answer"
    start=$(date +%s)
    # Give the reboot a head start: for the first few seconds the machine is
    # still up and answering, and a poll that succeeds immediately would report
    # "back in host mode" about the machine that has not gone yet.
    sleep 45
    while :; do
        if mode=$(mac 'cat /etc/wk-image 2>/dev/null | sed -n "s/^id=//p"; echo READY' 2>/dev/null); then
            mode=$(printf '%s' "$mode" | tr -d '\r' | head -1)
            # Same boot as before means it never rebooted, whatever it says
            # about its mode. Without this the old code reported "back in HOST
            # mode" about a machine that had simply never gone down -- the
            # failure phase_go's comment describes, arriving one function later.
            local bt; bt=$(mac_boottime)
            if [ -n "$BOOT_BEFORE" ] && [ "$bt" = "$BOOT_BEFORE" ]; then
                warn "  $HOST is answering on the SAME boot ($bt) -- it never rebooted"
                printf 'noreboot'; return 1
            fi
            case "$mode" in
                READY) info "  $HOST is back in HOST mode"; printf 'host'; return 0 ;;
                *)     info "  $HOST answers in BENCH mode ($mode)"; printf 'bench'; return 0 ;;
            esac
        fi
        now=$(date +%s)
        if [ $((now - start)) -ge "$limit" ]; then
            warn "  $HOST has not answered in ${limit}s"
            printf 'silent'; return 1
        fi
        [ "$last" != waiting ] && { log "  no answer yet (this is the reboot, or bench mode, which has no network)"; last=waiting; }
        sleep 20
    done
}

# --- reading it back ---------------------------------------------------------

phase_collect() {
    local root; root=$(bench_root)
    info "collect: reading the A/B off $VOLUME"

    local st; st=$(mac "cat $(sh_quote "$root/autorun.state") 2>/dev/null" 2>/dev/null | tr -d '\r')
    if [ -n "$st" ]; then
        log "  autorun state:"
        printf '%s\n' "$st" | sed 's/^/    /' >&2
    else
        warn "  no autorun state on the volume -- the agent never ran"
    fi

    local stamp; stamp=$(printf '%s\n' "$st" | sed -n 's/^job_stamp=//p' | tail -1)
    local runs="$root/ab/$stamp/runs.tsv"
    if [ -n "$stamp" ] && mac "test -f $(sh_quote "$runs")" 2>/dev/null; then
        log ""
        log "  runs:"
        mac "cat $(sh_quote "$runs")" 2>/dev/null | sed 's/^/    /' >&2
        log ""
        bwk bench ab-summary --root "$(sh_quote "$root")" --runs "$(sh_quote "$runs")" || \
            warn "  the summary could not be produced; the results are still on the volume"
    else
        warn "  no run map at $runs -- no arm completed"
        log  "  the autorun's own log is the place to look:"
        log  "    ssh $HOST tail -60 $(sh_quote "$root/autorun.log")"
    fi
}

phase_status() {
    local root; root=$(bench_root 2>/dev/null) || die "'$VOLUME' is not attached on $HOST"
    local mode; mode=$(mac 'cat /etc/wk-image 2>/dev/null | sed -n "s/^id=//p"' 2>/dev/null | tr -d '\r')
    # Not `${mode:+bench mode ($mode)}${mode:-host mode}`: when mode is set the
    # second expansion yields the value again, so it printed
    # "bench mode (perf-macos-tolken-2026-08)perf-macos-tolken-2026-08".
    if [ -n "$mode" ]; then info "$HOST is in bench mode ($mode)"
    else                   info "$HOST is in host mode"; fi
    mac "cat $(sh_quote "$root/job.json") 2>/dev/null" 2>/dev/null | sed 's/^/  /' >&2 \
        || log "  no job planted"
    log ""
    mac "cat $(sh_quote "$root/autorun.state") 2>/dev/null" 2>/dev/null | sed 's/^/  /' >&2 \
        || log "  no autorun state"
    log ""
    log "  last 20 lines of the autorun log:"
    mac "tail -20 $(sh_quote "$root/autorun.log") 2>/dev/null" 2>/dev/null | sed 's/^/    /' >&2 \
        || log "    (none)"
}

# --- arguments ---------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --plan)     PLAN="${2:-}"; shift 2 ;;
        --config)   CONFIG="${2:-}"; shift 2 ;;
        --rounds)   ROUNDS="${2:-}"; shift 2 ;;
        --count)    COUNT="${2:-}"; shift 2 ;;
        --timeout)  TIMEOUT="${2:-}"; shift 2 ;;
        --settle)   SETTLE="${2:-}"; shift 2 ;;
        --patch)    PATCH="${2:-}"; shift 2 ;;
        --base)     BASE_REF="${2:-}"; shift 2 ;;
        --a)        A_ID="${2:-}"; shift 2 ;;
        --b)        B_ID="${2:-}"; shift 2 ;;
        --a-args)   A_ARGS="${2:-}"; shift 2 ;;
        --b-args)   B_ARGS="${2:-}"; shift 2 ;;
        --host)     HOST="${2:-}"; shift 2 ;;
        # Which wk-tools on the Mac runs the host-side verbs (`bench seed`,
        # `bench stage`). Normally discovered, and worth being able to override
        # for the case that actually happens: this lane is newer than the
        # checkout on the machine, so seeding and staging have to come out of a
        # tree that has the fixes rather than out of somebody's working copy --
        # which this must not write into to do its own job.
        --tools)    TOOLS="${2:-}"; shift 2 ;;
        --machine)  MACHINE="${2:-}"; shift 2 ;;
        --stage)    DO_STAGE=1; shift ;;
        --allow-network-fetch) ALLOW_FETCH=1; shift ;;
        --force)    FORCE=1; shift ;;
        --agent-home) AGENT_HOME="${2:-}"; shift 2 ;;
        --preflight) ACTION=preflight; shift ;;
        --status)   ACTION=status; shift ;;
        --collect)  ACTION=collect; shift ;;
        --plant)    ACTION=plant; shift ;;
        --dry-run)  DRY=1; shift ;;
        -h|--help)  usage ;;
        -*)         die "unknown option: $1" ;;
        *)          [ -z "$WS" ] || die "one workspace at a time (got '$WS' and '$1')"
                    WS="$1"; shift ;;
    esac
done

# The driver cannot live on the machine it reboots. Same guard, and the same
# reason, as bench/mac-lane.sh: phase_go takes the shell with it.
_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
if is_macos && [ "$(_lc "$(hostname -s 2>/dev/null)")" = "$(_lc "$HOST")" ]; then
    die "this lane reboots $HOST, so it cannot be driven from $HOST -- the reboot
  would take the driver with it. Run it from another machine (rpi5, moose)."
fi

case "$ACTION" in
    preflight) preflight; exit $? ;;
    status)    phase_status; exit 0 ;;
    collect)   phase_collect; exit 0 ;;
esac

if ! preflight; then
    [ -n "$DRY" ] || die "preflight failed -- nothing on $HOST has been changed"
    warn "preflight failed; showing the plan anyway because this is --dry-run"
fi

# --patch supersedes --stage: it stages both arms itself, and it must run before
# the plant because the plant validates A_ID and B_ID against what is actually on
# the volume.
if [ -n "$PATCH" ]; then
    [ -z "$DO_STAGE" ] || warn "--stage is redundant with --patch (which stages both arms)"
    [ -z "$A_ID$B_ID" ] || die "--patch chooses both arms; do not also pass --a/--b"
    phase_build_ab
elif [ -n "$DO_STAGE" ]; then
    phase_stage
fi
phase_plant >/dev/null

[ "$ACTION" = plant ] && {
    info "planted and not started. The A/B runs the next time '$VOLUME' boots --"
    log  "  by itself if it is the firmware default, or from the startup manager."
    exit 0
}

phase_go

came_back=$(phase_wait "$BOOT_WAIT") || true
log ""
case "$came_back" in
    dry)
        info "dry run -- nothing on $HOST was changed and nothing was rebooted" ;;
    bench)
        info "$HOST came back in BENCH mode and is reachable -- the A/B is running there."
        log  "  'wk bench mac-ab --status' follows it." ;;
    host)
        # Two very different things look the same from here, and the state file
        # is what tells them apart: a machine that went to bench mode, ran the
        # A/B and came back, versus one that never left host mode at all.
        phase_collect ;;
    noreboot)
        # Distinguished from "not answering" because the remedy is the opposite.
        # The machine is up, the job is planted and untouched, and nothing is
        # wrong with the volume -- only the reboot failed. A catch-all "not
        # answering" here sends the reader to look for an outage that does not
        # exist.
        warn "$HOST never rebooted, so the A/B has not run."
        log  "  The job is planted and still valid -- nothing needs re-staging."
        log  "  Reboot the machine by any means (the startup manager works too)"
        log  "  and it runs by itself; 'wk bench mac-ab --collect' reads it after." ;;
    *)
        warn "$HOST is not answering."
        log  "  If it went to bench mode, that is expected: that install has no"
        log  "  network. The job carries a watchdog and its own hand-back, so it"
        log  "  returns on its own; 'wk bench mac-ab --collect' reads the result"
        log  "  once it does." ;;
esac
