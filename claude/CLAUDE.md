# Working environment

You are almost certainly running inside a **wk workspace** — a disposable
environment holding one WebKit checkout for one task. Two consequences:

- **The host filesystem is not reachable, by design.** There is no macOS home
  mounted here. Do not look for one, and do not try to reach it.
- **The workspace is disposable.** Scratch files, experimental branches and
  broken build directories are fine; everything is discarded when the workspace
  is deleted. Prefer making a mess here over being careful.

There are two kinds, and they differ in ways that matter:

| | Linux workspace | macOS workspace |
|---|---|---|
| what it is | a podman container | a macOS VM (Tart) |
| checkout | `/src/WebKit` | `/Users/admin/WebKit` |
| ports | JSCOnly, GTK, WPE | the Apple ports, via Xcode |
| egress | filtered, see below | filtered, see below |

`pwd` tells you which one you are in.

In a Linux workspace the checkout's base is a read-only snapshot with a
copy-on-write layer on top; in a macOS workspace it is an APFS clone of a
golden image. Either way the edits are yours alone and cannot affect another
workspace.

## Commands

Use `wk` rather than invoking build scripts directly — it derives the job count
from available memory and runs the build at a nice level that keeps the host
usable. A raw `ninja -j$(nproc)` can hang the machine.

```
wk build <config>     # jsc-release, gtk-debug, wpe-release, mac-release, ...
wk run -- <args>      # run jsc from the current build
wk test <args>        # run tests
wk status             # this workspace's build/test state; exit code is machine-readable
wk logs [-f|--all]    # the build log, errors first
wk build <config> --dry-run    # what it would build, and where, without building
```

No workspace name in any of them: you are inside the workspace, and it is the
only one there is. `wk build --list` shows the configs, and a bare `wk run` or
`wk test` uses the config this workspace was built with — so a macOS guest does
not have to be told it is an Apple port every time.

Commands that act on the *host* — `wk new`, `wk rm`, `wk sync`, `wk gc`,
`wk session`, `wk quiesce`, `wk verify`, `wk claude` — refuse in here and say
so. That is not something to work around: nothing in a workspace can create or
destroy a workspace, and `wk verify` measures the boundary from the outside as
well as the inside, so a result from in here would mean nothing.

## Network

**In a Linux workspace**, egress is restricted to the Anthropic API, GitHub, and
the two Raspberry Pi test devices over Tailscale. Everything else — including
the rest of the local network — is dropped by a firewall you cannot see or
modify. If a fetch fails, that is expected: find another way rather than trying
to work around it.

**In a macOS workspace the same allowlist applies**, enforced differently: the
guest has a real network interface, and Softnet — a packet filter running on
the host, outside the guest — denies everything except the address of that same
proxy. You cannot turn it off from in here, which is the point.

If a fetch fails in either kind of workspace, that is the boundary working.
Find another way rather than trying to route around it. One thing genuinely
does not work in a macOS workspace: ssh does not go through an HTTP proxy, so
push over HTTPS rather than ssh.

The Pis (`rpi4`, `rpi5`) are reachable over SSH for performance testing and
deploying builds.

## Git

Reads of public repositories are anonymous over HTTPS. The only push
destination configured is the `justinmichaud/WebKit` fork, via a deploy key
scoped to that one repository — you cannot push anywhere else.

Never use `git push --force` against a shared branch, and never commit unless
asked.

## WebKit conventions

The `jsc` skill is mandatory for any edit under `Source/`. It carries the house
rules: comment style, C++ conventions, smart pointers, Safer C++, naming, and
copyright headers. Invoke it before editing, not after.

Before considering a change done, run `Tools/Scripts/check-webkit-style`.
