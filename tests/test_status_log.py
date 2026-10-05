"""`wk status <ws> --log` (cmd/status, driven directly with WK_NAME/WK_PLACE/WK_STORE, as the machine that holds the store): build.log, else the image stage
log its builder wrote under home/, and the errors it names."""
import unittest

from tests.support import REPO, WkTest, bash

CMD_STATUS = REPO / "cmd" / "status"
FAILED = "Building place foo\nfoo.cpp:10:5: error: use of undeclared identifier 'x'\nninja: build stopped: subcommand failed.\n"
CLEAN = "Building place foo\n-- no error: handling here, everything is fine\nninja: no work to do.\n"


class TestLogsPicksTheRightFile(WkTest):
    def _run(self, name, files):
        """`files` is {path under the workspace's directory: text}; the output, and whether it exited 0."""
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        for rel, text in files.items():
            (wsdir / rel).parent.mkdir(parents=True, exist_ok=True)
            (wsdir / rel).write_text(text)
        env = {"WK_NAME": name, "WK_PLACE": "container", "WK_IN_VM": "1", "WK_STORE": str(self.tmp)}
        cp = bash(f'exec "{CMD_STATUS}" --log', env=env)
        return cp.stdout + cp.stderr, cp.returncode == 0

    def test_which_log_is_shown_and_what_it_says(self):
        for name, files, ok, shown, hidden in (
                ("yoctows", {"home/yocto-image.log": "the yocto stage output\n"}, True,
                 ("the yocto stage output", "home/yocto-image.log"), ()),
                ("buildrootws", {"home/buildroot-image.log": "the buildroot stage output\n"}, True,
                 ("the buildroot stage output", "home/buildroot-image.log"), ()),
                ("bothws", {"home/yocto-image.log": "stale image-stage output\n", "build.log": "ninja: building places\n"}, True,
                 ("ninja: building places",), ("stale image-stage output",)),
                ("emptyws", {}, False, ("wk build", "wk sysimage build"), ()),
                ("badws", {"build.log": FAILED}, True, ("ninja: build stopped",), ("(none)",))):
            with self.subTest(name):
                out, rc_ok = self._run(name, files)
                self.assertEqual(rc_ok, ok, out)
                for text in shown:
                    self.assertIn(text, out)
                for text in hidden:
                    self.assertNotIn(text, out)

    def test_a_message_containing_the_word_error_mid_sentence_is_not_reported(self):
        out, ok = self._run("goodws", {"build.log": CLEAN})
        self.assertTrue(ok, out)
        self.assertIn("(none)", out, out)
        self.assertNotIn("no error: handling here", out.split("errors:", 1)[1].split("last output:", 1)[0], out)


if __name__ == "__main__":
    unittest.main()
