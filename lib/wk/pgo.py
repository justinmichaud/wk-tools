"""Profile-guided builds: the facts the Mac and the boards share (build/mac-pgo.sh, lib/wk/bench/board.py), and a board's cycle -- an
instrumented slot, `wk bench run --collect` per benchmark, a mix, the measured build -- as `wk` steps of one graph.
Every profdata operation is WebKit's own, imported from the checkout: `python3 -m wk.pgo mix|check`."""

import argparse
import concurrent.futures as futures
import glob
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import sys

from wk import act, fleet, images, job, project, record as progress, sched
from wk.act import Refused, die, info, log, warn

BENCHMARKS = ("speedometer3", "jetstream3", "motionmark")   # the weights are upstream's (Tools/Scripts/pgo-profile); `mix` refuses one it does not weigh
COLLECT_TIMEOUT = 7200   # an instrumented run is several times slower than the measured one a plan's timeout is sized for
GLIB_LIB = "WPEWebKit"   # a GLib port links one shared library, where the Apple ports carry three (PROFILED_DYLIBS)
BOARD_DIR = "/var/wk/pgo"   # baked in as PGO_PROFILE_DIR, so a browser started by hand still writes somewhere writable
BOARD_FILE = BOARD_DIR + "/" + GLIB_LIB + "_%p.profraw"   # one file per process: the browser's and each web process's counters merge as peers
COLLECT, USE = project.get("PGO_COLLECT"), project.get("PGO_USE")
PRESETS = ("wpe-cross", COLLECT, USE)
MIN_FUNCTIONS = 1000
MIN_COVERAGE = 0.25   # of the combined profile's, per library; the thinnest leg measured was 53%
SHA = re.compile(r"^[0-9a-f]{40}$")
USAGE = "usage: wk sysimage webkit %s --commit <sha> --slot <name> [--workspace <ws>] [--preset <p>] [--detach|--stop]"


# The readings travel beside the products (lib/wk/bench/mac_pgo.py's `PgoCollect.evidence`), read from the build
# the workspace holds now, on the machine holding it, each verdict re-derived by its own checker.
GATES = r'''
set -u
shopt -s nullglob
seen=0
for f in "$SRC"/WebKitBuild/*/wk-browser-check.json; do
    seen=1; printf 'browser check %s\n' "$f"
    python3 "$TOOLS/bench/mac-browser-check.py" --read "$f" 2>&1 | sed 's/^/  /'
done
for f in "$SRC"/WebKitBuild/*/wk-profile-check.json "$SRC"/WebKitBuild/wk-pgo/*/profile-check.json; do
    [ -f "$f" ] || continue
    seen=1; printf 'profile check %s\n' "$f"
    PYTHONPATH="$TOOLS/lib" python3 -m wk.pgo check --read "$f" 2>&1 | sed 's/^/  /'
done
for f in "$SRC"/WebKitBuild/*/wk-payload-pins; do
    seen=1; printf 'benchmark payloads %s\n' "$f"
    sed 's/^/  /' "$f"
done
[ "$seen" = 1 ] || printf 'no readings under %s/WebKitBuild -- only a profile-guided build
collects them (build/mac-pgo.sh beside the products, a board cycle under
wk-pgo/); every other build has no gates.\n' "$SRC"
'''


def show_gates(driver, name):
    r = driver.exec(name, ["bash", "-c", "SRC=%s TOOLS=%s\n%s" % (shlex.quote(driver.src(name)), shlex.quote(driver.tools(name)), GATES)])
    sys.stdout.write(r.out)
    sys.stderr.write(r.err)
    return r.rc


def collect_timeout(env):
    return env.get("WK_PGO_COLLECT_TIMEOUT") or str(COLLECT_TIMEOUT)


def pgo_dir_in(slot):
    return "%s/%s/%s/%s" % (project.get("SRC"), project.get("BUILD_DIR"), images.PGO_SUBDIR, slot)


def board_collection(ws, slot, env, timeout=None):
    """A board's `--collect`: where its one iteration's profile lands, and how long it may take."""
    return {"pgo_dir": images.pgo_dir(ws, slot, env), "pgo_board": BOARD_DIR, "pgo_file": BOARD_FILE,
            "count": "1", "timeout": timeout or collect_timeout(env)}


def profile_path(slot):
    return "%s/output/%s.profdata" % (pgo_dir_in(slot), GLIB_LIB)


