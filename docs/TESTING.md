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
      docs/HANDOFF-mac-minibrowser.md B9 for the one-command tag check**
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
      cores, load and free memory, capped by `WK_REMOTE_MAX_JOBS`
      (80 cores / load 1 / 248 GB → -j16); jsc-release in 8m47s
- [V] `wk build --dry-run` resolves remote paths: checkout, build dir, tools,
      and a ccache under the remote root rather than the container's /ccache
- [V] two builds serialise on the flock (a second `flock -w 2` is refused
      while a build runs)
- [V] the build log and status are written on the driving side; `wk status`
      and `wk logs` read them there
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
      missing `flock` is fatal while a missing zsh or ccache is a warning
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
- [ ] `wk pi setup rpi5`, and a workspace can reach the Pi
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
- [ ] a machine armed to reboot into a bench image shows the transition on
      its status line (image id, who armed it, when); after it reboots, the
      walk reports it in its new role or as off-ssh under the image driver
- [ ] an armed machine still in its old role long after arming, or back in
      its old role with the arming record uncleared, is flagged as desync
- [ ] a mutating command against a machine armed to leave its role warns or
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

## 7. Boot images and role transitions — `wk image`, `wk boot` (Linux)

A machine booted into an image is a *role transition*, not a reboot: the box
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
- [V] a benchmark runs in the benchmark role or it does not run — the
      workstation role is refused, and `--force` does not open it (2026-08-20,
      in `wk selftest --quick`: same refusal with and without the flag; the two
      roles produce the same shape of result and nothing tells them apart
      afterwards)
- [V] `--dry-run` still describes it from the workstation role, quoting
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

