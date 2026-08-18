# HANDOFF — macOS MiniBrowser (Lane B step 1)

Original three-line spec, kept verbatim because it is the acceptance test:

> Fix the mac DerivedData location so that it is fast and not interfering with
> other builds.
>
> Support debugging a layout test
>
> Support running minibrowser graphically so that I can interact with it.
> Support attaching a debugger.

Plus one clarification from the user, which changes the shape of the work:

> Minibrowser should work with **full GPU acceleration** and let me interact
> with it in the macOS VM case. For podman, vnc/spice/rdp (or whatever is
> simplest) is fine, since podman can never use GPU acceleration. **Headless
> macOS VMs should never really be used**, we don't need to support that.

---

## Background established by the survey (2026-08-18)

### GPU

Apple's Virtualization.framework gives a **macOS** guest on Apple Silicon a
paravirtualised graphics device backed by the host GPU, so the guest's Metal
stack runs on real hardware. A **Linux** guest gets no GPU device at all — this
is why the podman machine can never be accelerated, as `SETUP.md:364` already
says, and it is permanent rather than a gap to close.

`--no-graphics` does **not** remove the graphics device: the display is part of
the persistent VM config (`tart get wk-mac-rel` → `"Display": "1024x768"`), and
tart's own help tells you to VNC into a `--no-graphics` guest. What the native
window buys is direct presentation of the guest framebuffer; VNC/Screen Sharing
captures and re-encodes every frame, which is fine for poking at a UI and
useless for anything rendering-performance-related. That, not a missing GPU, is
the reason the macOS guest should always run windowed.

### What exists today

- Every guest boots `--no-graphics`: `targets/vm.sh:353` (workspace),
  `targets/vm.sh:731` (base provisioning). There is no other boot path.
- `_create` (`targets/vm.sh:229`) sets `--cpu --memory --random-mac
  --random-serial` and **never `--display`**, so every workspace inherits
  1024x768.
- `cmd/gui` is Linux/WPE-GTK only, in three independent ways: the port gate
  (`cmd/gui:51-56`) rejects any config with an empty `CFG_PORT`, which is every
  `mac-*` config (`build/configs.sh:127-129`); the build check (`cmd/gui:66`)
  wants `$BUILD_DIR/bin/MiniBrowser`, but an Xcode build produces
  `WebKitBuild/Release/MiniBrowser.app`; and the display source
  (`cmd/gui:71-73`) is a host Wayland socket.
- `cmd/session` refuses macOS outright (`cmd/session:47`).
- No debugger support for macOS anywhere. `wk run --lldb`
  (`cmd/run:23,53-54`), `container/lldb/run-file`, `dotfiles/lldbinit` and
  `container/lldb/rr.py` are JSC-shell-only and provisioned only into Linux
  containers (`container/firstrun.sh:195-202`). `vm/provision-base.sh` sets up
  no debug tooling. Zero repo hits for `debugserver`, `codesign`,
  `get-task-allow`, `DevToolsSecurity`, SIP or `task_for_pid`.
- Commands reach a guest over ssh **without a pty**: `t_exec`
  (`targets/vm.sh:382-391`). `t_exec_tty` (`:393-399`) exists but its only
  caller in the whole repo is `cmd/claude:70` — not `cmd/run`, `cmd/test` or
  `cmd/gui`.
- `run-safari`, `debug-minibrowser` and `debug-safari` are referenced nowhere.

### DerivedData — the diagnosis is "never configured", not "wrong path"

There is no `WEBKIT_OUTPUTDIR`, no `-derivedDataPath`, no
`IDECustomDerivedDataLocation` and no `COMPILATION_CACHE_CAS_PATH` anywhere in
the repo. The only mention of DerivedData in the tree is a comment
(`targets/vm.sh:448-452`). Everything falls through to defaults, giving two
roots inside the guest:

