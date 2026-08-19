# HANDOFF — `wk` inside a workspace

**Status: the Linux container half is done and verified (2026-08-18). The macOS
guest half is implemented and unverified — that is lane B's step, below.**
Discovered 2026-08-18 while working the macOS MiniBrowser lane
(`docs/HANDOFF-mac-minibrowser.md`, item group I).

Claude only ever runs inside a wk workspace — that is the sandbox boundary and
the whole point of the setup — so it has to be able to drive its own building
and testing from in there. The global `CLAUDE.md` documented that interface and
it did not exist. It does now:

```sh
wk build jsc-release        # no workspace name: the checkout it is in
wk run -- -e 'print(1+1)'
wk test <args>
wk status                   # this workspace's own build and test state
wk logs -f
```

---

## What the Linux half actually was

Measured before implementing, since the original was written from the macOS
guest and the container case was explicitly unknown:

- `wk` **is** present in a container, at `/opt/wk-tools`, bind-mounted
  read-only by `t_create`. Nothing had to be installed.
- Nothing forwards: the podman-machine forward is `is_macos`-gated, so the
  "podman is required" failure the macOS guest hits does not happen here.
  `wk ls`, `wk status` and `wk build --list` already ran.
- But the interface was still missing in the same way: `wk build jsc-release`
  printed `usage: wk build <workspace> <config>`, and `wk run -- -e ...` failed
  on `invalid name '--'`. `wk ls` reported no workspaces at all, because the
  store it reads is the host's and is not mounted in.

So the fix took the shape the design called for, unchanged.

## What was built

1. **A workspace marker.** `~/.wk-workspace`, `key=value`, naming the workspace
   and its checkout. Written by `container/firstrun.sh` for a container and by
   `targets/vm.sh`'s `t_sync_tools` for a macOS guest. `in_workspace`,
   `wk_marker_field`, `wk_self` and `default_target` in `lib/target.sh` are the
   only readers. A file rather than an environment variable, because it has to
   be true for anything that reaches in — an agent's `bash -c`, a hook, ssh —
   and not only for shells that inherited the right environment.

   It lives in the workspace's home, which for a container is `$ws/home` on the
   host and *not* the host's own `$HOME`. Verified: after creating a workspace,
   the host still has no `~/.wk-workspace`, which is what keeps host behaviour
   byte-for-byte unchanged.

2. **`targets/local.sh`** — the driver where the target is this machine.
   `t_src`/`t_tools` from the marker and `$WK_ROOT`; `t_exec` through a login
   shell (the proxy variables and the PATH additions are in the profile, not in
   this process); `t_cores`/`t_mem_mb` from the cgroup, so a workspace capped
   tighter than its host does not size a build from the host's cores;
   `t_create`, `t_destroy` and `t_enter` refuse.

3. **Dispatch.** `wk` selects `local` when the marker is present and never
   forwards; `default_target()` replaces the hardcoded `container` fallback in
   every command that resolves a workspace, so the same is true when a `cmd/`
   file is run directly. With the marker absent, nothing changed.

4. **Optional workspace argument** in `cmd/build`, `cmd/run`, `cmd/test` and
   `cmd/logs`. The leading argument is consumed as a name only when it is this
   workspace's own name — and in `wk build`, only when that word is not also a
   config, so a workspace named after one still builds. `wk build <other>
   <config>` from inside names the missing thing instead of reading a workspace
   as a config.

5. **`wk build --list` needs no target** and no longer forwards (`is_targetless`
   in `wk`), which is what made it demand podman on a macOS host and fail with
   an error naming the wrong problem.

6. **Refusals, so nothing lies or acts on the wrong machine.** `wk verify`
   refuses from inside: its checks are gated on `WK_SANDBOX`, which the local
   driver does not set, so it reported **"sandbox intact" after measuring two
   things and skipping the boundary entirely** — found by running it. `wk
   claude` refuses (it would nest a relaxed-permission agent inside a sandbox
   nothing re-measured). `wk new`/`wk rm` refuse, `wk rm` before prompting. And
   host-only commands — `sync`, `gc`, `session`, `quiesce`, `vm`, `pi`,
   `stop`, `start`, `setup`, `backup`, `mcp` — refuse rather than no-op against
   an empty store; `wk sync` would otherwise fetch a 13 GB mirror into a
   directory that is discarded with the workspace.

