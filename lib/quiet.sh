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

    v=$(softwareupdate --schedule 2>/dev/null | head -1)
    case "$v" in
        *"turned off"*) log "  updates:    automatic checking off" ;;
        '')             log "  updates:    unknown" ;;
        *)              warn "  updates:    $v"; bad=1 ;;
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
