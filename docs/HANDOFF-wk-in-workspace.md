# HANDOFF — `wk` inside a workspace

**Status: done and verified on both halves (2026-08-18)** — the Linux
container half from the workstation, the macOS guest half from the macOS host
(where the bash 3.2 parse could also be checked). Claude only ever runs inside
a wk workspace — that is the sandbox boundary and the whole point of the setup
— so it has to be able to drive its own building and testing from in there.
The global `CLAUDE.md` documented that interface before it existed. It exists:

```sh
wk build jsc-release        # no workspace name: the checkout it is in
wk run -- -e 'print(1+1)'
wk test <args>
wk status                   # this workspace's own build and test state
wk logs -f
```

What remains in this file is the decisions a later reader needs, and the one
leftover.

## The leftover: build state is recorded twice, once per side

A container workspace writes `build.status` and `build.log` into its own
`~/.local/state/wk/ws/<name>/`, because the host's store is not mounted in —
so `wk status <ws>` on the host says `build=none` while `wk status` inside the
same workspace says `build=ok`. Both are honest about what they can see, and
neither is the single machine-readable answer `wk status` exists to give. (The
*remote* target closed its copy of this in `docs/HANDOFF-linux-remote.md`: the
canonical file lives beside the checkout and `t_state_put` keeps it there.
Containers still have the split.)

The fix is one file for both sides, and the obvious way — bind-mounting the
host's `$ws` into the workspace — is wrong as stated: `$ws` also holds
`changes/` and `overlay-work/`, and a second write path into the upperdir of a
live overlay is exactly what `lib/store.sh` warns is undefined. A dedicated
`$ws/state:/wk-ws` mount would work, but the host reads `$ws/build.status`
directly through `wk_ws_dir`, so it means moving those files for every target.
Worth doing deliberately, not as a side effect of another change. (This is a
violation of the one-copy-per-machine rule in
`docs/HANDOFF-workspace-state.md` — that file's rules govern.)

## The load-bearing decisions

1. **A workspace marker, `~/.wk-workspace`** — `key=value`, naming the
   workspace and its checkout. Written by `container/firstrun.sh` for a
   container and by `targets/vm.sh` for a macOS guest; read only through
   `in_workspace`, `wk_marker_field`, `wk_self` and `default_target`
   (`lib/target.sh`). A file rather than an environment variable, because it
   has to be true for anything that reaches in — an agent's `bash -c`, a hook,
   ssh — not only for shells that inherited the right environment. It lives in
   the workspace's home, which for a container is `$ws/home` on the host and
   *not* the host's own `$HOME` — which is what keeps host behaviour
   byte-for-byte unchanged.

2. **`targets/local.sh`** — the driver where the target is this machine.
   `t_src`/`t_tools` from the marker; `t_exec` through a login shell (the
   proxy variables and PATH live in the profile, not this process);
   `t_cores`/`t_mem_mb` from the cgroup, so a workspace capped tighter than
   its host does not size a build from the host's cores; `t_create`,
   `t_destroy` and `t_enter` refuse.

3. **Dispatch.** `wk` selects `local` when the marker is present and never
   forwards; `default_target()` replaced the hardcoded `container` fallback in
   every command that resolves a workspace. Marker absent, nothing changed.

4. **The workspace argument is optional** in `cmd/build`, `cmd/run`,
   `cmd/test`, `cmd/logs`. The leading argument is consumed as a name only
   when it is this workspace's own name — and in `wk build`, only when that
   word is not also a config, so a workspace named after one still builds.

5. **Refusals, so nothing lies or acts on the wrong machine.** `wk verify`
   refuses inside: its checks are gated on `WK_SANDBOX`, which the local
   driver does not set, so it once reported "sandbox intact" after skipping
   the boundary entirely — found by running it. `wk claude` refuses (it would
   nest a relaxed-permission agent inside a sandbox nothing re-measured).
   `wk new`/`wk rm` refuse, and every host-only command — `sync`, `gc`,
   `session`, `quiesce`, `vm`, `pi`, `stop`, `start`, `setup`, `backup`,
   `mcp` — refuses rather than no-ops against an empty store; `wk sync` would
   otherwise fetch a 13 GB mirror into a directory discarded with the
   workspace.

6. **`config=` in the marker** (`default_config()`, `lib/target.sh`) — the
   config a bare `wk run`/`wk test` means, written by whoever writes the
   marker: `jsc-release` from `container/firstrun.sh`, `$WK_VM_BASE_PREBUILD`
   from `targets/vm.sh`. Without it a macOS guest — which can build nothing
   but the Apple ports — went looking for a JSCOnly binary that cannot exist.

7. **The marker is written on boot as well as on sync** (one `_write_marker`
   in `targets/vm.sh`): a guest booted and handed straight to `wk claude` has
   had no host-side build, and that agent's only way to build is this
   interface.

8. **`--dry-run` on `wk build` and `wk test`** — what made the guest half
   checkable at all (a cold `mac-release` is ~99 minutes). It stops on the
   line between resolution and mutation: no tooling sync, no status file.

9. **Sizing counts a workspace as headless.** A macOS guest has no cgroup, so
   the resource fallthrough once subtracted the 12 GB *desktop* reserve inside
   a guest whose size the host had already chosen — -j5 inside against -j9
   from the host, for the same build. `is_headless()` counts a workspace, so
   the reserve applied is the small headless one.

Also found by the same audit: **`wk report` is a host command** (it is `gh`
and nothing else; forwarded, it ran in the podman machine, which has neither
gh nor a credential), and `wk build --list` needs no target at all
(`is_targetless` in `wk`) — it used to forward and demand podman on a macOS
host. `wk pr` is deliberately left targetless-but-unforwarded: it runs `git`
in the current directory, and belongs to `docs/HANDOFF-git-tools.md`.

## Still open, smaller

- **`wk` auto-starts the podman machine for any mutating container-target
  command even when a macOS VM is running** — on a 32 GB host the two cannot
  coexist; it should report and let the user choose. (Read-only commands no
  longer boot it: that guarantee lives in `forward_to_vm` now —
  `docs/HANDOFF-workspace-state.md`.)
- **`wk ls` inside a workspace** prints `?` for BASE and `-` for CHANGES.
  Honest — both are overlay concepts that do not exist for this target — but
  it reads as missing data rather than as not-applicable.
- Workspaces created before the marker existed have none, and `.wkdev-init`
  runs `firstrun.sh` exactly once, so they never will. `wk rm` and `wk new` is
  the fix; a `wk enter` that writes a missing marker would be one line and was
  deliberately not added — one mechanism, written by provisioning, is easier
  to reason about than two.
- **`docs/HANDOFF-claude.md`** is unblocked by this work: "every skill invokes
  a deterministic tool rather than freehand steps" depends on this interface,
  which now exists on both platforms.
