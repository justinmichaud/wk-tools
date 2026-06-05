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

When the user asks to fix EWS issues on the current branch's WebKit PR, follow this workflow. It is built around the observation that **most red EWS bots are not the PR's fault** — they are infra hiccups (jhbuild storage errors, worker timeouts, flaky tests, pre-existing failures). Spending the user's time on those is wasteful. The goal is: triage fast, fix what's real, summarize the rest cleanly.

> **HARD CONSTRAINT — never act on the user's behalf.** This skill is strictly read-from-EWS and edit-locally. NEVER run `git commit`, `git push`, `git commit --amend`, or `gh pr comment` — and never any other command that publishes, pushes, or posts. Apply fixes to the working tree only and stop. Leave committing, pushing, and PR comments entirely to the user. If you ever think a push or comment is warranted, describe what you would do and let the user do it themselves.

> **HARD CONSTRAINT — prove "unrelated" before claiming it.** Most EWS failures ARE caused by the patch. Do not default to "infra" or "pre-existing." Before bucketing any failure as infra/pre-existing/flaky, you must present evidence: either (a) find another PR (or a main-branch/post-landing build) exhibiting the *same* error, or (b) show the same test/bot is flaky (e.g. it passed on a re-run of this same PR, or has a documented flaky history). Cite the specific build/PR URL or log line as proof. If you cannot find such proof, treat the failure as PR-caused and dig deeper — assume it is your fault until evidence says otherwise.

## Step 1: Identify the PR

```bash
gh pr view --json number,title,url,state,headRefName
```

If there is no PR for the current branch, stop and tell the user.

## Step 2: List check statuses

```bash
gh pr checks <pr-number>
```

Each failing row has a URL like `https://ews-build.webkit.org/#/builders/<BUILDER_ID>/builds/<BUILD_NUMBER>`. **The number in the URL is the per-builder build number, not the global build id.** The API endpoint to use is:

```
https://ews-build.webkit.org/api/v2/builders/<BUILDER_ID>/builds/<BUILD_NUMBER>
```

Not `/api/v2/builds/<BUILD_NUMBER>` — that uses a different (global) id and will give you an unrelated build.

## Step 2.5: Don't trust `gh pr checks` to show every bot — sweep in-progress builds too

`gh pr checks` / `statusCheckRollup` only report the bots that have already posted a GitHub status. WebKit's EWS runs **many more builders than GitHub surfaces at any moment** — the slow Linux ones (GTK, WPE) and others may still be compiling and simply haven't reported. A bot that is *currently* building can already be doomed: if it has reached the `compile-webkit-without-change` step, it has **already failed `compile-webkit` with the PR** and is rebuilding without it to confirm the PR is at fault. That is the "retrying without changes" signal — treat it as a failure-in-progress, diagnose it now, don't wait for it to go red.

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

Any bot showing `compile-webkit-without-change` (HAS_RETRY) has a real with-change compile failure — fetch its `compile-webkit` (the *first* compile step, not the without-change one) log per Step 4 and diagnose it. Bots still on their *first* `compile-webkit` are not yet proven failures, but if they share a toolchain with a bot you've already diagnosed (e.g. GTK/WPE all use the same wkdev SDK as gtk3-libwebrtc), predict the same failure and confirm your fix covers them.

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
- `check-change-relevance` — if this step says "Pull request doesn't have relevant changes" but other steps still ran, the failures are pre-existing, not yours
- `find-modified-layout-tests` — same idea for layout tests; "doesn't have relevant changes" means failures are pre-existing

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

For layout test failures, look at the `test-failures` slug log specifically — it lists just the failing test names. Then check whether `find-modified-layout-tests` said the PR has relevant changes; if not, the failures are pre-existing.

## Step 5: Triage each failure into one of these buckets

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

### B. Infrastructure failure — leave it, do not "fix" (but PROVE it first)

Mark as infra and move on. Do not commit fake changes to placate these. **You must back the "infra" label with proof** — a matching error on another PR or a main-branch build, or evidence the step is a known infra flake (per the "prove unrelated" constraint above). The patterns below are strong *hints*, not a free pass; still cite the proof. Examples:

- `jhbuild` step failing with `Error: configure storage: open /var/lib/shared-sdk-images/overlay-images/images.json: permission denied` — wkdev container storage. Common on WPE bots.
- `run-webkit-tests` exiting non-zero in under ~5 seconds with no captured output — worker spawning issue, especially on win-tests.
- `worker_preparation` failing or step stuck on `Killed old processes` — worker state, not your code.
- `Unexpected infrastructure issue: ... retrying with the hope it was a random infrastructure error` already present in the step state — the system already knows.
- `analyze-compile-webkit-results => Unable to build WebKit without PR, retrying build (failure)` — the bot couldn't even build main without the PR; not the PR's fault.

### C. Pre-existing failure — leave it, note it (but PROVE it first)

Same evidence bar as bucket B: cite the proof, don't assume.

- Any bot where `find-modified-layout-tests` or `check-change-relevance` says "Pull request doesn't have relevant changes" but tests still ran and reported failures. (This message *is* the proof — quote it.)
- API/layout tests already marked as pre-existing in passing bots' "Ignored pre-existing failure: ..." messages — cross-reference: if the same test name appears in passing-bot ignored-failures AND in the failing bot's results, it is pre-existing. (Cite both bots.)
- Otherwise: find another PR or a main-branch build hitting the same failure and cite its URL.

## Step 6: Apply fixes locally — do NOT commit or push

Edit the files in the working tree to apply each real fix. **Stop there.** Do not stage, commit, amend, or push — that is the user's job.

WebKit convention is one commit per PR, so when the user is ready they will typically `git commit --amend --no-edit` and force-push to their fork themselves. Do not do this for them. When your local edits are done, tell the user exactly which files you changed so they can review and commit.

## Step 7: Report back

In the final message to the user, list each failing bot in one line and bucket it as **fixed (locally)**, **infra**, or **pre-existing**. For real fixes, name the file changed and the one-sentence reason. Keep it short — the user wants the result, not a play-by-play.

Do not post a PR comment. If a summary on the PR would genuinely help reviewers, draft the text in your message to the user and let them post it themselves.

## Anti-patterns

- **Don't fetch every passing bot's log.** Filter to failing/in-progress rows first. (Sweeping the *steps* of in-progress builds per Step 2.5 is cheap and expected — it's full *logs* of green bots you should skip.)
- **Don't conclude from `gh pr checks` alone that you've seen every bot.** It only shows what GitHub has been told so far; do the Step 2.5 in-progress sweep before declaring the PR triaged.
- **Don't add fallback / defensive code** to paper over an infra failure. If the bot is broken, say so.
- **Don't open subagents for each bot in parallel** unless the bots represent genuinely independent root causes. Most Apple-platform compile failures share one root cause across mac/tv/vision/watch — diagnose one, the rest follow.
- **Don't commit, push, or comment.** Ever. Edits go to the working tree and stop. The user commits, pushes, and posts comments themselves.
