# Sourced, never run, and sourcing nothing itself, so it can be streamed into a guest with no copy of wk-tools on disk yet.

wk_quiet_desktop_rows() {   # `@` is a key per hardware UUID (`defaults -currentHost`), which `tart clone` remints for every guest
    cat <<'ROWS'
widgets_desktop com.apple.WindowManager StandardHideWidgets bool true desktop widgets are hidden
widgets_stage com.apple.WindowManager StageManagerHideWidgets bool true Stage Manager's widgets are hidden
reduce_motion com.apple.universalaccess reduceMotion bool true window animations are off
reduce_transparency com.apple.universalaccess reduceTransparency bool true the compositor is not blurring behind windows
appnap NSGlobalDomain NSAppSleepDisabled bool true App Nap cannot throttle a backgrounded browser
window_anim NSGlobalDomain NSAutomaticWindowAnimationsEnabled bool false windows open and close without an animation
askforpassword com.apple.screensaver askForPassword int 0 the screen does not lock
askforpassworddelay com.apple.screensaver askForPasswordDelay int 0 the screen does not lock after a delay either
idletime @com.apple.screensaver idleTime int 0 the screen saver is disarmed
desktop_icons com.apple.finder CreateDesktop bool false nothing is drawn on the desktop
dock_launchanim com.apple.dock launchanim bool false the Dock does not animate a launch
dock_recents com.apple.dock show-recents bool false the Dock does not rearrange itself
crash_dialog com.apple.CrashReporter DialogType string none a crash writes a log instead of a dialog
quarantine com.apple.LaunchServices LSQuarantine bool false no "downloaded from the internet" dialog can appear
timemachine_offer com.apple.TimeMachine DoNotOfferNewDisksForBackup bool true no disk prompts to become a backup
personalised_ads com.apple.AdLib allowApplePersonalizedAdvertising bool false no advertising identifier is refreshed
ROWS
}

# Setup Assistant's MiniBuddy is not here: it is submitted by runningboardd on behalf of loginwindow and has no launchd label to disable (measured on a Tahoe 26.4 clone, 2026-09-05).
wk_quiet_desktop_agents() {
    cat <<'ROWS'
widgets_agent com.apple.chronod chronod redraws desktop and Notification Centre widgets on a timer of its own
notifications com.apple.notificationcenterui NotificationCenter draws a banner over whatever is being measured
notification_daemon com.apple.usernoted usernoted queues and delivers every alert an application posts
spotlight_menu com.apple.Spotlight Spotlight opens a search panel over the window
siri com.apple.assistantd assistantd listens and answers on its own
siri_knowledge com.apple.siriknowledged siriknowledged builds Siri's index in the background
siri_inference com.apple.siriinferenced siriinferenced runs on-device inference in the background
suggestions com.apple.suggestd suggestd mines documents and mail for suggestions
spotlight_suggestions com.apple.parsecd parsecd fetches Spotlight suggestions from Apple
knowledge com.apple.knowledge-agent knowledge-agent records what the account does, on a timer
proactive com.apple.proactived proactived predicts and pre-fetches on a timer
photo_analysis com.apple.photoanalysisd photoanalysisd analyses the photo library whenever the machine looks idle
media_analysis com.apple.mediaanalysisd mediaanalysisd analyses media whenever the machine looks idle
icloud_drive com.apple.bird bird syncs iCloud Drive over the run
icloud_photos com.apple.cloudphotod cloudphotod syncs the photo library over the run
music_library com.apple.AMPLibraryAgent AMPLibraryAgent scans and updates the music library
screentime com.apple.ScreenTimeAgent ScreenTimeAgent records usage and can draw a limit dialog
usage_tracking com.apple.UsageTrackingAgent UsageTrackingAgent records application usage on a timer
experiments com.apple.triald triald fetches and applies Apple's experiment configurations
sharing com.apple.sharingd sharingd advertises and scans for AirDrop and Handoff peers
tips com.apple.tipsd tipsd posts a Tips notification over the window
ROWS
}

