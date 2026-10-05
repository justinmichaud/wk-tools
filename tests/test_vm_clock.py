"""The guest's clock, set from the host on every start (lib/wk/guest.py): Softnet carries no NTP, so a clone keeps
the base's date and TLS fails. The guest is this host, with a fake `date` and a `sudo` that records."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, places  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.store import Store  # noqa: E402


# Pins `date -u +%s` to FAKE_EPOCH, so the skew is arithmetic rather than two reads of a moving clock.
FAKE_DATE = """#!/bin/sh
if [ "$1" = "-u" ] && [ "$2" = "+%s" ]; then echo "$FAKE_EPOCH"; exit 0; fi
exec /bin/date "$@"
"""

HOST_EPOCH = 1788900000

# `printf`, not `echo`: the first argument is `-n`.
FAKE_SUDO = """#!/bin/sh
printf '%s\\n' "$*" >> "$SUDO_LOG"
exit ${SUDO_RC:-0}
"""


class TestGuestClock(WkTest):
    def _drive(self, skew, sudo_rc=0, tolerance=None):
        """Set the clock of a guest whose clock is `skew` seconds behind the host's (negative for ahead).
        Returns (rc=<0|1> and what was said, sudo log text)."""
        datebin = self.tmp / "date-bin"
        sudobin = self.tmp / "sudo-bin"
        for d, name, body in ((datebin, "date", FAKE_DATE),
                              (sudobin, "sudo", FAKE_SUDO)):
            d.mkdir(exist_ok=True)
            (d / name).write_text(body)
            (d / name).chmod(0o755)
        log = self.tmp / "sudo.log"
        log.write_text("")
        env = {"HOME": str(self.tmp), "WK_VM_STORE": str(self.tmp / "vmstore"), "WK_STORE": str(self.tmp / "store")}
        if tolerance:
            env["WK_VM_CLOCK_SKEW"] = tolerance
        guest_env = {"PATH": "%s:%s:%s" % (datebin, sudobin, os.environ["PATH"]), "FAKE_EPOCH": str(HOST_EPOCH - skew),
                     "SUDO_LOG": str(log), "SUDO_RC": str(sudo_rc)}
        err = io.StringIO()
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch.dict(os.environ, guest_env), contextlib.redirect_stderr(err):
            vm = places.Registry(str(REPO), env=env).load("vm")
            g = guest.Guest(guest.Host(vm, FakeClock(HOST_EPOCH)), "testguest", Local())
            rc = 0 if g.set_guest_clock() else 1
        return "rc=%d\n%s" % (rc, err.getvalue()), log.read_text()

    def test_a_guest_behind_or_ahead_of_the_host_is_set_from_it(self):
        for skew, said in ((11 * 86400, "950400s"), (-3600, "3600s")):
            with self.subTest(skew=skew):
                out, sudolog = self._drive(skew)
                self.assertIn("rc=0", out, out)
                self.assertIn("clock was", out, out)
                self.assertIn(said, out, out)
                self.assertIn("-n date -u ", sudolog, sudolog)

    def test_a_guest_already_in_time_is_left_alone(self):
        out, sudolog = self._drive(0)
        self.assertIn("rc=0", out, out)
        self.assertNotIn("clock was", out, out)
        self.assertEqual("", sudolog.strip(), sudolog)

    def test_a_guest_that_refuses_the_write_fails(self):
        out, _ = self._drive(86400, sudo_rc=1)
        self.assertIn("rc=1", out, out)

    def test_wk_vm_clock_skew_sets_the_tolerance(self):
        out, sudolog = self._drive(300, tolerance="600")
        self.assertEqual("", sudolog.strip(), out)
        out, sudolog = self._drive(300)
        self.assertIn("-n date -u ", sudolog, out)


if __name__ == "__main__":
    unittest.main()
