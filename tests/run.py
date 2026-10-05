"""The suite's runner: python3 tests/run.py [--lint] [--unit] [--live] [-v] [-k PAT]... [--list]
A test's tier is the wk_tier a gate in tests/support.py set on its method or
class, else its module's TIER, else unit; no tier named means lint and unit."""
import argparse
import atexit
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TIERS = ("lint", "unit", "live")
BUDGET = {"lint": 30.0, "unit": 30.0, "live": 1800.0}
SLOWEST = 10
MACHINE_TOOLS = ("ssh", "scp", "podman", "tart", "tailscale", "nmap", "gh", "sudo")
SHIM = '#!/bin/sh\necho "unit tier reached %s $*" >&2\nexit 97\n'
# `ssh -G` prints the resolved config and never connects, so it is the one call handed to the real ssh.
SSH_SHIM = '#!/bin/sh\nfor a; do [ "$a" = -G ] && exec %s "$@"; done\n' + SHIM
# Beside WK_*, what a workspace container exports (lib/wk/places.py create_flags, sandbox_flags).
WORKSPACE_PREFIXES = ("WK_", "WKDEV_", "CCACHE_", "BR2_")
WORKSPACE_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY",
                  "WAYLAND_DISPLAY", "DL_DIR", "SSTATE_DIR")


def parse_args(argv):
    p = argparse.ArgumentParser(prog="tests/run.py")
    for tier in TIERS:
        p.add_argument("--" + tier, action="append_const", dest="tiers", const=tier)
    p.add_argument("-v", action="store_true")
    p.add_argument("-k", action="append", dest="patterns", default=[])
    p.add_argument("--tests", default=str(HERE), type=Path)
    p.add_argument("--list", action="store_true")
    a = p.parse_args(argv)
    a.tiers = sorted(set(a.tiers or ["lint", "unit"]), key=TIERS.index)
    return a


def test_tier(test):
    mod = sys.modules.get(type(test).__module__)
    if mod is None or mod.__name__.startswith("unittest."):
        return None
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    for holder, attr in ((method, "wk_tier"), (type(test), "wk_tier"), (mod, "TIER")):
        tier = getattr(holder, attr, None)
        if tier in TIERS:
            return tier
    return "unit"


def budget_for(test):
    given = os.environ.get("WK_TEST_BUDGET")
    return float(given) if given else BUDGET[test_tier(test) or "unit"]


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for t in flatten(item):
                yield t
        else:
            yield item


def owed_reason(test):
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    for holder in (method, type(test)):
        reason = getattr(holder, "wk_owed", None)
        if reason is not None:
            return reason
    return ""


def shim_machine_tools():
    """In the tiers that need no machine, every tool that reaches one answers
    with exit 97 and names the caller; a test's own stub goes ahead of it."""
    d = tempfile.mkdtemp(prefix="wk-test-shims-")
    atexit.register(shutil.rmtree, d, True)
    real_ssh = shutil.which("ssh") or "/usr/bin/ssh"
    for tool in MACHINE_TOOLS:
        p = Path(d) / tool
        p.write_text((SSH_SHIM % (real_ssh, tool)) if tool == "ssh" else (SHIM % tool))
        p.chmod(0o755)
    os.environ["WK_TEST_SHIMS"] = d
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def isolate_from_this_machine():
    """The tiers that need no machine run the same on a host and inside a workspace: no WK_* or workspace
    variable of the caller's reaches a test, and HOME is an empty directory, so no marker, ~/.wk-remote,
    ~/.ssh/config or dotfile of this machine's is read."""
    for var in list(os.environ):
        if var in WORKSPACE_VARS or (var.startswith(WORKSPACE_PREFIXES) and not var.startswith("WK_TEST_")):
            del os.environ[var]
    home = tempfile.mkdtemp(prefix="wk-test-home-")
    atexit.register(shutil.rmtree, home, True)
    os.environ["HOME"] = home


def select(tests_dir, tiers, patterns):
    loader = unittest.TestLoader()
    if patterns:
        loader.testNamePatterns = [p if "*" in p else "*%s*" % p for p in patterns]
    found = loader.discover(str(tests_dir), top_level_dir=str(tests_dir.parent))
    picked = unittest.TestSuite()
    for test in flatten(found):
        if test_tier(test) in (None, *tiers):
            picked.addTest(test)
    return picked


def hang_after(budget):
    """When a test is stopped where it is: past its budget it already fails, and the margin lets a slow skip finish."""
    return 2 * budget + 5


class OverBudget(AssertionError):
    """Raised inside a test that outlived hang_after, so a wait nothing else bounds fails rather than hangs the run."""


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.timings = []
        self.owed_passed = []
        self._started = None
        self._passed = False

    def startTest(self, test):
        self._passed = False
        act = sys.modules.get("wk.act")
        if act:
            del act._forced[:]   # process-global: a barrier forced by one in-process test would count in the next
        super().startTest(test)
        self._started = time.monotonic()
        stop = hang_after(budget_for(test))

        def over(signum, frame):
            raise OverBudget("over budget: %s was stopped after %gs" % (test.id(), stop))
        signal.signal(signal.SIGALRM, over)
        signal.setitimer(signal.ITIMER_REAL, stop)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        took = time.monotonic() - self._started
        self.timings.append((took, test.id()))
        budget = budget_for(test)
        if self._passed and took > budget:
            msg = "over budget: %s took %.1fs, budget %gs" % (test.id(), took, budget)
            self.addFailure(test, (AssertionError, AssertionError(msg), None))
        super().stopTest(test)

    def addSuccess(self, test):
        self._passed = True
        super().addSuccess(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.owed_passed.append("%s (%s)" % (test.id(), owed_reason(test) or "no reason recorded"))


def summarize(result, tiers, took, out):
    out.write("\n")
    for line in result.owed_passed:
        out.write("owed test passed -- remove its owed mark: %s\n" % line)
    out.write("tiers: %s  tests: %d  time: %.1fs  failures: %d  errors: %d  skipped: %d  owed: %d\n"
              % (",".join(tiers), result.testsRun, took, len(result.failures),
                 len(result.errors), len(result.skipped), len(result.expectedFailures)))
    slowest = sorted(result.timings, reverse=True)[:SLOWEST]
    if slowest:
        out.write("slowest:\n")
        for t, name in slowest:
            out.write("  %7.2fs  %s\n" % (t, name))
    out.flush()


def main(argv):
    a = parse_args(argv)
    os.environ["WK_TEST_TIERS"] = ",".join(a.tiers)
    if "live" not in a.tiers:
        isolate_from_this_machine()
        shim_machine_tools()
    for root in (a.tests.resolve().parent, REPO):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    suite = select(a.tests.resolve(), a.tiers, a.patterns)
    if not suite.countTestCases():
        sys.stderr.write("nothing selected: tests/run.py %s\n" % " ".join(argv))
        return 1
    if a.list:
        for test in flatten(suite):
            print(test.id())
        print("selected: %d" % suite.countTestCases())
        return 0
    runner = unittest.TextTestRunner(verbosity=2 if a.v else 1, resultclass=Result)
    started = time.monotonic()
    result = runner.run(suite)
    summarize(result, a.tiers, time.monotonic() - started, sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
