command -v warn >/dev/null 2>&1 || . "$(dirname "${BASH_SOURCE[0]}")/common.sh"   # sourced inside a build target too, where no cmd/* has defined info/warn and `info` is a texinfo reader
# shellcheck disable=SC1090
. "$(dirname "${BASH_SOURCE[0]}")/../bench/mac-window-probe.sh"
# shellcheck disable=SC1090
. "$(dirname "${BASH_SOURCE[0]}")/../bench/mac-quiet-desktop.sh"
# shellcheck disable=SC1090
. "$(dirname "${BASH_SOURCE[0]}")/../bench/mac-raiser.sh"

# Every reading a preflight takes is an instant and a run is an hour, so anything that starts after it was read is invisible: a consent dialog sat over a whole PGO collection that way (2026-09-06), and macOS restarts a paused agent on demand, so the banner-drawing pair -- NotificationCenter and usernoted -- can come back inside a leg that began with both held stopped. Samples every WK_SCREEN_WATCH_SECONDS while the measured thing runs, with one `ps` for all forty-odd rather than a `pgrep` each: forty forks every ten seconds beside the thing being measured is a load of its own.
_watch_restarted() {   # the must-not-run processes that are running again, comma-separated
    is_macos || return 0
    local listing want
    listing=$(ps -Ao stat=,comm= 2>/dev/null) || return 0
    want=$(wk_quiet_desktop_stopped | awk '{print $2}' | grep -vxF -f <(wk_quiet_desktop_unstoppable))
    # awk's `-v` reads an embedded newline in `want` as a parse error, not data; python3 takes both lists through the environment instead
    WK_QUIET_LISTING="$listing" WK_QUIET_WANT="$want" python3 -c '
import os

want = {w for w in os.environ.get("WK_QUIET_WANT", "").split("\n") if w}
seen = set()
for line in os.environ.get("WK_QUIET_LISTING", "").split("\n"):
    if not line:
        continue
    state, _, rest = line.partition(" ")
    comm = rest.strip().rsplit("/", 1)[-1]
    if comm in want and not state.startswith("T"):
        seen.add(comm)
print(",".join(sorted(seen)), end="")
'
}

screen_watch_start() {   # <record file>
    local record="$1"
    : > "$record"
    ( while :; do
          local seen; seen=$(screen_blocker)
          case "$seen" in ""|"?") ;; *) printf '%s\t%s\n' "$(date -u +%H:%M:%SZ)" "$seen" >> "$record" ;; esac
          local back; back=$(_watch_restarted)
          [ -z "$back" ] || printf '%s\trunning again: %s\n' "$(date -u +%H:%M:%SZ)" "$back" >> "$record"
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