# One list of processes that must not run during a measurement, whichever half of the machine starts them, held by signal and judged by process state. Measured 2026-09-07: `launchctl disable` then `bootout` left 17 of the 21 agents running within the second, because macOS starts them on demand; and `kill -STOP` answers EPERM for a platform binary however it is sent, so the five the kernel refuses are named above, skipped, and reported for what they can still do rather than failing every leg for ever.
# Rows no script can set on this macOS: com.apple.universalaccess is TCC-protected, so a `defaults write` for it is dropped however it is sent -- measured 2026-09-07, written at a first boot as root and again in the account's own session, still reading '?'. Demanding them refuses every leg for ever, so what they cost is said instead.
wk_quiet_desktop_unsettable() {
    printf '%s\n' reduce_motion reduce_transparency
}

_wk_qd_unsettable() { wk_quiet_desktop_unsettable | grep -qxF "$1"; }

wk_quiet_desktop_unstoppable() {   # SIP refuses SIGSTOP for these even as root -- `kill -STOP` answers `Operation not permitted`. XProtect is here because it was measured refusing (bench install, macOS 26.6.2, 2026-09-09), and because the two processes that do and schedule its scanning were already here: refusing every leg on the one that launches them was the odd case out
    printf '%s\n' ScreenTimeAgent UsageTrackingAgent suhelperd XProtect XprotectService xprotectd
}

_wk_qd_unstoppable() { wk_quiet_desktop_unstoppable | grep -qxF "$1"; }

wk_quiet_desktop_stopped() {
    while read -r name plist proc why; do
        [ -n "$plist" ] || continue
        printf '%s %s %s\n' "$name" "$proc" "$why"
    done <<ROWS
$(wk_quiet_desktop_agents)
ROWS
    wk_quiet_desktop_daemons
}

# Not mds and not sysmond: `mdutil` and `pgrep` ask those two over XPC and never return while they are held stopped -- measured in the rehearsal guest, 2026-09-05, where each deadlocked the command that would have undone it. `mdutil -i off` is what makes mds idle instead.
wk_quiet_desktop_daemons() {
    cat <<'ROWS'
spotlight_content corespotlightd indexes application content on a timer of its own
softwareupdate softwareupdated scans for updates, whatever the hosts denial lets through
softwareupdate_helper suhelperd stages an update a scan has found
malware_scan XProtect scans the whole disk on Apple's own schedule
malware_service XprotectService does that scanning
malware_daemon xprotectd schedules that scan
timemachine backupd copies the disk the run is using
timemachine_helper backupd-helper does that copying
analytics_daemon analyticsd records and uploads usage analytics
analytics_helper osanalyticshelper collects a report whenever anything crashes
diagnostics diagnosticservicesd submits diagnostics on a timer
crash_reporter ReportCrash writes a crash log and can draw a dialog over the run
hang_sampler spindump samples every process when one stops responding
power_records powerdatad records power counters on a timer
process_stats systemstats samples every process's counters on a timer
icloud cloudd fetches and pushes iCloud records mid-run
icloud_defaults syncdefaultsd syncs preferences to iCloud on a timer
downloads nsurlsessiond runs every background download in the machine
findmy searchpartyd beacons for and scans for nearby devices
experiments_system triald_system fetches Apple's experiment configurations
ROWS
}

# Judged the other way round: expected running, and never a member of wk_quiet_desktop_stopped, whose every row is held stopped by signal.
wk_quiet_desktop_expected() {
    cat <<'ROWS'
tailnet tailscaled is the bench install's only way to be reached while it measures, and pausing it would drop the tailnet mid-leg and leave a live utun with nothing draining it; it costs 1.78% of one core and 71 MB RSS, measured over 3.7 days on moose, the fleet's busiest node
ROWS
}

# One key per `pmset -a` call: which keys a Mac has depends on the model (highpowermode raises the fans, so a fanless Mac has none) and pmset applies nothing at all from a command line naming one it does not know.
wk_quiet_desktop_power() {
    cat <<'ROWS'
power_displaysleep displaysleep 0 displaysleep
power_disksleep disksleep 0 disksleep
power_sleep sleep 0 sleep
power_disablesleep disablesleep 1 SleepDisabled
power_lowpowermode lowpowermode 0 lowpowermode
power_highpowermode highpowermode 1 highpowermode
ROWS
}

