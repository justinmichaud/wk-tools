"""`NO_COLOR` and a redirected stdout both drop the colour from `wk status`'s"""
import io
import os
import sys
import types
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import statusview  # noqa: E402

ESC = "\033["


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''


def _render(colour):
    records = [
        {"kind": "machine", "name": "buildbox4"},
        {"kind": "workspace", "machine": "buildbox4", "method": "container", "name": "demo",
         "state": "running", "branch": "main", "base": "main"},
        {"kind": "exit", "code": 0},
    ]
    out = io.StringIO()
    statusview.render_text_stream(iter(records), out, colour)
    return out.getvalue()


class TestColourDecision(unittest.TestCase):
    def test_only_a_terminal_without_no_color_gets_colour(self):
        for tty, env, want in ((True, {}, True), (True, {"NO_COLOR": "1"}, False), (False, {}, False),
                               (False, {"NO_COLOR": "1"}, False)):
            with self.subTest(tty=tty, env=env):
                self.assertEqual(statusview.colour_wanted(types.SimpleNamespace(isatty=lambda: tty), env), want)

    def test_the_renderer_honours_the_flag(self):
        self.assertIn(ESC, _render(True))
        self.assertNotIn(ESC, _render(False))

    def test_the_default_view_is_web_only_at_a_colour_terminal(self):
        mode = statusview.default_mode
        self.assertEqual(mode({"HOME": "/nonexistent"}, True), "web")
        self.assertEqual(mode({"HOME": "/nonexistent"}, False), "text")
        self.assertEqual(mode({"HOME": "/nonexistent", "NO_COLOR": "1"}, True), "text")
        self.assertEqual(mode({"HOME": "/nonexistent", "CI": "1"}, True), "text")
        self.assertEqual(mode({"HOME": "/nonexistent", "NO_COLOR": "1", "WK_STATUS_VIEW": "json"}, True), "json")


class TestEndToEndTextModeHasNoEscBytes(WkTest):

    def _run(self, no_color):
        with stub_path({"ssh": _ANSWERING_SSH}) as binp, \
             scratch_dir(prefix="wk-test-remote-root-") as root:
            name = f"demo-{rand_suffix()}"
            (root / "ws" / name).mkdir(parents=True)
            (root / "ws" / name / ".wk-ready").touch()
            env = {
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "WK_REMOTE_ROOT": str(root),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "WK_PROBE_SECONDS": "1",
            }
            if no_color:
                env["NO_COLOR"] = "1"
            else:
                env.pop("NO_COLOR", None)
            return run("status", name, "--text", env=env, timeout=30)


    def test_no_esc_with_no_color_unset_because_stdout_is_a_pipe(self):
        cp = self._run(no_color=False)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn(ESC, cp.stdout, cp.stdout)


if __name__ == "__main__":
    unittest.main()
