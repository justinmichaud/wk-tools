"""The guest's clock, set from the host on every start (targets/vm.sh).

A macOS guest cannot keep its own time. `tart clone` hands a clone the golden
base's clock, and Softnet allows one address -- the proxy -- while NTP is UDP,
which an HTTP CONNECT proxy cannot carry. Left alone the clock stays at the
date the base was sealed, and every TLS handshake to a certificate issued
after it fails as CERT_NOT_YET_VALID: `wk ai claude` in the guest reports
`Failed to connect to platform.claude.com`, which reads like an egress refusal
and is not one.

Hermetic: `_ssh` is replaced with a stub that runs the guest half of the
function locally. Both clocks the function compares are a fake `date` the test
pins, and `sudo` is a fake that records what would have been set. No tart, no
VM, no network.

Run: python3 -m unittest tests.test_vm_clock -v
"""
import unittest

from tests.support import REPO, WkTest, assert_guest_start_converges


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
    def _drive(self, skew, sudo_rc=0, skew_var=""):
        """Call _set_guest_clock against a guest whose clock is `skew` seconds
        behind the host's (negative for ahead). Returns (CompletedProcess,
        sudo log text)."""
        datebin = self.tmp / "date-bin"
        sudobin = self.tmp / "sudo-bin"
        for d, name, body in ((datebin, "date", FAKE_DATE),
                              (sudobin, "sudo", FAKE_SUDO)):
            d.mkdir(exist_ok=True)
            (d / name).write_text(body)
            (d / name).chmod(0o755)
        log = self.tmp / "sudo.log"
        log.write_text("")

        cp = self.bash(
            f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/targets/vm.sh"

export PATH="{datebin}:$PATH"
export FAKE_EPOCH={HOST_EPOCH}
export SUDO_LOG={log}
export SUDO_RC={sudo_rc}

# The guest half, run here: its own clock, and the only `sudo` in the run, so a
# regression that set the clock on the host side would leave the log empty. A
# subshell, so neither reaches the host half.
_ssh() {{ shift; ( PATH="{sudobin}:$PATH"; export FAKE_EPOCH={HOST_EPOCH - skew}
                   eval "$*" ); }}

# `|| rc=$?` rather than `; echo $?`: lib/common.sh turns errexit on, and a
# refusal would otherwise end the script before it could be reported.
rc=0
{skew_var}_set_guest_clock testguest 10.0.0.2 || rc=$?
echo "rc=$rc"
'''
        )
        return cp, log.read_text()

    def test_a_stale_guest_is_set_from_the_host(self):
        """Eleven days behind -- the skew actually measured on a cloned guest.
        The function reports it and hands `date` a value to set."""
        cp, sudolog = self._drive(11 * 86400)
        out = cp.stdout + cp.stderr
        self.assertIn("rc=0", out, out)
        self.assertIn("clock was", out, out)
        self.assertIn("950400s", out, out)          # 11 days, to the second
        self.assertIn("-n date -u ", sudolog, sudolog)

    def test_a_guest_already_in_time_is_left_alone(self):
        """Idempotent by measurement: no sudo, and nothing reported."""
        cp, sudolog = self._drive(0)
        out = cp.stdout + cp.stderr
        self.assertIn("rc=0", out, out)
        self.assertNotIn("clock was", out, out)
        self.assertEqual("", sudolog.strip(), sudolog)

    def test_a_guest_ahead_of_the_host_is_set_too(self):
        """The comparison is absolute: a clock in the future breaks TLS at the
        other end of the validity window and is just as wrong."""
        cp, sudolog = self._drive(-3600)
        out = cp.stdout + cp.stderr
        self.assertIn("rc=0", out, out)
        self.assertIn("3600s", out, out)
        self.assertIn("-n date -u ", sudolog, sudolog)

    def test_a_guest_that_refuses_the_write_fails(self):
        """No passwordless sudo means the clock stays wrong, and the caller
        has to hear about it rather than start a guest that cannot do TLS."""
        cp, _ = self._drive(86400, sudo_rc=1)
        out = cp.stdout + cp.stderr
        self.assertIn("rc=1", out, out)

    def test_wk_vm_clock_skew_sets_the_tolerance(self):
        """The override reaches the comparison: a five-minute error is left
        alone at a ten-minute tolerance and corrected at the default 30s."""
        cp, sudolog = self._drive(300, skew_var="WK_VM_CLOCK_SKEW=600 ")
        self.assertEqual("", sudolog.strip(), cp.stdout + cp.stderr)
        cp, sudolog = self._drive(300)
        self.assertIn("-n date -u ", sudolog, cp.stdout + cp.stderr)


class TestStartSetsTheClock(unittest.TestCase):
    """Source-level: the wiring, which no hermetic call can reach -- t_start
    needs a real guest. Both of t_start's branches set the clock (a running
    guest is the one most likely to have been cloned days ago), and the base
    build sets it before provisioning speaks HTTPS."""

    def test_both_t_start_branches_set_the_clock(self):
        """One `_converge_guest`, called from both t_start arms."""
        assert_guest_start_converges(self, '_set_guest_clock "$name" "$ip"')

    def test_the_clock_is_set_before_the_proxy(self):
        """Order matters: everything reached through the proxy speaks TLS."""
        vm = _src("targets", "vm.sh").splitlines()
        for i, line in enumerate(vm):
            if '_set_guest_egress "$name" "$ip"' in line:
                self.assertIn("_set_guest_clock", vm[i - 1], f"line {i + 1}")

    def test_the_base_build_sets_the_clock_before_provisioning(self):
        vm = _src("targets", "vm.sh")
        clock = vm.index('_set_guest_clock "$WK_VM_BASE" "$ip"')
        prov = vm.index("provisioning the base VM")
        self.assertLess(clock, prov)


if __name__ == "__main__":
    unittest.main()
