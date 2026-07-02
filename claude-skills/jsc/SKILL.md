---
name: jsc
description: Conventions and workflow for any WebKit work — house rules, comment style, C++ conventions, smart pointers, Safer C++, naming, copyright headers, building, and testing — plus JavaScriptCore-specific architecture (tier-up, JITs, the jsc shell). Use whenever editing, testing, or reasoning about any code in the WebKit repo (WebCore, WebKit, WTF, JavaScriptCore, Tools), not just JSC.
---

# WebKit conventions and JavaScriptCore

This file guides work anywhere in the WebKit repo. It has two parts: **conventions that apply to all WebKit code** (house rules, comments, C++ style, smart pointers, Safer C++, naming, copyright, building, testing) and **JavaScriptCore-specific** material (tier-up, the JIT pipeline, the jsc shell, stress tests). The JSC sections are called out as such; everything else holds repo-wide. Valid on **macOS and Linux**.

`$WEBKIT_ROOT` is the repository root. Almost every command runs from there, not from a subdirectory like `Source/JavaScriptCore`.

## House rules (read first)

These override defaults and apply to every change, anywhere in WebKit.

- **The only git commands that may change the tree are `git stash push` and `git stash apply`, and only to build a baseline for perf testing.** Never run `git checkout`, `commit`, `amend`, `push`, `reset`, `rebase`, `add`, or `restore`. Finish at the working tree, show `git status` / `git diff`, and let the user drive git. Never ask "Want me to commit?" — the user initiates git themselves.
- **Always `git stash apply`, never `git stash pop`.** Apply keeps the stash, so a conflict on restore cannot lose the change.
- **Never draft, suggest, or write a commit message, PR description, or any review/issue/PR comment.** This is a firm boundary with no exceptions. Say in one plain sentence what a change does if that helps; never produce commit-message or comment text.
- **Never search for, read, or transmit credentials, secrets, tokens, or keys, and never post or send anything on the user's behalf** (GitHub, Bugzilla, EWS, anywhere). Reading bot status and logs is fine; publishing is the user's job.
- **Say plainly what a thing does or guarantees — name the concrete mechanism, never a stock idiom or metaphor standing in for it** (in any output, chat included). When you are about to reach for a ready-made figure of speech for "critical/depended-on" or for "a redundant safeguard," write the literal fact instead; stock idioms read as filler here.
- **Put durable, general preferences (voice, conventions, workflow) in this skill, not project memory.** The user works across multiple workstations and checkouts, and project memory is keyed to a single checkout path — so an always-apply rule belongs here where it travels with them.
- **No unrelated changes.** Especially whitespace: only touch whitespace on a line you are already editing for another reason. Never introduce trailing-whitespace or tab/space errors. Leave surrounding formatting alone.
- **Always run `Tools/Scripts/check-webkit-style`** on your diff before considering a change done, and fix what it reports.
- **Always do the comment pass before considering a change done.** Re-read every comment you added or touched and apply the test in [Comment style](#comment-style). This is not optional and not covered by `check-webkit-style`.
- **No new safer-cpp exceptions.** Do not add entries to the safer-cpp / `-Wunsafe-buffer-usage` exception lists. Fix the code to be safe instead.
- **Run the project's real test harness, not hand-rolled binaries.** For JSC specifically, use `Tools/Scripts/run-jsc-stress-tests`, never a bare `jsc test.js` — the harness honors each test's `//@ runDefault(...)` directives, applies the standard flag matrix, and reports like EWS does. Use the analogous script for other components (`run-webkit-tests` for layout tests, `run-api-tests` for API tests).
- **Use the matching `jsc-*` skill and its canonical pipeline; never reinvent one.** Benchmarking uses jsc-jetstream-compare / jsc-microbenchmark (the official `compare-results` statistics); profiling uses jsc-profile; building uses build-webkit; review uses jsc-review; EWS triage uses fix-webkit-ews. Do not hand-roll an ad-hoc harness or your own statistics — a bespoke comparison is untrustworthy and its results get thrown away.
- **A long context or auto-compaction is never a reason to stop or to call a task done.** Continue until the task's real stopping condition is met (the benchmark decision rule, every EWS bot triaged, the loop's target). Persist progress to disk as you go — result JSONs, the current best option deltas, the build fingerprint — so compaction or a restart cannot lose the run. Keep each response bounded: write large output to files under the scratch dir, not into the chat, so no session is lost to an output-token limit.
- **On long or exploration-heavy tasks, delegate to subagents to keep the main context clean.** Spawn a subagent for read-heavy investigation — tracing where something is emitted, mapping a subsystem, reading the code around a large diff — and keep only its conclusion, not the file dumps, in the main thread. Fan out genuinely independent work in parallel (one agent per EWS bot, per option-matrix cell, per subsystem). But keep a *sequential* measurement or interleaving loop in one context — its correctness depends on shared state, so delegate the analysis around it, never the loop itself.

