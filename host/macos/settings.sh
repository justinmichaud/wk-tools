# macOS desktop settings. Applied from data files so that `wk backup` can
# regenerate them without touching code.
#
# Every write is guarded by a read, so a second ./setup reports no changes.
# `defaults write` always rewrites the plist and bumps its mtime, which would
# otherwise make the idempotency check meaningless.

_defaults_conf="$WK_ROOT/host/macos/defaults.conf"
_hotkeys_plist="$WK_ROOT/host/macos/symbolichotkeys.plist"

_current_value() {
    defaults read "$1" "$2" 2>/dev/null || true
}

# Normalise so that a stored `true` compares equal to the `1` defaults reports.
_normalise_bool() {
    case "$1" in
        true|yes|1) echo 1 ;;
        false|no|0) echo 0 ;;
        *) echo "$1" ;;
    esac
}

apply_default() {
    local domain="$1" key="$2" type="$3" value="$4"
    local want current

    case "$type" in
        bool)  want=$(_normalise_bool "$value") ;;
        *)     want="$value" ;;
    esac

    current=$(_current_value "$domain" "$key")
    [ "$type" = bool ] && current=$(_normalise_bool "$current")

    if [ "$current" = "$want" ]; then
        unchanged "default $domain $key"
        return 0
    fi

    case "$type" in
        string) defaults write "$domain" "$key" -string "$value" ;;
        bool)   defaults write "$domain" "$key" -bool "$value" ;;
        int)    defaults write "$domain" "$key" -int "$value" ;;
        float)  defaults write "$domain" "$key" -float "$value" ;;
        *)      die "defaults.conf: unknown type '$type' for $domain $key" ;;
    esac
    changed "default $domain $key = $value (was ${current:-unset})"
}

if [ -f "$_defaults_conf" ]; then
    while read -r domain key type value; do
        case "$domain" in ''|'#'*) continue ;; esac
        apply_default "$domain" "$key" "$type" "$value"
    done < "$_defaults_conf"
fi

# Keyboard shortcuts: compare the whole domain against the stored copy and
# import only on difference. 16 shortcuts are disabled here (Spaces and Mission
# Control switching, input-source cycling) so they stay free for other uses.
if [ -f "$_hotkeys_plist" ]; then
    _tmp="$(mktemp -t wk-hotkeys).plist"
    defaults export com.apple.symbolichotkeys "$_tmp" 2>/dev/null || true
    plutil -convert xml1 "$_tmp" >/dev/null 2>&1 || true

    if [ -f "$_tmp" ] && cmp -s "$_tmp" "$_hotkeys_plist"; then
        unchanged "keyboard shortcuts"
    else
        defaults import com.apple.symbolichotkeys "$_hotkeys_plist"
        changed "keyboard shortcuts imported"
        _hotkeys_changed=1
    fi
    rm -f "$_tmp"
fi

# Only nudge the UI when something actually moved; otherwise a no-op setup run
# would still visibly flicker the Dock and menu bar.
if [ "$WK_CHANGES" -gt 0 ]; then
    if [ -n "${_hotkeys_changed:-}" ]; then
        # Reload the shortcut table without a logout.
        /System/Library/PrivateFrameworks/SystemAdministration.framework/Resources/activateSettings -u \
            >/dev/null 2>&1 || warn "could not reload shortcuts; log out to apply"
    fi
    log "note: some settings apply only to newly launched apps"
fi

unset _defaults_conf _hotkeys_plist _tmp _hotkeys_changed
