# macOS VMs, for the Apple ports

Everything else in `wk-tools` runs Linux workspaces inside a podman VM. The
Apple ports cannot: they need Xcode and a real macOS userspace. This is that
second, parallel kind of target. It shares nothing with the podman one.

```sh
wk new mac-rel --target vm     # clones the golden base (seconds)
wk vm start mac-rel            # boots it, writes an ssh alias
wk build mac-rel mac-release
wk run   mac-rel --config mac-release -- -e 'print(1+1)'
wk rm    mac-rel               # deletes the guest and its host-side state
```

---

## Why a VM at all

macOS has no container primitive for macOS userspace. Apple's `container` CLI —
which is installed on this machine — runs *Linux* containers. So isolation for
an Apple-port build means a virtual machine, and there is no lighter option.

## What a workspace is

**A workspace is a VM**, cloned from a golden base.

The base is built once: pull the Cirrus Labs image (Xcode already installed,
which sidesteps both the Setup Assistant and a multi-hour Xcode install), boot
it, inject an ssh key, clone WebKit, install the Claude CLI, shut it down.
`wk vm base` does this, and `wk new --target vm` does it for you the first time.

Every workspace after that is `tart clone`, which is APFS copy-on-write. This
is the point: **creating a workspace never clones a repository.** It is the
same property the Linux overlay scheme provides, obtained from the filesystem
instead of from `mount -t overlay`.

The alternatives were a shared checkout inside one long-lived guest (`cp -c`
per workspace — works, but gives up the VM boundary between workspaces) and an
independent clone per guest (simplest, and wastes a multi-gigabyte fetch every
time). The golden base gets the cheapness of the first with the isolation of
the third.

Building the base is a long, one-time job — the image pull dominates the first
hour and the in-guest WebKit clone the next. Both are network-bound, and
neither is repeated per workspace.

Keeping the base current:

```sh
wk vm base --refresh    # boot it, fast-forward the checkout, shut it down
wk vm base --rebuild    # throw the guest away and provision from scratch
```

`--refresh` is the one you want week to week. New workspaces then start from
current `main`; existing ones are unaffected, because a clone is independent
from the moment it is made. This is the macOS analogue of `wk sync` publishing
a new base snapshot — same purpose, and the same property that a workspace
already in flight keeps working.

## Isolation: what you get, and what you do not

Be precise here, because it is **not** what a Linux workspace gives you.

| | Linux workspace | macOS workspace |
|---|---|---|
| host filesystem | unreachable | unreachable |
| disposable | yes | yes |
| filtered egress | **yes**, nftables | **no** |

The Linux workspaces are confined by nftables in the podman VM's root network
namespace — a namespace the workspace does not own and cannot modify. A macOS
guest has no equivalent. There is no shared netns to filter from outside, and
`pf` inside the guest is writable by anything running as root there, which
includes whatever you were trying to sandbox.

So a macOS workspace is a blast-radius boundary, not a network boundary.
`wk claude` says so out loud before it hands over control, and the workspace
`CLAUDE.md` says so too.

If you need the network closed as well, the honest options are host-side `pf`
against the VM's interface, or a restrictive Tailscale ACL — remembering that
Tailscale's own documentation says ACLs "don't affect local network traffic",
so that one is defence in depth and never the boundary.

## The two limits that actually bite

**Apple permits two running macOS VMs per host**, and Virtualization.framework
enforces it: a third fails with `VZErrorDomain` code 6. The limit is on
*running* guests, so `wk vm new` only warns and `wk vm start` refuses. Creating
a third VM is fine; starting it is not.

**Memory is the tighter limit in practice.** On a 32 GB machine the podman VM
already holds the whole envelope (20 GB, leaving 12 GB for the desktop), so
there is nothing left for a macOS guest. `wk vm start` checks this and refuses
with the numbers rather than letting two hypervisors each promise memory that
does not exist:

```
podman machine 'wk'          20480 MB   running
macOS VM 'mac-rel'           20480 MB   requested
host envelope                20480 MB   (32768 MB total, 12288 MB kept for the desktop)
```

