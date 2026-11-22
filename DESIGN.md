# wk: design

`wk` gives a WebKit engineer, or an AI agent working for them, a named
workspace on a server, and builds, runs and tests WebKit in it. One person
types `wk` on one interactive machine; every other machine is a server.

This file is the design and the plan. Where code disagrees with it, one of the
two is a bug.

## Decisions

**Fleet**

- One machine is interactive: the **client**. Every other machine is a
  **server** that `wk` manages from a cold boot. There is no workstation role
  and no peer.
- One machine is the **credential server**: it runs OpenBao and the injector
  and nothing else. It holds no workspace and no checkout of `wk`.
- A server is yours (Linux or macOS) or **borrowed**: someone else's shared
  build box, where you are an ordinary user with no root, podman is not
  yours to use, cgroups are read-only, and other people are root.
- A machine is reached by its tailnet name over ssh, and nothing else. Borrowed
  boxes behind a gateway are the only ssh-config entries.
- There is no podman VM on macOS and no Tart guest. A Mac is a server like any
  other; its workspaces are plain checkouts.
- Bench boards are WiFi only. A LAN device that cannot run Tailscale is the
  job of a separate, stand-alone tailnet bridge, not of `wk`.

**Trust**

- Agents and other users are not malicious. The guardrail is that an agent
  cannot find a credential and cannot publish. Multi-stage attacks (a planted
  git hook, an edited script) are out of scope.
- A human session is an ordinary login. `git push`, `git-webkit` and `gh` work
  unmodified, with no wrapper, prompt or approval.
- Only the agent is sandboxed, and only to hide credentials: one Unix user,
  and a wrapper that makes the credential paths unreadable to the agent's
  process tree. Nothing else is restricted: no egress filter, no write limits.
- An agent may commit. It cannot push, because it can read no key or token.
- An agent builds, runs, tests and debugs through `wk`; the raw build tools
  refuse it, so job sizing always applies.
- On your own servers an agent asks permission for nothing: Claude runs with
  `--dangerously-skip-permissions`. A borrowed box is not yours, and a login
  there may be root, to which Claude refuses that flag: there it runs with
  `--permission-mode auto`.

**A way through**

- There is always a manual way through when something is broken. A refusal
  that exists because of a rule, or because a reading could not be taken, is
  a barrier: `--force` crosses it, and the command says so again at exit.
- A refusal the command line itself caused (an unknown name, a bad argument)
  is not a barrier: there is nothing to cross.
- `--force` is the only override. No command has another.

**Credentials**

- One store: OpenBao, on the **credential server**. It is a plain store: no
  approval flow. The playbook installs it, initialises it once and gives it
  an unattended unseal.
- The client reads the store as the person, through the `bao` CLI, and
  delivers credentials to each of your servers over ssh at provisioning. A
  server never talks to the store.
- A borrowed box holds no credential. ssh agent forwarding makes `git push`
  work there.
- Nobody logs in to Claude or pi on a server. Remote Control is required, and
  it needs a full claude.ai login, so one **injector** on the credential
  server holds the real login and every server, a borrowed box included,
  holds a placeholder.

**Provisioning**

- Provisioning is Ansible; `wk machine setup` only generates the inventory
  and runs it. There is no provisioning code in `wk`.
- Nothing is installed with a package manager and nothing needs root, on any
  machine: sudo asks for a password everywhere. What a server must already have is
  asserted, and named when it is absent.

**Off the shelf**

- Provisioning: Ansible, the full `ansible` package on the client. Beside
  `ansible.builtin` the playbook uses two collections that package bundles,
  `containers.podman` and `community.general`, so there is no
  `requirements.yml`.
- Tasks: `pueue`, one daemon per machine.
- Secrets: OpenBao, and its `bao` CLI on the client.
- Network and identity: Tailscale, including Tailscale SSH.
- Linux build environment: the WebKit container SDK image under rootless
  podman, on your own Linux servers.

## Vocabulary

| Term | Meaning |
| --- | --- |
| client | The one interactive machine. Holds no workspace. |
| server | A machine `wk` manages: `linux`, `mac` or `borrowed`. |
| credential server | The one machine that runs OpenBao and the injector. Runs nothing else. |
| workspace | A named, disposable checkout for one task, on one server. |
| driver | How a workspace's commands run: `container` or `plain`. |
| mirror | A server's one bare WebKit repository. Every workspace clones from it. |
| task | Anything that outlives its terminal. A pueue task. |
| build preset | One way to build WebKit (`jsc-release`, `mac-release`, ...). |
| agent session | Claude Code or pi running in a workspace, under the credential wrapper. |

