# Working environment

You are almost certainly running inside a **wk workspace** — a disposable
container holding one WebKit checkout for one task. Two consequences:

- **The host filesystem is not reachable, by design.** There is no macOS home
  mounted here. Do not look for one, and do not try to reach it.
- **The workspace is disposable.** Scratch files, experimental branches and
  broken build directories are fine; everything is discarded when the workspace
  is deleted. Prefer making a mess here over being careful.

`/src/WebKit` is your checkout. Its base is a read-only snapshot with a
copy-on-write layer on top, so edits are yours alone and cannot affect other
workspaces.

## Commands

Use `wk` rather than invoking build scripts directly — it derives the job count
from available memory and runs the build at a nice level that keeps the host
usable. A raw `ninja -j$(nproc)` can hang the machine.

```
wk build <config>     # jsc-debug, jsc-release, gtk-debug, gtk-release-asan, ...
wk run -- <args>      # run jsc from the current build
wk test <args>        # run tests
```

`wk build --list` shows the configs.

## Network

Egress is restricted to the Anthropic API, GitHub, and the two Raspberry Pi
test devices over Tailscale. Everything else — including the rest of the local
network — is dropped by a firewall you cannot see or modify. If a fetch fails,
that is expected: find another way rather than trying to work around it.

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
