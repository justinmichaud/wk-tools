# wk: design

`wk` gives a WebKit engineer, or an AI agent working for them, a named
workspace on a server, and builds, runs and tests WebKit in it. The person
works on a server through Zed's remote projects; every server is cattle,
reinstalled from nothing by one command on the controller.

This file is the design and the plan. Where code disagrees with it, one of the
two is a bug.

## Decisions

**Fleet**

- **Servers** (`moose`, Linux; `tolken`, macOS) hold workspaces, build, run,
  test and benchmark WebKit, and drive the bench boards. A server is never
  shared: one person, their agents, nobody else.
- The **controller** (`alce`) is the one authority. It holds every
  credential, the injector, the fleet's list of workspaces, and the one
  checkout of `wk` that provisions. Everything that installs, provisions or
  checks a server is typed there by a person; nothing runs on a schedule.
- The **workstation** (`ante`) runs Zed. `wk` on it answers `wk ls` and opens a
  workspace in Zed (`wk zed <ws>`), both by asking the controller. It is the
  one machine with personal data: the controller alone may ssh into it, to set
  it up. The person installs its Debian and gives the conf's user sudo
  without a password; `wk machine setup` does the rest.
- Every connection is Tailscale: ssh is Tailscale SSH, authorised by the
  tailnet's policy, and no machine holds an `authorized_keys` entry or an ssh
  key for another. The person and the controller may ssh into every machine,
  and servers into each other; nothing but the person logs in to the
  controller.
- A server reaches the controller only through the controller's API and the
  injector, two ports the tailnet's policy opens to servers. The API knows
  which server is asking from the tailnet itself (`tailscale whois`).
- Bench boards are WiFi only (pass 2).

**Trust**

- Agents are not malicious and not sandboxed. An agent runs as the person's
  own login, with every tool and file the person has.
- What keeps that safe is what a server holds: nothing a leak of which costs
  more than a fork's branches. The interesting credentials (the claude.ai
  login, the tailnet's keys, the BMCs' logins) never leave the controller.
- `github-token` is on every server because `git-webkit` needs it, and a
  classic token with `public_repo` can itself add a deploy key to a fork:
  rotation bounds the ssh keys, not that token.
- A server's push keys are rotated every time an agent session starts there,
  so a key a session saw stops working at the next one.
- A human session is an ordinary login. `git push`, `git-webkit` and `gh` work
  unmodified, with no wrapper, prompt or approval, between agent sessions as
  in them.
- Claude runs with `--dangerously-skip-permissions` and Remote Control.
- A server is fresh-installed often, which is how any drift, an agent's
  included, is removed. `wk machine check` proves the fresh server builds and
  runs WebKit before anyone needs it.

**A way through**

- There is always a manual way through when something is broken. A refusal
  that exists because of a rule, or because a reading could not be taken, is
  a barrier: `--force` crosses it, and the command says so again at exit.
- A refusal the command line itself caused (an unknown name, a bad argument)
  is not a barrier: there is nothing to cross.
- `--force` is the only override. No command has another.

**Off the shelf**

- Provisioning and checks: Ansible (Ubuntu's `ansible` package on the
  controller). Beside `ansible.builtin` the playbooks use `containers.podman`
  and `community.general`, which that package bundles.
- Installing a Linux server: Ubuntu's autoinstall through the BMC, with
  `xorriso`, `qemu-nbd` and `websocat` on the controller.
- Installing a Mac server: a person reinstalls macOS and joins the tailnet;
  everything after that is the controller's.
- Tasks: `pueue`, one daemon per server.
- Secrets: files on the controller, mode 0600, read by Ansible's `file`
  lookup. There is no secrets daemon.
- Network, identity and ssh: Tailscale, with Tailscale SSH on every machine
  (on a Mac, the open-source `tailscaled`, the one macOS variant that serves
  ssh).
- Linux build environment: the WebKit container SDK image under rootless
  podman. Mac: Xcode, and WebKit's own scripts.

## Vocabulary