_wk_qd_uid() { id -u "${1:-$(id -un)}" 2>/dev/null; }

# Do Not Disturb, which stops a banner being drawn at all rather than stopping the process that would draw it. An assertion record in the account's own home, not a preference: `defaults write com.apple.notificationcenterui doNotDisturb` governs nothing from macOS 12 on, and a record with no end timestamp is indefinite -- read off tolken 26.6.2, whose own DND is one such record from com.apple.controlcenter.dnd. Keyed by home rather than user, so the plant can write it onto a volume that is merely mounted.
WK_DND_MODE=com.apple.donotdisturb.mode.default
WK_DND_CLIENT=com.apple.controlcenter.dnd

wk_quiet_dnd_path() { printf '%s/Library/DoNotDisturb/DB/Assertions.json' "$1"; }

# dscl even for the account this is running as: a LaunchDaemon has no HOME.
_wk_qd_home() { # <user>
    dscl . -read "/Users/$1" NFSHomeDirectory 2>/dev/null | awk '{print $2}'
}

wk_quiet_dnd_state() { # <home> -- on, off, or `?<reason>`, because a bare `?` refuses a run without saying which of "no such file", "not allowed to read it" and "not the JSON this writes" it met, and they have different remedies
    /usr/bin/python3 - "$(wk_quiet_dnd_path "$1")" <<'PY' 2>/dev/null || printf '?nopython'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except FileNotFoundError:
    print("?nofile"); raise SystemExit(0)
except PermissionError:
    print("?denied"); raise SystemExit(0)
except OSError as exc:
    print("?errno%s" % (exc.errno or 0)); raise SystemExit(0)
except ValueError:
    print("?malformed"); raise SystemExit(0)
records = [r for entry in doc.get("data") or []
           for r in entry.get("storeAssertionRecords") or []]
# An end timestamp is an assertion that lapses, off by the time a run reaches it.
print("on" if any("assertionEndDateTimestamp" not in r
                  for r in records) else "off")
PY
}

wk_quiet_dnd_on() { # <home> -- turn it on indefinitely; prints the state it reads back
    /usr/bin/python3 - "$(wk_quiet_dnd_path "$1")" "$WK_DND_MODE" "$WK_DND_CLIENT" <<'PY' 2>/dev/null
import json, os, sys, time, uuid
path, mode, client = sys.argv[1], sys.argv[2], sys.argv[3]
now = time.time() - 978307200.0   # CFAbsoluteTime
doc = {"data": [{"storeAssertionRecords": [{
    "assertionUUID": str(uuid.uuid4()).upper(),
    "assertionSource": {"assertionClientIdentifier": client},
    "assertionStartDateTimestamp": now,
    "assertionDetails": {"assertionDetailsIdentifier": client,
                         "assertionDetailsModeIdentifier": mode,
                         "assertionDetailsReason": "user-action"}}]}],
       "header": {"version": 8, "timestamp": now}}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as handle:
    json.dump(doc, handle)
PY
    wk_quiet_dnd_state "$1"
}

# Run as root at a first boot, an unqualified `defaults write` lands in root's own domain and the account that gets measured never sees it.
_wk_qd_defaults() { # <user> <args...>
    local u="$1"; shift
    if [ "$u" = "$(id -un)" ]; then
        defaults "$@"
    else
        sudo -u "$u" defaults "$@"
    fi
}

_wk_qd_want() { # <type> <value>
    case "$1:$2" in
        bool:true)  printf 1 ;;
        bool:false) printf 0 ;;
        *)          printf '%s' "$2" ;;
    esac
}

_wk_qd_pmset() { # <key, spelt as `pmset -g` -- what governs the machine now -- prints it>
    pmset -g 2>/dev/null | awk -v k="$1" '$1 == k { print $2; exit }'
}

