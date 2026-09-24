"""wk.decl.Args: how a command, and resolve_target, read the options the
dispatcher checked and handed on -- by the command's own declaration.

Run: python3 tests/run.py --unit -k test_dispatch_args
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402


def declare(tmp, *lines):
    p = Path(tmp) / "probe"
    p.write_text("#!/usr/bin/env python3\n# wk probe <x> -- a probe\n%s\n" % "\n".join(lines))
    return D.Decl(p)


def load_cmd(name):
    loader = importlib.machinery.SourceFileLoader("cmd_" + name, str(REPO / "cmd" / name))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("cmd_" + name, loader))
    loader.exec_module(mod)
    return mod


class TestArgs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wk-test-args-")
        self.addCleanup(self.tmp.cleanup)
        self.d = declare(self.tmp.name, "# wk: opts --zed,--base=,--env=",
                         "# wk: flag --kill opts=--kill")

    def test_flags_values_positionals_and_the_tail(self):
        a = D.Args(self.d, ["x", "--zed", "--base", "--zed", "y", "--", "--base", "z"])
        self.assertTrue(a.flag("--zed"))
        self.assertEqual(a.value("--base"), "--zed")   # a value is never taken for an option
        self.assertEqual(a.positionals, ["x", "y"])
        self.assertEqual(a.tail, ["--base", "z"])

    def test_an_option_not_given_is_none_false_or_empty(self):
        a = D.Args(self.d, [])
        self.assertIsNone(a.value("--base"))
        self.assertFalse(a.flag("--zed"))
        self.assertEqual(a.values("--env"), [])

    def test_a_repeated_option_keeps_every_value_and_its_last_wins(self):
        a = D.Args(self.d, ["--env", "A=1", "--env", "B=2"])
        self.assertEqual(a.values("--env"), ["A=1", "B=2"])
        self.assertEqual(a.value("--env"), "B=2")

    def test_a_flag_override_narrows_what_is_an_option(self):
        a = D.Args(self.d, ["--kill", "--zed"])
        self.assertTrue(a.flag("--kill"))
        self.assertFalse(a.flag("--zed"))
        self.assertEqual(a.positionals, ["--zed"])

    def test_order_keeps_every_option_seen_in_the_order_typed(self):
        a = D.Args(self.d, ["--zed", "x", "--base", "b", "--env", "A=1", "--zed"])
        self.assertEqual(a.order, ["--zed", "--base", "--env", "--zed"])

    def test_order_excludes_positionals_and_the_tail(self):
        a = D.Args(self.d, ["x", "--zed", "--", "--base", "z"])
        self.assertEqual(a.order, ["--zed"])


class TestResolveTargetReadsTheSameWay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wk-test-args-")
        self.addCleanup(self.tmp.cleanup)
        self.d = declare(self.tmp.name, "# wk: name=none opts --target=")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WK_TARGET", None)

    def resolve(self, *canonical):
        return dispatch.resolve_target(dispatch.Invocation("probe", self.d, list(canonical)), "none", 0, "0", "")

    def test_the_named_target_is_the_answer(self):
        self.assertEqual(self.resolve("--target=vm"), "vm")

    def test_an_empty_target_falls_through_to_the_default(self):
        self.assertEqual(self.resolve("--target="), "container")


class TestNewReadsEveryOptionThroughArgs(unittest.TestCase):
    def test_new(self):
        got = load_cmd("new").parse(["--target", "vm", "--arch", "armhf", "--base", "b1", "--pr", "7",
                                     "--zed", "--no-wait", "--kill", "--sysroot", "--_detached"])
        self.assertEqual(got, {"target": "vm", "base": "b1", "arch": "armhf", "pr": "7", "zed": True,
                               "no_wait": True, "kill": True, "sysroot": True, "detached": True})

    def test_nothing_given(self):
        got = load_cmd("new").parse([])
        self.assertEqual(got, {"target": None, "base": None, "arch": None, "pr": None, "zed": False,
                               "no_wait": False, "kill": False, "sysroot": False, "detached": False})


if __name__ == "__main__":
    unittest.main()
