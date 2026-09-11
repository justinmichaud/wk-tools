# Owed verification

Each line is a check nothing in `tests/` runs, on hardware or a platform a
test cannot reach from here. A line goes when a test covers it or the work
lands; the file goes with its last line.

## Host bootstrap

- `./setup` completes on Ubuntu 26.04
- an interactive shell on each host lands in zsh, with history settings applied
- `./setup --stage quiesce` installs the privileged helper (needs a terminal)
- `./setup --stage softnet` installs softnet SUID root (needs a terminal, macOS only)
- `wk sudo require` installs `/etc/sudoers.d/zz-<user>-passwd`, validates with `visudo -c` before and after, and then proves the property: `sudo -k` followed by `sudo -n true` fails
- `wk sudo require --target <machine>` on a machine that needs it: the password prompt gets a terminal over ssh

## Container workspaces

- `wk stop` then `wk start` returns every workspace to running
- `wk new <ws>` with no base snapshot says `run 'wk sync' first`, creates nothing, and registers nothing
- `wk ai claude <ws>` refuses to start when the proxy is stopped
- a Pi address in pi-hosts is reachable on port 22, and only port 22
- an address NOT in pi-hosts is refused (the negative, not just the positive)
- `sdk-patches/apply.sh` verify fails when a security section no-ops (temporarily break one token to prove the check can fail)
- one `claude login` in a workspace seeds `/secrets` and a second workspace inherits it
- `wk ai claude --force` starts an agent in a workspace whose sandbox check failed, with the warning repeated at exit
- `wk ai claude` in a terminal turns it back on when the session ends
- `wk key register` registers each machine's key separately, titled with the machine name, and `wk key check` reports per machine
- a remote checkout gets `origin` = WebKit/WebKit, both forks, the machine's mirror, and `core.sshCommand` pointing at `$root/ssh/config`, with nothing outside the wk root edited: true of `db`, false of `bb4`, which `wk remotes` reports
- a push from a remote target: buildbox4's deploy key exists and is not registered on GitHub (`wk key check` says so for bb4, devbox-arm64-2 and moose), and only the host can ask GitHub. Register each, then prove one push from a remote workspace
- `wk build <ws> gtk-release`, `wpe-release`
- `wk test <ws>` (JSC suite) and `wk test <ws> --layout`
- `wk bench` produces per-subtest results with confidence intervals
- the E2E: plant a compile error, `--babysit`, disconnect the terminal, reconnect — the error is fixed, the build is green, `wk status` stepped through the babysit record's attempts and ended ok, and babysit.report says what was changed
- a second `--babysit` while one is alive is refused by its record's pid; a record whose pid is dead does not refuse
- a stalled build (exit 124) ends `stalled` and is not handed to the model; exhausted attempts end `gave-up` naming the last exit
- claude failing to *run* ends `error` and does not retry forever
- `--babysit` refuses inside a workspace, on a remote target, and on the local target; `--babysit --dry-run` prints the command and starts nothing
- a babysitter killed mid-run: `wk status` reads its record as died rather than as progress, and the exit code says a person is needed
- `wk test <ws>` — on trunk it hits the known SIGBUS (see `docs/HANDOFF-linux-arm32.md`); on a 2.48 branch it is the real test
- an armhf workspace on `webkitglib/2.48`, where the ARMv7 JIT still exists
- the same, in an armhf workspace — needs a branch whose JSC runs, i.e. 2.48

## macOS guests

