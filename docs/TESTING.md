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
- [!] `wk build <config>` / `wk run` / `wk test` with no workspace name —
      **does not exist**; see docs/HANDOFF-wk-in-workspace.md (blocking)
- [ ] `wk build --list` works in a workspace with no podman anywhere

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
- [ ] the tart window resizes / goes fullscreen  **-- KNOWN BROKEN**
- [ ] the guest runs the latest macOS  **-- 26.4 vs host 26.6.1; no suitable
      image published, see docs/HANDOFF-mac-minibrowser.md**
- [ ] `open -a` inside the guest  **-- KNOWN BROKEN**, LaunchServices -10825:
      the app targets the 26.5 SDK, the guest is 26.4. Use direct bundle exec
- [ ] a debugger attaches to MiniBrowser / a layout test  -- not started

### Build and run
- [V] `wk build <ws> mac-release` succeeds
- [V] `wk run <ws> --config mac-release` runs jsc via `DYLD_FRAMEWORK_PATH`
- [ ] a build in a **fresh clone off a warm base** completes in well under 45 min
- [ ] `wk build <ws> mac-debug`
- [ ] `wk build <ws> ios-sim-release` — config written, never run
- [ ] `wk test` against an Apple port -- **was impossible until 2026-08-18**:
      `run-webkit-tests` is python, imports webkitpy, and webkitpy autoinstalls
      from PyPI, which the guest could not reach (see the egress fix below)
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
| `wk start` / `wk stop` with a driver loaded | driver defaults evaluated at source time |
| two `config_build_dir` definitions | a clean merge leaving the wrong one live |
| guest reaches nothing with the proxy bypassed | softnet actually enforcing, rather than the env vars being politely obeyed |
| a macOS guest reaches PyPI through the proxy | the proxy address being passed as a raw unset variable, so the guest got a hardcoded 192.168.64.1 that nothing listens on -- indistinguishable from the filter working |
| a macOS guest's `~/.zprofile` carries the `wk-tools: egress` block | provisioning silently not having run, which looks like a network fault |
| `mac-release-asan` and `mac-release` resolve to different dirs | Xcode toggling ASan within a configuration without changing the path, so the two builds silently share one tree |
| the guest desktop is visible after a reboot | three independent things hiding it -- screen saver, display sleep, and the screen *lock* -- where disabling any two is not enough |
