# The re-architecture: how it lands

README.md's first half says what `wk` is. This file is the order of work
that gets the tree there, and what proves each step. It is deleted when the
last step lands.

## Where we are

Measured 2026-09-21 on this workstation.

| | |
| --- | --- |
| code | 38k lines of bash, 9k of Python (the dispatcher among it); 41 commands, 7 of them over 500 lines and holding 55% of command code |
| tests | 70k lines, 188 modules, 4273 tests in the lint and unit tiers, 39 in the live tier |
| `wk selftest` (lint then unit) | 13.7 minutes, green; the lint tier alone 27 s |
| the slowest unit test | 16 s, under a 30 s budget the runner enforces |
| owed tests | 8 |
| places a test starts a process | 1,600 |

Why bugs come back:

1. **Every command is its own program.** Nine ssh wrappers, three ways to
   hold a lock, eleven wait loops, four sets of loggers, about twenty inline
   JSON readers, an exit-code vocabulary per command (3 means "no answer" in
   four commands and "stalled" in a fifth). A fix lands in one copy.
2. **The only seam is the process boundary.** A test of one rule forks
   `./wk`, which sources 2,500 lines of bash and probes machines, and waits
   in real time; or it lifts one function out of a file with `sed` and runs
   it under `bash -c`. Tools are faked four ways (a stub on `PATH`, a shell
   function shadowing the real one, an environment variable pointing at a
   dead port, a hand-written executable) and no production code has a test
   seam. A rule that does not show at the process boundary is not tested.
3. **`act` and `t_*` are asked for, not enforced.** `stop`, `start`, `gc`,
   `doctor`, `bridge` and `pi` call podman, ssh and rm directly. `--dry-run`
   is a per-command promise, and 25 commands refuse it.
4. **Tests remember incidents.** Names carry the bug that was found by hand;
   dated measurements sit in comments. The push switch is tested in nine
   modules and the credential store in ten, because each agent wrote its own. The
   design lives in 2,000 lines of prose and each agent reads a different
   part of it.
5. **Hardware code has never run under a test.** The card edits, the
   two-system layouts, the rpi4 boot path, every `b_*` driver: each fails on
   the board, where a fix costs a walk to a card reader.

## Where we are going

Ten rules. Each is held by a test, not by asking.

1. **One program.** `wk` is one Python program. Python is on every host and
   image already, and the scheduler, status renderer, credential rules and
   bench records are already Python. Bash stays only where the shell is the
   point: what runs inside a target or on a board's first boot (the guard,
   the first-run script, the self-disarm), each small and each run by the
   live tier.
2. **A command is data.** A command is a module that declares the shape
   README's "Every command, the same way" names. The dispatcher is the only
   argv parser, the only help, the only prompt, the only exit-code table.
3. **One seam for every effect.** Running a process here or on a machine,
   copying bytes, touching a file outside the tree, sleeping: all of it goes
   through one `Machine` object. One implementation per target kind, and one
   fake. `--dry-run` is the command run against the recorder. Nothing else
   can act, so nothing bypasses `act`.
4. **Time is a parameter.** `clock.now()` and `clock.sleep()`; a test
   advances the clock. Every timeout in the tree is tested in milliseconds.
5. **One record, one lock.** The task record (kind, plan, steps, holds,
   kill, log, machine, pid) is the only state that outlives a command. A
   build budget, a device claim and a workspace lock are all `holds` on one.
   Liveness is asked one way.
6. **One fleet.** `machines/<name>.conf` with a `kind` replaces the three
   directories of machine files. A machine is reached by tailnet name and
   probed once per invocation. A `case` on a machine name fails lint.
7. **One bench pipeline.** Build, deploy, run, record, report. A Pi board, a
   Mac volume, a Mac guest and a container are bench systems behind one
   interface: boot, deploy, run, collect. One result record, one report.
8. **Three test tiers, one runner, budgets enforced.**
   - *unit*: fake machine, fake clock. The tier runs in under 60 s. A test
     over 0.5 s fails.
   - *lint*: the static rules, one pass over the tree, seconds.
   - *live*: the same tests with a real machine underneath, `wk selftest
     --live [<machine>]`. A test names what it needs and skips by name.
   A bug is fixed by a unit test that fails first. No test asserts prose.
9. **Owed work is a test.** An owed behaviour is a test marked `owed`. The
   runner expects it to fail and fails when it passes, naming the mark to
   remove. `wk selftest` prints the owed count. This file lists nothing a
   test does not.
10. **Docs are the spec and the examples.** README's first half is the
    design; its second half is commands by example; `wk <cmd> -h` is
    generated from the declaration. No dates, incidents or tuning stories in
    prose: a measured constant lives in code with a one-line why.

## Decisions taken

Python core, agreed 2026-09-21. Stdlib only, Python 3.9 syntax: the Mac's
system Python is 3.9, and every other host, guest and image has newer with
nothing installed. Work lands on the `python-core` branch, committed at the
end of each step and each merged command, never pushed by an agent.