| what | where | set by |
|---|---|---|
| products, intermediates, `XCBuildData/build.db`, `PrecompiledHeaders` | `/Users/admin/WebKit/WebKitBuild` | WebKit `webkitdirs.pm:445,460` |
| **DerivedData** — `CompilationCache.noindex`, `ModuleCache.noindex`, `Index.noindex` | `/Users/admin/Library/Developer/Xcode/DerivedData` | nothing; the xcodebuild default |

So the ~10 GB CAS the `vm.sh:449` comment describes is in neither the checkout
nor `WebKitBuild` — it is in the guest user's machine-wide Xcode singleton.

**Collisions, in descending severity:**

1. `mac-release-asan` and `mac-release` resolve to the **identical** output
   directory — `build/configs.sh:78` keys `config_build_dir` only on
   `CFG_TYPE`. Building one after the other silently yields a mixed/relinked
   tree, and `wk run`/`wk bench` cannot tell which one they got. Upstream
   confirms the cause: `webkitdirs.pm:3132-3133`, "Xcode toggles ASan within
   Debug/Release, so its path is unchanged."
2. All four mac configs share one `WebKitBuild/XCBuildData/build.db`, one
   `PrecompiledHeaders`, and one user-wide CAS/ModuleCache/Index. WebKit says
   so itself at `webkitdirs.pm:3149`: "build.db is shared across Xcode
   configurations." Alternating `mac-release` and `mac-debug` in one workspace
   thrashes all of them.
3. The planned macOS **remote** target (`targets/remote.sh:63-70`, and
   `docs/HANDOFF-other-remote.md`) would have every workspace and every other
   user session on the box share one `~/Library/Developer/Xcode/DerivedData`.
   The `flock` at `:68` serialises them but does not separate their caches.
4. Nothing pins the Xcode user defaults that silently relocate output
   (`webkitdirs.pm:411-429` reads `IDEBuildLocationStyle`,
   `IDECustomBuildProductsPath`, `IDEApplicationwideBuildSettings`). If any is
   ever set in a guest, `config_build_dir()` becomes a lie and every consumer
   reports "no MiniBrowser" — the quiet failure `configs.sh:63-70` warns about.

**Slowness has a separate, named cause.** A cold `mac-release` is ~99 min
(`SETUP.md:318`). `--export-compile-commands` is passed unconditionally
(`build/build-in-target.sh:75`), which forces `GCC_PRECOMPILE_PREFIX_HEADER=NO`
— prefix headers off for WebCore/WebKit/JSC — and writes a
`-gen-cdb-fragment-path` JSON fragment per each of ~6,300 TUs. It also poisons
the CAS: different flags mean different cache keys than a plain build.

Secondary: every DerivedData write in a fresh workspace is a copy-on-write
fault, because the base's CAS ships inside the cloned image
(`targets/vm.sh:441-445`) and the first build rewrites those entries.

---

## Work items

Nothing here is done yet. Each line should end up as a `docs/TESTING.md` entry
as it is picked up (`docs/HANDOFF-testing.md`).

### A. DerivedData and build-output separation
- [ ] A1 Give each mac config its own output dir so `mac-release-asan` stops
      colliding with `mac-release` (`build/configs.sh:78`).
- [ ] A2 Place DerivedData explicitly instead of inheriting the machine-wide
      default — per workspace, inside the guest (**not** over virtiofs; a CAS
      on a `--dir` share would be far slower than the problem it solves).
- [ ] A3 Set it from `config_build_env()` (`build/configs.sh:169-183`) so
      `cmd/build` and `_prebuild_base` both inherit it from the one place they
      already share.
- [ ] A4 Keep the CAS path stable across `wk vm base --refresh`.
- [ ] A5 Decide whether `--export-compile-commands` stays unconditional; it is
      the single largest build-time cost and it fragments the CAS.
- [ ] A6 Pin/guard the Xcode user defaults that can silently relocate output.
- [ ] A7 Fix the disk-size disagreement: `targets/vm.sh:115` says 320,
      `SETUP.md:319` says 250.

