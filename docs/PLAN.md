# The re-architecture: how it lands

README.md's first half says what `wk` is. This file is the order of work
that gets the tree there, and what proves each step. It is deleted when the
last step lands.

## Where we are

Measured 2026-10-22 06:00 MST on the host, after the night's cut (`./wk selftest`).

| | |
| --- | --- |
| code | 29.8k non-blank lines of Python outside `tests/` (26.7k in the budget rows: `lib/wk`, `lib/*.py`, `cmd/`, `wk`), 7.0k of shell (what runs on a board, a bench install or in a place, the helpers and the setup stages); 25 commands, each a table and one function |
| tests | 42.4k lines, 137 modules, 3,163 unit and 94 lint tests, 166 in the live tier |
| `wk selftest` (lint then unit) | 3,257 tests, 4.2 minutes, green; the lint tier alone 9 s |
| the slowest unit test | 10.7 s (`test_crash_only`'s stage converge), under the 30 s budget the runner enforces |
| owed tests | 27 in each tier: the budget rows (`lint.budgets`), the dry runs the kill-point table found not on the recorder, `lint.no_addresses`, `lint.one_wifi_reader` |
| the Owed table | `tests/test_owed_live.py` is generated from it at import: one stub per `live`/`unit` name no test holds (89) |
| the last live run from the host | 2026-10-22, lane 8 (see the night's memory): the push rule by the caller's ancestry is served to a person (GitHub's 422) and refused to wk's own run and an agent (412) |

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
   dated measurements sit in comments. The push service is tested in nine
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
   point: what runs inside a place or on a board's first boot (the guard,
   the first-run script, the self-disarm), each small and each run by the
   live tier.
2. **A command is data.** A command is a module that declares the shape
   README's "Every command, the same way" names. The dispatcher is the only
   argv parser, the only help, the only prompt, the only exit-code table.
3. **One seam for every effect.** Running a process here or on a machine,
   copying bytes, touching a file outside the tree, sleeping: all of it goes
   through one `Machine` object. One implementation per driver, and one
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

Answered 2026-10-04: `pr.mirror_fetch` moves under `wk sync`, the mirror's one writer; the sync that rewrites the host mirror remounts its share in each running macOS guest; a push from a build box is made only from the workstation, against the box's checkout over ssh, and no key reaches a box; a build a workstation drives on a build box keeps its record on the box, handed to the box's wk.

`wk ai --dry-run` runs its reads (the agent probe and the sandbox checks), then prints the session's argv, and starts nothing.

Answered 2026-10-04 (second batch): DevIntegration step 13 closes the person's Zed, runs, and reopens nothing; guest commands run through `tart exec` (the guest agent), the one way into a guest; the mirror is its own tagged virtiofs share in a guest, mounted at boot, so a remount never touches agent-rw; a box whose wk-tools sha differs from the workstation's refuses, naming `wk sync --tools <box>`; `Host igalia.com` forwards no agent; a box push goes through the ssh-agent `wk push on` loads, not the key file; `wk gc` names a leftover `refs/wk/push/*` ref; agents are installed at `wk new`, so `wk ai` throws the push switch and starts the session (Claude's remote control on by default) and nothing more; the size budgets stand and the cut follows four rules -- upstream first, fewer cases and branches, no duplication, tests that test behaviour rather than that the code is unchanged; `MacHostSystem`/`HostRun` and wk's own claude.ai login and OAuth refresh go; one way of running commands across systems; every concept has one spelling (`experiment` stays only where nothing else says it); shell stays only where the shell is the point and the rest is Python.

Answered 2026-10-04 (third batch): `pi-mbr` is deleted; `wk selftest` is exempt from dry-run, saying why; the injector answers its own TLS/DNS failure with a standard 502 and an `X-Wk-Injector` header; README defines the home/lab/wk/field/stock layers and `lint.layering` holds them; a macOS guest gets the request broker; `wk key adopt`, `wk pr rebase` and `wk sync --tools`' reset stay unprompted; existing perf tasks and workspaces are obsolete and were removed; "target" is retired in both meanings -- a *place* is the vm or container a workspace lives in, a *driver* is what makes one (podman, tart, remote, ...), and a *build preset* names a build target tuple, arch and build system together (e.g. `yocto-2.54-debug-pgo`); "profile" stays PGO's and samply's; buildroot stays, and the 2.38 rpi4 image pins a newer rpi-firmware instead of tryboot; rpi3 runs Speedometer 3 with zram, clearing foreign processes; boards reach the benchmark server directly through a tailnet ACL grant; the boards' perf build uses the Mac's LTO (thin, then full); rr record, the sysprof JIT dump, the option-toggle A/B and the weekly report come back; a host setting persists only with a recorded reason; device connectivity and perf facts are `wk doctor <device>` rows; git-lfs is on every host; wk-tools work moves into a workspace and the host `claude` goes; the sandbox escape audit follows the cut; reprovisioning tolken and the write-ups and upstreaming are deferred; boards and bridge phones get network-switched smart plugs, declared in their confs; the `$WK_STORE` parts are named now; the fork's `main` protection is lifted for `git-sync-fork`; Bugzilla editbugs is probed with a harmless test edit; the bench macOS install is updated on purpose but need not match the host; Speedometer 3's `wakeLock` error under MiniBrowser is an upstream patch at the very end; a Pi with an orphaned root is re-provisioned, not rescued.

Answered 2026-10-05: the flag that names where a workspace lives is `--on <place|machine>`; every named build (gtk-debug, mac-release, yocto-2.54-debug-pgo) is a *build preset*; the layers section goes below README's marker; the `$WK_STORE` parts are the *store* (a place's data: `ws/`, `base/`, `cache/`), the *records* (`task/`, `log/`, locks), the *mirror*, the *snapshots* (`base/<id>`), the *keyring* (secrets, agent-rw, push keys) and the *runtime* (the broker socket); the cut deletes no feature: tests test behaviour, branches are fewer, comments earn their place; `version` and `disk` become `doctor` sections, `logs` becomes `status --log`, `session` becomes `quiesce session`, `push` becomes `key push`, `profile` becomes `run`/`test --profile`; every tombstone goes and none is added; `wk pr` stays as it is; Claude commits the green tree (never pushes), syncs the boxes' wk-tools, rebuilds the macOS guest base and runs every lane.

Answered 2026-10-05 (second batch): rotation allows one holder of the claude.ai credential, so the host-side injector holds and refreshes it and adds the bearer token to Anthropic and claude.ai requests, Remote Control's included, and no workspace holds it; `wk machine setup` installs a pinned node into a build box user's `~/.local` so pi installs there; the size budgets are targets, met by cutting under the four rules; Claude pushed `python-core` once (fast-forward) so moose could sync.

Answered 2026-10-05 (third batch): Bugzilla editbugs is probed through the comment-tag API, after one live check that bugs.webkit.org gates it on editbugs; boards reach the benchmark server through a `tag:wk -> tag:wk` grant; smart plugs wait for the hardware to be chosen; the images `wk sysimage` builds are *image presets*, separating the base image from WebKit's build presets.

Answered 2026-10-05 (fourth batch): wk-tools work happens in a workspace made by `wk new <ws> --repo wk-tools`, whose checkout is wk-tools instead of WebKit and where `./wk selftest` runs, while live lanes stay driven from the host; the host `claude` goes once that works.

Answered 2026-10-05 (fifth batch): the user's Bugzilla account has no editbugs, so the comment-tag probe's "cannot tag comments" is right for it; boards keep the ssh -R tunnel to the page server until upstream run-benchmark takes a bind address (an upstream patch), after which a run from a `tag:wk` machine hands boards the server's tailnet address.

Answered 2026-10-18: a publish from a container is served by the caller's ancestry (a person's own run, no agent session between), and wk-tools is the one repository an agent commits and pushes; `kernel.yama.ptrace_scope` stays as the machine ships it (0 on the podman machine), since an agent attaching to a person's shell is a hole the sandbox need not close.

Answered 2026-10-21: the size budgets stay (core 10k, tests 15k) and are met by architecting around the four workflows (the workspace loop, agent sessions, board bench, Mac bench), each feature well defined and nothing over-engineered; the bridge phones and the pmOS builder are deleted; the forks and git identity stay hard-coded in `lib/wk/project.json` and `dotfiles/gitconfig` (one user); what has no user yet lives in `docs/future/`, which no agent reads, with the by-hand items (Tailscale console, the stray fork ref, the memory patches, the clangd misses); `round`, `device` (a `board` is a Pi), `slot` and `warmup profile` are the spellings, held by `lint.vocabulary`; the pseudo bbappend is scarthgap head's own recipe (WebKit's yocto manifest pins an older poky), so there is no reproducer to build and the row goes, and the manifest bump is an upstream WebKit patch; Claude commits and pushes wk-tools (the never-commit rule is WebKit's); the Mac's arm is measured at the end of the night, with the return the morning's `wk boot mbp --back`.

Answered 2026-10-21 (second batch): kept in the workspace loop are `run --profile` (sampling, bytecode, samply), `run --until-crash`, `--rr`/`--replay`, `build --babysit` and `gui --lldb`/`--rr`; `key sudo` and `quiesce session` stay, `key backup`, `pr report` and `zed --tools` go; `wk status` is text, `--json` and `--wait`, its web page goes; the argument-set A/B, the subtest exclusions and the EEPROM recovery path stay, the Mac `ab-summary` goes; the rpi5 tuning tree and the desktop and shell preferences stay in core; the SDK patches move to a fork branch of webkit-container-sdk checked out at a pin; the `container/bin/` conveniences no code calls go; the key election and the `CFG_NEEDS` presets stay.

Answered 2026-10-05 (sixth batch): each repo has its own push identity (wk-tools its own deploy key); commands declare the repos they serve and the dispatcher refuses the rest; on a Mac the host-side injector holds and refreshes the claude.ai login and the podman VM's injector forwards Claude traffic to it, so a guest never needs the podman VM (the two VMs do not fit the Mac's memory envelope together); the injector adds the bearer for api.anthropic.com, claude.ai and platform.claude.com.

