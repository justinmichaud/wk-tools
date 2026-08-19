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

There is no runner yet: this is a hand-ticked checklist, which means "run the
test plan after a re-install" is not actually possible. Building `wk selftest`
is `docs/HANDOFF-test-runner.md`; until it lands, every [V] is only as fresh
as the last human pass.

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

## 1. Container workspaces — the default target

Run these inside the podman VM on macOS, and directly on Linux.

### Lifecycle
- [V] `wk sync` publishes a base snapshot
- [V] `wk new <ws>` succeeds **and waits for firstrun to finish**
- [V] firstrun completed everything: `~/.wk-firstrun-complete` exists, plus
      `.lldbinit`, `.bash_profile`, the shell rc line, `hx`, the push keys
- [V] `wk ls` / `wk status` list it; `wk status` exit code is 0
- [V] `wk rm <ws>` reclaims the container, the overlay, the home and the ssh alias
- [ ] `wk stop` then `wk start` returns every workspace to running

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
- [ ] `wk new <ws> --target remote` clones on the shared box
- [ ] `wk build` runs niced, job count from live load
- [ ] two builds serialise on the flock
- [V] `wk claude` refuses a remote workspace outright

## 4. Cross-cutting
- [V] a workspace remembers its target; only `wk new` needs `--target`
- [V] `wk status` with no argument walks every target
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
- [ ] `wk pr <user>:<branch>` checks out a PR head, confirming each command
- [ ] `wk report` prints the weekly summary (needs gh auth)
- [ ] the MCP server (`wk mcp`) creates and destroys a workspace from Claude
      Desktop, and refuses past its workspace cap
- [ ] `wk help` lists every cmd/* entry (no orphan commands, no dead entries)
- [V] `wk doctor` runs read-only (does not start the podman machine or a
      guest), reports ok/--/?? per item, and exits 1 when something is missing
- [ ] `wk doctor` on a freshly set-up machine reports everything ok, and each
      `--` line's printed fix actually clears that line when run
- [V] every shell file parses under both bash 5 and bash 3.2

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
| guest reaches nothing with the proxy bypassed | softnet actually enforcing, rather than the env vars being politely obeyed |
| a macOS guest reaches PyPI through the proxy | the proxy address being passed as a raw unset variable, so the guest got a hardcoded 192.168.64.1 that nothing listens on -- indistinguishable from the filter working |
| a macOS guest's `~/.zprofile` carries the `wk-tools: egress` block | provisioning silently not having run, which looks like a network fault |
| `mac-release-asan` and `mac-release` resolve to different dirs | Xcode toggling ASan within a configuration without changing the path, so the two builds silently share one tree |
| the guest desktop is visible after a reboot | three independent things hiding it -- screen saver, display sleep, and the screen *lock* -- where disabling any two is not enough |