| Term | Meaning |
| --- | --- |
| workstation | Where the person sits: Zed and ssh. Holds no workspace and no credential. |
| controller | The one machine that holds the credentials and the injector, and provisions, installs, rotates and checks servers. |
| server | A machine that holds workspaces: `linux` or `mac`. Cattle. |
| workspace | A named, disposable checkout for one task, on one server. |
| driver | How a workspace's commands run: `container` (Linux) or `plain` (Mac). |
| mirror | A server's one bare WebKit repository. Every workspace clones from it. |
| task | Anything that outlives its terminal. A pueue task. |
| build preset | One way to build WebKit (`jsc-release`, `mac-release`, ...). |
| agent session | Claude Code or pi running in a workspace. |

## Shape

```
workstation           server (linux | mac)                      controller (alce)
-----------           --------------------                      -----------------
Zed  --ssh-->         wk <cmd> <ws> ...                         secrets/   every credential, 0600
wk ls, wk zed           |- workspace: ~/wk/ws/<name>/src        injector   the one claude.ai login
                        |- driver:    container | plain          wk machine install | setup | check
                        |- tasks:     pueue                     wk key set | ls | check | rotate
                        |- secrets/:  rotated push keys, env.sh
                        '- mirror:    ~/wk/git/WebKit.git
```

A command that works on a workspace is typed on the server that holds it, in
Zed's terminal or over ssh. There is no hand-over and no command asks every
server: what the fleet as a whole knows, the controller holds.

The controller's API (`wk serve`, one systemd user unit, standard library
HTTP on the tailnet address):

| Request | Answer |
| --- | --- |
| `PUT /ws/<name>` | reserves the name for the asking server; 409 naming its holder when another holds it |
| `DELETE /ws/<name>` | releases it, when the asking server holds it |
| `GET /ws` | every name and its server |
| `POST /rotate` | rotates the asking server's push keys and returns them |

The caller is the tailnet node `tailscale whois` names for the request's
address; a node with no conf is refused.

## Rules every command obeys

These are enforced once, in the dispatcher, from a table each command declares.

- A command is data: name, the kinds of machine it runs on, positionals,
  options, subverbs, `readonly` or `destructive`, plus one `main(args, inv)`.
- A command is one module in `lib/wk/cmd/`, found by listing the directory:
  `build.py` is `wk build`, `key_set.py` is the verb `wk key set`. No file
  lists the commands.
- A command typed on a kind of machine it does not run on is refused, naming
  where it runs.
- `inv` is the one object a command reaches machines, the clock, the fleet,
  drivers, tasks and secrets through. A test builds it of fakes. A machine's
  wk home is asked of it and computed nowhere else.
- `--dry-run`: every state change goes through one function, `act`, which
  under dry-run prints the command and runs nothing.
- Only a destructive action prompts. It asks once, defaults to No, declines
  without a terminal; `--yes` answers it.
- A refusal says why and names the remedy. A barrier also names `--force`.
- Exit codes: 0 ok, 1 failed, 2 busy, 3 stalled, 4 needs a person, 75
  refused for now.
- Anything undeclared is refused with the usage line.

## Seams

Every effect goes through one of these, so a command runs unchanged against a
fake in a unit test.

| Seam | Interface | Implementations |
| --- | --- | --- |
| `Machine` | run, act, read, write, remove, exists, listdir, copy, spawn | `Local`, `Ssh`, `Fake` |
| `Clock` | now, sleep, wait_until | real, fake |
| `Driver` | create, destroy, state, exec argv, enter argv | `Container`, `Plain`, fake |
| `Tasks` | add, status, log, kill, wait, follow, quiet | pueue, fake |
| `Secrets` | get, put, list | files under the controller's `secrets/`, fake |

A task's label is `<ws>:<what>:<preset>`. A workspace's tasks are those whose
label's first `:`-field is its name.

## State

- **Recomputed on every read:** a workspace's state, reachability, health,
  what is running.
- **Stored because it cannot be recomputed:** credentials (the controller's
  files), the fleet's workspace names (the controller's `ws/`), task history
  and logs (pueue), a workspace's files.
