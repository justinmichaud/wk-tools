# wk: status

What of pass 1 (`DESIGN.md`, Plan) exists, and how far each piece is proven.
"Unit-tested" means `wk selftest` types it into fake machines; no real server,
store or Ansible run is in that. "Live" means it ran on a real machine.

## Pass 1

The fleet the live column is about: alce (the controller, Ubuntu 26.04 x86_64, 1 core, 1 GB), moose (a Linux server,
Ubuntu 24.04 aarch64, 80 cores, podman 4.9.3, offline since 2026-11-23), tolken (a Mac server, macOS 27.0.1, 10 cores,
32 GB, Xcode), ante (the workstation, Debian 13 x86_64, GNOME, an HP ProDesk 600 G4). The moose rows were typed on tolken while it was the client of the
previous design; the tolken rows on tolken, provisioned from alce.

| Piece | Implemented | Unit-tested | Verified live |
| --- | --- | --- | --- |
| Launcher, dispatcher, declared commands, `--yes`, `--force`, exit codes | yes | yes | yes |
| `--dry-run` | yes | yes | no |
| A command typed on a kind of machine it does not run on is refused, naming where it runs | yes | yes | no |
| Machine confs (`kind`: workstation, controller, linux, mac; `user`), the fleet, `wk machine ls`, `wk machine inventory` on the controller | yes | yes | no |
| `wk help`, `wk selftest` | yes | yes | yes |
| The mirror, `wk new`, `wk rm`, `wk ls` on a server, `wk enter -- <command>` | yes | yes, and against a real local git | yes: moose |
| `wk ls` on a workstation or the controller: the controller's API asked | yes | yes | yes: alce |
| `wk zed <ws>` on a workstation: the controller's API asked, the server's home read | yes | yes | yes: ante, `e2e` on moose |
| `wk machine setup <workstation>`: the `workstation` role (apt with passwordless sudo, Zed by its installer, the person's Zed settings and keymap, an ssh `Host` per machine, their git settings, no idle suspend on mains), then `wk` | yes | the inventory and the line; its syntax | yes: ante, from alce, a second run `changed=0` |
| The API reads `machines/` at each request | yes | yes | yes: ante answered after its conf was added |
| The controller's API, `wk serve`: the systemd user unit `wk-api` on the tailnet address, the asker from `tailscale whois`, a name a file made with `O_EXCL`, 403 for a node with no conf | yes | yes, and its HTTP on a loopback socket | yes: alce, asked from tolken and alce |
| A machine reaches the API at `tailscale ip -4 <controller>`: on the controller its own name resolves to `127.0.1.1` | yes | yes | yes: alce |
| `wk new` reserves the name first (409 names the holder; no answer is a barrier, forced it is made with no reservation) | yes | yes | the reservation: tolken |
| `wk rm` releases the name last; a name held with no workspace is released | yes | yes | the release: tolken |
| `wk machine install <server>` releases the names a server held, a barrier first | yes | yes | no |
| `wk machine install <mac>`: the one Terminal line, a one-off pre-approved tagged auth key from the tailnet's API | yes | yes, HTTP scripted | no |
| The playbook: `xcode` (the controller's `.xip`, `xip --expand`, `/Applications`, select, licence, first launch) | yes | its syntax | no: tolken holds Xcode, and its first launch is not run (Needs a person) |
| `wk doctor`'s `api` row: the API asked from the machine typed on | yes | yes | yes: tolken |
| `wk enter` as the person's door: a shell, a command, with and without a terminal | yes | yes | yes: moose (credentials sourced) |
| `wk sync` typed by a person | yes | yes | no (`wk sync` ran as the playbook's first fetch) |
| The `plain` driver | yes | yes | yes: tolken |
| The `container` driver (rootless podman, the SDK image) | yes | yes: the podman lines | yes: moose |
| The pueue client, `wk status`, `--wait`, `--log` | yes | yes, against pueue's own state fixture | yes: moose, tolken |
| A task under the system's `PATH` alone, whatever the `PATH` `wk` was typed under | yes | yes | yes: tolken |
| `wk stop`, `wk build --kill`, `wk status --follow` | yes | yes | no |
| Job sizing | yes | yes | yes: 78 jobs on moose, 10 at 3072 MB on tolken |
| `wk build`, `wk run`, `wk test` for `jsc-release` | yes | yes | yes: moose, tolken |
| `wk build` for `mac-release` | yes | yes | yes: tolken |
| The other presets (`jsc-debug`, `gtk-*`, `wpe-release`, `mac-debug`) | yes | yes | no |
| The credentials as files under the controller's `secrets/`, 0600 in 0700 directories: `wk key set`, `wk key ls` | yes | yes, against real files | no |
| `wk key check` | yes | yes | no |
| Each credential's rule and live probe | yes | yes, HTTP scripted | yes: `github-user`, `github-token`, `tailnet-api-key`, `claude-login`; no for `bugzilla-*`, `litellm-key` |
| `wk key rotate <server>`: a writable deploy key per fork, the server's older ones deleted, the private halves as JSON; it asks, `--yes` answers | yes | yes, HTTP and ssh-keygen scripted | yes: alce, for tolken, by the `credentials` role |
| `wk ai` asking the controller's API for a rotation (`POST /rotate`) and writing the keys; a failed one a barrier | yes | yes, the rotation itself through the API end to end | yes: tolken, the forks' `wk tolken <time>` keys replaced in 4 s |
| The injector: started, its CA made, answering with no login | yes | yes | yes: alce |
| The injector: TLS for Claude under its CA, the placeholder recognised, a refresh asked for on first use | yes | yes, on loopback sockets | yes: alce, from moose |
| The injector: the login replaced, a refreshed login kept | yes | yes, on loopback sockets | yes: alce, a refresh on first use and the login replaced on requests from moose |
| `wk ai claude`: the session starts, with and without a terminal, and reaches the injector | yes | yes | without a terminal: tolken |
| `wk ai claude`: an answer, Remote Control | yes | yes | an answer: tolken |
| `wk ai pi` | yes | yes | its refusal alone: no server has pi |
| `wk machine setup` on the controller: the inventory (the controller local) and the `ansible-playbook` line under `LC_ALL=C.UTF-8` | yes | yes | yes: alce, for alce and tolken |
| The playbook: `wk`, `pueue`, `mirror`, `sdk`, `agents` (Claude Code) | yes | its syntax, by Ansible | yes: moose; tolken but `sdk` |
| The playbook: pi | yes | its syntax | no: neither server's login `PATH` has node >= 22.19 |
| The playbook: `controller` (`bin/wk`, the pinned websocat, the injector and the API, each restarted when older than its program or unit) | yes | its syntax; no handler, no registered name hiding an inventory variable | yes: alce, a second run `changed=0` |
| The playbook: `credentials` (the rotation, `env.sh`, `ssh_config`) | yes | its syntax | yes: tolken |
| The playbook on a Mac (launchd) | yes | its syntax | yes: tolken |
| `wk machine check <server>`: the refusals, the `ansible-playbook` line of `ansible/check.yml`, `--dry-run` | yes | yes; the playbook's syntax by Ansible | yes: alce, against tolken |
| `wk doctor`, `wk doctor <ws>`, `wk gc`, each on the machine typed on | yes | yes | no |
| `wk machine install` on the controller: the refusals, the question, the `ansible-playbook` line, `--rehearse`, `--dry-run` | yes | yes | no |
| The rules for `tailnet-api-key`, `wifi-ssid`, `wifi-psk`, `sudo-password`, `bmc/<machine>/user` and `password` | yes | yes, HTTP scripted | no |
| The install playbook: the credentials read, the BMC asked, the image made, attached through the BMC by the pinned websocat, seen from the server, detached | yes | its YAML is parsed and its autoinstall template rendered by a real Ansible | no |
| The install playbook: what is tagged `wipe` (the boot override, the restart, the installer, the tailnet node, the first ssh) | yes | its YAML is parsed | yes: moose, 2026-11-28, one run (below) |

## Verified live: moose installed from nothing, and ante

Typed on alce and tolken, 2026-11-28.

- moose had been off since a power loss (OpenBMC's chassis `Off`, the BMC's last reboot cause `POR`, the serial console
  silent); Redfish `ResetType: On` started it into its old system.
- `wk machine install moose --yes` stopped at the one-time boot: this OpenBMC answers HTTP 412 to every `If-Match`,
  the ETag it has just sent included, and `community.general.redfish_command` always sends one. A PATCH of `Boot`
  without `If-Match` answers 204; the play now sets and clears the boot so (0c30cb89).
- The next run set the boot and restarted moose from its own system with `sudo-password`, but the firmware booted the
  NVMe: the bridge had ended 41 s after it attached (websocat rc 0, the BMC's NBD client giving the disk up), before the
  restart. The bridge now serves with `qemu-nbd --persistent` and connects again whenever the BMC closes it (0885de9f).
- By hand, as the play does it: the bridge up, `Boot` `Once`/`Usb`/`UEFI`, `systemctl reboot`: the firmware ran GRUB
  from the USB disk ("Try or Install Ubuntu Server"), subiquity installed without a question, and the installed system
  joined the tailnet as `moose-1` 63 min later. The bridge held the whole install at 640 KiB/s. Then the old node was
  deleted and the new one named `moose` through the tailnet API, the override cleared, the image removed, and alce
  took the new host key: Ubuntu 24.04.5, kernel 6.8.0-139-generic, sudo without a password, podman, git, python3.
- The renewal as one line on alce, `wk machine install moose --yes && wk machine setup moose && wk machine check
  moose`: exit 0 in 1 h 43 min. Install 69 min 48 s (`ok=42 changed=13`): the boot set without If-Match, the bridge
  connected the whole install, the new node joined as `tag:wk-server`, the old node retired and the new one named
  `moose` by the play, the override cleared and the image removed. Once, the firmware stopped in POST after the restart
  from the running system (`Synchronous Exception ... ASSERT [EmulatorDxe]` on the serial console) and a Redfish
  `ForceRestart` by hand booted the USB disk; the other two restarts booted it themselves. Setup 22 min 13 s (`ok=54
  changed=30`), check 11 min 9 s (`ok=7 changed=5`).
- WebKit development on the renewed moose, typed from ante through Tailscale SSH: `wk ls` on ante answered by the
  controller before and after `wk new e2e` (34 s); `wk build e2e jsc-release --detach`, 79 jobs at 1536 MB, `wk status
  e2e --wait` 0 after 7 min 40 s; `wk run e2e -- -e 'print(1+1)'` printed 2; `wk test e2e -- stress/array-push.js`
  passed in 53 s; `wk ai claude e2e -- -p "Reply with exactly: OK"` rotated the push keys through the API and answered
  OK in 12 s through the injector; `wk enter e2e -- git push --dry-run fork HEAD:refs/heads/wk-e2e-moose` answered
  `* [new branch]`; `wk zed e2e` on ante opened the checkout, Zed's remote server 1.30.2 running on moose.
- That push first failed `Host key verification failed`: a fresh server had never seen github.com's host key. The
  credentials role now writes GitHub's published keys (its API's `meta`) to `secrets/known_hosts` for the push aliases,
  and `wk machine check` pushes to the fork as a dry run (e237a2b3, d966c574).
- ante: a person installed Debian 13, gave `jmichaud` sudo without a password, and tagged the node `tag:person`; the
  tailnet's policy lets `tag:wk-controller` (alce) alone ssh into it. `wk machine setup ante` from alce: `ok=20
  changed=11`, then `changed=0`. Zed 1.30.2, `wk ls` answered by the controller.
- The tailnet's policy now has the design's tags, applied through its API from alce and checked by its own tests:
  `tag:wk-controller` (alce), `tag:wk-server` (moose), `tag:person` (ante). A server reaches the controller on 3128 and
  3129 alone; the person and the controller ssh into every server, the controller alone into ante. Measured: moose
  answered by the API, alce into ante and to moose's BMC, tolken refused by ante. The freshly installed moose had joined
  as `tag:wk`, the stored auth key's tag, which reached no port of alce, so `wk machine check moose` built `jsc-release`,
  ran `jsc` and passed the stress test, then its `wk rm` could not release the name. `tailnet-auth-key` is now a
  reusable, pre-approved `tag:wk-server` key minted through the API.
- ante sleeps in s2idle, which draws more than S3: its FADT declares no low-power S0 idle, and across 30 s of s2idle
  `slp_s0_residency_usec` stayed 0 (package C10 for 23 s; XHCI, GBE and NPK powered). S3 is woken by the RTC alone; a
  key or the power button leaves it hung, below Linux (every `pm_test` stage resumes; Debian's kernel has no
  `pm_trace`). The person chose no BIOS update (02.07.00 against HP's 02.30.00 of 2025-01), and s2idle for now. After
  a power-button wake from s2idle the system ran but the monitor stayed off (DP-1 `enabled=disabled`; gnome-shell had
  let its sleep inhibitor time out); a key wake relit it.
- ante's Dell Universal Receiver (413c:301c, port 4 of a VIA VL817 hub) stopped answering after one s2idle that a USB
  wake ended at 3 s: its reset during the resume failed (`-32`, then `-71`), and neither the hub port's `disable`,
  `usbreset` of the hub, nor an S3 brought it back; a replug did. Not reproduced in three tries (30 s to an alarm with
  its wake off and on, and a wake by the mouse itself), so its wake is left as it is.
- ante's power LED kept blinking after a wake. Its DSDT drives no LED: `_PTS` and `_WAK` reach empty `SIOS`/`SIOW` and
  write debug codes, so the blink is the platform's own; the last S3 there was woken at 0.5 s.
- ante froze in GNOME's idle suspend. Measured: the kernel's `pm_test` stages (freezer, devices, platform, processors)
  each resume; real S3 and s2idle each wake on an RTC alarm, through `rtcwake` and through `systemctl suspend`; but from
  S3 neither a key nor the power button wakes it (the journal ends at `Performing sleep operation 'suspend'`), with or
  without wakeup enabled on its USB hubs. The firmware is HP Q07 02.07.00 (2019), which LVFS does not carry. In s2idle a
  key wakes it once the Ethernet controller's wake is off: its PME (GPE 0x6D, the SCI as the wake IRQ) ended s2idle
  within a second every time, so an earlier "woke on a key" had never slept. With it off, s2idle slept 30 s to an
  alarm and 20 s to a key. The role writes systemd's `MemorySleepMode=s2idle` and a udev rule that disables wake on
  every Ethernet controller; GNOME's idle suspend stays on.

## Verified live: the controller and tolken

Typed on alce (Ubuntu's `ansible`, core 2.20) and on tolken, 2026-11-27.

- alce bootstrapped as DESIGN.md says: a clone at `~/wk/tools` on `rewrite`, `~/wk/bin` on `PATH` in `~/.profile`, then
  `~/wk/tools/wk machine setup alce`: about 10 s a run. A second run reports `ok=20 changed=0`.
- `wk machine setup tolken` from a Mac with no `~/wk`: 965 s, most of it the mirror's first fetch (12 GB), `ok=57
  changed=32 failed=0`. pueued runs as the launchd agent `gui/<uid>/wk.pueued`; Claude Code 2.1.308 is installed under
  the staging home. A second run, 3 min, reports changed only for the rotation's tasks.
- The rotation, by the `credentials` role: each fork holds one writable deploy key titled `wk tolken <time>`, and the
  run before's is gone.
- On tolken, typed as `~/wk/bin/wk` (an interactive shell there finds the previous tree's `wk` first): `wk new t1`
  (with moose silent, `--force`) 32 s; `wk build t1 jsc-release --detach`, 10 jobs at 3072 MB, and `wk status t1 --wait`
  0 after 6 min; `wk run t1 -- -e 'print(1+1)'` prints 2; `wk test t1 -- stress/array-push.js` passes its 17 variants in
  73 s; `wk enter t1 -- git status`; `wk enter t1 -- git push --dry-run fork HEAD:refs/heads/wk-live-check-tolken`
  answers `* [new branch]` through `github-justinmichaud-webkit`, whose `ssh -G` names the delivered deploy key with
  `IdentitiesOnly`; `wk build t1 mac-release` 46 min after `jsc-release`, leaving `MiniBrowser.app` and
  `WebKit.framework`; `wk rm t1 --yes` 31 s.
- `wk ai claude t1 --force -- -p "Reply with exactly: OK"` prints `OK` through the injector on alce.
- `wk machine check tolken`, typed on alce, exits 0 after 54 min: `check-20261127120143` made, `jsc-release` then
  `mac-release` built cold, `jsc` printing 2, `stress/array-push.js` passing, the workspace removed; `ok=7 changed=5`.
  An earlier run whose first build failed removed its workspace all the same.

What these runs corrected: a registered result named `wk_store` hid the store's directory from the controller role; the
injector kept running its program of the previous design after a failed run, its restart handler forgotten; a task
took the environment `wk` was typed in, so the previous tree's `make` shim refused an agent's build and Ansible's
`/usr/bin/python3` gave WebKit's Makefile an `SDKROOT` it turned into a refused `--sdk`; on a Mac the scripts read
the products of `WEBKIT_OUTPUTDIR` itself, where Xcode puts them under a directory of the configuration's name; and
`wk key rotate`, which revokes keys, did not ask.

## Verified live: the controller's API

Typed on alce and on tolken, 2026-11-27, at commits 8848b524 and 5112aa16.

- `wk machine setup alce` starts `wk-api.service` (`==> serving the controller's API on 100.76.223.32:3129`) in 16 s;
  a second run `ok=20 changed=0`; a new commit rewrites the unit, which is then restarted (`changed=2`).
- From tolken, `curl http://alce:3129/ws` answers `{"names": {}}` in 0.3 s: the tailnet's policy admits it.
- On tolken: `wk new t2` 21 s, and alce's `~/wk/ws/t2` holds `tolken`; `wk ls` lists it with its state, `GET /ws`
  answers `{"names": {"t2": "tolken"}}`. `wk ai claude t2 -- -p "Reply with exactly: OK"` answers `OK` in 13 s, the
  API's journal shows `POST /rotate` 200 from 100.112.56.33, and each fork's `wk tolken <time>` key went from 16:38:45Z
  to 16:40:23Z, one key each. `wk enter t2 -- git push --dry-run fork HEAD:refs/heads/wk-live-check-tolken` answers
  `* [new branch]` with the new key. `wk rm t2 --yes` 20 s, and alce's `ws/` is empty again. `wk doctor`'s `api` row is
  ok on tolken and moose.
- `wk ls` on alce answers once the client asks `tailscale ip -4 alce`: by its name alce reached `127.0.1.1`, from
  `/etc/hosts`, where nothing listens.
- moose, back online after its outage: `wk machine setup moose` first failed `Host key verification failed`: alce had
  never recorded moose's Tailscale SSH host key, so ssh now takes a node's first key (`StrictHostKeyChecking=accept-new`).
  Then 4 min 45 s, `ok=45 changed=7`. `wk machine check moose` exits 0 in 10 min 41 s, `ok=7 changed=5`; its workspace
  `check-20261127164858` was reserved on alce while it ran and released at its end.
- `wk machine install moose --dry-run`, while the check held its workspace, is refused naming
  `check-20261127164858` and `--force`.

## Verified live: moose and the previous design

Typed on tolken. Ansible is the Homebrew `ansible` package (core 2.22.0); `bao` is Homebrew's OpenBao 2.7.3.

- `ansible-playbook --syntax-check ansible/site.yml` passes, and is the `live` test of `tests/test_provision_setup.py`.
- `wk machine setup alce`: OpenBao 2.7.3 from the pinned archive, a systemd user unit of a lingering user, listening on the tailnet address; `seal "static"` and `storage "raft"` as written; `bao operator init -format=json -recovery-shares=1 -recovery-threshold=1` run on the client was accepted, and its output is `~/.config/wk/openbao-init.json`, mode 0600, with one recovery key and a root token; the mount `secret` enabled. `GET /v1/sys/seal-status` answers initialized, unsealed, type static. The injector runs, makes its CA and answers that it holds no login. A second run: `ok=27 changed=0 failed=0`.
- `wk machine setup moose`, from a server with no `~/wk`: 19 minutes, most of it the first fetch of the WebKit mirror (run by `wk sync`, polled) and the pull of the 15 GB SDK image `ghcr.io/igalia/wkdev-sdk:2.55-v5-94a7493` (the registry has it for arm64 and amd64; there is no `latest`). `ansible.builtin.git` checked out the client's commit from the `rewrite` branch. Claude Code 2.1.308 installed under the staging home. A second run: `ok=47 changed=0 failed=0`.
- A setup run with no `bao login` on the client finishes: it prints that nothing of the store's is delivered and names `bao login`.
- On moose: `wk new t1` 38 s; `wk ls`; `wk enter t1 -- git status` and `-- git log -1 --oneline` inside the container; `wk build t1 jsc-release --detach` sized 78 jobs at 1536 MB and `wk status t1 --wait` returned 0 after a 7 min 22 s build; `wk run t1 -- -e 'print(1+1)'` printed 2; `wk test t1 --detach -- stress/array-push.js` ran that test's variants and passed within a minute; `wk rm t1 --yes`; `wk gc` found nothing to reclaim.

- `wk key ls` lists the store's ten names; `wk key check` exits 1 with `github-user`, `github-token`, the three deploy keys and `tailnet-api-key` ok ok; a `claude-login` whose refresh token has expired is called bad.
- The person's door on moose, in `t2`: `wk enter t2 -- <command>` without a terminal, the same under a pseudo-terminal, and `wk enter t2` as an interactive login shell each run in the checkout with `GITHUB_COM_TOKEN` and `GH_TOKEN` set.
- A person's push on moose: `wk enter t2 -- git push --dry-run fork HEAD:refs/heads/wk-live-check` answers `* [new branch]`; `GIT_TRACE` shows `ssh git@github-justinmichaud-webkit` and `ssh -G` that alias's own deploy key with `IdentitiesOnly`. From a scratch repository in the container whose remote is the `wk-tools` https URL, a real push of an empty commit to `wk-live-check-2026-11-22` succeeds through `pushInsteadOf`, `git ls-remote` shows it, and it is deleted again.
- `git-webkit`'s own library (`webkitscmpy.remote.GitHub`) finds its credentials in `t2` and reads `pulls` of WebKit/WebKit; `GET /user` with them answers 200. The SDK image has no `gh`.
- `wk ai claude t3 -- -p "Reply with exactly: OK"`, with no terminal, on moose: Claude Code 2.1.308 reads the placeholder from `claude-login/`, honours `HTTPS_PROXY`, trusts the injector's CA, the injector refreshes the store's login on first use and replaces it on the request, and Claude prints `OK`. No login is on moose.
- `wk ai claude t3` under a pseudo-terminal starts in bypass-permissions mode with no dialog, prints `/rc connecting…` and then a `https://claude.ai/code/session…` link: Remote Control is connected. It leaves at `/exit`, and no agent process remains on moose.
- `wk ai pi` is refused on both servers naming node >= 22.19 and `wk machine setup`; `wk ai claude <ws>` with no terminal and no words is refused naming `-- -p <prompt>`.
- `wk doctor`: every row ok but node and pi on both servers, pi's `models.json` on moose.
- After these fixes `wk machine setup` of moose and alce each report `changed=0` on a second run.

- `wk machine install moose --dry-run`: the store holds every name asked for, the BMC (OpenBMC, firmware 1.06.00) takes the stored login and lists `Usb` among its one-time boot targets. `ok=12 changed=0`.
- `wk machine install moose --rehearse --yes`, twice, 27 s each with Ubuntu's image already fetched: the image is made, `qemu-nbd` and `websocat` started, and moose, running, reports a USB disk `sda` (`Linux File-Stor Gadget`) of the image's exact size, 3825532928 bytes, labelled `Ubuntu-Server 24.04.5 LTS arm64`; then both processes are stopped, the disk leaves moose and `~/.cache/wk/wk-install-moose` is gone. `ok=35 changed=11 failed=0`.
- The image a rehearsal made, kept by a hard link and read back with `xorriso`: `/autoinstall.yaml` is YAML with the keys the template writes (user `jmichaud` with `NOPASSWD` sudo and one ed25519 key, `wlo1`, `shutdown: reboot`), both kernel lines of `/boot/grub/grub.cfg` read `autoinstall fsck.mode=skip --- console=ttyAMA0,115200 console=tty0`, and the El Torito UEFI record is at the block and of the size it has in Ubuntu's image.
- That image attached by the playbook's two command lines and read on moose: `blkid` says `iso9660` with Ubuntu's label on `sda` and `sda1` and `vfat` `ESP` on `sda2`; the first 64 MiB read with `dd iflag=direct` has the sha256 of the same bytes on the client, at 635 kB/s. Three readers at once took 304 MiB of Ubuntu's image from three offsets with matching sha256 and the bridge up, 590 kB/s between them.
- The pace is measured. Unthrottled, the attach died four times in four: the BMC and moose both probe the new disk's partitions, replies arrive together, the BMC's NBD client logs `Connection timed out` 30 s later and the websocket closes with `Error in connecting to VM port`, while `qemu-nbd`'s trace shows every reply sent. Images that are probed with one read at a time (32 MiB of noise, 24 MiB with zeros) survived unthrottled at 1.9 MB/s. Throttled by bytes: 1 MiB/s died once in two, 768 KiB/s survived two of two, 640 KiB/s seven of seven.
- `efibootmgr -v` on moose with the image attached lists no entry for it: this firmware adds none while the system runs. The disk is a USB mass-storage disk (`sd`, not `sr`), which is why the boot device is `Usb`.
- The BMC's serial console: `ssh -p 2200` with the stored login opens a session (the BMC runs `obmc-console-client` for it) and shows nothing while nobody types. On moose `/sys/class/tty/console/active` is `ttyAMA0 tty0` and a getty runs on `ttyAMA0`, the firmware's SPCR console.
- Ansible's `become` on moose: with the password it runs `shutdown --show` as root; with an empty one it is refused `Missing sudo password`, which is how an install fails, before any restart, while the store holds no `sudo-password`.
- moose's tailnet node is named `moose` and tagged; the installed system joins with the stored auth key alone and takes the tags that key carries.

What the live runs corrected: the unit directory is made before a unit is written; the SDK image had no `latest` tag; WebKit's scripts take `WEBKIT_OUTPUTDIR` whole as the product directory, so each preset now builds in its own; a leaf certificate without an authority key identifier is refused by Python 3.15 with OpenSSL 4; `wk enter <ws> -- <command>` without a terminal sourced no `env.sh`, and the plain driver never did; `wk ai` demanded a terminal even for `-p`; Claude Code stops at a dialog for bypass-permissions mode until `skipDangerousModePermissionPrompt` is in its settings; the injector's refusal hid the token endpoint's reason, and it called a login it could not refresh held.

## Needs a person

- The claude.ai login is the one the previous tree's injector on the client held; its refresh token ends in 26 days, and the previous tree's copy stops working at the injector's next refresh. A login made for this purpose alone replaces it: `wk key set claude-login`, `wk machine setup alce --stage controller`.
- pi is installed on no server. moose's node is 18.19.1, tolken's login shell has none (nvm's is on an interactive
  shell's `PATH` alone), and pi needs 22.19.
- tolken is still `tag:workstation`, which the tailnet's policy lets reach alce (ssh and the API) while this session
  drives the fleet from it; its reinstall makes it a `tag:wk-server` (`wk_server_tags`, its join key's tag), and the
  policy's `tag:workstation` exception goes. rpi5 is `tag:workstation` too.
- alce reaches tolken by an OpenSSH key in tolken's `authorized_keys` (tolken runs the GUI Tailscale app, which serves
  no Tailscale SSH), and tolken still holds `secrets/rotate-key`, alce `~/.ssh/authorized_keys`'s `wk-rotate tolken`
  line: tolken's reinstall, with the open-source `tailscaled`, removes the first two; the line is removed by hand.
- tolken's Xcode has not run its first launch (`xcodebuild -checkFirstLaunchStatus` exits 69), so the `xcode` role
  asks for `sudo xcodebuild -runFirstLaunch`, and the store's `sudo-password` is not tolken's: `wk machine setup tolken`
  fails there until a person runs it, or the store holds tolken's password. Its stages but `xcode` run.
- tolken's interactive shells put the previous tree (`~/Development/wk-tools/bin`, `container/bin`) ahead of
  `~/wk/bin`, from `.zshrc`; its reinstall removes it.
- The forks still hold the previous design's deploy keys (`wk build key (...)`, `devbox-arm64`, `wk shared deploy key`),
  and alce still runs `wk-openbao.service` and holds `deploy-key/*` in `secrets/`.
- `bugzilla-password` is not on the controller, and its conf has no `litellm_url=`: Bugzilla through `git-webkit` and pi's `models.json` wait for them.

## Not yet verified live

- TODO: `wk key set` typed on alce.
- TODO: a task's empty environment under podman on a Linux server (`XDG_RUNTIME_DIR` kept).
- TODO: pi's `models.json`, `gh` (the SDK image has none), a pull request, a Bugzilla login with `bugzilla-password`.
- TODO: the injector's delivered-digest marker leaving a refreshed login alone at the next setup; the hosts Remote Control uses beside the injector's three.
- TODO: a reboot of each machine: the injector on alce, pueued under its unit on moose.
- TODO: `wk machine setup --dry-run`: what Ansible's check mode reports on a bare and on a provisioned server.
- TODO: pueued's launchd agent with no one at tolken's console, and after a reboot.
- TODO: `wk sync` typed by a person, `wk stop --tasks`, `wk build --kill`, `wk status --follow`, a build that fails, a build killed over its budget.
- TODO: the presets other than `jsc-release` and `mac-release`, and layout tests.
- TODO: `wk` typed inside the container, and the pueue client reaching the daemon's socket from there.
- TODO: `wk gc` with something to reclaim, and `wk new` over a half-made workspace, on a real server.
- TODO: install: `PowerForceRestart` for a server that does not answer, and a server whose chassis is off.