### B. Graphical, GPU-accelerated MiniBrowser
- [ ] B1 Stop passing `--no-graphics` (`targets/vm.sh:353`, `:731`). Headless
      is not a supported mode for macOS guests.
- [ ] B2 Set a real display size at create time (`targets/vm.sh:229`) —
      `tart set --display`, and consider `--display-refit`.
- [ ] B3 Make `cmd/gui` accept mac configs: port gate, `.app` bundle path
      instead of `bin/MiniBrowser`, and skip the Wayland/Mesa env entirely.
- [ ] B4 **Confirm empirically that WebKit's GPU process gets hardware Metal
      in the guest** rather than falling back. This gates the whole item and is
      a measurement, not a design choice.
- [ ] B5 Decide what `wk gui` does for a podman workspace on a macOS host,
      where there is no Wayland socket and no GPU — VNC/SPICE/RDP, whichever
      is simplest.

### B — measured results (2026-08-18)

**B4 is SATISFIED. The GPU is real.** MiniBrowser runs with hardware Metal:

- WebGL, verbatim: `VENDOR=WebKit | RENDERER=WebKit WebGL | VERSION=WebGL 2.0 |
  UNMASKED_VENDOR=Apple Inc. | UNMASKED_RENDERER=Apple GPU |
  MAX_TEXTURE_SIZE=16384`. WebGL 1 the same; WebGPU present. A software
  fallback would read `Apple Software Renderer`/`llvmpipe`.
- The GPU process has
  `AppleParavirtGPUMetalIOGPUFamily.bundle` and WebKit's own
  `libANGLE-shared.dylib` open, with a populated Metal shader cache.
- All four processes come up: MiniBrowser, GPU, Networking, WebContent.
- Raw Metal, guest vs host: `Apple Paravirtual device` vs `Apple M4`;
  families apple1-**5** vs apple1-**9**; **no metal3, no raytracing** in the
  guest; compute 3.6 vs 5.9 Gthread/s (~61%). **Caveat: the paravirtual device
  is feature-capped.** Good enough to interact with; NOT a sound basis for
  judging WebGPU behaviour or rendering performance against bare metal.

**Launching.** `open -a` does **not** work and must not be used in `wk gui`:
the app's `LSMinimumSystemVersion` is 26.5 (built against the 26.5 SDK) but the
guest runs 26.4, so LaunchServices refuses with `-10825`. Direct exec of the
bundle binary bypasses the check:

    R=/Users/admin/WebKit/WebKitBuild/Release
    DYLD_FRAMEWORK_PATH=$R DYLD_LIBRARY_PATH=$R \
    __XPC_DYLD_FRAMEWORK_PATH=$R __XPC_DYLD_LIBRARY_PATH=$R \
    $R/MiniBrowser.app/Contents/MacOS/MiniBrowser <url>

`Tools/Scripts/run-minibrowser --release <url>` also works, but only through a
**login shell** (it needs the proxy env, see F) and it is python, so it pays
autoinstall. `DISABLE_WEBKITCOREPY_AUTOINSTALLER=1` skips that entirely and is
a good safety net for when the proxy is down.

**Still open in B, with what was measured:**

- [x] B1 `--no-graphics` removed. Confirmed: `tart run` carries no such flag
      and a host window exists, `onscreen=YES`, 1920x1108 at 0,30.
- [~] B2 `WK_VM_DISPLAY` is applied and the VZ display now *offers*
      1024x576 … 3840x2160, including 1920x1080 as mode [8]. **But the guest
      never switches its current mode** — it stays at 1024x768, so MiniBrowser
      gets a 776x608 window. `tart set --display` sets the display's
      capability, not the guest's active mode, and `--display-refit` did not
      change it either, though `tart-guest-agent` is running (daemon + agent).
      A `CGConfigureDisplayWithDisplayMode` to mode [8] returned error 1014
      while the display was asleep. **Needs a guest-side step at provisioning
      time.**