## Comment style

Match the surrounding code's comment density and idiom. WebKit comments explain the non-obvious **why** — a subtle invariant, a footgun, a reason a check is safe to skip — never what the code already plainly says, and never the story of how the change came to be.

Rules:
- **Concise. Comments are rarely longer than one line.** Go past a line only to explain a genuinely new theoretical concept. Litmus test: if every comment in the file were written like this one, would it be a wall of text? If yes, cut it down.
- Write for a fresh reader with zero knowledge of this session, the bug, the PR, or CI/EWS. Cut investigation narrative, platform-by-platform enumeration, bug/PR references, and justification for *why the change was made* — that belongs in the commit message.
- **Only use characters reachable on a keyboard.** Plain ASCII. No em-dashes, arrows, or other Unicode glyphs in comments or code.
- **Say plainly what the code guarantees** — name the mechanism rather than reaching for a stock idiom (the banned-phrase rule in House rules applies here too).
- **If you can make it impossible to use incorrectly, do that instead of documenting the correct usage.** Prefer a type, an assertion, a private constructor, or a `static_assert` over a comment warning people not to misuse something.
- `FIXME:` convention is `// FIXME: <description> https://bugs.webkit.org/show_bug.cgi?id=NNNN` (a bug URL when one exists).

Examples of good, succinct WebKit comments to imitate:

```cpp
// This has to be a forward loop because we are using the insertion set.
// dfg/DFGDCEPhase.cpp

// Not checking for an exception here is ok because jsSingleCharacterString will
// just fetch an unused string if there's an exception.
// runtime/StringConstructor.cpp

// Never use jsCast here. It is possible that this value is "Dead" but not
// "Finalized" yet. In this case we can still access non-JS data.
// jit/JITThunks.cpp

// Run all pending finalizers now because we won't get another chance.
// heap/Heap.cpp -- Heap::lastChanceToFinalize()
```

Each is one line (or two), states a constraint or invariant a fresh reader needs, and uses plain ASCII.

Write the standalone invariant the current code satisfies, for a reader who never saw the prior version or your review. Two failure modes recur; study the good/bad pairs -- they teach the line faster than the rule does:

```cpp
// 1. Narrating the edit -- only parses if you saw the diff.
// BAD:  Report via dataLogLn rather than RELEASE_ASSERT_WITH_MESSAGE, whose message compiles out.
// GOOD: The stress tests match these messages in the crash output via mustCrashWith!.

// 2. Answering the reviewer -- justifies the spelling, names an API absent from the code.
// BAD:  sizeof(void*) == 4 is the WTF-level equivalent of is32Bit(), which WTF cannot include.
// GOOD: (delete it; the condition documents itself)

// Over-explaining -- a paragraph where a line does; enumerates arms the code already shows.
// BAD:  On 32-bit there is no horizontal reduce so findFirstNonZeroIndex lowers to per-lane vmov;
//       on short strings (JSON tokens) that loses to a scalar scan. On 64-bit keep the vector scan.
// GOOD: No efficient SIMD horizontal reduce on 32-bit; a scalar scan wins on short inputs.

// Narrating a mechanism the type/annotation/assert already enforces -- redundant to the reader,
// and it rots when the mechanism moves. WebKit does not comment members this way; the field name
// and its use sites carry it. Trust the reader to read the types (you would not comment an int).
// BAD:  // Bumped by resume() so a captured SuspendedThreadRegisters view traps if read after the
//       // suspension ends.   (on: unsigned m_suspendGeneration)
// GOOD: (delete it)
// BAD:  Returns a view valid only while suspended; scratch must outlive it; LIFETIME_BOUND flags escapes.
// GOOD: Returns a view of the suspended thread's registers, using the suspended thread's stack or scratch as storage.

// The one to KEEP -- a footgun warning: self-contained, names an alternative the reader might
// independently reach for, and says why it breaks. Idiomatic WebKit.
// GOOD: Never use jsCast here. This value may be "Dead" but not "Finalized" yet.
```