## Verified, on the Linux workstation

In a fresh `wk new selftest`, so provisioning is what wrote the marker:

| | |
|---|---|
| marker written by firstrun | `name=selftest`, `src=/src/WebKit`; host `$HOME` still has none |
| `wk build jsc-release` (inside) | BUILD OK, 1m16s, `-j75` from the cgroup's 79 cores / 115805 MB |
| `wk run -- -e 'print(1+1)'` | `2` |
| `wk run --config jsc-release -- ...`, `wk run selftest -- ...` | both work |
| `wk test --no-jsc-stress ...` (testmasm only) | TESTS OK, 15s |
| `wk status`, `wk logs` (no name) | this workspace's build=ok and test=ok |
| every refusal above | refuses, with the reason |
| `wk build --list` | works inside and out |
| host, marker absent | `wk build --list`, `wk ls`, `wk status`, `wk build selftest jsc-release` (15s incremental) unchanged; `wk verify selftest` still green |
| dispatch, host | `in_workspace=no` for every invocation; `resolve_target` unchanged; only `build --list` newly skips forwarding |
| `bash -n` | every touched file |

## What is left

- **The macOS guest half — lane B.** `targets/vm.sh` now writes the marker from
  `t_sync_tools` (not at creation: a clone is not booted until `wk vm start`, so
  `t_create` cannot reach in; and not into the golden base, which is not a
  workspace and whose marker every clone would inherit). Nothing about it has
  been run. Verify as `docs/HANDOFF-mac-minibrowser.md` describes: rsync the
  tree in, then `wk build --list`, `wk run`, `wk test` with **no `WK_IN_VM=1`
  and no podman error**. Do not run a full `mac-release` to prove it — verify as
  far as config, target and job count.
- **bash 3.2 was not checked.** The repo invariant is `bash -n` *and*
  `/bin/bash -n`, and on this Linux host both are bash 5.2. The additions use
  nothing newer (no arrays, no `[[ =~ ]]`, no `${x^^}`), but the 3.2 parse is
  lane B's to run.
- **Build state is recorded twice, once per side.** A workspace writes
  `build.status` and `build.log` into its own `~/.local/state/wk/ws/<name>/`,
  because the host's store is not mounted in — so `wk status <ws>` on the host
  says `build=none` while `wk status` inside the same workspace says
  `build=ok`. Both are honest about what they can see, and neither is the single
  machine-readable answer `wk status` exists to give. The fix is to give the two
  sides one file, and the obvious way — bind-mounting the host's `$ws` into the
  workspace — is wrong as stated: `$ws` also holds `changes/` and
  `overlay-work/`, and a second write path into the upperdir of a live overlay
  is exactly what `lib/store.sh` warns is undefined. A dedicated
  `$ws/state:/wk-ws` would work, but the host reads `$ws/build.status` directly
  through `wk_ws_dir`, so it means moving those files for every target. Worth
  doing deliberately, not as a side effect of this change.
- **Two adjacent macOS-host defects from the original report, still open.**
  `wk` auto-starts the podman machine for any container-target command even when
  a macOS VM is running (and on a 32 GB host the two cannot coexist) — it should
  report and let the user choose. `is_targetless` only removes the case that
  needed no target at all. And `wk status` with no argument should show macOS
  VMs as well as containers; `cmd/status` now walks every registered target,
  which looks right and has not been checked on macOS.
- **`docs/HANDOFF-claude.md`** can now be picked up: "every skill invokes a
  deterministic tool rather than freehand steps" depends on this interface
  existing, and it does on Linux.

## Open questions

- [ ] Does the `remote` target need the same treatment, or is it host-driven by
      definition? (`wk claude` already refuses a remote workspace outright —
      `docs/TESTING.md` §3.) Unchanged by this work.
- [ ] Workspaces created before the marker existed do not have one, and
      `.wkdev-init` runs `firstrun.sh` exactly once, so they never will.
      `wk rm` and `wk new` is the fix; a `wk enter` that writes a missing marker
      would be one line, and was deliberately not added — one mechanism, written
      by provisioning, is easier to reason about than two.
