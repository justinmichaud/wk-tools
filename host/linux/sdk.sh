# The container SDK and the units a workspace-running machine carries: the Linux
# counterpart of host/macos/vmtools.sh. Nothing here needs root.

. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/host/units.sh"

# Not /opt: an unprivileged checkout is one the user can reset and re-clone.
# targets/container.sh reads WK_SDK the same way.
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

# Without the reset the idempotent patcher leaves a tampered file as found.
_before=$(cd "$SDK" && git rev-parse HEAD)$(cd "$SDK" && git status --porcelain | wc -l)
git -C "$SDK" reset --hard --quiet
git -C "$SDK" clean -qfd

if bash "$WK_ROOT/container/sdk-patches/apply.sh" "$SDK" >/dev/null 2>&1; then
    unchanged "SDK patched"
else
    bash "$WK_ROOT/container/sdk-patches/apply.sh" "$SDK" || die "SDK patching failed"
fi

_unit_journal=""

unit_start wk-proxy.service "$WK_ROOT" "$WK_STORE" \
    "workspaces will have no egress" "$_unit_journal" sh -c

unit_start wk-ssh-agent.service "$WK_ROOT" "$WK_STORE" \
    "no workspace here can push" "$_unit_journal" sh -c

unit_start wk-github-inject.service "$WK_ROOT" "$WK_STORE" \
    "'git-webkit pr' in a workspace will fail" "$_unit_journal" sh -c

# The standing read token goes in beside it, from what this machine holds:
# reading is open whatever position `wk push` is in.
if push_agent_pat_sync push_agent_exec "$(push_agent_machine_read_pat)"; then
    debug "GitHub read token converged"
else
    warn "could not write $(push_agent_machine_read_pat), so a read from a
  workspace answers 401 ('wk key set github-pat' stores a token)"
fi

unset SDK _before _unit_journal