- guest resolves names only through softnet's resolver — known, accepted, and the one channel a container does not have
- the paravirtual GPU is feature-capped: families apple1-5 only, **no metal3, no raytracing**. Fine to interact with, NOT a basis for judging WebGPU or rendering performance against bare metal
- the base VM provisions with unfiltered egress -- Softnet's flags are passed in `t_start` only, so the base boots on plain vmnet (192.168.64.x) and `curl https://pypi.org` answers 200; the egress block it writes names the Softnet gateway and is for the clones. One-shot and host-driven with no agent in it, so the sandbox audit records it as a decision rather than finding it
- the tart window resizes and goes fullscreen -- broken today
- a guest on the host's own macOS: the guest is 26.4 against a 26.6.1 host because no suitable image exists upstream (re-checked 2026-08-18), and the only symptom is `open -a`. docs/HANDOFF.md lane B step 2 (item B9) carries the one-command tag check
- `open -a` inside the guest -- broken today, LaunchServices -10825: the app targets the 26.5 SDK and the guest is 26.4, so a launch goes through a direct bundle exec
- the ~100 `llvmcas:/... does not exist` warnings lldb prints resolving Swift-interop types: the explicit Swift `.pcm`s record inputs as CAS ids `llvm-cas --print-kind` calls "unknown object", including in a CAS built minutes earlier with the same id byte for byte, so the objects were never in the compilation cache. `symbols.cas-path` is measured and has no effect. Root-cause it
- a mac build reaches ImageDiff, not just `BUILD SUCCEEDED`: every pixel and reftest comparison needs `WebKitBuild/Release/ImageDiff`, and build-webkit's second xcodebuild invocation (`build-imagediff`, which has no `-scheme`) is the one that produces it
- a build in a **fresh clone off a warm base** completes in well under 45 min
- `wk build <ws> mac-debug`
- `wk build <ws> ios-sim-release` — config written, never run

## Remote build boxes

- `wk new --zed` warns instead of failing when zed is missing or the workspace has no route yet (a vm before first boot) — the workspace is created either way
- a **trunk** build needs a newer C++ toolchain than Debian 12 has: clang 18 with libstdc++ 12 has no `<format>`, which `Source/WTF/wtf/ FormattedLogging.h` has required since 2026-06-16. The box builds releases up to 2.52.x; trunk there needs the container SDK (`docs/HANDOFF-cross-compile.md`), which is already installed on it
- a machine with no zsh warns and stays in bash — written, and now with no machine here to exercise it (buildbox4 has zsh since 2026-08-19)
- a cleanup candidate accepted at the prompt is actually removed

## Cross-cutting commands

- `wk backup` → `./setup` round-trips with no spurious changes
- `wk backup`'s junk filters strip what they claim (weather location, WiFi UUIDs, last-folder paths, timestamps)
- `wk skills` status/diff/pull/push; pull refuses over uncommitted repo edits
- `wk key register` / `check`
- `wk pi setup rpi4`, and a workspace can reach the Pi (the rpi5 is a workstation and never goes through `wk pi setup`)
- `wk enter <ws>` lands in a shell; `wk enter <ws> <cmd>` runs the command
- `wk logs <ws> -f` follows a live build
- `wk stop --keep-vm` leaves the podman machine running
- `wk gc` prunes an unreferenced snapshot, keeps the newest, trims ccache, removes a stale bench payload seed, and reports the dirs it keeps
- `wk sync --all` and `WK_MIRROR_BRANCHES` carry the extra branches
- the MCP server (`wk mcp`) creates and destroys a workspace from Claude Desktop, and refuses past its workspace cap
- `wk doctor` on a freshly set-up machine reports everything ok, and each `--` line's printed fix actually clears that line when run
- `--mode sampling` in a real workspace prints the tier breakdown
- `--mode bytecode` leaves exactly one JSCProfile json and the summary prints (the file is identified by being newer than a stamp taken before the run, not by being newest in /tmp — two runs at once)
- `--mode samply` in a container: refuses with the host remedy when `perf_event_paranoid` > 1, records otherwise
- `--mode instruments` in a macOS guest records a .trace
- `--fetch` copies a recording out of a guest byte for byte (t_pull)
- `wk disk` inside a workspace answers the only version of the question available in there — this checkout, its build trees, its caches — because the host's store is not visible from a workspace by design
- `wk disk` with the podman machine stopped leaves it stopped (the read-only rule, measured the same way as `wk status`)
- `wk vm base --rm` deletes the golden base, then asks *separately* about the pulled OCI image (a download, not hours), and existing vm workspaces keep working — a `tart clone` is an independent guest

