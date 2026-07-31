---
name: build-webkit
description: Use when building JavaScriptCore, WebKit, or any WebKit subproject. Handles platform detection, ASan configuration checks, correct working directory, and build commands.
user-invocable: true
allowed-tools:
  - Bash(make release:*)
  - Bash(make debug:*)
  - Bash(make clean:*)
  - Bash(set-webkit-configuration:*)
  - Bash(Tools/Scripts/set-webkit-configuration:*)
  - Bash(Tools/Scripts/build-webkit:*)
  - Bash(pwd:*)
  - Bash(cat:*)
  - Bash(ls:*)
  - Bash(rm -rf:*)
  - Bash(uname:*)
  - Bash(git rev-parse:*)
  - Bash(wkdev-enter:*)
---

# Building JavaScriptCore / WebKit

Every command runs from `$WEBKIT_ROOT`, the repository root. Resolve it with `git rev-parse --show-toplevel`. Default to a **release** build and the **JavaScriptCore** target unless the user says otherwise; swap `release` for `debug` when they ask.

## Parallelism: size `-j` to memory, and always `nice` the build

**A WebKit build is a memory hog, not just a CPU hog, and it usually shares the machine with the
user's desktop.** WebCore/WebKit unified sources routinely take **3-6 GB of RSS per `cc1plus`**, so the
core count is the wrong budget: `-j32` on an 80-core / 125 GB host was measured at 28-29 concurrent
compilers, 76 GB resident, and 3 GB of swap in use, with load pinned at 30-32 for hours. Cores were
free the whole time; memory and interactive responsiveness were not.

- **Budget `-j` from RAM at ~4-5 GB per job, then cap by cores** — the same rule the `jsc` skill applies
  to `run-jsc-stress-tests -c N`. It is the same box and the same failure mode; do not apply it only to
  test workers. Leave headroom for the desktop: on a 125 GB workstation that is roughly `-j16`, not `-j32`.
- **Always `nice -n 10` the build.** Job count protects memory; niceness is what keeps the user's editor,
  browser, and LSP responsive while 16+ compilers saturate their cores. It costs the build almost
  nothing because the build is throughput-bound, not latency-bound.
- **Ask before taking a big share of a machine you know is in use**, and say what you intend to take.
  Check first: `ps -eo pcpu,comm --sort=-pcpu | head` showing firefox/an editor/clangd means someone is
  working on it. Several multi-hour builds back to back is a much bigger ask than one.

```bash
nice -n 10 Tools/Scripts/build-webkit --gtk --release -j16   # shared workstation default
```

Pick the platform with `uname -s`: **Darwin** is macOS and uses `make`; **anything else** (Linux) uses `Tools/Scripts/build-webkit`. The `make` wrapper and ASan file checks apply to macOS only.

## macOS (Darwin) — `make`

```bash
make release SCHEME="Everything up to JavaScriptCore"   # JSC only (debug: make debug)
make release SCHEME="Everything up to WTF"              # another subproject
make release                                            # full WebKit / Safari, no SCHEME
```

`"Everything up to X"` builds X plus its dependencies (WTF, bmalloc, etc.). Pass `SCHEME=` explicitly so behavior does not depend on the current directory.

**ASan check before building** (looks for the marker file `$WEBKIT_ROOT/WebKitBuild/ASan`):
- User asked for ASan, marker absent: run `Tools/Scripts/set-webkit-configuration --asan` first.
- User did not ask for ASan, marker present: warn ASan is enabled. Offer to clean by removing `$WEBKIT_ROOT/WebKitBuild` (destructive — confirm first). If declined, build anyway and note it will be ASan-enabled.

Run binaries with `DYLD_FRAMEWORK_PATH` set to the build output:
```bash
DYLD_FRAMEWORK_PATH=$WEBKIT_ROOT/WebKitBuild/Release $WEBKIT_ROOT/WebKitBuild/Release/jsc test.js
```

Artifacts (flat framework layout):
| Artifact | Path |
|----------|------|
| JSC shell | `$WEBKIT_ROOT/WebKitBuild/Release/jsc` (or `Debug/jsc`) |
| Test binaries | `$WEBKIT_ROOT/WebKitBuild/Release/{testmasm,testb3,testair}` |
| Frameworks | `$WEBKIT_ROOT/WebKitBuild/Release/JavaScriptCore.framework/`, etc. |
| Generated headers | `$WEBKIT_ROOT/WebKitBuild/Release/DerivedSources/JavaScriptCore/` |

## Linux — `Tools/Scripts/build-webkit`

```bash
Tools/Scripts/build-webkit --jsc-only --release   # JSC only (debug: --debug)
Tools/Scripts/build-webkit --release              # full WebKit
```

Artifacts (standard `bin/`/`lib/` layout):
| Artifact | Path |
|----------|------|
| JSC shell | `$WEBKIT_ROOT/WebKitBuild/Release/bin/jsc` |
| Test binaries | `$WEBKIT_ROOT/WebKitBuild/Release/bin/{testmasm,testb3,testair}` |
| Shared libraries | `$WEBKIT_ROOT/WebKitBuild/Release/lib/` |
| Generated headers | `$WEBKIT_ROOT/WebKitBuild/Release/DerivedSources/JavaScriptCore/` |

A JSCOnly `bin/jsc` runs in place — it links its sibling `lib/` via **RPATH**, so just
`"$DIR/bin/jsc" …`, no env var. Because that is RPATH (not RUNPATH), `LD_LIBRARY_PATH` does **not**
redirect it to a different `libJavaScriptCore.so`; to run an alternate or saved lib, copy it over the
in-place file (back it up first).

### 32-bit ARMv7 (wkdev32 container)

Build and run 32-bit inside the container: `wkdev-enter --name wkdev32 --exec -- bash -lc '<cmd>'`
(interactive: `wkdev-enter --name wkdev32`). In-container `/home/<u>/Development` maps to host
`/home/<u>/Development/32/Development`. The build is `linux32 Tools/Scripts/build-webkit --jsc-only
--release --no-unified-builds` with `-march=armv7-a -mthumb -mfpu=neon -mfloat-abi=hard` in
`CMAKE_{C,CXX}_FLAGS`, `-DCMAKE_BUILD_TYPE=RelWithDebInfo`, and `-DUSE_LD_LLD=OFF`. The full recipe is
saved at `WebKitBuild/build-jsc-32.sh` (incremental ~11-17 min); output is
`WebKitBuild/JSCOnly/Release/bin/jsc`.

When a 32-bit build/link fails or the JIT comes up disabled, verify the target arch first — this is
ARM32 (ARMv7/Thumb-2), not x86 or `linux32`; assuming x86 sends the diagnosis the wrong way. A common
real cause of a disabled JIT is **Thumb-2 detection failing at CMake configure time** (check the
configure output), not a bug in the code.

## Checking results

`make` and `build-webkit` return 0 on success, non-zero on failure — always check the exit code. On failure, read the output and fix the cause:
- Compilation error: fix the source and rebuild.
- Missing generated files: clean and rebuild (`make clean`, then rebuild).
- Xcode version mismatch (macOS): confirm the selected Xcode is compatible.

## Nuclear reset

Last resort, when a build is hopelessly broken. Removing `WebKitBuild` deletes all artifacts, the ASan config, and the Configuration file — confirm with the user first, then:
```bash
rm -rf $WEBKIT_ROOT/WebKitBuild
```
Then rebuild from scratch.
