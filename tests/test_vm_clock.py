"""The guest's clock, set from the host on every start (lib/wk/guest.py).

A macOS guest cannot keep its own time. `tart clone` hands a clone the golden
base's clock, and Softnet allows one address -- the proxy -- while NTP is UDP,
which an HTTP CONNECT proxy cannot carry. Left alone the clock stays at the
date the base was sealed, and every TLS handshake to a certificate issued
after it fails as CERT_NOT_YET_VALID: `wk ai claude` in the guest reports
`Failed to connect to platform.claude.com`, which reads like an egress refusal
and is not one.

Hermetic: the guest is this host, running the step's script locally. The
host's clock is a FakeClock, the guest's a fake `date` the test pins, and
`sudo` is a fake that records what would have been set. No tart, no VM, no
network.

Run: python3 -m unittest tests.test_vm_clock -v
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, assert_guest_start_converges

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, targets  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.store import Store  # noqa: E402


def _src(*parts):
    return REPO.joinpath(*parts).read_text()


# Both clocks the function compares are pinned by this one fake, each half
# reaching it with its own FAKE_EPOCH: the skew asserted below is then
# arithmetic rather than the difference between two reads of a moving clock,
# which crossed a second boundary and reported 950401s. Everything but
# `-u +%s` is the real date, so the set form the function builds is still
# parsed by a real `date` if it is ever run.
FAKE_DATE = """#!/bin/sh
if [ "$1" = "-u" ] && [ "$2" = "+%s" ]; then echo "$FAKE_EPOCH"; exit 0; fi
exec /bin/date "$@"
"""

HOST_EPOCH = 1788900000

# `printf`, not `echo`: the first argument is `-n`, which /bin/sh's echo eats
# as its own no-newline flag -- so the log lost the very token the assertions
# below look for, and every test here failed against correct code.
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
        # The guest half, run here: its own clock, and the only `sudo` in the run.
        guest_env = {"PATH": "%s:%s:%s" % (datebin, sudobin, os.environ["PATH"]), "FAKE_EPOCH": str(HOST_EPOCH - skew),
                     "SUDO_LOG": str(log), "SUDO_RC": str(sudo_rc)}
        err = io.StringIO()
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch.dict(os.environ, guest_env), contextlib.redirect_stderr(err):
            vm = targets.Registry(str(REPO), env=env).load("vm")
            g = guest.Guest(guest.Host(vm, FakeClock(HOST_EPOCH)), "testguest", "10.0.0.2")
            g.m = Local()
            rc = 0 if g.set_guest_clock() else 1
        return "rc=%d\n%s" % (rc, err.getvalue()), log.read_text()

    def test_a_stale_guest_is_set_from_the_host(self):
        """Eleven days behind -- the skew actually measured on a cloned guest.
        The function reports it and hands `date` a value to set."""
        out, sudolog = self._drive(11 * 86400)
        self.assertIn("rc=0", out, out)
        self.assertIn("clock was", out, out)
        self.assertIn("950400s", out, out)          # 11 days, to the second
        self.assertIn("-n date -u ", sudolog, sudolog)

    def test_a_guest_already_in_time_is_left_alone(self):
        """Idempotent by measurement: no sudo, and nothing reported."""
        out, sudolog = self._drive(0)
        self.assertIn("rc=0", out, out)
        self.assertNotIn("clock was", out, out)
        self.assertEqual("", sudolog.strip(), sudolog)

    def test_a_guest_ahead_of_the_host_is_set_too(self):
        """The comparison is absolute: a clock in the future breaks TLS at the
        other end of the validity window and is just as wrong."""
        out, sudolog = self._drive(-3600)
        self.assertIn("rc=0", out, out)
        self.assertIn("3600s", out, out)
        self.assertIn("-n date -u ", sudolog, sudolog)

    def test_a_guest_that_refuses_the_write_fails(self):
        """No passwordless sudo means the clock stays wrong, and the caller
        has to hear about it rather than start a guest that cannot do TLS."""
        out, _ = self._drive(86400, sudo_rc=1)
        self.assertIn("rc=1", out, out)

    def test_wk_vm_clock_skew_sets_the_tolerance(self):
        """The override reaches the comparison: a five-minute error is left
        alone at a ten-minute tolerance and corrected at the default 30s."""
        out, sudolog = self._drive(300, tolerance="600")
        self.assertEqual("", sudolog.strip(), out)
        out, sudolog = self._drive(300)
        self.assertIn("-n date -u ", sudolog, out)


class TestStartSetsTheClock(unittest.TestCase):
    """The wiring: both of a start's arms set the clock (a running guest is the one most likely to have been
    cloned days ago), and the base build sets it before provisioning speaks HTTPS, through the same code."""

    def test_both_start_arms_set_the_clock(self):
        assert_guest_start_converges(self, '_set_guest_clock "$name" "$ip"')

    def test_the_clock_is_set_before_the_proxy(self):
        """Order matters: everything reached through the proxy speaks TLS."""
        steps = [s[0] for s in guest.STEPS]
        self.assertEqual(steps.index("set_guest_clock") + 1, steps.index("set_guest_egress"))

    def test_the_base_builds_call_reaches_the_same_code(self):
        """targets/vm.sh's `_set_guest_clock` is lib/wk/guest.py's, over the guest's own ssh."""
        from tests.support import bash, stub_path
        ssh = 'for a in "$@"; do last="$a"; done\nPATH="$FAKE_GUEST_PATH" sh -c "$last"\n'
        with stub_path({"ssh": ssh}) as binp:
            d = binp / "guest"
            d.mkdir()
            for name, body in (("date", FAKE_DATE), ("sudo", FAKE_SUDO)):
                (d / name).write_text(body)
                (d / name).chmod(0o755)
            log = binp / "sudo.log"
            cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; . "$WK_ROOT/lib/target.sh"\n'
                      'load_target vm >/dev/null 2>&1\n_set_guest_clock wk-base 10.0.0.2',
                      env={"PATH": "%s:%s" % (binp, os.environ["PATH"]), "FAKE_GUEST_PATH": "%s:%s" % (d, os.environ["PATH"]),
                           "FAKE_EPOCH": "1000", "SUDO_LOG": str(log), "WK_VM_STORE": str(binp / "vmstore")})
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            self.assertIn("wk-base's clock was", cp.stderr)
            self.assertIn("-n date -u ", log.read_text())

    def test_the_base_build_sets_the_clock_before_provisioning(self):
        vm = _src("targets", "vm.sh")
        clock = vm.index('_set_guest_clock "$WK_VM_BASE" "$ip"')
        prov = vm.index("provisioning the base VM")
        self.assertLess(clock, prov)
        self.assertIn('_set_guest_clock() { _guest_py clock "$@"; }', vm)


if __name__ == "__main__":
    unittest.main()
