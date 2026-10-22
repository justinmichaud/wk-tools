
wk_eval wk.store paths
. "$WK_ROOT/host/units.sh"

SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"

_before=$(git -C "$SDK" rev-parse HEAD 2>/dev/null || true)
bash "$WK_ROOT/container/sdk-refresh.sh" "$SDK" "$(wk_py wk.project SDK_REPO)" "$(wk_py wk.project SDK_PIN)" \
    || die "pinning the webkit-container-sdk checkout failed (above)"
_after=$(git -C "$SDK" rev-parse HEAD)
if [ "$_before" = "$_after" ]; then
    unchanged "webkit-container-sdk at the pin"
else
    changed "webkit-container-sdk at $_after"
fi

_unit_journal=""

unit_start wk-proxy.service "$WK_ROOT" "$WK_STORE" \
    "workspaces will have no egress" "$_unit_journal" sh -c

unit_start wk-push.service "$WK_ROOT" "$WK_STORE" \
    "no workspace here can push" "$_unit_journal" sh -c

WK_UNIT_CLAUDE_LOGIN="$keyring_claude_login" unit_start wk-github-inject.service "$WK_ROOT" "$WK_STORE" \
    "'git-webkit pr' and claude in a workspace will fail" "$_unit_journal" sh -c
unit_prune sh -c

if wk_py wk.secrets push-converge; then
    debug "GitHub token, Bugzilla key and deploy keys converged"
else
    warn "could not write the injector's tokens or the push service's deploy keys, so a read from a
  workspace answers 401 ('wk key set github-pat' stores a token) and a push is refused"
fi

unset SDK _before _after _unit_journal