## Shape

```
client                         server (linux | mac | borrowed)
------                         --------------------------------
wk <cmd> <ws> ...   --ssh-->   wk <cmd> <ws> ...     (the same program)
                                 |- workspace: ~/wk/ws/<name>/src   (git clone --shared)
                                 |- driver:    container (podman + SDK) | plain
                                 |- tasks:     pueue  (group "build", one at a time)
                                 |- agent:     wrapper hiding credential paths
                                 '- mirror:    ~/wk/git/WebKit.git

credential server:  OpenBao (the one store)   injector (the one claude.ai login)
```

The same `wk` runs on every machine. A command that names a workspace is
handed whole to the server that holds it; that server's `wk` does the work and
the client confirms by reading back. Flags cross the hop as environment.

## Rules every command obeys

These are enforced once, in the dispatcher, from a table each command declares.

- A command is data: name, where it runs, positionals, options, subverbs,
  `readonly` or `destructive`, plus one `main(args, inv)`.
- A command is one module in `lib/wk/cmd/`, found by listing the directory:
  `build.py` is `wk build`, `key_set.py` is the verb `wk key set`. No file
  lists the commands.
- Where it runs: `client` (fleet-wide commands), `server` (the workspace's
  machine), `any`. A `server` command typed on the client is handed to the
  server named by `--on`, else to the one holding its `<ws>`; that one is
  found by asking every server for `wk ls --json`. A command that needs both
  ends is `any` and hands itself over.
- A command may declare a `forward`: something the client reaches that the
  command needs on its server. The hand-over's ssh then carries it as a
  reverse forward, on a connection of its own, to a port of the server's
  loopback that the command's words choose and `$WK_FORWARD` names.
- `inv` is the one object a command reaches machines, the clock, the fleet,
  drivers, tasks and secrets through. A test builds it of fakes. A machine's
  wk home is asked of it and computed nowhere else.
- `--dry-run`: every state change goes through one function, `act`, which
  under dry-run prints the command and runs nothing.
- Only a destructive action prompts. It asks once, defaults to No, declines
  without a terminal; `--yes` answers it. A destructive command that changes
  state before it asks, or a `readonly` one that changes state at all, is a
  bug the gate raises.
- A refusal says why and names the remedy. A barrier also names `--force`,
  which crosses it and says so again at exit.
- Exit codes: 0 ok, 1 failed, 2 busy, 3 stalled or out of memory, 4 needs a
  person, 75 refused for now.
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
| `Secrets` | get, put, list | the `bao` CLI run through `Machine`, fake |

`run` reads and `act` changes state: `act` and every other mutator end in the
one gate that `--dry-run` turns into a printed line. A server that does not
answer raises; its answer is unknown, never absent or empty.

A task's label is `<ws>:<what>:<preset>`. A workspace's tasks are those whose
label's first `:`-field is its name; `wk status`, `wk build` and `wk rm` all
use that one rule.

## State

- **Recomputed on every read:** the list of workspaces (each server is asked),
  a workspace's state, reachability, health, what is running.
- **Stored because it cannot be recomputed:** credentials (in OpenBao), task
  history and logs (in pueue), a workspace's files.
- **Artifacts:** the mirror, compiler caches, the SDK image, pinned downloads.
- `wk` keeps no database and no record files of its own.

Layout on a server, under its wk home: `~/wk` of the login, or `$WK_HOME` for
a `wk` typed on that machine itself.

| Path | Holds |
| --- | --- |
| `tools/` | wk itself: a checkout at exactly the client's commit |
| `bin/wk` | a link to `tools/wk`; one line in the login shell's profile puts `bin/` on `PATH` |
| `git/WebKit.git` | the mirror; `gc.auto 0`, since workspaces borrow its objects |
| `ws/<name>/src` | the checkout: `git clone --shared` from the mirror |
| `ws/<name>/build` | build output and the workspace's ccache |
| `ws/<name>/build/guard.py` | the memory guard, copied in by each build and test run |
| `ws/<name>/.ready` | written last by `wk new`; absent means half-made |
| `cache/` | pinned downloads: `pueue-<version>/`, `agents/`, `openbao-<version>/`. `wk gc` reclaims an entry no lane pins |
| `pueue/` | pueue's config, socket, state and task logs |
| `shims/` | the build-tool shims an agent's `PATH` starts with |
| `secrets/` | delivered credentials, mode 0700: deploy keys, `ssh_config`, `env.sh`. Hidden from agents |
| `claude-login/` | the placeholder claude.ai login an agent session reads |
| `injector-ca.pem` | the injector's CA, which an agent session trusts |
| `injector/` | on the credential server alone: the injector, its CA key and the real login |
| `openbao/` | on the credential server alone: OpenBao's config, its data and its unseal key |

