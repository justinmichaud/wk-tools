# `defaults write` rewrites the plist and bumps its mtime, so read before write.

_defaults_conf="$WK_ROOT/host/macos/defaults.conf"
_hotkeys_plist="$WK_ROOT/host/macos/symbolichotkeys.plist"

_current_value() {
    defaults read "$1" "$2" 2>/dev/null || true
}

# `defaults read` reports a bool as 1/0.
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
    # The Dock reads its preferences once, at launch.
    [ "$domain" = com.apple.dock ] && _dock_changed=1
    return 0
}

_dock_changed=""
if [ -f "$_defaults_conf" ]; then
    while read -r domain key type value; do
        case "$domain" in ''|'#'*) continue ;; esac
        apply_default "$domain" "$key" "$type" "$value"
    done < "$_defaults_conf"
fi

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

if [ "$WK_CHANGES" -gt 0 ]; then
    if [ -n "${_hotkeys_changed:-}" ]; then
        /System/Library/PrivateFrameworks/SystemAdministration.framework/Resources/activateSettings -u \
            >/dev/null 2>&1 || warn "could not reload shortcuts; log out to apply"
    fi
    if [ -n "$_dock_changed" ]; then
        killall Dock 2>/dev/null && changed "restarted the Dock" \
            || warn "could not restart the Dock; log out to apply its settings"
    fi
    log "note: some settings apply only to newly launched apps"
fi

unset _defaults_conf _hotkeys_plist _tmp _hotkeys_changed _dock_changed
