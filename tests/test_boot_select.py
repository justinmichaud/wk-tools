"""Which system an arming boots (lib/wk/boot/driver.py): `systems` enumerates the medium's candidate partitions (a
driver's system_parts), `select_system` resolves --system against that evidence, `medium_read` is the one reader of
a boot partition's files, and the arming record needs no privilege.

Run: python3 tests/run.py --unit -k test_boot_select
"""
import contextlib
import io
import sys
import unittest

from tests.support import REPO, real_confs

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.cli import load_conf  # noqa: E402
from wk.boot.pi import PiTryboot, Rpi5Usb  # noqa: E402
from wk.machine import Result  # noqa: E402

CONF = {"name": "b", "device": "/dev/sda", "root": "/dev/mmcblk0p2", "role": "workstation"}


class Channel:
    """Answers each call from `answers` (fn or the on-board file's name), recording every call."""

    def __init__(self, answers=None):
        self.answers, self.calls, self.channel = answers or {}, [], "host"

    def call(self, fn, *args, input=None, mutates=False):
        words = tuple(getattr(a, "name", a) for a in args)
        params = [getattr(a, "params", None) for a in args if hasattr(a, "params")]
        self.calls.append((fn,) + words + tuple(params))
        key = next((w for w in words if isinstance(w, str) and w.endswith(".sh")), fn)
        got = self.answers.get(key, self.answers.get(fn, Result(0)))
        return got(fn, words, params) if callable(got) else got


def refused(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            fn(*args)
        except act.Refused:
            return err.getvalue()
    raise AssertionError("%s did not refuse" % fn.__name__)


def listing(*systems):
    d = PiTryboot(REPO, dict(CONF, name="rpi4"), Channel())
    d.systems = lambda: list(systems) if systems != (None,) else None
    return d


class TestEnumeration(unittest.TestCase):
    def test_systems_reads_each_candidate_and_skips_an_empty_one(self):
        ch = Channel({"card_priv": lambda fn, w, p: Result(0, "alpha-1\n" if w[2] == "1" else "")})
        self.assertEqual(PiTryboot(REPO, CONF, ch).systems(), [("/dev/sda1", "alpha-1")])

    def test_a_slot_the_medium_does_not_have_reads_as_empty_and_says_nothing(self):
        """a card written with one system simply has no p3: an empty slot, not a card that cannot be read."""
        ch = Channel({"card_priv": Result(1), "part-absent.sh": Result(0, "no\n")})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(Rpi5Usb(REPO, CONF, ch).medium_read("/dev/sda3", "wk-image.id"), "")
        self.assertNotIn("card helper is older", err.getvalue())

    def test_a_slot_that_is_present_but_unreadable_warns_and_fails(self):
        ch = Channel({"card_priv": Result(1), "part-absent.sh": Result(0, "yes\n")})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(Rpi5Usb(REPO, CONF, ch).medium_read("/dev/sda3", "wk-image.id"))
            self.assertIsNone(PiTryboot(REPO, CONF, ch).systems())
        self.assertIn("card helper is older", err.getvalue())
        self.assertIn("./setup --stage quiesce", err.getvalue())


class TestMediumRead(unittest.TestCase):
    def test_a_bench_device_mounts_the_medium_itself(self):
        ch = Channel({"r_sudo": Result(0, "id-1\n")})
        d = PiTryboot(REPO, dict(CONF, role="bench-device"), ch)
        self.assertEqual(d.medium_read("/dev/mmcblk0p1", "wk-image.id"), "id-1\n")
        self.assertEqual(ch.calls[0][:2], ("r_sudo", "medium-read.sh"))
        self.assertEqual(ch.calls[0][2], {"WK_PART": "/dev/mmcblk0p1", "WK_FILE": "wk-image.id"})

    def test_a_workstation_goes_through_the_card_helper_by_partition_number(self):
        ch = Channel()
        d = PiTryboot(REPO, CONF, ch)
        d.medium_read("/dev/sda3", "wk-image.id")
        d.medium_read("/dev/mmcblk0p12", "wk-diag.txt")
        self.assertEqual(ch.calls, [("card_priv", "boot-read", "/dev/sda", "3", "wk-image.id"),
                                    ("card_priv", "boot-read", "/dev/mmcblk0", "12", "wk-diag.txt")])

class TestSelection(unittest.TestCase):
    def test_sole_system_is_the_default(self):
        self.assertEqual(listing(("/dev/sda1", "alpha-1")).select_system(""), ("/dev/sda1", "alpha-1"))

    def test_two_systems_refuse_to_guess(self):
        err = refused(listing(("/dev/sda1", "alpha-1"), ("/dev/sda3", "beta-2")).select_system, "")
        for want in ("holds 2 systems", "alpha-1", "beta-2", "--system"):
            self.assertIn(want, err)

    def test_named_system_is_matched_against_the_medium(self):
        d = listing(("/dev/sda1", "alpha-1"), ("/dev/sda3", "beta-2"))
        self.assertEqual(d.select_system("beta-2"), ("/dev/sda3", "beta-2"))

    def test_a_name_the_medium_does_not_hold_is_refused_with_the_list(self):
        err = refused(listing(("/dev/sda1", "alpha-1")).select_system, "gamma-3")
        for want in ("alpha-1", "gamma-3", "@second"):
            self.assertIn(want, err)

    def test_an_empty_medium_names_the_write_remedy(self):
        err = refused(listing().select_system, "")
        self.assertIn("holds no wk system yet", err)
        self.assertIn("wk sysimage write", err)

    def test_an_unreadable_medium_is_not_an_empty_one(self):
        self.assertIn("could not read", refused(listing(None).select_system, ""))


class TestDiag(unittest.TestCase):
    def test_diag_reads_every_system_and_says_which_is_which(self):
        """after a failed boot of the second system, the first one's dump is the stale one."""
        d = listing(("/dev/sda1", "alpha-1"), ("/dev/sda3", "beta-2"))
        d.medium_read = lambda p, name: "(dump)" if p == "/dev/sda1" else ""
        out = d.diag()
        self.assertIn("== alpha-1 (/dev/sda1) ==\n(dump)", out)
        self.assertIn("== beta-2 (/dev/sda3) ==\n(no wk-diag.txt", out)


class TestEveryMachineConfLoads(unittest.TestCase):
    """A conf `load_conf` cannot load is a machine that silently leaves the fleet."""

    CONFS = real_confs("board", "mac", "guest")

    def test_every_conf_loads(self):
        self.assertTrue(self.CONFS, "no machine confs found")
        for conf in self.CONFS:
            with self.subTest(machine=conf.stem):
                got = load_conf(REPO, conf.stem, {"WK_MACHINES_DIR": str(REPO / "machines")})
                self.assertEqual((got or {}).get("name"), conf.stem, f"load_conf {conf.stem} failed")


if __name__ == "__main__":
    unittest.main()
