
wk_eval wk.store paths

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

wk_eval wk.daemons here proxy push github-inject

if wk_py wk.secrets push-converge; then
    debug "GitHub token, Bugzilla key and deploy keys converged"
else
    warn "could not write the injector's tokens or the push service's deploy keys, so a read from a
  workspace answers 401 ('wk key set github-pat' stores a token) and a push is refused"
fi

unset SDK _before _after
