---
name: jsc
description: "Page-in preferences about working on JSC"
---

#  How to work on JavaScriptCore

Use this skil any time you see that you are in a WebKit repository  to understand details about how jsc works.

## Repository Context

JavaScriptCore (JSC) is WebKit's JavaScript engine. This directory (`Source/JavaScriptCore`) is part of the larger WebKit repository. The WebKit root is the parent of `Source/` — i.e. `../..` relative to this directory. Commands should typically be run from the WebKit root, not from this JavaScriptCore subdirectory.

Throughout this document, `$WEBKIT_ROOT` refers to the WebKit repository root directory.

## Building

Use the `/build-webkit` skill for building JSC or WebKit. It handles platform detection, ASan checks, correct working directory, build commands, result checking, and build artifact locations.

## Test Commands

All `Tools/Scripts/*` commands run from `$WEBKIT_ROOT`. For running built binaries, see the `/build-webkit` skill for platform-specific artifact paths and `DYLD_FRAMEWORK_PATH` usage.

```bash
# Run all JavaScriptCore tests (comprehensive)
Tools/Scripts/run-javascriptcore-tests

# Run stress tests only (most commonly used for JSC development)
Tools/Scripts/run-jsc-stress-tests

# Run JSC benchmarks
Tools/Scripts/run-jsc-benchmarks

# Run API tests (C/Objective-C API)
Tools/Scripts/run-api-tests

# Run testmasm, testb3, testair — check exit code for failure detection
# See /build-webkit skill for the correct binary path on your platform
DYLD_FRAMEWORK_PATH=$WEBKIT_ROOT/WebKitBuild/Release $WEBKIT_ROOT/WebKitBuild/Release/testmasm <testTarget>
DYLD_FRAMEWORK_PATH=$WEBKIT_ROOT/WebKitBuild/Release $WEBKIT_ROOT/WebKitBuild/Release/testb3 <testTarget>
DYLD_FRAMEWORK_PATH=$WEBKIT_ROOT/WebKitBuild/Release $WEBKIT_ROOT/WebKitBuild/Release/testair <testTarget>

# You can also use V8 to check the other engine's behavior.
v8 test.js
```

## Architecture Overview

JavaScriptCore implements a **4-tier JIT compilation pipeline**:

1. **LLInt** (`llint/`) - Low Level Interpreter, first execution tier
   - Written in portable assembly (offlineasm language)
   - See `docs/offlineasm-instruction-reference.md` for instruction reference
   - Bytecode dispatching and initial profiling

2. **Baseline JIT** (`jit/`) - Second tier
   - Fast compilation with profiling instrumentation
   - Collects type feedback for higher tiers

3. **DFG JIT** (`dfg/`) - Data Flow Graph JIT, third tier
   - Data flow analysis and optimization
   - Speculative optimization based on profiling data

4. **FTL JIT** (`ftl/`) - Faster Than Light, fourth tier
   - Uses B3 backend (`b3/`) for aggressive optimization
   - Highest performance tier

### Key Component Directories

- **`runtime/`** - JavaScript runtime system (1000+ files)
  - Object model, built-in objects, garbage collection integration
  - Entry point for understanding JS semantics

- **`bytecode/`** - Bytecode definitions and structures
  - Defined in `BytecodeList.rb`
  - Central to understanding execution model

- **`bytecompiler/`** - Compiles AST to bytecode
  - Bridge between parser and execution

- **`parser/`** - JavaScript parser
  - Lexer, parser, AST construction
  - Keywords defined in `Keywords.table`

- **`heap/`** - Garbage collector (200+ files)
  - Generational GC with IsoHeap support

- **`wasm/`** - WebAssembly implementation
  - IPInt interpreter for WebAssembly
  - `wasm/debugger/` contains GDB Remote Protocol implementation
  - See `wasm/debugger/README.md` for debugging architecture

- **`assembler/`** - Machine code generation
  - Platform-specific assemblers (ARM64, x86-64, ARMv7, RISC-V64)

- **`b3/`** - B3 intermediate representation (200+ files)
  - SSA-based IR for FTL backend
  - `b3/air/` - Assembly IR with register allocation

- **`API/`** - Public C/Objective-C API
  - JSContext, JSValue, etc.
  - Used by embedders (Safari, Node.js via derivatives)

- **`builtins/`** - JavaScript built-in functions
  - Written in JS, compiled to bytecode at build time
  - Generator in `Scripts/generate-js-builtins.py`

- **`inspector/`** - Web Inspector protocol
  - Remote debugging support

- **`yarr/`** - Yarr RegExp engine
  - Regular Expression (RegExp) implementation, including JIT compiler for RegExp

## Code Generation System

JavaScriptCore generates significant amounts of code at build time:

**Bytecode System:**
- `bytecode/BytecodeList.rb` - Defines all bytecode opcodes
- Generates: `Bytecodes.h`, `BytecodeStructs.h`, LLInt implementations
- When adding bytecode: update BytecodeList.rb → implement in llint/ → add JIT support in jit/dfg/ftl/

