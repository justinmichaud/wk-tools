#!/usr/bin/env python3
"""Is the profile a PGO collection produced worth building against? A collection
behind a covered browser, or whose benchmark gave up early, still writes every
file the build expects. README.md, "The Mac lane", says what is asked of it."""
import argparse
import json
import os
import re
import subprocess
import sys

LIBRARIES = ("JavaScriptCore", "WebCore", "WebKit")
BENCHMARKS = ("speedometer3", "jetstream3", "motionmark")

MIN_FUNCTIONS = 1000
MIN_COVERAGE = 0.25   # of the combined profile's, per library; the thinnest leg measured was 53%


def profdata():
    cp = subprocess.run(["/usr/bin/xcrun", "--find", "llvm-profdata"],
                        capture_output=True, text=True)
    if cp.returncode != 0:
        sys.exit("mac-profile-check: xcrun cannot find llvm-profdata; no Xcode toolchain here")
    return cp.stdout.strip()


def summarise(tool, path):
    # Its own summary rather than a walk of the counters: the tool reads its own format.
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
    # `summary` is the reader: a real llvm-profdata, or a stub in a test.
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
    parser.add_argument("--profile-dir",
                        help="WK_PGO_DIR: what collect-pgo-profiles was given as --output-directory")
    parser.add_argument("--arch",
                        help="the compressed-profile sub path, which is the machine's arch")
    parser.add_argument("--read", metavar="JSON",
                        help="report a reading already taken (what --json wrote) "
                             "instead of taking one; needs no profile and no llvm-profdata")
    parser.add_argument("--json", help="write the whole reading here")
    args = parser.parse_args()

    if args.read:
        with open(args.read) as handle:
            reading = json.load(handle)
        for key in ("benchmarks", "combined", "compressed", "missing"):
            if key not in reading:
                parser.error(f"{args.read} has no '{key}': it is not a profile-check reading")
    elif args.profile_dir and args.arch:
        tool = profdata()
        reading = collect(args.profile_dir, args.arch, lambda path: summarise(tool, path))
    else:
        parser.error("--profile-dir and --arch to take a reading, or --read to report one")

    # Re-derived on every report, never stored: the floors live in one place.
    found = faults(reading)

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(reading, handle, indent=2, sort_keys=True)
    report(reading)

    if found:
        sys.stdout.flush()  # the readings above belong before the faults, down a pipe too
        print("\nthis profile is not one to build against:", file=sys.stderr)
        for fault in found:
            print(f"  {fault}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
