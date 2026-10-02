# The re-architecture: how it lands

README.md's first half says what `wk` is. This file is the order of work
that gets the tree there, and what proves each step. It is deleted when the
last step lands.

## Where we are

Measured 2026-09-22 on this workstation.

| | |
| --- | --- |
| code | 39k lines of bash, 13k of Python (the dispatcher, the registry and drivers, the record, status and doctor among it); 39 commands, 21 of them Python, 8 over 500 lines and holding 53% of command code |
| tests | 71k lines, 194 modules, 4434 tests in the lint and unit tiers, 40 in the live tier |
| `wk selftest` (lint then unit) | 13.4 minutes, green; the lint tier alone 27 s |
| the slowest unit test | 16 s, under a 30 s budget the runner enforces |
| owed tests | 11 |
| places a test starts a process | 1,540 |

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
end of each step and each merged command, never pushed by an agent. The
session's permission classifier refuses an agent's `git commit`, so the
user commits what an agent leaves green in the tree.

Moving the core to Python is the one call that cannot be made a step at a
time later. The alternative, keeping bash and faking tools on `PATH`, does
not reach the goal: bash cannot take a clock, cannot be tested below the
process boundary, and pays 30 ms per fork, so 4,300 tests floor at two
minutes before the first real wait. The CLI, the conf files and the record
on disk do not change, so nothing a person types or a running build depends
on moves.

`wk rm` refuses a workspace holding a task no export holds as it is now, naming `wk bench export <task>`; `--force` crosses it.

