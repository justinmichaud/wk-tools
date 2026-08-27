# HANDOFF — the unittest suite in `tests/`

`wk selftest` runs the stdlib unittest suite under `tests/` (`--quick` for the
modules that need no VM, machine or board). Every section of the retired bash
runner is ported; what remains is the behaviour that has no test yet.

## Tests to write

Grouped by what they exercise. Most of these need no hardware — a stub/fake
target driver (the technique the `state` section already uses for
`chk_ws_state_words`) or a faked filesystem/status-file state is enough.

**Crash-only convergence** (kill at the worst moment, re-run, reach the
declared final state, nothing half-made looks complete) — port the whole
"Interrupted and restarted" table from the old plan as one test class per
command, each case a named kill point:
`wk new` (container: the two remaining kill points, before-container-exists
and during `wkdev-create`; `--target vm`; `--target <machine>`, including an
ssh cut rather than a killed process), `wk rm` (each target, killed between
each pair of steps), `wk build` (dead pid + cold log reads as crashed),
`wk build --babysit`, `wk test`, `wk bench` (seed and run), `wk gc`,
`wk vm base`/`--refresh`, `wk vm start`/`stop`/`start`, `wk remote setup`,
`wk remote rm`, `wk pi setup`, `wk key register`, `wk skills pull`/`push`,
`wk backup`, `./setup`, `wk quiesce on`/`off`, `wk session`, `wk claude`.

**Concurrency**: two `wk build` on one workspace serialize on the workspace
lock; `wk vm base --refresh` is refused while one is running; two
`wk vm start` do not corrupt `~/.ssh/config.d/wk`.

**Status files are claims, not evidence**: a corrupted (truncated/garbage)
status file is reported stale/unparseable without hiding everything else; a
status file written by an older schema (missing keys) still renders, and
unknown keys are ignored.

**Readiness**: a remote workspace whose clone is cut mid-way reads `creating`
from *any* machine that asks, not only the driving one; `wk enter --zed`
against a `broken` workspace refuses with the repair command.

**Un-managed clobbering is detected, not silently trusted**: `tart delete` by
hand; deleting `$WK_STORE/ws/<n>` under a live workspace (`wk gc` refuses
to prune what the survivor may still pin); `git fetch` into a published base
snapshot by hand (the recorded sha no longer matches, refused by name);
hand-editing `~/.ssh/config.d/wk` — the next `wk vm start` regenerates only
its own block; a delegated `wk status` from an older wk-tools answering by its
own, possibly wrong, rules.

**Prompts guard destructive actions only**: tests/test_prompts.py audits
this — lib/common.sh's `confirm()` is the one yes/no prompt helper in
`cmd/*`, `lib/*.sh`, `boot/*.sh`, `bench/*.sh` and `image/*.sh` (no raw
`read` implements a competing one), and it defaults to No and declines
without a terminal, and every call site guards a destructive action.

**`wk status` text view**: `--json`/`--html` output is unaffected by the
column-alignment renderer.

**Fleet walk**, against faked ssh/status responses rather than real machines:
wk-tools version skew flagged by name with both shas; the same workspace name
on two machines reported as a conflict; two workstations reaching one build
box disagreeing, both views named; an armed machine's transition shown on its
status line, and flagged as desync when stale; the exit code aggregating the
worst state anywhere in the fleet.


**Image/disk logic that needs no card in a reader**: `image_root_class` /
`image_check_root`'s device-kind (not path) comparison; `disk_unique_identity`
producing distinct identities for two disks written from one image;
`image_fast_path_ok`'s provenance match (`wic_of` against the manifest's
`disk_sha256`); `_from_filter`'s decompressor selection by extension;
`_profile_from_path`'s profile-from-workspace-path derivation; the buildroot
`HOST_PYTHON_CONF_OPTS`/`HOST_PYTHON_DEPENDENCIES` three-line fix, reproduced
against buildroot's own rule shape rather than a real build; a `--from`
streamed write's skip of `image_check_boot_files`/`image_check_root` (only
the store-backed write runs them); a `--from` write installing the identity
marker and driving key but not the systemd units (`install_units`).

