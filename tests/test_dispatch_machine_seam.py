"""The dispatcher's two effects go through the one Machine seam: the `tailnet`"""
import contextlib
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import dispatch  # noqa: E402
from wk.machine import Fake  # noqa: E402

START = ("podman", "machine", "start", dispatch.MACHINE)
SELF_ONLY = '{"Self": {"DNSName": "here.tail.ts.net.", "TailscaleIPs": ["100.64.0.1"]}}'


def _needs_tailnet():
    return dispatch.Invocation("x", SimpleNamespace(needs_for=lambda a: "tailnet"), [])


class TestTailnetNeed(unittest.TestCase):
    def _check(self, fake):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            try:
                _needs_tailnet().check_needs(fake)
            except dispatch.Exit:
                return err.getvalue()
        return ""

    def test_a_machine_on_the_tailnet_passes(self):
        fake = Fake()
        fake.answer(("tailscale", "status", "--json"), out=SELF_ONLY)
        self.assertEqual(self._check(fake), "")

    def test_a_machine_whose_tailscale_fails_is_not_on_the_tailnet(self):
        fake = Fake()
        fake.answer(("tailscale", "status", "--json"), rc=1)
        self.assertIn("not on the tailnet", self._check(fake))

    def test_a_machine_without_tailscale_is_not_on_the_tailnet(self):
        self.assertIn("not on the tailnet", self._check(Fake()))


class TestPodmanMachineStart(unittest.TestCase):
    def _start(self, tty, dry=False):
        fake = Fake()
        fake.answer(START)
        env = {"WK_DRY_RUN": "1"} if dry else {}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(dispatch.guest, "podman_admit"), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            try:
                dispatch.start_podman_machine(fake, "enter", tty)
            except dispatch.Exit:
                pass
        return fake, err.getvalue()

    def test_the_start_is_recorded_through_the_machine(self):
        fake, _ = self._start(tty=True)
        self.assertIn(("run_tty", START, None), fake.effects)

    def test_a_dry_run_prints_the_start_and_does_not_run_it(self):
        fake, out = self._start(tty=True, dry=True)
        self.assertIn("would run", out)
        self.assertIn("podman machine start", out)
        self.assertEqual(fake.effects, [])

    def test_no_terminal_refuses_naming_wk_start(self):
        fake, out = self._start(tty=False)
        self.assertIn("wk start", out)
        self.assertEqual(fake.effects, [])


if __name__ == "__main__":
    unittest.main()
