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

If there is no PR for the current branch, stop and tell the user. Note which repo the PR belongs to: a downstream one such as `WebPlatformForEmbedded/WPEWebKit` has its own EWS on another host whose bots never post GitHub statuses — see the end of Step 2.6.

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

## Step 2.6: The bot set is per-PR — build bots trigger the test bots

Most ports schedule one *build* bot on the `pull_request` scheduler; their test bots are `Triggerable` and come into existence only when that build finishes green. So the roster on a PR is not fixed, and a bot that is simply absent is nearly always explained by its parent build rather than by infra. WPE is the clearest case (`Tools/CISupport/ews-build/config.json`, `builders` plus `schedulers`):

| shortname | builder | id | scheduled by |
| --- | --- | --- | --- |
| `wpe` | WPE-Build-EWS | 5 | `pull_request`, directly |
| `wpe-wk2` | WPE-WK2-Tests-EWS | 34 | triggered by `wpe-build-ews` |
| `api-wpe` | API-Tests-WPE-EWS | 41 | triggered by `wpe-build-ews` |
| `jsc-wpe` | JSC-Tests-WPE-EWS | 251 | triggered by `wpe-build-ews` |

The same parent/child shape holds for the other ports: `gtk` triggers `gtk-wk2` and `api-gtk`; `win` triggers `win-tests`; `mac` triggers `mac-wk2`, `mac-intel-wk2`, `mac-wk2-stress`, `mac-site-isolation`, `api-mac`, `jsc-x86-64`; `ios-sim`, `vision-sim` and `mac-AS-debug` likewise. Builder ids and the roster both change over time — read ids from `/api/v2/builders` and the trigger graph from `config.json` instead of assuming either.

What that buys you when triaging:

- A red **build** bot (`wpe`) is a compile failure, and its three test bots never ran. Do not go looking for them.
- A red **test** bot (`wpe-wk2`, `api-wpe`, `jsc-wpe`) proves the build was green, so never hunt a compile error there. Each child carries `parent_builderid` and `parent_buildnumber` in its properties — use them to jump to the build.
- The parent's `results` code says which case you are in: `0` success, `2` failure (`Hash <sha> for PR <n> does not build`), `3` skipped (`Hash <sha> on PR <n> is outdated`, i.e. superseded by a newer push — nothing is wrong and no children spawn).
- Old PRs carry green statuses from **retired** WPE builders: `wpe-skia` (52), `wpe-cairo` (65), `wpe-cairo-libwebrtc` (166), `wpe-libwebrtc` (172). Their build history stops around PRs 28k, 54k, 57k and 58k respectively. A stale green from one of those is not a bot you can re-run, and its absence from a new PR is not a regression.

Enumerate the bots that actually have a build for a PR — GitHub statuses lag the buildbot, this does not:

```bash
for id in 5 34 41 251; do
  curl -s "https://ews-build.webkit.org/api/v2/builders/$id/builds?limit=150&order=-number&property=github.number" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin)
for b in d.get('builds',[]):
    if str(((b.get('properties') or {}).get('github.number') or [None])[0])=='<PR_NUMBER>':
        print('builder $id build', b.get('number'), 'results=', b.get('results'), b.get('state_string'))
"
done
```

Two constraints on that scan. `limit` must stay at 150 or below — a larger page comes back truncated mid-JSON and fails to parse. And it only reaches back 150 builds, roughly a day on builder 5, so a PR last built earlier turns up nothing: add `&offset=N` and page back (`offset` is measured in builds from newest, so aim for `newest_build_number - wanted_build_number`). Filtering server-side does not work — `&github.number__eq=71523` is silently ignored and you get the newest builds instead, so always filter client-side.

Given one build, walk the trigger graph instead of scanning. A child names its parent directly in its properties (`parent_builderid`, `parent_buildnumber`). Going the other way, from a parent to its children, uses the global `buildid` (not the per-builder build number):

```bash
# 1. parent's global buildid:
curl -s ".../api/v2/builders/5/builds/<BUILD_NUMBER>" | python3 -c "import json,sys; print(json.load(sys.stdin)['builds'][0]['buildid'])"
# 2. the buildsets it triggered (reason names the Triggerable scheduler):
curl -s ".../api/v2/buildsets?parent_buildid=<BUILDID>"
# 3. each buildset's buildrequest, then the build itself:
curl -s ".../api/v2/buildrequests?buildsetid=<BSID>"      # -> buildrequestid
curl -s ".../api/v2/builds?buildrequestid=<BRID>"          # -> builderid + number + results
```


