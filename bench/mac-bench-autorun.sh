#!/bin/bash
#
# The benchmark install running an A/B by itself, with nobody driving it.
#
# Planted onto the benchmark volume by `wk bench mac-ab` while the machine is
# still in host mode, started by a per-user LaunchAgent when the bench account
# auto-logs in, and gone again by the time the machine hands itself back.
#
# Runs itself here, unlike bench/mac-lane.sh's state-elsewhere-reach-in
# shape, because this machine has no network in bench mode (tolken is
# Wi-Fi only; the benchmark install joins nothing). Defensible only because
# nothing here persists: one job file, one agent, both removed by the run
# that consumed them, and every parameter comes from the planted job.
#
# There is no software boot-volume switch on Apple Silicon
# (docs/HANDOFF-mac-perf-mode.md, "reading a preference tells you what it
# says, never what the daemon will do"), so this script reboots and sees
# where it lands: Macintosh HD returns to host mode; WK Bench lands back
# here, and the state file saying the job is finished makes this script
# halt rather than run again -- worst case a powered-off machine.
#
# THE ORDER OF OPERATIONS IS THE SAFETY: everything that can strand this
# machine happens before anything that can take a long time -- the state
# file is advanced *first* so a panic or power cut does not repeat the
# attempt, the watchdog is armed before the first run, and the hand-back
# runs from a trap so it fires on the error path too.

export PATH=/usr/sbin:/usr/bin:/sbin:/bin

WK_AB_ROOT="${WK_AB_ROOT:-/var/wk}"
JOB="$WK_AB_ROOT/job.json"
STATE="$WK_AB_ROOT/autorun.state"
LOG="$WK_AB_ROOT/autorun.log"
AGENT_LABEL="com.wk.bench-ab"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

# How many boots may attempt this job before it is abandoned: enough that a
# run interrupted by something transient (a watchdog reboot mid-benchmark)
# gets another go, bounded so "try again" cannot mean "boot loop".
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
    sync 2>/dev/null || true  # the next thing this has to survive is an ungraceful reboot
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
# One place that decides how this machine stops being a benchmark. `halt`
# when rebooting would loop back to this same volume; `reboot` otherwise.
_left=""
leave_bench() {
    local how="$1" why="$2"
    [ -n "$_left" ] && return 0
    _left=1
    say "leaving bench mode ($how): $why"
    state_set left_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    state_set left_how "$how"
    sync 2>/dev/null || true
    case "$how" in  # sudo -n: log rather than hang on a prompt if NOPASSWD is gone
        halt)   sudo -n shutdown -h now >/dev/null 2>&1 || say "WARNING: could not halt" ;;
        *)      sudo -n shutdown -r now >/dev/null 2>&1 || say "WARNING: could not reboot" ;;
    esac
}

# The plist file, and *only* the file: `launchctl bootout` would kill this
# script (its own child) mid-function. Deleting the file is sufficient
# anyway: nothing loads it again before the halt below.
remove_agent() {
    [ -f "$AGENT_PLIST" ] || return 0
    rm -f "$AGENT_PLIST"
    say "removed the launch agent ($AGENT_PLIST)"
}

# --- defusing the first-boot daemon -----------------------------------------
#
# bench/mac-bench-firstboot.sh removes itself when it can; if it cannot, it
# runs on *every* boot and is fatal to a run planted here: it rsyncs
# --delete its wk-tools copy over ~bench/Development/wk-tools, and it ends
# with `shutdown -r +1`. Order matters: the daemon and this agent start
# within seconds of each other, so checking for an already-scheduled reboot
# first loses a race it cannot see (the `shutdown` is a minute *later*) --
# the script is killed first, and the check is repeated after the settle.
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
    if pgrep -f wk-bench-firstboot >/dev/null 2>&1; then  # kill it before it schedules the reboot
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
    # A second boot after finishing means the firmware default is this
    # volume; halt rather than loop.
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
FORCE=$(jf force)  # for the guest rehearsal, which cannot pass the quiet check; empty on the real volume

# The variance knobs (cmd/bench's header documents each); exported so
# `"$TOOLS/wk" bench staged` picks them up like a container run's WK_BENCH_*.
export WK_BENCH_ASLR=$(jf aslr)
export WK_BENCH_ENV_PAD=$(jf env_pad)
export WK_BENCH_PATH_PAD=$(jf path_pad)
export WK_BENCH_SHARED_CACHE=$(jf shared_cache)

say "job: plan=$PLAN rounds=$ROUNDS arms=$NARMS timeout=${TIMEOUT}s count=${COUNT:-default}${FORCE:+ FORCED}"
say "     variance: aslr=${WK_BENCH_ASLR:-unset} env_pad=${WK_BENCH_ENV_PAD:-0} path_pad=${WK_BENCH_PATH_PAD:-0} shared_cache=${WK_BENCH_SHARED_CACHE:-unset}"
say "     wk-tools=$TOOLS"

[ -x "$TOOLS/wk" ] || {
    say "FATAL: no wk at $TOOLS/wk -- cannot run anything"
    state_set phase done; state_set outcome "no-wk-tools"
    remove_agent
    leave_bench reboot "no wk-tools"
    exit 1
}

# `running` before the first run, `done` only after the last, so a mid-run
# kill leaves `running` behind for the attempt counter above to retry.
state_set phase running
state_set plan "$PLAN"
state_set started_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- the watchdog ------------------------------------------------------------
#
# Generous on purpose: a watchdog that fires on a slow-but-working run
# costs a whole cycle, and the first run against a freshly copied build
# tree is legitimately much slower than the rest.
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

