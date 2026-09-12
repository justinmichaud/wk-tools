# Owed: the CLI shape (CLAUDE.md, rules 7-9)

- [ ] `--dry-run` on every mutating command: route each state change through
      `act` (lib/common.sh) and declare `dryrun`. Refusing the flag today
      (`wk --declarations`, column 9): ai backup enter gc gui key mcp new
      notify pick pr quiesce remote rm run scp selftest session skills start
      stop sudo sync verify vm zed; and bench outside stage/staged/mac/mac-volume,
      pi outside boot-order, sysimage outside build/webkit/write.
- [ ] `wk ab --dry-run` still fetches the pull request and the base branch into
      the mirror to resolve the plan it prints; resolve them without fetching.
- [ ] The commands that print a plan of their own under WK_DRY_RUN (boot,
      sysimage, bridge, pi boot-order, bench stage/staged, ab, build, test,
      profile) branch on the variable at the top rather than at each leaf;
      move each leaf to `act` so the plan and the run cannot differ.
- [ ] Destructive paths still reached without a question of their own:
      `wk sysimage write`'s tailnet device DELETE (cmd/sysimage, lib/tailnet.py)
      rides under the card question without being named in it; `wk pi helper`
      replaces a root-owned helper on a board; lib/tools.sh removes a
      non-checkout destination on another machine; container/sdk-refresh.sh
      resets the shared SDK checkout.
- [ ] `wk key adopt` and `wk key set --paste` overwrite a credential on the
      machine they run on with no question, because the sending side asked;
      the far side has no way to know that. Carry the answer over (WK_CONFIRMED
      in the forwarded env) instead of exempting the receiver.
