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
---

# Building JavaScriptCore / WebKit

When the user asks to build JSC, WebKit, or any WebKit subproject, follow this skill exactly.

Throughout this document, `$WEBKIT_ROOT` refers to the WebKit repository root directory. This CLAUDE.md file lives at `$WEBKIT_ROOT/Source/JavaScriptCore/CLAUDE.md`, so the WebKit root is `../..` relative to this file's directory. Resolve the actual path at runtime (e.g. via `git rev-parse --show-toplevel` or by navigating from the current working directory).

## Step 1: Detect Platform

Run `uname -s` and branch:

- **Darwin** → follow the macOS path below.
- **Anything else** (Linux, etc.) → follow the non-macOS path below.

---

## macOS / iOS (Darwin) — uses `make`

### Pre-build: ASan check

1. Check whether the file `$WEBKIT_ROOT/WebKitBuild/ASan` exists.
2. **If the user asked for ASan** and the file does NOT exist: run `Tools/Scripts/set-webkit-configuration --asan` from `$WEBKIT_ROOT`.
3. **If the user did NOT ask for ASan** and the file DOES exist: warn the user that ASan is currently enabled. Offer to clean the build by removing `$WEBKIT_ROOT/WebKitBuild`. This is destructive — confirm with the user before proceeding. If the user declines, proceed anyway but note the build will be ASan-enabled.

### Run the build

Always run `make` from `$WEBKIT_ROOT`, passing `SCHEME=` explicitly. This avoids CWD-dependent behavior.

| What to build | Command (from `$WEBKIT_ROOT`) |
|---------------|-------------------------------|
| JavaScriptCore only | `make release SCHEME="Everything up to JavaScriptCore"` |
| Full WebKit / Safari | `make release` |
| Another subproject (e.g. WTF) | `make release SCHEME="Everything up to WTF"` |

```bash
cd $WEBKIT_ROOT
make release SCHEME="Everything up to JavaScriptCore"   # or: make debug SCHEME=...
```

- Use `make release` unless the user asks for a debug build.
- If the user says "build" without specifying release/debug, default to **release**.
- The `"Everything up to X"` scheme builds X and all its dependencies (WTF, bmalloc, etc.).

### Build artifacts (macOS)

The macOS `make` build produces a flat framework layout:

| Artifact | Path |
|----------|------|
| JSC shell | `$WEBKIT_ROOT/WebKitBuild/Release/jsc` (or `Debug/jsc`) |
| Test binaries | `$WEBKIT_ROOT/WebKitBuild/Release/testmasm`, `testb3`, `testair` |
| Frameworks | `$WEBKIT_ROOT/WebKitBuild/Release/JavaScriptCore.framework/`, etc. |
| Generated headers | `$WEBKIT_ROOT/WebKitBuild/Release/DerivedSources/JavaScriptCore/` |

To run binaries, set `DYLD_FRAMEWORK_PATH` to the build output directory:
```bash
DYLD_FRAMEWORK_PATH=$WEBKIT_ROOT/WebKitBuild/Release $WEBKIT_ROOT/WebKitBuild/Release/jsc test.js
```

---

## Other Platforms (Linux, etc.) — uses `build-webkit`

Run from `$WEBKIT_ROOT`:

| What to build | Command |
|---------------|---------|
| JavaScriptCore only | `Tools/Scripts/build-webkit --jsc-only --release` |
| Full WebKit | `Tools/Scripts/build-webkit --release` |

- Replace `--release` with `--debug` if the user asks for a debug build.
- The macOS-specific `make` wrapper and ASan file checks do NOT apply here.

### Build artifacts (Linux / CMake)

The CMake build uses a standard `bin/`/`lib/` layout:

| Artifact | Path |
|----------|------|
| JSC shell | `$WEBKIT_ROOT/WebKitBuild/Release/bin/jsc` |
| Test binaries | `$WEBKIT_ROOT/WebKitBuild/Release/bin/testmasm`, `testb3`, `testair` |
| Shared libraries | `$WEBKIT_ROOT/WebKitBuild/Release/lib/` |
| Generated headers | `$WEBKIT_ROOT/WebKitBuild/Release/DerivedSources/JavaScriptCore/` |

---

## Checking Build Results

- **Exit code**: `make` and `build-webkit` return non-zero on failure. Always check the exit code to determine success or failure.
- **On failure**: Read the build output to identify the error. Common causes:
  - Compilation errors — fix the source code and rebuild.
  - Missing generated files — try a clean build (`make clean` then rebuild, or nuclear reset as last resort).
  - Xcode version mismatch (macOS) — check that the selected Xcode version is compatible.
- **On success**: The exit code is 0. Binaries are available at the platform-specific paths listed above.

---

## Nuclear Reset

If the user asks to clean everything or a build is hopelessly broken:

1. **Confirm with the user** — removing `WebKitBuild` deletes all build artifacts, ASan config, and the Configuration file.
2. `rm -rf $WEBKIT_ROOT/WebKitBuild`
3. Rebuild from scratch.

Use only as a last resort.

---

## Quick Reference

| User says | Action |
|-----------|--------|
| "build JSC" / "build JavaScriptCore" | `make release SCHEME="Everything up to JavaScriptCore"` from `$WEBKIT_ROOT` |
| "build JSC debug" | `make debug SCHEME="Everything up to JavaScriptCore"` from `$WEBKIT_ROOT` |
| "build WebKit" / "build Safari" | `make release` from `$WEBKIT_ROOT` (no SCHEME needed) |
| "build with ASan" | Enable ASan config first, then build |
| "clean build" | Remove WebKitBuild, then rebuild |
