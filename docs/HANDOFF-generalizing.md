# HANDOFF — generalizing beyond one person's WebKit setup

**Status: decided (2026-08-20).** Research done, and the user took all three
decisions the same day: the three-layer decomposition and its dependency rule
are in force for new code; the command names are the unix-y slate —
`home` / `lab` / `wk` / `field` / `stock`; and the Galician naming idea is
scrapped entirely — no Galician name at any level, the suite stays wk-tools.
The original brainstorm prompt this file held is answered section by section
below. No CLI is minted until a layer has a second consumer, and the
mechanical split itself belongs with `docs/HANDOFF-architecture-review.md`,
which already owns "find the seams", and does not start before it.

Companions: `docs/HANDOFF-vocabulary.md` (roles/modes/systems/fleet — decided)
and `docs/HANDOFF-architecture-review.md`.

---

## 1. What other contributors actually do (the research)

Three sweeps, 2026-08-20: WebKit contributors, Chromium/ChromeOS, and
LLVM + Linux kernel + board-farm tooling. Condensed to what bears on this
repo; every claim carries its source.

### WebKit

- **The ecosystem converged on exactly this repo's model, one iteration at a
  time.** JHBuild → Flatpak SDK (reproducible but too sealed for IDEs/rr) →
  podman container SDK, now the *officially recommended* GTK/WPE environment
  ("closely matches the CI/CD testing setup"). Disposable parallel containers
  are a first-class feature there too.
  https://base-art.net/Articles/introducing-the-webkit-flatpak-sdk/ ·
  https://blog.tingping.se/2024/05/23/Introducing-WebKit-Container-SDK.html ·
  https://docs.webkit.org/Getting%20Started/ContributingCode.html
