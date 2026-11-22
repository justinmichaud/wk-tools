# wk: status

What of pass 1 (`DESIGN.md`, Plan) exists, and how far each piece is proven.
"Unit-tested" means `wk selftest` types it into fake machines; no real server,
store or Ansible run is in that. "Live" means it ran on a real machine.

## Pass 1

The fleet the live column is about: tolken (the client, macOS), alce (the credential server, Ubuntu 26.04 x86_64,
1 core, 1 GB), moose (a Linux server, Ubuntu 24.04 aarch64, 80 cores, podman 4.9.3) and buildbox4 (a borrowed box,
Debian x86_64, 128 cores, an unprivileged user behind an ssh gateway). There is no Mac server in it.

| Piece | Implemented | Unit-tested | Verified live |
| --- | --- | --- | --- |
| Launcher, dispatcher, declared commands, `--yes`, `--force`, exit codes | yes | yes | yes |
| `--dry-run` | yes | yes | no |
| Machine confs (`kind`, `user`), the fleet, `wk machine ls`, `wk machine inventory` | yes | yes | yes |
| Hand-over to a server over ssh, `wk ls --json` routing | yes | yes | yes: moose, buildbox4 |
| The reverse forward a command declares (`wk ai` to a borrowed box) | yes | yes: the ssh argv and the port | yes: buildbox4, through the gateway; a taken port is refused |
| `wk help`, `wk selftest` | yes | yes | yes |
| The mirror, `wk new`, `wk rm`, `wk ls`, `wk enter -- <command>` | yes | yes, and against a real local git | yes: moose, buildbox4 |
| `wk enter` as the person's door: a shell, a command, with and without a terminal | yes | yes | yes: moose (credentials sourced), buildbox4 (none to source) |
| `wk sync` typed by a person | yes | yes | no (`wk sync` ran as the playbook's first fetch) |
| The `plain` driver | yes | yes | yes: buildbox4 |
| The `container` driver (rootless podman, the SDK image) | yes | yes: the podman lines | yes: moose |
| The pueue client, `wk status`, `--wait`, `--log` | yes | yes, against pueue's own state fixture | yes: moose, buildbox4 |
| `wk stop`, `wk build --kill`, `wk status --follow` | yes | yes | no |
| Job sizing | yes | yes | yes: 78 jobs on moose, 64 niced on buildbox4 |
| The memory guard: the watchdog | yes | yes | yes: it watched both builds; none went over |
| The memory guard: a child cgroup with a ceiling | yes | yes | no: neither machine lets one be made |
| `wk build`, `wk run`, `wk test` for `jsc-release` | yes | yes | yes: moose, buildbox4 (`wk test` on moose) |
| The other presets (`jsc-debug`, `gtk-*`, `wpe-release`, the Mac's) | yes | yes | no |
| The store's client (the `bao` CLI): the refusal without a login | yes | yes | yes |
| `wk key check`, `wk key ls` against a store with a login | yes | yes | yes |
| `wk key set` | yes | yes | no: the owner typed it, not this verification |
| Each credential's rule and live probe | yes | yes, HTTP scripted | yes: `github-user`, `github-token`, the three deploy keys, `tailnet-api-key`, `claude-login`; no for `bugzilla-*`, `litellm-key` |
| The injector: started, its CA made, answering with no login | yes | yes | yes: alce |
| The injector: TLS for Claude under its CA, the placeholder recognised, a refresh asked for on first use | yes | yes, on loopback sockets | yes: alce, from moose and buildbox4 |
| The injector: the login replaced, a refreshed login kept | yes | yes, on loopback sockets | yes: alce, a refresh on first use and the login replaced on requests from moose and buildbox4 |
| The credential wrapper's probe (`bwrap` in the container; the minimal environment) | yes | yes | yes: moose over real secrets, buildbox4 |
| The Seatbelt wrapper | yes | yes: the argv | no |
| `wk ai claude`: the session starts, with and without a terminal, and reaches the injector | yes | yes | yes: moose, buildbox4 |
| `wk ai claude`: an answer, Remote Control | yes | yes | yes: moose, buildbox4 |
| `wk ai pi` | yes | yes | its refusal alone: no server has pi |
| `wk machine setup`: the inventory and the `ansible-playbook` line | yes | yes | yes |
| The playbook: `wk`, `pueue`, `mirror`, `sdk`, `agents` (Claude Code) | yes | its syntax, by Ansible | yes: moose; all but `sdk` on buildbox4 |
| The playbook: pi | yes | its syntax | no: neither server's login `PATH` has node >= 22.19 |
| The playbook: `openbao`, `injector` | yes | its syntax | yes: alce, with no login to deliver |
| The playbook: `credentials`, `borrowed-claude` | yes | its syntax | yes: moose with the store's secrets (deploy keys, `env.sh`, `ssh_config`), buildbox4; no for pi's `models.json` |
| The playbook on a Mac (launchd) | yes | its syntax | no |
| `wk doctor`, `wk doctor <ws>`, `wk gc` | yes | yes | yes |
| `wk machine install`: the refusals, the question, the `ansible-playbook` line, `--rehearse`, `--dry-run` | yes | yes | yes: `--dry-run` and `--rehearse` on moose |
| The rules for `tailnet-api-key`, `wifi-ssid`, `wifi-psk`, `sudo-password`, `bmc/<machine>/user` and `password` | yes | yes, HTTP scripted | no |
| The install playbook: the store and the BMC asked, the image made, attached through the BMC, seen from the server, detached | yes | its YAML is parsed and its autoinstall template rendered by a real Ansible | yes: moose |
| The install playbook: what is tagged `wipe` (the boot override, the restart, the installer, the tailnet node, the first ssh) | yes | its YAML is parsed | no |

## Verified live

Typed on tolken. Ansible is the Homebrew `ansible` package (core 2.22.0); `bao` is Homebrew's OpenBao 2.7.3.

- `ansible-playbook --syntax-check ansible/site.yml` passes, and is the `live` test of `tests/test_provision_setup.py`.
- `wk machine setup alce`: OpenBao 2.7.3 from the pinned archive, a systemd user unit of a lingering user, listening on the tailnet address; `seal "static"` and `storage "raft"` as written; `bao operator init -format=json -recovery-shares=1 -recovery-threshold=1` run on the client was accepted, and its output is `~/.config/wk/openbao-init.json`, mode 0600, with one recovery key and a root token; the mount `secret` enabled. `GET /v1/sys/seal-status` answers initialized, unsealed, type static. The injector runs, makes its CA and answers that it holds no login. A second run: `ok=27 changed=0 failed=0`.
- `wk machine setup moose`, from a server with no `~/wk`: 19 minutes, most of it the first fetch of the WebKit mirror (run by `wk sync`, polled) and the pull of the 15 GB SDK image `ghcr.io/igalia/wkdev-sdk:2.55-v5-94a7493` (the registry has it for arm64 and amd64; there is no `latest`). `ansible.builtin.git` checked out the client's commit from the `rewrite` branch. Claude Code 2.1.308 installed under the staging home. A second run: `ok=47 changed=0 failed=0`.
- `wk machine setup buildbox4`, through the gateway as an unprivileged user: 17 minutes, most of it the mirror. pueued is a systemd user unit there too. A second run: `ok=34 changed=0 failed=0`.
- A setup run with no `bao login` on the client finishes: it prints that nothing of the store's is delivered and names `bao login`.
- On moose: `wk new t1 --on moose` 38 s; `wk ls`; `wk enter t1 -- git status` and `-- git log -1 --oneline` inside the container; `wk doctor t1` reports the credential wrapper ok (the stand-in ran under `bwrap` inside the rootless container); `wk build t1 jsc-release --detach` sized 78 jobs at 1536 MB and `wk status t1 --wait` returned 0 after a 7 min 22 s build; `wk run t1 -- -e 'print(1+1)'` printed 2; `wk test t1 --detach -- stress/array-push.js` ran that test's variants and passed within a minute; `wk rm t1 --yes`; `wk gc` found nothing to reclaim.
- On buildbox4: `wk new b1 --on buildbox4` 56 s; `wk enter b1 -- git log -1 --oneline`; `wk doctor b1` ok; `wk build b1 jsc-release --detach` sized 64 jobs with a 98304 MB budget, and the build's processes ran at nice 19 under the watchdog (no child cgroup can be made there); built in 6 min 42 s; `wk run b1 -- -e 'print(6*7)'` printed 42; `wk rm b1 --yes`; `wk gc`. The box is left provisioned, with no workspace.

- `wk key ls` lists the store's ten names; `wk key check` exits 1 with `github-user`, `github-token`, the three deploy keys and `tailnet-api-key` ok ok; a `claude-login` whose refresh token has expired is called bad.
- The person's door on moose, in `t2`: `wk enter t2 -- <command>` without a terminal, the same under a pseudo-terminal, and `wk enter t2` as an interactive login shell each run in the checkout with `GITHUB_COM_TOKEN` and `GH_TOKEN` set. On buildbox4, which holds no `env.sh`, `wk enter b2 -- <command>` runs in the checkout with neither.
- A person's push on moose: `wk enter t2 -- git push --dry-run fork HEAD:refs/heads/wk-live-check` answers `* [new branch]`; `GIT_TRACE` shows `ssh git@github-justinmichaud-webkit` and `ssh -G` that alias's own deploy key with `IdentitiesOnly`. From a scratch repository in the container whose remote is the `wk-tools` https URL, a real push of an empty commit to `wk-live-check-2026-11-22` succeeds through `pushInsteadOf`, `git ls-remote` shows it, and it is deleted again.
- `git-webkit`'s own library (`webkitscmpy.remote.GitHub`) finds its credentials in `t2` and reads `pulls` of WebKit/WebKit; `GET /user` with them answers 200. The SDK image has no `gh`.
- The wrapper over real secrets on moose: `wk doctor t2` says `credential wrapper ok`; a stand-in under the session's own `bwrap` line lists `secrets/` empty, carries none of the credential variables, no `GIT_CONFIG_*` and no `SSH_AUTH_SOCK`, and the same `git push --dry-run` fails there with `could not read Username`.
- `wk ai claude t3 -- -p "Reply with exactly: OK"`, with no terminal, on moose: Claude Code 2.1.308 reads the placeholder from `claude-login/`, honours `HTTPS_PROXY`, trusts the injector's CA, the injector refreshes the store's login on first use and replaces it on the request, and Claude prints `OK`. No login is on moose.
- `wk ai claude t3` under a pseudo-terminal starts in bypass-permissions mode with no dialog, prints `/rc connecting…` and then a `https://claude.ai/code/session…` link: Remote Control is connected. It leaves at `/exit`, and no agent process remains on moose.
- `wk ai claude b3 -- -p ...` on buildbox4 prints `OK` through `-R 127.0.0.1:<port>` on the hand-over's ssh, across the gateway's `ProxyJump`; the interactive session starts in auto mode and prints its Remote Control link the same way; no process or listener remains. The same command typed while a session holds the port is refused, exit 4.
- `wk ai pi` is refused on both servers naming node >= 22.19 and `wk machine setup`; `wk ai claude <ws>` with no terminal and no words is refused naming `-- -p <prompt>`.
- `wk doctor`: every row ok but node and pi on both servers, pi's `models.json` on moose.
- After these fixes `wk machine setup` of moose, buildbox4 and alce each report `changed=0` on a second run.

- `wk machine install moose --dry-run`: the store holds every name asked for, the BMC (OpenBMC, firmware 1.06.00) takes the stored login and lists `Usb` among its one-time boot targets. `ok=12 changed=0`.
- `wk machine install moose --rehearse --yes`, twice, 27 s each with Ubuntu's image already fetched: the image is made, `qemu-nbd` and `websocat` started, and moose, running, reports a USB disk `sda` (`Linux File-Stor Gadget`) of the image's exact size, 3825532928 bytes, labelled `Ubuntu-Server 24.04.5 LTS arm64`; then both processes are stopped, the disk leaves moose and `~/.cache/wk/wk-install-moose` is gone. `ok=35 changed=11 failed=0`.
- The image a rehearsal made, kept by a hard link and read back with `xorriso`: `/autoinstall.yaml` is YAML with the keys the template writes (user `jmichaud` with `NOPASSWD` sudo and one ed25519 key, `wlo1`, `shutdown: reboot`), both kernel lines of `/boot/grub/grub.cfg` read `autoinstall fsck.mode=skip --- console=ttyAMA0,115200 console=tty0`, and the El Torito UEFI record is at the block and of the size it has in Ubuntu's image.
- That image attached by the playbook's two command lines and read on moose: `blkid` says `iso9660` with Ubuntu's label on `sda` and `sda1` and `vfat` `ESP` on `sda2`; the first 64 MiB read with `dd iflag=direct` has the sha256 of the same bytes on the client, at 635 kB/s. Three readers at once took 304 MiB of Ubuntu's image from three offsets with matching sha256 and the bridge up, 590 kB/s between them.
- The pace is measured. Unthrottled, the attach died four times in four: the BMC and moose both probe the new disk's partitions, replies arrive together, the BMC's NBD client logs `Connection timed out` 30 s later and the websocket closes with `Error in connecting to VM port`, while `qemu-nbd`'s trace shows every reply sent. Images that are probed with one read at a time (32 MiB of noise, 24 MiB with zeros) survived unthrottled at 1.9 MB/s. Throttled by bytes: 1 MiB/s died once in two, 768 KiB/s survived two of two, 640 KiB/s seven of seven.
- `efibootmgr -v` on moose with the image attached lists no entry for it: this firmware adds none while the system runs. The disk is a USB mass-storage disk (`sd`, not `sr`), which is why the boot device is `Usb`.
- The BMC's serial console: `ssh -p 2200` with the stored login opens a session (the BMC runs `obmc-console-client` for it) and shows nothing while nobody types. On moose `/sys/class/tty/console/active` is `ttyAMA0 tty0` and a getty runs on `ttyAMA0`, the firmware's SPCR console.
- Ansible's `become` on moose: with the password it runs `shutdown --show` as root; with an empty one it is refused `Missing sudo password`, which is how an install fails, before any restart, while the store holds no `sudo-password`.
- moose's tailnet node is named `moose` and tagged; the installed system joins with the stored auth key alone and takes the tags that key carries.

What the live runs corrected: the unit directory is made before a unit is written; `--daemonize` does not survive the provisioning session on buildbox4, where a user unit does; the SDK image had no `latest` tag; WebKit's scripts take `WEBKIT_OUTPUTDIR` whole as the product directory, so each preset now builds in its own; a leaf certificate without an authority key identifier is refused by Python 3.15 with OpenSSL 4; `wk enter <ws> -- <command>` without a terminal sourced no `env.sh`, and the plain driver never did; `wk ai` demanded a terminal even for `-p`; Claude Code stops at a dialog for bypass-permissions mode until `skipDangerousModePermissionPrompt` is in its settings; the injector's refusal hid the token endpoint's reason, and it called a login it could not refresh held.

## Needs a person

- The claude.ai login is the one the previous tree's injector on the client held; its refresh token ends in 26 days, and the previous tree's copy stops working at the injector's next refresh. A login made for this purpose alone replaces it: `wk key set claude-login`, `wk machine setup alce --stage injector`.
- pi is installed on no server. moose's node is 18.19.1 and pi needs 22.19. buildbox4 has node 22 in `~/.local/bin`, which is not on its login shell's `PATH`.
- buildbox4's `~/.bash_profile` and `~/.bashrc` each source `~/wk/tools/shell/bashrc`, a line the previous wk-tools wrote: that file does not exist, so every login there prints an error. The line is the owner's to remove.
- `bugzilla-password` is not in the store, and the client's conf has no `litellm_url=`: Bugzilla through `git-webkit` and pi's `models.json` wait for them.

## Not yet verified live

- TODO: `wk key set` typed in this verification: `kv put ... value=-` from stdin.
- TODO: pi's `models.json`, `gh` (the SDK image has none), a pull request, a Bugzilla login with `bugzilla-password`.
- TODO: the injector's delivered-digest marker leaving a refreshed login alone at the next setup; the hosts Remote Control uses beside the injector's three; auto mode's classifier on a borrowed box doing real work.
- TODO: `wk ai pi`; whether WebKit's own bubblewrap sandbox runs inside the wrapper.
- TODO: a reboot of each server: OpenBao unsealed with no person and its listener once Tailscale is up, the injector, pueued under its unit on moose and on buildbox4.
- TODO: `wk machine setup --dry-run`: what Ansible's check mode reports on a bare and on a provisioned server.
- TODO: a Mac server: the playbook there, pueued as a launchd agent (`launchctl bootstrap gui/<uid>` with no one at the console), the Seatbelt profile, the Xcode presets and where they leave `jsc`.
- TODO: `wk sync` typed by a person, `wk stop --tasks`, `wk build --kill`, `wk status --follow`, a build that fails, a build killed over its budget.
- TODO: the presets other than `jsc-release`, and layout tests.
- TODO: a memory ceiling in a child cgroup, on a machine that lets one be made.
- TODO: `wk` typed inside the container, and the pueue client reaching the daemon's socket from there.
- TODO: `wk gc` with something to reclaim, and `wk new` over a half-made workspace, on a real server.
- TODO: install: everything tagged `wipe`. Each line below is unverified.
- TODO: install: `SetOneTimeBoot` to `Usb` in UEFI mode as this OpenBMC takes it from `community.general.redfish_command`, the firmware booting the USB disk's ESP for it once and the disk after, and `DisableBootOverride`. The BMC reports the running server as `Off`.
- TODO: install: `shutdown --reboot +1` itself, with `sudo-password` in the store (it holds none yet); `PowerForceRestart` for a server that does not answer.
- TODO: install: the image under UEFI after `xorriso -boot_image any replay`, and the boot menu on the serial console.
- TODO: install: subiquity reading `/autoinstall.yaml` from a USB disk with `autoinstall` on the kernel command line: no question asked, and its restart at the end asking for no key.
- TODO: install: the bridge for the length of an install: the firmware's and the installer's reads at the measured pace, and the client awake throughout.
- TODO: install: WiFi: the installer's own system has no `wpasupplicant`, so it installs from the medium (`apt: fallback: offline-install`) and puts `wpasupplicant` on the disk from the image's pool; the conf's interface name under Ubuntu's kernel; cloud-init waiting for the WiFi network on the first boot before it installs packages.
- TODO: install: Ubuntu's generic kernel on the server's hardware (the machine now runs a vendor kernel).
- TODO: install: cloud-init's `apt.sources` with `signed-by=$KEY_FILE` for Tailscale's repository, and `tailscale up --auth-key=file:` under the stored key.
- TODO: install: the tailnet API with `tailnet-api-key`: the new node listed as `<name>-1` with a `created` after the install began, `DELETE /device/<id>` of the old node, `POST /device/<id>/name`.
- TODO: install: `StrictHostKeyChecking=accept-new` through Ansible's ssh for the new host key, and Tailscale SSH admitting the conf's user under the tailnet's policy.
- TODO: install: `wk machine setup` on a freshly installed server.
