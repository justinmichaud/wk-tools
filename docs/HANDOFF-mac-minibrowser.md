# HANDOFF — macOS MiniBrowser (Lane B step 2) — DONE 2026-08-18

Original three-line spec, kept verbatim because it is the acceptance test:

> Fix the mac DerivedData location so that it is fast and not interfering with
> other builds.
>
> Support debugging a layout test
>
> Support running minibrowser graphically so that I can interact with it.
> Support attaching a debugger.

All three lines are done, plus one clarification from the user that shaped the
work: MiniBrowser must run with **full GPU acceleration** in the macOS VM case;
headless macOS VMs are not a supported mode; container workspaces may fall back
to VNC-class access "or whatever is simplest" — and in the end got a refusal
instead (see "GPU" below).

What remains is small and none of it blocks the spec:

- **A9** — the golden base still carries 3.4 GB of `CompilationCache.noindex`
  and 263 MB of `ModuleCache.noindex` in `~/Library/Developer/Xcode/DerivedData`
  from builds that predate the explicit placement; dead weight every clone
  inherits. Deleting it during provisioning costs nothing (anything wanted is
  regenerated) but only takes effect on the next refresh, which is why it was
  not done here.
- **B9 — parked by decision, 2026-08-18.** The guest runs macOS 26.4 while the
  build targets the 26.5 SDK. The only symptom is `open -a` being refused
  (-10825), and nothing in `wk` uses `open -a` — `wk gui` execs the binary
  inside the bundle, which is both the working path and the better one for a
  debugger. Upgrades are by REBUILD ONLY (`WK_VM_IMAGE` + a base rebuild);
  in-place upgrades are ruled out by policy because a reproducible base is the
  point. Blocked upstream: there is no `macos-tahoe-xcode:26.6` (re-checked
  2026-08-18), `27-beta-4` ships an Xcode whose SDK makes the mismatch worse,
  and `macos-runner:tahoe` is 520 GB against `WK_VM_DISK_GB=320` and ~149 GB
  free on this host. One command re-checks, no tart pull required:

      T=$(curl -s "https://ghcr.io/token?scope=repository:cirruslabs/macos-tahoe-xcode:pull&service=ghcr.io" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
      curl -s -H "Authorization: Bearer $T" https://ghcr.io/v2/cirruslabs/macos-tahoe-xcode/tags/list

Everything below is the decision record: what was chosen, why, and the
measurements that settled it. The pre-fix survey and the per-checkbox narrative
are collapsed; `docs/TESTING.md` §2 carries the line items.

---

## DerivedData and build output — placed explicitly, per config

The diagnosis was "never configured", not "wrong path": nothing in the repo set
`WEBKIT_OUTPUTDIR` or any DerivedData location, so products fell to
`WebKitBuild` and the ~10 GB compilation cache to the guest user's machine-wide
`~/Library/Developer/Xcode/DerivedData` singleton. Worst consequence:
`mac-release-asan` and `mac-release` resolved to the **identical** output
directory (upstream keys the path only on Debug/Release —
`webkitdirs.pm`: "Xcode toggles ASan within Debug/Release, so its path is
unchanged"), so building one after the other silently yielded a mixed tree. All
four mac configs also shared one `XCBuildData/build.db` and one user-wide
CAS/ModuleCache.

The fix (landed 2026-08-18): `config_build_env()` in `build/configs.sh` sets
`WEBKIT_OUTPUTDIR` and `WK_DERIVED_DATA` per config, derived from `$src` (which
provisioning's `git reset --hard` does not touch, so a base refresh keeps its
warm cache), and `build/build-in-target.sh` turns the second into
`COMPILATION_CACHE_CAS_PATH` and `MODULE_CACHE_DIR`. Setting `WEBKIT_OUTPUTDIR`
also makes webkitdirs skip its whole `IDEBuildLocationStyle` block, so an Xcode
user default can no longer relocate the output from under the build.

**`-derivedDataPath` is deliberately NOT used, and finding out why took a full
rebuild (A8).** It is a flag, not a build setting, so it reached both of
build-webkit's xcodebuild invocations — and the second one,
`Tools/Scripts/build-imagediff`, passes `-project` with no `-scheme`, which
xcodebuild refuses when `-derivedDataPath` is present. The base prebuild
therefore ended `** BUILD SUCCEEDED **`, then that error, then exit 64 — and
**no ImageDiff**, which every pixel and reftest comparison needs. Dropping the
flag costs nothing: products and `build.db` already follow `WEBKIT_OUTPUTDIR`
through SYMROOT/OBJROOT, and the CAS and module cache are named explicitly as
*settings*, which every xcodebuild in the build inherits. Measured after the
change: 11 GB of CAS in `WebKitBuild/DerivedData`, and what still lands in the
machine-wide root is 24 KB plus 13 MB of `Index.noindex` and logs — nothing
that matters, which is the claim the whole placement rests on.

## The compilation cache and Swift-interop debug info (C5/C6)

With caching on, compiles carry `-fcas-backend -cas-friendly-debug-info` and
the debug info refers to Swift explicit-module inputs as `llvmcas:/<hash>` ids
that lldb cannot resolve — 103 `llvmcas:/... does not exist` warnings and five
"Unable to locate module needed for external types" per breakpoint resolve in
the web process. Measured twice, on two builds, including a CAS minutes old
with the *same* missing ids byte for byte: those objects were never in the
compilation cache; they belong to the Swift explicit-module pipeline. **What it
costs: nothing for C++** — breakpoints resolve and hit with source and
summaries; only types from the Swift modules are degraded.

The lever is `WK_NO_COMPILATION_CACHE=1`, carried through `config_build_env()`
to `COMPILATION_CACHE_ENABLE_CACHING=NO`. Measured end to end:

| | caching on | caching off |
|---|---|---|
| `llvmcas:/... does not exist` | 103 | **0** |
| "Unable to locate module" | 5 | **0** |
| breakpoint resolves and hits | yes | yes |
| cold build | 85.8 min | **68.6 min** |

The build time is the surprise: a cold build with caching on pays to write
11 GB of CAS and reads nothing back — the CAS earns its keep on rebuilds, not
the first one. So the flag is a real choice for a workspace whose job is
debugging, not a penalty box. It stays opt-in because the settings differ from
the base's, so switching costs one full rebuild in either direction.

## GPU, and where a browser can run at all

Virtualization.framework gives a macOS guest a paravirtualised graphics device
backed by the host GPU; a Linux guest gets no GPU device at all, which is why
the podman machine can never be accelerated (permanent, per `SETUP.md`).

**The guest's GPU is real but feature-capped** (measured 2026-08-18): WebGL
reports `UNMASKED_RENDERER=Apple GPU` (a software fallback would say
`Apple Software Renderer`), the GPU process has the paravirt Metal bundle open,
and all four processes come up. But raw Metal shows `Apple Paravirtual device`
vs `Apple M4`, families apple1-**5** vs apple1-**9**, no metal3, no raytracing,
~61% compute throughput. Good enough to interact with; **not** a sound basis
for judging WebGPU behaviour or rendering performance against bare metal.

**Launching**: `open -a` is refused (-10825, the B9 SDK/OS mismatch), so
`wk gui` execs the binary inside the bundle, which bypasses the
LaunchServices check:

    R=/Users/admin/WebKit/WebKitBuild/Release
    DYLD_FRAMEWORK_PATH=$R DYLD_LIBRARY_PATH=$R \
    __XPC_DYLD_FRAMEWORK_PATH=$R __XPC_DYLD_LIBRARY_PATH=$R \
    $R/MiniBrowser.app/Contents/MacOS/MiniBrowser <url>

The Apple MiniBrowser takes its page as `--url` — a positional URL is parsed by
nothing, so the window comes up on about:blank and looks broken.
`Tools/Scripts/run-minibrowser --release <url>` also works but needs a login
shell (the proxy env) and pays webkitpy autoinstall;
`DISABLE_WEBKITCOREPY_AUTOINSTALLER=1` skips that and is the safety net for
when the proxy is down.

`cmd/gui` gates on `$CFG_BUILDSYS:$CFG_PORT`, takes the path from
`config_browser_path`, keeps the Wayland/Mesa block behind `is_linux`, defaults
to `mac-release` on a macOS host, and works with no workspace name from inside
a guest — the only form an agent in there can use.

**A container workspace on a macOS host refuses `wk gui`, and says why.** It
has no seat and cannot acquire one — no GPU in the podman VM, no Wayland socket
on macOS. The refusal lives in the `wk` dispatcher, the only place both facts
are known; before that it advised `wk session on`, a command that refuses on
macOS, and wrong advice is worse than none. VNC/SPICE into the container was
considered and not taken: a compositor plus VNC server installed through the
egress allowlist to reach a software-rendered browser, while the same host has
an accelerated one a `wk vm new` away. Worth building only if a container-only
workspace ever has to be looked at.

## The window and the display

Everything here is a property of Tart plus AppKit, learned by measurement:

- **Configure the display smaller, not bigger.** Tart pins the window's
  *minimum* content size to the configured display resolution, and AppKit swaps
  `fullScreenPrimary` for `fullScreenNone` the moment `minSize` exceeds the
  screen — so `WK_VM_DISPLAY` at the host's own logical size produced a window
  that could neither shrink nor go full screen. Upstream treats the minimum as
  intended (tart PR #1086 rejected). The default is now **1280x800**;
  `--display-refit` then makes the guest track the window on every live resize.
  Tart issue #1248: `tart set` clears `displayRefit` unless the flag is passed,
  so `t_create` always passes it. VNC was rejected for the same reason as
  above, plus: capture/encode is lossy with no vsync-correct pacing, so any
  frame-rate number taken over it is meaningless.
- **The guest never selects a mode by itself.** `tart set --display` sets what
  the display is *capable* of, and `CGConfigureDisplayWithDisplayMode` fails
  against a sleeping display — so the mode is set by a guest-side helper
  (`wk-set-display`) run from a login LaunchAgent (`org.wk.display`), after
  login with the display awake. `WK_VM_DISPLAY` is passed from `targets/vm.sh`
  so the two ends cannot disagree.
- **Launch tart through its real bundle path.** `_tart_bin()` resolves symlinks
  (`readlink -f`, python fallback): launching a bundled app through a path
  outside its bundle means LaunchServices never associates the process with the
  bundle, and the window is created without `fullScreenPrimary` — it still
  resizes, but the green button falls back to zoom. Measured with one binary,
  varying only the launch path. This would bite any tool that launches a
  hand-installed Tart through the usual symlink on PATH.

## The guest must never lock or sleep (G)

User requirement: *"It should never lock or turn off. I don't know the password
to the guest."* **The password is `admin` / `admin`** — the Cirrus Labs image
default, verified and written down in `vm/provision-base.sh`, because a guest
nobody can unlock is a guest you get locked out of. It guards nothing: the VM
holds no credentials, its egress is filtered by Softnet from outside, and
`wk rm` destroys it.

Three *independent* mechanisms hide the desktop, and turning off any two is not
enough — this took several boots to separate. Provisioning now does all three:
the screen saver and `askForPassword` zeroed; display sleep held off by an
actual power assertion (a `caffeinate -dimsu` LaunchDaemon, `org.wk.nosleep` —
`pmset displaysleep 0`/`sleep 0`/`SleepDisabled 1` were all already set and it
**still slept**); and the screen lock, a separate setting that only
`sysadminctl -screenLock off` touches (auto-login was on and the session was
live *behind* the lock the whole time, which is why this was confusing).

## Egress: the browser does not read proxy environment variables (B11, F)

The guest's egress used to be `http_proxy`/`https_proxy` in `~/.zprofile` —
which curl reads and WebKit's network process does not, so every egress check
passed while the browser could load nothing, with **no entry at all in the host
proxy log**. A blank page and a denied page look identical; the proxy log is
what verification trusts.

- `_set_guest_proxy` in `targets/vm.sh`, called from `t_start` beside the
  marker and `.lldbinit`, sets the **system** proxy (`-setwebproxy`,
  `-setsecurewebproxy`, `-setproxybypassdomains`) on the service carrying the
  default route — derived in the guest, since which of its five services is
  real is the guest's business. Idempotent by reading the value back.
  `WK_VM_UNFILTERED` turns it off, because an unfiltered guest has nothing
  listening at the proxy address.
- Side effect worth knowing (B12): with a system proxy set, macOS's own
  services route through it too, so the proxy log carries
  `DENY token.safebrowsing.apple` and friends on every boot. Harmless — they
  are denied, which is the point — but do not mistake those lines for something
  the browser did.
- Two prior defects in the same area, both fixed: `targets/vm.sh` passed the
  raw (normally unset) `$WK_VM_PROXY_ADDR` override instead of the derived
  `$(_proxy_addr)`, and `provision-base.sh` fell back to guessing an address —
  it now refuses to guess, because a wrong proxy is silent and looks like the
  filter working.
- `_proxy_running` checks the **listener**, not just the pidfile: the pidfile
  goes stale when the proxy outlives the shell that recorded it, and `wk` then
  reported "the proxy did not start" while a perfectly good one held the port.
- **For the sandbox audit (lane B step 9):** the base VM provisions with
  unfiltered egress — Softnet's flags are passed in `t_start` only, so the base
  boots on plain vmnet and the egress block it writes is for the clones.
  Defensible (host-driven, one-shot, no agent runs in it), but it should be a
  decision on the record. Found via webkitpy's autoinstall warm-up, which
  reports an unreachable proxy as `ValueError: No archives for setuptools-59.8
  found`.

## Attaching a debugger (C, D)

Three commands, one per debugging shape, all through `t_exec_tty`:

- `wk run <ws> --lldb` — jsc, stopped at entry.
- `wk gui --lldb` (alias `--lldb ui`) — the browser under lldb, stopped in
  `main` before AppKit, with source. `wk gui --lldb web` — the browser running
  normally, lldb attached to the web process as it launches.
- `wk test --layout --lldb <test>` — the web process a layout test runs in;
  `wk test --layout --lldb ui <test>` — WebKitTestRunner itself.

The shape that works is **attach by name with `--waitfor`, debugger first, run
two seconds behind**: by the time a run has printed enough for a human to type
an attach command, the process has come and gone. `--wrapper 'lldb'` cannot
work for WebKitTestRunner because its stdin and stdout *are* the test protocol;
attaching by name touches no file descriptor, which is why one mechanism serves
both halves. A debug run also leaves the reporting machinery alone — no
watchdog (a breakpoint is silent by definition, so it would end in "TESTS
STALLED"), no status file, no failure summary — and a mistyped test path is
checked before the terminal is handed to lldb, because the failure otherwise
presents as a hang. `--child-processes=1`, `--no-timeout` and the pause
environment are passed for the layout case; the pause env only in `web` mode,
since it is the web process that reads it. Prewarm/PSON/site isolation are not
a problem on this port, measured rather than assumed: webkitpy has no prewarm
option, site isolation is off unless asked for, and with one child process
exactly one WebContent appears.

**No codesigning work was needed**: the VZ guest has SIP disabled and developer
mode on, so same-user attach — WebContent XPC service included — works with no
`get-task-allow`, no `DevToolsSecurity`, no prompt. A property of the Cirrus
Labs image, not of Virtualization.framework; re-check on a guest built from a
different image.

`~/.lldbinit` is written into the guest by `targets/vm.sh` at start, beside the
workspace marker: it names the workspace's own checkout, and a guest handed
straight to `wk claude` would otherwise have no debugger configuration. All 15
WebKit summaries register; `container/lldb/rr.py` is left out (rr is
Linux-only).

## Provisioning: what the base guarantees, and what it costs

- **The prebuild survives its driver.** It runs under `nohup` in the guest,
  writes its exit status to a file, and the host polls with a heartbeat —
  because it used to run in the foreground of one ssh and a blip killed it at
  ~90 minutes, leaving a base that looked built and was not. Load-bearing in
  practice: the 2026-08-20 rebuild had its driving process killed three times
  and the in-guest build carried on each time.
- **The base has a completion marker, written last** — same protocol as an
  image manifest or a snapshot sha. Before it existed, an unprovisioned base
  (no Xcode licence, no checkout, no prebuild) was adopted as "ready" in half a
  second by the next run, and every `wk vm new` would have cloned it.
- **`~/.wk-provisioned` is a record, not a guard** (F4): the base once carried
  a dated marker while missing the egress block, the `~/.claude` links and the
  warmed autoinstall — it had been provisioned by an older copy of the script,
  before those blocks existed. That is why `wk vm base --refresh` re-runs every
  block rather than trusting the marker.
- **The checkout is seeded from a host clone when one exists**
  (`WK_HOST_WEBKIT` overrides): `git clone --local` hardlinks, rsync moves it
  over the vmnet bridge, origin is re-pointed at GitHub and fetched — identical
  result, minus the download. Best-effort; any failure falls back to the
  network clone.
- **Job sizing:** the Apple build derives its job count at 3072 MB/job, not the
  CMake figure — `-j9` from 1536 MB/job peaked at 16.6 GB and the watchdog
  killed the prebuild 95% through; `-j6` finishes at nearly the same peak (it
  is one link, not parallelism — the *budget* was the operative fix). And the
  memory check no longer counts a running guest against itself, which had made
  `wk vm base --refresh` impossible on a live base.
- **`wk vm base --refresh` asks ssh first, guest agent second**: `tart ip
  --wait` answers before the agent listens, so agent-first provisioning died on
  a base whose ssh worked seconds later. The agent is only needed the first
  time, to get the key in.
- **`~/.claude` is linked at guest start** (F9 — `_write_claude_config` in
  `targets/vm.sh`) — CLAUDE.md, settings, hooks, skills, as symlinks into
  `$HOME/wk-tools` exactly as provisioning does it. Before this, an agent in a
  macOS guest had no instructions at all, and the failure is silent because an
  agent with no instructions still answers. Written at start rather than baked
  into the base because `wk build` re-rsyncs the tree on every run.
- **webkitpy's autoinstall is warmed in the base** (F6 — one `run-webkit-tests
  --help`, 7.6 MB) so clones do not pay the download at the worst possible
  moment — their first test run. The warm-up must run with the proxy variables
  *cleared*: the base has the open network, and the egress block it writes is
  for the clones.
- **Cost, measured:** a cold `mac-release` prebuild is 68.6–85.8 min depending
  on the compilation cache (see C6); `--export-compile-commands` stays on — the
  worry that it made builds much slower is not supported by measurement (H3).

## Dated records

**2026-08-18 — the lane itself.** DerivedData placed and separated per config;
MiniBrowser windowed with hardware Metal via `wk gui`; debugger attach three
ways; `wk test` against an Apple port ran green for the first time on the way
(the prerequisite for D — debugging a runner nobody had run would have
conflated two failures). The `-derivedDataPath`/ImageDiff defect (above) was
found and fixed the same day.

**2026-08-20 — golden base rebuilt** to rehearse the benchmark lane; four
defects, all latent since the last rebuild and none visible from a base that
already existed: the unfinished-base adoption, the job-count/budget sizing, the
memory check counting a guest against itself, and the prebuild's independence
from its driver being what saved the rebuild three times. All four are recorded
above where they now govern behavior, and in `docs/TESTING.md`.

**Documentation debt (E)**: SETUP.md's reference to the deleted
`docs/macos-vm.md` fixed 2026-08-18 — the tart install and licence note are
inlined in SETUP.md section 8.
