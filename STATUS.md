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
| The reverse forward a command declares (`wk ai` to a borrowed box) | yes | yes: the ssh argv and the port | no |
| `wk help`, `wk selftest` | yes | yes | yes |
| The mirror, `wk new`, `wk rm`, `wk ls`, `wk enter -- <command>` | yes | yes, and against a real local git | yes: moose, buildbox4 |
| `wk enter` as a shell, `wk sync` typed by a person | yes | yes | no (`wk sync` ran as the playbook's first fetch) |
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
| `wk key set`, `check`, `ls` against a store with a login | yes | yes | no |
| Each credential's rule and live probe | yes | yes, HTTP scripted | no |
| The injector: started, its CA made, answering with no login | yes | yes | yes: alce |
| The injector: TLS for Claude, the login replaced and refreshed | yes | yes, on loopback sockets | no |
| The credential wrapper's probe (`bwrap` in the container; the minimal environment) | yes | yes | yes: moose, buildbox4, over an empty `secrets/` |
| The Seatbelt wrapper | yes | yes: the argv | no |
| `wk ai claude`, `wk ai pi` | yes | yes | no |
| `wk machine setup`: the inventory and the `ansible-playbook` line | yes | yes | yes |
| The playbook: `wk`, `pueue`, `mirror`, `sdk`, `agents` (Claude Code) | yes | its syntax, by Ansible | yes: moose; all but `sdk` on buildbox4 |
| The playbook: pi | yes | its syntax | no: neither server's login `PATH` has node >= 22.19 |
| The playbook: `openbao`, `injector` | yes | its syntax | yes: alce, with no login to deliver |
| The playbook: `credentials`, `borrowed-claude` | yes | its syntax | yes for the CA, the placeholder login, `env.sh`, `ssh_config` and the two lines, with a store that could not be asked; no for any secret |
| The playbook on a Mac (launchd) | yes | its syntax | no |
| `wk doctor`, `wk doctor <ws>`, `wk gc` | yes | yes | yes |

## Verified live

Typed on tolken. Ansible is the Homebrew `ansible` package (core 2.22.0); `bao` is Homebrew's OpenBao 2.7.3.

- `ansible-playbook --syntax-check ansible/site.yml` passes, and is the `live` test of `tests/test_provision_setup.py`.
- `wk machine setup alce`: OpenBao 2.7.3 from the pinned archive, a systemd user unit of a lingering user, listening on the tailnet address; `seal "static"` and `storage "raft"` as written; `bao operator init -format=json -recovery-shares=1 -recovery-threshold=1` run on the client was accepted, and its output is `~/.config/wk/openbao-init.json`, mode 0600, with one recovery key and a root token; the mount `secret` enabled. `GET /v1/sys/seal-status` answers initialized, unsealed, type static. The injector runs, makes its CA and answers that it holds no login. A second run: `ok=27 changed=0 failed=0`.
- `wk machine setup moose`, from a server with no `~/wk`: 19 minutes, most of it the first fetch of the WebKit mirror (run by `wk sync`, polled) and the pull of the 15 GB SDK image `ghcr.io/igalia/wkdev-sdk:2.55-v5-94a7493` (the registry has it for arm64 and amd64; there is no `latest`). `ansible.builtin.git` checked out the client's commit from the `rewrite` branch. Claude Code 2.1.308 installed under the staging home. A second run: `ok=47 changed=0 failed=0`.
- `wk machine setup buildbox4`, through the gateway as an unprivileged user: 17 minutes, most of it the mirror. pueued is a systemd user unit there too. A second run changes nothing but the checkout when the client's commit moved.
- A setup run with no `bao login` on the client finishes: it prints that nothing of the store's is delivered and names `bao login`.
- `wk doctor`: every row of moose ok but node, pi and pi's `models.json`; alce reachable, OpenBao unsealed, the injector without a login; the client's `store login` missing, as it is.
- On moose: `wk new t1 --on moose` 38 s; `wk ls`; `wk enter t1 -- git status` and `-- git log -1 --oneline` inside the container; `wk doctor t1` reports the credential wrapper ok (the stand-in ran under `bwrap` inside the rootless container); `wk build t1 jsc-release --detach` sized 78 jobs at 1536 MB and `wk status t1 --wait` returned 0 after a 7 min 22 s build; `wk run t1 -- -e 'print(1+1)'` printed 2; `wk test t1 --detach -- stress/array-push.js` ran that test's variants and passed within a minute; `wk rm t1 --yes`; `wk gc` found nothing to reclaim.
- On buildbox4: `wk new b1 --on buildbox4` 56 s; `wk enter b1 -- git log -1 --oneline`; `wk doctor b1` ok; `wk build b1 jsc-release --detach` sized 64 jobs with a 98304 MB budget, and the build's processes ran at nice 19 under the watchdog (no child cgroup can be made there); built in 6 min 42 s; `wk run b1 -- -e 'print(6*7)'` printed 42; `wk rm b1 --yes`; `wk gc`. The box is left provisioned, with no workspace.

What the live runs corrected: the unit directory is made before a unit is written; `--daemonize` does not survive the provisioning session on buildbox4, where a user unit does; the SDK image had no `latest` tag; WebKit's scripts take `WEBKIT_OUTPUTDIR` whole as the product directory, so each preset now builds in its own; a leaf certificate without an authority key identifier is refused by Python 3.15 with OpenSSL 4.

## Needs a person

- pi is installed on no server. moose's node is 18.19.1 and pi needs 22.19. buildbox4 has node 22 in `~/.local/bin`, which is not on its login shell's `PATH`.
- buildbox4's `~/.bash_profile` and `~/.bashrc` each source `~/wk/tools/shell/bashrc`, a line the previous wk-tools wrote: that file does not exist, so every login there prints an error. The line is the owner's to remove.
- The client has no `bao login`, and the store holds nothing: `BAO_ADDR=http://alce:8200 bao login`, then `wk key set` for each credential, then `wk machine setup alce` and `wk key sync`.

## Not yet verified live

- TODO: `wk key set`, `wk key check`, `wk key ls` with a login: `kv get -field=value` (and whether it ends its output with a newline), `kv put ... value=-` from stdin, `kv list -format=json`, and the words and exit code for a path that holds nothing.
- TODO: the credentials role with real secrets: values through `lookup('ansible.builtin.pipe', 'bao kv get ...')`, that none reaches a log, argv or a diff, deploy keys, `env.sh` as `git-webkit` and `gh` read it, pi's `models.json`.
- TODO: `git push` through the deploy-key alias, a pull request, a Bugzilla login with `bugzilla-password`.
- TODO: the injector with a login: the delivered-digest marker keeping a refreshed login, Claude reading the placeholder from `claude-login/`, TLS through the injector's CA, a refresh, and Remote Control through the proxy.
- TODO: `wk ai claude` and `wk ai pi` on moose; whether WebKit's own bubblewrap sandbox runs inside the wrapper; the wrapper hiding a `secrets/` that holds something.
- TODO: Claude on buildbox4: the reverse forward through the gateway's `ProxyJump`, `ExitOnForwardFailure` on a taken port, the session through `http://127.0.0.1:<port>`, Remote Control over it, and `--permission-mode auto`.
- TODO: a reboot of each server: OpenBao unsealed with no person and its listener once Tailscale is up, the injector, pueued under its unit on moose and on buildbox4.
- TODO: `wk machine setup --dry-run`: what Ansible's check mode reports on a bare and on a provisioned server.
- TODO: a Mac server: the playbook there, pueued as a launchd agent (`launchctl bootstrap gui/<uid>` with no one at the console), the Seatbelt profile, the Xcode presets and where they leave `jsc`.
- TODO: `wk sync` typed by a person, `wk stop --tasks`, `wk build --kill`, `wk status --follow`, a build that fails, a build killed over its budget.
- TODO: the presets other than `jsc-release`, and layout tests.
- TODO: a memory ceiling in a child cgroup, on a machine that lets one be made.
- TODO: `wk` typed inside the container, and the pueue client reaching the daemon's socket from there.
- TODO: `wk gc` with something to reclaim, and `wk new` over a half-made workspace, on a real server.
