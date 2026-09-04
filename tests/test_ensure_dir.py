"""lib/common.sh: `ensure_dir` converges on a mode, it does not re-apply one.

Every store directory in the fleet is made by this one function, and two of
them arrive as read-only mounts rather than as directories anything here
creates: inside the podman machine the secrets directory is this host's,
mounted read-only (host/macos/machine.sh), already 0700. A chmod of a path on
a read-only filesystem fails whatever it asks for, so a function that re-states
a mode that is already right is a function that breaks `wk new` on a machine
whose mounts are exactly as the design says they must be.

Driven with a `chmod` of this test's own on PATH: what matters is whether one
is issued at all, which the resulting mode cannot answer.

Run: python3 -m unittest tests.test_ensure_dir -v
"""

import os
import unittest

from tests.support import WkTest, bash

# Sourced by each case, with a `chmod` that records its arguments and one that
# refuses the way a read-only filesystem does.
PREAMBLE = """
set -uo pipefail
mkdir -p "$BIN"
cat > "$BIN/chmod" <<'EOF'
#!/bin/sh
echo "chmod $*" >> "$LOG"
[ -z "${CHMOD_FAILS:-}" ] || exit 1
exec /bin/chmod "$@"
EOF
chmod 0755 "$BIN/chmod"
. "$WK_ROOT/lib/common.sh"
# After the source: lib/common.sh settles PATH itself (shell/path.sh), and a
# chmod put in front of it before that is one the source puts back behind.
export PATH="$BIN:$PATH"
"""


class TestEnsureDir(WkTest):
    def _run(self, script, env=None):
        d = self.tmp
        cp = bash(
            f'export BIN={d}/bin LOG={d}/chmod.log DIR={d}/store\n'
            f'{PREAMBLE}\n{script}',
            env=env,
        )
        log = d / "chmod.log"
        calls = log.read_text().splitlines() if log.exists() else []
        return cp, calls, str(d / "store")

    def test_a_missing_directory_is_created_with_the_mode(self):
        cp, calls, d = self._run('ensure_dir "$DIR" 0700; echo done')
        self.assertIn("done", cp.stdout, cp.stdout + cp.stderr)
        self.assertEqual(0o700, os.stat(d).st_mode & 0o777)
        self.assertTrue(calls, "no chmod was issued for a directory it created")

    def test_a_directory_that_already_has_the_mode_is_not_chmodded(self):
        cp, calls, _ = self._run(
            'mkdir -p "$DIR" && /bin/chmod 0700 "$DIR"\n'
            ': > "$LOG"\n'
            'ensure_dir "$DIR" 0700; echo done')
        self.assertIn("done", cp.stdout, cp.stdout + cp.stderr)
        self.assertEqual([], calls, calls)

    def test_a_directory_with_the_wrong_mode_is_converged(self):
        cp, calls, d = self._run(
            'mkdir -p "$DIR" && /bin/chmod 0755 "$DIR"\n'
            ': > "$LOG"\n'
            'ensure_dir "$DIR" 0700; echo done')
        self.assertIn("done", cp.stdout, cp.stdout + cp.stderr)
        self.assertEqual(1, len(calls), calls)
        self.assertEqual(0o700, os.stat(d).st_mode & 0o777)

    def test_a_read_only_mount_that_is_already_right_is_left_alone(self):
        """The live case: `wk new` inside the podman machine, whose store
        directory is a read-only virtiofs mount at 0700. Every chmod here
        fails, so reaching the end proves none was issued."""
        cp, calls, _ = self._run(
            'mkdir -p "$DIR" && /bin/chmod 0700 "$DIR"\n'
            ': > "$LOG"\n'
            'CHMOD_FAILS=1 ensure_dir "$DIR" 0700; echo done')
        self.assertIn("done", cp.stdout, cp.stdout + cp.stderr)
        self.assertEqual([], calls, calls)

    def test_a_read_only_mount_with_the_wrong_mode_says_so(self):
        """The other half: a mount that is genuinely wrong cannot be fixed
        from in here, and a caller must hear that rather than carry on."""
        cp, _, _ = self._run(
            'mkdir -p "$DIR" && /bin/chmod 0755 "$DIR"\n'
            'CHMOD_FAILS=1 ensure_dir "$DIR" 0700; echo done')
        self.assertNotIn("done", cp.stdout)
        self.assertIn("cannot set mode 0700", cp.stdout + cp.stderr)

    def test_a_caller_that_names_no_mode_asks_only_that_it_exists(self):
        cp, calls, d = self._run(
            'mkdir -p "$DIR" && /bin/chmod 0755 "$DIR"\n'
            ': > "$LOG"\n'
            'ensure_dir "$DIR"; echo done')
        self.assertIn("done", cp.stdout, cp.stdout + cp.stderr)
        self.assertEqual([], calls, calls)
        self.assertEqual(0o755, os.stat(d).st_mode & 0o777)


class TestFileMode(WkTest):
    """The reader ensure_dir asks, and the one cmd/key prints a credential's
    mode with: octal permission bits, on a GNU stat and a BSD one alike."""

    def test_it_reads_the_bits_without_a_leading_zero(self):
        d = self.tmp
        os.chmod(d, 0o700)
        cp = bash(f'. "$WK_ROOT/lib/common.sh"; file_mode {d}')
        self.assertEqual("700", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_a_path_that_is_not_there_reads_empty(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; file_mode /nonexistent-wk-test')
        self.assertEqual("", cp.stdout.strip(), cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
