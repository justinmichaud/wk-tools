#!/bin/bash
#
# The benchmark install running an A/B by itself, with nobody driving it.
#
# Planted onto the benchmark volume by `wk bench mac-ab` while the machine is
# still in host mode, started by a per-user LaunchAgent when the bench account
# auto-logs in, and gone again by the time the machine hands itself back.
#
# WHY THE RUN DRIVES ITSELF HERE, WHEN bench/mac-lane.sh SAYS IT MUST NOT
#
# mac-lane.sh holds the state on another machine and reaches into bench mode
# over ssh once per phase, for a good reason: state on the machine being
# measured is state that has to be maintained there, and a benchmark install
# that has grown a scheduler has started to become a workstation.
#
# That shape needs one thing this machine does not have: **a network in bench
# mode.** Measured 2026-08-23 -- tolken is Wi-Fi only, and the benchmark
# install's com.apple.airport.preferences.plist has an empty PreferredOrder, so
# it joins nothing and answers nowhere. Giving it credentials means writing
# SystemConfiguration and reading the System keychain, both of which need root
# in host mode, which an unattended agent does not have.
#
# So the choice is not "driven remotely" versus "self-driving". It is
# "self-driving" versus "not run at all", and what makes it defensible is that
# nothing here persists: one job file, one agent, both removed by the run that
# consumed them, and no decision of its own -- every parameter comes from the
# job that was planted with it.
#
# WHY IT CANNOT SIMPLY REBOOT BACK INTO HOST MODE
#
# There is no software boot-volume switch on Apple Silicon. Tested rather than
# read, 2026-08-23, in a macOS guest with SIP *disabled* and root:
#
#   nvram boot-volume=<other group>   exits 0 and changes nothing. The value
#                                     lands in the 7C436110-… namespace and is
#                                     discarded; IODeviceTree:/options still
#                                     holds the firmware's own, and so does
#                                     `nvram -p` before and after a reboot.
#   bless --setBoot                   "not supported on Apple Silicon based
#                                     systems", in its own man page.
#   systemsetup -getstartupdisk       prints "(null)"; -liststartupdisks prints
#                                     nothing at all.
#
# SIP is not what gates it -- the variable is firmware-owned, not
# SIP-protected -- so disabling SIP buys nothing here. What is left is the
# firmware's own default, and the only thing this script can do about it is
# reboot and see where it lands:
#
#   default is Macintosh HD   the reboot below returns the machine to host mode
#                             and the whole cycle cost nobody anything.
#   default is WK Bench       the reboot lands back here. The state file then
#                             says the job is finished, and this script shuts
#                             the machine *down* rather than running again --
#                             so the worst case is a machine that is off, not a
#                             machine in a loop, and the person who powers it on
#                             picks a disk once.
#
# THE ORDER OF OPERATIONS IS THE SAFETY
#
# Everything that can strand this machine is done before anything that can take
# a long time:
#
#   1. the state file is advanced *first*, so a panic, a hang, a power cut or a
#      watchdog reboot all land on a boot that knows the job was attempted and
#      does not attempt it again. The rpi4 image self-disarms on boot for the
#      same reason (docs/HANDOFF-boot.md); this is the same property reached
#      without a firmware register to clear.
#   2. a watchdog is armed before the first run, and it reboots the machine
#      whatever happens to the run. An unattended benchmark that hangs is
#      otherwise a machine nobody can reach, on a volume with no network.
#   3. the hand-back is in a trap, so it happens on the error path too.

export PATH=/usr/sbin:/usr/bin:/sbin:/bin

WK_AB_ROOT="${WK_AB_ROOT:-/var/wk}"
JOB="$WK_AB_ROOT/job.json"
STATE="$WK_AB_ROOT/autorun.state"
LOG="$WK_AB_ROOT/autorun.log"
AGENT_LABEL="com.wk.bench-ab"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

# How many boots may attempt this job before it is abandoned. Three, and the
# reason it is not one: a run interrupted by something transient (a watchdog
# reboot with the benchmark half done) deserves another go, and a run that
# fails the same way three times is not going to succeed on the fourth. A
# counter is what keeps "try again" from meaning "boot loop".
MAX_ATTEMPTS=3

mkdir -p "$WK_AB_ROOT" 2>/dev/null
exec >>"$LOG" 2>&1

say() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

state_get() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null | tail -1; }
state_set() {
    local k="$1" v="$2" tmp
    tmp="$STATE.tmp.$$"
    { grep -v "^$k=" "$STATE" 2>/dev/null; printf '%s=%s\n' "$k" "$v"; } > "$tmp" \
        && mv "$tmp" "$STATE"
    # Onto the disk, not into a cache: the next thing this file has to survive
    # is a reboot that may not be graceful.
    sync 2>/dev/null || true
}

jf() {  # a field out of the job, by python because the job is json
    /usr/bin/python3 - "$JOB" "$1" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
v = d
for part in sys.argv[2].split('.'):
    if isinstance(v, list):
        try: v = v[int(part)]
        except Exception: sys.exit(1)
    elif isinstance(v, dict):
        v = v.get(part)
    else:
        sys.exit(1)
if v is None: sys.exit(1)
if isinstance(v, bool): print("1" if v else "")
else: print(v)
PY
}

# --- leaving bench mode ------------------------------------------------------
#
# One function, called from the trap, so there is exactly one place that decides
# how this machine stops being a benchmark. `halt` when the job is finished and
# we have already come back here once (the firmware default is this volume, so
# rebooting would loop); `reboot` otherwise.
_left=""
leave_bench() {
    local how="$1" why="$2"
    [ -n "$_left" ] && return 0
    _left=1
    say "leaving bench mode ($how): $why"
    state_set left_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    state_set left_how "$how"
    sync 2>/dev/null || true
    # `sudo -n`: the bench account has a NOPASSWD rule (bench/mac-bench-firstboot.sh
    # explains why it is defensible on a disposable install). If it is somehow
    # gone, say so in the log rather than hanging on a prompt no one will answer.
    case "$how" in
        halt)   sudo -n shutdown -h now >/dev/null 2>&1 || say "WARNING: could not halt" ;;
        *)      sudo -n shutdown -r now >/dev/null 2>&1 || say "WARNING: could not reboot" ;;
    esac
}

# The agent removes itself once the job it was planted for is finished, so a
# later boot of this volume is an ordinary boot. Removed rather than left
# disabled: a disabled agent is a thing to remember, and the next
# `wk bench mac-ab` plants a fresh one anyway.
#
# The plist and *only* the plist. `launchctl bootout gui/<uid>/com.wk.bench-ab`
# was here and it is the same suicide as `kill $$`: this script is that agent's
# own child, so booting the label out kills the caller mid-function. Measured in
# the guest rehearsal 2026-08-23 -- the log ends after the message explaining
# what it was about to do, the plist was still in place, and the machine never
# halted. Which is the worst of the three outcomes: a benchmark install left
# running with nobody driving it, in the one branch whose entire job is to stop
# that.
#
# Deleting the file is sufficient anyway. Nothing loads it again until a login,
# and there is not going to be another login before the halt below.
remove_agent() {
    [ -f "$AGENT_PLIST" ] || return 0
    rm -f "$AGENT_PLIST"
    say "removed the launch agent ($AGENT_PLIST)"
}

# --- defusing the first-boot daemon -----------------------------------------
#
# bench/mac-bench-firstboot.sh is provisioning that runs once and removes
# itself. Until 2026-08-23 it could not remove itself -- it booted out its own
# launchd label, which killed it before the `rm` -- so it ran on *every* boot,
# and two things it does are fatal to a run planted here:
#
#   it rsyncs --delete its own copy of wk-tools over ~bench/Development/wk-tools,
#   replacing this lane's tooling with whatever the install was built with; and
#
#   it ends with `shutdown -r +1`, so the machine reboots about a minute into
#   the benchmark.
#
# That is exactly what happened to the cycles at 15:41Z and 17:51Z: the second
# one got two preflights in before the machine went down under it.
#
# The daemon is fixed now, but a volume provisioned before the fix still carries
# the broken copy, and this script cannot assume it is running on a freshly
# provisioned install -- so it defuses what it finds.
#
# THE ORDER MATTERS, AND SO DOES DOING IT TWICE.
#
# The daemon and this agent start within two seconds of each other (17:51:12 and
# 17:51:14 in the log), so a check for an already-scheduled reboot loses a race
# it cannot see: the `shutdown` is scheduled a minute *later*. So the script is
# killed first -- that removes the race rather than reacting to it -- and the
# check for a pending shutdown is repeated after the settle, where anything that
# slipped through will have appeared.
FB_PLIST=/Library/LaunchDaemons/com.wk.bench-firstboot.plist
FB_SELF=/usr/local/libexec/wk-bench-firstboot.sh

