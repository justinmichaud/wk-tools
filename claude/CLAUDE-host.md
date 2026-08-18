# Working environment

This is a **host machine**, not a workspace. Claude normally runs *inside* a
wk workspace (`wk claude <name>`), where the sandbox permits relaxed
permissions; a session here runs with normal prompts and none of that
protection.

Consequences:

- **Never build, test, or benchmark WebKit here.** Drive a workspace instead:
  `wk new <name>`, `wk build <name> <config>`, `wk test <name>`, `wk rm <name>`.
  `wk help` lists everything.
- **Do not weaken the sandbox to make something work.** If a workspace cannot
  do something, that is usually the boundary working; the fix belongs in
  wk-tools, not in giving an agent host access.
- The workspace-side rules (network allowlist, git conventions, the mandatory
  `jsc` skill for WebKit edits) live in `claude/CLAUDE.md` in the wk-tools
  repo and apply inside workspaces.

Never use `git push --force` against a shared branch, and never commit unless
asked.
