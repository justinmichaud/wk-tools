# `dconf load` without -f merges; -f would replace the whole tree.

_dconf="$WK_ROOT/host/linux/config.dconf"

if ! have dconf; then
    warn "dconf not installed; skipping desktop settings"
elif [ ! -f "$_dconf" ]; then
    warn "no config.dconf; run 'wk backup' on a configured machine first"
elif [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    warn "no session bus; run ./setup from inside a graphical session to apply GNOME settings"
else
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
