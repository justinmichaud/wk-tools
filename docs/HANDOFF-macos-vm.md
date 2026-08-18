# Handoff: native macOS builds (Phase 4)

You are picking up `wk-tools` to make the Apple ports buildable — macOS and iOS,
built with Xcode against a real macOS system, not the Linux GTK/WPE ports.

Read `SETUP.md` first for what the system does. This document covers only the
macOS-VM work.

---

## Why this needs a VM at all

macOS has no container primitive for macOS userspace. Apple's `container` CLI —
which is installed on this machine — runs *Linux* containers. So isolation for
an Apple-port build means a virtual machine, and there is no lighter option.

Everything else in `wk-tools` runs Linux workspaces inside a podman VM. This is
a second, parallel kind of target that happens to also be a VM, but shares
nothing with the podman one.

---

## What exists

`targets/vm.sh` and `cmd/vm` are written against **Tart** and have **never been
run**. They implement the same driver contract as `targets/container.sh`
(`t_create`, `t_exec`, `t_destroy`, `t_info`, `t_list`), so once the driver
works, `wk build` and `wk claude` should function unchanged.

`wk vm ls|new|start|stop|enter|rm` are wired up. `WK_VM_IMAGE` defaults to
`ghcr.io/cirruslabs/macos-sequoia-xcode:latest`.

---

## Constraints that are not negotiable

**Apple permits exactly two macOS VMs per host**, and Virtualization.framework
enforces it — a third fails with `VZErrorDomain` code 6. `t_create` counts
running VMs and refuses at the limit rather than letting it fail opaquely
later. Do not remove that check; the failure it prevents is confusing.

**Licence.** Tart moved from `cirruslabs/tart` to `openai/tart` and is
**FSL-1.1-ALv2**, not OSI-open. The user has accepted this on the basis that
full source is available and internal use is a Permitted Purpose — FSL defines
that as "any purpose other than a Competing Use" and explicitly permits internal
use. Building WebKit does not compete with Tart. Each version also converts to
Apache-2.0 two years after release, and OpenAI has stated it stopped charging
licensing fees.

If that ever becomes unacceptable, the researched alternatives, in order:

| | Licence | CLI lifecycle | Unattended install |
|---|---|---|---|
| `lume` | MIT | full | yes — the only FOSS one that closes this gap |
| UTM + `utmctl` | Apache-2.0 | no `create` for macOS guests | no, build the golden image in the GUI once |
| Apple's own sample | MIT | you write it (~500 lines Swift) | no |

Apple's sample code is *"Running macOS in a virtual machine on Apple silicon"*,
verbatim MIT, and needs only the `com.apple.security.virtualization`
entitlement with ad-hoc signing — no paid developer account. Installing from an
IPSW needs no Apple ID.

---

## The work

### 1. Get one VM up

```sh
brew install cirruslabs/cli/tart      # or the released binary
wk vm new mac-rel
wk vm start mac-rel
```

The prebuilt `macos-*-xcode` images carry Xcode already, which sidesteps both
the Setup Assistant and a multi-hour Xcode install. That is the main reason to
prefer Tart initially. Expect the first pull to be very large (tens of GB).

`tart clone` uses APFS copy-on-write, so cloning the prepared image should be
effectively instant. Verify that it actually is — if a clone takes minutes, it
fell back to a real copy and the disk cost multiplies per VM.

### 2. Decide where the source lives

This is the main design decision and it is **not settled**.

The Linux side is built around an overlay: one read-only base snapshot, a
copy-on-write layer per workspace, hardlinked snapshots. None of that exists on
APFS. What APFS does have is `clonefile(2)`, which gives cheap copy-on-write
file clones — a good analogue but not the same shape.

Options, roughly in order of appeal:

1. **APFS clone per VM.** Keep a pristine checkout in the base image, and
   `cp -c` it per workspace inside the guest. Cheap, native, no shared state.
