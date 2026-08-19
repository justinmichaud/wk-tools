# HANDOFF — workspace lifecycle state, readiness gating, and a status command that cannot lie

Written 2026-08-19. Grew out of three features that landed the same day —
`wk new --zed`, `wk build --babysit`, `wk build --branch` — and the question
they all forced: *when is a workspace ready to be acted on, and who is allowed
to say so?* Today the honest answer is "nobody, reliably": zed opens on
whatever exists, a build starts on whatever exists, and an ssh cut in the
middle of `wk new` leaves a half-workspace that every command treats as whole.

---

## The core requirement (read this before the plan)

**This is the requirement the rest of the document serves, and it is not
negotiable down to an implementation detail: `wk status` must be reliable and
clear under every failure the system can actually have.** Everything else here
— markers, staged creation, detached drivers — exists so that this command can
keep that promise. If a proposed mechanism makes status less trustworthy, the
mechanism is wrong, not the requirement.

Concretely, `wk status` (and everything that gates on the same state) must
stay correct, or degrade *visibly and honestly*, across:

- **Updates to this repo.** A status file written by last month's wk-tools is
  read by today's. So: unknown keys are ignored, missing keys mean "unknown"
  rather than a parse error, and no reader ever assumes a file's schema is the
  one it would write itself. A reader that cannot make sense of a file says
  so, names the file, and continues with the rest of the report — one strange
  workspace must not take down the whole listing.
- **OS updates and reinstalls.** Nothing load-bearing lives only in a process
  table, an environment variable, or a tmpfs. After a reboot or a reinstall,
  state is recomputed from what is on disk next to the artifacts. (This is the
  same rule [[no-in-place-upgrades]] already applies to machines: the record
  must survive the runtime.)
- **Network drops and ssh cuts.** A target that cannot be reached is reported
  as *unreachable, with the timeout that decided it* — never a hang, never
  `absent`, never a stale answer presented as current. Every probe is
  bounded (`ConnectTimeout` is already house style; it becomes a requirement).
  And an operation whose *driver* died with an ssh session must be
  distinguishable from one that is running: liveness is the process table plus
  the evidence's freshness, never the status file's own claim.
- **Random device corruption.** A truncated, half-written, or garbage status
  file is an expected input, not an exception: writes are atomic
  (tmp + `mv` on the same filesystem), reads are tolerant, and — the real
  defense — **a status file is a cache, never the record.** The record is
  evidence next to the artifact (a marker written as creation's last act, a
  git object store that either passes `rev-parse` or does not, a log whose
  mtime is its heartbeat). When cache and evidence disagree, evidence wins and
  status says the file was stale.
- **Clock trouble.** Timestamps are absolute UTC and used for display and
  staleness heuristics only; no correctness decision hinges on two machines
  agreeing what time it is.

The contract in one line: **`wk status` never hangs, never crashes, and never
asserts something it did not just verify; in the worst case it says "unknown",
why, and the one command that repairs it.** Exit codes stay machine-readable
(the current 0/1/2/3 contract extends; unreachable and broken get codes of
their own so scripts and agents can branch).

---

## What exists today (landed 2026-08-19)

- **`wk build --babysit[=model]`** — a detached loop (`build/babysit.sh`,
  nohup, no tty, survives ssh cuts) that rebuilds after every fix a sandboxed
  Claude makes: build → on failure, headless `wk claude -p` (default `haiku`,
  `WK_BABYSIT_MODEL`/`--babysit=<m>` override) with the classified errors and
  log tail in the prompt → rebuild, up to `WK_BABYSIT_ATTEMPTS` (default 5).
  Writes `babysit.status` / `babysit.report` / `babysit.log` in the workspace
  state dir; `wk status` renders them with pid-liveness (a "fixing" claim with
  a dead pid is reported as a crash, not believed). Refuses remote targets
  with the same words as `wk claude` (no sandbox on a shared machine) and
  refuses from inside a workspace. A second `--babysit` while one is alive is
  refused by pid. **Not yet run through a real fix cycle** — that E2E (plant a
  build error, watch it get fixed) is the first verification item below.
- **`wk claude` headless** — no tty on stdin/stdout → `t_exec` instead of
  `t_exec_tty`, same sandbox verification either way. This is why the
  babysitter goes through `wk claude` rather than running claude itself.
- **`wk build --branch <b>`** — checkout before build, fetch only when the
  name is not already local; in the babysit path the checkout happens once,
  up front, never under a fix the model just made.
