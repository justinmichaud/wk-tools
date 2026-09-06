#!/usr/bin/env python3
"""Is the profile a PGO collection produced worth building against?

A collection that ran behind a throttled browser, or whose benchmark gave up
after one iteration, still writes every file the build expects; the measured
build then links against a profile that names the wrong code as hot, and the
regression it invents is indistinguishable from the patch's. So the collection
is read back before the second phase starts.

What is asked of it, per library (JavaScriptCore, WebCore, WebKit -- pgo-profile's
own PROFILED_DYLIBS) and per benchmark:

  present    the merged profile exists, and so does the weighted combination and
             the compressed copy the measured build reads
  populated  llvm-profdata reports functions, and a maximum function count above
             zero -- an empty profile shows neither
  covered    each benchmark reached a real fraction of the functions the
             combined profile knows about, per library. Counts cannot be
             compared across benchmarks -- measured 2026-09-06, jetstream3's
             total count in WebKit is 545x smaller than motionmark's, because
             one is a JavaScript benchmark and the other is a rendering one --
             but how much of a library each one *touches* is stable, and a leg
             that gave up early is a leg that touched little of it.

  bench/mac-profile-check.py --profile-dir <dir> --arch <arch> [--json <path>]
"""
import argparse
import json
import os
import re
import subprocess
import sys

LIBRARIES = ("JavaScriptCore", "WebCore", "WebKit")
BENCHMARKS = ("speedometer3", "jetstream3", "motionmark")

# A collection that ran is orders of magnitude above this; one that died in its
# first iteration is below it.
MIN_FUNCTIONS = 1000

# Of the combined profile's functions for that library. Measured 2026-09-06 over
# a whole collection in a guest, the thinnest leg was motionmark's 12,783 of
# JavaScriptCore's 24,129 -- 53%; every other one was higher.
MIN_COVERAGE = 0.25


def profdata():
    cp = subprocess.run(["/usr/bin/xcrun", "--find", "llvm-profdata"],
                        capture_output=True, text=True)
    if cp.returncode != 0:
        sys.exit("mac-profile-check: xcrun cannot find llvm-profdata; no Xcode toolchain here")
    return cp.stdout.strip()


def summarise(tool, path):
    """`llvm-profdata show`'s header, as numbers. Its own summary rather than a
    walk of the counters: the tool is the reader of its own format."""
    cp = subprocess.run([tool, "show", path], capture_output=True, text=True)
    if cp.returncode != 0:
        return {"error": (cp.stderr or cp.stdout).strip().splitlines()[:1]}
    out = {}
    for line in cp.stdout.splitlines():
        match = re.match(r"\s*([A-Za-z][A-Za-z /_-]+):\s*([0-9]+)\s*$", line)
        if match:
            out[match.group(1).strip().lower().replace(" ", "_")] = int(match.group(2))
    return out


def collect(profile_dir, arch, summary):
    """Read every profile the collection should have written. `summary` is the
    reader (a real llvm-profdata, or a stub in a test)."""
    reading = {"profile_dir": profile_dir, "arch": arch,
               "benchmarks": {}, "combined": {}, "compressed": {}, "missing": []}

    for benchmark in BENCHMARKS:
        for library in LIBRARIES:
            path = os.path.join(profile_dir, benchmark, f"{library}.profdata")
            if not os.path.exists(path):
                reading["missing"].append(f"{benchmark}/{library}.profdata")
                continue
            reading["benchmarks"].setdefault(benchmark, {})[library] = summary(path)

    for library in LIBRARIES:
        path = os.path.join(profile_dir, "output", f"{library}.profdata")
        if not os.path.exists(path):
            reading["missing"].append(f"output/{library}.profdata")
        else:
            reading["combined"][library] = summary(path)
        compressed = os.path.join(profile_dir, arch, f"{library}.profdata.compressed")
        if not os.path.exists(compressed):
            reading["missing"].append(f"{arch}/{library}.profdata.compressed")
        else:
            reading["compressed"][library] = os.path.getsize(compressed)
    return reading


def faults(reading):
    """The verdict, as a list of reasons -- separated from the reading so it can
    be exercised against a profile that does not exist."""
    found = []
    for path in reading["missing"]:
        if path.startswith("output/"):
            found.append(f"{path} is missing: the three benchmarks were never "
                         "combined at their weights")
        elif path.endswith(".compressed"):
            found.append(f"{path} is missing, and it is the copy the measured build reads")
        else:
            found.append(f"{path} was never written -- that benchmark's leg did not finish")

    for library, summary in reading["combined"].items():
        if "error" in summary:
            found.append(f"llvm-profdata cannot read output/{library}.profdata: {summary['error']}")
            continue
        functions = summary.get("total_functions", 0)
        peak = summary.get("maximum_function_count", 0)
        if functions < MIN_FUNCTIONS:
            found.append(f"output/{library}.profdata carries {functions} functions, under "
                         f"{MIN_FUNCTIONS}: nothing ran long enough to profile")
        if peak <= 0:
            found.append(f"output/{library}.profdata's maximum function count is {peak}: "
                         "every counter in it is zero")

    for library in LIBRARIES:
        whole = reading["combined"].get(library, {}).get("total_functions", 0)
        for benchmark in BENCHMARKS:
            leg = reading["benchmarks"].get(benchmark, {}).get(library)
            if leg is None or "error" in leg:
                continue
            if leg.get("maximum_function_count", 0) <= 0:
                found.append(f"{benchmark}'s {library} profile has no counter above zero: "
                             "that leg produced a file and ran nothing")
            if whole <= 0:
                continue
            share = leg.get("total_functions", 0) / whole
            if share < MIN_COVERAGE:
                found.append(f"{benchmark} touched {leg.get('total_functions', 0)} of "
                             f"{library}'s {whole} functions ({share:.0%}, floor "
                             f"{MIN_COVERAGE:.0%}): that leg gave up early")
    return found


def report(reading):
    for library in LIBRARIES:
        summary = reading["combined"].get(library, {})
        print(f"{library}: functions={summary.get('total_functions', '?')} "
              f"peak={summary.get('maximum_function_count', '?')} "
              f"compressed={reading['compressed'].get(library, '?')}B")
    for benchmark in BENCHMARKS:
        per = reading["benchmarks"].get(benchmark, {})
        print(f"{benchmark}: " + " ".join(
            f"{lib}={per.get(lib, {}).get('total_functions', '?')}" for lib in LIBRARIES)
            + " functions touched")


def main():
    parser = argparse.ArgumentParser(prog="mac-profile-check", allow_abbrev=False)
    parser.add_argument("--profile-dir", required=True,
                        help="WK_PGO_DIR: what collect-pgo-profiles was given as --output-directory")
    parser.add_argument("--arch", required=True,
                        help="the compressed-profile sub path, which is the machine's arch")
    parser.add_argument("--json", help="write the whole reading here")
    args = parser.parse_args()

    tool = profdata()
    reading = collect(args.profile_dir, args.arch, lambda path: summarise(tool, path))
    found = faults(reading)

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(reading, handle, indent=2, sort_keys=True)
    report(reading)

    if found:
        print("\nthis profile is not one to build against:", file=sys.stderr)
        for fault in found:
            print(f"  {fault}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
