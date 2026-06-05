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

When adding source or header files to a WebKit subproject, multiple build system files must be updated in a coordinated way. The rules vary by project and file type. Follow the instructions below exactly.

## Source Files (.cpp, .c, .mm, .m)

### Step 1: Sources.txt (if project uses unified builds)

- JavaScriptCore, WebCore, and WebKit use `Sources.txt` for unified builds.
- Platform-specific variants exist: `SourcesCocoa.txt`, `SourcesGLib.txt`, etc.
- Sort entries alphabetically within each directory section; blank lines separate directories.
- Annotations:
  - `@no-unify` — file should compile separately (not bundled into a unified source).
  - `@nonARC` — non-ARC Objective-C file.
- **WTF does NOT have Sources.txt** — skip this step for WTF.

### Step 2: Xcode project file (`<Project>.xcodeproj/project.pbxproj`)

- Add a `PBXFileReference` entry with a unique 24-character uppercase hex ID.
- Add the file reference to the correct `PBXGroup` matching the directory structure.
- **If the file is listed in Sources.txt**: do NOT add it to `PBXSourcesBuildPhase` (unified builds handle compilation).
- **If the file is NOT in Sources.txt** (e.g. WTF, or special cases): also add a `PBXBuildFile` entry and list it in `PBXSourcesBuildPhase`.
- Run `Tools/Scripts/sort-Xcode-project-file <path-to-project.pbxproj>` after editing.

### Step 3: CMakeLists.txt (only if NOT in Sources.txt)

- WTF sources must be listed in `CMakeLists.txt` directly.
- For projects using Sources.txt, this step is not needed for source files.

---

## Header Files (.h)

### Step 1: Xcode project file

- Add a `PBXFileReference` entry with a unique 24-character uppercase hex ID.
- Add the file reference to the correct `PBXGroup`.
- Add a `PBXBuildFile` entry in `PBXHeadersBuildPhase` with the correct visibility:
  - **Public**: `settings = {ATTRIBUTES = (Public, ); };` — Official API headers only (e.g., `API/JSValueRef.h`).
  - **Private**: `settings = {ATTRIBUTES = (Private, ); };` — Headers used by other projects in the workspace (most WTF headers, JSC headers needed by WebCore).
  - **Project**: No settings attribute — Internal headers, only used within the project.
- Run `Tools/Scripts/sort-Xcode-project-file <path-to-project.pbxproj>` after editing.

### Step 2: CMakeLists.txt (only for Private headers)

- Add to the `PRIVATE_FRAMEWORK_HEADERS` list (or equivalent section).
- Not needed for Project-visibility headers.

---

## Per-Project Reference

| Project | Sources.txt | Xcode project | CMakeLists.txt (sources) | CMakeLists.txt (headers) |
|---------|-------------|--------------|--------------------------|--------------------------|
| JavaScriptCore | Yes + SourcesCocoa.txt | JavaScriptCore.xcodeproj | Only if not in Sources.txt | Private headers only |
| WebCore | Yes + SourcesCocoa.txt | WebCore.xcodeproj | Only if not in Sources.txt | Private headers only |
| WebKit | Yes | WebKit.xcodeproj | Only if not in Sources.txt | Private headers only |
| WTF | **No** | WTF.xcodeproj | **Yes, always** | Private headers (most are Private) |

---

## Xcode Project ID Generation

Generate unique 24-character uppercase hex strings for each new entry. Use:

```bash
python3 -c "import uuid; print(str(uuid.uuid4()).upper().replace('-','')[:24])"
```

or:

```bash
uuidgen
```

Each `PBXFileReference` and `PBXBuildFile` entry needs its own unique ID.

---

## Post-Edit

Always run after modifying any `.pbxproj` file:

```bash
Tools/Scripts/sort-Xcode-project-file <path-to-project.pbxproj>
```

This sorts all sections of the Xcode project file into a canonical order.