Two tells that a comment is aimed at your reviewer, not the next reader (neither shows up in the giveaway-word grep below): it names an API/type/header **not in the surrounding code**, or it explains a choice between two ways of writing the *same* thing. For contrast, WebKit leaves width gates uncommented (`if (sizeof(void*) == 8)` in `ftl/FTLOutput.h`) and comments a branch only with a bare constraint (`// this instruction is only selectable on 64-bit platforms`).

**The comment pass (a distinct step on every change).** Re-read each comment you added in isolation from the diff: would it make sense to someone who only sees the final code? If it needs the prior version or a rejected alternative to parse, rewrite it as a standalone invariant or delete it. Grep the diff for giveaways -- `rather than`, `instead of`, `used to`, `no longer`, `previously`, `now `, `not a`/`not an`, `equivalent`, `cannot`/`can't`, `lives in`, `in practice`, `we use` -- and treat each hit as guilty until cleared; a self-contained footgun warning is the one acquittal. Do the same on `code-review`/`simplify` output.

JSC runs JavaScript through four tiers, promoting hot code upward and demoting it on bad speculation. The whole point is: start cheap, get faster only where it pays, and stay correct by being able to fall back.

```
LLInt  ->  Baseline JIT  ->  DFG JIT  ->  FTL JIT
interp     fast compile     speculative   B3/Air, aggressive
```

**FTL is 64-bit only.** On 32-bit targets (e.g. ARMv7) DFG is the top tier and the DFG-to-FTL
transition below does not exist — a `--useDFGJIT=0` run there is effectively LLInt + Baseline only.

**How promotion happens.** Each tier carries an `ExecutionCounter` (`bytecode/ExecutionCounter.h`) that starts negative and counts up toward a threshold; crossing zero triggers compilation of the next tier. Counters live in the code's data (`m_llintExecuteCounter` for LLInt, `DFG::JITData::m_tierUpCounter` for DFG to FTL). Thresholds are JSC options in `runtime/OptionsList.h`:

| Transition | Key option (default) |
| --- | --- |
| LLInt to Baseline | `thresholdForJITAfterWarmUp` (500) |
| Baseline to DFG | `thresholdForOptimizeAfterWarmUp` (1000) |
| DFG to FTL | `thresholdForFTLOptimizeAfterWarmUp` (64000) |

Thresholds are scaled down dynamically under executable-memory pressure to avoid thrashing.

**Two senses of tier-up:**
- **Function-call**: a `CheckTierUpAtReturn` check decides, at return, whether to compile the next tier for the next call.
- **Loop (OSR entry)**: a long-running loop tiers up mid-execution at a back-edge via `CheckTierUpInLoop` / `CheckTierUpAndOSREnter`, then **OSR-enters** the optimized code without waiting for the function to return. See `dfg/DFGOSREntry.cpp` and `ftl/FTLOSREntry.cpp`.

**OSR exit (deoptimization).** DFG/FTL code is *speculative* — it assumes types and values that profiling suggested. When a guard fails, an OSR exit reconstructs the lower tier's stack state from recorded recovery info and resumes in Baseline at the right bytecode. Repeated exits cause reoptimization. See `dfg/DFGOSRExit.{h,cpp}`.

