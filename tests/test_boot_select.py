"""Which system an arming boots (lib/wk/boot/driver.py): `systems` enumerates the medium's candidate partitions (a
driver's system_parts), `select_system` resolves --system against that evidence, `medium_read` is the one reader of
a boot partition's files, and the arming record needs no privilege.

Run: python3 tests/run.py --unit -k test_boot_select
"""
import contextlib
import io
import re
import sys
import unittest

from tests.support import REPO, bash, real_confs

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.driver import RECORD, Driver  # noqa: E402
from wk.boot.pi import DRIVERS, PiTryboot, Rpi5Usb  # noqa: E402
from wk.machine import Result  # noqa: E402

ONBOARD = REPO / "boot" / "onboard"
CONF = {"NODE_NAME": "b", "NODE_DEVICE": "/dev/sda", "NODE_ROOT": "/dev/mmcblk0p2", "NODE_ROLE": "workstation"}


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
    d = PiTryboot(REPO, dict(CONF, NODE_NAME="rpi4"), Channel())
    d.systems = lambda: list(systems) if systems != (None,) else None
    return d


class TestEnumeration(unittest.TestCase):
    def test_every_driver_states_its_candidates(self):
        self.assertEqual(Driver.system_parts, (1,))
        want = {"pi-tryboot": (1, 3), "pi-sd": (3, 5, 7), "pi-mbr": (1,), "rpi5-usb": (1, 3)}
        self.assertEqual({k: v.system_parts for k, v in DRIVERS.items()}, want)

    def test_systems_reads_each_candidate_and_skips_an_empty_one(self):
        ch = Channel({"card_priv": lambda fn, w, p: Result(0, "alpha-1\n" if w[2] == "1" else "")})
        self.assertEqual(PiTryboot(REPO, CONF, ch).systems(), [("/dev/sda1", "alpha-1")])

    def test_an_unreadable_medium_is_not_an_empty_one(self):
        ch = Channel({"card_priv": Result(1), "part-absent.sh": Result(0, "yes\n")})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(PiTryboot(REPO, CONF, ch).systems())

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
        self.assertIn("card helper is older", err.getvalue())
        self.assertIn("./setup --stage quiesce", err.getvalue())


class TestMediumRead(unittest.TestCase):
    def test_a_bench_device_mounts_the_medium_itself(self):
        """its medium is often the disk it runs from, which the card helper refuses by design."""
        ch = Channel({"r_sudo": Result(0, "id-1\n")})
        d = PiTryboot(REPO, dict(CONF, NODE_ROLE="bench-device"), ch)
        self.assertEqual(d.medium_read("/dev/mmcblk0p1", "wk-image.id"), "id-1\n")
        self.assertEqual(ch.calls[0][:2], ("r_sudo", "medium-read.sh"))
        self.assertEqual(ch.calls[0][2], {"WK_PART": "/dev/mmcblk0p1", "WK_NAME": "wk-image.id"})

    def test_a_workstation_goes_through_the_card_helper_by_partition_number(self):
        ch = Channel()
        d = PiTryboot(REPO, CONF, ch)
        d.medium_read("/dev/sda3", "wk-image.id")
        d.medium_read("/dev/mmcblk0p12", "wk-diag.txt")
        self.assertEqual(ch.calls, [("card_priv", "boot-read", "/dev/sda", "3", "wk-image.id"),
                                    ("card_priv", "boot-read", "/dev/mmcblk0", "12", "wk-diag.txt")])

    def test_the_medium_is_read_over_the_channel_that_answered(self):
        """no reader of the medium names the rescue's own channel (m_ssh)."""
        text = (REPO / "lib" / "wk" / "boot" / "pi.py").read_text()
        self.assertNotIn("m_ssh", text)


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