Stop the podman machine, run the guest smaller with `WK_VM_MEM_MB`, or override
with `WK_VM_SHARE=1` if you know what you are doing.

## Tart, and its licence

Tart moved from `cirruslabs/tart` to `openai/tart` and is **FSL-1.1-ALv2**, not
OSI-open. Internal use is a Permitted Purpose — FSL defines that as any purpose
other than a Competing Use, and building WebKit does not compete with Tart.
Each version also converts to Apache-2.0 two years after release.

`./setup` does not install it. It cannot simply be copied to a bare path
either: the binary needs the `com.apple.security.virtualization` entitlement,
which lives in the signed `.app` bundle, so extracting the executable produces
something that fails at runtime.

```sh
curl -fsSLO https://github.com/openai/tart/releases/latest/download/tart.tar.gz
mkdir -p ~/.local/share/tart ~/.local/bin
tar -xzf tart.tar.gz -C ~/.local/share/tart/
ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart
```

If the licence ever becomes unacceptable, the researched alternatives, in order:

| | Licence | CLI lifecycle | Unattended install |
|---|---|---|---|
| `lume` | MIT | full | yes — the only FOSS one that closes this gap |
| UTM + `utmctl` | Apache-2.0 | no `create` for macOS guests | no, build the golden image in the GUI once |
| Apple's own sample | MIT | you write it (~500 lines Swift) | no |

Apple's sample code is *"Running macOS in a virtual machine on Apple silicon"*,
verbatim MIT, and needs only the `com.apple.security.virtualization`
entitlement with ad-hoc signing — no paid developer account. Installing from an
IPSW needs no Apple ID.

## Build configs

```
mac-debug          macOS (Apple port), Debug, Xcode
mac-release        macOS (Apple port), Release, Xcode
mac-release-asan   macOS (Apple port), Release + AddressSanitizer
ios-sim-release    iOS Simulator, Release, Xcode
```

There is no `--mac` flag to pass: `build-webkit` treats Apple Cocoa as the
default on Darwin, so the empty port string *is* the port selection.

Two differences from the CMake ports are load-bearing:

- **The job count travels differently.** CMake ports take `--makeargs=-jN`;
  the Xcode build ignores that entirely and needs `-jobs N` passed through to
  `xcodebuild`. Getting this wrong does not fail — it silently builds at
  xcodebuild's own core-count default, which is exactly the thing this whole
  system exists to prevent. `CFG_BUILDSYS` is what selects between them.
- **`CC`/`CXX` are left unset.** The clang requirement is Linux-specific: it
  exists because GCC fails on aarch64 with `-Werror=volatile-register-var`. On
  macOS the Xcode toolchain is the only option anyway, and forcing `CC=clang`
  would override the SDK's own choice for no benefit.

Products land in `WebKitBuild/<Configuration>`, with an SDK suffix for the
embedded platforms (`Release-iphonesimulator`). `jsc` is at the top of that
directory, not in `bin/`, and resolves its libraries as frameworks — so running
it needs `DYLD_FRAMEWORK_PATH`, not `LD_LIBRARY_PATH`. `wk run` handles this.

## Claude

`wk claude <name>` works, with two caveats that do not apply on Linux:

- **Credentials do not transfer.** On Darwin the CLI keeps them in the login
  Keychain, not in `~/.claude/.credentials.json`, so the shared-secrets volume
  the Linux workspaces use has nothing to share. Expect one `claude login` per
  VM — or log in inside the base before it is shut down, in which case every
  clone inherits it, because the Keychain is part of the disk image.
- **The sandbox is weaker.** See the isolation section. `wk claude` warns.

The CLI is installed by its own installer, to its own path. It self-updates
into `~/.local/share/claude/versions/`, so a binary parked anywhere else can
never update itself.

## Zed

`wk vm start` writes an ssh alias, so:

```sh
zed ssh://wk-mac-rel/Users/admin/WebKit
```

The alias is *rewritten* on every start rather than written once at creation,
because the guest's address changes on each boot. An alias that were only ever
appended would point at whatever address the guest got the first time, and Zed
would sit there timing out.

## Measured