**Named explicitly as needing a check that runs them:**
§Z): the target-kind dispatch (`container|vm|remote|local`), `arch_is_native`,
`_remote_is_local`, `image_root_class`.

**Every `WK_*` override read with a default** (71 of them, per the 2026-08-26
review) is either documented where the user meets it and covered by a test,
or removed — an audit pass, not a single test.

## Properties to preserve from the old runner

- **A missing prerequisite is a visible SKIP**, never nothing.
- **Every run prints its own coverage** — a runner reporting ok over a
  fraction of the suite and saying nothing about the rest is the silent pass
  the old plan's preamble forbade.
- **It starts nothing** it does not need — never the podman machine, never a
  guest, unless the specific test needs it.
- **A check that cannot fail loudly is not a check.**

## Relationship to `wk doctor`

Unchanged: `wk doctor` answers "what is provisioned here" by inspecting state,
read-only, safe anywhere; the test suite answers "does it behave" by running
commands and may take minutes. Do not merge them.

## Regressions that need a permanent test

One test each; the second clause is what the test must catch.

- [ ] check — regression: what it catches
- [ ] `wk run` on GTK/WPE actually starts — regression: per-port build dir, and `LD_LIBRARY_PATH` being replaced rather than prepended
- [ ] `wk new` waits and checks the marker — regression: firstrun aborting while creation reports success
- [ ] `.config` in a new workspace is owned by the user — regression: the SDK's systemd mount re-appearing and breaking firstrun
- [ ] `wk logs` shows `(none)` on a good build — regression: `error:` matching inside message text
- [ ] `wk enter <ws> <cmd>` runs the command — regression: `exec`-ing a shell function
- [ ] an in-workspace build sizes from the whole machine — regression: the 12 GB desktop reserve being subtracted a second time inside a guest the host had already sized: -j5 in a 20 GB guest against -j9 from the host
- [ ] a bare `wk run` in a macOS guest finds a binary — regression: the container-shaped `jsc-release` default resolving a JSCOnly path that an Apple-port guest can never have
- [ ] `wk build --list` with podman stopped — regression: a static table lookup starting a two-minute VM boot, and blaming podman when it could not
- [ ] `wk verify` refuses inside a workspace — regression: a sandbox check whose gate (`WK_SANDBOX`) is unset reporting "intact" after measuring two things
- [ ] the host's `$HOME` has no `~/.wk-workspace` — regression: the marker escaping into the host and making every host command act on a workspace that is not there
- [ ] `wk build <config>` inside, `wk build <ws> <config>` outside — regression: one argument form silently shadowing the other
- [ ] `wk start` / `wk stop` with a driver loaded — regression: driver defaults evaluated at source time
- [ ] two `config_build_dir` definitions — regression: a clean merge leaving the wrong one live
- [ ] loading a second target does not leave the first driver's overrides live — regression: a driver's function override outliving its load: `wk status` walked container first, and every remote workspace's branch was then read by the container driver's `t_branch` — plausibly, and wrongly, as `-`
- [ ] guest reaches nothing with the proxy bypassed — regression: softnet actually enforcing, rather than the env vars being politely obeyed
- [ ] a macOS guest reaches PyPI through the proxy — regression: the proxy address being passed as a raw unset variable, so the guest got a hardcoded 192.168.64.1 that nothing listens on -- indistinguishable from the filter working
- [ ] a macOS guest's `~/.zprofile` carries the `wk-tools: egress` block — regression: provisioning silently not having run, which looks like a network fault
- [ ] `mac-release-asan` and `mac-release` resolve to different dirs — regression: Xcode toggling ASan within a configuration without changing the path, so the two builds silently share one tree
- [ ] the guest desktop is visible after a reboot — regression: three independent things hiding it -- screen saver, display sleep, and the screen *lock* -- where disabling any two is not enough
- [ ] `WK_TARGET=vm wk gc` runs at all — regression: cmd/gc sourcing a driver without lib/target.sh (`wk_state_dir: command not found`, verified)
- [ ] `wk selftest --section <typo>` fails — regression: the runner exiting 0 having run nothing -- the silent pass the plan's own preamble forbids
- [ ] a container workspace's `Host wk-<name>` alias is a ProxyCommand, not a hostname — regression: a fictional `HostName localhost`, pointing zed at the host's own filesystem -- a container has no interface and no address, so any hostname there is a guess
- [ ] `wk zed` refuses inside a workspace — regression: "zed is not installed" from a machine where it cannot be installed -- the same words the host prints when Zed really is missing
- [ ] `wk status` names each machine once, and every name is a machine — regression: two processes assembling one listing and disagreeing about what this machine is called (`hostname` in the podman VM is `localhost`), and a *target* name arriving where a machine name belongs -- which grew a machine called "container" holding the VM's own facts
- [ ] `wk status` with the podman machine stopped leaves it stopped — regression: a read-only report booting a VM as a side effect
- [ ] a workspace's `~/.ssh/id_*` are symlinks, not files — regression: a copied deploy key: one the push switch cannot take back, and one that survives a key rotation as a dead key
- [ ] `origin` is `WebKit/WebKit` in every target's checkout — regression: the remote build machine pointing origin at the box's own shared clone, so `git log origin/main` answered for that box's last fetch
- [ ] `wk build --cmakeargs` is refused — regression: build-webkit taking one `--cmakeargs`, so a hand-written one silently replaces `DEVELOPER_MODE`, `USE_LIBBACKTRACE` and the architecture's flags
- [ ] `claude` is on `$PATH` in a container workspace — regression: firstrun installing the CLI to `~/.local/bin` and no rc putting it on the path, so `wk claude` failed with "claude: not found" in a workspace where it was installed and working
- [ ] `wk ls` and `wk status` name the same set — regression: one of them being forwarded whole into the podman VM, so a vm or remote workspace showed in one listing and not the other
- [ ] a snapshot with no `sha` is invisible to `current_base` — regression: an interrupted `wk sync` publishing rubble that the next `wk new` pins and the next `wk sync` hardlinks from
- [ ] `wk new` over a workspace with no `base-id` remakes it — regression: "already exists" answered about a half-made thing, and `base-id` re-pinned over a surviving `changes/` layer
- [ ] `wk sysimage build <pmos profile>` prints something — regression: `set -o pipefail` turning `x=$(ssh "cat missing" \
- [ ] `wk sysimage write --dry-run` prints its plan from a Mac — regression: `stat -c %s` and `numfmt`, which are GNU-only, in a path whose whole point is being driven from a workstation that is not Linux
- [ ] the image store is writable on the machine that owns it — regression: `$WK_STORE` resolving to `/var/lib/wk` on a macOS host — the podman VM's path, which the Mac cannot create — in a command that is never forwarded into that VM
- [ ] a second `wk sysimage build` refuses while one runs — regression: `pgrep -f <pattern>` matching the ssh that carries the pattern, so an idle build host reports a build in progress
- [ ] `wk gc` keeps the newest build per profile on a build host — regression: keeping the newest two overall, which two PinePhone builds in a row turned into "the Librem 5's artifacts are old" — deleting the only un-imported copy
- [ ] `cmd/gc` honours a pre-set `$WK_ROOT` — regression: deriving it from `$0` under `bash -s`, where `$0` is "bash": the container half then sourced /var/home/lib/common.sh and died
- [ ] `cmd/gc` sources tree files optionally — regression: the container half being this file piped into a VM whose copy of the *rest* of the tree is only as new as the last `wk sync --target container`
- [ ] a lock outlives the command that took it — regression: a flock inherited by the `conmon` podman leaves behind, holding a workspace's lock for as long as the container exists
- [ ] two machines sharing one ssh destination get separate lane state — regression: keying the state file by host alone. `mbp` and `benchvm` are different machines on one address, so the rehearsal and the real run wrote the same file — and immediately after a completed benchvm lane, an `mbp` lane would read `done_through=collect` and skip build, stage, arm and run, reporting a finished lane for a benchmark volume it had never touched
- [ ] the result is collected from where it was written — regression: collecting after leaving bench mode unconditionally. On the volume that is the point (it proves the result survived the reboot); in a guest the result lives *inside the guest*, and leaving the role stops it — so host mode was asked to read a benchmark volume this Mac does not have, and said so. Guest collects before back, volume after
- [ ] a phase-order change updates the order list too — regression: the high-water mark being compared against a hardcoded sequence. Twice: reordering stage for the guest silently marked it done, and then reordering collect did the same
- [ ] never edit a shell script while it is executing — regression: bash reads scripts incrementally, so an edit that shifts byte offsets makes the running shell resume mid-token. Produced `line 859: eport: command not found` from the middle of the word `report`, on a line that was `}` — an error that looks like corruption and is really a live edit
- [ ] `wk quiesce on` returns when driven over ssh — regression: `caffeinate` backgrounded with the session's stdout/stderr still attached. ssh waits for the streams, not for the shell, so the command hung for ever — on the only path where it is ever run unattended. Detach all three descriptors
- [ ] `./setup --stage quiesce` succeeds on macOS — regression: an unguarded Linux block masking `getty@tty2`/`autovt@tty2`, which fails with `sudo: systemctl: command not found` after the helper and its sudoers rule have installed correctly. The stage reported failure for a machine it had finished provisioning
- [ ] the update-check state is read from the setting, not `softwareupdate --schedule` — regression: that command reporting "Automatic checking for updates is turned on" while `AutomaticCheckEnabled = 0` sits in the plist it describes (macOS 26, verified on hardware). Twice written off as a virtualisation quirk before being reproduced on the real install; it is the reader, the same way `systemsetup -getremotelogin` needs admin and exits 0 while refusing to answer
- [ ] a preflight check that cannot pass degrades to unknown, not failure — regression: the update check failing on every machine that cannot read the setting as root. It was *the* failing check, so it was the one people `--force` past — and `--force` is all-or-nothing, so believing it disabled every other check with it. A check that cannot pass on a correct machine trains people to ignore the whole preflight
- [ ] the per-user Setup Assistant stays suppressed across reboots — regression: writing the `com.apple.SetupAssistant` keys once, in a session that is then replaced. Auto-login creates a fresh session on the next boot and the pane returns — caught only because `screen_blocker` was there to notice
- [ ] the bench install is reached at its own address, not the host's — regression: `Host tolken-bench` carrying `HostName tolken`, which MagicDNS resolves to the *host* install's tailnet address. The bench install is a different OS with no tailscale and no tailnet identity, so that name reaches host mode or nothing. Cost most of an evening: ssh, authorized_keys, Remote Login and the network were all working the whole time and every probe was aimed at the wrong machine. Found by scanning the LAN for a host answering as the bench marker. The stanza's own comment had predicted this before the volume existed
- [ ] a fresh macOS install has no `/usr/bin/python3` — regression: provisioning that writes `/etc/kcpassword` with a Python heredoc. Command Line Tools are not present on a new install, so the writer failed silently, `autoLoginUser` was set without a password blob, and the machine landed at a login window every boot. The same absence is why pyobjc was missing
- [ ] a fresh macOS install has no network credentials — regression: assuming ssh reachability means the machine is configured. On a Wi-Fi-only Mac a new install joins nothing, so Remote Login can be genuinely enabled on a machine nothing can route to — indistinguishable from sshd being off unless you check for an address
- [ ] the per-user Setup Assistant is a separate pane from the system one — regression: `/var/db/.AppleSetupDone` suppressing only the system-level assistant. A newly created account still gets Apple ID / analytics / Screen Time on its first login, and it owns the front window — which is exactly the modal-pane condition that silently times out a benchmark
- [ ] CLT can be installed headlessly, but only after a trigger file — regression: `softwareupdate --list` not offering Command Line Tools at all until `/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress` exists. Without it the only documented route is `xcode-select --install`, a GUI prompt no daemon can answer
- [ ] a `--installpackage` LaunchDaemon runs on the boot it was installed on — regression: those packages being laid down during the boot-install phase of the first boot (install.log shows `.com.apple.templatemigration.boot-install/`), which is *after* launchd has scanned `/Library/LaunchDaemons`. A `RunAtLoad` daemon dropped then does not run until the next boot — and with Setup Assistant suppressed and no account yet created, nothing causes a next boot. The volume booted, ran for minutes (wifi.log, asl) and never opened the daemon's StandardOutPath. Fixed with a package `postinstall` that bootstraps the job into the running system
- [ ] a failsafe does not live inside the thing it is protecting against — regression: the lockout guard being written *in* the first-boot script: when the script never ran, the guard never ran either, and `.AppleSetupDone` plus no account left a login window with nothing to click and no ssh. A failsafe downstream of the failure is not a failsafe
- [ ] `startosinstall --installpackage` accepts an unsigned productbuild package — regression: (answered, not a bug) — assumed to need a Developer ID Installer signature. It does not: the receipt was written and every payload file landed from a package reporting `no signature`. Worth knowing before anyone builds signing into this path
- [ ] the benchmark preflight notices a modal pane owning the screen — regression: nothing looking at the screen at all. The console check asks "is someone logged in", not "is the screen usable" — so an unanswered Setup Assistant sailed through, MiniBrowser launched but never became frontmost, and Speedometer was throttled in a background window until `run-benchmark` timed out at exit 124. No error, no crash, no progress
- [ ] `--force` cannot hide a *fatal* preflight failure behind a benign one — regression: `--force` being all-or-nothing. It was added for a guest quirk that genuinely cannot pass (`softwareupdate --schedule off` does not stick in a guest — unrelated to Setup Assistant, confirmed by dismissing it and re-reading), and it then forced past the modal-pane failure too. The lane now asserts the fatal condition itself, before forcing the benign one
- [ ] `screen_blocker` asks *what is frontmost*, not *what is running* — regression: matching process command lines for `<app>.app`: `softwareupdated` and `suhelperd` live inside `Software Update.app/Contents/Resources/` and run on every healthy Mac, so the check failed on a machine whose screen was free. A check that cannot pass is worse than no check — the first thing anyone does with it is force past it. `lsappinfo front` asks the window server, needs no assistive access (System Events answers `-25211` on a fresh install), and sees GUI apps only. Verified both directions: empty on a free screen, and firing when a listed app is frontmost
- [ ] one definition of "is the screen free", not one per caller — regression: the check living in cmd/bench with three apps while the lane that decided whether to `--force` past it kept its own list of one. `Setup Assistant` was caught; `Software Update`, which came to the front the moment Setup Assistant was dismissed, was forced straight through. Same shape as `b_probeable` duplicating `_tart_bin` — a probe that reimplements a resolver drifts from it
- [ ] only the machine being measured is running during a measurement — regression: the lane starting the build guest for the build and stage and never stopping it, so a second macOS VM competed for the same CPUs throughout the run. Noticed by a person looking at the screen and counting two windows; the code had a comment claiming the opposite
- [ ] a lane that launches a detached build waits for *that* build — regression: polling `wk status` immediately after `wk build --detach`, which answers about the *previous* build until the new one registers. The first iteration read a two-day-old `ok` and the lane declared "build ok" in seconds — then armed the bench machine and staged, while the real build was still compiling. It would have published a Speedometer number for a tree nobody had just built, labelled fresh. The tell was the elapsed time: `8m1s`, the exact figure recorded for the build. Fix: snapshot the report before launching and believe a terminal exit code only once the report has changed
- [ ] a trailing `[ -n "$x" ] && …` in a function under `set -e` — regression: the function returning 1 when the test is false, killing an unguarded caller. Harmless mid-function (verified: `set -e` does not fire there), fatal as the last statement — the exit status becomes the function's. A pattern worth grepping for wherever optional args are appended to an array
- [ ] `wk boot benchvm` actually *starts* the guest — regression: `guest` matching neither the one-shot nor the medium branch in `cmd_arm`, so `b_arm` was never called and the unconditional `b_reboot` at the end ran instead — which for a guest is stop. It reported `rc=0` having done the exact opposite of arming
- [ ] `wk boot <guest-machine>` works from a *stopped* guest — regression: the `unreachable` refusal in `cmd_arm` ("arming is an ssh command"), true of every model but `guest`, where arming *is* starting it. A stopped guest is the state the rehearsal exists to be armed from, and it was the one state that refused
- [ ] a `guest` machine needs no `wk sysimage build` — regression: `cmd_arm` sending it down the store path, dying with `no system built for benchvm yet -- 'wk sysimage build perf-macos-benchvm'` and advising a command that cannot exist for a machine whose system *is* the guest
- [ ] a driver declares the libraries it calls — regression: `targets/vm.sh` calling `envelope_mem_mb` while relying on the caller to have sourced `lib/resources.sh`. Making the call lazy moved the failure from source time to call time rather than removing it: `wk boot benchvm` died `rc=127` with the guest half-started. `targets/local.sh` already had the guarded-source idiom; vm.sh did not
- [ ] a guest boots when driven over ssh, not only from a login shell — regression: `tart` resolving `softnet` through `PATH` while wk checks it by absolute path (`$WK_SOFTNET_BIN`) — so the guard passed and the boot died with `InitializationFailed(why: "softnet not found in PATH")`. Invisible interactively, because a login shell has `/usr/local/bin` and a non-interactive ssh has only `/usr/bin:/bin:/usr/sbin:/sbin`. The guest therefore booted by hand and never from another machine, which is every fleet verb there is
- [ ] `wk boot benchvm --status` reports a stopped guest as stopped, not absent — regression: the vm target speaking two namespaces and a caller mixing them: `_vm()` maps a workspace name to the tart VM backing it by prefixing `wk-`, and while `t_start`/`t_stop`/`_ip` map it themselves, `_vm_state` takes the mapped name. Three calls in `boot/mac-guest.sh` passed the unmapped one, so every probe answered `absent` about a guest sitting there stopped — `--status` lied and `b_arm` was fatal, meaning the driver could never arm and the whole guest rehearsal was dead. Hidden by the workspace being called `wk-bench`, which makes the correct tart name the double-prefixed `wk-wk-bench` and the wrong one entirely plausible. Also hidden by `wk vm ls` being *right* about the same guest at the same moment — two commands disagreeing about one fact, which is the signal that was there to be read
- [ ] a `command -v <tool>` probe over non-interactive ssh — regression: a PATH artifact read as a fact about the machine. Non-interactive ssh to a Mac gets `/usr/bin:/bin:/usr/sbin:/sbin`, so a Homebrew or `~/.local/bin` tool is "missing" — `tart` was reported absent on a machine running tart 2.35.0 with three VMs on it. Probe with an absolute path, or `zsh -l -c`, and never conclude "not installed" from one bare `command -v`
- [ ] `wk ls` and `wk status` on a macOS host name every workspace — regression: `wk` sourcing `lib/target.sh` without `lib/store.sh`, so `target_all`'s `wk_state_dir` was undefined: on Linux `wk` execs cmd/ls (which sources it) and on macOS the walk runs inside `wk` itself. The listing came back truncated and confident, hiding the two macOS guests the benchmark lane needs, behind one stderr line nobody reads. Third sighting of this one helper vanishing (cmd/gc lib/image.sh before it) — now in `lib/common.sh`, which every store.sh user already sources
- [ ] `wk bench mac-volume` runs on the Mac — regression: the macOS host forwarding it into the podman VM, where it asked a Linux guest about `diskutil` and died on `podman is required` — the same failure `bench stage`/`bench staged` are already exempted from
- [ ] the lane's preflight refuses when the benchmark volume is absent — regression: matching a list of ways it could be missing instead of the one way it is present: `benchmark_volume=WK Bench (not attached)` was not in the list, so the preflight reported ready and the lane would have failed at the stage. Matching `(attached at …)` instead also fails closed on a new spelling — and needs `^[[:space:]]*`, because the driver indents those lines and a `^`-anchored version blocked the lane even *with* the volume attached
- [ ] `--dry-run` is the real path with mutations suppressed, and not a second path — regression: a dry run that models what its own earlier steps *would* have done. `wk bench mac-volume --all --dry-run` was made to walk its whole chain by simulating the volume `--create` had not really made — so the dry run reasoned about a fictional disk, and could have passed while the real path failed. That is exactly the evidence it exists to provide, inverted. Backed out one `run` gate on mutating commands, every precondition checked for real, and an unmet one refuses identically in both modes
- [ ] `--dry-run` on a fresh lane prints a plan — regression: two ways it did not: `state_get` under `set -o pipefail` failing on a state file that does not exist yet — the normal starting state — so `set -e` killed the run at the first read with no error and exit 0; and the dry run *writing* `done_through` as it walked, so the next real run skipped build and stage and looked for products nobody had staged
- [ ] a check in `cmd/selftest` asks about text with a here-string, never `printf … \ — regression: grep -q`
- [ ] a `wk sysimage write` onto a *reused* disk leaves a mountable filesystem — regression: bmaptool writing only the mapped blocks, so every hole keeps the previous system's bytes -- and a hole is not don't-care: a free FAT directory slot and a free FAT entry are *defined* as zeros. Writing the rpi3's image onto a card that had held a PinePhone system left 100 MB of the 130 MB boot partition unwritten, the root directory region among it; it mounted with garbage entries beside the real files, `ls` gave `Input/output error`, and fsck found files whose start clusters were past the end of the partition. Every block bmaptool *did* write was correct and checksummed against the map, which is exactly why nothing caught it -- the map's checksums cover what was written and never what was not, and the bmap path has no read-back check by deliberate design (`disk_verify_dd` is dd-only). `refresh_fast_path`'s note had reasoned the opposite: safe "because its holes are filesystem free space that was never written", which holds for a blank card and fails for a reused one. `disk_write_bmap` now zeros the image's extent first -- the image's extent only, since zeroing the other 56 GB of a 64 GB card costs more than the write. The image itself was clean throughout (`93 files, 7656/33241 clusters` before and after), so the corruption was purely the write path's
- [ ] `wk sysimage build --detach` leaves `yocto.status` saying what happened — regression: only the *waiting* parent writing the terminal state, so a detached build's status reads `state=running` for ever -- after the artifacts are written, after the process is gone. `wk ls` then reports an idle workspace as "running" indefinitely, and the only way to tell a live build from a finished one is `podman exec`-ing in to check the pid.
- [ ] `wk bench compare` can install scipy in the workspace that built the thing being compared — regression: `pip3 install --user` alone, which Ubuntu 24.04 refuses outright under PEP 668 (`externally-managed-environment`). The SDK image is not externally managed so this passed everywhere it was tried; a yocto build workspace is plain Ubuntu 24.04 (`container/yocto/Containerfile`), so the one workspace holding the cross-built WebKit an on-board run measures was the one that could not compare its results. `--break-system-packages` *with* `--user` writes `~/.local/lib` and touches no distro package, whatever the flag is called, and the workspace is disposable by construction
- [ ] an on-board run reaches `wk bench compare` at all — regression: `wk pi bench` printing run-benchmark's JSON to stdout and stopping there, so the number lives in terminal scrollback and never enters the store `wk bench compare` reads. Two on-board runs could not be compared to each other by any means the tool offered -- which is most of why anyone takes a second one
- [ ] `wk pi bench --ab` compares only rounds where both arms finished — regression: including a survivor whose partner crashed: that puts an extra sample on one arm, and since a crash is usually a property of the build that crashed (OOM under a heavier binary), the surviving arm is precisely the one that would bias the answer. Dropped from the comparison, not deleted from the store -- the run that finished is still evidence about why its partner did not
