# Testing checklist

What has to pass before believing this works. Grouped by target, because a
change that is fine for containers can be silently wrong for a macOS guest and
vice versa — that has already happened more than once.

Two rules make the rest of it worth doing:

- **Test the property, not the configuration.** A firewall that failed to load
  and a proxy that is not running both look perfectly fine in the file that was
  supposed to produce them. `wk verify` connects and sees what happens; prefer
  it to reading a ruleset.
- **A silent pass is not a pass.** Several failures here were invisible
  (`wk new` reporting success while firstrun had aborted; `wk run` finding a
  build directory that did not exist). If a check cannot fail loudly, it is not
  a check.

Legend: **[V]** verified working at least once · **[ ]** not yet verified ·
**[!]** known broken or unimplemented.

**`wk selftest` runs the automatable part of this file.** `wk selftest --quick`
needs no workspace, no podman and no ssh, so it is cheap enough to gate an
edit; a bare `wk selftest` adds the sections whose prerequisites are present
and skips the rest out loud. It exits 0 only if everything it ran passed.

Each check names a phrase from the line it implements and looks that phrase up
here at run time, so rewording a line without touching the runner is reported
as DRIFT rather than quietly passing. The runner prints how much of this file
it covers on every run — the remainder is still hand-ticked, and the manual
ones (watching a monitor go dark, judging a desktop) always will be. Every [V]
outside the runner's coverage is only as fresh as the last human pass.

---

## 0. Host bootstrap — both platforms

- [V] `./setup` completes on macOS
- [ ] `./setup` completes on Ubuntu 26.04
- [V] `./setup` run twice reports **no changes** the second time
- [V] `zsh` present: system shell on macOS, installed from `apt.txt` on Linux
- [V] `~/.bashrc` and `~/.zshrc` both source `shell/bashrc`, exactly once
- [ ] an interactive shell on each host lands in zsh, with history settings applied
- [ ] `./setup --stage quiesce` installs the privileged helper (needs a terminal)
- [ ] `./setup --stage softnet` installs softnet SUID root (needs a terminal, macOS only)
- [V] no `wk` command in the daily path calls `sudo` on either host
- [V] `wk sudo status` answers without ever prompting (every probe is
      `sudo -n`), names what is wrong and the command that fixes it — measured
      on this Mac (keeps a timestamp) and on buildbox4 (`NOPASSWD: ALL` from
      the site sudoers)
- [V] and it reads sudoers by its **last-match** rule, not by grepping for
      `NOPASSWD`. Fixed 2026-08-19: on buildbox4 `sudo -l` lists the site's
      `NOPASSWD: ALL` *and* the drop-in's `PASSWD: ALL` after it, and the grep
      matched the line already overridden — reporting a correctly hardened
      machine as wide open. The verdict now starts from `sudo -n true`, which
      is evidence rather than a reading
- [ ] `wk sudo require` installs `/etc/sudoers.d/zz-<user>-passwd`, validates
      with `visudo -c` before and after, and then proves the property: `sudo -k`
      followed by `sudo -n true` fails
- [V] `wk sudo require --target <machine>` is idempotent: with the property
      already true it says "already required" and does **not** prompt or
      re-install (measured on buildbox4, 2026-08-19). It gates on the property
      and not on `[ -f "$DROPIN" ]`, which is false whenever the remote login
      name differs from this machine's — the normal case for a shared box, and
      the reason every run used to ask for a password
- [ ] `wk sudo require --target <machine>` on a machine that needs it: the
      password prompt gets a terminal over ssh
- [V] `wk sudo status --all` walks every configured machine and names the fix
      per machine

## 1. Container workspaces — the default target

Run these inside the podman VM on macOS, and directly on Linux.

### Lifecycle
- [V] `wk sync` publishes a base snapshot
- [V] `wk new <ws>` succeeds **and waits for firstrun to finish**
- [V] firstrun completed everything: `~/.wk-firstrun-complete` exists, plus
      `.lldbinit`, `.bash_profile`, the shell rc line, `hx`, the push keys
- [V] `wk ls` / `wk status` list it; `wk status` exit code is 0
- [V] every checkout has `origin` = `WebKit/WebKit` with pushing to it refused
      up front, and both forks already added (fetch HTTPS, push ssh via the
      per-fork host alias) — one wiring for every target, `wk_wiring_script`
- [V] `wk rm <ws>` reclaims the container, the overlay, the home and the ssh alias
- [ ] `wk stop` then `wk start` returns every workspace to running
- [ ] `wk new <ws>` with no base snapshot says `run 'wk sync' first`, creates
      nothing, and registers nothing
- [V] `wk new` interrupted mid-create leaves nothing half-alive: a re-run
      destroys the rubble and remakes the workspace from scratch — it never
      re-pins `base-id` over a surviving `changes/` layer from the first try
      (2026-08-19, macOS→podman VM: `base-id` removed to stand for a create
      killed before it was written; `wk ls`/`wk status` then said `creating`,
      and `wk new` destroyed and remade it in 50s)
- [V] `wk rm` that cannot remove everything exits nonzero, keeps the registry
      entry, and names what is left — never "destroyed" over an orphan whose
      `base-id` pins a snapshot forever (2026-08-19, forced with `chattr +i`
      on a file in the overlay); and a workspace with nothing left but its
      registry entry is forgotten rather than refused
- [V] `wk ls` and a bare `wk status` print the same workspace-name set on
      every host shape (macOS with containers+vm+remote, Linux, a build box)
      — verified on macOS with a container and a remote workspace, machine
      running and stopped; encoded in `wk selftest --section state`

### Sandbox — the part that must never be assumed
- [V] `wk verify <ws>` reports **sandbox intact**, all checks ok
- [V] podman is rootless
- [V] the workspace has no network interface but `lo`
- [V] a host outside the allowlist is refused
- [V] the local network is refused
- [V] there is no direct egress with the proxy bypassed
- [V] no host home, runtime directory or D-Bus socket is visible
- [V] `/opt/wk-tools` is read-only inside the workspace
- [ ] `wk claude <ws>` refuses to start when the proxy is stopped
- [ ] a Pi address in pi-hosts is reachable on port 22, and only port 22
- [ ] an address NOT in pi-hosts is refused (the negative, not just the positive)
- [ ] `sdk-patches/apply.sh` verify fails when a security section no-ops
      (temporarily break one token to prove the check can fail)
- [ ] one `claude login` in a workspace seeds `/secrets` and a second
      workspace inherits it
- [ ] the egress allowlist carries `ports.`/`archive.`/`security.ubuntu.com`
      (2026-08-24), so a workspace can `apt-get install` — which is what
      `wk zed` uses to put an sshd in one, and is also any other package in the
      distribution, in a workspace that already has sudo. A deliberate widening
      taken over the alternative (a derived image built outside the sandbox);
      `docs/HANDOFF-sandboxing.md` carries it for the audit
- [ ] `wk sync --target container` applies a changed egress policy rather than
      only pushing it: the tools-only path used to return before the proxy was
      reloaded, so a refusal was logged for a name the allowlist plainly
      contained until somebody re-ran `./setup`

### A barrier can be forced, loudly (`--force`)
- [V] a barrier refuses by default and names `--force`
- [V] `--force` turns it into a warning at the point of bypass **and** a
      repeated summary when the command ends
- [V] `wk build ... --force --cmakeargs=...` passes the flag through after
      saying what it will cost
- [ ] `wk claude --force` starts an agent in a workspace whose sandbox check
      failed, with the warning repeated at exit
- [V] `--force` is not consumed after a `--`: `wk run <ws> -- --force` is the
      program's argument

### Remotes: `origin` and `fork` are different things (`wk remotes`)
- [V] the wiring is *checked*, not only asserted at creation. Measured
      2026-08-19: `db` on devbox-arm64-2 was correct and `bb4` on buildbox4 was
      not — `origin` was that machine's local mirror **and pushable**, `fork`
      pushed over https so no deploy key could ever be offered, `forkwpe` was
      missing, and nothing resolved the ssh aliases. Both had been created by
      the same driver; nothing had ever asked
- [V] `wk remotes` reports every workspace this machine can see (the same walk
      `wk status` uses, delegating to machines of their own), naming each fault
      and what it costs; `wk remotes <ws> --fix` re-asserts the wiring from the
      same authority that creates it (`wk_wiring_script`) and re-checks
      afterwards rather than claiming success
- [V] `--fix` takes its arguments from the driver (`t_wiring_args`), so a
      re-assertion cannot wire a workspace differently from the way it was
      created — the machine's mirror or its shared WebKit, and the ssh config
      under the wk root
- [V] a wrong origin is flagged where it is seen: `wk status <ws>` prints
      `origin is <url>, not upstream -- 'wk remotes <ws> --fix'` (verified by
      breaking it deliberately in a container workspace and repairing it)
- [V] `origin` refuses a push at once rather than after an auth round trip:
      `git push origin` → `remote helper 'no-push' aborted session`
- [V] the *branch* is checked by the same rule, because the rule is absolute --
      we never push to an upstream, always to a fork: a branch tracking
      `origin/<x>` can never be pushed by a bare `git push`, and on bb4 that ref
      existed only on the fork. `--fix` retargets it (fetching the one ref
      first, since `git branch -u` refuses an upstream it has never seen) and
      says so; a branch not yet on the fork is left alone, naming the push
- [V] each upstream is paired with *its own project's* fork: a branch tracking
      `wpe/<x>` belongs to `forkwpe`, not to `fork`. Derived by repository name
      from `wk_remotes` and `wk_push_forks`, so adding a project is one edit
- [V] the wiring creates every remote the wiki's own set names: `origin`, `wpe`,
      `fork`, `forkwpe` (plus the machine's local copy). `wpe` was missing
      everywhere until 2026-08-19 — so no workspace could `git fetch wpe` at
      all, while `wk pr` accepts PRs from that project and the mirror had been
      carrying its objects the whole time. `igalia` is a deliberate omission
      with the reason recorded next to the list: ssh port 4429 is not in the
      egress allowlist and a workspace holds no personal key
- [V] the **base snapshot** is checked too, because it is where every future
      workspace's remotes come from — and it was wrong: `origin` pushable over
      https, `fork` pushing to github.com directly rather than through the
      alias that selects its deploy key, no `forkwpe`, no `wpe`. Published
      before the wiring authority existed and never looked at since;
      `container/firstrun.sh` had been quietly correcting each workspace.
      `--fix` re-wires it (2026-08-19). The environment half of the check
      (can ssh resolve the alias *here*) is skipped for a snapshot: it lives in
      the podman VM, which has no alias config and needs none, so asking would
      report a fault no re-wiring can clear
- [V] the podman VM does not enumerate the shared registry: the whole
      repository is rsynced in there, so a forwarded half was walking every
      machine in the fleet and paying an ssh timeout each for machines it has
      no key or route to. A bare `wk status` went from 12.1 s to 8.7 s
- [V] the podman VM's copy of wk-tools has a way to be refreshed at all:
      `wk sync --target container` pushes it (`t_sync` on the container driver).
      Before that, only `./setup --stage vmtools` did — so a command added to
      this repo was "unknown command" inside every container, which is exactly
      how `wk remotes` first failed
- [V] a delegated command a far side is too old to know explains itself
      instead of dumping that machine's usage unexplained (measured against
      moose, a peer on an older checkout)

### Pushing to git is a switch (`wk push`)
- [V] a workspace's deploy keys are **symlinks** into the read-only `/secrets`,
      never copies — a copy is a key the switch cannot reach, and it also
      strands the workspace on a rotated key
- [V] `wk push off` takes effect in a workspace that is already running, with
      nothing restarted: `ssh -T git@github-webkit` and `git push fork` both
      fail with `no such identity`
- [V] from inside a workspace with the switch off, the held keys are
      unreachable (`/var/lib/wk/push-keys`: no such directory) and `/secrets`
      is read-only, so nothing in there can turn it back on
- [V] `wk push off` holds back **every** private key in `secrets/`, not only
      the ones `wk_push_forks` names — a leftover `build_key` is a push
      credential too
- [V] `wk claude` turns the switch off before starting an agent, says so, and
      leaves it off after a headless run
