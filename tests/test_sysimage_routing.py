"""`wk sysimage` runs where the image workspace is: `build` and `webkit` derive their workspace name from the preset,
and the dispatcher places the command by it before anything runs."""
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import decl, dispatch, images  # noqa: E402
from wk.sysimage import cli  # noqa: E402

TABLE = load_cmd("sysimage").COMMAND


def args(*words):
    return decl.parse("sysimage", TABLE, list(words))[1]


class TestTheImageWorkspaceAnswer(unittest.TestCase):
    def test_every_preset_answers_for_its_builder_and_no_preset_names_none(self):
        reg = mock.Mock(env=os.environ, default=lambda: "container")
        for preset in images.names():
            builder = images.load(preset)["IMG_BUILDER"]
            in_ws = builder in images.WS_BUILDERS
            with self.subTest(preset=preset):
                self.assertEqual({cli.wsname(args(s, preset)) for s in ("build", "webkit", "holds", "path")},
                                 {"%s-%s" % (builder, preset) if in_ws else ""})
                self.assertEqual(cli.where(args("build", preset), reg), "workspace" if in_ws else "host")
        self.assertEqual(cli.derived(args("holds", "webkit-2.52-yocto-rpi5-64", "--workspace", "arm-b"), reg)[0], "arm-b")
        self.assertEqual(cli.derived(args("build", "nosuchpreset"), reg), ("", ""))


class TestTheDispatcherPlacesIt(unittest.TestCase):
    def test_only_declared_options_pass_one_word_per_value(self):
        self.assertEqual(args("build", "p", "--stage", "webkit", "--detach").options(), ["--stage", "webkit", "--detach"])
        for words in (("build", "p", "--bogus"), ("webkit", "p", "--keep-work"), ("build", "p", "--", "--stage", "image")):
            with self.assertRaises(decl.Usage):
                args(*words)

    def test_the_derived_name_decides_the_place_unless_the_spec_names_one(self):
        def resolve(wsplace, derived, words):
            with mock.patch.object(dispatch, "registry", lambda: mock.Mock(ws_place=lambda name: "place-of:" + name)), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WK_PLACE", None)
                return dispatch.resolve_place(args(*words), derived, wsplace)
        self.assertEqual([resolve("", "yocto-demo", ["build", "demo"]), resolve("", "", ["build", "demo"]),
                          resolve("moose", "yocto-demo", ["build", "demo@moose"])], ["place-of:yocto-demo", "container", "moose"])


if __name__ == "__main__":
    unittest.main()