- [ ] B6 **The guest display sleeps and locks itself.** `Display 1 Shield` +
      `loginwindow` cover the screen and `CGDisplayIsAsleep` reports 1, which
      is why a `screencapture` comes back solid black. This persists even
      though `pmset` already reports `displaysleep 0`, `sleep 0`,
      `SleepDisabled 1`, and `com.apple.screensaver idleTime`/`askForPassword`
      were set to 0. `caffeinate -u` wakes it, and it re-sleeps within
      seconds. Auto-login is already enabled (`autoLoginUser = admin`).
      Most likely explanation: nothing generates HID input, because every
      probe so far has been driven over ssh — a human clicking in the window
      may simply not hit this. **Needs confirming with a real click before
      any more automation is built around it.**
- [ ] B7 Guest is macOS 26.4 while the build targets the 26.5 SDK. Worth
      aligning the base image, since it already breaks `open -a`.

### G. The guest must never lock or sleep — RESOLVED (2026-08-18)

User requirement: *"It should never lock or turn off. I don't know the password
to the guest."*

**The guest password is `admin` / `admin`** — the Cirrus Labs image default,
verified with `dscl . -authonly`. It is written down in `vm/provision-base.sh`
now, because a guest nobody can unlock is a guest you get locked out of. It
guards nothing: the VM holds no credentials, its egress is filtered by Softnet
from outside, and `wk rm` destroys it.

Three *independent* mechanisms hide the desktop, and turning off any two is not
enough. This took several boots to separate:

1. **Screen saver** — `com.apple.screensaver idleTime`. Was already 0.
2. **Display sleep** — `pmset`. Already `displaysleep 0`, `sleep 0`,
   `SleepDisabled 1`, and **still slept**, which presents as a solid-black
   `screencapture` and then a lock. Only a held power assertion actually
   worked: a `caffeinate -dimsu` LaunchDaemon (`org.wk.nosleep`, `RunAtLoad` +
   `KeepAlive`). Confirmed holding `PreventUserIdleDisplaySleep` and surviving
   reboot.
3. **The screen lock** — a *separate* setting from either of the above, and the
   one that actually bit. With the screen saver off and `askForPassword`
   already 0, the guest still came up behind "Enter Password" every boot.
   `sudo sysadminctl -screenLock off` is the only thing that touches it.
   FileVault is off and there are no configuration profiles; auto-login was
   already on and working (`admin` was on the console the whole time — the
   session was live *behind* the lock, which is why this was confusing).

After all three: no shield, no loginwindow, `asleep=0`, desktop live.

- [x] G1 `sysadminctl -screenLock off` in provisioning.
- [x] G2 `org.wk.nosleep` caffeinate LaunchDaemon in provisioning.
- [x] G3 Screen saver + `askForPassword` zeroed in provisioning.
- [x] G4 Password recorded, with the reasoning for why that is safe here.

### B2 — RESOLVED: the guest now runs at 1920x1080

`tart set --display WxH` sets what the virtual display is **capable** of, not
the mode the guest selects. The guest was offered every mode from 1024x576 to
3840x2160 and still sat at 1024x768; `--display-refit` is `true` in the VM
config and `tart-guest-agent` is running, and neither changed it.

The fix is a guest-side `CGConfigureDisplayWithDisplayMode` call. It returns an
error against a *sleeping* display (this is why an earlier attempt failed with
1014), so it must run after login with the display awake — hence a LaunchAgent
(`org.wk.display`) rather than a one-shot at provisioning time. Verified:
`set 1920x1080 -> err=0`, and a screenshot shows a full desktop with menu bar,
Dock and MiniBrowser at 1920x1080.

- [x] B2 `wk-set-display` + `org.wk.display` login agent; `WK_VM_DISPLAY` is
      passed from `targets/vm.sh` so the guest and `tart set --display` cannot
      disagree.

### Still open from the user's latest round