2. **Share the Linux mirror over the network.** The podman VM already holds a
   WebKit mirror at `/var/lib/wk/git`. The macOS VM could clone from it over
   the tailnet. Avoids a second multi-GB fetch, but couples the two VMs.
3. **Independent clone in the guest.** Simplest, most wasteful.

Whatever you choose, keep the property that matters: **creating a workspace must
not require a full clone**, and the base must not be mutated while something is
using it.

### 3. Networking and the sandbox

The Linux workspaces are confined by nftables in the podman VM's root network
namespace. **A macOS guest has no equivalent** you can apply from outside — there
is no cgroup, no shared netns, and pf inside the guest is modifiable by anything
running as root in the guest.

So be honest in the docs about what isolation a macOS VM gives: it is a VM
boundary (the host filesystem is not reachable, which is the main thing) but
**not** a filtered-egress boundary like the Linux workspaces. Either:

- accept that and say so, or
- filter on the host side with pf against the VM's interface, or
- put the guest on the tailnet with a restrictive ACL, accepting that Tailscale
  ACLs "do not affect local network traffic" and so are defence in depth only.

Do not claim the Linux sandbox properties for the macOS VM without implementing
them.

### 4. Build configs

`build/configs.sh` currently has JSCOnly, GTK and WPE. The Apple ports need new
entries — `mac-release`, `mac-debug`, and possibly `ios-release` — driving
`Tools/Scripts/build-webkit --release` with the Xcode toolchain rather than
CMake.

Two things carry over and should not be re-derived:

- **Job count comes from available memory, not core count.** A WebKit link step
  is what turns `-j$(nproc)` into an unresponsive machine. `lib/resources.sh`
  does this; make sure the VM path uses it and sizes from the *guest's* memory.
- **The clang requirement is Linux-specific.** It exists because GCC fails on
  aarch64 with `-Werror=volatile-register-var`. On macOS the Xcode toolchain is
  the only option anyway, so `CFG_CC`/`CFG_CXX` should be left unset there.

### 5. Claude in the VM

`wk claude` should work through the driver contract. Note:

- Claude Code stores credentials in the **macOS Keychain** on Darwin, not in
  `~/.claude/.credentials.json` as on Linux. The shared-credentials-volume trick
  used for Linux workspaces will not transfer; expect a login per VM, or find a
  Keychain-based equivalent.
- Install it with its **own installer, to its own path**. It self-updates into
  `~/.local/share/claude/versions/`, so a binary copied anywhere else can never
  update itself. This was a real mistake here once already.

### 6. Zed

`zed ssh://admin@<ip>/path` should work once `wk vm start` reports an address.
`tart ip` needs the guest to have generated traffic before it resolves, so
expect to poll — `t_start` already does.

---

## Traps carried over from the Linux work

**Never infer build state from a process list.** `pgrep -f "ninja"` matches its
own command line; a build between phases has no compiler running while being
perfectly healthy. Both mistakes were made here and cost real time. Use
`wk status` (exit 0 ok, 1 failed, 2 running, 3 stalled) and `wk logs`. The
watchdog in `lib/watchdog.sh` warns after 300 s of silence and kills after
1800 s — reuse it for VM builds rather than writing another waiting loop.

**`wk rm` must delete everything the target created.** On Linux that includes
the overlay upperdir, which podman does not clean up. Work out the VM equivalent
and make `t_destroy` thorough, or disks fill quietly.

**Measure, do not assume.** The Linux numbers, for comparison: `wk new` ~30 s,
JSC release ~5 min cold / ~3 min warm, WPE full build ~30 min. Record the macOS
equivalents in `SETUP.md` as they land.

---

## Verification

```sh
wk vm new mac-rel && wk vm start mac-rel
wk vm ls                                  # state and address
wk build mac-rel mac-release              # via the driver contract, not by hand
wk status mac-rel                         # exit 0
wk vm rm mac-rel                          # everything reclaimed
```

Then confirm the limit is enforced rather than hit: create two VMs, start both,
and check `wk vm new` on a third refuses with a clear message instead of a
`VZErrorDomain` error.
