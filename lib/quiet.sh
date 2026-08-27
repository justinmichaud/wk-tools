# How quiet is this machine, measured rather than assumed. Sourced by
# `wk quiesce` and `wk bench staged`, which refuses a number taken on a
# machine that was busy. `quiesce on` turns off persistent sources and
# stops any running backup; this *measures* the result instead of trusting
# either half, and also covers what quiesce doesn't touch: a Time Machine
# destination and thermal state.
macos_noise() {
    local bad=0 v

    v=$(mdutil -s / 2>/dev/null | sed -n '2s/^[[:space:]]*//p')
    case "$v" in
        *disabled*) log "  spotlight:  $v" ;;
        '')         log "  spotlight:  unknown (mdutil said nothing)" ;;
        *)          warn "  spotlight:  $v -- 'sudo mdutil -i off /' on a benchmark install"; bad=1 ;;
    esac

    v=$(tmutil destinationinfo 2>&1 | head -1)
    case "$v" in
        *"No destinations"*) log "  timemachine: no destination configured" ;;
        *) warn "  timemachine: a destination is configured; a backup can start mid-run"; bad=1 ;;
    esac

    # Not `softwareupdate --schedule`, which reports "on" with
    # AutomaticCheckEnabled 0 in the same plist. `sudo -n` first: an
    # ordinary user reads this domain as "does not exist"; `-n` never prompts.
    v=$(sudo -n defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null) \
        || v=$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null) \
        || v=""
    case "$v" in
        0)  log  "  updates:    automatic checking off" ;;
        1)  warn "  updates:    automatic checking is on"; bad=1 ;;
        # Unreadable is unknown elsewhere, but a benchmark install has
        # passwordless root, so it means something is wrong there.
        '') if [ -f /etc/wk-image ]; then
                warn "  updates:    unreadable, on an install that has passwordless root."
                warn "              Not fatal -- see the scanner note below -- but not reassuring."
            else
                log  "  updates:    unknown (needs root to read; not treated as a failure)"
            fi ;;
        *)  log  "  updates:    unknown (AutomaticCheckEnabled=$v)" ;;
    esac

    # AutomaticCheckEnabled=0 still lets a background scan advance -- a
    # network fetch and CPU burst no preference read would catch. Checked
    # on a benchmark install only; warned not failed, since SIP permission
    # for `launchctl bootout` can't be checked from host mode (the hard
    # check is bench/mac-bench-autorun.sh sampling scan timestamps per arm).
    if [ -f /etc/wk-image ]; then
        if sudo -n launchctl print system/com.apple.softwareupdated >/dev/null 2>&1; then
            warn "  scanner:    softwareupdated is LOADED -- a scan can start mid-run."
            warn "              Per-arm scan evidence is recorded; check the summary."
        else
            log "  scanner:    softwareupdated not loaded"
        fi
    fi

    # A banner takes focus, a lock is "nowhere to draw" -- both invisible
    # to `screen_blocker`'s frontmost-*application* check. Benchmark
    # install only; warned not failed, per the scanner note above.
    if [ -f /etc/wk-image ]; then
        if pgrep -x NotificationCenter >/dev/null 2>&1; then
            warn "  notifs:     NotificationCenter is RUNNING -- a banner can draw mid-run"
        else
            log "  notifs:     NotificationCenter not running"
        fi
        v=$(defaults -currentHost read com.apple.screensaver idleTime 2>/dev/null || echo "")
        case "$v" in
            0)  log  "  screensaver: disabled (idleTime=0)" ;;
            '') warn "  screensaver: unreadable -- cannot say whether the screen can lock" ;;
            *)  warn "  screensaver: idleTime=${v}s -- a benchmark makes no input, so this
              timer runs at full load and the lock behind it ends the run" ;;
        esac
    fi

    v=$(pmset -g 2>/dev/null | awk '$1 == "sleep" { print $2 }')  # a laptop sleep ends the run looking like a crash
    [ "${v:-0}" = 0 ] && log "  sleep:      off" || { warn "  sleep:      $v minutes -- 'wk quiesce on' holds it awake for the run only"; bad=1; }

    v=$(pmset -g 2>/dev/null | awk '$1 == "lowpowermode" { print $2 }')
    [ "${v:-0}" = 0 ] && log "  lowpower:   off" || { warn "  lowpower:   ON -- every number from this machine is a low-power number"; bad=1; }

    # Invalidates a comparison, not just adds variance: throttling during
    # one half of an A/B produces a difference unrelated to the change.
    v=$(pmset -g therm 2>/dev/null | sed -n 's/.*CPU_Speed_Limit *= *//p' | head -1)
    if [ -n "$v" ] && [ "$v" != 100 ]; then
        warn "  thermal:    CPU_Speed_Limit=$v -- the machine is being held back right now"
        bad=1
    else
        log "  thermal:    no limit recorded"
    fi

    return $bad
}

# --- is the screen usable, as opposed to merely occupied --------------------
# Prints the name of an app owning the *front window*, or nothing: a
# benchmark in a background window is throttled and times out silently
# (run-benchmark exit 124). `lsappinfo front` asks the window server
# directly rather than grepping process command lines (which matches
# daemons like softwareupdated living *inside* an .app bundle); needs no
# assistive access, unlike System Events, which lies on a fresh install.
WK_SCREEN_BLOCKERS="${WK_SCREEN_BLOCKERS:-Setup Assistant|Software Update|Installer|Migration Assistant|System Settings}"

# --- the panel that is not an application ------------------------------------
# screen_blocker can't see a modal auth panel: macOS draws those from
# SecurityAgent, not a frontmost application. SecurityAgent runs only
# while a panel is up, so it's a reliable signal.
auth_panel() {
    pgrep -x SecurityAgent >/dev/null 2>&1 && printf 'SecurityAgent'
    return 0
}

screen_blocker() {
    command -v lsappinfo >/dev/null 2>&1 || return 0
    local asn name
    asn=$(lsappinfo front 2>/dev/null) || return 0
    [ -n "$asn" ] || return 0
    name=$(lsappinfo info -only name "$asn" 2>/dev/null \
             | sed -n 's/.*"LSDisplayName"="\([^"]*\)".*/\1/p')
    [ -n "$name" ] || return 0
    case "|$WK_SCREEN_BLOCKERS|" in
        *"|$name|"*) printf '%s' "$name" ;;
    esac
    return 0
}
