"""Lifecycle commands: `wk start`/`wk stop` (all or one), `wk enter --`, and one real container workspace end to end."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, rand_suffix, requires_container_place, run, stub_path
from tests.test_layers import load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.machine import Fake  # noqa: E402


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
        with mock.patch.object(start, "here", lambda: False), mock.patch.object(start.status, "Walk"), \
                mock.patch.object(start.statusview, "render_text_stream"):
            self.assertIn("would run: podman start wk-a wk-b", self.dry(start.start_everything, self.reg))

    def test_start_everything_starts_a_stopped_podman_machine_the_one_way(self):
        start = load_cmd("start")
        self.ctr.machine_state = lambda: "stopped"
        self.reg.machine = self.m
        with mock.patch.object(start, "here", lambda: True), mock.patch.object(start.status, "Walk"), \
                mock.patch.object(start.statusview, "render_text_stream"), \
                mock.patch.object(start.dispatch, "start_podman_machine") as one:
            self.dry(start.start_everything, self.reg)
        one.assert_called_once_with(self.m, "start", True)

    def test_stop_everything(self):
        stop = load_cmd("stop")
        with mock.patch.object(stop, "here", lambda: False):
            self.assertIn("would run: podman stop --time 30 wk-a wk-b", self.dry(stop.stop_everything, self.reg, False))

    def test_one_workspace(self):
        for verb in ("start", "stop"):
            with self.subTest(verb=verb), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertTrue(getattr(self.ctr, verb)("a"))
            self.assertIn("would run: podman %s" % verb, err.getvalue())
        self.assertEqual([], self.m.effects)


class TestEnterDashDash(unittest.TestCase):
    def test_enter_dash_dash_command_reaches_the_command(self):
        cp = run("enter", "zz-does-not-exist-xyz", "--", "true", env={"WK_PLACE": "vm"})
        self.assertNotIn("--: command not found", cp.stdout)
        self.assertIn("no such workspace", cp.stdout)


@requires_container_place()
class TestContainerLifecycle(WkTest):
    """new -> stop <ws> -> start <ws> -> the shared store mount -> rm <ws> <bogus>."""

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self._created = False

    def tearDown(self):
        if self._created:
            cp = run("rm", self.name, env={"WK_YES": "1"})
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout}")
        super().tearDown()

    def _state_line(self, ls_output):
        return next((l for l in ls_output.splitlines() if self.name in l), "")

    def test_stop_start_shared_mount_and_multi_rm(self):
        cp = run("new", self.name, "--on", "container", timeout=600)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, cp.stdout)

        run("status", self.name, "--wait", "--timeout", "300")

        cp = run("stop", self.name)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        cp = run("ls")
        self.assertIn(self.name, cp.stdout)
        self.assertNotIn("running", self._state_line(cp.stdout))

        cp = run("start", self.name)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("running", self._state_line(run("ls").stdout))

        # The store is mounted at the same path inside, so an in-workspace build's status reaches the host.
        cp = run("enter", self.name, "--", "test", "-d", f"/var/lib/wk/ws/{self.name}")
        self.assertEqual(cp.returncode, 0, cp.stdout)

        cp = run("rm", self.name, f"wk-test-{rand_suffix()}-nonexistent", env={"WK_YES": "1"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn(self.name, run("ls").stdout)
        self._created = False


if __name__ == "__main__":
    unittest.main()
