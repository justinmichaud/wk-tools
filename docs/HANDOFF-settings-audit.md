# HANDOFF — audit non-default host settings, ask before persisting

`wk backup` is the inverse of `./setup`: it writes live desktop/OS settings back
into the repo (`host/linux/config.dconf`, `host/macos/defaults.conf`) so a fresh
machine can reproduce them. Both files are meant to hold *deliberate* choices
only — dumping everything produces a file that cannot be applied to another
machine and that changes on every run.

That intent has never been checked against reality on either platform. This is
that check, and it has not started.

**Do not silently add or drop settings.** Each one is a choice the user made.
Present findings, wait for an answer, then write.

## The workflow, same shape on both platforms

1. Run `wk backup` to regenerate the tracked file from the live machine.
2. `git diff` it — that is exactly what is new, changed or gone.
3. Apply `cmd/backup`'s existing junk filters and drop that silently; the user
   should not be asked about noise the filter was supposed to remove. If the
   filter misses something it should catch, fix the filter.
4. Ask about everything left, in **one batch**: keep it in the repo, or drop it.
5. Write the kept set back, run `./setup` (or the stage) to confirm the round
   trip reproduces the state, and commit only after that.
6. Finish with a short written summary — what is persisted and the one-line
   reason each is a deliberate choice. That summary is the deliverable as much
   as the file change: it is what makes the next audit fast.

## Remaining — Linux (`host/linux/config.dconf`)

The file is a raw dump containing a weather location, four nm-applet WiFi UUIDs,
a GTK last-folder path, Ptyxis profile UUIDs and timestamps — none of which
transfer or represent a choice. `cmd/backup` has filters for these and **they
have never been exercised**.

1. Verify the filters strip that specific junk before trusting them further.
2. Run the workflow on the dconf tree. `apt.txt` deserves the same treatment —
   audit it for packages that crept in without a deliberate decision.
3. Confirm the round trip: `wk backup` → `./setup` → same state, with a second
   `./setup` reporting no further changes.

## Remaining — macOS (`host/macos/defaults.conf` and friends)

`defaults.conf` is curated (domain/key/type triples with comments) and
`cmd/backup` refreshes only keys already listed — "keep what is decided in
sync", not an audit for what is missing. macOS reports ~574 preference domains
and nothing looks for ones that are non-default, plausibly deliberate, and not
yet listed.

1. Enumerate `defaults read` across domains, filter obvious window-position and
   per-app transient state, surface the remainder as candidates.
2. Ask per candidate: add with the same domain/key/type/comment shape, or leave
   untracked.
3. Treat the rest of `host/macos/` the same way — `symbolichotkeys.plist`,
   `softnet.sh`, `vmtools.sh`, `mcp.sh`, `tools.sh`, `playbook.yaml`. One audit
   pass across the directory, not one file.
4. Confirm the round trip as on Linux.

## Three things that make this bigger than it was

- **The macOS bench install is a second machine's worth of settings**, and what
  it needs is the opposite of a workstation's: no FileVault, no update checking,
  no App Nap. `docs/HANDOFF-mac-perf-mode.md` and `docs/HANDOFF-reprovision.md`
  both name settings this audit is supposed to persist-or-drop.
- **The rpi5's hand-applied state** (`host/linux/rpi5/`) is the sharpest live
  example — overclock, fan, wifi powersave, a pinned BSSID — restored from
  backup and recreated by nothing.
- **`wk doctor`'s machine-local-state section** is the enforcement point for
  cattle-not-pets; this audit's output should reconcile with it rather than
  becoming a second list.
