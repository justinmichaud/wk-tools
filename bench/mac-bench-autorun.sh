#!/bin/bash
# The benchmark install running an A/B by itself: planted by `wk bench mac-ab` from host mode, started by a per-user LaunchAgent at autologin. It drives itself, unlike bench/mac-lane.sh, because this machine has no network in bench mode (tolken is Wi-Fi only).
# The bench volume is the firmware default, so the job ends with the machine powered off however it ends: a reboot would land back here and run it again. THE ORDER OF OPERATIONS IS THE SAFETY: the state file is advanced before the run so a power cut cannot repeat the attempt and before the summary so a power cut in that cannot either, the watchdog is armed before the first run, and the power-off runs from a trap.

set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

WK_AB_ROOT="${WK_AB_ROOT:-/var/wk}"
# On this install /var/wk *is* where wk keeps its artifacts: it is uid 501 and writable, where the Darwin default (/var/lib/wk) is root's. The planter seeds the profiler into it, this install having no network to fetch one over.
export WK_STORE="$WK_AB_ROOT"
JOB="$WK_AB_ROOT/job.json"
STATE="$WK_AB_ROOT/autorun.state"
LOG="$WK_AB_ROOT/autorun.log"
AGENT_LABEL="com.wk.bench-ab"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

MAX_ATTEMPTS=3   # so "try again" cannot mean "boot loop"

mkdir -p "$WK_AB_ROOT" 2>/dev/null
exec >>"$LOG" 2>&1

say() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

state_get() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null | tail -1 || true; }  # || true: a missing $STATE makes sed exit nonzero, fatal under pipefail
state_set() {
    local k="$1" v="$2" tmp
    tmp="$STATE.tmp.$$"
    { grep -v "^$k=" "$STATE" 2>/dev/null || true; printf '%s=%s\n' "$k" "$v"; } > "$tmp" \
        && mv "$tmp" "$STATE"
    sync 2>/dev/null || true  # the next thing this has to survive is an ungraceful reboot
}