def steps(step, holds, board, ws, spec, on, place, commit, slot, needs):
    """The cycle's phases, each on the machine holding the image workspace, into whose build directory the board writes."""
    instr, res, dev, w = images.instr_slot(slot), images.build_resource(on), "device:" + board, ["--workspace", ws]
    out = [step("instr:%s:%s" % (ws, slot), on, tuple(needs), (res,),
                holds(spec, ws, "--slot", instr, "--commit", commit, "--preset", COLLECT),
                ["sysimage", "webkit", spec] + w + ["--commit", commit, "--slot", instr, "--preset", COLLECT]),
           step("deploy:%s:%s" % (board, instr), on, ("instr:%s:%s" % (ws, slot),), (dev,), None,
                ["bench", "deploy", ws, board, "--slot", instr], place)]
    legs = []
    for plan in BENCHMARKS:
        legs.append("collect:%s:%s:%s" % (board, slot, plan))
        out.append(step(legs[-1], on, ("deploy:%s:%s" % (board, instr),), (dev,), None,
                        ["bench", "run", ws, plan, "--system", board, "--slot", instr, "--collect"], place))
    out.append(step("mix:%s:%s" % (ws, slot), on, legs, (res,), None,
                    ["sysimage", "build", spec] + w + ["--stage", "pgo-mix", "--slot", slot]))
    out.append(step("slot:%s:%s" % (ws, slot), on, ("mix:%s:%s" % (ws, slot),), (res,),
                    holds(spec, ws, "--slot", slot, "--commit", commit),
                    ["sysimage", "webkit", spec] + w + ["--commit", commit, "--slot", slot, "--preset", USE]))
    return out