**Builtins:**
- `builtins/*.js` - JavaScript implementations
- Build system generates C++ code that compiles these to bytecode
- Add to `CMakeLists.txt` and `DerivedSources.make`

**WebAssembly:**
- `wasm/wasm.json` - WebAssembly opcode definitions
- Generates opcode tables and validation logic

**Other Generators:**
- Ruby: Opcodes, LLInt assembly, structured headers
- Python: Builtins, inspector protocol, Unicode tables
- Perl: Hash tables for lookup tables

## Development Workflow Patterns

### Adding a New Bytecode Instruction

1. Define in `bytecode/BytecodeList.rb`
2. Implement interpreter tier in `llint/LowLevelInterpreter*.asm`
3. Add Baseline JIT in `jit/JIT*.cpp`
4. Add DFG support in `dfg/DFGByteCodeParser.cpp` and `dfg/DFGSpeculativeJIT*.cpp`
5. Add FTL support in `ftl/FTLLowerDFGToB3.cpp`
6. Update bytecompiler in `bytecompiler/BytecodeGenerator.cpp`
7. Add stress tests in `JSTests/stress/`

### Adding a New Built-in Function

1. Create `.js` file in `builtins/` (e.g., `ArrayPrototype.js`)
2. Add to build: `CMakeLists.txt` (JAVASCRIPTCORE_BUILTINS_SOURCES) and `DerivedSources.make`
3. Wire up in corresponding `runtime/` class (e.g., `ArrayPrototype.cpp`)
4. Built-in generator creates C++ code automatically
5. Add tests

### Debugging Tips

- **JSC shell** has `$vm` object (JSDollarVM) for internal testing
- Use `--dumpOptions` to see all JSC options
- Use `--useJIT=0` to disable JIT for testing interpreter
- Options like `--thresholdForFTLOptimizeAfterWarmUp=1000` control tier thresholds
- WebAssembly debugging via GDB Remote Protocol: see `wasm/debugger/README.md`

### Understanding Execution Flow

1. **Parse**: `parser/` creates AST
2. **Compile to bytecode**: `bytecompiler/` generates bytecode
3. **Execute**: LLInt (`llint/`) interprets bytecode
4. **Profile**: Baseline JIT profiles hot code
5. **Optimize**: DFG/FTL compile hot functions with speculation
6. **Deoptimize**: On speculation failure, fall back to lower tier (OSR Exit)

## File Organization Conventions

- **Inlines**: `*Inlines.h` files contain template/inline implementations
- **Forward declarations**: `*Forward.h` files reduce compilation dependencies
- **Platform-specific**: Cocoa-specific in `*Cocoa.*`, Mac in `*Mac.*`, etc.
- **Adding files**: Use `/add-file-to-webkit` (or just add a file — the skill auto-triggers) for the full procedure covering Sources.txt, Xcode project, and CMakeLists.txt updates

## WebAssembly Debugger Architecture

The WebAssembly debugger (`wasm/debugger/`) implements GDB Remote Protocol for debugging WASM:
- Runs as server in JSC process
- GDB/LLDB connects as client
- Source-level debugging of WebAssembly (requires DWARF debug info)
- Integration tests in `JSTests/wasm/debugger/`
- See `wasm/debugger/README.md` for detailed architecture

## Testing Philosophy

- **Stress tests** (`JSTests/stress/`) - Main test suite, covers edge cases
- **Microbenchmarks** (`JSTests/microbenchmarks/`) - Performance regression detection
- **ChakraCore tests** - Imported test262 and other compatibility tests
- **API tests** (`API/tests/`) - Validates C/Objective-C API contracts
- Tests should cover all JIT tiers (use JSC options to force tiers)
- Finally included tests must not have any print-output. It should throw an error / output if it fails (or crash). If it passes, it should have no output.

## Cross-Cutting Concerns

**Feature Flags:**
- Defined in `features.json`
- Control via CMake: `-DENABLE_FEATURE=ON/OFF`
- Runtime via JSC options (see `--dumpOptions`)

**Platform Abstraction:**
- Platform-specific code in `Platform*.cmake` files
- Use `#if PLATFORM(MAC)`, `#if OS(DARWIN)`, etc.
- WTF library provides cross-platform primitives

**Memory Management:**
- Garbage collected heap for JS objects
- Reference counting (Ref/RefPtr) for C++ objects
- IsoHeap segregates types for security

## Documentation Resources

- `docs/offlineasm-instruction-reference.md` - Complete LLInt assembly reference
- `docs/offlineasm-register-reference.md` - Register naming conventions
- `wasm/debugger/README.md` - WebAssembly debugger architecture
- WebKit blog: https://webkit.org/blog/ (architecture deep-dives)
- Introduction.md in WebKit root - Overall WebKit architecture