jf() {  # a field out of the job, by python because the job is json; || true because the python side signals an absent field with exit 1
    /usr/bin/python3 - "$JOB" "$1" <<'PY' 2>/dev/null || true
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

jf_list() {  # a JSON array of strings as one space-separated line
    /usr/bin/python3 - "$JOB" "$1" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
v = d.get(sys.argv[2])
if not isinstance(v, list):
    sys.exit(1)
print(" ".join(str(x) for x in v))
PY
}

TOOLS=$(jf wk_tools); TOOLS="${TOOLS:-$HOME/Development/wk-tools}"   # read here because the settings this volume is judged by are read out of the tree, before the job is
QUIET_DESKTOP="$TOOLS/bench/mac-quiet-desktop.sh"

# The host install: the one mounted macOS system volume that is not this one and carries no bench marker -- the mirror of wk-boot-priv's own gate, read here because this install has the sudo and the host one is not running.
host_install() {
    local v n=0 found=""
    for v in /Volumes/*; do
        [ -f "$v/System/Library/CoreServices/SystemVersion.plist" ] || continue
        [ -f "$v/etc/wk-image" ] && continue
        [ "$(stat -f %d "$v" 2>/dev/null)" = "$(stat -f %d / 2>/dev/null)" ] && continue
        found="$v"; n=$((n + 1))
    done
    [ "$n" -eq 1 ] || return 1
    printf '%s' "$found"
}

# Hands the machine back rather than halting on it, so the only human step is a login. Ordered so a bless that did not take cannot cost a boot loop: this volume is the firmware default, so a reboot with it still default lands back here and measures again -- the reboot is taken only once the firmware is *read back* as naming the host install, and otherwise this halts, which is always safe. Whether `bless --setBoot` succeeds for a volume this account does not own is the platform's answer and one boot has it.
_left=""
_stay=""
_had_job=""

# A boot that produced no number is a boot somebody has to read, and this log is only readable while this install is up: booting this Mac into *host* mode needs a password typed at the machine and booting the benchmark volume does not, so handing back is what makes a refusal unreadable. Held here, reachable on the tailnet, for a bounded window; a boot whose legs landed hands back at once, its numbers being on the volume either way.
BENCH_HOLD="${WK_MAC_BENCH_HOLD:-900}"
hold_for_a_reader() {
    [ -n "$_had_job" ] || return 0
    [ -s "${RUNS:-/nonexistent}/runs.tsv" ] && return 0
    case "$BENCH_HOLD" in ''|*[!0-9]*) return 0 ;; 0) return 0 ;; esac
    say "no number came out of this boot. Holding the machine here for ${BENCH_HOLD}s,"
    say "  where it is reachable and host mode would not be:"
    say "    ssh $(sed -n 's/^hostname=//p' "$WK_AB_ROOT/tailnet/tailnet.conf" 2>/dev/null || echo '<the bench node>') tail -120 $LOG"
    sleep "$BENCH_HOLD"
}

leave_bench() {
    local why="$1" host grp want
    [ -n "$_left" ] && return 0
    _left=1
    state_set left_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    hold_for_a_reader

    if host=$(host_install); then
        want=$(python3 "$TOOLS/lib/wkmac.py" volume-group "$host" 2>/dev/null) || want=""
        sudo -n bless --mount "$host" --setBoot >/dev/null 2>&1 || true
        grp=$(python3 "$TOOLS/lib/wkmac.py" boot-volume 2>/dev/null) || grp=""
        grp="${grp##*:}"
        if [ -n "$want" ] && [ "$grp" = "$want" ]; then
            say "handing the machine back: $why"
            say "  the firmware now names $host, so this reboot comes up in host mode"
            sync 2>/dev/null || true
            sudo -n /sbin/reboot >/dev/null 2>&1 || say "WARNING: could not reboot"
            return 0
        fi
        say "  the firmware still names this volume (reads '${grp:-nothing}', wanted '${want:-unreadable}')"
        say "  so it powers off instead: a reboot would land back here and measure again."
    else
        say "  no single host install is mounted, so there is nothing to hand back to"
    fi

    say "powering off: $why"
    sync 2>/dev/null || true
    # `/sbin/halt`, not `shutdown -h`, which asks loginwindow: any modal dialog on the screen vetoes that, and one sat over a finished first boot for ten minutes (2026-09-07). `-n` logs rather than hangs if NOPASSWD is gone.
    sudo -n /sbin/halt >/dev/null 2>&1 || say "WARNING: could not power off"
}

# The file only: `launchctl bootout` would kill this script, its own child.
remove_agent() {
    [ -f "$AGENT_PLIST" ] || return 0
    rm -f "$AGENT_PLIST"
    say "removed the launch agent ($AGENT_PLIST)"
}

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

stand_aside_if_provisioning() {   # before defuse_firstboot, which would otherwise kill the daemon mid-provisioning
    pgrep -f wk-bench-firstboot >/dev/null 2>&1 || return 0
    say "provisioning is running right now -- standing aside so it can finish."
    say "  It reboots at the end, and this agent starts again on that boot."
    _stay=1
    exit 0
}

# Judged by the same probe and findings `wk bench staged` uses before every leg, so a volume that drifted is refused up front rather than leg by leg on a machine with no network to say so. After `wk quiesce on`, never before it: the user half of those rows does not survive this account's session starting, so quiesce writes them again where they can take (cmd/quiesce says why), and judging first refused a volume on rows this boot was about to set -- which is what job 20260909T042343Z did, in its first minute.
refuse_unprovisioned() {
    local probe wrong installed=no
    if [ ! -r "$QUIET_DESKTOP" ]; then
        say "no $QUIET_DESKTOP, so nothing here can judge what this volume is set to."
        leave_bench "no quiet-desktop table to judge this volume by"
        exit 0
    fi
    # shellcheck disable=SC1090
    . "$QUIET_DESKTOP"
    probe=$(wk_quiet_desktop_probe)
    wrong=$(wk_quiet_desktop_findings "$probe" "" | awk -F'\t' '$1 == "wrong" { print $2 }')
    [ -n "$wrong" ] || return 0
    if [ -f "$FB_PLIST" ] || [ -f "$FB_SELF" ]; then installed=yes; fi
    say "this volume is not set up as a measured Mac (first-boot daemon installed: $installed):"
    printf '%s\n' "$wrong" | while IFS= read -r _w; do say "  $_w"; done
    say "  Every leg would be refused for these. From host mode:"
    say "    wk bench mac-volume --repair    then boot this volume once"
    leave_bench "this volume is not set up as a measured Mac"
    exit 0
}

# A daemon that outlives its own provisioning runs on every boot: it rsync --deletes an older wk-tools over ~bench/Development/wk-tools and ends with `shutdown -r +1`, a minute after this agent starts. Only reached once the log records the end, so what it interrupts here is a redundant re-run and never the provisioning itself.
defuse_firstboot() {
    [ -f "$FB_PLIST" ] || [ -f "$FB_SELF" ] || { cancel_pending_reboot; return 0; }
    say "the first-boot daemon outlived its provisioning -- defusing it"
    if pgrep -f wk-bench-firstboot >/dev/null 2>&1; then
        say "  it is re-running right now -- stopping it before it schedules a reboot"
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


if [ ! -f /etc/wk-image ]; then
    say "not bench mode (/etc/wk-image absent) -- this agent has nothing to do here"
    remove_agent
    exit 0
fi
say "bench mode: $(sed -n 's/^id=//p' /etc/wk-image)"
stand_aside_if_provisioning
defuse_firstboot

if [ ! -f "$JOB" ]; then
    say "no job at $JOB -- nothing to run"
    remove_agent
    leave_bench "no job"
    exit 0
fi

PHASE=$(state_get phase)
ATTEMPTS=$(state_get attempts); ATTEMPTS=${ATTEMPTS:-0}

if [ "$PHASE" = done ]; then
    say "the job is already finished, and this volume booted again -- so it is the"
    say "firmware default. Powering off rather than looping."
    remove_agent
    leave_bench "job already complete"
    exit 0
fi

ATTEMPTS=$((ATTEMPTS + 1))
state_set attempts "$ATTEMPTS"
if [ "$ATTEMPTS" -gt "$MAX_ATTEMPTS" ]; then
    say "attempt $ATTEMPTS exceeds the limit of $MAX_ATTEMPTS -- abandoning the job"
    state_set phase done
    state_set outcome abandoned
    remove_agent
    leave_bench "too many attempts"
    exit 0
fi
say "attempt $ATTEMPTS of $MAX_ATTEMPTS"
_had_job=1   # from here on a refusal is something to read, so leave_bench holds the machine first

PLANS=$(jf_list plans);  PLANS="${PLANS:-speedometer3}"
ROUNDS=$(jf rounds);     ROUNDS="${ROUNDS:-5}"
MAX_ROUNDS=$(jf max_rounds); MAX_ROUNDS="${MAX_ROUNDS:-40}"
DETECT=$(jf detect_pct); DETECT="${DETECT:-0.3}"
TIMEOUT=$(jf timeout);   TIMEOUT="${TIMEOUT:-1800}"
COUNT=$(jf count);       COUNT="${COUNT:-2}"   # one run of one count carries no within-run p-value
NARMS=$(jf n_arms);      NARMS="${NARMS:-2}"
SETTLE=$(jf settle);     SETTLE="${SETTLE:-90}"
DISPLAY_EXPECT=$(jf display)
REHEARSAL=$(jf rehearsal)   # its own field and never `--force`'s: one flag meaning both "cross a driver barrier" and "force every leg" made crossing the first silently disable the second

export WK_BENCH_ASLR=$(jf aslr)
export WK_BENCH_ENV_PAD=$(jf env_pad)
export WK_BENCH_PATH_PAD=$(jf path_pad)
export WK_BENCH_SHARED_CACHE=$(jf shared_cache)

say "job: plans=$PLANS rounds=$ROUNDS-$MAX_ROUNDS detect=${DETECT}% arms=$NARMS timeout=${TIMEOUT}s count=$COUNT"
say "     variance: aslr=${WK_BENCH_ASLR:-unset} env_pad=${WK_BENCH_ENV_PAD:-0} path_pad=${WK_BENCH_PATH_PAD:-0} shared_cache=${WK_BENCH_SHARED_CACHE:-unset}"
say "     wk-tools=$TOOLS  display=${DISPLAY_EXPECT:-unpinned}"
[ -z "$REHEARSAL" ] || say "     REHEARSAL: every leg is forced past its own preflight, and every number
     it takes is recorded as forced. This measures the path, not the machine."

RUNS="$WK_AB_ROOT/ab/$(state_get job_stamp)"
[ -n "$(state_get job_stamp)" ] || RUNS="$WK_AB_ROOT/ab/unstamped"
mkdir -p "$RUNS" 2>/dev/null

summarise() {
    say "summarising"
    "$TOOLS/wk" bench ab-summary --root "$WK_AB_ROOT" --runs "$RUNS/runs.tsv" \
        --out "$RUNS/summary.txt" >>"$LOG" 2>&1 \
        || say "(no summary -- 'wk bench ab-summary' failed; the results are still on the volume)"
}

# Silence, not a deadline: the round count is decided by the numbers as they arrive, so there is no total to budget. Generous, because the first run against a freshly copied build tree is legitimately much slower than the rest and a watchdog firing on it costs a whole cycle.
watchdog() {
    local quiet
    while :; do
        sleep 60
        [ "$(state_get phase)" = done ] && return 0
        quiet=$(( $(date +%s) - $(stat -f %m "$LOG") ))
        [ "$quiet" -lt "$STALL" ] && continue
        say "WATCHDOG FIRED -- nothing written for ${quiet}s; the run is not coming back"
        state_set phase done      # before the summary, so a power cut in it cannot repeat the attempt
        state_set outcome watchdog
        summarise                 # the rounds that did land are a measurement, and this is the only machine that can say what they resolve
        leave_bench "watchdog: nothing written for ${quiet}s"
        return 0
    done
}

STALL=$(( TIMEOUT + 900 ))
say "watchdog: ${STALL}s of silence"
watchdog &   # armed before the first step that can block -- joining a tailnet, writing a display mode, quiescing, launching a browser all wait on the system for as long as it takes, and a boot that reaches one of those with neither this nor the trap below is a machine left in bench mode with nothing able to report it and no way back
WATCHDOG=$!
# `_stay` is set where an exit deliberately leaves the machine to reboot itself, and handing back would take the boot it is waiting for.
trap 'kill "$WATCHDOG" 2>/dev/null; [ -n "$_stay" ] || leave_bench "run finished or failed"' EXIT INT TERM

# The panel is a load on the package the browser is measured on, and a panel left lit is a panel being spent, so it goes down before anything that can stall -- the tailnet join, the mode write, the quiesce, a browser -- and nothing later raises it. It stayed at 0.85 for tens of minutes on 2026-09-09 while `wk quiesce on` was deadlocked, because this ran after it.
dim_display() {
    local got rc=0
    got=$(python3 "$TOOLS/lib/wkmac.py" brightness --set 0) || rc=$?
    if [ "$rc" -ne 0 ]; then
        say "the display would not go to minimum brightness (rc=$rc, read back '${got:-nothing}')."
        say "  A backlight that varies is a load that varies, so nothing runs."
        leave_bench "the display would not dim"
        exit 0
    fi
    say "display at minimum brightness (reads $got)"
}
dim_display

[ -x "$TOOLS/wk" ] || {
    say "FATAL: no wk at $TOOLS/wk -- cannot run anything"
    state_set phase done; state_set outcome "no-wk-tools"
    remove_agent
    leave_bench "no wk-tools"
    exit 1
}

refuse_unpinned_display() {   # unpinned is two runs at different resolutions compared as if they matched, with nothing downstream to say so
    [ -z "$DISPLAY_EXPECT" ] || return 0
    say "the job names no display, so what a round would be measured at is unknown."
    say "  From host mode: set NODE_DISPLAY in boot/machines/mbp.conf, then plant again."
    state_set phase done
    state_set outcome "no-display-expectation"
    remove_agent
    leave_bench "the job names no display"
    exit 0
}
refuse_unpinned_display

# The declared mode is held, not hoped for: WindowServer reads it at start and no runtime call reaches a scaled mode (lib/wkmac.py says why), so a mode that is not the declared one is written and this boot repeated -- the firmware default is this volume, so the repeat lands back here. Bounded by the record: a write that did not take is refused rather than tried again, and the repeat spends no attempt, having measured nothing.
converge_display_mode() {
    local want running
    want=$(printf '%s' "$DISPLAY_EXPECT" | awk '{print $2}')
    running=$(python3 "$TOOLS/lib/wkmac.py" display-mode) || running=""
    if [ "$running" = "$want" ]; then
        say "display mode: $running, as the job declares"
        return 0
    fi
    say "display mode: running at ${running:-unreadable}, and the job declares $want"
    if [ "$(state_get mode_declared)" = "$want" ]; then
        say "  $want was written into the WindowServer configuration for this boot and"
        say "  the panel still comes up at ${running:-unreadable}, so the write does not take."
        say "  Nothing is measured at a mode that is not the declared one: MotionMark's"
        say "  score is the area it draws. From host mode, set the mode on this install"
        say "  by hand and re-plant, or declare the mode it does come up at:"
        say "    NODE_DISPLAY=\"${DISPLAY_EXPECT%% *} ${running:-<what it reads>}\"  in boot/machines/mbp.conf"
        state_set phase done
        state_set outcome "display-mode-unsettable"
        remove_agent
        leave_bench "the declared display mode cannot be set"
        exit 0
    fi
    if ! sudo -n python3 "$TOOLS/lib/wkmac.py" display-mode --declare "$want" >/dev/null 2>&1; then
        say "  the WindowServer configuration would not take $want."
        state_set phase done
        state_set outcome "display-mode-unwritable"
        remove_agent
        leave_bench "the declared display mode could not be written"
        exit 0
    fi
    state_set mode_declared "$want"
    state_set attempts "$((ATTEMPTS - 1))"   # this boot measured nothing
    say "  wrote it; restarting so WindowServer comes up at $want"
    _stay=1   # the reboot below is the point of this branch; handing back would take it
    sync 2>/dev/null || true
    sudo -n /sbin/reboot >/dev/null 2>&1 || say "WARNING: could not restart"
    exit 0
}
hold_auto_brightness() {
    local got rc=0
    got=$(python3 "$TOOLS/lib/wkmac.py" auto-brightness --off) || rc=$?
    case "$got" in
        off)  say "ambient light: compensation off (read back)" ;;
        none) say "ambient light: this panel has no sensor to hold" ;;
        *)    say "ambient light: still reads '${got:-nothing}' (rc=$rc) after being turned off."
              say "  A brightness the sensor can raise again is a load that varies, so"
              say "  nothing runs. The display gate below says the same thing."
              state_set attempts "$((ATTEMPTS - 1))"
              leave_bench "ambient-light compensation could not be turned off"
              exit 0 ;;
    esac
}

# Asked before the mode is touched, and about the topology only -- one online panel and it the built-in one -- because with a second attached there is no single built-in mode to converge to, and the mode itself is what converge_display_mode below is for. The driver asked the same question seconds before the restart, so what this catches is a monitor plugged in between the two.
refuse_wrong_displays() {
    local said rc=0
    said=$(/usr/bin/python3 "$TOOLS/bench/mac-browser-check.py" --displays-only 2>&1) || rc=$?
    if [ "$rc" -eq 0 ]; then
        say "displays: $said"
        return 0
    fi
    say "the screen this would be measured on is not the declared one:"
    printf '%s\n' "$said" | while IFS= read -r _l; do say "  $_l"; done
    say "  Nothing runs and no mode is written. Disconnect the monitor and boot"
    say "  this volume again -- the job stays planted and spends no attempt."
    state_set attempts "$((ATTEMPTS - 1))"
    leave_bench "the display is not the declared one"
    exit 0
}
# This install provisions itself from what the plant put in /var/wk with no privilege at all: every path it writes belongs to this install, over which the bench account holds passwordless root by design, which is why no step of the lane needs a password on the host install. `stage_payload` is the host writer's own function handed this install's root, so a payload file cannot land one way and not the other.
converge_self() {
    local vol="$TOOLS/../tailnet"
    [ -r "$TOOLS/bench/mac-bench-payload.sh" ] || { say "no payload writer in the planted tree, so this install cannot converge itself"; return 0; }
    say "converging this install from the planted tree"
    # The writer alone, never mac-bench-volume.sh: that one dispatches on source (`case "${ACTION:---report}"`), so sourcing it would run a report and its argument parsing.
    if sudo -n bash -c "set -e
WK_ROOT=$(printf %q "$TOOLS")
. \"\$WK_ROOT/lib/common.sh\"
run() { \"\$@\"; }
. \"\$WK_ROOT/bench/mac-bench-payload.sh\"
stage_payload /" >>"$LOG" 2>&1; then
        say "  payload staged into this install"
    else
        say "  WARNING: the payload did not fully stage; the log above says which file"
    fi
    if [ -d "$vol" ]; then
        if sudo -n "$TOOLS/bench/mac-tailnet.sh" install / "$vol" >>"$LOG" 2>&1; then
            say "  tailnet payload installed"
        else
            say "  WARNING: the tailnet payload would not install; this install stays unreachable"
        fi
    else
        say "  no tailnet payload at $vol, so this install has no tailnet identity to join with"
    fi
    if sudo -n "$TOOLS/bench/mac-tailnet.sh" join >>"$LOG" 2>&1; then
        say "  tailnet: joined, so this run can be watched while it measures"
    else
        say "  tailnet: did not join (see the log); the run is unobservable but not affected"
    fi
}
converge_self


hold_auto_brightness   # before the display gate, which refuses a panel under ambient-light control: it is a load that varies, and this is what holds it rather than declining the machine
refuse_wrong_displays
converge_display_mode

state_set phase running
state_set plans "$PLANS"
state_set started_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# The agent starts at login, the moment the machine is least quiet.
say "settling for ${SETTLE}s"
sleep "$SETTLE"

# DARWIN_USER_TEMP_DIR, which run-benchmark's `patch` writes into, is created by the per-user bootstrap, which at login may not have happened yet.
for _t in 1 2 3 4 5 6 7 8 9 10 11 12; do
    _tmp=$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null) || true
    [ -n "$_tmp" ] && [ -d "$_tmp" ] && [ -w "$_tmp" ] && break
    say "waiting for the per-user temp directory (${_tmp:-unset})"
    sleep 5
done
_tmp=$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null) || true
if [ -n "$_tmp" ] && [ -w "$_tmp" ]; then
    say "temp: $_tmp"
else
    say "WARNING: no writable per-user temp directory -- run-benchmark's patch step will fail"
fi

cancel_pending_reboot  # again, now that the minute someone else could schedule one has passed

# A window over MiniBrowser throttles it into a timeout with no error. Killing one takes the desktop session with it when it is Setup Assistant (measured 2026-09-05: the console user went back to root), so each is named and killed on its own rather than as one pattern.
if [ -r "$TOOLS/lib/quiet.sh" ]; then
    front=$( . "$TOOLS/lib/common.sh" >/dev/null 2>&1
             . "$TOOLS/lib/quiet.sh"  >/dev/null 2>&1
             screen_blocker 2>/dev/null )
    if [ "$front" = "?" ]; then
        say "WARNING: could not ask the window server what is on the screen; a run that
    times out with no error is this and nothing else"
    elif [ -n "$front" ]; then
        say "on the screen, and nothing this job put there: $front"
        sudo -n touch /var/db/.AppleSetupDone >/dev/null 2>&1 || true
        printf '%s' "$front" | tr ',' '\n' | while IFS= read -r _w; do
            [ -n "$_w" ] || continue
            case "$_w" in
                "Setup Assistant") say "  leaving '$_w': killing it ends the desktop session" ;;
                *) say "  closing '$_w'"; sudo -n pkill -f "$_w.app" >/dev/null 2>&1 || true ;;
            esac
        done
        sleep 10
    fi
fi

# The modal auth panel is invisible to the check above (SecurityAgent never becomes the frontmost *application*), and SIGKILL because it holds XPC transactions open instead of exiting.
if pgrep -x SecurityAgent >/dev/null 2>&1; then
    say "a modal authentication panel is up (SecurityAgent) -- dismissing it"
    sudo -n killall -9 SecurityAgent >/dev/null 2>&1 || true
    sleep 3
    pgrep -x SecurityAgent >/dev/null 2>&1 \
        && say "  WARNING: it is still up; the browser may not get focus" \
        || say "  dismissed"
fi

# The preference reads back false yet a scan can still run, so the daemon is booted out; self-reversing, since this install goes off when the job ends.
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

# Read out of the plist file because cfprefsd answers with values the file does not carry, and sorted because a re-serialised dict can reorder. Any of these moving across a run means a scan ran.
msu_stamp() {
    /usr/bin/plutil -p /Library/Preferences/com.apple.SoftwareUpdate.plist 2>/dev/null \
        | grep -E '"Last[A-Za-z]*Date"' | sort | tr -d ' \n' || true
}
say "  scan stamp before the job: $(msu_stamp)"

say "quiescing"
"$TOOLS/wk" quiesce on >>"$LOG" 2>&1 || say "WARNING: quiesce reported a problem; the runner will judge it"

refuse_unprovisioned

newest_result() { ls -1 "$WK_AB_ROOT/results" 2>/dev/null | sort | tail -1 || true; }

repause_daemons() {   # again before every leg, not once before the rounds: macOS restarts these on demand, and one that came back between the quiesce and a leg fails that leg on the gate `wk bench staged` asks -- XProtect did, and took all four legs of job 20260909T154515Z with it. The quiesce's own function, so the rule has one implementation, and the gate still judges what this left
    sudo -n bash -c ". $(printf %q "$QUIET_DESKTOP"); wk_quiet_daemons_pause" >>"$LOG" 2>&1 \
        || say "    WARNING: could not re-pause the background daemons; the leg's own gate will say which came back"
}

leg() {   # <round> <plan> <arm index> [profile]. A software-update scan across one arm is a number to drop, not a reason to disbelieve the rest, so each row says.
    local r="$1" plan="$2" i="$3" profile="${4:-}"
    local label sid bargs before msu_before msu_after clean got rc
    label=$(jf "arms.$i.label");        label="${label:-arm$i}"
    sid=$(jf "arms.$i.id")
    bargs=$(jf "arms.$i.browser_args")
    say "--- round $r, $plan, arm $label (staged $sid) ---"
    repause_daemons
    set -- bench staged --plan "$plan" --timeout "$TIMEOUT" --expect-display "$DISPLAY_EXPECT"
    [ -z "$REHEARSAL" ] || set -- "$@" --force
    [ -n "$sid" ]   && set -- "$@" --id "$sid"
    set -- "$@" --count "$COUNT"
    [ -n "$bargs" ] && set -- "$@" --browser-args "$bargs"
    [ -n "$profile" ] && set -- "$@" --profile "$profile"
    before=$(newest_result)
    msu_before=$(msu_stamp)
    rc=0
    "$TOOLS/wk" "$@" >>"$LOG" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        say "--- round $r, $plan, arm $label: FAILED (rc=$rc) ---"
        state_set "fail_${plan}_${label}_$r" "$rc"
        return 1
    fi
    say "--- round $r, $plan, arm $label: OK ---"
    state_set "ok_${plan}_${label}_$r" 1
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
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$r" "$label" "$sid" "$got" "$clean" "$plan" >> "$RUNS/runs.tsv"
        say "    -> results/$got ($clean)"
    else
        say "    WARNING: no new result directory appeared"
    fi
    return 0
}

arm_results() {  # <plan> <label> -- the run directories recorded for that arm, comma-separated
    awk -F'\t' -v p="$1" -v l="$2" -v root="$WK_AB_ROOT" \
        '$6 == p && $2 == l && $5 == "clean" { printf "%s%s/results/%s", sep, root, $4; sep="," }' \
        "$RUNS/runs.tsv" 2>/dev/null
}

# Peeking at a p-value and stopping when it crosses inflates the false-positive rate; peeking at how fine a difference the data resolves does not.
# The job carries this as JSON, so `--detect 0` arrives as `0.0` and a string test against `0` reads it as "stopping rule on" -- measured 2026-09-07, a run asked for one round and took forty.
detect_off() {
    awk -v d="${DETECT:-0}" 'BEGIN { exit !(d + 0 == 0) }'
}

plan_resolves() {  # <plan> -- 0 when this plan already detects $DETECT
    local plan="$1" a b out
    a=$(arm_results "$plan" A) || a=""; b=$(arm_results "$plan" B) || b=""
    [ -n "$a" ] && [ -n "$b" ] || return 1
    out=$(/usr/bin/python3 "$TOOLS/lib/wkdata.py" ab-precision \
            --a "$a" --b "$b" --target "$DETECT" 2>/dev/null) || return 1
    say "  $plan: $(printf '%s' "$out" | tr '\n' ' ')"
    printf '%s' "$out" | grep -q '^met=yes$'
}

# `wk bench staged` judges each leg's settings; this judges the browser those settings are for, which is the thing actually measured. Once per boot and before any round, because a window that is throttled, drawn on no Metal device, or behind something else makes every number after it a throttle's, and nothing downstream can tell. No --force crosses it: there is no number to save.
refuse_throttled_browser() {
    local sid dir
    sid=$(jf "arms.0.id")
    dir=$(ls -1d "$WK_AB_ROOT/staged/$sid"/WebKitBuild/*/ 2>/dev/null | head -1) || dir=""
    if [ -z "$dir" ]; then
        say "no products under $WK_AB_ROOT/staged/$sid -- nothing to check the browser with"
        leave_bench "arm A is not staged"
        exit 0
    fi
    say "browser check against arm A's build ($sid)"
    if /usr/bin/python3 "$TOOLS/bench/mac-browser-check.py" \
            --build-directory "${dir%/}" --expect-display "$DISPLAY_EXPECT" \
            --json "$RUNS/browser-check.json" >>"$LOG" 2>&1; then
        say "  the browser here is accelerated and unthrottled (readings above)"
        return 0
    fi
    say "  this install cannot present a browser worth measuring (faults above)."
    say "  Every round would measure that instead of the patch, so nothing runs."
    leave_bench "browser check failed"
    exit 0
}
refuse_throttled_browser

