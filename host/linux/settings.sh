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
    # Comparing the whole live tree against the captured file can never match:
    # the dump holds only the subtrees `wk backup` captures, while the live tree
    # holds everything this desktop has ever set. That made the stage report a
    # change on every single run, which is exactly the signal the "second run
    # reports no changes" rule exists to give.
    #
    # So apply it and then ask whether anything actually moved. `dconf load`
    # without -f merges, so this is safe to run when nothing changes.
    _before=$(mktemp)
    _after=$(mktemp)
    dconf dump / > "$_before" 2>/dev/null || true

    dconf load / < "$_dconf"

    dconf dump / > "$_after" 2>/dev/null || true
    if cmp -s "$_before" "$_after"; then
        unchanged "dconf settings"
    else
        changed "loaded dconf settings"
    fi
    rm -f "$_before" "$_after"
fi

unset _dconf _before _after
