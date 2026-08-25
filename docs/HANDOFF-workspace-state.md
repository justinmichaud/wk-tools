# HANDOFF — workspace state and readiness: the open list

Readiness gating, the status-file schema, the locks and the clobber detection
are all built. The rules behind them — smallest state, crash-only, wipe over
repair, one lock per mutated resource, detect un-managed clobbering — live in
`CLAUDE.md` and bind the whole repo.

`docs/TESTING.md` is the verification authority: the "Babysit" subsection of §1
and §6 "State, concurrency and clobbering" (the per-command interruption matrix,
the status-files-are-claims checks, the lock checks, the clobber-detection
checks). Nothing is listed here so the two cannot drift.

## Remaining

(Fixed since this list was written, and noted only to say where each rule lives
now: `wk quiesce`'s state directory is `wk_state_dir`-based and `off` also
undoes a session left under the old `$TMPDIR` path; `build_live`
(`lib/detach.sh`) is the one answer to "is that build running", used by both
`wk status` and `wk bench`'s preflight; `headless_markers`
(`lib/resources.sh`) is the one spelling of the headless marker; `wk vm rm`
removes `.unfiltered`; `wk gc` prunes orphaned creation records; and
`ccache_conf_render` (`lib/store.sh`) is the one ccache ceiling.)

- **Babysit has never run a real fix cycle.** `wk build --babysit[=model]` is
  built — a detached loop that rebuilds after every fix a sandboxed Claude
  makes, up to `WK_BABYSIT_ATTEMPTS` — and eight of its TESTING.md lines are
  unticked, including the E2E: plant a compile error, disconnect the terminal,
  watch it get fixed. Also untested: a stalled build (exit 124) ending
  `stalled` rather than being handed to the model, claude failing to *run*
  ending `error` rather than retrying forever, and a `fixing` claim with a dead
  pid being reported as a crash.
- **Build state is recorded twice for a container workspace.** It writes
  `build.status`/`build.log` into its own `~/.local/state/wk/ws/<name>/` because
  the host's store is not mounted in, so `wk status <ws>` on the host says
  `build=none` while `wk status` inside the same workspace says `build=ok`. The
  obvious fix is wrong: `$ws` also holds `changes/` and `overlay-work/`, and a
  second write path into a live overlay's upperdir is undefined. A dedicated
  `$ws/state:/wk-ws` mount would work, but the host reads `$ws/build.status`
  directly through `wk_ws_dir`, so it means moving those files for every target.
  Do it deliberately, not as a side effect — the one-copy-per-machine rule
  governs.
- **Remote builds still hold an ssh open end to end.** ssh without a pty carries
  no signal, so ctrl-c — and the watchdog's stall abort — ends the local half
  while the compiler keeps going at the far end, and the build lock makes the
  next `wk build` wait behind it, which reads as a hang. The fix is a
  remote-side pid file and a `wk build --abort`, not a shorter lock timeout;
  moving the build under the machine-side `wk` that `wk remote setup` installs
  closes the last ssh-cut hole.
- **A delegated `wk status` is answered by the far machine's own wk-tools**, so
  a build box running an older copy answers by the older rules — measured: a
  workspace whose creation had died read `present` from over there and
  `creating` here. The fleet block prints that the tooling differs, which is the
  honest half; what is missing is saying that the drift changes *answers*, not
  just versions.
- **Smaller**: `wk` auto-starts the podman machine for any mutating
  container-target command even when a macOS VM is running (on 32 GB the two
  cannot coexist — it should report and let the user choose); `wk ls` inside a
  workspace prints `?` for BASE and `-` for CHANGES, which is honest but reads
  as missing data rather than not-applicable.
- **No remote or guest workspace has been created end to end** in a state pass —
  the container has been, including a driver killed mid-provisioning and remade.

## Open questions

- Does `creating` need sub-stages surfaced in status (`clone 4.2G…`,
  `provisioning`), or is the driver's log line enough? (Lean: show the stage
  name from `ws.status`, point at the log for detail.)
- `wait_ready` default timeout: creating a remote workspace legitimately takes
  30+ minutes on a first mirror clone. Probably no timeout while the driver is
  alive, fail fast when it is dead.
- Should `wk rm` of a `creating` workspace kill the driver first? (Lean: yes,
  and say so.)

## The status-file schema — the contract to keep

Plain `key=value`, one file per concern, single writer, atomic tmp+`mv`,
absolute UTC timestamps, unknown keys ignored by every reader:

    state=…            # a hint; evidence decides
    pid=…              # the driving process, for liveness — this host only
    log=… report=…     # where the words went
    updated=…          # staleness display, never correctness
    (+ concern-specific: config, model, attempt/max, branch, stage)

Written and read through `status_write` / `status_field` / `detach_alive` in
`lib/detach.sh`, so "a file written by last month's wk-tools still renders" is a
property of two functions rather than of every caller. The single-writer rule is
enforced by the lock: whoever holds the resource's lock writes its status file —
which is why `wk new`'s waiting half writes nothing at all. `ws.status` sits
*beside* the workspace directory, not inside it, so it survives the artifacts.

A build's status file deliberately carries **no pid**: a build can be driven
from either end of an ssh and a pid written by one machine is not a fact on the
other, so liveness there is the age of the build log.

## Branch semantics

`wk new --branch <b>` clones directly onto `<b>` and records it as the workspace
default; `wk build --branch <b>` overrides per build. A bare `wk build` never
touches the checkout — the user's working tree is sacred.