**Key files:** `bytecode/ExecutionCounter.*`, `bytecode/CodeBlock.*` (`optimizeAfterWarmUp`, `optimizeSoon`), `dfg/DFGJITCode.*` (tierUpCounter), `dfg/DFGOperations.cpp` (`triggerTierUpNow`, `triggerOSREntryNow`, `shouldTriggerFTLCompile`, `triggerFTLReplacementCompile`), `dfg/DFGOSREntry.*`, `dfg/DFGOSRExit.*`, `ftl/FTLOSREntry.*`, `jit/JITWorklist.*` (async compile queue).

Useful options when reasoning about tiers: `--useJIT=0` (interpreter only), `--useDFGJIT=0`, `--useFTLJIT=0`, `--thresholdForFTLOptimizeAfterWarmUp=N`. `--dumpOptions` lists everything.

## Building

Use the `/build-webkit` skill. It handles platform detection (macOS vs Linux), ASan checks, the correct working directory, build commands, and artifact locations. Do not hand-roll build commands.

## Testing

All `Tools/Scripts/*` commands run from `$WEBKIT_ROOT`.

```bash
Tools/Scripts/run-javascriptcore-tests     # full suite
Tools/Scripts/run-jsc-stress-tests         # stress tests (most common for JSC dev)
Tools/Scripts/run-api-tests                # C / Objective-C API
```

**Always pass `-c <N>` to `run-jsc-stress-tests`, and size N to memory, not cores.** Its default worker count comes from `numberOfProcessors`, which both (a) under-detects in cgroup-limited/containerized environments (e.g. reports ~3 while `nproc` shows 80), making the default run ~10-25x too slow, and (b) on a big box would launch far more workers than RAM allows. Each worker is a full VM that can spike to several GB, so a high `-c` OOM-kills the run: `-c 40` OOM-killed everything on a 125 GB host; `-c 20` was safe there. Pick `-c` from available memory (budget a few GB per worker), then cap by cores. On a well-fed box a full `JSTests/stress` run should finish in well under an hour; if it is crawling, the worker count is the cause, not the test load. Pass a directory (`JSTests/stress`), not individual files — the per-file path hits a collection-YAML parse bug. On Linux set `LD_LIBRARY_PATH=$WEBKIT_ROOT/WebKitBuild/JSCOnly/Release/lib` and `--jsc <abs path>/bin/jsc`.

For `testmasm` / `testb3` / `testair`, check the exit code for failure. The library path differs by platform — defer to `/build-webkit` for the exact path:
- macOS: `DYLD_FRAMEWORK_PATH=$WEBKIT_ROOT/WebKitBuild/Release $WEBKIT_ROOT/WebKitBuild/Release/testb3 <target>`
- Linux: `LD_LIBRARY_PATH=$WEBKIT_ROOT/WebKitBuild/Release/lib $WEBKIT_ROOT/WebKitBuild/Release/bin/testb3 <target>`

`v8 test.js` is available to cross-check the other engine's behavior.

### Writing tests

- **A test must be included in the suite and produce no output on success.** It throws / crashes / prints only on failure.
- **Confirm the test actually tests something.** Revert your source change, compile, and run the test — it must fail. Then restore the change and confirm it passes. A test that passes against the unfixed code proves nothing.
- **Think about how the test can be fragile** before finalizing: does it depend on a specific tier being reached, on iteration counts, on GC timing, on platform-specific behavior, on object-property order? Make the assertion robust to those.
- **Use the built-in `testLoopCount` (and `wasmTestLoopCount`) global instead of a hardcoded loop bound.** The jsc shell sets `testLoopCount` to the smallest count that still reaches the top enabled tier under the current flag matrix (`jsc.cpp` clamps it to the warm-up thresholds, smaller when higher tiers are disabled). Writing `for (let i = 0; i < testLoopCount; i++)` makes the test reach tier-up yet run fast across the whole matrix, where a fixed `1000000` would be needlessly slow.

### jsc shell test helpers

