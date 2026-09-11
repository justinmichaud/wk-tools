
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/host/units.sh"

SDK="${WK_SDK:-${XDG_DATA_HOME:-$HOME/.local/share}/webkit-container-sdk}"

if [ -d "$SDK/.git" ]; then
    unchanged "webkit-container-sdk present"
else
    info "cloning webkit-container-sdk into $SDK"
    ensure_dir "$(dirname "$SDK")"
    git clone -q https://github.com/Igalia/webkit-container-sdk.git "$SDK" \
        || die "could not clone the container SDK"
    changed "cloned webkit-container-sdk"
fi

_before=$(git -C "$SDK" rev-parse HEAD)
bash "$WK_ROOT/container/sdk-refresh.sh" "$SDK" \
    || die "refreshing the webkit-container-sdk checkout failed (above)"
_after=$(git -C "$SDK" rev-parse HEAD)
if [ "$_before" = "$_after" ]; then
    unchanged "webkit-container-sdk up to date"
else
    changed "webkit-container-sdk moved to $_after"
fi

_unit_journal=""

unit_start wk-proxy.service "$WK_ROOT" "$WK_STORE" \
    "workspaces will have no egress" "$_unit_journal" sh -c

unit_start wk-ssh-agent.service "$WK_ROOT" "$WK_STORE" \
    "no workspace here can push" "$_unit_journal" sh -c

unit_start wk-github-inject.service "$WK_ROOT" "$WK_STORE" \
    "'git-webkit pr' in a workspace will fail" "$_unit_journal" sh -c

if push_agent_pat_sync push_agent_exec "$(push_agent_machine_read_pat)"; then
    debug "GitHub read token converged"
else
    warn "could not write $(push_agent_machine_read_pat), so a read from a
  workspace answers 401 ('wk key set github-pat' stores a token)"
fi

unset SDK _before _after _unit_journal
