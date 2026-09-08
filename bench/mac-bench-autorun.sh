#!/bin/bash
# The benchmark install running an A/B by itself: planted by `wk bench mac-ab` from host mode, started by a per-user LaunchAgent at autologin. It drives itself, unlike bench/mac-lane.sh, because this machine has no network in bench mode (tolken is Wi-Fi only).
# There is no software boot-volume switch on Apple Silicon, so it reboots and sees where it lands. THE ORDER OF OPERATIONS IS THE SAFETY: the state file is advanced before the run so a power cut cannot repeat the attempt, the watchdog is armed before the first run, and the hand-back runs from a trap.

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

_left=""
leave_bench() {
    local how="$1" why="$2"
    [ -n "$_left" ] && return 0
    _left=1
    say "leaving bench mode ($how): $why"
    state_set left_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    state_set left_how "$how"
    sync 2>/dev/null || true
    case "$how" in  # halt where a reboot would loop back to this volume; sudo -n logs rather than hangs if NOPASSWD is gone
        halt)   sudo -n shutdown -h now >/dev/null 2>&1 || say "WARNING: could not halt" ;;
        *)      sudo -n shutdown -r now >/dev/null 2>&1 || say "WARNING: could not reboot" ;;
    esac
}

# The file only: `launchctl bootout` would kill this script, its own child.
remove_agent() {
    [ -f "$AGENT_PLIST" ] || return 0
    rm -f "$AGENT_PLIST"
    say "removed the launch agent ($AGENT_PLIST)"
}

FB_PLIST=/Library/LaunchDaemons/com.wk.bench-firstboot.plist
FB_SELF=/usr/local/libexec/wk-bench-firstboot.sh
FB_LOG=/var/log/wk-bench-firstboot.log

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

fb_provisioned() {  # the daemon removes itself just before it logs this line, so the log is the only record that provisioning ever finished
    grep -q "provisioning complete" "$FB_LOG" 2>/dev/null
}