The jsc shell exposes globals (and `$vm`, the JSDollarVM object) for deterministic testing — prefer these over hoping a loop reaches a tier:

- Tier control: `noInline(fn)`, `noDFG(fn)`, `noFTL(fn)`, `neverInlineFunction(fn)`, `optimizeNextInvocation(fn)`.
- Inspect compilation: `numberOfDFGCompiles(fn)`, `reoptimizationRetryCount(fn)`, `failNextNewCodeBlock()`.
- GC: `gc()`, `fullGC()`, `edenGC()`, `gcHeapSize()`, `drainMicrotasks()`.
- Value probes: `describe(v)`, `isRope(s)`, `isHeapBigInt(v)`, `ensureArrayStorage(o)`.

### Stress-test directives

Tests select their flag coverage with `//@` directives read by `run-jsc-stress-tests`. Common ones: `//@ runDefault`, `//@ defaultRun`, `//@ runNoFTL`, `//@ requireOptions("--useConcurrentJIT=0")`, `//@ runWithOptions(...)`, `//@ skip if $architecture == ...`. Use these instead of baking options into the test body.

### API tests

API tests live in `Tools/TestWebKitAPI/` and exercise the public C / Objective-C / C++ API contracts using GoogleTest (`TEST(Suite, Name) { ... }`), not the JS engine's tiering. Use one when you are changing or relying on `API/` surface (`JSContext`, `JSValue`, `JSGlobalContextRef`, JSC C API) or WTF data structures — not for language-semantics coverage, which belongs in `JSTests/stress/`.

```bash
Tools/Scripts/run-api-tests --release JSC          # JSC API suite only
Tools/Scripts/run-api-tests --release WTF_Vector    # a single suite or test by name
```

Assert with GoogleTest macros (`EXPECT_TRUE`, `EXPECT_EQ`, `EXPECT_WK_STREQ`). Add new test files via `/add-file-to-webkit` so the TestWebKitAPI build picks them up. The same "revert the fix and watch it fail" discipline applies.

## Architecture map

- `runtime/` — JS runtime, object model, built-ins, GC integration. Start here for JS semantics.
- `bytecode/` — bytecode definitions (`BytecodeList.rb`) and structures; `CodeBlock`, `ExecutionCounter`.
- `bytecompiler/` — AST to bytecode (`BytecodeGenerator`).
- `parser/` — lexer, parser, AST; keywords in `Keywords.table`.
- `llint/` — Low Level Interpreter in offlineasm (`docs/offlineasm-instruction-reference.md`).
- `jit/` — Baseline JIT, thunks, worklists.
- `dfg/` — DFG JIT: data-flow graph, speculation, OSR entry/exit.
- `ftl/` — FTL JIT, lowering DFG to B3.
- `b3/` — SSA IR for FTL; `b3/air/` is the register-allocated assembly IR.
- `assembler/` — per-architecture assemblers (ARM64, x86-64, ARMv7, RISC-V64).
- `heap/` — generational GC with IsoHeap segregation.
- `wasm/` — WebAssembly; IPInt interpreter; `wasm/debugger/` is a GDB Remote Protocol server (`wasm/debugger/README.md`).
- `yarr/` — RegExp engine and its JIT.
- `builtins/` — built-ins written in JS, compiled to bytecode at build time.
- `API/` — public C / Objective-C API (JSContext, JSValue).
- `inspector/` — Web Inspector remote-debugging protocol.

### Code generation at build time

- **Bytecode**: edit `bytecode/BytecodeList.rb`, then implement in `llint/`, then add JIT support in `jit/`/`dfg/`/`ftl/`, then `bytecompiler/`.
- **Builtins**: add the `.js` file and register it in `CMakeLists.txt` and `DerivedSources.make`.
- **WebAssembly**: opcodes in `wasm/wasm.json`.
- Generators are Ruby (opcodes, offlineasm), Python (builtins, inspector, Unicode), Perl (lookup hash tables).

### File conventions