class TestRpi5SelectsBetweenTwoSystems(unittest.TestCase):
    """The stick's pair is autoboot.txt's `boot_partition=`, written and read back before each arm, and the reboot
    is a plain one: this board's tryboot flag belongs to flash-kernel's staging on its NVMe."""

    def arm(self, part, written):
        ch = Channel({"card_priv": lambda fn, w, p: Result(0, "[all]\nboot_partition=%s\n" % written if w[0] == "boot-read" else ""),
                      "boot_priv": Result(0, "0x0 0x80000000\n")})
        d = Rpi5Usb(REPO, CONF, ch)
        return d, ch

    def test_each_pair_is_selected_explicitly(self):
        for p, pair in (("/dev/sda3", "3"), ("/dev/sda1", "1"), ("", "1")):
            with self.subTest(part=p):
                d, ch = self.arm(p, pair)
                d.arm(p, "0xf64")
                self.assertIn(("card_priv", "autoboot", "/dev/sda", pair), ch.calls)
                self.assertIn(("boot_priv", "order", "0xf64"), ch.calls)
                d.reboot(armed=True)
                self.assertEqual(ch.calls[-1], ("boot_priv", "reboot"))

    def test_the_helper_is_asked_for_before_the_firmware_call(self):
        """otherwise a missing helper reads as a firmware that would not answer."""
        d, ch = self.arm("/dev/sda1", "1")
        ch.answers["boot_priv_require"] = Result(1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertRaises(act.Refused, d.arm, "/dev/sda1", "0xf64")
        self.assertEqual([c[0] for c in ch.calls], ["boot_priv_require"])

    def test_a_selector_that_did_not_take_is_refused(self):
        d, _ = self.arm("/dev/sda3", "1")
        err = refused(d.arm, "/dev/sda3", "0xf64")
        self.assertIn("does not select pair 3", err)
        self.assertIn("./setup --stage quiesce", err)

    def test_a_partition_this_stick_does_not_select_is_refused(self):
        d, _ = self.arm("/dev/sda5", "5")
        self.assertIn("partition 1 or 3", refused(d.arm, "/dev/sda5", "0xf64"))

    def test_the_selector_is_only_written_where_the_firmware_uses_it(self):
        """an autoboot.txt on the rpi4's stick would make its tryboot flag boot the stick's second pair."""
        self.assertEqual([k for k, v in DRIVERS.items() if v.selects_by_partition], ["rpi5-usb"])


class TestTheWatchdogIsTheSystemsFactNotTheDriversFact(unittest.TestCase):
    """`--keep` asks the running system whether it carries the self-return watchdog, not whether its driver has a
    self-disarm: rpi5-usb has none, and its cards carry the watchdog all the same."""

    def test_keep_asks_the_machine_and_not_the_driver(self):
        body = (REPO / "cmd" / "boot").read_text()
        fn = body[body.index("cmd_keep()"):]
        fn = fn[:fn.index("\ncmd_back()")]
        self.assertIn("b_watchdog_present", fn)
        self.assertNotIn("command -v b_self_disarm_sh", fn)



class TestEveryMachineConfLoads(unittest.TestCase):
    """A conf `machine_load` cannot load is a machine that silently leaves the fleet."""

    CONFS = real_confs("board", "mac", "guest")

    def test_every_conf_loads(self):
        self.assertTrue(self.CONFS, "no machine confs found")
        for conf in self.CONFS:
            with self.subTest(machine=conf.stem):
                cp = bash(f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n. "{REPO}/boot/machines.sh"\nmachine_load {conf.stem}\n',
                          env={"WK_MACHINES_DIR": str(REPO / "machines")})
                self.assertEqual(cp.returncode, 0, f"machine_load {conf.stem} failed: {cp.stdout}{cp.stderr}")

    def test_no_conf_invents_a_field_prefix(self):
        """the loader defaults every field it knows, so a misspelled one reads as never set."""
        for conf in self.CONFS:
            with self.subTest(machine=conf.stem):
                stray = [l for l in conf.read_text().splitlines()
                         if re.match(r"[A-Z][A-Z0-9_]*=", l) and not l.startswith(("NODE_", "KIND="))]
                self.assertEqual(stray, [], f"{conf.name} assigns fields outside NODE_")


class TestTheArmingRecordNeedsNoPrivilege(unittest.TestCase):
    """The record is written over BatchMode ssh with no terminal, so a sudo in it cannot be answered on a
    workstation; `./setup` grants the directory once, where a prompt can be."""

    def test_no_record_script_calls_sudo(self):
        for name in ("record-write.sh", "record-read.sh", "record-clear.sh"):
            with self.subTest(file=name):
                self.assertNotIn("sudo", (ONBOARD / name).read_text())
        text = (REPO / "lib" / "wk" / "boot" / "driver.py").read_text()
        body = text[text.index("    def record("):text.index("    def armed_barrier(")]
        self.assertNotIn("sudo", body)

    def test_the_record_is_not_directly_under_the_root_owned_dir(self):
        """/var/lib/wk stays root-owned: the card helper keeps the board's tailnet node key there."""
        self.assertTrue(RECORD.startswith("/var/lib/wk/"), RECORD)
        self.assertNotEqual("/var/lib/wk", RECORD.rsplit("/", 1)[0])

    def test_setup_owns_that_directory_and_not_its_parent(self):
        text = (REPO / "admin" / "install.sh").read_text()
        self.assertIn("/var/lib/wk/boot", text)
        for line in text.splitlines():
            if "install -d" in line and "/var/lib/wk" in line:
                with self.subTest(line=line.strip()):
                    self.assertIn("/var/lib/wk/boot", line)
        block = text[text.index("_bootdir=/var/lib/wk/boot"):]
        self.assertIn("./setup --stage quiesce", block[:block.index("unset _bootdir")])


if __name__ == "__main__":
    unittest.main()
