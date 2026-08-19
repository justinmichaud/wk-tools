# HANDOFF — macOS MiniBrowser (Lane B step 2)

Original three-line spec, kept verbatim because it is the acceptance test:

> Fix the mac DerivedData location so that it is fast and not interfering with
> other builds.
>
> Support debugging a layout test
>
> Support running minibrowser graphically so that I can interact with it.
> Support attaching a debugger.

Status, 2026-08-18: the three lines are done. DerivedData is placed and
separated per config (A); MiniBrowser runs windowed on the guest's desktop with
hardware Metal, launched by `wk gui` (B); a debugger attaches to the browser,
to its web process, and to the web process a layout test runs in (C, D). What
is left is written up below and none of it blocks the spec: the guest is a
macOS release behind because no suitable image is published (B9), and Swift/CAS
debug info does not resolve in lldb (C5). The browser reaching nothing --
found while verifying `wk gui`, because the guest's egress was environment
variables and WebKit does not read those -- is fixed (B11).

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

Each line should end up as a `docs/TESTING.md` entry as it is picked up
(standing rule in `docs/HANDOFF.md`).

### A. DerivedData and build-output separation — DONE 2026-08-18
The code landed in `ace9d87`; the boxes were never ticked. `config_build_env()`
sets `WEBKIT_OUTPUTDIR` and `WK_DERIVED_DATA` for the Xcode configs
(`build/configs.sh`), and `build/build-in-target.sh` turns the second into
`-derivedDataPath`, `COMPILATION_CACHE_CAS_PATH` and `MODULE_CACHE_DIR`.

- [x] A1 Each mac config has its own output dir; `mac-release-asan` no longer
      shares `Release/` with `mac-release`.
- [x] A2 DerivedData is placed explicitly, per workspace, on the guest's own
      APFS volume rather than over virtiofs.
- [x] A3 Set from `config_build_env()`, so `cmd/build` and `_prebuild_base`
      inherit it from the one place they already share.
- [x] A4 Derived from `$src`, which provisioning's `git reset --hard` does not
      touch, so a `wk vm base --refresh` keeps its warm cache.
- [~] A5 `--export-compile-commands` — tracked as H3 below, where the decision
      (keep it, measure it on the next rebuild) is recorded.
- [x] A6 Moot rather than done, which is better: setting `WEBKIT_OUTPUTDIR`
      makes webkitdirs skip its whole `IDEBuildLocationStyle` block
      (`webkitdirs.pm:401-441`), so an Xcode user default can no longer
      relocate the output from under the build.
- [x] A7 Both say 320 GB now.
- [x] A8 **`-derivedDataPath` had to come back out, and finding out why took a
      full rebuild.** It is a flag, not a build setting, so it reached both of
      build-webkit's xcodebuild invocations -- and the second one,
      `Tools/Scripts/build-imagediff`, goes through `buildXCodeProject`
      (`webkitdirs.pm:2399`): `-project`, no `-scheme`. xcodebuild refuses:

          xcodebuild: error: The flag -scheme, -testProductsPath, or
          -xctestrun is required when specifying -derivedDataPath.

      The base prebuild therefore ended `** BUILD SUCCEEDED ** [5149 sec]`,
      then that error, then exit 64 -- and **no ImageDiff**, which every pixel
      and reftest comparison needs. A build that reports failure after
      succeeding is bad; one that silently skips a tool the tests need is worse,
      and both arrived in the same change.

      Dropping the flag costs nothing: products, intermediates and
      `XCBuildData/build.db` already follow `WEBKIT_OUTPUTDIR` through
      SYMROOT/OBJROOT, indexing is off (`COMPILER_INDEX_STORE_ENABLE=NO`), and
      `COMPILATION_CACHE_CAS_PATH` and `MODULE_CACHE_DIR` are named explicitly
      -- they are *settings*, so every xcodebuild in the build inherits them.
      Measured after the change: the CAS is 11 GB in
      `WebKitBuild/DerivedData`, and what still lands in the machine-wide root
      is 24 KB for ImageDiff's own project directory and 13 MB of
      `Index.noindex` plus logs -- nothing that matters, which is the claim the
      whole placement rests on.
