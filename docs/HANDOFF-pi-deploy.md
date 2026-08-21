# HANDOFF — `wk pi deploy` and `wk pi bench`

Two rituals from the wiki that are done by hand on every Pi cycle, encoded as
`cmd/pi` verbs. Depends on `wk pi setup` having put the device on the tailnet
(`docs/HANDOFF-linux-pi.md`) and, for cross-built binaries, on the
cross-compile flow (`docs/HANDOFF-cross-compile.md`).

## `wk pi deploy <ws> <device>`

Replaces the four-step deploy loop (wiki: Container-Development-Setup "Yocto",
and the Buildroot page's dev cycle):

1. In the workspace: `unset CC CXX LD_LIBRARY_PATH` first — the wiki marks the
   env-poisoning as "VERY IMPORTANT" because a poisoned env produces a broken
   archive with no error; the command must scrub its own environment rather
   than trusting the shell's.
2. `built-product-archive ... archive` (or, for the buildroot fast path, just
   the `libWPEWebKit*` libraries — a `--libs-only` flag).
3. Copy to the device over the tailnet (the proxy allows the pi-hosts
   addresses on port 22; scp/rsync ride ssh).
4. On the device: `built-product-archive extract` (or drop the libs into
   `/usr/lib`).

## `wk pi bench <device> <plan>`

Replaces the per-session device babysitting (wiki: the rpi3 Yocto page's
"bash_history" ritual; the rpi3 skill already *teaches* this to an agent — the
command encodes the mechanics so neither human nor agent re-derives them):

- prep: stop `netdata`, ensure weston is up (seat/sudoers as the skill
  documents), extract `WAYLAND_DISPLAY` from weston's `/proc/<pid>/environ`;
- memory prep where the device needs it (rpi3: gpu_mem, swapfile on, per the
  wiki's "Potential RPI3 workaround" — one-time halves belong in
  `wk pi setup`);
- launch the browser at the benchmark URL, detect completion or crash (the
  rpi3 skill's monitor script shows how), pull the score;
- record provenance next to `wk bench`'s results so device runs and
  workstation runs live in one place. Whether this becomes
  `wk bench --device <pi>` instead of a `pi` verb is the implementer's call —
  provenance and refusal rules should be shared either way, and
  `wk bench stage`/`staged` (2026-08-20, `docs/HANDOFF-benchmarking.md`) is
  the existing shape for a run driven onto another machine.

## Traps

- The rpi3 is 32-bit WPE with its own OOM behaviour — the rpi3 skill is the
  reference for completion/crash detection; do not re-derive it.
- Keep the bench-mode question separate: the perf systems exist now
  (`wk sysimage build perf-linux-rpi4`), so `wk pi bench` targets whatever
  identity the booted system announces — a perf system's is mDNS, the
  downstream image's is whatever `wk pi setup` gave it — and nothing here
  should assume which system the device booted.