**The downstream `WebPlatformForEmbedded/WPEWebKit` repo has its own EWS, on a different host: `https://ews-wpe-rdk.igalia.com`.** Nothing about it reaches the GitHub checks API — `gh pr checks`, `/commits/<sha>/check-runs` and `/commits/<sha>/status` are all empty for those PRs. The bots post their result as an `<!--EWS-Status-Bubble-Start-->` markdown table rendered into the PR page, so the page is the index of bots and their verdicts. Read the links straight out of it:

```bash
curl -sL "https://github.com/WebPlatformForEmbedded/WPEWebKit/pull/<PR_NUMBER>" \
  | grep -oE 'https://ews-wpe-rdk\.igalia\.com[^" ]*' | sort -u
```

Each link's `title` attribute is the verdict (`Hash ed28d251 for PR 1713 does not build (failure)`). From there the API is the same buildbot shape as upstream, just at that host: `/api/v2/builders/<ID>/builds/<N>/steps`, `/steps/<N>/logs`, `/api/v2/logs/<ID>/raw`.

The builder set there depends on the PR's **target branch** — this is where the roster really varies per PR. `curl -s https://ews-wpe-rdk.igalia.com/api/v2/builders` lists 12: `wpe-2.38`, `wpe-2.42` and `wpe-2.46`, each with an x86-64 and an ARM-32 Build bot plus a matching LayoutTests bot. A PR against `wpe-2.46` runs builders 11 (`WPE-246-x86-64-bit-Build-EWS`, worker `ews-rdk-wpe-246-1`) and 12 (`WPE-246-ARM-32-bit-Build-EWS`, worker `ews-rdk-wpe-246-armhf-1`), with 9 and 10 the corresponding LayoutTests bots; silence from the 2.38 and 2.42 bots is expected, not a problem to chase.

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
- **A cherry-pick that references a member the branch does not have** — symptom on a downstream `wpe-2.4x` PR: `error: 'class WebCore::StyleInheritedData' has no member named 'fontData'`, same error on every architecture, with `compile-webkit-without-change` green. The upstream commit was written against a later refactor of that class. Fix by rewriting the new code in the branch's own idiom: grep the file for how neighbouring functions reach the same state (here `m_inheritedData.access().fontCascade`, already used a few lines below) rather than backporting the refactor as well.
- **A real layout/API test regression**: only treat as a real regression if `find-modified-layout-tests` said the PR has relevant changes AND the failing tests overlap with the PR's diff. Otherwise treat as pre-existing.

### B. Infrastructure failure — leave it, cite proof

Back the "infra" label with proof (a matching error on another PR or a main-branch build, or evidence of a known flake). The patterns below are strong *hints*, not a free pass:

- `jhbuild` failing with `Error: configure storage: open /var/lib/shared-sdk-images/overlay-images/images.json: permission denied` — wkdev container storage, common on WPE bots.
- `run-webkit-tests` exiting non-zero in under ~5 seconds with no captured output — worker spawning, especially on win-tests.
- `download-built-product` dying with `curl: (28) Operation too slow. Less than 102400 bytes/sec transferred the last 60 seconds` partway through the ~236 MB archive — the worker's S3 fetch stalled. Check that the parent build bot is green (the archive uploaded fine) and it is infra; common on the `igaliaN-wpe-ews` workers.
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
- **Never conclude a PR has no bots from `gh pr checks` output.** Enumerate the builders by PR number (Step 2.6) first; a port whose build bot is still running or whose PR-relevance check skipped it has no statuses yet, and a triggered test bot does not exist until its build goes green.
- **When a PR's bots are missing from `gh pr checks`, read the PR page HTML before concluding anything.** `curl -sL <pr-url> | grep -oE 'https?://[^" ]*ews[^" ]*'` finds the status bubble's links, including EWS instances that never post GitHub statuses. Guessing hostnames does not find them; the page always names them.
- **Diagnose one bot per shared root cause.** Most Apple-platform compile failures share one cause across mac/tv/vision/watch — fix one, the rest follow. Open parallel subagents only for genuinely independent root causes.
- **Leave a broken bot broken and say so** — no fallback/defensive code to paper over infra.
- **Never commit, push, amend, or comment.** Edits go to the working tree and stop.