- [x] B8 **RESOLVED — and it was self-inflicted.** Tart pins the window's
      *minimum* content size to the configured display resolution
      (`Run.swift`, `.frame(minWidth: config.display.width, ...)`), and AppKit
      swaps `NSWindowCollectionBehavior.fullScreenPrimary` for
      `fullScreenNone` the moment a window's `minSize` exceeds `screen.frame`.
      Setting `WK_VM_DISPLAY` to 1920x1080 -- the host's *own* logical size --
      therefore produced a window that could neither shrink nor go full screen,
      with its resize corner hanging below the bottom of the display. Measured
      threshold on this host: min height 1048 works, 1080 does not.
      **Fix: make the configured display smaller, not bigger** -- now 1280x800.
      `--display-refit` (`VZVirtualMachineView.automaticallyReconfiguresDisplay`)
      then makes the *guest* follow the *window* on every live resize, which is
      why it had appeared to do nothing: it fires on view-size change, and the
      view could never change size. Verified after the change: the window came
      up 1920x960 (inside `visibleFrame`) and the guest tracked it to
      3008x1460 with no manual mode set. Costs nothing in GPU terms -- the
      presentation path is unchanged.
      Upstream treats this as intended: PR #1086 proposed dropping the minimum
      and was rejected (issue #1087, recommending exactly this workaround);
      unchanged as of 2.35.0, the newest release. Also note tart issue #1248:
      `tart set` clears `displayRefit` unless the flag is passed, so
      `t_create` now always passes it.
      Rejected alternative: **VNC**. It keeps guest GPU acceleration (the
      graphics device stays attached) but capture/encode is lossy and
      asynchronous with no vsync-correct pacing, so any MotionMark or
      frame-rate number taken over it is meaningless.

- [ ] B9 **The guest should run the latest macOS** -- by REBUILD ONLY. In-place
      upgrades (`softwareupdate`, `startosinstall`, sideloading an installer)
      are ruled out by policy: the point of this setup is that rebuilding from
      scratch is fast, and an in-place upgrade trades that away for a guest
      nobody can reproduce. So the only lever is `WK_VM_IMAGE` + a base
      rebuild.
      Blocked on upstream: there is **no `macos-tahoe-xcode:26.6`** -- Cirrus
      intended to publish it (it is in their runner matrix) but the release run
      failed. `27-beta-4` does reach macOS 26.6.1 but ships Xcode 27 beta,
      whose 27.0 SDK would make the `-10825` mismatch *worse*.
      `macos-runner:tahoe` has macOS 26.6.1 + Xcode 26.6 but is 520 GB on disk,
      against `WK_VM_DISK_GB=320` and ~149 GB free. Watch for the 26.6 tag, or
      file an issue on `cirruslabs/macos-image-templates`.
      Naming, since it is not obvious: the repo name is the macOS codename and
      the tag is the **Xcode** version; macOS versions appear in tags only on
      the `-vanilla` line. The post-Tahoe codename is "Golden Gate" (macOS 27),
      vanilla only so far.


### B10 — full screen: the launch path, not the window size

Shrinking `WK_VM_DISPLAY` (B8) fixed **resizing** but not full screen: the
green button zoomed instead of entering macOS full screen. The cause turned out
to be unrelated to window geometry.

`_tart_bin()` returned `~/.local/bin/tart`, which is a **symlink** into
`~/.local/share/tart/tart.app/Contents/MacOS/tart`. Launching a bundled app
through a path *outside* its own bundle means LaunchServices never associates
the process with the bundle, and the window is then created without
`NSWindowCollectionBehavior.fullScreenPrimary` — which cannot be restored at
runtime. A window without it still resizes; its green button just falls back to
zoom, which is exactly the reported symptom.

Measured with one identical binary and an identical `minSize`, varying only the
path it was launched by:

| launched via | fullScreenPrimary | fullScreenNone |
|---|---|---|
| `Probe.app/Contents/MacOS/Probe` | **true** | false |
| a symlink to that same file | **false** | true |

This is a consequence of Tart being hand-installed (it must stay inside its
signed bundle for the virtualization entitlement, so a symlink on PATH is the
normal way to reach it) and it would bite any tool that launches it that way.

- [x] B10 `_tart_bin()` now resolves the path through symlinks (`readlink -f`,
      falling back to `python3 os.path.realpath` for older macOS where
      `readlink -f` does not resolve chains). Verified: the running process is
      now `.../tart.app/Contents/MacOS/tart` and the window came up 1920x1080
      at 0,0 -- the full screen frame, with no title-bar offset -- rather than
      1920x960 at 0,30.
- Note for the earlier analysis: Tart's `CommandGroup(replacing: .windowSize)`
  does **not** affect `collectionBehavior`. It only deletes "Enter Full Screen"
  from the Window menu and its Ctrl-Cmd-F shortcut, so the green button is the
  only route to full screen regardless.

### H. Provisioning speed and robustness

Where the ~3 hours actually goes: the cold `mac-release` prebuild dominates at
~99 min; the WebKit clone is ~10-20 min; the OCI pull is free on a rebuild
because the image is cached; everything else is minutes.

- [x] H1 **The prebuild no longer dies with the ssh session.** It used to run
      in the foreground of one `ssh`, so a blip killed it -- and did: the last
      base build ended `client_loop: send disconnect: Broken pipe` with no
      `BUILD SUCCEEDED`, ~90 minutes in, leaving a base that looked built and
      was not. `ServerAliveInterval=60`/`CountMax=10` were already set and did
      not save it. It now runs under `nohup`, writes its exit status to a file
      in the guest, and the host polls with a 10-minute heartbeat. Losing the
      connection now costs one retry, not the build.
- [x] H2 **The checkout is seeded from a host clone when one exists.** A bare
      `git clone --local` on the host hardlinks rather than copies, so making
      the seed is nearly free; rsync then moves it over the vmnet bridge
      instead of pulling from GitHub through the egress proxy. origin is
      re-pointed at GitHub and fetched afterwards, so the result is identical
      to a network clone -- the seed only saves the download. Entirely
      best-effort: no host checkout, or any failure, falls back to the old
      path. `WK_HOST_WEBKIT` overrides the location.
- [ ] H3 **`--export-compile-commands` has never been measured.** `git log -S`
      shows it was added *after* the only timed build, and that build's log
      contains zero occurrences of it -- so **`SETUP.md`'s "~99 min" is the
      cost WITHOUT the flag**, and current cold builds are slower by an unknown
      amount. It cannot be enabled per-target: it sets
      `GCC_PRECOMPILE_PREFIX_HEADER=NO`, and if the base and a workspace
      disagree on build settings the workspace does a full rebuild and the
      golden base is worthless. So it is all-or-nothing. Decision taken: keep
      it and measure the real number on the next rebuild, then correct
      `SETUP.md`.

### I. `wk` does not work inside a workspace

Split out into its own document: **`docs/HANDOFF-wk-in-workspace.md`**, and
escalated — Claude only ever runs inside a workspace, so an in-workspace `wk`
that cannot build or test makes `wk claude` useless on this lane. Summary:
`wk` is present in the guest but every command fails with "podman is required"
(`resolve_target` defaults to `container` when no workspace is named, and the
entrypoint forwards to a podman machine that cannot exist in a macOS guest);
`WK_IN_VM=1` unblocks informational commands but the bare
`wk build <config>` form is still unsupported. The guest has no podman, no
tart and `kern.hv_support = 0`, so it can never host a workspace itself.

### C. Attaching a debugger
- [ ] C1 Route debugger paths through `t_exec_tty`, not `t_exec` — an
      interactive lldb currently gets no pty.
- [ ] C2 Provision WebKit's lldb helpers into the guest
      (`Tools/lldb/lldb_webkit.py` + `dotfiles/lldbinit`), mirroring
      `container/firstrun.sh:195-202`.
- [ ] C3 Establish the macOS prerequisites for attaching to another process —
      `DevToolsSecurity`, `get-task-allow`/codesigning, SIP — and record which
      are actually needed inside a VZ guest.
- [ ] C4 Wire up `debug-minibrowser`/`debug-safari`, or the equivalent.

### D. Debugging a layout test
- [ ] D1 `cmd/test`'s watchdog kills at 1800 s of silence (`cmd/test:119`,
      exit 124) — which is exactly what a breakpoint looks like. It must be
      disabled on a debug run.
