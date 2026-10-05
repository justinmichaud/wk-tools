"""`wk sysimage` runs where the image workspace is: `build` and `webkit` derive their workspace name from the preset."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, rand_suffix, run, run_here, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402
from wk import images  # noqa: E402
from wk.sysimage import cli  # noqa: E402

SYSIMAGE = REPO / "cmd" / "sysimage"
PRESET = "webkit-2.52-yocto-rpi5-64"


def _presets():
    return [(n, images.load(n)["IMG_BUILDER"]) for n in images.names()]


def _hook(*args):
    cp = subprocess.run([str(SYSIMAGE), *args], capture_output=True, text=True,
                        cwd=str(REPO))
    return cp.stdout.strip(), cp


class TestTheImageWorkspaceAnswer(WkTest):
    """`wk sysimage --wsname` and `--where`: the two questions the dispatcher asks cmd/sysimage."""

    def test_every_preset_answers_for_its_builder(self):
        for preset, builder in _presets():
            in_ws = builder in ("yocto", "buildroot")
            with self.subTest(preset=preset):
                for sub in ("build", "webkit", "holds", "path"):
                    self.assertEqual(cli.wsname([sub, preset]), "%s-%s" % (builder, preset) if in_ws else "")
                self.assertEqual(cli.where(["build", preset]), "workspace" if in_ws else "host")

    def test_the_hook_answers_and_no_preset_names_no_image_workspace(self):
        self.assertEqual(_hook("--wsname", "holds", PRESET, "--workspace", "arm-b")[0], "arm-b")
        for args in (["build"], ["build", "nosuchprofile-" + rand_suffix()],
                     ["webkit"], ["write", "--from", "/tmp/x"]):
            got, cp = _hook("--wsname", *args)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(got, "", args)

    def test_an_image_nothing_has_built_is_no_on_stdout_and_exit_0(self):
        """A readonly command forwarded to a stopped podman machine exits 0, so the verdict is what is printed."""
        cp = run_here("sysimage", "holds", PRESET, "--workspace", "yocto-%s-selftest" % PRESET, timeout=240)
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "no"), cp.stdout)


class TestTheDeclaredBuildOptions(unittest.TestCase):
    def check(self, *args):
        inv = dispatch.Invocation("sysimage", D.Decl(REPO / "cmd" / "sysimage"), list(args))
        with mock.patch.object(dispatch, "in_workspace", lambda: False), \
                mock.patch("sys.stderr", new_callable=lambda: open(os.devnull, "w")):
            return inv.argv_check()

    def test_an_undeclared_option_is_refused_before_the_builder_runs(self):
        for sub in ("build", "webkit"):
            with self.assertRaises(dispatch.Exit) as cm:
                self.check(sub, "p", "--bogus")
            self.assertEqual(cm.exception.status, 2, sub)

    def test_a_declared_option_passes_one_word_per_value(self):
        self.assertEqual(self.check("build", "p", "--stage", "webkit", "--detach"), ["build", "p", "--stage=webkit", "--detach"])

    def test_a_tail_after_dashdash_is_refused(self):
        with self.assertRaises(dispatch.Exit):
            self.check("build", "p", "--", "--stage", "image")


class TestTheDispatcherReadsDerived(unittest.TestCase):
    def _decl(self, impl="cmd/sysimage"):
        return D.Decl(REPO / impl)

    def test_the_workspace_verbs_derive_their_name_and_nothing_else_does(self):
        d = self._decl()
        got = ["%s=%s" % (s, d.name_for([s])) for s in ("build", "webkit", "ls", "write", "disks", "rm", "flash")]
        self.assertEqual(got, ["build=derived", "webkit=derived", "ls=none", "write=none",
                               "disks=none", "rm=none", "flash=none"])

    def test_a_name_outside_the_vocabulary_is_refused(self):
        impl = REPO / "cmd" / f"faux-{rand_suffix()}"
        impl.write_text("#!/usr/bin/env bash\n#\n# wk faux -- x\n"
                        "# wk: where=workspace name=inferred group=other\n")
        try:
            with self.assertRaises(D.DeclError) as cm:
                self._decl(impl.relative_to(REPO))
        finally:
            impl.unlink()
        self.assertIn("name=inferred is not one of", str(cm.exception))

    def _resolve(self, *, wsplace, derived, args):
        inv = dispatch.Invocation("sysimage", self._decl(), args)
        with mock.patch.object(dispatch.Invocation, "named_place", lambda self: wsplace), \
                mock.patch.object(dispatch, "registry", lambda: mock.Mock(ws_place=lambda name: "place-of:" + name)), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WK_PLACE", None)
            return dispatch.resolve_place(inv, "derived", 0, "2", derived)

    def test_the_derived_name_decides_the_place(self):
        self.assertEqual(self._resolve(wsplace="", derived="yocto-demo", args=["build", "demo"]),
                         "place-of:yocto-demo")
        self.assertEqual(self._resolve(wsplace="", derived="", args=["build", "demo"]), "container")

    def test_a_target_the_command_names_outright_wins(self):
        """A spec that names its machine is not located, whether or not another one holds its name."""
        self.assertEqual(self._resolve(wsplace="moose", derived="yocto-demo", args=["build", "demo@moose"]),
                         "moose")


class TestAnImageWorkspaceOnAnotherMachine(WkTest):
    """`fakebox` is a machine conf and a stubbed ssh: nothing is built anywhere."""

    SSH_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_SSH_LOG"
case "$*" in *"/wk doctor --probe-tools"*) printf 'sha=%s\n' "$WK_TEST_TOOLS_SHA" ;; esac
case "$*" in *===MEM===*) printf '/home/u\\nLinux\\n4\\n0.1 0 0\\n===MEM===\\nMemAvailable: 1024 kB\\n===IONICE===\\nno\\n' ;; esac
exit 0
"""

    def setUp(self):
        super().setUp()
        self.preset = next((p for p, b in _presets() if b == "yocto"), "")
        if not self.preset:
            self.skipTest("this checkout defines no yocto preset")
        self.ws = "noimage-" + rand_suffix()
        self.log = self.tmp / "ssh.log"
        self.log.write_text("")
        registry = self.tmp / "hosts"
        registry.mkdir()
        (registry / "fakebox.conf").write_text(
            "kind=build\ndriver=remote\n"
            "host=fakebox\n"
            f"root={self.tmp}/box\n")
        (self.tmp / "here").mkdir()
        self.env = {"WK_MACHINES_DIR": str(registry),
                    "WK_STORE": str(self.tmp / "here"),
                    "WK_TEST_SSH_LOG": str(self.log),
                    "WK_TEST_TOOLS_SHA": subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                                        capture_output=True, text=True).stdout.strip()}

    def test_the_build_is_delegated_with_its_arguments_intact(self):
        with stub_path({"ssh": self.SSH_STUB}) as binp:
            env = dict(self.env, PATH=f"{binp}:{os.environ['PATH']}",
                       WK_PLACE="fakebox")
            cp = run("sysimage", "build", self.preset,
                     "--workspace", self.ws, "--detach", env=env)
        sent = self.log.read_text()
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(
            f"sysimage build {self.preset} --workspace {self.ws} --detach",
            sent)

    def test_the_spec_names_the_machine_with_no_target_set(self):
        with stub_path({"ssh": self.SSH_STUB}) as binp:
            env = dict(self.env, PATH=f"{binp}:{os.environ['PATH']}")
            self.assertNotIn("WK_PLACE", env)
            cp = run("sysimage", "build", f"{self.preset}@fakebox",
                     "--workspace", self.ws, "--detach", env=env)
        sent = self.log.read_text()
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(f"sysimage build {self.preset}@fakebox", sent,
                      f"not delegated to fakebox: {sent!r}")

    def test_an_image_workspace_that_does_not_exist_yet_is_not_refused(self):
        # `vm` is a place the yocto builder refuses, so this stops one line into the command.
        with stub_path({"tart": "exit 0\n"}) as binp:
            cp = run("sysimage", "build", self.preset, "--workspace", self.ws,
                     env=dict(self.env, WK_PLACE="vm",
                              PATH=f"{binp}:{os.environ['PATH']}"))
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertNotIn("no such workspace", out)
        self.assertIn("place 'vm' is a vm one", out)


if __name__ == "__main__":
    unittest.main()