Outside it, in the login's home: `~/.claude/` (the agents' instructions and
settings), `~/.claude.json` (Claude's start-up answers), `~/.pi/` (pi's
models), the `Include` line in `~/.ssh/config`, the profile lines, and the
unit or launchd agent that keeps a daemon running.

On the client, `~/.config/wk/openbao-init.json` (mode 0600) is the one copy of
what initialising the store printed: its recovery key and its root token.

Every mutating command is crash-only: killed at any point and re-run, it
converges. A half-made workspace is wiped, never repaired.

## Workspaces

- `wk new <ws> [--on <server>]` refuses a name alive on any server, and any
  name while a server does not answer, since it cannot be shown to be free
  (a barrier). Then on the chosen server: clone, wire remotes, create the
  container if the driver has one, write `.ready`.
- Remotes: `origin` (WebKit, fetch from the mirror, push refused), `fork`
  (yours). Git settings carry the measured fixes: no tags, commit-graph
  writing off, `insteadOf` to the mirror with the GitHub URL kept for the PR
  tool.
- `wk sync [<ws>]` fetches the mirror from GitHub, then `origin` in one
  workspace or in every one; on the client with no `<ws>`, on every server.
  Only remote-tracking branches move. `wk sync` owns the mirror's
  configuration: the playbook runs it once, on a server whose mirror lacks a
  branch.
- Drivers:
  - `container`: your Linux servers. Rootless podman and the SDK image, one
    idle container per workspace; a command is a `podman exec`.
  - `plain`: Mac servers and borrowed boxes. Commands run in the checkout.
- What a container shows. Three mounts, each at the server's own path: the
  login's home, the wk home, and the mirror again read-only. `HOME` is the
  login's and the hostname is the server's. So a session inside reads exactly
  what a login on the server reads: every path wk prints, the agents' state,
  the ssh configuration, and `wk` on the profile's `PATH`. The home is mounted
  whole because Claude replaces `~/.claude.json` by rename, which a mount of
  the one file does not survive. An agent in a container sees what an agent
  on a Mac sees: everything but `secrets/`, hidden at the same path.
- `wk` typed inside a container has no podman. It knows it is inside
  (`/run/.containerenv`) and which workspace's (`$WK_WORKSPACE`): that
  workspace is running, the others are unknown, a command for it runs
  directly, and a task is still a `podman exec`, because the server's pueue
  daemon starts it.
- `wk enter <ws>` is a normal shell in the workspace with normal credentials:
  a login shell, which in a container has first sourced `secrets/env.sh`.
- `wk rm <ws>` refuses under a queued or running task, asks, then removes
  container and directory. The `.ready` marker goes first.

## Tasks

- pueue runs from wk's pinned binaries, by absolute path, under wk's own
  config in `pueue/`: one daemon per server, wherever a login's XDG variables
  point. A systemd user unit, of a user who lingers, keeps it running on a
  Linux server and on a borrowed box, and a launchd agent on a Mac.
- `wk build <ws> [<preset>] [--detach]` adds a pueue task in the server's
  `build` group, whose parallel limit is 1: one build per machine. With no
  preset it repeats the one the workspace built last.
- `wk status [<ws>]`, `--log`, `--follow`, `--wait` read pueue. Silence is
  computed from the log's age on each read; nothing watches in the background.
- `wk build <ws> --kill` and `wk stop --tasks` kill through pueue.
- **Job sizing** is the one thing never left to the tool: jobs =
  min(cores, cap, available memory / memory per job), with the measured
  figures (1536 MB JSC-only, 4608 MB other CMake ports, 3072 MB Xcode) and a
  reserve. A reading that cannot be taken is fatal.
- **Memory guard:** the build runs in a child cgroup with a memory ceiling
  where one can be made and read back; otherwise under a watchdog that kills
  the build over budget. This is what keeps a borrowed box alive.
- `wk run <ws> -- <args>` and `wk test <ws> -- <tests and options>` wrap
  `Tools/Scripts` inside the workspace for the preset built last (`--preset`
  names another); a test run is a task too.
- Every tool `wk build`, `wk run` and `wk test` start runs with `WK_BUILD=1`,
  which is what lets it past the shims on an agent's `PATH`.

## Credentials

- `wk key set <name>` writes OpenBao. `wk key check` runs each credential's
  live probe (one that writes nothing). `wk key ls` lists the names.
  `wk key sync [<server>]` delivers: it is the playbook's `credentials` role.
