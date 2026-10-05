# The Mac's credential injector under launchd: the guests' and, through the podman machine's, the containers'.
. "$WK_ROOT/host/units.sh"

_label=com.wk.inject
_plist="$HOME/Library/LaunchAgents/$_label.plist"
_log="$(wk_state_dir)/inject.log"
ensure_dir "$HOME/Library/LaunchAgents"
ensure_dir "$(wk_state_dir)"

case "$(wk_py wk.guest inject-retire)" in
    stopped) changed "stopped the injector 'wk start' spawned; launchd keeps it now" ;;
esac
case "$(wk_py wk.secrets claude-login-migrate)" in
    moved) changed "moved the claude.ai login out of agent-rw, beside the keyring" ;;
    failed) die "could not move the claude.ai login out of agent-rw (above)" ;;
esac

_plist_new=$(mktemp)
wk_py wk.guest inject-plist "$_label" "$_log" "/opt/podman/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" > "$_plist_new" \
    || die "could not write the injector's LaunchAgent (above)"

_stamp="$(wk_state_dir)/.inject-program"
_program=$(program_stamp container/proxy/github-inject.py)
_svc="gui/$(id -u)/$_label"
if cmp -s "$_plist_new" "$_plist" && [ "$(cat "$_stamp" 2>/dev/null)" = "$_program" ] && launchctl print "$_svc" >/dev/null 2>&1; then
    unchanged "$_label running"
elif [ -n "${WK_DRY_RUN:-}" ]; then
    changed "would install $_plist and restart $_label"
else
    install -m 0644 "$_plist_new" "$_plist"
    if wk_py wk.guest inject-restart "$_label" "$_plist"; then
        printf '%s\n' "$_program" > "$_stamp"
        changed "started the credential injector ($_label)"
    else
        warn "could not start $_label -- a guest gets no credential and no container a claude.ai login.
    launchctl print $_svc says why; the log is $_log"
    fi
fi
rm -f "$_plist_new"

unset _label _plist _log _plist_new _stamp _program _svc
