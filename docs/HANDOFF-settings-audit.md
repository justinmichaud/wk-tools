# Handoff: audit non-default host settings, ask before persisting

`wk backup` is the inverse of `./setup` — it writes live desktop/OS settings
back into the repo (`host/linux/config.dconf`, `host/macos/defaults.conf`) so
a fresh machine can reproduce them. Both files are meant to hold *deliberate*
choices only, per `cmd/backup`'s own comment: dumping everything produces a
file that can't be applied to another machine and that changes on every run.

That intent has never actually been checked against reality on either
platform. This handoff is the check: find every setting that's currently
non-default on each host, decide — with the user, not for them — whether it
belongs in the repo, and end with a plain summary of what's now tracked and
why.

**Do not silently add or drop settings.** The point of this task is that each
one is a choice the user made, and the previous handoffs already have a
warning about not respecting that: `docs/HANDOFF-sandboxing.md` lists "claude
overwriting my work" as a real incident. Present findings, wait for an answer,
then write.

## Workflow (same shape on both platforms)

1. Run `wk backup` to regenerate the tracked file from the live machine.
2. `git diff` it against the last-committed version — that's the exact list
   of what's new, changed, or (if something reverted) gone.
3. Before showing anything to the user, apply `cmd/backup`'s existing filters
   for known machine-specific junk (see Linux section below) and drop those
   silently — the user shouldn't be asked about noise the filter was already
   supposed to remove. If the filter doesn't catch something it should, fix
   the filter rather than asking every time.
4. For everything left, ask the user explicitly, one batch of questions
   rather than one prompt per setting: keep it in the repo (so `./setup`
   reproduces it on a new machine), or drop it (machine-specific, accidental,
   or no longer wanted).
5. Write the kept set back to the tracked file, run `./setup` (or the
   relevant stage) to confirm the round trip actually reproduces the state,
   and commit only after that's confirmed.
6. Finish with a short written summary — what's now persisted and, for each,
   the one-line reason it's a deliberate choice rather than machine noise.
   That summary is the deliverable as much as the file change is; it's what
   makes the next audit fast instead of starting from zero.

## Linux — `host/linux/config.dconf`

The known problem, already flagged in `docs/HANDOFF-linux.md`: the file is a
raw dump containing a weather location, four nm-applet WiFi UUIDs, a GTK
last-folder path, Ptyxis profile UUIDs, and timestamps — none of which
transfer to another machine or represent a real choice. `cmd/backup` has
filters written for these but they've never been exercised.

1. Verify those filters actually strip that specific junk before trusting
   them for anything else.
2. Run the full workflow above on the dconf tree. `apt.txt` (the installed
   package list) is adjacent state worth the same treatment — audit it for
   packages that crept in without being a deliberate "this belongs on every
   machine" decision.
3. Confirm the full round trip: `wk backup` → `./setup` → the machine ends up
   in the same state, with `./setup` reporting no further changes on a second
   run (the existing invariant from `docs/HANDOFF-linux.md`).

## macOS — `host/macos/defaults.conf` and friends

`defaults.conf` is already curated (domain/key/type triples with comments),
and `cmd/backup` refreshes only the values for keys already listed there —
it's a "keep what's already decided in sync," not an audit for what's missing.
The gap this handoff covers: macOS reports ~574 preference domains total, and
nothing currently looks for domains that are non-default, plausibly
deliberate, and *not yet* in `defaults.conf`.

1. Enumerate current `defaults read` state across domains, filter out what's
   obviously window position / per-app transient state (the same reasoning
   `cmd/backup`'s comment already gives for why 574 domains isn't the list),
   and surface the remainder as candidates.
2. Ask the user per candidate: add to `defaults.conf` (with the same
   domain/key/type/comment shape the file already uses), or leave untracked.
3. The same question applies to the other machine-state files in
   `host/macos/`: `symbolichotkeys.plist`, `softnet.sh`, `vmtools.sh`,
   `mcp.sh`, `tools.sh`, `playbook.yaml`. Anything in there that's drifted
   from what's committed is an audit candidate the same way `defaults.conf`
   is; treat this as one audit pass across the whole directory, not just the
   one file.
4. Confirm the round trip the same way as Linux: `wk backup` → `./setup` on a
   clean-ish machine, second run reports no changes.
