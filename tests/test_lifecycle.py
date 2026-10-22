"""`wk start`/`wk stop`, everything or one workspace: the dry run, and `wk start`'s exit code is its own."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, load_cmd, run, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from tests.fakes import Fake  # noqa: E402


# A podman machine that is up with nothing stopped, and a peer that refuses every connection.
STUB_PODMAN = """#!/bin/sh
case "$1 $2" in
  "machine inspect") echo '[{"Name": "wk", "State": "running"}]' ;;
  "machine ssh") ;;
  *) ;;
esac
exit 0
"""
REFUSING_SSH = """#!/bin/sh
echo "ssh: connect to host: Connection refused" >&2
exit 255
"""


class TestStartExitsOnItsOwnResult(WkTest):
    """The status `wk start` prints is information; its exit code is its own."""

    def _env(self, binp):
        return {"PATH": "%s:%s" % (binp, os.environ.get("PATH", "/usr/bin:/bin")),
                "XDG_STATE_HOME": str(self.tmp / "xdg"),
                "WK_REMOTE_ROOT": str(self.tmp / "rr"),
                "WK_MACHINES_DIR": str(self.tmp / "machines"),
                "WK_PLACE": "remote",
                "WK_REMOTE_HOST": "fake-unreachable-machine",
                "WK_PROBE_SECONDS": "1"}

    def setUp(self):
        super().setUp()
        (self.tmp / "machines").mkdir(parents=True)
        ws = self.tmp / "xdg" / "wk" / "remote" / "remote" / "ws" / "peer-ws"
        ws.mkdir(parents=True)
        (ws / "build.status").write_text("state=ok\nexit=0\n")

    def test_an_unreachable_peer_does_not_become_wk_starts_exit_code(self):
        with stub_path({"podman": STUB_PODMAN, "ssh": REFUSING_SSH}) as binp:
            env = self._env(binp)
            status = run("status", env=env, timeout=90)
            start = run("start", env=env, timeout=90)
        self.assertEqual(status.returncode, 4, status.stdout)
        self.assertEqual(start.returncode, 0, start.stdout)


class TestStartAndStopHaveADryRun(unittest.TestCase):
    """`wk start` and `wk stop`, everything or one container workspace, under --dry-run: each podman change printed, none run."""

    def setUp(self):
        self.m = Fake("here")
        self.m.answer(["podman", "ps"], out="wk-a\nwk-b\n")
        self.ctr = places.Container("container", str(REPO), {}, self.m)
        self.reg = mock.Mock()
        self.reg.load.return_value = self.ctr
        for p in (mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), mock.patch.object(self.ctr, "podman", lambda: ["podman"]),
                  mock.patch("wk.secrets.Secrets")):
            p.start()
            self.addCleanup(p.stop)

    def dry(self, fn, *args):
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, fn(*args))
        self.assertEqual([], [e for e in self.m.effects if e[0] != "run" or e[1][1] != "ps"], "only the listing ran")
        return err.getvalue()

    def test_start_everything(self):
        start = load_cmd("start")
        with mock.patch.object(start.Store, "macos_host", False):
            self.assertIn("would run: podman start wk-a wk-b", self.dry(start.start_everything, self.reg))

    def test_start_everything_starts_a_stopped_podman_machine_the_one_way(self):
        start = load_cmd("start")
        self.ctr.machine_state = lambda: "stopped"
        self.reg.machine = self.m
        with mock.patch.object(start.Store, "macos_host", True), \
                mock.patch.object(start.dispatch, "start_podman_machine") as one:
            self.dry(start.start_everything, self.reg)
        one.assert_called_once_with(self.m, "start", True)

    def test_stop_everything(self):
        from wk import stop
        with mock.patch.object(stop.Store, "macos_host", False):
            self.assertIn("would run: podman stop --time 30 wk-a wk-b", self.dry(stop.stop_everything, self.reg, False))

    def test_one_workspace(self):
        for verb in ("start", "stop"):
            with self.subTest(verb=verb), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertTrue(getattr(self.ctr, verb)("a"))
            self.assertIn("would run: podman %s" % verb, err.getvalue())
        self.assertEqual([], self.m.effects)


if __name__ == "__main__":
    unittest.main()
