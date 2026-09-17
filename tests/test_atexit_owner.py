"""wk_atexit (lib/common.sh): a handler runs in the process that asked for it
and in no other.

bash keeps one `trap ... EXIT`, so the handlers are a list and the trap is
re-armed on every registration. A subshell inherits both, so anything that
registers a cleanup of its own inside one arms the trap there -- and the
subshell's exit then runs the list it inherited, including the handlers of the
process around it. Measured 2026-09-16 (bash 5.2): `wk ab` drew its step graph
into a temporary file, and `$(ws_target ...)` deleted it half way through, so
the first arm's steps were gone by the time the second declared its own and
the graph named a step nothing had declared.

Run: python3 -m unittest tests.test_atexit_owner -v
"""
import unittest

from tests.support import REPO, WkTest, bash

COMMON = '. "%s/lib/common.sh"' % REPO


class TestAHandlerBelongsToItsProcess(WkTest):
    def test_the_process_that_registered_it_runs_it(self):
        cp = bash(COMMON + '''
gone() { echo "CLEANED"; }
wk_atexit gone
echo "BODY"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split(), ["BODY", "CLEANED"])

    def test_a_subshell_that_registers_its_own_does_not_run_the_parents(self):
        """The shape that lost cmd/ab its step file: the inner registration
        arms the trap inside the substitution, and its exit would otherwise
        run every handler the list had inherited."""
        cp = bash(COMMON + '''
outer_gone() { echo "OUTER-CLEANED"; }
inner_gone() { echo "INNER-CLEANED"; }
wk_atexit outer_gone
inner() { wk_atexit inner_gone; echo value; }
v=$(inner)
echo "AFTER $v"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        words = cp.stdout.split()
        self.assertIn("INNER-CLEANED", words, cp.stdout)
        self.assertEqual(words.count("OUTER-CLEANED"), 1, cp.stdout)
        self.assertEqual(words[-1], "OUTER-CLEANED", cp.stdout)

    def test_a_file_the_parent_is_still_writing_survives_such_a_subshell(self):
        """The behaviour, not the mechanism: a temporary the parent holds open
        across several command substitutions is still there afterwards."""
        cp = bash(COMMON + '''
f=$(mktemp)
gone() { rm -f "$f"; }
wk_atexit gone
echo one >> "$f"
inner() { local g; g=$(mktemp); wk_atexit gone2; echo x; }
gone2() { :; }
v=$(inner)
echo two >> "$f"
wc -l < "$f" | tr -d " "
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "2", cp.stdout)

    def test_registering_twice_in_one_process_runs_it_once(self):
        cp = bash(COMMON + '''
gone() { echo "CLEANED"; }
wk_atexit gone
wk_atexit gone
''')
        self.assertEqual(cp.stdout.split().count("CLEANED"), 1, cp.stdout)

    def test_the_exit_status_still_reaches_the_handlers(self):
        """WK_EXIT_STATUS is published rather than passed, and several
        handlers (a task record's end, the forced summary) read it."""
        cp = bash(COMMON + '''
gone() { echo "STATUS=$WK_EXIT_STATUS"; }
wk_atexit gone
exit 3
''')
        self.assertEqual(cp.returncode, 3)
        self.assertIn("STATUS=3", cp.stdout)

    def test_the_exit_status_is_not_replaced_by_the_handlers_own(self):
        cp = bash(COMMON + '''
gone() { false; }
wk_atexit gone
exit 4
''')
        self.assertEqual(cp.returncode, 4, cp.stdout + cp.stderr)


class TestTheGuardIsInOnePlace(WkTest):
    """One implementation per rule: the ownership check lives where the
    handlers are run, so every caller gets it and none re-decides."""

    def test_the_handler_list_carries_the_pid(self):
        text = (REPO / "lib" / "common.sh").read_text()
        self.assertIn('_WK_ATEXIT="$_WK_ATEXIT $_me"', text)
        self.assertIn('[ "${_h%%:*}" = "$_me" ] || continue', text)

    def test_the_pid_is_expanded_and_never_taken_from_a_substitution(self):
        """A command substitution's subshell has a BASHPID of its own, so a
        function answering with one would name the wrong process every time."""
        text = (REPO / "lib" / "common.sh").read_text()
        self.assertIn('local _me="${BASHPID:-$$}:$1"', text)
        self.assertNotIn("$(_wk_pid)", text)

    def test_no_command_keeps_a_guard_of_its_own(self):
        """A second copy can drift into permitting what the first refuses."""
        for rel in ("cmd/ab", "image/pgo.sh", "cmd/pi"):
            with self.subTest(file=rel):
                self.assertNotIn("BASHPID", (REPO / rel).read_text())


if __name__ == "__main__":
    unittest.main()