class Cycle:
    """`wk sysimage webkit` of a 2.52+ yocto image preset; `build`, `mode_of` and `pool` are injectable."""

    def __init__(self, reg, p, spec, clock, build=None, mode_of=None, pool=futures.ThreadPoolExecutor):
        self.reg, self.p, self.spec, self.clock = reg, p, spec, clock
        self.here, self.env, self.store, self.root = reg.machine, reg.env, reg.store, str(reg.root)
        self.name = p["IMG_PRESET"]
        self.wk = os.path.join(self.root, "wk")
        self.build, self.mode_of, self.pool = build or self.yocto_build, mode_of or self.board_mode, pool
        self.log = ""

    def yocto_build(self, rest):
        from wk.sysimage import yocto
        return yocto.Yocto(self.reg, self.p, self.name, self.clock).build(rest)

    def board_mode(self, name):
        from wk.bench import board
        try:
            return board.for_board(self.root, self.reg, "", self.clock, name).driver.probe()
        except Refused:
            return "unreachable"

    def records(self):
        return progress.Records(self.store.records_dir(), clock=self.clock, env=self.env, machine=self.here)

    def webkit(self, rest):
        from wk.sysimage import task
        o = task.options(rest, ("--detach", "--stop"), ("--commit", "--slot", "--workspace", "--preset"),
                         USAGE % self.name)
        commit, slot, preset = o.get("--commit") or "", o.get("--slot") or "", o.get("--preset") or ""
        if slot:
            images.check_slot_name(slot)
        ws = o.get("--workspace") or images.image_ws(self.name, self.env)
        if o.get("--stop"):
            if not slot:
                die("usage: wk sysimage webkit %s --slot <name> --stop\n    --stop stops the cycle running for one slot, so it needs --slot"
                    % self.name)
            if commit or preset or o.get("--detach") or act.dry_run():
                die("'wk sysimage webkit %s --slot %s --stop' takes nothing else: no --commit, --preset,\n"
                    "    --detach or --dry-run." % (self.spec, slot))
            return self.stop(ws, slot)
        if preset and preset not in PRESETS:
            die("--preset takes one of %s, not '%s': each pgo preset is one phase of the cycle,\n"
                "    'wpe-cross' a slot built without a profile." % (", ".join(PRESETS), preset))
        if not (commit and slot):
            die(USAGE % self.name + "\n    a slot needs both --commit <sha> and --slot <name>")
        if not SHA.match(commit):
            die("--commit takes a full sha (40 hex digits), got '%s'" % commit)
        if preset:
            return self.phase(ws, commit, slot, preset, o.get("--detach"))
        if act.dry_run():
            return self.plan(ws, commit, slot)
        board = self.require_board()
        if o.get("--detach"):
            pid = job.detach(self.here, [self.wk, "sysimage", "webkit", self.spec, "--workspace", ws, "--commit", commit,
                                         "--slot", slot], self.log_path(ws, slot))
            info("detached as pid %d -- this end can go away" % pid)
            log("  follow:  tail -f %s" % self.log_path(ws, slot))
            return 0
        return self.cycle(ws, commit, slot, board)

    def phase(self, ws, commit, slot, preset, detach):
        extra = []
        if preset == "wpe-cross":
            warn("slot '%s' is being built WITHOUT a profile: use it only for a comparison against a\n"
                 "  profile-guided slot ('wk sysimage ls' and each run's env.json record it as wpe-cross)." % slot)
        elif preset == COLLECT:
            self.here.remove(images.pgo_dir(ws, images.measured_slot(slot), self.env))   # legs of the last instrumented build say nothing of this one
        else:
            extra = ["--pgo-profile", profile_path(slot)]
        return self.build(["--stage", "webkit", "--workspace", ws, "--commit", commit, "--slot", slot, "--preset", preset]
                          + extra + (["--detach"] if detach else []))

    def board(self):
        """The profile's own IMG_MACHINE, if the fleet has a board of that name: two arms of one board share it."""
        m = self.p["IMG_MACHINE"]
        try:
            conf = self.reg.fleet.load(m) if m else None
        except fleet.ConfError:
            conf = None
        return m if conf and conf.get("kind") == "board" else ""

    def require_board(self):
        board = self.board()
        if not board:
            die("no fleet board carries %s, so there is nowhere to collect a profile:\n    the fleet has no board named by "
                "its IMG_MACHINE (%s)." % (self.name, images.conf_path(self.name, self.env)))
        mode = self.mode_of(board)
        if not mode.startswith("bench %s-" % self.name):
            die("%s is '%s', not a bench system built from %s. Put it into the image first:\n"
                "        wk sysimage write --from %s --disk %s:<device>\n        wk boot %s"
                % (board, mode, self.name, self.name, board, board))
        return board

    def log_path(self, ws, slot):
        return os.path.join(self.store.ws_dir(ws), "pgo-%s.log" % slot)

    def step(self, sid, on, needs, holds, done, words, place=""):
        return sched.wk_step(self.here, self.wk, lambda s: self.log, sid, on, needs, holds, done, words, place)

    def holds(self, spec, ws, *rest):
        return sched.wk_yes(self.here, [self.wk, "sysimage", "holds", spec, "--workspace", ws] + list(rest))

    def graph(self, ws, commit, slot, board):
        named, me = images.spec_machine(self.spec), progress.machine_name(self.env, self.here)
        try:
            on = images.ws_machine(named, "" if named else self.reg.ws_place(ws), me)
        except LookupError as e:
            die(str(e))
        place = named if named and named != me else ""
        return sched.validate(steps(self.step, self.holds, board, ws, self.spec, on, place, commit, slot, ()))

    def say_collection(self, ws, slot):
        log("  collection  %s" % images.pgo_dir(ws, slot, self.env))
        log("  benchmarks  %s, mixed at WebKit's own weights (Tools/Scripts/pgo-profile)" % " ".join(BENCHMARKS))

    def plan(self, ws, commit, slot):
        board = self.board()
        log("would build slot '%s' of %s as a profile-guided build" % (slot, self.name))
        log("  workspace   %s" % ws)
        log("  board       %s" % ("%s -- it has to be running this image; wk boot %s --status" % (board, board) if board
                                  else "the fleet has no board named by IMG_MACHINE, so this refuses"))
        self.say_collection(ws, slot)
        graph = self.graph(ws, commit, slot, board or "<board>")
        sched.render(graph, sched.done_ids(graph, self.pool), sys.stderr)
        log("dry run -- nothing was built.")
        return 0

    def cycle(self, ws, commit, slot, board):
        order = sched.plan_order(self.graph(ws, commit, slot, board))
        self.log = self.log_path(ws, slot)
        self.here.mkdir(os.path.dirname(self.log))
        t = self.records().begin("pgo", "here", "%s/%s" % (ws, slot), "wk sysimage webkit %s --workspace %s --slot %s --stop"
                                 % (self.spec, ws, slot), self.log, [s.command for s in order])
        info("profile-guided slot '%s' of %s in workspace %s: instrument, collect on %s, rebuild" % (slot, self.name, ws, board))
        self.say_collection(ws, slot)

        def announce(event, step, rc=0):
            t.step_event(order.index(step) + 1, event)
            log(sched.say_event(order, event, step, rc, self.log))

        rc = 1
        try:
            with job.Signals():
                s = sched.Scheduler(order, announce, pool=self.pool)
                rc = s.run_all()
                for line in sched.summary(s):
                    log(line)
                if rc:
                    die("the cycle for '%s' stopped (above); what was collected is in %s,\n    and re-running this command "
                        "takes up what is left." % (slot, images.pgo_dir(ws, slot, self.env)))
        except job.Interrupted as e:
            rc = "cancelled"
            raise Refused(job.EXIT_OF.get(e.signum, 130))
        except Refused as e:
            rc = e.status
            raise
        finally:
            t.end(rc)
        info("slot '%s' is a profile-guided build of %s" % (slot, commit[:12]))
        d = images.pgo_dir(ws, slot, self.env)
        log("  profile     %s/output/%s.profdata" % (d, GLIB_LIB))
        log("  readings    %s/profile-check.json  ('wk sysimage ls' has the slot)" % d)
        log("  next:       wk bench deploy %s %s --slot %s" % (ws, board, slot))
        return 0

    def stop(self, ws, slot):
        name = "%s/%s" % (ws, slot)
        t = self.records().find("pgo", name)
        if t is None or not t.alive(None):
            log("no pgo is running for '%s' -- 'wk status' says what it last did" % name)
            return 0
        info("stopping the cycle for '%s' (pid %s)" % (name, t.field("pid")))
        if not job.kill(None, ws, t, "cancelled", self.here, self.clock, self.env):
            die("the detached run of '%s' outlived a TERM and a KILL:\n    ps -p %s" % (name, t.field("pid")))
        return 0