_wk_qd_procstate() { # <process>
    local pid st
    pid=$(pgrep -x "$1" 2>/dev/null | head -1) || pid=""
    [ -n "$pid" ] || { printf absent; return 0; }
    st=$(ps -o state= -p "$pid" 2>/dev/null | tr -d ' ')
    case "$st" in T*) printf stopped ;; *) printf running ;; esac
}

# A daemon wk_quiet_daemons_pause holds stopped answers no XPC request ever, and macOS ships no timeout(1): every reading below that asks one is asked through here, or the leg hangs at that reading for as long as the machine stays up.
_WK_QD_TIMEOUT='!timeout'   # not a value any of those readings can answer
_WK_QD_READ_SECS=20         # a healthy `mdutil -s /` answers in under a second

_wk_qd_read() { # [-e: what it says on stderr is part of the reading] <seconds> <command...> -- its stdout, or $_WK_QD_TIMEOUT
    local err=drop out pid killer rc=0 secs
    [ "$1" = -e ] && { err=keep; shift; }
    secs="$1"
    shift
    out=$(mktemp "${TMPDIR:-/tmp}/wk-qd.XXXXXX" 2>/dev/null) || return 0   # nowhere to write is a reading that did not answer, which is not a bound that expired
    if [ "$err" = keep ]; then "$@" >"$out" 2>&1 & else "$@" >"$out" 2>/dev/null & fi   # a file and not a pipe, either way: a grandchild the kill below cannot reach holds a pipe open for as long as it hangs
    pid=$!
    ( t=0
      while [ "$t" -lt "$secs" ]; do
          sleep 1
          kill -0 "$pid" 2>/dev/null || exit 0
          t=$((t + 1))
      done
      kill -9 "$pid" 2>/dev/null || true ) >/dev/null 2>&1 &
    killer=$!
    wait "$pid" || rc=$?
    kill "$killer" 2>/dev/null || true
    wait "$killer" 2>/dev/null || true
    if [ "$rc" -eq 137 ]; then printf '%s' "$_WK_QD_TIMEOUT"; else cat "$out"; fi   # 128 + SIGKILL
    rm -f "$out"
}

wk_quiet_desktop_user() { # [user] -- 0 when every setting above took
    local u="${1:-$(id -un)}" bad=0 name domain key type value why host uid label
    uid=$(_wk_qd_uid "$u") || uid=""

    local want got moved=""
    while read -r name domain key type value why; do
        [ -n "$domain" ] || continue
        host=""
        case "$domain" in @*) host=-currentHost; domain="${domain#@}" ;; esac
        want=$(_wk_qd_want "$type" "$value")
        # shellcheck disable=SC2086 -- $host is one flag or nothing.
        got=$(_wk_qd_defaults "$u" $host read "$domain" "$key" 2>/dev/null) || got=""
        [ "$got" = "$want" ] && continue
        # shellcheck disable=SC2086
        _wk_qd_defaults "$u" $host write "$domain" "$key" "-$type" "$value" \
            || { echo "wk: $u could not be given $domain $key" >&2; bad=1; continue; }
        moved=1
    done <<ROWS
$(wk_quiet_desktop_rows)
ROWS

    [ -n "$uid" ] || { echo "wk: no such account '$u'" >&2; return 1; }

    local home dnd; home=$(_wk_qd_home "$u")
    dnd=$(wk_quiet_dnd_state "$home")
    if [ -z "$home" ] || [ ! -d "$home" ]; then
        echo "wk: no home directory for '$u', so Do Not Disturb cannot be set" >&2; bad=1
    elif [ "$dnd" = on ]; then
        :
    elif [ "$dnd" = '?denied' ]; then
        echo "wk: $u may not read its own Do Not Disturb record, so this side can" >&2
        echo "    neither set nor judge it -- the machine that has the volume mounted does" >&2
    elif [ "$(wk_quiet_dnd_on "$home")" != on ]; then
        echo "wk: could not turn Do Not Disturb on for $u" >&2; bad=1
    fi

    [ -z "$moved" ] || killall -u "$u" Finder Dock >/dev/null 2>&1 || true
    return "$bad"
}

