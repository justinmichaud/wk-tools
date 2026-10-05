"""wk_atexit (lib/common.sh): a handler runs in the process that asked for it and in no other."""
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


if __name__ == "__main__":
    unittest.main()