cancel_pending_reboot() {
    pgrep -x shutdown >/dev/null 2>&1 || return 0
    say "  a reboot is scheduled by something else -- cancelling it"
    sudo -n pkill -x shutdown >/dev/null 2>&1 || true
    sleep 2
    if pgrep -x shutdown >/dev/null 2>&1; then
        say "  WARNING: shutdown is still pending; this run may be cut off"
    else
        say "  reboot cancelled"
    fi
}

defuse_firstboot() {
    [ -f "$FB_PLIST" ] || [ -f "$FB_SELF" ] || { cancel_pending_reboot; return 0; }
    say "the first-boot daemon is still installed -- defusing it"
    # The running script first: it is what would schedule the reboot, and killing
    # it before it gets there is the only version of this that is not a race.
    if pgrep -f wk-bench-firstboot >/dev/null 2>&1; then
        say "  it is running right now -- stopping it before it schedules a reboot"
        sudo -n pkill -f wk-bench-firstboot >/dev/null 2>&1 || true
    fi
    sudo -n rm -f "$FB_PLIST" "$FB_SELF" >/dev/null 2>&1 || true
    if [ -f "$FB_PLIST" ]; then
        say "  WARNING: could not remove $FB_PLIST -- it will run again next boot"
    else
        say "  removed the first-boot daemon; later boots are ordinary boots"
    fi
    cancel_pending_reboot
}

say "=== wk bench autorun: boot $(sysctl -n kern.boottime 2>/dev/null | sed -n 's/.*{ *sec *= *\([0-9]*\).*/\1/p') ==="

# --- the refusals ------------------------------------------------------------

if [ ! -f /etc/wk-image ]; then
    say "not bench mode (/etc/wk-image absent) -- this agent has nothing to do here"
    remove_agent
    exit 0
fi
say "bench mode: $(sed -n 's/^id=//p' /etc/wk-image)"
defuse_firstboot

if [ ! -f "$JOB" ]; then
    say "no job at $JOB -- nothing to run"
    remove_agent
    leave_bench reboot "no job"
    exit 0
fi

PHASE=$(state_get phase)
ATTEMPTS=$(state_get attempts); ATTEMPTS=${ATTEMPTS:-0}

if [ "$PHASE" = done ]; then
    # We are here on a *second* boot after finishing, which means the firmware
    # default is this volume and rebooting would bring us straight back. Halt,
    # and let the person who powers the machine on pick a disk once. This is
    # the branch that turns "no way back in software" into "one human step"
    # instead of "a loop".
    say "the job is already finished, and this volume booted again -- so it is the"
    say "firmware default. Halting rather than looping; the way to host mode is"
    say "the startup manager, once."
    remove_agent
    leave_bench halt "job already complete"
    exit 0
fi

ATTEMPTS=$((ATTEMPTS + 1))
state_set attempts "$ATTEMPTS"
if [ "$ATTEMPTS" -gt "$MAX_ATTEMPTS" ]; then
    say "attempt $ATTEMPTS exceeds the limit of $MAX_ATTEMPTS -- abandoning the job"
    state_set phase done
    state_set outcome abandoned
    remove_agent
    leave_bench reboot "too many attempts"
    exit 0
fi
say "attempt $ATTEMPTS of $MAX_ATTEMPTS"

# --- the job -----------------------------------------------------------------

