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

- **Claude only ever runs inside a workspace.** The in-workspace interface --
  `wk build <config>`, `wk run -- <args>`, `wk test <args>`, no workspace
  name -- is load-bearing, not a convenience: it carries the job-count and
  nice-level policy, and hand-rolling `build-webkit` around it is forbidden.
- **Never edit wk-tools while a `wk` command is running**, here or on a
  machine `t_sync_tools` copies the tree to: bash reads a script by byte
  offset, so a rewritten `cmd/build` resumes a running build mid-word. Check
  `wk status` first.
- **Measure before theorising.** When something is unreachable or flaky, take
  the measurement (packet counts, tcpdump on the far side, timing per
  candidate) before proposing a cause, and fix the root cause; do not build
  machinery around a fault nobody has measured.
- **No in-place upgrades.** A guest, golden base or image that is wrong is
  fixed by changing the input that produces it (`WK_VM_IMAGE`,
  `vm/provision-base.sh`, an image config) and rebuilding. Hand-patching a
  live guest is how to discover the fix, never how to deliver it.
- **Do not mutate the host or the fleet unprompted.** `wk sync --machine`,
  `wk quiesce on`, `wk stop`, `wk boot`, card writes and provisioning change
  real machines; run them only when the user asked for that outcome.

Never use `git push --force` against a shared branch, and never commit unless
asked.

## Long-running wk commands

`wk build`, `wk test` and `wk bench` outlive any tool call: the Bash tool
defaults to a 2-minute timeout and maxes out at 10, while a cold WebKit build
is tens of minutes. A foreground timeout SIGTERMs the local driver, and that
propagates to the remote ninja -- the build dies at whatever object it had
reached.

So never run one in the foreground. Two ways, and prefer the first:

- **`wk build <ws> <config> --detach`** returns in a fraction of a second and
  leaves the build running *on the machine that builds it* — in the podman VM
  for a container workspace, on the build machine itself for a remote one. No
  ssh session has to stay up, so nothing this end does can kill it. Then poll
  `wk status <ws>` and read `wk logs <ws>` when it ends.
- `run_in_background` for anything without a `--detach` of its own
  (`wk test`, `wk bench`, `./setup`), then poll the same way.

**Do not hand-roll a poll loop around a pid.** `wk status <ws> --wait` blocks
while the workspace is busy and then reports once, with the same
machine-readable exit code as a bare `wk status` (2 means busy, and it is the
only state `--wait` waits through). A pid on this side is not evidence about a
build driven over ssh, and a `for i in $(seq …); do kill -0 …; sleep 5; done`
reports "still building" about a build that failed a minute ago.

`wk setup` and the other provisioning commands are minutes, not seconds: run
them in the background too rather than watching a tool call time out.

`wk status` reporting `alive: [N/M] (last output Xs ago)` just after a kill is
not evidence the build survived; in-flight compile jobs drain for a minute or
more. Confirm with `pgrep -c ninja` on the target before believing it.
