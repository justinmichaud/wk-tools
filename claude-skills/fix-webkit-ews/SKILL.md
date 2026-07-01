---
name: fix-webkit-ews
description: Use when the user asks to fix WebKit EWS issues, look at red EWS bots, address pre-commit-queue or build failures on a WebKit PR, or otherwise diagnose and fix failing checks on a https://github.com/WebKit/WebKit pull request created from the current branch.
user-invocable: true
allowed-tools:
  - Bash(gh pr view:*)
  - Bash(gh pr checks:*)
  - Bash(curl:*)
  - Bash(python3 -c *)
  - Bash(git log:*)
  - Bash(git diff:*)
  - Bash(git status:*)
---

# Fix WebKit EWS Issues

**Before doing anything else, invoke the `jsc` skill** (skip only if it is already loaded this conversation). Every fix lands in WebKit and is bound by its house rules — comment style, smart-pointer/Safer-CPP discipline, `protect()` vs `NODELETE`, `testLoopCount`, no-commit-without-authorization. Loading `jsc` first keeps you from "fixing" a safer-cpp warning with a NODELETE annotation that doesn't hold, or papering over a style error against the comment guide.

The workflow: triage fast, fix what's real, summarize the rest cleanly. **Most red EWS bots are not the PR's fault** — infra hiccups (jhbuild storage, worker timeouts, flaky/pre-existing tests) waste the user's time.

With several red bots, triage them **in parallel — one subagent per bot** — each reproducing and classifying its own failure and returning a one-line verdict plus evidence; you merge the results. That keeps each bot's logs out of your main context.

> **HARD CONSTRAINT — read-from-EWS and edit-locally only.** Apply fixes to the working tree and stop. Committing, pushing, amending, and posting are the user's job: never run `git commit`, `git push`, `git commit --amend`, `gh pr comment`, `gh pr edit`, or anything that publishes, and never POST/PATCH via `gh api` or `curl`. **Never draft a commit message, PR description, or comment — a firm boundary with no exceptions.** Never search for or read credentials or tokens. If a push or comment is warranted, say so in one sentence and let the user write and send it.

> **HARD CONSTRAINT — prove "unrelated" before claiming it.** Assume every failure is PR-caused until evidence says otherwise. To bucket one as infra/pre-existing/flaky, cite proof: either (a) another PR or a main-branch/post-landing build with the *same* error, or (b) the same test/bot passing on a re-run of this PR or a documented flaky history. Quote the build/PR URL or log line. No proof means dig deeper.

## Step 1: Identify the PR

```bash
gh pr view --json number,title,url,state,headRefName
```

If there is no PR for the current branch, stop and tell the user.

## Step 2: List check statuses

```bash
gh pr checks <pr-number>
```

Each failing row has a URL like `https://ews-build.webkit.org/#/builders/<BUILDER_ID>/builds/<BUILD_NUMBER>`. That number is the per-builder build number. Query the build through the builder:

```
https://ews-build.webkit.org/api/v2/builders/<BUILDER_ID>/builds/<BUILD_NUMBER>
```

`/api/v2/builds/<BUILD_NUMBER>` uses a different (global) id and returns an unrelated build.

## Step 2.5: Sweep in-progress builds — `gh pr checks` under-reports

`gh pr checks` / `statusCheckRollup` show only bots that have already posted a GitHub status; EWS runs many more, and the slow Linux ones (GTK, WPE) may still be compiling. A bot that has reached the `compile-webkit-without-change` step has **already failed `compile-webkit` with the PR** and is rebuilding without it to confirm fault. That "retrying without changes" signal is a failure-in-progress — diagnose it now rather than waiting for red.

Sweep all in-progress builds for this PR and find their current step:

```bash
# List in-progress builds belonging to this PR:
curl -s "https://ews-build.webkit.org/api/v2/builds?complete=false&limit=200&order=-started_at&property=github.number&property=buildername" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
for b in d.get('builds',[]):
    props=b.get('properties') or {}
    num=(props.get('github.number') or [None])[0]
    name=(props.get('buildername') or [None])[0]
    if str(num)=='<PR_NUMBER>':
        print(b.get('builderid'), name, 'build#', b.get('number'))
"

# For each, look at the latest step + whether it has hit the without-change retry:
curl -s "https://ews-build.webkit.org/api/v2/builders/<BUILDER_ID>/builds/<BUILD_NUMBER>/steps" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); s=d.get('steps',[]); print(s[-1].get('name'),'=>',s[-1].get('state_string')); print('HAS_RETRY' if any('without' in (x.get('name') or '') for x in s) else '')"
```