PLAN=$(jf plan);         PLAN="${PLAN:-speedometer3.0}"
ROUNDS=$(jf rounds);     ROUNDS="${ROUNDS:-2}"
TIMEOUT=$(jf timeout);   TIMEOUT="${TIMEOUT:-1800}"
COUNT=$(jf count)
TOOLS=$(jf wk_tools);    TOOLS="${TOOLS:-$HOME/Development/wk-tools}"
NARMS=$(jf n_arms);      NARMS="${NARMS:-2}"
SETTLE=$(jf settle);     SETTLE="${SETTLE:-90}"
# --force, carried in the job rather than decided here.
#
# It exists for the guest rehearsal and for nothing else: a macOS guest cannot
# pass the quiet check (`softwareupdate --schedule off` does not stick there, and
# `wk quiesce` refuses inside a workspace), so a rehearsal that could not force
# would only ever exercise the refusal. On the real volume it stays empty, and
# the result records `forced` either way -- which is the whole point of the flag
# being in the record instead of in the operator's memory.
FORCE=$(jf force)

say "job: plan=$PLAN rounds=$ROUNDS arms=$NARMS timeout=${TIMEOUT}s count=${COUNT:-default}${FORCE:+ FORCED}"
say "     wk-tools=$TOOLS"

[ -x "$TOOLS/wk" ] || {
    say "FATAL: no wk at $TOOLS/wk -- cannot run anything"
    state_set phase done; state_set outcome "no-wk-tools"
    remove_agent
    leave_bench reboot "no wk-tools"
    exit 1
}

# The state advances to `running` *before* the first run, and to `done` only
# after the last one. Anything that kills this process in between leaves
# `running` behind, which the attempt counter above turns into a bounded retry
# rather than a loop.
state_set phase running
state_set plan "$PLAN"
state_set started_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- the watchdog ------------------------------------------------------------
#
# Armed before the first run and disarmed after the last. The budget is
# generous on purpose: a first run against a freshly copied build tree is
# legitimately much slower than the second (docs/HANDOFF-mac-perf-mode.md), and
# a watchdog that fires on a slow-but-working run costs a whole cycle.
DEADLINE=$(( (ROUNDS * NARMS + 1) * TIMEOUT + 600 ))
say "watchdog: ${DEADLINE}s"
(
    sleep "$DEADLINE"
    [ "$(state_get phase)" = done ] && exit 0
    say "WATCHDOG FIRED after ${DEADLINE}s -- the run is not coming back"
    state_set phase done
    state_set outcome watchdog
    sudo -n shutdown -r now >/dev/null 2>&1
) &
WATCHDOG=$!

# The hand-back is a trap so that it happens on every exit path, including the
# ones nobody wrote down. Without it a `set -e` death or an unexpected `exit`
# leaves a machine in bench mode with no network and nobody driving it.
trap 'kill "$WATCHDOG" 2>/dev/null; leave_bench reboot "run finished or failed"' EXIT INT TERM

# --- a settled session -------------------------------------------------------
#
# The agent starts at login, which is the moment the machine is *least* quiet:
# the Dock, Spotlight's first-run work, the window server settling. Waiting is
# cheaper than measuring that.
say "settling for ${SETTLE}s"
sleep "$SETTLE"

# The per-user temp directory, waited for rather than assumed.
#
# Found in the guest rehearsal, 2026-08-23, and it is exactly the class of
# failure this whole design exists to keep off the real machine: the first arm
# died 20 s after login with
#
#   patch: Can't create '/var/folders/…/T/patchXXXX' … No such file or directory
#
# run-benchmark applies a plan's patch through `patch`, which writes into
# DARWIN_USER_TEMP_DIR -- and that directory is created by the per-user
# bootstrap, which at login has not necessarily happened yet. The second arm,
# sixteen seconds later, was fine. So the failure is a race with the session
# coming up, it costs a whole arm of the A/B, and on the real volume it would
# have been discovered as a missing number with nobody there to see why.
for _t in 1 2 3 4 5 6 7 8 9 10 11 12; do
    _tmp=$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)
    [ -n "$_tmp" ] && [ -d "$_tmp" ] && [ -w "$_tmp" ] && break
    say "waiting for the per-user temp directory (${_tmp:-unset})"
    sleep 5
done
_tmp=$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)
if [ -n "$_tmp" ] && [ -w "$_tmp" ]; then
    say "temp: $_tmp"
else
    say "WARNING: no writable per-user temp directory -- run-benchmark's patch step will fail"
fi

# Again, now that the minute in which somebody else could have scheduled a
# reboot has passed. Cheap, and the failure it catches costs a whole cycle.
cancel_pending_reboot