- The store is asked with the `bao` CLI on the client, at
  `http://<credential server>:8200`, with the token `bao login` keeps:
  `bao kv get -field=value`, `bao kv put ... value=-` with the value on
  stdin, `bao kv list -format=json`. One value per name, in the KV version 2
  mount `secret`, under `wk/`.
- The names: `github-user`, `github-token`, `bugzilla-user`,
  `bugzilla-password` (unmodified `git-webkit` sends Bugzilla a login and a
  password), `litellm-key`, `claude-login`, `tailnet-auth-key`, and
  `deploy-key/<owner>/<repo>`.
- Delivered to each of your servers, into the places the tools already read:
  one deploy key per repository with its ssh alias, and `env.sh` with what
  `git-webkit` and `gh` look for, all under `secrets/`.
- pi's model key (`litellm-key`, in `~/.pi/agent/models.json`) is the one key
  an agent can read.
- Values are read on the client and travel inside a module's arguments, never
  argv; the tasks that carry one log nothing and show no diff.
- OpenBao listens on the credential server's tailnet address alone, without
  TLS of its own: the tailnet is the encryption. Its seal is a static key
  beside its data, which it reads at start, so a reboot needs no person; the
  directory's mode is what guards the key, on a machine no agent runs on.
- The store is initialised once, from the client. What that prints is kept on
  the client alone and never touches the server.
- The injector is a forward proxy on the credential server, reachable only on
  the tailnet. For Claude's three hosts it terminates TLS with its own CA and
  replaces the placeholder with the real login, which it alone refreshes,
  under a lock. Every other host is tunnelled untouched.
- The store's copy of the claude.ai login is spent once the injector first
  refreshes it. Losing the credential server's disk means logging in again.
- An agent session, and only that, gets the injector as its proxy, the
  injector's CA added to its trust, and `claude-login/` as the place its
  login is read from. The creds lane names all three; the agent lane asks it.

## Agent sessions

- `wk ai claude|pi <ws>` starts the agent in the workspace under the
  credential wrapper, with Remote Control on and named after the workspace.
- The wrapper: on Linux, `bwrap` mounting an empty directory over
  `secrets/`; on macOS, a Seatbelt profile denying reads of it; on a borrowed
  box, which holds no credential, nothing. All three give the agent a minimal
  environment: no credential variable and no ssh-agent socket.
- An agent's `PATH` starts with `shims/`, then `bin/`. The shims are the
  build tools' names: they refuse an agent and name `wk build`.
- `wk doctor <ws>` measures the wrapper with a stand-in process that tries to
  read a credential and must fail. A probe that dies is reported unmeasured,
  never passed.
- Claude Code is pinned and fetched once per server; pi is installed from npm.
- **A borrowed box** is off the tailnet, the one network the injector listens
  on, and it holds only the placeholder login and the injector's CA.
  `wk ai claude <ws>` typed on the client carries the injector to it: the
  hand-over's ssh holds a reverse forward from a port of the box's loopback
  to the injector for as long as the session lasts, and that port is the
  session's proxy. A port already taken ends the ssh before anything starts.
  Typed on the box itself there is no forward, and the command is refused
  naming the client.
- While such a session runs, the forwarded port is reachable by the other
  users of the box.

## Provisioning

- `wk machine setup <server>` runs, from the client,
  `ansible-playbook -i ansible/inventory.py --limit <server> ansible/site.yml`
  and returns its exit code. `--stage <role>` adds `--tags <role>`;
  `--dry-run` adds `--check --diff`. The same line typed by hand does the
  same.
- The inventory is `machines/*.conf` and nothing else: `ansible/inventory.py`
  prints `wk machine inventory`. A host per server, a group per kind
  (`linux`, `mac`, `borrowed`, `credential`), each conf literal as the
  host variable `wk_<key>`, and for every host the client's literals, this
  checkout's commit and the store's address.
- The roles, each tagged with its own name: `wk` (the client's commit at
  `tools/`, the layout, `wk` on the login `PATH`), `pueue`, `mirror`, `sdk`
  (Linux: subordinate ids asserted, the SDK image), `agents`, `openbao` and
  `injector` (the credential server), `credentials` (your servers) and
  `borrowed-claude` (a borrowed box: the placeholder login and the injector's
  CA, nothing else).
- A server fetches exactly the client's commit from the repository's public
  URL. Uncommitted changes, and a commit on no remote branch, are barriers.
- Every task is idempotent: a second run reports `changed=0`, which is how
  convergence is verified, and a run killed anywhere converges when typed
  again. A task that needs a person stops the run with its remedy.