## Host: quiesce, session, gui

- `wk quiesce on` sets the performance governor with no password; `off` restores; `status` reports
- `wk session on` starts the kiosk compositor on the GPU; the socket appears at the fixed path and `/run/wk-session-mode` says `gpu`
- `wk session on --bmc` moves the session to the BMC chip, records `bmc`, and `wk bench` refuses to run against it
- `wk session gdm` / `gdm --bmc` bring up a desktop on the intended chip; `wk session status` shows `greeter: wayland` (x11 means not enforced)
- `wk session off` darkens every GPU output (`lit:` empty) and the console does not repaint over it; `wk session gdm` gets a desktop back
- `wk gui <ws>` opens MiniBrowser in the seat; in a bmc session it pins the browser to Mesa and the picture actually appears

## State, concurrency and clobbering

- `wk sync` killed during the `cp -al`, during the fetch, and after checkout but before the marker: the half-written snapshot does not exist to `current_base`, `wk new` can never pin it, the next `wk sync` finishes or replaces it, and the mirror is intact throughout. The marker half has tests; killing a real sync at each of the three points does not
- `wk new --target vm` — killed during the clone: a re-run replaces or completes the clone; registry and guest agree at the end
- `wk new --target <machine>` — killed mid-clone over ssh (and: the ssh cut rather than the process killed): the far checkout without its marker is rubble, `t_info` does not call it `present`, `wk status` says creating-with-dead-driver, and a re-run remakes it; killed between the far clone and the near state dir: both ends converge
- `wk rm`, each target — final state: no container/guest/checkout, no ws dir, no registry entry, no alias. Killed between each pair of those steps: a re-run finishes; the registry entry outlives the artifacts it describes (never the reverse), so the re-run can still resolve the target
- `wk build` — final state: the record says ok/failed with the log to prove it. Driver killed mid-build: `wk status` and `wk bench`'s idle check agree about the record, and a re-run simply builds
- `wk build --babysit` — babysitter killed between attempts: its record reads died; a re-run starts attempt 1 with the checkout in the state the last fix left it, stated in the report
- `wk test` — same convergence as build; a re-run overwrites cleanly
- `wk bench` — killed during seed: the payload without `.wk-seeded` is re-fetched whole; leaked `.tmp-*` seed dirs are pruned by `wk gc`. Killed during the run: `env.json` without `result.json` reads as a crashed run in `wk bench ls`, never as a comparable result
- `wk gc` — killed between prunes: nothing referenced was removed, and a re-run finishes the unreferenced remainder
- `wk vm base` / `--refresh` — killed host-side while the detached guest build runs: a second `--refresh` detects the live far-side build and waits or refuses — it never starts a second build in the same tree; killed guest-side: the rc file names the failure and a re-run rebuilds
- `wk vm start` / `wk stop` / `wk start` — killed mid-way: re-run converges (these are already idempotent by construction; prove it)
- `wk remote setup` — killed between the tools push, the conf write and the rc edits: a re-run completes every stage; the box is never half-provisioned with no path forward
- `wk remote rm` — killed after the far side is cleaned but before the local conf goes: a re-run (or the documented ordering) removes the rest; nothing ends orphaned on the far side with the local conf gone
- `wk pi setup` — killed mid-push: re-run converges; `pi-hosts` gains no duplicate or stale address
- `wk key register` — killed between keygen and GitHub registration: a re-run registers the existing key rather than generating a second
- `wk skills pull` / `push` — killed mid-rsync: a re-run completes; the half-synced tree is never left looking authoritative (rsync --delete re-converges both directions)
- `wk backup` — killed mid-write: the repo files are whole or unchanged (cmp-guarded write), never truncated
- `./setup` — killed inside any stage: a re-run reports and completes only what is missing; the second full run still reports no changes
- `wk quiesce on`/`off` — `off` after a reboot or a lost `on` record restores the machine's real prior values, not hardcoded guesses; a re-run of either is a no-op that says so
- `wk session on|gdm|off` — killed mid-transition: the next invocation reaches the asked-for mode from whatever half-state remains
- `wk ai claude` — killed during verify or launch: nothing persists but the verify log; a re-run verifies again from scratch
- a remote workspace whose clone is cut mid-way reads `creating` from *any* machine that asks, including the box itself — the marker is over there, not in the driving machine's record
- two `wk sync` at once: the second waits or refuses naming the first
- two `wk build` on one workspace: the second is refused at once on the vm and remote targets too (the container case is tests/test_build_kill.py); `wk vm base --refresh` while one runs is refused
- two `wk vm start` do not corrupt `~/.ssh/config.d/wk`
- corrupt a task record (truncate a field file, garbage in it): status reports it as unreadable, keeps listing everything else, and the evidence-derived answer is unchanged
- a record written by an older shape (missing fields) still renders; unknown fields are ignored
- against `broken` it refuses with the repair command
- `tart delete` a guest by hand: same
- delete `$WK_STORE/ws/<n>` by hand under a live registry entry: same, and `wk gc` refuses to prune what the survivor may still pin
- `git fetch` into a published base snapshot by hand: the recorded sha no longer matches `rev-parse HEAD`; `wk new` and `wk sync` refuse it by name
- hand-edit `~/.ssh/config.d/wk`: the next `wk vm start` regenerates only its own block and leaves foreign lines alone
- a machine still running an older wk-tools answers a *delegated* `wk status` by its own rules — measured 2026-08-19: a workspace whose creation had died read `present` from the far side and `creating` from this one. The fleet block already says the tooling DIFFERS; what it does not say is that the difference changes answers, not just versions
- every interactive prompt in the tree guards a destructive action — `wk rm`, `wk vm rm`, `wk vm base --rebuild`, `wk vm base --rm` and its second question about the image cache, `wk gc --purge-mirror`, `wk remote rm` and its cleanup offers, `wk skills` overwrites, `wk pr`'s `reset --hard` — and nothing else prompts: `wk remote setup` writes its conf and says so, `wk pr` runs fetch/checkout/remote-add/set-upstream unprompted, and `wk pi setup` asks for an auth key only when the node is not already on the tailnet
- destructive prompts default to No and decline without a terminal, never block and never proceed (`WK_YES=1` is the scripted yes)
- a second device: fresh clone + `./setup`, and every machine in the registry is a target there with no state copied from the first
- a workspace name that exists on two targets refuses and names both; `--target` disambiguates
- a target that cannot be probed during resolution is reported unreachable by name — never silently left out of the view
- a workstation that is down is listed unreachable with its timeout; the walk never hangs on it and never drops it silently
- the remote half is read-only absolutely: nothing starts, boots or is repaired on the far machine (its podman machine stays stopped)
- wk-tools version skew is flagged: a machine on an older or dirty checkout is named, with both shas
- the same workspace name alive on two machines is reported as a conflict, not listed twice as if normal
- two workstations reaching one build box see one state; a disagreement is reported naming both views
- a machine armed to reboot into a bench system shows the transition on its status line (system id, who armed it, when); after it reboots, the walk reports its new mode or off-ssh under the bench channel
- an armed machine still in host mode long after arming, or back in host mode with the arming record uncleared, is flagged as desync
- a mutating command against a machine armed to leave host mode warns or refuses — no build starts on a box about to reboot out from under it
- the exit code aggregates the worst state found anywhere in the fleet
- provisioning the second of two remotes that share one home folder does not clobber the first's identity; `wk` on either box resolves its own target (by hostname against the confs, not a shared marker); `wk remote rm` of one leaves the other provisioned and working
- builds from the two machines never collide in a shared checkout or on a shared lock: build dirs and locks are keyed per machine, derived, not configured

