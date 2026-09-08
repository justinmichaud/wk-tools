command -v warn >/dev/null 2>&1 || . "$(dirname "${BASH_SOURCE[0]}")/common.sh"   # sourced inside a build target too, where no cmd/* has defined info/warn and `info` is a texinfo reader
# shellcheck disable=SC1090
. "$(dirname "${BASH_SOURCE[0]}")/../bench/mac-window-probe.sh"
# shellcheck disable=SC1090
. "$(dirname "${BASH_SOURCE[0]}")/../bench/mac-quiet-desktop.sh"
# shellcheck disable=SC1090
. "$(dirname "${BASH_SOURCE[0]}")/../bench/mac-raiser.sh"

# Only the clock is judged on a workstation: the rest is what a benchmark install is and a workstation never will be, and a red line nothing there can clear teaches a reader to skip the list.
macos_noise() {
    local bad=0 probe v
    probe=$(wk_quiet_desktop_probe)

    render_findings <<FINDINGS || bad=$((bad + $?))
$(wk_quiet_cpu_findings "$probe")
FINDINGS

    if in_bench_mode; then
        render_findings <<FINDINGS || bad=$((bad + $?))
$(wk_quiet_desktop_findings "$probe" "wk bench mac-volume --provision")
$(wk_quiet_daemons_findings "$probe" "wk quiesce on")
FINDINGS
    fi

    v=$(_wk_qd_read -e "$_WK_QD_READ_SECS" tmutil destinationinfo | head -1)   # -e: "No destinations configured" is what it says on stderr
    case "$v" in
        "$_WK_QD_TIMEOUT") warn "  timemachine: backupd did not answer inside its bound (this preflight holds it stopped), so whether a backup can start mid-run is unknown" ;;
        *"No destinations"*) log "  timemachine: no destination configured" ;;
        *) warn "  timemachine: a destination is configured; a backup can start mid-run"; bad=$((bad + 1)) ;;
    esac

    v=$(_wk_qd_read "$_WK_QD_READ_SECS" sudo -n defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled)   # not `softwareupdate --schedule`, which says "on" with AutomaticCheckEnabled 0 in the same plist
    case "$v" in
        "$_WK_QD_TIMEOUT") warn "  updates:    softwareupdated did not answer inside its bound (this preflight holds it stopped), so whether automatic checking is on is unknown" ;;
        0)  log  "  updates:    automatic checking off" ;;
        1)  warn "  updates:    automatic checking is on"; bad=$((bad + 1)) ;;
        *)  log  "  updates:    AutomaticCheckEnabled unset, where softwareupdated leaves it -- a scan is stopped by the endpoint denial and the paused scanner, not by this key" ;;
    esac

    if in_bench_mode; then
        local _qh
        _qh="$(dirname "${BASH_SOURCE[0]}")/../bench/mac-quiet-hosts.sh"
        if [ -r "$_qh" ]; then
            # shellcheck disable=SC1090
            . "$_qh"
            if wk_bench_hosts_present /etc/hosts; then
                log "  hosts:      update endpoints denied"
            else
                warn "  hosts:      NOT denied -- wk bench mac-volume --provision"; bad=$((bad + 1))
            fi
        fi
    fi

    MACOS_NOISE_FAULTS="$bad"
    return $bad
}

auth_panel() {  # a modal panel is invisible to screen_blocker; SecurityAgent runs only while one is up
    pgrep -x SecurityAgent >/dev/null 2>&1 && printf 'SecurityAgent'
    return 0
}

# screen_blocker is an instant and a run is an hour, so anything drawing after it was read is invisible: a consent dialog sat over a whole PGO collection that way (2026-09-06). Samples every WK_SCREEN_WATCH_SECONDS while the measured thing runs.
screen_watch_start() {   # <record file>
    local record="$1"
    : > "$record"
    ( while :; do
          local seen; seen=$(screen_blocker)
          case "$seen" in ""|"?") ;; *) printf '%s\t%s\n' "$(date -u +%H:%M:%SZ)" "$seen" >> "$record" ;; esac
          sleep "${WK_SCREEN_WATCH_SECONDS:-10}"
      done ) </dev/null >/dev/null 2>&1 &
    echo $! > "$record.pid"
}

screen_watch_stop() {   # <record file> -- prints what it saw, and succeeds only when it saw nothing
    local record="$1" pid
    if [ -f "$record.pid" ]; then
        pid=$(cat "$record.pid")
        kill "$pid" 2>/dev/null || true
        rm -f "$record.pid"
    fi
    [ -s "$record" ] || return 0
    sort -u -k2 "$record"
    return 1
}

screen_blocker() {
    local reading uninvited
    reading=$(wk_window_probe 2>/dev/null | sed -n 's/^windows=//p')
    # `?` is "the window server was not asked", which is not "nothing is there": a caller that read it as free would say the screen is clear on every machine with no compiler to build the probe with.
    [ -n "$reading" ] && [ "$reading" != '?' ] || { printf '?'; return 0; }
    uninvited=$(wk_window_unexpected "$reading") || return 0
    [ -n "$uninvited" ] || return 0
    printf '%s' "${uninvited%;}" | tr ';' '\n' | cut -d: -f1 | sort -u | paste -sd, -
    return 0
}