Answered 2026-10-22: the Mac's bench default is set by hand in a later session (`wk boot mbp --status` read `firmware_default=the host install` at 08:10 MST, so `live boot.arm[mbp]`, `bench[mbp]` and `doctor.bench_readiness[mbp]` wait for it); a plain `sudo <command>` in a container workspace runs under a root bridge of its own through a `sudo` shim in `container/bin/ws`, an entry of the build wall (measured: a sudoers `env_keep` keeps the variables but the person's bridge cannot name a root client and refuses it 403, and the SDK's own `/etc/sudoers.d/sudoers` is not mode 0440, so a post-install `visudo -c` fails; the fork's `.wkdev-init` fix is owed); the guest's three launchd jobs (nosleep, mirror, display) are `daemons.py` rows and the golden base is rebuilt in the session that lands them; `./setup --stage sdk` on Linux is owed to a session on moose (the podman VM's checkout is at `SDK_PIN`, the head of the fork's `wk` branch, measured); every bench, sysimage, secrets, places, workspace, status and guest behaviour stays except the snapshot's bookkeeping (the SNAP column, `snap_behind` and the `wk ls` footer; the overlay, the hard-linked snapshot refresh and `Snapshots.verify`, which defines the current snapshot for `wk new` and the refresh, stay), the ntfy publish and the build-box dependency findings, which go; a command the broker runs never runs `./setup` (the setup restarts the broker serving it, measured); the `cmd` row counts tables and `main` only (a command's docstring is its help, held by the help-text rule) and the bodies of `test`, `ai`, `run`, `pr`, `gui` and `stop` move into their lib modules; the budget table carries two numbers per row, the budget (10k and 15k stand) and the interim lint holds now -- the measured size after the day's cuts, lowered as each dedup pass lands and never raised; the tests are redesigned as regression tests (step 7.13), end to end through the dispatcher against the fake world, designed in this file first and rewritten module by module afterwards.

## Cutting it down

Measured 2026-10-21 (non-blank lines): 36k of Python core, 52k of tests, after
every step below 5 landed and a trimming pass. Four read-only inventories of the
tree (`scratchpad/inv-*.md` of that night) agree on where it goes: dedup alone
takes the core to about 25k, so the rest of the way is architecture -- each
feature defined by the workflow it serves, one object per concept, no second
pipeline for a second machine kind. The budgets stand and lint holds them: a
change that goes over trims something.

| | today | budget |
| --- | --- | --- |
| core (`wk`, `cmd/`, `lib/`) | 36k Python | 10k Python |
| what runs on a board, a bench install or in a place | 1.1k bash | 1k bash |
| the host setup stages (`setup`, `host/`, `admin/`) | 5.5k bash | 3k bash |
| tests | 52k | 15k |
| commands | 25 | 20 |

**The four workflows**, and nothing else is architected around:

1. *The workspace loop*: `new`, `build`, `run` (with `--profile` sampling,
   bytecode and samply, `--until-crash`, `--rr`/`--replay`), `test`, `enter`,
   `zed`, `scp`, `sync`, `pr`, `rm`, `gui` (with `--lldb`/`--rr`), `build
   --babysit`, on a container, a macOS guest or a build box; the PR workflow
   through the push service.
2. *Agent sessions*: `ai` with the wall, the credential injector, `key` (setup,
   deploy, sudo, push) and the push rule by the caller's ancestry.
3. *Board bench*: `sysimage` (buildroot, yocto, write, disks), `boot`, `bench
   deploy|run|ab|report` with arms of (system, slot, args), PGO, the subtest
   exclusions, the EEPROM recovery path.
4. *Mac bench*: the bench volume as a sysimage builder, stage, `bench run
   --system mbp`, the Mac A/B through the plant and the autorun, `quiesce`
   (and `quiesce session` for the Linux seat), zero-touch nights.

Gone, by the answers of 2026-10-21: the bridge phones and the pmOS builder,
the `fetch` builder, `wk status`'s web page, `key backup`, `pr report`, `zed
--tools`, the Mac on-volume `ab-summary` (the volume holds no task; `--collect` prints the task report here),
the workspace conveniences in `container/bin/` that no code calls (`git-clean`,
`git-sync-fork`, `commit-count`, `strip-addresses`, `plot-memory-log`, the
memory patches), and the SDK patch script (`container/sdk-patches/`): the
edits live on a fork branch of webkit-container-sdk that `./setup` checks out
at a pin. Kept, by the same answers: the rpi5 workstation tuning tree, the
desktop and shell preferences in `host/`, the key election, the nine
`CFG_NEEDS` presets.

**One object per concept.** Each is a contract a conformance test runs over
every implementation; nothing outside the contract is called from a command.

- *A command is a table.* `cmd/<name>` is a dict (name, where, takes, opts,
  subverbs, destructive, dryrun, repos) and one function. The dispatcher
  builds argparse from the table, prints the help from the table and the
  docstring, prompts, forwards and sets the exit code. No `# wk:` DSL, no
  per-command `sys.path`/`noqa` bootstrap, no `d.post` hooks: a command that
  needs an after-step calls it.
- *`Machine`*: `Local` and `Ssh`; the podman machine and `tart exec` are
  `Ssh` argv variants, not classes. `Fake`, `FakeClock`, `Exited` and `Killed`
  live in `tests/fakes.py`; nothing in `lib/` names them.
- *`Driver`*: one contract of about twenty methods -- list, info, state,
  create, destroy, start, stop, exec, exec_argv, exec_tty, copy in and out,
  src, home, store, ssh_endpoint, build_argv, ccache_dir, build_size, sync,
  install_agents -- over `Container`, `Vm`, `Remote` and `LocalWorkspace`.
  A method one driver has and another lacks is not in the contract and not
  called from a command. One readiness wait, one `remake_hint`, every podman
  machine call through the one `Ssh`.
- *The record*: `Task` with `holds=`; a build budget, a device claim and a
  workspace lock are holds on it (`resources.Budget` and `job.busy_reason`
  fold in). One wait, one liveness question, one kill.
- *The fleet*: `fleet.each(argv)` is the one "ask every place's own wk and
  merge" (today in `dispatch.bare_report`, `cmd/ls`, `cmd/stop`,
  `fleetwalk`, `status.Walk`, `record.fleet_stores`, `sudo`).
- *Diagnostics*: `doctor` owns every "is X healthy" row (the guest desktop
  findings, `status.Walk.health`, the machine-local table); `status` reports
  records and renders them once, as text, `--json` and `--wait`.
- *Credentials*: one delivery (today `guest.write_agent_secrets`, the firstrun
  symlinks and `machine_cmd.build.credentials`), one verdict line per
  credential, `credcheck` imported, never spawned.
- *The bench `System`*: `boot`, `deploy`, `run`, `collect`, `after`, with
  `env_fields` and `home`, over container, guest, board and the Mac volume;
  one `Run` (no `BoardRun`/`StagedRun` overrides, no `run_class` switch); one
  `AB` whose arm is (system, slot, args) with one rounds engine
  (counterbalance, lost-round limit, the stopping rule asked in process) over
  a board, the fleet and the Mac; one report behind one entry point; one
  profiler attach; the autorun is that `Run` on the bench install, planted.
  The Mac is a system, not a special case: `stage` is `deploy`, `staged` is
  `run --system mbp`.
- *Boot*: one `Channel` with methods (no `call(fn_name)` dispatch), a
  per-driver `arm_sequence` the board A/B calls instead of its own; `Boot`
  has no `arming` ladder.
- *Images*: builders are data and stages over one `task.Stage` lifecycle;
  an image is found by `Builder.outputs`, never catalogued.
- *Daemons*: one supervisor renders a systemd unit or a launchd plist from a
  (program, env, socket) row for the proxy, the push service, the injector
  and the broker, on the host, in the podman machine and in a guest.
- *Shell stays where the shell is the point*: `boot/onboard/`,
  `bench/mac-bench-firstboot.sh`, `build/guard.sh`, the three helpers and the
  setup stages. Shell inside Python strings (530 lines today) becomes argv
  lists or a file under those directories.

**Tests, once per rule.** `tests/fakes.py` holds the one fake world (the
twenty-seven `World` classes and six act recorders fold into it); one
table-driven module holds kill points, dry run and final state for every
mutating command (today `converges(` in 40 modules); one conformance mixin per
contract (Machine, Driver, System, Channel, Builder); one module per subsystem
(the push service in one, the credential store in one, the dispatcher in
three); no test asserts prose, README text, comment counts or line counts
except the lint rules, which are one module with a rule table. `test_owed_live`
is generated from the Owed table.

