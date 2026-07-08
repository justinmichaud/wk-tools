# Claude Code skills

WebKit / JavaScriptCore skills for Claude Code. These are the real files; on each machine
`~/.claude/skills` is a symlink to *this* folder, so the repo is the single source of truth
and Claude Code edits land here directly. `jsc` is the main skill and cross-references the
others, so they live together. The sibling `../claude-settings.json` and `../claude-hooks/`
are shared the same way (symlinked to `~/.claude/settings.json` and `~/.claude/hooks`).

## Fresh install on a new machine

Claude Code loads skills, settings, and hooks from `~/.claude`. Point each of those at this
repo with a symlink, so every skill here (and any added later) is picked up automatically
and the settings/hooks stay in sync:

```bash
# 1. Clone wk-tools (skip if already cloned).
git clone git@github.com:justinmichaud/wk-tools.git ~/Development/wk-tools

# 2. Back up any existing real files/dirs, then symlink skills, settings, and hooks.
#    Each `[ -e ] && [ ! -L ]` guard backs up a real file/dir once and never clobbers an
#    existing symlink, so re-running is safe. A directory symlink means new skills committed
#    here show up with no extra steps.
mkdir -p ~/.claude
[ -e ~/.claude/skills ]        && [ ! -L ~/.claude/skills ]        && mv ~/.claude/skills        ~/.claude/skills.bak
[ -e ~/.claude/settings.json ] && [ ! -L ~/.claude/settings.json ] && mv ~/.claude/settings.json ~/.claude/settings.json.bak
[ -e ~/.claude/hooks ]         && [ ! -L ~/.claude/hooks ]         && mv ~/.claude/hooks         ~/.claude/hooks.bak

ln -sfn ~/Development/wk-tools/claude-skills   ~/.claude/skills
ln -sfn ~/Development/wk-tools/claude-settings.json ~/.claude/settings.json
ln -sfn ~/Development/wk-tools/claude-hooks    ~/.claude/hooks

# 3. Verify: skills should list jsc, build-webkit, etc.; the others should be symlinks.
ls -l ~/.claude/skills
ls -l ~/.claude/settings.json
ls -l ~/.claude/hooks
```

`ln -sfn` is idempotent: re-running it just re-points the link, so it is safe on a machine
that is already set up. The hook command uses `$HOME`, not a hardcoded path, so the same
symlink works everywhere.

## wkdev64 container (`~/Development/64`)

The exact same commands work inside the wkdev64 container with no changes. Inside the
container `$HOME` is the container's own home (mounted at `~/Development/64` from the host),
and `~/Development/wk-tools` there is a symlink into the host checkout
(`/host/home/.../Development/wk-tools`). So `~/.claude/*` lands in the container's `.claude`
while still pointing at the one shared repo. Just run the step 2 block from inside the
container (skip the clone in step 1 — `~/Development/wk-tools` already resolves).