On an M4 (10 cores / 32 GB), guest at 9 vCPU / 20 GB, macOS 26.4 guest with
Xcode 26.5:

| | |
|---|---|
| `tart pull` of the base image | ~2 h (68.8 GB compressed, 87 GB cached) |
| `wk vm base`, dominated by the in-guest WebKit clone | ~1 h |
| `wk new --target vm` | **1 s, ~1 MB of real disk** |
| `wk vm start`, cold boot to ssh | ~10 s |
| `wk build mac-release`, cold, no ccache | **99 min** |
| resulting build tree | 39 GB |
| build log | 66 MB (xcodebuild echoes every command) |

The workspace figure is the one that matters, and it is the whole argument for
the golden base: `du` reports the clone as ~118 GB, the container's free space
moves by a megabyte. That is `clonefile(2)` doing its job. If you ever see
`wk new` take minutes, the clone fell back to a real copy and `t_create` says
so.

## Disk

**The stock image does not have room to build in.** It ships a 140 GB disk of
which the system and Xcode already occupy ~76 GB; a WebKit checkout is ~19 GB
and a Release build tree is 39 GB, so a build dies partway through WebCore.
This is measured — the first real build here ran 38 minutes and then failed
with `No space left on device`, which xcodebuild reported as *"The Xcode build
system has crashed."*

So `wk vm base` sets the disk to `WK_VM_DISK_GB` (250 by default) before the
first boot. Tart can only grow a disk, and only while the VM is off, so this
has to happen there rather than per workspace. An already-built base is fixed
with `wk vm base --refresh`.

Growing the disk is only half of it — the guest's APFS container has to span
the new size too. Recent macOS does that by itself on first boot, in which case
`diskutil apfs resizeContainer` returns error `-69743` ("the new size must be
different than the existing size"), which is success wearing an error's
clothes. Provisioning therefore judges the outcome on free space afterwards
rather than on an exit status, and warns only below a 60 GB floor.

After all that, a guest has ~133 GB free and finishes a Release build with
82 GB to spare.

Everything lives under `~/.tart`, and everything after the first pull is APFS
copy-on-write, so `du` will overstate it badly — count the container's free
space, not the sum of the parts.

- the OCI cache holds the pulled image
- `wk-base` is a clone of it
- each workspace is a clone of `wk-base`

`wk rm` deletes a workspace's guest. `wk gc` does **not** touch any of this — it
is built around the overlay snapshots and runs inside the podman VM. To drop the
OCI cache once the golden base exists (the base does not depend on it staying
around):

```sh
tart prune --space-budget 0     # --entries defaults to "caches", not VMs
```

`--older-than` is the other lever, but it counts days since last *access*, so
it will not touch an image you pulled this week.

## Deliberately absent

- **ccache.** There is no Homebrew here and no signed installer, and the Xcode
  build only uses ccache if `WK_USE_CCACHE=YES` finds one on `PATH`. A macOS
  build is therefore always a real build. Prefer an incremental build in a
  long-lived workspace over recreating one.
- **The egress firewall.** Not possible from outside a macOS guest; see above.
- **A base *snapshot*.** The overlay scheme's `wk sync` mirror is not used
  here — the golden VM is the base — so `wk new --target vm` does not require
  `wk sync` to have run.

## Still open

- The `ios-sim-release` config is written but unexercised. `mac-release` is
  verified end to end; `mac-debug` and `mac-release-asan` differ from it only
  in the flags they pass.
- No JSC-only Apple config. `--only=<scheme>` exists in `build-webkit` and
  would make a much smaller build; the scheme name has not been verified.
- No ccache, so every build is a real build. 99 minutes is the number to plan
  around, and it is the argument for keeping a workspace alive and building
  incrementally rather than recreating one.
- `wk test` has not been run against an Apple port. The command is wired up and
  the Mesa/WPE rendering variables are correctly suppressed for Xcode builds,
  but no suite has actually been executed here.
- Two guests at the default envelope will not both fit on a 32 GB host, and
  neither will one guest beside the podman machine. `wk vm start` refuses with
  the arithmetic; it does not schedule around it for you.
