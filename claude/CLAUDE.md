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

A Linux workspace may also be **armhf** — 32-bit ARM, natively. `arch=` in
`~/.wk-workspace` is the authority, and it is the only one: the kernel is the
host's, so `uname -m` answers `aarch64` in a 32-bit workspace and `lscpu` looks
64-bit too. Nothing has gone wrong there, and it needs no fixing — `wk build`
already runs the build under `linux32` and passes the ARM flags, so a plain
`wk build jsc-release` in an armhf workspace is a 32-bit JSC. There is no GPU in
one (the NVIDIA userspace is aarch64-only), so it is a software-rendering
workspace: fine for JSC and for CPU-class benchmarks, useless for MotionMark.

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
wk profile [file.js]  # where the time went: JSC's own profilers, samply,
                      # Instruments -- one flag each, no env-var walls
```

`wk profile --mode sampling` (the default) prints the tier breakdown, which is
what decides the next step: mostly FTL/DFG/Baseline means the cost is in
generated JS, so `--mode bytecode` next; mostly C/C++ means the engine itself,
so `--mode native` -- samply on Linux, Instruments on a macOS guest. The
jsc-profile skill explains how to read each one; the flags are here so no run
has to be assembled by hand.

Debugging, in a macOS workspace (the Apple ports; the Linux half is not wired
up yet). Each needs a terminal, and each gets one:

```
wk run --lldb -- <args>       # jsc under lldb
wk gui [url]                  # MiniBrowser on the guest's own desktop
wk gui [url] --lldb           # ... under lldb, stopped before it starts
wk gui [url] --lldb web       # ... running, with lldb attached to the web process
wk test --layout --lldb <test>  # one layout test, no timeout, lldb on the web process
wk test --layout --lldb ui <test>  # ... on WebKitTestRunner instead
```

`--lldb web` and `wk test --lldb` both wait for the web process and attach as it
starts — that is where a page's JS, layout and rendering run, so it is usually
the process a breakpoint belongs in. Type `continue` once you have set yours.
`wk test --lldb` writes the run's output to `/tmp/wk-test-lldb.log`, because the
terminal belongs to the debugger.

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

`origin` is `WebKit/WebKit` and pushing to it fails immediately — there is no
write access to upstream and never will be. The fork remotes (`fork`,
`forkwpe`) are already configured in every checkout: fetch over HTTPS, push
over ssh through a deploy key scoped to that one repository.

Reads are anonymous over HTTPS and always work: fetching `origin` or either
fork needs no credential at all.

**Pushing is a switch, and it is normally off while you are running.** The
deploy keys are held outside the workspace (`wk push`, on the host) and
`wk claude` turns the switch off before handing over control, so a push is
refused at the door — `no such identity` from ssh means exactly that, not a
broken setup. Do not try to work around it: publishing is the one thing a
disposable workspace is not allowed to do on its own. Say what you would have
pushed and let the person at the keyboard run `wk push on`.

Never use `git push --force` against a shared branch, and never commit unless
asked.

## Long-running commands, from inside a workspace

A build here is tens of minutes and the shell running it is not guaranteed to
last that long. `wk build <config> --detach` starts it and returns
immediately; `wk status` and `wk logs -f` follow it. Nothing is lost if this
session ends, and `build.status` ends up saying what actually happened rather
than `running` forever.

Every build is watched for memory (a job count is a prediction, and a link
step can break it). If yours is killed you will see `build=oom` in `wk status`
with the peak and the budget: build with fewer jobs
(`WK_MB_PER_JOB=3072 wk build <config>`) rather than assuming the code is at
fault.

## WebKit conventions

The `jsc` skill is mandatory for any edit under `Source/`. It carries the house
rules: comment style, C++ conventions, smart pointers, Safer C++, naming, and
copyright headers. Invoke it before editing, not after.

Before considering a change done, run `Tools/Scripts/check-webkit-style`.