def upstream(scripts):
    path = os.path.join(scripts, "pgo-profile")
    if not os.path.isfile(path):
        sys.exit("wk.pgo: no %s: --scripts names a checkout's Tools/Scripts." % path)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)   # pgo-profile imports webkitpy from beside itself
    loader = importlib.machinery.SourceFileLoader("wk_pgo_profile", path)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def profile_utils():
    """webkitpy's llvm-profdata search runs /usr/bin/xcrun for each SDK, which off macOS raises FileNotFoundError."""
    import webkitpy.llvm_profile_utils as utils
    if not os.path.exists("/usr/bin/xcrun"):
        utils.locate_binary_xcrun = lambda sdk, binary_name: None
    return utils


def profdata(utils):
    if not utils.LLVMProfDataExecutable.detect_binaries():
        sys.exit("wk.pgo: no llvm-profdata on PATH and none xcrun can find. A .profraw is only\n"
                 "  readable by the toolchain that wrote it: run this where that clang is.")
    return utils.LLVMProfDataExecutable


def cmd_mix(args):
    module = upstream(args.scripts)
    utils = profile_utils()
    profdata(utils)
    weights = dict(module.BENCHMARK_GROUP_WEIGHTS)
    plans = args.plan or list(weights)
    unweighed = [plan for plan in plans if plan not in weights]
    if unweighed:
        sys.exit("wk.pgo mix: %s carries no weight in %s/pgo-profile; upstream weighs: %s."
                 % (", ".join(unweighed), args.scripts, ", ".join(weights)))
    module.PROFILED_DYLIBS = [args.lib]
    per_plan = {}
    for plan in plans:
        raw = os.path.join(args.dir, plan, "diagnose")
        if not glob.glob(os.path.join(raw, "%s*.profraw" % args.lib)):
            sys.exit("wk.pgo mix: %s holds no %s*.profraw: the %s leg ran an uninstrumented\n  build or never started "
                     "the browser." % (raw, args.lib, plan))
        per_plan[plan] = os.path.join(args.dir, plan)
        utils.merge_raw_profiles_in_directory_by_prefixes([args.lib], raw, output_directory=per_plan[plan])
    combined = os.path.join(args.dir, "output")
    os.makedirs(combined, exist_ok=True)
    module.combine(argparse.Namespace(output=combined, **{plan: per_plan.get(plan) for plan in weights}))
    print(os.path.join(combined, "%s.profdata" % args.lib))


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
    reading = {"profile_dir": profile_dir, "arch": compressed_sub or "", "libraries": list(libraries), "plans": list(plans),
               "benchmarks": {}, "combined": {}, "compressed": {}, "missing": []}
    for plan in plans:
        for library in libraries:
            path = os.path.join(profile_dir, plan, "%s.profdata" % library)
            if not os.path.exists(path):
                reading["missing"].append("%s/%s.profdata" % (plan, library))
                continue
            reading["benchmarks"].setdefault(plan, {})[library] = summary(path)
    for library in libraries:
        path = os.path.join(profile_dir, "output", "%s.profdata" % library)
        if not os.path.exists(path):
            reading["missing"].append("output/%s.profdata" % library)
        else:
            reading["combined"][library] = summary(path)
        if not compressed_sub:
            continue
        packed = os.path.join(profile_dir, compressed_sub, "%s.profdata.compressed" % library)
        if not os.path.exists(packed):
            reading["missing"].append("%s/%s.profdata.compressed" % (compressed_sub, library))
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
            found.append("%s is missing: the benchmarks were never combined at their weights" % path)
        elif path.endswith(".compressed"):
            found.append("%s is missing, and it is the copy the measured build reads" % path)
        else:
            found.append("%s was never written -- that benchmark's leg did not finish" % path)
    for library, summary in reading["combined"].items():
        if "error" in summary:
            found.append("llvm-profdata cannot read output/%s.profdata: %s" % (library, summary["error"]))
            continue
        functions, peak = summary.get("total_functions", 0), summary.get("maximum_function_count", 0)
        if functions < MIN_FUNCTIONS:
            found.append("output/%s.profdata carries %d functions, under %d: nothing ran long enough to profile"
                         % (library, functions, MIN_FUNCTIONS))
        if peak <= 0:
            found.append("output/%s.profdata's maximum function count is %d: every counter in it is zero" % (library, peak))
    for library in libraries_of(reading):
        whole = reading["combined"].get(library, {}).get("total_functions", 0)
        for plan in plans_of(reading):
            leg = reading["benchmarks"].get(plan, {}).get(library)
            if leg is None or "error" in leg:
                continue
            if leg.get("maximum_function_count", 0) <= 0:
                found.append("%s's %s profile has no counter above zero: that leg produced a file and ran nothing" % (plan, library))
            if whole <= 0:
                continue
            share = leg.get("total_functions", 0) / whole
            if share < MIN_COVERAGE:
                found.append("%s touched %d of %s's %d functions (%.0f%%, floor %.0f%%): that leg gave up early"
                             % (plan, leg.get("total_functions", 0), library, whole, share * 100, MIN_COVERAGE * 100))
    return found


