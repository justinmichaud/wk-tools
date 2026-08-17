---
name: jsc-review
description: Use to review a JavaScriptCore (or WebKit) change for correctness, security, test coverage, performance, and newcomer readability — e.g. "review this PR", "review my JSC changes", "is this patch clean and safe to land". Reviews the current branch's PR, or the working-tree diff when on the tracking branch. Produces a findings report for the user; it never posts to the PR and never drafts a comment.
user-invocable: true
allowed-tools:
  - Bash(gh pr view:*)
  - Bash(gh pr diff:*)
  - Bash(gh pr checks:*)
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git show:*)
  - Bash(git status:*)
  - Bash(git rev-parse:*)
  - Bash(git merge-base:*)
  - Bash(make release:*)
  - Bash(Tools/Scripts/build-webkit:*)
  - Bash(Tools/Scripts/run-jsc-stress-tests:*)
  - Bash(Tools/Scripts/run-javascriptcore-tests:*)
  - Bash(wkdev-enter:*)
  - Bash(taskset:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(grep:*)
---

# Reviewing a JavaScriptCore change

Give a change a fresh, thorough review: correctness, security, real test coverage, performance, and
whether a newcomer would find the code clean. Conventions themselves (naming, comment style, Safer C++,
smart pointers) live in the **jsc skill** — this skill is the review *process* and a JSC-specific
checklist. Building and running tests use the **build-webkit** and **jsc** skills.

**Boundary (from the jsc house rules):** deliver findings to the user in chat. Never post to the PR
(`gh pr review`/`gh pr comment`), never format the output as a comment to paste, and never draft a
commit message. This is analysis for the user, not a published review. Do not touch the patch or
history. The only changes you may make to the tree are (a) the temporary mutation in step 3 and (b) new
stress tests that demonstrate a finding (also step 3) — nothing else.

## Get the change under review

```bash
gh pr view --json title,body,number    # the PR's intent — review against what it claims to do
gh pr diff                              # the diff under review
# On the tracking branch with no PR: review the working tree instead:
git diff ; git diff --cached
```

Read the whole diff once, then open the **surrounding code** of each touched function — a diff hides the
context that makes a change wrong (a caller that can throw, a lock held, an invariant two lines up).

## Mindset: read it as a newcomer

- Is each function's purpose obvious from its name and a glance? Could a caller misuse it? Prefer a
  change that makes misuse impossible (a type, an assertion, a `private` constructor) over one that
  documents the correct usage.
- Sort every finding into **Must-fix** (correctness / security / missing test), **Should** (clarity /
  perf), or **Nit** (pure style). Spend your attention on Must-fix.
- Assume nothing is tested until you have watched a test fail without the fix (step 3).

## Checklist

Each item names what to check and a real WebKit commit (or CVE) showing why it matters.

### 1. Correctness and security — JSC's highest-value bugs

- **Exception safety:** every call that can throw is followed by `RETURN_IF_EXCEPTION` before its result
  is used, and `throwScope.release()` is not moved ahead of a throwing call. Ex: `0bf37696c4bd`
  (missing check after `eval` in `commonCallDirectEval`), `a084fca782e9` (iterator completion),
  `8b609688f1de` (`newCodeBlockFor`).
- **Callback re-entry:** any `valueOf` / getter / proxy trap / `Symbol.toPrimitive` between reading a
  length or pointer and using it can invalidate it — re-validate, never trust a cached length across a
  callback. Ex: **CVE-2023-38600** (`copyWithin` on a resizable ArrayBuffer).
- **Bounds and index math:** raw index arithmetic on a butterfly / TypedArray / arguments is checked
  against the *current* length, and `index * elementSize` (or `len + n`, `count * size`) cannot wrap
  32-bit before the check. Ex: `891e85f5de95` (arrayInitData bounds), `b1a470704480` (32-bit overflow),
  `f9a7aaf14df9` (OOB write in `JSStringGetUTF8CString`).
- **Write barriers:** every new store of a `JSCell` into a heap object has a matching
  `vm.writeBarrier(owner, value)` — easy to miss on new containers and Wasm table grow/set.
- **Type / speculation (DFG/FTL):** a `clobberize`/`AbstractInterpreter`/fixup change keeps
  `clobberWorld()`/`write(Heap)` on any op that still has effects, and a new speculation has a bailout.
  Ex: `959410e0ccfa` (JSMap/SetIterator speculation); **CVE-2023-37450** (dropped `clobberWorld`).
- **Use-after-free / lifetime:** no raw reference to a GC or ref-counted object is held across a call
  that can free it. Ex: `cae26b36ccb9` (GC during `B3::generate`), `9257a50c70ba` (RegExp exec UAF).
- **Uninitialized memory:** a new fast-path allocation zero-fills its butterfly/slots; no codegen path
  reads a spilled or uninitialized local. Ex: **CVE-2024-44308** (DFG TypedArray store).

### 2. Safer C++ and lifetimes

Defer to the jsc skill for the rules; on review, confirm: `std::span` not pointer+length
(`d3dc83426c85`, `72b0ae885e7d`); owning members are smart pointers and flagged call sites use
`protect(m_x)->`; a ref-counted object is never stored in a `std::unique_ptr` (`8e3c2c07a923`, a UAF).
**No new safer-cpp / `-Wunsafe-buffer-usage` exceptions** — the fix is to make the code safe.

### 3. Tests — and prove they actually catch the bug (do not eyeball this)

- **The patch ships a regression test** (usually `JSTests/stress/...`) — most real fixes add one
  alongside the change (`891e85f5de95`, `cae26b36ccb9`, `a084fca782e9`).
- **Mutation check — the core of this skill.** Build the patch and run its test (passes). Then break
  the fix: temporarily edit the fixed line(s) back to the buggy behavior, rebuild, and re-run. An
  existing or new stress test **must fail**. If nothing fails, the change is **untested** — a Must-fix
  finding, not a nit. Restore afterward with `git stash push` to shelve the mutation (the sanctioned
  revert — never `git checkout`). This is safe only when reviewing a *committed PR* (the working tree is
  clean). When reviewing an *uncommitted working-tree diff*, the patch is unstaged and a mutation could
  lose it — **stop and ask the user to back up their diff themselves before you change anything**, and
  proceed only after they confirm.
  ```bash
  # build + run the test through the real harness (size -c to memory; see the jsc / build-webkit skills)
  Tools/Scripts/run-jsc-stress-tests -c 20 JSTests/stress/<the-new-test>.js
  # ... then break the fix line, rebuild, re-run: expect a failure, then restore (git stash push).
  ```
- **Writing new tests is encouraged, and they stay in the tree.** If you find missing coverage, a bug
  the patch introduces, or a pre-existing bug, write a new `JSTests/stress` test that demonstrates it
  and leave it for the user — a failing test is the clearest finding you can hand over. This is the one
  case where the review adds to the tree; call it out explicitly in the report.
- **Fragility — will it still test its intent after unrelated engine changes?**
  - Loop to tier-up with the built-in **`testLoopCount`**, never a hardcoded `1000000`.
  - Do not depend on a specific tier, OSR timing, GC timing, inlining decision, or property enumeration
    order *unless that is the behavior under test* — and if it is, pin it deterministically with
    `noInline` / `optimizeNextInvocation` / `$vm`, not by hoping a loop gets there.
  - Assert the actual behavior, not an incidental side effect, and never match on an error-message
    string that can be reworded.
  - Ex: `866927170c08` / `d17d81bc253d` (a stress test that was flaky because it leaned on timing).

### 4. Performance — regressions and missed wins

- **No new work in a hot path:** an allocation, a redundant type check, or a recomputation that could
  hoist; a slow-path call where a fast path already exists. Ex: `7445b8e96e6b` (spread fast path),
  `bef1df82911b` (inline `InstanceOfMegamorphic`).
- **Missed opportunity:** could this reuse or extend an inline cache, add a fast path, or avoid a
  Structure transition? Ex: `9fa6f1a7715e` (adaptive DataIC).
- **Cross-tier pessimization:** a codegen/IC change that speeds one case can slow others — if in doubt,
  measure with the **jsc-jetstream-compare** / **jsc-microbenchmark** skills before trusting it. Ex:
  `bf6e09860923` (a perf regression that shipped).
- **32-bit:** watch for added register pressure / spills (there is no FTL there, so DFG is the top tier
  and codegen quality matters more).

### 5. Readability (newcomer lens)

`check-webkit-style` already enforces indentation, braces, include order, `nullptr`, `unsigned`,
`WTFMove`, and the like — run it and do not spend review attention there. Focus on what it cannot catch:

- Naming: `m_`/`s_` members, predicate bools (`isX`/`didX`), no `getX`, `setX`/`x()` pairs, protectors
  named `protectedThis`.
- Early-return over deep nesting; references over pointers when the value is never null; `auto` only
  when it aids reading (not when it hides a type the reader needs to see); `const`-correctness; an `enum class` argument
  where a bare `bool` at the call site would be a mystery.
- **No unrelated reformatting** in the diff — it hides the real change from the next reviewer.
- Comments explain the non-obvious *why* (jsc skill Comment style), never what the code already says.

## Report

Present the review in chat as three buckets — **Must-fix**, **Should**, **Nit** — each finding with
`file:line`, the concrete problem, and the checklist rule it maps to. For a Must-fix, state how you
confirmed it (e.g. "reverted the bounds check, `stress/foo.js` still passed → the change is untested").
Lead with whether the patch does what its PR body claims. Do not post it and do not shape it as a
submittable PR comment.
