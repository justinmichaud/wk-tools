# Claude Code skills

WebKit / JavaScriptCore skills for Claude Code (plus a few adjacent ones, e.g.
Chromium build flags). `jsc` is the main skill and cross-references the others,
so they live together.

Nothing here is installed by hand. `./setup` links `~/.claude/skills` to this
directory on the host (claude/install.sh); a container workspace mounts the
shared mutable copy at `/skills` (container/firstrun.sh), which `wk skills
status|diff|pull|push` reconciles with this directory; a macOS guest gets a
read-only symlink into its synced wk-tools tree (vm/provision-base.sh).

Settings and hooks follow the same scheme, with one split that matters: the
host gets `claude/settings-host.json` and `claude/CLAUDE-host.md`, workspaces
get `claude/settings.json` and `claude/CLAUDE.md`. The workspace variants
assume the sandbox is the blast radius and must never be installed on a host.