# Whatever owns the front window would throttle MiniBrowser into a timeout with
# no error (measured 2026-08-22). `wk bench staged` refuses on it, but on a
# machine nobody can reach a refusal is as expensive as a hang -- so try to
# clear it first, and let the runner have the last word.
if [ -r "$TOOLS/lib/quiet.sh" ]; then
    front=$( . "$TOOLS/lib/common.sh" >/dev/null 2>&1
             . "$TOOLS/lib/quiet.sh"  >/dev/null 2>&1
             screen_blocker 2>/dev/null )
    if [ -n "$front" ]; then
        say "'$front' owns the front window -- closing it"
        sudo -n touch /var/db/.AppleSetupDone >/dev/null 2>&1 || true
        sudo -n pkill -f "$front.app" >/dev/null 2>&1 || true
        sleep 10
    fi
fi

# The modal authentication panel, which the check above cannot see.
#
# macOS draws it from SecurityAgent, which never becomes the frontmost
# *application*, so `lsappinfo front` says "Finder" with a password sheet on top
# of everything. Found by somebody looking at the screen, 2026-08-23, five
# minutes into a run that had passed "the screen is free" six times.
#
# SIGKILL, and that is not impatience: SIGTERM does not work. SecurityAgent holds
# XPC transactions open while its sheet is modal and logs
# "Remaining transactions after SIGTERM: 2" twice a second indefinitely --
# `killall` left it running with the dialog still up. The pending authorization
# simply fails, which on a disposable benchmark install is the correct outcome.
if pgrep -x SecurityAgent >/dev/null 2>&1; then
    say "a modal authentication panel is up (SecurityAgent) -- dismissing it"
    sudo -n killall -9 SecurityAgent >/dev/null 2>&1 || true
    sleep 3
    pgrep -x SecurityAgent >/dev/null 2>&1 \
        && say "  WARNING: it is still up; the browser may not get focus" \
        || say "  dismissed"
fi

# Automatic update checking, turned off here as well as at provisioning time.
#
# Not belt-and-braces: it is on record as not sticking. This install's own
# first-boot log says "Automatic checking for updates is turned on" *after*
# setting it off, and on 2026-08-23 the plist on the volume had no
# AutomaticCheckEnabled key at all -- while `LastFullSuccessfulDate` showed a
# software-update scan starting at 15:41:17, which is thirty seconds into a
# benchmark run. That is a network fetch and a scan inside the measurement.
#
# So it is set again, per run, from the side that has passwordless root. Written
# rather than requested (`softwareupdate --schedule off` is the requester, and it
# is the thing that does not stick), and read back rather than trusted. Failure
# is not fatal: `wk quiesce` and the runner's own quiet check both measure this
# afterwards and get the last word.
say "turning automatic update checking off"
sudo -n defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticCheckEnabled -bool false >/dev/null 2>&1 || true
sudo -n defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticDownload -bool false >/dev/null 2>&1 || true
say "  AutomaticCheckEnabled now: $(sudo -n defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>&1)"

say "quiescing"
"$TOOLS/wk" quiesce on >>"$LOG" 2>&1 || say "WARNING: quiesce reported a problem; the runner will judge it"

# Which result belongs to which arm, recorded as it happens.
#
# It cannot be recovered afterwards from the file names: `wk bench staged` names
# a result `<stamp>-<plan>-<staged-id>`, and an A/A control runs both arms out of
# the *same* staged payload -- so the two arms produce names that differ only by
# their timestamp, and nothing in them says which side they were. So the arm
# label is written down at the moment it is known, next to the run it names.
RUNS="$WK_AB_ROOT/ab/$(state_get job_stamp)"
[ -n "$(state_get job_stamp)" ] || RUNS="$WK_AB_ROOT/ab/unstamped"
mkdir -p "$RUNS" 2>/dev/null
newest_result() { ls -1 "$WK_AB_ROOT/results" 2>/dev/null | sort | tail -1; }

