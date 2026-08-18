# HANDOFF — `wk` does not work inside a workspace

**Status: broken, blocking, not started.** Discovered 2026-08-18 while working
the macOS MiniBrowser lane (`docs/HANDOFF-mac-minibrowser.md`, item group I).

Two adjacent host-side defects, same dispatch logic, fix together:

- `wk` auto-starts the podman machine for any container-target command, even
  when a macOS VM is running (and on a 32 GB host the two cannot coexist).
  It should report and let the user choose, not auto-start.
- `wk status` with no argument should show macOS VMs too, not just containers.

---

## Why this is a blocker and not a papercut

Claude **only ever runs inside a wk workspace** — a macOS Tart guest or a Linux
container. That is the sandbox boundary and the whole point of the setup:
Claude never runs on the host. The direct consequence is that **Claude must be
able to drive its own building and testing from inside the workspace.**

The global `CLAUDE.md` already documents that interface, and tells every agent
to use it:

    wk build <config>        # note: NO workspace name -- the checkout it is in
    wk run -- <args>
    wk test <args>

That interface does not exist. So `wk claude <ws>` puts Claude in a correctly
sandboxed guest and then leaves it unable to build or test anything. Its only
escape is to hand-roll `Tools/Scripts/build-webkit`, which `CLAUDE.md` forbids
precisely because the wrapper carries the job-count and nice-level policy that
stops a WebKit link step from taking the machine down.

**Do not "fix" this by documenting that builds are driven from the host.** That
breaks the sandbox model rather than serving it.

---

## What actually happens (measured in the running macOS guest)

`wk` is present in the guest at `/Users/admin/wk-tools` (rsynced by
`_prebuild_base`). Every command fails:

    $ ./wk ls
    error: podman is required; install the official pkg from podman.io
    $ ./wk build --list
    error: podman is required; install the official pkg from podman.io

The mechanism, in `wk` (by function, since line numbers drift):

- `main()`'s dispatch guard:
  `if is_macos && [ -z "${WK_IN_VM:-}" ] && ! is_host_command "$cmd"` — then if
  `resolve_target` says `container`, forward.
- `resolve_target()` falls through to `printf '%s' container`
  whenever no workspace name resolves — **including when no name was given at
  all**, which is exactly the in-workspace form.
- `forward_to_vm()` opens with `require podman ...` and then
  `podman machine ssh`.

So inside a macOS guest, `wk <anything>` tries to forward into a podman machine
that is not there and never will be. `WK_IN_VM=1` suppresses forwarding
(it exists so the podman-VM-side `wk` does not re-forward), and
with it some commands work:

    $ WK_IN_VM=1 ./wk build --list     # works, prints the config table
    $ WK_IN_VM=1 ./wk build mac-release
    usage: wk build <workspace> <config>

i.e. even with forwarding suppressed, the **bare `wk build <config>` form is
unsupported**: `cmd/build`'s argument parsing requires `<workspace> <config>`.

The guest also has **no podman, no tart, and `kern.hv_support = 0`** — no
nested virtualisation — so it can never host a workspace of its own. "Just run
a target driver in there" is not available.

---

## Design to implement

1. **A workspace self-marker.** A small file that says "this machine *is* a
   workspace" and names its checkout — e.g. `/etc/wk-workspace` or
   `~/.wk-workspace` containing the source path. Written by provisioning:
   `vm/provision-base.sh` for the macOS guest, and the container equivalent
   (check `container/firstrun.sh`).

2. **A `local` target driver — `targets/local.sh`.** Same contract as
   `targets/container.sh` / `targets/vm.sh`; the contract and its defaults are
   documented at the top of `lib/target.sh`. It means
   "the workspace is this machine":
   - `t_src` — from the marker
   - `t_exec` / `t_exec_tty` — run locally through a **login shell** (the
     proxy env and PATH additions live in the profile; see
     `vm/provision-base.sh`)
   - `t_tools` — `$WK_ROOT`; `t_sync_tools` — a no-op
   - `t_info` — running; `t_cores`/`t_mem_mb` — from `lib/resources.sh`
   - `t_create` / `t_destroy` — **must refuse.** A workspace may not create or
     destroy itself.

3. **Entrypoint.** In `wk`, when the marker is present: never forward to the
   podman machine, and default the target to `local`. **When the marker is
   absent, host behaviour must be byte-for-byte unchanged.**

4. **Optional workspace argument** in `cmd/build`, `cmd/run`, `cmd/test`: when
   inside a workspace and the first argument is a known config (or is absent,
   or is `--`), treat it as the config and supply the implicit workspace.
   Outside a workspace, `wk build <workspace> <config>` must keep working
   exactly as today. Disambiguate by asking `config_load` whether the argument
   is a real config — it returns non-zero for unknown — rather than
   pattern-matching names.

5. **Fix the misleading gate.** `wk build --list` needs no target and must not
   require podman on any machine. Today it does, and the error names the wrong
   problem entirely. Check for other target-free commands with the same bug.

---

## Constraints

- Everything must parse under **bash 5 and bash 3.2** (`bash -n` *and*
  `/bin/bash -n`). This is an existing repo invariant — `docs/TESTING.md`
  lists it as a permanent check.
- **Do not start the podman machine.** The `wk` entrypoint auto-starts it for
  container-target commands, which is easy to trip accidentally (running
  `./wk build --list` on the host does it). Guard host-side invocations with
  `WK_IN_VM=1`, or exercise the dispatch logic directly in bash.
- On a 32 GB host the podman machine and a macOS guest **cannot both run** —
  the podman machine alone holds the whole 20480 MB envelope
  (`targets/vm.sh`, `_check_memory_budget`).
- House style: comments explain *why*, not *what*. Match `wk`, `cmd/build`,
  `targets/vm.sh`.

## Verification required

A claim without evidence is not done.

- `bash -n` and `/bin/bash -n` on every file touched.
- Host, marker absent: `wk build --list` works **with podman stopped**; and
  show that dispatch for a container-target command is unchanged *without
  executing it* (trace `resolve_target` / `is_host_command` directly).
- In a macOS guest: rsync the tree in
  (`rsync -az --delete --exclude '.git/' -e ssh ./ wk-mac-rel:/Users/admin/wk-tools/`)
  and show `wk build --list`, `wk run` and `wk test` behaving sensibly with
  **no `WK_IN_VM=1` and no podman error**.
- Do **not** run a full build to prove it: a cold `mac-release` is ~99 min.
  Verify as far as config, target, job count and the command it *would* run —
  add a dry-run path if none exists.

## Open questions

- [ ] **The Linux container case is unverified.** Everything above was measured
      in the macOS guest; podman was stopped throughout for the macOS lane. It
      is not known whether `wk` is even present inside a container, or whether
      the container half already has a working in-workspace path. **Establish
      this before implementing** — it may change the shape of the fix.
- [ ] Does the `remote` target need the same treatment, or is it host-driven by
      definition? (`wk claude` already refuses a remote workspace outright —
      `docs/TESTING.md` §3.)
- [ ] Once this lands, `docs/HANDOFF-claude.md` should be revisited: it already
      calls for making every skill invoke a deterministic tool rather than
      freehand steps, which depends on this interface existing.