# A trap, so it fires on every exit path: without it a `set -e` death or an
# unexpected `exit` leaves the machine in bench mode with nobody driving it.
trap 'kill "$WATCHDOG" 2>/dev/null; leave_bench reboot "run finished or failed"' EXIT INT TERM

# --- a settled session -------------------------------------------------------
#
# The agent starts at login, the moment the machine is *least* quiet.
say "settling for ${SETTLE}s"
sleep "$SETTLE"

# Waited for rather than assumed: DARWIN_USER_TEMP_DIR, which run-benchmark's
# `patch` writes into, is created by the per-user bootstrap, which at login
# has not necessarily happened yet.
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

cancel_pending_reboot  # again, now that the minute someone else could schedule one has passed

# Whatever owns the front window would throttle MiniBrowser into a timeout
# with no error; on a machine nobody can reach that is as expensive as a
# hang, so try to clear it first and let the runner have the last word.
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

# The modal auth panel, invisible to the check above (SecurityAgent never
# becomes the frontmost *application*). SIGKILL, not SIGTERM: SecurityAgent
# holds XPC transactions open and logs forever instead of exiting.
if pgrep -x SecurityAgent >/dev/null 2>&1; then
    say "a modal authentication panel is up (SecurityAgent) -- dismissing it"
    sudo -n killall -9 SecurityAgent >/dev/null 2>&1 || true
    sleep 3
    pgrep -x SecurityAgent >/dev/null 2>&1 \
        && say "  WARNING: it is still up; the browser may not get focus" \
        || say "  dismissed"
fi

# Stopped, not merely asked to stay away: the preference reads back false
# yet a scan can still run (docs/HANDOFF-mac-perf-mode.md, "reading a
# preference tells you what it says, never what the daemon will do"). The
# daemon is booted out instead -- self-reversing, since this install
# reboots when the job ends.
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

# Read out of the plist file, not `defaults`: cfprefsd has answered with a
# value the file does not carry. Sorted, since `defaults write` rewrites
# the file at job start and a re-serialised dict can reorder. Any of these
# moving across a run means a scan happened -- recorded against the
# individual arm rather than aborting the job.
msu_stamp() {
    /usr/bin/plutil -p /Library/Preferences/com.apple.SoftwareUpdate.plist 2>/dev/null \
        | grep -E '"Last[A-Za-z]*Date"' | sort | tr -d ' \n'
}
say "  scan stamp before the job: $(msu_stamp)"

# The radio stays on during a run (docs/HANDOFF-mac-perf-mode.md); the scan
# is only *detected*, per arm below.

say "quiescing"
"$TOOLS/wk" quiesce on >>"$LOG" 2>&1 || say "WARNING: quiesce reported a problem; the runner will judge it"

# Recorded as it happens: an A/A control runs both arms out of the *same*
# staged payload, so file names alone cannot say which side is which.
RUNS="$WK_AB_ROOT/ab/$(state_get job_stamp)"
[ -n "$(state_get job_stamp)" ] || RUNS="$WK_AB_ROOT/ab/unstamped"
mkdir -p "$RUNS" 2>/dev/null
newest_result() { ls -1 "$WK_AB_ROOT/results" 2>/dev/null | sort | tail -1; }

# --- the A/B -----------------------------------------------------------------
#
# Interleaved (A B A B …), not blocked (A A B B): the machine drifts, and
# blocking puts all of that drift on one side of the comparison.
#
# any_ok: a whole round failing means the next fails the same way, so
# finishing the schedule only burns time. Not "abort on the first failure"
# -- one flaky arm is what more rounds are for.
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
            msu_after=$(msu_stamp)  # per arm: one contaminated arm is a number to drop, not a reason to disbelieve the rest
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

# No `wk quiesce off`: quiet is this install's permanent state
# (bench/mac-bench-firstboot.sh sets it at provisioning time), not a
# temporary workstation setting to undo.
say "leaving the machine quiesced (its permanent state; see the comment here)"

# --- the verdict, written where the results are ------------------------------
#
# Best effort, not fatal: the comparison can be redone from host mode
# against the same files. Done here anyway so it travels with the results.
say "summarising"
"$TOOLS/wk" bench ab-summary --root "$WK_AB_ROOT" --runs "$RUNS/runs.tsv" \
    --out "$RUNS/summary.txt" >>"$LOG" 2>&1 \
    || say "(no summary -- 'wk bench ab-summary' failed; the results are still on the volume)"

state_set phase done
state_set outcome ran
state_set finished_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "=== job finished ==="

kill "$WATCHDOG" 2>/dev/null  # and reaped below, or bash logs "Terminated: 15" as if it failed
wait "$WATCHDOG" 2>/dev/null || true
trap - EXIT INT TERM

# Decides what "finishing" means. Read the same way `wk boot mbp --status`
# does: `boot-volume` is three colon-separated UUIDs, only the last naming
# anything on disk, compared against the group booted now.
booted_is_default() {
    local nv grp
    nv=$(python3 "$TOOLS/lib/wkmac.py" boot-volume 2>/dev/null)
    nv="${nv##*:}"
    [ -n "$nv" ] || return 1
    grp=$(python3 "$TOOLS/lib/wkmac.py" volume-group / 2>/dev/null | tr -d ' \r')
    [ -n "$grp" ] || return 1
    [ "$nv" = "$grp" ]
}

# When this volume IS the firmware default, finishing means staying up: the
# reboot below is right for the *host* volume, but here it would land back
# and halt, leaving a completed A/B unreachable until somebody walks over.
# Staying up does not loop (the agent is removed) and keeps the results
# reachable over the network immediately.
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
