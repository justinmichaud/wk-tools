"""lib/common.sh's confirm(), the one prompt in bash (tests/test_lint.py one_prompt): No by default, and no terminal declines."""

import os
import pty
import subprocess
import unittest

from tests.support import REPO


class TestConfirmDefaultsToNoAndDeclinesWithoutATerminal(unittest.TestCase):
    SCRIPT = (
        ". lib/common.sh\n"
        "rc=0\n"
        "confirm 'do the thing?' || rc=$?\n"
        "echo RC=$rc\n"
    )

    def test_declines_a_piped_yes_because_a_pipe_is_not_a_tty(self):
        cp = subprocess.run(
            ["bash", "-c", self.SCRIPT],
            cwd=str(REPO),
            env={**os.environ, "WK_ROOT": str(REPO)},
            input="y\n",
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertIn("RC=1", cp.stdout, cp.stderr)

    def _confirm_over_a_real_tty(self, reply):
        master, slave = pty.openpty()
        try:
            env = {**os.environ, "WK_ROOT": str(REPO)}
            env.pop("WK_YES", None)
            proc = subprocess.Popen(
                ["bash", "-c", self.SCRIPT],
                cwd=str(REPO),
                env=env,
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(slave)
            slave = -1
            os.write(master, reply)
            return proc.communicate(timeout=20)
        finally:
            if slave != -1:
                os.close(slave)
            os.close(master)

    def test_only_y_at_a_terminal_is_yes(self):
        for reply, rc in ((b"\n", 1), (b"n\n", 1), (b"y\n", 0), (b"Y\n", 0)):
            with self.subTest(reply=reply):
                out, err = self._confirm_over_a_real_tty(reply)
                self.assertIn("RC=%d" % rc, out, err)


if __name__ == "__main__":
    unittest.main()
