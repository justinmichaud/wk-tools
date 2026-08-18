# HANDOFF — rewrite `cmd/bench` in Python

`cmd/bench` (~580 lines of bash) has outgrown the language: four inline Python
heredocs, an env.json assembled by passing sixteen `WK_M_*` variables through
the environment, a hand-rolled `.plan` JSON parser, and argument lists built by
string concatenation. Each of those is a workaround for bash, not a design.

## Shape

One Python program (the repo already ships Python-only `cmd/mcp`, so the
precedent and the "no third-party imports" constraint both exist):

- Keep the exact CLI: `wk bench <ws> <plan> [flags]`, `seed`, `compare`, `ls`.
- Keep the behavior contract: preflight refuses rather than annotates
  (`--force` records itself in provenance), payloads seeded and pinned by
  commit, env.json provenance written before the run.
- Drive the workspace through `wk`'s target drivers by shelling out to
  `cmd/...`/`t_exec` equivalents — do not grow a second container-exec path.
- stdlib only; must run on the podman VM's python3 and Ubuntu's.

## Watch out

- The `set -e`/pipefail traps documented in the bash version's comments are
  the regression tests: llvmpipe-vs-GPU refusal, session-mode refusal,
  count=1 compare warning, the awk-not-grep-head lesson.
- `wk bench` is on the benchmarking daily path; land the rewrite behind a
  side-by-side comparison of env.json and result layout on one real run.
- Add a TESTING.md line item and a `wk selftest` check
  (`docs/HANDOFF-test-runner.md`).