**Budgets per package** (Python non-blank). The budget is the goal each row is owed; the interim is what lint holds now (measured 2026-10-22 after the day's cuts, in `tests/test_lint_budgets.py`), lowered as a dedup pass lands and never raised; a `cmd/*` file counts without its docstring, which is its help:

| package | today | budget | interim | tests today | tests budget | tests interim |
| --- | --- | --- | --- | --- | --- | --- |
| dispatcher: `dispatch`, `decl`, `act`, `completion`, `wk` | 1,230 | 800 | 773 | 1,734 | 900 | 932 |
| `machine` | 773 | 400 | 491 | 554 | 500 | 1,786 |
| `places` and the drivers | 1,816 | 950 | 1,786 | 1,135 | 1,000 | 4,020 |
| record, job, lock, resources, clock | 1,174 | 550 | 1,242 | 1,500 | 700 | 2,700 |
| workspace, build, sync, pr, git, presets, store, tools, agents | 2,350 | 1,150 | 3,010 | 6,001 | 1,800 | 5,748 |
| wall, pushgate, secrets, key, credcheck, ai, sudo | 3,000 | 1,150 | 2,600 | 5,400 | 2,000 | 4,724 |
| status, doctor, gc, ls, fleet, reach | 2,100 | 700 | 1,703 | 1,600 | 900 | 2,635 |
| bench, pgo, sched, scores | 7,300 | 1,300 | 6,319 | 9,500 | 2,200 | 6,790 |
| boot | 1,433 | 650 | 1,261 | 3,000 | 1,100 | 2,243 |
| sysimage, images, slot | 3,900 | 950 | 3,756 | 4,300 | 1,400 | 3,060 |
| guest, mac, quiet, session, screen, notify, tailnet, machine_cmd | 2,600 | 900 | 2,221 | 3,300 | 1,000 | 3,514 |
| `cmd/*` | 2,763 | 500 | 951 | -- | -- | -- |
| lint rules | -- | -- | -- | 1,300 | 700 | 2,070 |
| live bodies | -- | -- | -- | 2,000 | 800 | 151 |
| **sum** | **~30k + 2.8k cmd** | **10,000** | **26,113** | **~41k** | **15,000** | **42,178** |

## Testing, concretely

- **The fake machine** holds files, a process table and a clock in memory,
  answers as told (this ssh says X, that podman says Y), and records every
  effect in order.
- **Kill points.** One helper runs a mutating command against the fake,
  stops it after effect *n*, runs it again, and asserts the final state is
  the same for every *n*. Every mutating command has this test, which is
  what "crash-only" means here.
- **Driver conformance.** One test class runs over every driver and
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
container place. A bash command and its Python replacement never both
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
   (`lib/wk/places.py`): container, guest and workspace-local whole, and
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
     `lib/wk/places.py`; `tests/test_wk_workspace.py` holds every refusal,
     `killpoints[new]`, `killpoints[rm]` and dry-run-equals-wet-run.
   - *`enter`/`scp`/`zed`.* All three are Python entry points over
     `lib/wk/places.py`: `enter` runs a command through `Driver.exec` or
     execs into `Driver.enter_argv`'s shell; `scp` moves bytes through
     `pull`/`push`/`pull_dir`/`push_dir`/`path_kind` -- `podman cp` for a
     container, the one `Machine` copy (`copy_in`/`copy_out`/`copy_tree_in`/
     `copy_tree_out` on `Local`/`Ssh`/`Fake`) for a guest or a build machine;
     `zed` reaches a workspace through `ssh_host`/`ssh_prepare`/`ssh_user`/
     `ssh_proxy`, one hop further for a peer's own `--route`; `cmd/profile`
     calls `Driver.pull_dir` directly.
     `tests/test_enter.py`, `tests/test_scp.py` and `tests/test_zed.py` hold
     the refusals; `tests/test_wk_places.py` holds each driver's argv;
     `tests/test_wk_machine.py` holds the copy conformance. Live:
     `test_dev_integration`'s steps 3 and 5 run `wk enter <ws> -- <cmd>` in a
     container, a guest and a peer's container and assert the command's
     output. Owed: `enter.shell` (the interactive shell) and `zed.peer`.
   - *`pr`.* `cmd/pr` is a Python entry point: `rebase` and `open` run over
     `Driver.exec`/`src`/`mirror_dir` (on the base `Driver` and on
     `LocalWorkspace`), and `pr_open_target`/
     `pr_open_gh_args` are module-level functions `tests/test_pr_workflow.py`
     and `tests/test_pr_upstream.py` import directly rather than lifting bash.
     The plain `wk pr <ws> <spec>` checkout is `pr.checkout`, over
     `Driver.exec`/`act_exec`; the mirror fetch a pull request or a fork's
     branch resolves through (`sync.fetch_into_mirror`/`fetch_pull_into_mirror`, the mirror's one writer) runs only
     inside `pr.resolved_or_planned`, the one dry-run recorder `wk bench ab`
     shares, so a dry run never reaches git. `killpoints[pr]` is in `tests/test_pr_workflow.py`.
   - *`verify`.* Merged into `wk doctor <workspace>` (and `wk doctor` inside
     one, the half `wk ai claude` runs there): the checks are
     `lib/wk/wall.py`, run at once, `tests/test_doctor_wall.py` holds each;
     `wk verify` is a tombstone. Live: `test_dev_integration`'s
     `test_11_push_off_reaches_nothing` runs `wk doctor <ws>` on a container
     and a guest and asserts it exits 0, which `cmd/doctor` returns only for
     an intact wall.
   - *`ai`.* `cmd/ai` is a Python entry point: the wall's checks are
     `lib/wk/wall.py`'s own (`from_host`, `from_inside`, `commit_walled`,
     `commit_wall_prefix`), and each driver's `exec_argv` is in
     `lib/wk/places.py`. `tests/test_ai.py` holds the flow,
     `ai.verifies_wall` and the session's `--remote-control <ws>`; the
     agents are installed at `wk new` (`lib/wk/agents.py`, `container/agents.sh`,
     `tests/test_agents.py`), and step 2 asserts a made workspace's Claude CLI runs. Live:
     `test_dev_integration`'s `test_04_claude_starts_after_the_sandbox_check`
     starts `wk ai claude` on a container, a guest and a peer's container and
     asserts the wall check printed `sandbox intact`; `test_05_a_push_is_served_to_a_person_alone`
     asserts a person's push is served beside a running claude session and the session's own, wk's
     own run's and a daemon the session left behind are refused (a guest: refused while claude runs). Owed: the live
     checks `ai.walled_session` (the stopped-proxy refusal, a tool refused
     the network), `ai.commit_wall` (git's own error and the rule) and `ai.remote_control`, and
     the unit and live halves of the push rule since it became the caller's ancestry (`unit push_service`,
     `test_05`, `ToolsWorkspaceLive`) have not run on the host.
   - *`sync`.* `cmd/sync` is a Python entry point (its parse and `--where`)
     over `lib/wk/sync.py`; each driver's furniture is `Driver.sync` in
     `lib/wk/places.py`, and `wk remotes` is a tombstone for `wk sync
     [<ws>] --fix`, the wiring read back in every fetch.
     `tests/test_sync.py` holds `sync.scopes`, the wiring report and fix,
     `killpoints[sync]`, `dispatch.where[sync]` and dry-run-equals-wet-run.
     Live: `test_dev_integration`'s
     `test_12_sync_leaves_every_remote_current` runs `wk sync <ws>` and
     asserts every remote current. Owed: `sync.fleet` (bare, `--all`,
     `--tools`, `WK_MIRROR_BRANCHES`); `status.py` calls
     `Driver.workspaces`, and the one remaining inline copy of its union is
     `cmd/ls`.
   - *`build`.* `cmd/build` is a Python entry point over `lib/wk/build.py`
     (front, detached run, `--detach`, `--kill`, the babysitter), `lib/wk/job.py`
     (the watched run, the announced pid, the one job stop) and
     `lib/wk/presets.py` (the presets as data, the cross presets
     included); each driver's `ccache_dir`, `build_argv` and
     `build_size` are in `lib/wk/places.py`, the budget in
     `lib/wk/resources.py`'s `Budget`. `tests/test_wk_build.py` holds every
     refusal, `killpoints[build]`, `progress_shape[build]` and
     dry-run-equals-wet-run; `tests/test_presets.py` the presets. Owed:
     the live checks (`build.preset[<preset>]`, `build.babysit_e2e`). Every
     caller resolves a config through `presets.resolve`.
   - *`run`/`gui`.* Both are Python entry points that resolve a config
     straight through `lib/wk/presets.py`'s `Config` (no `config_load`
     call at all) and exec into `Driver.exec_argv`'s result with
     `os.execvp`, the same replace-this-process pattern as `enter`/`zed` --
     every branch of both is a tail call into the place, so neither reads
     a `Result` or an exit status. `Registry.default_config` (from the
     workspace's own `build` task record, `lib/wk/record.py`) is the default
     config for both, and `Driver.lldb_opts` carries Container's two `-O`
     flags. `wk gui` refuses a `kind == "remote"` place (`unit
     gui.refuses_remote`).
     `tests/test_run_until_crash.py` passes unmodified against the port;
     `tests/test_wk_run.py` holds `run.finds_binary[<port>]` and `--lldb`'s
     tty request on every place, `tests/test_wk_gui.py` holds
     `gui.refuses_remote`, the jsc-only/no-browser/macOS-container
     refusals and the fullscreen-flag table. Live:
     `tests.test_session.TestOnMoose.test_modes_moose` reads `wk session
     status` on moose. Owed: `run.lldb_tty`, and `session.modes[moose]`'s
     driving of each mode from each half-state.
   - *`test`/`profile`.* Both are Python entry points. `cmd/test` runs the
     JSC and layout suites over `lib/wk/job.py` (`watch`, `PidWatch`,
     `kill`, `stop`) and `lib/wk/resources.py`'s `Budget`, its record
     wrapped through `build.records_of` for the same `WK_ABORT_SECONDS`
     default a build's carries; `cmd/profile` is argv/refusal construction
     and direct `Driver.exec`/`exec_tty` calls with no task record at all.
     Both resolve a config straight
     through `lib/wk/presets.py`. The one new
     primitive either needed: `Machine.run_tty`/`Driver.exec_tty`,
     blocking with this process's own stdio inherited (a real pty for
     lldb/samply/xctrace) but returning control here afterward, unlike
     `exec_argv`'s `os.execvp` replace -- `enter`/`run`/`gui` still use
     that replace where nothing follows. `Registry.default_config` (`lib/wk/
     places.py`) is `run`/`gui`'s and `test`/`profile`'s one caller.
     `tests/test_wk_test.py` holds `progress_shape[test]` and
     `killpoints[test]`; `tests/test_wk_profile.py` the host-side
     `perf_event_paranoid` gate and `--fetch`; `tests/test_layout_paths.py`
     and `tests/test_profile_debug.py` exercise the ported behaviour
     directly; `presets.ARCH`/`arch_label` name the arch. Owed: the live checks (`test.suite[<place>]`,
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
     (`cmd/doctor`, `cmd/ai`) and `wall.could_publish(results)` the one
     "an agent could publish" decision.
     Also fixed: `is_linux`/`is_macos` live once in `lib/wk/machine.py`,
     the session socket in `places.session_socket_present`, the loader-path
     prelude in `lib/wk/ldpath.py`, the Zed CLI in `places.zed_cli`, and
     `lib/wk/sshalias.py` is the
     one alias writer and `lib/wk/store.py`'s `cache_dir` the one artifact
     path. "Is this pid alive in the place" is one answer,
     `Driver.pid_alive` (`lib/wk/places.py`), which `record.of_driver`,
     `workspace.py`, `cmd/stop` and `cmd/status` all ask.
4. **Credentials.** `key`, `push`, `sudo`, `backup`, `skills`.
   - *Landed.* `cmd/key` (`lib/wk/key/`: `cli.Key`, one mixin per concern -- `creds`, `deploy`,
     `election`, `check` -- over `common`'s verdict line, `GitHub` seam and `Fleet`) and `cmd/push` are Python
     over `lib/wk/secrets.py`'s `Secrets`. `wk sudo` is `wk key sudo` (`lib/wk/sudo.py`), the old names tombstones; `skills` is a tombstone.
     A destructive command whose destructive part is conditional calls
     `act.confirm` or `act.nothing_to_ask`, and `act.asked` is the one
     "acted before asking" check. Every Python command reads its options
     through `wk.decl.Args`. `tests/test_wk_key.py`, `test_wk_secrets.py`,
     `test_push_service.py` and `test_wk_sudo.py` hold the
     flows, `killpoints[key]`, `killpoints[push]` and dry-run-equals-wet-run.
     The fork and agent-secret tables are `secrets.FORKS` and
     `secrets.AGENT_SECRETS`, the one copy, and `container/firstrun.sh` asks
     `python3 -m wk.secrets` directly. `wk doctor`'s credential rows are
     `Key`'s own (`stored_verdict`).
     `Secrets.agent_secret_remedy` (`lib/wk/secrets.py`) is the one remedy,
     over `cred_stored`/`cred_verdict` and `lib/credcheck.py`;
     `tests/test_wk_secrets.py`'s `TestAStoredCredentialIsReadTheOneWay`
     holds the read against a `Fake` refusing as `lib/secretfile.py` would.
     `-h` prints each subverb's destructive override under the command's line.
   - *Owed.* The live checks (`key.election[<peer>]`, `push.from_box`,
     `sudo.require[<machine>]`).
5. **Fleet and bench.** `sysimage`, `boot`, `pi`, `bench`, `ab`, `quiesce`,
   `session`, `bridge`, `vm`, `find`, `remote`, `gc`, `completion`, as the one
   pipeline over one `machines/` directory. Done when `lib/common.sh` is the
   one bash library, every command is Python, and `bench/`, `boot/`,
   `image/`, `bridge/`, `vm/` and `remote/` hold only what runs on a board, a
   phone, a bench install or in a place.

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

   **Driver shape.**

   | command | subverbs | replaces |
   | --- | --- | --- |
   | `machine` | `setup`, `rm`, `ls`, `probe` | `remote`, `find` (`probe` with no name sweeps), `pi setup`, `bridge` (every verb), `vm` (the lifecycle verbs become `new`/`start`/`stop`/`enter`/`sync`/`rm --on vm`, `check` becomes `doctor <ws>`) |
   | `bench` | `deploy`, `run`, `ab`, `ls`, `report` (`compare` and `precision` are `report` over two runs) | `pi deploy`, `pi bench`, `bench <ws> <plan>`, `bench stage`/`staged`/`mac`/`mac-ab`/`seed`, `ab` |
   | `boot` | `<machine>` with `--status`, `--keep`, `--back`, `--disarm`, `--diag`, `--system`, `--boot-order` | `boot`, `pi boot-order`, `pi helper` (moves to `machine setup <board>`), `boot --prepare` (moves to `machine setup mbp`) |
   | `sysimage` | `build`, `write`, `ls`, `disks`, `rm` | `sysimage`, `image/*`, `vm base` (the guest base is a builder), `bench mac-volume` (the bench volume is a builder) |
   | `quiesce` | `on [--seat=kiosk\|desktop] [--bmc]`, `off`, `status` | `quiesce`, `session` (see 5.9) |
   | `gc` | kept, in Python | every kind of rubble is named by the module that makes it |
   | `completion` | generated by the dispatcher from the declarations | the `completion` command |
   | `notify` | a tombstone; `lib/wk/notify.py` is the library call | the bash `wk_notify` and the notify CLI |

   Commands after step 5: `new rm build run test enter scp sync pr ai zed gui
   profile status ls logs stop start doctor key machine bench boot
   sysimage quiesce gc selftest`. That is 27 against the budget of 20.
   `logs`/`status` and `start`/`stop` are the obvious next merges, and they
   are not in this step.

   **One `machines/` directory.** `machines/<name>.conf` holds one machine,
   named by the name the CLI takes. `KIND` is one of `build`, `peer`,
   `board`, `mac`, `guest` or `bridge`. The other keys are today's, unchanged
   (`NODE_*`, `WK_REMOTE_*`, `WK_DRIVER`, `BR_*`), so the phone-side
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
     `named_by_host`) instead of the remote marker's old machine line, which
     `remote/provision.sh` no longer writes (`root=`/`inputs=` stay: `stale`
     still reads `inputs=`); `places.py`'s `Remote.is_local` follows suit,
     since it was the marker's other reader of that line. README's spec half
     names `machines/` alone. Owed: none.
   - Closes: `unit machine_cmd.shared_home`, meaning each machine resolves
     its own place by hostname with no ssh.
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
     (`Driver.state`/`wait_ready` through the machine, `lib/wk/tools.py` the one
     push, `Registry.local_workspaces()`; `doctor.gh_authenticated` asks
     `gh` directly; `Driver.wk_cmd` builds every far wk's command line, the
     podman-machine hop's and a machine's alike; `delegate_run` execs `ssh`
     over the `Remote`'s own machine; `dispatch.json_merge_list` is plain
     `json`). Owed: none.
   - Closes: `unit killpoints[new]` over the real drivers,
     `unit machine.dead_creation_refused_at_once`.
   - Decision: one push for every kind. The guest gets the same bundle; its
     `_push_tools` goes.

   5.4 **The store, the mirror and the PR fetch.** *Landed* (`lib/wk/git.py`,
     `lib/wk/pr.py`, `store.Snapshots`). `places.py`, `build.py` and `doctor.py`
     call `wk.git` directly and `cmd/ls` uses `store.Snapshots`.
     `tests/test_pr_workflow.py`
     holds `killpoints[pr]` (checkout onto a fork's branch, `pr_rebase`'s
     fetch and rebase, and `pr_open`'s push and `gh pr create`, each killed
     after any effect and rerun converging; the box push `push_from_here`'s fetch, push and temporary-ref delete likewise) and dry-run-equals-wet-run for
     the plain checkout; `cmd/pr` declares `dryrun` for that form only --
     `rebase` and `open` mutate through plain `Driver.exec`, not `act_exec`,
     so their own sub declarations turn it back off. Owed: none.
   - Closes: `unit new.refuses_without_mirror`, `unit
     cli.refspecs` (the owed module).
   - Decision: the wiring runs as git argv lists through `Driver.exec`, not
     as script text sent into the place.

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
     place), and `FakeBoard` is that Channel over two fake `Machine`s.
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
     one byte formatter; `cmd/sysimage`'s table says `outside`, so
     the dispatcher refuses `wk sysimage` inside any workspace.
     `ls` also lists the mac-volume and guest builders (5.32, 5.34), through
     `ls.host_presets`/`ls.builder_outputs` -- the one `builder_outputs`
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
     `buildroot.py`, and `buildroot_ws.py`, the in-workspace half, run as `python3
     /opt/wk-tools/lib/wk/sysimage/buildroot_ws.py image|webkit` under `task.stage_main`; it folds
     in the tailnet and wifi overlays, and `buildroot.py`'s `kernel_pin` replaces `kernel-pin.sh` on the
     driving machine). What buildroot itself executes stays shell and counts in the in-place budget: the
     post-image hook `buildroot_ws.py` writes for a pinned kernel, and the overlay init scripts
     (`image/buildroot/overlay/etc/init.d/`); the memory guard stays `build/guard.sh`, reached as one
     `bash -c` line. `wk build`'s far argv goes through `task.in_workspace`, closing `lint.build_wall`.
     Nothing of the in-workspace half has run in a workspace yet. Owed: a live image and slot build
     (hours, past the runner's per-test budget).
   - Closes: `unit sysimage.task_states`, `unit killpoints[sysimage build]`,
     `lint.build_wall`, `unit record.progress_shape[sysimage]`.
   - Decision: one detach and one watchdog, `build.py`'s.

   5.18 **yocto, host half.** *Landed* (`lib/wk/sysimage/yocto.py`, a `task.Stage` with
     no deadline on its record; `job.watch_pid` gives up on silence only when told to, and on a
     wedge of `WEDGE_BEATS` heartbeats naming one bitbake task). The cross presets live here; a
     yocto stage refuses a held workspace lock instead of waiting an hour. `Yocto` and `Buildroot` are `task.ContainerBuilder`s: the driver refusal, the
     digest-tagged host image and the workspace it makes are one implementation, and each subclass
     is its data and its stages. Owed: `live sysimage.sstate_reuse` has no body -- a second image build is minutes to hours, past the
     runner's per-test budget; `WEDGE_BEATS` (4 h) is a guess no wedged run has been measured
     against; `WK_YOCTO_BASE` is not in `wk sysimage -h`.
   - Closes: marks live `sysimage.sstate_reuse`.
   - Decision: "silent" and "wedged" are `job.stall_report` verdicts over the
     same heartbeat, not a second yocto rule.

   5.19 **yocto, in-workspace half.** *Landed* (`lib/wk/sysimage/yocto_ws.py`, with
     port-target folded in; the memory guard stays `build/guard.sh`, reached as one `bash -c`
     line). Nothing of it has run in a workspace yet. Owed: WebKit's `Tools/yocto/rpi/manifest.xml`
     pins a poky whose pseudo (1.9.3) fails under GNU tar 1.35; `image/yocto/meta-wk`'s bbappend is
     scarthgap head's own recipe (1.9.11) until an upstream patch moves the pin.
   - Decision: port it; the container has python3. Bitbake is reached as
     `bash -c '. oe-init-build-env && bitbake …'`, one line.

   5.20 **pmos builder.** *Deleted* with the bridge phones (answered 2026-10-21).

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
     profile: `--preset` is one phase as one yocto stage, no `--preset` the whole graph under one `pgo` record in this
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
     spend that machine's time on, left to whoever runs the live tier next; a real run ends in bench
     mode, and the way back is the person's power-button pick.
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
     two build phases (they are build-in-workspace.sh's `_xc_settings`/`guard_run`) and asks `PgoCollect` for the
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
     `report.task_report`. The
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
     phase, each refusal, the drift refusal after the quiesce, the settle, the hold, the watchdog, the power-off, a run
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

   **Zero-touch Mac nights.** Decided 2026-10-22: the Mac is a bench machine by default, and the desktop is the
   exception.
   - `WK Bench` is the firmware default, set once by hand in System Settings > General > Startup Disk with the
     owner's password, and wk blesses nothing after that. The arm was measured (2026-10-22 07:07 MST, `wk boot mbp`
     on tolken): root's `bless --mount "/Volumes/WK Bench" --setBoot` asked for the owner's password with nothing to
     answer it, exited 0, and the Mac came up in the host install -- root's bless does not take on Apple Silicon,
     which is why wk does not bless.
   - Entering bench mode is a plain restart (`wk boot mbp`, or the A/B's own), and the arm refuses while the default
     is not the bench volume. A boot that ran a job holds the install up for `WK_MAC_BENCH_HOLD` seconds, then powers
     it off; a boot with no job stays at its login window.
   - Leaving is the person's: at startup hold the power button and pick the host install (the firmware boots that
     pick once), or `sudo bless` by hand. `wk boot mbp --back` refuses naming that, and `wk bench ab --devices mbp
     --collect` reads the result from the host install.
   - SIP is per install on Apple Silicon: `csrutil` in recoveryOS acts on the selected volume, so the
     bench volume runs with it off (the six `unstoppable` daemons in `bench/quiet/macos.tsv` then stop)
     while the host keeps it on. It is set once at install, by hand, like the tailscale key; `wk doctor
     mbp` reads both (`unit doctor.sip_rows`).
   Rows: `live boot.arm[mbp]`, `live doctor.bench_readiness[mbp]`, `live bench[mbp]`.

   *Group 4, ∥ Group 3*

   5.33 **Guest start and stop, and the host daemons.** *Landed* (`lib/wk/guest.py`:
     `Host` and `Guest`; `Vm.write_marker` is the one marker writer). Owed: none.
   - Closes: `unit record.one_lock_per_resource[guest start]`; marks live
     `vm.egress[<c>]`, `vm.shared_mirror`.
   - Decision: the proxy, inject and agent daemons are `Machine.spawn`ed
     with pidfiles, which are locks, not state. The same code serves every
     guest.

   5.34 **Guest create and destroy, and the base as a builder.** *Landed* (`lib/wk/sysimage/guestbase.py`, the `guest` builder of
     `image/presets/macos-guest-base.conf`; every converge step, the admission, the
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

   5.35-5.37 **The bridge phones.** *Deleted* (answered 2026-10-21): `machines/` holds no `bridge` kind, and moose's BMC is in `docs/future/`.

   5.38 **`wk gc` in Python.** *Landed* (`cmd/gc` over `lib/wk/gc.py`; the row
     shape is `lib/wk/rubble.py`, and `rubble()` is appended to `store.py`,
     `workspace.py`, `bench/seed.py`, `bench/mac.py`, `sysimage/pmos.py` and
     `sysimage/guestbase.py`; the podman images, ccache, runner trees, build
     outputs, board slots and remote mirrors are `gc.py`'s own). One question
     covers every row, the VM's half included (`wk gc --rows` there), and
     `wk disk` renders the same rows. Owed: the VM's half
     runs through `Container.wk`, not an effect, so no kill point lands inside
     it; half-made workspaces are found only on places whose store this
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
     (`python3 -m wk.places store-init`) and the injector's read token (`python3 -m wk.secrets
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
     The tart locator is `wk.places tart` (`tart_path`), asked through `wk_py` by every shell
     reader and directly by `Vm.tart` in Python; the privileged-helper table is
     `lib/wk/priv.py`, asked by `doctor.Host` and, through `wk_py`, by `./setup` and `admin/install.sh`.
     `machines/*.conf` keys are lowercase (`fleet.parse_text` refuses an old uppercase one, naming its
     new spelling), and a build machine's conf reaches its `WK_*` variables through the one table
     `places.CONF_ENV` (one build preset's own `cmake_<preset>`/`build_args_<preset>` included).
   - Closes: `lint.one_machine_name_reader`, `lint.vocabulary`, the `hostname`
     row, and step 2's done condition.

   **What stays shell** (it runs on a board, a phone, a bench install or in a
   place, before or without python3). Lint counts it by directory:

   | where | what | today | after step 5 |
   | --- | --- | --- | --- |
   | a board's first boot | `boot/onboard/`: self-disarm, self-return watchdog, rescue marker, tryboot staging | 141 | ~150 |
   | the Mac bench install before python3 | `bench/mac-bench-firstboot.sh` | 279 | ~180 |
   | the bridge phone (busybox ash, no python3) | `bridge/init.d/*`, `bridge/bin/*` | 642 | ~600 |
   | a place | `build/guard.sh` (exec wrapper) | 54 | 54 |
   | **step-5 share** | | **1,116** | **~1,000** |

   The rest of the tree's in-place shell stays outside step 5, and neither
   part fits the 1k budget alone:
   - `container/`'s scripts are 470 lines, of which `firstrun.sh` 298 is
     portable, since a container has python3.
   - `build/build-in-workspace.sh` and `mem-watchdog.sh` are 225.
   - The three privileged helpers are 1,994, with `wk-card-priv` also on
     every rescue.

   The budget holds either by porting `container/`'s two large scripts to
   Python after step 5 or by raising it, and the helpers are budgeted apart
   and frozen at their size. Both are listed under decisions. Everything
   else in `image/`, `bench/`, `vm/` and `remote/` is Python once step 5 is
   done: the in-workspace builder halves (5.19, 5.20), the guest provisioning
   (5.34), the build-box probe and provision (5.6) and the bench autorun
   (5.31).

   Decisions for the user this step adds: `session` into `quiesce` (5.9);
   deleting the `pi-mbr` boot driver (5.7, already listed); the in-place
   budget above.

6. **Results.** A task's results live in its workspace, the task restarts
   from where it stopped, and the deliverables export as one archive
   (README, "results").

   Landed: every task lives in `ws/<name>/bench/<task>` of its workspace,
   on the machine holding it; a driver elsewhere reaches it through
   `Driver.results` (`record.ws_home`), and `record.homes` finds the tasks
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
   a place's store of its own (the podman VM's, a build box's), reads it
   through `Machine.read_tree`, which refuses a link, and builds the zip here, recording where it
   went in the task (`unit test_bench_results`, `killpoints[bench export]`).
   `wk rm` refuses a workspace holding a task no readable export holds as it
   is now, and `--force` crosses it (`unit test_rm_final_state`); `wk doctor`
   names each workspace's `bench/` as backed-up beside the store's.
7. **The cut.** The architecture above, landed package by package. Each
   sub-step ports one package to its contract, deletes what it replaces,
   moves its tests to `tests/fakes.py` and the table-driven modules, and
   leaves lint and unit green; lint holds the package's budget from the
   sub-step that lands it. ∥ marks what runs concurrently on disjoint files.

   7.1 **Fakes and the test tables.** *Landed 2026-10-22* (`tests/fakes.py`: the one `World` and `FlowTest`;
     `tests/test_killpoints.py` over `tests/killpoints.py`, one row per mutating command, each row a kill-after-any-
     effect test and a dry-run-equals-wet-run test; one conformance mixin per contract: Machine, Driver, bench
     System, boot Channel, image Builder, daemons renderer).
   7.2 ∥ **The command table and the dispatcher.** *Landed 2026-10-22* (`decl.Command`/`Opt`; every `cmd/<name>`
     is a docstring, a table and `main(args, inv)`; one importlib load per command, in process; help, completion
     and the refusals come from the table; a child forward relays signals). Rows: dispatcher 769 of 800, cmd
     2,032 of 500 (the bodies of ai, test, run, pr, gui and stop, and the docstrings that are the help).
   7.3 ∥ **`Machine` and the `Driver` contract.** *Landed 2026-10-22* (`Driver.CONTRACT`/`FIELDS` checked by
     `TestTheContract` over Container, Vm, LocalWorkspace, Remote and `Peer`; `Ssh.podman`/`Ssh.tart` are the one
     `Ssh`; one copy path; one readiness wait `wait_ready`; one git probe `git.probe`; the fakes live in
     `tests/fakes.py`). Rows: machine 491 of 400, places 1,781 of 950, workspace 2,552 of 1,150.
   7.4 ∥ **The record and the fleet.** *Landed 2026-10-22* (`record.Records.claim`/`holds=` absorb `Budget` and
     `busy_reason`; `fleet.each`/`ask`/`listing` replace six fan-outs; `Clock.wait_until` is the one wait).
     Row `record`: 1,155 of 550.
   7.5 ∥ **Diagnostics.** *Landed 2026-10-22* (`doctor` owns the daemons, the guest desktop and load rows, the
     machine-local table and `--all`; `status` reports machines, workspaces and tasks as text, `--json`, `--wait`).
     Row `status`: 1,723 of 700.
   7.6 ∥ **Credentials and the wall.** *Landed 2026-10-22* (`secrets.deliver` for containers, guests and build
     boxes; `credcheck` imported, one verdict line; `wall.measure` the one run-and-decide; `wk ai` ends its far
     session on a signal; the per-exec bridge serving only its command's tree is the push rule). Row secrets:
     2,454 of 1,150.
   7.7 ∥ **The bench `System`, `Run` and `AB`.** *Landed 2026-10-22* (`systems.System` over Container, Guest,
     Board and Mac `Volume`; one `Run`; `ab.AB` with `Arm(system, slot, args)` and `Rounds`; the fleet orchestrator
     is `change.py`; `stage` is `deploy`, `staged` is `run --system`; the autorun runs the AB in process).
     Row `bench`: 6,343 of 1,300 -- `change.py` (530), the statistics (650) and the record format (320) remain.
   7.8 ∥ **Boot.** *Landed 2026-10-22* (one `Channel` with methods over two Machines; `arm_sequence` per driver;
     `Boot.enter`/`release` the one retry rule the board A/B calls). Row `boot`: 1,263 of 650.
   7.9 ∥ **Images.** *Landed 2026-10-22* (every builder is data and stages over `task.Stage`; `task.show` the one
     dry-run printer; the `_ws` halves read their own preset; the old-helper probes and the never-varied knobs are
     gone; the SDK's edits are the fork's `wk` branch at `SDK_PIN`). Row `sysimage`: 3,767 of 950 -- the card
     verbs, the Mac volume's provisioning and the two in-workspace builder halves remain.
   7.10 ∥ **Guests and daemons.** *Landed 2026-10-22* (`lib/wk/daemons.py` renders and converges the proxy, push
     service, injector and broker on the host, in the podman machine and in a guest; `guest.py` is start, boot,
     forwards and admission; `machine_cmd` is a kind table). Row guest: 2,387 of 900. Owed: the in-guest
     LaunchDaemons of `vm/provision-base.sh` as rows (needs a base rebuild to test).
   7.11 **Tests by cluster.** *Landed 2026-10-22* (every cluster to its subsystem's module; `tests/test_lint.py`
     is the one rule table, `-k <rule>` selecting a row; `tests/test_owed_live.py` is generated at import from this
     file's Owed table, one stub per `live`/`unit` name no test holds). The tests rows sum to 40.2k against 15k:
     what remains is distinct behaviour at about twelve lines a test; the budget is met by shorter tests over
     shared answer tables, or by scope the user names.
   7.12 **Budgets in lint.** *Landed 2026-10-21* (`tests/test_lint_budgets.py`: one owed test per row of the
     table above and one for the sums, failing loudly once a row is under budget; `python3 -m
     tests.test_lint_budgets` prints the rows).
   7.13 **Regression tests, end to end.** *Designed 2026-10-22; lands module by module.* A unit test names a
     behaviour a person can see and states it as `wk <argv>` in and four things out: the exit code, the text as the
     person reads it, the commands the fake machines were asked to run (the `World`'s recorded argv, in order) and
     the store's final state (records, locks, files). It enters through the dispatcher (`tests.fakes.FlowTest`),
     never a lib function, and asserts nothing about which function ran or how often. The `World` is the one fake:
     it answers as the real substrate does (podman, tart, ssh, launchctl, the filesystem) from answer tables kept
     beside the tests, one table per subsystem, and a test that needs an answer the `World` cannot give extends the
     `World`, not the test. The test is a regression test when a change that keeps the behaviour (a rename, a move,
     a merge of two functions) breaks no test and a change that breaks the behaviour breaks the one test that names
     it. So: no assertion that a function was called; no `mock.patch` of a lib name; no test of a private helper; no
     assertion on prose but the one line a person acts on (a refusal's first line, a status row). The kill-point rows
     (`tests/killpoints.py`) and the conformance mixins already have this shape and stay. The rewrite follows the
     workflows -- workspace (`new`, `build`, `rm`, `status`), agent sessions (`ai`, `key`, the wall), board bench,
     Mac bench, then the rest -- one module at a time, its old tests deleted in the same change; `lint.test_shape`
     (owed until the first module lands) refuses a test that imports a lib module or patches one. The tests
     interim budget is lowered per row as each module lands.
8. Delete this file.

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
| `wk run --profile` records in every mode (sampling prints the tier breakdown, bytecode leaves one JSCProfile json, samply refuses with the host remedy above `perf_event_paranoid` 1, instruments records a `.trace`) and `--fetch` copies the recording out byte for byte | 3 | `live profile.modes[<mode>]` |
| `wk run --profile=sysprof` captures a jsc run with sysprof-cli in a container workspace (unprivileged, no sysprofd), and the capture opened in Sysprof names JS frames from the JIT dump | 3 | `live profile.modes[sysprof]` |
| `wk run --rr` records jsc and `wk gui --rr` MiniBrowser's process tree in a container workspace (seccomp, the perf counters in the podman VM, the web process sandbox), and `wk run --replay` reaches lldb with rr's commands | 3 | `live run.rr_record_replay` |
| `wk bench run --a-args/--b-args` runs a JetStream3 subtest in the jsc shell with an option toggled, in alternating rounds, and `wk bench report <task>` compares it per subtest | 5 | `live bench.options_ab` |
| rpi4 boots its bench system reliably: EEPROM sd-first with `BOOT_WATCHDOG_TIMEOUT` and `MAX_RESTARTS`, a medium that holds the bus under write load, the stick reproduced from the repo by `wk sysimage write --disk rpi4:/dev/sda`, and `--back` reaches the rescue | 5 | `live boot[rpi4]` |
| The Mac's bench volume runs the whole lifecycle: `wk boot mbp` against the real install, a stage from a guest onto it, a measured run that holds and powers off, `--collect` from the host install after the power-button pick, `wk quiesce status` before it, the screen watch during it, and `wk bench compare` against a container run | 5 | `live bench[mbp]` |
| A second spelling of a bench system (`system image`) or a warmup profile (`warmup capture`) fails lint; `round` and `slot` have no second spelling in the tree, and `device` is the CLI's word (`--devices`) for a bench machine | 5 | `lint.vocabulary` (landed 2026-10-21) |
| A failed leg on a board leaves evidence readable afterwards: a persistent journal, browser and tunnel logs on every leg, a board-at-failure capture, and warmup evidence on a leg that timed out | 5 | `unit bench.failed_leg_keeps_evidence`, `live bench.evidence[<board>]` |
| The live tier runs against the container place on Linux and macOS alike; no test is gated on the podman VM | 1 | `live` runner rule |
| The fleet view is one: the exit code is the worst state found anywhere, a name alive on two machines is a conflict `--on` disambiguates, two workstations reaching one box see one state and a disagreement names both views | 2 | `unit status.fleet_is_one` |
| `wk status <ws> --log -f` follows a live build on any place | 2 | `live logs.follow[<place>]` |
| Bare `wk stop` then `wk start` returns every workspace to running; `--keep-vm` leaves the podman machine up | 2 | `live start.roundtrip` (one workspace's stop and start is `tests.test_lifecycle.TestContainerLifecycle`) |
| `wk doctor` on a freshly set-up machine reports ok, and each printed fix clears its line when run | 2 | `live doctor.fix_clears_line` |
| `wk doctor` reports a bench machine's readiness (SIP on both installs, the quieting) the way `wk quiesce status` does | 5 | `live doctor.bench_readiness[mbp]` |
| A machine is rebuilt from the repo alone: `wk doctor` names every machine-local entry regenerable, re-authable or backed-up before the wipe, and a fresh clone plus `./setup` sees the whole fleet with nothing copied | 2 | `live doctor.reprovision[<machine>]` |
| `./setup` completes on every host OS and every privileged stage installs its helper | 2 | `live setup.completes[<host>]` |
| A workspace on a peer is created there by hand (refused here, naming the command) and removed from here; `wk rm --all` asks once for the whole fleet and routes each removal | 3 | `live rm.peer[<machine>]` |
| `wk build <box-ws> <config> --detach` from a workstation leaves its record on buildbox4 alone: the box's own `wk status`, this workstation's and another's show the one build, `wk status --log -f`, `wk status --wait` and `wk build --kill` reach it through the hand-over, and a hand-over killed mid-way and re-run converges | 3 | `live build.box_record` |
| `wk build --babysit` is a task: one at a time by its record, ends stalled, gave-up or error by name, refuses where it cannot run, and a killed one reads died | 3 | `live build.babysit_e2e` |
| Every declared build preset builds on its place (gtk, wpe, mac-debug, ios-sim, armhf on 2.48), a fresh clone off a warm base builds in under 45 min, and a mac build produces ImageDiff. Measured 2026-10-21: `mac-release` cold in a 9-core, 20 GB guest took 116 min at six jobs with the compilation cache cold and precompiled prefix headers off (`--export-compile-commands`, the default), peaking at 12.9 GB; `gtk-release` finished in a container at three 4608 MB jobs. The guest's job count and the compile-commands default are the next two measurements | 3 | `live build.preset[<preset>]` |
| `wk test <ws>` runs the JSC suite and `--layout` on every place, against a remote place's own build | 3 | `live test.suite[<place>]` |
| `wk run` finds its binary on every port (GTK, WPE, an Apple-port guest) with `LD_LIBRARY_PATH` prepended, and `--lldb` gets a pty on every place | 3 | `unit run.finds_binary[<port>]`, `live run.lldb_tty` |
| `wk enter <ws>` lands in a shell, `--zed` against a broken workspace refuses naming the repair | 3 | `live enter.shell` |
| `wk sync` bare inside a workspace syncs it, `--all` reaches every workspace on every place, `--tools` refreshes every machine's copy and publishes one snapshot, `WK_MIRROR_BRANCHES` carries the extra branches | 3 | `live sync.fleet` (`wk sync <ws>` is `test_dev_integration` step 12) |
| The PR workflow runs as one flow: `wk sync --fix`, `wk pr`, a push through the push service, the `container/bin` helpers, agents building while a person pushes, including from an armhf container | 3 | `live pr.workflow` |
| `wk ai claude` refuses a stopped proxy, and a tool inside wanting the network is refused and told so | 3 | `live ai.walled_session` |
| In an agent session `git commit` and `git push` name the rule after git's own error | 3 | `live ai.commit_wall` |
| A publish from a container is served to a person's shell alone: a process left behind by an agent session (`setsid`, a detached job), a build that pushes from its build script, and `wk enter --own` are refused by the caller's ancestry; a person's shell beside a running agent is served | 3 | `unit push_service.TestAPush`, `live dev_integration.test_05_a_push_is_served_to_a_person_alone` |
| `wk ai claude` on a terminal, against a real container and a real guest holding only the placeholder login while the injector holds the claude.ai login, starts a session Remote Control shows under the workspace's name, its connection (WebSocket or SSE) passing through the injector; on a build box holding the inference token it starts without it and says so | 3 | `live ai.remote_control` |
| In a real guest `wk doctor <ws>` shows the placeholder login and no readable claude.ai token, and a `claude -p` answers through the host injector (measured for containers; the guest half is held by `live inject.claude_login[vm]`, run while a guest is up); any host beyond api.anthropic.com, claude.ai and platform.claude.com the CLI sends the bearer to is measured from the proxy log and added | 3 | `live inject.claude_login[vm]` |
| The Mac's (or a Linux machine's) injector refreshes an expired claude.ai login against the CLI's token endpoint and stores the rotated refresh token; the podman machine's injector never reads the login | 3 | `live inject.claude_refresh` |
| The injector puts the claude.ai access token only on the path prefixes the Claude CLI sends the placeholder to, measured from the injector's log over a real session (Remote Control included); until then it injects on every path but the OAuth token and authorize endpoints | 3 | `live inject.claude_paths` |
| `wk zed` reaches a workspace through its `Host wk-<name>` ProxyCommand alias on every place, one hop for a peer's, and `wk new --zed` warns instead of failing when zed cannot launch | 3 | `unit zed.alias_is_proxycommand`, `live zed.peer` |
| `wk key setup` elects across workstations: the credential its issuer accepts wins from whichever machine runs it, and a second run moves nothing | 4 | `live key.election[<peer>]` |
| A build box holds no deploy key at rest, no ssh to it (nor to the gateway in front of it) forwards a key, a push on the box is refused naming `wk pr open`, and `wk pr open <box-ws>` on the workstation fetches the box's branch into the mirror over ssh and pushes it from there with the deploy key, refusing naming `wk key deploy` while this machine holds none; the `refs/wk/push/*` ref a killed push leaves is taken by a plain `wk gc`. The unit half is green; the live run against buildbox4 is owed | 4 | `unit push.remote_forwarding`, `live push.from_box` |
| `wk new <ws> --on buildbox4` and `wk rm <ws>` from a workstation are the box's own `wk new` and `wk rm`: the creation record and log are on the box and the workstation keeps none, the box's `wk status` and the workstation's read one state for the workspace during creation and after, and the creation's fetch step fetches. The unit half is green; the live run is owed | 3 | `unit box_workspace`, `live new.box_record[buildbox4]` |
| A hand-over to buildbox4 while its wk-tools commit differs from the workstation's is refused naming `wk sync --tools buildbox4`, `--force` crosses it and says so, and `wk status`/`wk status --log` hand over and report the difference. The unit half is green; the live run is owed | 3 | `unit handover.tools_level`, `live handover.tools_level[buildbox4]` |
| `wk key sudo setup` installs its sudoers rule, validates with `visudo -c` before and after, proves `sudo -n true` fails, gets a terminal over ssh with `--on`, and no fleet machine holds a NOPASSWD grant wider than the three helpers | 4 | `live sudo.require[<machine>]` |
| One host-owned mirror feeds the podman VM and every guest: alternates resolve inside a container, `./setup` recreates the mount, and a missing mirror refuses naming `wk sync` | 5 | `live vm.shared_mirror` (the guest half), `unit new.refuses_without_mirror` |
| The golden base is rebuilt from `WK_VM_IMAGE`, carries no build caches, tracks the Xcode GA image, and `wk vm base --rm` asks separately about the pulled image while guests keep working | 5 | `live vm.base_matches_pin`, `unit vm.base_rm_asks_twice` |
| `tart exec` is the one way into a guest: a command runs as the guest's user with its home and its status, a binary copy crosses both ways intact, and a detached job outlives its exec | 5 | `live vm.tart_exec` |
| A guest booted from a base rebuilt with vm/mount-mirror.sh has the mirror's `wk-mirror` tag mounted at boot under `/Volumes/wk-mirror`, the checkout's alternates resolve there, and `wk sync`'s remount leaves agent-rw's mount as it was | 5 | `live vm.mirror_tag_mount`, `live sync.guest_remount`, `live vm.shared_mirror` |
| The editor and the socket forwards reach a guest's sshd over `tart exec` (`sshd -i` as the guest's user, no network): `wk zed <guest>` opens, and a `git push` in the guest reaches the push service through its forward | 5 | `live vm.ssh_transport`, `live zed.peer` |
| `wk sync` inside a guest asks the host's broker over the socket each start forwards to `~/.wk-broker.sock` | 5 | `live vm.broker_forward` |
| The guest desktop is usable and stays so: the window resizes, `open -a` launches, screen saver, sleep and lock stay off across a reboot, both Setup Assistants stay suppressed, lldb prints no `llvmcas:` warnings | 5 | `live vm.desktop` |
| `wk quiesce on` sets and reads back every setting on every machine (governor, App Nap, high power mode, sleep, update checks from the setting, Do Not Disturb proven by a banner not drawn), `off` restores the real prior values after a reboot, a re-run is a no-op, and it returns over ssh | 5 | `live quiesce.readback[<machine>]` |
| Every launchd job on a Mac bench install and every systemd unit on a Pi image is classified in the quiet tables, none wedges a probe when stopped, and the table is re-read after an OS bump | 5 | `live quiesce.classified[<machine>]` |
| `wk quiesce session on\|gdm\|off` reaches the asked mode from any half-state on the intended chip, `wk gui` draws in that seat and refuses a remote place, and `wk bench` refuses a BMC seat | 5 | `live session.modes[moose]`, `unit gui.refuses_remote` |
| A bench run pins the cores it records, in a container and in a guest | 5 | `unit bench.pins_cores`, `live bench.pins_cores[<place>]` |
| `wk bench compare` gives per-subtest confidence intervals from the workspace that built the run | 5 | `live bench.compare` |
| A report labels an instrumented leg's time and leaves no stray settle directory (a settle or warmup leg is already no measured leg and no cost) | 5 | `unit bench.report_and_cost` |
| An A/B on the Mac resolves a PR-sized delta: one run varies `--count`, one `--rounds`, against the measured per-round spreads of all three plans | 5 | `live ab.resolution[mbp]` |
| `wk bench ab --patch` builds, stages and measures both arms of a `mac-release-pgo` pair end to end, and the measured build's dSYMs symbolicate a capture | 5 | `live ab.pgo_pair[mbp]` |
| The guest rehearsal drives the whole Mac A/B path (two arms staged, run, collected) on `benchvm` | 5 | `live bench.rehearsal[benchvm]` |
| The warmup round's profile is captured, saved and symbolicated on every system (samply on the Mac and aarch64, sysprof-cli with the image's own flags on armhf) | 5 | `live bench.warmup_profile[<system>]` |
| A first run after a stage is no slower than the second, or the cause is named and paid before the run | 5 | `live bench.first_run_after_stage[mbp]` |
| A browser launch on a board starts from a named, cleared cache so a cache-served load never stalls a leg, the warmup probes are read on both widths, and GPU engine time is read or the warmup says why not | 5 | `live bench.leg_completes[<board>]` |
| A deploy routed through a macOS workstation's podman VM reaches a board in bench mode over Tailscale SSH with no key of its own | 5 | `live bench.routed_deploy` |
| A board PGO collection records its cost per plan, its coverage against the floor, and what the collecting browser rendered and JITted with; the kill waits long enough for the profile dump | 5 | `live bench.pgo_collection[<board>]` |
| Upstream carries what the lane patches locally (OSXMiniDriver's pgo dirs, `locate_binary_xcrun` off macOS, an optional `pgo-profile compress`, the JIT-report crash in JSC, the SSID redaction), and the local patch file goes | 5 | `unit pgo.no_local_patch` |
| `wk boot mbp` restarts into the bench default and refuses when the default is not the bench volume | 5 | `live boot.arm[mbp]` |
| A fresh bench volume provisions itself: python3 present before it is needed, Command Line Tools fetched and the update denial restored, no FileVault, the `bench` account, room checked, network credentials checked | 5 | `live sysimage.mac_volume_provision` |
| A second build of one profile reuses sstate, and `--keep-work` leaves the kernel tree to configure | 5 | `live sysimage.sstate_reuse` |
| A disk written from any image is unique per disk (`LABEL=` images included) and mountable whatever the medium held | 5 | `unit sysimage.write_identity`, `live sysimage.write[<board>]` |
| A person's plain `sudo apt-get` inside a container workspace reaches the network through the per-exec bridge: the sudoers drop-in keeps the proxy and CA variables | 7 | `live sudo.network_in_workspace` |
| A guest booted from the rebuilt base runs its three launchd jobs as `daemons.py` rows: nosleep and mirror in the system domain, display as the user's agent | 7 | `live vm.guest_daemons`, `live vm.mirror_tag_mount` |
| A unit test enters through the dispatcher and patches no lib name (step 7.13) | 7 | `lint.test_shape` |
| Every card verb runs against a real card and is read back before unmount, and a board boots from it onto the tailnet with its seeded key | 5 | `live sysimage.card_verbs[rpi5]` |
| One WiFi credential reader, in the card helper | 5 | `lint.one_wifi_reader` |
| A board's first boot is proven on hardware: self-disarm parks the medium, self-return reboots an unclaimed board within the watchdog, the rescue marker holds, `config.txt.append` lands for every builder, an absent boot device falls through to host mode, armed-not-rebooted reads ARMED exit 2, and the prompt shows `bench` | 5 | `live boot.firstboot[<board>]` |
| rpi5 boots a bench system from its stick, the second-system pair included, and hands itself back | 5 | `live boot[rpi5]` |
| rpi3 runs the shared-card two-system layout: the helper on the rescue, `@second` and `@third` written from it, armed by id, an `--ab-systems` run, and a panicking bench kernel reverts | 5 | `live boot[rpi3]` |
| rpi4's 2.52 system brings up KMS on every boot, or the run names why not | 5 | `live boot[rpi4].kms` |
| rpi4's 32-bit buildroot system reaches userspace, read at a serial console | 5 | `live boot[rpi4].armhf` |
| rpi3 finishes Speedometer 3 on its yocto bench system with zram as its only swap (`/proc/swaps` after boot names `/dev/zram0` alone) | 5 | `live bench.speedometer3_zram[rpi3]` |
| A board run stops what another ssh login left running (a shell loop, a hand-started cog) before its session comes up, and names what it stopped | 5 | `live bench.clear_foreign[rpi3]` |
| The 2.38 buildroot rpi4 image boots a rev 1.4/1.5 Pi 4 from the SD on its own pinned firmware (raspberrypi/firmware 52185fdd) | 5 | `live sysimage.firmware[rpi4]` |
| A board PGO cycle builds its collection slot with thin LTO and its measured slot with full LTO, and both link and run on the board | 5 | `live sysimage.webkit_lto[<board>]` |
| A board is re-flashed from nothing by the machine holding its card reader, with no other provisioned machine | 5 | `live sysimage.reflash[rpi5]` |
| `wk machine setup <board>` takes a board from a blank card to an answering tailnet name in `pi-hosts` and back, `wk machine probe` finds it when on, and a workspace reaches only that address, on port 22 | 5 | `live pi.setup[<board>]` |
| `wk machine setup <box>` leaves a build box in one shape (zsh, or a named warning), and a cleanup accepted at the prompt is removed | 5 | `live machine_cmd.setup[<box>]` |
| `wk machine setup <box>` installs the pinned node into the box user's `~/.local` without sudo, and `wk new --on <box>` then installs pi there | 5 | `live machine_cmd.node[buildbox4]` |
| A board is reached by tailnet name alone: no MAC, `.local`, address stanza, `HostKeyAlias` or ProxyJump remains once both boards join by image, and the bench install is reached at its own name | 5 | `lint.no_addresses` (tests/test_lint.py; the tree check is owed: the board and bridge confs' MACs, the bridges' `HostKeyAlias`) |
| An image build runs from a Tart guest and the image reaches the host for writing | 5 | `live sysimage.build[vm]` |
| A task's results live in its workspace, `wk bench ls` names them wherever they are, the task restarts from where it stopped on any machine, and `wk doctor` names the results backed-up | 6 | `unit test_bench_results`, `killpoints[bench export]` (tests/test_bench_results.py) |
| On a macOS host a board A/B's run-benchmark and page server run in the podman VM and reach the boards | 5 | `live bench.vm_reaches_boards` |
| `wk doctor <board>` against a live board prints its tailnet names, its system and arm, and its governor and temperature, with the rows the unit half asserts on a fake | 5 | `unit doctor.TestDeviceRows`, `live doctor.device[<board>]` |
| `./setup --stage tools` on a bare macOS host downloads git-lfs at its pinned version, checks it against the release's sha256sums, and `wk doctor` then reports it present | 5 | `unit doctor.TestHostToolsGitLfs`, `live setup.git_lfs[macos]` |
| bugs.webkit.org gates comment tagging on editbugs, so `wk doctor`'s Bugzilla row can say editbugs: the comment-tag probe answers a key of an account with editbugs and refuses (304) one without | 4 | `live credcheck.editbugs_probe` |
| A board reaches the page server at its tailnet name and port through the `tag:wk -> tag:wk` grant, with no `ssh -R` held for the run | 5 | `live bench.direct_page_server[<board>]` |
| A push from a wk-tools workspace, with no agent running in it, reaches the user's wk-tools repository on GitHub through the push service and its own deploy key | 3 | `live repo_workspace_live.ToolsWorkspaceLive.test_a_push_reaches_the_tools_repository_through_its_deploy_key` |
| A container's `git push` reaches the push service, which maps the peer's pid through its cgroup (`libpod-<id>.scope`, measured 2026-10-08 on the podman VM) to the workspace and reads its ancestry; wk-tools, the one repository an agent publishes, is served to a person, to wk's own run and to an agent session alike, and a fork is not a wk-tools workspace's to push | 3 | `live repo_workspace_live.ToolsWorkspaceLive.test_the_push_service_serves_the_tools_repository_to_a_person_to_wks_own_run_and_to_an_agent`, `test_a_fork_is_not_a_tools_workspaces_to_push` |
| The podman machine's push service pushes with the deploy keys `./setup` and `wk key deploy` copied into it, and its proxy sends what is not a person's shell to the injector's reading socket | 3 | `live repo_workspace_live.ToolsWorkspaceLive.test_the_push_service_serves_the_tools_repository_to_a_person_to_wks_own_run_and_to_an_agent` (the keys), `live repo_workspace_live.ToolsWorkspaceLive.test_the_proxy_gives_a_person_the_write_socket_and_wks_own_run_the_reading_one` |
| A macOS guest's `git push` reaches the Mac's push service through the guest's forward and its own listener, and the Mac proxy sends a guest with an agent to the injector's reading socket by its address | 5 | `live push.guest_forward` |
| Speedometer 3 under MiniBrowser runs without its `wakeLock` error: an upstream WebKit patch, landed last, after every other owed row | end | `live bench.speedometer3_wakelock[mbp]` |
| A `gtk-release` build in a container on the Mac's 20 GB podman VM finishes under the default sizing: WebCore's unified and derived sources at -O3 -g hold 14.3 GB anonymous at four jobs (measured 2026-10-21 from the cgroup's `memory.stat`; three runs killed at 3072 MB per job), so a CMake web port is sized at 4608 MB per job; the watchdog reads `anon`+`shmem`, since `memory.current` carried 2.2 GB of page cache and slab at that peak; and the OOM advice it prints changes the next run's job count from a macOS host | 3 | `live build.preset[gtk-release]` |
| `wk run --profile --browser --process ui\|web\|network\|gpu` attaches samply to a live MiniBrowser in a workspace with a display, heaptrack and massif reach `--browser` on the CMake ports, and the fixed-wait attach holds on a slow page | 3 | `live run.browser_profile` |
| `wk run --lldb` in a fresh workspace symbolicates glibc, Mesa and GTK frames, and `gtk-debug` runs under lldb | 3 | `live run.debug_symbols` |
| A container workspace holds `CAP_CHECKPOINT_RESTORE` in the init user namespace for mya, `wk build` sets the capability on `bin/mya` and `bin/testLibJSCTools` after an `ENABLE_MYA` build, the proxy allows the SDK's debuginfod and the workspace sets `DEBUGINFOD_URLS`, the SDK carries liblldb-dev, and the `testLibJSCTools` suites pass in a fresh workspace | 3 | `live mya.checkpoint_restore`, `unit mya.setcap_after_build` |
| moose boots a bench system: a bench-role conf naming the peer, `--bootnext` and `toram` proven on its firmware, a yocto image with UEFI rules in `boot/check-boot-files.py` and `IMG_WATCHDOG`, a reboot refused while the BMC is unreachable, the UEFI entry in `wk doctor`'s machine-local section, and `--accept-routes` right on rpi5 and moose | 5 | `live boot[moose]` |
| The Mac in bench mode caps its battery charge, turns the display off between legs and on for one, and `wk quiesce status` reports both | 5 | `live bench.mac_charge_cap`, `live bench.mac_screen` |
| SIP is read on both Mac installs (`csrutil status`), on for the host and off for the bench volume, and `wk doctor mbp` reports each; `wk boot mbp` refuses to arm while the bench volume's is on | 5 | `unit doctor.sip_rows`, `live doctor.bench_readiness[mbp]` |
| An A/B's rounds cross the variance factors (layout, env size, path length, shared cache) and the report pairs rounds across them, on the Mac and on Linux alike; a side whose spread grows in one configuration is named; one measured run per arm runs under samply with a JIT dump; an unattended run stops at its wall-clock budget; run wall time is compared beside score | 5 | `unit bench.variance_pairing`, `live bench.linux_variance` |
| Both arms of an A/B carry an identifier the tooling reads back from the running binary, so a linking slip or an interrupted deploy cannot pass as a measurement | 5 | `live bench.binary_identifier` |
| The clangd flags in `container/helix/languages.toml` are measured (indexing time, peak memory) against a real checkout | 3 | `live clangd.measure` |
| pi runs on a macOS guest and a build box, its TUI runs in a container, and one `~/.pi/agent/models.json` per machine replaces the one `wk new` writes per workspace | 3 | `live pi.session`, `unit agents.models_per_machine` |
| Web Inspector reaches a layout test or a page in a headless workspace over an ssh forward | 3 | `live run.inspector_remote` |
| `wk build <ws> <preset> --sysroot <name>` cross-builds GTK or WPE for an Ubuntu rpi3 from x86_64 or a Mac into its own build directory, deployed and run on the board; riscv64 and a QEMU user-mode JSTests lane follow | 3 | `live build.sysroot` |
| A macOS guest gets its own helix `languages.toml` (no `--malloc-trim`, the Xcode compile-commands directory) | 3 | `unit helix.macos_languages` |
| `wk pr open` on a branch pushed before (its `-u` upstream resolves nowhere under the mirror's fork refspecs) opens the PR instead of refusing, and leaves the workspace wired right | 3 | `unit pr.open_twice` |
| An asan and a non-asan CMake preset build into separate directories, so `wk run --preset jsc-release-asan` runs the asan build | 3 | `unit presets.asan_build_dir` |
| `./setup` converges the podman machine's git identity and speed settings, the rows `wk doctor` names | 2 | `live setup.vm_gitconfig` |
| A fresh container workspace reports itself wired right (the push alias is the push service's to resolve, not the workspace's ssh), and `wk new`'s summary tells a wiring finding from a fetch that failed | 3 | `live sync.fresh_workspace_wired` |
| A referenced tree path exists: every `/opt/wk-tools/<path>` and `container/bin/<name>` a file names is in `git ls-files`, so an untracked file cannot leave a workspace's entry broken | 1 | `lint.referenced_paths_exist` |
| The macOS `_remote_probe_cmd`/`_remote_probe_parse` branches hold against a live macOS or BSD remote, not only captured samples | 5 | `live remote.probe_macos_bsd` |

### Decisions for the user

Decisions are asked as questions, and their answers are recorded under "Decisions taken".

- Zero-touch Mac nights: decided 2026-10-22 (step 5): `WK Bench` is the firmware default, set once by hand, and
  wk never blesses; the way back is the person's power-button pick. The default is not yet set (`wk boot mbp
  --status`, 2026-10-22 08:10 MST: `firmware_default=the host install`); the Mac rows run once it is. Detection under 0.1% and the 8-hour budget stay rows (`live ab.resolution[mbp]`,
  `unit bench.variance_pairing`).