Moving the core to Python is the one call that cannot be made a step at a
time later. The alternative, keeping bash and faking tools on `PATH`, does
not reach the goal: bash cannot take a clock, cannot be tested below the
process boundary, and pays 30 ms per fork, so 4,300 tests floor at two
minutes before the first real wait. The CLI, the conf files and the record
on disk do not change, so nothing a person types or a running build depends
on moves.

## Cutting it down

40k lines of bash is the problem, not the raw material. It is that big
because every command re-implements the shared concerns, because incidents
became special cases, and because 46 commands do about 20 jobs. The size is
budgeted and lint holds the budget; a change that goes over trims something.

| | today | budget |
| --- | --- | --- |
| core (`wk`, commands, drivers) | 40k bash | 10k Python |
| what runs on a board or in a target | in the 40k | 1k bash |
| tests | 70k | 15k |
| commands | 46 | 20 |
| a command | up to 1,550 lines | 200 |
| machine directories | 3 | 1 |

Where the lines go:

- **Duplication collapses to one.** Nine ssh wrappers, three locks, eleven
  waits, four loggers, twenty JSON readers and 350 `awk`/`sed` pipelines
  become one `Machine`, one record, one clock, one logger and dictionaries.
- **Commands merge by job.** One name per job, subverbs for the rest:

  | today | becomes |
  | --- | --- |
  | `remote`, `vm`, `bridge`, `pi setup`, `find` | `machine` (setup, rm, ls, probe) over `machines/` |
  | `pi deploy`, `pi bench`, `bench staged`, `bench stage`, `bench mac-ab`, `bench mac-volume`, `ab` | `bench` (deploy, run, ab, report) over the one pipeline |
  | `boot`, `pi boot-order`, `pi helper` | `boot` |
  | `sysimage`, `image/*` | `sysimage` (build, write, ls) with one build path per image kind |
  | `key`, `push`, `sudo`, `backup`, `skills` | `key` and `push` |
  | `doctor`, `disk`, `version`, `verify` | `doctor` |
  | `remotes` | `sync --fix` |
  | `completion` | generated by the dispatcher |
  | `zed`, `gui` | kept: the editor, and the browser in the benchmark seat for layout tests on a GPU |
  | `pick`, `notify`, `mcp`, `skills` | deleted (`notify` becomes a library call) |

- **Special cases become data.** The Mac's quiet-desktop tables, the board
  confs, the subtest exclusions and the credential rules are files a reader
  loads, not code paths.
- **Tests are written once per rule.** Kill-point and conformance tests are
  parameterised over commands and drivers, so a rule is one test body
  instead of one per command per agent.
- **Prose leaves the code.** The 15% comment ceiling stays; incident history
  and rejected alternatives go, since the test is the record of the rule.

Step 0 includes the deletion pass: every command and every image kind is
kept, merged or deleted by name, in this file, before anything is ported.
Deleting is cheaper than porting, so anything with no user in the next
quarter goes first.

## Testing, concretely

- **The fake machine** holds files, a process table and a clock in memory,
  answers as told (this ssh says X, that podman says Y), and records every
  effect in order.
- **Kill points.** One helper runs a mutating command against the fake,
  stops it after effect *n*, runs it again, and asserts the final state is
  the same for every *n*. Every mutating command has this test, which is
  what "crash-only" means here.
- **Driver conformance.** One test class runs over every target driver and
  every boot driver, so a function one driver forgot is a failing test and
  not a surprise on a board.
- **The live tier reuses the bodies.** A live test is a unit test with a
  real machine in the fake's place. It runs as a task, so `wk status` shows
  it like any other, and it never runs beside a build.
- **Budgets in the runner.** Per-test and per-tier wall clock, the owed
  count, and the lint pass are the runner's, not a convention.

## The order

Each step leaves the tree working and the suite green, deletes what it
replaces in the same change, and ends with the live tier run against the
container target. A bash command and its Python replacement never both
exist.

1. **The core.** The dispatcher is Python (`lib/wk/`); two parts remain, each landing on its own.
   - *The record and the clock*: the task record as a Python module reading
     and writing the same directory the bash one does, and the clock every
     wait takes. Done when `wk status` reads records through it (step 2).
   - *`Machine`*: the one seam, its fake, and the local, container, remote
     and vm implementations, landing with the first command that needs each
     (steps 2 and 3); the bridge is deleted with the last bash caller of
     `lib/target.sh`.
2. **The seam and the drivers.** `Machine` and its fake are in; the
   registry and the read side of the container, guest and workspace-local
   drivers are Python (`lib/wk/targets.py`), the remote driver's probe and
   every driver's create and destroy still bridge to `targets/*.sh`. The
   readers port first as the drivers' smallest callers: `ls`, `version`,
   `logs`, `start` and `disk` are; `status` (with `lib/status-view.py`
   folded in) and `doctor` follow, then the write side of each driver. Done when
   `lib/target.sh` and `targets/*.sh` are gone and no bash file parses
   JSON.