PyYAML stays in `lib/wk/sysimage/pmos_build.py` (netplan's own dependency on the build host; the stdlib has no YAML reader); the bridge phones' images no longer install avahi and nothing here uses mDNS; `lint.one_wifi_reader` stays owed because the Mac reads its WiFi credential from the System keychain and the pmos build host from netplan, and routing both through `admin/wk-card-priv` would widen a privileged helper; `wk machine setup <board|mbp> --dry-run` on an unreachable machine prints the plan and exits 0 rather than refusing (a wet run still refuses), so the unit tier's ssh shim does not fail dry runs.

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
     (steps 2 and 3).
2. **The seam and the drivers.** `Machine` and its fake are in. The
   registry and the read side of every driver are Python
   (`lib/wk/targets.py`): container, guest and workspace-local whole, and
   the remote driver's probe (one memoised ssh round trip), list, info,
   exec, `wk`, start and stop. The readers are Python as the drivers'
   smallest callers: `ls`, `version`, `logs`, `start`, `stop`, `disk`,
   `status` (the walk in `lib/wk/status.py`, the renderer in
   `lib/wk/statusview.py`) and `doctor` (`lib/wk/doctor.py`: rows of state,
   what and remedy, one renderer). Every driver, the tooling push, the
   guest's start and stop, the boot and reach probes, the credential
   verdicts and the store paths are Python; this host's envelope is
   `Resources`.
3. **Workspaces.** `new`, `rm`, `build`, `run`, `test`, `enter`, `scp`,
   `sync`, `pr`, `remotes`, `verify`, `ai`, `zed`, `gui`, `profile`. Done
   when each has a kill-point test. **Where each stands:**
   - *Landed.* `cmd/new` and `cmd/rm` are Python entry points over the flows
     in `lib/wk/workspace.py`, with the lock in `lib/wk/lock.py`, the alias in
     `lib/wk/sshalias.py`, and each driver's write side and `mirror_dir` in
     `lib/wk/targets.py`; `tests/test_wk_workspace.py` holds every refusal,
     `killpoints[new]`, `killpoints[rm]` and dry-run-equals-wet-run.
   - *Owed.* The live check, one pattern per run: `wk selftest --live
     crash_only`, `wk selftest --live container_workspace`, `wk selftest
     --live lifecycle`.
   - *`enter`/`scp`/`zed`.* All three are Python entry points over
     `lib/wk/targets.py`: `enter` runs a command through `Target.exec` or
     execs into `Target.enter_argv`'s shell; `scp` moves bytes through
     `pull`/`push`/`pull_dir`/`push_dir`/`path_kind` -- `podman cp` for a
     container, the one `Machine` copy (`copy_in`/`copy_out`/`copy_tree_in`/
     `copy_tree_out` on `Local`/`Ssh`/`Fake`) for a guest or a build machine;
     `zed` reaches a workspace through `ssh_host`/`ssh_prepare`/`ssh_user`/
     `ssh_proxy`, one hop further for a peer's own `--route`; `cmd/profile`
     calls `Target.pull_dir` directly.
     `tests/test_enter.py`, `tests/test_scp.py` and `tests/test_zed.py` hold
     the refusals; `tests/test_wk_targets.py` holds each driver's argv;
     `tests/test_wk_machine.py` holds the copy conformance. Owed: the live
     checks (`enter.shell`, `zed.peer`) against a real container, guest and
     peer.
   - *`pr`.* `cmd/pr` is a Python entry point: `rebase` and `open` run over
     `Target.exec`/`src`/`mirror_dir` (on the base `Target` and on
     `LocalWorkspace`), and `pr_open_target`/
     `pr_open_gh_args` are module-level functions `tests/test_pr_workflow.py`
     and `tests/test_pr_upstream.py` import directly rather than lifting bash.
     The plain `wk pr <ws> <spec>` checkout is `pr.checkout`, over
     `Target.exec`/`act_exec`; the mirror fetch a pull request or a fork's
     branch resolves through (`pr.mirror_fetch`/`mirror_fetch_pull`) runs only
     inside `pr.resolved_or_planned`, the one dry-run recorder `wk bench ab`
     shares, so a dry run never reaches git. Owed: `killpoints[pr]` (5.4's).
   - *`verify`.* Merged into `wk doctor <workspace>` (and `wk doctor` inside
     one, the half `wk ai claude` runs there): the checks are
     `lib/wk/wall.py`, run at once, `tests/test_doctor_wall.py` holds each;
     `wk verify` is a tombstone. Owed: the live check against a container and
     a guest.
   - *`ai`.* `cmd/ai` is a Python entry point: the wall's checks are
     `lib/wk/wall.py`'s own (`from_host`, `from_inside`, `commit_walled`,
     `commit_wall_prefix`), and each driver's `exec_argv` is in
     `lib/wk/targets.py`. `tests/test_ai.py` holds the flow,
     `ai.verifies_wall` and the session's `--remote-control <ws>`. Owed: the
     live checks (`ai.walled_session`, `ai.commit_wall`,
     `ai.remote_control`).
   - *`sync`.* `cmd/sync` is a Python entry point (its parse and `--where`)
     over `lib/wk/sync.py`; each driver's furniture is `Target.sync` in
     `lib/wk/targets.py`, and `wk remotes` is a tombstone for `wk sync
     [<ws>] --fix`, the wiring read back in every fetch.
     `tests/test_sync.py` holds `sync.scopes`, the wiring report and fix,
     `killpoints[sync]`, `dispatch.where[sync]` and dry-run-equals-wet-run.
     Owed: the live check (`sync.fleet`); `status.py` calls
     `Target.workspaces`, and the one remaining inline copy of its union is
     `cmd/ls`.
   - *`build`.* `cmd/build` is a Python entry point over `lib/wk/build.py`
     (front, driver, `--detach`, `--kill`, the babysitter), `lib/wk/job.py`
     (the watched run, the announced pid, the one job stop) and
     `lib/wk/buildconf.py` (the configs as data, the cross configs
     included); each driver's `ccache_dir`, `build_argv` and
     `build_size` are in `lib/wk/targets.py`, the budget in
     `lib/wk/resources.py`'s `Budget`. `tests/test_wk_build.py` holds every
     refusal, `killpoints[build]`, `progress_shape[build]` and
     dry-run-equals-wet-run; `tests/test_buildconf.py` the configs. Owed:
     the live checks (`build.config[<config>]`, `build.babysit_e2e`). Every
     caller resolves a config through `buildconf.resolve`.
   - *`run`/`gui`.* Both are Python entry points that resolve a config
     straight through `lib/wk/buildconf.py`'s `Config` (no `config_load`
     call at all) and exec into `Target.exec_argv`'s result with
     `os.execvp`, the same replace-this-process pattern as `enter`/`zed` --
     every branch of both is a tail call into the target, so neither reads
     a `Result` or an exit status. `Registry.default_config` (from the
     workspace's own `build` task record, `lib/wk/record.py`) is the default
     config for both, and `Target.lldb_opts` carries Container's two `-O`
     flags. `wk gui` refuses a `kind == "remote"` target (`unit
     gui.refuses_remote`).
     `tests/test_run_until_crash.py` passes unmodified against the port;
     `tests/test_wk_run.py` holds `run.finds_binary[<port>]` and `--lldb`'s
     tty request on every target, `tests/test_wk_gui.py` holds
     `gui.refuses_remote`, the jsc-only/no-browser/macOS-container
     refusals and the fullscreen-flag table. Owed: the live checks
     (`run.lldb_tty`, `session.modes[moose]`).
   - *`test`/`profile`.* Both are Python entry points. `cmd/test` runs the
     JSC and layout suites over `lib/wk/job.py` (`watch`, `PidWatch`,
     `kill`, `stop`) and `lib/wk/resources.py`'s `Budget`, its record
     wrapped through `build.records_of` for the same `WK_ABORT_SECONDS`
     default a build's carries; `cmd/profile` is argv/refusal construction
     and direct `Target.exec`/`exec_tty` calls with no task record at all.
     Both resolve a config straight
     through `lib/wk/buildconf.py`. The one new
     primitive either needed: `Machine.run_tty`/`Target.exec_tty`,
     blocking with this process's own stdio inherited (a real pty for
     lldb/samply/xctrace) but returning control here afterward, unlike
     `exec_argv`'s `os.execvp` replace -- `enter`/`run`/`gui` still use
     that replace where nothing follows. `Registry.default_config` (`lib/wk/
     targets.py`) is `run`/`gui`'s and `test`/`profile`'s one caller.
     `tests/test_wk_test.py` holds `progress_shape[test]` and
     `killpoints[test]`; `tests/test_wk_profile.py` the host-side
     `perf_event_paranoid` gate and `--fetch`; `tests/test_layout_paths.py`
     and `tests/test_profile_debug.py` exercise the ported behaviour
     directly; `buildconf.ARCH`/`arch_label` name the arch. Owed: the live checks (`test.suite[<target>]`,
     `profile.modes[<mode>]`); no unit test yet for `--lldb`'s tty request
     (needs `Machine.run_tty`, landed, but not yet driven from `cmd/test`'s
     own suite).
   - *How agents work on it.* Disjoint file sets per agent, named in the
     brief; no `git checkout`/`restore`/`stash`/`commit`; no full `wk
     selftest` while anyone edits (`python3 tests/run.py --unit -k <pattern>`
     for a package's own modules, `--lint` before reporting); no mutating
     `wk` command; every report checked against `git diff`.
   - *Python-vs-Python duplicates a review pass found.* Fixed already:
     `wall.verdict(rep, publishing)` is the one "N check(s) failed" render
     (`cmd/doctor`, `cmd/ai`) and `wall.push_verdict(rc)` the one push-status
     decode (`cmd/ai`'s `push_switch` only runs `wk push <verb>` now).
     Also fixed: `is_linux`/`is_macos` live once in `lib/wk/machine.py`,
     the session socket in `targets.session_socket_present`, the loader-path
     prelude in `lib/wk/ldpath.py`, the Zed CLI in `targets.zed_cli`, and
     `cmd/zed --tools` resolves its target once. `lib/wk/sshalias.py` is the
     one alias writer and `lib/wk/store.py`'s `artifact_dir` the one artifact
     path. "Is this pid alive in the target" is one answer,
     `Target.pid_alive` (`lib/wk/targets.py`), which `record.of_target`,
     `workspace.py`, `cmd/stop` and `cmd/status` all ask.
4. **Credentials.** `key`, `push`, `sudo`, `backup`, `skills`.
   - *Landed.* `cmd/key` (`lib/wk/key/`: `cli.Key`, one mixin per concern -- `creds`, `deploy`, `login`,
     `election`, `check` -- over `common`'s verdict line, `GitHub` seam and `Fleet`) and `cmd/push` are Python
     over `lib/wk/secrets.py`'s `Secrets`. `wk sudo` and `wk
     backup` are `wk key sudo` and `wk key backup` (`lib/wk/sudo.py`,
     `lib/wk/backup.py`), the old names tombstones; `skills` is a tombstone.
     A destructive command whose destructive part is conditional calls
     `act.confirm` or `act.nothing_to_ask`, and `act.asked` is the one
     "acted before asking" check. Every Python command reads its options
     through `wk.decl.Args`. `tests/test_wk_key.py`, `test_wk_secrets.py`,
     `test_push_switch.py`, `test_wk_sudo.py` and `test_backup.py` hold the
     flows, `killpoints[key]`, `killpoints[push]` and dry-run-equals-wet-run.
     The fork and agent-secret tables are `secrets.FORKS` and
     `secrets.AGENT_SECRETS`, the one copy, and `container/firstrun.sh` asks
     `python3 -m wk.secrets` directly. `wk doctor`'s credential rows and the
     peers' login rows are `Key`'s own (`stored_verdict`, `cred_verdict_of`).
     `Secrets.agent_secret_remedy` (`lib/wk/secrets.py`) is the one remedy,
     over `cred_stored`/`cred_verdict` and `lib/credcheck.py`;
     `tests/test_wk_secrets.py`'s `TestAStoredCredentialIsReadTheOneWay`
     holds the read against a `Fake` refusing as `lib/secretfile.py` would.
     `-h` prints each subverb's destructive override under the command's line.
   - *Owed.* The live checks (`key.election[<peer>]`, `push.from_remote`,
     `backup.roundtrip`, `sudo.require[<machine>]`).
5. **Fleet and bench.** `sysimage`, `boot`, `pi`, `bench`, `ab`, `quiesce`,
   `session`, `bridge`, `vm`, `find`, `remote`, `gc`, `completion`, as the one
   pipeline over one `machines/` directory. Done when `lib/common.sh` is the
   one bash library, every command is Python, and `bench/`, `boot/`,
   `image/`, `bridge/`, `vm/` and `remote/` hold only what runs on a board, a
   phone, a bench install or in a target.

   **Sizes today** (non-blank lines, 2026-09-27): 10,114 of shell and 38,919 of Python
   outside `tests/` (68,352); 31 commands, all Python.

   | tree | shell | Python |
   | --- | --- | --- |
   | `admin/` (the three helpers and their installer) | 2,079 | 0 |
   | `container/` | 2,174 | 1,857 |
   | `host/` (the setup stages) | 2,122 | 0 |
   | `bench/` | 989 | 316 |
   | `bridge/` | 638 | 0 |
   | `claude/` | 365 | 610 |
   | `shell/`, `setup` | 470 | 0 |
   | `lib/` (`common.sh`) | 358 | 31,264 |
   | `build/`, `boot/`, `image/`, `remote/`, `vm/` | 919 | 245 |
   | `cmd/` | 0 | 4,593 |

   Python in this scope outside `lib/wk/`: `lib/wkdata.py` 1,554 and
   `bench/mac-browser-check.py` 372; `sched`, `pgo`, `notify`, `tailnet` and
   the board driver live under `lib/wk/`.

   **Target shape.**

   | command | subverbs | replaces |
   | --- | --- | --- |
   | `machine` | `setup`, `rm`, `ls`, `probe` | `remote`, `find` (`probe` with no name sweeps), `pi setup`, `bridge` (every verb), `vm` (the lifecycle verbs become `new`/`start`/`stop`/`enter`/`sync`/`rm --target vm`, `check` becomes `doctor <ws>`) |
   | `bench` | `deploy`, `run`, `ab`, `ls`, `report` (`compare` and `precision` are `report` over two runs) | `pi deploy`, `pi bench`, `bench <ws> <plan>`, `bench stage`/`staged`/`mac`/`mac-ab`/`ab-summary`/`seed`, `ab` |
   | `boot` | `<machine>` with `--status`, `--keep`, `--back`, `--disarm`, `--diag`, `--system`, `--boot-order` | `boot`, `pi boot-order`, `pi helper` (moves to `machine setup <board>`), `boot --prepare` (moves to `machine setup mbp`) |
   | `sysimage` | `build`, `write`, `ls`, `disks`, `rm` | `sysimage`, `image/*`, `vm base` (the guest base is a builder), `bench mac-volume` (the bench volume is a builder) |
   | `quiesce` | `on [--seat=kiosk\|desktop] [--bmc]`, `off`, `status` | `quiesce`, `session` (see 5.9) |
   | `gc` | kept, in Python | every kind of rubble is named by the module that makes it |
   | `completion` | generated by the dispatcher from the declarations | the `completion` command |
   | `notify` | a tombstone; `lib/wk/notify.py` is the library call | the bash `wk_notify` and the notify CLI |

   Commands after step 5: `new rm build run test enter scp sync pr ai zed gui
   profile status ls logs stop start doctor key push machine bench boot
   sysimage quiesce gc selftest`. That is 28 against the budget of 20.
   `logs`/`status` and `start`/`stop` are the obvious next merges, and they
   are not in this step.

   **One `machines/` directory.** `machines/<name>.conf` holds one machine,
   named by the name the CLI takes. `KIND` is one of `build`, `peer`,
   `board`, `mac`, `guest` or `bridge`. The other keys are today's, unchanged
   (`NODE_*`, `WK_REMOTE_*`, `WK_TARGET_*`, `BR_*`), so the phone-side
   `bridge/provision.sh` reads the same names.
   A machine-local overlay lives in `~/.config/wk/machines/`, which replaces
   `~/.config/wk/bridges/`. A bench role that is also a peer names the peer:
   `mbp.conf` sets `NODE_SSH=tolken` and keeps no copy of `tolken.conf`'s
   facts. Confs hold literals only, so `mbp.conf`'s `${WK_BENCH_VOLUME:-…}`
   default moves into code.

   **How a sub-step lands.** Each one:
   - ports one slice;
   - deletes the bash it replaces in the same change;
   - moves its tests from forking `./wk` or lifting bash to the fake
     `Machine` and the fake clock;
   - marks each live row's body `live` and each owed row `owed`;
   - leaves `python3 tests/run.py --unit` and `--lint` green.

   A bash library still sourced by an unported caller becomes a *shim*, one
   line per function calling the Python. It is never a second
   implementation, and 5.39 deletes every shim. `tests/support.py` is
   shared: a sub-step edits only the functions it names, by exact match.

   **The sequence.** ∥ marks what could run concurrently.

   *Group 0, serial*

   5.1 **`machines/`.** *Landed* (`lib/wk/fleet.py`, the one reader; a machine
     whose conf name is not its `hostname -s` says which in
     `WK_REMOTE_HOSTNAME`). `cmd/ls`, `cmd/disk` and `status.py`'s
     `remake_hint` ask `Registry.in_remote_host()`/`self_target()` (fleet's
     `named_by_host`) instead of the remote marker's `target=` line, which
     `remote/provision.sh` no longer writes (`root=`/`inputs=` stay: `stale`
     still reads `inputs=`); `targets.py`'s `Remote.is_local` follows suit,
     since it was the marker's other reader of that line. README's spec half
     names `machines/` alone. Owed: none.
   - Closes: `unit machine_cmd.shared_home`, meaning each machine resolves
     its own target by hostname with no ssh.
   - Decision: `KIND` plus the old keys verbatim, one file per CLI name.
     Renaming the keys waits for 5.39, once no bash reads them.

   *Group 1, all ∥ after 5.1*

   5.2 **The bash libraries become shims.** *Landed* (`record.py`,
     `job.py`, `resources.py` and `lock.py` hold the one copy;
     `machine.far_side_start` is the one far-side start line, `Ssh.spawn`
     and `job.remote_line` both calling it).
   - Closes: `unit record.hold_names_its_taker`, a hold naming its taker on
     one path.
   - Decision: `device_hold` becomes a hold named `device:<machine>` on the
     holder's own record, not a separate lock file.

   5.3 **The dispatcher's last bash asks, and the tooling push.** *Landed*
     (`Target.state`/`wait_ready` through the machine, `lib/wk/tools.py` the one
     push, `Registry.local_workspaces()`; `doctor.gh_authenticated` asks
     `gh` directly; `Target.wk_cmd` builds every far wk's command line, the
     podman-machine hop's and a machine's alike; `delegate_run` execs `ssh`
     over the `Remote`'s own machine; `dispatch.json_merge_list` is plain
     `json`). Owed: none.
   - Closes: `unit killpoints[new]` over the real drivers,
     `unit machine.dead_creation_refused_at_once`.
   - Decision: one push for every kind. The guest gets the same bundle; its
     `_push_tools` goes.

   5.4 **The store, the mirror and the PR fetch.** *Landed* (`lib/wk/git.py`,
     `lib/wk/pr.py`, `store.Bases`). `targets.py`, `build.py` and `doctor.py`
     call `wk.git` directly and `cmd/ls` uses `store.Bases`.
     `tests/test_pr_workflow.py`
     holds `killpoints[pr]` (checkout onto a fork's branch, `pr_rebase`'s
     fetch and rebase, and `pr_open`'s push and `gh pr create`, each killed
     after any effect and rerun converging) and dry-run-equals-wet-run for
     the plain checkout; `cmd/pr` declares `dryrun` for that form only --
     `rebase` and `open` mutate through plain `Target.exec`, not `act_exec`,
     so their own sub declarations turn it back off. Owed: none.
   - Closes: `unit new.refuses_without_mirror`, `unit
     cli.refspecs` (the owed module).
   - Decision: the wiring runs as git argv lists through `Target.exec`, not
     as script text sent into the target.

   5.5 **Image profiles as data, and each image's workspace.** *Landed*
     (`lib/wk/images.py`). A PGO run reads a profile's own `IMG_MACHINE`
     (two arms of one board, or the `-oc` profile, share the fleet machine
     their stock profile names), and the `-oc` profile's `config.txt.append`
     is the one place that sets `arm_freq`/`v3d_freq`/`over_voltage_delta`.
     Owed: none.
   - Closes: `lint.profiles_are_data`, `unit sysimage.oc_profile_in_image`,
     and the "lane" half of `lint.vocabulary`.
   - Decision: "lane" is retired. It is the image's workspace
     (`<builder>-<profile>[-<arm>]`), named in code `image_ws`.

   5.6 **`wk machine` for build and peer machines; reach.** *Landed*
     (`cmd/machine`, `lib/wk/machine_cmd/`, `lib/wk/reach.py`; `wk remote` and
     `wk find` are tombstones; every caller imports `wk.reach`/
     `wk.machine_cmd` directly). Owed: none.
   - Closes: `unit killpoints[machine setup]`, `killpoints[machine rm]`,
     `unit machine.probed_once_per_invocation`,
     `unit machine.unreachable_is_named`, `live machine_cmd.setup[<box>]`.
   - Decision: a first `machine setup <name>` with no conf requires
     `--kind`; after that the conf's `KIND` is the answer. A probe never
     guesses a kind.

   5.7 **The boot-driver core.** *Landed* (`lib/wk/boot/`, the on-board
     shell in `boot/onboard/`). The transport is `driver.Channel` over `Machine`s (`Ssh` to NODE_SSH
     or the bench system's found address, the driving machine itself when
     standing on it), the card and boot helpers through the same machine under
     `sudo -n` only where the answering system is not root; `wk.boot.open_driver`
     gives each driver its own (the Macs' `mac.Channel`, the guest's over the vm
     target), and `FakeBoard` is that Channel over two fake `Machine`s.
     `part`/`disk_of`/`partno` have one implementation, in `driver.py`, which
     `wk.sysimage.disk` imports. Owed: none.
   - Closes: `unit machine.conformance[pi-sd|pi-tryboot|rpi5-usb]`,
     `unit boot.arming_exact`.
   - Decision: every driver's on-board shell (`b_self_disarm_sh`, tryboot
     staging) becomes a file under `boot/onboard/`, read verbatim. That is
     where lint counts the on-board budget, and no driver builds shell by
     string. Delete `pi-mbr` (no machine declares it; listed under
     decisions).

   5.8 **Completion in the dispatcher.** *Landed* (`lib/wk/completion.py` generates
     the script from the declarations; its workspace slot calls
     `python3 -m wk.completion --list-workspaces`, the checkout root baked in
     at generation time).
   - Closes: `unit dispatch.help_previews_and_lists_values` for the flag
     lists.
   - Decision: workspace names come from `Registry.local_workspaces()`, which
     never reaches a machine. The hidden `--list-workspaces`/`--flags` verbs
     are gone.

   5.9 **`quiesce` and `session`.** *Landed as two Python commands*
     (`lib/wk/quiet.py`, `lib/wk/session.py`, the tables in `bench/quiet/`),
     `killpoints[quiesce]`, `killpoints[session]` and dry-run-equals-wet-run
     in `tests/test_quiesce.py` and `tests/test_session.py`; the live bodies
     read only, their mutating half owed. The merge waits for the user.
   - Closes: `unit killpoints[quiesce]`, `live quiesce.readback[<m>]`,
     `live quiesce.classified[<m>]`, `live session.modes[moose]`,
     `live doctor.bench_readiness[mbp]` (doctor reads `quiet.status`).

   5.10 **The bench record and report; `cmd/bench` goes Python.** *Landed*
     (`lib/wk/bench/`; `lib/wk/status.py` imports `wk.bench.record`
     directly). Owed: the report does not yet label an instrumented leg's
     time (env.json's `profile` field means two things).
   - Closes: `unit bench.one_record[container]`, `unit
     bench.report_and_cost` (the report half), `unit bench.seed_from_mirror`,
     `live bench.compare` (`tests/test_bench_container_run.py`).
   - Decision: the task directory stays where it is (`task.json`,
     `runs/<run>/`). `record.py` takes the directory as a parameter, so step
     6's move into the workspace is one line.

   *Group 2*

   5.11 **`wk boot`.** *Landed* (`cmd/boot` over `lib/wk/boot/cli.py`; `--boot-order` and the pinned
     recovery.bin path in `lib/wk/boot/eeprom.py`; `wk status`'s fleet probe is `python3 -m wk.boot.cli
     fleet-probe`; `wk pi boot-order`/`helper` and `wk boot --prepare` are tombstones). `--boot-order` takes the order by name, and
     `--revert` is `local`. The recovery.bin staging goes through the driver Channel's `Machine`, and an arming
     record with no boot id reads as unknown, never by clocks. Owed: the live bodies of `boot[rpi3|rpi4|rpi5]` read
     `--status` only, since the live tier reboots nothing, so the arming halves of those rows, `boot[rpi4].kms`,
     `boot[rpi4].armhf` and `boot.firstboot[<b>]` need a person at the boards.
   - Closes: `unit killpoints[boot]`, `unit status.armed_transition`,
     `unit status.web_mirrors_text`; marks live `boot[rpi3|rpi4|rpi5]`,
     `boot[rpi4].kms`, `boot[rpi4].armhf`, `boot.firstboot[<b>]`.
   - Decision: `--prepare` leaves `boot`. Putting the tree and helpers on a
     machine is `machine setup mbp`.

   5.12 **Mac boot drivers.** *Landed* (`lib/wk/boot/mac.py`, over
     `lib/wk/mac.py`, which runs on the Mac as `python3 -`). A failed
     `--disarm` refuses instead of clearing the record.
     `lib/wk/bench/mac.py`'s `MacVolumeSystem` asks
     `driver_class(...).measures` and writes it into `env.json`
     (`bool_facts()`'s `measures=`). `Vm` and `lib/wk/boot/mac.py` both take
     `guest.DISPLAY`. Owed: none.
   - Closes: `unit machine.conformance[mac-volume|mac-guest]`; marks live
     `boot.arm[mbp]`.
   - Decision: keep `mac-guest`, since the rehearsal row needs it. It
     conforms like the real driver, and its readings are refused as
     measurements.

   5.13 **`machine setup` and `rm` for a board; `machine setup mbp`.** *Landed*
     (`lib/wk/machine_cmd/`'s `setup_board`/`rm_board`/`setup_mac`, over the
     generic `Machines.answers()`/`board_machine()`; `wk boot <mac> --prepare`
     is a tombstone naming `wk machine setup <mac>`). `setup <board>` checks the tailnet name
     answers and installs `admin/wk-card-priv`/`boot/check-boot-files.py` at
     `/usr/local/libexec/`; `rm <board>` removes them and the conf, asking once
     each. `setup <mac>` pushes this tree
     through the one `tools.push` (lib/wk/tools.py), then runs
     `./setup --stage quiesce` from a terminal, refusing (not merely warning)
     when there is none. Every one of these prints what it would do under
     `--dry-run` rather than refusing, even when the unit tier's ssh shim
     reports the machine unreachable -- real unreachability still refuses
     outside `--dry-run`.
   - Closes: `unit killpoints[machine setup]` for a board
     (`TestBoardSetup.test_a_setup_killed_after_any_effect_and_rerun_converges`).
     Owed: `lint.no_addresses` -- `NODE_MAC` still lives in the board confs
     (closing it needs one pass across those plus every
     bench-kind conf, since `TestConfFieldSets` holds one field set per
     kind); marks live `pi.setup[<board>]` (needs a board in hand).
   - Decision: `setup <board>` checks that the tailnet name answers, installs
     this checkout's card helper and writes `KIND=board`. A board never gets
     tailscale any other way. `setup mbp` never fakes reachability: an
     unreachable Mac still refuses outside `--dry-run`, matching every other
     kind `wk machine setup` takes.

   5.14 **sysimage read side; `cmd/sysimage` goes Python.** *Landed*
     (`lib/wk/sysimage/`; `rm` is a tombstone). Bench's and sysimage's
     `Listing` share one fleet walk (`lib/wk/fleetwalk.py`; each `Listing`
     keeps its own `store_rows` and row-label rule); `ls.human_bytes` is the
     one byte formatter; `cmd/sysimage`'s `# wk:` line takes `outside`, so
     the dispatcher refuses `wk sysimage` inside any workspace.
     `ls` also lists the mac-volume and guest builders (5.32, 5.34), through
     `ls.host_profiles`/`ls.builder_outputs` -- the one `builder_outputs`
     `cli.Sysimage` delegates to -- shown only once found, since neither has a
     workspace to anchor a placeholder row at. Owed: `live sysimage.build[vm]`
     has no body.
   - Closes: `unit sysimage.builders_conform` (the read half); marks live
     `sysimage.build[vm]` (reaching the host).
   - Decision: an image is found as `Builder.outputs(ws)` per builder kind,
     recomputed on every read. There is no manifest.

   5.15 **sysimage disks.** *Landed* (`lib/wk/sysimage/disk.py`; sysimage imports boot,
     never the reverse). Only `lsblk -J` is parsed: `admin/wk-card-priv` is
     Linux-only, so no writer is a Mac and a `diskutil` parse would have no
     caller. Owed: none.
   - Closes: `unit sysimage.write_refusals`.
   - Decision: `lsblk -J`/`diskutil -plist` run on the writer through
     `Machine`, and the parse runs here in Python.

   5.16 **sysimage write.** *Landed* (`lib/wk/sysimage/write.py`; systemd units in
     `boot/firstboot/`, init scripts in `boot/onboard/`). The confirm now comes
     before the first card change, and `LABEL=`/`UUID=` roots are retargeted to
     the written disk's PARTUUID; neither has run on a board. The auth key and
     a stale node's retirement go through `wk.tailnet.Fleet` in process (so
     does `mactailnet.py`'s key). Owed: `lint.one_wifi_reader`
     (`lib/wk/sysimage/pmos_build.py` and `lib/wk/sysimage/macvolume.py` read
     WiFi themselves).
   - Closes: `unit sysimage.write_identity`, `unit killpoints[sysimage
     write]`, `lint.one_wifi_reader`; marks live `sysimage.write[<b>]`,
     `sysimage.card_verbs[rpi5]`.
   - Decision: the first-boot units are files in `boot/onboard/`, templated
     only by the `KEY=value` lines the card helper writes.

   5.17 **sysimage build task, buildroot, fetch.** *Landed* (`lib/wk/sysimage/task.py`,
     `buildroot.py`, and `buildroot_target.py`, the in-workspace half, run as `python3
     /opt/wk-tools/lib/wk/sysimage/buildroot_target.py image|webkit` under `task.stage_main`; it folds
     in the tailnet and wifi overlays, and `buildroot.py`'s `kernel_pin` replaces `kernel-pin.sh` on the
     driving machine). What buildroot itself executes stays shell and counts in the on-target budget: the
     post-image hook `buildroot_target.py` writes for a pinned kernel, and the overlay init scripts
     (`image/buildroot/overlay/etc/init.d/`); the memory guard stays `build/guard.sh`, reached as one
     `bash -c` line. `wk build`'s far argv goes through `task.in_workspace`, closing `lint.build_wall`.
     Nothing of the in-workspace half has run in a workspace yet. Owed: a live image and slot build
     (hours, past the runner's per-test budget).
   - Closes: `unit sysimage.task_states`, `unit killpoints[sysimage build]`,
     `lint.build_wall`, `unit record.progress_shape[sysimage]`.
   - Decision: one detach and one watchdog, `build.py`'s.

   5.18 **yocto, host half.** *Landed* (`lib/wk/sysimage/yocto.py`, a `task.Stage` with
     no deadline on its record; `job.watch_pid` gives up on silence only when told to, and on a
     wedge of `WEDGE_BEATS` heartbeats naming one bitbake task). The cross configs live here; a
     yocto stage refuses a held workspace lock instead of waiting an hour. `Yocto` and `Buildroot` are `task.ContainerBuilder`s: the target refusal, the
     digest-tagged host image and the workspace it makes are one implementation, and each subclass
     is its data and its stages. Owed: `live sysimage.sstate_reuse` has no body -- a second image build is minutes to hours, past the
     runner's per-test budget; `WEDGE_BEATS` (4 h) is a guess no wedged run has been measured
     against; `WK_YOCTO_BASE` is not in `wk sysimage -h`.
   - Closes: marks live `sysimage.sstate_reuse`.
   - Decision: "silent" and "wedged" are `job.stall_report` verdicts over the
     same heartbeat, not a second yocto rule.

   5.19 **yocto, in-target half.** *Landed* (`lib/wk/sysimage/yocto_target.py`, with
     port-target folded in; the memory guard stays `build/guard.sh`, reached as one `bash -c`
     line). Nothing of it has run in a workspace yet. Owed: `live sysimage.pseudo_reproducer` has
     no body -- it needs a pseudo built at scarthgap's pin (e11ae91), which nothing builds, so
     whether to build one for the test or drop the bump at the next poky move is the user's call.
   - Closes: marks live `sysimage.pseudo_reproducer`.
   - Decision: port it; the container has python3. Bitbake is reached as
     `bash -c '. oe-init-build-env && bitbake …'`, one line.

   5.20 **pmos builder.** *Landed* (`lib/wk/sysimage/pmos.py`, the driving `Pmos`
     class `cli.py`'s `build()` routes a pmos profile to, and
     `lib/wk/sysimage/pmos_build.py`, the build itself. The far half imports
     `wk.*` plainly: the driver pushes this checkout's `lib/wk` to the build
     host (`copy_tree_in`, so it needs rsync) and runs `PYTHONPATH=<root>/lib
     python3 -m wk.sysimage.pmos_build remote-build|wifi-ssid`, every process
     through `here()` and every wait through the clock. Its one non-stdlib
     import is PyYAML, for the netplan it reads the phone's uplink from --
     netplan's own dependency (`host/linux/apt.txt`), with no stdlib YAML
     reader. The image comes off through `Machine.copy_out`, is decompressed
     by `xz -d` here and hashed in blocks. `wk gc`'s pmos rows are built from
     `cache_probe`'s `{work, out}` numbers, and `purge_work` asks nothing: gc's
     one question covers it; there is no mDNS. Owed: no live check (needs an aarch64
     Linux build host and a phone); `test_owed_pmos.py` covers the reporting,
     refusals, host resolution, the copy and the rubble rows against the Fake,
     and `pmos_build.py`'s pure parts, not `Build.run` or the `iw scan` band
     check -- pmbootstrap, loop devices and sudo have no fake to run against;
     `lint.one_wifi_reader` stays owed for `pmos_build.py`'s netplan read.
   - Closes: the `test_owed_pmos` rows.
   - Decision: pmos stays. It exists only for the bridge phones, which
     5.37 needs.

   *Group 3: the bench pipeline*

   5.21 **The `System` interface and the workspace run.** *Landed*
     (`lib/wk/bench/systems.py`: container and guest; `pipeline.py`: `wk bench
     run`). Owed: none.
   - Closes: `unit bench.pipeline_conformance[container|guest]`,
     `unit bench.pins_cores`, `unit killpoints[bench]`,
     `unit record.progress_shape[bench]`,
     `unit dispatch.dry_run_is_the_recorder[bench]`.
   - Decision: `run` leaves its run directory on the system's machine, and
     `collect` copies it through the one `Machine` copy. Where run-benchmark
     runs is each system's choice, not the pipeline's.

   5.22 **Board deploy.** *Landed* (`lib/wk/bench/board.py`'s `BoardSystem`, reached as `wk bench
     deploy <ws> <board> [--slot <name>]` through `lib/wk/bench/cli.py`'s `Bench.deploy` and `cmd/bench`'s
     `deploy` sub; `wk pi deploy` is a tombstone naming `wk bench deploy`). `<ws>` is the image's
     workspace, not a `<profile>[@<machine>]` spec: the dispatcher's ordinary `where=workspace`
     routing finds the machine holding it and forwards there. Owed: `live bench.routed_deploy`
     needs a board in hand, so it stays unwritten.
   - Closes: marks live `bench.routed_deploy` (needs a board in hand).
   - Decision: deploy is `Machine.copy_tree_in`, then the slot's sha
     manifest read back. There is no rsync path.

   5.23 **Board run.** *Landed* (`lib/wk/bench/board.py`: `BoardSystem`'s `boot`/`checks`/`deploy`/`run`/`evidence`
     and `BoardRun`, reached as `wk bench run <ws> <plan> --system <board> [--slot]` through `systems.for_workspace`
     and `pipeline._run_class`; the driver moved to `lib/wk/bench/board_driver.py`; the board's shell is
     `boot/onboard/bench-*.sh`; `Machine.forward` on `Ssh` and `Fake`; `pipeline.Run`'s `records()`/`put()` make a
     board run's record and device claim this machine's). The armed barrier is `BoardSystem.barrier`, asked by
     deploy and run through the boot driver; a deploy lands on the bench system (the boot driver's `i_ssh`
     machine, not NODE_SSH) and claims the board. In a workspace, deploy and a `--system` run are the broker's
     `stage`/`run` verbs, `wk bench deploy`/`wk bench run` on the host (`boot/cli.py`'s `broker_request`, shared
     with `wk boot`). `wk pi bench` is a tombstone. Owed: `live
     bench.leg_completes[<b>]` needs a person at a board (a leg pins the clock, kills browsers and starts a
     compositor, which the live tier may not); `live bench.evidence[<b>]` has its read-only body
     (`TestARealBoardAnswersWhatALegRecords`, not yet run against a board); whether the podman VM a Mac forwards a container lane into can reach a
     board over the tailnet is the ACL question, unchanged and yours.
   - Closes: `unit bench.pipeline_conformance[board]`,
     `unit bench.failed_leg_keeps_evidence`,
     `unit record.progress_shape[board run]`; marks live
     `bench.leg_completes[<b>]`, `bench.evidence[<b>]`.
   - Decision: the benchmark server reaches the board through
     `Machine.forward(port)`, a context manager on `Ssh` and `Fake`. The
     ACL question stays yours; this is the one place it changes.

   5.24 **Two-arm A/B on one board.** *Landed* (`lib/wk/bench/board_ab.py`, reached as `wk bench run
     <ws> <plan> --system <board> --ab A,B | --ab-systems A,B [--slot] [--rounds] [--task] [--timeout] [--exclude-subtests]
     [--no-warmup-profile] [--jit-tiers]` through `pipeline.run`; each leg is a `BoardRun` named `<ws>-leg` under the A/B's
     own record, which holds the board and is what `--kill` stops). The system boot per leg is `AB.boot`, over `wk boot`'s
     own `Boot.arm`/`back`, deciding by the driver's `arm_from_bench` where the bash tried and fell back; a system A/B ends
     by handing the board back to its rescue and clearing the arming record there. `BoardSystem` pins the clock, claims
     the board and brings the session up once per (system id, boot id) and re-reads the probe, facts, display and slot
     every leg; `BoardRun.pin` resolves the runner tree and payload once per A/B; a slot A/B's task records
     `devices=<board>=<profile>`. The width rule and `bench/subtest-exclusions.conf` are read in Python. `--pgo` is `wk
     bench run ... --slot <s>-instr --collect` (`BoardRun.collection`, reading `lib/wk/pgo.py`'s facts). `wk pi` is a
     dispatcher tombstone naming each verb's replacement, and the device claim is the one lock. Owed:
     `killpoints[bench]` over a whole A/B is not written (a single
     leg's is); `live boot[rpi3]`'s `--ab-systems` run needs a person at the board; an A/B or a collection typed in a
     workspace is refused, since the broker has no verb for either.
   - Closes: `unit boot.two_system_lane_on_fake` (renamed
     `boot.two_systems_on_fake`).
   - Decision: `--ab` slots and `--ab-systems` are one arm type, (system,
     slot); a slot-only A/B holds the system fixed.

   5.25 **`bench ab` across the fleet.** *Landed* (`lib/wk/bench/ab.py`, reached as `wk bench ab
     <pr-spec|branch|sha> --devices <a,b> ...`, `wk bench ab --systems A,B --devices <board> [--slot S]` and `wk bench ab
     <task> --kill` through `lib/wk/bench/cli.py`'s `Bench.ab`; `cmd/bench`'s `flag --kill takes=0` line, which refused
     every name=none subverb's `--kill`, is the `sub ab` line; `wk ab` is a dispatcher tombstone). The scheduler is
     `lib/wk/sched.py`, run in-process on an injected executor: each build, deploy and collection step is the `wk` command
     a person types, run through `Machine.act_run` with its output streamed to one log per resource, each board's rounds are
     `board_ab.run` in this process recording into the A/B's task, and the report is `report.task_report`. The kill is
     `job.kill` over the record's process tree. A plan states its cost before it runs: each board's legs times the median
     measured leg of that plan there (`leg_seconds`, scaled by `--count`). The report's pairing is
     `lib/wk/bench/record.py`'s `paired`, which drops a round an arm did not finish or whose arms ran on two payload pins
     (runner commit, benchmark copy). Owed: the in-process board A/B steps log to this process's
     stderr rather than a per-board file, so two boards' rounds interleave there; `--systems` takes one board, since a
     system id names one board's image; an A/B across real boards (`live bench.leg_completes[<b>]`) needs a person at
     them.
   - Closes: `unit ab.plan_and_pairing`, `unit killpoints[bench ab]`,
     `unit bench.report_and_cost` (the cost half).
   - Decision: the scheduler runs in-process, and each step is a callable
     whose effects go through `Machine`, so kill points land inside a step.

   5.26 **Board PGO.** *Landed* (`lib/wk/pgo.py`: the facts as Python data -- `BENCHMARKS`, `GLIB_LIB`, `BOARD_DIR`,
     `BOARD_FILE`, `collect_timeout` -- that `board.py`'s `collection`, `yocto.py` and `ab.py` import; `steps`, the cycle's
     graph, which `ab.py`'s `pgo_steps` and the cycle both run; `Cycle`, reached from `cli.py`'s `webkit` for a 2.52+ yocto
     profile: `--config` is one phase as one yocto stage, no `--config` the whole graph under one `pgo` record in this
     process, `--stop` its kill, the dry run the rendered graph; and the mixer and gate, `python3 -m wk.pgo mix|check`).
     A collection is `wk bench run <lane> <plan> --system <board> --slot <s>-instr --collect`. `sched.wk_step`/`wk_yes`
     are the one `wk`-command step and done question both graphs build from. Owed: `unit pgo.no_local_patch` stays owed, since upstream's OSXMiniDriver still
     names no profile directories and webkitpy's `locate_binary_xcrun` still runs `/usr/bin/xcrun` off macOS, so
     `build/pgo-run-benchmark.py` and the blunting stay; `live bench.pgo_collection[<b>]` is marked on its read-only half
     (the newest collection passes the gate with every leg's run), while recording each plan's cost and what the
     collecting browser rendered and JITted with is not written, and taking a collection needs a person at the board.
   - Closes: `unit pgo.no_local_patch`; marks live
     `bench.pgo_collection[<b>]`.
   - Decision: a collection is `bench run --collect` against an instrumented
     slot, called from `sysimage build`'s pgo phase. There is no
     `pi bench --pgo`.

   5.27 **Mac stage and staged run.** *Landed* (`lib/wk/bench/mac.py`: `Stage`,
     `MacVolumeSystem`, `StagedRun`, `Gates` as `wk bench staged --gates`; a
     guest's run records as `rehearsal`). A staged dry run whose checks fail
     exits 1. Owed: none.
   - Closes: `unit bench.pipeline_conformance[mac-volume]`,
     `unit bench.one_record[mac-volume]`,
     `unit bench.preflight_asks_every_gate`, `unit dispatch.where[bench]`.
   - Decision: `staged` is `bench run` on the bench install, which resolves
     itself as the system in bench mode.

   5.28 **`bench mac`.** *Landed* (`wk bench run <ws> <plan> --system
     mbp` is the pipeline run the decision below asked for, in `lib/wk/bench/mac.py`'s
     `MacHostSystem`/`HostRun`, registered in `systems.py`'s `_named_systems()` under the machine's
     `NODE_DRIVER` and reached through `pipeline.run()`'s new `system_name` argument and
     `cmd/bench --system=`. `boot()` stages the workspace's build onto the volume (5.27's `Stage`,
     unchanged), arms it and waits for bench mode through 5.11's `Boot`/driver `probe()`; `run()` is
     `wk bench staged` (5.27) invoked over the bench-mode ssh alias (`Channel.exec_argv`'s own
     resolution, so a self-driven Mac runs it locally like `wk bench staged` always has); `collect()`
     copies the newest `results/` entry back through one `Ssh.copy_out`; `after()` reboots back
     through the same `Boot`, and the task record (steps, log, `wk status`) is the same one a
     container or guest run writes -- no bespoke state file. `wk bench mac` is a tombstone in
     `lib/wk/bench/cli.py`'s `Bench.mac`, naming `--system mbp`, not a sequence of commands.
     `tests/test_bench_pipeline.py` proves it against a fake Mac (5.12's `FakeMac` as the channel,
     the pipeline's own ssh-argv answers for the rest): `unit bench.pipeline_conformance[mac-volume]`,
     `unit record.progress_shape[bench]`, `unit dispatch.dry_run_is_the_recorder[bench]` and
     `unit killpoints[bench]` (bounded to the effects before the machine reboots -- `Boot.arm()`
     refuses to re-arm an already-armed machine by design, so a kill after that point is a person's
     call, not a rerun's, on real hardware or fake). Owed: `live bench[mbp]` and `live bench.first_run_after_stage[mbp]` close
     only their read-only half (`tests/test_owed_bench.py`'s `TestBenchReachesTheRealMbp`, gated
     `requires_machine("tolken")`) -- staging a real build, rebooting mbp into bench mode, running a
     plan and comparing against a container run needs a decision about which workspace and plan to
     spend that machine's time on, left to whoever runs the live tier next; `MacHostSystem.after()`
     cannot bless the machine back to host mode from its own benchmark install (only the host
     install carries the boot helper -- measured against 5.12's own driver, not theorised), so a
     real run stops there and names the two-click remedy, same as arming does.
   - Closes: marks live `bench[mbp]`, `bench.first_run_after_stage[mbp]`.
   - Decision: `bench mac` is the pipeline run with `--system mbp`, not a
     verb of its own. `mac` stays only as a tombstone.

   5.29 **Mac A/B, front half.** *Landed* (`lib/wk/bench/mac.py`'s `MacAB`, reached as `wk bench ab --devices <mac>
     --systems <staged-a>,<staged-b>` or `--patch <ref|diff> --workspace <ws> [--base <ref>]` through `ab.run`, which hands
     a `KIND=mac|guest` machine to it; `ab.check_plan` is the plan refusals both share, `board_ab.pair` the arms', and each
     side refuses the other's options). Preflight, build-and-stage in the guest (the patch travels inside the guest
     script), reclaim, the plant (task record first, tree verified by digest, screensaver, Do Not Disturb, samply,
     tailnet payload, job, state, LaunchAgent) and the restart with its wait for which install came up are Python over
     the driver's channel, the drivers' `manager()`/`manager_tools()` and the fake clock. `build/mac-pgo.sh` keeps its
     two build phases (they are build-in-target.sh's `_xc_settings`/`guard_run`) and asks `PgoCollect` for the
     collection, the evidence and the instrumented directory's name; `PUT_SKIP` is the one list. The preflight names
     `wk machine setup` rather than running it (a read-only check does not provision). Owed: the live rows close only their read-only
     halves (`tests/test_mac_ab_driver.py`'s `TestTheLiveRows`: each machine's preflight) -- building, staging and
     measuring a real pair on mbp and the rehearsal on benchvm spend hours of those machines, a decision for whoever
     runs the live tier; the plant's display count (`display_verdict`) and each leg's (`bench/mac-browser-check.py`)
     are two readers.
   - Closes: marks live `ab.pgo_pair[mbp]`, `bench.rehearsal[benchvm]`.
   - Decision: the Mac A/B is `bench ab` whose arms are staged ids. It
     shares 5.25's plan, pairing and refusals.

   5.30 **Mac A/B, back half, and notify.** *Landed* (`lib/wk/bench/mac.py`'s `MacAB.back`, reached as `wk bench ab
     --devices <mac> --preflight|--progress|--status|--collect` through `ab.run`; a board refuses them, and `wk bench
     mac-ab` is a tombstone in `cli.Bench.mac_ab`). `--collect` copies each clean measured leg off the volume onto the
     plant's task (a base64 tar per leg over the probe's channel, `record.write_env` pairing it), then prints
     `report.task_report`; `report.ab_summary` is `wk bench ab-summary`, which the autorun asks for on the volume. The
     stopping rule is `board_ab.stopping` (`--rounds` the floor, `--detect` the precision, `--max-rounds` the ceiling)
     and `report.resolved`, asked by `board_ab.AB.measured` between rounds on a board and carried through `wk bench ab`
     and `wk bench run --ab`; a board's default is 0, a Mac's `mac.DETECT`. Every on-Mac command is a named
     `bench/onboard/mac-*.sh` taking `KEY=value` (`mac.OnMac`; `boot/mac.py`'s `Script.where`), and a tree file runs by
     `mac-py.sh` as a parameter, since a guest's channel carries no stdin. `lib/wk/notify.py` is the one sd_notify and
     `send` (the credential rule calls it in-process). Owed: `tests/test_host_units.py`'s
     `test_one_sd_notify_in_the_tree` reads `git ls-files`, so it goes green once `lib/wk/notify.py` is
     committed; the live rows close only their read-only halves (`tests/test_mac_ab_rounds.py`'s
     `TestTheLiveRows`: mbp's steps and its legs) -- resolving a PR-sized delta on mbp and a warmup profile on every
     system spend hours of each machine, a decision for whoever runs the live tier.
   - Closes: marks live `ab.resolution[mbp]`,
     `bench.warmup_profile[<s>]`.
   - Decision: stopping at `--detect` is `report.precision` asked between
     rounds on every system, not a Mac special.

   5.31 **The bench-install autorun in Python.** *Landed* (`lib/wk/bench/autorun.py`'s `Autorun`, started by the
     launch agent as `/usr/bin/python3 /var/wk/wk-tools/lib/wk/bench/autorun.py`: `mac.py`'s `AUTORUN`/`PLIST` and the
     plant's check name it). Every effect goes through `Machine` (`Local` on the install), every wait through the clock;
     a logged command (a leg, the quiesce, the convergence, the browser check, the summary) streams into the run's log
     through `run_tty`, and the watchdog is a thread reading the log's mtime. The convergence calls
     `python3 -m wk.sysimage.macvolume stage-payload /` and `python3 -m wk.sysimage.mactailnet install / <vol>`
     directly; the tree the autorun runs from is its `wk-tools`. The job's outcome is the stopping rule's
     (`resolved-at-round-N`, `hit-max-rounds`, `rounds-done`, `all-failed-round-N`). `tests/test_mac_autorun.py` runs it against the Fake and the fake clock: each
     phase, each refusal, the drift refusal after the quiesce, the settle, the hold, the watchdog, the hand-back, a run
     killed after any effect finishing on the next boot, and the dry run. Owed: nothing has run on the Mac: a planted job measured end to end
     spends hours of mbp, a decision for whoever runs the live tier.
   - Decision: port it, because firstboot guarantees python3 before the
     LaunchAgent can fire. `mac-bench-firstboot.sh` stays shell.

   5.32 **The Mac volume as a `sysimage` builder.** *Landed* (`lib/wk/sysimage/macvolume.py`,
     `mactailnet.py`; `wk sysimage build perf-macos-tolken [--create|...|--all]`, and `wk bench mac-volume`
     is a tombstone). The pin lives in `mactailnet.py`. The join stays shell in `bench/mac-tailnet.sh`: the
     first boot joins before the install has a python3; `bench/mac-pyobjc.sh` stays shell, since
     `vm/provision-base.sh` pipes it into a guest with no tree. Owed: `holds`, `path` and `ls` (5.14) reach the
     volume's marker through `cli.Sysimage.builder_outputs`/`ls.builder_outputs`, shown only once installed
     since a builder with no workspace has nothing to anchor a placeholder row at (`test_every_builder_
     a_profile_names_has_outputs` now closes for every builder, including pmos and fetch); the WiFi read
     is still the keychain's (`lint.one_wifi_reader`: the card helper is Linux-only
     and gates usb/mmc disks, so taking the Mac's read is a decision for you); the Go cache moves to the
     state directory on a Mac host. Nothing has run on the Mac.
   - Closes: `unit sysimage.builders_conform[mac-volume]`; marks live
     `sysimage.mac_volume_provision`.
   - Decision: the install's `/etc/wk-image` marker is the builder's done
     marker, the same as a board's.

   *Group 4, ∥ Group 3*

   5.33 **Guest start and stop, and the host daemons.** *Landed* (`lib/wk/guest.py`:
     `Host` and `Guest`; `Vm.write_marker` is the one marker writer). Owed: none.
   - Closes: `unit record.one_lock_per_resource[guest start]`; marks live
     `vm.egress[<c>]`, `vm.shared_mirror`.
   - Decision: the proxy, inject and agent daemons are `Machine.spawn`ed
     with pidfiles, which are locks, not state. The same code serves every
     guest.

   5.34 **Guest create and destroy, and the base as a builder.** *Landed* (`lib/wk/sysimage/guestbase.py`, the `guest` builder of
     `image/configs/macos-guest-base.conf`; every converge step, the admission, the
     desktop and load findings and `wk doctor <guest>`'s rows in `lib/wk/guest.py`;
     `Vm.start` writes the alias; `vm` is a dispatcher tombstone). Owed:
     `vm/desktop.sh`, `vm/provision-base.sh` and `bench/mac-pyobjc.sh` still
     name `wk vm` in their messages, since editing a base input re-stales every
     base -- fold it into the next rebuild; `holds`, `path` and now `sysimage ls`
     (5.14) reach the sealed base's marker through `cli.Sysimage.builder_outputs`/
     `ls.builder_outputs` and `guestbase.Base.outputs`, shown only once sealed
     since a builder with no workspace has nothing to anchor a placeholder row
     at; `live vm.base_matches_pin` fails on the one Mac until its
     base is rebuilt (sealed before an input change on this branch).
   - Closes: `unit vm.base_rm_asks_twice`, `unit killpoints[vm base]`,
     `unit sysimage.builders_conform[guest]`; marks live
     `vm.base_matches_pin`, `vm.desktop`.
   - Decision: `vm ls`, `check` and `ip` go (to `ls`, `doctor <ws>`, and
     nothing, since a guest is reached by its alias). The base's staleness
     stays a recomputed inputs hash.

   5.35 **Bridge read side.** *Landed* (`lib/wk/bridge.py`: `ls`, `status`,
     `battery`, `resolve` (the conf name, then `wk.reach`'s sweep of this
     machine's segments, and the ssh destination it finds) and `judge` (the facts a healthy/unhealthy bridge reports);
     `bridge/bin/wk-bridge-healthcheck` trimmed to raw `key=value` facts,
     no judging, no ANSI; `lib/wk/status.py`'s bridge probing
     (`BRIDGE_PROBE`, `bridge_record`) reads the same facts through
     `bridge.judge`; `lib/wk/fleet.py`'s bridge kind fills the `BR_*`
     defaults). Owed: `live bridge.segment[<bridge>]` waits on a person at
     the phone; `wk.reach`'s sweep
     needs ip(8) and nmap, so a macOS host finds a phone off the tailnet only
     by `--at`.
   - Closes: `unit bridge.segment_down_vs_off`.
   - Decision: the phone prints raw facts and the judging moves to
     Python, which trims `wk-bridge-healthcheck`.

   5.36 **Bridge setup, tailnet and rm.** *Landed* (`lib/wk/bridge/` is a
     package: `__init__` the read half, `plan.py` renders every file, the
     manifest and the bundle from the conf and the facts the phone reports,
     `role.py` is `wk machine setup|tailnet|rm <bridge>` through
     `machine_cmd`'s kind dispatch, the tailnet join and the policy done from
     here; `bridge/provision.sh` is a 184-line busybox apply of `base` and
     `role`; `lib/wk/tailnet.py` with `Api.transport` as its one seam, every
     importer repointed). Owed: `live bridge.setup[moose-bmc]` is marked and
     has not run; the phone's output arrives when each phase ends, not as it
     runs; `rm` logs the node out and does not retire it through
     `wk.tailnet`. The auth key's mint-or-reuse rule is `wk.tailnet.Fleet`
     (bash asks it through `wk_tailscale_authkey` in `lib/common.sh`).
   - Closes: `unit killpoints[machine setup]` for a bridge; marks live
     `bridge.setup[moose-bmc]`.
   - Decision: the host renders nftables, dnsmasq and the init scripts from
     the conf, and the phone only applies them. The phone has no python3.

   5.37 **Bridge provision.** *Landed*
     (`lib/wk/bridge/provision.py`: `wk machine setup <bridge> --disk
     <machine>:<device> [--image <path>|--rebuild]` asks once, gets the
     newest `PMO_BRIDGE` build off its host (building it if none, or on
     `--rebuild`), hands it to `wk sysimage write --disk` as the one writer,
     prints the hands-on steps and waits up to 15 minutes for the phone (names
     every tick, a sweep once a minute) before `Role.apply`.
     `bridge/devices.tsv` is the device table (`wk.bridge.devices`); `wk
     machine status [<bridge>]` lists and reports bridges, `wk doctor`'s
     battery row reads `Bridge.battery` in process, and `bridge` is a
     dispatcher tombstone). Owed: `live
     bridge.segment[<bridge>]` is marked (health check and cable only) and
     has not run; the rest of that row -- the eMMC route end to end, a board
     getting its reserved address, the netwatch ladder, the dock at 480, the
     camera -- needs a person at the phone; the copied image lives under the
     store's artifact directory, under a per-bridge lock, and
     `bridge.provision.rubble` names one a kill left and `wk gc` takes it.
   - Closes: marks live `bridge.segment[<b>]`.
   - Decision: a card goes to `sysimage write --disk` only. Jumpdrive
     auto-detection is a second write path and goes.

   *Group 5, serial*

   5.38 **`wk gc` in Python.** *Landed* (`cmd/gc` over `lib/wk/gc.py`; the row
     shape is `lib/wk/rubble.py`, and `rubble()` is appended to `store.py`,
     `workspace.py`, `bench/seed.py`, `bench/mac.py`, `sysimage/pmos.py` and
     `sysimage/guestbase.py`; the podman images, ccache, runner trees, build
     outputs, board slots and remote mirrors are `gc.py`'s own). One question
     covers every row, the VM's half included (`wk gc --rows` there), and
     `wk disk` renders the same rows. Owed: the VM's half
     runs through `Container.wk`, not an effect, so no kill point lands inside
     it; half-made workspaces are found only on targets whose store this
     process reads, not on a build machine; no live test runs gc against the
     real VM, a board or tart; the pmos row sizes all of `out/`, not what
     `prune` takes. `wk disk` renders `Gc.rows()` once, grouped by kind, and
     counts the golden base and tart's cache as storage in its own rows; a
     failed `podman images` is a `why=` row; the VM's half gets `--yes` in its
     argv rather than an environment poke.
   - Closes: `unit gc.reclaims_or_names[<kind>]` for every listed kind,
     `unit killpoints[gc]`.
   - Decision: each module that makes rubble exports `rubble()` rows (what,
     size, the flag that takes it). gc and `disk` render one list.

   5.39 **The deletion.** *Landed.* No step-5 shim and no bash driver remains:
     `lib/common.sh` is the one bash library. What ran through the shims is Python: `wk sysimage
     disks` (`Sysimage.disks`), `wk bench plans` (`Bench.plans`), samply (`lib/wk/samply.py`),
     the container's ssh transport (`container/ssh-transport`, `Container.ssh_transport`),
     `cmd/selftest`, the broker's fleet and reach, the store a setup stage lays out
     (`python3 -m wk.targets store-init`) and the injector's read token (`python3 -m wk.secrets
     pat-converge`). `lib/wk/lock.py` is the one lock, and `wk_machine_name` asks
     `record.machine_name`, so `record.host_name` is the one
     reader of this host's name (`lint.one_machine_name_reader`, bash included). A timed-out
     `Local.run` kills the command's whole session. The setup stages read the store's paths and
     the envelope from Python (`eval "$(wk_py wk.store paths)"`, `wk_py wk.resources --os <os> <verb>`,
     `wk_py` in `lib/common.sh`); the Mac screen watch is `lib/wk/screen.py` (`blocker`, `restarted`,
     the `Watch` thread) over the window probe and the quiet table; `sysimage.write` reads a bench
     machine's conf through `boot.cli.load_conf`, the one reader. Step 2's done condition
     (`test_no_bash_file_parses_json`) passes: no bash file uses `jq` or inline `python3 -c ... import json`
     except `bridge/bin/wk-bridge-netwatch`, named there by its own narrow
     exemption (busybox ash, no python3, judging tailscale's own JSON with no host in the loop).
     The tart locator is `wk.targets tart` (`tart_path`), asked through `wk_py` by every shell
     reader and directly by `Vm.tart` in Python; the privileged-helper table is
     `lib/wk/priv.py`, asked by `doctor.Host` and, through `wk_py`, by `./setup` and `admin/install.sh`.
     `machines/*.conf` keys are lowercase (`fleet.parse_text` refuses an old uppercase one, naming its
     new spelling), and a target conf reaches its `WK_*` variables through the one table
     `targets.CONF_ENV` (one build config's own `cmake_<config>`/`build_args_<config>` included).
   - Closes: `lint.one_machine_name_reader`, `lint.vocabulary`, the `hostname`
     row, and step 2's done condition.

   **What stays shell** (it runs on a board, a phone, a bench install or in a
   target, before or without python3). Lint counts it by directory:

   | where | what | today | after step 5 |
   | --- | --- | --- | --- |
   | a board's first boot | `boot/onboard/`: self-disarm, self-return watchdog, rescue marker, tryboot staging | 141 | ~150 |
   | the Mac bench install before python3 | `bench/mac-bench-firstboot.sh` | 279 | ~180 |
   | the bridge phone (busybox ash, no python3) | `bridge/init.d/*`, `bridge/bin/*` | 642 | ~600 |
   | a build target | `build/guard.sh` (exec wrapper) | 54 | 54 |
   | **step-5 share** | | **1,116** | **~1,000** |

   The rest of the tree's on-target shell stays outside step 5, and neither
   part fits the 1k budget alone:
   - `container/`'s scripts are 947 lines, of which `firstrun.sh` 298 and
     `sdk-patches/apply.sh` 480 are portable, since a container has
     python3.
   - `build/build-in-target.sh` and `mem-watchdog.sh` are 225.
   - The three privileged helpers are 1,994, with `wk-card-priv` also on
     every rescue.

   The budget holds either by porting `container/`'s two large scripts to
   Python after step 5 or by raising it, and the helpers are budgeted apart
   and frozen at their size. Both are listed under decisions. Everything
   else in `image/`, `bench/`, `vm/` and `remote/` is Python once step 5 is
   done: the in-target builder halves (5.19, 5.20), the guest provisioning
   (5.34), the build-box probe and provision (5.6) and the bench autorun
   (5.31).

   Decisions for the user this step adds: `session` into `quiesce` (5.9);
   deleting the `pi-mbr` boot driver (5.7, already listed); the on-target
   budget above.

6. **Results.** A task's results live in its workspace, the task restarts
   from where it stopped, and the deliverables export as one archive
   (README, "results").

   Landed: every task lives in `ws/<name>/bench/<task>` of its workspace,
   on the machine holding it; a driver elsewhere reaches it through
   `Target.results` (`record.ws_home`), and `record.homes` finds the tasks
   here for `ls`, `report`, `status` and the cost estimate. The workspace is
   the one `wk bench run` names; for `wk bench ab` across boards, the first
   device's image workspace, the task recorded after its image step and
   each board's rounds a `wk bench run` there; for a `--systems` A/B or a
   Mac A/B, `--workspace`. A store's `bench/` is rubble
   `wk gc --purge-rubble` takes. A container writes its run through its own
   `/var/lib/wk/ws/<ws>` mount, and the store-wide `/bench` mount is gone.
   The report heads with the commit each arm measured against the task's,
   and a verdict per check (preflight, warmup, PGO reading), and an
   incomplete task names its `restart` command, which `wk bench ab` and a
   board A/B record. A board A/B restarted with `--task` skips the rounds the
   task holds with both arms. A staged Mac run carries its build's
   `wk-profile-check.json`, and a Mac A/B's collect copies the warmup
   captures, and a board run of a PGO slot carries the reading its image
   workspace's collection took. A restart with `--task` takes the task's
   lock unless the A/B driving it holds it (`WK_TASK_HELD`), and a one-run
   `wk bench run --task` runs again only when the task lacks its run ok.
   `wk bench export <task> [--to <dir>]` finds the task in this store or in
   a target's store of its own (the podman VM's, a build box's), reads it
   through `Machine.read_tree`, which refuses a link, and builds the zip here, recording where it
   went in the task (`unit test_bench_results`, `killpoints[bench export]`).
   `wk rm` refuses a workspace holding a task no readable export holds as it
   is now, and `--force` crosses it (`unit test_rm_results`); `wk doctor`
   names each workspace's `bench/` as backed-up beside the store's.
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
| Every mutating command, killed after any effect and re-run, converges on the declared final state (`new`, `rm`, `build`, `test`, `bench`, `gc`, `vm base`, `machine setup/rm`, `key`, `skills`, `backup`, `quiesce`, `session`, `boot`, `sysimage`, `./setup`) | 1 (helper), then each command's step | `unit killpoints[<cmd>]`, `live killpoints[setup]` |
| `wk gc` reclaims or names every kind of rubble: tagged container images nothing references, an abandoned `.tmp-*` seed, a workspace a killed selftest left, a staged build on the Mac volume, an instrumented slot on a board, a remote store's mirror, ccache and dead workspaces; and `wk disk` names what it can reclaim | 5 | `unit gc.reclaims_or_names[<kind>]` |
| A machine on another wk-tools sha or a dirty checkout is named with both shas, a delegated answer from it is reported as its own rather than merged, and the remedy is `wk sync --tools` once clean, "commit and push here first" while dirty | 2 | `unit status.version_skew` |
| `wk profile` records in every mode (sampling prints the tier breakdown, bytecode leaves one JSCProfile json, samply refuses with the host remedy above `perf_event_paranoid` 1, instruments records a `.trace`) and `--fetch` copies the recording out byte for byte | 3 | `live profile.modes[<mode>]` |
| rpi4 boots its bench system reliably: EEPROM sd-first with `BOOT_WATCHDOG_TIMEOUT` and `MAX_RESTARTS`, a medium that holds the bus under write load, the stick reproduced from the repo by `wk sysimage write --disk rpi4:/dev/sda`, and `--back` reaches the rescue | 5 | `live boot[rpi4]` |
| The Mac's bench volume runs the whole lifecycle: `wk boot mbp` against the real install, a stage from a guest onto it, a measured run, `wk quiesce status` before it, the screen watch during it, and `wk bench compare` against a container run | 5 | `live bench[mbp]` |
| One spelling per concept: a round is run-benchmark's round, a perf task is (name, stamp, description, ref), a device is a device, a slot is named for its arm and phase, a time profile is one thing; "lane" and the second spellings fail lint | 5 | `lint.vocabulary` |
| Every gate a bench run needs is asked over the running install before anything reboots (quiet desktop, quiesce readback, brightness, display mode, browser check, the staged dry run, the window in front, no other machine running), a reboot is only the transition, and a run that starts behind another window fails at its own gate | 5 | `unit bench.preflight_asks_every_gate` |
| A failed leg on a board leaves evidence readable afterwards: a persistent journal, browser and tunnel logs on every leg, a board-at-failure capture, and warmup evidence on a leg that timed out | 5 | `unit bench.failed_leg_keeps_evidence`, `live bench.evidence[<board>]` |
| Bash mechanics the Python core removes: a script read mid-edit, `capped`'s orphaned sleep holding `wk status` open, `wk key`'s two hops into the VM, `wk help hardware` tracking the drivers, `wk pick`, `wk mcp` | — | delete |
| A task's log is read through the machine that holds it, as its record is (`Task.verdict`'s mtime, `log_age`, `progress_line`, `first_error`, `Records.wait`), and `wk build <ws> --kill` finds a host-driven box build after its tools sync hands the far side a wk | 1 | `unit record.home_is_the_machine` (tests/test_record_home.py) |
| Every `WK_*` override is read once, documented where the user meets it and tested, or removed | 1 | `lint.wk_overrides` (tests/test_lint_wk_overrides.py; the once-read check is owed for WK_BENCH_ENV_PAD, WK_BENCH_PATH_PAD and WK_BOARD_PGO, each read in two lib/wk/bench functions, and the documented check for WK_BOARD_KILL, _LAUNCH, _PGO, _RESET, _SSH and _URL; the tested check passes) |
| No unit test asserts a wall-clock bound of its own (the interrupt test's 10 s, the lock tests' polls): the runner's budget is the one bound, and a test proves ordering with a fake clock | 1 | `lint.no_wall_clock_assertions` |
| A workspace whose creation died (container up, directory gone, nothing creating it) is refused at once by every command, naming `wk rm`; nothing asks the SDK's `wkdev-enter`, which waits 182 s on such a container before aborting, and `wk gc` names it as rubble | 1 | `unit machine.dead_creation_refused_at_once` |
| The live tier runs against the container target on Linux and macOS alike; no test is gated on the podman VM | 1 | `live` runner rule |
| A hold is released only when its holder is provably gone, an unreadable holder keeps it, and no child process inherits one | 1 | `unit record.hold_follows_holder` |
| A workspace name is resolved once per invocation and every machine probed at most once | 1 | `unit machine.probed_once_per_invocation` |
| Interrupting a command (Ctrl-C, a lost ssh) stops the process it started on the far machine and releases its holds | 1 | `unit machine.interrupt_stops_remote_process` |
| A scratch store never puts two targets on one directory, and one machine's task records live in one directory | 1 | `unit record.one_store_per_target` |
| Every mutating command honours `--dry-run` as the recorder: the plan and the run cannot differ, and a dry run fetches nothing | 1 | `unit dispatch.dry_run_is_the_recorder[<cmd>]`; `lint` tests/test_cli_shape.py `test_every_mutating_command_and_verb_has_a_dry_run` names the commands and verbs still refused it: `wk ai` |
| No state change in lib/wk or a Python command bypasses `Machine`/`act`: `bench/board_driver.py`'s own ssh and tar, `mac.py`'s WindowServer plist, `cmd/ai`'s session | 1 | `lint.effects_through_machine` (tests/test_lint_effects.py) |
| The dispatcher parses every argument: the build config, the subverb, `--target`, paths | 1 | `unit dispatch.parses_every_argument` |
| `wk <cmd> -h` previews the command line it would run and lists the values every config-taking flag accepts | 1 | `unit dispatch.help_previews_and_lists_values` |
| Every destructive effect is named in one question asked before it (a tailnet device delete, a replaced root-owned helper, a removed far destination, a reset SDK checkout, an overwritten credential), the default is No, no terminal declines, `--yes` answers, and a forwarded command carries the answer rather than exempting the receiver | 1 | `unit dispatch.destructive_asks_once[<cmd>]` |
| `--force` crosses only the barriers it names and records itself; a preflight that cannot be measured reports unknown, never failure | 1 | `unit dispatch.force_names_what_it_crosses` |
| Every long-running command (build, test, bench, image build, A/B, board run) writes the one progress record (step n of m, since when, the log) that dies with it; `wk status` shows every running one and a board run prints its iterations from it | 1 | `unit record.progress_shape[<cmd>]` |
| Two commands mutating one resource serialise or refuse naming the holder, on every target (two builds, two syncs, two guest starts, two image builds, a base refresh during a build) | 1 | `unit record.one_lock_per_resource[<cmd>]` |
| An unreadable or older-shape task record renders as unreadable and everything else still lists | 1 | `unit record.tolerates_corrupt_and_old` |
| Every target driver and every boot driver implements the whole interface (a guest driver starts a stopped guest, reports stopped as stopped and needs no image build) | 1, 5 | `unit machine.conformance[<kind>]` |
| A machine that cannot be probed is named unreachable with its timeout, never dropped or hung on | 1 | `unit machine.unreachable_is_named` |
| A probe or a boot driven over non-interactive ssh finds the same tools a login shell does | 1 | `unit machine.remote_path` |
| The podman machine is not started beside a running macOS guest on a host too small for both | 1 | `unit machine.podman_not_started_beside_guest` |
| Copying bytes out of a workspace, onto a board or onto a card is the one `Machine` copy | 1 | `unit machine.one_copy_path` |
| Each command runs where its declaration says, and a forwarded one forwards only the flags that apply there (`bench` on the Mac's volume runs on the Mac) | 1 | `unit dispatch.where[<cmd>]` |
| When the record and the machine disagree (a hand `podman rm` or `tart delete`, a deleted `ws/<n>`, an edited `~/.ssh/config.d/wk`, a fetch into a published base) the command reports it, believes the machine, refuses by name and touches only its own lines | 1 | `unit machine.machine_wins[<case>]` (tests/test_machine_wins.py) |
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
| A workspace on a peer is created there by hand (refused here, naming the command) and removed from here; `wk rm --all` asks once for the whole fleet and routes each removal | 3 | `live rm.peer[<machine>]` |
| `wk rm` leaves nothing on any target: no container, guest or checkout, no ws dir, no registry entry, no `Host wk-<name>` alias, no `.unfiltered`; the registry entry outlives the artifacts, never the reverse | 3 | `unit rm.final_state[<target>]` (tests/test_rm_final_state.py) |
| `wk build --babysit` is a task: one at a time by its record, ends stalled, gave-up or error by name, refuses where it cannot run, and a killed one reads died | 3 | `live build.babysit_e2e` |
| Every declared build config builds on its target (gtk, wpe, mac-debug, ios-sim, armhf on 2.48), a fresh clone off a warm base builds in under 45 min, and a mac build produces ImageDiff | 3 | `live build.config[<config>]` |
| `wk test <ws>` runs the JSC suite and `--layout` on every target, against a remote target's own build | 3 | `live test.suite[<target>]` |
| `wk run` finds its binary on every port (GTK, WPE, an Apple-port guest) with `LD_LIBRARY_PATH` prepended, and `--lldb` gets a pty on every target | 3 | `unit run.finds_binary[<port>]`, `live run.lldb_tty` |
| `wk enter <ws>` lands in a shell, `wk enter <ws> <cmd>` runs the command, `--zed` against a broken workspace refuses naming the repair | 3 | `unit enter.runs_command`, `live enter.shell` |
| `wk sync` bare inside a workspace syncs it, `--all` reaches every workspace on every target, `--tools` refreshes every machine's copy and publishes one snapshot, `WK_MIRROR_BRANCHES` carries the extra branches | 3 | `live sync.fleet` |
| The PR workflow runs as one flow: `wk push on\|off`, `wk sync --fix`, `wk pr`, the `container/bin` helpers, agents building while a person pushes, including from an armhf container | 3 | `live pr.workflow` |
| `wk ai claude` against a real container and guest runs the wall's checks, refuses a stopped proxy, and a tool inside wanting the network is refused and told so | 3 | `live ai.walled_session` |
| In an agent session `git commit` and `git push` name the rule after git's own error, `wk push on` on the host ends the session, and a terminal session turns push back on at exit | 3 | `live ai.commit_wall` |
| `wk ai claude` on a terminal, against a real container with the claude.ai login, starts a session Remote Control shows under the workspace's name; on a build box holding the inference token it starts without it and says so | 3 | `live ai.remote_control` |
| `wk zed` reaches a workspace through its `Host wk-<name>` ProxyCommand alias on every target, one hop for a peer's, and `wk new --zed` warns instead of failing when zed cannot launch | 3 | `unit zed.alias_is_proxycommand`, `live zed.peer` |
| `wk key setup` elects across workstations: the credential its issuer accepts wins from whichever machine runs it, a refused peer is re-logged in, a second run moves nothing, and one `claude login` seeds every workspace | 4 | `live key.election[<peer>]` |
| `wk key register` registers each fork's one shared deploy key once under the single title, `wk key check` reports per workstation, and a peer that did not answer reads differently from one holding no key | 4 | `unit key.register_per_machine` |
| A build box holds no deploy key at rest, an agent session is forwarded none, `wk push status --target <box>` says so, and a push from a remote workspace succeeds once the user chooses how it is authorised (Decisions for the user) | 4 | `unit push.remote_forwarding`, `live push.from_remote` |
| `wk key backup` then `./setup` round-trips with no spurious change; the junk filters strip what they claim; a write is whole or unchanged; one path with a per-platform adapter | 4 | `unit backup.filters`, `live backup.roundtrip` |
| Every skill is followable from inside a container and a guest | 4 | `live ai.skills_workspace_true` |
| `wk key sudo setup` installs its sudoers rule, validates with `visudo -c` before and after, proves `sudo -n true` fails, gets a terminal over ssh with `--target`, and no fleet machine holds a NOPASSWD grant wider than the three helpers | 4 | `live sudo.require[<machine>]` |
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
| The two-system lane runs against the fake board (arm, boot, probe, measure, disarm) | 5 | `unit boot.two_systems_on_fake` |
| A board's first boot is proven on hardware: self-disarm parks the medium, self-return reboots an unclaimed board within the watchdog, the rescue marker holds, `config.txt.append` lands for every builder, an absent boot device falls through to host mode, armed-not-rebooted reads ARMED exit 2, and the prompt shows `bench` | 5 | `live boot.firstboot[<board>]` |
| rpi5 boots a bench system from its stick, the second-system pair included, and hands itself back | 5 | `live boot[rpi5]` |
| rpi3 runs the shared-card two-system layout: the helper on the rescue, `@second` and `@third` written from it, armed by id, an `--ab-systems` run, and a panicking bench kernel reverts | 5 | `live boot[rpi3]` |
| rpi4's 2.52 system brings up KMS on every boot, or the run names why not | 5 | `live boot[rpi4].kms` |
| rpi4's 32-bit buildroot system reaches userspace, read at a serial console | 5 | `live boot[rpi4].armhf` |
| rpi5's tuning has two owners: stability in `./setup`, the overclock as an `oc` image profile, never the EEPROM; the 26.04 paths re-checked | 5 | `unit sysimage.oc_profile_in_image` |
| rpi5's workstation tuning is restorable: `wk key backup` holds what `rpi5-setup.sh` cannot re-make (the NUMA kernel build, the ssh key shipped beside the script); the 26.04 re-check covers `/boot/firmware/config.txt` and `cmdline.txt` under A/B boot, the root fstab label and its `discard`, the GNOME 50 indexer names and default swap | 5 | `unit key.backup_rpi5_tuning` |
| A board is re-flashed from nothing by the machine holding its card reader, with no other provisioned machine | 5 | `live sysimage.reflash[rpi5]` |
| `wk machine setup <board>` takes a board from a blank card to an answering tailnet name in `pi-hosts` and back, `wk machine probe` finds it when on, and a workspace reaches only that address, on port 22 | 5 | `live pi.setup[<board>]` |
| `wk machine setup <box>` leaves a build box in one shape (zsh, or a named warning), and a cleanup accepted at the prompt is removed | 5 | `live machine_cmd.setup[<box>]` |
| Two machines sharing one home each resolve their own target by hostname with no ssh, provisioning one never clobbers the other, and builds key dirs and locks per machine | 5 | `unit machine_cmd.shared_home` |
| A board is reached by tailnet name alone: no MAC, `.local`, address stanza, `HostKeyAlias` or ProxyJump remains once both boards join by image, and the bench install is reached at its own name | 5 | `lint.no_addresses` (tests/test_lint_no_addresses.py; the tree check is owed: the board and bridge confs' MACs, the bridges' `HostKeyAlias`) |
| The Librem 5 runs the pmOS bridge role in front of moose's BMC, with the BMC's own config in a conf file | 5 | `live bridge.setup[moose-bmc]` |
| A bridge's segment is proven: provision on the eMMC route asks which disk, a board on `lan0` gets its reserved address and a workspace reaches it, `wk bridge tailnet` approves the route and `autoApprovers` holds after a policy edit, the netwatch ladder stops at its budget, the dock holds 480 Mbit, the watchdog device exists, the camera streams, and a bridge whose segment is down reads differently from one that is off | 5 | `live bridge.segment[<bridge>]`, `unit bridge.segment_down_vs_off` |
| An image build runs from a Tart guest and the image reaches the host for writing | 5 | `live sysimage.build[vm]` |
| A task's results live in its workspace, `wk bench ls` names them wherever they are, the task restarts from where it stopped on any machine, and `wk doctor` names the results backed-up | 6 | `unit results.restart_anywhere` |
| A task's deliverables export as one archive | 6 | `unit results.export_archive` |
| A long effect (`wkdev-create`, `sdk-refresh`, `tart clone`) streams to the task log as it runs, so the silence watchdog and a followed log see it; `Machine.run` captures and prints only after it ends | 3 | `unit machine.streams_long_effects` |
| An effect run over ssh counts as an effect on the machine that drives it, so a kill point can land inside a remote flow (`Ssh.act_run` runs through `via.run` today) | 3 | `unit machine.ssh_effects_are_effects` |
| A hold names the pid that took it: the record carries the taker's pid before `holds` | 3 | `unit record.hold_names_its_taker` |
| `Target.state` reads the workspace directory through the machine, not `os.path`, so the real drivers run in a Fake world and `killpoints[new]` runs over them rather than a stand-in | 3 | `unit killpoints[new]` |
| The `--web` status page renders `armed_by`, `armed_at`, `armed_desync` and `disagree` as the text renderer does | 5 | `unit status.web_mirrors_text` |

### Decisions for the user

- `lib/wk/dispatch.py` starts the podman machine itself (it needs a terminal, so it is not under `act`): move it behind a command, or keep it.
- `lib/wk/dispatch.py` probes `tailscale status` directly rather than through `Machine`.
- `lib/wk/bench/board_driver.py` runs its own ssh outside `Machine`.
- `lib/wk/mac.py` writes WindowServer's plist directly rather than through `Machine`.


- On a macOS host a board A/B's run-benchmark and page server now run in the podman VM (the image workspace's machine, as its deploys and PGO collections already did); a live run must confirm the VM reaches the boards.
- README gets a section defining the `home`/`lab`/`wk`/`field`/`stock` layers, or the layering goes (5.39 deletes the `lint.layering` row; a later step can re-add it once README defines the layers).

- The word "target": a device configuration or the execution target; one workspace per (perf task, device) rather than per profile, deletable once its results are in.
- `git-sync-fork` against the fork's protected `main`: lift the protection, or the helper refuses by name.
- `wk key check` reports a Bugzilla key's authentication only; whether editbugs capability is probed another way.
- Whether `wk boot mbp --diag` and `--back` reach the bench install from host mode.
- The bench volume runs 26.6.1, the host 26.6.2: whether the two installs need to be comparable.
- Speedometer 3's `wakeLock` NotAllowedError under MiniBrowser: confirm Safari's path, or accept.
- rpi3: refuse a board with foreign processes, or clear them; Speedometer 2.1 as its plan, or swap/zram for 3.
- The 2.38 buildroot rpi4 image: pin a newer rpi-firmware so it boots a rev 1.4/1.5 board without tryboot, or keep tryboot.
- Delete the `pi-mbr` boot driver (`lib/wk/boot/pi.py`'s `PiMbr`; no machine declares it), or keep it for a firmware-bootable SSD.
- Tailnet ACL: boards reach a workstation's benchmark server directly, or keep the ssh tunnel; the `tag:wk` grant `wk pi setup` prints as a manual step.
- `wk pi helper` onto a workstation holding a card reader: a wk-driven path, or none.
- A rescue whose root reference is orphaned: a `wk` verb, or the helper's one-liner.
- LTO mode for the boards' perf build (the Mac lane is thin then full; the boards set none).
- Which pre-`wk` helper capabilities return: rr record, sysprof JIT dump, the wasm wrappers, SIMD and V8 comparisons, the weekly report, an option-toggle A/B; a baseline build reachable outside `wk bench`.
- Settings audit: which non-default host settings persist (config.dconf's `why: unknown` entries; tolken's `wk key backup --candidates`).
- A per-device connectivity and perf subcommand (wifi channels, autosleep, overclock) and its boundary with the settings audit.
- Host tools: git-lfs on every host or none; pmOS builder prerequisites only on the aarch64 build host; wk-tools work in a workspace so the host `claude` goes.
- Bridge phones: auto power-on after power loss; remote power for the boards, which nothing here can switch on.
- Write-ups and upstreaming: the egress-proxy design, SDK patches 3 and 11, the cross-compile commits, the `CONFIG_NUMA_EMU` Launchpad request, `gpr`/`wk pr` and the profiling wrappers.
- The sandbox escape audit, last and on both platforms: the incident list, the allowlist and CDNs, apt and ddebs, the broker, the agent-rw mount, the injector's standing tokens, remote targets, bridges, device paths, yocto egress, the git helpers.
- Reprovision tolken: host user `jmichaud`, Remote Login on both installs, keys per install; register each build box's deploy key on GitHub.
- What each part of `$WK_STORE` is called before a second project needs a name.
- How a push from a build box is authorised. A build box holds no deploy key and nothing forwards one to it. A forwarded agent socket is usable by root and by every same-uid process on the box (a `wk ai claude` session in another workspace there included, since a remote target has no container), and `wk enter` on a box with its own wk runs the far wk, so the workstation's ssh never opens that shell. The options: push only from the workstation (`wk pr` or `git push` run here against the box's checkout over ssh); a per-push forwarded agent (`ssh -A`) holding the key under `ssh-add -c`, so each signature is confirmed on the workstation; or a deploy key per box, held on the box.
- Where a build this workstation drives on a build box keeps its record: here, beside the driver (so another workstation, and the box's own `wk status`, do not see it, and records an earlier copy left on a box read `died` and nothing reclaims them), or on the box (the build hands itself to the box's wk once its tools sync lands, so the box's record is the one record).
- The mirror's second writer: `pr.mirror_fetch` fetches PR and branch heads into the mirror for `wk bench ab`. Move that fetch under `wk sync` (the mirror's one writer), or keep it.
- A macOS guest has no request broker, so `wk sync` inside one fetches only itself and warns; whether a guest gets the broker.
- Not prompted, judged recoverable or the receiving half of a prompted command: `wk key adopt` (stdin carries the key), `wk pr rebase` (reflog), the `reset --hard`/`clean` of a machine's tooling copy in `wk sync --tools`.
- `wk ai --dry-run`: its install, probe and session depend on each other (a dry install fails the next probe) and it throws the push switch; what its dry run prints.
- `wk selftest` has no dry run: exempt it in its declaration (the dispatcher then says why rather than "not yet"), or give it one that lists the tests it would run.
- A task left in `<store>/bench/` from before tasks lived in workspaces: `wk gc` names a `mv` into the workspace's `bench/`; on a Mac the store is the host's and the workspace is in the podman VM, so that move crosses machines and is not one command yet. Unmeasured.
- `wk bench run --task <task>` now accepts only a task in the leg's own workspace (every current caller passes that one).
- The injector answers `598` for its own TLS-verification or DNS failure, so the wall can tell it from an upstream's 500; a standard status with a marker header is the alternative.
- A running macOS guest keeps a stale view of the host's mirror after `wk sync --mirror` rewrites it: git replaces a ref by renaming a new file over it, and the guest's shared-folder mount keeps the old, unlinked inode (`refs/heads/main` showed link count 0 and the previous mtime; `git` called the directory "not a git repository"), so every fetch in the guest fails until the guest restarts, which cleared it. Measured 2026-10-02. The fix: the sync that writes the mirror restarts or remounts each running guest, or guests read the mirror another way.
