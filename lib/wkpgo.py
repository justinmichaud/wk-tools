#!/usr/bin/env python3
"""The parts of a PGO collection that belong to no one lane: mixing what came
back into a profile to build against, and deciding whether that profile is one
to build against at all. Every profdata operation is WebKit's own -- imported
from the checkout being built, so the weights and the llvm-profdata calls are
upstream's. What this file supplies is the list of libraries a profile is
carried in: three frameworks on the Apple ports, one shared library on GLib."""
import argparse
import glob
import importlib.machinery
import importlib.util
import json
import os
import re
import sys

MIN_FUNCTIONS = 1000
MIN_COVERAGE = 0.25   # of the combined profile's, per library; the thinnest leg measured was 53%


def upstream(scripts):
    path = os.path.join(scripts, "pgo-profile")
    if not os.path.isfile(path):
        sys.exit(f"wkpgo: no {path}. --scripts names a checkout's Tools/Scripts, and the\n"
                 "  mixing, the weights and the llvm-profdata calls are that checkout's.")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)   # pgo-profile imports webkitpy from beside itself
    loader = importlib.machinery.SourceFileLoader("wk_pgo_profile", path)
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def profile_utils():
    import webkitpy.llvm_profile_utils as utils   # upstream() has put Tools/Scripts on sys.path
    # Its llvm-profdata search asks /usr/bin/xcrun for each SDK, and off macOS that is
    # not a program at all: subprocess.run raises FileNotFoundError rather than
    # returning non-zero, so every merge on a Linux host dies before it starts.
    if not os.path.exists("/usr/bin/xcrun"):
        utils.locate_binary_xcrun = lambda sdk, binary_name: None
    return utils


def profdata(utils):
    if not utils.LLVMProfDataExecutable.detect_binaries():
        sys.exit("wkpgo: no llvm-profdata on PATH and none xcrun can find, so no profile\n"
                 "  here can be read or mixed. A .profraw is only readable by the\n"
                 "  toolchain that wrote it: run this where that clang is -- inside the\n"
                 "  cross environment for a board, in the Xcode toolchain for macOS.")
    return utils.LLVMProfDataExecutable


def cmd_plans(args):
    for plan, weight in upstream(args.scripts).BENCHMARK_GROUP_WEIGHTS:
        print(f"{plan} {weight}")


def cmd_mix(args):
    module = upstream(args.scripts)
    utils = profile_utils()
    profdata(utils)

    weights = dict(module.BENCHMARK_GROUP_WEIGHTS)
    plans = args.plan or list(weights)
    unweighed = [plan for plan in plans if plan not in weights]
    if unweighed:
        sys.exit(f"wkpgo mix: {', '.join(unweighed)} carries no weight in "
                 f"{args.scripts}/pgo-profile, so there is no ratio to mix it in at.\n"
                 f"  The benchmarks a profile is taken from are the ones upstream weighs: "
                 f"{', '.join(weights)}.")

    module.PROFILED_DYLIBS = [args.lib]   # one shared library on the GLib ports, not three frameworks
    per_plan = {}
    for plan in plans:
        raw = os.path.join(args.dir, plan, "diagnose")
        if not glob.glob(os.path.join(raw, f"{args.lib}*.profraw")):
            sys.exit(f"wkpgo mix: {raw} holds no {args.lib}*.profraw, so the {plan} leg\n"
                     "  wrote no profile. An instrumented build writes one per process at\n"
                     "  LLVM_PROFILE_FILE; a leg that produced none either ran an\n"
                     "  uninstrumented build or never started the browser.")
        per_plan[plan] = os.path.join(args.dir, plan)
        utils.merge_raw_profiles_in_directory_by_prefixes([args.lib], raw,
                                                          output_directory=per_plan[plan])

    combined = os.path.join(args.dir, "output")
    os.makedirs(combined, exist_ok=True)
    module.combine(argparse.Namespace(
        output=combined, **{plan: per_plan.get(plan) for plan in weights}))
    print(os.path.join(combined, f"{args.lib}.profdata"))


def summarise(tool, path):
    completed = tool.run(["show", path], capture_output=True, text=True)
    if completed.returncode:
        return {"error": (completed.stderr or completed.stdout).strip().splitlines()[:1]}
    out = {}
    for line in completed.stdout.splitlines():
        match = re.match(r"\s*([A-Za-z][A-Za-z /_-]+):\s*([0-9]+)\s*$", line)
        if match:
            out[match.group(1).strip().lower().replace(" ", "_")] = int(match.group(2))
    return out


def collect(profile_dir, libraries, plans, compressed_sub, summary):
    reading = {"profile_dir": profile_dir, "arch": compressed_sub or "",
               "libraries": list(libraries), "plans": list(plans),
               "benchmarks": {}, "combined": {}, "compressed": {}, "missing": []}

    for plan in plans:
        for library in libraries:
            path = os.path.join(profile_dir, plan, f"{library}.profdata")
            if not os.path.exists(path):
                reading["missing"].append(f"{plan}/{library}.profdata")
                continue
            reading["benchmarks"].setdefault(plan, {})[library] = summary(path)

    for library in libraries:
        path = os.path.join(profile_dir, "output", f"{library}.profdata")
        if not os.path.exists(path):
            reading["missing"].append(f"output/{library}.profdata")
        else:
            reading["combined"][library] = summary(path)
        if not compressed_sub:
            continue
        packed = os.path.join(profile_dir, compressed_sub, f"{library}.profdata.compressed")
        if not os.path.exists(packed):
            reading["missing"].append(f"{compressed_sub}/{library}.profdata.compressed")
        else:
            reading["compressed"][library] = os.path.getsize(packed)
    return reading