3. **Workspaces.** `new`, `rm`, `build`, `run`, `test`, `enter`, `scp`,
   `sync`, `pr`, `remotes`, `verify`, `ai`, `zed`, `gui`, `profile`. Done
   when `lib/store.sh`'s workspace half is gone and each has a kill-point
   test.
4. **Credentials.** `key`, `push`, `sudo`, `backup`, `skills`. Done when
   `lib/store.sh` is gone.
5. **Fleet and bench.** `sysimage`, `boot`, `pi`, `bench`, `ab`, `quiesce`,
   `session`, `notify`, `bridge`, `vm`, `find`, `remote`, `gc`, as the one
   pipeline over one `machines/` directory. Done when the seven files over
   500 lines are gone and `bench/`, `boot/`, `image/` hold only what runs on
   a board.
6. **Results.** A task's results live in its workspace, the task restarts
   from where it stopped, and the deliverables export as one archive
   (README, "results").
7. Delete this file.

## Owed

What the handoffs owed, one row per behaviour. `unit x.y` and `live x.y` name
the test (`tests/test_<x>.py`, tier by prefix); `killpoints[<cmd>]` and
`conformance[<kind>]` are the parameterised bodies from "Testing, concretely";
`lint.<rule>` is a rule of the lint pass; `delete` marks an item the Python
core makes obsolete. A handoff item marked `[decision]` that the plan already
decides is a row; one still open is listed under "Decisions for the user".

