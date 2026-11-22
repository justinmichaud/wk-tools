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
- On your own servers an agent asks permission for nothing: Claude runs with
  `--dangerously-skip-permissions`. On a borrowed box the login is root, to
  which Claude refuses that flag, so it runs with `--permission-mode auto`.

**A way through**

- There is always a manual way through when something is broken. A refusal
  that exists because of a rule, or because a reading could not be taken, is
  a barrier: `--force` crosses it, and the command says so again at exit.
- A refusal the command line itself caused (an unknown name, a bad argument)
  is not a barrier: there is nothing to cross.
- `--force` is the only override. No command has another.

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
- A command is one module in `lib/wk/cmd/`, found by listing the directory:
  `build.py` is `wk build`, `key_set.py` is the verb `wk key set`. No file
  lists the commands.
- Where it runs: `client` (fleet-wide commands), `server` (the workspace's
  machine), `any`. A `server` command typed on the client is handed to the
  server named by `--on`, else to the one holding its `<ws>`; that one is
  found by asking every server for `wk ls --json`. A command that needs both
  ends is `any` and hands itself over.
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
| `Secrets` | get, put, list | OpenBao HTTP API, fake |

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
| `cache/` | pinned downloads: `pueue-<version>/`, `agents/`. `wk gc` reclaims an entry no lane pins |
| `pueue/` | pueue's config, socket, state and task logs |
| `shims/` | the build-tool shims an agent's `PATH` starts with |
| `secrets/` | delivered credentials, mode 0700: deploy keys, `ssh_config`, `env.sh`. Hidden from agents |
| `claude-login/` | the placeholder claude.ai login an agent session reads |
| `injector-ca.pem` | the injector's CA, which an agent session trusts |
| `injector/` | on the credential server alone: the injector, its CA key and the real login |

Outside it, in the login's home: `~/.claude/` (the agents' instructions and
settings), `~/.claude.json` (Claude's start-up answers), `~/.pi/` (pi's
models), the `Include` line in `~/.ssh/config`, and the profile lines.

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
  Only remote-tracking branches move.
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
  point.
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
  live probe (one that writes nothing). `wk key sync [<server>]` delivers.
- The names: `github-user`, `github-token`, `bugzilla-user`,
  `bugzilla-password` (unmodified `git-webkit` sends Bugzilla a login and a
  password), `litellm-key`, `claude-login`, `tailnet-auth-key`, and
  `deploy-key/<owner>/<repo>`.
- Delivered to each of your servers, into the places the tools already read:
  one deploy key per repository with its ssh alias, and `env.sh` with what
  `git-webkit` and `gh` look for, all under `secrets/`.
- pi's model key (`litellm-key`, in `~/.pi/agent/models.json`) is the one key
  an agent can read.
- Values travel on stdin, never argv. Delivery compares digests and reports
  what changed.
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

## Provisioning

- `wk machine setup <server>` runs from the client and converges the server in
  idempotent stages: wk itself (a git bundle of HEAD), pueue (pinned, hash
  checked), the mirror, the SDK image, the agents, the credentials. On the
  credential server: wk itself and the injector. A second run reports no
  changes, and `--dry-run` prints the whole plan on a bare server.
- `wk doctor` reports each thing a server needs as ok, missing or unknown,
  with the command that fixes it. Unknown is never reported as ok. A server
  that does not answer is one unknown row; the others keep theirs.
- `wk gc` reclaims what loses no work and names the flag for the rest.
- A machine is one `machines/<name>.conf`: `kind` and literal values. Code
  never names a machine. The file's name must equal the machine's short
  hostname, which is also its tailnet name: that is how `wk` knows which
  machine it is on, and no key overrides it.
- Setup stages, doctor rows, gc rows and cache pins are collected from a
  `hooks` module in each package under `lib/wk/`; no file lists them.

## Open decisions

- **Claude on a borrowed box.** Nothing is delivered there, so a session has
  no placeholder login and no CA to trust. Neither is a credential;
  delivering those two would make `wk ai claude` work on a borrowed box.
- **Refusals `--force` does not cross.** A build whose place cannot be
  measured has no job count to use, and a command whose podman or pueue does
  not answer has nothing to act through. Crossing either needs a stated
  stand-in (one job; no container), which is the owner's to choose.

## Tree

```
wk                      launcher (python3 -I)
DESIGN.md
machines/               one conf per machine
lib/wk/core/            dispatch, decl, act, inv, route, machine, clock, conf, fleet,
                        errors, seams and hooks (the contracts between lanes), fakes
lib/wk/cmd/             one module per command: COMMAND table + main
lib/wk/workspace/       drivers, mirror, git wiring, lifecycle
lib/wk/tasks/           pueue client, sizing, memory guard, status
lib/wk/webkit/          project.json, presets, Tools/Scripts command lines (the only WebKit names)
lib/wk/creds/           OpenBao client, credential rules, delivery, injector
lib/wk/agent/           wrapper, session, install, build-tool shims
lib/wk/provision/       wk itself on a server, the rows no other lane owns
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
- A lane's tests ask that lane's hooks alone. `tests/test_end_to_end.py` is
  where every real lane runs together, over fake machines: a Linux, a Mac and
  a borrowed server from bare to a build and an agent session and back.
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

- TODO: a live run of every pass 1 command on a Linux server, a Mac and a borrowed box, from a client its owner configured.
- TODO: pueue 4.0.5: the pinned binaries' digests, the daemon under wk's config, the JSON of `status` and `log`, a task's environment (it is the `pueue add`'s), and the client reaching the socket from inside a container.
- TODO: podman and the SDK image: the `podman run` line (keep-id, the host's name, `HOME`, the home mounted) with the real image, subordinate ids, and `wk` typed inside the container.
- TODO: the wrapper: the Seatbelt profile on macOS, `bwrap` inside a container, and whether WebKit's own bubblewrap sandbox runs inside it.
- TODO: the injector and Remote Control: Claude reading the placeholder from `claude-login/`, TLS through the injector's CA, a refresh, and Remote Control through the proxy.
- TODO: OpenBao: the client against a real KV version 2 store and a `bao login` token.
- TODO: git wiring with `git-webkit`: `env.sh` as `git-webkit` and `gh` read it, a push through the deploy-key alias, a pull request, a Bugzilla login with `bugzilla-password`.
- TODO: a borrowed box: a writable child cgroup, `nice` and `ionice`, and Claude as root under `--permission-mode auto`.
