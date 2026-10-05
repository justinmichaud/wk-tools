"""Audit: "Prompts guard destructive actions only" (CLAUDE.md). Every"""

import os
import pty
import subprocess
import unittest

from tests.support import REPO


def _target_files():
    files = [p for p in sorted((REPO / "cmd").iterdir()) if p.is_file()]
    files += [p for p in sorted((REPO / "admin").iterdir()) if p.is_file()]
    for d in ("lib", "boot", "bench", "image"):
        files += sorted((REPO / d).glob("*.sh"))
    return [p.relative_to(REPO) for p in files]


def _grep(pattern, files):
    if not files:
        return []
    cp = subprocess.run(
        ["grep", "-n", "-E", pattern, *[str(p) for p in files]],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    out = []
    for line in cp.stdout.splitlines():
        path, lineno, content = line.split(":", 2)
        out.append((path, int(lineno), content))
    return out


def _raw_read_sites():
    files = [f for f in _target_files() if f.name != "common.sh"]
    out = {}
    for path, lineno, content in _grep(r"read -r|read -p", files):
        if "while" in content:
            continue
        out[(path, content.strip())] = lineno
    return out


EXPECTED_SAFE_RAW_READS = {
    ("admin/wk-card-priv", 'read -r type tran <<EOF'):
        "reads two fields from a heredoc, not a terminal",
}


class TestOnePromptHelper(unittest.TestCase):

    def test_confirm_is_defined_exactly_once(self):
        sites = _grep(r"^confirm\(\)", _target_files())
        self.assertEqual(
            [(path, content) for path, _, content in sites],
            [("lib/common.sh", "confirm() {")],
            f"expected exactly one confirm() definition, found: {sites}",
        )

    def test_no_competing_raw_read_prompt(self):
        found = _raw_read_sites()
        expected = set(EXPECTED_SAFE_RAW_READS)
        unexpected = set(found) - expected
        self.assertEqual(
            unexpected, set(),
            "raw read(s) outside confirm() not accounted for -- audit each "
            f"(current line, if still there): "
            f"{[(f, c, found[(f, c)]) for f, c in unexpected]}",
        )
        missing = expected - set(found)
        self.assertEqual(
            missing, set(),
            f"expected safe read site(s) not found -- audit is stale: {sorted(missing)}",
        )


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
            out, err = proc.communicate(timeout=20)
            return out, err
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