- [V] `wk claude` on a **remote** target refuses by default naming what is
      missing (no container, no separate uid, no filesystem boundary, other
      people's work), and `--force` proceeds — offering to install the Claude
      CLI there when it is absent, and declining by itself with no terminal
- [ ] `wk claude` in a terminal turns it back on when the session ends
- [V] `wk key check` says "registered, but push is OFF" rather than failing to
      authenticate; `wk key ensure` does not generate a second key over a held
      one
- [V] fetching is unaffected by the switch: with push off, `git ls-remote fork`
      and a real `git fetch fork main` both work from inside a workspace
      (anonymous HTTPS, no credential — 0.6 s)
- [V] `wk push status` answers with the podman machine stopped and leaves it
      stopped: a subcommand that only lists must not boot a VM

### Push keys on a shared build machine
- [V] `wk key ensure` on a build machine generates a key of *its own* under
      the remote root — never a copy of another machine's
- [ ] `wk key register` registers each machine's key separately, titled with
      the machine name, and `wk key check` reports per machine
- [!] a remote checkout gets `origin` = WebKit/WebKit, both forks, the
      machine's mirror, and `core.sshCommand` pointing at `$root/ssh/config`
      — nothing outside the wk root is edited. True of `db` (created after the
      wiring landed) and **false of `bb4`**, which predated it: see
      `wk remotes` above, which is what now says so
- [V] **push works end to end from a container**: with the switch off,
      `git push --dry-run fork HEAD:refs/heads/…` from inside fails; with it on,
      the same command reports `* [new branch]` (2026-08-19, a throwaway
      workspace; `--dry-run`, so nothing was published)
- [!] and does **not** yet work from a remote target, for a reason that is not
      the switch: with push on, the wiring correct and the alias resolving,
      buildbox4 got `Permission denied (publickey)` — its deploy key exists but
      was never registered on GitHub (`wk key check`: bb4 "not registered",
      devbox-arm64-2 and moose "no key"). `wk push on` now says that a push
      also needs `wk key check`, because only the host can ask GitHub
- [V] `wk push off --target <machine>` leaves ssh with `no such identity`
      there, and `git fetch fork main` still works
- [V] `wk status` lists each machine's keys by fingerprint with their state,
      once per machine, and the two machines' fingerprints differ

### Inside the workspace (the interface `wk claude` hands an agent)
- [V] `wk build jsc-release` with no workspace name builds this workspace
      (1m16s warm, from inside `selftest`)
- [V] `wk run -- -e 'print(1+1)'` prints 2; `wk test <args>` runs and reports
- [V] `wk status` / `wk logs` with no name report this workspace
- [V] the name form is *refused* from inside, not accepted as a synonym:
      `wk build <own-name> <config>`, `wk run/test/logs/gui/status/pr
      <own-name> ...` each say "there is no workspace argument in here" and
      print the form to type (2026-08-19; it used to build exactly what the
      short form builds, so everything written about the in-here interface was
      describing one of two spellings)
- [V] the one exception: a workspace *named after a config* still builds that
      config — `wk build jsc-release` in a workspace called `jsc-release`
- [V] `wk build <other-name> <config>` says *which* thing is missing, rather
      than reading the config as a workspace name
- [V] `wk build --list` works in a workspace with no podman anywhere
- [V] `wk new` / `wk rm` refuse — a workspace may not create or destroy one,
      and `wk rm` refuses *before* prompting
- [V] `wk verify` refuses from inside (it would report "sandbox intact" having
      measured neither the interfaces nor the proxy)
- [V] `wk claude` refuses from inside; `wk enter <own-name>` says so too
- [V] host-only commands refuse: `wk sync`, `wk gc`, `wk session`, `wk quiesce`,
      `wk vm`
- [V] the marker `~/.wk-workspace` is written by firstrun and names this
      workspace and its checkout; the host's own `$HOME` never gets one
- [V] the same, in a macOS guest — `wk-mac-rel`, no `WK_IN_VM`, no podman
      error: `wk build --list`, `wk build mac-release --dry-run` (-j9, the
      Release tree, `WEBKIT_OUTPUTDIR` set), `wk run -- -e 'print(2+2)'` → 4
      with the config taken from the marker, `wk test --dry-run` both suites,
      `wk status`/`wk logs`, and every refusal
- [V] `wk vm start` alone writes the guest's marker (no host-side build first),
      which is the case `wk claude` on a fresh guest depends on
- [V] the in-workspace build is sized from the whole guest: -j9, not the -j5 a
      second desktop reserve produced
- [V] `wk build --dry-run` / `wk test --dry-run` resolve everything and change
      nothing — no tooling sync, no status file
- [V] `wk build --dry-run` also prints the exact commands, including the ones
      only the target can resolve: `cd <src>` and the full `ionice -c3 choom -n
      500 -- nice -n 19 Tools/Scripts/build-webkit …` line, quoted so it can be
      pasted (2026-08-19, against buildbox4). `--explain` names the flag rather
      than trying to describe them
- [V] and it refuses to ask a target whose wk-tools predates the mechanism,
      naming `wk sync --target <t>`: an old `build-in-target.sh` ignores
      `WK_DRY_RUN` and *builds*. Measured the hard way — a dry run against a
      stale build box started a real build-webkit in a workspace somebody was
      building in, and rewrote that build directory's options file
- [V] the job count has no configured ceiling: it is derived per build from
      what the target has free *at that moment*, and the derivation is printed
      (`resources: 63 jobs (cores=128 avail=138327MB @ 1536MB/job, polite,
      load=65)` on buildbox4, where the old conf capped it at 16)
- [V] a conf that still sets `WK_REMOTE_MAX_JOBS` is reported as ignored,
      where the job count is chosen
- [V] every build is watched for memory every 30s from inside the machine
      building it: over budget or under the machine's free-memory floor, the
      tree is killed TERM-then-KILL, and the log carries `wk: MEMORY LIMIT`
      and the peak
- [V] `wk build` turns that into `state=oom` with `peak_mb`, and `wk status`
      renders "killed for memory (peak 1050MB)" plus the reason line
      (2026-08-19: `--mem-budget 700`, killed at 1050 MB after 16 s)
- [V] an explicit `--mem-budget` is used as given, never raised to the floor
      that applies to the derived one
- [V] the watchdog does not kill itself: it is a child of the shell that
      becomes the build, so it is in the tree it measures
- [V] `wk build --detach` returns in under a second and the build survives
      this end going away — in the podman VM for a container workspace, on
      the machine's own `wk` for a remote one (verified separately, 1.1 s)
- [V] both detaching paths go through one primitive (`detach_run`,
      lib/detach.sh): `--detach`'s local fallback and `--babysit` no longer
      carry a nohup of their own. `--detach` passes *no* status file on
      purpose — a build's liveness is the age of its log, not a pid, so
      nothing there can tell last run's `build.status` from a build that is
      running right now
- [V] `WK_TARGET_CMAKE` in a target's conf is added to every build's CMake
      flags on that machine, after the config's and the architecture's and
      before `--cmake`
- [V] `wk build ... --env NAME=VALUE` (repeatable) is applied *last*, so it
      overrides what the config sets rather than joining it — `--env CC=gcc-14`
      lands after the config's `CC=clang` and `env` takes the last assignment;
      a value containing spaces survives (`--env 'CXXFLAGS=-O2 -g'`), and a
      word without `=` is refused
- [V] `wk build ... --cmake '<flags>'` **adds** to the config's CMake flags
      (repeatable, shown by `--dry-run`), a bare `--cmakeargs` is refused
      naming the flags it would have silently dropped, and everything after
      `--` reaches build-webkit verbatim

### Build and run
- [V] `wk build <ws> jsc-release` succeeds
- [V] `wk run <ws> -- -e 'print(1+1)'` prints 2
- [V] `wk run <ws> --config wpe-release` starts (finds `WebKitBuild/WPE/...`
      and does not clobber `LD_LIBRARY_PATH`)
- [V] `wk build <ws> wpe-release` succeeds. On 80 cores, and the debug info is
      most of it: 4m47s / 11.3GB peak / 1.9GB tree before release builds carried
      any, 24m2s / 82.5GB peak / 5.9GB tree after
- [V] `wk build <ws> gtk-release` succeeds (24m47s, 96973MB peak, 5393 `.dwo`)
- [ ] `wk test <ws>` (JSC suite) and `wk test <ws> --layout`
- [V] every build writes `compile_commands.json` by default
- [V] `wk logs <ws>` shows `(none)` under errors for a successful build
- [ ] `wk bench` produces per-subtest results with confidence intervals

### Release builds carry debug info  (the CMake ports)
- [V] every release config is `CMAKE_BUILD_TYPE=RelWithDebInfo` with the
      optimization flags pinned to `-O3 -g -DNDEBUG`. The pin is the point:
      CMake's own RelWithDebInfo is **-O2**, so adopting the build type alone
      would have dropped every release build — and every benchmark number — by
      an optimization level. Read out of a configured cache, not assumed
- [V] `GCC_OFFLINEASM_SOURCE_MAP` comes on with the build type, which is what
      gives the offlineasm-generated LLInt line information
- [V] `DEBUG_FISSION=ON` is stated rather than defaulted, because its default
      cannot fire: `OptionsCommon.cmake:175` tests `ENABLE_DEVELOPER_MODE`, and
      `WebKitCommon.cmake:338` includes `OptionsCommon` *before* the
      `Options${PORT}` that defines it. Measured off, with zero `.dwo` files, in
      a tree that had asked for RelWithDebInfo and nothing else
- [V] what fission is worth here: 10G -> 5.9G tree, 1.9G -> 737M for
      libWPEWebKit, 98.4GB -> 82.5GB peak build memory, 26m2s -> 24m2s. lldb
      still resolves source from the 5471 `.dwo` files
- [V] a build directory whose identity variables changed is wiped and rebuilt
      rather than failing partway in. WebKit stamps those six outside the cache
      (`.webkit-config-stamp`) and refuses to reconfigure across them; upstream's
      own `removeCMakeCache` handles every *other* changed flag, and both paths
      were seen firing on the correct half
- [V] the same for `gtk-release`: RelWithDebInfo, `-O3 -g -DNDEBUG`,
      `DEBUG_FISSION=ON`, `GCC_OFFLINEASM_SOURCE_MAP=ON`
- [ ] the two `-asan` configs, which this moved from an accidental -O2 to a
      stated -O3

### Debugging  (Linux, the CMake ports)
Verified 2026-08-24 in a `wpe-release` container workspace, wkdev-sdk
2.53-v9, aarch64. The two entries first are not conveniences: without either,
every one of the four below fails before the program starts.
- [V] the debugger is *resolved*, not assumed. The image's PATH puts
      `/opt/swift/usr/bin` first and that lldb cannot start at all — linked
      against `libxml2.so.2` in an image shipping `libxml2.so.16`, so it dies
      in the loader before printing its version. `lldb_prelude` runs each
      candidate newest-first and lands on the working `/usr/bin/lldb-22`
- [V] a container cannot turn ASLR off, and lldb tries to by default:
      `personality set failed: Function not implemented`, which is podman's
      seccomp answering ENOSYS rather than EPERM. `t_lldb_opts` sets
      `target.disable-aslr false`, and `process launch` then works
- [V] the debugger stays in the process it was pointed at.
      `dotfiles/lldbinit:10` says `follow-fork-mode child`, so before
      `lldb_pin_opts` the session left MiniBrowser for `WPENetworkProcess`
      seconds after `run` — observed, `stop reason = exec`, with the UI
      process's breakpoints re-resolving in the child
- [V] `wk run <ws> --config wpe-release --lldb -- -e 'print(6*7)'` stops at
      entry; a breakpoint on `JSC::Interpreter::executeProgram` resolves in
      `libWPEWebKit-2.0.so.1` and hits; `42` prints; exit 0
- [V] `wk gui <ws> --lldb` holds MiniBrowser before `main` with a backtrace
      through `__libc_start_call_main`, and stays there while the browser
      spawns its children. The debugger reaches the binary through
      run-minibrowser's own `WEBKIT_MINI_BROWSER_PREFIX` hook, so the port
      still sets `WEBKIT_EXEC_PATH` and the rest — `lldb -- run-minibrowser`
      would have debugged Python
- [V] `wk gui <ws> --lldb web <url>` attaches to `WPEWebProcess` as it launches
      and a breakpoint on `WebCore::Document::implicitClose` hits on the real
      page load
- [V] `wk test <ws> --layout --lldb <test>` attaches the web process the run
      spawns; the same breakpoint hits with a full symbolicated stack —
      `FrameLoader::checkCallImplicitClose` → `WebPage::WebPage` →
      `IPC::Connection::dispatchMessage` → `WebProcessMain`
- [V] `wk test <ws> --layout --lldb ui <test>` attaches WebKitTestRunner:
      `WTR::TestController::runTest` ← `runTestingServerLoop` ← `main`
- [V] a config with no browser is refused by name rather than by platform —
      `'jsc-release' builds no browser to drive` from `wk gui`, and the layout
      equivalent from `wk test`
- [V] `--waitfor` on the *first* web process is the right process: prewarming
      does not happen until a page has already loaded
      (`didReachGoodTimeToPrewarm`, `WebPageProxy.cpp:1231`)
- [!] but only until the page navigates. A cross-site navigation swaps the page
      into a new web process and leaves lldb in the old one — measured, with the
      document URL read at every `Document::implicitClose`: the attached process
      renders the empty document and `a.html`, then nothing, while the server
      logs `b.html` being fetched by somebody else.

      **PSON cannot be turned off on WPE or GTK4**, so this is described rather
      than fixed. `ProcessSwapOnCrossSiteNavigation` is a stable preference
      MiniBrowser exposes through `--features`, and setting it is inert here:

          bool processSwapsOnNavigation() const
          { return m_processSwapsOnNavigationFromClient
                       .value_or(m_processSwapsOnNavigationFromExperimentalFeatures); }

      `WebKitWebContext.cpp:444` calls `setProcessSwapsOnNavigation(true)`
      unconditionally on these ports, so the client value is always engaged and
      `value_or` never reaches the preference. Measured as well as read:
      `--features=-ProcessSwapOnCrossSiteNavigation` changes neither the
      documents the attached process sees nor the process count.
- [V] so the debugger finds the page instead of being told where it went.
      `follow-page` (`container/lldb/webprocess.py`, imported and aliased by
      `wk gui --lldb web`) attaches to *every* web process and asks each one what
      it holds, which takes two reads and needs both:

          pid 2501045  pages=1 suspended=1   <- attached, page already gone
          pid 2501296  pages=1 suspended=0   <- the live page
          pid 2502198  pages=? suspended=?   <- prewarmed, still starting

      `pages` is `WebProcess::m_pageMap`'s key count; `suspended` is
      `m_hasSuspendedPageProxy`, which the UI process sets on the process a page
      was swapped *out* of (`WebProcessProxy.cpp:2559`). Page count alone is not
      enough — two processes report a page after a swap and the stale one is the
      one you are attached to.

      Verified end to end: the original attach sees the empty document and
      `a.html` and then goes quiet; `follow-page` detaches the stale and
      prewarmed processes, keeps 2501296 stopped, and a breakpoint set there
      catches that page's next navigation (`c.html`). Two refusals are verified
      too — it declines rather than guesses when more than one process holds a
      live page, and it says "still inside the 30 s launch pause" when a
      candidate cannot be asked yet
- [V] `m_pageMap`'s count is read out of memory, not asked for: WTF keeps it in
      the words before the table (`keyCountOffset = -3`, `HashTable.h:614`), and
      calling `size()` in the inferior takes SIGSEGV
- [V] `reattach` remains for the other case — catching the next web process at
      *launch*, before it runs, rather than finding the one that already has the
      page. Breakpoints do not survive either move; both messages say so
- [ ] site isolation, which splits one page across processes per site. Off by
      default (`SiteIsolation (unstable)`) and untried
- [V] all of the above against `gtk-release` as well. Every mode behaves
      identically — `wk run --lldb` stops in `JSCConfig.h:161` in
      `libjavascriptcoregtk-6.0.so.1`; `wk gui --gtk --lldb` stops at
      `main.c:1016`; `--lldb web` attaches `WebKitWebProcess` and hits
      `Document.cpp:4323` in `libwebkitgtk-6.0.so.4`; the layout-test modes
      attach the web process and `WTR::TestController::runTest`
      (`TestController.cpp:3304`, with the test path as an argument); and
      `follow-page` picks the live page out of three processes after a
      cross-site swap, which then renders `c.html`
- [V] the 15-character `comm` truncation, which only GTK reaches:
      `WebKitWebProcess` is 16 characters and the kernel records
      `WebKitWebProces`. `follow-page` matches on `name[:15]` and finds all
      three; lldb's own `--waitfor` matches the full name regardless, so it
      reads the command line rather than `comm`. Matching on `comm` is the
      better of the two here — a `pgrep -f WebKitWebProcess` in the test
      harness also matched the shell that mentioned the name, and `comm` did not
- [!] two things the GTK MiniBrowser does not have: `--headless` and
      `--features`. The first did not matter — GTK loads a page in a screen-off
      session where WPE needed `--headless` — and the second means the PSON
      preference was never reachable there in any case
- [ ] a page that renders. These ran with the session off, where WPE gets no
      output and a page never finishes loading — the `--lldb web` and layout
      runs above used `-- --headless` and the test runner's own surface to get
      a real load. `wk session on` first is what an interactive session wants
- [V] source-level debugging, which is what the release configs now carry debug
      info for. `wk run --lldb` stops in `JSCConfig.h:161` with the source line
      and the inlined frame; `wk gui --lldb` stops at `main.cpp:768` with
      argument values; the web process gives a full source backtrace —
      `Document.cpp:4323` -> `FrameLoader.cpp:1106` -> `FrameLoader.cpp:1030` —
      with WebKit's own summaries rendering `this`

### Babysit — `wk build --babysit` (landed 2026-08-19, zero lines here until now)
- [ ] the E2E: plant a compile error, `--babysit`, disconnect the terminal,
      reconnect — the error is fixed, the build is green, `wk status` showed
      building→fixing→building→ok throughout, and babysit.report says what
      was changed
- [ ] a second `--babysit` while one is alive is refused by pid; a stale
      status file with a dead pid does not refuse
- [ ] a stalled build (exit 124) ends `stalled` and is not handed to the
      model; exhausted attempts end `gave-up` naming the last exit
- [ ] claude failing to *run* ends `error` and does not retry forever
- [ ] `--babysit` refuses inside a workspace, on a remote target, and on the
      local target; `--babysit --dry-run` prints the command and starts nothing
- [ ] `wk status` reports a `fixing` claim with a dead pid as a crash, not as
      progress, and its exit code goes to 1

### Architecture — `--arch armhf` (Linux only)
- [V] `wk new <ws> --arch armhf` creates a native armhf container: `dpkg
      --print-architecture` is armhf, clang targets `arm-unknown-linux-gnueabihf`
- [V] `wk ls` shows the ARCH column; the marker carries `arch=armhf`, which is
      the only way an in-workspace `wk build` can know (`uname -m` says aarch64
      in there, because the kernel is the host's)
- [V] `wk build <ws> jsc-release` builds 32-bit: `CMAKE_SYSTEM_PROCESSOR=armv7l`
      in the cache, the arch flags in `CMAKE_CXX_FLAGS`, gold in the linker flags
- [V] `wk run <ws> -- -e 'print(1+1)'` prints 2 from a 32-bit jsc
      (`file bin/jsc` → ELF 32-bit LSB pie executable, ARM, EABI5)
- [V] the in-workspace form works in there: `wk build jsc-release --dry-run` and
      `wk test --dry-run` name the architecture and put `linux32` in the command
- [V] both debuggers drive the 32-bit process: `wk run <ws> --lldb` stops at
      entry, and lldb/gdb catch the trunk SIGBUS with a backtrace and disassembly
- [ ] `wk test <ws>` — on trunk it hits the known SIGBUS (see
      `docs/HANDOFF-linux-arm32.md`); on a 2.48 branch it is the real test
- [ ] an armhf workspace on `webkitglib/2.48`, where the ARMv7 JIT still exists
- [V] `wk build <ws> wpe-release` — 22m, 32-bit ARM MiniBrowser, links with the
      gold low-memory flags; needs three feature disables the image forces
      (Vulkan, WebRTC, WPE Qt API), now in `arch_cmake`
- [V] MiniBrowser launches headless on armhf; a benchmark through it does not
      complete on trunk (same JSC SIGBUS, in the web process)
- [V] a native workspace's build environment is unchanged: no `WK_ARCH*` in
      `wk build <native-ws> jsc-release --dry-run`
- [V] `--arch` is refused on a non-container target; an unknown arch and
      `riscv64` are refused by name; `--sysroot` is refused on both `wk new` and
      `wk build` with a pointer to the cross-compile handoff

### Benchmark axes
- [V] a gpu-class plan in an armhf workspace refuses, naming the missing GPU;
      a gpu-class plan with a JSCOnly config refuses, naming the missing browser
- [V] a cpu-class plan (jetstream3) with a JSCOnly config runs in the jsc shell,
      merges `--count` iterations into one `result.json`, and compares cleanly
      against another such run (per-subtest table, FDR p-value)
- [V] a cpu-class plan with a browser config runs headless where there is no
      usable display, and records `software_reason` saying which case it was
- [V] `wk bench compare` warns across differing `runner`, `arch` or
      `bench_host`; `wk bench ls` shows the axes
- [ ] the same, in an armhf workspace — needs a branch whose JSC runs, i.e. 2.48
- [V] every run records the provenance that decides whether two of them are one
      series: `host.kernel_arch` (the width of the *kernel*, which `arch` does
      not answer), `host.root_device` (the parent disk, its transport, whether
      it spins and whether it takes a discard), and — for a run in bench mode —
      the `system` that booted and the `profile` it was built from. Written by
      all three record paths: `wk bench` (container), `wk bench staged` (macOS
      bench mode) and `wk pi bench` (a board)
- [ ] `wk bench compare` warns on differing `kernel_arch` and `root_device`, and
      notes a differing `profile` — needs two runs that actually differ on one
      of them

## 2. macOS guest VMs — the `vm` target

### Lifecycle
- [V] `wk vm base` builds the golden base (pull, provision, prebuild)
- [V] `wk vm base --refresh` fast-forwards the checkout without a rebuild
- [V] `wk new <ws> --target vm` clones in ~1 s for ~1 MB (copy-on-write)
- [V] `wk vm start <ws>` boots and writes the ssh alias
- [V] `wk vm ls`, `wk status <ws>` report it
- [V] `wk rm <ws>` removes the guest, host state, alias and registry entry
- [V] `wk vm start` refuses a third running guest with a clear message
- [V] `wk vm start` refuses when memory is already committed, and says by what

### Sandbox
- [V] softnet installed SUID root and tart accepts `--net-softnet`
- [V] the host proxy binds the guest-facing address (waits for the bridge first)
- [V] `tart ip` resolves behind softnet with the default dhcp resolver
- [V] guest reaches `github.com` **through the proxy** (HTTP 200)
- [V] guest is refused a host outside the allowlist
- [V] guest cannot reach the LAN
- [V] guest cannot reach the internet with the proxy bypassed
      (the real test: it proves softnet, not just the env vars)
- [V] guest cannot reach an external DNS resolver
- [ ] guest resolves names only through softnet's resolver — known, accepted,
      and the one channel a container does not have
- [V] `wk vm start` says so loudly when softnet is missing
- [V] the guest has no view of the host filesystem
- [V] `wk claude` reports which of the two it is before starting

### Screen, GPU and egress  (macOS MiniBrowser lane)
- [V] the guest boots **windowed** -- `tart run` carries no `--no-graphics`
- [V] a host window exists and is onscreen (1920x1108 incl. title bar)
- [V] the guest has a real Metal device: `Apple Paravirtual device`,
      3.6 Gthread/s vs 5.9 on the host -- hardware, not a software rasteriser
- [!] the paravirtual GPU is feature-capped: families apple1-5 only, **no
      metal3, no raytracing**. Fine to interact with, NOT a basis for judging
      WebGPU or rendering performance against bare metal
- [V] MiniBrowser runs with hardware WebGL: `UNMASKED_RENDERER=Apple GPU`
- [V] the GPU process loads `AppleParavirtGPUMetalIOGPUFamily` + WebKit's ANGLE
- [V] all four processes start (MiniBrowser, GPU, Networking, WebContent)
- [V] the guest reaches 1920x1080, not the stock 1024x768
- [V] the guest never sleeps: `org.wk.nosleep` holds a power assertion and
      survives reboot (pmset alone was **not** sufficient -- measured)
- [V] the guest never locks: `sysadminctl -screenLock off` (a setting separate
      from both the screen saver and display sleep)
- [V] the guest comes up on a live desktop with no password prompt
- [V] guest egress reaches the allowlist: webkit.org, browserbench.org,
      igalia.com, gnome.org, google.com, wikipedia, reddit -- all answer
- [V] guest egress still refuses a host outside it (example.com,
      news.ycombinator.com both fail closed)
- [V] `webkit.org.evil.com` is refused -- suffix match is on a dot boundary
- [!] the **base VM** provisions with *unfiltered* egress: Softnet's flags are
      passed in `t_start` only, so the base boots on plain vmnet (192.168.64.x)
      with the open network -- `curl https://pypi.org` direct answers 200. The
      egress block it writes names the Softnet gateway and is for the clones.
      One-shot and host-driven, with no agent in it, but the sandbox audit
      should record it as a decision rather than find it
- [V] **MiniBrowser reaches the allowlist too.** It did not until 2026-08-18:
      the guest's egress was `http_proxy` in `~/.zprofile` and nothing else,
      which WebKit's network process does not read -- a `--url
      https://webkit.org/` load produced a blank window and no proxy-log entry
      at all, while curl from the same guest went through. `wk vm start` now
      sets the guest's *system* proxy. Verified by the log, not the window:
      `allow webkit.org:443`, `allow shynet.webkit.org:443`, page rendered
- [V] the allowlist still applies to the browser: `token.safebrowsing.apple`,
      `metrics.icloud.com`, `gdmf.apple.com` and the rest of macOS's own
      traffic are denied (and now appear in the log on every boot)
- [ ] the tart window resizes / goes fullscreen  **-- KNOWN BROKEN**
- [!] the guest runs the latest macOS  **-- 26.4 vs host 26.6.1. PARKED: no
      suitable image exists upstream (re-checked 2026-08-18) and the only
      symptom is `open -a`, which nothing uses. Do not spend time here.
      Upgrades are by rebuild only (`WK_VM_IMAGE` + a base rebuild); one
      command re-checks the available tags, no `tart pull` required:
      `T=$(curl -s "https://ghcr.io/token?scope=repository:cirruslabs/macos-tahoe-xcode:pull&service=ghcr.io" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'); curl -s -H "Authorization: Bearer $T" https://ghcr.io/v2/cirruslabs/macos-tahoe-xcode/tags/list`**
- [ ] `open -a` inside the guest  **-- KNOWN BROKEN**, LaunchServices -10825:
      the app targets the 26.5 SDK, the guest is 26.4. Use direct bundle exec

### Debugging  (macOS MiniBrowser lane)
- [V] lldb attaches to an ordinary process in the guest: no `taskgated` prompt,
      no codesigning work -- SIP is **disabled** in the VZ guest and
      `DevToolsSecurity` already reports developer mode enabled
- [V] lldb attaches to the WebContent XPC service and resolves its symbols
- [V] `~/.lldbinit` is written into the guest by `targets/vm.sh` at start, and
      registers WebKit's summaries (15 of them: `WTF::String`, `JSC::JSValue`,
      ...); Xcode's lldb accepts every setting in `dotfiles/lldbinit` unchanged
- [V] `wk run --lldb -- -e 'print(6*7)'` on `mac-release`: an interactive lldb
      with a pty, stopped at entry, prints 42 on `continue`
- [V] `wk gui <ws> [url]` launches MiniBrowser on the guest's desktop -- from
      the host, and from **inside** the guest with no workspace name
- [V] `wk gui --lldb` stops in `main` (`main.m:33`) with source, before AppKit
- [V] `wk gui --lldb web` attaches the web process as it launches
- [V] `wk test --layout --lldb <test>` attaches the web process WebKitTestRunner
      spawns, and the run reaches "The test ran as expected." after `continue`
- [V] `wk test --layout --lldb ui <test>` attaches WebKitTestRunner itself
- [V] a mistyped test path fails before the debugger starts (`no such test in
      <ws>`) rather than hanging with lldb waiting for a process that will
      never launch
- [V] a breakpoint by name resolves and hits in the layout-test web process:
      `WebCore::Document::implicitClose` at `Document.cpp:4323`, with source and
      with WebKit's summary printing `this`
- [!] ...but resolving it also prints ~100 `llvmcas:/... does not exist`
      warnings. The explicit Swift `.pcm`s record their inputs as CAS ids that
      `llvm-cas --print-kind` calls "unknown object" -- **including in a CAS
      built minutes earlier**, with the same id byte for byte, so it is not
      eviction: those objects were never in the compilation cache. Not an lldb
      setting either (`symbols.cas-path` measured, no effect). Only
      Swift-interop types are degraded
- [V] `WK_NO_COMPILATION_CACHE=1` clears them completely: 103 -> 0 llvmcas
      warnings, 5 -> 0 missing modules, breakpoint still hits -- and the cold
      build is *faster* without the cache, 68.6 min against 85.8 min

### Build and run
- [!] a mac build must reach **ImageDiff**, not just `BUILD SUCCEEDED`.
      `-derivedDataPath` broke the second of build-webkit's two xcodebuild
      invocations (`build-imagediff`, which has no `-scheme`), so the prebuild
      ended `** BUILD SUCCEEDED ** [5149 sec]` + exit 64 + no ImageDiff -- and
      every pixel/reftest comparison needs it. Fixed by dropping the flag;
      check `WebKitBuild/Release/ImageDiff` exists after a build
- [V] `wk build <ws> mac-release` succeeds
- [V] `wk run <ws> --config mac-release` runs jsc via `DYLD_FRAMEWORK_PATH`
- [ ] a build in a **fresh clone off a warm base** completes in well under 45 min
- [V] a **cold** base prebuild: 85.8 min (5149 s), -j9, with
      `--export-compile-commands` on -- against SETUP.md's ~99 min without it,
      so the flag is not the cost it was feared to be (not a clean A/B: the CAS
      moved and the tree was newer)
- [ ] `wk build <ws> mac-debug`
- [ ] `wk build <ws> ios-sim-release` — config written, never run
- [V] `wk test` against an Apple port -- **was impossible until 2026-08-18**:
      `run-webkit-tests` is python, imports webkitpy, and webkitpy autoinstalls
      from PyPI, which the guest could not reach (see the egress fix below).
      First real run: one layout test, green, 15 s including webkitpy start-up
- [V] the guest disk fits a build (Release tree ~39 GB) with room for a second

## 3. Remote target

Verified 2026-08-19 against `devbox-arm64-2` (80 cores, 250 GB, Debian 12,
reached through a ProxyJump), driven from the macOS host.

- [V] a machine name is a target: `--target devbox-arm64-2` resolves through
      `targets/hosts/<name>.conf` in this repository, or
      `~/.config/wk/targets/<name>.conf` for this device alone
- [V] an unconfigured name is refused, and the error prints the conf to write
- [V] two remote targets in one process do not inherit each other's host,
      root or capacity
- [V] `wk new <ws> --target <machine>` clones on the shared box (39 s from a
      warm mirror; the mirror's own first fetch is 25 min / 13 GB)
- [V] the mirror is created and fetched by the driver on `wk new`
- [V] `wk sync --target <machine>` is the far-side equivalent of `wk sync`, and
      is what refreshes a machine between builds: it pushes wk-tools (the stale
      copy that answers a delegated command with `unknown option --quiet` is
      the failure this closes — `wk status` named the drift and nothing but a
      full `wk remote setup` fixed it) and fetches that machine's own mirror.
      Verified 2026-08-19 on buildbox4: 11 s, and `wk status` went from
      "DIFFERS from the workstation" to "in sync"
- [V] a machine that clones from a shared repository in its MOTD is told so and
      nothing of ours is fetched — that repository is the sysadmins', not ours
      to write to
- [V] `wk sync --target all` walks every machine; `wk sync --target container`
      (or vm, or local) is refused, naming a plain `wk sync` as the thing that
      refreshes this machine's own store
- [V] `wk build` runs niced 19 + ionice, job count from **that machine's**
      cores, load and free memory (80 cores / load 1 / 248 GB → -j16 when
      measured; the configured ceiling was later removed); jsc-release in 8m47s
- [V] `wk build --dry-run` resolves remote paths: checkout, build dir, tools,
      and a ccache under the remote root rather than the container's /ccache
- [V] two builds serialise on the build lock (`lib/lockrun.sh` on the machine
      that builds; a second taker is refused while a build runs)
- [V] `wk status` with no argument includes remote workspaces on a macOS host
      (it used to forward into the podman VM and report containers only —
      which silently hid the `vm` target too)
- [V] `wk enter <ws> <cmd>` runs the command in the remote checkout
- [V] `wk enter <ws> --zed` and `wk new <ws> --target <machine> --zed` open the
      checkout over ssh, and work when Zed.app is installed without the `zed`
      CLI symlink (the app bundle's own cli is used). The URL for a machine is
      that machine's own ssh name and its real path
      (`ssh://buildbox4/home/…/wk/ws/bb4/WebKit`, checked 2026-08-19)
- [ ] `wk new --zed` warns instead of failing when zed is missing or the
      workspace has no route yet (a vm before first boot) — the workspace is
      created either way
- [V] `wk claude` refuses a remote workspace outright
- [V] forced onto a remote with `--force`, Claude starts in **auto mode**
      (`--permission-mode auto`), not `--dangerously-skip-permissions`: skipping
      permissions is the sandbox's bargain, and a shared build box is the one
      target with no sandbox — forcing past the barrier says "run an agent
      there", not "and give it everything"
- [V] forced onto a remote with `--force`, it hands over only to a Claude that
      *runs*: on a shared box the CLI on PATH is often somebody else's global
      npm install on that machine's own node, which dies on start (measured
      2026-08-19 on devbox-arm64-2: node v16.19.0, "ReferenceError:
      ReadableStream is not defined"). `wk claude` probes by running
      `claude --version`, prints what it found and that machine's node version,
      offers the user-local installer that carries its own runtime, and
      launches by absolute path — preferring `~/.local/bin/claude`, because
      PATH ordering on a shared machine is not ours to change
- [V] **layout tests run on a remote target against the build that is already
      there** — no second build, no second config. `wk test db --layout
      fast/dom/Element/id-attribute.html`: 15 s, green, 2026-08-19 on
      devbox-arm64-2 against its `gtk-release-asan` tree. Two things had to be
      true for it: `--no-build` (always was), and the config defaulting to what
      the workspace was last built with (`config=` in build.status) — a bare
      `wk test --layout` used to resolve `jsc-release`, i.e. `--jsc-only`, and
      point run-webkit-tests at a tree with no WebKitTestRunner and no
      ImageDiff in it. That combination is now refused by name
- [V] `wk claude <ws>` takes Claude's own options on either side of the
      workspace name: `wk claude -r db` and `wk claude db -r` are the same
      command. Which bare word is the name is decided by the registry, not by
      position, so `-r <session-id>` does not lose its argument to it
- [V] `wk verify` refuses a remote workspace outright, rather than reporting a
      sandbox that does not exist
- [!] a **trunk** build needs a newer C++ toolchain than Debian 12 has: clang
      18 with libstdc++ 12 has no `<format>`, which `Source/WTF/wtf/
      FormattedLogging.h` has required since 2026-06-16. The box builds
      releases up to 2.52.x; trunk there needs the container SDK
      (`docs/Nice to have/HANDOFF-cross-compile.md`), which is already installed on it
- [V] `wk run` executes the remote build's jsc (stderr carries whatever noise
      the box's own login shell prints — a shared machine's dotfiles are not
      ours to fix)
- [V] `wk rm` removes the remote checkout, the local state and the registry
      entry, and leaves the mirror, the ccache and the pushed tools alone
- [V] `t_list` answers in the contract's `<name>\t<state>` form

### Provisioning and use on the machine itself (`wk remote setup`)
- [V] `wk remote setup <target>` probes and reports the machine, and offers to
      write the target conf when there is none
- [V] nothing in the remote path uses sudo; prerequisites are checked, and a
      missing `git` is fatal while a missing zsh or ccache is a warning
      (`flock` was a prerequisite until `lib/lockrun.sh` replaced it)
- [V] zsh: interactive bash sessions move to it through `shell/bashrc`, with no
      `chsh` and no root
- [ ] a machine with no zsh warns and stays in bash — written, and now with no
      machine here to exercise it (buildbox4 has zsh since 2026-08-19)
- [V] the stale `wk-tools/bashrc` source line is removed from `~/.bashrc`,
      `~/.zshrc` and `~/.bash_profile` — it was printing three
      `setopt: command not found` errors into every interactive shell
- [V] cleanup candidates are listed with their size and **declined** when there
      is no terminal; nothing is removed unattended
- [ ] a cleanup candidate accepted at the prompt is actually removed
- [V] `wk remote setup` works on a machine that has never seen wk: the tools
      push creates the remote root itself (rsync makes the last path element
      but not a missing parent — buildbox4, 2026-08-19)
- [V] `wk remote rm <target>` undoes provisioning: the rc lines go from
      `~/.zshrc`/`~/.bashrc`/`~/.bash_profile` (the machine's own rc content
      untouched), the marker and machine-side conf go, the remote root goes
      only after a size-attached confirm, and the local conf goes last
- [V] `wk remote rm` then `wk remote setup` round-trips: the box comes back
      identical (buildbox4, 2026-08-19)
- [V] `wk remote rm` refuses while workspaces are still registered to the
      target, naming them and `wk rm`
- [V] `wk remote rm` against an unreachable machine offers to remove only the
      local conf and says the machine keeps what it has; declining changes
      nothing
- [V] `wk` is on PATH on the box: `ssh box bash -lc 'wk ls'` works
      (a plain `ssh box wk ls` cannot, and needs root to fix)
- [V] on the box: `wk ls`, `wk status`, `wk build`, `wk run`, `wk logs`,
      `wk enter` act on the machine directly, with no ssh to itself
      (`wk build zz jsc-release` there: 2m30s, 31.5% ccache hits)
- [V] on the box, `wk new` and `wk rm` refuse: a build machine holds workspaces
      and does not own them, and the registry that says which machine a
      workspace lives on is the workstation's
- [V] on the box, `wk sync` / `gc` / `vm` / `pi` refuse and say why
- [V] an empty listing on the box points at the workstation, not at a `wk new`
      that would refuse
- [V] `wk status`/`wk ls` list what is on a configured machine even with
      nothing in the registry
- [V] the build state is the machine's: a build driven from the box reports
      identically on the workstation (`build=failed (jsc-release) 0m45s`), and
      one driven from the workstation reports identically on the box
- [V] `wk status` and `wk logs` ask the machine and bring its **exit status**
      back with it (1 for a failed build, from either end)
- [V] a bare `wk status` on the workstation reaches every configured machine
      and its worst state becomes the command's exit code
- [V] the canonical log has one writer: no duplicate lines when the build is
      driven from the machine itself
- [V] a machine that has not been provisioned falls back to the local
      transcript instead of failing
- [V] a workspace is cloned from a WebKit repository the machine advertises in
      its MOTD, hardlinked: `.git` costs 69 MB against a 13 GB source
- [V] an advertised path that does not exist is rejected rather than used —
      buildbox4 names `/var/git/WebKit.git` and has no such directory

## 4. Cross-cutting
- [V] a workspace remembers its target; only `wk new` needs `--target`
- [V] `wk status` with no argument walks every target — including, since
      2026-08-19, the host-side ones on a macOS host: it forwarded into the
      podman VM and reported containers only, hiding every `vm` and remote
      workspace behind an exit code that looked fine
- [V] `wk build --list` shows all configs
- [V] `wk build --list` answers on a macOS host **with the podman machine
      stopped**, and leaves it stopped (26 ms; it used to boot the machine)
- [V] `wk gui` on a macOS host refuses a *container* workspace with the reason
      and the alternative, instead of forwarding into the podman VM to fail a
      seat check and advise `wk session on`, which refuses on macOS
- [V] `wk ls` and `wk status` no longer boot the podman machine on a macOS
      host -- they say it is stopped and point at `wk start` / `wk vm ls`.
      Booting it used to cost a macOS guest its memory budget: `wk vm start`
      then refused, because both want the whole envelope
### The listing's shape — one document, three views
- [V] `wk status --json` is the listing as one document, and the terminal table
      and the page are rendered *from* it (`lib/status-view.py`) rather than
      printed alongside it — so a number that appears in one and not the other
      is impossible rather than merely unlikely. Measured 2026-08-24: four
      machines, five workspaces, the fleet and the bridges, exit 4
- [V] the collectors emit **records** — one JSON object per line — and the
      renderer merges them by machine name. That is what makes a macOS listing
      work: it is assembled by two processes (this machine's targets out here,
      its containers inside the podman VM) and two JSON documents cannot be
      concatenated where two streams of lines can. It also retired the flag the
      two halves used to pass between them to agree on who had printed a heading
- [V] `wk status` **names each machine once** and groups what is on it by
      method: the machine as a heading, `container` / `macOS guest` / `native`
      under it, workspaces in a table under that. It used to squash both into
      one `moose:container` column, and this Mac's own containers were printed
      after every shared build machine because they come from the forwarded half
- [V] the columns line up across the **whole** listing, not per group: one set
      of widths, computed after the last machine has answered. Per-group widths
      made every block line up with itself and with nothing else, so the eye had
      to find the columns again at every heading
- [V] a machine running an **older** copy of wk-tools cannot answer in records —
      it rejects `--records` as a workspace name — so it is asked again in the
      vocabulary it has and its own listing is carried as one `raw` block, under
      its heading, with a note naming `wk remote setup <machine>`. Measured
      2026-08-24 against buildbox4 and devbox-arm64-2, both on an older tree
- [V] the fleet block is printed **once**. The forwarded half used to print a
      second one, from inside the podman VM, saying "unknown from here" about
      every board in it — the devices are reached over ssh from the host and the
      VM can see none of them. It runs with `--no-devices`, which keeps the half
      of the machine facts that *is* in there: the copy of wk-tools every
      container bind-mounts, and the keys in that store
- [V] `wk status --json` covers everything a bare `wk status` covers. A flag is
      not a subject: counting arguments to decide what to forward — which is
      what the dispatcher did — sent `--json` down the forward-it-whole path, so
      it answered for this machine's containers and silently left out its guests
      and every build machine (`bare_subject` in `wk`)
- [V] `wk status --web` serves the page on **127.0.0.1 only**, opens a browser,
      and keeps it current: the page polls this process every 1.5 s and this
      process re-runs the walk every `--interval` seconds (default 20, minimum
      5). A page that re-ran the walk per request would put a browser's refresh
      rate onto a phone-linked Pi. Measured 2026-08-24 on port 8792
- [V] the page is self-contained — no CDN, no font host, no framework. A page
      served from a laptop to look at a fleet must not depend on the network the
      fleet is the reason to be worried about
- [V] `wk status --html [file]` writes that page once and serves nothing
- [V] a port already in use is a refusal naming the likely cause (the last
      `--web` still running, whose page is already live) rather than a traceback
- [V] when the server stops, the page says so unmistakably: the whole thing goes
      grey with a red frame and the footer reads "this listing is frozen". A page
      that has stopped updating looks exactly like one that is current, and this
      listing is the thing people act on
- [ ] the page's own refresh survives the fleet being slow: a walk that takes
      longer than the interval must not stack up walks
- [ ] `NO_COLOR` and a redirected stdout both drop the colour from the table

### What a machine is, apart from the workspaces on it
- [V] **disk, where the store actually lives**: percentage used (green under 75,
      yellow to 89, red at 90 — the thresholds are "will the next build fit",
      not tidiness), free and total, the snapshot count, and how many `wk gc`
      would reclaim. A macOS host reports two, labelled: the podman VM's store
      (where builds fill a disk) and its own (guests, Tart images, the VM's disk
      image). `df`, never `du` — walking a 200 GB store to answer would cost
      more than the build
- [V] **the egress proxy**, where there is a unit to ask about. If it is down
      every workspace has no network and every build fails as a hundred
      unrelated fetch errors; the line names `systemctl --user start wk-proxy`
- [V] **the locks held right now**, with the holder's pid and command — and
      `held` against `stale` for one whose holder is gone, because a lock dies
      with its holder and the next taker breaks it. "Waiting for the store lock"
      had no way to find out what was holding it
- [V] **the push switch in words** (`on` / `off` / `partly on` / `no keys`, with
      the count live and held back), per store — it was inferable only from the
      phrase "held back" on a key line
- [V] **capacity**: load against cores, free against total memory, per machine —
      what decides where to send a build. On a macOS host both the VM's and the
      Mac's are reported and labelled, because 9 cores and 10 cores are both
      true about different things
- [V] **quiesced or not**: a machine left pretending to be a measurement
      instrument (caffeinate, paused daemons, the raiser) says so, with
      `wk quiesce off` named. That state is invisible and it changes every
      number taken on the machine
- [V] **the newest benchmark run**, and whether it produced a result: a run
      directory with `env.json` and no `result.json` is either happening now or
      it died, and either way it is a machine not to touch
- [ ] the health block is empty and silent on a machine that has none of these
      (no store, no proxy unit, no locks)

### What is *in* a workspace, before anything destructive
- [V] every workspace row carries **work that exists nowhere else**: commits not
      pushed, dirty files, untracked files — or `clean`. The model here is
      wipe-over-repair (`wk rm` and remake), and until now the listing consulted
      before doing that said nothing about what would be lost
- [V] and **how far the checkout has drifted**: `main ↓424` against its upstream,
      `↑` for commits ahead. Measured 2026-08-24
- [V] and **which snapshot it is standing on** (`SNAP -2`): the overlay's lower
      layer is pinned at creation, so this is the honest answer to "I ran
      `wk sync` and nothing moved"
- [V] all of it in **one exec** per workspace — the same round trip the origin
      check already paid for. Measured in a WebKit checkout: `status -uno`
      524 ms, `ls-files` 395 ms, both rev-lists 47 ms
- [ ] a workspace whose exec fails (stopped container, guest not booted) shows
      the row without the extra fields rather than an error

### Every machine says how to reach it, and where it was declared
- [V] each machine, fleet device and bridge carries **how it is reached now**
      (its tailnet address and whether the tailnet says it is up) and **a path
      without tailscale** — and both are calculated at read time, never stored
      (`lib/reach.sh`). Measured 2026-08-24: moose `100.84.25.21 (up)` /
      `192.168.1.40 (mDNS moose.local)`; buildbox4 not on the tailnet at all and
      `jmichaud@buildbox4 (through igalia.com)` from `ssh -G`; rpi4
      `10.99.1.10 (through tailnet-bridge-generic)`, composed from the board's
      `MACH_BRIDGE` and that bridge's `BR_LEASES`; rpi5 tailnet-only, and the
      listing says nothing rather than inventing a second route
- [V] the derivations are ssh's and the tailnet's own: `ssh -G` resolves the
      config (Host blocks, Includes, ProxyJump) without connecting, and
      `tailscale status --json` is asked once per run. Neither is a table in
      this repository, which is the rule the fleet has (CLAUDE.md, "a node is
      reached by its tailnet name and how to reach it is not written down") —
      and the question it left unanswered, "and when the tailnet is down?", now
      has a computed answer
- [V] every line that came out of a conf names the conf it came from, relative
      to the checkout: `targets/hosts/buildbox4.conf`, `boot/machines/rpi4.conf`,
      `bridge/hosts/<name>.conf`. A machine declared twice (the registry and this
      device's own view) names both files
- [ ] mDNS is the only lookup here that goes on the wire, and it is bounded: a
      `.local` name nothing answers for must cost 2s, not the resolver's default

### The bridges are probed, not just declared
- [V] each bridge is asked, in parallel and under a ceiling of its own: is it
      reachable, does it carry the role, what does its own health check say, and
      is the role **this repository's**. Measured 2026-08-24:
      tailnet-bridge-generic `up`, "All checks passed.", role older than the
      repo (49386 bytes installed against 49940 here) with
      `wk bridge setup tailnet-bridge-generic` named; the BMC bridge unreachable
- [V] "up to date" is a checksum of the files provision.sh installs
      (`bridge/bin/*` then `bridge/init.d/*`, in that order on both sides) rather
      than a version somebody has to remember to bump — the same trick the
      wk-tools tree hash plays, for the same reason
- [V] the probe's ceiling is its own (20s, `WK_BRIDGE_TIMEOUT`) and larger than a
      fleet device's, because it runs a health check on a phone: at the fleet's
      4s the root attempt was killed and came back **empty** — a remote shell
      buffers its stdout when it is not a tty — which read as "root does not
      work here" and sent every probe down a doas path that needs a tty
- [V] a health check that cannot run for want of privilege is reported as
      "role installed" with the reason, not as "unhealthy": that is a verdict
      about this end's route, not about the bridge
- [ ] a bridge that is on the tailnet but whose *segment* is down is
      distinguishable from one that is simply off

### Opening a workspace in an editor — `wk zed`
- [V] `wk zed <ws>` opens the checkout, whatever the target is, and
      `wk enter <ws> --zed` / `wk new <ws> --zed` are the same launch: they call
      it rather than carrying a copy
- [V] a stale copy of wk-tools names the command that fixes it, and which
      command that is depends on what kind of copy it is: `wk sync --target
      container` for the tree inside the podman VM, `wk sync --target <machine>`
      for a build box, and a `git pull` over there for a peer workstation, which
      runs a checkout of its own that nothing here pushes to. It used to say
      "push it there" and name nothing
- [V] `wk zed --tools` opens this checkout locally (a path, not an ssh url to
      the machine you are sitting at); `wk zed --tools <machine>` opens that
      machine's copy — `ssh://buildbox4/home/…/wk/tools`, checked 2026-08-24 —
      and `wk zed --tools <ws>` the `/opt/wk-tools` a workspace bind-mounts
- [V] inside a workspace it refuses: **there is no Zed in here**, and it names
      the command to run on the workstation instead. Encoded in
      `wk selftest --section state`
- [V] Zed's own upload path works through the transport: it tries to fetch its
      server binary *inside* the workspace first (no DNS in there, correctly
      refused), then uploads the 36 MB binary over sftp and runs it — measured
      2026-08-24, `/src/WebKit` opened with 109,621 entries and three
      zed-remote-server processes alive in the container. Two things had to be
      true for that: `internal-sftp` rather than the sftp-server binary (an
      external subsystem is started through the login shell, and bash run by
      sshd reads `~/.bashrc`, which ends in `cd /src/WebKit` — so an upload to
      `~/.zed_server/…` failed against a directory that was plainly there), and
      the same `cd` guarded on an interactive shell in `container/firstrun.sh`
- [ ] a language server that wants the network from inside a workspace is
      refused by the allowlist and says so rather than hanging (Zed's ACP
      registry fetch does exactly this today; nothing has been added for it)
- [ ] `wk new --zed` warns instead of failing when the launch cannot happen —
      the workspace is created either way, and its exit status says so
- [ ] `wk rm <ws>` takes the `Host wk-<name>` alias with it, ProxyCommand and
      all, and a later `ssh wk-<name>` is an unknown host rather than a hang.
      On a macOS host that removal happens **out here**: `wk rm` of a container
      is forwarded, and the forwarded half would strike an entry out of the
      VM's own `~/.ssh`, where nothing reads one

### The prompt — what a shell says before you type
- [V] the shared rc sets a prompt carrying the machine, what that machine
      currently is (`host` / `bench` / `shared` / `ws`), the working directory,
      the git branch, and the exit code of the last command when it was not 0.
      Measured 2026-08-24: `tolken:host ~/Development/wk-tools main* ❯` on the
      host, `238-tolken-backports:ws …` inside a workspace (bash there — the
      image has no zsh, and the fallback carries the same facts)
- [V] it forks nothing it does not have to: the machine, the role and the
      workspace are read once at rc load, and the branch comes from `.git/HEAD`
      rather than from `git rev-parse`. ~42 ms per prompt in this checkout, and
      all of it the one `git diff --quiet` that decides the dirty mark
- [V] the dirty mark is honest: `*` means git was asked and said yes, `?` means
      it was **not** asked because the index is over 20 MB — which is every
      WebKit checkout, where the answer costs seconds. Never a blank where a
      `*` might belong
- [ ] a machine in bench mode says `bench` (the `/etc/wk-image` marker, the same
      evidence `wk boot --status` reads) — untested on a booted bench system

### `wk sync` — the master copy, then everything that clones from it
- [V] a plain `wk sync` fetches **every remote** — origin, wpe, fork and forkwpe.
      The old default of origin-alone-unless-`--all` meant the mirror was
      reliably missing whichever branch somebody was about to want; `--all` is
      still accepted and now asks for what already happens
- [V] the mirror is mounted read-only into every new container at `/mirror`, and
      a workspace's fetch comes from **there**: no network, and the objects are
      already on the disk. Measured 2026-08-25 on a fresh workspace —
      `ok (from the mirror)`, and 586 remote-tracking refs afterwards (242 wpe,
      226 forkwpe, 116 fork, 2 origin) with the egress proxy untouched
- [V] read-only, and that is the safety argument: the mirror is what every base
      snapshot is cloned from, so a workspace that could write it could change
      what every future workspace builds
- [V] a workspace made before that mount says so rather than failing — it falls
      back to its own remotes over the proxy and the line reads
      `ok (over the network: this workspace has no /mirror -- remake it to get one)`
- [V] and then every workspace fetches too, which is the half that was missing:
      a new snapshot does nothing for the workspaces that already exist, since
      they stay pinned to the one they were made on. Measured 2026-08-24:
      mirror (origin ok, wpe ok) → snapshot published → `238-tolken-backports ok`
- [V] `wk sync <workspace>` does the same for one of them; a name that is not
      there is refused rather than skipped
- [V] a workspace that is not `present` costs a line saying which state it is in,
      and not the run; the same for one whose fetch fails
- [V] only the remotes a workspace actually has are fetched: one made before
      `wpe` was wired in has no such remote, and naming it would fail the whole
      fetch over a remote that was never there
- [ ] `wk sync` inside a workspace is still refused, naming the host command
- [ ] the refspecs follow the mirror's own layout: origin's branches are its
      heads (a clone ignores refs/remotes, so a mirror that filed origin there
      would clone into a repository with no branches) and every other upstream is
      namespaced. A fifth upstream must need no change here beyond `wk_remotes`

- [ ] `wk backup` → `./setup` round-trips with no spurious changes
- [ ] `wk backup`'s junk filters strip what they claim (weather location,
      WiFi UUIDs, last-folder paths, timestamps)
- [ ] `wk skills` status/diff/pull/push; pull refuses over uncommitted repo edits
- [ ] the skills are workspace-true: an agent started by `wk claude` in a container
      and in a macOS guest can follow every skill it can trigger without hitting a
      host-only instruction (2026-08-24 sweep: build guidance has one owner and
      opens with `wk build`, `wkdev-enter` and the `wkdev32`/`/sdk/webkit` paths
      are gone, pinning is `wk quiesce on` with a labelled `uclampset` degraded
      mode, samply is found on PATH, `rpi3` names the board instead of asking for
      an IP)
- [ ] `wk quiesce on` disables App Nap for MiniBrowser and starts the raiser; `off`
      restores both, and `off` also undoes a session started under the old
      `$TMPDIR` state directory
- [ ] `wk key register` / `check`
- [ ] `wk pi setup rpi4`, and a workspace can reach the Pi (the rpi5 is a
      workstation and never goes through `wk pi setup`)
- [ ] `wk enter <ws>` lands in a shell; `wk enter <ws> <cmd>` runs the command
- [ ] `wk status <ws> --wait` blocks while the workspace is busy and reports once
      when it is not, with the same exit code a bare `wk status` would give;
      `--timeout S` stops waiting and says so without claiming the work stopped
- [ ] `wk logs <ws> -f` follows a live build
- [ ] `wk stop --keep-vm` leaves the podman machine running
- [ ] `wk gc` also prunes a creation record whose workspace, environment and
      registry entry are all gone, and keeps one whose creation is still in
      flight (exercised 2026-08-24 with a planted orphan; the in-flight half is
      untested)
- [V] `wk status`'s fleet and bridge *headings* go to stderr and its rows to
      stdout, like every other heading here — they were both on stdout, which
      put "fleet" and "tailnet" into the workspace-name set anything parsing the
      command reads, and `wk selftest --section state`'s ls-and-status agreement
      check had been failing on exactly that (found and fixed 2026-08-24)
- [ ] one liveness rule: `build_live` (lib/detach.sh) answers for both
      `wk status` and `wk bench`'s preflight — a `state=running` file whose log
      has not moved for `WK_STALL_SECONDS` is not live, so a `kill -9`'d build
      no longer refuses every later benchmark
- [ ] one headless marker: `headless_markers` (lib/resources.sh) is read by
      `is_headless` and by the Linux machine stage that removes it, so a marker
      in either spelling is seen by both
- [ ] `wk vm rm` removes `<name>.unfiltered`, so a recreated guest of the same
      name is not refused by `wk claude` for the previous guest's sins
- [ ] one ccache ceiling: `ccache_conf_render` (lib/store.sh) renders it for the
      store and for a remote machine's cache, and neither overwrites a config
      that is already there
- [ ] `wk gc` prunes an unreferenced snapshot, keeps the newest, trims ccache,
      removes a stale bench payload seed, and reports the dirs it keeps
- [ ] `wk sync --all` and `WK_MIRROR_BRANCHES` carry the extra branches
- [V] `wk pr [ws] <user>:<branch>` checks out a PR head from **either**
      WebKit or WPEWebKit — the project is found by asking both forks for the
      branch, and a name in both is refused rather than guessed
      (2026-08-19: `justinmichaud:eng/non-cocoa-fuzz-Frame-cache-…`, 4 s on a
      re-run, reusing the existing `fork` remote rather than adding a second)
- [V] it warns about nothing except your own work: uncommitted changes are
      named, and a local branch with commits the PR head lacks is checked out
      as it is, never reset — `--force` takes the head and says what it
      discarded, twice
- [V] a branch in neither project is refused naming both URLs it checked
- [V] `wk status` shows each workspace's current branch, for every target
- [ ] `wk pick <ws> <id>@main` resolves the identifier without the network and
      picks it: the arithmetic (`main~(count - N)`) agrees with the commit's own
      `Canonical link:` trailer, a deliberately wrong id is reported `unresolved`
      rather than picked, a dirty tree is a barrier, and a conflict leaves the
      sequencer for `git cherry-pick --continue`
- [ ] `container/bin/` helpers on PATH in a workspace: `git-clean` (empty repo,
      all-untracked tree, and a tracked modification — each ends `clean` and
      exits 0), `commit-count`, and `git-sync-fork` (refuses when the fork's
      main is ahead; fast-forwards otherwise; says so plainly when `wk push` is
      off)
- [ ] **the PR workflow, end to end and as one flow**: sandboxed agents driving
      builds while a person pushes, rebases, fetches forks and uploads PRs —
      `wk push on|off`, `wk remotes --fix`, `wk pr`, `wk pick` and
      `git-sync-fork` all in the loop, including making a PR from an armhf
      container where `git-webkit` cannot run. Every piece is verified alone;
      this is what proves they compose
- [ ] `wk report` prints the weekly summary (needs gh auth)
- [ ] the MCP server (`wk mcp`) creates and destroys a workspace from Claude
      Desktop, and refuses past its workspace cap
- [V] `wk help` lists every cmd/* entry (no orphan commands, no dead entries)
- [V] `wk version` prints a tree hash that is identical on the workstation and
      on every machine its tooling was pushed to — the git sha is only
      available where there is a checkout, so the tree is the comparable one
- [V] `wk status` flags a machine whose wk-tools differs from this machine's,
      by name, and says "in sync" when it does not — the skew that produced
      `unknown option --quiet` from a command that works here
- [V] every command answers `--explain` without running anything: what it does
      (its own header comment), whether it changes anything, what machine it
      acts on, and how to preview it — answerable with the podman machine
      stopped, inside a workspace, and on a shared build machine, because it
      is a question about the command rather than a use of it
- [V] an unknown command prints the usage and exits 2
- [V] host-only commands refuse inside a workspace rather than acting on an
      empty store — `wk sync` in there would fetch a 13 GB mirror into a
      directory that is discarded with the workspace
- [V] `wk doctor` runs read-only (does not start the podman machine or a
      guest), reports ok/--/?? per item, and exits 1 when something is missing
- [ ] `wk doctor` on a freshly set-up machine reports everything ok, and each
      `--` line's printed fix actually clears that line when run
- [V] every shell file parses under both bash 5 and bash 3.2
- [V] `wk selftest --quick` passes on a set-up machine, needs no workspace, no
      podman and no ssh, and starts nothing — in particular it leaves the
      podman machine exactly as it found it
- [V] `wk selftest` reports DRIFT when a check's plan line is reworded or
      removed, so the runner and this file cannot part company quietly

### Profiling — `wk profile`
- [V] `wk profile` composes the right environment for each port (2026-08-20,
      in `wk selftest --quick`): `DYLD_FRAMEWORK_PATH` for an Apple config and
      `LD_LIBRARY_PATH` for a CMake one, JSC options *before* the script rather
      than after it (jsc treats everything after the file as the script's own
      arguments, so an appended `--sample` turns the profiler off in silence),
      and `--mode native` resolving to xctrace on the Apple ports and samply
      everywhere else
- [V] every mode either resolves or refuses with a reason — none traceback
      (2026-08-20, `wk selftest --quick`; the refusals name the port, the
      platform or the missing tool)
- [ ] `--mode sampling` in a real workspace prints the tier breakdown
- [ ] `--mode bytecode` leaves exactly one JSCProfile json and the summary
      prints (the file is identified by being newer than a stamp taken before
      the run, not by being newest in /tmp — two runs at once)
- [ ] `--mode samply` in a container: refuses with the host remedy when
      `perf_event_paranoid` > 1, records otherwise
- [ ] `--mode instruments` in a macOS guest records a .trace
- [ ] `--fetch` copies a recording out of a guest byte for byte (t_pull)

### Help topics — the concepts, not the commands
- [V] `wk help` lists every topic under `docs/help/` with its one-line summary,
      derived from the files that exist rather than from a second list
- [V] `wk help targets` / `machines` / `disk` print the page; an unknown topic
      warns, lists what there is, and exits 2
- [V] a topic is answerable wherever `--explain` is — inside a workspace, on a
      build machine, on a host with no podman — because nothing is resolved,
      forwarded or started to print one

### Disk: counting it, and erasing the masters
- [V] `wk disk` reports the three places the bytes are — the podman VM's sparse
      disk image, the Tart guests, the store — with a total, and starts nothing:
      a stopped podman machine is `??` naming `wk start`, never a boot
      (216 G measured here 2026-08-20: 54 G image + 162 G golden base + 37 M
      host state)
- [V] rows that are inside a row already counted are parenthesised and left out
      of the total, so the column adds up by eye
- [V] the du-vs-df caveats are printed, not hidden: allocated blocks, APFS
      clones charged twice (macOS only), and `df` underneath as the filesystem's
      own answer
- [ ] `wk disk` inside a workspace answers the only version of the question
      available in there — this checkout, its build trees, its caches — because
      the host's store is not visible from a workspace by design
- [ ] `wk disk` with the podman machine stopped leaves it stopped (the
      read-only rule, measured the same way as `wk status`)
- [V] `wk gc --purge-mirror` refuses while any workspace exists, naming them: a
      snapshot is the lower layer of a live overlay mount, so this would delete
      the ground they stand on
- [V] on a target with no snapshot store it refuses naming *that* target's
      equivalent — the golden guest for `vm`, the machine's own repository for a
      remote — rather than reporting a git error about an empty directory
- [V] with no workspaces it prints both sizes, asks once, erases the mirror and
      every snapshot, and says that `wk sync` rebuilds them; the fstrim at the
      end of gc is what returns the bytes to a macOS host
- [ ] `wk vm base --rm` deletes the golden base, then asks *separately* about
      the pulled OCI image (a download, not hours), and existing vm workspaces
      keep working — a `tart clone` is an independent guest
- [V] both name the size before asking, and decline without a terminal rather
      than blocking or proceeding

## 5. Host: quiesce, session, gui (Linux)
- [ ] `wk quiesce on` sets the performance governor with no password;
      `off` restores; `status` reports
- [ ] `wk session on` starts the kiosk compositor on the GPU; the socket
      appears at the fixed path and `/run/wk-session-mode` says `gpu`
- [ ] `wk session on --bmc` moves the session to the BMC chip, records `bmc`,
      and `wk bench` refuses to run against it
- [ ] `wk session gdm` / `gdm --bmc` bring up a desktop on the intended chip;
      `wk session status` shows `greeter: wayland` (x11 means not enforced)
- [ ] `wk session off` darkens every GPU output (`lit:` empty) and the console
      does not repaint over it; `wk session gdm` gets a desktop back
- [ ] `wk gui <ws>` opens MiniBrowser in the seat; in a bmc session it pins
      the browser to Mesa and the picture actually appears

## 6. State, concurrency and clobbering

The authoritative statement of these rules is "The rules" at the top of
`docs/HANDOFF-workspace-state.md`; the summary here only exists so the
checks below read on their own:

- **Smallest state, no caching.** Every fact is recomputed from evidence at
  read time; a fact lives in exactly one place per machine; a status file's
  claim is never believed without checking the evidence behind it.
- **Crash-only.** Every mutating command can be killed at any point and
  re-run, and the re-run converges to the declared final state. "Already
  exists" is never the answer to a half-made thing.
- **Wipe over repair.** A re-run converges by destroying and remaking
  anything half-made or unrecognized, not by patching it in place;
  resume-in-place is only for expensive stages with unambiguous evidence
  (the mirror fetch, the vm base). The corollary under test: destroy and
  re-provision must be cheap and *total*.
- **Read-only commands are read-only absolutely.**
- **One lock per mutated resource.** Concurrent mutators serialize or refuse
  by name; a lock dies with its holder.

### Read-only commands make no changes
- [V] `wk status` / `wk ls` / `wk logs` / `wk doctor` with the podman machine
      stopped leave it stopped — machine state identical before and after
      (the 2026-08-19 observation could not be reproduced on the tree as it
      then stood; the guard now lives inside `forward_to_vm`, which is the
      only thing that can start the machine, rather than at the call sites)
- [V] the same four start no guest, write no file, and repair nothing
      (guests counted through `tart` directly, writes by fingerprinting the
      host state directory before and after)

### Interrupted and restarted — every mutating command, kill -9, re-run

The invariant, per command: name the final state the command guarantees,
kill it at its worst moment, re-run it, and the final state is reached — the
re-run resumes or rolls forward, never refuses a half-made thing, and no
intermediate state is ever visible to another command as complete.

- [ ] `wk sync` — final state: one new complete snapshot, or none. Killed
      during the `cp -al`, during the fetch, and after checkout but before
      the marker: the half-written snapshot does not exist to `current_base`
      (completion marker absent), `wk new` can never pin it, the next
      `wk sync` finishes or replaces it, and the mirror is intact throughout.
      The marker half of this is verified (2026-08-19: a snapshot directory
      with no `sha` was invisible to `current_base`, refused by name by
      `wk new --base`, and removed by `wk gc`; a snapshot whose recorded sha
      no longer matches its tree was refused by name) — killing a real sync
      at each of the three points is not
- [V] `wk new`, container — final state: registered, running, firstrun
      complete. Killed before the container exists, during `wkdev-create`,
      and during firstrun: a re-run destroys the rubble and remakes the
      workspace from scratch, saying so — it never re-pins `base-id` over a
      surviving `changes/` layer, and never repairs a half-made workspace
      in place (wipe over repair: a workspace that never finished creating
      holds nothing of value). 2026-08-19: verified for the firstrun kill
      (the detached driver -9 at stage `init`) — the other two kill points
      are still owed
- [ ] `wk new --target vm` — killed during the clone: a re-run replaces or
      completes the clone; registry and guest agree at the end
- [ ] `wk new --target <machine>` — killed mid-clone over ssh (and: the ssh
      cut rather than the process killed): the far checkout without its
      marker is rubble, `t_info` does not call it `present`, `wk status`
      says creating-with-dead-driver, and a re-run remakes it; killed
      between the far clone and the near state dir: both ends converge
- [ ] `wk rm`, each target — final state: no container/guest/checkout, no
      ws dir, no registry entry, no alias. Killed between each pair of those
      steps: a re-run finishes; the registry entry outlives the artifacts it
      describes (never the reverse), so the re-run can still resolve the target
- [ ] `wk build` — final state: status says ok/failed with the log to prove
      it. Driver killed mid-build: `state=running` with a dead pid and a
      cold log reads as crashed in `wk status`, `wk bench`'s idle check
      agrees (today it reads `state=running` as gospel until `--force`), and
      a re-run simply builds
- [ ] `wk build --babysit` — babysitter killed between attempts: status says
      crashed, not fixing; a re-run starts attempt 1 with the checkout in
      the state the last fix left it, stated in the report
- [ ] `wk test` — same convergence as build; a re-run overwrites cleanly
- [ ] `wk bench` — killed during seed: the payload without `.wk-seeded` is
      re-fetched whole; leaked `.tmp-*` seed dirs are pruned by `wk gc`.
      Killed during the run: `env.json` without `result.json` reads as a
      crashed run in `wk bench ls`, never as a comparable result
- [ ] `wk gc` — killed between prunes: nothing referenced was removed, and a
      re-run finishes the unreferenced remainder
- [ ] `wk vm base` / `--refresh` — killed host-side while the detached guest
      build runs: a second `--refresh` detects the live far-side build and
      waits or refuses — it never starts a second build in the same tree;
      killed guest-side: the rc file names the failure and a re-run rebuilds
- [ ] `wk vm start` / `wk stop` / `wk start` — killed mid-way: re-run
      converges (these are already idempotent by construction; prove it)
- [ ] `wk remote setup` — killed between the tools push, the conf write and
      the rc edits: a re-run completes every stage; the box is never
      half-provisioned with no path forward
- [ ] `wk remote rm` — killed after the far side is cleaned but before the
      local conf goes: a re-run (or the documented ordering) removes the
      rest; nothing ends orphaned on the far side with the local conf gone
- [ ] `wk pi setup` — killed mid-push: re-run converges; `pi-hosts` gains no
      duplicate or stale address
- [ ] `wk key register` — killed between keygen and GitHub registration: a
      re-run registers the existing key rather than generating a second
- [ ] `wk skills pull` / `push` — killed mid-rsync: a re-run completes; the
      half-synced tree is never left looking authoritative (rsync --delete
      re-converges both directions)
- [ ] `wk backup` — killed mid-write: the repo files are whole or unchanged
      (cmp-guarded write), never truncated
- [ ] `./setup` — killed inside any stage: a re-run reports and completes
      only what is missing; the second full run still reports no changes
- [ ] `wk quiesce on`/`off` — `off` after a reboot or a lost `on` record
      restores the machine's real prior values, not hardcoded guesses; a
      re-run of either is a no-op that says so
- [ ] `wk session on|gdm|off` — killed mid-transition: the next invocation
      reaches the asked-for mode from whatever half-state remains
- [ ] `wk claude` — killed during verify or launch: nothing persists but the
      verify log; a re-run verifies again from scratch

### Readiness — creation is detached, and one gate decides

Phase 2 of `docs/HANDOFF-workspace-state.md`: a completion marker next to the
workspace, a driver that says `creating` while it is missing, one `wait_ready`
that every consumer goes through, and a `wk new` whose work outlives the
command that asked for it.

- [V] `.wk-ready` is written last by every driver, next to the workspace it
      describes: the container's own home (by `container/firstrun.sh`, whose
      last act it is), the far side for a remote, and the host-side workspace
      directory for a vm — a freshly cloned guest is not running, so there is
      nothing inside it to write to. And they agree on the name:
      every driver writes the same completion marker
      (2026-08-19: container verified end to end, the marker in the workspace's
      home after creation and `creating` until it appeared; the remote and vm
      halves are code-verified and name-checked by selftest, not yet exercised
      by a real remote or guest creation)
- [ ] a remote workspace whose clone is cut mid-way reads `creating` from
      *any* machine that asks, including the box itself — the marker is over
      there, not in the driving machine's record
- [V] `wk new` detaches the driver: the waiting end killed (or its ssh cut)
      leaves creation running, `wk status` shows `creating` with a live pid and
      the stage it is on, and re-running `wk new` says it is already being
      created and names the log — it never starts a second driver
      (2026-08-19: a second `wk new` during a live creation was refused by name
      with the pid and the stage, and wiped nothing)
- [V] `wk new --no-wait` returns as soon as the driver is up, and the workspace
      still finishes; `wk status` and the create log are how it is followed
- [V] the detached driver killed -9 at each stage (wipe, create, init,
      register): `wk status` says creation never finished and nothing is
      creating it now, names the stage it reached, and names `wk new` as the
      repair; a re-run destroys the rubble and remakes it
      (2026-08-19: killed at `init` — status said so and exited 4, `wk build`
      refused with the same repair command, and a re-run wiped and remade the
      workspace to `present`. The other three kill points are not exercised;
      `init` is the long stage and so the easy one to hit)
- [V] the creation log and status live *beside* the workspace directory, so the
      wipe at the start of a re-run does not delete the log the driver is
      writing to; `wk rm` removes them with the workspace
      (and the related bug this pass found the hard way: the waiter must not
      believe the *previous* attempt's status file — it read a killed pid from
      it and called a healthy new run crashed, so `detach_run` clears a file no
      live process owns and the waiter trusts the pid it forked)
- [V] `ws_state` answers five words — absent, creating, present, broken,
      unreachable — five words, each from the evidence that decides it, and
      never from `ws.status`, which is consulted only for the driver's liveness
      (`wk selftest --section state`, a stub driver through all five; `broken`
      is the one that also reads the record, because a record-vs-machine
      disagreement cannot be detected without it)
- [V] the readiness refusal is a *barrier*, not an absolute: a workspace whose
      creation never finished refuses by default and proceeds under `--force`,
      warning at the point of bypass and again when the command ends — because
      a clone that finished and lost its marker looks identical to one cut in
      the middle, and only the person looking can tell
      (2026-08-19, after `wk claude db --force` on a build machine could not
      get past it: refuses and exits 1 bare, proceeds under WK_FORCE)
- [V] the repair a refusal names can be typed where it is printed: on a machine
      that only *hosts* workspaces, "remake it" says "from the workstation",
      because `wk new` is refused there
- [V] `wk build` waits (bounded) for a workspace that is still being created
      and starts only once it is `present`; against a creation whose driver is
      gone it refuses and names `wk new`; `--babysit` inherits both, because it
      re-runs `wk build`
      (2026-08-19: both halves measured against a container workspace. The
      babysitter's inheritance is by construction, not measured)
- [V] `wk status` exit code 4 for a workspace that needs a person (creation
      abandoned, environment removed from under the record, machine
      unreachable) — distinct from 3, which is a build to re-run
      (2026-08-19: 4 for an abandoned creation and for a hand-removed
      container, 2 for one still being created, 0 when it is present)

### Concurrency — every mutating verb locks what it mutates
- [ ] two `wk sync` at once: the second waits or refuses naming the first
- [V] `wk gc` racing `wk new`: gc cannot prune the base new is pinning
      (2026-08-19: gc waited 50s on the store lock, named the pid holding it,
      and the snapshot survived)
- [V] two `wk new <same-name>`: exactly one wins, no half-merged workspace
      (2026-08-19: the second waited on the workspace lock, then found a
      finished workspace and refused by name)
- [ ] two `wk build` on one workspace serialize on every target (the
      workspace lock is taken; not yet exercised by two real builds);
      `wk vm base --refresh` while one runs is refused
- [ ] two `wk vm start` do not corrupt `~/.ssh/config.d/wk`
- [V] a lock holder killed -9 releases the lock; no stale lock to clean
      (2026-08-19, macOS host: the next taker broke the dead holder's lock
      immediately and the directory was clean afterwards)
- [V] a lock dies with its holder, and a lock naming nobody is not waited out
      (2026-08-20: the mkdir form had a window between creating the lock and
      writing the pid into it, and a lock left in that window was
      indistinguishable from a live one — `wk rm` sat out its whole timeout on
      one. The lock is a symlink now: `ln -s` writes the holder *with* the
      lock, so the window does not exist. In `wk selftest --quick`)
- [V] twelve takers of one lock, one at a time — started together, with a dead
      holder's lock already in place for all of them to break at once
      (2026-08-20: exactly twelve critical sections and nothing left behind.
      Two earlier attempts failed this: payloads that every subshell of one
      command computed identically, and a re-entrancy test that compared `$$`
      — which twelve subshells of one script all share. In `wk selftest
      --quick`)
- [V] a command's own end-of-run work does not disable the lock release
      (2026-08-20: there was one EXIT trap and six claimants, and bash keeps
      the last one set. Handlers register under one trap now — `wk_atexit` —
      and nothing outside `lib/common.sh` takes the trap. Checked both ways in
      `wk selftest --quick`: the behaviour, and that no file has gone back to
      trapping)
- [V] one lock mechanism, everywhere: nothing in the tree calls `flock`
      (2026-08-20: a flock is held by the open file descriptor, so every
      process that inherits it holds the lock — which is how `conmon` came to
      hold a workspace lock for the life of a container. macOS ships no
      flock(1) either, so the one lock a Mac took was no lock at all. The
      remote build lock is `lib/lockrun.sh` on the machine that builds. In
      `wk selftest --quick`)
- [V] a job detached *into* a workspace is not overrun by a later `wk build`
      (2026-08-20: a lock here dies with the command that took it, so a Yocto
      stage detached into the container held nothing out here and a `wk build`
      in the same checkout was let straight in — two writers, both corrupted.
      The workspace is now asked for evidence instead: `ws_busy_reason` reads
      the pid a detached job leaves in `home/<job>.pid` and tests it *inside*
      the workspace, which is the only namespace the number means anything in.
      A barrier, so `--force` can say "that pid is not really there")

### Status files are claims; evidence decides
- [ ] corrupt each status file (truncate, garbage): status reports the file
      as stale/unparseable, keeps listing everything else, and the
      evidence-derived answer is unchanged
- [ ] a status file written by an older schema (missing keys) still renders;
      unknown keys are ignored
- [V] `wk enter --zed` against a `creating` workspace waits and opens only on
      `present`. Exercised for real 2026-08-19 with a throwaway container
      workspace: `wk new zedgate --no-wait`, then `wk enter zedgate --zed`,
      which printed "waiting for 'zedgate' to finish being created (at: init)"
      and held — no editor — until creation finished, then "'zedgate' is ready"
      and on to the launch
- [V] and the launch works on a macOS host too, for a container workspace, which
      it did not: it was forwarded into the podman VM, looked for
      `/Applications/Zed.app` in a Linux VM and reported "zed is not installed"
      about a Mac that has it — and behind that, the `wk-<name>` alias was
      written by whichever side ran `wk new` (the VM), pointing at a `localhost`
      which has no /src/WebKit on either host. Both are gone: `wk zed` is a host
      command, and a container is reached by the ssh protocol over `podman exec`
      (`container/ssh-transport.sh`), so no address and no interface are needed.
      Measured 2026-08-24 on tolken against 238-tolken-backports: openssh-server
      installed into the workspace on first use, the zed key generated and
      authorised, `ssh wk-238-tolken-backports` landed in /src/WebKit as `core`,
      and `scp` through the same transport round-tripped a file byte-exact —
      which is Zed's own upload path
- [ ] against `broken` it refuses with the repair command

### Un-managed commands clobbering the record
- [V] `podman rm` a workspace's container by hand: `wk ls`/`wk status` say
      "the record says container, the machine has none" and name the repair —
      not a bare `absent`, not a crash
      (2026-08-19: `ws=broken` in status with `wk rm` named and the surviving
      overlay layer's path printed, `broken` in the `wk ls` STATE column, exit
      4; `wk new` refused rather than wiping a layer that may hold work, and
      `wk build` said the same instead of "no such workspace")
- [ ] `tart delete` a guest by hand: same
- [ ] delete `$WK_STORE/ws/<n>` by hand under a live registry entry: same,
      and `wk gc` refuses to prune what the survivor may still pin
- [ ] `git fetch` into a published base snapshot by hand: the recorded sha no
      longer matches `rev-parse HEAD`; `wk new` and `wk sync` refuse it by name
- [ ] hand-edit `~/.ssh/config.d/wk`: the next `wk vm start` regenerates only
      its own block and leaves foreign lines alone
- [V] an unreachable remote machine is reported unreachable with its timeout —
      never `absent` — and any fallback to the stale local status copy says so
      (2026-08-19, against an unroutable address with `WK_SSH_TIMEOUT=3`:
      `ws=unreachable`, the timeout named, exit 4, and `wk build` refused with
      the ssh command to try. `wk ls` on the same target lists nothing rather
      than claiming absence)
- [ ] a machine still running an older wk-tools answers a *delegated*
      `wk status` by its own rules — measured 2026-08-19: a workspace whose
      creation had died read `present` from the far side and `creating` from
      this one. The fleet block already says the tooling DIFFERS; what it does
      not say is that the difference changes answers, not just versions

### Prompts guard destructive actions only
- [ ] every interactive prompt in the tree guards a destructive action —
      `wk rm`, `wk vm rm`, `wk vm base --rebuild`, `wk vm base --rm` and its
      second question about the image cache, `wk gc --purge-mirror`,
      `wk remote rm` and its cleanup offers, `wk skills` overwrites,
      `wk pr`'s `reset --hard` —
      and nothing else prompts: `wk remote setup` writes its conf and says
      so, `wk pr` runs fetch/checkout/remote-add/set-upstream unprompted,
      and `wk pi setup` asks for an auth key only when the node is not
      already on the tailnet
- [ ] destructive prompts default to No and decline without a terminal,
      never block and never proceed (`WK_YES=1` is the scripted yes)

### The target registry — one list of machines, shared by git
- [V] `targets/hosts/<name>.conf` in this repository makes a machine a target
      on every device that pulls it; `~/.config/wk/targets/<name>.conf` still
      overrides it line by line (sourced second, so a device's own conf keeps
      the registry's answer for everything it does not mention)
- [V] `wk build bb4 gtk-release-asan` needs no `--cmake` any more: buildbox4's
      three clang-19 flags are in its registry conf and land in every build's
      CMake flags (`--dry-run` shows them, 2026-08-19)
- [V] the registry is **not** enumerated on a machine that is the far end of a
      target: the whole repository is pushed to a build box, so without that a
      delegated `wk ls` there walked the fleet and tried to ssh to machines it
      has no route or host key for, printing "Could not resolve hostname" into
      somebody else's listing
- [V] and never itself: a target whose name is this machine's hostname is
      skipped, so a workstation does not ssh to itself to ask what it knows
- [V] `wk remote setup` still writes the machine-local conf, and says that it
      is this device only, naming the registry path to share it; `wk remote rm`
      warns when a registry entry outlives the local conf, and does not touch a
      tracked file itself
- [ ] a second device: fresh clone + `./setup`, and every machine in the
      registry is a target there with no state copied from the first

### Changing workstations — the view is calculated, not carried
- [V] deleting the workspace→target registry loses nothing: every command
      still resolves every workspace from the evidence on the targets
      (partly: `ws_target` falls back to whichever target's own store on this
      machine holds the name — a file test per configured target, no ssh and
      nothing started. Measured 2026-08-19: with no registry entry at all,
      `db` resolved to devbox-arm64-2 from this machine's store, where every
      command had previously fallen back to `container` and answered "no such
      workspace" about a complete checkout on a build box. What is *not* done
      is eliminating the registry: it is still the fast path, and a workspace
      whose near-side store directory is also gone is still unresolvable
      without probing the targets themselves)
- [V] the record precedes the artifacts: `wk new` registers the target before
      it creates anything, so a creation killed at any point is still findable
      by name — the mirror image of `wk rm`, where the record outlives them
- [ ] a workspace name that exists on two targets refuses and names both;
      `--target` disambiguates
- [ ] a target that cannot be probed during resolution is reported
      unreachable by name — never silently left out of the view

### The fleet walk — `wk status` reaches every workstation that is up
- [V] a bare `wk status` sshes into each listed workstation that answers, runs
      its read-only status, and merges the answer. Verified 2026-08-19 from the
      Mac: moose's own container workspace appears in the Mac's listing, with
      moose's wk-tools sha and tree beside it — a *peer* target
      (`WK_REMOTE_PEER=1`), which is a workstation that can be asked and not
      driven: no tooling is pushed to it (its checkout is git's), and `wk new`
      / `wk rm` against it are refused naming the command to run there.
      Provisioning it as a build box would have written `~/.wk-remote` on it
      and made it refuse `wk sync`, `wk gc` and `wk new` on itself
- [V] a delegated row says which machine it came from: the asking side passes
      the name it knows (`--label`), and a row whose own target is not already
      that name is qualified — `moose:container`, while a build box stays
      `buildbox4` rather than `buildbox4:buildbox4`
- [V] `wk ls` delegates the same way and lists the same names as `wk status`
      (it walked only the local store before, so a peer's workspaces appeared
      in one command and not the other)
- [V] every machine is probed in **one round** rather than one after another:
      two unreachable machines cost 10.0 s against one machine's 9.9 s
      (measured 2026-08-19; serially it was one `WK_SSH_TIMEOUT` each). The
      report itself is untouched — same order, same streams, same exit status
      — because only the waiting moved
- [ ] a workstation that is down is listed unreachable with its timeout;
      the walk never hangs on it and never drops it silently
- [ ] the remote half is read-only absolutely: nothing starts, boots or is
      repaired on the far machine (its podman machine stays stopped)
- [ ] wk-tools version skew is flagged: a machine on an older or dirty
      checkout is named, with both shas
- [ ] the same workspace name alive on two machines is reported as a
      conflict, not listed twice as if normal
- [ ] two workstations reaching one build box see one state; a disagreement
      is reported naming both views
- [ ] a machine armed to reboot into a bench system shows the transition on
      its status line (system id, who armed it, when); after it reboots, the
      walk reports its new mode or off-ssh under the bench channel
- [ ] an armed machine still in host mode long after arming, or back in host
      mode with the arming record uncleared, is flagged as desync
- [ ] a mutating command against a machine armed to leave host mode warns or
      refuses — no build starts on a box about to reboot out from under it
- [ ] the exit code aggregates the worst state found anywhere in the fleet

### Shared-home remotes (devbox-arm64-2 / devbox-armhf-2)
- [ ] provisioning the second of two remotes that share one home folder does
      not clobber the first's identity; `wk` on either box resolves its own
      target (by hostname against the confs, not a shared marker); `wk remote
      rm` of one leaves the other provisioned and working
- [ ] builds from the two machines never collide in a shared checkout or on a
      shared lock: build dirs and locks are keyed per machine, derived, not
      configured

---

## 7. Systems and mode transitions — `wk sysimage`, `wk boot` (Linux)

A machine booted into a system is a *mode transition*, not a reboot: the box
stops being a workstation for a while and then becomes one again. So the
checks come in three groups — the build (which must be unprivileged and
reproducible), the transition (which must be one-shot and self-reverting), and
the reader (which must never mistake intent for evidence).

### The drift warning is only useful while it is true
- [V] a machine reports "in sync" immediately after a successful sync
      (2026-08-20: it never did. `dotfiles/zed/prompts/…/lock.mdb` is an LMDB
      lock file — rewritten by any process that *opens* the database, a read
      included — and it is tracked, so every machine reported "wk-tools
      DIFFERS from the workstation" straight after a sync, permanently. The
      tree hash excludes it now; `wk sync --target container` then produced
      matching hashes on both sides. The file should not be tracked either)

### The bare-metal benchmark run (`wk bench stage` / `wk bench staged`)
- [V] a benchmark runs in bench mode or it does not run — host mode is
      refused, and `--force` does not open it (2026-08-20, in
      `wk selftest --quick`: same refusal with and without the flag; the two
      modes produce the same shape of result and nothing tells them apart
      afterwards)
- [V] `--dry-run` still describes it from host mode, quoting
      included — the default volume name has a space in it
- [V] run-benchmark drives from a *partial* tree: `Tools/Scripts` alone, no
      checkout root, no `Source/` (2026-08-20, on this Mac against a tree
      copied out of a base snapshot: `--list-plans` exits 0. Plans and their
      patches resolve relative to webkitpy itself, not to a checkout)
- [V] the arguments the runner builds are the ones webkitpy accepts:
      `--browser minibrowser --platform osx --build-directory …` parses, and
      `BrowserDriverFactory.create('osx','minibrowser')` returns OSXMiniDriver
      (2026-08-20). With `--build-directory` the driver launches
      `MiniBrowser.app/Contents/MacOS/MiniBrowser` itself with `DYLD_*` set —
      that is what makes the partial tree sufficient
- [V] it must run under a python that has PyObjC: the driver's `prepare_env`
      does a bare `import objc`, which is *not* autoinstalled. Apple's
      `/usr/bin/python3` has it (3.9.6, pyobjc 11.1); the Homebrew python3
      first on PATH here does not (2026-08-20 — so the interpreter is named
      explicitly rather than left to the shebang)
- [V] the whole runner, against a simulated role: preflight (role, tooling,
      build, python, console session, AC power, machine quiet), payload build
      from `--payload`, http server, driver, browser launch, and the timeout
      path — ending in a recorded failure with the real exception surfaced
      (2026-08-20; the browser was a stub, so everything up to the measurement
      itself is exercised)
- [V] a run that dies leaves the Dock's launch animation off — webkitpy turns
      it off in `prepare_env` and only restores it on a clean exit. Measured,
      and put back by the runner afterwards (2026-08-20)
- [V] the record carries the three axes plus the role, the display size, the
      thermal limit and the machine, and `wk bench compare` warns across
      `bench_host` (container vs image) and on a rehearsal
      (`role_marker_overridden`)
- [V] comparing two results *by path* is not forwarded into the podman VM
      (2026-08-20: it was, and reported "no such run" about a file that was
      sitting right there)
- [ ] a real measured run: a real `mac-release` build, staged from a guest,
      on a real benchmark install

### Every boot driver answers `--status` from either workstation
- [V] `wk boot <machine> --status` exits 0/2/3 and always says something, for
      every machine with a driver (2026-08-20, in `wk selftest --quick`). It
      found two: the guest driver exited 1 in silence when its guest was off
      (its probes ran under `set -o pipefail`), and `wk boot rpi5 --status`
      could not run on the Mac at all — `date -u -d @<epoch>` is GNU-only and
      BSD date answers "illegal option -- d" plus a usage block. The fleet is
      meant to be drivable from either workstation; one of them could not read
      a machine's boot time

### The golden base finishes, or it is rubble
- [V] a base that exists but was never provisioned is destroyed and remade,
      not adopted (2026-08-20, found the hard way: `wk vm base` pulled 68.8 GB,
      cloned the guest, then refused to start it because the podman machine
      held the whole memory envelope — and the *next* run found a VM by that
      name and said "golden base is ready" in half a second. An unprovisioned
      macOS image with no Xcode licence, no checkout and no prebuild, which
      every `wk vm new` would have cloned. There is a completion marker now,
      written last, with the same protocol as an image manifest and a snapshot
      sha; `--rm` and `--rebuild` clear it, `--refresh` rewrites it)
- [V] and it says so rather than doing it silently: "exists but was never
      finished (no completion marker)" (2026-08-20)
- [V] the prebuild survives its driver dying — it is started with `nohup`
      inside the guest and merely *polled* from here (2026-08-20: the driving
      process was killed mid-provision and the in-guest `xcodebuild` carried on
      to WebKitLegacy without noticing. `wk vm base --refresh` is the recovery:
      it re-provisions the existing guest and writes the marker, with no 68 GB
      re-pull and no lost build tree)

### The job count and the memory budget agree with the build system
- [V] an Apple build derives its job count at 3072 MB/job, a CMake one at 1536,
      and an explicit `WK_MB_PER_JOB` still wins (2026-08-20, measured the
      expensive way: the golden base's `mac-release` prebuild ran at `-j9`,
      derived from 13824 MB at the CMake figure, peaked at **16593 MB** and was
      killed by the memory watchdog 95% of the way through. `claude/CLAUDE.md`
      already told agents to pass `WK_MB_PER_JOB=3072` by hand for exactly
      this — a default that needs a manual override on one of the two build
      systems is a wrong default)
- [V] and the figure reaches the *job count*, not only the environment handed
      to the target (2026-08-20: fixing it in `config_build_env` alone left the
      count still derived from 1536, and the next run was `-j9` again)
- [V] the same build then finished: **peak 16783 MB of an 18432 MB budget**,
      against 16593 MB of 13824 before. Note what the two numbers say together:
      the peak barely moved between `-j9` and `-j6`, so it is dominated by one
      step (the big link) rather than by parallelism — the *budget* was the
      operative fix and the job count mostly buys wall-clock. An Apple build on
      this machine wants ~17 GB whatever it is told to do

### The memory check does not count a guest against itself
- [V] `wk vm base --refresh` on a base that is already running is not refused
      (2026-08-20: it reported "only -20480MB is unspoken for" — the guest's
      own allocation subtracted twice)

### The rehearsal: a guest standing in for bench mode (`benchvm`)
- [V] `wk boot benchvm --status` on a Mac with no such guest says so —
      "guest=wk-bench (absent)" — instead of exiting silently; from a host
      the registry says cannot drive it (`os=macos`), the answer is the loud
      os refusal instead (2026-08-20: it
      did exit silently, because the driver's probes ran under `set -o
      pipefail` and a guest that is merely off is a normal state)
- [V] the arming model reaches the "next step" text: a guest is *started*, not
      armed and rebooted, and the staging output says so (2026-08-20)
- [V] **build in one guest, stage to the other, run there — done end to end,
      2026-08-20.** `wk build bench-build mac-release` (8m1s, peak 7.6 GB),
      `wk bench stage bench-build --to benchvm` (1.5 GB across the boundary),
      then in the guest: `wk bench staged --plan jetstream2.2 --count 1`.
      BENCH OK, with real per-subtest scores — string-unpack-code-SP 452.1,
      tagcloud-SP 242.3, tsf-wasm 71.1, typescript 17.5, uglify-js-wtb 39.3 —
      and a record carrying `bench_host=image`, `role=mac-bench-rehearsal`,
      `role_marker_overridden: false`, the full sha, and the machine
      (VirtualMac2,1, 9 cores, 8192 MB, macOS 26.4, AC, `cpu_speed_limit` 100).
      The numbers mean nothing (a guest), the path means everything
- [V] the products-only tree is sufficient for the browser driver: the web
      process launched from it — `com.apple.WebKit.WebContent.Development` out
      of `…/staged/…/WebKitBuild/Release/com.apple.WebKit.WebContent.xpc`, at
      55% CPU next to MiniBrowser's 43% (2026-08-20). The XPC services survive
      the exclusions
- [!] the *first* run after a stage timed out at 900 s; the second, identical,
      finished in about five minutes. Not root-caused — the candidates are a
      first-launch Gatekeeper/XProtect scan of 1.5 GB of freshly copied
      binaries, webkitpy's autoinstall on first use, and a cold dyld cache.
      Worth knowing before blaming a build: give the first run after a stage a
      generous `--timeout`
- [V] the staged tree is products only, and the exclusions are the ones that
      matter (2026-08-20: the first list — `*.noindex`, `DerivedData`, `*.dSYM`
      — excluded *nothing*, because those live under `WebKitBuild/DerivedData`,
      a sibling of `Release` rather than inside it, and 37 GB started going
      across the wire. What is actually in there: `WebCore.build` 16 G,
      `WebKit.build` 7.1 G, `JavaScriptCore.build` 3.3 G, `libJavaScriptCore.a`
      2.8 G, `TestWebKitAPI.build` 1.4 G, `XCBuildData` 856 M. Excluding
      `*.build`, `XCBuildData`, `DerivedSources`, `PrecompiledHeaders`,
      `compile_commands`, `*.a`, `*.dSYM`: **1.3 GB kept, 37.4 GB skipped**)

- [V] the manifest crosses the wire *last*, on its own (2026-08-20: a killed
      `wk bench stage` left 13 GB in the guest with a perfectly good
      `stage.json` on top of it — rsync carries the manifest along with the
      bulk and gives no order guarantee, so "written last" does not survive a
      network hop unless it is sent separately)
- [V] and it parses (2026-08-20: `"payload_pinned": ${payload:+true}${payload:-false}`
      emitted `true/private/tmp/...` — a bare path where a JSON literal
      belongs. Every field then read back empty, and a reader cannot tell "no
      workspace recorded" from "this file is not JSON". `wk selftest --quick`
      now rejects that shell idiom in a JSON value)

### A guest that draws has to be a guest with nothing in front of it
- [V] no post-login Setup Assistant pane on a fresh clone (2026-08-20, reported
      from outside as "it looks stuck on Update Automatically" — and it was not
      stuck: `.AppleSetupDone` was present, auto-login had happened, the console
      belonged to `admin`, and ssh and builds worked throughout. What was on
      the screen was the *post*-login assistant, which asks about automatic
      updates, Siri and appearance on the first login of what macOS considers a
      new install — and every clone of the base is one. Harmless for ssh, not
      harmless for anything that draws: it sits modal in front of the desktop,
      and a browser window under it is occluded, which throttles its timers.
      The `DidSee*` keys are set in provisioning now)

### Quiesce measures rather than assumes (macOS)
- [V] `wk quiesce status` prints the privileged half's *claim* and what the
      machine says *now*, labelled separately (2026-08-20; when they disagree
      the disagreement is the finding)
- [V] the measured block covers what the helper does not: a configured Time
      Machine destination, the sleep timer, and `CPU_Speed_Limit` — a machine
      being thermally held back during one half of an A/B is a difference that
      has nothing to do with the change (2026-08-20, on this Mac: it found
      automatic update checking still on)
- [ ] the same on a benchmark install, before a run

### The Mac: a mode transition nobody can automate (`wk boot mbp`)
- [V] `wk boot mbp --status` reports the booted volume and whether the
      benchmark volume is attached, and changes nothing (2026-08-20, on this
      Mac: `booted_volume=Macintosh HD`, `benchmark_volume=... (not attached)`)
- [V] boot time and boot identity come from `kern.boottime` and are right
      (2026-08-20: the first attempt parsed `usec` out of
      `{ sec = …, usec = … }` — greedy — and reported a 1970 boot date)
- [V] a spent arming record is recognised on macOS too, by boot id
      (2026-08-20, with a hand-written record: reported as spent, naming the
      boot that consumed it)
- [V] arming with no volume attached refuses *and writes no record*
      (2026-08-20: it wrote one the first time — the checks now run before the
      record, the opposite order to the one-shot machines, because a ritual
      printed to a person cannot half-happen and a firmware call can)
- [V] the full lifecycle against a disposable APFS volume: dry-run → arm →
      status (exit 2, ARMED, "waiting for a person") → diag → disarm → status
      clean (2026-08-20, `hdiutil` volume named "WK Bench Test")
- [ ] the same against a real benchmark install, booted for real
- [ ] `wk bench stage <ws> --to mbp` from a macOS guest onto the volume
- [V] ... and its refusals: no `--to`, a machine whose other role is only
      reachable over the network (rpi5), a volume that is not attached
      (2026-08-20)
- [V] staging lays out `WebKitBuild/<config>`, `Tools/` and a `stage.json`
      written last, with workspace, sha, config and `bench_host=image`
      (2026-08-20, from a local workspace onto the disposable volume)
- [V] `--status` reports which install the *firmware* will boot next
      (2026-08-23: `firmware_default=73C12614-… ('WK Bench' — a plain reboot is
      expected to enter bench mode)`, on this Mac, while it was running
      Macintosh HD. That combination is the point: `startosinstall` set the
      default and the startup manager overrode it once without updating it)
- [V] the boot volume still cannot be *set* from software, with SIP disabled and
      root (2026-08-23, in a guest: `nvram boot-volume=<other group>` exits 0
      and changes nothing before or after a reboot; `bless --setBoot` refuses by
      documentation; `systemsetup -getstartupdisk` answers `(null)`). Re-tested
      rather than assumed, because "SIP is off now" is the obvious reason to
      expect otherwise

### The unattended A/B (`wk bench mac-ab`)
Rehearsed in the `wk-bench` guest before it was pointed at the volume, which is
the only reason the three bugs below were not found on hardware with nobody in
the room.

- [V] `--preflight` answers from another machine, changes nothing, and names
      every reason the run could fail after the reboot: host mode, the volume,
      `/var/wk` and `~bench` writable without sudo, autologin, pyobjc, scipy,
      wk-tools and what is staged (2026-08-23, from rpi5 against tolken)
- [V] `--dry-run` prints the plan and reboots nothing (2026-08-23; the first
      version reported "tolken is not answering" from a dry run, because
      `phase_wait` returned an empty answer the caller then branched on)
- [V] a per-user LaunchAgent in `~/Library/LaunchAgents` fires at autologin and
      drives the whole job (2026-08-23, guest: agent started ~60 s after the
      reboot, which is worth knowing — a poll at 30 s reads as "it never ran")
- [V] the arm→result map is written as it happens, because it cannot be
      recovered afterwards: an A/A runs both arms out of one staged payload, so
      the result names differ only by timestamp (2026-08-23, `ab/<stamp>/runs.tsv`)
- [V] both arms complete and produce a score (2026-08-23, guest:
      `Speedometer-3:Score: 31.467pt stdev=7.3%` for arm A)
- [V] **the job cannot loop.** Finishing sets `phase=done`; the reboot at the
      end landed back in bench mode (a guest always does), and the next boot
      removed the agent and *halted* the machine (2026-08-23, verified twice —
      once failing, once passing, see below)
- [V] the state file advances before the run, so an interrupted attempt is
      counted rather than repeated forever (2026-08-23: `attempts=1`,
      `phase=running` during, `phase=done outcome=ran` after)
- [V] the watchdog is armed and reaped quietly (2026-08-23; it first left
      `Terminated: 15` in the log, which reads like a failure in a transcript
      whose whole job is to be read after the machine has gone)

Three bugs the rehearsal found, each of which would have cost a trip to the
keyboard:

- [V] `launchctl bootout gui/<uid>/<label>` on the agent's *own* label is
      `kill $$`: it killed the script mid-function, so the plist survived and
      the machine never halted — in the one branch whose entire job is to stop
      a benchmark install running unattended. Deleting the plist is sufficient
      and is what it does now (2026-08-23)
- [V] `run-benchmark`'s patch step needs `DARWIN_USER_TEMP_DIR`, and at login
      that directory does not necessarily exist yet: the first arm died 20 s in
      with `patch: Can't create '/var/folders/…/T/patchXXXX'`, the second was
      fine 16 s later. The autorun waits for a writable per-user temp directory
      (2026-08-23)
- [V] **`wk bench staged` used a staged tree's pinned payload for any plan.** A
      tree staged for `jetstream2.2`, run with `--plan speedometer3.0`, handed
      JetStream to run-benchmark as `--local-copy` and died inside `patch` with
      "No file to patch". The loud failure is the lucky case — a payload for a
      *near* plan would have produced a number for the wrong benchmark under
      the right name. The manifest's plan is now checked (2026-08-23)
- [V] `wk bench seed` refused every workspace but a container, so a payload
      could not be pinned for the macOS lane at all — the plan file it reads
      lives in the guest that did the build. Seeding is not running and no
      longer inherits running's refusal; it also prints the directory, which
      `>/dev/null` had been discarding (2026-08-23)
- [V] `wk bench seed <guest-ws>` is not forwarded into the podman VM. It was,
      and the forwarding was silent: on tolken it *started the podman machine*
      and then could not find the guest workspace, so the payload was not
      pinned and the lane went ahead anyway (2026-08-23 — see the refusal below,
      which is what that cost bought)
- [V] `wk bench compare a1,a2 b1,b2` with file paths is not forwarded either:
      the arm is comma-separated, so the whole argument is not a file even when
      every part of it is, and testing it as one string sent a comparison of
      local files into the VM (2026-08-23, unit-checked against the matcher)
- [V] staging **refuses** when the payload cannot be pinned, rather than warning
      and continuing. It is cheap to pin on the machine that has a network, and
      there is no version of "we will find out over there" worth a cycle
      (`--allow-network-fetch` overrides and says what it is buying). Note this
      was *not* what broke the 2026-08-23 cycles — the runs never reached the
      payload; see the first-boot daemon below
- [V] a seeded payload carries no `.git`. git's fsmonitor leaves a Unix socket
      at `.git/fsmonitor--daemon.ipc`, openrsync dies on it
      (`mkstempsock: Invalid argument`) and `cp -R` skips it with a warning,
      which is the same problem arriving quietly. Dropping it also makes two
      seeds of one commit the same tree, which they were not before (2026-08-23)
- [V] `wk bench seed` computes its cache path *after* a target is loaded.
      `SEED_DIR` is assigned at source time from the default store, which is
      `/var/lib/wk` on a Mac — right for a container, wrong for a macOS guest, so
      seeding died on `mkdir: /var/lib/wk: Permission denied`, which reads like a
      missing setup step and is really a variable read too early (2026-08-23)
- [V] **the first-boot daemon removes itself.** It removed itself with
      `launchctl bootout` on its own label, which kills the job before the `rm`,
      so it had never once succeeded: ten runs across two days, both files still
      carrying their original mtimes, and "removing the first-boot daemon" in the
      log every time. Every boot it therefore `rsync --delete`d its payload copy
      of wk-tools over `~bench/Development/wk-tools` and then rebooted the machine
      a minute later — which is what actually killed three planted A/B cycles:
      the run read an old `lib/quiet.sh` and the machine went down under it
      (2026-08-23). Deleting the files is the whole of the fix, and the `rm` is
      now checked
- [V] the autorun defuses a first-boot daemon it finds on a volume provisioned
      before that fix, by killing the running script *before* it can schedule a
      reboot rather than cancelling one after — the two start within two seconds
      of each other, so reacting to it is a race that cannot be observed — and
      re-checks for a pending shutdown after the settle (2026-08-23)
- [V] planted tooling lives at `/var/wk/wk-tools`, not `~bench/Development`:
      the install's own copy is the one directory on that volume that something
      else rewrites, and a run should not depend on what the install happens to
      carry (2026-08-23)
- [V] `put_tree`/`put_file` verify the far end — a sentinel file and a byte
      count — instead of trusting an exit status. Worth being precise about what
      this does and does not buy: it was already verifying when the tree reverted,
      and the verification was true when it was made. It catches a transfer that
      did not land; it cannot catch one that is undone afterwards (2026-08-23)
- [V] a whole round producing nothing stops the schedule. The build, the payload
      and the machine do not change between rounds, so finishing them buys
      nothing and spends the machine's time in a mode nobody can reach. One
      flaky arm does *not* abort — that is what more rounds are for
      (2026-08-23, unit-checked: `nnnnnn` stops after round 1; `ynnnnn` and
      `nyyyyy` both run all three)

### Building — unprivileged, reproducible, crash-only

- [V] `wk sysimage build <profile> --dry-run` resolves the profile, the machine,
      the base and the destination, builds nothing, and names any missing
      tooling rather than failing at the first `require`
- [V] `wk sysimage build perf-linux-rpi5` completes with **no sudo anywhere** — the FAT
      seed goes in through mtools at a byte offset, never a loop mount
- [V] the base is pinned by sha256 and re-verified on every build; a cached
      base that matches is not re-downloaded
- [V] the built image carries `user-data`, `meta-data`, `network-config` and
      an appended `config.txt` on its boot partition, and all three YAML files
      parse
- [V] the network config is **rendered for the image's own backend**, not
      copied: the board's netplan says `renderer: NetworkManager` and the base
      image has no NetworkManager, so a verbatim copy is an image with no
      network on a board with no cable
- [V] the wifi PSK reaches the image without appearing in any terminal output,
      log or agent transcript — it is read on the board and written into the
      image by the same pipeline
- [V] the image's filesystem labels are its own, and every place that names
      them agrees: the ext4 superblock, the FAT boot sector, `/etc/fstab` and
      `root=LABEL=` in `current/cmdline.txt`. **None of them may equal the
      target machine's own install** — a distro image and an install made from
      it are label-twins, and `root=LABEL=writable` with both disks attached
      names two filesystems
- [V] `e2fsck -fn` on the built image's root passes clean after that surgery
- [V] `wk sysimage ls` lists only images with a manifest; a build directory
      without one is reported as rubble, by name
- [V] `wk sysimage show <id>` re-hashes `disk.img` and refuses an image that no
      longer matches its manifest
- [V] a build that fails partway leaves a directory with no manifest; it is
      reported as rubble and the next build destroys it, never "already
      exists" (rule 2) — seen for real when the target machine went away
      mid-build
- [V] a machine that is unreachable fails the build *before* the unpack, and
      names the reason: the network profile is read from that machine
- [ ] `kill -9` mid-build, re-run: same, at every other point
- [ ] two `wk sysimage build` at once: the second waits on the store lock rather
      than racing the first's rubble cleanup (rule 4)

### Writing a system onto a disk — `wk sysimage write`

- [V] with no `--device` it lists candidates on the machine and **refuses to
      guess** which disk is the card
- [V] refuses the machine's own system disk: `/dev/nvme0n1` → *not removable
      (RM=0) and its transport is 'nvme', not usb or mmc*
- [V] refuses a partition: `/dev/mmcblk0p1 is a 'part', not a whole disk` — an
      image carries its own partition table
- [V] `--dry-run` resolves the image, the device, what is mounted on it, and the
      image's own root spec, and writes nothing
- [V] the real write, to the rpi5's reader: unmounts the partition the desktop
      automounter had taken, streams 3984 MB with zstd, **reads it back and
      compares sha256**, then grows partition 2 to fill the card
- [V] confirmed on the card independently of the log — `mmcblk0p1 130M vfat
      boot`, `mmcblk0p2 7.3G ext4 root`, grown from the image's 3.8 G
- [ ] the card actually boots an rpi4 (needs the card moved to the board)
- [ ] the confirmation prompt appears and "no" leaves the device untouched
- [V] the `bmaptool` path: *mapped 566163 of 1019904 blocks (2.2 GiB of 3.9
      GiB, 55.5%)*, 547 MB sent instead of 4 GB, each block checksummed against
      the map. Verified on the card afterwards — `boot`/`root` labels, the
      bcm2711 DTBs, `root=/dev/mmcblk0p2` intact
- [V] the fallback is **not silent**: an image with a block map written to a
      machine without `bmaptool` warns and gives the install command, because
      that is a missing package rather than a property of the image
- [V] the read-back sha256 check runs only after a **dd** write. After a bmap
      write it would always fail — bmaptool does not write unmapped blocks, so
      comparing the whole span compares bytes nobody wrote
- [V] one verb, not two: `wk sysimage flash` and `wk pi flash` both fail with a
      message saying why the *name* was wrong ("reads as reflash that machine";
      "nothing here is permanent"), not merely where it moved
- [V] `wk sysimage disks <machine>` marks which disk that machine is configured to
      boot from, so `wk boot` does not look like it takes a disk argument
- [V] after writing, the closing line says whether anything will boot it and
      names `wk boot <machine>` — the sentence that would have prevented the
      original confusion

### The image must be able to boot from what it is written to

- [V] `wk sysimage write` of the yocto image to the rpi4 is **refused**: it says
      `root=/dev/mmcblk0p2` and `MACH_DEVICE` is `/dev/sda`, so the firmware
      would load the kernel and the kernel would find no root. The message says
      exactly that, and says it in the dry run too
- [V] no false positive on the distro images: `root=LABEL=wk-image-root` is
      classed `portable` and passes on any device
- [V] compared by device *kind*, not path — a card written in one machine's
      reader is routinely booted in another, so `/dev/mmcblk0` on the writer and
      on the booter are two facts that merely share a spelling
- [V] `WK_ANY_ROOT=1` overrides it, and says the write proves the transfer only

### Arming and returning — the one-shot
- [ ] a mutating command aimed at an armed machine is refused, and `--force`
      turns it into a warning: `wk sysimage write --disk <machine>:<dev>` and
      `wk pi deploy <ws> <machine>` both stop while an arming is unspent, and
      both proceed once it is spent or disarmed (`machine_armed_barrier`)

- [V] `wk boot <machine>` refuses to arm when the boot device does not start
      with the image it was asked to arm — checked before the firmware call,
      because a one-shot that falls through looks like a firmware fault
- [V] arming leaves a record of intent on the machine and **nowhere else**
- [V] `wk boot <machine>` lands in the image, reachable over the LAN — 53 s
      from arming to an answering ssh, on the rpi5
- [V] a plain reboot from the image lands back on the workstation: the
      one-shot is spent by any boot (~40 s)
- [V] `wk boot <machine> --keep` cancels the self-return watchdog
- [ ] left alone, the image hands the machine back by itself within the
      profile's watchdog period
- [V] `wk boot <machine> --back` returns the machine and clears the record
- [V] `wk boot <machine> --disarm` re-arms the normal boot order and clears
      the record, and the next reboot is an ordinary one
- [ ] with the boot device absent, arming falls through to host mode
      rather than hanging at firmware

### Reading the transition — intent is never evidence

- [V] `wk boot <machine> --status` starts nothing, writes nothing and repairs
      nothing (rule 6)
- [V] it reports the mode from the machine's own identity marker (the role
      comes from config), and the firmware's persistent boot order alongside it
- [ ] armed and not yet rebooted: reported as ARMED, exit 2, with the warning
      that the next reboot leaves this role
- [V] armed, rebooted, and back in host mode: the record is reported as
      **spent** rather than as a desync — the transition happened, and the
      mechanism worked
- [V] spentness is decided by the machine's **boot id**, not by comparing the
      driving host's clock against the machine's. The clock version called a
      plainly-spent arming "armed" — `date -u -d "$(uptime -s)"` re-reads a
      local-time string as UTC, so a machine six hours off UTC reports a boot
      six hours in the past
- [V] unreachable in both roles: reported as unreachable by name, exit 3,
      never hung on
- [V] `wk boot <machine> --diag` prints the image's own account of its last
      boot, read off the image's boot device from the *other* role — the
      channel that works precisely when the image never appeared. Its "the
      image did not get that far" answer is what identified a boot that never
      reached userspace
- [V] the image is found by the machine's hardware address in the driving
      host's neighbour table, with mDNS as the fallback and `WK_IMAGE_HOST` as
      the override — no address is stored anywhere

### The image is what the profile says

- [V] `perf_event_paranoid = -1` and `kptr_restrict = 0` on the booted image —
      the reason this step exists, and the thing a workspace can never have
- [V] `perf` present, the JIT-dump directory created, `WK_IMAGE` and
      `WK_JITDUMP_DIR` exported
- [V] the JSC JIT-dump variables are written to `/etc/wk-perf-env` and **not**
      exported globally — every one of them changes what JSC does, so a shell
      that set them for all processes would silently change every measurement
      taken on the machine
- [V] the image mounts *its own* root and boot partitions (`/dev/sda2`,
      `/dev/sda1`), not the workstation's
- [V] cloud-init's datasource is the image's own boot partition
      (`DataSourceNoCloud [seed=/dev/sda1]`), not the workstation's NVMe
- [V] the persistent journal survives a boot, and is readable off the stick
      from the other role with `journalctl -D` — which is how three failed
      boots were finally explained
- [ ] first boot is slow (~17 min) because `packages:` installs over WiFi.
      (The sysctls already moved into the rootfs at build time; anything else
      not needing a per-machine secret should follow.)

### The boot-file check — what `wk sysimage write` refuses

(The boot-file resolver lives in `boot/check-boot-files.py`, where
`wk sysimage write` runs it before anything touches a disk. The
fleet-integration and disk-side lines below are
the live ones.)

- [V] `wk sysimage write` refuses an image whose boot partition cannot get the
      firmware as far as a kernel, before writing anything — firmware that
      finds no kernel halts, where a kernel that finds no root reboots. The
      check asks a resolver that models the firmware's name rules, not
      os.path.exists: the files that halted the rpi4 twice on 2026-08-20 were
      all present in the tree and unreachable through the names asked for
- [V] the check refuses when the boot files the firmware will ask for are not
      all reachable, and names the missing ones
- [V] a `config.txt` that names no `kernel=` and no `arm_64bit=` is accepted
      when any firmware-default kernel is present — the Dev@CI Yocto image is
      that shape and boots the rpi4 daily
- [V] path traversal is refused (`deadbeef/../../etc/passwd` resolves to
      nothing)
- [V] the rpi4's arm/disarm is one byte of the MBR at offset 450, it round-trips
      0x0c <-> 0x83, and it neither truncates the device nor moves anything
      else in the sector — a stick with a FAT boot partition and no
      `start4.elf` **halts** the firmware rather than being skipped, which is
      why the disarm removes the partition type and not the file; the fixture
      is a *minimal valid MBR* (0x55AA signature, one entry with a real LBA
      start and size), because the sfdisk assertion reads a table and runs
      only where sfdisk exists — rebuilding the fixture as bare zeros on the
      Mac passed there and failed on every Linux box (found 2026-08-20)
- [V] a driver's self-disarm command contains no single quote — it is
      interpolated into a single-quoted systemd `ExecStart`, where one would
      close the string early and leave three fragments where a command should
      be
- [V] a freshly built image's partition 1 is type 0x0c, so `wk sysimage write`
      leaves the stick in the armed state the driver expects
- [V] the cloud-init seed's heredoc contains no unescaped backtick — it is an
      unquoted heredoc, so prose in it is shell input: three `systemd-run`
      invocations per build ran on the workstation and left holes where the
      words had been
- [V] every bench lane boots local media: no image is built to take its root
      from anywhere but the disk it was written to

### Retargeting an imported system — `wk sysimage retarget`

An imported distribution is faithful to its recipe and that is exactly the
problem: a wic image's `root=` names the device its wks file assumed, so the
rpi4's own image could not be written to the disk the rpi4 boots. This verb is
the seam between "what the distribution says" and "what this fleet needs", and
it is checked against a copy of the store rather than the store.

- [V] `retarget` rewrites `root=/dev/mmcblk0p2` to `root=PARTUUID=<sig>-02`,
      which `image_root_class` then calls `portable`, so `image_check_root`
      accepts **both** `/dev/sda` and `/dev/mmcblk0` where it refused the stick
      before
- [V] `PARTUUID=` and not `LABEL=`: resolving a filesystem label at boot needs
      an initramfs to do the lookup and the wic image has none (no
      `initramfs*` in its boot partition), where `PARTUUID` is resolved inside
      the kernel. The distro builder's `relabel` uses `LABEL=` because Ubuntu's
      raspi images *do* carry an initramfs — two spellings for two facts, not
      an inconsistency
- [V] `/etc/fstab`'s `/boot` is rewritten too (`/dev/mmcblk0p1` →
      `PARTUUID=<sig>-01`). Rewriting only the command line gives a system that
      boots and then has an empty `/boot`, which looks fine until something
      writes a kernel there. The image has util-linux `mount`, `blkid` and
      `findfs`, so userspace can resolve it
- [V] the driving ssh key lands in root's home **as the image's own
      `/etc/passwd` reports it** (`/home/root` on Yocto, `/root` on Debian),
      mode 0600 in a 0700 `.ssh`. Without it the fleet integration is
      unusable: the marker, the disarm and the benchmark are all read over ssh,
      and a Yocto image ships `PermitRootLogin yes` with an **empty root
      password**, which `ssh -o BatchMode=yes` cannot offer
- [V] the identity marker is installed when absent. The stored
      `rpi4-wpe-2.48-20260820T124927Z` had **no `/etc/wk-image` at all**, so it
      would have booted invisible to `wk boot --status` and left the stick
      armed for ever — found by looking, not by a boot
- [V] the filesystem survives every rewrite: `e2fsck -fn` clean afterwards,
      and the marker, units and key all read back
- [V] idempotent — a second run reports the root already portable and rewrites
      nothing but the key
- [V] the manifest is rewritten **last and atomically**, the same publishing
      gate as a build; a crash before it leaves the old manifest, which
      `image_verify` then reports as a mismatch rather than destroying anything
- [V] the retargeted image **boots on the rpi4**, 2026-08-21: `wk boot rpi4
      --status` reports `bench mode -- system rpi4-wpe-2.48-20260820T124927Z`,
      the running system is on `/dev/sda2` with `root=PARTUUID=ea58701f-02`,
      `/etc/wk-image` names the image, `governor=performance`, swap off, and
      the **self-disarm ran** (partition 1's MBR type byte is `0x83`), so the
      next reboot falls through to the SD card with no hands on the board
- [V] and it **came back by itself**: the watchdog rebooted the board at
      18:10:52, the disarmed stick was skipped, and `wk boot rpi4 --status`
      reads `bench-device, host mode` with `usb_stick=disarmed`. Arm → bench
      system → self-disarm → self-return → host mode, no hands at any point
- [V] `disk_grow` falls back to `sfdisk -N 2` + `partx -u` where `growpart` is
      absent. The rpi4's rescue system is a Yocto image with **no apt**, so
      "install cloud-utils" is not advice it can take — it does ship sfdisk,
      partx and resize2fs. (This stick was written before the fallback existed,
      so its root is still the image's 3.8 GB of a 29.5 GB device)

### The known-good configuration, built and imported

`downstream-wpe-2.46-rpi4` — WPE 2.46 from the downstream
`WebPlatformForEmbedded/WPEWebKit` repo, 2026-08-21.

- [V] it builds, on a 24.04 host, with the pseudo bump and nothing else:
      `stage 'image' done`, all three artifacts, **zero errors and zero pseudo
      signatures**. Unmodified it dies (see the profile's note) — the same
      controlled experiment run both ways
- [V] reaching it needed two independent fixes, either of which alone leaves
      the build impossible: the `wpe` remote had never been applied to any
      workspace or to the base (`wk remotes --fix`), and `yocto_ensure_ws`
      fetched only from `origin` (`YOC_REMOTE`)
- [V] **the import path produces everything by itself**, which until now had
      only been proven through `wk sysimage retarget`: a portable
      `root=PARTUUID=…`, `wk-image.id` on the boot partition, `/etc/wk-image`,
      the driving ssh key, `/boot` in fstab by PARTUUID, five `wk-*` units, and
      `wic_of == disk_sha256`. No manual step, and `wk sysimage show` reports
      `disk.img matches its manifest`
- [V] the manifest records `branch_remote`, because a branch name alone no
      longer says which repository a system came from
- [V] and the two rpi4 images **share a disk signature** (`0x076c4a2a`): they
      come from the same recipe lineage, so the wic's MBR id is identical. That
      is the case `disk_unique_identity` exists for, and it is why the stamping
      is per *disk* at write time rather than per image at build time — an
      image-time fix would have made these two collide with each other
- [V] a `#` line inside the manifest heredoc is **not a comment** — it is prose
      written into the record. Three lines of explanation landed above
      `watchdog=900` in a real manifest; `manifest_get` greps `^key=` and
      shrugged, so it took reading one to notice. The heredoc holds key=value
      and nothing else, and the explanation lives in the shell above it

### Speedometer 3 on the rpi4 — a score, 2026-08-22

The first browser benchmark this fleet has produced on a bench device, end to
end from `wk pi deploy` and `wk pi bench`.

- [V] **`Speedometer-3: 1.35pt, stdev 3.5%`** (the harness's own summary), from
      4 iterations x 10 runs over 22 minutes. Per-iteration means 1.340, 1.359,
      1.342, 1.343 -- 0.6% spread between iterations, which is the number worth
      looking at: the run is repeatable even software-composited
- [V] the score is **not comparable with real hardware** and is not offered as
      one: compositing is pixman on the CPU because no display is attached. It
      proves the path, which is what it was for
- [V] the content came from **loopback** -- run-benchmark clones
      `WebKit/Speedometer.git` at `release/3.0` and serves it from its own http
      server (port 44647 this run), so the measured-run rule holds without
      anything extra
- [V] the harness is `run-benchmark`, not a URL in a browser: the benchmarks
      keep their score in the DOM, so a `run-minibrowser` launch can prove
      completion and never a number
- [V] verified the printed result against the board's own
      `/tmp/wk-bench-speedometer3.json` rather than trusting the command's
      output -- they match exactly
- [ ] `wk pi bench` prints `result after 0s` for a run that took 22 minutes.
      The elapsed counter is wrong; the result is not. Cosmetic, and worth
      fixing before anyone reads the timing as data
- [ ] the result is printed and not **saved**: nothing files it beside
      `wk bench`'s own runs, so `wk bench ls`/`compare` cannot see it. That is
      the remaining half of "record provenance next to wk bench's results"
      (docs/HANDOFF-pi-deploy.md)
- [ ] the manifest still records `display_forced` for this image although the
      forced mode was removed from the stick's cmdline by hand, so the run
      warned about a mode it was not using. A manifest describes the image and
      the disk was edited after it -- the provenance and the disk have to be
      reconciled

### Cross-building WebKit for the board — `--stage webkit`

The last stage, and the first time it had been run since the base image changed.
Both failures were in how this repo invokes `build-webkit`, not in WebKit.

- [V] **the target's cmakeargs must be merged, not replaced.** wpe-2.46 defaults
      `ENABLE_WPE_1_1_API` and `ENABLE_WPE_PLATFORM` both on and CMake refuses
      the pair ("You must disable one or the other"), so configure failed before
      a line compiled. `build-webkit` takes one `--cmakeargs` and the last wins,
      so passing ours would have dropped the target's — including the `bwrap`
      and `xdg-dbus-proxy` paths the sandbox needs. So `BUILD_WEBKIT_ARGS` is
      read out of `Tools/yocto/targets.conf`, its `--cmakeargs` split off, and
      ours appended: upstream stays the source of truth. Verified the parse
      keeps `--no-fatal-warnings` and all five target flags
- [V] **the job count has to be memory-sized.** `build-webkit` appends
      `-j$(numberOfCPUs)` unless `--makeargs` already carries a `-j`, so it ran
      **-j80** on WebCore's unified sources — the largest TUs in the tree — and
      the OOM killer took cc1plus three times. The symptom reads like a
      compiler bug: `fatal error: Killed signal terminated program cc1plus`
- [V] bitbake's own `PARALLEL_MAKE` *was* capped by the envelope (`-j4` from 79)
      — this stage is a cmake/ninja build bitbake never sees, so nothing applied
      that reasoning to it. Now sized with `build_jobs` at 2560 MB/job, the
      figure the failure itself established (80 jobs exceeded 125 GB): **47
      jobs**, and the build completed 3736/3736 with zero errors
- [V] the products are what the deploy needs: `MiniBrowser`, `jsc`,
      `WPEWebProcess`, `WPENetworkProcess`, and `libWPEWebKit-1.1.so` — the
      SONAME confirming `ENABLE_WPE_1_1_API=ON` took effect
- [ ] a wrapper whose command failed can still be `yocto_any_running` for a
      while afterwards (ninja finishes in-flight jobs), so a restart refuses
      with "already running" until `--stop`. Benign, and confusing the first time

### The skeleton on the board — what "skeleton" should mean

- [V] resumable, and it had to be: the first clone was killed at "Updating
      files: 42%" by a timeout on the *driving* end. The objects survived
      (2.8 GB, right branch) so a re-run completes the checkout rather than
      refetching, and `Tools/Scripts/run-benchmark` is verified present before
      `.part` is moved into place
- [V] `--quiet` on clone and checkout: git's progress meter wrote several
      thousand `Updating files: N%` lines into the log and buried everything else
- [ ] **and it is not really a skeleton.** `--depth 1` of WPEWebKit is
      **427,711 files and ~4.2 GB**, and checking that out onto a USB stick on a
      Pi 4 takes longer than cross-building WebKit did. The board only needs the
      scripts it runs there — `Tools/CISupport/built-product-archive`,
      `Tools/Scripts/run-benchmark`, `run-minibrowser` and their imports — so a
      sparse checkout of `Tools/` (~5,000 files) is the right shape. The
      depth-1 form is what the wiki and the `rpi3` skill document, which is why
      it was used first

### A compositor with no display — software rendering, proven 2026-08-22

"No display attached" and "no compositor" are different problems, and conflating
them is what made Speedometer look hardware-blocked when it is not.

- [V] weston's **RDP backend synthesises a head with no display hardware**:
      `weston --backend=rdp-backend.so --renderer=pixman --width=1280
      --height=1024` logs `Output 'rdp-0' enabled with head(s) rdp-0` on a board
      whose every HDMI connector reads `disconnected`. Nothing has to connect to
      the RDP port — it is a virtual output, not a remote desktop — so no client
      and no network are in the run
- [V] and a wayland client renders into it: `weston-simple-shm` stays up with
      empty stderr. That is the check that matters; the compositor starting
      proves less than a client drawing
- [V] the backend **refuses to start without keys** ("the RDP compositor
      requires keys and an optional certificate"), so `wk pi bench` generates a
      self-signed pair into `/etc/wk-bench` once, rather than depending on
      whatever somebody left on the board
- [V] `--shell=fullscreen-shell.so` is the wrong shell for this: it serves no
      xdg-shell, and a client asking for one dies with `wl_display@1: error 0:
      invalid object 6`. Watched `weston-simple-shm` do exactly that, then
      succeed under `desktop-shell.so`. Worth having found with a 20-line client
      rather than with a browser, where it would have read as a WebKit failure
- [V] the image ships `gl` and `pixman` renderers and **no headless backend**
      (drm, rdp, wayland, x11 only), and has no Xvfb — so rdp+pixman is the
      headless path here, not a preference among several
- [V] `wk pi bench` picks the session from the hardware: a connected HDMI
      connector takes **drm + gl**, otherwise **rdp + pixman**, and it records
      which. A pixman run is announced as not comparable with real display
      hardware, which is what makes it usable as a proof rather than a result
- [V] and this is **not** the same as `video=`: forcing a mode hangs vc4 in
      probe and takes the board off the network, where a virtual head touches
      no display driver at all

### Forcing a display on a headless board — and the incident that came with it

The rpi4 has no panel: both HDMI connectors read `disconnected`, weston's DRM
backend has no output, and `weston.service` fails. Speedometer is gpu-class by
this repo's own definition, so there is nothing to run it on.

- [V] `video=HDMI-A-1:1920x1080M@60D` is the KMS spelling of "drive this
      connector at this mode with no EDID" — a software dummy plug. What makes
      it the right override rather than `--software` is what it leaves alone:
      weston still composites on the real vc4/v3d through the real DRM path.
      `hdmi_force_hotplug=1` does **not** do this under `dtoverlay=vc4-kms-v3d`,
      which is why it is a kernel argument and not a `config.txt` line
- [V] declared as profile config (`image/<profile>/cmdline.txt.append`), applied
      by `apply_cmdline_append` in **both** the import and `retarget`, appended
      as a single line (the firmware reads the first line only, so a stray
      newline would drop the lot), idempotent, and recorded as `display_forced`
      in the manifest so no score from it can be read as a real-panel result
- [V] **and it does not boot.** Established by removing one variable: the same
      image on the same stick, with only `video=…` deleted from cmdline.txt,
      boots and is reachable in bench mode. Forcing a mode on a connector with
      nothing attached leaves vc4's KMS driver waiting on hardware that never
      answers, and a driver that hangs in probe hangs the boot. Disabled
      (`.disabled`) so no later write repeats it
- [V] "a display setting cannot stop userspace" is **false** when the setting is
      handled by a driver the boot waits on — the argument that exonerated
      `video=` was wrong, and one boot with one variable removed settled it
      where reasoning did not
- [V] so **Speedometer stays blocked on real hardware**: a physical dummy HDMI
      plug or a monitor. There is no software substitute that keeps the run
      gpu-class, which is what the plan requires
- [V] a cpu-class plan is the headless alternative and is not degraded for
      lacking a display (`cmd/bench`): JetStream in the **jsc shell**
      (`runner=jsc`) needs no compositor at all
- [V] an armed stick that boots-but-hangs is **sticky**: firmware finds
      `start4.elf` and keeps choosing it, so a power cycle re-enters the hang
      instead of falling through to the SD. The self-disarm is in the rootfs
      and never runs. `docs/HANDOFF-boot.md`'s residual hands-on trap is exactly
      this, observed
- [V] these Yocto images carry no `panic=10` where the distro profiles do — so
      a distro image that cannot find its root reboots and a Yocto one hangs.
      Not the cause here, and an undocumented asymmetry worth knowing
- [ ] the safe order, not taken: prove a kernel argument on the **SD** system
      first, where a bad one still leaves the stick an unarmed fall-through

### Reproducibility of the bench stick — the standing rule

**cattle, not pets** (`CLAUDE.md`) applied to the one device that
got hand-tuned. Restated 2026-08-21 by the user: *all changes to the stick and
the hardware setup must be fully reproducible.*

- [V] every edit the stick needed is now a code path, not a command someone
      remembered: the boot-partition id (`install_disk_id`, in the image), the
      unique disk signature (`disk_unique_identity`, at write time), the grow
      (`disk_grow`'s `sfdisk` fallback), the portable root
      (`retarget_root`/the import)
- [ ] **the stick itself is still a pet.** The one in the rpi4 today was
      produced by hand-stamping over ssh before those paths existed, so it is
      correct but not reproduced. The proof is one command --
      `wk sysimage write rpi4-wpe-2.48-20260820T124927Z --disk rpi4:/dev/sda`
      -- which now does all four by construction and would come out grown as
      well. It needs a confirmed erase, so it is a person's
- [V] a *backup* placed inside `~/.ssh/config.d/` is not a backup, it is a
      second config file: `Include config.d/*` globs it. Dropping
      `local.pre-bmc-fix` there left the stale `moosebmc` resolving even after
      the real stanza was corrected -- found by `ssh -G` still reporting the
      dead address with no matching stanza anywhere in `local`
- [V] `moosebmc` is repo-owned (`dotfiles/ssh/config`) rather than
      machine-local, because its address is not a preference:
      `bridge/hosts/tailnet-bridge-moose-bmc.conf` pins it by MAC and makes it
      the whole DHCP pool. `ssh -G moosebmc` resolves it from the repo with no
      machine-local copy, so `./setup` reproduces it anywhere
- [V] `config.d/local` is read **before** `config.d/wk-tools`, so a
      hand-written stanza overrides a repo-owned one rather than duplicating
      it. Some of those are deliberate (`moose` → localhost *on* moose), so
      `./setup` now warns about the shadowing instead of dropping it, and the
      hard drop stays limited to fleet machine names

### The remote wiring, applied rather than declared

- [V] the wiki's set (`WebKit JSC Container Development Setup`) is `origin`,
      `wpe`, `fork`, `forkwpe` plus `igalia`, and `wk_remotes` matches it
      exactly, with `igalia` a documented omission (ssh port 4429 is not in the
      egress allowlist)
- [V] but declaring is not applying, and nothing had applied it: the yocto
      workspace had **no `wpe` remote**, and the **base snapshot every new
      workspace starts from** was wired wrong four ways — `origin` accepting a
      push, no `wpe`, `fork` pushing to `git@github.com:` instead of the
      `github-webkit` ssh alias that selects the deploy key, and no `forkwpe`.
      `wk remotes` names all of it; `wk remotes --fix` repaired both
- [V] and it was load-bearing immediately: `wpe-2.46` cannot be checked out
      without that remote, so the known-good profile could not have been built
      at all. `yocto_ensure_ws` also fetched only from `origin` until
      `YOC_REMOTE` (2026-08-21) — two independent reasons the same build failed

### One disk, one identity — `disk_unique_identity`

The failure this exists for was reached on hardware 2026-08-21, and every check
on the writing side passed while it happened.

- [V] a raw image write copies the **MBR disk signature**, and `PARTUUID=` is
      that signature plus a partition number — so two disks written from one
      image answer to the same `root=`. Observed: the rpi4's SD rescue and its
      bench stick both carried `0x076c4a2a`, the board loaded the **stick's**
      kernel and mounted the **card's** root filesystem, and came up as a
      system that was neither. `/proc/cmdline` said
      `root=PARTUUID=076c4a2a-02`, `findmnt /` said `/dev/mmcblk0p2`
- [V] the symptom is *not* a failure. The board boots, answers ssh, and reports
      the right distribution — it is simply running the other disk's rootfs, so
      the written image's fleet integration appears to be missing and
      `usb_stick=armed` never clears because the self-disarm is in the rootfs
      that did not boot
- [V] the wiki's own manual ritual for this (`Building WPE evaluation images`,
      "USB Stick Alternative") edits `cmdline.txt` to `/dev/sda2` and `/etc/fstab`
      to `/dev/sda1` — the same two files, by **device path**. Device paths
      would have dodged this collision entirely, at the cost of an image that
      only boots from one kind of device. Keeping the root spec portable and
      making each *disk* unique buys both, but only because the stamping
      exists; portable-and-unstamped is the one combination that fails
- [V] the same class of ambiguity `image/profiles.sh` records for filesystem
      labels ("booted with both attached, `root=LABEL=writable` is ambiguous,
      and which disk wins is a property of enumeration order"), reached through
      the partition table instead. Making the root spec portable is what allows
      one image to boot from a card *or* a stick; it is also what makes two
      copies indistinguishable, and those are the same property
- [V] so uniqueness is stamped per **disk**, at write time, after the read-back
      verification (which necessarily stops matching once this runs), and both
      resolvers are rewritten together: `root=` in cmdline.txt for the kernel,
      `/boot` in `/etc/fstab` for mount(8). Rewriting only the first gives a
      system whose `/boot` is the *other* disk's
- [V] read back rather than trusted — `blkid -s PARTUUID` on the target — and a
      disk that did not take the new identity is refused rather than left
      ambiguous
- [ ] the distro builder's `LABEL=` images have the same disk-copy exposure
      (`relabel` makes each *image* unique, not each disk). Not yet reached: the
      fleet has one Ubuntu bench system per board

### The compressed copy and the image beside it

- [V] the bmap write path is opt-in by **provenance**, not by existence
      (`image_fast_path_ok`): the manifest records `wic_of`, the disk.img
      sha256 the compressed copy was derived from, and only an exact match
      takes the fast path. This was a live silent bug — the yocto import
      decompresses bitbake's wic and *then* edits disk.img (fleet integration,
      and now the retarget), while `disk_write_bmap` sends only
      `disk.wic.xz`. A write reported success and put a disk on a board with
      none of that work on it
- [V] an image with no `wic_of` (anything imported before the field existed)
      falls back to dd with a warning naming `wk sysimage retarget` — the safe
      direction to be wrong in
- [V] `refresh_fast_path` re-derives both from the edited image, and
      `xz -dc disk.wic.xz | sha256sum` then equals the manifest's
      `disk_sha256` exactly
- [V] it does **not** `fallocate --dig-holes` first. That would shrink the map
      and be wrong: a hole tells bmaptool "nothing here", so it leaves whatever
      the destination had. bitbake's holes are filesystem free space that was
      never written; holes dug by scanning for zeros are not the same set —
      ext4's inode tables are allocated and mostly zeros, and skipping them
      would leave a previous image's inode tables in place
- [ ] a bmap write onto **used** media, which is the case the above reasoning
      is about and which no test has exercised (the rpi4 has no bmaptool, so
      every write to it takes the dd path today)

### Building a Yocto system — `wk sysimage build downstream-yocto-wpe-2.48-rpi4`

The second builder behind the same verb. What is checked here is the seam
between the two, and the things the distro builder never had to think about: a
build that outlives its driver, a cache that outlives its workspace, and an
egress list that cannot be "every upstream in six layers".

- [V] `wk sysimage build downstream-yocto-wpe-2.48-rpi4 --dry-run` resolves the branch, the
      cross-target, the recipe, the stage list, the workspace, the two caches
      and the free disk, and builds nothing
- [V] the same command with a *distro* profile still takes the distro path
      unchanged — one verb, dispatched on `IMG_BUILDER`, and neither builder
      sees the other's flags (`wk sysimage build downstream-yocto-wpe-2.48-rpi4 --bogus` names the
      yocto flags; `wk sysimage build perf-linux-rpi4 --dry-run` is untouched)
- [V] the build workspace is created on demand and left on the profile's
      branch; a workspace on the wrong branch is checked out rather than built
      in — the branch *is* the version pin
- [V] `--stage layers` does the network-bound `repo sync` and nothing else, so
      an egress failure is not reported as a build failure
- [V] a workspace can actually be *created* from a non-SDK image. Three things
      had to give, each found by a container that exited on startup:
      `.wkdev-init` refuses to run outside a wkdev-sdk container (its test is
      `/usr/bin/podman-host`, so our image writes `/etc/wk-container` and SDK
      patch 13 accepts it); `utilities/podman.sh` then demanded `systemctl`
      because patch 13 made our container claim podman-host integration it does
      not have (patch 14 scopes that file to the file its own comment names);
      and Ubuntu base images ship an `ubuntu` user at **uid 1000**, so
      `useradd --uid 1000` failed quietly and the next step said "usermod: user
      'jmichaud' does not exist"
- [V] the workspace image is a **supported Yocto build host** — `ubuntu:24.04`,
      GCC 13, Python 3.12, glibc 2.39 — and not a layer on the wkdev SDK image.
      That is the single check that subsumes five others: on the SDK image
      (26.04 / GCC 15 / Python 3.14) the build failed five distinct ways, the
      last of which was unbounded. See `container/yocto/Containerfile`
- [V] the preflight names missing Yocto host tooling **in the first second**
      rather than after the layer sync. Verified against the plain SDK image,
      which lacked four (`makeinfo socat python3-git python3-pexpect`);
      `makeinfo` is in bitbake's `HOSTTOOLS` and therefore mandatory
- [V] the image is built on the **host** (`podman build`), not by `apt` inside
      the sandbox: a workspace has no interface and the allowlist has no Ubuntu
      archive, and neither of those is a thing to change to make a build work
- [V] it is built on demand, tagged with the base's own tag, and passed to
      `wk new` through `WK_SDK_IMAGE`
- [V] bitbake starts and parses the whole metadata — 3105 recipes, 5137
      targets, **0 errors**
- [V] `tmp/hosttools/gcc` points at the toolchain in force **now**. bitbake
      hands tasks `tmp/hosttools` rather than `PATH` and only creates a
      *missing* symlink, so a directory from an earlier run pins that run's
      compiler: the log once reported GCC 13.3 while the build used GCC 15, and
      `m4-native`/`unzip-native` failed identically. Verified by reading the
      symlink, never by reading the log
- [V] and **only** `tmp/hosttools` is discarded. Deleting native work trees
      alongside it — which an earlier version did — leaves `tmp/stamps` saying
      those tasks are done, and produces `do_patch` on an empty directory and
      `do_compile` with no makefile. `tmp/work` is not the unit of
      invalidation; `bitbake -c cleansstate` or the whole of `tmp` is
- [V] nothing in the workspace half can fail silently: an `ERR` trap prints the
      line and the command. Earned — a `find` on a not-yet-existing directory
      met `pipefail` and ended a detached run with no message at all
- [V] Chromium is **out** of the image by default (`YOC_CHROMIUM=0`), and
      `--chromium` puts it back. The branch's own `local.conf` adds it for
      WPE-vs-Chromium comparison; measured here it is
      `chromium-ozone-wayland` and `gn-native` at **21 GB of TMPDIR each**, plus
      rust/cargo/rust-llvm and mozjs behind them, and about half of the 13,379
      tasks
- [V] a knob that lands in `local.conf` takes effect on **every** run, not only
      the one that created the file. `configure_local_conf` rewrites its own
      block rather than returning early when its marker is present — otherwise
      `--chromium`, `--keep-work` and the job counts silently do nothing on the
      second invocation
- [ ] **OPEN — is the pseudo bump needed at all?** 24.04 + `wpe-2.46` is
      known-good unpatched, and the Yocto spec is *byte-identical* between
      `wpe-2.46` and `webkitglib/2.48` (same poky `6879650b`, same layers, same
      `local-rpi4-64bits-mesa.conf` — `diff` is empty). So the variable is not
      the branch, and cannot be: the reproducer involves no WebKit. Settle it by
      running the three-line reproducer in the known-good container, and by
      diffing `objdump -T $(command -v tar)` between the two.
      `docs/HANDOFF-yocto.md` has the full note. Delete `image/yocto/meta-wk` if
      it turns out unnecessary
- [V] `do_package` works, with pseudo bumped to 1.9.11 in
      `image/yocto/meta-wk`. Verified by the three-line reproducer and by the
      exact recipe that failed (`update-rc.d ... do_package: Succeeded`) rather
      than by a rebuild. The layer's rule is build-time recipes only: pseudo
      never enters the image, so this changes how the image is built and not
      what it contains
- [V] all three of scarthgap's pseudo patches had to be dropped — two are
      upstream by name, the third (`older-glibc-symbols.patch`) is safe to drop
      only because sstate is namespaced per build-host image, which the bbappend
      records
- [x] ~~**blocked:** `do_package` fails under scarthgap's
      `pseudo` on this host (`got *at() syscall for unknown directory`).
      Reduced to `pseudo bash -c 'tar -cf - . | tar -xf -'`, which fails with no
      bitbake involved, and fails identically in a **plain `podman run`** with
      default seccomp, default caps and network up. The overlay, the sandbox,
      the host tar and mixed sstate were each tested and refuted.~~ Fixed; the
      four refuted hypotheses are kept in `docs/HANDOFF-yocto.md` because each
      is the obvious guess
- [V] `wk sysimage build downstream-yocto-wpe-2.48-rpi4` compiles the whole
      image — completed and imported 2026-08-20 (store id
      `rpi4-wpe-2.48-20260820T124927Z`; see `docs/HANDOFF-yocto.md`)
- [V] the *import* half is verified independently of it, against a hand-made
      8 MB image in a throwaway cross-target directory: `disk.img` and
      `rootfs.tar.xz` land in the store, the manifest is written last,
      `wk sysimage ls` lists it and `wk sysimage show` re-hashes it and agrees. Worth
      testing separately precisely because a bug here would only surface after
      six hours of compiling
- [V] the import is a real file copy (`t_pull`), not `t_exec … cat`. That was
      the first attempt and it **silently corrupts binary**: a 1396-byte image
      arrived as 1399 bytes and xz refused it. Nothing at the call site could
      have seen that, which is why it is a named driver primitive
- [V] the imported image is checked for a readable partition table before it is
      hashed — a contaminated stream would otherwise hash perfectly and fail on
      the board
- [V] an interrupted import leaves a directory with no manifest, which
      `wk sysimage ls` reports as rubble by name (seen for real, from the corrupt
      first attempt)
- [V] the manifest records `cross_version`, the hash
      `cross-toolchain-helper` also installs in the image at
      `/usr/share/cross-target-info-version`, so "is this board running this
      image" is a string comparison rather than a belief
- [ ] a second `wk sysimage build` of the same profile reuses the sstate cache and
      is dramatically faster than the first
- [V] the store now holds both namespaces — `sstate/ubuntu-26.04/` from the
      abandoned 26.04 image (where uninative was disabled, so native sstate was
      pinned to that release) and `sstate/universal/` from this one. Dead weight
      rather than a problem, and `wk gc` reports the directory as kept so it is
      visible rather than silent
- [V] the caches are store-backed and shared, tested from a **fresh, unrelated
      workspace**: `/cache/yocto` is a bind of
      `$WK_STORE/cache/yocto`, `DL_DIR`/`SSTATE_DIR` point into it, it is
      writable, and it already holds 24 GB / 1726 download entries, 3263 sstate
      packages — none of which that workspace put there
- [V] `wk rm` on that workspace left both caches **byte-identical**
      (`downloads` 25357468380 → 25357468380 bytes) while the workspace
      directory and container went. `t_destroy` removes only `$ws`; the cache is
      not under it. Shown twice, the second time across a **base-image change**:
      the workspace was destroyed and remade on a different image and the 24 GB
      of `DL_DIR` was still there to reuse
- [V] `DL_DIR` reuse is real, not just written: the second `--stage fetch` pass
      reported *1490 of 1492 tasks didn't need to be rerun*
- [V] **sstate read-reuse works** — but the first demonstration of it was also
      a bug. The first build on the 24.04 image reported *Sstate summary: Wanted
      6369 Local 3007 Missed 3362 (47% match)*, reusing 3007 packages written
      under the **abandoned 26.04 host**, where bitbake had disabled uninative
      "so that sstate is not corrupted". Five recipes then failed in
      `do_package` with `pseudo` unable to intercept `*at()` syscalls
      (`got *at() syscall for unknown directory`, `tar: Cannot mkdir: Bad
      address`). *Target* sstate paths carry no host marker, so bitbake had no
      way to refuse the mix
- [V] so `SSTATE_DIR` is namespaced by the build-host image tag
      (`cache/yocto/sstate/wk-yocto-host-24.04-<digest>`), which makes the mix
      impossible rather than documented. `DL_DIR` stays shared — a source
      tarball is a source tarball whatever built it, and it is the 24 GB that is
      expensive to refill
- [V] uninative is **enabled** on this host, which is what makes native sstate
      portable rather than namespaced per host release. Checked by evidence, not
      by the `Build Configuration` header — that prints
      `NATIVELSBSTRING = ubuntu-24.04` before uninative's own event handler
      rewrites it to `universal<gcc>`. The evidence is a `sstate/universal/`
      subtree and `downloads/uninative`, plus the *absence* of the "your host
      glibc is newer than uninative's — disabling" warning that the 26.04 image
      produced
- [V] the trap underneath all of this is bitbake's **environment filtering**:
      `DL_DIR` and `SSTATE_DIR` are set by `targets/container.sh` and are
      silently *dropped* unless named in `BB_ENV_PASSTHROUGH_ADDITIONS`, so a
      build can look perfectly healthy while writing its cache into the layer
      `wk rm` deletes. They are named there *and* written into `local.conf`,
      and the 24 GB in the store is the evidence that it took
- [V] `wk sysimage build <profile> --stop` stops a detached build, killing bitbake
      as well as the wrapper, with SIGTERM rather than SIGKILL so bitbake closes
      its own state and the sstate cache stays resumable
- [V] a killed build leaves no reclaimable-forever lock behind: the symlink
      lock (2026-08-20) writes the holder *with* the lock, so a lock naming
      nobody cannot exist and a dead holder's lock is broken by compare-and-
      swap — the atomic-mkdir failure this line used to record (`wk rm`
      waiting out its whole timeout on an empty lock directory) is closed
- [V] editing `container/yocto/Containerfile` rebuilds the workspace image. The
      tag carries a digest of the spec, because keying it on the base tag alone
      meant `podman image exists` said yes, the edit never reached any
      workspace, and it looked exactly like the change not working
- [V] `--stop` matches on **this workspace's build directory**, never on the
      word `bitbake`. A wkdev container shares the host's PID namespace, so
      `pkill -f bitbake` from inside one reaches every bitbake on the machine —
      another workspace's build included
- [ ] `--keep-work` turns off `rm_work`, and `bitbake -c menuconfig
      virtual/kernel` in `--bitbake-dev-shell` then has the kernel tree to
      configure (the wiki's 16 KB-page / 36-bit-VA flow)

### Detached, and it really is detached

- [V] a stage started by `wk sysimage build` survives the driving process being
      killed. The reason this needs its own line: `setsid nohup` through
      `podman exec` **does not** — measured, with a detached `sleep 3; echo`
      that never wrote its file — so the container driver detaches with
      podman's own `exec -d` (`t_spawn`), and the generic `setsid nohup` is
      right only where the far side is reached over ssh
- [V] `podman exec -d` needs `--user` and a login shell spelled out, because it
      bypasses `wkdev-enter`: without them the build runs as **root** with no
      SDK `PATH`, and fails for reasons that have nothing to do with the build
- [V] the detached stage's log is followable with a plain `tail -f` on the
      host, no podman involved — the workspace's `$HOME` *is* `$ws/home`
- [ ] `--detach` returns immediately, and re-running the same stage later
      re-attaches rather than starting a second one
- [ ] a stage still running is refused by name rather than started twice
- [ ] "finished" is decided from the wrapper's own marker line, not from an
      exit status — a process nobody forked cannot be waited for, and a build
      whose container was killed leaves a log with no marker
- [ ] a long-silent bitbake task is **reported and not killed**, unlike a
      stalled compile: `run_watched`'s abort would cost hours that sstate
      cannot always give back

### The Yocto egress widening

- [V] the build is pointed at the Yocto source mirror first (`own-mirrors` +
      `SOURCE_MIRROR_URL`), and a full `--runall=fetch` pass over **1492 fetch
      tasks** then needs only **seven** hostnames in total. That number is the
      measurement the design rests on, not an estimate: `yoctoproject.org`,
      `openembedded.org`, `googlesource.com`, `freedesktop.org`, `kernel.org`,
      `videolan.org`, `metacpan.org`
- [V] `BLOCKED_NETS` is unchanged, so none of the three can become a route onto
      the LAN or the tailnet
- [V] a recipe whose source the mirror lacks is refused by the proxy and the
      refusal names the host — `gitlab.freedesktop.org` for `polkit`, 2900 tasks
      in. The mirror carries oe-core's sources and not meta-openembedded's,
      meta-raspberrypi's, meta-clang's or meta-webkit's, so this is the normal
      case for those four layers rather than an edge one
- [V] `--stage fetch` (`bitbake --runall=fetch -k`) names **every** such host in
      one pass instead of one halted build per host. `-k` is the load-bearing
      flag; without it the allowlist could only grow one host per full run
- [V] with all seven hosts allowed, the pass comes back clean: *Attempted 1492
      tasks … all succeeded*, 24 GB in `DL_DIR`, so the compile that follows
      needs no network at all
- [V] the yocto hosts are allowed on **port 80 as well as 443**. poky's built-in
      `PREMIRRORS`/`MIRRORS` are `http://` URLs, so 443-only refuses the mirror
      itself (`DENY downloads.yoctoproject.org:80`) and sends every fetch
      upstream — the inverse of what pointing at a mirror was for
- [V] two stages cannot run in one workspace: they share a bitbake build
      directory, and `yocto_spawn` refuses on *any* live stage rather than only
      the one asked for. Found by a `--stage fetch` starting on top of a live
      `--stage image` and reaching two cookers

### Reclaiming — `wk gc` knows about images

- [V] rubble from an interrupted image build is removed
- [V] all but the **newest complete image per profile** is removed, and the
      newest is kept unconditionally — unlike a base snapshot there is no pin
      to consult, because an image is written to a device and the device holds
      no reference back
- [V] anything under the retired serve/ tree is reclaimed whole (`wk serve`
      is gone, 2026-08-21)
- [V] `cache/images` (the pinned distro bases, 1.5 GB) is reported as kept
      rather than pruned — it is re-downloadable but slow, and growth should be
      visible rather than silent

### The registry: cattle, not pets

- [V] every machine is a conf under boot/machines/ (2026-08-21) that loads
      standalone and carries a driver that exists, a role and an os the
      vocabulary knows, a profile and a note — and `machine_list` and the
      directory are the same set, so a conf cannot exist invisibly. In
      `wk selftest --quick`.
- [V] each conf opens with its device's from-nothing recipe, so the file that
      defines a machine says how to reproduce it (hand-checked; the confs are
      the ledger, and there is no second copy of it)
- [V] a machine whose `os=` does not match this host is refused by `wk boot`
      with the conf named — probing a MACH_LOCAL machine from the wrong host
      used to answer confidently about the wrong computer
- [V] `wk doctor` ends its config half with the machine-local-state section:
      everything a rebuild cannot get from this repo, each entry declared as
      regenerable, re-authable, or backed-up, with how to get it back

### The EEPROM's only writer — `wk pi boot-order`

- [V] `--dry-run` prints a unified diff of the firmware configuration and
      writes nothing
- [V] the network nibble and the network-boot keys (`TFTP_IP`, `CLIENT_IP`,
      `SUBNET`, `GATEWAY`) come out of every order unconditionally — no lane
      here boots over the network, and a board still trying a source nothing
      answers is a stale fact that reads as a live one
- [V] the transform is idempotent and reversible (`--revert` is `local`)
- [ ] an actual write, confirmed, applied, and read back on a board that is
      not this session's workstation

### Still owed here

- [V] the boot time a status reports equals `/proc/stat`'s btime on the
      machine. Two separate bugs produced a plausible-looking wrong answer
      here: `date -u -d "$(uptime -s)"` re-reading a local string as UTC, and
      an `awk '{print $2}'` whose `$2` did not survive three shells on the way
      through ssh. A remote one-liner that reports a *number* is worth
      checking against the source, because a wrong one does not look wrong.
- [V] `wk status` ends with the fleet block (2026-08-21): one line per
      machine — role, mode, and the media wk owns with what is on it right
      now (the rpi4's stick and its system + armed/disarmed, the Mac's bench
      volume attached-or-MISSING) — probed in parallel, one subshell per
      machine so no driver's overrides leak into the next, read-only, with a
      short fleet timeout so a powered-off board costs seconds. A machine in
      host mode with an unspent-looking arming record is flagged **armed
      for <id>** on its line. Honesty rules verified by inspection: a
      MACH_LOCAL or guest machine that cannot be probed from this host says
      "unknown from here" rather than reporting the driving machine's own
      marker, and the block is skipped inside a workspace (no network, five
      timeouts to say so).
- [V] the tailnet overlay for a buildroot image assembles from the same pin as
      everything else: `image/buildroot/tailnet-overlay.sh <arch> <staging>`
      reads the version and sha256 from
      `meta-wk-tailnet/.../tailscale-release.inc`, verifies the tarball before
      unpacking it, and installs the *same* `wk-tailnet-join` file the Yocto
      images run — three installers (image recipe, buildroot overlay, `wk pi
      setup`), one pin, one join script. Checked for `arm`: 32-bit ARM,
      statically linked, 64 MB, plus an `S99tailscale` for BusyBox init, which
      is what that image has instead of systemd units. It carries no credential:
      the key and the fleet name arrive with the card.
- [V] **wpe-2.38 for the rpi3 builds, with tailscale in it** (2026-08-24, on
      this Mac's podman VM). It is a *buildroot* image and not a Yocto one,
      which was itself part of the confusion: `wpe-2.38` carries no
      `Tools/yocto` (`wpe-2.42` is the earliest that does), and what matches
      this board is `WebPlatformForEmbedded/buildroot`'s
      `raspberrypi3_wpe_2_38_cog_defconfig` — buildroot 2020.02, WPE WebKit
      2.38 at `79cd67f78d314cc5507723552b111ccc890f2e62`, cog 0.16.0, kernel
      `rpi-5.15.y`, dropbear, BusyBox init. Verified in the built tree:
      `tailscale`/`tailscaled` present and ELF 32-bit ARM statically linked,
      `wk-tailnet-join` and `S99tailscale` installed (and `rcS` does run
      `/etc/init.d/S??*`), `/var` and `/var/lib` real directories on the rootfs
      so tailnet state survives a reboot, **no `/etc/wk`** — the image carries
      no credential — root's home `/root` for the driving key dropbear reads,
      and `sdcard.img` partitioned p1 32M FAT32-LBA bootable + p2 700M Linux.
      Three things had to be found on the way, all recorded where they bite:
      host-python's arm64 libffi (`image/buildroot/external/`), this
      defconfig's tar-only filesystem output — it builds no `rootfs.ext4`, so
      the board's own genimage config had nothing to assemble — and an overlay
      that must contain only world-readable files, because `target-finalize`
      rsyncs it as the build user (a 0700 state directory failed the build with
      rsync error 23; the mode is set at boot instead).
- [ ] the 2.38 image reaches the board **without anyone touching it**. The
      card-carrying plan was wrong and is withdrawn: `wk sysimage write` already
      writes a disk attached to a machine it reaches over ssh, which is how the
      rpi4 writes its own stick while running its rescue. The rpi3 cannot only
      because it has one medium and one system. The remote transition, in order:
      write a rescue to a USB stick from the running board; flip the SD's MBR
      partition type to 0x83 so the ROM skips it; let the USB rescue repartition
      and rewrite the SD as rescue + bench with free space left over; flip back.
      One window without fall-through, at the step where the SD is disarmed and
      the stick has not yet proved it boots.
- [!] nothing in this fleet can power a Pi on. moose has a BMC; the boards have
      nothing, and the rpi3 is off right now. Every remote path assumes power,
      so "never touch the boards" is not literally met until a switchable plug
      or PoE exists. Hardware, not code.
- [V] there is **one** privileged path to a card and no fallback (2026-08-24).
      Everything that needs privilege on the machine holding the disk goes
      through `admin/wk-card-priv`: the write, the unmount, the identity stamp,
      the fleet integration, the tailnet seed, the grow, the read-back verify.
      The inline-`sudo` alternative was written and removed the same day — a
      fallback is the path that runs on the machine nobody tested, on the day
      something is already wrong. `boot/disk.sh` now contains no inline `sudo`
      at all; the single remaining mention is the helper's own invocation inside
      the decompression pipeline, where `zstd -dc` runs unprivileged and only
      the plain stream reaches the writer.
- [V] the bmaptool writer is gone, and with it a second hardware-tested path.
      It existed to consume `disk.wic.xz` and `disk.bmap` — store artifacts — so
      it had no inputs once the store went. `disk_write_dd` is now `disk_write_stream`
      with a file on stdin rather than a second implementation of the same
      thing. CLAUDE.md, "One path, not two", carries the rule this came from.
- [V] "may this disk be written" has one implementation, in the helper's gate.
      `disk_refuse_unless_safe` used to be a second copy of the same checks and
      now asks the helper, adding only the question the helper cannot answer:
      does the image fit. Two copies of a safety rule is one that can drift into
      permitting what the other refuses.
- [V] the card helper (`admin/wk-card-priv`) may only write a **usb or mmc whole
      disk the machine is not running from**, checked against real hardware on
      the rpi4 (2026-08-24). `/dev/sda` — USB, so it *passes* the transport
      check — was refused because the board's root is `/dev/sda2`, which is the
      case that matters: every board here boots from exactly the kind of device
      the transport check allows, so the boot check is the load-bearing half.
      `/dev/mmcblk0` (mmc, not booted) was permitted. Also refused: a loop
      device (`is a 'loop', not a whole disk`), a partition rather than a disk,
      `/dev/null` (not a block device), a path traversal, and a device name with
      a shell metacharacter in it. It is the second NOPASSWD grant in this repo
      and CLAUDE.md now says so, with what earns one: a fixed verb list, no
      passthrough, no argument that becomes part of a command, and a gate
      narrower than the capability sounds.
- [V] the card-side fleet integration was **run against a real device and read
      back** (2026-08-24, on the rpi4 in bench mode, which is the one machine in
      the fleet where privileged commands need no password): a fixture with the
      real layout — FAT boot partition, ext4 root, a passwd file — took the
      identity marker, the driving key (101 bytes, `ssh-ed25519 …`, 0600
      root:root inside a 0700 `.ssh`), `/etc/wk/tailnet.conf` (`hostname=rpi3`,
      `tag=tag:wk`), the auth key at 0600, and `wk-image.id` on the FAT. A
      fixture *without* `wk-tailnet-join` was correctly skipped by the tailnet
      seeding rather than left holding a credential it cannot spend.
- [!] `disk_install_fleet` shipped the driving key over one stdin split on a
      blank line, and the key was lost every time: `sed` reads in buffered
      chunks, so the first reader swallowed the whole stream and the second got
      nothing. The card came out with a perfect marker and an **empty**
      authorized_keys — a board that boots and cannot be reached. Fixed by
      passing both on the command line, which is safe because neither is a
      secret (the marker is metadata, the driving key is public; the tailscale
      auth key still goes over stdin, where one reader consumes it). Found by
      reading a written card back, not by review — the failure is invisible from
      the writing side.
- [V] `wk sysimage write --from <path|vm:path>` writes an image that is in no
      store (2026-08-24): it reads the bytes where the builder left them —
      including out of this machine's podman VM, which a macOS driver cannot
      open directly — hashes them for the identity, refuses a disk that cannot
      hold them, streams them to the machine the disk is attached to, and then
      edits the *card*: unique disk identity, identity marker, driving key,
      tailnet name and key. No manifest, no id lookup, no import, and no Linux
      image-editing tools on the driving machine. Exercised as far as hardware
      allows: it read the 733 MB 2.38 image through the VM and refused
      `rpi5:/dev/mmcblk0` with the disks that *are* attached, because there is
      no card in the reader.
- [V] a store-free write does **not** grow the root partition unless asked.
      Growing to fill is what leaves a card with one system and no room for a
      second, which is the state that makes a board need a person — so `--grow`
      is opt-in on this path and the reason is printed when it is skipped.
- [V] every builder's output has somewhere `wk gc` looks. There is no store, so
      output lives where the builder left it and the set of places grows with
      the set of builders; `image_build_locations` declares them with a
      `# builder: <name>` annotation each, `wk gc` walks them and reports every
      one with its size, and `wk selftest` checks the annotations against the
      `IMG_BUILDER` values in `image/profiles.sh`. Reporting rather than
      deleting: a buildroot tree is hours and a yocto cache is what makes the
      next build minutes, so `--purge-builds` is where that decision is made.
- [ ] **no image store** (decided 2026-08-24, `wk help images`): a workspace
      produces an image, wk detects that it did, and `wk sysimage write` streams
      it to a card. What leaves the tree: `images/<id>/` with its `disk.img`,
      `disk.wic.xz`, `disk.bmap` and manifest; the import step and the rubble a
      half-finished one leaves; `image_verify`, `image_latest`, `image_ids`,
      `image_complete`, `image_fast_path_ok` and the fast-path re-derivation;
      and `wk sysimage rm`'s reason to exist. Eleven files touch it today —
      `cmd/sysimage`, `lib/image.sh`, `image/yocto.sh`, `image/pmos.sh`,
      `image/fetch.sh`, `boot/disk.sh`, `cmd/boot`, `cmd/bridge`, `cmd/gc`,
      `cmd/disk`, `cmd/selftest`. `image/fetch.sh` is the exception and stays: a
      downloaded distro base keyed by its checksum is a re-fetchable input, not
      a built output.
- [ ] identity without a catalogue: `/etc/wk-image` is written at *write* time
      from the profile, the workspace, the build time and the sha256 of the
      bytes as they stream past. Stronger than a store id — it names content
      rather than a slot — and free, since the writer reads every byte anyway.
- [ ] every image edit moves onto the card, on the machine holding the reader:
      identity marker, driving key, unique disk identity, root retarget, cmdline
      append, tailnet key and name. That machine has the tools (checked
      2026-08-24 on the rpi5: sfdisk, debugfs, mcopy, bmaptool, e2fsck) and has
      to be reachable for the write to happen at all — so a macOS driver needs
      no Linux tooling and the "mac portion" of `wk sysimage` stops existing.
- [ ] the order: the **buildroot lane is new code, so write it store-free from
      the start** — it proves the model end to end without touching a working
      path, and it is what the WPE 2.38 image needs anyway. Then the yocto lane,
      then pmos/bridge last, because that one is exercised only with a phone in
      hand and is the worst thing to refactor blind.
- [V] a base image is never mistaken for a bench system (2026-08-24). Every
      image wk writes carries `/etc/wk-image`, so `b_probe` calling anything
      with a marker `bench <id>` meant a board that had fallen back to the
      medium it is never armed from — an unarmed stick, a stick that would not
      boot, or the rpi3, which has only ever had one medium — reported bench
      mode and `wk pi bench` measured it. The evidence that settles it was
      already declared and simply never asked for: `MACH_ROOT` is the root
      device that is never written to, `MACH_DEVICE` is the medium systems are
      written onto, and `b_system_kind` compares the running root against both.
      `wk pi bench` refuses a base image by name, `wk boot --status` says "base
      image" rather than "bench mode", and `wk status`'s fleet line says "not a
      bench system". A board on its base image is also *armable* again — that
      state used to match `bench*` and be refused with "already in bench mode".
- [V] a rescue image gets no self-return watchdog and no self-disarm
      (`IMG_ROLE=rescue`, image/profiles.sh; recorded as `role=` in
      `/etc/wk-image`). Both units exist to hand a machine *back* to what it
      falls back to, and a rescue image is that thing — so on one, the watchdog
      is a 15-minute reboot in the middle of whatever card the helper is
      writing, and the self-disarm parks a medium the image is not even on.
- [!] the rpi4's SD card holds a *bench*-profile image acting as its rescue, so
      it carries a 900-second self-return watchdog and reboots itself every 15
      minutes when the board is sitting on it. Rewrite it with a rescue-role
      image; needs a build and a card.
- [ ] the rpi3 gets its second system, on the same card. Decided 2026-08-24 and
      written up in `wk help hardware` ("why the three Pis are arranged
      differently") with the priority order it follows in CLAUDE.md: quality of
      results first, durability second, fewest configurations third. The board's
      Ethernet is a USB device, so its bench root belongs on the SD; its ROM
      prefers the SD, so the rescue lives there too. Left to build, in order:
      a rescue-role system for the first slot and a bench system for the second;
      a slot-aware `wk sysimage write` (today it writes one whole system to one
      whole device); stage-2 arming in `boot/pi-sd.sh` — `root=` plus the bench
      kernel installed onto the shared boot partition — and the revert that a
      stage-2 arming needs, since a kernel that cannot mount the armed root
      panic-loops; and the BusyBox equivalents of the watchdog and self-disarm.
- [ ] the rpi4 keeps two media and the rpi5 keeps its NVMe host install, and
      neither is changed for uniformity's sake: separate media are separate
      failure domains and the rpi4's fall-through is enforced by firmware, both
      of which outrank the third priority. What is shared across all three is
      the code above the medium — one image model, one write path, one arming
      interface, one set of refusals — which is where "don't make me test on
      three boards" is actually paid for.
- [V] the tailnet overlay for a buildroot image assembles from the same pin as
      everything else: `image/buildroot/tailnet-overlay.sh <arch> <staging>`
      reads the version and sha256 from
      `meta-wk-tailnet/.../tailscale-release.inc`, verifies the tarball before
      unpacking it, and installs the *same* `wk-tailnet-join` file the Yocto
      images run — three installers (image recipe, buildroot overlay, `wk pi
      setup`), one pin, one join script. Checked for `arm`: 32-bit ARM,
      statically linked, 64 MB, plus an `S99tailscale` for BusyBox init, which
      is what that image has instead of systemd units. It carries no credential:
      the key and the fleet name arrive with the card.
- [ ] **wpe-2.38 for the rpi3 builds.** It is a *buildroot* image and not a
      Yocto one, which is itself part of the confusion: `wpe-2.38` carries no
      `Tools/yocto` (the earliest downstream branch that does is `wpe-2.42`),
      and what matches the board is
      `WebPlatformForEmbedded/buildroot`'s `raspberrypi3_wpe_2_38_cog_defconfig`
      — buildroot 2020.02, WPE 2.38 + cog on weston, cortex-a53 32-bit, kernel
      `rpi-5.15.y`, dropbear, BusyBox init, genimage → `sdcard.img`. Building
      now in an `ubuntu:20.04` container in this Mac's podman VM (`-j6`: 19 GB
      of RAM against ~2 GB per WPE compile), tree and downloads under
      `/var/lib/wk/cache/buildroot`, which `lib/store.sh` already reserves and
      `targets/container.sh` already exports `BR2_DL_DIR` for. Deliberately a
      scouting run outside `wk`: buildroot 2020.02 on a modern host was the
      unknown, and the lane should be written around what actually works. What
      it found, and the reason the run was worth making: the toolchain builds
      fine (host-gcc-final 9.2.0), but **host-python-2.7.17 cannot build on an
      arm64 host** — its bundled 2013-era libffi's `aarch64/sysv.S` no longer
      assembles, and the build dies at `sharedmods`. An architecture problem and
      not an old-distribution one: on x86_64 that file is never compiled, which
      is why this tree has always built on moose. Fixed in
      `image/buildroot/external/` — a BR2_EXTERNAL tree this repo owns rather
      than a patch against somebody else's vendor branch, since buildroot
      already gives the *target* python `--with-system-ffi` and ships a
      host-libffi package and simply never joins the two for the host build.
- [V] an image without systemd does not get systemd units written into it and
      called a watchdog. `install_units` reads the init out of the rootfs
      (`/lib/systemd/systemd` or `/usr/lib/systemd/systemd`, via debugfs) and
      refuses rather than filling `/etc/systemd/system` on an image where
      nothing will ever start them — which would report a board as carrying a
      self-return watchdog it does not have, and the watchdog exists precisely
      for the run nobody is watching. The rpi3's WPE 2.38 image is the first
      one whose answer is "no": buildroot, BusyBox init.
- [ ] the BusyBox half of the fleet integration — S-script equivalents of the
      self-return watchdog and the self-disarm. Deliberately not written yet:
      on the rpi3 there is nothing for a self-disarm to disarm until its card
      has a second root slot, and a watchdog with nowhere to return to is a
      reboot loop rather than a safety net. It becomes real with the two-slot
      card (docs/HANDOFF-boot.md, "The rpi3").
- [V] a base image is never mistaken for a bench system (2026-08-24). Every
      image wk writes carries `/etc/wk-image`, so `b_probe` calling anything
      with a marker `bench <id>` meant a board that had fallen back to the
      medium it is never armed from — an unarmed stick, a stick that would not
      boot, or the rpi3, which has only ever had one medium — reported bench
      mode and `wk pi bench` measured it. The evidence that settles it was
      already declared and simply never asked for: `MACH_ROOT` is the root
      device that is never written to, `MACH_DEVICE` is the medium systems are
      written onto, and `b_system_kind` compares the running root against both.
      `wk pi bench` refuses a base image by name, `wk boot --status` says "base
      image" rather than "bench mode", and `wk status`'s fleet line says "not a
      bench system". A board on its base image is also *armable* again — that
      state used to match `bench*` and be refused with "already in bench mode".
- [V] a rescue image gets no self-return watchdog and no self-disarm
      (`IMG_ROLE=rescue`, image/profiles.sh; recorded as `role=` in
      `/etc/wk-image`). Both units exist to hand a machine *back* to what it
      falls back to, and a rescue image is that thing — so on one, the watchdog
      is a 15-minute reboot in the middle of whatever card the helper is
      writing, and the self-disarm parks a medium the image is not even on.
- [!] the rpi4's SD card holds a *bench*-profile image acting as its rescue, so
      it carries a 900-second self-return watchdog and reboots itself every 15
      minutes when the board is sitting on it. Rewrite it with a rescue-role
      image; needs a build and a card.
- [ ] the rpi3 has no bench system at all and therefore cannot be measured: one
      medium, so `MACH_ROOT` is on `MACH_DEVICE` and every root on that card is
      the base image. Which shape fixes it turns on a fact nobody here has
      checked — this repo assumed the USB-boot OTP fuse is unburned, and the
      owner says it is already blown (2026-08-24; automatic on a 3B+).
      `cat /proc/device-tree/model` and `vcgencmd otp_dump | grep '^17:'` settle
      it on a board that is up. Blown → a stick is the bench medium and
      `boot/pi-usb.sh` is most of the answer, minding that the Pi 3's ROM tries
      the SD *before* USB. Unblown → a second root partition on the card. Either
      way `boot/pi-sd.sh` needs an arming model; today it refuses rather than
      pretends.
- [V] no fleet probe can outlive its ceiling, and one that hits it still gets a
      line (2026-08-24). `wk status` hung outright: the rpi4 is reached through
      `tailnet-bridge-generic`, and options on an ssh command line do not reach
      a ProxyJump child — so the hop ran with `StrictHostKeyChecking=ask`,
      found no known_hosts line for the bridge's tailnet name (its `.local`
      name and three LAN addresses had one), and asked the question on the
      terminal from a subshell whose stdout was a file. Two fixes, and both are
      needed: the bounds a jump hop *can* read (below), and a wall-clock
      ceiling in the fleet block, because the next unbounded thing will not be
      that one. A probe killed for time prints "no answer within Ns" rather
      than vanishing, since a missing line reads as a machine that is not in
      the fleet.
- [V] a jump host's stanza carries the bounds its jump cannot inherit:
      `accept-new` and a `ConnectTimeout` for every name any `ProxyJump` in
      `dotfiles/ssh/config` names. Checked by resolving that file the way the
      jump child resolves it (`ssh -G -F`), not by reading it. Each bridge also
      gets a `HostKeyAlias` so its tailnet name, its `.local` name and its
      addresses are one identity — without it, `accept-new` silently accepts a
      key that has *moved* the first time the phone is asked for under a name
      not used before, which is the one thing that mode exists to refuse.
- [V] `capped`'s ceiling reaches the whole process group, and a command that
      finishes early is not held for the rest of it. It lives in
      `lib/common.sh` because `wk bridge`'s probes and `wk status`'s fleet
      block need the same one, and two copies of a bound are two bounds that
      drift.
- [!] rpi3 and rpi4 are the only fleet machines not on the tailnet, and so the
      only ones whose reachability is written down anywhere:
      `raspberrypi3.local` for one, `10.99.1.10` behind `ProxyJump
      tailnet-bridge-generic` for the other (`dotfiles/ssh/config`), plus
      `image_addr`'s MAC → ARP → mDNS ladder and `MACH_MAC` in
      `boot/machines.sh` for a booted bench system. The rule is that none of
      that exists — a node is on the tailnet and its name is the whole address
      (CLAUDE.md, "Cattle, not pets"). What stands in the way is a decision, not
      an accident: the bench images carry no tailscale ("no tailscale and never
      will", `image/profiles.sh`), so the fix belongs in provisioning — an image
      that joins the tailnet on first boot — and not in `wk pi setup` against a
      running board, which is an in-place upgrade of a guest. Until then the jump
      hop stays, and with it the bounds its stanza has to carry.
- [V] one tailscale auth key for the whole fleet, in one place, used by every
      path that joins anything to it: `wk pi setup`, `wk bridge setup` and the
      Mac bench volume all resolve it through `wk_tailscale_authkey`
      (`~/.config/wk/tailscale-authkey`, 0600, machine-local, never in the
      tree), and none of them reads one from a prompt of its own — a join that
      needs somebody at a keyboard is how a device ends up staying off the
      tailnet with its address written down instead. What the key has to be is
      documented where it is resolved: tagged `tag:wk`, reusable, **not**
      ephemeral, longest expiry. The tag is the permission and a tagged node
      never key-expires; an ephemeral node is removed from the tailnet the
      moment it goes offline, taking with it the name that is its whole address.
      Declared in `wk doctor`'s machine-local section as `re-authable`, because
      a fresh tagged key is as good as the old one and the nodes already joined
      do not care.
- [V] no `tailscale up` in the tree puts the key on a command line — every one
      passes `--auth-key file:<path>`, because argv is world readable in /proc.
      `bridge/provision.sh` had this right and says why; `wk pi setup` and the
      Mac bench first boot were fixed to match (2026-08-24), and the Mac install
      now advertises `tag:wk` like every other wk-managed node instead of
      joining untagged and key-expiring in 180 days.
- [V] the Yocto images are built with tailscale in them, from a layer of their
      own: `image/yocto/meta-wk-tailnet`, added to bblayers beside `meta-wk` and
      deliberately *not* part of it — that layer's rule is that it may change
      how an image is built and never what is in it, and this changes what is in
      it. `IMAGE_INSTALL:append = " tailscale"` is written by
      `image/yocto-build.sh`, so "this image is not stock" is two greppable
      places rather than something hidden in a layer. `wk sysimage build
      … --no-tailnet` builds without it, for a measurement that has to compare
      against numbers taken before it existed.
- [V] the tailscale release is pinned in one file for both halves of the fleet:
      `meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc` carries
      the version and a published sha256 per architecture, the recipe `require`s
      it, and `wk pi setup` reads the same file with `sed` — so the board that
      gets tailscale pushed onto it and the image that ships with it cannot
      drift. `wk pi setup` now verifies that checksum before unpacking, and
      refuses on a board with no sha256 tool rather than trusting the bytes; it
      used to pipe a tarball off the internet straight into `tar`.
- [V] no auth key is ever written into an image. Nothing under `image/` or in
      `cmd/sysimage`'s image-editing paths resolves one: an image is stored,
      compressed, copied between machines and kept after it is superseded, so a
      key inside one is a credential in every copy, revocable only fleet-wide.
      The key reaches the *card* instead (`disk_seed_tailnet`, boot/disk.sh),
      one boot before `wk-tailnet-join` spends and deletes it.
- [ ] `wk sysimage write` seeds the card and the board comes up on the tailnet.
      Written, not yet run against hardware: it probes the written rootfs for
      the image's own `wk-tailnet-join` and does nothing for a card that has
      none (a bridge image, a rescue system, an older build); for one that has
      it, it writes `/etc/wk/tailnet.conf` (the fleet's ssh name for the machine
      the image is *for*, and the tag) and the key at 0600, reads back what can
      be read back without printing a credential, and refuses — a barrier, so
      `--force` states the exception — rather than writing a card that will come
      up reachable only over whatever LAN it lands on.
- [ ] and then the deletions, in this order: build, write, boot, confirm the
      board answers to its own tailnet name, and only *then* remove
      `image_addr`'s MAC → ARP → mDNS ladder, `MACH_MAC`, the `.local`
      HostNames, the `10.99.1.10` stanza and the `ProxyJump`. Deleting them
      first turns a reachable board into an unreachable one.
- [ ] with those two on the tailnet, each one's ssh config is its name and
      nothing else: no `HostName`, no `ProxyJump`, no
      `UserKnownHostsFile=/dev/null`, and with Tailscale SSH no host key to
      accept — at which point `HostKeyAlias` and `accept-new` on the bridge
      stanzas are dead config and leave with the hop, kept only for `wk
      bridge`'s own probes. The two jumps that remain are the two nodes that
      cannot be on the tailnet: moose's BMC, and Igalia's build boxes.
- [ ] mutating commands aimed at an armed machine warn or refuse — still
      open; the fleet block shows the arming, nothing gates on it yet.
- [ ] `wk help hardware` stays true to `boot/machines.sh` and the drivers —
      hand-checked when either changes; it documents the media each device
      needs and which steps are a person's.
- [V] `wk` help text lists `sysimage` and `boot`, and both are refused inside
      a workspace and on a shared build machine (`is_host_only`). Same reason.
- [ ] the rpi3 end to end: provision it, `wk sysimage write` its SD card, and
      boot it. (The OTP door stays shut for good; its driver is a hands-on
      stub until then.)
- [V] `wk sysimage build perf-linux-rpi3` refuses rather than handing the fleet's only
      32-bit board an arm64 base
- [ ] **first contact with an unreachable Pi is physical.** `wk pi boot-order`
      writes the EEPROM over ssh, so it needs the board running. A Pi that
      answers nothing has to be met once with a card written by
      `wk sysimage write`.

## 8. Tailnet bridges — `wk bridge`

A bridge is a phone routing a segment onto the tailnet. Two halves, and only
one is testable without the hardware: the spec (conf files, the provisioner,
the inverse) is checkable here; whether a phone actually forwards a packet is
not, and is marked as such rather than assumed.

### The spec, checkable with no phone at all
- [V] `wk bridge ls` lists every declared bridge, with its device and segment,
      and says whether each answers — a conf with no phone behind it reads as
      `unreachable`, which is the normal state of one not flashed yet
- [V] `wk status` names every declared bridge, in its own block after the
      fleet block. Before it, `wk status` reported "the machines wk owns" while
      omitting two machines this repo defines, provisions and depends on --
      "enumerated in a registry" and "visible where a person looks" are
      different claims. Declared facts only and no probe: state is
      `wk bridge ls`'s to pay for, because it costs an ssh across a radio to a
      device that may be in a drawer
- [V] a kernel config delta names an aport and well-formed options. `PMO_KCONFIG`
      is the one place this repo edits somebody else's tree, and every way of
      getting it wrong — a delta with no `PMO_KERNEL_APORT`, an aport name that
      is not a path inside pmaports, an assignment that is not
      `CONFIG_<NAME>=y|m|n` — fails tens of minutes into a build that has
      already installed a chroot. It is config, so it is read here instead
- [V] an unknown host key is not reported as an absent phone. `ls` probed with
      plain ssh under BatchMode, which for an unknown key is *refuse* rather than
      ask — so it read `unreachable`, the same word as a phone in a drawer, and
      the first thing seen on 2026-08-22 was both bridges reporting that while
      one of them was up, provisioned and healthy. `accept-new` is the mode that
      fits (accepts a key never seen, still refuses one that has *moved*), and a
      refused key now reads `key-changed`, because a reflash changes the key and
      "unreachable" sends the diagnosis at the radio instead of at known_hosts
- [V] every conf in `bridge/hosts/` loads and names a device `bridge/devices.sh`
      knows, and `wk bridge setup <name> --dry-run` resolves the whole thing and
      changes nothing
- [V] `wk bridge rm` is the inverse of `wk bridge setup`: every path
      `bridge/provision.sh` writes is a path `rm` removes. A file the
      provisioner leaves behind after `rm` is a bridge that cannot be
      re-provisioned cleanly, and the failure appears months later
- [V] `wk bridge` is refused inside a workspace and on a shared build machine
      (`is_host_only`), and `bridge ls` / `bridge status` are read-only reports
- [V] `wk help bridge` exists and covers the half no command does: getting
      postmarketOS onto the phone
- [V] the image carries every package the role requires, so a first provision
      touches no network at all — `wk bridge setup` reports "all packages present
      -- apk not contacted". A package in bridge/provision.sh's list that the
      profile does not bake in would send a phone in a drawer to an apk index
      that has moved on
- [V] the profile and the bridge conf name the same phone — `bridge-pinephone`
      builds `pine64-pinephone` and `tailnet-bridge-generic.conf` says
      `BR_DEVICE=pinephone`, and likewise for the Librem 5. Disagreeing, they
      would produce an image for one phone provisioned as the other, and nothing
      short of the phone failing to boot would say so
- [V] `wk bridge provision <name> --dry-run` resolves the whole chain with no
      phone in the room, for every declared bridge: the bridge's own image
      profile, the card, the image, and the service image that reaches internal
      storage. Each is a lookup that can come back empty, and a run that
      discovers it *after* erasing a disk is the expensive way to find out. Both
      joins are derived, never declared twice: the bridge's profile from
      `PMO_BRIDGE`, its service image from `PMO_DEVICE` matching a fetch
      profile's `FET_DEVICE`
- [V] a phone with no service image is refused by name, and *for that reason* —
      Jumpdrive is a PinePhone project and image/profiles.sh carries no Librem 5
      equivalent, so that bridge cannot be installed from here at all. It is not
      quietly given the card instead: there is one destination, and a refusal
      that names the missing fetch profile beats a fallback that puts a bridge
      where it should not live. A refusal for any other reason means the
      `PMO_DEVICE`/`FET_DEVICE` join has broken, and the PinePhone is next
- [V] the two images can actually be told apart by content, which is what the
      eMMC route's whole safety argument rests on: both carry `eGON.BT0` at
      offset 8196 (the sunxi ROM's magic, at the 8 KiB
      `deviceinfo_sd_embed_firmware` declares — without it the card does not
      boot and nothing later matters), and their first mebibytes *differ*.
      Identical heads would make every exported disk look like the Jumpdrive
      card and the run would refuse; a head hashing to the sha256 of the empty
      string would make an unreadable disk look like the eMMC
- [V] `wk bridge provision` refuses a headless run before it erases anything.
      Two of its steps are a person — the card into the phone, the policy into
      the console — so it cannot finish unattended; the refusal has to come
      first, and it names the three commands that *can* run without a terminal
- [V] the Alpine facts the provisioner hardcodes are real, checked in a
      container rather than assumed (2026-08-20, `alpine:latest` = 3.24,
      main + community): every package name resolves (`tailscale`, `dnsmasq`,
      `nftables`, `chrony`, `jq`, `iw`, `ethtool`, `openssh`, `networkmanager`,
      `logrotate`, `zram-init`, `v4l-utils`, `ffmpeg`); the service names are
      `chronyd`, `networkmanager`, `tailscale`, `dnsmasq`, `sshd`; the binaries
      are `/usr/sbin/{nft,dnsmasq,sshd}`; `busybox` has a `watchdog` applet;
      sshd_config already carries the `sshd_config.d` Include *above* every
      directive and chrony.conf already carries `confdir /etc/chrony/conf.d`,
      so both edits are fallbacks; and `/etc/nftables.nft` really does begin
      with `flush ruleset`, which is what the packaged service being disabled
      is about

- [V] a bridge image is refused when the SSID its credential names is not on the
      air in a band the phone's radio has (2026-08-21, and it cost the afternoon
      that produced this line). The uplink credential is copied from the build
      host's own association, so the band comes along implicitly: rpi5 is
      dual-band and was on channel 52, the PinePhone's RTL8723CS is 802.11 b/g/n
      single-band, and the image booted with a valid PSK for a network the phone
      cannot see. Everything else about it verified — card, SPL, write,
      read-back — which is exactly what made it expensive to find
- [V] the check asks whether *the SSID* is on the air in a usable band, not
      which band the build host sits on. The first version asked the latter and
      was wrong inside the hour: the SSID gained a 2.4 GHz radio while rpi5
      stayed associated on 5 GHz, which is a working build and a check that
      refuses it
- [V] it unions a fresh `iw scan` with `scan dump` rather than taking the first
      non-empty answer, because the cached dump predated the new 2.4 GHz radio
      and a band missing from the answer is a build refused for nothing. And it
      truncates `iw`'s `freq: 2412.0` to an integer first — `[ 2412.0 -lt 3000 ]`
      is not a false comparison but a syntax error reported as false, which sent
      every frequency down the 5 GHz branch
- [V] every pmos profile declares `PMO_WIFI_BANDS`, checked in the selftest: the
      guard reads that field and skips silently when it is empty, so a phone
      added without one would get no protection against the failure above

- [V] first contact does not depend on mDNS, because mDNS is not dependable. The
      phone's avahi was running and publishing correctly — a unicast query to its
      address returned the right answer — while the access point declined to
      forward multicast between its 2.4 and 5 GHz radios (2026-08-21). Four ways
      in now, in order of "somebody said so": `--at`, the conf's name,
      `<hostname>.local` by multicast, then discovery. Discovery is required
      rather than optional: on this network the first three cannot cover a first
      provision, and finding the phone by hand each time is the manual step the
      verb exists to remove
- [V] discovery is built on the half of the mDNS measurement that worked.
      Multicast never arrives; a *unicast* mDNS query to an address, port 5353,
      asking for the phone's own name is answered by the phone and nothing else —
      34 ms against a live responder, no credentials. Validated against a
      third-party responder (`moose.local`, 85 ms), against a host running no
      avahi (times out), and against the wrong host for that name (no answer)
- [V] it asks in parallel on a one-second deadline, so a round costs a second
      rather than a second per host. The first version asked over ssh — two
      accounts, two handshakes, two timeouts — and took 100 s; measured 26 s for
      the whole failure path now, and sub-second when the phone is already in the
      neighbour table. The sweep that populates that table is bounded to 32 at a
      time and only runs if the table alone fails
- [V] reaching the phone cannot hang, checked offline against 192.0.2.1
      (TEST-NET-1). ssh's `ConnectTimeout` bounds the TCP connect and nothing
      after it, and BSD `nc -w` claims to bound connects and does not — 75 s
      against a black-holed address here versus 1.05 s for `-G1`. A phone
      half-way into suspend stalls exactly that way, so `_capped` enforces the
      ceiling rather than any tool's own flag, and the selftest proves the
      ceiling rather than the flag
- [V] `resolve_priv` bootstraps root rather than giving up. pmbootstrap installs
      the ssh key for the console user only, and that user cannot become root
      without a password, so provisioning died on its first privileged write
      against a phone that was otherwise perfectly healthy. Newer images seed
      root's key at build time; for a phone flashed from an older card, setup
      uses `PMO_PASSWORD` — which image/profiles.sh keeps in plain text on
      purpose — over `ssh -tt`, because doas refuses a pipe. Success is verified
      by root then answering, since with a pty ssh's exit status describes the
      pty and not the command
- [ ] the first provision has to land while the phone is awake, and that is
      circular: step 4 of the role is what stops it suspending, so an
      unprovisioned phone idles off the network after a few minutes. Said in the
      error message and in `wk help bridge`. Worth revisiting if it keeps
      costing runs — the image could ship the elogind drop-in itself

### First provision of a real phone, 2026-08-21/22 — what it cost and why
- [V] the bridge role applies end to end over ssh and survives a reboot: all
      four `wk-bridge-*` services, sshd, chronyd, NetworkManager, tailscaled and
      avahi come back unattended, nftables table loaded, power save off, clock
      correct, DNS resolving, and HTTPS to login.tailscale.com working — which
      is the precondition `tailscale up` actually needs
- [V] **WiFi power save is the thing that makes a PinePhone look absent.** The
      RTL8723CS powers its RF side down when idle and misses frames aimed at it,
      so the phone reaches its router while answering nothing — ARP included —
      from every host on the LAN. It reads exactly like a phone that is off, on
      another subnet, or behind an isolated AP; four hypotheses about the access
      point were wrong before the answer turned out to be one line already in
      `provision.sh`. `iw dev wlan0 set power_save off` proves it in seconds.
      Now applied by the *image* as well as the role, because a phone that needs
      it in order to answer ssh cannot be sent it over ssh
- [V] the mDNS failure was the same cause, not the network. Multicast queries
      went unanswered because the radio was asleep; unicast happened to land
      while it was awake. The access point was innocent throughout
- [V] no DNS, and it looks like no internet. The image can carry an
      `/etc/resolv.conf` that NetworkManager does not manage — here a
      systemd-resolved stub pointing at 127.0.0.53 on an OpenRC system with no
      systemd-resolved — so every lookup fails while routing is perfect and DHCP
      has been offering three good nameservers all along
- [V] no RTC, so the phone boots at 1970, and chrony *slews* by default: it will
      never close a 56-year gap. A 1970 clock fails every TLS check, so
      `tailscale up` cannot reach its coordination server. The role had `rtcsync`
      and needed `makestep 1.0 -1` — any offset, every boot
- [V] and the consequence nobody would predict: OpenRC caches its dependency
      tree and rebuilds it only when an init script is *newer* than the cache.
      Scripts written while the clock said 1970, then a chrony step to 2026, and
      the cache looks decades newer — so the services are enabled, symlinked,
      listed by `rc-update show`, invisible to `rc-status`, and never started at
      boot. The role now runs `rc-update -u` explicitly, because mtime is not a
      safe signal on a device whose clock is not
- [V] `ip -br` does not exist in BusyBox, which is what Alpine and so pmOS
      ships. Four uses; the damaging one was in `wk-bridge-netwatch`, where
      `healthy()` read an empty address every check and so returned false
      unconditionally — setting the escalation ladder climbing on a working
      uplink: interface bounce, NetworkManager restart, driver reload, then
      reboots against its budget, forever. Use `-o`
- [V] avahi renames itself on a name conflict — seen as
      `tailnet-bridge-generic-2.local` — at which point the phone stops
      answering for the name being asked about while remaining perfectly
      reachable. Discovery therefore falls back to asking over ssh, which is the
      phone stating what it is with no announcement protocol in between
- [V] provisioning over USB works and is the reliable path when the radio is
      not: pmOS presents a CDC-NCM gadget, macOS binds it as a network interface
      (`172.16.42.2` here, phone at `.1`), and `--at 172.16.42.1` provisions over
      a cable that cannot sleep or drop. It is inherently one-shot — the role
      switches the port to host mode for the Ethernet dock, which tears the
      gadget down. rpi5 cannot do this: its NUMA kernel has `CONFIG_USB_USBNET=y`
      but no `CDC_NCM` and no modules on disk
- [V] the image-side fixes are in a real image, not just in the source
      (`bridge-pinephone-20260822T054554Z`): the build log shows all three
      seeding steps, and the built rootfs was mounted and inspected —
      `/etc/NetworkManager/conf.d/99-wk-bridge-reachable.conf`,
      `/etc/elogind/logind.conf.d/10-wk-bridge-nosleep.conf`, and root holding
      the same ssh key. So a freshly flashed phone is reachable and stays
      reachable before anything is provisioned
- [V] and building it is what caught the root-key step silently not working.
      pmbootstrap creates the account's `.ssh` as mode 700 owned by the image's
      own uid, and pmos-build.sh runs unprivileged — so a bare
      `[ -f .../authorized_keys ]` is denied search permission and returns false
      on a file `find` as root lists plainly. Measured both ways on the built
      image. Every neighbouring operation there already used `sudo -n`; the test
      was the one that did not. A `warn` branch is the only reason it was not
      silent, which is the argument for writing the else-branch at all
- [ ] the segment itself — `lan0`, DHCP to rpi3/rpi4 — is untested, because it
      needs the USB-C Ethernet dock physically attached and none was
- [ ] `wk bridge tailnet <name>` untested: it is the one step left, and the only
      one needing a credential fetched by hand

### The pmos builder — flashing, which is a command and not a procedure
- [V] `wk sysimage build bridge-pinephone` runs pmbootstrap on rpi5 over ssh and
      produces a bootable image: 3.2 GB raw (msdos, boot + root), block map and
      compressed copy beside it, ~4 minutes with a warm package cache
      (2026-08-20). The build is detached on the far side with setsid, so a
      dropped WiFi link costs a poll rather than the run
- [V] the WiFi credential is copied from the build host's own netplan *on the
      build host* — the image comes out with an `wk-uplink` NM keyfile and the
      PSK never enters a log, a command line, or an agent's context. The bssid
      is deliberately not copied: two APs on one SSID, roaming expected
- [V] `--detach` returns immediately and `--resume` picks the build back up and
      imports it. This is the path that matters for a twenty-minute build: no
      local process has to survive it
- [V] every pinned thing is pinned and recorded: pmbootstrap 3.9.0 (a git tag —
      every PyPI release is yanked), channel v25.12 (what channels.cfg calls the
      latest release; `v26.06` exists as a branch and is not a channel), and the
      pmaports commit lands in the image's manifest
- [V] `wk sysimage write <id> --disk rpi5:/dev/mmcblk0` puts it on the card
      (2026-08-21): bmaptool wrote the 1.9 GiB of mapped blocks in 1m12s at
      27 MiB/s, growpart+resize2fs took the root to 59.4 G, and the card came
      out `pmOS_boot` + `pmOS_root`. The firmware check is *skipped* rather than
      failed — the card goes into a phone, so checking it against rpi5's boot
      files would refuse a perfectly good image — and the dry run says so
      instead of claiming a check it did not make
- [V] the same write path works from a Mac at all. It never had: `stat -c %s`
      and `numfmt` are GNU-only, so `wk sysimage write --dry-run` died printing
      its own plan (`stat: illegal option -- c`), and the image store resolved
      to /var/lib/wk — the podman VM's path, which the Mac cannot create.
      lib/common.sh now has `file_bytes`/`human_bytes` and lib/image.sh puts the
      store under the state dir on a Mac, the same correction targets/vm.sh
      already makes for the same reason
- [V] one build at a time per build host: a second `wk sysimage build` refuses
      while one is running, because the first thing a build does is
      `pmbootstrap shutdown` — which would pull the chroots out from under the
      other one
- [V] the same for `bridge-librem5` (2026-08-21): same builder, same host, one
      device name different — 1109 packages, `linux-purism-librem5`, avahi
      enabled, 526 MB compressed, imported and hash-checked end to end. Not
      written to a card only because there is one card
- [ ] `wk bridge provision tailnet-bridge-generic` end to end on the eMMC
      route: Jumpdrive to the card, the phone cabled to rpi5, its internal
      storage appearing as a new USB disk, the bridge image written there, the
      card out, and the phone coming up on its own install. The disk is found by
      *difference* against a baseline taken before the phone was attached —
      Jumpdrive exports the SD card as well as the eMMC, so the candidate set is
      normally two, and writing to the wrong one destroys the tool being used to
      do the writing. That one is asked, not guessed
- [ ] the phone comes up on its own install and `wk bridge setup
      tailnet-bridge-generic` reaches it. First contact is `<hostname>.local`:
      the image carries avahi with the service enabled by symlink, because until
      `tailscale up` has run there is no tailnet name and the DHCP address is not
      knowable in advance
- [V] the PinePhone will not boot a Jumpdrive card just because one is in the
      slot (2026-08-21). The image was exonerated at byte level — `eGON.BT0` at
      8196 with a well-formed header naming `sun50i-a64-pinephone`, U-Boot
      2020.07 behind it, a complete self-consistent FAT32 holding `Image.gz`,
      three DTBs and `initramfs.gz`, written and read-back verified — and the
      phone still came up on its eMMC pmOS. Identifiable from the other end,
      because postmarketOS's default USB gadget is MTP with Pine64's vendor id
      (`18d1:4ee1`, "Product: PinePhone"), so the timeout reads `lsusb` on the
      card machine and names the failure instead of listing possibilities
- [V] no special configuration is needed for SD boot, checked against upstream
      rather than assumed (2026-08-21): the A64 ROM reads sector 16 of the SD
      before the eMMC, no switch or key combination is involved, and Tow-Boot in
      the eMMC's firmware storage does not override it. Which means a card that
      does not boot was never *read* — and PINE64 lists "the microSD card is in
      the wrong slot" first among the causes, the microSD being the upper slot
      and the micro-SIM the lower. Recorded in `wk help bridge` under the traps,
      because every cheaper explanation had already been eliminated by then
- [V] Jumpdrive 0.8 is the newest release upstream has (checked 2026-08-21), and
      0.4 onwards "expose both the eMMC and SD" — the upstream confirmation that
      the two-LUN case the content discriminator handles is the normal one, not
      an edge case. Observed exactly so on the real phone: `/dev/sdb 29.1G` and
      `/dev/sdc 59.6G`
- [V] the content discriminator picks the right disk on real hardware
      (2026-08-21): given the two exported LUNs it matched `/dev/sdc`'s first
      mebibyte against the Jumpdrive image it had just written, and concluded
      `/dev/sdb` — correct — with no prompt. Note the sizes: the eMMC came up
      29.1 GB against rpi5's own 29 GB USB boot stick, so size would have been
      ambiguous between *the phone and the machine writing it*. That is what the
      baseline-difference step is for, and it is the reason it runs first
- [V] Jumpdrive does label its LUNs, and an earlier comment in cmd/bridge
      claimed it does not (fixed 2026-08-21). The real strings are `e eMMC` and
      `e microSD`, which say outright which is which — better evidence than the
      hash on the face of it, but an undocumented cosmetic choice of one
      release, so it is used to *corroborate* the content match rather than to
      make it. The two agreeing is checked; a disk chosen as the eMMC while
      calling itself microSD stops the run, because two independent signals
      disagreeing is not resolved by preferring one of them

Traps found while automating this, each of which cost a run (all now handled in
image/pmos-build.sh, and every one of them a silent failure):

- `pmbootstrap init` cannot be driven non-interactively — it prompts for the
  work path before reading anything, and `-y` does not answer that. The work
  folder is prepared directly instead: a version file and a pmaports clone
- `aports` does not follow `work` in the config; unset, pmbootstrap looks under
  its *default* work folder and reports "pmaports dir not found"
- pmaports' default branch is `main`, but pmbootstrap reads channels.cfg from
  `origin/master`, so that ref has to be fetched explicitly
- fetching into the checked-out branch is refused by git, so the channel is
  fetched into its tracking ref and `checkout -B`'d off that
- pmbootstrap leaves its chroots mounted and the previous image on a loop
  device; the next install then fails at `mkfs.ext4 /dev/installp2`. `shutdown`
  before and after
- the build script must not `rm -rf` its own output directory — the log it is
  writing lives there, and unlinking it makes the whole failure invisible
- **`deviceinfo_sd_embed_firmware`'s offset is in units of 1024 bytes, not
  sectors** — and pmbootstrap embeds the firmware into the image *file* itself,
  which I got wrong twice in a row on real hardware. Sector 8 is byte 4096; the
  firmware is at byte 8192, so "sector 8 is all zeros" is true, meaningless, and
  exactly the wrong conclusion. Writing a second copy at 4 KiB then overlapped
  pmbootstrap's (736 KiB of SPL from 4 KiB runs over 8 KiB), replacing a working
  bootloader with a misaligned one: a hand-written step that turned a good image
  into an unbootable card. The lesson is the process one — reach for a known-good
  reference image (`recovery-pinephone`) before theorising about boot ROMs.
  image/pmos-build.sh now only *checks*, at the byte the boot ROM reads

### Storage: the caches a phone-image build leaves, on two machines
- [V] `wk disk` reports them: the boot images (3 GB each — on a macOS host inside
      the state-directory row, named rather than hidden inside "logs, keys,
      target registry"), and each pmos build host's pmbootstrap chroots (8.2 GB
      measured on rpi5) and builds awaiting import (1.0 GB). The build-host rows
      are parenthesised and excluded from the total, because the total is this
      machine's — and a host that does not answer says `??` rather than `0`
- [V] `wk gc` reclaims both. On a macOS host it runs in two halves, because the
      things it reclaims are on two machines: images and build-host caches out
      here, the container store inside the podman VM (this file piped into the
      VM's bash, the same trick cmd/disk's store probe uses). `wk` no longer
      forwards it whole — which is what made it prune the VM's empty image
      directory and report success
- [V] `wk gc --purge-pmos` erases the chroots, per host, with the size, after
      shutting the chroots down first — they carry bind mounts into the host's
      own /proc and /dev, so `rm -rf` alone would follow them out
- [V] `./setup` installs what a build host needs (multipath-tools for kpartx,
      bmap-tools, python3-venv, xz-utils), so the builder's own ask-and-install
      is a fallback rather than the path
- [V] the image prune holds the image-store lock (rule 4). It did not: `wk gc`
      pruning "all but the newest per profile" while `wk sysimage build` imports
      is how the newest gets deleted

### Getting a system onto internal storage
- [V] `wk sysimage build recovery-pinephone` fetches Jumpdrive (the PinePhone's
      service image) into the store, pinned by release and by content, and it is
      an image like any other afterwards — 41 MB, verified, written by the same
      path with the same refusals. Upstream publishes no checksum file, so the
      pin is "what this repo verified once", which still has the property that
      matters: it cannot change underneath us silently
- [V] the reference settles arguments: Jumpdrive's own layout has `eGON.BT0` at
      8 KiB and zeros at 4 KiB, which is what proved the pmOS images were right
      all along and the hand-written embed was the bug
- [V] Jumpdrive boots the phone, exports its eMMC over USB, and
      `wk sysimage write <bridge image> --disk rpi5:/dev/sdX` installs the bridge
      onto internal storage — the end state, using the ordinary write path with
      no new verb. Confirmed on the running phone 2026-08-22: root is
      `/dev/mmcblk2p2` on a 29.1 GB disk that carries `mmcblk2boot0`/`boot1`
      (an eMMC-only feature), and there is no `mmcblk0` at all — the card is
      out and the install underneath it is what boots
- [V] there is exactly one route. The second one (write to the idle eMMC from
      the running card system over ssh) was written and then removed: it
      reimplemented dd, partition growth and identity-copying to reach a disk
      that Jumpdrive hands to `wk sysimage write` for free. One way to write a
      disk, and it is the one with the refusals

### The hardware — done, 2026-08-22, and verified across a reboot
- [V] a PinePhone flashed with pmOS, answering ssh, provisioned end to end:
      `wk bridge setup tailnet-bridge-generic` applies the whole role and the
      health check passes on everything that does not need the downstream leg.
      pmOS v25.12, `linux-postmarketos-allwinner` 6.17.5, on the tailnet as
      `tag:bridge` advertising 10.99.1.0/24, never sleeping, WiFi power save
      off, clock stepped, every wk-bridge service `started`, and `rc-update -u`
      having done its job (the services survive a boot, which is the 1970-clock
      trap)
- [V] the dock does a USB Data Role Swap. The anx7688 reaches DFP and stays
      there (`data_role = [host] device`, a `port0-partner` present, the phone a
      1.5 A sink), and a hub enumerates behind it — so the fault this was
      written to catch is not the fault that is left
- [V] **which dock, and why it is a property of the phone.** A generic USB-C
      dock cannot work here and the PinePhone's own dock can, for one reason:
      the A64 has no SuperSpeed anywhere, so a dock whose NIC sits behind its
      own USB3 hub presents only its USB2 hub — VIA VL813, four downstream
      ports reading `not attached`, permanently, with the role swap and the
      power negotiation both perfect. The PinePhone dock puts its NIC on the
      USB2 path (CoreChips `0fe6:9900` "10/100M LAN" behind a `1a40:0101` hub),
      `cdc_ether` claims it with no extra packages, and it links at 100 Mb/s.
      The unusable case reads exactly like a dock refusing the role swap, which
      is why the health check now separates the cases by name
- [V] the adapter present under its kernel name is reported as *that*, not as
      absence. The first version of the improved check listed a device called
      "10/100M LAN" and concluded "none of it an ethernet adapter" — `cdc_ether`
      had already claimed it as `eth0` and the only thing wrong was the name.
      Also separated: a NIC on the bus with no driver bound, which is a missing
      kernel module rather than a missing dock
- [V] `wk bridge setup` renames the interface in place, so an adapter plugged in
      after a bare run needs no hand at the phone. udev's `NAME=` applies only
      when a device appears and a live link cannot be renamed under itself,
      which is why this used to end at "re-plug the dock, or reboot" — a hand at
      the device, for the one machine whose whole purpose is being reachable
      without going to it. A downed link renames fine; NetworkManager has to be
      told to let go of it first and to take it back after
- [V] the segment survives a reboot of the bridge, which is the claim an
      in-place rename does *not* establish on its own. Rebooted 2026-08-22:
      `lan0` came back named (udev), addressed (the NM keyfile), with carrier,
      the lease file intact, the route still approved and every `wk-bridge-*`
      service started
- [V] **the udev rule matches the *permanent* MAC, not the live one.** pmOS
      ships NetworkManager with `ethernet.cloned-mac-address=stable`
      (`/usr/lib/NetworkManager/conf.d/50-random-mac.conf`), so the interface is
      running on a synthetic address within seconds of appearing — `lan0`
      answering to `ae:bf:97:99:88:46` while `ethtool -P` said
      `00:00:00:00:03:88`. The rule applies at `ACTION=="add"`, when the
      hardware address is still in place, so autodetecting the *current* address
      writes a rule matching nothing and the rename stops working at the next
      boot — invisibly, because the interface was already renamed by the
      previous correct rule. Found by re-running setup and noticing the rule had
      changed under it. The keyfile also pins `cloned-mac-address=permanent` on
      this one leg: a subnet router's segment address is something other
      machines key on, and the phone's uplink randomization is left alone
- [!] **the downstream NIC can enumerate perfectly and be forty times too
      slow, and this is unfixed at the root.** On a PinePhone the dock asks for
      the data-role swap itself and the phone is host by 3.5 s — but at 13 s the
      pmOS initramfs sets up its USB-gadget network and flips the phy back to
      peripheral (`Changing dr_mode to 2`) with a hub physically attached. EHCI
      then fails for a minute (`device descriptor read/64, error -110`, port
      power cycles) and gives up at 77 s with "unable to enumerate USB device";
      the port falls to the *companion* OHCI controller and the dock comes up at
      **12 Mbit/s instead of 480**, capping a 100 Mbit link at about eight. Host
      mode is *correct* in that state, the link says 100 Mb/s, and nothing else
      notices. It is a race — earlier boots of the same phone landed on EHCI —
      which is the worst way for a fault to behave. The real fix is to stop the
      initramfs taking the port; that is not done
- [V] the degraded link is *reported* and *recovered*. `wk bridge status` prints
      the USB bus speed and fails on anything below high speed;
      wk-bridge-usb-host recovers it with a four-step rebind — unbind OHCI (so
      the companion releases the device), unbind EHCI (so it forgets it gave
      up), bind EHCI (it takes the device at high speed), bind OHCI — and
      netwatch runs it every pass, not only when the interface is missing, since
      a slow NIC is *present*
- [V] the recovery is capped at three attempts per boot, in `/run`. Rebinding
      EHCI *alone* looked right in a hand test where EHCI had just been unbound
      seconds earlier, shipped, and then ran every 60 s against a real degraded
      boot without ever moving the device off OHCI — logging a recovery it was
      not performing. A repair that repeats forever is a fault, not a recovery,
      and the health check's honest "12 Mbit/s" is worth more than a bridge that
      resets its own USB controllers every minute for a day
- [V] the board does not wait ~80 s to reappear after the bridge reboots. It
      asks for DHCP on carrier-up — which is when the *kernel* enumerates the
      adapter, before any of userspace — so waiting for `net` put dnsmasq well
      behind the first request and what brought the segment back was the
      client's own retry (80 s, measured). `wk-bridge-dhcp` is now `after net`
      rather than `need net`, which `bind-dynamic` makes safe: dnsmasq starts
      before its interface exists and binds when it appears, checked on the
      phone with a throwaway instance on a nonexistent interface rather than
      assumed
- [V] a client that has given up is made to ask again. Carrier up and an empty
      lease file for two consecutive netwatch passes flaps the segment link
      once — the only thing that makes a DHCP client re-request, since nothing
      on the phone can reach into it. Two passes because one can catch a board
      that is still booting, and once per episode because an empty segment is a
      normal state and a bridge that flaps its own link every minute is a fault
      rather than a recovery
- [!] no `/dev/watchdog`, and it is the kernel rather than the phone: the A64
      carries a watchdog and the device tree declares it
      (`allwinner,sun50i-a64-wdt` at 0x1c20ca0), but
      `linux-postmarketos-allwinner` is built with `CONFIG_SUNXI_WATCHDOG`
      unset. Until a kernel with it on exists, the netwatch ladder is this
      phone's only recovery and it cannot see a kernel that has stopped
      scheduling
- [V] the tailnet policy for 10.99.1.0/24 — `autoApprovers` and the grant, and
      **a paste is not enough**. `autoApprovers` is evaluated when a node
      *advertises* a route, so a route first advertised before the policy
      existed stays unapproved: re-running setup re-asserts the same value and
      `tailscale set` with an unchanged value is a no-op. Observed exactly so —
      policy in place, every check green except the one that matters,
      `PrimaryRoutes` null. Withdrawing and re-advertising forces the
      evaluation, and setup now does it whenever the route is advertised but
      not primary, conditionally, because on a working bridge it would drop the
      segment for a second for nothing
- [V] a board on the segment gets its reserved address and is reachable from
      the workstation. `10.99.1.10 rpi4` in the lease file — the address pinned
      by the MAC `boot/machines/rpi4.conf` declares — pings from the phone,
      answers ssh through it by ProxyJump, and reaches the internet through the
      NAT egress
- [V] the fleet can still find a board that has moved behind the bridge. It
      could not: `MACH_SSH=rpi4-test` was `raspberrypi4-64.local`, and mDNS does
      not cross the phone, so `wk boot rpi4 --status` reported "unreachable ...
      a plain outage" about a board that was up, linked and one hop away. The
      ssh entry now jumps through the bridge to the reserved address, and
      matches the bare *address* as a Host pattern too — `wk boot`'s bench-mode
      channel (`i_ssh`) reaches a booted system by address with no jump host of
      its own, and a bench system on this board lands on the same reserved
      address, so one entry covers both modes
- [V] a board that moved networks carries its old lease, and that is what breaks
      first. The rpi4 held `192.168.1.159` with 22 h left *and* `10.99.1.10`:
      egress worked, DNS did not, because the stale lease's nameservers sat at
      route metric 10 against the bridge's 1002 and were reachable only through
      a gateway that is no longer there. It looks like a broken bridge and is a
      client that has not let go. Dropping the dead address and rebinding the
      client fixed it; the lease would also have expired on its own

### Needs the hardware
- [ ] a board on the segment gets its reserved address, and is reachable from a
      workspace over the tailnet — which needs `autoApprovers` in the policy,
      the failure that looks exactly like success
- [ ] the escalation ladder does what it says: pull the AP, watch
      `wk-bridge-netwatch` climb, and confirm it stops at the reboot budget
      rather than rebooting forever
- [ ] `BR_CAMERA=http` streams at all. The pipeline is unproven on both phones:
      libcamera-era sensors do not always present a format ffmpeg will open
- [ ] the Librem 5 reflashed to pmOS and moved onto this role, replacing the
      hand-built PureOS configuration. Until then `wk bridge setup` refuses it,
      which is the intended behaviour rather than a gap

### The macOS benchmark lane — `wk bench mac`, 2026-08-22

The six commands of `docs/HANDOFF-mac-perf-mode.md` as one resumable command,
driven from rpi5 against tolken. Written and exercised as far as a Mac with no
benchmark volume allows, which is everything except the measurement.

- [V] **the lane is driven from another machine, and refuses to be driven from
      the Mac.** Phase 4 reboots the computer the driver would be running on, so
      the driver holds the state elsewhere and reaches in once per phase. The
      guard compares `hostname -s` against the ssh destination **case-folded**:
      tolken reports `Tolken` while every config spells it `tolken`, so the
      case-sensitive first version never fired — a guard that silently does
      nothing is worse than no guard, because the refusal it owes you is
      replaced by a reboot that kills the driver
- [V] **bench mode gets its own ssh alias.** One address, two macOS installs,
      two host keys — and ssh refuses a *changed* key outright, where
      `accept-new` accepts an unknown host and still refuses a changed one. So
      the lane would arm the machine, wait for it to come back, and then be
      unable to talk to what came back. `tolken-bench` carries a `HostKeyAlias`:
      separate known-hosts identities on one address, each pinned normally, and
      a phase aimed at the wrong mode fails on the key rather than doing the
      wrong thing quietly
- [V] **every phase asserts its mode from `/etc/wk-image` first.** The machine
      answers ssh in both modes. `wk bench staged` in host mode is refused, but
      a `wk build` aimed at bench mode would quietly start turning the benchmark
      install into a workstation
- [V] the phase state machine, unit-tested over all seven phases × the fresh /
      mid-lane / resumed / finished cases. The first version had the comparison
      inverted — `past build` was false with `done_through=stage` — which would
      have rebuilt and re-staged on every resume
- [V] `--dry-run` prints the whole plan on a machine that is **not ready to run
      it**, which is exactly when the plan is worth reading. It writes no lane
      state and performs no waits; both were bugs first (below)
- [V] the preflight names the exact key to authorise, and the exact
      `systemsetup` line, when ssh is refused — the state tolken was actually
      in, and one where nothing else about the machine can be inspected
- [ ] **the measurement.** No benchmark volume exists yet, so the lane has never
      completed. `wk bench mac-volume` is the other half of this task

### Making the benchmark install — `wk bench mac-volume`, 2026-08-22

- [V] **the second APFS volume, decided over an external SSD.** The disk is the
      one variable that cannot be corrected for afterwards; every cost of
      sharing the container is visible and manageable. Recorded in
      `docs/HANDOFF-mac-perf-mode.md` with the costs, not just the choice
- [V] the headroom check is **fatal, not a warning**: APFS volumes have no fixed
      size, so the benchmark install growing is the workstation shrinking, and
      the disk that fills is the one being worked on. tolken: 167 GB free in
      `disk3` against a 120 GB floor
- [V] it stays **out of the `wk quiesce` privileged helper**. That helper is a
      fixed allowlist granted NOPASSWD forever; "create a volume" and "run
      `startosinstall` as root" are once-per-machine provisioning, and granting
      them unconditionally trades a real root escalation for one password prompt
- [V] `--provision` **refuses to run in host mode**, because writing a
      bench-mode marker onto the workstation would make `wk bench staged`
      measure a machine with a desktop under it and stamp the result
      `bench_host=image` — a wrong number that looks right
- [V] the installer is fetched by version, matching the host (26.6.1), so the
      two installs do not differ by a macOS version nobody chose. No sudo
      needed; ~17 GB
- [ ] `--create`, `--install` and `--provision` against the real disk. Each
      needs a credential a script cannot supply

## Regressions worth a permanent test

Each of these shipped, looked fine, and was wrong. They are the cheapest checks
in the file because each one already cost a debugging session.

| check | what it catches |
|---|---|
| `wk run` on GTK/WPE actually starts | per-port build dir, and `LD_LIBRARY_PATH` being replaced rather than prepended |
| `wk new` waits and checks the marker | firstrun aborting while creation reports success |
| `.config` in a new workspace is owned by the user | the SDK's systemd mount re-appearing and breaking firstrun |
| `wk logs` shows `(none)` on a good build | `error:` matching inside message text |
| `wk enter <ws> <cmd>` runs the command | `exec`-ing a shell function |
| an in-workspace build sizes from the whole machine | the 12 GB desktop reserve being subtracted a second time inside a guest the host had already sized: -j5 in a 20 GB guest against -j9 from the host |
| a bare `wk run` in a macOS guest finds a binary | the container-shaped `jsc-release` default resolving a JSCOnly path that an Apple-port guest can never have |
| `wk build --list` with podman stopped | a static table lookup starting a two-minute VM boot, and blaming podman when it could not |
| `wk verify` refuses inside a workspace | a sandbox check whose gate (`WK_SANDBOX`) is unset reporting "intact" after measuring two things |
| the host's `$HOME` has no `~/.wk-workspace` | the marker escaping into the host and making every host command act on a workspace that is not there |
| `wk build <config>` inside, `wk build <ws> <config>` outside | one argument form silently shadowing the other |
| `wk start` / `wk stop` with a driver loaded | driver defaults evaluated at source time |
| two `config_build_dir` definitions | a clean merge leaving the wrong one live |
| loading a second target does not leave the first driver's overrides live | a driver's function override outliving its load: `wk status` walked container first, and every remote workspace's branch was then read by the container driver's `t_branch` — plausibly, and wrongly, as `-` |
| guest reaches nothing with the proxy bypassed | softnet actually enforcing, rather than the env vars being politely obeyed |
| a macOS guest reaches PyPI through the proxy | the proxy address being passed as a raw unset variable, so the guest got a hardcoded 192.168.64.1 that nothing listens on -- indistinguishable from the filter working |
| a macOS guest's `~/.zprofile` carries the `wk-tools: egress` block | provisioning silently not having run, which looks like a network fault |
| `mac-release-asan` and `mac-release` resolve to different dirs | Xcode toggling ASan within a configuration without changing the path, so the two builds silently share one tree |
| the guest desktop is visible after a reboot | three independent things hiding it -- screen saver, display sleep, and the screen *lock* -- where disabling any two is not enough |
| `WK_TARGET=vm wk gc` runs at all | cmd/gc sourcing a driver without lib/target.sh (`wk_state_dir: command not found`, verified 2026-08-19) |
| `wk selftest --section <typo>` fails | the runner exiting 0 having run nothing -- the silent pass the plan's own preamble forbids |
| a container workspace's `Host wk-<name>` alias is a ProxyCommand, not a hostname | the fictional `HostName localhost` it used to carry, pointing zed at the host's own filesystem -- a container has no interface and no address, so any hostname there is a guess |
| `wk zed` refuses inside a workspace | "zed is not installed" from a machine where it cannot be installed -- the same words the host prints when Zed really is missing |
| `wk status` names each machine once, and every name is a machine | two processes assembling one listing and disagreeing about what this machine is called (`hostname` in the podman VM is `localhost`), and a *target* name arriving where a machine name belongs -- which grew a machine called "container" holding the VM's own facts |
| `wk status` with the podman machine stopped leaves it stopped | a read-only report booting a VM as a side effect |
| a workspace's `~/.ssh/id_*` are symlinks, not files | a copied deploy key: one the push switch cannot take back, and one that survives a key rotation as a dead key |
| `origin` is `WebKit/WebKit` in every target's checkout | the remote build machine pointing origin at the box's own shared clone, so `git log origin/main` answered for that box's last fetch |
| `wk build --cmakeargs` is refused | build-webkit taking one `--cmakeargs`, so a hand-written one silently replaces `DEVELOPER_MODE`, `USE_LIBBACKTRACE` and the architecture's flags |
| `claude` is on `$PATH` in a container workspace | firstrun installing the CLI to `~/.local/bin` and no rc putting it on the path, so `wk claude` failed with "claude: not found" in a workspace where it was installed and working |
| `wk ls` and `wk status` name the same set | one of them being forwarded whole into the podman VM, so a vm or remote workspace showed in one listing and not the other |
| a snapshot with no `sha` is invisible to `current_base` | an interrupted `wk sync` publishing rubble that the next `wk new` pins and the next `wk sync` hardlinks from |
| `wk new` over a workspace with no `base-id` remakes it | "already exists" answered about a half-made thing, and `base-id` re-pinned over a surviving `changes/` layer |
| `wk sysimage build <pmos profile>` prints something | `set -o pipefail` turning `x=$(ssh "cat missing" \| tr …)` into a silent `set -e` exit: the poll asks for an rc file that does not exist yet — the normal state of a running build — and the command dies with no message at all. Every "the build vanished after one poll" was this one bug |
| `wk sysimage write --dry-run` prints its plan from a Mac | `stat -c %s` and `numfmt`, which are GNU-only, in a path whose whole point is being driven from a workstation that is not Linux |
| the image store is writable on the machine that owns it | `$WK_STORE` resolving to `/var/lib/wk` on a macOS host — the podman VM's path, which the Mac cannot create — in a command that is never forwarded into that VM |
| a second `wk sysimage build` refuses while one runs | `pgrep -f <pattern>` matching the ssh that carries the pattern, so an idle build host reports a build in progress |
| `wk gc` keeps the newest build **per profile** on a build host | keeping the newest two overall, which two PinePhone builds in a row turned into "the Librem 5's artifacts are old" — deleting the only un-imported copy |
| `cmd/gc` honours a pre-set `$WK_ROOT` | deriving it from `$0` under `bash -s`, where `$0` is "bash": the container half then sourced /var/home/lib/common.sh and died |
| `cmd/gc` sources tree files optionally | the container half being this file piped into a VM whose copy of the *rest* of the tree is only as new as the last `wk sync --target container` |
| a lock outlives the command that took it | a flock inherited by the `conmon` podman leaves behind, holding a workspace's lock for as long as the container exists |
<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
>>>>>>> Stashed changes
| two machines sharing one ssh destination get **separate lane state** | keying the state file by host alone. `mbp` and `benchvm` are different machines on one address, so the rehearsal and the real run wrote the same file — and immediately after a completed benchvm lane, an `mbp` lane would read `done_through=collect` and skip build, stage, arm and run, reporting a finished lane for a benchmark volume it had never touched |
| the result is collected from **where it was written** | collecting after leaving bench mode unconditionally. On the volume that is the point (it proves the result survived the reboot); in a guest the result lives *inside the guest*, and leaving the role stops it — so host mode was asked to read a benchmark volume this Mac does not have, and said so. Guest collects before back, volume after |
| a phase-order change updates the **order list** too | the high-water mark being compared against a hardcoded sequence. Twice: reordering stage for the guest silently marked it done, and then reordering collect did the same |
| never edit a shell script while it is executing | bash reads scripts incrementally, so an edit that shifts byte offsets makes the running shell resume mid-token. Produced `line 859: eport: command not found` from the middle of the word `report`, on a line that was `}` — an error that looks like corruption and is really a live edit |
| `wk quiesce on` returns when driven over ssh | `caffeinate` backgrounded with the session's stdout/stderr still attached. ssh waits for the streams, not for the shell, so the command hung for ever — on the only path where it is ever run unattended. Detach all three descriptors |
| `./setup --stage quiesce` succeeds on macOS | an unguarded Linux block masking `getty@tty2`/`autovt@tty2`, which fails with `sudo: systemctl: command not found` **after** the helper and its sudoers rule have installed correctly. The stage reported failure for a machine it had finished provisioning |
| the update-check state is read from the **setting**, not `softwareupdate --schedule` | that command reporting "Automatic checking for updates is turned on" while `AutomaticCheckEnabled = 0` sits in the plist it describes (macOS 26, verified on hardware). Twice written off as a virtualisation quirk before being reproduced on the real install; it is the reader, the same way `systemsetup -getremotelogin` needs admin and exits 0 while refusing to answer |
| a preflight check that **cannot pass** degrades to unknown, not failure | the update check failing on every machine that cannot read the setting as root. It was *the* failing check, so it was the one people `--force` past — and `--force` is all-or-nothing, so believing it disabled every other check with it. A check that cannot pass on a correct machine trains people to ignore the whole preflight |
| the **per-user** Setup Assistant stays suppressed across reboots | writing the `com.apple.SetupAssistant` keys once, in a session that is then replaced. Auto-login creates a fresh session on the next boot and the pane returns — caught only because `screen_blocker` was there to notice |
| the bench install is reached at **its own address**, not the host's | `Host tolken-bench` carrying `HostName tolken`, which MagicDNS resolves to the *host* install's tailnet address. The bench install is a different OS with no tailscale and no tailnet identity, so that name reaches host mode or nothing. Cost most of an evening: ssh, authorized_keys, Remote Login and the network were all working the whole time and every probe was aimed at the wrong machine. Found by scanning the LAN for a host answering as the bench marker. The stanza's own comment had predicted this before the volume existed |
| a fresh macOS install has **no `/usr/bin/python3`** | provisioning that writes `/etc/kcpassword` with a Python heredoc. Command Line Tools are not present on a new install, so the writer failed silently, `autoLoginUser` was set without a password blob, and the machine landed at a login window every boot. The same absence is why pyobjc was missing |
| a fresh macOS install has **no network credentials** | assuming ssh reachability means the machine is configured. On a Wi-Fi-only Mac a new install joins nothing, so Remote Login can be genuinely enabled on a machine nothing can route to — indistinguishable from sshd being off unless you check for an address |
| the **per-user** Setup Assistant is a separate pane from the system one | `/var/db/.AppleSetupDone` suppressing only the system-level assistant. A newly created account still gets Apple ID / analytics / Screen Time on its first login, and it owns the front window — which is exactly the modal-pane condition that silently times out a benchmark |
| CLT can be installed headlessly, but only after a trigger file | `softwareupdate --list` not offering Command Line Tools at all until `/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress` exists. Without it the only documented route is `xcode-select --install`, a GUI prompt no daemon can answer |
| a `--installpackage` LaunchDaemon runs on the boot it was installed on | those packages being laid down during the **boot-install phase of the first boot** (install.log shows `.com.apple.templatemigration.boot-install/`), which is *after* launchd has scanned `/Library/LaunchDaemons`. A `RunAtLoad` daemon dropped then does not run until the next boot — and with Setup Assistant suppressed and no account yet created, nothing causes a next boot. The volume booted, ran for minutes (wifi.log, asl) and never opened the daemon's StandardOutPath. Fixed with a package `postinstall` that bootstraps the job into the running system |
| a failsafe does not live inside the thing it is protecting against | the lockout guard being written *in* the first-boot script: when the script never ran, the guard never ran either, and `.AppleSetupDone` plus no account left a login window with nothing to click and no ssh. A failsafe downstream of the failure is not a failsafe |
| `startosinstall --installpackage` accepts an **unsigned** productbuild package | (answered, not a bug) — assumed to need a Developer ID Installer signature. It does not: the receipt was written and every payload file landed from a package reporting `no signature`. Worth knowing before anyone builds signing into this path |
| the benchmark preflight notices a **modal pane owning the screen** | nothing looking at the screen at all. The console check asks "is someone logged in", not "is the screen usable" — so an unanswered Setup Assistant sailed through, MiniBrowser launched but never became frontmost, and Speedometer was throttled in a background window until `run-benchmark` timed out at exit 124. No error, no crash, no progress |
| `--force` cannot hide a *fatal* preflight failure behind a benign one | `--force` being all-or-nothing. It was added for a guest quirk that genuinely cannot pass (`softwareupdate --schedule off` does not stick in a guest — unrelated to Setup Assistant, confirmed by dismissing it and re-reading), and it then forced past the modal-pane failure too. The lane now asserts the fatal condition itself, before forcing the benign one |
| `screen_blocker` asks *what is frontmost*, not *what is running* | matching process command lines for `<app>.app`: `softwareupdated` and `suhelperd` live inside `Software Update.app/Contents/Resources/` and run on every healthy Mac, so the check failed on a machine whose screen was free. **A check that cannot pass is worse than no check** — the first thing anyone does with it is force past it. `lsappinfo front` asks the window server, needs no assistive access (System Events answers `-25211` on a fresh install), and sees GUI apps only. Verified both directions: empty on a free screen, and firing when a listed app is frontmost |
| one definition of "is the screen free", not one per caller | the check living in cmd/bench with three apps while the lane that decided whether to `--force` past it kept its own list of one. `Setup Assistant` was caught; `Software Update`, which came to the front the moment Setup Assistant was dismissed, was forced straight through. Same shape as `b_probeable` duplicating `_tart_bin` — a probe that reimplements a resolver drifts from it |
| only the machine being measured is running during a measurement | the lane starting the build guest for the build and stage and never stopping it, so a second macOS VM competed for the same CPUs throughout the run. Noticed by a person looking at the screen and counting two windows; the code had a comment claiming the opposite |
| a lane that launches a **detached** build waits for *that* build | polling `wk status` immediately after `wk build --detach`, which answers about the *previous* build until the new one registers. The first iteration read a two-day-old `ok` and the lane declared "build ok" in seconds — then armed the bench machine and staged, **while the real build was still compiling**. It would have published a Speedometer number for a tree nobody had just built, labelled fresh. The tell was the elapsed time: `8m1s`, the exact figure recorded for the 2026-08-20 build. Fix: snapshot the report before launching and believe a terminal exit code only once the report has changed |
| a trailing `[ -n "$x" ] && …` in a function under `set -e` | the function returning 1 when the test is false, killing an unguarded caller. Harmless mid-function (verified: `set -e` does not fire there), fatal as the **last** statement — the exit status becomes the function's. A pattern worth grepping for wherever optional args are appended to an array |
| `wk boot benchvm` actually *starts* the guest | `guest` matching neither the one-shot nor the medium branch in `cmd_arm`, so `b_arm` was never called and the unconditional `b_reboot` at the end ran instead — which for a guest is **stop**. It reported `rc=0` having done the exact opposite of arming |
| `wk boot <guest-machine>` works from a *stopped* guest | the `unreachable` refusal in `cmd_arm` ("arming is an ssh command"), true of every model but `guest`, where arming *is* starting it. A stopped guest is the state the rehearsal exists to be armed from, and it was the one state that refused |
| a `guest` machine needs no `wk sysimage build` | `cmd_arm` sending it down the store path, dying with `no system built for benchvm yet -- 'wk sysimage build perf-macos-benchvm'` and advising a command that cannot exist for a machine whose system *is* the guest |
| a driver declares the libraries it calls | `targets/vm.sh` calling `envelope_mem_mb` while relying on the caller to have sourced `lib/resources.sh`. Making the call lazy moved the failure from source time to call time rather than removing it: `wk boot benchvm` died `rc=127` with the guest half-started. `targets/local.sh` already had the guarded-source idiom; vm.sh did not |
| a guest boots when driven **over ssh**, not only from a login shell | `tart` resolving `softnet` through `PATH` while wk checks it by absolute path (`$WK_SOFTNET_BIN`) — so the guard passed and the boot died with `InitializationFailed(why: "softnet not found in PATH")`. Invisible interactively, because a login shell has `/usr/local/bin` and a non-interactive ssh has only `/usr/bin:/bin:/usr/sbin:/sbin`. The guest therefore booted by hand and never from another machine, which is every fleet verb there is |
| `wk boot benchvm --status` reports a stopped guest as **stopped**, not absent | the vm target speaking two namespaces and a caller mixing them: `_vm()` maps a workspace name to the tart VM backing it by prefixing `wk-`, and while `t_start`/`t_stop`/`_ip` map it themselves, `_vm_state` takes the mapped name. Three calls in `boot/mac-guest.sh` passed the unmapped one, so every probe answered `absent` about a guest sitting there stopped — `--status` lied and `b_arm` was fatal, meaning **the driver could never arm and the whole guest rehearsal was dead**. Hidden by the workspace being called `wk-bench`, which makes the correct tart name the double-prefixed `wk-wk-bench` and the wrong one entirely plausible. Also hidden by `wk vm ls` being *right* about the same guest at the same moment — two commands disagreeing about one fact, which is the signal that was there to be read |
| a `command -v <tool>` probe over non-interactive ssh | a PATH artifact read as a fact about the machine. Non-interactive ssh to a Mac gets `/usr/bin:/bin:/usr/sbin:/sbin`, so a Homebrew or `~/.local/bin` tool is "missing" — `tart` was reported absent on a machine running tart 2.35.0 with three VMs on it. Probe with an absolute path, or `zsh -l -c`, and never conclude "not installed" from one bare `command -v` |
| `wk ls` and `wk status` on a **macOS host** name every workspace | `wk` sourcing `lib/target.sh` without `lib/store.sh`, so `target_all`'s `wk_state_dir` was undefined: on Linux `wk` execs cmd/ls (which sources it) and on macOS the walk runs inside `wk` itself. The listing came back **truncated and confident**, hiding the two macOS guests the benchmark lane needs, behind one stderr line nobody reads. Third sighting of this one helper vanishing (cmd/gc 2026-08-19, lib/image.sh before it) — now in `lib/common.sh`, which every store.sh user already sources |
| `wk bench mac-volume` runs on the Mac | the macOS host forwarding it into the podman VM, where it asked a Linux guest about `diskutil` and died on `podman is required` — the same failure `bench stage`/`bench staged` are already exempted from |
| the lane's preflight refuses when the benchmark volume is absent | matching a list of ways it could be missing instead of the one way it is present: `benchmark_volume=WK Bench (not attached)` was not in the list, so the preflight reported **ready** and the lane would have failed at the stage. Matching `(attached at …)` instead also fails closed on a new spelling — and needs `^[[:space:]]*`, because the driver indents those lines and a `^`-anchored version blocked the lane even *with* the volume attached |
| `--dry-run` is the real path with mutations suppressed, and **not a second path** | a dry run that models what its own earlier steps *would* have done. `wk bench mac-volume --all --dry-run` was made to walk its whole chain by simulating the volume `--create` had not really made — so the dry run reasoned about a fictional disk, and could have passed while the real path failed. That is exactly the evidence it exists to provide, inverted. Backed out 2026-08-22: one `run()` gate on mutating commands, every precondition checked for real, and an unmet one refuses identically in both modes |
| `--dry-run` on a fresh lane prints a plan | two ways it did not: `state_get` under `set -o pipefail` failing on a state file that does not exist yet — the normal starting state — so `set -e` killed the run at the first read with no error and exit 0; and the dry run *writing* `done_through` as it walked, so the next real run skipped build and stage and looked for products nobody had staged |
<<<<<<< Updated upstream
=======
| a check in `cmd/selftest` asks about text with a here-string, never `printf … \| grep -q` | `grep -q` exiting at the first match, SIGPIPEing the producer, and `pipefail` reporting a *successful* match as a failed pipeline. Invisible while the text is short: this file had 34 of them and the day `docs/help/bridge.txt` reached ~29 KB one began failing 24 times in 80 runs, reported as a broken help topic. `grep -q P <<<"$out"` reads a file, not a pipe — 0 in 80 on the same text. The rest of the tree still has the pipe form, latent for the same reason |
| a `wk sysimage write` onto a *reused* disk leaves a mountable filesystem | bmaptool writing only the mapped blocks, so every hole kept the previous system's bytes -- and a hole is not don't-care: a free FAT directory slot and a free FAT entry are *defined* as zeros. Writing the rpi3's image onto a card that had held a PinePhone system left 100 MB of the 130 MB boot partition unwritten, the root directory region among it; it mounted with garbage entries beside the real files, `ls` gave `Input/output error`, and fsck found files whose start clusters were past the end of the partition. Every block bmaptool *did* write was correct and checksummed against the map, which is exactly why nothing caught it -- the map's checksums cover what was written and never what was not, and the bmap path has no read-back check by deliberate design (`disk_verify_dd` is dd-only). `refresh_fast_path`'s note had reasoned the opposite: safe "because its holes are filesystem free space that was never written", which holds for a blank card and fails for a reused one. `disk_write_bmap` now zeros the image's extent first -- the image's extent only, since zeroing the other 56 GB of a 64 GB card costs more than the write. The image itself was clean throughout (`93 files, 7656/33241 clusters` before and after), so the corruption was purely the write path's |
| `wk sysimage build --detach` leaves `yocto.status` saying what happened | only the *waiting* parent writing the terminal state, so a detached build's status reads `state=running` for ever -- after the artifacts are written, after the process is gone. `wk ls` then reports an idle workspace as "running" indefinitely, and the only way to tell a live build from a finished one is `podman exec`-ing in to check the pid. Found while trying to decide whether a 75 GB workspace was busy or abandoned; the 2.46 workspace had been "running" since 02:14 with no bitbake in it. The fix belongs in `image/yocto-build.sh`, which is the half that runs detached and therefore the only half present at the end |
| `wk bench compare` can install scipy in the workspace that built the thing being compared | `pip3 install --user` alone, which Ubuntu 24.04 refuses outright under PEP 668 (`externally-managed-environment`). The SDK image is not externally managed so this passed everywhere it was tried; a yocto build workspace is plain Ubuntu 24.04 (`container/yocto/Containerfile`), so the one workspace holding the cross-built WebKit an on-board run measures was the one that could not compare its results. `--break-system-packages` *with* `--user` writes `~/.local/lib` and touches no distro package, whatever the flag is called, and the workspace is disposable by construction |
| an on-board run reaches `wk bench compare` at all | `wk pi bench` printing run-benchmark's JSON to stdout and stopping there, so the number lived in terminal scrollback and never entered the store `wk bench compare` reads. Two on-board runs could not be compared to each other by any means the tool offered -- which is most of why anyone takes a second one |
| `wk pi bench --ab` compares only rounds where **both** arms finished | including a survivor whose partner crashed: that puts an extra sample on one arm, and since a crash is usually a property of the build that crashed (OOM under a heavier binary), the surviving arm is precisely the one that would bias the answer. Dropped from the comparison, not deleted from the store -- the run that finished is still evidence about why its partner did not |
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
