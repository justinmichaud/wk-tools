# wk: status

What of pass 1 (`DESIGN.md`, Plan) exists, and how far each piece is proven.
"Unit-tested" means `wk selftest` types it into fake machines; no real server,
store or Ansible run is in that. "Live" means it ran on a real machine.

## Pass 1

The fleet the live column is about: alce (the controller, Ubuntu 26.04 x86_64, 1 core, 1 GB), moose (a Linux server,
Ubuntu 24.04 aarch64, 80 cores, podman 4.9.3), tolken (a Mac server, not yet provisioned). There is no workstation
conf. The verifications below were typed on tolken while it was the client of the previous design.

| Piece | Implemented | Unit-tested | Verified live |
| --- | --- | --- | --- |
| Launcher, dispatcher, declared commands, `--yes`, `--force`, exit codes | yes | yes | yes |
| `--dry-run` | yes | yes | no |
| A command typed on a kind of machine it does not run on is refused, naming where it runs | yes | yes | no |
| Machine confs (`kind`: workstation, controller, linux, mac; `user`), the fleet, `wk machine ls`, `wk machine inventory` on the controller | yes | yes | no |
| `wk help`, `wk selftest` | yes | yes | yes |
| The mirror, `wk new`, `wk rm`, `wk ls` on a server, `wk enter -- <command>` | yes | yes, and against a real local git | yes: moose |
| `wk ls` on a workstation or the controller, every server asked | yes | yes | no |
| `wk zed <ws>` on a workstation | yes | yes | no |
| `wk enter` as the person's door: a shell, a command, with and without a terminal | yes | yes | yes: moose (credentials sourced) |
| `wk sync` typed by a person | yes | yes | no (`wk sync` ran as the playbook's first fetch) |
| The `plain` driver | yes | yes | no |
| The `container` driver (rootless podman, the SDK image) | yes | yes: the podman lines | yes: moose |
| The pueue client, `wk status`, `--wait`, `--log` | yes | yes, against pueue's own state fixture | yes: moose |
| `wk stop`, `wk build --kill`, `wk status --follow` | yes | yes | no |
| Job sizing | yes | yes | yes: 78 jobs on moose |
| `wk build`, `wk run`, `wk test` for `jsc-release` | yes | yes | yes: moose |
| The other presets (`jsc-debug`, `gtk-*`, `wpe-release`, the Mac's) | yes | yes | no |
| The credentials as files under the controller's `secrets/`, 0600 in 0700 directories: `wk key set`, `wk key ls` | yes | yes, against real files | no |
| `wk key check` | yes | yes | no |
| Each credential's rule and live probe | yes | yes, HTTP scripted | yes: `github-user`, `github-token`, `tailnet-api-key`, `claude-login`; no for `bugzilla-*`, `litellm-key` |
| `wk key rotate <server>`: a writable deploy key per fork, the server's older ones deleted, the private halves as JSON | yes | yes, HTTP and ssh-keygen scripted | no |
| `wk ai` asking the controller for a rotation through the rotation key, and writing the keys; a failed one a barrier | yes | yes | no |
| The injector: started, its CA made, answering with no login | yes | yes | yes: alce |
| The injector: TLS for Claude under its CA, the placeholder recognised, a refresh asked for on first use | yes | yes, on loopback sockets | yes: alce, from moose |
| The injector: the login replaced, a refreshed login kept | yes | yes, on loopback sockets | yes: alce, a refresh on first use and the login replaced on requests from moose |
| `wk ai claude`: the session starts, with and without a terminal, and reaches the injector | yes | yes | no |
| `wk ai claude`: an answer, Remote Control | yes | yes | no |
| `wk ai pi` | yes | yes | its refusal alone: no server has pi |
| `wk machine setup` on the controller: the inventory (the controller local) and the `ansible-playbook` line under `LC_ALL=C.UTF-8` | yes | yes | no |
| The playbook: `wk`, `pueue`, `mirror`, `sdk`, `agents` (Claude Code) | yes | its syntax, by Ansible | yes: moose |
| The playbook: pi | yes | its syntax | no: neither server's login `PATH` has node >= 22.19 |
| The playbook: `controller` (`bin/wk`, the pinned websocat, the injector) | yes | its syntax | no |
| The playbook: `credentials` (the rotation, the rotation key and its `authorized_keys` line, `env.sh`, `ssh_config`) | yes | its syntax | no |
| The playbook on a Mac (launchd) | yes | its syntax | no |
| `wk doctor`, `wk doctor <ws>`, `wk gc`, each on the machine typed on | yes | yes | no |
| `wk machine install` on the controller: the refusals, the question, the `ansible-playbook` line, `--rehearse`, `--dry-run` | yes | yes | no |
| The rules for `tailnet-api-key`, `wifi-ssid`, `wifi-psk`, `sudo-password`, `bmc/<machine>/user` and `password` | yes | yes, HTTP scripted | no |
| The install playbook: the credentials read, the BMC asked, the image made, attached through the BMC by the pinned websocat, seen from the server, detached | yes | its YAML is parsed and its autoinstall template rendered by a real Ansible | no |
| The install playbook: what is tagged `wipe` (the boot override, the restart, the installer, the tailnet node, the first ssh) | yes | its YAML is parsed | no |

## Verified live

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
- pi is installed on no server. moose's node is 18.19.1 and pi needs 22.19.
- `bugzilla-password` is not on the controller, and its conf has no `litellm_url=`: Bugzilla through `git-webkit` and pi's `models.json` wait for them.

## Not yet verified live

- TODO: `wk key set` typed on alce, each name moved from the store to `secrets/`, and `wk key rotate` against the forks.
- TODO: pi's `models.json`, `gh` (the SDK image has none), a pull request, a Bugzilla login with `bugzilla-password`.
- TODO: the injector's delivered-digest marker leaving a refreshed login alone at the next setup; the hosts Remote Control uses beside the injector's three.
- TODO: a reboot of each machine: the injector on alce, pueued under its unit on moose.
- TODO: `wk machine setup --dry-run`: what Ansible's check mode reports on a bare and on a provisioned server.
- TODO: a Mac server: the playbook there, pueued as a launchd agent (`launchctl bootstrap gui/<uid>` with no one at the console), the Xcode presets and where they leave `jsc`.
- TODO: `wk sync` typed by a person, `wk stop --tasks`, `wk build --kill`, `wk status --follow`, a build that fails, a build killed over its budget.
- TODO: the presets other than `jsc-release`, and layout tests.
- TODO: `wk` typed inside the container, and the pueue client reaching the daemon's socket from there.
- TODO: `wk gc` with something to reclaim, and `wk new` over a half-made workspace, on a real server.
- TODO: install: everything tagged `wipe`. Each line below is unverified.
- TODO: install: `SetOneTimeBoot` to `Usb` in UEFI mode as this OpenBMC takes it from `community.general.redfish_command`, the firmware booting the USB disk's ESP for it once and the disk after, and `DisableBootOverride`. The BMC reports the running server as `Off`.
- TODO: install: `shutdown --reboot +1` itself, with `sudo-password` in the store (it holds none yet); `PowerForceRestart` for a server that does not answer.
- TODO: install: the image under UEFI after `xorriso -boot_image any replay`, and the boot menu on the serial console.
- TODO: install: subiquity reading `/autoinstall.yaml` from a USB disk with `autoinstall` on the kernel command line: no question asked, and its restart at the end asking for no key.
- TODO: install: the bridge for the length of an install: the firmware's and the installer's reads at the measured pace, and the controller awake throughout.
- TODO: install: WiFi: the installer's own system has no `wpasupplicant`, so it installs from the medium (`apt: fallback: offline-install`) and puts `wpasupplicant` on the disk from the image's pool; the conf's interface name under Ubuntu's kernel; cloud-init waiting for the WiFi network on the first boot before it installs packages.
- TODO: install: Ubuntu's generic kernel on the server's hardware (the machine now runs a vendor kernel).
- TODO: install: cloud-init's `apt.sources` with `signed-by=$KEY_FILE` for Tailscale's repository, and `tailscale up --auth-key=file:` under the stored key.
- TODO: install: the tailnet API with `tailnet-api-key`: the new node listed as `<name>-1` with a `created` after the install began, `DELETE /device/<id>` of the old node, `POST /device/<id>/name`.
- TODO: install: `StrictHostKeyChecking=accept-new` through Ansible's ssh for the new host key, and Tailscale SSH admitting the conf's user under the tailnet's policy.
- TODO: install: `wk machine setup` on a freshly installed server.
