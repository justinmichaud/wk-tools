"""`wk logs` shows `(none)` on a good build, owed by
docs/HANDOFF-test-runner.md: "catches: `error:` matching inside message
text". `first_error` (lib/watchdog.sh) greps a build.log for lines that look
like a compiler/ninja failure; the risk this guards is a bare, unanchored
`error:` matching a line that merely *talks about* an error (a log message
whose own text is "-- no error: handling here") rather than reporting one.

Driven the way tests/test_logs.py drives cmd/logs directly (WK_NAME/WK_TARGET=
vm/WK_VM_STORE, bypassing ./wk so a leftover WK_TARGET in this shell cannot
point it elsewhere).

Run: python3 -m unittest tests.test_owed_logs -v
"""
import unittest

from tests.support import REPO, WkTest, bash

CMD_LOGS = REPO / "cmd" / "logs"


class TestLogsShowsNoneOnAGoodBuild(WkTest):
    def _run(self, name, store):
        env = {"WK_NAME": name, "WK_TARGET": "vm", "WK_VM_STORE": str(store)}
        return bash(f'exec "{CMD_LOGS}"', env=env)

    def test_a_message_containing_the_word_error_mid_sentence_is_not_reported(self):
        """No real failure line, but the log's own text contains the
        substring 'error:' inside an unrelated sentence -- first_error's
        anchored patterns (^error:, : error:, ...) must not fire on it, so
        `wk logs` reports '(none)', not that sentence as a build error."""
        name = "goodws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building target foo\n"
            "-- no error: handling here, everything is fine\n"
            "ninja: no work to do.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("(none)", out, out)
        # first_error must not have surfaced the sentence as an error line.
        errors_section = out.split("errors:", 1)[1].split("last output:", 1)[0]
        self.assertNotIn("no error: handling here", errors_section, out)

    def test_a_real_ninja_failure_still_reports_by_the_same_path(self):
        """Contrast: a genuine failure line is still caught, so '(none)'
        above is not the result of first_error being broken outright."""
        name = "badws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building target foo\n"
            "foo.cpp:10:5: error: use of undeclared identifier 'x'\n"
            "ninja: build stopped: subcommand failed.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertNotIn("(none)", out, out)
        self.assertIn("ninja: build stopped", out, out)


if __name__ == "__main__":
    unittest.main()
