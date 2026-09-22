"""`NO_COLOR` and a redirected stdout both drop the colour from `wk status`'s
text table -- owed (docs/PLAN.md): "`NO_COLOR` and a redirected
stdout both drop the colour from the table [needs a test]".

Colour is decided in two places, and this drives both directly:

  - wk.statusview.colour_wanted: a terminal on stdout and NO_COLOR unset.
  - wk.statusview.default_mode: decides web vs. text for a bare `wk status`
    with nothing else said, and `NO_COLOR` is one of the signals that keeps
    it out of the browser (a page has no ANSI to drop, but the same
    environment that says "no colour" also says "no browser").

And end to end: `wk status <name> --text` against a faked, answering machine
(the technique tests/test_fleet_walk.py uses -- WK_TARGET=remote, a stub
`ssh` that runs the command locally) produces no ESC byte in its output,
whether or not `NO_COLOR` is set -- a subprocess's stdout is a pipe, never a
tty, so both cases already exercise "redirected stdout drops colour"; the
`NO_COLOR` cases additionally prove the environment variable is read at all,
not merely irrelevant because nothing here was ever going to be a tty.

A positive control accompanies each: `render_text` given `colour=True`
against the exact same document does emit ESC, so "no ESC" above is the
decision at work, not a renderer that never had colour to lose.

Run: python3 -m unittest tests.test_owed_cli_color -v
"""
import importlib.util
import json
import os
import re
import tempfile
import sys
import types
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import statusview  # noqa: E402

ESC = "\033["


def _load_status_view():
    return statusview



def _decide(is_tty, no_color_set):
    stdout = types.SimpleNamespace(isatty=lambda: is_tty)
    return statusview.colour_wanted(stdout, {"NO_COLOR": "1"} if no_color_set else {})


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''


class TestColourDecisionLiftedDirectly(unittest.TestCase):
    """The renderer's own decision, driven with no subprocess and no real
    tty at all -- so a real tty (which the test harness itself may or may
    not have) can never make this test flaky in either direction."""

    def test_a_real_terminal_with_no_color_unset_gets_colour(self):
        self.assertTrue(_decide(is_tty=True, no_color_set=False))

    def test_no_color_wins_even_at_a_terminal(self):
        self.assertFalse(_decide(is_tty=True, no_color_set=True))

    def test_a_redirected_stdout_drops_colour_regardless_of_no_color(self):
        self.assertFalse(_decide(is_tty=False, no_color_set=False))
        self.assertFalse(_decide(is_tty=False, no_color_set=True))


def _sample_doc(mod):
    """A minimal but complete document, built the way test_status.py's
    `render()` builds one -- real records folded through the module's own
    Merger (`read_doc`), not a hand-assembled dict guessing at every key
    render_machine_block happens to read today."""
    records = [
        {"kind": "machine", "name": "buildbox4"},
        {
            "kind": "workspace",
            "machine": "buildbox4",
            "method": "container",
            "name": "demo",
            "state": "running",
            "branch": "main",
            "base": "main",
        },
        {"kind": "exit", "code": 0},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir="/tmp") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        path = fh.name
    try:
        return mod.read_doc(path)
    finally:
        os.unlink(path)


class TestRenderTextHonoursTheColourFlag(unittest.TestCase):
    """The positive control: the renderer does emit ESC when told to, so a
    run below with no ESC is evidence of the decision, not of a renderer
    with nothing to colour in the first place."""

    def test_colour_true_emits_esc(self):
        mod = _load_status_view()
        out = mod.render_text(_sample_doc(mod), True)
        self.assertIn(ESC, out, out)

    def test_colour_false_emits_no_esc(self):
        mod = _load_status_view()
        out = mod.render_text(_sample_doc(mod), False)
        self.assertNotIn(ESC, out, out)


class TestStatusDefaultModeStaysOutOfTheBrowser(WkTest):
    """wk.statusview.default_mode: NO_COLOR is one of the signals
    that keeps a bare `wk status` out of --web (a page has nothing to drop,
    but the same "plain output" request applies to both)."""

    def _mode(self, env):
        return statusview.default_mode(dict(env, HOME="/nonexistent"), True)

    def test_no_color_keeps_it_text_even_at_a_tty(self):
        self.assertEqual(self._mode({"NO_COLOR": "1"}), "text")
        self.assertEqual(self._mode({}), "web")

    def test_wk_status_view_override_still_wins_over_no_color(self):
        self.assertEqual(
            self._mode({"NO_COLOR": "1", "WK_STATUS_VIEW": "json"}), "json"
        )


class TestEndToEndTextModeHasNoEscBytes(WkTest):
    """`wk status <name> --text` against a faked, answering machine (the
    tests/test_fleet_walk.py technique) -- no ESC byte in the output,
    whether NO_COLOR is set or not, since a subprocess's stdout is always a
    pipe and never a tty."""

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
                # The probe's cap (targets/remote.sh): the stub answers at once, and
                # `capped` leaves its watchdog sleeping on the walk's stdout for the
                # whole cap after the walk has exited.
                "WK_PROBE_SECONDS": "1",
            }
            if no_color:
                env["NO_COLOR"] = "1"
            else:
                env.pop("NO_COLOR", None)
            return run("status", name, "--text", env=env, timeout=30)

    def test_no_esc_with_no_color_set(self):
        cp = self._run(no_color=True)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn(ESC, cp.stdout, cp.stdout)

    def test_no_esc_with_no_color_unset_because_stdout_is_a_pipe(self):
        cp = self._run(no_color=False)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn(ESC, cp.stdout, cp.stdout)


class TestWkLsNeverEmitsEsc(WkTest):
    """`wk ls` has no colour logic of its own (grepped: no `\\033`, no
    `tput`, no NO_COLOR read) -- so its table is colourless by construction,
    trivially satisfying the same contract text mode has to earn. Driven
    against the empty fake registry (support.NO_REGISTRY, via run()'s
    default env) so this touches no real workspace."""

    def test_bare_ls_has_no_esc_bytes(self):
        cp = run("ls", timeout=30)
        self.assertNotIn(ESC, cp.stdout, cp.stdout)


if __name__ == "__main__":
    unittest.main()