- **One script directory is the universal interface.** Every port, every OS:
  `Tools/Scripts/{build-webkit, run-webkit-tests, run-benchmark, git-webkit}`.
  Personal tooling that wraps these survives; tooling that replaces them
  bit-rots. (Already this repo's stated principle; the research confirms it is
  everyone's.) https://docs.webkit.org/
- **Cross-compilation is never hand-rolled** — it is a Yocto layer
  (`Igalia/meta-webkit`, with a PerformanceTips wiki beside the recipes) or a
  Buildroot overlay (`Igalia/buildroot-wpe`).
  https://github.com/Igalia/meta-webkit/wiki/RPi
- **Igalia already runs the board-farm perf model this repo is building**:
  `browserperfdash` (bots POST results to a Django dashboard) plus
  `browserperfrunner` — run-benchmark's driving code *extracted from the
  checkout* so an embedded board doesn't need a WebKit tree, with netdata
  system-metrics captured beside the run on the RPi bots.
  https://github.com/Igalia/browserperfdash ·
  https://github.com/Igalia/browserperfrunner
- **Windows is a genuinely different environment, documented natively**:
  Visual Studio + CMake/Ninja via Chocolatey/WinGet, Developer Mode for
  symlinks, prebuilt WebKitRequirements, XAMPP for http tests — no container
  SDK. Sony cross-compiles from Windows to PS4 with clang and mirrors
  build.webkit.org's BuildBot internally.
  https://docs.webkit.org/Ports/WindowsPort.html ·
  https://fujii.github.io/2019/07/05/webkit-on-windows/ ·
  https://trac.webkit.org/wiki/PlayStationWebKitPortUpdate2019
- **EWS does the multi-platform matrix so individuals don't.** Contributors
  build one port locally; ports invest in adding *themselves to EWS* rather
  than in local multi-platform setups.
  https://trac.webkit.org/wiki/EarlyWarningSystem
- **mya — corrected.** Not crash-report ingestion: it is Mark Lam's (Apple,
  GitHub `MenloDorian`) "MemorY Analyzer" — attach to a live JSC/WebKit
  process by pid, take read-only Mach corpse snapshots, query them from a
  REPL; reusable parts in a new `libJavaScriptCoreTools`. Apple-platforms-only
  by design (Mach APIs), PR still open as of 2026-08-20.
  https://github.com/WebKit/WebKit/pull/71194
  The *field crash* story for the Linux ports is Breakpad/crash dumps out of
  WPE (Lauro Moura's writeup) — a different tool for a different layer.
  https://dev.to/lauromoura/using-breakpad-to-generate-crash-dumps-with-wpe-webkit-5da8

### Chromium / ChromeOS

- **Stable wrapper, replaceable engine.** `autoninja` survived three
  generations of build acceleration (goma → reclient → siso) without changing
  what anyone types. Put the abstraction at the CLI verb, not in docs.
  (`wk build` already is this; keep it that way through any split.)
  https://groups.google.com/a/chromium.org/g/chromium-dev/c/v-WOvWUtOpg
- **Pinpoint is the perf-as-a-service contract to copy**: the client submits
  *(patch, device config, benchmark, story)* — never binaries, never numbers.
  The service builds both arms, interleaves them **on the same physical
  device**, repeats until statistically significant, returns a comparison.
  https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/speed/perf_trybots.md
- **Bisection over archived prebuilt binaries** (`bisect-builds.py`): a
  regression range narrowed with zero compiles, the tool only asking "did it
  repro?". Cheap to imitate for anyone who archives per-commit builds.
  https://www.chromium.org/developers/bisect-builds-py/
- **Per-board SDK on demand, not build-the-world** (Simple Chrome):
  `cros chrome-sdk --board=X` materializes toolchain+sysroot+VM for one
  board; `deploy_chrome` rsyncs onto the device. The full chroot exists but is
  not the daily path.
  https://chromium.googlesource.com/chromiumos/docs/+/master/simple_chrome_workflow.md
- **Downstream forks that survive**: Brave keeps 100% of its delta as
  overrides + `.patch` files applied by script, never edits upstream files;
  Igalia's downstream series (José Dapena Paz) adds *measure the delta
  continuously, upstream aggressively, rebase on a fixed cadence keyed to
  upstream's stabilization branches*.
  https://github.com/brave/brave-browser/wiki/Patching-Chromium ·
  https://blogs.igalia.com/dape/2024/09/13/maintaining-chromium-downstream-update-strategies/
- **Docs live in-tree and are the contract** — every workflow is a versioned
  markdown updated in the same CL that changes it, which is why external
  "my setup" blogs are sparse: the official docs are current.

### LLVM / kernel / board farms

- **labgrid's vocabulary is the field-tested version of what
  `docs/HANDOFF-vocabulary.md` decided**: *resource* (passive: a serial port,
  a power port) / *driver* (implements protocols over resources) / *target*
  (the board, assembled) / *place* (the logical slot, matched by pattern,
  **acquired/released** with an exclusive lock) / *role* (the name test code
  binds to, resolved at run time). Coordinator is control-plane only; data
  flows client↔board directly. LAVA adds the batch half: *device type*
  (class defaults) vs *device dictionary* (per-unit overrides), and **health
  checks** that auto-offline a sick board before it eats a job.
  https://labgrid.readthedocs.io/en/latest/overview.html ·
  https://validation.linaro.org/static/docs/v2/glossary.html
- **Local engine / cloud service with identical semantics** (tuxmake →
  tuxsuite): the build definition (arch × toolchain × config, executed in a
  versioned toolchain container) is the portable artifact; laptop vs farm is a
  deployment detail. https://docs.tuxmake.org/
- **The minimal perf-service API that could possibly work**
  (llvm-compile-time-tracker): register your fork once; afterwards *pushing a
  branch named `perf/*` is the entire client*. No CLI, no tokens.
  https://llvm-compile-time-tracker.com/about.php
- **Unsolicited external QA with auto-bisect** (Intel 0-day): the service
  watches trees and mails you a bisected culprit; the contributor's cost is
  zero. https://github.com/intel/lkp-tests/blob/master/doc/faq.md
- **Community hardware, central config-as-code** (llvm-zorg, KernelCI):
  machines are owned by individuals; what runs on them is a PR-reviewed file
  in a shared repo, with a staging tier before production.
  https://llvm.org/docs/HowToAddABuilder.html
- **Every board-farm writeup lands on netboot as the inner loop** — TFTP
  kernel + network root, power-cycle via PDU, flash only what must persist.
  (Independent confirmation of the netboot-first decision in
  `docs/HANDOFF-netboot.md`.)
  https://deferred.io/posts/2014/01/16/board-farm-pt1-tftpboot.html
- **The environment is a pinned container; personal config is an overlay the
  tool composes** (tuxmake toolchain images, kas include chains, `repo` local
  manifests, nix flakes). Base owned by the project, overlay owned by you —
  exactly the `targets/hosts/*.conf` + `~/.config/wk/targets/*.conf` split
  this repo already made, so keep that shape as the extension mechanism
  everywhere. https://kas.readthedocs.io/en/latest/userguide/project-configuration.html

---

## 2. The users and use cases

Seven personas. Each is a test the design has to pass, not a promise to build
for them now.

| # | who | what they have | what breaks today |
|---|---|---|---|
| P0 | me | Linux + macOS workstations, Pi fleet, home infra | nothing — the baseline |
| P1 | Windows-port contributor | one Windows box, Visual Studio, no podman | the workspace model assumes overlayfs/containers |
| P2 | Apple-platforms-only contributor | a Mac, Tart guests; iOS devices later | nothing structural — lane B is this persona |
| P3 | remote-only developer | a laptop; every real machine is elsewhere | half-true already (the remote driver was built *from* the Mac); some commands still assume a local container runtime |
| P4 | new contributor handed an environment | one VPS, no fleet, no history with the repo | no one-command "stock" onboarding; provisioning assumes my tailnet |
| P5 | downstream-port maintainer, 5 years out | mips on WPE 2.22, maybe no hardware | must be config + at most one driver file, zero core edits |
| P6 | perf-service consumer | no boards at all; submits runs somewhere | `wk bench` knows only its own fleet |

What each one demands:

- **P1 (Windows).** The port's official environment is native (VS + Ninja via
  WinGet, prebuilt WebKitRequirements) — there is no container SDK to wrap. So
  the *workspace* contract must not require a container: a workspace is "a
  checkout + a build environment + an isolation level", and on Windows the
  isolation level is honestly `none` (like `remote` today). The driver
  contract already admits this; what has to give is any code that equates
  workspace with overlay mount. WSL2 is a second, separate answer (a Linux
  workspace that happens to sit on a Windows host) — worth having, not a
  substitute for the native port.