# Automatic update *checking* is not here: measured on Tahoe 26.4, softwareupdated erases AutomaticCheckEnabled from this domain within seconds and `softwareupdate --schedule off` exits 0 having changed nothing, so the only thing that stops a scan is not reaching Apple.
wk_quiet_desktop_system() {
    local bad=0 key name value shown
    [ "$(id -u)" -eq 0 ] || { echo "wk: wk_quiet_desktop_system needs root" >&2; return 1; }

    mdutil -i off -a >/dev/null 2>&1 || { echo "wk: spotlight is still indexing" >&2; bad=1; }
    defaults write "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory" \
        AutoSubmit -bool false 2>/dev/null \
        || { echo "wk: could not turn off diagnostic submission" >&2; bad=1; }
    tmutil disablelocal >/dev/null 2>&1 || true

    while read -r name key value shown; do
        [ -n "$key" ] || continue
        pmset -a "$key" "$value" >/dev/null 2>&1 && continue
        if [ -z "$(_wk_qd_pmset "$shown")" ]; then
            echo "wk: this Mac has no '$key' setting" >&2
        else
            echo "wk: could not set $key=$value" >&2; bad=1
        fi
    done <<ROWS
$(wk_quiet_desktop_power)
ROWS

    for key in AutomaticDownload AutomaticallyInstallMacOSUpdates \
               CriticalUpdateInstall ConfigDataInstall; do
        defaults write /Library/Preferences/com.apple.SoftwareUpdate "$key" -bool false \
            || { echo "wk: could not turn off SoftwareUpdate $key" >&2; bad=1; }
    done
    return "$bad"
}

# A signal, not `launchctl disable`: SIP refuses to unload a system daemon, and nothing a signal does survives a reboot.
_wk_qd_daemons_signal() { # <STOP|CONT>
    local sig="$1" bad=0 name proc why pids listing
    [ "$(id -u)" -eq 0 ] || { echo "wk: signalling system daemons needs root" >&2; return 1; }
    listing=$(ps -Ao pid=,comm=)   # taken once, before anything is signalled: asking a machine again after stopping part of it is how a stopper stops itself
    while read -r name proc why; do
        [ -n "$proc" ] || continue
        _wk_qd_unstoppable "$proc" && continue
        pids=$(printf '%s\n' "$listing" | awk -v p="$proc" \
            '{ pid = $1; $1 = ""; sub(/^ +/, ""); sub(/.*\//, ""); if ($0 == p) print pid }')
        [ -n "$pids" ] || continue
        # shellcheck disable=SC2086 -- one signal for every pid of that name.
        kill -"$sig" $pids 2>/dev/null \
            || { echo "wk: could not send $sig to $proc" >&2; bad=1; }
    done <<ROWS
$(wk_quiet_desktop_stopped)
ROWS
    return "$bad"
}

wk_quiet_daemons_pause()  { _wk_qd_daemons_signal STOP; }
wk_quiet_daemons_resume() { _wk_qd_daemons_signal CONT; }

wk_quiet_desktop_probe() { # [user] -- `<name>=<value>`; `?` is "no such key", which is not off
    local u="${1:-$(id -un)}" name domain key type value why host uid label shown proc md an

    while read -r name domain key type value why; do
        [ -n "$domain" ] || continue
        host=""
        case "$domain" in @*) host=-currentHost; domain="${domain#@}" ;; esac
        # shellcheck disable=SC2086 -- $host is one flag or nothing.
        printf '%s=%s\n' "$name" \
            "$(_wk_qd_defaults "$u" $host read "$domain" "$key" 2>/dev/null || echo '?')"
    done <<ROWS
$(wk_quiet_desktop_rows)
ROWS

    uid=$(_wk_qd_uid "$u") || uid=""
    # By the process, not by launchd's disabled list: disabling the wrong label is recorded there as cheerfully as the right one, and the agent goes on running.
    while read -r name proc why; do
        [ -n "$proc" ] || continue
        printf '%s=%s\n' "$name" "$(_wk_qd_procstate "$proc")"
    done <<ROWS
