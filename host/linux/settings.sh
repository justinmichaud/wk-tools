# Ubuntu 26.04 desktop settings, restored from a filtered dconf dump.
#
# `dconf load -f /` replaces the whole tree, which is destructive on a machine
# that has settings this repo does not know about. Loading into the specific
# subtrees that were captured is safer and still idempotent.

_dconf="$WK_ROOT/host/linux/config.dconf"

if ! have dconf; then
    warn "dconf not installed; skipping desktop settings"
elif [ ! -f "$_dconf" ]; then
    warn "no config.dconf; run 'wk backup' on a configured machine first"
elif [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    warn "no session bus; run ./setup from inside a graphical session to apply GNOME settings"
else
    # Compare against the live tree so a second run reports no change.
    _live=$(mktemp)
    dconf dump / > "$_live" 2>/dev/null || true

    if diff -q "$_live" "$_dconf" >/dev/null 2>&1; then
        unchanged "dconf settings"
    else
        # No -f: merge rather than replace, so settings outside the captured
        # subtrees survive.
        dconf load / < "$_dconf"
        changed "loaded dconf settings"
    fi
    rm -f "$_live"
fi

unset _dconf _live