| owed behaviour | lands in step | test |
| --- | --- | --- |
| Every mutating command, killed after any effect and re-run, converges on the declared final state (`new`, `rm`, `sync`, `build`, `test`, `bench`, `gc`, `vm base`, `machine setup/rm`, `key`, `skills`, `backup`, `quiesce`, `session`, `ai`, `boot`, `sysimage`, `./setup`) | 1 (helper), then each command's step | `unit killpoints[<cmd>]`, `live killpoints[setup]` |
| `wk gc` reclaims or names every kind of rubble: tagged container images nothing references, an abandoned `.tmp-*` seed, a workspace a killed selftest left, a staged build on the Mac volume, an instrumented slot on a board, a remote store's mirror, ccache and dead workspaces; and `wk disk` names what it can reclaim | 5 | `unit gc.reclaims_or_names[<kind>]` |
| A machine on another wk-tools sha or a dirty checkout is named with both shas, a delegated answer from it is reported as its own rather than merged, and the remedy is `wk sync --tools` once clean, "commit and push here first" while dirty | 2 | `unit status.version_skew` |
| `wk profile` records in every mode (sampling prints the tier breakdown, bytecode leaves one JSCProfile json, samply refuses with the host remedy above `perf_event_paranoid` 1, instruments records a `.trace`) and `--fetch` copies the recording out byte for byte | 3 | `live profile.modes[<mode>]` |
| rpi4 boots its bench system reliably: EEPROM sd-first with `BOOT_WATCHDOG_TIMEOUT` and `MAX_RESTARTS`, a medium that holds the bus under write load, the stick reproduced from the repo by `wk sysimage write --disk rpi4:/dev/sda`, and `--back` reaches the rescue | 5 | `live boot[rpi4]` |
| The Mac's bench volume runs the whole lifecycle: `wk boot mbp` against the real install, a stage from a guest onto it, a measured run, `wk quiesce status` before it, the screen watch during it, and `wk bench compare` against a container run | 5 | `live bench[mbp]` |
| One spelling per concept: a round is run-benchmark's round, a perf task is (name, stamp, description, ref), a device is a device, a slot is named for its arm and phase, a time profile is one thing; "lane" and the second spellings fail lint | 5 | `lint.vocabulary` |
| A task's record and log live on the machine that runs it; every reader reaches them through that machine, nothing copies them, and a build started in a guest shows on the host | 1 | `unit record.home_is_the_machine` |
| Every gate a bench run needs is asked over the running install before anything reboots (quiet desktop, quiesce readback, brightness, display mode, browser check, the staged dry run, the window in front, no other machine running), a reboot is only the transition, and a run that starts behind another window fails at its own gate | 5 | `unit bench.preflight_asks_every_gate` |
| A failed leg on a board leaves evidence readable afterwards: a persistent journal, browser and tunnel logs on every leg, a board-at-failure capture, and warmup evidence on a leg that timed out | 5 | `unit bench.failed_leg_keeps_evidence`, `live bench.evidence[<board>]` |
| Bash mechanics the Python core removes: a script read mid-edit, `capped`'s orphaned sleep holding `wk status` open, `wk key`'s two hops into the VM, `wk help hardware` tracking the drivers, `wk pick`, `wk mcp` | — | delete |
| Every `WK_*` override is read once, documented where the user meets it and tested, or removed | 1 | `lint.wk_overrides` |
| No unit test asserts a wall-clock bound of its own (the interrupt test's 10 s, the lock tests' polls): the runner's budget is the one bound, and a test proves ordering with a fake clock | 1 | `lint.no_wall_clock_assertions` |
| A workspace whose creation died (container up, directory gone, nothing creating it) is refused at once by every command, naming `wk rm`; nothing asks the SDK's `wkdev-enter`, which waits 182 s on such a container before aborting, and `wk gc` names it as rubble | 1 | `unit machine.dead_creation_refused_at_once` |
| The live tier runs against the container target on Linux and macOS alike; no test is gated on the podman VM | 1 | `live` runner rule |
| A hold is released only when its holder is provably gone, an unreadable holder keeps it, and no child process inherits one | 1 | `unit record.hold_follows_holder` |
| A workspace name is resolved once per invocation and every machine probed at most once | 1 | `unit machine.probed_once_per_invocation` |
| Interrupting a command (Ctrl-C, a lost ssh) stops the process it started on the far machine and releases its holds | 1 | `unit machine.interrupt_stops_remote_process` |
| The vm target's records live in a store of their own; a scratch store never puts two targets on one directory | 1 | `unit record.one_store_per_target` |
| Every mutating command honours `--dry-run` as the recorder: the plan and the run cannot differ, and a dry run fetches nothing | 1 | `unit dispatch.dry_run_is_the_recorder[<cmd>]` |
| The dispatcher parses every argument: the build config, the subverb, `--target`, paths | 1 | `unit dispatch.parses_every_argument` |
| `wk <cmd> -h` previews the command line it would run and lists the values every config-taking flag accepts | 1 | `unit dispatch.help_previews_and_lists_values` |
| Every destructive effect is named in one question asked before it (a tailnet device delete, a replaced root-owned helper, a removed far destination, a reset SDK checkout, an overwritten credential), the default is No, no terminal declines, `--yes` answers, and a forwarded command carries the answer rather than exempting the receiver | 1 | `unit dispatch.destructive_asks_once[<cmd>]` |
| `--force` crosses only the barriers it names and records itself; a preflight that cannot be measured reports unknown, never failure | 1 | `unit dispatch.force_names_what_it_crosses` |
| Every long-running command (build, test, bench, image build, A/B, board run) writes the one progress record (step n of m, since when, the log) that dies with it; `wk status` shows every running one and a board run prints its iterations from it | 1 | `unit record.progress_shape[<cmd>]` |
| A detached build truncates its log before its record says running, and a run that launched one waits for that build, not the previous report | 1 | `unit record.detach_reads_its_own_build` |
| Two commands mutating one resource serialise or refuse naming the holder, on every target (two builds, two syncs, two guest starts, two image builds, a base refresh during a build) | 1 | `unit record.one_lock_per_resource[<cmd>]` |
| An unreadable or older-shape task record renders as unreadable and everything else still lists | 1 | `unit record.tolerates_corrupt_and_old` |
| Every target driver and every boot driver implements the whole interface (a guest driver starts a stopped guest, reports stopped as stopped and needs no image build) | 1, 5 | `unit machine.conformance[<kind>]` |
| A machine that cannot be probed is named unreachable with its timeout, never dropped or hung on | 1 | `unit machine.unreachable_is_named` |
| A probe or a boot driven over non-interactive ssh finds the same tools a login shell does | 1 | `unit machine.remote_path` |
| The podman machine is not started beside a running macOS guest on a host too small for both | 1 | `unit machine.podman_not_started_beside_guest` |
| Copying bytes out of a workspace, onto a board or onto a card is the one `Machine` copy | 1 | `unit machine.one_copy_path` |
| Each command runs where its declaration says, and a forwarded one forwards only the flags that apply there (`sync` scope flags never enter the podman VM; `bench` on the Mac's volume runs on the Mac) | 1 | `unit dispatch.where[<cmd>]` |
| When the record and the machine disagree (a hand `podman rm` or `tart delete`, a deleted `ws/<n>`, an edited `~/.ssh/config.d/wk`, a fetch into a published base) the command reports it, believes the machine, refuses by name and touches only its own lines | 1 | `unit machine.machine_wins[<case>]` |
| The fleet view is one: the exit code is the worst state found anywhere, a name alive on two machines is a conflict `--target` disambiguates, two workstations reaching one box see one state and a disagreement names both views | 2 | `unit status.fleet_is_one` |
| An armed machine's status line shows the transition (system, who, when); armed too long or back in host mode with the record uncleared reads desync; a mutating command aimed at it refuses | 2, 5 | `unit status.armed_transition` |
| `wk status` renders what it has: an empty health block is silent, a workspace whose exec fails shows its row without the extra fields, one whose machine does not answer reads unreachable, `--json` and `--html` are untouched by the text renderer, and delegated headings come before their rows | 2 | `unit status.renders_partial` |
| `wk status <ws> --wait` blocks while busy and reports once; `--timeout` stops waiting without claiming the work stopped | 2 | `unit status.wait_and_timeout` |
| Every session start (`wk status`, `wk help`) leads with the machine's role and mode | 2 | `unit status.leads_with_role_and_mode` |
| `wk logs <ws> -f` follows a live build on any target | 2 | `live logs.follow[<target>]` |
| `wk ls` inside a workspace prints a not-applicable marker for BASE and CHANGES | 2 | `unit ls.in_workspace_marks_not_applicable` |
| A reporting command (`status`, `ls`, `logs`, `disk`, `doctor`) starts, boots or repairs nothing, here or on the far machine | 2 | `unit report.readonly[<cmd>]` |
| `wk stop` then `wk start` returns every workspace to running; `--keep-vm` leaves the podman machine up | 2 | `live start.roundtrip` |
| `wk doctor` on a freshly set-up machine reports ok, and each printed fix clears its line when run | 2 | `live doctor.fix_clears_line` |
| `wk doctor` reports a bench machine's readiness (SIP on both installs, the quieting) the way `wk quiesce status` does | 5 | `live doctor.bench_readiness[mbp]` |
| A machine is rebuilt from the repo alone: `wk doctor` names every machine-local entry regenerable, re-authable or backed-up before the wipe, and a fresh clone plus `./setup` sees the whole fleet with nothing copied | 2 | `live doctor.reprovision[<machine>]` |
| `./setup` completes on every host OS and every privileged stage installs its helper | 2 | `live setup.completes[<host>]` |
| `wk new` refuses without a base snapshot naming `wk sync` and creates nothing, remakes a half-made workspace rather than answering "already exists", and waits for the ready marker | 3 | `unit new.lifecycle` |
| `wk new` where the organisation denies Remote Control refuses before creating and names the owner; `WK_NO_CLAUDE_RC=1` makes the workspace | 3 | `unit new.remote_control_policy` |
| A workspace on a peer is created there by hand (refused here, naming the command) and removed from here; `wk rm --all` asks once for the whole fleet and routes each removal | 3 | `live rm.peer[<machine>]` |
| `wk rm` leaves nothing on any target: no container, guest or checkout, no ws dir, no registry entry, no `Host wk-<name>` alias, no `.unfiltered`; the registry entry outlives the artifacts, never the reverse | 3 | `unit rm.final_state[<target>]` |
| A build config is data: `--cmakeargs` is refused, an ASan config builds instrumented into its own dir, a profile-guided config declares its disk need for the guest and for the host image it grows | 3 | `unit build.config_is_data` |
| A build sizes from the whole machine once, reserving the desktop once, on the host and inside a guest | 3 | `unit build.sizes_once` |
| `wk build --babysit` is a task: one at a time by its record, ends stalled, gave-up or error by name, refuses where it cannot run, and a killed one reads died | 3 | `unit build.babysit_states`, `live build.babysit_e2e` |
| Every declared build config builds on its target (gtk, wpe, mac-debug, ios-sim, armhf on 2.48), a fresh clone off a warm base builds in under 45 min, and a mac build produces ImageDiff | 3 | `live build.config[<config>]` |
| `wk test <ws>` runs the JSC suite and `--layout` on every target, against a remote target's own build | 3 | `live test.suite[<target>]` |
| `wk run` finds its binary on every port (GTK, WPE, an Apple-port guest) with `LD_LIBRARY_PATH` prepended, and `--lldb` gets a pty on every target | 3 | `unit run.finds_binary[<port>]`, `live run.lldb_tty` |
| `wk enter <ws>` lands in a shell, `wk enter <ws> <cmd>` runs the command, `--zed` against a broken workspace refuses naming the repair | 3 | `unit enter.runs_command`, `live enter.shell` |
| `wk sync` bare inside a workspace syncs it, `--all` reaches every workspace on every target, `--tools` refreshes every machine's copy and publishes one snapshot, `WK_MIRROR_BRANCHES` carries the extra branches | 3 | `unit sync.scopes`, `live sync.fleet` |
| Every checkout's wiring is rendered once (`origin` WebKit/WebKit, both forks, the machine's mirror, `core.sshCommand`, the ccache ceiling), nothing already there is overwritten, nothing outside the wk root is edited, and `wk remotes` reports a deviation | 3 | `unit remotes.wiring[<target>]` |
| The PR workflow runs as one flow: `wk push on\|off`, `wk remotes --fix`, `wk pr`, the `container/bin` helpers, agents building while a person pushes, including from an armhf container | 3 | `live pr.workflow` |
| `wk ai claude` runs the wall's checks at once, gives the report `wk verify` gives, refuses a stopped proxy, `--force` repeats the warning at exit, the patch verifier can fail, and a tool inside wanting the network is refused and told so | 3 | `unit ai.verifies_wall`, `live ai.walled_session` |
| In an agent session `git commit` and `git push` name the rule after git's own error, `wk push on` on the host ends the session, and a terminal session turns push back on at exit | 3 | `live ai.commit_wall` |
| `wk zed` reaches a workspace through its `Host wk-<name>` ProxyCommand alias on every target, one hop for a peer's, and `wk new --zed` warns instead of failing when zed cannot launch | 3 | `unit zed.alias_is_proxycommand`, `live zed.peer` |
| `wk key setup` elects across workstations: the credential its issuer accepts wins from whichever machine runs it, a refused peer is re-logged in, a second run moves nothing, and one `claude login` seeds every workspace | 4 | `live key.election[<peer>]` |
| `wk key register` registers one key per machine titled with its name, `wk key check` reports per machine, and a peer that did not answer reads differently from one holding no key | 4 | `unit key.register_per_machine` |
| A build box holds no deploy key at rest: an explicit push forwards the workstation's wk agent, an agent session forwards none, `wk push status --target <box>` says so, and a push from a remote workspace succeeds | 4 | `unit push.remote_forwarding`, `live push.from_remote` |
| `wk backup` then `./setup` round-trips with no spurious change; the junk filters strip what they claim; a write is whole or unchanged; one path with a per-platform adapter | 4 | `unit backup.filters`, `live backup.roundtrip` |
| Skills sync (status, diff, pull, push) refuses over uncommitted edits, and every skill is followable from inside a container and a guest | 4 | `unit key.skills_refuse_dirty`, `live ai.skills_workspace_true` |
| `wk sudo require` installs its sudoers rule, validates with `visudo -c` before and after, proves `sudo -n true` fails, gets a terminal over ssh with `--target`, and no fleet machine holds a NOPASSWD grant wider than the three helpers | 4 | `live sudo.require[<machine>]` |
| No builder or configure cache records a tool from `container/bin` (the build wall stays off PATH) | 5 | `lint.build_wall` |
| A macOS guest reaches the network only through softnet's proxy: nothing with it bypassed, PyPI through it, the egress block in `~/.zprofile` | 5 | `live vm.egress[<check>]` |
| One host-owned mirror feeds the podman VM and every guest: clones are `--shared`, alternates resolve inside a container, `./setup` recreates the mount, and a missing mirror refuses naming `wk sync` | 5 | `live vm.shared_mirror`, `unit new.refuses_without_mirror` |
| The golden base is rebuilt from `WK_VM_IMAGE`, carries no build caches, tracks the Xcode GA image, and `wk vm base --rm` asks separately about the pulled image while guests keep working | 5 | `live vm.base_matches_pin`, `unit vm.base_rm_asks_twice` |
| The guest desktop is usable and stays so: the window resizes, `open -a` launches, screen saver, sleep and lock stay off across a reboot, both Setup Assistants stay suppressed, lldb prints no `llvmcas:` warnings | 5 | `live vm.desktop` |
| `wk quiesce on` sets and reads back every setting on every machine (governor, App Nap, high power mode, sleep, update checks from the setting, Do Not Disturb proven by a banner not drawn), `off` restores the real prior values after a reboot, a re-run is a no-op, and it returns over ssh | 5 | `live quiesce.readback[<machine>]` |
| Every launchd job on a Mac bench install and every systemd unit on a Pi image is classified in the quiet tables, none wedges a probe when stopped, and the table is re-read after an OS bump | 5 | `live quiesce.classified[<machine>]` |
| `wk session on\|gdm\|off` reaches the asked mode from any half-state on the intended chip, `wk gui` draws in that seat and refuses a remote target, and `wk bench` refuses a BMC seat | 5 | `live session.modes[moose]`, `unit gui.refuses_remote` |
| One bench pipeline (deploy, run, record, report) over the driver interface for board, volume, guest and container; state keyed per machine and per run, the result collected from where it was written, the phases one ordered list, no per-system runner, arming or record writer | 5 | `unit bench.pipeline_conformance[<system>]` |
| Every bench run writes the one result record with its provenance (kernel, arch, profile, root device, cores, the manifest reconciled with the disk) and `wk bench ls`, `compare` and `report` read only that | 5 | `unit bench.one_record[<system>]` |
| A bench run pins the cores it records, in a container and in a guest | 5 | `unit bench.pins_cores`, `live bench.pins_cores[<target>]` |
| `wk bench compare` gives per-subtest confidence intervals from the workspace that built the run | 5 | `live bench.compare` |
| A benchmark payload is seeded from the mirror, once, whatever runs at once | 5 | `unit bench.seed_from_mirror` |
| `wk bench ab` drives a PR's two slots or two system images alike, refuses arms whose payload pins differ, and compares only rounds both arms finished | 5 | `unit ab.plan_and_pairing` |
| A report names iteration spread apart from run-to-run spread, labels an instrumented leg's time, leaves no stray settle directory, and states a plan's cost (`--rounds`, `--count`) from measured leg times before it runs | 5 | `unit bench.report_and_cost` |
| An A/B on the Mac resolves a PR-sized delta: one run varies `--count`, one `--rounds`, against the measured per-round spreads of all three plans | 5 | `live ab.resolution[mbp]` |
| `wk bench ab --patch` builds, stages and measures both arms of a `mac-release-pgo` pair end to end, and the measured build's dSYMs symbolicate a capture | 5 | `live ab.pgo_pair[mbp]` |
| The guest rehearsal drives the whole Mac A/B path (two arms staged, run, collected) on `benchvm` | 5 | `live bench.rehearsal[benchvm]` |
| The warmup round's profile is captured, saved and symbolicated on every system (samply on the Mac and aarch64, sysprof-cli with the image's own flags on armhf) | 5 | `live bench.warmup_profile[<system>]` |
| A first run after a stage is no slower than the second, or the cause is named and paid before the run | 5 | `live bench.first_run_after_stage[mbp]` |
| A browser launch on a board starts from a named, cleared cache so a cache-served load never stalls a leg, the warmup probes are read on both widths, and GPU engine time is read or the warmup says why not | 5 | `live bench.leg_completes[<board>]` |
| A deploy routed through a macOS workstation's podman VM reaches a board in bench mode over Tailscale SSH with no key of its own | 5 | `live bench.routed_deploy` |
| A board PGO collection records its cost per plan, its coverage against the floor, and what the collecting browser rendered and JITted with; the kill waits long enough for the profile dump | 5 | `live bench.pgo_collection[<board>]` |
| Upstream carries what the lane patches locally (OSXMiniDriver's pgo dirs, `locate_binary_xcrun` off macOS, an optional `pgo-profile compress`, the JIT-report crash in JSC, the SSID redaction), and the local patch file goes | 5 | `unit pgo.no_local_patch` |
| The Mac's startup volume is set by the privileged helper, proven round trip before arming, and read back from firmware | 5 | `live boot.arm[mbp]` |
| A fresh bench volume provisions itself: python3 present before it is needed, Command Line Tools fetched and the update denial restored, no FileVault, the `bench` account, room checked, network credentials checked | 5 | `live sysimage.mac_volume_provision` |
| One image model: the Mac volume and the guest base are `wk sysimage build` builders with the one marker and manifest, and `wk sysimage ls` reaches a build left on a build host | 5 | `unit sysimage.builders_conform` |
| `wk sysimage build` is a task: `--detach` re-attaches, a running stage is refused by name, done is the wrapper's marker, a silent bitbake is reported not killed, one naming the same task past N heartbeats is reported wedged and given up on, `--stop` kills the process group and reports only when it is gone | 5 | `unit sysimage.task_states` |
| A second build of one profile reuses sstate, and `--keep-work` leaves the kernel tree to configure | 5 | `live sysimage.sstate_reuse` |
| An image profile is data: a board declares it or it goes, its defconfig is derived by the repo, a pinned kernel builds none, and a buildroot 2.52 profile takes the three PGO phases or says why not | 5 | `lint.profiles_are_data` |
| meta-wk's pseudo bump stays only if the reproducer shows it needed | 5 | `live sysimage.pseudo_reproducer` |
| A disk written from any image is unique per disk (`LABEL=` images included) and mountable whatever the medium held | 5 | `unit sysimage.write_identity`, `live sysimage.write[<board>]` |
| Every card verb runs against a real card and is read back before unmount, and a board boots from it onto the tailnet with its seeded key | 5 | `live sysimage.card_verbs[rpi5]` |
| A write refuses by name: two unmarked disks of one transport are listed, a missing or out-ranked card helper names the remedy | 5 | `unit sysimage.write_refusals` |
| One WiFi credential reader, in the card helper | 5 | `lint.one_wifi_reader` |
| Arming is exact: two systems with one image id are told apart by slot, the leg is verified after the last arm, and a failsafe lives outside the script it guards | 5 | `unit boot.arming_exact` |
| The two-system lane runs against the fake board (arm, boot, probe, measure, disarm) | 5 | `unit boot.two_system_lane_on_fake` |
| A board's first boot is proven on hardware: self-disarm parks the medium, self-return reboots an unclaimed board within the watchdog, the rescue marker holds, `config.txt.append` lands for every builder, an absent boot device falls through to host mode, armed-not-rebooted reads ARMED exit 2, and the prompt shows `bench` | 5 | `live boot.firstboot[<board>]` |
| rpi5 boots a bench system from its stick, the second-system pair included, and hands itself back | 5 | `live boot[rpi5]` |
| rpi3 runs the shared-card two-system layout: the helper on the rescue, `@second` and `@third` written from it, armed by id, an `--ab-systems` run, and a panicking bench kernel reverts | 5 | `live boot[rpi3]` |
| rpi4's 2.52 system brings up KMS on every boot, or the run names why not | 5 | `live boot[rpi4].kms` |
| rpi4's 32-bit buildroot system reaches userspace, read at a serial console | 5 | `live boot[rpi4].armhf` |
| rpi5's tuning has two owners: stability in `./setup`, the overclock as an `oc` image profile, never the EEPROM; the 26.04 paths re-checked | 5 | `unit sysimage.oc_profile_in_image` |
| `wk machine setup <board>` takes a board from a blank card to an answering tailnet name in `pi-hosts` and back, `wk machine probe` finds it when on, and a workspace reaches only that address, on port 22 | 5 | `live pi.setup[<board>]` |
| `wk machine setup <box>` leaves a build box in one shape (zsh, or a named warning), and a cleanup accepted at the prompt is removed | 5 | `live machine_cmd.setup[<box>]` |
| Two machines sharing one home each resolve their own target by hostname with no ssh, provisioning one never clobbers the other, and builds key dirs and locks per machine | 5 | `unit machine_cmd.shared_home` |
| A board is reached by tailnet name alone: no MAC, `.local`, address stanza, `HostKeyAlias` or ProxyJump remains once both boards join by image, and the bench install is reached at its own name | 5 | `lint.no_addresses` |
| The Librem 5 runs the pmOS bridge role in front of moose's BMC, with the BMC's own config in a conf file | 5 | `live bridge.setup[moose-bmc]` |
| A bridge's segment is proven: provision on the eMMC route asks which disk, a board on `lan0` gets its reserved address and a workspace reaches it, `wk bridge tailnet` approves the route and `autoApprovers` holds after a policy edit, the netwatch ladder stops at its budget, the dock holds 480 Mbit, the watchdog device exists, the camera streams, and a bridge whose segment is down reads differently from one that is off | 5 | `live bridge.segment[<bridge>]`, `unit bridge.segment_down_vs_off` |
| The lab layer (targets, boot, image, bench mechanics) names nothing of WebKit, and the layers are directories | 5 | `lint.layering` |
| An image build runs from a Tart guest and the image reaches the host for writing | 5 | `live sysimage.build[vm]` |
| A task's results live in its workspace, `wk bench ls` names them wherever they are, the task restarts from where it stopped on any machine, and `wk doctor` names the results backed-up | 6 | `unit results.restart_anywhere` |
| A task's deliverables export as one archive | 6 | `unit results.export_archive` |

### Decisions for the user

- README gets a section defining the `home`/`lab`/`wk`/`field`/`stock` layers, or the layering goes.
- The word "target": a device configuration or the execution target; one workspace per (perf task, device) rather than per profile, deletable once its results are in.
- `git-sync-fork` against the fork's protected `main`: lift the protection, or the helper refuses by name.
- `wk key check` reports a Bugzilla key's authentication only; whether editbugs capability is probed another way.
- Whether `wk boot mbp --diag` and `--back` reach the bench install from host mode.
- The bench volume runs 26.6.1, the host 26.6.2: whether the two installs need to be comparable.
- Speedometer 3's `wakeLock` NotAllowedError under MiniBrowser: confirm Safari's path, or accept.
- rpi3: refuse a board with foreign processes, or clear them; Speedometer 2.1 as its plan, or swap/zram for 3.
- The 2.38 buildroot rpi4 image: pin a newer rpi-firmware so it boots a rev 1.4/1.5 board without tryboot, or keep tryboot.
- Delete `boot/pi-mbr.sh` (no machine declares it), or keep it for a firmware-bootable SSD.
- Tailnet ACL: boards reach a workstation's benchmark server directly, or keep the ssh tunnel; the `tag:wk` grant `wk pi setup` prints as a manual step.
- `wk pi helper` onto a workstation holding a card reader: a wk-driven path, or none.
- A rescue whose root reference is orphaned: a `wk` verb, or the helper's one-liner.
- LTO mode for the boards' perf build (the Mac lane is thin then full; the boards set none).
- Which pre-`wk` helper capabilities return: rr record, sysprof JIT dump, the wasm wrappers, SIMD and V8 comparisons, the weekly report, an option-toggle A/B; a baseline build reachable outside `wk bench`.
- Settings audit: which non-default host settings persist (config.dconf's `why: unknown` entries; tolken's `wk backup --candidates`).
- A per-device connectivity and perf subcommand (wifi channels, autosleep, overclock) and its boundary with the settings audit.
- Host tools: git-lfs on every host or none; pmOS builder prerequisites only on the aarch64 build host; wk-tools work in a workspace so the host `claude` goes.
- Bridge phones: auto power-on after power loss; remote power for the boards, which nothing here can switch on.
- Write-ups and upstreaming: the egress-proxy design, SDK patches 3 and 11, the cross-compile commits, the `CONFIG_NUMA_EMU` Launchpad request, `gpr`/`wk pr` and the profiling wrappers.
- The sandbox escape audit, last and on both platforms: the incident list, the allowlist and CDNs, apt and ddebs, the broker, the agent-rw mount, the injector's standing tokens, remote targets, bridges, device paths, yocto egress, the git helpers.
- Reprovision tolken: host user `jmichaud`, Remote Login on both installs, keys per install; register each build box's deploy key on GitHub.
- What each part of `$WK_STORE` is called before a second project needs a name.