- **Artifacts:** the mirror, compiler caches, the SDK image, pinned downloads.
- The controller's `ws/` is the one record `wk` keeps: one file per name,
  holding its server, made with `O_EXCL` so two reservations cannot both win.

Layout under a machine's wk home, `~/wk` (or `$WK_HOME`):

| Path | Where | Holds |
| --- | --- | --- |
| `tools/` | all | wk itself; on a server, exactly the controller's commit |
| `bin/wk` | all | a link to `tools/wk`, on the login shell's `PATH` |
| `secrets/` | controller | every credential, one file per name (`bmc/<machine>/user` is a path), mode 0600 in 0700 directories |
| `injector/` | controller | the injector, its CA key and the real login |
| `ws/<name>` | controller | the fleet's workspace names: the file holds the server's name |
| `git/WebKit.git` | server | the mirror; `gc.auto 0`, since workspaces borrow its objects |
| `ws/<name>/src` | server | the checkout: `git clone --shared` from the mirror |
| `ws/<name>/build` | server | build output, a directory per preset, and the workspace's ccache |
| `ws/<name>/.ready` | server | written last by `wk new`; absent means half-made |
| `cache/` | all | pinned downloads; `wk gc` reclaims an entry nothing pins |
| `pueue/` | server | pueue's config, socket, state and task logs |
| `secrets/` | server | what a server holds: its rotated deploy keys, `ssh_config`, `env.sh` |
| `claude-login/` | server | the placeholder claude.ai login |
| `injector-ca.pem` | server | the injector's CA, which an agent session trusts |

Every mutating command is crash-only: killed at any point and re-run, it
converges. A half-made workspace is wiped, never repaired.

## Workspaces

- `wk new <ws>` on a server reserves the name with the controller (refused,
  naming the holder, when another server holds it; the controller not
  answering is a barrier). Then: clone, wire remotes, create the container if
  the driver has one, write `.ready`. A name this server already holds is
  made again from wherever it got to.