- [ ] A9 Small follow-up, no urgency: the refreshed base still carries 3.4 GB
      of `CompilationCache.noindex` and 263 MB of `ModuleCache.noindex` in
      `~/Library/Developer/Xcode/DerivedData` from its pre-A2/A3 builds, dead
      weight that every clone inherits. `wk vm base --rebuild` clears it;
      deleting it during provisioning would too, and costs nothing, since
      anything still wanted is regenerated. Not done here because it only takes
      effect on the next refresh and would need one to verify.

### B. Graphical, GPU-accelerated MiniBrowser
- [ ] B1 Stop passing `--no-graphics` (`targets/vm.sh:353`, `:731`). Headless
      is not a supported mode for macOS guests.
- [ ] B2 Set a real display size at create time (`targets/vm.sh:229`) —
      `tart set --display`, and consider `--display-refit`.
- [x] B3 **DONE 2026-08-18.** `cmd/gui` takes the mac configs: the gate is now
      on `$CFG_BUILDSYS:$CFG_PORT`, the path comes from `config_browser_path`
      (the binary *inside* the bundle), the Wayland/Mesa/BMC block is behind
      `is_linux`, and a bare `wk gui` defaults to `mac-release` on a macOS host.
      Two things had to be measured rather than guessed, and both are recorded
      in `build/configs.sh`: `open -a` is refused (-10825), and the Apple
      MiniBrowser takes its page as `--url` -- a positional URL is parsed by
      nothing, so the window comes up on about:blank and looks broken.
      `wk gui` also works with no workspace name from *inside* a guest, which
      is the only form an agent in there can use.
- [ ] B4 **Confirm empirically that WebKit's GPU process gets hardware Metal
      in the guest** rather than falling back. This gates the whole item and is
      a measurement, not a design choice.
- [x] B5 **Decided: it refuses, and says why.** A container workspace on a
      macOS host has no seat and cannot acquire one -- the podman machine is a
      Linux VM with no GPU (a permanent property, `SETUP.md`), and macOS has no
      Wayland socket to pass in. The refusal lives in the `wk` dispatcher rather
      than in `cmd/gui`, because that is the only place both facts are known:
      forwarded into the podman VM, `is_linux` is true, so `wk gui` failed the
      seat check and advised `wk session on` -- a command that refuses on macOS.
      Wrong advice is worse than none.
      VNC/SPICE is the option not taken. It would mean a compositor and a VNC
      server inside the workspace, installed through the egress allowlist, to
      reach a software-rendered browser -- while the same host has an
      accelerated one a `wk vm new` away, which is what the refusal points at.
      Worth building only if a container-only workspace ever has to be looked
      at; nothing on either lane needs it today.

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

### B11 — the browser reached nothing, and the checks could not see it — FIXED (2026-08-18)

Found while verifying `wk gui` (2026-08-18). Every egress check in
`docs/TESTING.md` was run with curl from a login shell, and the guest's egress
*is* `http_proxy`/`https_proxy` exported in `~/.zprofile` -- nothing else. The
system network proxy is unset (`networksetup -getsecurewebproxy Ethernet` →
`Enabled: No`), and WebKit's network process does not read the environment
variables: a `--url https://webkit.org/` load produces a blank window and **no
entry at all in the host proxy log**, while curl to the same host from the same
guest succeeds.

So a browser in the guest can show `about:blank` and local files, and nothing
else -- which is not "interact with MiniBrowser" in any useful sense.

- [x] B11 `_set_guest_proxy` in `targets/vm.sh`, called from `t_start` beside
      the marker and `.lldbinit`. It sets `-setwebproxy`, `-setsecurewebproxy`
      and `-setproxybypassdomains` on the service carrying the default route --
      derived in the guest rather than named here, since the image ships five
      services (`com.redhat.spice.0`, three `tart-version-*`, `Ethernet`) and
      which is real is the guest's business. `WK_VM_UNFILTERED` turns the
      setting back off, because an unfiltered guest has nothing listening on
      the proxy address. Idempotent by reading the current value back, so a
      boot does not pay three `networksetup` writes it does not need.
      Verified by the proxy log, not by the window -- a blank page and a denied
      page look identical: `allow webkit.org:443`, `allow shynet.webkit.org:443`,
      and the page rendered.
