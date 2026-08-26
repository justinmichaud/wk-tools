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
# mac-lane.sh holds state on another machine and reaches into bench mode over
# ssh once per phase: state on the machine being measured has to be
# maintained there, and a benchmark install that has grown a scheduler has
# started to become a workstation.
#
# That shape needs a network in bench mode, which this machine does not
# have: tolken is Wi-Fi only, and the benchmark install's
# com.apple.airport.preferences.plist has an empty PreferredOrder, so it
# joins nothing. Giving it credentials means writing SystemConfiguration and
# reading the System keychain, both of which need root in host mode, which
# an unattended agent does not have.
#
# So the choice is "self-driving" versus "not run at all", made defensible
# by nothing here persisting: one job file, one agent, both removed by the
# run that consumed them, and no decision of its own -- every parameter
# comes from the job that was planted with it.
#
# WHY IT CANNOT SIMPLY REBOOT BACK INTO HOST MODE
#
# There is no software boot-volume switch on Apple Silicon. Tested rather
# than read, in a macOS guest with SIP *disabled* and root:
#
#   nvram boot-volume=<other group>   exits 0 and changes nothing; the value
#                                     is discarded and IODeviceTree:/options
#                                     keeps the firmware's own.
#   bless --setBoot                   "not supported on Apple Silicon based
#                                     systems", in its own man page.
#   systemsetup -getstartupdisk       prints "(null)".
#
# SIP is not the gate -- the variable is firmware-owned -- so what is left
# is the firmware's own default, and the only thing this script can do is
# reboot and see where it lands:
#
#   default is Macintosh HD   the reboot below returns the machine to host
#                             mode and the cycle cost nobody anything.
#   default is WK Bench       the reboot lands back here. The state file
#                             then says the job is finished, and this script
#                             halts rather than running again -- so the
#                             worst case is a machine that is off, and the
#                             person who powers it on picks a disk once.
#
# THE ORDER OF OPERATIONS IS THE SAFETY
#
# Everything that can strand this machine is done before anything that can
# take a long time:
#
#   1. the state file is advanced *first*, so a panic, a hang, a power cut
#      or a watchdog reboot all land on a boot that knows the job was
#      attempted and does not attempt it again -- the same property the
#      rpi4 image gets by self-disarming (docs/HANDOFF-boot.md), reached
#      without a firmware register to clear.
#   2. a watchdog is armed before the first run and reboots the machine
#      whatever happens: an unattended benchmark that hangs is otherwise a
#      machine nobody can reach, on a volume with no network.
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
# The plist and *only* the plist: `launchctl bootout gui/<uid>/com.wk.bench-ab`
# is the same suicide as `kill $$`, since this script is that agent's own
# child -- booting the label out kills the caller mid-function, leaving the
# plist in place and the machine never halted. Deleting the file is
# sufficient anyway: nothing loads it again before the halt below.
remove_agent() {
    [ -f "$AGENT_PLIST" ] || return 0
    rm -f "$AGENT_PLIST"
    say "removed the launch agent ($AGENT_PLIST)"
}

# --- defusing the first-boot daemon -----------------------------------------
#
# bench/mac-bench-firstboot.sh is provisioning that runs once and removes
# itself. If it cannot -- booting out its own launchd label kills it before
# the `rm` -- it runs on *every* boot, and two things it does are fatal to a
# run planted here: it rsyncs --delete its own copy of wk-tools over
# ~bench/Development/wk-tools, replacing this lane's tooling with whatever
# the install was built with, and it ends with `shutdown -r +1`, rebooting
# the machine about a minute into the benchmark.
#
# The daemon is fixed now, but a volume provisioned before the fix still
# carries the broken copy, and this script cannot assume it is running on a
# freshly provisioned install -- so it defuses what it finds.
#
# THE ORDER MATTERS, AND SO DOES DOING IT TWICE.
#
# The daemon and this agent start within seconds of each other, so a check
# for an already-scheduled reboot loses a race it cannot see: the `shutdown`
# is scheduled a minute *later*. So the script is killed first -- removing
# the race rather than reacting to it -- and the check for a pending
# shutdown is repeated after the settle, where anything that slipped
# through will have appeared.
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
# It exists for the guest rehearsal and for nothing else: a macOS guest
# cannot pass the quiet check (`softwareupdate --schedule off` does not
# stick there, and `wk quiesce` refuses inside a workspace), so a rehearsal
# that could not force would only ever exercise the refusal. On the real
# volume it stays empty, and the result records `forced` either way.
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

# The per-user temp directory, waited for rather than assumed: an arm can
# die soon after login with
#
#   patch: Can't create '/var/folders/…/T/patchXXXX' … No such file or directory
#
# run-benchmark applies a plan's patch through `patch`, which writes into
# DARWIN_USER_TEMP_DIR -- created by the per-user bootstrap, which at login
# has not necessarily happened yet. The failure is a race with the session
# coming up, and on the real volume it would show up as a missing number
# with nobody there to see why.
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
# no error. `wk bench staged` refuses on it, but on a
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

