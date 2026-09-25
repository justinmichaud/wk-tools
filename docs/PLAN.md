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
2. **The seam and the drivers.** `Machine` and its fake are in. The
   registry and the read side of every driver are Python
   (`lib/wk/targets.py`): container, guest and workspace-local whole, and
   the remote driver's probe (one memoised ssh round trip), list, info,
   exec, `wk`, start and stop. The readers are Python as the drivers'
   smallest callers: `ls`, `version`, `logs`, `start`, `stop`, `disk`,
   `status` (the walk in `lib/wk/status.py`, the renderer in
   `lib/wk/statusview.py`) and `doctor` (`lib/wk/doctor.py`: rows of state,
   what and remedy, one renderer). What still bridges to bash through
   `lib/wk/shell.py`, each a named helper: every driver's tooling push
   (`t_sync_tools`); the guest's
   start and stop (`targets/vm.sh`); the boot drivers' probe and the reach
   probes; the credential verdicts (`wk_cred_check`,
   `peer_cred_verdict`), the privileged-helper table, `wk_remote_probe`,
   `wk_remote_findings`, `remote_provision_stale`, `vm_base_findings` and
   the store paths `lib/store.sh` names; in `lib/wk/status.py`,
   `current_base` and `unreferenced_bases` (this host's envelope is
   `Resources` now). Done when `lib/target.sh` and `targets/*.sh` are gone
   and no bash file parses JSON.