$(wk_quiet_desktop_stopped)
ROWS

    while read -r name key value shown; do
        [ -n "$key" ] || continue
        printf '%s=%s\n' "$name" "$(_wk_qd_pmset "$shown")"
    done <<ROWS
$(wk_quiet_desktop_power)
ROWS

    local dnd; dnd=$(wk_quiet_dnd_state "$(_wk_qd_home "$u")")
    case "$dnd" in on|off) printf 'notifications_dnd=%s\n' "$dnd" ;; esac   # left out rather than answered with a reason nothing can judge: ~/Library/DoNotDisturb is TCC-protected and root does not bypass it (`Operation not permitted` as bench and under sudo alike, on the bench install 2026-09-09), so nothing on this install can read the record, and _wk_qf_judge reports a key the probe did not answer as unknown -- which is what it is. The side that can read it is a machine with the volume merely mounted, where the plant sets it and reads it back

    while read -r name proc why; do
        [ -n "$proc" ] || continue
        printf '%s=%s\n' "$name" "$(_wk_qd_procstate "$proc")"
    done <<ROWS
$(wk_quiet_desktop_expected)
ROWS

    md=$(_wk_qd_read "$_WK_QD_READ_SECS" mdutil -s /)
    case "$md" in "$_WK_QD_TIMEOUT") ;; *) md=$(printf '%s\n' "$md" | sed -n '2s/^[[:space:]]*//p') ;; esac
    printf 'spotlight=%s\n' "$md"
    an=$(_wk_qd_read "$_WK_QD_READ_SECS" sudo -n defaults read \
        "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory" AutoSubmit)
    printf 'analytics=%s\n' "${an:-?}"   # 0600 root: an unprivileged read answers "does not exist" whatever it holds
    printf 'power_source=%s\n' "$(pmset -g batt 2>/dev/null | sed -n "s/.*'\\(.*\\)'.*/\\1/p" | head -1)"
    printf 'cpu_speed_limit=%s\n' \
        "$(pmset -g therm 2>/dev/null | sed -n 's/.*CPU_Speed_Limit *= *//p' | head -1)"
}

# A key the probe never printed is a machine whose copy of this file is older, so unknown; `?` is the probe asking and finding no such key, so a fault.
_wk_qf() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }
_wk_qf_read() { printf '%s\n' "$1" | sed -n "s|^$2=||p" | tail -1; }
_wk_qf_has()  { printf '%s\n' "$1" | grep -q "^$2="; }

_wk_qf_judge() { # <probe> <key> <want> <what> <remedy>
    local got
    if ! _wk_qf_has "$1" "$2"; then
        _wk_qf note "unknown whether $4: this machine's probe did not answer '$2'" "$5"
        return 0
    fi
    got=$(_wk_qf_read "$1" "$2")
    if [ "$got" = "$_WK_QD_TIMEOUT" ]; then
        _wk_qf note "unknown whether $4: reading '$2' did not answer inside its bound, which is a daemon held stopped answering no XPC request" "$5"
    elif [ "$got" = "$3" ]; then
        _wk_qf ok "$4"
    else
        _wk_qf wrong "not so: $4 ($2 reads '$got', wanted '$3')" "$5"
    fi
}