# Provisioning applies the desktop quieting every leg's own preflight then requires, so a volume it never finished measures nothing: `wk bench staged` refuses each leg for settings that are not a measured Mac's, one leg after another, on a machine with no network to say so. Asked of the log and not of the daemon, because either can be absent: a daemon killed partway leaves the volume unprovisioned with nothing installed to finish it.
refuse_unprovisioned() {
    if fb_provisioned; then
        return 0
    fi
    say "$FB_LOG records no 'provisioning complete', so this volume was never"
    say "  provisioned and cannot be measured on."
    if pgrep -f wk-bench-firstboot >/dev/null 2>&1; then
        say "  provisioning is running right now -- standing aside so it can finish."
        say "  It reboots at the end, and this agent starts again on that boot."
        exit 0
    fi
    local installed=no
    if [ -f "$FB_PLIST" ] || [ -f "$FB_SELF" ]; then
        installed=yes
    fi
    say "  nothing is running to finish it (daemon installed: $installed), and running"
    say "  the job now would fail every leg for the settings it applies. From host mode:"
    say "    wk bench mac-volume --repair    then boot this volume once"
    leave_bench halt "provisioning never completed"
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
refuse_unprovisioned
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


PLANS=$(jf_list plans);  PLANS="${PLANS:-speedometer3}"
ROUNDS=$(jf rounds);     ROUNDS="${ROUNDS:-5}"
MAX_ROUNDS=$(jf max_rounds); MAX_ROUNDS="${MAX_ROUNDS:-40}"
DETECT=$(jf detect_pct); DETECT="${DETECT:-0.3}"
TIMEOUT=$(jf timeout);   TIMEOUT="${TIMEOUT:-1800}"
COUNT=$(jf count)
TOOLS=$(jf wk_tools);    TOOLS="${TOOLS:-$HOME/Development/wk-tools}"
NARMS=$(jf n_arms);      NARMS="${NARMS:-2}"
SETTLE=$(jf settle);     SETTLE="${SETTLE:-90}"
FORCE=$(jf force)  # for the guest rehearsal, which cannot pass the quiet check; empty on the real volume

export WK_BENCH_ASLR=$(jf aslr)
export WK_BENCH_ENV_PAD=$(jf env_pad)
export WK_BENCH_PATH_PAD=$(jf path_pad)
export WK_BENCH_SHARED_CACHE=$(jf shared_cache)

say "job: plans=$PLANS rounds=$ROUNDS-$MAX_ROUNDS detect=${DETECT}% arms=$NARMS timeout=${TIMEOUT}s count=${COUNT:-default}${FORCE:+ FORCED}"
say "     variance: aslr=${WK_BENCH_ASLR:-unset} env_pad=${WK_BENCH_ENV_PAD:-0} path_pad=${WK_BENCH_PATH_PAD:-0} shared_cache=${WK_BENCH_SHARED_CACHE:-unset}"
say "     wk-tools=$TOOLS"

[ -x "$TOOLS/wk" ] || {
    say "FATAL: no wk at $TOOLS/wk -- cannot run anything"
    state_set phase done; state_set outcome "no-wk-tools"
    remove_agent
    leave_bench reboot "no wk-tools"
    exit 1
}

state_set phase running
state_set plans "$PLANS"
state_set started_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Silence, not a deadline: the round count is decided by the numbers as they arrive, so there is no total to budget. Generous, because the first run against a freshly copied build tree is legitimately much slower than the rest and a watchdog firing on it costs a whole cycle.
STALL=$(( TIMEOUT + 900 ))
say "watchdog: ${STALL}s of silence"
(
    while :; do
        sleep 60
        [ "$(state_get phase)" = done ] && exit 0
        quiet=$(( $(date +%s) - $(stat -f %m "$LOG") ))
        [ "$quiet" -lt "$STALL" ] && continue
        say "WATCHDOG FIRED -- nothing written for ${quiet}s; the run is not coming back"
        state_set phase done
        state_set outcome watchdog
        sudo -n shutdown -r now >/dev/null 2>&1
        exit 0
    done
) &
WATCHDOG=$!

# A trap, so a `set -e` death cannot leave the machine in bench mode.
trap 'kill "$WATCHDOG" 2>/dev/null; leave_bench reboot "run finished or failed"' EXIT INT TERM

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

# The preference reads back false yet a scan can still run, so the daemon is booted out; self-reversing, since this install reboots when the job ends.
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

RUNS="$WK_AB_ROOT/ab/$(state_get job_stamp)"
[ -n "$(state_get job_stamp)" ] || RUNS="$WK_AB_ROOT/ab/unstamped"
mkdir -p "$RUNS" 2>/dev/null
newest_result() { ls -1 "$WK_AB_ROOT/results" 2>/dev/null | sort | tail -1 || true; }

leg() {   # <round> <plan> <arm index> [profile]. A software-update scan across one arm is a number to drop, not a reason to disbelieve the rest, so each row says.
    local r="$1" plan="$2" i="$3" profile="${4:-}"
    local label sid bargs before msu_before msu_after clean got rc
    label=$(jf "arms.$i.label");        label="${label:-arm$i}"
    sid=$(jf "arms.$i.id")
    bargs=$(jf "arms.$i.browser_args")
    say "--- round $r, $plan, arm $label (staged $sid) ---"
    set -- bench staged --plan "$plan" --timeout "$TIMEOUT"
    [ -n "$sid" ]   && set -- "$@" --id "$sid"
    [ -n "$COUNT" ] && set -- "$@" --count "$COUNT"
    [ -n "$bargs" ] && set -- "$@" --browser-args "$bargs"
    [ -n "$profile" ] && set -- "$@" --profile "$profile"
    [ -n "$FORCE" ] && set -- "$@" --force
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

arm_results() {  # <plan> <label> -- the result.json paths recorded for that arm, comma-separated
    awk -F'\t' -v p="$1" -v l="$2" -v root="$WK_AB_ROOT" \
        '$6 == p && $2 == l && $5 == "clean" { printf "%s%s/results/%s/result.json", sep, root, $4; sep="," }' \
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
        leave_bench halt "arm A is not staged"
        exit 0
    fi
    say "browser check against arm A's build ($sid)"
    if /usr/bin/python3 "$TOOLS/bench/mac-browser-check.py" \
            --build-directory "${dir%/}" --json "$RUNS/browser-check.json" >>"$LOG" 2>&1; then
        say "  the browser here is accelerated and unthrottled (readings above)"
        return 0
    fi
    say "  this install cannot present a browser worth measuring (faults above)."
    say "  Every round would measure that instead of the patch, so nothing runs."
    leave_bench halt "browser check failed"
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

# `boot-volume` is three colon-separated UUIDs, only the last naming anything on disk.
booted_is_default() {
    local nv grp
    nv=$(python3 "$TOOLS/lib/wkmac.py" boot-volume 2>/dev/null) || true
    nv="${nv##*:}"
    [ -n "$nv" ] || return 1
    grp=$(python3 "$TOOLS/lib/wkmac.py" volume-group / 2>/dev/null | tr -d ' \r') || true
    [ -n "$grp" ] || return 1
    [ "$nv" = "$grp" ]
}

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