Any bot showing `compile-webkit-without-change` (HAS_RETRY) has a real with-change compile failure — fetch its `compile-webkit` log (the *first* compile step, not the without-change one) per Step 4 and diagnose it. Bots still on their first `compile-webkit` are not yet proven failures, but if they share a toolchain with a bot you already diagnosed (GTK/WPE all use the same wkdev SDK as gtk3-libwebrtc), predict the same failure and confirm your fix covers them.

## Step 3: For each failing bot, find the failing step

```bash
curl -s "https://ews-build.webkit.org/api/v2/builders/<BUILDER_ID>/builds/<BUILD_NUMBER>/steps" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(s.get('number'), s.get('name'), '=>', s.get('state_string')) for s in d.get('steps',[])]"
```

Look for the step whose `state_string` contains `failure`, `(failure)`, `Failed`, or similar. Common step names:

- `compile-webkit` — build failure
- `layout-tests`, `re-run-layout-tests`, `layout-tests-site-isolation` — layout test failure
- `api-tests`, `run-api-tests` — API test failure
- `jhbuild` — Linux dependency build failure (almost always infra)
- `check-change-relevance`, `find-modified-layout-tests` — a "Pull request doesn't have relevant changes" message here while other steps still ran means those failures are pre-existing, not yours

## Step 4: Fetch the log for the failing step

```bash
# Get log metadata (logid, slug, num_lines):
curl -s "https://ews-build.webkit.org/api/v2/builders/<BUILDER_ID>/builds/<BUILD_NUMBER>/steps/<STEP_NUMBER>/logs" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(l.get('logid'), l.get('slug'), l.get('num_lines')) for l in d.get('logs',[])]"

# Fetch the actual log content (use the logid from above):
curl -s "https://ews-build.webkit.org/api/v2/logs/<LOG_ID>/raw"
```

For long compile logs, filter:

```bash
curl -s "https://ews-build.webkit.org/api/v2/logs/<LOG_ID>/raw" | grep -i -E "error:|fatal|undefined|undeclared|cannot find" | head -30
```

For layout test failures, read the `test-failures` slug log — it lists just the failing test names. Then check whether `find-modified-layout-tests` said the PR has relevant changes; if not, the failures are pre-existing.

## Step 5: Triage each failure into a bucket

### A. Real PR-caused failure — fix it

Diagnose from the log and apply the fix. Common WebKit EWS failure patterns:

