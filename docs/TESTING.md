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
- [ ] `wk sudo require` installs `/etc/sudoers.d/zz-<user>-passwd`, validates
      with `visudo -c` before and after, and then proves the property: `sudo -k`
      followed by `sudo -n true` fails
- [ ] `wk sudo require --target <machine>` does the same over ssh, with a
      terminal for the password prompt
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
- [V] a remote checkout gets `origin` = WebKit/WebKit, both forks, the
      machine's mirror, and `core.sshCommand` pointing at `$root/ssh/config`
      — nothing outside the wk root is edited
- [V] `wk push off --target <machine>` leaves ssh with `no such identity`
      there, and `git fetch fork main` still works
- [V] `wk status` lists each machine's keys by fingerprint with their state,
      once per machine, and the two machines' fingerprints differ

### Inside the workspace (the interface `wk claude` hands an agent)
- [V] `wk build jsc-release` with no workspace name builds this workspace
      (1m16s warm, from inside `selftest`)
- [V] `wk run -- -e 'print(1+1)'` prints 2; `wk test <args>` runs and reports
- [V] `wk status` / `wk logs` with no name report this workspace
- [V] the explicit form still works from inside: `wk run <own-name> -- ...`
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
- [V] `WK_TARGET_CMAKE` in a target's conf is added to every build's CMake
      flags on that machine, after the config's and the architecture's and
      before `--cmake`
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
      `~/.config/wk/targets/<name>.conf` to the remote driver
- [V] an unconfigured name is refused, and the error prints the conf to write
- [V] two remote targets in one process do not inherit each other's host,
      root or capacity
- [V] `wk new <ws> --target <machine>` clones on the shared box (39 s from a
      warm mirror; the mirror's own first fetch is 25 min / 13 GB)
- [V] the mirror is created and fetched by the driver — there is no `wk sync`
      for a remote target and there is not meant to be one
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
      CLI symlink (the app bundle's own cli is used)
- [ ] `wk new --zed` warns instead of failing when zed is missing or the
      workspace has no route yet (a vm before first boot) — the workspace is
      created either way
- [V] `wk claude` refuses a remote workspace outright
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
- [ ] `wk new`, container — final state: registered, running, firstrun
      complete. Killed before the container exists, during `wkdev-create`,
      and during firstrun: a re-run destroys the rubble and remakes the
      workspace from scratch, saying so — it never re-pins `base-id` over a
      surviving `changes/` layer, and never repairs a half-made workspace
      in place (wipe over repair: a workspace that never finished creating
      holds nothing of value)
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

### Status files are claims; evidence decides
- [ ] corrupt each status file (truncate, garbage): status reports the file
      as stale/unparseable, keeps listing everything else, and the
      evidence-derived answer is unchanged
- [ ] a status file written by an older schema (missing keys) still renders;
      unknown keys are ignored
- [ ] `wk enter --zed` against a `creating` workspace waits and opens only
      on `present`; against `broken` it refuses with the repair command

### Un-managed commands clobbering the record
- [ ] `podman rm` a workspace's container by hand: `wk ls`/`wk status` say
      "the record says container, the machine has none" and name the repair —
      not a bare `absent`, not a crash
- [ ] `tart delete` a guest by hand: same
- [ ] delete `$WK_STORE/ws/<n>` by hand under a live registry entry: same,
      and `wk gc` refuses to prune what the survivor may still pin
- [ ] `git fetch` into a published base snapshot by hand: the recorded sha no
      longer matches `rev-parse HEAD`; `wk new` and `wk sync` refuse it by name
- [ ] hand-edit `~/.ssh/config.d/wk`: the next `wk vm start` regenerates only
      its own block and leaves foreign lines alone
- [ ] an unreachable remote machine is reported unreachable with its timeout —
      never `absent` — and any fallback to the stale local status copy says so

### Prompts guard destructive actions only
- [ ] every interactive prompt in the tree guards a destructive action —
      `wk rm`, `wk vm rm`, `wk vm base --rebuild`, `wk remote rm` and its
      cleanup offers, `wk skills` overwrites, `wk pr`'s `reset --hard` —
      and nothing else prompts: `wk remote setup` writes its conf and says
      so, `wk pr` runs fetch/checkout/remote-add/set-upstream unprompted,
      and `wk pi setup` asks for an auth key only when the node is not
      already on the tailnet
- [ ] destructive prompts default to No and decline without a terminal,
      never block and never proceed (`WK_YES=1` is the scripted yes)

### Changing workstations — the view is calculated, not carried
- [ ] a fresh clone + `./setup` on a second workstation sees every remote
      target and its workspaces, with no state copied from the first
- [ ] deleting the workspace→target registry loses nothing: every command
      still resolves every workspace from the evidence on the targets
- [ ] a workspace name that exists on two targets refuses and names both;
      `--target` disambiguates
- [ ] a target that cannot be probed during resolution is reported
      unreachable by name — never silently left out of the view

### The fleet walk — `wk status` reaches every workstation that is up
- [ ] a bare `wk status` sshes into each listed workstation that answers,
      runs its read-only status, and merges the answer — this Mac's guests
      and containers appear in the other workstation's view, attributed to
      their host
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
| `wk status` with the podman machine stopped leaves it stopped | a read-only report booting a VM as a side effect |
| a workspace's `~/.ssh/id_*` are symlinks, not files | a copied deploy key: one the push switch cannot take back, and one that survives a key rotation as a dead key |
| `origin` is `WebKit/WebKit` in every target's checkout | the remote build machine pointing origin at the box's own shared clone, so `git log origin/main` answered for that box's last fetch |
| `wk build --cmakeargs` is refused | build-webkit taking one `--cmakeargs`, so a hand-written one silently replaces `DEVELOPER_MODE`, `USE_LIBBACKTRACE` and the architecture's flags |
| `claude` is on `$PATH` in a container workspace | firstrun installing the CLI to `~/.local/bin` and no rc putting it on the path, so `wk claude` failed with "claude: not found" in a workspace where it was installed and working |
| `wk ls` and `wk status` name the same set | one of them being forwarded whole into the podman VM, so a vm or remote workspace showed in one listing and not the other |
| a snapshot with no `sha` is invisible to `current_base` | an interrupted `wk sync` publishing rubble that the next `wk new` pins and the next `wk sync` hardlinks from |
| `wk new` over a workspace with no `base-id` remakes it | "already exists" answered about a half-made thing, and `base-id` re-pinned over a surviving `changes/` layer |
| a lock outlives the command that took it | a flock inherited by the `conmon` podman leaves behind, holding a workspace's lock for as long as the container exists |