- `*Inlines.h` hold inline/template bodies; `*Forward.h` are forward-declaration headers.
- Platform-specific files use suffixes like `*Cocoa.*`, `*Mac.*`.
- **Adding a source/header file**: use `/add-file-to-webkit` (auto-triggers) — it covers `Sources.txt`, the Xcode project, and `CMakeLists.txt`.
- Feature flags live in `features.json`; toggle at build via `-DENABLE_FEATURE=ON/OFF`, at runtime via JSC options.
- Platform guards: `#if PLATFORM(MAC)`, `#if OS(DARWIN)`, `#if OS(LINUX)`, `#if CPU(ARM64)`, etc. WTF provides the cross-platform primitives.

## Memory management model

- JS objects: garbage-collected heap. C++ objects: `Ref`/`RefPtr` reference counting. IsoHeap segregates types for security.
- The JSC shell exposes `$vm` (JSDollarVM) for internal testing.

## JSC C++ conventions

**Exception-scope discipline.** Any function that can throw must declare a scope and check after every call that might throw:

- `auto scope = DECLARE_THROW_SCOPE(vm);` in functions that may throw; `DECLARE_CATCH_SCOPE(vm)` where you handle one.
- `RETURN_IF_EXCEPTION(scope, <returnValue>);` immediately after a call that can throw, before touching its result.
- `RELEASE_AND_RETURN(scope, <expr>);` to return the result of a throwing tail call without an extra check.
- `EXCEPTION_ASSERT(!scope.exception());` to assert an invariant about exception state.
- The exception-check validator runs in debug builds and will fire if a throwing call is not followed by a check — do not silence it by reordering; fix the missing check.

Declare the scope once, then check immediately after each throwing call, before touching its result:

```cpp
auto scope = DECLARE_THROW_SCOPE(vm);
auto key = argument.toPropertyKey(globalObject);
RETURN_IF_EXCEPTION(scope, { });                            // check before using `key`
RELEASE_AND_RETURN(scope, target->get(globalObject, key));  // throwing tail call, no extra check
```

**Prefer WTF types over `std`.** `Vector`, `HashMap`, `HashSet`, `String`, `Ref`/`RefPtr`, `std::unique_ptr` via `makeUnique<T>()`. String literals are `"..."_s` (builds an `ASCIILiteral`/`String` without a strlen). Move with `WTFMove(x)`, not `std::move`. Allocate owned objects with `makeUnique`/`makeUniqueWithoutFastMallocCheck`, not bare `new`.

**Assertions.** `ASSERT(cond)` and `ASSERT_NOT_REACHED()` compile out in release. `RELEASE_ASSERT(cond)` / `RELEASE_ASSERT_NOT_REACHED()` stay in release — use them for security-relevant or correctness-critical invariants. `ASSERT_UNUSED(var, cond)` when the variable is only used by the assert.

**Options at runtime.** Every JSC option is settable from the shell as `--useFoo=1` or as an env var `JSC_useFoo=1` (same name). Useful dump/trace flags: `--dumpDisassembly`, `--dumpDFGDisassembly`, `--dumpFTLDisassembly`, `--verboseOSR`, `--verboseCompilation`, `--reportCompileTimes`, `--dumpOptions` (lists everything).

## Smart pointers and lifetimes

Source: the WebKit code style guide and Safer C++ guidelines. These hold across WebKit, not just JSC.

