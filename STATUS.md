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
| `wk machine install`: the refusals, the question, the `ansible-playbook` line, `--rehearse` | yes | yes | no |
| The rules for `tailnet-api-key`, `wifi-ssid`, `wifi-psk`, `bmc/<machine>/user` and `password` | yes | yes, HTTP scripted | no |
| The install playbook (`ansible/install.yml`, `ansible/install/`) | yes | no: its YAML is parsed and its autoinstall template rendered by a real Ansible | no |

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
- TODO: `wk machine install`: nothing of it has run against a BMC, a media host or a server. Each line below is unverified.
- TODO: install: `xorriso -boot_image any replay` on Ubuntu's arm64 live-server image: that the result still boots under UEFI, and that its `/boot/grub/grub.cfg` has the `linux /casper/vmlinuz ---` line the playbook edits.
- TODO: install: subiquity reading `/cdrom/autoinstall.yaml` with `autoinstall` on the kernel command line: no question asked, on a BMC's virtual CD.
- TODO: install: the BMC's Redfish as `community.general.redfish_command` drives it: `VirtualMediaInsert` under the Manager, the `TransferProtocolType` it lists (AMI firmware commonly lists NFS, CIFS and HTTPS and not HTTP), whether remote media must first be enabled, `SetOneTimeBoot` to `Cd` in UEFI mode, `PowerForceRestart`, `PowerOn`, `DisableBootOverride`.
- TODO: install: the BMC fetching a 3.8 GB image from `python3 -m http.server`, which answers no Range request.
- TODO: install: the media host: `systemd-run` of the server as root, `nft --json --handle list chain` and a rule found again by its comment, and `ansible.builtin.copy` of the image over the tailnet.
- TODO: install: WiFi in the installer and on the first boot: `wpasupplicant` from the image's own pool, the conf's interface name under Ubuntu's kernel, and cloud-init waiting for a WiFi network before it installs packages.
- TODO: install: Ubuntu's generic kernel on the server's hardware (the machine now runs a vendor kernel).
- TODO: install: the installer powering the machine off, the BMC reporting `PowerState` `Off`, and the machine booting its disk at the next `PowerOn` with the image ejected.
- TODO: install: cloud-init's `apt.sources` with `signed-by=$KEY_FILE` for Tailscale's repository, and `tailscale up --auth-key=file:` with `--advertise-tags` under the stored key.
- TODO: install: the tailnet API with `tailnet-api-key`: listing devices, deleting the old node, and the new node taking the bare name.
- TODO: install: `StrictHostKeyChecking=accept-new` through Ansible's ssh for the new host key, and Tailscale SSH admitting the conf's user under the tailnet's policy.
- TODO: install: `wk machine setup` on a freshly installed server.
