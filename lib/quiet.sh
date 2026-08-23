# How quiet is this machine, measured rather than assumed.
#
# Sourced by `wk quiesce`, which does the quieting, and by `wk bench staged`,
# which refuses to believe a number taken on a machine that was busy being a
# desktop. One implementation, because two answers to "is it quiet" is how a
# benchmark ends up with a footnote nobody can check.
#
# --- the noise, measured rather than assumed ----------------------------------
#
# `quiesce on` has two halves. The privileged one (admin/wk-quiesce-priv) turns
# off the persistent sources -- Spotlight indexing, automatic updates, low
# power mode -- and stops any running backup; the unprivileged one holds the
# machine awake and pauses a few analysis daemons for the length of the run.
#
# This is the third thing, and it is not a third half: it *measures* the result
# rather than trusting either. "Test the property, not the configuration"
# (docs/TESTING.md) applies to the machine's own quietness as much as to a
# firewall -- a helper that is not installed, a `mdutil` that reported success
# on a volume it does not own, an update check that came back on by itself,
# all look perfectly fine in the command that was supposed to have fixed them.
# It also covers two things the helper deliberately does not touch: a Time
# Machine *destination* (stopping a backup is not the same as there being none
# to start) and the thermal state, which no setting controls.
#
# It matters most for the benchmark volume (docs/HANDOFF-benchmarking.md): an
# install that boots for a run, indexes itself for ten minutes and then reports
# a Speedometer score is exactly the failure that is invisible in the number.
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

    # Read the setting, not `softwareupdate --schedule`.
    #
    # On macOS 26 that command reports "Automatic checking for updates is turned
    # on" with AutomaticCheckEnabled set to 0 in the very plist it describes --
    # verified on real hardware 2026-08-22, after the same disagreement in a
    # guest had been written off as a virtualisation quirk twice. It is not a
    # quirk and it is not the guest: the reader is simply wrong, the same way
    # `systemsetup -getremotelogin` needs admin and exits 0 while refusing to
    # answer (host/macos/sharing.sh).
    #
    # This matters more than a cosmetic warning. It is the check that fails, so
    # it is the check that gets `--force`d past -- and `--force` is
    # all-or-nothing, so believing this one costs every other preflight failure
    # too. A check that cannot pass on a correctly configured machine trains
    # people to ignore the whole preflight.
    # `sudo -n` first, and it is not paranoia: as an ordinary user this domain
    # answers "does not exist" for a key that root reads as 0, because cfprefsd
    # serves /Library domains differently by privilege -- and reading the plist
    # file directly does not help, plutil finds no such key in it either. Root
    # is the only reader that sees the truth. `-n` so it never prompts; a
    # machine without passwordless sudo just falls through.
    v=$(sudo -n defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null) \
        || v=$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null) \
        || v=""
    case "$v" in
        0)  log  "  updates:    automatic checking off" ;;
        1)  warn "  updates:    automatic checking is on"; bad=1 ;;
        # Unreadable is reported as unknown and does NOT fail the check. The
        # temptation is to treat it as "on" and be safe, but this is the check
        # that gets --force'd past when it cannot pass, and --force is
        # all-or-nothing -- so a check that fails on machines it cannot read
        # ends up disabling every other check too. Say what is known.
        '') log  "  updates:    unknown (needs root to read; not treated as a failure)" ;;
        *)  log  "  updates:    unknown (AutomaticCheckEnabled=$v)" ;;
    esac

    # Sleep is not only a noise source: a display that sleeps mid-run changes
    # what the compositor is doing, and on a laptop the whole machine going to
    # sleep ends the run in a way that looks like a crash.
    v=$(pmset -g 2>/dev/null | awk '$1 == "sleep" { print $2 }')
    [ "${v:-0}" = 0 ] && log "  sleep:      off" || { warn "  sleep:      $v minutes -- 'wk quiesce on' holds it awake for the run only"; bad=1; }

    v=$(pmset -g 2>/dev/null | awk '$1 == "lowpowermode" { print $2 }')
    [ "${v:-0}" = 0 ] && log "  lowpower:   off" || { warn "  lowpower:   ON -- every number from this machine is a low-power number"; bad=1; }

    # The one that invalidates a comparison rather than adding variance: a
    # machine that was thermally limited during one half of an A/B produced a
    # difference that has nothing to do with the change.
    v=$(pmset -g therm 2>/dev/null | sed -n 's/.*CPU_Speed_Limit *= *//p' | head -1)
    if [ -n "$v" ] && [ "$v" != 100 ]; then
        warn "  thermal:    CPU_Speed_Limit=$v -- the machine is being held back right now"
        bad=1
    else
        log "  thermal:    no limit recorded"
    fi

    return $bad
}

# --- is the screen usable, as opposed to merely occupied ----------------------
#
# Prints the name of an app that owns the *front window* and would ruin a run,
# or nothing. macOS puts several of these in front of a fresh install and they
# all have the same effect: a benchmark in a background window is throttled by
# the browser, so the run makes no progress and times out with no error at all
# (run-benchmark exit 124, measured 2026-08-22).
#
# Here, and not in the two places that ask, for the reason at the top of this
# file. The first version lived in cmd/bench with a list of three apps while the
# lane deciding whether to --force past it kept its own list of one -- so
# `Setup Assistant` was caught and `Software Update`, which came to the front the
# moment Setup Assistant was dismissed, was forced straight through. One list,
# two callers.
#
# ASKED AS "WHAT IS FRONTMOST", NOT "WHAT IS RUNNING"
#
# The first version pattern-matched process command lines for `<app>.app` and
# was wrong in the worst direction: `softwareupdated` and `suhelperd` live
# *inside* `Software Update.app/Contents/Resources/`, run on every healthy Mac,
# and matched. The check failed on a machine whose screen was perfectly free --
# and a check that cannot pass is worse than no check, because the first thing
# anybody does with it is force past it, which is exactly the habit this whole
# area is trying to break.
#
# `lsappinfo front` asks the window server which application is actually
# frontmost. It needs no assistive access -- unlike System Events, which answers
# `-25211` on a fresh install rather than the truth -- and it reports GUI apps
# only, so a daemon buried in an .app bundle cannot be mistaken for a window.
WK_SCREEN_BLOCKERS="${WK_SCREEN_BLOCKERS:-Setup Assistant|Software Update|Installer|Migration Assistant|System Settings}"

# --- the panel that is not an application ------------------------------------
#
# screen_blocker above asks the window server which *application* is frontmost,
# and that is the right question for the things it lists. It cannot see a modal
# authentication panel: macOS draws those from SecurityAgent, which is not a
# frontmost application, so `lsappinfo front` answers "Finder" while a password
# sheet sits on top of everything.
#
# Found the way these things are always found -- somebody looked at the screen.
# 2026-08-23, during an A/B on the benchmark install: `the screen is free` passed
# on every preflight while an authentication dialog had been up for five minutes.
# (The run survived it, and that is luck rather than reassurance: arm A scored
# 42.176 against a 42.774 baseline, so MiniBrowser was getting focus. A panel
# that *had* taken focus would have produced the silent exit-124 timeout this
# whole area exists to prevent.)
#
# SecurityAgent is a reliable signal precisely because it is not a daemon: it
# runs only while it has a panel up. The host install, idle, does not have it in
# `lsappinfo list` at all -- which is what makes this a check that can pass
# rather than one that trains people to force past it.
#
# On this install it comes from the login keychain not matching the account
# password, so autologin raises an unlock prompt: bench/mac-bench-firstboot.sh
# logs `passwd: DS error: eDSAuthFailed` where it tries to set that password.
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