- **P2 (Apple, then iOS).** iOS slots into the *fleet* vocabulary with no new
  words: an iPhone is `role=bench-device`, arming=`hands-on` at first
  (exactly tolken's model), later `devicectl`-driven; its "mode transition"
  is install-and-launch rather than boot-a-system. A run records the same
  provenance axes. That the vocabulary absorbs a device class it was not
  designed for is the best evidence it generalizes.
- **P3 (remote-only).** Everything must work when *no* target is local. The
  audit line: no command may touch podman/Tart except through a driver, and
  `wk status` must degrade gracefully when the only targets are remote. Cloud
  VPS rental (HANDOFF-other-remote.md) is this persona plus provisioning.
- **P4 (handed a sandboxed environment).** One command from zero to "a
  sandboxed workspace with an agent in it" on a machine that has never seen
  the fleet: stock config, no personal keys, push off by default, egress
  allowlist on. This is the real meaning of "SandboxKit" — see §3.
- **P5 (downstream, mips, WPE 2.22).** The extension test, concretely: add
  `machines/<board>.conf` (or a qemu-system stanza when there is no board),
  a builder pin (yocto/buildroot at the downstream branch — the environment
  *is* a pinned container, per tuxmake/kas), and a branch-keyed build config.
  If any of those steps is "edit a case statement in core", the design has
  failed — this is the same finding as the vocabulary handoff's "the registry
  is a case statement" item, promoted to a design rule.
- **P6 (perf service).** `wk bench` grows a **backend** seam: today's backend
  is "my fleet" (stage → boot → run → collect); a service backend submits
  *(build/patch, device config, plan)* and polls. Pinpoint defines the
  contract (same-device A/B, service owns repetition and statistics);
  compile-time-tracker defines the floor for how small the client can be.
  Both backends must return the same result JSON + provenance axes so
  `wk bench compare` never learns which one produced a number.

---

## 3. The decomposition — five names, three real layers

The five kits from the brainstorm survive, but they are not five peer
codebases. They are **three layers plus a profile plus a data consumer**, and
the load-bearing decision is the dependency rule:

```
LifeKit (personal overlay)      config only — nothing may import it
        ↓ provides facts (machines, reachability)
project kits: WebKitKit=wk, chromium…, llvm…   ← AnalysisKit consumes these
        ↓ drive targets through the contract
TargetKit (the lab layer)       knows machines, boards, images, runs
        ↓
(nothing)
```

- **TargetKit (`lab`) — the lab layer.** The target driver contract
  (container/vm/remote/local, plus a conformance test — already an
  architecture-review candidate), fleet/roles/modes, sysimage build/flash,
  boot/provision/unprovision, quiesce/session, cross builds and qemu, and
  bench *mechanics*: stage a payload, run it quiesced, record provenance.
  **It must not know what WebKit is.** The forcing function is already on the
  books: a chrome-vs-webkit comparison is two payloads on one lab, so the
  mechanics have to take "a payload + a driver" — which is also exactly what
  `browserperfrunner` proved by extracting run-benchmark's driving code from
  the checkout. Rigid on purpose; extension is a conf file, never a fork.
- **WebKitKit (`wk`) — a project kit.** Checkout/workspace semantics, build
  configs, test/run/debug/profile as thin wrappers over `Tools/Scripts`
  (wrap, never replace — the one pattern every ecosystem agrees on), bench
  *plans* and their class/runner axes, git/PR conventions, claude skills.
  Later siblings (chromium, llvm, a side project) are new project kits that
  reuse the lab layer untouched; each is also how the workflows get
  "documented and tested automatically on every system" — the chromium
  lesson: the doc lives in-tree and changes in the same commit as the
  workflow, which here means `wk help` + TESTING.md lines stay mandatory.
- **LifeKit (`home`) — the personal overlay.** Home topology, tailnet-bridge/BMC,
  router, home services. It *provides facts* — registry entries, how to reach
  things — as config the layers below consume, and no code below may import
  it. It is the only layer allowed to be non-portable, and the natural
  candidate to split into its own repo eventually (it is the part with no
  second user).
- **SandboxKit (`stock`) — a profile, not a fifth codebase.** "As close as possible to
  stock, without any fuss" is an *environment profile* of the lab layer: a
  workspace seeded from the stock SDK with no personal config, no fleet
  reach, push off — plus one onboarding command for P4. Every ecosystem
  studied treats "pristine" as a pinned image you instantiate, not a separate
  tool. Debian tarball builds and maintainer-patch testing are consumers of
  the profile; the tarball recipe itself is project-kit material (it is a
  WebKit build).
- **AnalysisKit (`field`) — a data consumer, kept thin.** Field crash dumps
  (Breakpad/crashpad for the Linux ports, per Moura's writeup; debian crash
  dumps later), perf-dashboard pulls, symbolization — it needs the project
  kit for symbols and builds, and nothing needs it. The mya lesson cuts both
  ways: mya itself is Apple-only, in-tree, and upstream — so this kit wraps
  and drives upstream analysis tools, and anything generically useful it
  grows should be upstreamed rather than carried (the same rule as
  HANDOFF-architecture-review.md's upstreaming lens).

The two "undecided" items from the brainstorm, resolved by the layering:
**debian tarballs** = project-kit recipe run in the stock profile;
**chrome-vs-webkit perf** = lab-layer bench mechanics + one adapter per
project kit; the comparison is a run matrix, not a new tool.

**How the split actually lands** (the growing-organically part): first as
directories plus an *enforced* dependency rule — a selftest that greps the
lab layer for WebKit knowledge the way `host/dotfiles.sh` already refuses
hand-written fleet stanzas — inside this one repo. One repo, several entry
points, is the depot_tools shape, and it avoids the cross-repo version skew
this repo already fights every time it pushes copies of itself to build
machines. Separate CLIs split out only when a second consumer actually
exists; LifeKit splits out when the repo gains a second *user*.

---

## 4. Naming

Requirements that fall out of the personas: short to type, ascii, memorable,
unambiguous against the decided vocabulary (role/mode/system/fleet), usable
by someone who is not me (P4), and able to grow organically.

Three slates:

| layer | A. descriptive | B. unix-y | C. Galician |
|---|---|---|---|
| personal overlay | lifekit | `home` | `casa` (home) |
| lab layer | targetkit | `lab` | `vigo` (the port city, shipyards) |
| WebKit project kit | wk | `wk` | `wk` |
| analysis | analysiskit | `field` | `faro` (lighthouse — watches the sea) |
| stock profile / onboarding | sandboxkit | `stock` | `illa` (island — isolation) |

- **Slate A** is honest and self-describing but long, and "-Kit" collides
  with WebKit's own naming (WebKitKit is the tell).
- **Slate B** is what the research favors: `lab` is literally what the board-
  farm world calls this layer (labgrid, LAVA, "hardware labs" in KernelCI),
  and `stock` names the property the profile guarantees. Collisions exist
  (`lab` is also a GitLab CLI) but none on the machines this repo manages.
- **Slate C** keeps the Igalia/Galicia identity, all ascii, 2–5 letters, each
  semantically apt — `illa` for an isolated environment and `faro` for field
  monitoring are genuinely good. Cost: opaque to a stranger until told, which
  is exactly P4.

**Decided (2026-08-20): slate B, and the Galician idea is scrapped entirely**
— no Galician name at any level, the suite stays `wk-tools`. So the words are
`home` (personal overlay), `lab` (the lab layer), `wk` (unchanged), `field`
(analysis), `stock` (the pristine profile). They are recorded in
`docs/HANDOFF-vocabulary.md` beside the role/mode/system/fleet words. No
command is minted before its layer has a second consumer — the split is
directories and a dependency rule first, entry points later.

---

## 5. What to do, in order

1. **Decided 2026-08-20:** the three-layer decomposition, the dependency rule
   (lab layer knows no WebKit; nothing imports the personal overlay),
   SandboxKit-as-profile, AnalysisKit-kept-thin, and the extension mechanism
   (shared conf + personal overlay, everywhere). New code written from today
   respects the boundary even though nothing moves yet.
2. **Decided 2026-08-20:** names picked (§4) and recorded in
   `docs/HANDOFF-vocabulary.md`, which is where words live.
3. **With the architecture review** (`docs/HANDOFF-architecture-review.md`,
   still last): enforce the rule — directory moves, the grep selftest, the
   driver-contract conformance test it already lists — and re-check the
   review's abstraction candidates against this layering.
4. **When the first external persona is real** (P4 hand-off or P6 service,
   whichever comes first): build the stock profile / onboarding command, and
   give `wk bench` its backend seam. Until then they are design constraints,
   not code.
5. **When a second project kit is real:** split entry points; not before.