# The modal authentication panel, which the check above cannot see: macOS
# draws it from SecurityAgent, which never becomes the frontmost
# *application*, so `lsappinfo front` says "Finder" with a password sheet on
# top of everything.
#
# SIGKILL, not SIGTERM: SecurityAgent holds XPC transactions open while its
# sheet is modal and logs "Remaining transactions after SIGTERM" forever
# instead of exiting. The pending authorization simply fails, which on a
# disposable benchmark install is the correct outcome.
if pgrep -x SecurityAgent >/dev/null 2>&1; then
    say "a modal authentication panel is up (SecurityAgent) -- dismissing it"
    sudo -n killall -9 SecurityAgent >/dev/null 2>&1 || true
    sleep 3
    pgrep -x SecurityAgent >/dev/null 2>&1 \
        && say "  WARNING: it is still up; the browser may not get focus" \
        || say "  dismissed"
fi

# The software-update scanner, stopped rather than merely asked to stay
# away.
#
# THE SETTING IS NOT THE MECHANISM: writing AutomaticCheckEnabled false and
# reading it back succeeds, yet `LastSuccessfulBackgroundMSUScanDate` can
# still advance inside an arm on the same boot -- a preference tells you
# what the preference says, never what the daemon will do.
#
# So the daemon is booted out instead. That is per-boot and self-reversing
# -- this install reboots when the job ends and every system daemon comes
# back with it, the same shape as the rest of `wk quiesce`. The preference
# is still written, since it costs nothing and is the documented intent; it
# is simply not the thing that is believed.
say "stopping the software-update scanner"
for svc in system/com.apple.softwareupdated system/com.apple.mobile.softwareupdated; do
    if sudo -n launchctl bootout "$svc" >/dev/null 2>&1; then
        say "  booted out $svc"
    elif ! sudo -n launchctl print "$svc" >/dev/null 2>&1; then
        say "  $svc is not loaded"
    else
        say "  WARNING: could not boot out $svc and it is still loaded --"
        say "    a scan can still start inside a run. The per-arm scan check"
        say "    below will say so if one does."
    fi
done
sudo -n defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticCheckEnabled -bool false >/dev/null 2>&1 || true
sudo -n defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticDownload -bool false >/dev/null 2>&1 || true

# The scan timestamps, as evidence rather than as configuration.
#
# Read out of the plist file and not through `defaults`: cfprefsd serves
# this domain differently by privilege and has answered with a value the
# file does not carry. The file is what survives, so the file is compared --
# sorted, since `defaults write` rewrites it at job start and a
# re-serialised dict can come back in a different order.
#
# Any of these moving across a benchmark run means a scan happened during
# it. That does not abort the job -- abandoning the remaining rounds would
# throw away good arms to punish a bad one -- but it is recorded against the
# individual arm, so a contaminated number is visible in the verdict instead
# of silently averaged into it.
msu_stamp() {
    /usr/bin/plutil -p /Library/Preferences/com.apple.SoftwareUpdate.plist 2>/dev/null \
        | grep -E '"Last[A-Za-z]*Date"' | sort | tr -d ' \n'
}
say "  scan stamp before the job: $(msu_stamp)"

# --- why the radio is NOT turned off during a run -----------------------------
#
# Both routes to "do not scan" are closed on this install: the preference
# does not work -- it reads back as off and a scan runs anyway -- and
# `launchctl bootout system/com.apple.softwareupdated` is SIP-protected and
# fails.
#
# Turning Wi-Fi off during an arm was tried and rejected: **a measurement
# fix must never be able to make the machine unreachable.** A trap that
# turns the radio back on is not a guarantee -- a panic, a power cut, a
# SIGKILL, or the watchdog's own reboot all leave the interface down, and in
# bench mode that is unrecoverable without walking to the machine. It would
# also disable tailscale, the one thing that makes this install observable
# at all.
#
# So the scan is not prevented, only *detected*, per arm below, and named
# in the summary before any number. A scoped alternative -- denying only
# Apple's update endpoints in /etc/hosts -- would remove what the scan
# needs without risking reachability, but is not done here, untested.

say "quiescing"
"$TOOLS/wk" quiesce on >>"$LOG" 2>&1 || say "WARNING: quiesce reported a problem; the runner will judge it"

# Which result belongs to which arm, recorded as it happens: it cannot be
# recovered afterwards from the file names, since `wk bench staged` names a
# result `<stamp>-<plan>-<staged-id>` and an A/A control runs both arms out
# of the *same* staged payload -- the two arms differ only by timestamp,
# and nothing in the name says which side they were.
RUNS="$WK_AB_ROOT/ab/$(state_get job_stamp)"
[ -n "$(state_get job_stamp)" ] || RUNS="$WK_AB_ROOT/ab/unstamped"
mkdir -p "$RUNS" 2>/dev/null
newest_result() { ls -1 "$WK_AB_ROOT/results" 2>/dev/null | sort | tail -1; }

