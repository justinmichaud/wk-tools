# An unfocused MiniBrowser is rAF-throttled into a stalled, silent exit-124, so every macOS run that draws needs App Nap off and the window pulled to the front. Sourced by `wk quiesce on` and by build/mac-pgo.sh's collection; sources nothing itself, and the caller provides info/warn.
MB_BUNDLE=org.webkit.MiniBrowser

mac_raiser_on() {  # <state dir>
    local state="$1"
    mkdir -p "$state"
    if [ ! -f "$state/caffeinate.pid" ]; then
        # All three descriptors detached, or the backgrounded process holds an ssh session open and the caller never returns.
        caffeinate -dimsu </dev/null >/dev/null 2>&1 &
        echo $! > "$state/caffeinate.pid"
        info "caffeinate started"
    fi
    defaults write "$MB_BUNDLE" NSAppSleepDisabled -bool YES 2>/dev/null \
        && { touch "$state/appnap_disabled"; info "App Nap disabled for MiniBrowser"; } \
        || warn "could not disable App Nap for MiniBrowser -- rAF may be throttled mid-run"

    if [ -f "$state/raiser.pid" ] && kill -0 "$(cat "$state/raiser.pid")" 2>/dev/null; then
        info "raiser already running"
        return 0
    fi
    if ! /usr/bin/python3 -c 'import AppKit' >/dev/null 2>&1; then
        warn "no python3 AppKit here, so no raiser: MiniBrowser can be backgrounded and
    its rAF throttled. /usr/bin/python3 with pyobjc is what a benchmark install needs
    anyway -- see docs/HANDOFF-mac-perf-mode.md."
        return 0
    fi
    cat > "$state/raiser.py" <<'RAISEREOF'
#!/usr/bin/python3
# By bundle id, so each round's relaunched instance is picked up; activateIgnoringOtherApps reaches it from a fullscreen terminal's Space.
import sys, time
from AppKit import (NSRunningApplication, NSWorkspace,
                    NSApplicationActivateIgnoringOtherApps)
BID = "org.webkit.MiniBrowser"
INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
while True:
    f = NSWorkspace.sharedWorkspace().frontmostApplication()
    if f is None or f.bundleIdentifier() != BID:
        for a in NSRunningApplication.runningApplicationsWithBundleIdentifier_(BID):
            a.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    time.sleep(INTERVAL)
RAISEREOF
    /usr/bin/python3 "$state/raiser.py" 1 </dev/null >"$state/raiser.log" 2>&1 &
    echo $! > "$state/raiser.pid"
    info "raiser started -- MiniBrowser is kept frontmost during runs"
}

mac_raiser_off() {  # <state dir>
    local state="$1" pid
    if [ -f "$state/caffeinate.pid" ]; then
        kill "$(cat "$state/caffeinate.pid")" 2>/dev/null || true
        rm -f "$state/caffeinate.pid"
        info "caffeinate stopped"
    fi
    if [ -f "$state/raiser.pid" ]; then
        pid=$(cat "$state/raiser.pid")
        kill "$pid" 2>/dev/null && info "raiser stopped (pid $pid)" || info "raiser was not running"
        rm -f "$state/raiser.pid"
    fi
    if [ -f "$state/appnap_disabled" ]; then
        defaults delete "$MB_BUNDLE" NSAppSleepDisabled >/dev/null 2>&1 \
            && info "App Nap restored for MiniBrowser" \
            || warn "could not restore App Nap for MiniBrowser"
        rm -f "$state/appnap_disabled"
    fi
}
