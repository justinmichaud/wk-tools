# Claude Code skills

WebKit / JavaScriptCore skills for Claude Code (plus a few adjacent ones, e.g.
Chromium build flags). `jsc` is the main skill and cross-references the others,
so they live together.

Nothing here is installed by hand. `./setup` links `~/.claude/skills` to this
directory on the host (claude/install.sh); a container workspace
(container/firstrun.sh) and a macOS guest (vm/provision-base.sh) get a
read-only symlink into their wk-tools tree.

Settings and hooks follow the same scheme, with one split that matters: the
host gets `claude/settings-host.json` and `claude/CLAUDE-host.md`, workspaces
get `claude/settings.json` and `claude/CLAUDE.md`. The workspace variants
assume the sandbox is the blast radius and must never be installed on a host.