- [ ] D2 Single-test debug path: `--child-processes=1`, no timeout, and some
      way to pass a wrapper/env through `run-webkit-tests`.
- [ ] D3 Prewarm processes, PSON and site isolation must not get in the way —
      inherited from `docs/HANDOFF-linux-minibrower.md`, unaddressed on either
      platform.

### F. Guest egress was broken — found while doing B4, fixed
The guest had **no working egress at all**, which is why nothing that touches
the network worked in a macOS workspace. Two independent defects:

1. `targets/vm.sh` passed the raw `$WK_VM_PROXY_ADDR` to `provision-base.sh`.
   That variable is normally *unset* — it is an override, and the real address
   is derived from the bridge by `_proxy_addr()`, which only exists once a
   guest is running. So an empty value was passed, and `provision-base.sh` fell
   back to a hardcoded `192.168.64.1` (Tart's stock vmnet gateway). This repo
   puts guests on `WK_VM_SUBNET` = `192.168.2.x`; the proxy is on
   **192.168.2.1**. The guest was pointed at an address nothing listens on.
2. The `wk-tools: egress` block was **entirely absent** from `wk-mac-rel`'s
   `~/.zprofile` — its profile was the stock Cirrus one (brew, rbenv, node,
   android, flutter) with none of wk's additions, and `claude` was not
   installed either. So provisioning either never ran on the base this
   workspace was cloned from, or failed part way. Worth determining which.

Measured in the guest: no proxy → HTTP 000; `192.168.64.1` → 000;
`192.168.2.1` → **200**. The boundary itself was fine; only the address was
wrong. PyPI is and was already on the allowlist
(`container/proxy/wk-proxy.py:53-57`).

Why this mattered here: `Tools/Scripts/run-minibrowser` is python and imports
`webkitpy`, whose `autoinstall` downloads setuptools/urllib3 from PyPI on first
use. With no egress it died in an import traceback before opening any window —
which read as "the GUI cannot start" rather than "the network is broken".
`build-webkit` is perl and needs none of this, which is exactly why
`wk build mac-release` passed and nothing caught it.

- [x] F1 Pass `$(_proxy_addr)` rather than the raw variable (`targets/vm.sh`).
- [x] F2 Refuse to guess an address in `vm/provision-base.sh` — a wrong proxy
      is silent and looks like the filter working, so fail loudly instead.
- [x] F3 Repair the live `wk-mac-rel` guest in place so verification could
      continue.
- [ ] F4 Work out whether the golden base is missing the provisioning block
      too, and re-provision it if so. Every workspace cloned from it inherits
      the defect.
- [ ] F5 `claude` is not installed in this guest — same root cause. Confirm it
      installs once egress works.
- [ ] F6 webkitpy's autoinstall downloads land in the workspace, not the base,
      so every fresh workspace re-downloads them. Consider warming this during
      base provisioning.
- [ ] F7 **`wk test` against an Apple port has never been run**
      (`docs/TESTING.md` §2) and would have hit this same wall, since
      `run-webkit-tests` is python too. Re-check once egress is fixed.
- [ ] F8 Two `wk-proxy.py` processes were seen running on the host at once
      (one under Homebrew python 3.14, one under Xcode python 3.9). Determine
      whether one is stale.

### E. Documentation debt found on the way
- [ ] E1 `SETUP.md:165` and `SETUP.md:298` reference `docs/macos-vm.md`, which
      was deleted in `8400f49` and does not exist.
