"""mac-guest (lib/wk/boot/mac.py): a Tart guest standing in for a Mac in bench mode, against FakeGuest
(tests/test_mac_volume.py) and GuestChannel over a vm target in memory. It conforms like the real driver, and a
reading from it is refused as a measurement.

Run: python3 tests/run.py --unit -k test_mac_guest
"""
import os
import sys
import unittest

from tests.support import REPO, bash
from tests.test_mac_volume import mac_board, quiet

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.mac import GuestChannel, Script  # noqa: E402
from wk.machine import Result  # noqa: E402


def guest(**state):
    fake, d = mac_board("mac-guest")
    for k, v in state.items():
        setattr(fake, k, v)
    return fake, d


class TestTheRehearsalIsNotAMeasurement(unittest.TestCase):
    def test_a_reading_from_it_is_refused(self):
        _, d = guest()
        got, err = quiet(d.check_measurement)
        self.assertIs(got, act.Refused)
        self.assertIn("benchvm is a rehearsal", err)
        self.assertEqual(d.facts()["B_MEASURES"], "no")
        self.assertIn("measurement=refused", d.evidence())


class TestArming(unittest.TestCase):
    """Arming a guest is starting it, and what makes it a benchmark install is its marker."""

    def test_no_guest_names_the_commands_that_make_one(self):
        fake, d = guest(st="absent")
        got, err = quiet(d.arm)
        self.assertIs(got, act.Refused)
        self.assertIn("wk vm new wk-bench", err)
        self.assertEqual(fake.effects, [])

    def test_a_stopped_guest_is_started(self):
        fake, d = guest()
        self.assertEqual(quiet(d.arm)[0], 0)
        self.assertEqual(fake.effects, [("start",)])

    def test_a_running_guest_with_no_marker_is_a_workstation_guest(self):
        fake, d = guest(st="running", marked=False)
        got, err = quiet(d.arm)
        self.assertIs(got, act.Refused)
        self.assertIn("carries no /etc/wk-image", err)
        self.assertEqual(fake.effects, [])

    def test_leaving_the_role_is_stopping_the_guest(self):
        fake, d = guest(st="running")
        self.assertEqual(d.probe(), "bench sys-a")
        quiet(d.reboot)
        self.assertEqual(fake.effects, [("stop",)])
        self.assertEqual(d.probe(), "unreachable")


class TestWhatItReports(unittest.TestCase):
    def test_the_declared_display_is_the_mode_the_guest_is_built_with(self):
        self.assertEqual(guest()[1].display(), "external 1280x800")
        fake, d = mac_board("mac-guest", env={"WK_VM_DISPLAY": "1920x1200"})
        self.assertEqual(d.display(), "external 1920x1200")

    def test_the_guest_is_named_by_wk_bench_guest(self):
        _, d = mac_board("mac-guest", env={"WK_BENCH_GUEST": "my-custom-guest"})
        self.assertEqual(d.facts()["NODE_GUEST"], "my-custom-guest")

    def test_without_tart_it_is_not_probeable_and_says_where_it_is_managed(self):
        _, d = guest(tart=False)
        self.assertFalse(d.probeable())
        self.assertIn("managed on the macOS host", d.media())

    def test_its_manager_is_this_machine(self):
        _, d = guest()
        self.assertEqual(d.manage_argv("true"), ["bash", "-c", "true"])
        self.assertEqual(d.manage_name(), "this machine")
        self.assertTrue(d.restart_ready())


class TestStaging(unittest.TestCase):
    def test_the_staging_root_is_owned_before_anything_lands(self):
        fake, d = guest(st="running")
        d.probe()
        self.assertEqual(d.bench_put_file("/dev/null", "/var/wk/stage.json"), 0)
        self.assertEqual([e for e in fake.effects if e[0] == "m_ssh"],
                         [("m_ssh", "mac-own.sh"), ("m_ssh", "mac-put.sh")])
        self.assertEqual((d.bench_root(), d.bench_home(), d.bench_local()), ("/var/wk", "/Users/admin", False))


class FakeVm:
    """The vm target's surface GuestChannel uses."""

    env = {}

    def __init__(self, state="running"):
        self.state, self.ran = state, []

    def vm_state(self, ws):
        return self.state

    def exec(self, ws, argv):
        self.ran.append((ws, argv))
        return Result(0, "id=perf-macos-benchvm\n")

    def exec_argv(self, ws, argv):
        return ["ssh", "admin@192.0.2.9", "bash -lc " + " ".join(argv)], None


class TestGuestChannel(unittest.TestCase):
    def test_a_script_runs_in_the_guest_through_the_vm_target(self):
        vm = FakeVm()
        ch = GuestChannel(REPO, {}, {"WK_BENCH_GUEST": "g"}, vm=vm)
        self.assertTrue(ch.call("r_ssh", Script(REPO, "mac-probe.sh")).ok)
        self.assertEqual(vm.ran[0][0], "g")
        self.assertEqual(vm.ran[0][1][:2], ["sh", "-c"])
        self.assertEqual(ch.call("i_ssh", Script(REPO, "mac-probe.sh")).rc, 255)
        self.assertEqual(ch.exec_argv("true")[0], "ssh")

    def test_a_dry_run_runs_nothing_that_mutates(self):
        vm = FakeVm()
        ch = GuestChannel(REPO, {}, {}, vm=vm)
        own = Script(REPO, "mac-own.sh", WK_DEST="/var/wk", WK_OWN="/var/wk")
        os.environ["WK_DRY_RUN"] = "1"
        try:
            quiet(lambda: ch.call("m_ssh", own, mutates=True))
            quiet(ch.start)
        finally:
            del os.environ["WK_DRY_RUN"]
        self.assertEqual(vm.ran, [])


class TestTheShim(unittest.TestCase):
    def test_sourced_alone_it_still_names_the_guest(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"\n. "$WK_ROOT/boot/mac-guest.sh"\necho "$NODE_GUEST|$BOOT_ARMING|$(b_bench_root)"',
                  env={"WK_BENCH_GUEST": "my-custom-guest"})
        self.assertEqual(cp.stdout.strip(), "my-custom-guest|guest|/var/wk", cp.stderr)


if __name__ == "__main__":
    unittest.main()