def report(reading):
    libraries = libraries_of(reading)
    for library in libraries:
        summary, packed = reading["combined"].get(library, {}), reading["compressed"].get(library)
        print("%s: functions=%s peak=%s" % (library, summary.get("total_functions", "?"), summary.get("maximum_function_count", "?"))
              + (" compressed=%dB" % packed if packed is not None else ""))
    for plan in plans_of(reading):
        per = reading["benchmarks"].get(plan, {})
        print("%s: " % plan + " ".join("%s=%s" % (lib, per.get(lib, {}).get("total_functions", "?")) for lib in libraries)
              + " functions touched")


def cmd_check(args):
    if args.read:
        with open(args.read) as handle:
            reading = json.load(handle)
        for key in ("benchmarks", "combined", "compressed", "missing"):
            if key not in reading:
                sys.exit("%s has no '%s': it is not a profile-check reading" % (args.read, key))
    else:
        if not args.dir or not args.scripts:
            sys.exit("wk.pgo check: --dir and --scripts to take a reading, or --read to report one")
        module = upstream(args.scripts)
        tool = profdata(profile_utils())
        libraries = args.lib or module.PROFILED_DYLIBS
        plans = args.plan or [plan for plan, _ in module.BENCHMARK_GROUP_WEIGHTS]
        reading = collect(args.dir, libraries, plans, args.compressed, lambda path: summarise(tool, path))
    found = faults(reading)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(reading, handle, indent=2, sort_keys=True)
    report(reading)
    if found:
        sys.stdout.flush()
        print("\nthis profile is not one to build against:", file=sys.stderr)
        for fault in found:
            print("  %s" % fault, file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m wk.pgo", allow_abbrev=False)
    sub = parser.add_subparsers(required=True, dest="subcommand")
    mix = sub.add_parser("mix", help="merge each leg's .profraw and combine the legs at upstream's weights")
    mix.add_argument("--scripts", required=True, help="a checkout's Tools/Scripts")
    mix.add_argument("--dir", required=True, help="the collection: <dir>/<plan>/diagnose/*.profraw")
    mix.add_argument("--lib", required=True, help="the library the profile is carried in")
    mix.add_argument("--plan", action="append", help="a leg to mix in (default: all upstream weighs)")
    mix.set_defaults(func=cmd_mix)
    check = sub.add_parser("check", help="is this collection one to build against")
    check.add_argument("--dir", help="the collection to read")
    check.add_argument("--scripts", help="a checkout's Tools/Scripts")
    check.add_argument("--lib", action="append", help="a library to read (default: upstream's)")
    check.add_argument("--plan", action="append", help="a leg to read (default: upstream's)")
    check.add_argument("--compressed", metavar="SUBPATH",
                       help="also expect <dir>/<SUBPATH>/<lib>.profdata.compressed, which is what an Apple measured build reads")
    check.add_argument("--read", metavar="JSON", help="report a reading already taken; needs no profile and no llvm-profdata")
    check.add_argument("--json", help="write the whole reading here")
    check.set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