wk_quiet_desktop_findings() { # <probe output> [remedy]
    local probe="$1" fix="${2:-}" name domain key type value plist proc why

    while read -r name domain key type value why; do
        [ -n "$domain" ] || continue
        if _wk_qd_unsettable "$name" \
           && [ "$(_wk_qf_read "$probe" "$name")" != "$(_wk_qd_want "$type" "$value")" ]; then
            _wk_qf note "$why is what a measured Mac would be, and macOS lets no script set it ($name reads '$(_wk_qf_read "$probe" "$name")')" ""
            continue
        fi
        _wk_qf_judge "$probe" "$name" "$(_wk_qd_want "$type" "$value")" \
            "$why" "$fix"
    done <<ROWS
$(wk_quiet_desktop_rows)
ROWS


    case "$(_wk_qf_read "$probe" spotlight)" in
        *disabled*) _wk_qf ok "Spotlight is not indexing" ;;
        "$_WK_QD_TIMEOUT") _wk_qf note "Spotlight did not answer inside its bound: a Spotlight daemon held stopped answers no XPC request, so whether it indexes under a run is unknown" "$fix" ;;
        "")         _wk_qf note "Spotlight did not answer, so whether it indexes under a run is unknown" "$fix" ;;
        *)          _wk_qf wrong "Spotlight is indexing ($(_wk_qf_read "$probe" spotlight)) -- it reads the disk the run writes" "$fix" ;;
    esac
    _wk_qf_judge "$probe" analytics 0 "diagnostics are not submitted" "$fix"
    _wk_qf_judge "$probe" notifications_dnd on \
        "Do Not Disturb is on, so no banner is drawn over the browser" "$fix"
}

# Apple silicon has no frequency pin: `enable_skstb` binds a thread to a core on a development kernel and no shipping Mac runs one. So every lever that would take the clock down is held, and whether the machine took it down anyway is measured.
wk_quiet_cpu_findings() { # <probe output> [remedy]
    local probe="$1" fix="${2:-}" name key value shown v

    case "$(_wk_qf_read "$probe" power_source)" in
        "AC Power")      _wk_qf ok "on AC" ;;
        "Battery Power") _wk_qf wrong "on battery, and a Mac on battery is not the same Mac" "plug it in" ;;
        "")              _wk_qf note "this machine did not say what it is powered by" "$fix" ;;
        *)               _wk_qf note "powered by '$(_wk_qf_read "$probe" power_source)'" ;;
    esac

    while read -r name key value shown; do
        [ -n "$key" ] || continue
        if [ -z "$(_wk_qf_read "$probe" "$name")" ]; then
            _wk_qf note "this Mac does not report '$key', so nothing here can confirm that lever" ""
        else
            _wk_qf_judge "$probe" "$name" "$value" "pmset $key is $value" "$fix"
        fi
    done <<ROWS
$(wk_quiet_desktop_power)
ROWS

    v=$(_wk_qf_read "$probe" cpu_speed_limit)
    case "$v" in
        100|"")  _wk_qf ok "no thermal limit on the clock" ;;
        *)       _wk_qf wrong "the clock is held to $v% right now (CPU_Speed_Limit), so this is a throttled machine" \
                          "let it cool, then measure again" ;;
    esac
}

wk_quiet_daemons_findings() { # <probe output> [remedy]
    local probe="$1" fix="${2:-}" name proc why state
    while read -r name proc why; do
        [ -n "$proc" ] || continue
        state=$(_wk_qf_read "$probe" "$name")
        case "$state" in
            running) _wk_qf ok "$proc is running, as it must be: it $why" ;;
            stopped) _wk_qf wrong "$proc is STOPPED, and it $why" \
                              "nothing here stops it; find what did" ;;
            absent)  _wk_qf note "$proc is not running here, and it $why" "$fix" ;;
            *)       _wk_qf note "$proc was not answered by this machine's probe" "$fix" ;;
        esac
    done <<ROWS
$(wk_quiet_desktop_expected)
ROWS
    while read -r name proc why; do
        [ -n "$proc" ] || continue
        state=$(_wk_qf_read "$probe" "$name")
        case "$state" in
            stopped|absent) _wk_qf ok "$proc is $state" ;;
            "")             _wk_qf note "$proc was not answered by this machine's probe" "$fix" ;;
            *)              if _wk_qd_unstoppable "$proc"; then
                                _wk_qf note "$proc is running and cannot be stopped (SIP refuses the signal); it $why" ""
                            else
                                _wk_qf wrong "$proc is running, and it $why" "$fix"
                            fi ;;
        esac
    done <<ROWS
$(wk_quiet_desktop_stopped)
ROWS
}
