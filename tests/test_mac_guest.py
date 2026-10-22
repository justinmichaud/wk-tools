"""mac-guest (lib/wk/boot/mac.py): a Tart guest standing in for a Mac in bench mode, against FakeGuest
(tests/fake_boot.py), the vm place in memory. It conforms like the real driver, and a reading from it is refused as a
measurement."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO
from tests.fake_boot import board

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402


def quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args), err.getvalue()
        except act.Refused:
            return act.Refused, err.getvalue()


def guest(env=None, **state):
    fake, d = board("mac-guest", env=env)
    for k, v in state.items():
        setattr(fake, k, v)
    return fake, d


class TestTheRehearsalIsNotAMeasurement(unittest.TestCase):
    def test_it_says_it_measures_nothing(self):
        _, d = guest()
        self.assertFalse(d.measures)
        self.assertIn("measurement=refused", d.evidence())


class TestArming(unittest.TestCase):
    """Arming a guest is starting it, and what makes it a benchmark install is its marker."""

    def test_a_stopped_guest_is_started_and_an_absent_or_unmarked_one_is_refused(self):
        fake, d = guest()
        self.assertIsNone(quiet(d.arm)[0])
        self.assertEqual(fake.effects, [("start",)])
        for state, says in (({"st": "absent"}, "wk new wk-bench --on vm"),
                            ({"st": "running", "marked": False}, "carries no /etc/wk-image")):
            with self.subTest(says):
                fake, d = guest(**state)
                got, err = quiet(d.arm)
                self.assertIs(got, act.Refused)
                self.assertIn(says, err)
                self.assertEqual(fake.effects, [])

    def test_leaving_the_role_is_stopping_the_guest(self):
        fake, d = guest(st="running")
        self.assertEqual(d.probe(), "bench sys-a")
        quiet(d.reboot)
        self.assertEqual(fake.effects, [("stop",)])
        self.assertEqual(d.probe(), "unreachable")

    def test_a_dry_run_starts_nothing(self):
        fake, d = guest()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            quiet(d.arm)
            quiet(d.own, "/var/wk")
        self.assertEqual(fake.effects, [])


class TestWhatItReports(unittest.TestCase):
    def test_the_declared_display_is_the_mode_the_guest_is_built_with(self):
        self.assertEqual(guest()[1].display(), "external 1280x800")
        self.assertEqual(guest(env={"WK_VM_DISPLAY": "1920x1200"})[1].display(), "external 1920x1200")

    def test_the_guest_is_named_by_wk_bench_guest(self):
        self.assertEqual(guest(env={"WK_BENCH_GUEST": "my-custom-guest"})[1].guest(), "my-custom-guest")

    def test_without_tart_it_is_not_probeable_and_says_where_it_is_managed(self):
        _, d = guest(has_tart=False)
        with mock.patch("wk.boot.mac.is_macos", return_value=True):
            self.assertFalse(d.probeable())
            self.assertIn("managed on the macOS host", d.media())

    def test_its_manager_is_this_machine(self):
        _, d = guest()
        self.assertIs(d.manager(), d.ch.via)
        self.assertEqual(d.manager_tools(d.manager()), str(REPO))
        self.assertTrue(d.restart_ready())


class TestStaging(unittest.TestCase):
    def test_the_staging_root_is_owned_before_anything_lands(self):
        fake, d = guest(st="running")
        d.probe()
        self.assertEqual(d.bench_put_file("/dev/null", "/var/wk/stage.json"), 0)
        ran = [e for e in fake.effects if e[0] != "push"]
        self.assertEqual(ran[:2], [("mkdir", "-p", "/var/wk"), ("chown", "-R", "admin", "/var/wk")])
        self.assertEqual(ran[2][:2], ("mv", "-f"))
        self.assertEqual((d.bench_root(), d.bench_home(), d.bench_local()), ("/var/wk", "/Users/admin", False))


if __name__ == "__main__":
    unittest.main()
