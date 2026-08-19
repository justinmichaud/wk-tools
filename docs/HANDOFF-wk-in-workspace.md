# HANDOFF — `wk` inside a workspace

**Status: done and verified on both halves (2026-08-18).** The Linux container
half landed first, from the workstation; the macOS guest half — which was
implemented but never run, and was lane B's step 1 — is verified from the macOS
host, together with the bash 3.2 parse the Linux side could not do. Discovered
2026-08-18 while working the macOS MiniBrowser lane
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

## What the macOS half needed on top

Four things, found while verifying the guest and fixed there rather than worked
around:

7. **The build was sized from a third of the guest.** `t_cores`/`t_mem_mb` read
   the cgroup, which is the right answer on Linux and no answer at all on
   Darwin: a macOS guest has no cgroup, so both fall through to the host
   figures, and `avail_mem_mb()`'s Darwin path then subtracts the 12 GB
   *desktop* reserve — inside a guest whose size the host had already chosen.
   Measured in a 20 GB guest: -j5 in there against -j9 for the same build driven
   from the host. `is_headless()` now counts a workspace, so the reserve applied
   in one is the small headless reserve. Both sides now say -j9.

8. **`config=` in the marker.** `wk run`/`wk test` defaulted to `jsc-release`,
   which is right in a container and impossible in a macOS guest — it can build
   nothing but the Apple ports, so a bare `wk run` went looking for a JSCOnly
   binary that cannot exist and reported the path rather than the reason. The
   marker now carries the config a bare run should use (`default_config()` in
   `lib/target.sh`), written by whoever writes the marker: `jsc-release` from
   `container/firstrun.sh`, `$WK_VM_BASE_PREBUILD` from `targets/vm.sh`.

9. **The marker is written on boot as well as on sync.** `t_sync_tools` alone
   leaves a real gap: a guest that is booted and handed straight to `wk claude`
   has had no host-side build, so it has no marker — and that agent's only way
   to build is this interface. Both callers now go through one `_write_marker`
   in `targets/vm.sh`; verified by booting a guest whose marker had been deleted
   and finding it written by `wk vm start` alone.

10. **`--dry-run` on `wk build` and `wk test`**, which is what makes the guest
    half checkable at all: a cold `mac-release` is ~99 minutes, so "did it
    resolve the right target, config, tree and job count" needed an answer that
    is not a build. It stops on the line between resolution and mutation — no
    tooling sync, no status file — which for `cmd/test` meant moving that setup
    below the command construction. `--dry-run` is accepted on either side of
    the config.

Also, unrelated to the workspace but found by the same audit: **`wk report` is
now a host command.** It is `gh` and nothing else, and forwarded it ran in the
podman machine, which has neither gh nor a credential. `is_targetless` covers
the one case that needs no target at all; `wk pr` is the remaining one of this
shape and is deliberately left — it runs `git` in the current directory, so
neither the host nor the podman machine is a good answer, and it belongs to
`docs/HANDOFF-git-tools.md`.

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

## Verified, on the macOS host

In the guest `wk-mac-rel`, over ssh, with the tree rsynced in — and with **no
`WK_IN_VM=1`**, which is the whole point: every command below failed with
"podman is required" before this work.

| | |
|---|---|
| marker written by `wk vm start` alone | `name=mac-rel`, `src=/Users/admin/WebKit`, `config=mac-release`, with no host-side build first |
| `wk build --list` | works in the guest, and on the host with the podman machine **stopped** (26 ms, and it stays stopped — it used to boot it) |
| `wk build mac-release --dry-run` | target `local`, `src=/Users/admin/WebKit`, `WebKitBuild/Release`, `WEBKIT_OUTPUTDIR`/`WK_DERIVED_DATA` set, **-j9** |
| `wk build --dry-run mac-release` | same, flag on either side of the config |
| `wk run -- -e 'print(2+2)'` | `4` — the real `jsc` from the Release tree, config taken from the marker |
| `wk test --dry-run`, `--layout --dry-run` | resolved suite and command, both carrying `WEBKIT_OUTPUTDIR` |
| `wk status`, `wk logs` (no name) | report this workspace |
| `wk build other-ws mac-release` | names the missing workspace and prints the in-workspace form |
| refusals | `new`, `rm`, `sync`, `gc`, `verify`, `claude`, `enter` all refuse, each naming the host |
| host path unchanged | `wk build mac-rel mac-release --dry-run` and `wk run mac-rel --config mac-release -- -e 'print(6*7)'` → `42` from the host |
| dispatch, host | with a fake `podman` on PATH: a container-target command still forwards, `build --list` never calls podman, `default_target` is `container` |
| **`/bin/bash -n`** | every non-doc file the change touches, under bash 3.2.57 as well as bash 5 — the check the Linux host could not run |

A full `mac-release` was deliberately not run: ~99 minutes, and `--dry-run`
exists so it does not have to be. The container half was re-run here too, on the
merged tree: real in-workspace `wk build jsc-release` (15 s incremental,
BUILD OK), `wk run -- -e 'print(2+2)'` → 4, status, and the refusals.

## What is left

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
  doing deliberately, not as a side effect of this change. (This is a
  violation of the one-copy-per-machine rule in
  `docs/HANDOFF-workspace-state.md` — that file's rules govern.)
- **Two adjacent macOS-host defects from the original report, still open.**
  `wk` auto-starts the podman machine for any container-target command even when
  a macOS VM is running (and on a 32 GB host the two cannot coexist) — it should
  report and let the user choose. `is_targetless` only removes the case that
  needed no target at all. And `wk status` with no argument should show macOS
  VMs as well as containers; `cmd/status` now walks every registered target,
  which looks right and has not been checked on macOS.
- **`wk ls` inside a workspace** prints `?` for BASE and `-` for CHANGES.
  Honest — both are overlay concepts that do not exist for this target — but it
  reads as missing data rather than as not-applicable.
- **`docs/HANDOFF-claude.md`** can now be picked up: "every skill invokes a
  deterministic tool rather than freehand steps" depends on this interface
  existing, and it now does on both platforms.

## Open questions

- [ ] Does the `remote` target need the same treatment, or is it host-driven by
      definition? (`wk claude` already refuses a remote workspace outright —
      `docs/TESTING.md` §3.) Unchanged by this work.
- [ ] Workspaces created before the marker existed do not have one, and
      `.wkdev-init` runs `firstrun.sh` exactly once, so they never will.
      `wk rm` and `wk new` is the fix; a `wk enter` that writes a missing marker
      would be one line, and was deliberately not added — one mechanism, written
      by provisioning, is easier to reason about than two.
