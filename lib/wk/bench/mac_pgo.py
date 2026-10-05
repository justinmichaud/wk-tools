"""build/mac-pgo.sh's middle phase on a Mac: the instrumented browser profiled against pinned payloads, and its readings."""

import os
import sys

from wk import act, images, pgo, screen
from wk.act import Refused, die, log
from wk.bench import seed
from wk.bench.mac import CHECK, console_row, screen_row
from wk.bench.mac_ab import PGO_INSTR, PGO_STATE, q
from wk.clock import Clock
from wk.lock import Lock
from wk.machine import Local, isolated_module, lib_argv
from wk.quiet import RAISER


class PgoCollect:

    def __init__(self, root, m, env, src, clock=None):
        self.root, self.m, self.env, self.src = str(root), m, env, src
        self.clock = clock
        self.state = os.path.join(env.get("HOME", ""), PGO_STATE)
        self.scripts = os.path.join(src, "Tools", "Scripts")

    def faults(self):
        """Every reason, not the first: a throttled collection looks exactly like a good one."""
        ok, said = console_row(self.m)
        out = [] if ok is True else [said]
        if not self.m.run(lib_argv(self.root, "bench/mac-pyobjc.sh", "wk_pyobjc_have")).ok:
            return out + ["no pyobjc: run-benchmark cannot size the screen and no raiser can hold the browser in front"]
        if not self.m.run(["/usr/bin/python3", "-c", "import AppKit, sys; sys.exit(0 if AppKit.NSScreen.mainScreen() else 1)"]).ok:
            out.append("there is no main screen, so nothing can be drawn at all")
        ok, said = screen_row(self.m, self.root)
        return out + ([said] if ok is False else [])

    def read(self, rel):
        try:
            return self.m.read(os.path.join(self.scripts, rel))
        except OSError:
            return None

    def pins(self):
        """speedometer3 and jetstream3 name a moving branch, so each benchmark is pinned by its upstream commit first."""
        from wk.store import Store
        store = Store(self.env)
        lock, out = Lock(store, self.m, self.clock or Clock()), []
        for plan in pgo.BENCHMARKS:
            d = seed.pin(self.m, lock, store, self.read, plan)[1]
            if not d or not self.m.isdir(d):
                die("could not pin the %s payload, so the collection could profile an unpinned revision of it" % plan)
            out.append((plan, d))
        return out

    def argv(self, instr, profile, arch, pins):
        custom = [w for plan, d in pins for w in ("--benchmark-custom-options", plan, "local-copy:" + d, "timeout:" + pgo.collect_timeout(self.env))]
        return (["env", "WK_WEBKIT_SCRIPTS=" + self.scripts, "/usr/bin/python3", os.path.join(self.scripts, "collect-pgo-profiles"),
                 "--run-benchmark-harness", os.path.join(self.root, "build", "pgo-run-benchmark.py"), "--benchmarks"] + list(pgo.BENCHMARKS)
                + ["--output-directory", profile, "--compressed-profile-sub-path", arch, "--build-directory", instr, "--browser", "minibrowser"]
                + custom)

    def run(self, instr, profile, arch):
        if act.dry_run():
            print("rm -rf %s && %s" % (q(profile), q(*self.argv(instr, profile, arch, [("<each benchmark>", "<pinned payload>")]))))
            return 0
        faults = self.faults()
        if faults:
            die("this machine cannot present an unthrottled browser, so every profile it collected would be of a throttled one:\n"
                "%s\n    Collect on a machine that can -- the benchmark install." % "\n".join("  " + f for f in faults))
        self.m.mkdir(self.state)
        self.m.act_run(lib_argv(self.root, RAISER, "mac_raiser_on", self.state))
        try:
            check = os.path.join(self.state, "browser-check.json")
            if not self.m.act_run(["/usr/bin/python3", os.path.join(self.root, CHECK), "--build-directory", instr, "--json", check]).ok:
                die("the instrumented build cannot present an accelerated, unthrottled browser here, so every profile it\n"
                    "    collected would be of the wrong code (above)")
            pins = self.pins()
            text = "".join("%s\t%s\n" % p for p in pins)
            self.m.write(os.path.join(self.state, "payload-pins"), text)
            log("wk: profiling against pinned payloads:\n" + "\n".join("  " + l for l in text.splitlines()))
            self.m.remove(profile)   # collect-pgo-profiles refuses a directory that is not empty
            watch = screen.Watch(self.m, self.root, self.clock or Clock(), self.env)
            watch.start()
            rc = self.m.run_tty(self.argv(instr, profile, arch, pins)).rc
            seen = watch.stop()
            if seen:
                die("something drew over this collection, so every leg after it profiled a covered browser:\n%s"
                    % "\n".join("  " + l for l in seen))
        finally:
            self.m.act_run(lib_argv(self.root, RAISER, "mac_raiser_off", self.state))
        if rc:
            return rc
        self.m.write(os.path.join(profile, "payload-pins"), text)
        if not self.m.act_run(isolated_module(os.path.join(self.root, "lib"), "wk.pgo", "/usr/bin/python3") + ["check", "--dir", profile, "--scripts", self.scripts, "--compressed", arch,
                               "--json", os.path.join(self.state, "profile-check.json")]).ok:
            die("the collection finished and its profile is not one to build against (above)")
        return 0

    def evidence(self, final, profile):
        """The readings that justify this build go beside its products, so a staged arm carries them."""
        if act.dry_run():
            return 0
        self.m.mkdir(final)
        for src, name in ((os.path.join(self.state, "browser-check.json"), "wk-browser-check.json"),
                          (os.path.join(self.state, "profile-check.json"), "wk-profile-check.json"),
                          (os.path.join(profile, "payload-pins"), "wk-payload-pins")):
            if self.m.exists(src):
                self.m.write(os.path.join(final, name), self.m.read(src))
        return 0


def main(argv, env=None, here=None):
    env = os.environ if env is None else env
    root = images.root(env)
    here = here or Local()
    verb, args = (argv or [""])[0], argv[1:]
    try:
        if verb == "pgo-instr" and len(args) == 1:
            print(args[0] + PGO_INSTR)
            return 0
        if verb == "pgo-collect" and len(args) == 4:
            return PgoCollect(root, here, env, args[0]).run(*args[1:])
        if verb == "pgo-evidence" and len(args) == 3:
            return PgoCollect(root, here, env, args[0]).evidence(*args[1:])
    except Refused as e:
        return e.status
    sys.stderr.write("usage: python3 -m wk.bench.mac_pgo pgo-instr <final> | pgo-collect <src> <instr> <profile> <arch>\n"
                     "       | pgo-evidence <src> <final> <profile>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