- **Ref-counted objects** (subclasses of `RefCounted`/`ThreadSafeRefCounted`): own with `Ref<T>` when non-null, `RefPtr<T>` when nullable. `Ref` cannot be null and cannot be moved-from into null.
- **CheckedPtr / CheckedRef** for types deriving `CanMakeCheckedPtr` — these assert no live pointers remain at destruction. **WeakPtr / WeakRef** to break cycles.
- **Use smart pointers, not raw pointers, for all data members** that participate in ownership. A raw `T*`/`T&` member to a ref-counted object is a Safer C++ violation.
- **When calling a non-trivial member function on a heap object, hold a smart pointer to it on the stack** for the duration. Name it `protectedThis` when it protects `this`, otherwise `protector` or `protected` + the capitalized variable name (e.g. `protectedNode`). A smart-pointer member that is never reassigned should be `const` — that removes the need for callers to re-protect it.
- **Lambda captures:** for asynchronous lambdas capture `protectedThis = Ref { *this }` (or `weakThis = WeakPtr { *this }` with a null-check when keeping `this` alive is not required). Mark a function that only runs its lambda synchronously `NOESCAPE`.
- Declare `adopt`-initialized locals with the explicit smart-pointer type, not `auto`: `Ref origin = adoptRef(*new SecurityOrigin);`, `RetainPtr dict = adoptNS(...);`.
- Use `std::exchange(ptr, nullptr)` rather than `WTFMove(ptr)` when the moved-from variable is read again later.
- **Casting:** in JSC, cast `JSCell`s with `jsCast<T>()` (asserts) and `jsDynamicCast<T>()` (null on mismatch). The WebCore equivalents are `downcast<T>()` / `dynamicDowncast<T>()`; prefer the dynamic form over `is<T>()` then a cast. Never `jsCast` a value that may be Dead-but-not-Finalized inside a GC callback.

The canonical shapes (the good form is what to write; the flagged form follows `//`):

```cpp
Ref<Node> m_node;                         // owning member is a smart pointer, not Node* m_node
Ref protectedThis { *this };              // hold a heap object on the stack across a call on it
protect(m_controller)->didFinish();       // protect a member Ref/RefPtr at a flagged call site
loader->load([protectedThis = Ref { *this }] { ... });   // async lambda captures a protector
```

## Safer C++

JSC is built with the Safer C++ checkers on, and **no new exceptions may be added** to the suppression lists. Fix the code, do not exempt it.

- **`std::span` instead of pointer + length**, and `std::array` instead of C arrays: `void write(std::span<const uint8_t>)`, not `void write(const uint8_t*, size_t)`. Container and view `operator[]` carry `RELEASE_ASSERT(index < length())`; the span work relies on hardened libc++ (`-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_EXTENSIVE`).
- `-Wunsafe-buffer-usage` flags raw pointer arithmetic and unchecked indexing.
- Clang static-analyzer checkers enforce the lifetime rules: `alpha.webkit.UncountedCallArgsChecker`, `RefCntblBaseVirtualDtor`, `NoUncountedMemberChecker`. They are what fail your EWS build when a raw pointer to a ref-counted object slips in.
- **RefCounted subclasses** give their constructors `private` visibility and expose a public `static Ref<T> create(...)`. A non-final ref-counted class needs a virtual destructor (`RefCntblBaseVirtualDtor` enforces this).
- Prefer `enum class` (with an explicit underlying type) over plain enums, `std::variant` over unions, and strongly-typed `ObjectIdentifier<>` over bare `uint64_t`.
- **Cross-thread:** pass data with `crossThreadCopy(WTFMove(data))`. Guard shared state with the `<wtf/ThreadSafetyAnalysis.h>` macros `WTF_GUARDED_BY_LOCK(...)` and `WTF_REQUIRES_LOCK(...)` so the analyzer verifies locking at build time.
- **Fixing `UncountedCallArgsChecker` on a member RefPtr/Ref** (e.g. `m_foo->bar(...)` flagged "Call argument for 'this' parameter is uncounted and unsafe"): the idiomatic fix is `protect(m_foo)->bar(...)`. `protect()` is a WTF free function (`Source/WTF/wtf/RefPtr.h`, `Source/WTF/wtf/Ref.h`) that copies the smart pointer for the call's duration — 338+ call sites across the tree. Use it only at flagged sites; locals constructed from `Foo::create()` are already protected by their stack `Ref`/`RefPtr`, so the checker doesn't warn there.
- **`NODELETE` is a contract, not a suppression.** Defined in `wtf/Compiler.h` as `[[clang::annotate_type("webkit.nodelete")]]`; the comment on the macro is the spec: *"the function does not run any destructor or free memory"* — **any**, not just `this`. Clang's `TrivialFunctionAnalysis` enforces it: every parameter must have a trivial destructor, and the body may not call un-annotated/un-trivial functions, `delete`, or `free`. So before annotating: a method that overwrites a stored `Ref` (`HashMap::set` on an existing key) or grows a `Vector<Ref<...>>` (the old buffer is `fastFree`d) is *not* NODELETE-safe even if it "feels trivial." When in doubt, use `protect(m_member)->...` at the call site instead — it actually adds a refcount rather than asserting a guarantee you haven't verified. **Never add `SUPPRESS_NODELETE`** (nor `SUPPRESS_UNCOUNTED_*`) — they are suppressions, barred by the no-new-exceptions house rule. And do not rewrite idiomatic `std::tuple`/`std::optional`/lambda code just to satisfy the checker: use the `NODELETE` annotation when the function genuinely frees nothing, or `protect(m_x)->` at the call site; if neither a clean annotation nor a real fix applies, stop and raise it with the user — never suppress and never mangle the idiom.
- Recent JSC reviews enforce these directly: reviewers asked for `std::span` over pointer+length and for deriving state from an existing object instead of threading extra raw parameters through.