### The rehearsal: a guest standing in for the benchmark role (`benchvm`)
- [V] `wk boot benchvm --status` on a machine with no such guest says so —
      "guest=wk-bench (absent)" — instead of exiting silently (2026-08-20: it
      did exit silently, because the driver's probes ran under `set -o
      pipefail` and a guest that is merely off is a normal state)
- [V] the arming model reaches the "next step" text: a guest is *started*, not
      armed and rebooted, and the staging output says so (2026-08-20)
- [ ] build in one guest, stage to the other, run there: the whole path with
      nothing hands-on in the middle
- [ ] the staged tree is products only — no `*.noindex`, no `DerivedData`, no
      `*.dSYM` — and is a few GB rather than the ~39 GB of an Apple build tree

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

### The Mac: a role transition nobody can automate (`wk boot mbp`)
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

- [V] `wk image build <profile> --dry-run` resolves the profile, the machine,
      the base and the destination, builds nothing, and names any missing
      tooling rather than failing at the first `require`
- [V] `wk image build rpi5-perf` completes with **no sudo anywhere** — the FAT
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
- [V] `wk image ls` lists only images with a manifest; a build directory
      without one is reported as rubble, by name
- [V] `wk image show <id>` re-hashes `disk.img` and refuses an image that no
      longer matches its manifest
- [V] a build that fails partway leaves a directory with no manifest; it is
      reported as rubble and the next build destroys it, never "already
      exists" (rule 2) — seen for real when the target machine went away
      mid-build
- [V] a machine that is unreachable fails the build *before* the unpack, and
      names the reason: the network profile is read from that machine
- [ ] `kill -9` mid-build, re-run: same, at every other point
- [ ] two `wk image build` at once: the second waits on the store lock rather
      than racing the first's rubble cleanup (rule 4)

### Writing an image onto a disk — `wk image write`

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
- [V] one verb, not two: `wk image flash` and `wk pi flash` both fail with a
      message saying why the *name* was wrong ("reads as reflash that machine";
      "nothing here is permanent"), not merely where it moved
- [V] `wk image disks <machine>` marks which disk that machine is configured to
      boot from, so `wk boot` does not look like it takes a disk argument
- [V] after writing, the closing line says whether anything will boot it and
      names `wk boot <machine>` — the sentence that would have prevented the
      original confusion

### The image must be able to boot from what it is written to

- [V] `wk image flash rpi4` with the yocto image is **refused**: the image says
      `root=/dev/mmcblk0p2` and `MACH_DEVICE` is `/dev/sda`, so the firmware
      would load the kernel and the kernel would find no root. The message says
      exactly that, and says it in the dry run too
- [V] no false positive on the distro images: `root=LABEL=wk-image-root` is
      classed `portable` and passes on any device
- [V] compared by device *kind*, not path — a card written in one machine's
      reader is routinely booted in another, so `/dev/mmcblk0` on the writer and
      on the booter are two facts that merely share a spelling
- [V] `WK_ANY_ROOT=1` overrides it, and says the write proves the transfer only

### Flashing

- [V] `wk image flash <machine> --dry-run` writes nothing
- [V] it refuses a device with mounted partitions until it has agreement to
      erase, then unmounts them itself rather than sending the user to a
      hand-typed `umount`
- [ ] it refuses a device whose transport is not usb, and the machine's own
      root device
- [V] the write is verified by reading the same number of bytes back off the
      device and comparing hashes (4.6 GB, ~7 min over WiFi with zstd)
- [ ] the prompt appears and "no" leaves the device untouched

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
- [ ] with the boot device absent, arming falls through to the normal role
      rather than hanging at firmware

### Reading the transition — intent is never evidence

- [V] `wk boot <machine> --status` starts nothing, writes nothing and repairs
      nothing (rule 6)
- [V] it reports the role from the machine's own identity marker, and the
      firmware's persistent boot order alongside it
- [ ] armed and not yet rebooted: reported as ARMED, exit 2, with the warning
      that the next reboot leaves this role
- [V] armed, rebooted, and back in the normal role: the record is reported as
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
- [ ] first boot is slow (~17 min) because `packages:` installs over WiFi
      before `runcmd` applies the sysctls. Anything not needing a per-machine
      secret should move into the rootfs at build time, where systemd applies
      it with no cloud-init involved.

### Serving — `wk serve`

- [V] `wk serve --dry-run` resolves image, address, ports and helper state and
      starts nothing
- [V] `wk serve` fills its TFTP root from an image already in the store, with
      mtools at a byte offset — no mount, no privilege, and what a netboot
      client gets is the same artifact `wk image flash` writes to a stick
- [V] a second machine on the LAN fetches boot files over TFTP: `config.txt`
      (2699 B) and a 14.7 MB kernel, byte-identical, ~23 s over WiFi at a
      firmware-realistic 1468-byte block size
- [V] a request under a serial-number directory that does not exist falls back
      to the root, and the log says which happened — the board's serial is not
      knowable before it first asks, and this is the part of Pi netboot most
      often got wrong from a machine with no console
- [V] path traversal is refused, including via a serial-directory prefix
      (`deadbeef/../../etc/passwd`)
- [V] a write request is refused explicitly rather than ignored — the server
      has no WRQ handler at all
- [V] `wk serve --status` reports from evidence: a pid that is alive **and**
      still the process we started, never the status file alone
- [V] `wk serve --stop` stops both daemons and forgets the record
- [V] without the privileged helper it degrades to port 6969 and says plainly
      that no firmware can reach it — a Pi's TFTP client speaks to port 69 and
      cannot be told otherwise
- [ ] with the helper installed, port 69 binds, and `drop_privileges()` has
      become the invoking user before any path is resolved (`./setup` once)
- [ ] `wk serve` refuses while a build or a bench run is live on the same host
- [V] serving an image whose `root=` is a local label/UUID is **refused**, with
      the local path named instead — a netbooted client would otherwise fetch
      the kernel, find no root, and (with `panic=10` and network first in
      `BOOT_ORDER`) loop headlessly
- [ ] a netboot root that actually works: NFS, or an initramfs pulling a
      squashfs into RAM. **Not built.** Until it is, netboot proves the
      transfer and nothing further.
- [ ] the service alias IP is claimed for the duration and released after, so
      the fixed `TFTP_IP` in a client's firmware can point at whichever machine
      is serving

### Why the server is not a container

- [V] rootless podman cannot publish port 69 (`rootlessport cannot expose
      privileged port 69`), and cannot bind it with `--network host
      --cap-add NET_BIND_SERVICE` either — the capability lands in the
      container's user namespace, the bind is checked against the initial one
- [V] podman's own suggested workaround, `ip_unprivileged_port_start=69`, is a
      **broader** grant than the helper it would replace: every local process
      could then bind 69–1023 permanently, including 80 and 443
- [ ] the same on macOS, where the podman machine's user-mode NAT plus TFTP's
      ephemeral data port make it worse again — untested, and not worth
      testing unless someone proposes it a second time

### Building a Yocto image — `wk image build rpi4-wpe-2.48`

The second builder behind the same verb. What is checked here is the seam
between the two, and the things the distro builder never had to think about: a
build that outlives its driver, a cache that outlives its workspace, and an
egress list that cannot be "every upstream in six layers".

- [V] `wk image build rpi4-wpe-2.48 --dry-run` resolves the branch, the
      cross-target, the recipe, the stage list, the workspace, the two caches
      and the free disk, and builds nothing
- [V] the same command with a *distro* profile still takes the distro path
      unchanged — one verb, dispatched on `IMG_BUILDER`, and neither builder
      sees the other's flags (`wk image build rpi4-wpe-2.48 --bogus` names the
      yocto flags; `wk image build rpi4-perf --dry-run` is untouched)
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
- [ ] `wk image build rpi4-wpe-2.48` compiles the whole image — **not yet run to
      completion** (hours; see `docs/HANDOFF-yocto.md`)
- [V] the *import* half is verified independently of it, against a hand-made
      8 MB image in a throwaway cross-target directory: `disk.img` and
      `rootfs.tar.xz` land in the store, the manifest is written last,
      `wk image ls` lists it and `wk image show` re-hashes it and agrees. Worth
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
      `wk image ls` reports as rubble by name (seen for real, from the corrupt
      first attempt)
- [V] the manifest records `cross_version`, the hash
      `cross-toolchain-helper` also installs in the image at
      `/usr/share/cross-target-info-version`, so "is this board running this
      image" is a string comparison rather than a belief
- [ ] a second `wk image build` of the same profile reuses the sstate cache and
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
- [V] `wk image build <profile> --stop` stops a detached build, killing bitbake
      as well as the wrapper, with SIGTERM rather than SIGKILL so bitbake closes
      its own state and the sstate cache stays resumable
- [ ] a killed build leaves no lock behind. **Fails today**: the atomic-mkdir
      lock writes the holder's pid *inside* the directory it has just created,
      and the next taker reclaims only when it can read that pid — so a
      directory with no pid file is indistinguishable from a live holder, and
      `wk rm` waited out its whole timeout on one. `lib/common.sh`, reported in
      `docs/HANDOFF-yocto.md` rather than fixed (other lane's file this week)
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

- [V] a stage started by `wk image build` survives the driving process being
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
- [V] the TFTP root is emptied when the image its stamp names is gone
- [V] `cache/images` (the pinned distro bases, 1.5 GB) is reported as kept
      rather than pruned — it is re-downloadable but slow, and growth should be
      visible rather than silent

### The EEPROM's only writer — `wk pi netboot-enable`

- [V] `--dry-run` prints a unified diff of the firmware configuration and
      writes nothing
- [V] network is added as the **last** entry before the restart nibble
      (`0xf461` → `0xf2461`), never the first: `BOOT_ORDER` is shared by both
      of a machine's roles, and a Pi that netboots by default is a Pi that
      stops being a workstation the moment a server answers
- [V] the transform is idempotent and reversible (`--revert` removes the
      network nibble and the netboot keys)
- [ ] an actual write, confirmed, applied, and read back on a board that is
      not this session's workstation
- [ ] `TFTP_PREFIX` is never written — the tftpd's root fallback removes the
      need, and it is the one value nobody can know before first contact

### Still owed here

- [V] the boot time a status reports equals `/proc/stat`'s btime on the
      machine. Two separate bugs produced a plausible-looking wrong answer
      here: `date -u -d "$(uptime -s)"` re-reading a local string as UTC, and
      an `awk '{print $2}'` whose `$2` did not survive three shells on the way
      through ssh. A remote one-liner that reports a *number* is worth
      checking against the source, because a wrong one does not look wrong.
- [ ] `wk status` shows the armed transition on the machine's line, and
      mutating commands aimed at an armed machine warn or refuse. The reader
      exists (`wk boot --status`); wiring it into `cmd/status` waits for the
      macOS lane to release that file.
- [ ] `wk` help text lists `image` and `boot`, and both are refused inside a
      workspace and on a shared build machine (`is_host_only`). Same reason.
- [ ] the rpi4/rpi3 half end to end: a board that actually netboots. Blocked
      on hardware, not on code — the rpi4 is not powered on (a full LAN sweep
      finds no Raspberry Pi but the rpi5), and moose's three wired NICs are all
      `carrier=0`.
- [V] `wk image build rpi3-perf` refuses rather than handing the fleet's only
      32-bit board an arm64 base
- [ ] the rpi3 at all: it needs proxy DHCP with option 43 (not built), an
      irreversible OTP burn, and its model (3B vs 3B+) established. Deliberately
      last.
- [ ] **first contact with an unreachable Pi is physical.** `netboot-enable`
      writes the EEPROM over ssh, so it needs the board running; netboot
      removes the *second* trip to a device, not the first. A Pi that answers
      nothing has to be met once with an SD card.

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
| a lock outlives the command that took it | a flock inherited by the `conmon` podman leaves behind, holding a workspace's lock for as long as the container exists |