- **`missing submodule 'wtf.Core.<HeaderName>' [-Werror,-Wincomplete-umbrella]`** on Apple platform compile bots after adding a new `.h` under `Source/WTF/wtf/`. Fix: bump the "Touch count: N" comment on line 1 of [Source/WTF/wtf/module.modulemap](Source/WTF/wtf/module.modulemap) (the comment itself explains this; there is a clang module-cache bug rdar://173516139). Same pattern can occur for other framework modulemaps if they have a similar comment.
- **Linker error for a new source file** on a specific platform: the file is missing from the platform-specific build glue. Check the `add-file-to-webkit` skill checklist:
  - macOS/iOS: `<Project>.xcodeproj/project.pbxproj` + `Sources.txt` (for unified builds)
  - GTK/WPE/Linux: `Source/.../PlatformGTK.cmake` or `PlatformWPE.cmake` etc.
  - JSCOnly: `PlatformJSCOnly.cmake`
  - Windows: usually picked up automatically via CMake globs, but check `CMakeLists.txt`
- **`UnretainedCallArgsCheckerExpectations` or other safer-cpp expectation diff** (mac-safer-cpp, ios-safer-cpp): the static analyzer expectations file is now stale. The fix is to delete the now-passing line(s) from the relevant `*Expectations` file.
- **`<stdatomic.h>` macros leaking into C++** — symptom: `error: no type named '__c11_atomic_thread_fence' in namespace 'std'` and/or `definition or redeclaration of 'memory_order_*' not allowed inside a function`, often as `could not build module 'wtf'`. Cause: a C header that does `#include <stdatomic.h>` (e.g. a libpas `pas_*.h`) became reachable from a C++ header (e.g. the PR made `wtf/Threading.h` include `bmalloc/ThreadSuspend.h` → `pas_thread_suspend.h` → `pas_utils.h`). In C++ TUs `<stdatomic.h>` `#define`s `atomic_thread_fence`/`memory_order_*` to `__c11_atomic_*` builtins, which poison later C++ code using `std::atomic_thread_fence` (e.g. `wtf/SequenceLocked.h`). **Linux-only** (libstdc++/wkdev SDK); Apple bots pass because libc++'s `<stdatomic.h>` is C++-aware — so this looks platform-specific but IS PR-caused (the `compile-webkit-without-change` step passing proves it). Fix: in the offending C header, guard the include with `#ifndef __cplusplus` (C TUs unchanged; only the C++ leak is removed). Verify no C++ consumer actually needs the C atomic *macro* API — `__c11_atomic_*`/`__atomic_*` builtins and the `_Atomic` keyword need no header.
- **Type/member declared under too-broad a platform guard** — symptom on a non-mainstream POSIX bot (e.g. PlayStation): `error: unknown type name '<Type>'` where `<Type>` is defined only for certain platforms. Cause: the PR moved a member/typedef into a generic `#else` (all non-DARWIN) branch, but the type is defined only for, say, `OS(LINUX)`/`OS(WINDOWS)` (check where the `using <Type> = ...` lives, often in `ThreadingPrimitives.h`). The build reaches a platform that takes the broad branch but lacks the type. Fix: narrow the guard to exactly the platforms where the type exists *and* is used (grep for the member's read/write sites and their guards) — e.g. `#if OS(LINUX) || OS(WINDOWS)`. Confirm the member is never *used* on the excluded platform before removing it there.
- **A real layout/API test regression**: only treat as a real regression if `find-modified-layout-tests` said the PR has relevant changes AND the failing tests overlap with the PR's diff. Otherwise treat as pre-existing.

### B. Infrastructure failure — leave it, cite proof

Back the "infra" label with proof (a matching error on another PR or a main-branch build, or evidence of a known flake). The patterns below are strong *hints*, not a free pass:

- `jhbuild` failing with `Error: configure storage: open /var/lib/shared-sdk-images/overlay-images/images.json: permission denied` — wkdev container storage, common on WPE bots.
- `run-webkit-tests` exiting non-zero in under ~5 seconds with no captured output — worker spawning, especially on win-tests.
- `worker_preparation` failing or step stuck on `Killed old processes` — worker state, not your code.
- `Unexpected infrastructure issue: ... retrying with the hope it was a random infrastructure error` already in the step state — the system already knows.
- `analyze-compile-webkit-results => Unable to build WebKit without PR, retrying build (failure)` — the bot couldn't build main even without the PR.

### C. Pre-existing failure — leave it, cite proof

Same evidence bar as bucket B:

- A `find-modified-layout-tests` / `check-change-relevance` "Pull request doesn't have relevant changes" message with tests still failing — quote it; the message *is* the proof.
- A test name appearing both in a passing bot's `Ignored pre-existing failure: ...` and in the failing bot's results — cite both bots.
- Otherwise: cite another PR or main-branch build hitting the same failure.

## Step 6: Apply fixes locally

Edit the working tree to apply each real fix, then stop. When done, tell the user exactly which files you changed so they can review and commit. WebKit uses one commit per PR; the user does the `git commit --amend --no-edit` and force-push themselves.

## Step 7: Report back

List each failing bot on one line, bucketed **fixed (locally)**, **infra**, or **pre-existing**. For real fixes, name the file changed and the one-sentence reason. Keep it short — the result, not a play-by-play. Do not draft PR-summary or comment text; the user writes anything that gets posted.

## Anti-patterns

- **Fetch only failing/in-progress bot logs, not passing ones.** Sweeping the *steps* of in-progress builds (Step 2.5) is cheap and expected; skip full *logs* of green bots.
- **Run the Step 2.5 in-progress sweep before declaring the PR triaged** — `gh pr checks` alone shows only what GitHub has been told so far.
- **Diagnose one bot per shared root cause.** Most Apple-platform compile failures share one cause across mac/tv/vision/watch — fix one, the rest follow. Open parallel subagents only for genuinely independent root causes.
- **Leave a broken bot broken and say so** — no fallback/defensive code to paper over infra.
- **Never commit, push, amend, or comment.** Edits go to the working tree and stop.