- Under `--dry-run` a task that asks a running daemon or a server's API is
  skipped: Ansible's check mode reports files, downloads and units.
- `ansible/group_vars/all.json` holds what the playbook installs by and the
  program names: the pinned versions with their digests, and the ports. It
  is the one copy; both read it.
- `wk doctor` reports each thing a server needs as ok, missing or unknown,
  with the command that fixes it. Unknown is never reported as ok. A server
  that does not answer is one unknown row; the others keep theirs.
- `wk gc` reclaims what loses no work and names the flag for the rest.
- A machine is one `machines/<name>.conf`: `kind` and literal values. Code
  never names a machine. The file's name must equal the machine's short
  hostname, which is also its tailnet name (for a borrowed box, its ssh-config
  name): that is how `wk` knows which machine it is on, and no key overrides
  it. The comparison is of lowercase names. `user=` names the login ssh and
  Ansible use there when it is not the client's.
- Doctor rows, gc rows and cache pins are collected from a `hooks` module in
  each package under `lib/wk/`; no file lists them.

## Open decisions

- **Refusals `--force` does not cross.** A build whose place cannot be
  measured has no job count to use, and a command whose podman or pueue does
  not answer has nothing to act through. Crossing either needs a stated
  stand-in (one job; no container), which is the owner's to choose.

## Tree

```
wk                      launcher (python3 -I)
DESIGN.md               the design
STATUS.md               what is implemented, tested and verified on a real machine (`wk help status`)
machines/               one conf per machine
ansible/site.yml        the playbook: provisioning, whole
ansible/inventory.py    Ansible's inventory script: prints `wk machine inventory`
ansible/group_vars/     all.json: the pins and ports the playbook and the program share
ansible/roles/          wk, pueue, mirror, sdk, agents, openbao, injector, credentials, borrowed-claude
lib/wk/core/            dispatch, decl, act, inv, route, machine, clock, conf, fleet,
                        errors, seams and hooks (the contracts between lanes), fakes
lib/wk/cmd/             one module per command: COMMAND table + main
lib/wk/workspace/       drivers, mirror, git wiring, lifecycle
lib/wk/tasks/           pueue client, sizing, memory guard, status
lib/wk/webkit/          project.json, presets, Tools/Scripts command lines (the only WebKit names)
lib/wk/creds/           the bao CLI's client, credential rules, the injector, what a session takes
lib/wk/agent/           wrapper, session
lib/wk/provision/       the inventory and the ansible-playbook line, the rows no other lane owns
tests/                  unittest, one test per behaviour, against fakes
```

Python, standard library only, 3.9 syntax. Shell only where the shell is the
point (the build-tool shim). YAML only in `ansible/`.

## Testing

- `wk selftest` runs `tests/`: a command is typed into an in-memory world and
  the test asserts the exit code, the line a person acts on, the commands
  recorded in order, and the final state.
- Crash safety is enumerated: each mutating command is killed after every
  effect and re-run, and must reach the uninterrupted final state.
- In unit tests every real machine tool (`ssh`, `podman`, `pueue`, `bao`,
  `ansible-playbook`, `git` on a remote) is unreachable; a test that reaches
  one fails. So the playbook is not run by them: the front is tested (the
  inventory, the exact `ansible-playbook` line, the refusals), and the
  journeys start from fake servers holding what the playbook leaves.
- A lane's tests ask that lane's hooks alone. `tests/test_end_to_end.py` is
  where every real lane runs together, over fake machines: a Linux server, a
  Mac, a borrowed box and the credential server, from provisioned to a build
  and an agent session and back.
- A behaviour that needs a real tool or server is a test marked `live`,
  skipped by name when it is absent: `ansible-playbook --syntax-check` of the
  playbook is one.

## Plan

**Pass 1: macOS and Linux servers, end to end.**

1. Core: launcher, dispatcher, seams and fakes, machine confs, hand-over,
   `wk help`, `wk selftest`.
2. Workspaces: mirror, `new`, `rm`, `ls`, `enter`, `sync`, both drivers.
3. Tasks: pueue, sizing, memory guard, `build`, `run`, `test`, `status`, `stop`.
4. Credentials: the store's client, `wk key`, the injector.
5. Agent sessions: wrapper, shims, `wk ai`, the wrapper's probe, the forward
   to a borrowed box.
6. Provisioning: the playbook, `wk machine setup`, `wk doctor`, `wk gc`.

**Pass 2: the bench lab.** Boards, images, boot selection, the benchmark
pipeline and its statistics, the dedicated bench Mac.

What of pass 1 is implemented, tested and verified on a real machine, and
every check still owed on one, is in `STATUS.md`.
