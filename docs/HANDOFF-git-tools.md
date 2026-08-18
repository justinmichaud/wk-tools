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

1. **Bring `cmd/pr` up to house style.** It exists and works, but predates the
   repo's conventions: it does not source `lib/common.sh`, and it assumes VS
   Code for viewing the diff. Align it rather than rewriting it.
2. **`git-sync-fork`.** Natural pair with `wk pr` — both are fork-branch
   plumbing — so share its remote-handling code instead of duplicating it.
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
question, not a "restore the helper" question — see the git-push permission
toggle in `docs/HANDOFF-sandboxing.md`. Build `gpr`/`git-sync-fork` assuming
that toggle exists and respect it (i.e. don't have the restored helper push
directly; let it hand off to whatever `git push` the toggle already gates).