- Remotes: `origin` (WebKit, fetch from the mirror, push refused), `fork`
  (yours, through the rotated deploy key's ssh alias). Git settings carry the
  measured fixes: no tags, commit-graph writing off, `insteadOf` to the mirror
  with the GitHub URL kept for the PR tool.
- `wk sync [<ws>]` fetches the mirror from GitHub, then `origin` in one
  workspace or in every one. Only remote-tracking branches move.
- Drivers:
  - `container`: Linux. Rootless podman and the SDK image, one idle container
    per workspace; a command is a `podman exec`. Three mounts at the server's
    own paths (the home, the wk home, the mirror read-only), `HOME` the
    login's, hostname the server's: inside reads what the login reads.
  - `plain`: Mac. Commands run in the checkout.
- `wk enter <ws> [-- <command>]` is a login shell in the checkout (inside the
  container on Linux) that has sourced `secrets/env.sh`. Zed's terminal on a
  Mac is already that shell.
- `wk ls` asks the controller for every name and its server; on a server,
  `wk ls` lists that server's own workspaces with their state.
- `wk zed <ws>` on the workstation asks the controller which server holds
  `<ws>` and opens `ssh://<server>/<checkout>` in Zed.
- `wk rm <ws>` refuses under a queued or running task, asks, then removes
  container and directory, and releases the name last. The `.ready` marker
  goes first.
- `wk machine install <server>` releases every name the server held: its
  disk is gone.

## Tasks

- pueue runs from wk's pinned binaries under wk's own config in `pueue/`:
  one daemon per server, kept running by a systemd user unit of a lingering
  user on Linux and a launchd agent on a Mac.
- A task starts from an empty environment: the system's `PATH`
  (`/usr/bin:/bin:/usr/sbin:/sbin`) and the login's `HOME`, `USER`,
  `LOGNAME`, `SHELL`, `TMPDIR`, `LANG` and `XDG_RUNTIME_DIR`. pueue would give
  it the environment `wk` was typed in, where a login's `PATH`, or the
  `SDKROOT` Apple's `/usr/bin/python3` exports, breaks WebKit's scripts.
- `wk build <ws> [<preset>] [--detach]` adds a pueue task in the `build`
  group, whose parallel limit is 1. With no preset it repeats the last.
- `wk status [<ws>]`, `--log`, `--follow`, `--wait` read pueue. Silence is
  computed from the log's age on each read.
- `wk build <ws> --kill` and `wk stop` kill through pueue.
- **Job sizing:** jobs = min(cores, available memory / memory per job), with
  the measured figures (1536 MB JSC-only, 4608 MB other CMake ports, 3072 MB
  Xcode) and a reserve. A reading that cannot be taken is fatal. This is the
  one thing never left to WebKit's scripts.
- `wk run <ws> -- <args>` and `wk test <ws> -- <tests and options>` run
  `Tools/Scripts` in the workspace for the preset built last (`--preset`
  names another); a test run is a task too.

## Credentials

- On the controller, each is a file `~/wk/secrets/<name>`. `wk key set
  <name>` writes one from stdin, checked by its rule. `wk key ls` lists the
  names. `wk key check` runs each one's live probe, which writes nothing.
- The names: `github-user`, `github-token` (classic, `public_repo`: what
  `git-webkit` and `gh` use, and what manages the forks' deploy keys),
  `bugzilla-user`,
  `bugzilla-password`, `litellm-key`, `claude-login`, `tailnet-auth-key`,
  `tailnet-api-key`, `wifi-ssid`, `wifi-psk`, `bmc/<machine>/user` and
  `bmc/<machine>/password`, and `sudo-password` while a system being replaced
  asks for one.
- What a server holds, under `secrets/`: one deploy key per fork with its ssh
  alias, and `env.sh` (`github-token`, the Bugzilla login).
  pi's `litellm-key` sits in `~/.pi/agent/models.json`. Nothing else.
- **Rotation.** `wk key rotate <server>` on the controller makes a new
  ed25519 key for each fork in `wk_forks`, adds its public half to the fork
  as a writable deploy key titled `wk <server> <time>`, deletes that fork's
  older `wk <server> ...` keys, and prints the private halves as JSON
  (rotation revokes, so it is destructive and asks). Two callers: the
  `credentials` role, which writes them to the server; and the API's
  `POST /rotate`, which `wk ai` calls before the agent starts and whose
  answer it writes.
- The injector is a forward proxy on the controller, reachable only on the
  tailnet. For Claude's hosts it terminates TLS with its own CA and replaces
  the placeholder with the real login, which it alone refreshes. Every other
  host is tunnelled untouched. Nobody logs in to Claude on a server.

## Agent sessions

- `wk ai claude|pi <ws>` on a server rotates the server's push keys, then
  starts the agent in the workspace (inside the container on Linux) with
  Remote Control on, named after the workspace, the injector as its proxy and
  its CA trusted. The words after `--` are the agent's; given any, it runs
  without a terminal too (`-- -p <prompt>`).
- An agent's instructions say to build, run and test through `wk`, which is
  what sizes the jobs. Nothing enforces it.
- Claude Code is pinned and fetched once per server; pi is installed from npm.

## Provisioning

All of it runs on the controller, typed by the person.

- `wk machine setup <server>` runs
  `ansible-playbook -i ansible/inventory.py --limit <server> ansible/site.yml`
  and returns its exit code. `--stage <role>` adds `--tags <role>`;
  `--dry-run` adds `--check --diff`. The same line typed by hand does the
  same.
- The inventory is `machines/*.conf`: a host per machine (a workstation too), a group per kind,
  each conf literal as `wk_<key>`, this checkout's commit.
- Roles: `wk`, `pueue`, `mirror`, `sdk` (Linux), `agents`, `credentials`
  (servers), `xcode` (Mac), `controller` (websocat, the injector and the API, `wk serve`),
  `workstation` (Zed by its own per-user installer, the person's Zed settings
  and keymap, an ssh `Host` per machine with its conf's `user`, their git
  settings). Every task but the rotation is idempotent: a second run
  reports changed only for the keys it rotated.
- A server fetches exactly the controller's commit from the repository's
  public URL. Uncommitted changes, and a commit on no remote branch, are
  barriers.
- `wk machine setup` installs nothing with a package manager: a fresh install
  put every package there, and what is absent is named.
- `wk machine check <server>` runs `ansible/check.yml`: on the server, a
  workspace `check-<time>` is made, built with each of the server's check
  presets (`jsc-release` everywhere, `mac-release` on a Mac), `jsc` runs a
  line, one JSC stress test passes, and the workspace is removed. It is the
  end-to-end proof that a fresh server, with today's WebKit and today's
  distribution, works. Its exit code is the playbook's.
- Renewing a server is three commands typed together:
  `wk machine install moose --yes && wk machine setup moose && wk machine check moose`.
- `wk doctor` reports each thing a machine needs as ok, missing or unknown,
  with the command that fixes it. `wk gc` reclaims what loses no work.
- A machine is one `machines/<name>.conf`: `kind` (`workstation`,
  `controller`, `linux`, `mac`) and literal values. Code never names a
  machine. The file's name is the machine's lowercase short hostname, which
  is also its tailnet name.

The controller itself is set up by hand once, from Ubuntu: `sudo apt install
ansible git xorriso qemu-utils`, `sudo tailscale set --accept-routes` (the
BMCs are behind tailnet subnet routes), `websocat` pinned in
`group_vars/all.json`, a clone of this repository at `~/wk/tools`, `~/wk/bin`
on `PATH`. Then `wk machine setup alce` runs the `controller` role there. Its `secrets/` is the one
copy of every credential; it is backed up by the person.

## Installing a Linux server

A server's operating system is installed from nothing, unattended, through
its BMC. The installer is Ubuntu's own (autoinstall: subiquity and
cloud-init), and every other part is a tool that exists: `xorriso` puts the
one file into Ubuntu's image, `qemu-nbd` serves the image, `websocat` carries
it to the BMC, `community.general`'s Redfish modules set the boot, and the
tailnet's API says when the new system has joined. All of them run on the
controller. The BMC is OpenBMC.

- `wk machine install <server>` asks once, then runs
  `ansible-playbook -i ansible/inventory.py --limit <server> ansible/install.yml`
  and returns its exit code. `--dry-run` adds `--check --diff`. `--rehearse`
  adds `--skip-tags wipe`: the image is made, attached, seen from the running
  server as a USB disk and detached, and nothing boots.
- It refuses a machine with no `bmc=`. A server that holds a workspace is a
  barrier. A server that does not answer is installed: that is what a
  machine being reinstalled usually does.
- The image: Ubuntu's pinned live-server image for the conf's `arch`, with
  `/autoinstall.yaml` added and each kernel command line ending
  `autoinstall fsck.mode=skip --- console=<the conf's> console=tty0`.
- The media: OpenBMC's virtual media is a websocket, `wss://<bmc>/vm/0/0`,
  behind which the BMC runs an NBD client. The controller runs `qemu-nbd` on
  the image, on a Unix socket, and `websocat --binary` between the two, again
  whenever the BMC closes the websocket.
- The pace: this BMC loses NBD replies that arrive together. `qemu-nbd`'s
  throttle spaces them (`wk_install_media_bps`, measured: `STATUS.md`).
- The boot: Redfish sets a one-time UEFI boot from `Usb`, then the server is
  restarted: `shutdown --reboot` when it answers ssh, the BMC's reset when it
  does not.
- The tailnet: the old node is kept until the new one exists. The play asks
  the tailnet's API for a node of the server's name made since the install
  began; then it deletes the old node, names the new one, forgets the old host
  key and waits for ssh.
- However the run ends, the bridge and the image's server are stopped, the
  boot override cleared and the image removed: it holds the WiFi passphrase,
  the tailnet auth key and the BMC's login.
- The way through by hand is the BMC's serial console:
  `ssh -p 2200 <BMC login>@<bmc>`.
- Conf keys: `bmc`, `arch` (`arm64`, `amd64`), `os` (`24.04`), `wifi`
  (optional interface), `console` (optional, `ttyAMA0,115200`), `user`.
- The installed system: Ubuntu on LVM over the largest disk; one user with
  sudo without a password and no ssh key; git,
  podman, uidmap, python3, curl, openssl, tar; Tailscale, joined under the
  server's name with `--ssh`; lingering on.

## Installing a Mac server

A person reinstalls macOS; the controller does everything after the tailnet.

1. The person: Erase All Content and Settings (or a recovery reinstall),
   Setup Assistant creating the conf's `user` as an administrator, then one
   line in Terminal, which `wk machine install <mac>` prints on the
   controller: it installs Homebrew's `tailscale` formula, runs
   `sudo tailscaled install-system-daemon`, and joins with a one-off auth key
   the controller made for it (`tailscale up --ssh --auth-key ...`), naming
   the machine after its conf.
2. The controller, once the node answers Tailscale SSH: `wk machine setup
   <mac>` installs Xcode from the `.xip` kept in the controller's `cache/`
   (downloaded once by the person: Apple's download needs an Apple Account),
   accepts its licence, and runs every role as on any server.

The rest of the Mac's provisioning is the same `site.yml` as Linux's.

## Tree

```
wk                      launcher (python3 -I)
DESIGN.md               the design
STATUS.md               what is implemented, tested and verified on a real machine
machines/               one conf per machine
ansible/site.yml        provisioning
ansible/install.yml     a Linux server's operating system, through its BMC (tasks in ansible/install/)
ansible/check.yml       the end-to-end check of a server
ansible/inventory.py    prints `wk machine inventory`
ansible/group_vars/     all.json: the pins and ports the playbooks and the program share
ansible/roles/          wk, pueue, mirror, sdk, agents, credentials, xcode, controller
lib/wk/core/            dispatch, decl, act, inv, machine, clock, conf, fleet, errors, seams, hooks, fakes
lib/wk/cmd/             one module per command: COMMAND table + main
lib/wk/workspace/       drivers, mirror, git wiring, lifecycle
lib/wk/tasks/           pueue client, sizing, status
lib/wk/webkit/          project.json, presets, Tools/Scripts command lines (the only WebKit names)
lib/wk/creds/           the secrets files, credential rules, rotation, the injector
lib/wk/api/             the controller's API: the workspace names and rotation
lib/wk/agent/           session
lib/wk/provision/       the inventory and the ansible-playbook lines, doctor rows
tests/                  unittest, one test per behaviour, against fakes
```

Python, standard library only, 3.9 syntax. YAML only in `ansible/`.

## Testing

- `wk selftest` runs `tests/`: a command is typed into an in-memory world and
  the test asserts the exit code, the line a person acts on, the commands
  recorded in order, and the final state.
- Crash safety is enumerated: each mutating command is killed after every
  effect and re-run, and must reach the uninterrupted final state.
- In unit tests every real machine tool is unreachable; a test that reaches
  one fails. The playbooks' fronts are tested (inventory, the exact
  `ansible-playbook` lines, the refusals).
- A behaviour that needs a real tool or server is a test marked `live`,
  skipped by name when it is absent.
- `wk machine check <server>` is the live end-to-end test of a server.

## Plan

**Pass 1: the controller and both servers, end to end.**

1. Cut: the borrowed kind, the hand-over and its reverse forward, the memory
   guard, the agent wrapper, the shims, the Seatbelt and bwrap paths.
2. Controller: kinds `workstation`/`controller`; OpenBao replaced by files;
   `wk key rotate` and the forced-command key; provisioning, install and
   `wk key` run on the controller; `wk zed`.
3. Mac server: tolken provisioned from the controller, `mac-*` presets built,
   run and tested through `wk`.
4. `wk machine check`, run green on moose and tolken.

5. Centralise: the controller's API (`wk serve`): workspace names and
   rotation; Tailscale SSH everywhere, no ssh keys; `wk new` free of offline
   servers; `wk ls` and `wk zed` from the controller; the Mac's install line.

**Pass 2: the bench lab.** Boards, images, boot selection, the benchmark
pipeline and its statistics, driven from both servers.

What is implemented, tested and verified on a real machine is in `STATUS.md`.