# --- the A/B -----------------------------------------------------------------
#
# Interleaved (A B A B …) rather than blocked (A A B B), because the machine
# drifts: the SSD warms, the fans spin up, the chip's thermal budget is not what
# it was twenty minutes ago. Blocking the arms puts all of that drift on one
# side of the comparison and calls it a result. `wk pi bench --ab` interleaves
# for the same reason.
# Whether anything at all has worked yet. A whole round failing means the *next*
# round will fail the same way -- the build, the payload and the machine do not
# change between rounds -- so finishing the schedule buys nothing and costs the
# machine's time in a mode nobody can reach. Found 2026-08-23: a lane staged
# without a pinned payload burnt six arms on the same missing benchmark and then
# handed back a volume with no numbers on it.
#
# Deliberately not "abort on the first failure": one flaky arm is exactly what
# more rounds are for. It is a whole round with nothing in it that is fatal.
any_ok=""

r=1
while [ "$r" -le "$ROUNDS" ]; do
    i=0
    while [ "$i" -lt "$NARMS" ]; do
        label=$(jf "arms.$i.label");        label="${label:-arm$i}"
        sid=$(jf "arms.$i.id")
        bargs=$(jf "arms.$i.browser_args")
        say "--- round $r, arm $label (staged $sid) ---"
        set -- bench staged --plan "$PLAN" --timeout "$TIMEOUT"
        [ -n "$sid" ]   && set -- "$@" --id "$sid"
        [ -n "$COUNT" ] && set -- "$@" --count "$COUNT"
        [ -n "$bargs" ] && set -- "$@" --browser-args "$bargs"
        [ -n "$FORCE" ] && set -- "$@" --force
        before=$(newest_result)
        if "$TOOLS/wk" "$@" >>"$LOG" 2>&1; then
            say "--- round $r, arm $label: OK ---"
            state_set "ok_${label}_$r" 1
            any_ok=1
            got=$(newest_result)
            if [ -n "$got" ] && [ "$got" != "$before" ]; then
                printf '%s\t%s\t%s\t%s\n' "$r" "$label" "$sid" "$got" >> "$RUNS/runs.tsv"
                say "    -> results/$got"
            else
                say "    WARNING: no new result directory appeared"
            fi
        else
            rc=$?
            say "--- round $r, arm $label: FAILED (rc=$rc) ---"
            state_set "fail_${label}_$r" "$rc"
        fi
        i=$((i + 1))
    done
    if [ -z "$any_ok" ]; then
        say "round $r produced nothing at all -- every arm failed the same way, and"
        say "the next round has nothing different to try. Stopping here so the"
        say "machine hands itself back instead of burning the schedule."
        state_set outcome "all-failed-round-$r"
        break
    fi
    r=$((r + 1))
done

# Deliberately no `wk quiesce off`.
#
# `quiesce off` is written for a workstation: it turns Spotlight indexing and
# automatic update checking back *on*, because on the machine somebody works on
# those are the normal state and quieting them is the temporary condition. Here
# it is the other way round. The benchmark install's permanent state is quiet --
# bench/mac-bench-firstboot.sh sets exactly those two off at provisioning time
# and means it -- so running `off` at the end of a run would undo provisioning
# and leave the *next* measurement to be taken on a machine that has started
# indexing. The install is cattle with no desktop and nobody at it; there is
# nothing here that wants Spotlight back.
say "leaving the machine quiesced (its permanent state; see the comment here)"

# --- the verdict, written where the results are ------------------------------
#
# Best effort, and deliberately not fatal: the numbers are the deliverable and
# the comparison can be redone from host mode against the same files. It is done
# here anyway because this is where the run happened, and a summary that travels
# with the results is one fewer thing to reconstruct later.
say "summarising"
"$TOOLS/wk" bench ab-summary --root "$WK_AB_ROOT" --runs "$RUNS/runs.tsv" \
    --out "$RUNS/summary.txt" >>"$LOG" 2>&1 \
    || say "(no summary -- 'wk bench ab-summary' failed; the results are still on the volume)"

state_set phase done
state_set outcome ran
state_set finished_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "=== job finished ==="

# The agent stays for exactly one more boot. If that boot happens here, the
# `phase = done` branch above halts the machine and removes the agent -- which
# is the only way this volume can tell that it is the firmware default.
# Killed *and reaped*: without the wait, bash reports the job as
# "Terminated: 15" into the log after the fact, which reads like a failure in a
# transcript whose whole purpose is to be read after the machine has gone.
kill "$WATCHDOG" 2>/dev/null
wait "$WATCHDOG" 2>/dev/null || true
trap - EXIT INT TERM
leave_bench reboot "job finished"
