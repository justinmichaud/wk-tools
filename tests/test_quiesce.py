"""Tests for cmd/quiesce's state-directory handling.

cmd/quiesce keeps exactly one state directory (WK_QUIESCE_STATE, or
$(wk_state_dir)/quiesce). A directory left behind by an older wk-tools at
${TMPDIR:-/tmp}/wk-quiesce is not read, undone, or migrated into it -- one
state directory, no second copy of the same fact -- but `status` names it as
stale so a person can `rm -rf` it themselves.

Only `status` is exercised here (never `on`/`off`): `status` is read-only,
and this suite must never touch the real host's quiesce state.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUIESCE = REPO / "cmd" / "quiesce"


def _dead_pid():
    """A pid guaranteed to belong to no running process: start a real
    process, wait for it to exit and be reaped, then hand back that pid.
    The OS reusing that exact number in the next few milliseconds is not a
    real risk."""
    p = subprocess.Popen(["true"])
    pid = p.pid
    p.wait()
    return pid


def run_quiesce(*args, env, timeout=60):
    return subprocess.run(
        [str(QUIESCE), *args],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


class QuiesceStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-quiesce-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.state = self.tmp / "state"
        self.state.mkdir()
        self.tmpdir = self.tmp / "tmpdir"
        self.tmpdir.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()

    def _env(self):
        # WK_QUIESCE_STATE and TMPDIR pin exactly where cmd/quiesce looks
        # (see STATE / STALE_STATE near its top); HOME and a cleared
        # XDG_STATE_HOME keep $(wk_state_dir) from resolving anywhere real
        # in case that fallback is ever hit.
        env = dict(os.environ)
        env["WK_QUIESCE_STATE"] = str(self.state)
        env["TMPDIR"] = str(self.tmpdir)
        env["HOME"] = str(self.home)
        env.pop("XDG_STATE_HOME", None)
        env.pop("WK_STORE", None)
        return env

    def test_dead_pid_files_report_not_running(self):
        pid = _dead_pid()
        (self.state / "caffeinate.pid").write_text(str(pid))
        (self.state / "raiser.pid").write_text(str(pid))

        cp = run_quiesce("status", env=self._env())

        self.assertIn("caffeinate: no", cp.stdout, cp.stdout)
        self.assertNotIn("caffeinate: running", cp.stdout)
        # raiser is only reported on macOS; skip the assertion elsewhere.
        if "raiser:" in cp.stdout:
            self.assertNotIn("raiser:     running", cp.stdout)

    def test_live_pid_reports_running(self):
        p = subprocess.Popen(["sleep", "5"])
        try:
            (self.state / "caffeinate.pid").write_text(str(p.pid))
            cp = run_quiesce("status", env=self._env())
            self.assertIn("caffeinate: running", cp.stdout, cp.stdout)
        finally:
            p.terminate()
            p.wait()

    def test_missing_pid_files_report_not_running(self):
        cp = run_quiesce("status", env=self._env())
        self.assertIn("caffeinate: no", cp.stdout, cp.stdout)

    def test_stale_legacy_dir_is_named_not_read(self):
        legacy = self.tmpdir / "wk-quiesce"
        legacy.mkdir()
        # Something that would prove the stale dir was actually read: an
        # 'on'-shaped pid file in it must never surface as "running".
        (legacy / "caffeinate.pid").write_text(str(_dead_pid()))

        cp = run_quiesce("status", env=self._env())

        self.assertIn(str(legacy), cp.stdout, cp.stdout)
        self.assertIn("stale", cp.stdout.lower())
        self.assertNotIn("caffeinate: running", cp.stdout)

    def test_no_stale_dir_nothing_is_named(self):
        cp = run_quiesce("status", env=self._env())
        self.assertNotIn(str(self.tmpdir / "wk-quiesce"), cp.stdout)

    def test_no_legacy_state_variable_in_source(self):
        # static: the two-state-directory design is removed, not renamed --
        # a second spelling of the same variable would be the same bug back.
        text = QUIESCE.read_text()
        self.assertNotIn("LEGACY_STATE", text)


if __name__ == "__main__":
    unittest.main()
