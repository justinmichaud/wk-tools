"""lib/common.sh: `ensure_dir` converges on a mode and issues no chmod when it is already right, since a store
directory may be a read-only mount."""

import os
import unittest

from tests.support import WkTest, bash

# A `chmod` that records its arguments, and refuses as a read-only filesystem does under CHMOD_FAILS.
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
export PATH="$BIN:$PATH"
"""


class TestEnsureDir(WkTest):
    def _run(self, script, env=None):
        d = self.tmp
        cp = bash(f'export BIN={d}/bin LOG={d}/chmod.log DIR={d}/store\n{PREAMBLE}\n{script}', env=env)
        log = d / "chmod.log"
        calls = log.read_text().splitlines() if log.exists() else []
        return cp, calls, str(d / "store")

    def test_it_converges_on_the_mode_and_chmods_only_when_the_mode_is_wrong(self):
        # (mode the directory has or None, mode asked, chmod refuses, chmods issued, mode after)
        for before, mode, ro, chmods, after in ((None, "0700", "", None, 0o700), ("0700", "0700", "", 0, 0o700),
                                                ("0755", "0700", "", 1, 0o700), ("0700", "0700", "1", 0, 0o700),
                                                ("0755", "", "", 0, 0o755)):
            with self.subTest(before=before, mode=mode, read_only=bool(ro)):
                pre = 'rm -rf "$DIR"\n' + ('mkdir -p "$DIR" && /bin/chmod %s "$DIR"\n' % before if before else "")
                cp, calls, d = self._run(pre + ': > "$LOG"\nCHMOD_FAILS=%s ensure_dir "$DIR" %s; echo done' % (ro, mode))
                self.assertIn("done", cp.stdout, cp.stdout + cp.stderr)
                self.assertEqual(after, os.stat(d).st_mode & 0o777)
                if chmods is None:
                    self.assertTrue(calls, "no chmod was issued for a directory it created")
                else:
                    self.assertEqual(chmods, len(calls), calls)

    def test_a_read_only_mount_with_the_wrong_mode_says_so(self):
        cp, _, _ = self._run(
            'mkdir -p "$DIR" && /bin/chmod 0755 "$DIR"\n'
            'CHMOD_FAILS=1 ensure_dir "$DIR" 0700; echo done')
        self.assertNotIn("done", cp.stdout)
        self.assertIn("0700", cp.stdout + cp.stderr)


class TestFileMode(WkTest):
    def test_it_reads_the_bits_without_a_leading_zero_and_nothing_for_no_path(self):
        os.chmod(self.tmp, 0o700)
        for path, want in ((self.tmp, "700"), ("/nonexistent-wk-test", "")):
            with self.subTest(path=path):
                cp = bash(f'. "$WK_ROOT/lib/common.sh"; file_mode {path}')
                self.assertEqual(want, cp.stdout.strip(), cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
