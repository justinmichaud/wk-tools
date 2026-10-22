"""`wk sysimage` runs where the image workspace is: `build` and `webkit` derive their workspace name from the preset,
and the dispatcher places the command by it before anything runs."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch, images  # noqa: E402
from wk.sysimage import cli  # noqa: E402


class TestTheImageWorkspaceAnswer(unittest.TestCase):
    def test_every_preset_answers_for_its_builder_and_no_preset_names_none(self):
        for preset in images.names():
            builder = images.load(preset)["IMG_BUILDER"]
            in_ws = builder in images.WS_BUILDERS
            with self.subTest(preset=preset):
                self.assertEqual({cli.wsname([s, preset]) for s in ("build", "webkit", "holds", "path")},
                                 {"%s-%s" % (builder, preset) if in_ws else ""})
                self.assertEqual(cli.where(["build", preset]), "workspace" if in_ws else "host")
        hook = lambda *a: subprocess.run([str(REPO / "cmd" / "sysimage"), "--wsname", *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
        self.assertEqual(hook("holds", "webkit-2.52-yocto-rpi5-64", "--workspace", "arm-b"), "arm-b")
        for args in (["build"], ["build", "nosuchpreset"], ["webkit"], ["write", "--from", "/tmp/x"]):
            self.assertEqual(hook(*args), "", args)


class TestTheDispatcherPlacesIt(unittest.TestCase):
    def test_only_declared_options_pass_one_word_per_value(self):
        def check(*args):
            inv = dispatch.Invocation("sysimage", D.Decl(REPO / "cmd" / "sysimage"), list(args))
            with mock.patch.object(dispatch, "in_workspace", lambda: False), mock.patch("sys.stderr", new_callable=lambda: open(os.devnull, "w")):
                return inv.argv_check()
        self.assertEqual(check("build", "p", "--stage", "webkit", "--detach"), ["build", "p", "--stage=webkit", "--detach"])
        for args in (("build", "p", "--bogus"), ("webkit", "p", "--keep-work"), ("build", "p", "--", "--stage", "image")):
            with self.assertRaises(dispatch.Exit):
                check(*args)

    def test_the_derived_name_decides_the_place_unless_the_spec_names_one(self):
        def resolve(wsplace, derived, args):
            inv = dispatch.Invocation("sysimage", D.Decl(REPO / "cmd" / "sysimage"), args)
            with mock.patch.object(dispatch.Invocation, "named_place", lambda self: wsplace), \
                    mock.patch.object(dispatch, "registry", lambda: mock.Mock(ws_place=lambda name: "place-of:" + name)), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WK_PLACE", None)
                return dispatch.resolve_place(inv, "derived", 0, "2", derived)
        self.assertEqual([resolve("", "yocto-demo", ["build", "demo"]), resolve("", "", ["build", "demo"]),
                          resolve("moose", "yocto-demo", ["build", "demo@moose"])], ["place-of:yocto-demo", "container", "moose"])


if __name__ == "__main__":
    unittest.main()