## Systems and mode transitions

- a real measured run: a real `mac-release` build, staged from a guest, on a real benchmark install
- root-cause the first staged run after a copy timing out at 900 s where the second, identical, finishes in about five minutes; the candidates are a first-launch Gatekeeper/XProtect scan of 1.5 GB of freshly copied binaries, webkitpy's autoinstall on first use, and a cold dyld cache. Until then a first run after a stage takes a generous `--timeout`
- the same on a benchmark install, before a run
- the same against a real benchmark install, booted for real
- `wk bench stage <ws> --to mbp` from a macOS guest onto the volume
- `kill -9` mid-build, re-run: same, at every other point
- two `wk sysimage build` at once: the second waits on the store lock rather than racing the first's rubble cleanup (rule 4)
- the card actually boots an rpi4 (needs the card moved to the board)
- the confirmation prompt appears and "no" leaves the device untouched
- left alone, the image hands the machine back by itself within the profile's watchdog period
- with the boot device absent, arming falls through to host mode rather than hanging at firmware
- armed and not yet rebooted: reported as ARMED, exit 2, with the warning that the next reboot leaves this role
- first boot is slow (~17 min) because `packages:` installs over WiFi. (The sysctls already moved into the rootfs at build time; anything else not needing a per-machine secret should follow.)
- `wk pi bench` prints `result after 0s` for a run that took 22 minutes. The elapsed counter is wrong; the result is not. Cosmetic, and worth fixing before anyone reads the timing as data
- the result is printed and not **saved**: nothing files it beside `wk bench`'s own runs, so `wk bench ls`/`compare` cannot see it. That is the remaining half of "record provenance next to wk bench's results" (docs/HANDOFF-pi-deploy.md)
- the manifest still records `display_forced` for this image although the forced mode was removed from the stick's cmdline by hand, so the run warned about a mode it was not using. A manifest describes the image and the disk was edited after it -- the provenance and the disk have to be reconciled
- a wrapper whose command failed can still be `yocto_any_running` for a while afterwards (ninja finishes in-flight jobs), so a restart refuses with "already running" until `--stop`. Benign, and confusing the first time
- the board's checkout is a sparse checkout of `Tools/` (~5,000 files), not `--depth 1` of WPEWebKit (427,711 files, ~4.2 GB, longer to check out onto a USB stick on a Pi 4 than cross-building WebKit takes): the board runs only `Tools/CISupport/built-product-archive`, `Tools/Scripts/run-benchmark`, `run-minibrowser` and their imports
- the safe order, not taken: prove a kernel argument on the **SD** system first, where a bad one still leaves the stick an unarmed fall-through
- reproduce the rpi4's stick from the repo instead of the hand-stamped one in it today: `wk sysimage write rpi4-wpe-2.48-20260820T124927Z --disk rpi4:/dev/sda` does all four steps by construction. It needs a confirmed erase, so it is a person's
- the distro builder's `LABEL=` images have the same disk-copy exposure (`relabel` makes each *image* unique, not each disk). Not yet reached: the fleet has one Ubuntu bench system per board
- a bmap write onto **used** media, which is the case the above reasoning is about and which no test has exercised (the rpi4 has no bmaptool, so every write to it takes the dd path today)
- settle whether meta-wk's pseudo bump is needed at all: 24.04 with `wpe-2.46` is known-good unpatched and the Yocto spec is byte-identical between `wpe-2.46` and `webkitglib/2.48` (same poky `6879650b`, same layers, same `local-rpi4-64bits-mesa.conf`), so the variable is not the branch. Run the three-line reproducer in the known-good container and diff `objdump -T $(command -v tar)` between the two; docs/HANDOFF-yocto.md has the note. Delete `image/yocto/meta-wk` if it is unnecessary
- a second `wk sysimage build` of the same profile reuses the sstate cache and is dramatically faster than the first
- `--keep-work` turns off `rm_work`, and `bitbake -c menuconfig virtual/kernel` in `--bitbake-dev-shell` then has the kernel tree to configure (the wiki's 16 KB-page / 36-bit-VA flow)
- `--detach` returns immediately, and re-running the same stage later re-attaches rather than starting a second one
- a stage still running is refused by name rather than started twice
- "finished" is decided from the wrapper's own marker line, not from an exit status — a process nobody forked cannot be waited for, and a build whose container was killed leaves a log with no marker
- a long-silent bitbake task is **reported and not killed**, unlike a stalled compile: `run_watched`'s abort would cost hours that sstate cannot always give back
- an actual write, confirmed, applied, and read back on a board that is not this session's workstation
- mutating commands aimed at an armed machine warn or refuse — still open; the fleet block shows the arming, nothing gates on it yet.
- `wk help hardware` stays true to `boot/machines.sh` and the drivers — hand-checked when either changes; it documents the media each device needs and which steps are a person's.
- the rpi3 end to end: provision it, `wk sysimage write` its SD card, and boot it. (The OTP door stays shut for good; its driver is a hands-on stub until then.)
- **first contact with an unreachable Pi is physical.** `wk pi boot-order` writes the EEPROM over ssh, so it needs the board running. A Pi that answers nothing has to be met once with a card written by `wk sysimage write`.

## Tailnet bridges

- the first provision has to land while the phone is awake, and that is circular: step 4 of the role is what stops it suspending, so an unprovisioned phone idles off the network after a few minutes. Said in the error message and in `wk help bridge`. Worth revisiting if it keeps costing runs — the image could ship the elogind drop-in itself
- the segment itself — `lan0`, DHCP to rpi3/rpi4 — is untested, because it needs the USB-C Ethernet dock physically attached and none was
- `wk bridge tailnet <name>` untested: it is the one step left, and the only one needing a credential fetched by hand
- `wk bridge provision tailnet-bridge-generic` end to end on the eMMC route: Jumpdrive to the card, the phone cabled to rpi5, its internal storage appearing as a new USB disk, the bridge image written there, the card out, and the phone coming up on its own install. The disk is found by *difference* against a baseline taken before the phone was attached — Jumpdrive exports the SD card as well as the eMMC, so the candidate set is normally two, and writing to the wrong one destroys the tool being used to do the writing. That one is asked, not guessed
- the phone comes up on its own install and `wk bridge setup tailnet-bridge-generic` reaches it. First contact is `<hostname>.local`: the image carries avahi with the service enabled by symlink, because until `tailscale up` has run there is no tailnet name and the DHCP address is not knowable in advance
- the downstream NIC enumerating perfectly and being forty times too slow: on a PinePhone the dock asks for the data-role swap and the phone is host by 3.5 s, then at 13 s the pmOS initramfs sets up its USB-gadget network and flips the phy back to peripheral (`Changing dr_mode to 2`) with a hub attached; EHCI fails for a minute (`device descriptor read/64, error -110`) and gives up at 77 s, the port falls to the companion OHCI controller, and the dock comes up at 12 Mbit/s instead of 480. Stop the initramfs taking the port
- recovery from a kernel that has stopped scheduling: there is no `/dev/watchdog` because `linux-postmarketos-allwinner` is built with `CONFIG_SUNXI_WATCHDOG` unset, though the A64 carries a watchdog and the device tree declares it (`allwinner,sun50i-a64-wdt` at 0x1c20ca0). Until a kernel with it on exists the netwatch ladder is this phone's only recovery
- a board on the segment gets its reserved address, and is reachable from a workspace over the tailnet — which needs `autoApprovers` in the policy, the failure that looks exactly like success
- the escalation ladder does what it says: pull the AP, watch `wk-bridge-netwatch` climb, and confirm it stops at the reboot budget rather than rebooting forever
- `BR_CAMERA=http` streams at all. The pipeline is unproven on both phones: libcamera-era sensors do not always present a format ffmpeg will open
- the Librem 5 reflashed to pmOS and moved onto this role, replacing the hand-built PureOS configuration. Until then `wk bridge setup` refuses it, which is the intended behaviour rather than a gap

