# HANDOFF — rewrite `cmd/bench` in Python

## Remaining

- [ ] rewrite `cmd/bench` as one stdlib-only Python program (the `cmd/mcp` precedent), keeping the CLI (`seed`, `stage`, `staged`, `compare`, `ls`, `ab-summary`) and the behaviour contract: preflight refuses rather than annotates, `--force` records itself in provenance, payloads seeded and pinned by commit, env.json provenance written before the run [decision]
- [ ] drive the workspace through `wk`'s target drivers by shelling out, not a second container-exec path [needs the rewrite]
- [ ] design the store format so `wk pi bench` can file on-board runs where `wk bench ls`/`compare` can see them, rather than bolting it on after [decision]
- [ ] keep the regression coverage the bash version encodes: llvmpipe-vs-GPU refusal, session-mode refusal, count=1 compare warning, awk-not-grep-head parsing [needs the rewrite]
- [ ] land the rewrite behind a side-by-side comparison of env.json and result layout against one real run [needs a workspace]
- [ ] add a `tests/` case and a `wk selftest` check for the rewrite [needs the rewrite]
- [ ] confirm `wk bench` killed during seed re-fetches the whole payload and prunes leaked `.tmp-*` dirs (`wk gc`); killed during a run leaves an `env.json` with no `result.json`, reading as crashed and never comparable [needs a workspace]
