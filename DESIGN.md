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
- A server is yours (Linux or macOS) or **borrowed**: a shared build box where
  you are root inside someone else's container, podman does not work, and
  other people are root too.
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

**Credentials**

- One store: OpenBao, on the **credential server**. It is a plain store: no
  approval flow.
- The client reads the store as the person and delivers credentials to each of
  your servers over ssh at provisioning. A server never talks to the store.
- A borrowed box holds no credential. ssh agent forwarding makes `git push`
  work there.
- Nobody logs in to Claude or pi on a server. Remote Control is required, and
  it needs a full claude.ai login, so one **injector** on the credential
  server holds the real login and every server holds a placeholder.

**Off the shelf**

- Tasks: `pueue`, one daemon per machine.
- Secrets: OpenBao.
- Network and identity: Tailscale, including Tailscale SSH.
- Linux build environment: the WebKit container SDK image under rootless
  podman, on your own Linux servers.

## Vocabulary

| Term | Meaning |
| --- | --- |
| client | The one interactive machine. Holds no workspace. |
| server | A machine `wk` manages: `linux`, `mac` or `borrowed`. |
| credential server | The server that runs OpenBao and the injector. Runs nothing else. |
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
- Where it runs: `client` (fleet-wide commands), `server` (the workspace's
  machine), `any`.
- `--dry-run`: every state change goes through one function, `act`, which
  under dry-run prints the command and runs nothing.
- Only a destructive action prompts. It asks once, defaults to No, declines
  without a terminal; `--yes` answers it.
- A refusal says why and names the remedy. `--force` crosses a refusal that
  exists because of a rule, and says so again at exit.
- Exit codes: 0 ok, 1 failed, 2 busy, 3 stalled or out of memory, 4 needs a
  person, 75 refused for now.
- Anything undeclared is refused with the usage line.

## Seams

Every effect goes through one of these, so a command runs unchanged against a
fake in a unit test.

| Seam | Interface | Implementations |
| --- | --- | --- |
| `Machine` | run, read, write, exists, listdir, copy, spawn | `Local`, `Ssh`, `Fake` |
| `Clock` | now, sleep, wait_until | real, fake |
| `Driver` | create, destroy, state, exec argv, enter argv | `Container`, `Plain` |
| `Tasks` | add, status, log, kill, wait | pueue, fake |
| `Secrets` | get, put, list | OpenBao HTTP API, fake |

## State

- **Recomputed on every read:** the list of workspaces (each server is asked),
  a workspace's state, reachability, health, what is running.
- **Stored because it cannot be recomputed:** credentials (in OpenBao), task
  history and logs (in pueue), a workspace's files.
- **Artifacts:** the mirror, compiler caches, the SDK image, pinned downloads.
- `wk` keeps no database and no record files of its own.

Layout on a server, under `~/wk` (`WK_HOME`):

| Path | Holds |
| --- | --- |
| `git/WebKit.git` | the mirror; `gc.auto 0`, since workspaces borrow its objects |
| `ws/<name>/src` | the checkout: `git clone --shared` from the mirror |
| `ws/<name>/build` | build output and the workspace's ccache |
| `ws/<name>/.ready` | written last by `wk new`; absent means half-made |
| `cache/` | pinned downloads: pueue, agents |
| `secrets/` | delivered credentials, mode 0700; hidden from agents |

Every mutating command is crash-only: killed at any point and re-run, it
converges. A half-made workspace is wiped, never repaired.

## Workspaces

- `wk new <ws> [--on <server>]` refuses a name alive on any server, then on
  the chosen server: clone, wire remotes, create the container if the driver
  has one, write `.ready`.
- Remotes: `origin` (WebKit, fetch from the mirror, push refused), `fork`
  (yours). Git settings carry the measured fixes: no tags, commit-graph
  writing off, `insteadOf` to the mirror with the GitHub URL kept for the PR
  tool.
- Drivers:
  - `container`: your Linux servers. Rootless podman, the SDK image, the
    checkout and `secrets/` bind-mounted. Commands run by `podman exec`.
  - `plain`: Mac servers and borrowed boxes. Commands run in the checkout.
- `wk enter <ws>` is a normal shell in the workspace with normal credentials.
- `wk rm <ws>` refuses under a running task, asks, then removes container and
  directory. The `.ready` marker goes first.

## Tasks

- `wk build <ws> <preset> [--detach]` adds a pueue task in the server's
  `build` group, whose parallel limit is 1: one build per machine.
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
- `wk run <ws> -- <args>` and `wk test <ws> <args>` wrap `Tools/Scripts`
  inside the workspace; long ones are tasks too.

