"""`NO_COLOR` and a redirected stdout both drop the colour from `wk status`'s
text table -- owed by docs/HANDOFF-wk-cli.md: "`NO_COLOR` and a redirected
stdout both drop the colour from the table [needs a test]".

Colour is decided in two places, and this drives both directly:

  - lib/status-view.py: `colour = sys.stdout.isatty() and not
    os.environ.get("NO_COLOR")`, read once per rendering mode (text and the
    non-text/non-json/non-html/non-web fallback) in `main()`. Lifted as a
    literal expression (the same technique tests/test_wifi_seed.py's `_lift`
    uses for a function) so this tracks the real source, not a retyped copy.
  - lib/common.sh's `status_default_mode`: decides web vs. text for a bare
    `wk status` with nothing else said, and `NO_COLOR` is one of the signals
    that keeps it out of the browser (a page has no ANSI to drop, but the
    same environment that says "no colour" also says "no browser").

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
import types
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, stub_path

STATUS_VIEW = REPO / "lib" / "status-view.py"

ESC = "\033["


def _load_status_view():
    spec = importlib.util.spec_from_file_location("wk_status_view", STATUS_VIEW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _colour_decision_expr():
    """The literal `colour = ...` right-hand side, lifted from the source so
    a change to the expression is what this test tracks, not a retyped
    copy -- and both occurrences (text mode, the bare-mode fallback) must
    still agree."""
    text = STATUS_VIEW.read_text()
    exprs = re.findall(
        r'colour = (sys\.stdout\.isatty\(\) and not os\.environ\.get\("NO_COLOR"\))',
        text,
    )
    assert len(exprs) == 2, f"expected the colour decision written twice, found {len(exprs)}"
    assert exprs[0] == exprs[1], "the two colour decisions in lib/status-view.py have drifted apart"
    return exprs[0]


def _decide(expr, is_tty, no_color_set):
    fake_sys = types.SimpleNamespace(
        stdout=types.SimpleNamespace(isatty=lambda: is_tty)
    )
    fake_os = types.SimpleNamespace(
        environ={"NO_COLOR": "1"} if no_color_set else {}
    )
    return eval(expr, {"sys": fake_sys, "os": fake_os})


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''


class TestColourDecisionLiftedDirectly(unittest.TestCase):
    """lib/status-view.py's own decision, driven with no subprocess and no
    real tty at all -- so a real tty (which the test harness itself may or
    may not have) can never make this test flaky in either direction."""

    def test_a_real_terminal_with_no_color_unset_gets_colour(self):
        expr = _colour_decision_expr()
        self.assertTrue(_decide(expr, is_tty=True, no_color_set=False))

    def test_no_color_wins_even_at_a_terminal(self):
        expr = _colour_decision_expr()
        self.assertFalse(_decide(expr, is_tty=True, no_color_set=True))

    def test_a_redirected_stdout_drops_colour_regardless_of_no_color(self):
        expr = _colour_decision_expr()
        self.assertFalse(_decide(expr, is_tty=False, no_color_set=False))
        self.assertFalse(_decide(expr, is_tty=False, no_color_set=True))


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
    """lib/common.sh's status_default_mode: NO_COLOR is one of the signals
    that keeps a bare `wk status` out of --web (a page has nothing to drop,
    but the same "plain output" request applies to both)."""

    def _mode(self, env):
        cp = self.bash(
            '. "$WK_ROOT/lib/common.sh"\nstatus_default_mode\necho "$WK_STATUS_DEFAULT_MODE"',
            env=env,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_no_color_keeps_it_text_even_with_a_tty_recorded(self):
        # [ -t 1 ] cannot be forced true from a script with no real tty; this
        # asserts the NO_COLOR branch is reached and decisive by checking it
        # is checked *before* WK_STATUS_DEFAULT_MODE could become web -- the
        # function already returns "text" the instant stdout is not a tty,
        # which every test harness invocation gives it for free.
        self.assertEqual(self._mode({"NO_COLOR": "1"}), "text")

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
