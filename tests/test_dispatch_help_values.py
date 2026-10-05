"""`preset=` (the declaration): a command that takes a build preset says so to"""
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, run

sys.path.insert(0, str(REPO / "lib"))
from wk import presets                 # noqa: E402
from wk import decl as D                 # noqa: E402
from wk import dispatch                  # noqa: E402

def _decl(cmd):
    return D.Decl(REPO / "cmd" / cmd)


class TestTheHelpListsEveryPreset(unittest.TestCase):
    def test_every_taker_lists_every_preset(self):
        takers = [d for d in D.all_commands(REPO) if d.preset]
        self.assertTrue(takers)
        for d in takers:
            text = run(d.name, "-h", timeout=30).stdout
            with self.subTest(cmd=d.name):
                self.assertIn("valid values (%s):" % ("<preset>" if d.preset == "arg" else "--preset"), text)
                for name in presets.names():
                    self.assertIn(name, text)


class TestTheDispatcherHandsItOver(unittest.TestCase):
    def take(self, cmd, *args):
        inv = dispatch.Invocation(cmd, _decl(cmd), list(args))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WK_PRESET", None)
            rest = inv.take_preset(list(args))
            return rest, os.environ.get("WK_PRESET")

    def test_the_option_is_lifted_into_wk_config(self):
        self.assertEqual(self.take("run", "--preset=gtk-release", "--lldb", "--", "--preset=x"),
                         (["--lldb", "--", "--preset=x"], "gtk-release"))

    def test_builds_argument_is_lifted_into_wk_config(self):
        self.assertEqual(self.take("build", "--detach", "jsc-debug", "--", "x"), (["--detach", "--", "x"], "jsc-debug"))

    def test_a_flag_that_takes_no_argument_leaves_argv_alone(self):
        self.assertEqual(self.take("build", "--kill"), (["--kill"], None))

    def test_no_config_named_hands_none_over(self):
        self.assertEqual(self.take("test", "--layout"), (["--layout"], None))

    def test_a_name_presets_does_not_hold_is_refused(self):
        with mock.patch("sys.stderr"), self.assertRaises(dispatch.Exit) as cm:
            self.take("test", "--preset=jsc-relase")
        self.assertEqual(cm.exception.status, 2)

    def exec_env(self, *argv, inherited="nonsense"):
        seen = []

        def execv(path, args):
            seen.append((args[1:], os.environ.get("WK_PRESET")))
            raise dispatch.Exit(0)
        with mock.patch.dict(os.environ, {"WK_PRESET": inherited}), mock.patch("os.execv", execv):
            for v in ("WK_NAME", "WK_PLACE", "WK_DRY_RUN"):
                os.environ.pop(v, None)
            with self.assertRaises(dispatch.Exit):
                dispatch.main(list(argv))
        return seen[0]

    def test_an_inherited_wk_config_is_not_this_invocations(self):
        self.assertEqual(self.exec_env("build", "--list"), (["--list"], None))

    def test_the_command_is_execd_with_the_config_named(self):
        self.assertEqual(self.exec_env("bench", "stage", "ws", "--to", "mbp", "--preset", "mac-release",
                                       inherited="mac-release"),
                         (["stage", "ws", "--to", "mbp"], "mac-release"))


class TestAnExportedConfigIsRefused(unittest.TestCase):

    def refused(self, *argv):
        cp = run(*argv, env={"WK_PRESET": "gtk-release"})
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("WK_PRESET=gtk-release is set in this environment", cp.stdout)
        return cp.stdout

    def test_an_option_taker_names_the_option(self):
        self.assertIn("--preset gtk-release", self.refused("run", "ws"))

    def test_one_that_disagrees_with_the_option_is_refused_too(self):
        self.refused("test", "ws", "--preset", "jsc-release")

    def test_build_names_its_argument(self):
        self.assertIn("wk build <workspace> gtk-release", self.refused("build", "ws"))


if __name__ == "__main__":
    unittest.main()