# --- the A/B -----------------------------------------------------------------
#
# Interleaved (A B A B …) rather than blocked (A A B B), because the machine
# drifts: the SSD warms, the fans spin up, the thermal budget is not what it
# was twenty minutes ago. Blocking the arms puts all of that drift on one
# side of the comparison. `wk pi bench --ab` interleaves for the same
# reason.
#
# any_ok tracks whether anything has worked yet: a whole round failing means
# the next round fails the same way too, since the build, payload and
# machine do not change between rounds, so finishing the schedule only
# burns time on an unreachable machine. Not "abort on the first failure",
# though -- one flaky arm is what more rounds are for; it is a whole round
# with nothing in it that is fatal.
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
        msu_before=$(msu_stamp)
        if "$TOOLS/wk" "$@" >>"$LOG" 2>&1; then
            say "--- round $r, arm $label: OK ---"
            state_set "ok_${label}_$r" 1
            any_ok=1
            # Did anything scan through this arm? Asked per arm rather than per
            # job because that is the resolution the answer is useful at: one
            # contaminated arm out of six is a number to drop, not a reason to
            # disbelieve the other five.
            msu_after=$(msu_stamp)
            clean=clean
            if [ "$msu_before" != "$msu_after" ]; then
                clean=scanned
                say "    CONTAMINATED: a software-update scan ran during this arm"
                say "      before: $msu_before"
                say "      after:  $msu_after"
            fi
            got=$(newest_result)
            if [ -n "$got" ] && [ "$got" != "$before" ]; then
                printf '%s\t%s\t%s\t%s\t%s\n' "$r" "$label" "$sid" "$got" "$clean" >> "$RUNS/runs.tsv"
                say "    -> results/$got ($clean)"
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

# Deliberately no `wk quiesce off`: it is written for a workstation, where
# Spotlight indexing and update checking are the normal state and quieting
# them is temporary. Here it is the reverse -- the benchmark install's
# permanent state is quiet (bench/mac-bench-firstboot.sh sets both off at
# provisioning time and means it) -- so running `off` would undo
# provisioning and leave the next measurement on a machine that has started
# indexing.
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

# Killed *and reaped*: without the wait, bash reports the job as
# "Terminated: 15" into the log after the fact, which reads like a failure in a
# transcript whose whole purpose is to be read after the machine has gone.
kill "$WATCHDOG" 2>/dev/null
wait "$WATCHDOG" 2>/dev/null || true
trap - EXIT INT TERM

# Is this volume the firmware default? It decides what "finishing" means --
# the two answers are genuinely different situations, not a preference.
#
# Read the same way `wk boot mbp --status` does: `boot-volume` is three
# colon-separated UUIDs and only the last names anything on the disk -- the
# APFS volume group -- compared against the group of the volume booted now.
booted_is_default() {
    local nv grp
    nv=$(python3 "$TOOLS/lib/wkmac.py" boot-volume 2>/dev/null)
    nv="${nv##*:}"
    [ -n "$nv" ] || return 1
    grp=$(python3 "$TOOLS/lib/wkmac.py" volume-group / 2>/dev/null | tr -d ' \r')
    [ -n "$grp" ] || return 1
    [ "$nv" = "$grp" ]
}

# WHEN THIS VOLUME IS THE FIRMWARE DEFAULT, FINISHING MEANS STAYING UP.
#
# The reboot below is right when the default is the *host* volume: it hands
# the machine back at no cost. When the default is this volume it is
# actively harmful -- the reboot lands back here, the `phase = done` branch
# halts the machine, and a completed A/B sits unreachable on a powered-off
# Mac until somebody walks over and picks a disk. Halting protects against
# a boot loop, but powering off is not the only way to avoid one.
#
# Staying up does not loop either: the job is done, the agent is removed,
# and nothing here starts anything again. What it buys is that the results
# are reachable the moment they exist, over the network, so the one human
# step that is genuinely unavoidable on this machine (the firmware cannot
# be told which volume to boot from software; see the header) becomes a
# plain reboot whenever it suits, rather than a trip to the keyboard before
# anyone can read the result.
if booted_is_default; then
    say "this volume is the firmware default, so a reboot would land back here and"
    say "halt -- leaving a finished A/B on a machine nothing can reach. Staying up"
    say "instead. The agent is removed, so nothing runs again; the numbers are"
    say "collectable over the network now, and the way back to workstation mode is"
    say "a plain reboot whenever it suits."
    remove_agent
    state_set left_how stayed-up
    state_set left_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    say "=== staying up in bench mode; nothing further will run ==="
    exit 0
fi

leave_bench reboot "job finished"
