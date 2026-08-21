# Handoff: git and GitHub helpers

Split out of the "Git and GitHub" table in `docs/HANDOFF-original-helpers.md`
— these don't share tooling or a machine constraint with the
profiling/benchmarking/wasm material that lives there, and they're
machine-agnostic (fine to pick up on either the Linux workstation or the
macOS host, whichever is free).

| original | did | now |
|---|---|---|
| `gpr` | check out a fork branch in `user:branch` PR syntax, adding the remote if needed, confirming every git command, diffing local vs remote before offering `reset --hard` | **done** — restored as `cmd/pr` (`wk pr <user>:<branch>`) |
| `git-sync-fork` | fetch both remotes, rebase `main` onto upstream | **gone** |
| `git-clean` | `git reset . && git checkout . && git clean -fd` | **gone** (one line, but destructive-by-design and easy to want) |
| `commit-count` | `git shortlog --summary --since "1 year"` | **gone** |
| `report` | weekly GitHub activity summary via `gh` | **done** — `wk report` already covers this, no action needed |

## What to do

1. ~~**Bring `cmd/pr` up to house style.**~~ **Done 2026-08-19**, and it did
   turn into a rewrite: the shape was wrong, not just the style. It now takes
   a workspace like `wk build` does (the checkout is in one, never on the
   host), finds the project by asking *both* forks — WebKit and WPEWebKit —
   for the branch rather than assuming whatever `origin` is, and drops the
   print-and-confirm-every-command narration. The VS Code diff is gone with
   it. One warning remains, which is the only one the user wanted: your own
   work, either uncommitted or as commits the PR head does not have, is named
   and never overwritten; `--force` takes the head and says twice what it
   discarded.
2. **`git-sync-fork`.** Natural pair with `wk pr` — both are fork-branch
   plumbing — so share its remote-handling code instead of duplicating it.
   The pieces to reuse are `wk_pr_repos` and `wk_wiring_script` in
   `lib/store.sh`: which projects exist, and what a checkout's remotes should
   be, are each answered in one place now.
3. **A cherry-pick helper for release-branch maintenance.** The
   `WebKit-branching` wiki page shows the manual loop: collect commit ids,
   resolve WebKit `NNNNNN@main` identifiers to ToT shas (today via a
   re-typed throwaway python snippet; they resolve via commits.webkit.org or
   a `git log --grep` over the trailer), `git cherry-pick --stdin`. Make it a
   `wk pick` verb or a script beside `wk pr`, with the same
   confirm-every-command discipline.
4. **`git-clean` and `commit-count`** are one-liners with no logic to design;
   restore both as trivial `container/bin/` scripts whenever there's spare
   time. Consolidating them into a single helper isn't worth it — they solve
   unrelated problems (destructive reset vs. a read-only report) and have
   nothing to share.

## Where this connects to sandboxing

Whether any of these are even allowed to push is a separate, security-shaped
question, not a "restore the helper" question. **The toggle now exists**:
`wk push on|off|status` (2026-08-19), which moves the deploy keys in and out of
the directory workspaces have mounted, and which `wk claude` turns off before
an agent takes over. Build `gpr`/`git-sync-fork` on top of it: don't have the
restored helper carry a credential or push by any other route — let it run
`git push`, which the switch already gates.

## The walkthrough still owed

The PR workflow has never been walked end to end as a whole: sandboxed agents
driving builds while I push, rebase, fetch forks and upload PRs myself — with
the push switch, `wk remotes`, and `wk pr` all in the loop — including the
edge case of making a PR from an armhf container, where git-webkit cannot
run. Each piece is verified alone; the walkthrough is what proves they
compose.
