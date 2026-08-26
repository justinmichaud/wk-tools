# Deploy the Claude configuration into ~/.claude, on a HOST -- this
# workstation, or a remote build machine reached over ssh by 'wk remote
# setup' (WK_CLAUDE_REMOTE=1; the two are otherwise identical). Workspaces get
# their own copies elsewhere: container/firstrun.sh links the workspace
# variants inside a container, and vm/provision-base.sh does the same inside a
# macOS guest.
#
# Either kind of host gets the -host variants deliberately. The workspace
# settings allow Bash(*) and the workspace CLAUDE.md says "you are inside a
# sandbox" -- both statements are true only where the workspace is the blast
# radius, and a host session is exactly where they must not apply.
#
# Symlinks rather than copies: editing the repo takes effect in the next
# session with no redeploy step. ~/.claude also holds live state (sessions,
# history, credentials), so only the config entries are linked; the directory
# itself is never replaced.

_claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

ensure_dir "$_claude_dir" 0755

# A remote host's settings.json is not a symlink: it is settings-host.json
# with one more permission merged in. `wk` is the one thing a shell there is
# for (cmd/remote's own MOTD-reachable workflow is "ssh in, then wk ls / wk
# build"), while this workstation keeps prompting for everything else -- so
# the same source of truth, materialised rather than linked, is one file
# short of a third tracked settings.json.
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
