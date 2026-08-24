# HANDOFF — rewrite `cmd/bench` in Python

Not started, and the counts that justify it keep growing: **1924 lines** of bash
today (~580 when this was first written), **7 heredocs — 4 of them Python**
(result.json assembly, env.json, the compare diff, the `staged` reader), and
**47 `WK_M_*` references** passing provenance through the environment. Each is a
workaround for bash, not a design.

## Remaining

One Python program (`cmd/mcp` is the precedent: Python-only, stdlib-only).

- Keep the exact CLI: `wk bench <ws> <plan> [flags]`, `seed`, `stage`, `staged`,
  `compare`, `ls` — plus `ab-summary` and whatever the mac A/B path added.
- Keep the behaviour contract: preflight refuses rather than annotates
  (`--force` records itself in provenance), payloads seeded and pinned by
  commit, env.json provenance written before the run.
- Drive the workspace through `wk`'s target drivers by shelling out — do not
  grow a second container-exec path.
- stdlib only; must run on the podman VM's python3 and Ubuntu's.

## Watch out

- The `set -e`/pipefail traps documented in the bash version's comments are the
  regression tests: llvmpipe-vs-GPU refusal, session-mode refusal, count=1
  compare warning, the awk-not-grep-head lesson.
- `wk bench` is on the daily path; land the rewrite behind a side-by-side
  comparison of env.json and result layout on one real run.
- The store format is about to gain a second producer — `wk pi bench` needs to
  file on-board runs where `wk bench ls`/`compare` can see them
  (`docs/HANDOFF-pi-deploy.md`). Design for that rather than around it.
- Add a TESTING.md line item and a `wk selftest` check.
