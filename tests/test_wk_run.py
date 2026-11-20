"""`wk run`: the jsc a workspace's build produced, direct or under lldb, once or until it crashes, typed into a world
holding workspace `ws`."""
import os
import shlex
import subprocess
import unittest

from tests.test_wk_workspace import World, WorkspaceTest

SRC = "/src/WebKit/WebKitBuild/"
LD = 'export LD_LIBRARY_PATH="%s${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"'
PLAIN = "%s; export JSC_validateOptions=1; exec %s --validateOptions=1 x.js"


class Bridged(World):
    """The guest bridge has its address, as ifconfig on a Mac running a guest says: wait_ready's real clock waits on it."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)
        self.answer(["ifconfig"], out="bridge100: flags=8863<UP>\n\tinet 192.168.2.1 netmask 0xffffff00\n")


class RunTest(WorkspaceTest):
    world_class = Bridged

    def setUp(self):
        super().setUp()
        self.w.make("ws")

    def run_(self, *argv, w=None):
        """`wk run ws <argv>`: (exit status, stderr, the argv this process was replaced with or None)."""
        w = w or self.w
        os.environ.pop("WK_PRESET", None)   # the dispatcher exports the last call's preset into this process
        w.effects = []
        rc, _, err = self.wk(w, "run", "ws", *argv)
        last = w.effects[-1] if w.effects else (None, ())
        return rc, err, list(last[1]) if last[0] == "exec" else None


class TestFindsBinaryOnEveryPort(RunTest):
    def test_each_linux_port_runs_its_jsc_with_its_library_directory_prepended(self):
        """`unit run.finds_binary[<port>]`: with no --preset and no build, jsc-release."""
        for preset, port in ((None, "JSCOnly"), ("jsc-release", "JSCOnly"), ("gtk-release", "GTK"), ("wpe-release", "WPE")):
            rc, _, argv = self.run_(*(["--preset", preset] if preset else []), "--", "x.js")
            lib = SRC + port + "/Release/lib"
            self.assertEqual((rc, argv[-3:-1], argv[-1]), (0, ["bash", "-lc"], PLAIN % (LD % lib, SRC + port + "/Release/bin/jsc")), preset)
            cp = subprocess.run(["sh", "-c", argv[-1].split("; ")[0] + '; printf %s "$LD_LIBRARY_PATH"'],
                                env={"LD_LIBRARY_PATH": "/old"}, capture_output=True, text=True)
            self.assertEqual(cp.stdout, lib + ":/old")

    def test_a_mac_guest_runs_the_apple_ports_jsc_with_its_framework_path(self):
        w = self.make_world(kinds={"fakebox": "vm"})
        w.make("ws")
        rc, _, argv = self.run_("--", "x.js", w=w)
        inner = shlex.split(argv[-1].split("; ", 1)[1])
        rel = "/Users/admin/WebKit/WebKitBuild/Release"
        self.assertEqual((rc, inner[:2], inner[2]), (0, ["bash", "-lc"], PLAIN % (
            'export DYLD_FRAMEWORK_PATH="%s${DYLD_FRAMEWORK_PATH:+:${DYLD_FRAMEWORK_PATH}}"' % rel, rel + "/jsc")))


class TestLldbGetsAPty(RunTest):
    def test_only_lldb_asks_for_a_tty_and_until_crash_loops_under_its_own_run_file(self):
        for flags, tty, says in ((["--lldb"], True, "-s /opt/wk-tools/container/lldb/run-file "),
                                 ([], False, "exec " + SRC + "GTK/Release/bin/jsc"),
                                 (["--until-crash", "--lldb"], True, "-s /opt/wk-tools/container/lldb/until-crash-run-file "),
                                 (["--until-crash"], False, 'while [ "$i" -lt 200 ]; do')):
            rc, _, argv = self.run_("--preset", "gtk-release", *flags, "--", "x.js")
            self.assertEqual((rc, "--no-tty" not in argv, says in argv[-1]), (0, tty, True), flags)

    def test_a_dry_run_prints_the_command_and_replaces_nothing(self):
        rc, err, argv = self.run_("--dry-run", "--preset", "gtk-release", "--", "x.js")
        self.assertEqual((rc, argv), (0, None))
        self.assertRegex(err, r"^would run: env .*/wkdev-enter .* exec %sGTK/Release/bin/jsc --validateOptions=1 x.js'\n$" % SRC)


class TestMaxValidation(RunTest):
    def test_max_caps_the_loop_and_a_non_numeric_one_is_refused_before_anything_runs(self):
        rc, _, argv = self.run_("--until-crash", "--max", "5", "--", "x.js")
        self.assertEqual((rc, 'while [ "$i" -lt 5 ]; do' in argv[-1]), (0, True))
        rc, err, argv = self.run_("--until-crash", "--max", "abc", "--", "x.js")
        self.assertEqual((rc, err.splitlines()[0], argv), (1, "error: --max needs a positive integer iteration count (got 'abc')", None))


if __name__ == "__main__":
    unittest.main()