# Not measured: it absorbs the first-run effect a freshly copied build tree has, and carries the capture the measured rounds cannot take afterwards.
say "warmup round -- discarded; it profiles each arm and settles the machine"
mkdir -p "$RUNS/warmup" 2>/dev/null
warm_plan=$(printf '%s' "$PLANS" | awk '{print $1}')
i=0
while [ "$i" -lt "$NARMS" ]; do
    wlabel=$(jf "arms.$i.label"); wlabel="${wlabel:-arm$i}"
    leg 0 "$warm_plan" "$i" "$RUNS/warmup/$warm_plan-$wlabel.json.gz" \
        || say "  the warmup leg for arm $wlabel did not complete"
    i=$((i + 1))
done
if [ -f "$RUNS/runs.tsv" ]; then
    grep -v '^0	' "$RUNS/runs.tsv" > "$RUNS/runs.tsv.tmp" 2>/dev/null || true
    mv "$RUNS/runs.tsv.tmp" "$RUNS/runs.tsv" 2>/dev/null || true
fi
say "warmup done; captures in $RUNS/warmup"

# Interleaved (A B A B ...), not blocked (A A B B): the machine drifts, and blocking puts all of that drift on one side. The order flips every round, so a monotonic drift cancels rather than landing on whichever arm always goes second.
any_ok=""