## Naming conventions

From the WebKit code style guide. `check-webkit-style` catches some, not all.

- CamelCase. Types/namespaces start uppercase (acronyms stay fully capitalized: `HTMLDocument`); variables/functions start lowercase (`mimeType`, not `MIMEType`).
- Data members are private and prefixed `m_`; statics `s_`. Objective-C ivars prefixed `_`.
- Booleans read as predicates: `isValid`, `didSendData`. Setters are `setX`; the matching getter is bare `x()` (no `getX`). Reserve `get` for getters returning through an out-argument.
- A getter that does not lazily create returns plain; the lazily-creating variant gets no prefix, and the non-creating one is suffixed `IfExists` (`styleResolverIfExists()` vs `styleResolver()`).
- Prefer an `enum class` parameter over a bare `bool` at call sites where a literal would be unclear.
- `#pragma once`, not include guards. Singleton accessor is `singleton()`.

## Comments and copyright headers

- Comment style is covered above. The style guide also requires: sentences start capitalized and end with a period; one space before an end-of-line comment; `FIXME:` with no attribution (no `FIXME(name)`, no `TODO`).
- **Every file edited substantively should carry an Igalia copyright line.** Add `Copyright (C) <year> Igalia S.L.` to the header when doing real work in a file (matching the existing header format).
- **Never remove or alter an existing copyright attribution.** Add ours alongside theirs; leave Apple's and every other party's lines intact.

## Environment note

A **headless** environment with no awake/attached display cannot complete GUI browser benchmarks (`run-benchmark` + MiniBrowser) — `requestAnimationFrame` is throttled to nothing and runs time out. Measure JS performance with the **jsc shell** headlessly instead. See the `/jsc-jetstream-compare` skill.

## Further reading

Authoritative references. The full coding style guide lives in-tree at `Websites/webkit.org/code-style.md`; the Safer C++ rules are on the WebKit GitHub wiki ("Safer-CPP-Guidelines"). For architecture, these blog posts are the canonical deep-dives:

- Speculation in JavaScriptCore — the definitive tier-up, OSR, and profiling writeup: https://webkit.org/blog/10308/speculation-in-javascriptcore/
- Introducing the WebKit FTL JIT — how source flows through bytecode, DFG CPS/SSA IR, and the FTL backend: https://webkit.org/blog/3362/introducing-the-webkit-ftl-jit/
- Introducing the B3 JIT Compiler — the SSA backend that replaced LLVM in the FTL: https://webkit.org/blog/5852/introducing-the-b3-jit-compiler/
- A New Bytecode Format for JavaScriptCore — the bytecode layout the parser and LLInt operate on: https://webkit.org/blog/9329/a-new-bytecode-format-for-javascriptcore/
- Introduction to WebKit and the JavaScriptCore deep dive: https://docs.webkit.org/

In-tree docs: `Source/JavaScriptCore/docs/offlineasm-instruction-reference.md`, `docs/offlineasm-register-reference.md`, and `wasm/debugger/README.md`.
