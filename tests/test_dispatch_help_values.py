"""`config=` (the declaration): a command that takes a build config says so to"""
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO, run

sys.path.insert(0, str(REPO / "lib"))
from wk import buildconf                 # noqa: E402
from wk import decl as D                 # noqa: E402
from wk import dispatch                  # noqa: E402

TAKERS = ("bench", "build", "gui", "profile", "run", "test")


def _decl(cmd):
    return D.Decl(REPO / "cmd" / cmd)


class TestTheCommandsThatTakeABuildConfigDeclareIt(unittest.TestCase):
    def test_each_declares_it(self):
        self.assertEqual(sorted(d.name for d in D.all_commands(REPO) if d.config), list(TAKERS))


    def test_a_config_that_is_not_one_is_a_declaration_error(self):
        with tempfile.NamedTemporaryFile("w", suffix="-cmd") as f:
            f.write("#!/bin/sh\n# wk x -- y\n# wk: config=--cfg\n")
            f.flush()
            with self.assertRaises(D.DeclError):
                D.Decl(f.name)


class TestTheHelpListsEveryConfig(unittest.TestCase):
    def test_every_taker_lists_every_config(self):
        for cmd in TAKERS:
            text = run(cmd, "-h", timeout=30).stdout
            with self.subTest(cmd=cmd):
                self.assertIn("valid values (%s):" % ("<config>" if cmd == "build" else "--config"), text)
                for name in buildconf.names():
                    self.assertIn(name, text)


class TestTheDispatcherHandsItOver(unittest.TestCase):
    def take(self, cmd, *args):
        inv = dispatch.Invocation(cmd, _decl(cmd), list(args))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WK_CONFIG", None)
            rest = inv.take_config(list(args))
            return rest, os.environ.get("WK_CONFIG")

    def test_the_option_is_lifted_into_wk_config(self):
        self.assertEqual(self.take("run", "--config=gtk-release", "--lldb", "--", "--config=x"),
                         (["--lldb", "--", "--config=x"], "gtk-release"))

    def test_builds_argument_is_lifted_into_wk_config(self):
        self.assertEqual(self.take("build", "--detach", "jsc-debug", "--", "x"), (["--detach", "--", "x"], "jsc-debug"))

    def test_a_flag_that_takes_no_argument_leaves_argv_alone(self):
        self.assertEqual(self.take("build", "--kill"), (["--kill"], None))

    def test_no_config_named_hands_none_over(self):
        self.assertEqual(self.take("test", "--layout"), (["--layout"], None))

    def test_a_name_buildconf_does_not_hold_is_refused(self):
        with mock.patch("sys.stderr"), self.assertRaises(dispatch.Exit) as cm:
            self.take("test", "--config=jsc-relase")
        self.assertEqual(cm.exception.status, 2)

    def exec_env(self, *argv, inherited="nonsense"):
        seen = []

        def execv(path, args):
            seen.append((args[1:], os.environ.get("WK_CONFIG")))
            raise dispatch.Exit(0)
        with mock.patch.dict(os.environ, {"WK_CONFIG": inherited}), mock.patch("os.execv", execv):
            for v in ("WK_NAME", "WK_TARGET", "WK_DRY_RUN"):
                os.environ.pop(v, None)
            with self.assertRaises(dispatch.Exit):
                dispatch.main(list(argv))
        return seen[0]

    def test_an_inherited_wk_config_is_not_this_invocations(self):
        self.assertEqual(self.exec_env("build", "--list"), (["--list"], None))

    def test_the_command_is_execd_with_the_config_named(self):
        self.assertEqual(self.exec_env("bench", "stage", "ws", "--to", "mbp", "--config", "mac-release",
                                       inherited="mac-release"),
                         (["stage", "ws", "--to", "mbp"], "mac-release"))


class TestAnExportedConfigIsRefused(unittest.TestCase):

    def refused(self, *argv):
        cp = run(*argv, env={"WK_CONFIG": "gtk-release"})
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("WK_CONFIG=gtk-release is set in this environment", cp.stdout)
        return cp.stdout

    def test_an_option_taker_names_the_option(self):
        self.assertIn("--config gtk-release", self.refused("run", "ws"))

    def test_one_that_disagrees_with_the_option_is_refused_too(self):
        self.refused("test", "ws", "--config", "jsc-release")

    def test_build_names_its_argument(self):
        self.assertIn("wk build <workspace> gtk-release", self.refused("build", "ws"))


if __name__ == "__main__":
    unittest.main()