- [ ] B12 Side effect worth knowing: with a system proxy set, macOS's own
      services route through it too, so the proxy log now also carries
      `DENY token.safebrowsing.apple`, `metrics.icloud.com`, `gdmf.apple.com`
      and friends on every guest boot. Harmless -- they are denied, which is
      the point -- but it makes the log noisier, and a future reader should not
      mistake those lines for something the browser did.

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

- [~] B9 **PARKED by decision, 2026-08-18.** Not worth further time: the only
      symptom is `open -a` being refused, nothing in `wk` uses it, and the fix
      is not ours to make. Pick it up if and when a `macos-tahoe-xcode:26.6`
      appears -- the check is one command, below. Everything after this line is
      what was established before it was parked.

      **The guest should run the latest macOS** -- by REBUILD ONLY. In-place
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

      Re-checked 2026-08-18, still blocked: `macos-tahoe-xcode` ends at `26.5`
      (the image we run: Xcode 26.5 on macOS 26.4) plus `27-beta-4`, while
      `macos-tahoe-vanilla` has reached `26.6.1` and `26.6.2` -- newer macOS,
      no Xcode, and installing one needs an Apple ID this environment should
      not hold. One command re-checks it, no tart pull required:

          T=$(curl -s "https://ghcr.io/token?scope=repository:cirruslabs/macos-tahoe-xcode:pull&service=ghcr.io" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
          curl -s -H "Authorization: Bearer $T" https://ghcr.io/v2/cirruslabs/macos-tahoe-xcode/tags/list

      Worth keeping in proportion: the only symptom is `open -a` being refused
      (-10825), and nothing in `wk` uses `open -a` -- `wk gui` execs the binary
      inside the bundle, which is both the working path and the better one for
      a debugger.


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
- [x] H3 **Measured on the 2026-08-18 base rebuild: 5149 s = 85.8 min**, cold,
      -j9, WITH `--export-compile-commands` (the log carries
      `-gen-cdb-fragment-path` and `GCC_PRECOMPILE_PREFIX_HEADER=NO`, so the
      flag was certainly in effect).

      That is *faster* than `SETUP.md`'s ~99 min, which was the run without the
      flag -- so the worry the item was written around, that current cold builds
      are silently much slower, is not supported. It is not a clean A/B either:
      that rebuild also moved the CAS to `WebKitBuild/DerivedData` (empty at the
      start, 11 GB at the end) and built a newer tree, so the delta is not
      attributable to the flag. What can be said is the useful half: a cold
      Apple-port build in this guest costs about an hour and a half with the
      flag on, and nothing suggests it should be turned off. `SETUP.md`'s
      figure should read ~86-99 min rather than a single number.

### I. `wk` does not work inside a workspace — FIXED 2026-08-18

Built on the Linux side and verified in this guest the same day:
**`docs/HANDOFF-wk-in-workspace.md`**. `wk build mac-release`, `wk run` and
`wk test` now work in the guest with no `WK_IN_VM` and no podman error, and the
guest's marker is written by `targets/vm.sh` from the host — so no
re-provisioning and no change to the golden base was needed. Original write-up,
for the record: split out into its own document, and escalated — Claude only ever runs inside a workspace, so an in-workspace `wk`
that cannot build or test makes `wk claude` useless on this lane. Summary:
`wk` is present in the guest but every command fails with "podman is required"
(`resolve_target` defaults to `container` when no workspace is named, and the
entrypoint forwards to a podman machine that cannot exist in a macOS guest);
`WK_IN_VM=1` unblocks informational commands but the bare
`wk build <config>` form is still unsupported. The guest has no podman, no
tart and `kern.hv_support = 0`, so it can never host a workspace itself.

### C. Attaching a debugger — DONE 2026-08-18
- [x] C1 Debugger paths go through `t_exec_tty`: `wk run --lldb`,
      `wk gui --lldb`, `wk test --lldb`. Measured on `mac-release`:
      `wk run --lldb -- -e 'print(6*7)'` stops at entry and prints 42.
- [x] C2 `~/.lldbinit` is written into the guest by `targets/vm.sh` at start,
      next to the workspace marker and for the same reasons -- it names the
      workspace's own checkout, and a guest handed straight to `wk claude`
      would otherwise have no debugger configuration at all. 15 WebKit
      summaries register (`WTF::String`, `JSC::JSValue`, ...) and Xcode's lldb
      (lldb-2100) accepts every setting in `dotfiles/lldbinit` unchanged.
      `container/lldb/rr.py` is left out: rr is Linux-only.
- [x] C3 **Nothing was needed.** The VZ guest reports
      `System Integrity Protection status: disabled` and `Developer mode is
      currently enabled`, so attaching to another process of the same user --
      including the WebContent XPC service -- works with no codesigning, no
      `get-task-allow`, no `DevToolsSecurity` call and no prompt. Measured
      against an ordinary process and against WebContent, symbols resolving in
      both. Worth re-checking on a guest built from a different image: this is
      a property of the Cirrus Labs image, not of Virtualization.framework.
- [x] C4 `debug-minibrowser`/`debug-safari` were not wired up: they are WebKit
      scripts that assume a desktop session and an installed browser, and the
      thing actually wanted is one command per debugging shape. `wk gui --lldb`
      stops in `main` (`main.m:33`) before AppKit, with source; `wk gui --lldb
      web` attaches the web process as it launches.
- [~] C5 **Swift-interop debug info does not resolve.** Resolving one
      breakpoint in the layout-test web process prints 103
      `llvmcas:/... does not exist` warnings and five "Unable to locate module
      needed for external types", naming
      `SwiftExplicitPrecompiledModules/{pal,wtf,Foundation}-*.pcm` and
      `libPAL.a(WTFExtras.o|CryptoKitShim.o)`. What was measured, in order:

      * the `.pcm` files are all on disk (8 MB, 40 MB, ...), so nothing was
        cleaned away by us;
      * with caching on, the compiles carry `-Xclang -fcas-backend -mllvm
        -cas-friendly-debug-info`, and the debug info then refers to its module
        inputs as `llvmcas:/<hash>` rather than as paths;
      * `llvm-cas --print-kind` answers **"unknown object"** for those ids;
      * **and it still does with a CAS built minutes earlier.** A fresh
        workspace off the rebuilt base -- 9.6 GB of CAS in the new
        `WebKitBuild/DerivedData` location, from a build that had just
        finished -- produced the same 103 warnings, the same five missing
        modules, and the *same id*, byte for byte, as the old workspace. So
        this is **not** cache eviction, which is what the first round of
        evidence suggested and what an earlier revision of this file said:
        those objects were never in the compilation cache. They belong to
        another store, and the only candidate in the build is the Swift
        explicit-module pipeline;
      * lldb's `symbols.cas-path` changes nothing either way, and the toolchain
        ships no CAS plugin for `symbols.cas-plugin-path`.

      **What it costs, measured twice on two different builds: nothing for
      C++.** In both runs the breakpoint resolved and hit --
      `WebCore::Document::implicitClose` at `Document.cpp:4323`, with source and
      with WebKit's own summary printing `this`. Only types coming from the
      Swift modules are degraded.

      The lever is `WK_NO_COMPILATION_CACHE=1`, carried through
      `config_build_env()` to `COMPILATION_CACHE_ENABLE_CACHING=NO`
      (`build/build-in-target.sh`): no CAS backend, so nothing in the debug info
      refers to one.
- [x] C6 **The lever works, measured end to end.** Same workspace, same test,
      same breakpoint, rebuilt with `WK_NO_COMPILATION_CACHE=1`:

      | | caching on | caching off |
      |---|---|---|
      | `llvmcas:/... does not exist` | 103 | **0** |
      | "Unable to locate module" | 5 | **0** |
      | breakpoint resolves and hits | yes | yes |
      | cold build | 85.8 min | **68.6 min** |

      The build time is the surprise: dropping the cache made the *cold* build
      17 minutes faster, because a cold build with caching on pays to write
      11 GB of CAS and reads nothing back. The CAS earns its keep on rebuilds,
      not on the first one -- so `WK_NO_COMPILATION_CACHE=1` is a real choice
      for a workspace whose job is debugging, not a penalty box. It stays
      opt-in because the settings differ from the base's, so switching costs one
      full rebuild in either direction.
- [ ] C6 Confirm the lever end to end: one `WK_NO_COMPILATION_CACHE=1` build,
      then the same breakpoint, and count the warnings. Not run here -- it is a
      ~99 min rebuild and nothing on this lane is waiting on it.

### D. Debugging a layout test — DONE 2026-08-18
`wk test --layout --lldb <test>`. The shape is the same as `wk gui --lldb web`:
lldb goes first and waits, the run starts two seconds behind it, and the attach
lands as WebKitTestRunner creates the web process. Anything else races -- by the
time a run has printed enough for a human to type an attach command, the process
to attach to has come and gone.

- [x] D1 A debug run leaves the reporting machinery alone entirely: no
      watchdog (it kills at 1800 s of silence, and a breakpoint is silent by
      definition, so a debugging session would have ended in "TESTS STALLED"),
      no status file, no failure summary. Those exist to make an *unattended*
      run reportable and every one of them is wrong here.
- [x] D2 `--child-processes=1` (one web process to attach to), `--no-timeout`
      (the runner's own per-test timeout is 30 s), and the pause environment
      passed through with `--additional-env-var`. Everything after the flags
      still reaches `run-webkit-tests` untouched, which is the general form of
      "pass a wrapper through": `wk test ... -- --wrapper='...'` works.
      Measured: attach, `continue`, and the run reaches "The test ran as
      expected."
- [x] D3 Not a problem on this port, measured rather than assumed: there is no
      prewarm option in `webkitpy` at all, site isolation is off unless asked
      for (`--site-isolation`), and with `--child-processes=1` exactly one
      WebContent process appears and `--waitfor` catches it. The Linux half is
      still `docs/HANDOFF-linux-minibrower.md`'s.
- [x] D5 A mistyped test path is checked before the terminal is handed to the
      debugger. It is the one input error this flow cannot report on its own:
      the run behind lldb fails into a log nobody is watching while lldb waits
      for a process that will never start, so it presents as a hang.
- [x] D4 The UI process is covered too: `wk test --layout --lldb ui <test>`.
      `--wrapper 'lldb'` is the shape that does *not* work -- WebKitTestRunner's
      stdin and stdout are the test protocol, and a debugger's prompt cannot
      share a pipe webkitpy is talking on. Attaching by name touches no file
      descriptor, which is why the same `--waitfor` mechanism serves both
      halves. Measured: attach at dyld start, `continue`, and the run reaches
      "The test ran as expected." The pause environment is only added in `web`
      mode, since it is the web process that reads it.

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
- [x] F4 **It was, and by more than the egress block.** Measured in the base on
      2026-08-18, after booting it read-only:

      | in the golden base | state |
      |---|---|
      | `wk-tools: egress` block in `~/.zprofile` | **absent** |
      | `~/.claude/{CLAUDE.md,settings.json,hooks,skills}` | **absent** -- only Claude's own `backups/` and `downloads/` |
      | `Tools/Scripts/libraries/autoinstalled` | **absent** (F6) |
      | `claude` CLI | present, 2.1.234 |

      The live `mac-rel` workspace had the egress block only because F3 repaired
      that one guest by hand. A *new* workspace would have come up with no proxy
      environment at all, so `wk test` would have died at webkitpy's autoinstall
      exactly as it did before -- and it would have read as the F1/F2 fix
      regressing. `~/.wk-provisioned` is dated 14:50 that day and the script has
      no early exit on it, so this was not a skipped run: the base was
      provisioned by an older copy of the script, before those blocks existed.
      The marker is a record, not a guard. Fixed by `wk vm base --refresh`,
      which re-runs every block.
- [x] F5 `claude` is installed and runs -- 2.1.234, in the base and in the
      workspace. Its *configuration* was the thing that was missing: see F9.
- [x] F6 Warmed during base provisioning by one `run-webkit-tests --help`.
      7.6 MB in the base, measured; the workspace that had run a real test had
      11 MB. In the golden image the APFS clone makes it free for every
      workspace, instead of each one paying the download at the worst possible
      moment -- its first test run. Best-effort: a cache, not a dependency.

      The first attempt failed, and the reason is worth keeping. It ran with the
      proxy variables *set*, which looked obviously right and was wrong: **the
      base is not a workspace and has the open network.** Softnet's flags are
      passed in `t_start` only, so the base boots on plain vmnet
      (192.168.64.4, gateway .1) while the egress block it writes names
      192.168.2.1 -- the Softnet gateway, which exists only while a filtered
      workspace is running. That block is for the clones. Inside the base it
      points nowhere, and webkitpy reports an unreachable proxy as
      `ValueError: No archives for setuptools-59.8 found`. Clearing the four
      variables for the warm-up fixed it; `curl https://pypi.org` direct from
      the base answers 200, which is the measurement that settles it.

      Worth carrying into the sandbox audit (lane B step 9): the base VM
      provisions with unfiltered egress. Defensible -- it is a host-driven,
      one-shot operation and no agent runs in it -- but it should be a decision
      on the record rather than a detail nobody noticed.
- [x] F7 **`wk test` against an Apple port now runs.** First green run
      2026-08-18: one layout test, 15 s including webkitpy's start-up, with the
      autoinstall reaching PyPI through the proxy (visible in the proxy log).
      This was the prerequisite for D -- debugging a test runner nobody had
      ever run would have conflated two failures.
- [x] F9 **`~/.claude` was never linked in any guest, which is this lane's own
      premise.** `wk claude` starts an agent inside a workspace; in a macOS
      guest that agent had no `CLAUDE.md`, no `settings.json`, no hooks and no
      skills. It had never been told it was in a workspace, what `wk` does for
      it, or that the host filesystem is out of reach -- and the failure is
      silent, because an agent with no instructions still answers.
      `_write_claude_config` in `targets/vm.sh` links the four paths at start,
      beside the marker, as symlinks into `$HOME/wk-tools` exactly as
      provisioning does it: `wk build` re-rsyncs that tree with `--delete` on
      every run, so a copy would go stale the first time this repo changed.
      Verified in `mac-rel`: four symlinks, `CLAUDE.md` reads back, `skills/`
      lists.
- [x] F10 `wk vm base --refresh` could not get past its own key installation:
      `tart ip --wait` answers as soon as the guest has an address, which is
      well before the guest agent listens, so `tart exec` reported "VM is not
      running" and provisioning died -- twice, on a base whose ssh worked
      seconds later. The agent is only needed the *first* time, to get the key
      in; on every later run ssh is both the better question and the one that
      matters, so it is asked first and the agent is the fallback.
- [ ] F8 Two `wk-proxy.py` processes were seen running on the host at once
      (one under Homebrew python 3.14, one under Xcode python 3.9). Determine
      whether one is stale.
      More on this, seen again 2026-08-18: `wk vm start mac-rel` printed
      **"the host egress proxy did not start; the guest will have no egress at
      all"** while a perfectly good proxy from an earlier session was already
      listening on 192.168.2.1:3128 — the new one died with `EADDRINUSE`. The
      cause is that `_proxy_running` trusts `$WK_VM_DIR/proxy.pid`, which goes
      stale when the proxy outlives the shell that recorded it, so `wk` tries to
      bind an address it already owns and then reports the opposite of the
      truth. Egress in the guest was fine throughout. Check the listener, not
      just the pidfile.
      **FIXED 2026-08-18**, after hitting it a third time: `_proxy_running`
      now falls back to asking whether anything is listening on
      `$(_proxy_addr):$WK_VM_PROXY_PORT` when the pidfile names nothing alive.
      Confirmed against the live host, where the listening proxy (pid 53775)
      and the pidfile (77371) disagreed.

### E. Documentation debt found on the way
- [x] E1 SETUP.md referenced `docs/macos-vm.md`, deleted in `8400f49` — fixed
      2026-08-18: the tart install and licence note are inlined in SETUP.md
      section 8, and every other reference is retargeted.
