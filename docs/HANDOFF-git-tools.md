# HANDOFF — git and GitHub helpers

`wk pr <user>:<branch>` is restored and `wk report` covers the weekly summary.
Everything else in the original set is still gone, and `container/bin/` holds
only `strip-addresses` and `show-profiled-functions`.

Machine-agnostic work: fine on either the Linux workstation or the macOS host.

## Remaining

1. **`git-sync-fork`** — fetch both remotes, rebase `main` onto upstream. The
   natural pair with `wk pr`; share its remote handling rather than duplicating
   it. `wk_pr_repos` and `wk_wiring_script` in `lib/store.sh` answer "which
   projects exist" and "what a checkout's remotes should be", each in one place.
2. **A cherry-pick helper for release-branch maintenance** (`wk pick`). The
   `WebKit-branching` wiki page shows the manual loop: collect commit ids,
   resolve WebKit `NNNNNN@main` identifiers to ToT shas (today a re-typed
   throwaway python snippet; they resolve via commits.webkit.org or `git log
   --grep` over the trailer), `git cherry-pick --stdin`. Same
   confirm-every-command discipline as `wk pr`.
3. **`git-clean`** (`git reset . && git checkout . && git clean -fd`) and
   **`commit-count`** (`git shortlog --summary --since "1 year"`) — one-liners
   with no logic to design; restore both as trivial `container/bin/` scripts.
   Do not consolidate them: a destructive reset and a read-only report share
   nothing.
4. **The end-to-end PR walkthrough, still owed.** Sandboxed agents driving
   builds while a person pushes, rebases, fetches forks and uploads PRs — with
   `wk push`, `wk remotes` and `wk pr` all in the loop, including making a PR
   from an armhf container where `git-webkit` cannot run. Every piece is
   verified alone; the walkthrough is what proves they compose. Prove
   `wk claude` turning the push switch back on at session end as part of it —
   that TESTING.md line is still unticked.

## Constraint

Whether any of these may push is gated, not designed per-helper: `wk push
on|off|status` moves the deploy keys in and out of what workspaces mount, and
`wk claude` turns it off before an agent takes over. Build on top of it — let
the helper run `git push` and let the switch refuse it. No helper carries a
credential or pushes by another route.
