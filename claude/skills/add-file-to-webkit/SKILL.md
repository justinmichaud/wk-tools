---
name: add-file-to-webkit
description: Use when adding source files (.cpp, .c, .mm, .m) or header files (.h) to any WebKit subproject (JavaScriptCore, WebCore, WTF, WebKit). Covers Sources.txt, Xcode project file, and CMakeLists.txt updates.
user-invocable: true
allowed-tools:
  - Bash(Tools/Scripts/sort-Xcode-project-file:*)
  - Bash(python3 -c *uuid*)
  - Bash(uuidgen:*)
---

# Adding Files to WebKit Projects

Adding a file means updating several build-system files in a coordinated way; the exact set depends on the project and file type. Which files to touch is driven by one question: **is the file listed in a `Sources.txt`?** If yes, unified builds compile it and CMake/Xcode-sources entries are skipped; if no (all of WTF, plus special cases), it must be listed directly.

## Source Files (.cpp, .c, .mm, .m)

### Step 1: Sources.txt (JavaScriptCore, WebCore, WebKit; skip for WTF)

Add the file to the platform-appropriate `Sources.txt` (`SourcesCocoa.txt`, `SourcesGLib.txt`, ...), sorted alphabetically within its directory section, blank lines separating directories:

```
runtime/JSCConfig.cpp
runtime/JSCJSValue.cpp
runtime/JSCallee.cpp @no-unify        # compile separately, not bundled into a unified source
```

Use `@nonARC` on a non-ARC Objective-C file.

### Step 2: Xcode project file (`<Project>.xcodeproj/project.pbxproj`)

Add a `PBXFileReference` with a unique 24-char uppercase hex ID (see [ID generation](#id-generation)) and place the reference in the `PBXGroup` matching the file's directory. Then, keyed on Step 1:

- File **is** in a `Sources.txt`: stop here. Unified builds compile it; do not add it to `PBXSourcesBuildPhase`.
- File is **not** in a `Sources.txt` (WTF, or a `@no-unify` special case): also add a `PBXBuildFile` entry and list it in `PBXSourcesBuildPhase`.

Run `Tools/Scripts/sort-Xcode-project-file <path-to-project.pbxproj>` (see [Post-edit](#post-edit)).

### Step 3: CMakeLists.txt (only when the file is not in a Sources.txt)

WTF sources go directly in `CMakeLists.txt`. For a project using `Sources.txt`, its unified build already covers the source, so nothing to add here.

## Header Files (.h)

### Step 1: Xcode project file

Add a `PBXFileReference` with a unique 24-char uppercase hex ID, place the reference in the matching `PBXGroup`, and add a `PBXBuildFile` in `PBXHeadersBuildPhase` with visibility matching the header's audience:

```
settings = {ATTRIBUTES = (Public, ); };    # official API only, e.g. API/JSValueRef.h
settings = {ATTRIBUTES = (Private, ); };   # used by other workspace projects (most WTF; JSC headers WebCore needs)
                                           # internal (Project): omit the settings attribute entirely
```

Run `Tools/Scripts/sort-Xcode-project-file <path-to-project.pbxproj>` (see [Post-edit](#post-edit)).

### Step 2: CMakeLists.txt (Private headers only)

Add a Private header to the `PRIVATE_FRAMEWORK_HEADERS` list (or equivalent section). Project-visibility headers need nothing here.

## Per-Project Reference

| Project | Sources.txt | Xcode project | CMakeLists.txt (sources) | CMakeLists.txt (headers) |
|---------|-------------|--------------|--------------------------|--------------------------|
| JavaScriptCore | Yes + SourcesCocoa.txt | JavaScriptCore.xcodeproj | Only if not in Sources.txt | Private headers only |
| WebCore | Yes + SourcesCocoa.txt | WebCore.xcodeproj | Only if not in Sources.txt | Private headers only |
| WebKit | Yes | WebKit.xcodeproj | Only if not in Sources.txt | Private headers only |
| WTF | **No** | WTF.xcodeproj | **Yes, always** | Private headers (most are Private) |

## ID generation

Every `PBXFileReference` and `PBXBuildFile` entry needs its own unique 24-char uppercase hex ID:

```bash
python3 -c "import uuid; print(str(uuid.uuid4()).upper().replace('-','')[:24])"   # or: uuidgen
```

## Post-edit

After modifying any `.pbxproj`, sort it into canonical order:

```bash
Tools/Scripts/sort-Xcode-project-file <path-to-project.pbxproj>
```
