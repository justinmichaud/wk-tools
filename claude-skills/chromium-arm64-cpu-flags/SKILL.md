---
name: chromium-arm64-cpu-flags
description: Use when adding CPU-targeting or ISA flags to a Chromium/GN build — tuning for a specific arm64 core (Neoverse, Cortex-A7x, Apple), building Chromium for a particular device, or debugging a build that fails with "instruction requires: sve2 or sme" / a similar assembler ISA error. Explains why a global -march breaks Chromium's per-file SIMD arches and why -mcpu is the flag that composes.
user-invocable: true
---

# CPU-targeting flags in a Chromium (GN) build: use `-mcpu`, never a global `-march`

To build Chromium tuned for a specific arm64 core, add **`-mcpu=<core>`**. Do **not** add a global
`-march=…` — it breaks the build, and the failure looks nothing like a flag problem.

## Why a global `-march` breaks it

Chromium compiles specific translation units with their own, higher `-march` so they can use newer SIMD
ISAs — e.g. libyuv's `row_sve.cc` is compiled with `-march=armv9-a+i8mm+sve2`. Two facts combine badly:

1. **GN emits a target's own `cflags` *before* config `cflags`.**
2. **clang honors the *last* `-march` on the command line.**

So a `-march` added in a shared config lands *after* the per-file one and **clobbers** it. The SIMD TU
then gets compiled for your baseline arch and the assembler rejects its intrinsics/asm:

```
error: instruction requires: sve2 or sme
```

The error names an ISA feature, so it reads like a toolchain or source problem. It is neither — it is
your flag overriding a per-target `-march`.

## Why `-mcpu` composes

clang lets an explicit per-target `-march` override the arch implied by `-mcpu` **regardless of order**.
So with `-mcpu`:

- SIMD TUs keep their own `-march` and still build.
- Every other TU gets the `-mcpu` arch plus that core's tuning.

That is the behavior you want, and it is the whole reason to prefer `-mcpu` here.

## Where to put it

Inject it into the `current_cpu == "arm64"` branch of `config("compiler")` in
`build/config/compiler/BUILD.gn`. **There is no global `extra_cflags` GN arg** — Chromium removed it, so
editing that config is the supported path. Anchor the edit on a stable nearby line (e.g. the
`__ARM_NEON__=1` define) and re-check the anchor after a rebase; this file moves between releases.

## Targeting a CPU that may lack crypto extensions

If the binary must also run on a core without the optional crypto extensions, add **`+nocrypto`**
(e.g. `-mcpu=neoverse-n1+nocrypto`). This only keeps AES/SHA/PMULL out of *codegen* — BoringSSL still
selects its accelerated paths at **runtime** via `getauxval`, so on a machine that has the extensions you
lose nothing. Verify that runtime detection is actually in play on your target rather than assuming it.

## Verify the flags landed

Read the real command line rather than trusting the GN edit:

```bash
gn args out/Release --list | grep -i cpu          # what GN thinks the target is
ninja -C out/Release -t commands <some_target> | tr ' ' '\n' | grep -E '^-m(cpu|arch)' | sort -u
```

Expect exactly one `-mcpu=…` on ordinary TUs, and a surviving per-file `-march=…` on the SIMD ones.
