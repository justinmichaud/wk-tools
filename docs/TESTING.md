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
- [ ] `wk build <ws> gtk-release`, `wpe-release`
- [ ] `wk test <ws>` (JSC suite) and `wk test <ws> --layout`
- [V] every build writes `compile_commands.json` by default
- [V] `wk logs <ws>` shows `(none)` under errors for a successful build
- [ ] `wk bench` produces per-subtest results with confidence intervals

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
      symptom is `open -a`, which nothing uses. Do not spend time here; see
      docs/HANDOFF.md lane B step 2 (item B9) for the one-command tag check**
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
      (`docs/HANDOFF-cross-compile.md`), which is already installed on it
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
- [ ] `wk backup` → `./setup` round-trips with no spurious changes
- [ ] `wk backup`'s junk filters strip what they claim (weather location,
      WiFi UUIDs, last-folder paths, timestamps)
- [ ] `wk skills` status/diff/pull/push; pull refuses over uncommitted repo edits
- [ ] `wk key register` / `check`
- [ ] `wk pi setup rpi4`, and a workspace can reach the Pi (the rpi5 is a
      workstation and never goes through `wk pi setup`)
- [ ] `wk enter <ws>` lands in a shell; `wk enter <ws> <cmd>` runs the command
- [ ] `wk logs <ws> -f` follows a live build
- [ ] `wk stop --keep-vm` leaves the podman machine running
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
- [!] and the launch is where it ends on a macOS host, for a container
      workspace: it was forwarded into the podman VM, looked for
      `/Applications/Zed.app` in a Linux VM and reported "zed is not installed"
      about a Mac that has it. Two reasons it cannot work as it stands, both now
      in the refusal: Zed runs on the host and the container is inside a VM with
      no ssh route in from here, and the generated `wk-<name>` alias is written
      by whichever side ran `wk new` — the VM. Refused on the host now, naming a
      macOS guest or a remote target instead
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
      and never runs. `docs/HANDOFF-benchmarking.md`'s "residual hands-on case"
      is exactly this, observed
- [V] these Yocto images carry no `panic=10` where the distro profiles do — so
      a distro image that cannot find its root reboots and a Yocto one hangs.
      Not the cause here, and an undocumented asymmetry worth knowing
- [ ] the safe order, not taken: prove a kernel argument on the **SD** system
      first, where a bad one still leaves the stick an unarmed fall-through

### Reproducibility of the bench stick — the standing rule

**cattle, not pets** (`docs/HANDOFF-cattle.md`) applied to the one device that
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
      defines a machine says how to reproduce it (hand-checked; the ledger is
      `docs/HANDOFF-cattle.md`)
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
- [ ] Jumpdrive boots the phone, exports its eMMC over USB, and
      `wk sysimage write <bridge image> --disk rpi5:/dev/sdX` installs the bridge
      onto internal storage — the end state, using the ordinary write path with
      no new verb
- [V] there is exactly one route. The second one (write to the idle eMMC from
      the running card system over ssh) was written and then removed: it
      reimplemented dd, partition growth and identity-copying to reach a disk
      that Jumpdrive hands to `wk sysimage write` for free. One way to write a
      disk, and it is the one with the refusals

### Needs the hardware
- [ ] a PinePhone flashed with pmOS, answering ssh, provisioned end to end:
      `wk bridge setup tailnet-bridge-generic` from nothing to a health check
      that passes. Blocked on the phone, which is in a drawer
- [ ] the dock does a USB Data Role Swap and the adapter enumerates. This is
      the step with no software fallback; a dock that refuses is hardware
- [ ] the udev rename takes effect: `lan0` exists after a re-plug, and the NM
      keyfile puts the router address on it
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
| no `Host wk-<name>` alias for a container workspace | a fictional `HostName localhost` entry pointing zed at the host's own filesystem -- containers have no sshd |
| `--zed` on a container workspace is refused on a macOS host | "zed is not installed" from inside the podman VM, about a Mac that has it -- and, behind that, an editor opened on an alias only the VM can resolve |
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