- **`wk new --zed` / `wk enter --zed`** — via `zed_cli()` (PATH entry or the
  app bundle's own cli). *Currently opens as soon as creation returns*, which
  is exactly the gap this handoff exists to close.
- The prior art the state model generalizes: `build.status` (whose `running`
  is already treated as a hint, with log mtime deciding liveness),
  `.wk-firstrun-complete` (a completion marker written as provisioning's last
  act, waited on by the container driver's `t_ready`), and `remote/provision.sh`
  (idempotent stages that check evidence before acting).

## The gaps

1. **Readiness is only half a concept.** Container creation is async and has
   a real marker; `t_ready` is a no-op for vm and remote; and *nothing gates
   on readiness* — not zed, not build, not babysit.
2. **Remote creation is one long synchronous ssh.** A cut mid-`wk new` leaves
   a partial clone that `t_info` calls `present`; `wk new` then refuses
   ("already exists"), `wk build` builds rubble, and only a by-hand `wk rm`
   recovers.
3. **Creation state does not exist at all** — there is no way to tell
   "creating, driver alive" from "creating, driver died with my ssh session"
   from "done".

## The plan

Order chosen so every step ships alone and the fragile target (remote) is
fixed first.

1. **Evidence at the artifact: a `.wk-ready` marker per workspace,** written
   as the *last* act of creation by every driver — the firstrun pattern,
   promoted to the contract. Next to the checkout (far side for remote/vm,
   the ws home for containers), so it survives anything that only kills the
   driving side.
2. **`t_info` grows one state: `absent | creating | present`.** `creating` =
   the workspace directory exists and the marker does not. Derived on every
   call, stored nowhere.
3. **One gate: `wait_ready <name> [timeout]`** in `lib/target.sh` — polls
   `t_info`, says what it is waiting for, dies honestly on timeout, and on
   creating-with-dead-driver points at `wk new <name>` to resume. Consumers:
   - `wk enter --zed` / `wk new --zed`: wait, *then* exec zed. The waiter
     stays **foreground** deliberately — if ssh dies, only the waiter dies;
     creation continues detached and re-running `wk enter --zed` is the
     resume. A detached GUI-opener would open windows into a session that no
     longer exists.
   - `wk build`: wait (bounded) before writing `state=running` — a build can
     no longer start on a half-initialised checkout. `--babysit` inherits the
     gate because it re-runs `wk build`.
4. **Creation becomes staged and resumable.** Each driver's `t_create` splits
   into idempotent stages that check evidence before acting: a mirror fetch
   resumes by nature; a clone directory without its marker is rubble and is
   remade; provisioning steps already self-check. `wk new <name>` on a
   `creating` workspace with a dead driver **resumes** instead of refusing.
   An ssh cut costs a re-run of the same command, nothing else. Remote first.
5. **One detach primitive, `lib/detach.sh`,** extracted from the babysitter
   (its status-file schema, pid-liveness, nohup discipline): `detach_run
   <status-file> <log> -- cmd…`. Second user: `wk new` itself — creation
   detaches by default and the command waits in foreground (`--no-wait` to
   return immediately). `ws.status` (schema below) is written by the detached
   driver; readers treat it as the cache it is.
6. **Status renders the lifecycle first.** `wk status` gains the workspace
   state line (`creating` with driver liveness and the current stage,
   `broken` with the repair command) above the existing build/test/babysit
   lines, under the reliability contract at the top of this file.

Follow-up, explicitly out of this pass: remote *builds* still hold an ssh
open end-to-end. Moving them under the machine-side `wk` that
`wk remote setup` installs closes the last ssh-cut hole and resolves the
"build state recorded twice" leftover in `docs/HANDOFF-wk-in-workspace.md`.

## The status-file schema (shared by ws/build/test/babysit)

Plain `key=value`, one file per concern, single writer, atomic tmp+`mv`,
absolute UTC timestamps, unknown keys ignored by every reader:

    state=…            # a hint; evidence decides
    pid=…              # the driving process, for liveness — this host only
    log=… report=…     # where the words went
    updated=…          # staleness display, never correctness
    (+ concern-specific: config, model, attempt/max, branch, stage)

## Branch semantics

`wk new --branch <b>` clones directly onto `<b>` and records it as the
workspace default; `wk build --branch <b>` overrides per build (landed). A
bare `wk build` never touches the checkout — the user's working tree is
sacred.

## Verification (docs/TESTING.md items to add as each step lands)

- The babysit E2E: plant a compile error, `wk build <ws> <cfg> --babysit`,
  disconnect the terminal, reconnect; the error is fixed, the build is green,
  `wk status` showed building→fixing→building→ok throughout, and the report
  says what was changed.
- Kill `wk new` mid-clone (remote): `wk status` says creating-with-dead-driver
  and names the resume; `wk new <name>` resumes; the workspace ends `present`
  with the marker.
- Corrupt each status file (truncate, garbage): status reports the file as
  stale/unparseable, keeps listing everything else, and the evidence-derived
  answer is unchanged.
- Unplug the network mid-`wk status` against a remote machine: bounded wait,
  "unreachable" with its timeout, distinct exit code, no hang.
- A status file written by the previous schema (no new keys) still renders.
- `wk enter --zed` against a `creating` workspace waits and opens only on
  `present`; against `broken` it refuses with the repair command.

## Open questions

- Does `creating` need sub-stages surfaced in status (`clone 4.2G…`,
  `provisioning`), or is the driver's log line enough? (Lean: show the stage
  name from `ws.status`, point at the log for detail.)
- `wait_ready` default timeout: creation of a remote workspace legitimately
  takes 30+ minutes on a first mirror clone. Probably: no timeout when the
  driver is alive, fail fast when it is dead.
- Should `wk rm` of a `creating` workspace kill the driver first? (Lean:
  yes, and say so.)