## Credentials

- `wk key set <name>` writes OpenBao. `wk key check` runs each credential's
  live probe (one that writes nothing). `wk key sync [<server>]` delivers.
- Delivered to each of your servers, into the places the tools already read:
  one deploy key per repository with its ssh alias, the GitHub token and
  Bugzilla key where `git-webkit` and `gh` look, the pi model key.
- Values travel on stdin, never argv. Delivery compares digests and reports
  what changed.
- The injector is a forward proxy on the credential server, reachable only on
  the tailnet. For Claude's three hosts it terminates TLS with its own CA and
  replaces the placeholder with the real login, which it alone refreshes,
  under a lock. Every other host is tunnelled untouched.
- An agent session, and only that, gets the injector as its proxy and the
  injector's CA added to its trust.

## Agent sessions

- `wk ai claude|pi <ws>` starts the agent in the workspace under the
  credential wrapper, with Remote Control on and named after the workspace.
- The wrapper: on Linux, `bwrap` mounting an empty directory over each
  credential path; on macOS, a Seatbelt profile denying reads of them. Both
  drop credential environment variables and the ssh-agent socket.
- Build tools on `PATH` are shims that refuse an agent and name `wk build`.
- `wk doctor <ws>` measures the wrapper with a stand-in process that tries to
  read a credential and must fail. A probe that dies is reported unmeasured,
  never passed.
- Claude Code is pinned and fetched once per server; pi is installed from npm.

## Provisioning

- `wk machine setup <server>` runs from the client and converges the server in
  idempotent stages: wk itself (a git bundle of HEAD), pueue (pinned, hash
  checked), the mirror, the SDK image or the build prerequisites, the agents,
  the credentials. A second run reports no changes.
- `wk doctor` reports each thing a server needs as ok, missing or unknown,
  with the command that fixes it. Unknown is never reported as ok.
- `wk gc` reclaims what loses no work and names the flag for the rest.
- A machine is one `machines/<name>.conf`: `kind` and literal values. Code
  never names a machine.

## Tree

```
wk                      launcher (python3 -I)
DESIGN.md
machines/               one conf per machine
lib/wk/core/            dispatch, decl, act, machine, clock, conf, fleet, errors
lib/wk/cmd/             one module per command: COMMAND table + main
lib/wk/workspace/       drivers, mirror, git wiring, lifecycle
lib/wk/tasks/           pueue client, sizing, memory guard, status
lib/wk/webkit/          presets, Tools/Scripts command lines (the only WebKit names)
lib/wk/creds/           OpenBao client, credential rules, delivery, injector
lib/wk/agent/           wrapper, session, install, build-tool shims
lib/wk/provision/       setup stages, doctor rows, gc rows
tests/                  unittest, one test per behaviour, against fakes
```

Python, standard library only, 3.9 syntax. Shell only where the shell is the
point (the build-tool shim).

## Testing

- `wk selftest` runs `tests/`: a command is typed into an in-memory world and
  the test asserts the exit code, the line a person acts on, the commands
  recorded in order, and the final state.
- Crash safety is enumerated: each mutating command is killed after every
  effect and re-run, and must reach the uninterrupted final state.
- In unit tests every real machine tool (`ssh`, `podman`, `pueue`, `git` on a
  remote) is unreachable; a test that reaches one fails.
- A behaviour that needs a real server is a test marked `live`, skipped by
  name when the server is absent.

## Plan

**Pass 1: macOS and Linux servers, end to end.**

1. Core: launcher, dispatcher, seams and fakes, machine confs, hand-over,
   `wk help`, `wk selftest`.
2. Workspaces: mirror, `new`, `rm`, `ls`, `enter`, `sync`, both drivers.
3. Tasks: pueue, sizing, memory guard, `build`, `run`, `test`, `status`, `stop`.
4. Credentials: OpenBao client, `wk key`, delivery, the injector.
5. Agent sessions: wrapper, shims, `wk ai`, install, the wrapper's probe.
6. Provisioning: `wk machine setup`, `wk doctor`, `wk gc`.

**Pass 2: the bench lab.** Boards, images, boot selection, the benchmark
pipeline and its statistics, the dedicated bench Mac.

**Owed, needs a real machine**

- TODO: live run of every pass 1 command on a Linux server and a Mac server.
- TODO: the Seatbelt profile on macOS, and `bwrap` inside a container.
- TODO: Remote Control through the central injector.
- TODO: whether WebKit's own bubblewrap sandbox runs inside the agent wrapper.
- TODO: a writable child cgroup on a borrowed box.
- TODO: the client machine, configured by its owner.
