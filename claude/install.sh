# Deploy the Claude configuration into ~/.claude, on the HOST.
#
# Sourced only by ./setup. Workspaces get their own copies elsewhere:
# container/firstrun.sh links the workspace variants inside a container, and
# vm/provision-base.sh does the same inside a macOS guest.
#
# The host gets the -host variants deliberately. The workspace settings allow
# Bash(*) and the workspace CLAUDE.md says "you are inside a sandbox" -- both
# statements are true only where the workspace is the blast radius, and a host
# session is exactly where they must not apply.
#
# Symlinks rather than copies: editing the repo takes effect in the next
# session with no redeploy step. ~/.claude also holds live state (sessions,
# history, credentials), so only the config entries are linked; the directory
# itself is never replaced.

_claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

ensure_dir "$_claude_dir" 0755

link_config "$WK_ROOT/claude/settings-host.json" "$_claude_dir/settings.json"
link_config "$WK_ROOT/claude/skills"             "$_claude_dir/skills"
link_config "$WK_ROOT/claude/hooks"              "$_claude_dir/hooks"
link_config "$WK_ROOT/claude/CLAUDE-host.md"     "$_claude_dir/CLAUDE.md"

# Hooks must be executable after a fresh clone; git preserves the bit, but a
# clone with a restrictive umask or a copy over a filesystem that drops it will
# silently disable the hook rather than erroring.
for _h in "$WK_ROOT"/claude/hooks/*.sh; do
    [ -f "$_h" ] || continue
    if [ -x "$_h" ]; then
        unchanged "hook +x $(basename "$_h")"
    else
        chmod +x "$_h"
        changed "hook +x $(basename "$_h")"
    fi
done

# The webkit-jsc-skill-reminder hook parses its JSON payload with jq. Without
# it the hook fails on every edit, which is noisy and easy to misdiagnose.
have jq || warn "jq is not installed; the WebKit skill-reminder hook will not fire"

unset _claude_dir _h
