# Handoff: git and GitHub helpers

Split out of the "Git and GitHub" table in `docs/HANDOFF-original-helpers.md`
— these don't share tooling or a machine constraint with the
profiling/benchmarking/wasm material that lives there, and they're
machine-agnostic (fine to pick up on either the Linux workstation or the
macOS host, whichever is free).

| original | did | now |
|---|---|---|
| `gpr` | check out a fork branch in `user:branch` PR syntax, adding the remote if needed, confirming every git command, diffing local vs remote before offering `reset --hard` | **gone** — 262 lines, the most substantial helper here |
| `git-sync-fork` | fetch both remotes, rebase `main` onto upstream | **gone** |
| `git-clean` | `git reset . && git checkout . && git clean -fd` | **gone** (one line, but destructive-by-design and easy to want) |
| `commit-count` | `git shortlog --summary --since "1 year"` | **gone** |
| `report` | weekly GitHub activity summary via `gh` | **done** — `wk report` already covers this, no action needed |

## What to do

Restore in this order — `gpr` first, the rest as filler:

1. **`gpr`.** The one worth doing first: it's the only helper here with real
   logic rather than a hardcoded path, it prints and confirms every git
   command before running it, and nothing in `wk` covers checking out someone
   else's PR branch today.
2. **`git-sync-fork`.** Natural pair with `gpr` — both are fork-branch
   plumbing — so do it right after, sharing whatever remote-handling code
   `gpr` ends up with instead of duplicating it.
3. **`git-clean` and `commit-count`** are one-liners with no logic to design;
   restore both as trivial `container/bin/` scripts whenever there's spare
   time. Consolidating them into a single `gpr`-adjacent helper isn't worth
   it — they solve unrelated problems (destructive reset vs. a read-only
   report) and have nothing to share.

## Where this connects to sandboxing

Whether any of these are even allowed to push is a separate, security-shaped
question, not a "restore the helper" question — see the git-push permission
toggle in `docs/HANDOFF-sandboxing.md`. Build `gpr`/`git-sync-fork` assuming
that toggle exists and respect it (i.e. don't have the restored helper push
directly; let it hand off to whatever `git push` the toggle already gates).