3. **Workspaces.** `new`, `rm`, `build`, `run`, `test`, `enter`, `scp`,
   `sync`, `pr`, `remotes`, `verify`, `ai`, `zed`, `gui`, `profile`. Done
   when `lib/store.sh`'s workspace half is gone and each has a kill-point
   test. **Where `new` and `rm` stand** (uncommitted on `python-core`; the
   full suite has not run over them yet -- run it first, in the background,
   with nothing editing):
   - *Landed.* `cmd/new` and `cmd/rm` are Python entry points over the flows
     in `lib/wk/workspace.py`, with the lock in `lib/wk/lock.py`, the alias in
     `lib/wk/sshalias.py`, each driver's write side and `mirror_dir` in
     `lib/wk/targets.py` and the bash they still reach in `lib/wk/shell.py`;
     `tests/test_wk_workspace.py` holds every refusal, `killpoints[new]`,
     `killpoints[rm]` and dry-run-equals-wet-run. Of the drivers' bash write
     side only `targets/vm.sh`'s `t_create`/`t_destroy` remain, for `wk vm`
     until step 5.
   - *Owed.* The live check, one pattern per run: `wk selftest --live
     crash_only`, `wk selftest --live container_workspace`, `wk selftest
     --live lifecycle`. `t_needs_base` and `t_created` stay in
     the bash drivers while `ws_state`, asked by the dispatcher's own
     `wait_ready` and by `cmd/gc`, asks them, and
     `tests/test_owed_new.py`'s and `tests/test_state.py`'s `ws_state` tests
     stay with them.
   - *`enter`/`scp`/`zed`.* All three are Python entry points over
     `lib/wk/targets.py`: `enter` runs a command through `Target.exec` or
     execs into `Target.enter_argv`'s shell; `scp` moves bytes through
     `pull`/`push`/`pull_dir`/`push_dir`/`path_kind` -- `podman cp` for a
     container, the one `Machine` copy (`copy_in`/`copy_out`/`copy_tree_in`/
     `copy_tree_out` on `Local`/`Ssh`/`Fake`) for a guest or a build machine;
     `zed` reaches a workspace through `ssh_host`/`ssh_prepare`/`ssh_user`/
     `ssh_proxy`, one hop further for a peer's own `--route`. The bash `t_pull`,
     `t_push`, `t_push_dir`, `t_path_kind`, `t_ssh_prepare`, `t_ssh_user` and
     `t_ssh_proxy` are gone with their last caller, and so is `t_pull_dir`;
     `targets/vm.sh`'s own `t_enter`/`t_ssh_host` for
     `cmd/vm` (every other driver's copy is gone; `cmd/profile` is Python
     now and calls `Target.pull_dir` directly).
     `tests/test_enter.py`, `tests/test_scp.py` and `tests/test_zed.py` hold
     the refusals; `tests/test_wk_targets.py` holds each driver's argv;
     `tests/test_wk_machine.py` holds the copy conformance. Owed: the live
     checks (`enter.shell`, `zed.peer`) against a real container, guest and
     peer.
   - *`pr`.* `cmd/pr` is a Python entry point: `rebase` and `open` run over
     `Target.exec`/`src`/`mirror_dir` (now on the base `Target` and on
     `LocalWorkspace` too, matching `targets/*.sh`), and `pr_open_target`/
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
     Owed: the live check (`sync.fleet`); the tooling push, the mirror
     refresh request, `newest_complete_base` and the wiring scripts stay
     bash behind `lib/wk/shell.py`; `status.py` calls `Target.workspaces`
     now, and the one remaining inline copy of its union is `cmd/ls`.
   - *`build`.* `cmd/build` is a Python entry point over `lib/wk/build.py`
     (front, driver, `--detach`, `--kill`, the babysitter -- `build/babysit.sh`
     is gone), `lib/wk/job.py` (the watched run, the announced
     pid, the one job stop) and `lib/wk/buildconf.py` (the configs as data;
     `build/configs.sh` is its shim for the bash callers, plus the cross
     configs `image/` reads); each driver's `ccache_dir`, `build_argv`,
     `build_size` and `task_put` are in `lib/wk/targets.py`, the budget in
     `lib/wk/resources.py`'s `Budget`. `tests/test_wk_build.py` holds every
     refusal, `killpoints[build]`, `progress_shape[build]` and
     dry-run-equals-wet-run; `tests/test_buildconf.py` the configs and the
     shim. Owed: the live checks (`build.config[<config>]`,
     `build.babysit_e2e`); `Budget` repeats `lib/resources.sh`'s
     `build_jobs`/`build_admit`/`builds_running`, `build.busy_reason` repeats
     `lib/target.sh`'s `ws_busy_reason`, and `job.py` repeats
     `lib/watchdog.sh`'s `run_watched`/`job_stop` and its `TOOLS`/
     `build_processes` repeat `lib/detach.sh`'s `_build_ps`, which
     `lib/watchdog.sh`'s `_stall_report` still uses, until `sysimage`
     and `image/` call the Python ones (`test` now does). `configs.sh` keeps
     only the four accessors `cmd/bench` and `bench/mac-ab.sh` still call
     (`config_build_dir`, `config_jsc_path`, `config_run_var`,
     `config_run_dir`); `run` and `gui` are ported, so the accessors that
     read the browser/process/test-runner fields (`config_browser_path`,
     `config_browser_url_flag`, `config_jsc_only`,
     `config_web/network/gpu_process_name`, `config_test_runner_name`,
     `config_web_process_pause_env`, `config_browser_env`) are gone (`test`
     and `profile` resolve a config through `buildconf.resolve` directly).
   - *`run`/`gui`.* Both are Python entry points that resolve a config
     straight through `lib/wk/buildconf.py`'s `Config` (no `config_load`
     call at all) and exec into `Target.exec_argv`'s result with
     `os.execvp`, the same replace-this-process pattern as `enter`/`zed` --
     every branch of both is a tail call into the target, so neither reads
     a `Result` or an exit status. `Registry.default_config` (from the
     workspace's own `build` task record, `lib/wk/record.py`) replaces
     `lib/target.sh`'s `default_config` for both; `Target.lldb_opts`
     (Container's two `-O` flags) and `shell.py`'s `lldb_prelude`/
     `lldb_pin_opts`/`session_mode`/`bmc_drm_device` bridge what stays
     bash. `wk gui` refuses a `kind == "remote"` target (`unit
     gui.refuses_remote`).
     `tests/test_run_until_crash.py` passes unmodified against the port;
     `tests/test_wk_run.py` holds `run.finds_binary[<port>]` and `--lldb`'s
     tty request on every target, `tests/test_wk_gui.py` holds
     `gui.refuses_remote`, the jsc-only/no-browser/macOS-container
     refusals and the fullscreen-flag table. Owed: the live checks
     (`run.lldb_tty`, `session.modes[moose]`); `Registry.default_config`
     repeats `lib/target.sh`'s `default_config` until `bench` calls the
     Python one (`test` and `profile` now do).
   - *`test`/`profile`.* Both are Python entry points. `cmd/test` runs the
     JSC and layout suites over `lib/wk/job.py` (`watch`, `PidWatch`,
     `kill`, `stop`) and `lib/wk/resources.py`'s `Budget`, its record
     wrapped through `build.records_of` for the same `WK_ABORT_SECONDS`
     default a build's carries; `cmd/profile` is argv/refusal construction
     and direct `Target.exec`/`exec_tty` calls with no task record at all.
     Both resolve a config straight
     through `lib/wk/buildconf.py`, no `build/configs.sh`. The one new
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
     directly. `lib/arch.sh`'s `arch_wrapper`/`arch_label` are gone
     (only `cmd/test` called them; `buildconf.ARCH`/`arch_label` replace
     both). Owed: the live checks (`test.suite[<target>]`,
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
     `cmd/zed --tools` resolves its target once. Still open:
     `lib/wk/sshalias.py`'s `alias_set`/`alias_remove` and
     `lib/wk/store.py`'s `artifact_dir` copy `lib/target.sh`'s
     `ssh_alias_set`/`ssh_alias_remove` and `lib/store.sh`'s
     `wk_artifact_dir` verbatim; the bash pair stays for `cmd/vm` until step
     5, `wk_artifact_dir` for `lib/bench.sh`/`lib/profiler.sh`.
     Fixed: the three "is this pid alive in the target" answers
     (`shell.target_pid_alive`'s bash `t_exec kill -0` round trip,
     `record.of_target`'s own closure, `cmd/stop`'s and `cmd/status`'s bash
     ask) are one now, `Target.pid_alive` (`lib/wk/targets.py`), which
     `record.of_target` and `workspace.py` (through it) and `cmd/stop`/
     `cmd/status` all ask; `shell.target_pid_alive` is gone.
4. **Credentials.** `key`, `push`, `sudo`, `backup`, `skills`. Done when
   `lib/store.sh`'s credential half is gone; its mirror, base, wiring and PR
   half goes in step 5.4, and the guest's push-agent half with
   `targets/vm.sh` in step 5.33.
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
     `secrets.AGENT_SECRETS`, the one copy; `wk_push_forks`,
     `wk_agent_secrets` and `wk_cred_read` are one-line shims over
     `python3 -m wk.secrets`, and `container/firstrun.sh` asks it directly.
     `wk doctor`'s credential rows and the peers' login rows are `Key`'s own
     (`stored_verdict`, `cred_verdict_of`); `peer_cred_verdict`,
     `wk_cred_store`/`wk_cred_clear`, `wk_cred_settable` and
     `wk_hook_levels`/`wk_wiring_check_script` are gone. `-h` prints each
     subverb's destructive override under the command's line.
   - *Owed.* The live checks (`key.election[<peer>]`, `push.from_remote`,
     `backup.roundtrip`, `sudo.require[<machine>]`). `lib/store.sh`'s
     credential half is still bash for its bash callers: `store_init`'s
     publish (`host/linux/machine.sh`), `push_agent_cred_sync`/`push_agent_exec`
     (`host/linux/sdk.sh`, `host/macos/vmtools.sh`),
     `push_agent_pat_converge_machine` (`targets/container.sh`), `wk_notify`
     (`bench/mac-ab.sh`), and `wk_cred_present` with the `wk_agent_secret*`
     family (`lib/wk/guest.py`'s `write_agent_secrets`, `wk_notify` itself);
     each twins a `Secrets`/`Key` method and goes with its caller.
     `agent_secret_store_remedy` (`lib/target.sh`) is gone: `Secrets.agent_secret_remedy`
     (`lib/wk/secrets.py`) is the one implementation, over `cred_stored`/`cred_verdict`
     and the already-Python `lib/credcheck.py`, and `Target.agent_secret_remedy`
     (`lib/wk/targets.py`) its only caller. `lib/wk/targets.py`'s `_agent_secret`/`_forks`
     and `lib/wk/status.py`'s push-record read now ask `wk.secrets` directly;
     `shell.agent_secrets`/`shell.push_forks`/`shell.agent_secret_store_remedy` are gone.
     `wk_push_forks`/`wk_agent_secrets` (`lib/store.sh`) stay: `host/linux/machine.sh`
     and `lib/store.sh`'s own `wk_github_user` still call them, so the
     shim goes with 5.4/5.33's callers, not before. `wk_cred_check`/`wk_cred_verdict`/
     `wk_cred_detail` (`lib/store.sh`) are gone: `agent_secret_store_remedy` was their
     last caller, and `test_credcheck.py`'s direct bash exercise of them is
     `tests/test_wk_secrets.py`'s `TestAStoredCredentialIsReadTheOneWay`, over
     `Secrets.cred_verdict`/`cred_stored` and a `Fake` reacting as `lib/secretfile.py`
     would to a refused read.
     `lib/wk/key/creds.py` is 198 lines: `cred_print` moved to `wk.key.common` as a
     plain function over `verdict`/`detail`, the only call sites it had.
5. **Fleet and bench.** `sysimage`, `boot`, `pi`, `bench`, `ab`, `quiesce`,
   `session`, `bridge`, `vm`, `find`, `remote`, `gc`, `completion`, as the one
   pipeline over one `machines/` directory. Done when `lib/target.sh`,
   `targets/*.sh` and every `lib/*.sh` but `common.sh` are gone, no bash file
   in `cmd/` remains but `selftest`, and `bench/`, `boot/`, `image/`,
   `bridge/`, `vm/` and `remote/` hold only what runs on a board, a phone, a
   bench install or in a target.

   **Sizes today** (lines of bash, 2026-09-23):

   | command | lines | | library / tree | lines |
   | --- | --- | --- | --- | --- |
   | `cmd/pi` | 1,550 | | `targets/vm.sh` | 1,578 |
   | `cmd/bench` | 1,496 | | `lib/store.sh` (non-credential half) | ~850 |
   | `cmd/bridge` | 1,234 | | `lib/target.sh` | 670 |
   | `cmd/sysimage` | 1,107 | | `targets/remote.sh`, `container.sh`, `local.sh` | 691 |
   | `cmd/ab` | 600 | | `lib/{task,resources,watchdog,image,bench,reach,quiet,detach,profiler,par,sched}.sh` | 1,867 |
   | `cmd/boot` | 408 | | `boot/` (disk 735, machines 487, mac-volume 338, pi-tryboot 156, pi-mbr 154, mac-guest 140, rpi-eeprom 134, pi-sd 99, rpi5-usb 92) | 2,335 |
   | `cmd/session` | 365 | | `image/` host halves (yocto 573, pmos 395, buildroot 275, pgo 237, profiles 189, fetch 30) | 1,699 |
   | `cmd/find` | 364 | | `image/` in-target halves (yocto-build 573, pmos-build 358, buildroot-build 240, buildroot-webkit 115) + overlays | ~1,470 |
   | `cmd/gc` | 310 | | `bench/` (mac-ab 1,238, autorun 707, mac-lane 646, mac-bench-volume 604, quiet-desktop 485, mac-tailnet 389, the rest ~500) | ~4,800 |
   | `cmd/remote` | 268 | | `bridge/` (provision.sh 664, bin 656, init.d 109, devices 24) | 1,453 |
   | `cmd/vm` | 186 | | `vm/` 244, `remote/` 312, `build/mac-pgo.sh` 176 | 732 |
   | `cmd/quiesce` | 132 | | | |
   | `cmd/completion` | 127 | | | |
   | **commands** | **8,147** | | **libraries and trees** | **~17,900** |

   Python already in this scope: `lib/wkdata.py` 1,554, `lib/wkmac.py` 395,
   `lib/sched.py` 359, `lib/wkpgo.py` 266, `lib/wknotify.py` 174, `lib/wkslot.py`
   168, `lib/tailnet.py` 125, `bench/wk_board_driver.py` 418,
   `bench/mac-browser-check.py` 372. Each moves under `lib/wk/` with the
   sub-step that owns its caller. Its CLI goes when the last bash caller goes.

   **Target shape.**

   | command | subverbs | replaces |
   | --- | --- | --- |
   | `machine` | `setup`, `rm`, `ls`, `probe` | `remote`, `find` (`probe` with no name sweeps), `pi setup`, `bridge` (every verb), `vm` (the lifecycle verbs become `new`/`start`/`stop`/`enter`/`sync`/`rm --target vm`, `check` becomes `doctor <ws>`) |
   | `bench` | `deploy`, `run`, `ab`, `ls`, `report` (`compare` and `precision` are `report` over two runs) | `pi deploy`, `pi bench`, `bench <ws> <plan>`, `bench stage`/`staged`/`mac`/`mac-ab`/`ab-summary`/`seed`, `ab` |
   | `boot` | `<machine>` with `--status`, `--keep`, `--back`, `--disarm`, `--diag`, `--system`, `--boot-order` | `boot`, `pi boot-order`, `pi helper` (moves to `machine setup <board>`), `boot --prepare` (moves to `machine setup mbp`) |
   | `sysimage` | `build`, `write`, `ls`, `disks`, `rm` | `sysimage`, `image/*`, `vm base` (the guest base is a builder), `bench mac-volume` (the bench volume is a builder) |
   | `quiesce` | `on [--seat=kiosk\|desktop] [--bmc]`, `off`, `status` | `quiesce`, `session` (see 5.9) |
   | `gc` | kept, in Python | every kind of rubble is named by the module that makes it |
   | `completion` | generated by the dispatcher from the declarations | `cmd/completion` |
   | `notify` | already a tombstone; `lib/wk/notify.py` is the library call | `wk_notify` (lib/store.sh), `lib/wknotify.py` |

   Commands after step 5: `new rm build run test enter scp sync pr ai zed gui
   profile status ls logs stop start doctor key push machine bench boot
   sysimage quiesce gc selftest`. That is 28 against the budget of 20.
   `logs`/`status` and `start`/`stop` are the obvious next merges, and they
   are not in this step.

   **One `machines/` directory.** `machines/<name>.conf` holds one machine,
   named by the name the CLI takes. `KIND` is one of `build`, `peer`,
   `board`, `mac`, `guest` or `bridge`. The other keys are today's, unchanged
   (`NODE_*`, `WK_REMOTE_*`, `WK_TARGET_*`, `BR_*`), so the phone-side
   `bridge/provision.sh` and the bash readers still left read the same names.
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
   line per function calling the Python (the `build/configs.sh` precedent). It
   is never a second implementation, and 5.39 deletes every shim.
   `lib/wk/shell.py` and `tests/support.py` are shared: a sub-step edits
   only the functions it names, by exact match. Two sub-steps that share a
   bash command file never run at once.

   **The sequence.** "Owns" lists files disjoint from every sub-step that can
   run beside this one. ∥ marks what can run concurrently.

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
   - Owns: `machines/`, `lib/wk/fleet.py`, `lib/wk/targets.py`'s
     `Registry.registry_dir`/`known`/`kind`, `boot/machines.sh`'s
     `machines_dir`/`machine_list`/`machine_load`, `lib/target.sh`'s
     `target_registry_*`, `cmd/bridge`'s `bridge_conf`/`bridge_names`,
     `tests/test_fleet_walk.py`, `tests/test_tailnet_name.py`.
   - Moves the 11 confs out of `boot/machines`, `targets/hosts` and
     `bridge/hosts`; their bash readers read `machines/` through `fleet.py`
     (`python3 -m wk.fleet load <name>` prints the assignments).
   - Adds `lint.one_machine_dir`: no `case` names a machine, and no path
     names the three old directories.
   - Closes: `unit machine_cmd.shared_home`, meaning each machine resolves
     its own target by hostname with no ssh.
   - Decision: `KIND` plus the old keys verbatim, one file per CLI name.
     Renaming the keys waits for 5.39, once no bash reads them.

   *Group 1, all ∥ after 5.1*

   5.2 **The bash libraries become shims.** *Landed* (`record.py`,
     `job.py`, `resources.py` and `lock.py` hold the one copy; `lib/par.sh`
     stays until its callers are ported; `shell.CallerShell` re-sources a
     bash caller's dumped shell state, `record.py` and `job.py` calling it
     there now; `machine.far_side_start` is the one far-side start line,
     `Ssh.spawn` and `job.remote_line` both calling it).
   - Owns: `lib/task.sh`, `lib/watchdog.sh`, `lib/detach.sh`,
     `lib/resources.sh`, `lib/par.sh`, `lib/lockrun.sh`, `lib/wk/record.py`,
     `lib/wk/job.py`, `lib/wk/resources.py`, `tests/test_stall_report.py`,
     `tests/test_task*.py`.
   - Ports `task_begin`…`device_hold` into `record.Task` holds;
     `run_watched`, `job_*` and `_build_ps` into `job.py`;
     `build_admit`/`build_jobs` into `Budget`; `detach_run`/`detach_remote`
     into one `job.detach`; `par_*` into `concurrent.futures` in the callers'
     ports. About 870 lines become shims.
   - This clears PLAN's standing duplicates (`Budget`, `job.py` versus
     `lib/watchdog.sh`).
   - Closes: `unit record.hold_names_its_taker`, a hold naming its taker on
     one path.
   - Decision: `device_hold` becomes a hold named `device:<machine>` on the
     holder's own record, not a separate lock file.

   5.3 **The dispatcher's last bash asks, and the tooling push.** *Landed*
     (`Target.state`/`wait_ready` through the machine, `lib/wk/tools.py` the one
     push, `Registry.local_workspaces()`; `shell.gh_authenticated` asks `gh`
     directly and the dispatcher's `needs=tart` branch went with `cmd/vm`;
     `Target.wk_cmd` builds every far wk's command line, the podman-machine
     hop's and a machine's alike, and `lib/target.sh`'s `vm_wk_cmd` is gone;
     `delegate_run` execs `ssh` over the `Remote`'s own machine, so the
     dispatcher's own `t_wk`/`t_wk_tty` ask is gone (`targets/remote.sh`'s stay, for `_peer_fetch`/`_peer_route`,
     whose `_remote_wk_cmd` is a one-line shim onto `python3 -m wk.targets wk-cmd`, the new verb calling
     `Target.wk_cmd` directly);
     `dispatch.json_merge_list` is plain `json`; `lib/common.sh`'s
     `gh_authenticated` and `json_merge_list` are gone, no caller left.
     5.13 landed and deleted `boot/machines.sh`'s `machine_prepare`, so
     `lib/tools.sh`'s bash `tools_push` shim had no caller left either --
     gone, with the dead source-guard line in `boot/machines.sh`; there is no
     bash `ws_busy_reason`: `image/buildroot.sh` does not exist (another
     sub-step ports `image/buildroot-build.sh` into `lib/wk/sysimage/`
     directly). Owed: none.
   - Owns: `lib/wk/dispatch.py`, `lib/target.sh`'s `ws_*`/`wait_ready`/
     `ws_exists_on`/`t_sync_tools`, `lib/tools.sh`, `lib/wk/targets.py`'s
     `sync_tools`, `tests/test_owed_new.py`, `tests/test_state.py`.
   - `ws_state`/`wait_ready` become `Target.state` plus one
     `clock.wait_until`. The tooling push (a git bundle over the one
     `Machine` copy) replaces `t_sync_tools` in remote, container and vm and
     `shell.sync_tools`. About 450 lines.
   - Closes: `unit killpoints[new]` over the real drivers,
     `unit machine.dead_creation_refused_at_once`.
   - Decision: one push for every kind. The guest gets the same bundle; its
     `_push_tools` goes.

   5.4 **The rest of `lib/store.sh`.** *Landed* (`lib/wk/git.py`, `lib/wk/pr.py`,
     `store.Bases`; `lib/store.sh` is shims plus the credential functions).
     `targets.py` (outside `Vm`), `build.py` and `doctor.py` call `wk.git`
     directly, `shell.py`'s four forwards are gone, `cmd/ls` uses
     `store.Bases`, and `test_new_fetch.py`/`test_hook_levels.py` import the
     Python instead of shelling to the bash shims. `tests/test_pr_workflow.py`
     holds `killpoints[pr]` (checkout onto a fork's branch, `pr_rebase`'s
     fetch and rebase, and `pr_open`'s push and `gh pr create`, each killed
     after any effect and rerun converging) and dry-run-equals-wet-run for
     the plain checkout; `cmd/pr` declares `dryrun` for that form only --
     `rebase` and `open` mutate through plain `Target.exec`, not `act_exec`,
     so their own sub declarations turn it back off. Owed: the store path
     helpers (`wk_mirror`, `wk_ws_dir`, `mirror_init`) are still bash;
     `lib/store.sh`'s `wk_hook_levels` and `wk_wiring_check_script` lost
     their last caller when the tests moved to the Python and are dead, but
     `lib/store.sh` is outside this pass.
   - Starts after step 4 lands.
   - Owns: `lib/store.sh`, `lib/wk/store.py`, `lib/wk/sync.py`, `cmd/pr`,
     `tests/test_sync.py`, `tests/test_pr_*.py`, `tests/test_owed_gc.py`
     (base selection).
   - Ports mirror init/refresh, the `base_*` family, `current_base`,
     `newest_complete_base`, `unreferenced_bases`, the wiring and
     check scripts, and the PR fetch (`pr_parse_spec`, `wk_pr_checkout`).
     About 850 lines; split at the mirror/PR seam if it runs over.
   - Closes: `unit new.refuses_without_mirror`, `unit
     cli.refspecs` (the owed module).
   - Decision: the wiring runs as git argv lists through `Target.exec`, not
     as script text sent into the target.

   5.5 **Image profiles as data, and each image's workspace.** *Landed*
     (`lib/wk/images.py`). `image/pgo.sh`'s `image_pgo_machine` now reads a
     profile's own `IMG_MACHINE` (two arms of one board, or the `-oc` profile,
     share the fleet machine their stock profile names), and `rpi5-setup.sh`
     no longer writes `arm_freq`/`v3d_freq`/`over_voltage_delta` -- the `-oc`
     profile's `config.txt.append` is the one place that sets them. Owed:
     none.
   - Owns: `lib/image.sh`, `image/profiles.sh`, `image/configs/`,
     `lib/wk/images.py`, `tests/test_lane_routing.py`,
     `tests/test_multilib_image.py`.
   - Ports the profile loader and derivations, `image_spec_*`, `image_lane_*`
     and slot paths. About 470 lines; the bash becomes a shim for
     `cmd/sysimage`, `cmd/pi` and `image/*.sh`.
   - Closes: `lint.profiles_are_data`, `unit sysimage.oc_profile_in_image`,
     and the "lane" half of `lint.vocabulary`.
   - Decision: "lane" is retired. It is the image's workspace
     (`<builder>-<profile>[-<arm>]`), named in code `image_ws`.

   5.6 **`wk machine` for build and peer machines; reach.** *Landed*
     (`cmd/machine`, `lib/wk/machine_cmd/`, `lib/wk/reach.py`; `wk remote` and
     `wk find` are tombstones). Owed: none -- `boot/machines.sh`'s
     `fleet_tailnet` is gone (5.11 deleted it) and `shell.py` carries no
     `remote_*`/reach forward any more (`git grep` finds none): every caller
     already imports `wk.reach`/`wk.machine_cmd` directly.
   - Owns: `cmd/machine` (new), `cmd/remote`, `cmd/find` (both become
     tombstones), `lib/reach.sh`, `lib/wk/reach.py`, `remote/deps.sh`
     (becomes a data table), `targets/remote.sh`'s `remote_provision_*`,
     the reach calls in `lib/wk/status.py`, `tests/test_remote*.py` (after
     step 4 releases `test_remote.py`), `tests/test_rm_remote.py`.
   - `setup`/`rm` build and peer machines; `ls`; `probe <name>`, and `probe`
     with no name sweeps (nmap plus the neighbour table, from each vantage).
     About 800 lines.
   - Bridge: `remote/probe.sh` and `remote/provision.sh` still run on the
     box.
   - Closes: `unit killpoints[machine setup]`, `killpoints[machine rm]`,
     `unit machine.probed_once_per_invocation`,
     `unit machine.unreachable_is_named`, `live machine_cmd.setup[<box>]`.
   - Decision: a first `machine setup <name>` with no conf requires
     `--kind`; after that the conf's `KIND` is the answer. A probe never
     guesses a kind.

   5.7 **The boot-driver core.** *Landed* (`lib/wk/boot/`, the on-board
     shell in `boot/onboard/`, `boot/pi-mbr.sh` kept as a shim pending the
     user). The transport is `driver.Channel` over `Machine`s (`Ssh` to NODE_SSH
     or the bench system's found address, the driving machine itself when
     standing on it), the card and boot helpers through the same machine under
     `sudo -n` only where the answering system is not root; `wk.boot.open_driver`
     gives each driver its own (the Macs' `mac.Channel`, the guest's over the vm
     target), and `FakeBoard` is that Channel over two fake `Machine`s. Owed:
     `boot/machines.sh`'s `m_ssh`/`i_ssh`/`r_ssh`/`image_addr` and their opts
     still twin `Channel` for `cmd/pi`, `cmd/ab`, `bench/mac-ab.sh` and
     `lib/sysimage-arms.sh` until those are ported (5.22, 5.29); `PiMbr.dev`
     imports `wk.sysimage.disk` (boot importing sysimage) until 5.39 decides
     pi-mbr. `part`/`disk_of`/`partno` have one implementation, in `driver.py`,
     which `wk.sysimage.disk` imports. The `b_*` shims nothing sources any more,
     and their `python3 -m wk.boot` verbs, are gone.
   - Owns: `boot/machines.sh`, `boot/pi-sd.sh`, `boot/pi-tryboot.sh`,
     `boot/rpi5-usb.sh`, `boot/pi-mbr.sh` (deleted), `lib/wk/boot/`
     (`driver.py`, `pi.py`, `fake.py`), `boot/onboard/` (new),
     `tests/test_pi_tryboot.py`, `tests/test_pi_mbr.py`,
     `tests/test_boot_armed.py`, `tests/test_boot_select.py`.
   - The `b_*` interface becomes a class: probe, boot_id, arm, disarm,
     evidence, media, reprovision, diag. `FakeBoard` holds media, a firmware
     one-shot and a clock. About 830 lines.
   - Bridge: `boot/machines.sh` becomes a shim for `cmd/sysimage`, `cmd/pi`
     and `bench/mac-*`.
   - Closes: `unit machine.conformance[pi-sd|pi-tryboot|rpi5-usb]`,
     `unit boot.arming_exact`.
   - Decision: every driver's on-board shell (`b_self_disarm_sh`, tryboot
     staging) becomes a file under `boot/onboard/`, read verbatim. That is
     where lint counts the on-board budget, and no driver builds shell by
     string. Delete `pi-mbr` (no machine declares it; listed under
     decisions).

   5.8 **Completion in the dispatcher.** *Landed* (`lib/wk/completion.py` generates
     the script from the declarations; `cmd/completion` is gone; the dispatcher's
     hidden `--list-workspaces` callback is gone too, the generated script's
     workspace slot instead calling `python3 -m wk.completion --list-workspaces`
     directly, the checkout root baked in at generation time).
   - Owns: `cmd/completion` (deleted), `lib/wk/dispatch.py`'s `completion`
     builtin, `shell/bashrc`, `tests/test_completion.py`.
   - Generates the script from `decl.all_commands`: the flags come from the
     declarations, not from `-h` text. About 130 lines.
   - Closes: `unit dispatch.help_previews_and_lists_values` for the flag
     lists.
   - Can run alongside 5.3 only if 5.3's edits to `dispatch.py` are in, so
     start it after 5.3 or give 5.3 the whole file.
   - Decision: workspace names come from `Registry.local_workspaces()`, which
     never reaches a machine. The hidden `--list-workspaces`/`--flags` verbs
     are gone.

   5.9 **`quiesce` and `session`.** *Landed as two Python commands*
     (`lib/wk/quiet.py`, `lib/wk/session.py`, the tables in `bench/quiet/`),
     `killpoints[quiesce]`, `killpoints[session]` and dry-run-equals-wet-run
     in `tests/test_quiesce.py` and `tests/test_session.py`; the live bodies
     read only, their mutating half owed. The merge waits for the user.
   - Owns: `cmd/quiesce`, `cmd/session` (becomes a tombstone),
     `lib/quiet.sh`, `bench/mac-quiet-desktop.sh` (becomes
     `bench/quiet/macos.tsv`), `bench/mac-quiet-hosts.sh` (becomes a table),
     `lib/wk/quiet.py`, `tests/test_quiesce.py`, `tests/test_mac_quiet*.py`,
     `tests/test_quiet_siblings.py`.
   - `on`/`off`/`status` go through `admin/wk-quiesce-priv`, which is
     unchanged; the seat (kiosk cage, gdm, off, `--bmc`) comes from
     `machines/<name>.conf` `SEAT_*` keys, never from a machine name. About
     620 lines plus the table.
   - Closes: `unit killpoints[quiesce]`, `live quiesce.readback[<m>]`,
     `live quiesce.classified[<m>]`, `live session.modes[moose]`,
     `live doctor.bench_readiness[mbp]` (doctor reads `quiet.status`).
   - Decision (yours): merge into one command, as recommended, or keep two.
     The plan's merge table does not list them.

   5.10 **The bench record and report; `cmd/bench` goes Python.** *Landed*
     (`lib/wk/bench/`, the unported arms verbatim in `lib/bench-arms.sh`).
     `lib/wk/status.py` imports `wk.bench.record` directly and `wkdata.py`'s
     `task_state`/`_subject_line` shims are gone (`git grep` finds no other
     caller). Owed: the bash `plan_json` goes with 5.21/5.22; the report does
     not yet label an instrumented leg's time (env.json's `profile` field
     means two things).
   - Owns: `cmd/bench` (Python entry point), `lib/bench.sh`,
     `lib/wk/bench/record.py`, `lib/wk/bench/report.py` (from `wkdata.py`'s
     report, precision and warmup code), `lib/bench-arms.sh` (the unported
     arms, verbatim, reached through `shell.py`),
     `tests/test_bench_report.py`, `tests/test_bench_task.py`,
     `tests/test_ab_precision.py`, `tests/test_reporting_words.py`.
   - Ports `ls` (fleet, delegated like status), `report` (text, html,
     compare, precision) and `seed`. About 450 lines.
   - Closes: `unit bench.one_record[container]`, `unit
     bench.report_and_cost` (the report half), `unit bench.seed_from_mirror`,
     `live bench.compare`.
   - Decision: the task directory stays where it is (`task.json`,
     `runs/<run>/`). `record.py` takes the directory as a parameter, so step
     6's move into the workspace is one line.

   *Group 2*

   5.11 **`wk boot`.** *Landed* (`cmd/boot` over `lib/wk/boot/cli.py`; `--boot-order` and the pinned
     recovery.bin path in `lib/wk/boot/eeprom.py`; `wk status`'s fleet probe is `python3 -m wk.boot.cli
     fleet-probe`; `wk pi boot-order`/`helper` and `wk boot --prepare` are tombstones; `boot/rpi-eeprom.sh`,
     `fleet_tailnet` and `machine_quiet_siblings` are gone). `--boot-order` takes the order by name, and
     `--revert` is `local`. The recovery.bin staging goes through the driver Channel's `Machine`, and an arming
     record with no boot id reads as unknown, never by clocks. Owed: the live bodies of `boot[rpi3|rpi4|rpi5]` read
     `--status` only, since the live tier reboots nothing, so the arming halves of those rows, `boot[rpi4].kms`,
     `boot[rpi4].armhf` and `boot.firstboot[<b>]` need a person at the boards; `cmd/boot` still asks
     `cli.in_workspace` rather than `Registry.in_workspace`.
   - Needs 5.7 and 5.6 (`status.py`). ∥ 5.12. Serial on `cmd/pi` with 5.13.
   - Owns: `cmd/boot` (Python), `boot/rpi-eeprom.sh`, `cmd/pi`'s
     `boot-order`/`helper` arms, `lib/wk/status.py`'s `FLEET_PROBE`,
     `lib/wk/statusview.py`'s armed fields, `tests/test_boot_priv.py`,
     `tests/test_pi.py` (the boot-order and helper classes).
   - arm, `--status`, `--keep`, `--back`, `--disarm`, `--diag`,
     `--boot-order`, through `admin/wk-boot-priv` (unchanged). About 750
     lines.
   - Closes: `unit killpoints[boot]`, `unit status.armed_transition`,
     `unit status.web_mirrors_text`; marks live `boot[rpi3|rpi4|rpi5]`,
     `boot[rpi4].kms`, `boot[rpi4].armhf`, `boot.firstboot[<b>]`.
   - Decision: `--prepare` leaves `boot`. Putting the tree and helpers on a
     machine is `machine setup mbp`.

   5.12 **Mac boot drivers.** *Landed* (`lib/wk/boot/mac.py`; `lib/wkmac.py` is a symlink to
     `lib/wk/mac.py`, which `bench/mac-ab.sh` pipes to the Mac's `python3 -`).
     A failed `--disarm` now refuses instead of clearing the record. The bench
     record no longer reads `B_MEASURES`/`check_measurement` from bash: no
     bash ever did (`git grep B_MEASURES` finds only the driver's `facts()`
     and its tests); `lib/wk/bench/mac.py`'s `MacVolumeSystem` already asks
     `driver_class(...).measures` directly, in Python, and writes it into
     `env.json` (`bool_facts()`'s `measures=`). `targets/vm.sh` is a 65-line
     read contract with no `1280x800` of its own any more; the duplicate was
     `lib/wk/guest.py`'s `DISPLAY` against a `"1280x800"` literal in
     `lib/wk/targets.py`'s `Vm.create()`; `Vm` and `lib/wk/boot/mac.py` both
     take `guest.DISPLAY` now. Owed: the bash transport overrides go with 5.29.
   - Needs 5.7. ∥ 5.11.
   - Owns: `boot/mac-volume.sh`, `boot/mac-guest.sh`,
     `lib/wk/boot/mac.py`, `lib/wkmac.py` (moves to `lib/wk/mac.py`),
     `tests/test_mac_display.py`, `tests/test_macos_machine_*.py`.
   - About 480 lines. `b_bench_put`/`b_manage_*` become the driver's copy
     and exec through `Ssh`.
   - Closes: `unit machine.conformance[mac-volume|mac-guest]`; marks live
     `boot.arm[mbp]`.
   - Decision: keep `mac-guest`, since the rehearsal row needs it. It
     conforms like the real driver, and its readings are refused as
     measurements.

   5.13 **`machine setup` and `rm` for a board; `machine setup mbp`.** *Landed*
     (`lib/wk/machine_cmd/`'s `setup_board`/`rm_board`/`setup_mac`, over the
     generic `Machines.answers()`/`board_machine()`; `cmd/pi`'s `setup` arm and
     `boot/machines.sh`'s `machine_prepare` are gone; `wk boot <mac> --prepare`
     is a tombstone naming `wk machine setup <mac>`, and `boot/mac-volume.sh`'s
     `b_manage_prepare` calls it). `setup <board>` checks the tailnet name
     answers and installs `admin/wk-card-priv`/`boot/check-boot-files.py` at
     `/usr/local/libexec/`; `rm <board>` removes them and the conf, asking once
     each. `setup <mac>` ports `machine_prepare` verbatim: pushes this tree
     through the one `tools.push` (lib/wk/tools.py), then runs
     `./setup --stage quiesce` from a terminal, refusing (not merely warning)
     when there is none. Every one of these prints what it would do under
     `--dry-run` rather than refusing, even when the unit tier's ssh shim
     reports the machine unreachable -- real unreachability still refuses
     outside `--dry-run`.
   - Needs 5.6 and 5.7; after 5.11 on `cmd/pi`.
   - Owns: `cmd/pi`'s `setup` arm (deleted), `boot/machines.sh`'s
     `machine_prepare` (deleted), `lib/wk/machine_cmd/`'s board/mac branches
     and kind dispatch, `cmd/boot`'s `--prepare` handling, `tests/test_pi.py`
     (setup), `tests/test_machine_prepare.py`, `tests/test_machine_cmd.py`
     (`TestBoardSetup`, `TestBoardRm`, `TestMacSetup`).
   - About 230 lines (cmd/pi shrank by ~400; machine_cmd.py grew by ~130).
   - Closes: `unit killpoints[machine setup]` for a board
     (`TestBoardSetup.test_a_setup_killed_after_any_effect_and_rerun_converges`).
     Owed: `lint.no_addresses` -- `NODE_MAC` still lives in the board confs and
     in `lib/wk/reach.py`/`boot/machines.sh`'s `image_addr` (both belong to
     other sub-steps' file ownership; closing it needs one pass across those
     plus every bench-kind conf, since `TestConfFieldSets` holds one field set
     per kind); marks live `pi.setup[<board>]` (needs a board in hand).
   - Decision: `setup <board>` checks that the tailnet name answers, installs
     this checkout's card helper and writes `KIND=board`. A board never gets
     tailscale any other way. `setup mbp` never fakes reachability: an
     unreachable Mac still refuses outside `--dry-run`, matching every other
     kind `wk machine setup` takes.

   5.14 **sysimage read side; `cmd/sysimage` goes Python.** *Landed*
     (`lib/wk/sysimage/`; the unported arms in `lib/sysimage-arms.sh`; `rm`
     stays a tombstone). Bench's and sysimage's `Listing` share one fleet walk
     (`lib/wk/fleetwalk.py`; each `Listing` keeps its own `store_rows` and
     row-label rule); `common.sh`'s `human_bytes` is gone, no bash caller left
     (`ls.human_bytes` is the one copy); `cmd/sysimage`'s `# wk:` line takes
     `outside`, so the dispatcher refuses `wk sysimage` inside any workspace
     and the hand-rolled `in_workspace()` check in its `main()` is gone.
     `ls` also lists the mac-volume and guest builders (5.32, 5.34), through
     `ls.host_profiles`/`ls.builder_outputs` -- the one `builder_outputs`
     `cli.Sysimage` delegates to -- shown only once found, since neither has a
     workspace to anchor a placeholder row at. Owed: `live sysimage.build[vm]`
     has no body.
   - Needs 5.5. Owns `cmd/sysimage` for 5.14–5.20, in turn.
   - Owns: `cmd/sysimage`, `lib/wk/sysimage/{cli,ls}.py`, `lib/wkslot.py`
     (moves to `lib/wk/slot.py`), `lib/sysimage-arms.sh` (the unported
     arms), `tests/test_sysimage_routing.py`,
     `tests/test_image_store_gone.py`, `tests/test_slots.py`.
   - `ls` (delegated across the fleet), `holds`, `path`, `rm`. About 350
     lines.
   - Closes: `unit sysimage.builders_conform` (the read half); marks live
     `sysimage.build[vm]` (reaching the host).
   - Decision: an image is found as `Builder.outputs(ws)` per builder kind,
     recomputed on every read. There is no manifest.

   5.15 **sysimage disks.** *Landed* (`lib/wk/sysimage/disk.py`; sysimage imports boot,
     never the reverse). Only `lsblk -J` is parsed: `admin/wk-card-priv` is
     Linux-only, so no writer is a Mac and a `diskutil` parse would have no
     caller. Owed: `lib/wk/boot/pi.py` reaches the model through the bash shim
     until 5.39.
   - Needs 5.14.
   - Owns: `boot/disk.sh`'s model half (`disk_parse` through
     `disk_refuse_unless_safe`, `disk_resolve_own`, `disk_for_machine`),
     `lib/wk/sysimage/disk.py`, `tests/test_disk_logic.py`.
   - About 350 lines.
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
     WiFi themselves); the presence checks `shell.tailnet_key_present`/
     `tailnet_api_present` are still bash, until `wk.tailnet` answers them.
   - Needs 5.15 and 5.7 (`boot/onboard`).
   - Owns: the rest of `boot/disk.sh`, `cmd_write*`, `stage_units`,
     `_tailnet_*_preflight`, `lib/wk/sysimage/write.py`,
     `boot/check-boot-files.py`, `tests/test_card_edits.py`,
     `tests/test_owed_image_write.py`, `tests/test_wifi_seed.py`,
     `tests/test_board_config_append.py`.
   - Stream, verify, card edits through `admin/wk-card-priv` (unchanged),
     seeding, first-boot units. About 800 lines. `flash` becomes a
     tombstone.
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
     (hours, past the runner's per-test budget); `unit profiles.a_pinned_kernel_builds_none` stays owed --
     the pinned-kernel defconfig still builds a kernel, and `post_image` reads the DTS name from it.
   - Needs 5.14 and 5.2.
   - Owns: `image/buildroot.sh`, `image/buildroot-build.sh`,
     `image/buildroot-webkit.sh`, `image/fetch.sh`, `image/buildroot/*.sh`,
     `lib/wk/sysimage/{task,buildroot}.py`.
   - `build`/`webkit` become a task through `lib/wk/build.py`'s detach and
     `job.py`. About 700 lines.
   - Closes: `unit sysimage.task_states`, `unit killpoints[sysimage build]`,
     `lint.build_wall`, `unit record.progress_shape[sysimage]`.
   - Decision: one detach and one watchdog, `build.py`'s. Neither `yocto.sh`'s
     nor `pmos.sh`'s survives.

   5.18 **yocto, host half.** *Landed* (`lib/wk/sysimage/yocto.py`, a `task.Stage` with
     no deadline on its record; `job.watch_pid` gives up on silence only when told to, and on a
     wedge of `WEDGE_BEATS` heartbeats naming one bitbake task). The cross configs moved here out
     of `build/configs.sh`; a yocto stage now refuses a held workspace lock instead of waiting an
     hour. `Yocto` and `Buildroot` are `task.ContainerBuilder`s: the target refusal, the
     digest-tagged host image and the workspace it makes are one implementation, and each subclass
     is its data and its stages. Owed: `image/yocto.sh` is two shims for `image/pgo.sh` until 5.26; `live
     sysimage.sstate_reuse` has no body -- a second image build is minutes to hours, past the
     runner's per-test budget; `WEDGE_BEATS` (4 h) is a guess no wedged run has been measured
     against; `WK_YOCTO_BASE` is not in `wk sysimage -h`.
   - Needs 5.17.
   - Owns: `image/yocto.sh`, `lib/wk/sysimage/yocto.py`,
     `tests/test_yocto_stage.py`.
   - Stage index, cooker stop and wedge detection. About 570 lines.
   - Closes: marks live `sysimage.sstate_reuse`.
   - Decision: "silent" and "wedged" are `job.stall_report` verdicts over the
     same heartbeat, not a second yocto rule.

   5.19 **yocto, in-target half.** *Landed* (`lib/wk/sysimage/yocto_target.py`, with
     port-target folded in; the memory guard stays `build/guard.sh`, reached as one `bash -c`
     line). Nothing of it has run in a workspace yet. Owed: `live sysimage.pseudo_reproducer` has
     no body -- it needs a pseudo built at scarthgap's pin (e11ae91), which nothing builds, so
     whether to build one for the test or drop the bump at the next poky move is the user's call.
   - Needs 5.18.
   - Owns: `image/yocto-build.sh`, `image/yocto/port-target.py`,
     `lib/wk/sysimage/yocto_target.py`.
   - Runs in the workspace as `python3 /opt/wk-tools/...`. About 570 lines.
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
     one question covers it. The avahi-daemon enablement is gone (there is no
     mDNS); `image/pmos.sh` is gone. Owed: no live check (needs an aarch64
     Linux build host and a phone); `test_owed_pmos.py` covers the reporting,
     refusals, host resolution, the copy and the rubble rows against the Fake,
     and `pmos_build.py`'s pure parts, not `Build.run` or the `iw scan` band
     check -- pmbootstrap, loop devices and sudo have no fake to run against;
     `lint.one_wifi_reader` stays owed for `pmos_build.py`'s netplan read.
   - Needs 5.17. ∥ 5.18.
   - Owns: `lib/wk/sysimage/pmos.py`, `lib/wk/sysimage/pmos_build.py`,
     `tests/test_owed_pmos.py`.
   - About 750 lines; the remote half goes Python too, since the aarch64
     build host has python3.
   - Closes: the `test_owed_pmos` rows.
   - Decision: pmos stays. It exists only for the bridge phones, which
     5.37 needs.

   *Group 3: the bench pipeline*

   5.21 **The `System` interface and the workspace run.** *Landed*
     (`lib/wk/bench/systems.py`: container and guest; `pipeline.py`: `wk bench
     run`). Owed: the bash `plan_json`/`seed_payload` and `bench_task_new`
     stay for `cmd/pi`, `cmd/ab` and `build/mac-pgo.sh` until 5.22.
   - Needs 5.10 and 5.2.
   - Owns: `lib/wk/bench/{pipeline,systems}.py`, `cmd/bench`'s run arm
     (`cmd_run`, `preflight`, `run_jsc`, cores and aslr), `lib/profiler.sh`,
     `tests/test_bench_cores.py`, `tests/test_bench_container_run.py`,
     `tests/test_bench_channel_ssh.py`.
   - `System`: boot, deploy, run, collect; the container and guest systems.
     `bench run <ws> <plan>`; bare `bench <ws> <plan>` refuses, naming
     `run`. About 650 lines.
   - Closes: `unit bench.pipeline_conformance[container|guest]`,
     `unit bench.pins_cores`, `unit killpoints[bench]`,
     `unit record.progress_shape[bench]`,
     `unit dispatch.dry_run_is_the_recorder[bench]`.
   - Decision: `run` leaves its run directory on the system's machine, and
     `collect` copies it through the one `Machine` copy. Where run-benchmark
     runs is each system's choice, not the pipeline's.

   5.22 **Board deploy.** *Landed* (`lib/wk/bench/board.py`'s `BoardSystem`, reached as `wk bench
     deploy <ws> <board> [--slot <name>]` through `lib/wk/bench/cli.py`'s `Bench.deploy` and `cmd/bench`'s
     `deploy` sub; `cmd/pi`'s `deploy` verb and `pi_deploy_slot` are gone, `deploy` a tombstone naming `wk
     bench deploy`). `<ws>` is the lane itself now, not a `<profile>[@<machine>]` spec: the dispatcher's
     ordinary `where=workspace` routing (the same `run` already uses) finds the machine holding it and
     forwards there, so cmd/pi's own `_pi_spec`/`_pi_lane`/`--wsname`/`--wstarget` machinery is gone with
     it. The bash `plan_json`/`seed_payload`/`bench_task_new` (lib/bench.sh) stay for `cmd/pi`'s `bench`
     arm, `cmd/ab` and `build/mac-pgo.sh`, none of which this touches. Owed: `machine_armed_barrier`
     (the check that a deploy is not landing in the system a `wk boot` arming is about to leave) is not
     ported -- `lib/wk/boot/driver.py`'s `Channel`/`driver_class` reach it, as `lib/wk/sysimage/write.py`
     and `lib/wk/bench/mac.py` already do, but wiring and testing it is real design work this step's
     Decision does not cover; `tests/test_boot_armed.py`'s `test_wk_bench_deploy_calls_it` records this
     as `owed`. Marking live `bench.routed_deploy` needs a board in hand, so it stays unwritten.
   - Needs 5.21, 5.14 and 5.13 (`cmd/pi`).
   - Owns: `cmd/pi`'s `deploy`, `pi_deploy_slot`, `lib/wk/bench/board.py`.
   - About 250 lines.
   - Closes: marks live `bench.routed_deploy` (needs a board in hand).
   - Decision: deploy is `Machine.copy_tree_in`, then the slot's sha
     manifest read back. There is no rsync path.

   5.23 **Board run.** *Landed* (`lib/wk/bench/board.py`: `BoardSystem`'s `boot`/`checks`/`deploy`/`run`/`evidence`
     and `BoardRun`, reached as `wk bench run <ws> <plan> --system <board> [--slot]` through `systems.for_workspace`
     and `pipeline._run_class`; the driver moved to `lib/wk/bench/board_driver.py`; the board's shell is
     `boot/onboard/bench-*.sh`; `Machine.forward` on `Ssh` and `Fake`; `pipeline.Run` gained `records()`/`put()` so a
     board run's record and device claim are this machine's). The armed barrier is `BoardSystem.barrier`, asked by
     deploy and run through the boot driver; a deploy now lands on the bench system (the boot driver's `i_ssh`
     machine, as `wk pi deploy` did, not NODE_SSH) and claims the board as `wk pi deploy` did. In a workspace, deploy
     and a `--system` run are the broker's `stage`/`run` verbs, now `wk bench deploy`/`wk bench run` on the host
     (`boot/cli.py`'s `broker_request`, shared with `wk boot`). `wk pi bench` for one slot is a tombstone; its
     `--ab`, `--ab-systems` and `--pgo` arms run each leg as `python3 -m wk.bench.board leg`, and
     `bench_runner_tree` is a one-line shim over `board.runner_tree`. `wkslot env/expect/verified/get`, `wkdata
     cores-wrap/bench-class` and `bench_configuration_args` lost their last caller and are gone. Owed: `live
     bench.leg_completes[<b>]` needs a person at a board (a leg pins the clock, kills browsers and starts a
     compositor, which the live tier may not); `live bench.evidence[<b>]` has its read-only body
     (`TestARealBoardAnswersWhatALegRecords`, not yet run against a board); an A/B leg now re-prepares the board
     (probe, clock, session) per leg, where the bash prepared once -- 5.24 decides; the A/B's own sysid probe and
     subtest exclusions stay bash until 5.24; whether the podman VM a Mac forwards a container lane into can reach a
     board over the tailnet is the ACL question, unchanged and yours.
   - Needs 5.22 and 5.11.
   - Owns: `cmd/pi`'s `pi_bench_once`, session and weston, tunnel,
     `pi_bench_record`, `pi_leg_prepare`, `bench/wk_board_driver.py`,
     `lib/wk/machine.py`'s new `forward`, `tests/test_pi_channel.py`,
     `tests/test_pi_agent.py`, `tests/test_bench_warmup.py`.
   - About 700 lines.
   - Closes: `unit bench.pipeline_conformance[board]`,
     `unit bench.failed_leg_keeps_evidence`,
     `unit record.progress_shape[board run]`; marks live
     `bench.leg_completes[<b>]`, `bench.evidence[<b>]`.
   - Decision: the benchmark server reaches the board through
     `Machine.forward(port)`, a context manager on `Ssh` and `Fake`. The
     ACL question stays yours; this is the one place it changes.

   5.24 **Two-arm A/B on one board; `cmd/pi` is gone.** *Landed* (`lib/wk/bench/board_ab.py`, reached as `wk bench run
     <ws> <plan> --system <board> --ab A,B | --ab-systems A,B [--slot] [--rounds] [--task] [--timeout] [--exclude-subtests]
     [--no-warmup-profile] [--jit-tiers]` through `pipeline.run`; each leg is a `BoardRun` named `<ws>-leg` under the A/B's
     own record, which holds the board and is what `--kill` stops). The system boot per leg is `AB.boot`, over `wk boot`'s
     own `Boot.arm`/`back`, deciding by the driver's `arm_from_bench` where the bash tried and fell back; a system A/B ends
     by handing the board back to its rescue and clearing the arming record there. `BoardSystem` pins the clock, claims
     the board and brings the session up once per (system id, boot id) and re-reads the probe, facts, display and slot
     every leg; `BoardRun.pin` resolves the runner tree and payload once per A/B; a slot A/B's task records
     `devices=<board>=<profile>`. The width rule and `bench/subtest-exclusions.conf` are read in Python. `--pgo` is `wk
     bench run ... --slot <s>-instr --collect` (`BoardRun.collection`, reading build/pgo.sh's facts). `wk pi` is a
     dispatcher tombstone naming each verb's replacement; `cmd/pi`, `bench_runner_tree`, `board.main` (the `leg`/`runner`
     CLI), `wkdata`'s `subtests`/`warmup-check`/`cores-valid`, `image_lane_arg`/`image_lane_here`/`image_spec_target` and
     `images.refuse_elsewhere` lost their last caller and are gone; so did the `pi-bench-<board>` lock, the device claim
     being the one lock. Owed: `image/pgo.sh`'s collect step (5.26) and `cmd/ab` (5.25) still name `wk pi` -- each already
     reached a tombstone at its `wk pi deploy` since 5.22; `killpoints[bench]` over a whole A/B is not written (a single
     leg's is); `live boot[rpi3]`'s `--ab-systems` run needs a person at the board; an A/B or a collection typed in a
     workspace is refused, since the broker has no verb for either.
   - Needs 5.23.
   - Owns: `pi_bench_ab`, `pi_bench_ab_systems`, `pi_warmup_round`,
     `pi_system_boot`, `bench/subtest-exclusions.conf` (read by Python),
     `tests/test_pi_ab_systems.py`.
   - About 400 lines; `cmd/pi` becomes a tombstone.
   - Closes: `unit boot.two_system_lane_on_fake` (renamed
     `boot.two_systems_on_fake`).
   - Decision: `--ab` slots and `--ab-systems` are one arm type, (system,
     slot); a slot-only A/B holds the system fixed.

   5.25 **`bench ab` across the fleet; `cmd/ab` is gone.** *Landed* (`lib/wk/bench/ab.py`, reached as `wk bench ab
     <pr-spec|branch|sha> --devices <a,b> ...`, `wk bench ab --systems A,B --devices <board> [--slot S]` and `wk bench ab
     <task> --kill` through `lib/wk/bench/cli.py`'s `Bench.ab`; `cmd/bench`'s `flag --kill takes=0` line, which refused
     every name=none subverb's `--kill`, is now the `sub ab` line; `wk ab` is a dispatcher tombstone). The scheduler is
     `lib/wk/sched.py`, run in-process on an injected executor: each build, deploy and collection step is the `wk` command
     a person types, run through `Machine.act_run` with its output streamed to one log per resource, each board's rounds are
     `board_ab.run` in this process recording into the A/B's task, and the report is `report.task_report`. The kill is
     `job.kill` over the record's process tree. A plan states its cost before it runs: each board's legs times the median
     measured leg of that plan there (`leg_seconds`, scaled by `--count`). The report's pairing is
     `lib/wk/bench/record.py`'s `paired`, which drops a round an arm did not finish or whose arms ran on two payload pins
     (runner commit, benchmark copy). `lib/sched.py`, `lib/common.sh`'s `match_any`, `lib/store.sh`'s `pr_*`/`*_refname`/
     `mirror_fetch_*` shims and `wk.pr`'s CLI, `bench_task_attach`, `task_stamp` (and `wk.record stamp`),
     `image_config_file` (and `wk.images conf-path`) lost their last caller and are gone. Owed: `lib/sched.sh` is a shim
     over `python3 -m wk.sched` for `image/pgo.sh`, whose own cycle (`image_pgo_steps`) still names `wk pi deploy` and `wk
     pi bench --pgo` -- 5.26 moves it onto `ab.py`'s `pgo_steps`; the in-process board A/B steps log to this process's
     stderr rather than a per-board file, so two boards' rounds interleave there; `--systems` takes one board, since a
     system id names one board's image; an A/B across real boards (`live bench.leg_completes[<b>]`) needs a person at
     them.
   - Needs 5.24 and 5.14.
   - Owns: `cmd/ab`, `lib/sched.sh`, `lib/sched.py` (moves to
     `lib/wk/sched.py`), `lib/wk/bench/ab.py`, `tests/test_ab_*.py`,
     `tests/test_sched.py`.
   - About 630 lines.
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
     are the one `wk`-command step and done question both graphs build from. `image/pgo.sh`, `image/yocto.sh`,
     `lib/sched.sh`, `wk.sched`'s records CLI, `yocto.main`/`pgo_facts`, `sysimage-arms.sh`'s `yocto-pgo` arm, and the
     `image_pgo_*`/`image_lane_*`/`image_spec_*`/`image_build_resource`/`image_holds_predicate`/`image_check_slot_name`/
     `image_pgo_wanted` shims with their `wk.images` verbs lost their last caller and are gone. Owed: `build/pgo.sh` is a
     one-line-per-value shim over `python3 -m wk.pgo fact` and `lib/wkpgo.py` one over `wk.pgo.main`, both for
     `build/mac-pgo.sh` until 5.29 ports it; `tests/test_mac_pgo.py`'s `test_the_two_lanes_take_the_list_from_the_one_place`
     still reads `image/pgo.sh` (5.29's file); `unit pgo.no_local_patch` stays owed, since upstream's OSXMiniDriver still
     names no profile directories and webkitpy's `locate_binary_xcrun` still runs `/usr/bin/xcrun` off macOS, so
     `build/pgo-run-benchmark.py` and the blunting stay; `live bench.pgo_collection[<b>]` is marked on its read-only half
     (the newest collection passes the gate with every leg's run), while recording each plan's cost and what the
     collecting browser rendered and JITted with is not written, and taking a collection needs a person at the board.
   - Needs 5.25 and 5.18.
   - Owns: `image/pgo.sh`, `build/pgo.sh`, `lib/wkpgo.py` (moves to
     `lib/wk/pgo.py`), `tests/test_board_pgo.py`,
     `tests/test_pgo_harness.py`.
   - About 250 lines.
   - Closes: `unit pgo.no_local_patch`; marks live
     `bench.pgo_collection[<b>]`.
   - Decision: a collection is `bench run --collect` against an instrumented
     slot, called from `sysimage build`'s pgo phase. There is no
     `pi bench --pgo`.

   5.27 **Mac stage and staged run.** *Landed* (`lib/wk/bench/mac.py`: `Stage`,
     `MacVolumeSystem`, `StagedRun`, `Gates` as `wk bench staged --gates`; a
     guest's run records as `rehearsal`). A staged dry run whose checks fail
     now exits 1. Owed: `bench/mac-lane.sh`, `bench/mac-ab.sh` and
     `bench/mac-bench-autorun.sh` call the Python (5.28-5.31); `PUT_SKIP`
     repeats `BENCH_PUT_SKIP` until `mac-ab.sh` is ported.
   - Needs 5.21 and 5.12. ∥ 5.22–5.26.
   - Owns: `cmd/bench`'s `stage`/`staged` arms, `bench/mac-browser-check.py`,
     `bench/mac-window-probe.sh`, `bench/mac-raiser.sh`,
     `lib/wk/bench/mac.py`, `tests/test_mac_gates.py`.
   - About 700 lines.
   - Closes: `unit bench.pipeline_conformance[mac-volume]`,
     `unit bench.one_record[mac-volume]`,
     `unit bench.preflight_asks_every_gate`, `unit dispatch.where[bench]`.
   - Decision: `staged` is `bench run` on the bench install, which resolves
     itself as the system in bench mode.

   5.28 **`bench mac`.** *Landed* (`bench/mac-lane.sh` deleted; `wk bench run <ws> <plan> --system
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
     call, not a rerun's, on real hardware or fake). `tests/test_wk_overrides_misc.py`'s
     `TestMacLaneOverrides` (bash-lifted coverage of the deleted script) is gone with it, and
     `tests/test_mac_pgo.py`'s `test_both_mac_lanes_default_to_it` now checks the one lane left
     (`bench/mac-ab.sh`). Owed: `live bench[mbp]` and `live bench.first_run_after_stage[mbp]` close
     only their read-only half (`tests/test_owed_bench.py`'s `TestBenchReachesTheRealMbp`, gated
     `requires_machine("tolken")`) -- staging a real build, rebooting mbp into bench mode, running a
     plan and comparing against a container run needs a decision about which workspace and plan to
     spend that machine's time on, left to whoever runs the live tier next; `MacHostSystem.after()`
     cannot bless the machine back to host mode from its own benchmark install (only the host
     install carries the boot helper -- measured against 5.12's own driver, not theorised), so a
     real run stops there and names the two-click remedy, same as arming always has. 5.27's own
     "Owed" line still names `bench/mac-lane.sh` as a caller for 5.29-5.31 to update; it is gone,
     and those steps call `wk bench run --system mbp` or the granular commands directly instead.
   - Needs 5.27.
   - Owns: `bench/mac-lane.sh`, `tests/test_owed_bench.py` (the mac class).
   - About 650 lines; the lane's state directory becomes the task record.
   - Closes: marks live `bench[mbp]`, `bench.first_run_after_stage[mbp]`.
   - Decision: `bench mac` is the pipeline run with `--system mbp`, not a
     verb of its own. `mac` stays only as a tombstone.

   5.29 **Mac A/B, front half.** *Landed* (`lib/wk/bench/mac.py`'s `MacAB`, reached as `wk bench ab --devices <mac>
     --systems <staged-a>,<staged-b>` or `--patch <ref|diff> --workspace <ws> [--base <ref>]` through `ab.run`, which hands
     a `KIND=mac|guest` machine to it; `ab.check_plan` is the plan refusals both share, `board_ab.pair` the arms', and each
     side refuses the other's options). Preflight, build-and-stage in the guest (the patch now travels inside the guest
     script: the manager's `/tmp` was never the guest's), reclaim, the plant (task record first, tree verified by digest,
     screensaver, Do Not Disturb, samply, tailnet payload, job, state, LaunchAgent) and the restart with its wait for which
     install came up are Python over the driver's channel (`boot/onboard/mac-sh.sh`), the drivers' new `manager()`/
     `manager_tools()` and the fake clock. `bench/mac-ab.sh` keeps only the back half (`--progress`, `--status`,
     `--collect`) and one-line shims over `python3 -m wk.bench.mac` for `--preflight`, the firmware and the provisioned
     reads; any front-half flag is refused naming `wk bench ab`. `build/mac-pgo.sh` keeps its two build phases (they are
     build-in-target.sh's `_xc_settings`/`guard_run`) and asks `PgoCollect` for the collection, the evidence and the
     instrumented directory's name; `build/pgo.sh`, `lib/wkpgo.py`, `wk.pgo fact`, `lib/bench.sh`'s `BENCH_PUT_SKIP`/
     `bench_put_excludes`/`plan_json`/`seed_payload`, `wk.bench.seed`'s CLI, `boot/machines.sh`'s `mac_ssh`/
     `machine_tools_*`, and the mac shims' `b_bench_put*`/`b_manage*`/`b_restart_detail` with their verbs lost their last
     caller and are gone; `PUT_SKIP` is the one list. The preflight no longer runs `wk machine setup` itself (a read-only
     check does not provision); it names it. `WK_MAC_SSH`/`--host`, `--tools` and `--agent-home` are gone. Owed: the
     back half's `mac()` is still `r_ssh` with the drivers' `image_addr`/`i_ssh_opts`/guest `m_ssh` overrides, and its
     notify moved into `MacAB.notify` over `wk_notify` (5.30 moves both); the live rows close only their read-only
     halves (`tests/test_mac_ab_driver.py`'s `TestTheLiveRows`: each machine's preflight) -- building, staging and
     measuring a real pair on mbp and the rehearsal on benchvm spend hours of those machines, a decision for whoever
     runs the live tier; the plant's display count (`display_verdict`) and each leg's (`bench/mac-browser-check.py`)
     are still two readers, as they were in bash.
   - Needs 5.28 and 5.25.
   - Owns: `bench/mac-ab.sh` (build, stage, plant, go), `build/mac-pgo.sh`,
     `tests/test_mac_ab_driver.py`, `tests/test_mac_pgo.py`.
   - About 650 lines.
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
     `send` (the credential rule calls it in-process). `bench/mac-ab.sh`, `bench/mac-ab-summary.sh`,
     `boot/onboard/mac-sh.sh`, `lib/wknotify.py`, `lib/store.sh`'s `wk_notify`, `boot/machines.sh`'s `i_ssh`/
     `i_ssh_opts`/`image_addr`/`r_ssh`, `lib/reach.sh`'s `reach_enumerate` with `wk.reach enumerate`, `lib/bench.sh`'s
     `SEED_DIR`, `bench/mac-tailnet.sh`'s `collect` arm with `wk.sysimage.mactailnet collect`, the Mac drivers' bash
     shims but `b_disarm`/`b_disarm_note` and `wk.boot.mac`'s CLI, `wk.bench.mac`'s preflight/firmware/provisioned
     verbs, and `tests/test_pi_channel.py` lost their last caller and are gone. Owed: `boot/machines.sh`'s `m_ssh`/
     `m_here`/`m_ssh_opts` and `lib/reach.sh`'s `reach_offline` stay for `lib/sysimage-arms.sh`'s `m_reachable`
     (`wk sysimage disks`); `lib/store.sh`'s `wk_cred_read` and `lib/bench.sh`'s `bench_task_dir`/`bench_task_new`
     have only tests left calling them; `load_driver` has no caller outside tests; CLAUDE.md still names
     `reach_enumerate` where it now means `lib/wk/reach.py`'s `find_mac` (yours to edit);
     `tests/test_host_units.py`'s `test_one_sd_notify_in_the_tree` reads `git ls-files`, so it goes green once
     `lib/wk/notify.py` is committed; the live rows close only their read-only halves (`tests/test_mac_ab_rounds.py`'s
     `TestTheLiveRows`: mbp's steps and its legs) -- resolving a PR-sized delta on mbp and a warmup profile on every
     system spend hours of each machine, a decision for whoever runs the live tier.
   - Needs 5.29. ∥ 5.31.
   - Owns: the rest of `bench/mac-ab.sh`, `bench/mac-ab-summary.sh`,
     `lib/wknotify.py` (moves to `lib/wk/notify.py`),
     `tests/test_mac_ab_rounds.py`.
   - About 700 lines.
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
     directly, so `bench/mac-bench-autorun.sh`, `bench/mac-bench-payload.sh` and `mac-tailnet.sh`'s `install` arm are
     gone; the tree the autorun runs from is its `wk-tools`, so the job's `wk_tools` field has no reader. The job's
     outcome is the stopping rule's (`resolved-at-round-N`, `hit-max-rounds`, `rounds-done`, `all-failed-round-N`),
     no longer overwritten with `ran`. `tests/test_mac_autorun.py` runs it against the Fake and the fake clock: each
     phase, each refusal, the drift refusal after the quiesce, the settle, the hold, the watchdog, the hand-back, a run
     killed after any effect finishing on the next boot, and the dry run. Owed: `tests/test_mac_ab_rounds.py` (5.30's)
     still lifts functions out of the deleted script, and its autorun classes are covered here and go with it;
     `mac-tailnet.sh`'s `collect` arm has no caller; nothing has run on the Mac: a planted job measured end to end
     spends hours of mbp, a decision for whoever runs the live tier.
   - Needs 5.29.
   - Owns: `bench/mac-bench-autorun.sh`, `lib/wk/bench/autorun.py`,
     `tests/test_mac_autorun.py`.
   - About 700 lines.
   - Decision: port it, because firstboot guarantees python3 before the
     LaunchAgent can fire. `mac-bench-firstboot.sh` stays shell.

   5.32 **The Mac volume as a `sysimage` builder.** *Landed* (`lib/wk/sysimage/macvolume.py`,
     `mactailnet.py`; `wk sysimage build perf-macos-tolken [--create|...|--all]`, and `wk bench mac-volume`
     is a tombstone). The pin moved into `mactailnet.py`; `WK_BENCH_NO_PKG` and `WK_BENCH_PASSWORD` are gone.
     The join stays shell in `bench/mac-tailnet.sh`: the first boot joins before the install has a python3.
     Owed: `bench/mac-tailnet.sh`'s `collect`/`install` and `bench/mac-bench-payload.sh` are shims for
     `mac-ab.sh` and the autorun (5.29, 5.31); `bench/mac-pyobjc.sh` stays shell, since `targets/vm.sh` and
     `vm/provision-base.sh` pipe it into a guest with no tree; `holds`, `path` and now `ls` (5.14) reach the
     volume's marker through `cli.Sysimage.builder_outputs`/`ls.builder_outputs`, shown only once installed
     since a builder with no workspace has nothing to anchor a placeholder row at (`test_every_builder_
     a_profile_names_has_outputs` now closes for mac-volume and guest, and stays owed for pmos and fetch
     only); the WiFi read is still the keychain's (`lint.one_wifi_reader`: the card helper is Linux-only
     and gates usb/mmc disks, so taking the Mac's read is a decision for you); `mac-ab.sh` and `cmd/bench`
     still name `wk bench mac-volume` (the autorun and `cmd/quiesce` now say `wk sysimage build
     perf-macos-tolken`), and `cmd/bench` still declares `--install,--all` destructive; the Go cache moves
     to the state directory on a Mac host. Nothing has run on the Mac.
   - Needs 5.17 and 5.12. ∥ Group 3.
   - Owns: `bench/mac-bench-volume.sh`, `bench/mac-tailnet.sh`,
     `bench/mac-tailnet-pin.inc`, `bench/mac-bench-payload.sh`,
     `bench/mac-pyobjc.sh`, `lib/wk/sysimage/macvolume.py`,
     `tests/test_mac_tailnet.py`.
   - About 1,100 lines; split at the tailnet seam.
   - Closes: `unit sysimage.builders_conform[mac-volume]`; marks live
     `sysimage.mac_volume_provision`.
   - Decision: the install's `/etc/wk-image` marker is the builder's done
     marker, the same as a board's.

   *Group 4, ∥ Group 3*

   5.33 **Guest start and stop, and the host daemons.** *Landed* (`lib/wk/guest.py`:
     `Host` and `Guest`; `Vm.write_marker` is the one marker writer). Owed: the
     guest's shell rc, lldbinit, checkout, Claude CLI and desktop steps still
     run from `targets/vm.sh` through `shell.guest_step` (5.34); the bash
     `push_agent_*` family is held only by `test_push_agent.py`'s bash classes.
   - Needs step 4 and 5.3.
   - Owns: `targets/vm.sh`'s `t_start`/`t_stop`/`_boot`, proxy, inject,
     agent forwarding, `vm_push_*`, `lib/wk/targets.py`'s `Vm` write side,
     `lib/wk/shell.py`'s `guest_*`/`vm_push_*`,
     `tests/test_push_vm.py`, `tests/test_vm_clock.py`.
   - About 800 lines.
   - Closes: `unit record.one_lock_per_resource[guest start]`; marks live
     `vm.egress[<c>]`, `vm.shared_mirror`.
   - Decision: the proxy, inject and agent daemons are `Machine.spawn`ed
     with pidfiles, which are locks, not state. The same code serves every
     guest.

   5.34 **Guest create and destroy, and the base as a builder; `cmd/vm` is
   gone.** *Landed* (`lib/wk/sysimage/guestbase.py`, the `guest` builder of
     `image/configs/macos-guest-base.conf`; every converge step, the admission, the
     desktop and load findings and `wk doctor <guest>`'s rows in `lib/wk/guest.py`;
     `Vm.start` writes the alias; `vm` is a dispatcher tombstone; the bash
     `ssh_alias_*`, `tools_committed`, `wk_push_key` and the test-only `push_agent_*`
     are gone). Owed: `targets/vm.sh` is the read contract its unported bash callers
     load (`ws_on_target`, `cmd/gc`, `cmd/boot`, `shell.target_pid_alive`) until
     5.39; `vm/desktop.sh`, `vm/provision-base.sh` and `bench/mac-pyobjc.sh` still
     name `wk vm` in their messages, since editing a base input re-stales every
     base -- fold it into the next rebuild; `holds`, `path` and now `sysimage ls`
     (5.14) reach the sealed base's marker through `cli.Sysimage.builder_outputs`/
     `ls.builder_outputs` and `guestbase.Base.outputs`, shown only once sealed
     since a builder with no workspace has nothing to anchor a placeholder row
     at (`test_every_builder_a_profile_names_has_outputs` now stays owed only for
     pmos and fetch); `live vm.base_matches_pin` fails on the one Mac until its
     base is rebuilt (sealed before an input change on this branch).
   - Needs 5.33 and 5.17.
   - Owns: the rest of `targets/vm.sh`, `cmd/vm` (becomes a tombstone),
     `vm/*.sh`, `vm/desktop-unblock.py`, `lib/wk/sysimage/guestbase.py`,
     `tests/test_vm_base.py`, `tests/test_vm_desktop.py`.
   - About 800 lines.
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
     defaults `cmd/bridge`'s bash used to). Owed: `cmd/bridge`'s
     `resolve_ssh` is a one-line shim onto `wk.bridge resolve` for
     `setup`/`tailnet`/`rm` (unported, 5.36); `BR_VIA`/`_ssh_via` (never
     set) are pre-existing dead code this step did not touch, since
     `rsh`/`_reaches` still read them for the unported arms; `live
     bridge.segment[<bridge>]` waits on 5.37 (hardware); `wk.reach`'s sweep
     needs ip(8) and nmap, so a macOS host finds a phone off the tailnet only
     by `--at`.
   - Owns: `cmd/bridge`'s `ls`/`status`/`battery`/discovery,
     `lib/wk/status.py`'s `BRIDGE_PROBE`, `lib/wk/fleet.py`'s bridge kind,
     `tests/test_bridge.py` (read classes).
   - About 400 lines.
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
     importer repointed; `cmd/bridge`'s `setup`/`tailnet`/`rm` are
     tombstones, `rsh`/`_reaches`/`resolve_priv`/`bridge_bootstrap_root`/
     `BR_VIA` are gone, and `provision` keeps one-line shims for `cmd_setup`
     and `route_approved`). Owed: `live bridge.setup[moose-bmc]` is marked and
     has not run; the device note and kill-switch text still come from
     `bridge/devices.sh` through bash (5.37 makes it a table); the phone's
     output arrives when each phase ends, not as it runs; `rm` logs the node
     out and does not retire it through `wk.tailnet`. The auth key's
     mint-or-reuse rule is `wk.tailnet.Fleet` (bash keeps a one-line
     `wk_tailscale_authkey` shim for `host/macos/machine.sh`); owed:
     `sysimage/write.py` and `sysimage/mactailnet.py` call `Fleet` directly,
     and `shell.tailnet_authkey`/`tailnet_retire`, forwarders onto it, go.
   - Needs 5.35.
   - Owns: `cmd/bridge`'s `setup`/`tailnet`/`rm`, `bridge/provision.sh`,
     `lib/wk/bridge.py`, `lib/tailnet.py` (moves to `lib/wk/tailnet.py`).
   - About 700 lines, `provision.sh` shrinking from 664 to about 150.
   - Closes: `unit killpoints[machine setup]` for a bridge; marks live
     `bridge.setup[moose-bmc]`.
   - Decision: the host renders nftables, dnsmasq and the init scripts from
     the conf, and the phone only applies them. The phone has no python3.

   5.37 **Bridge provision; `cmd/bridge` is gone.** *Landed*
     (`lib/wk/bridge/provision.py`: `wk machine setup <bridge> --disk
     <machine>:<device> [--image <path>|--rebuild]` asks once, gets the
     newest `PMO_BRIDGE` build off its host (building it if none, or on
     `--rebuild`), hands it to `wk sysimage write --disk` as the one writer,
     prints the hands-on steps and waits up to 15 minutes for the phone (names
     every tick, a sweep once a minute) before `Role.apply`; the Jumpdrive
     card, the head-hash disk detection, the `pause` prompts and the
     route-approval loop are gone, and `BR_CARD` with them.
     `bridge/devices.tsv` is the device table (`wk.bridge.devices`); `wk
     machine status [<bridge>]` is the old `ls`/`status`, `wk doctor`'s
     battery row reads `Bridge.battery` in process, `bridge` is a dispatcher
     tombstone, and the shims only it called (`image_fetch_base`,
     `pmos_host`/`newest_out`/`fetch_out`, `image_profile_list`,
     `disk_candidates`) and their Python CLI verbs are gone). Owed: `live
     bridge.segment[<bridge>]` is marked (health check and cable only) and
     has not run; the rest of that row -- the eMMC route end to end, a board
     getting its reserved address, the netwatch ladder, the dock at 480, the
     camera -- needs a person at the phone; the copied image lives under the
     store's artifact directory, under a per-bridge lock, and
     `bridge.provision.rubble` names one a kill left and `wk gc` takes it.
   - Needs 5.36, 5.16 and 5.20.
   - Owns: `cmd/bridge`'s `provision`, `bridge/devices.sh` (becomes a data
     table).
   - About 450 lines.
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
     `wk disk` renders the same rows. `gc_creation_records`,
     `image_build_locations`, `image_cache_dir`, `ws_create_log`,
     `_ws_task_lib`, `mirror_is_here`, `wk_base_dir`, the `unpinned`/
     `unreferenced` shims, `pmos_prune`/`pmos_purge_work` and the
     `WK_TART_CACHE_GB`/`TART_HOME` bash defaults are gone. Owed: the VM's half
     runs through `Container.wk`, not an effect, so no kill point lands inside
     it; half-made workspaces are found only on targets whose store this
     process reads, not on a build machine; no live test runs gc against the
     real VM, a board or tart; the pmos row sizes all of `out/`, not what
     `prune` takes. `wk disk` renders `Gc.rows()` once, grouped by kind, and
     counts the golden base and tart's cache as storage in its own rows; a
     failed `podman images` is a `why=` row; the VM's half gets `--yes` in its
     argv rather than an environment poke.
   - Needs 5.4, 5.14, 5.20, 5.27 and 5.34.
   - Owns: `cmd/gc`, `lib/wk/gc.py`, `tests/test_gc_tart_cache.py`,
     `tests/test_owed_gc.py`.
   - About 310 lines.
   - Closes: `unit gc.reclaims_or_names[<kind>]` for every listed kind,
     `unit killpoints[gc]`.
   - Decision: each module that makes rubble exports `rubble()` rows (what,
     size, the flag that takes it). gc and `disk` render one list.

   5.39 **The deletion.** *In progress.* Landed: the session mode, its warning and
     the BMC's `ast` node are `lib/wk/session.py`'s (`Session.mode`/`mode_warn`/`bmc_drm_device`,
     `session.here`); the lldb prelude and pin are `lib/wk/ldpath.py`'s; the workspace
     architectures, the armhf image and the GPU rule are `lib/wk/buildconf.py`'s (`arch_canon`,
     `arch_has_gpu`, `IMAGE_ARMHF`) and `lib/arch.sh` is gone; the store question is
     `Store.is_local`, the tailnet presence checks `tailnet.Fleet.key_present`/`api_present`, the
     peers `Registry.peer_workstations`, the stopped containers `cmd/start`/`cmd/stop` ask through
     `Container.podman()`, and `Doctor.paths`/`Doctor.in_vm` are Python. `wk bench seed`'s readiness
     wait is `Target.wait_ready` (`lib/wk/bench/cli.py`, no more `load_target`/`wait_ready` bridge),
     and the SDK's local pull and upstream tags are `Container.sdk_local`/`sdk_upstream`
     (`lib/wk/targets.py`; `lib/wk/status.py`'s `t_sdk_local`/`t_sdk_upstream` bridge is gone, and so
     are `targets/container.sh`'s bash bodies). Their bridges in
     `lib/wk/shell.py` (`session_mode*`, `bmc_drm_device`, `lldb_prelude`, `in_machine`,
     `local_state_paths`, `peer_workstations`, `store_is_local`, `tailnet_*_present`, `arch_*`,
     `agent_secret_stored`) and the bash they reached in `lib/common.sh` are gone. `lint.layering`'s
     row is deleted (no test held it); step 2's done condition (no bash file parses JSON) is
     `tests/test_static_rules.py`'s `test_no_bash_file_parses_json`, `owed` until `jq` leaves
     `bridge/bin/wk-bridge-healthcheck`, `bridge/bin/wk-bridge-netwatch`, `bridge/provision.sh`,
     `claude/hooks/webkit-jsc-skill-reminder.sh` and `claude/install.sh`, and inline `import json`
     leaves `bench/mac-quiet-desktop.sh`, `admin/wk-card-priv`, `host/macos/machine.sh`,
     `targets/vm.sh` and `targets/remote.sh`.
     Owed, in order: `lib/target.sh` and `targets/*.sh` (their remaining bash callers:
     `container/ssh-transport.sh`'s `t_ssh_exec`, `container/firstrun.sh`'s `_store_fn`,
     `lib/store.sh`'s `secrets_publish`, `lib/sysimage-arms.sh`'s `disks`, and `shell.LIBS`),
     with `lib/wk/targets.py`'s, `record.py`'s and `job.py`'s `main` and `shell.CallerShell`;
     every shim of 5.2-5.38 (`build/configs.sh`, `image/profiles.sh`, `lib/image.sh`,
     `lib/bench*.sh`, `lib/{task,watchdog,detach,resources,par,lockrun,quiet,profiler,reach,broker}.sh`,
     `boot/*.sh`, the callerless half of `lib/store.sh`) and the tests that lift them; the
     conf-key rename; `lint.one_machine_name_reader`, `lint.vocabulary`; the "Sizes today" and
     "What stays shell" tables.
   - Needs everything above.
   - Deletes `lib/target.sh`, `targets/*.sh`, every shim, every step-5
     `shell.py` bridge, and `lib/{arch,broker,tools}.sh` if nothing is
     left. Renames the conf keys to plain lowercase, since no bash reads
     them.
   - Closes: `lint.one_machine_name_reader`, `lint.vocabulary`, the `hostname`
     row, and step 2's done condition.
   - Decision taken: the `lint.layering` row is deleted rather than landed,
     since README does not yet define the layers.

   **What stays shell** (it runs on a board, a phone, a bench install or in a
   target, before or without python3). Lint counts it by directory:

   | where | what | today | after step 5 |
   | --- | --- | --- | --- |
   | a board's first boot | `boot/onboard/`: self-disarm, self-return watchdog, rescue marker, tryboot staging | ~150, inside drivers | ~150 |
   | the Mac bench install before python3 | `bench/mac-bench-firstboot.sh` | 279 | ~180 |
   | the bridge phone (busybox ash, no python3) | `bridge/init.d/*`, `bridge/bin/*`, the apply script | 1,453 | ~600 |
   | a build target | `build/guard.sh` (exec wrapper) | 54 | 54 |
   | **step-5 share** | | | **~1,000** |

   The rest of the tree's on-target shell stays outside step 5, and neither
   part fits the 1k budget alone:
   - `container/` is 1,198 lines, of which `firstrun.sh` 311 and
     `sdk-patches/apply.sh` 480 are portable, since a container has
     python3.
   - `build/build-in-target.sh` and `mem-watchdog.sh` are 225.
   - The three privileged helpers are 2,281, with `wk-card-priv` also on
     every rescue.

   The budget holds either by porting `container/`'s two large scripts to
   Python after step 5 or by raising it, and the helpers are budgeted apart
   and frozen at their size. Both are listed under decisions. Everything
   else in `image/`, `bench/`, `vm/` and `remote/` is Python once step 5 is
   done: the in-target builder halves (5.19, 5.20), the guest provisioning
   (5.34), the build-box probe and provision (5.6) and the bench autorun
   (5.31).

   Decisions for the user this step adds: `session` into `quiesce` (5.9);
   deleting `boot/pi-mbr.sh` (5.7, already listed); the on-target budget
   above.

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
| Every mutating command, killed after any effect and re-run, converges on the declared final state (`new`, `rm`, `build`, `test`, `bench`, `gc`, `vm base`, `machine setup/rm`, `key`, `skills`, `backup`, `quiesce`, `session`, `boot`, `sysimage`, `./setup`) | 1 (helper), then each command's step | `unit killpoints[<cmd>]`, `live killpoints[setup]` |
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
| A scratch store never puts two targets on one directory, and one machine's task records live in one directory | 1 | `unit record.one_store_per_target` |
| Every mutating command honours `--dry-run` as the recorder: the plan and the run cannot differ, and a dry run fetches nothing | 1 | `unit dispatch.dry_run_is_the_recorder[<cmd>]` |
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
| A workspace on a peer is created there by hand (refused here, naming the command) and removed from here; `wk rm --all` asks once for the whole fleet and routes each removal | 3 | `live rm.peer[<machine>]` |
| `wk rm` leaves nothing on any target: no container, guest or checkout, no ws dir, no registry entry, no `Host wk-<name>` alias, no `.unfiltered`; the registry entry outlives the artifacts, never the reverse | 3 | `unit rm.final_state[<target>]` |
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
| A build box holds no deploy key at rest: an explicit push forwards the workstation's wk agent, an agent session forwards none, `wk push status --target <box>` says so, and a push from a remote workspace succeeds | 4 | `unit push.remote_forwarding`, `live push.from_remote` |
| `wk key backup` then `./setup` round-trips with no spurious change; the junk filters strip what they claim; a write is whole or unchanged; one path with a per-platform adapter | 4 | `unit backup.filters`, `live backup.roundtrip` |
| Skills sync (status, diff, pull, push) refuses over uncommitted edits, and every skill is followable from inside a container and a guest | 4 | `unit key.skills_refuse_dirty`, `live ai.skills_workspace_true` |
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
| `wk machine setup <board>` takes a board from a blank card to an answering tailnet name in `pi-hosts` and back, `wk machine probe` finds it when on, and a workspace reaches only that address, on port 22 | 5 | `live pi.setup[<board>]` |
| `wk machine setup <box>` leaves a build box in one shape (zsh, or a named warning), and a cleanup accepted at the prompt is removed | 5 | `live machine_cmd.setup[<box>]` |
| Two machines sharing one home each resolve their own target by hostname with no ssh, provisioning one never clobbers the other, and builds key dirs and locks per machine | 5 | `unit machine_cmd.shared_home` |
| A board is reached by tailnet name alone: no MAC, `.local`, address stanza, `HostKeyAlias` or ProxyJump remains once both boards join by image, and the bench install is reached at its own name | 5 | `lint.no_addresses` |
| The Librem 5 runs the pmOS bridge role in front of moose's BMC, with the BMC's own config in a conf file | 5 | `live bridge.setup[moose-bmc]` |
| A bridge's segment is proven: provision on the eMMC route asks which disk, a board on `lan0` gets its reserved address and a workspace reaches it, `wk bridge tailnet` approves the route and `autoApprovers` holds after a policy edit, the netwatch ladder stops at its budget, the dock holds 480 Mbit, the watchdog device exists, the camera streams, and a bridge whose segment is down reads differently from one that is off | 5 | `live bridge.segment[<bridge>]`, `unit bridge.segment_down_vs_off` |
| An image build runs from a Tart guest and the image reaches the host for writing | 5 | `live sysimage.build[vm]` |
| A task's results live in its workspace, `wk bench ls` names them wherever they are, the task restarts from where it stopped on any machine, and `wk doctor` names the results backed-up | 6 | `unit results.restart_anywhere` |
| A task's deliverables export as one archive | 6 | `unit results.export_archive` |
| A long effect (`wkdev-create`, `sdk-refresh`, `tart clone`) streams to the task log as it runs, so the silence watchdog and a followed log see it; `Machine.run` captures and prints only after it ends | 3 | `unit machine.streams_long_effects` |
| An effect run over ssh counts as an effect on the machine that drives it, so a kill point can land inside a remote flow (`Ssh.act_run` runs through `via.run` today) | 3 | `unit machine.ssh_effects_are_effects` |
| A hold names the pid that took it: the bash record (`lib/task.sh`) writes the pid after the plan, the Python one before `holds` | 3 | `unit record.hold_names_its_taker` |
| `Target.state` reads the workspace directory through the machine, not `os.path`, so the real drivers run in a Fake world and `killpoints[new]` runs over them rather than a stand-in | 3 | `unit killpoints[new]` |
| `hostname` is read in one place: the bash readers in `lib/common.sh` (`wk_host_name`), `lib/target.sh`, `lib/resources.sh`, `cmd/bench`, `cmd/key`, `cmd/sudo`, `cmd/find` and `cmd/sysimage` go with their commands' ports (`cmd/zed` is Python already) | 3, 5 | `lint.one_machine_name_reader` |
| The `--web` status page renders `armed_by`, `armed_at`, `armed_desync` and `disagree` as the text renderer does | 5 | `unit status.web_mirrors_text` |

### Decisions for the user

- README gets a section defining the `home`/`lab`/`wk`/`field`/`stock` layers, or the layering goes (5.39 deletes the `lint.layering` row; a later step can re-add it once README defines the layers).
- Taken overnight on 2026-09-25, each reversible: PyYAML stays in `lib/wk/sysimage/pmos_build.py` (netplan's own dependency on the build host; the stdlib has no YAML reader); the bridge phones' images no longer install avahi and nothing here uses mDNS; `lint.one_wifi_reader` stays owed because the Mac reads its WiFi credential from the System keychain and the pmos build host from netplan, and routing both through `admin/wk-card-priv` would widen a privileged helper; `wk machine setup <board|mbp> --dry-run` on an unreachable machine prints the plan and exits 0 rather than refusing (a wet run still refuses), so the unit tier's ssh shim does not fail dry runs.
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
- Settings audit: which non-default host settings persist (config.dconf's `why: unknown` entries; tolken's `wk key backup --candidates`).
- A per-device connectivity and perf subcommand (wifi channels, autosleep, overclock) and its boundary with the settings audit.
- Host tools: git-lfs on every host or none; pmOS builder prerequisites only on the aarch64 build host; wk-tools work in a workspace so the host `claude` goes.
- Bridge phones: auto power-on after power loss; remote power for the boards, which nothing here can switch on.
- Write-ups and upstreaming: the egress-proxy design, SDK patches 3 and 11, the cross-compile commits, the `CONFIG_NUMA_EMU` Launchpad request, `gpr`/`wk pr` and the profiling wrappers.
- The sandbox escape audit, last and on both platforms: the incident list, the allowlist and CDNs, apt and ddebs, the broker, the agent-rw mount, the injector's standing tokens, remote targets, bridges, device paths, yocto egress, the git helpers.
- Reprovision tolken: host user `jmichaud`, Remote Login on both installs, keys per install; register each build box's deploy key on GitHub.
- What each part of `$WK_STORE` is called before a second project needs a name.