# With no precision target (--detect 0), --rounds is the whole plan rather than the floor under one.
if detect_off; then CEILING="$ROUNDS"; else CEILING="$MAX_ROUNDS"; fi

r=1
while [ "$r" -le "$CEILING" ]; do
    for plan in $PLANS; do
        i=0
        while [ "$i" -lt "$NARMS" ]; do
            if [ $((r % 2)) -eq 1 ]; then arm=$i; else arm=$((NARMS - 1 - i)); fi
            leg "$r" "$plan" "$arm" && any_ok=1
            i=$((i + 1))
        done
    done
    if [ -z "$any_ok" ]; then
        say "round $r produced nothing at all -- every arm failed the same way, and"
        say "the next round has nothing different to try. Stopping here so the"
        say "machine hands itself back instead of burning the schedule."
        state_set outcome "all-failed-round-$r"
        break
    fi
    state_set rounds_done "$r"

    if [ "$r" -ge "$ROUNDS" ] && ! detect_off; then
        say "precision after round $r (target ${DETECT}%):"
        unresolved=""
        for plan in $PLANS; do
            plan_resolves "$plan" || unresolved="$unresolved $plan"
        done
        if [ -z "$unresolved" ]; then
            say "every plan resolves ${DETECT}% -- stopping at round $r"
            state_set outcome "resolved-at-round-$r"
            break
        fi
        say "  still coarser than ${DETECT}%:$unresolved"
    fi
    r=$((r + 1))
done
if [ "$r" -gt "$CEILING" ] && detect_off; then
    say "ran the $ROUNDS round(s) asked for; no precision target was set, so what"
    say "these numbers resolve is whatever 'wk bench precision' says of them."
    state_set outcome "rounds-done"
elif [ "$r" -gt "$CEILING" ]; then
    say "reached the ceiling of $MAX_ROUNDS rounds without resolving ${DETECT}% on every"
    say "plan. The numbers are real; the claim they support is the one the"
    say "precision lines above allow, and not ${DETECT}%."
    state_set outcome "hit-max-rounds"
fi

# No `wk quiesce off`: quiet is this install's permanent state, set at provisioning time.
say "leaving the machine quiesced (its permanent state; see the comment here)"

state_set phase done
state_set outcome ran
state_set finished_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
summarise
say "=== job finished ==="

kill "$WATCHDOG" 2>/dev/null  # and reaped below, or bash logs "Terminated: 15" as if it failed
wait "$WATCHDOG" 2>/dev/null || true
trap - EXIT INT TERM

leave_bench "job finished"
