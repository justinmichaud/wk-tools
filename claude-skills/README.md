# Claude Code skills

WebKit / JavaScriptCore skills for Claude Code. These are the real files; on each machine
`~/.claude/skills` is a symlink to *this* folder, so the repo is the single source of truth
and Claude Code edits land here directly. `jsc` is the main skill and cross-references the
others, so they live together. The sibling `../claude-settings.json` and `../claude-hooks/`
are shared the same way (symlinked to `~/.claude/settings.json` and `~/.claude/hooks`).

## Fresh install on a new machine

Claude Code loads skills from `~/.claude/skills`. Point that whole directory at this folder
with one symlink, so every skill here (and any added later) is picked up automatically:

```bash
# 1. Clone wk-tools (skip if already cloned).
git clone git@github.com:justinmichaud/wk-tools.git ~/Development/wk-tools

# 2. Back up and remove any existing ~/.claude/skills, then symlink the whole folder.
#    A single directory symlink means new skills committed here show up with no extra steps.
mkdir -p ~/.claude
[ -e ~/.claude/skills ] && [ ! -L ~/.claude/skills ] && mv ~/.claude/skills ~/.claude/skills.bak
ln -sfn ~/Development/wk-tools/claude-skills ~/.claude/skills

# 2b. Same one-liner idea for the shared settings.json and hooks (works on machines and
#     containers -- the hook command uses $HOME, not a hardcoded path).
ln -sfn ~/Development/wk-tools/claude-settings.json ~/.claude/settings.json
ln -sfn ~/Development/wk-tools/claude-hooks ~/.claude/hooks

# 3. Verify: this should list jsc, build-webkit, etc.
ls ~/.claude/skills
```

`ln -sfn` is idempotent: re-running it just re-points the link, so it is safe on a machine
that is already set up.
