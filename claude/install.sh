# Deploy the Claude configuration into ~/.claude on a HOST -- this workstation,
# or a remote build machine reached by `wk remote setup` (WK_CLAUDE_REMOTE=1).
# Workspaces get their own from container/firstrun.sh and vm/provision-base.sh.
#
# A host gets the -host variants: the workspace settings allow Bash(*) and the
# workspace CLAUDE.md says "you are inside a sandbox", true only where the
# workspace is the blast radius. Symlinked, so editing the repo takes effect in
# the next session; ~/.claude also holds live state, so only the config entries
# are linked.

_claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

ensure_dir "$_claude_dir" 0755

# A remote host's settings.json is settings-host.json with one more permission
# merged in, since `wk` is the one thing a shell there is for.
if [ -n "${WK_CLAUDE_REMOTE:-}" ]; then
    have jq || die "jq is required to merge Bash(wk *) into a remote host's settings.json.
    Install it (./setup --stage tools installs it from host/linux/apt.txt on
    Linux; on macOS: brew install jq or the signed package) and re-run."
    jq '.permissions.allow += ["Bash(wk *)"] | .permissions.allow |= unique' \
        "$WK_ROOT/claude/settings-host.json" | write_file "$_claude_dir/settings.json" 0644
else
    link_config "$WK_ROOT/claude/settings-host.json" "$_claude_dir/settings.json"
fi
link_config "$WK_ROOT/claude/skills"             "$_claude_dir/skills"
link_config "$WK_ROOT/claude/hooks"              "$_claude_dir/hooks"
link_config "$WK_ROOT/claude/CLAUDE-host.md"     "$_claude_dir/CLAUDE.md"

# A restrictive umask or a filesystem that drops the bit disables a hook
# silently rather than erroring.
for _h in "$WK_ROOT"/claude/hooks/*.sh; do
    [ -f "$_h" ] || continue
    if [ -x "$_h" ]; then
        unchanged "hook +x $(basename "$_h")"
    else
        chmod +x "$_h"
        changed "hook +x $(basename "$_h")"
    fi
done

# The webkit-jsc-skill-reminder hook parses its JSON payload with jq.
have jq || warn "jq is not installed; the WebKit skill-reminder hook will not fire"

unset _claude_dir _h