def libraries_of(reading):
    return reading.get("libraries") or sorted(reading.get("combined", {}))


def plans_of(reading):
    return reading.get("plans") or sorted(reading.get("benchmarks", {}))


def faults(reading):
    found = []
    for path in reading["missing"]:
        if path.startswith("output/"):
            found.append(f"{path} is missing: the benchmarks were never combined at "
                         "their weights")
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

    for library in libraries_of(reading):
        whole = reading["combined"].get(library, {}).get("total_functions", 0)
        for plan in plans_of(reading):
            leg = reading["benchmarks"].get(plan, {}).get(library)
            if leg is None or "error" in leg:
                continue
            if leg.get("maximum_function_count", 0) <= 0:
                found.append(f"{plan}'s {library} profile has no counter above zero: "
                             "that leg produced a file and ran nothing")
            if whole <= 0:
                continue
            share = leg.get("total_functions", 0) / whole
            if share < MIN_COVERAGE:
                found.append(f"{plan} touched {leg.get('total_functions', 0)} of "
                             f"{library}'s {whole} functions ({share:.0%}, floor "
                             f"{MIN_COVERAGE:.0%}): that leg gave up early")
    return found


def report(reading):
    libraries = libraries_of(reading)
    for library in libraries:
        summary = reading["combined"].get(library, {})
        packed = reading["compressed"].get(library)
        print(f"{library}: functions={summary.get('total_functions', '?')} "
              f"peak={summary.get('maximum_function_count', '?')}"
              + (f" compressed={packed}B" if packed is not None else ""))
    for plan in plans_of(reading):
        per = reading["benchmarks"].get(plan, {})
        print(f"{plan}: " + " ".join(
            f"{lib}={per.get(lib, {}).get('total_functions', '?')}" for lib in libraries)
            + " functions touched")


def cmd_check(args):
    if args.read:
        with open(args.read) as handle:
            reading = json.load(handle)
        for key in ("benchmarks", "combined", "compressed", "missing"):
            if key not in reading:
                sys.exit(f"{args.read} has no '{key}': it is not a profile-check reading")
    else:
        if not args.dir or not args.scripts:
            sys.exit("wkpgo check: --dir and --scripts to take a reading, or --read to "
                     "report one")
        module = upstream(args.scripts)
        tool = profdata(profile_utils())
        libraries = args.lib or module.PROFILED_DYLIBS
        plans = args.plan or [plan for plan, _ in module.BENCHMARK_GROUP_WEIGHTS]
        reading = collect(args.dir, libraries, plans, args.compressed,
                          lambda path: summarise(tool, path))

    found = faults(reading)   # re-derived on every report, never stored: the floors live here

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(reading, handle, indent=2, sort_keys=True)
    report(reading)

    if found:
        sys.stdout.flush()   # the readings above belong before the faults, down a pipe too
        print("\nthis profile is not one to build against:", file=sys.stderr)
        for fault in found:
            print(f"  {fault}", file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(prog="wkpgo", allow_abbrev=False)
    subparsers = parser.add_subparsers(required=True, dest="subcommand")

    plans = subparsers.add_parser("plans", help="the benchmarks upstream weighs, and at what")
    plans.add_argument("--scripts", required=True, help="a checkout's Tools/Scripts")
    plans.set_defaults(func=cmd_plans)

    mix = subparsers.add_parser("mix", help="merge each leg's .profraw and combine the "
                                            "legs at upstream's weights")
    mix.add_argument("--scripts", required=True, help="a checkout's Tools/Scripts")
    mix.add_argument("--dir", required=True, help="the collection: <dir>/<plan>/diagnose/*.profraw")
    mix.add_argument("--lib", required=True, help="the library the profile is carried in")
    mix.add_argument("--plan", action="append", help="a leg to mix in (default: all upstream weighs)")
    mix.set_defaults(func=cmd_mix)

    check = subparsers.add_parser("check", help="is this collection one to build against")
    check.add_argument("--dir", help="the collection to read")
    check.add_argument("--scripts", help="a checkout's Tools/Scripts")
    check.add_argument("--lib", action="append", help="a library to read (default: upstream's)")
    check.add_argument("--plan", action="append", help="a leg to read (default: upstream's)")
    check.add_argument("--compressed", metavar="SUBPATH",
                       help="also expect <dir>/<SUBPATH>/<lib>.profdata.compressed, "
                            "which is what an Apple measured build reads")
    check.add_argument("--read", metavar="JSON",
                       help="report a reading already taken instead of taking one; "
                            "needs no profile and no llvm-profdata")
    check.add_argument("--json", help="write the whole reading here")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
