"""The boot-driver core (lib/wk/boot): every Pi driver against a FakeBoard holding its media, a firmware one-shot
and a clock -- machine.conformance[<kind>] over one test body, boot.arming_exact and the on-board files."""
import contextlib
import io
import re
import shlex
import subprocess
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))

from wk import act, fleet  # noqa: E402
from wk.boot import driver_class  # noqa: E402
from wk.boot.driver import Channel, Driver, Onboard, disk_of, part, partno  # noqa: E402
from tests.fake_boot import FakeBoard  # noqa: E402
from wk.boot import drivers, open_driver  # noqa: E402
from wk.machine import Fake  # noqa: E402
from tests.test_mac_volume import FAKES, MacConformance, mac_board, conf_for as mac_conf  # noqa: E402

DRIVERS = drivers()

ONBOARD = REPO / "boot" / "onboard"
BOARDS = {"pi-sd": "rpi3", "pi-tryboot": "rpi4", "rpi5-usb": "rpi5"}
SLOTS_OF = {"pi-sd": (5, 7), "pi-tryboot": (1, 3), "rpi5-usb": (1, 3)}


def conf_for(kind):
    conf = fleet.Fleet(REPO, {"HOME": "/nonexistent"}).load(BOARDS[kind])
    conf = {k: v for k, v in conf.items() if k != "kind"}
    conf.update(name=BOARDS[kind], driver=kind)
    return conf


def board(kind, ids=("sys-a",)):
    """A FakeBoard up on its rescue, one system per id written at the driver's slots, and the driver over it."""
    if kind in FAKES:
        return mac_board(kind, ids)
    conf = conf_for(kind)
    fake = FakeBoard(conf)
    cls = DRIVERS[kind]
    fake.rescue("rescue-1" if conf["role"] == "bench-device" else "")
    disk = conf["device"]
    for n, ident in zip(SLOTS_OF[kind], ids):
        fake.write_system(part(disk, n), ident, failsafe=cls.failsafe)
    return fake, cls(REPO, conf, fake)


def quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args), err.getvalue()
        except act.Refused:
            return act.Refused, err.getvalue()


def arm_and_boot(d, fake, want=""):
    d.probe()
    p, ident = d.select_system(want)
    d.record_write(ident, "prof", fake.conf["device"], d.order_image)
    d.arm(p, d.order_image)
    d.reboot(armed=True)
    return p, ident


class Conformance:
    """machine.conformance[<kind>]: one body, run for every Pi driver."""

    kind = None

    def test_the_driver_implements_the_whole_interface(self):
        cls = DRIVERS[self.kind]
        for verb in ("arm", "evidence", "media", "reprovision"):
            with self.subTest(verb=verb):
                self.assertIsNot(getattr(cls, verb), getattr(Driver, verb), "%s forgot %s" % (self.kind, verb))
        self.assertIs(driver_class(self.kind), cls)

    def test_the_production_transport_is_machines_and_no_bash(self):
        """The driver as `wk boot` builds it reaches its machine through Machine.run alone -- ssh, or the vm place
        for a guest -- and never through a shell library."""
        via = Fake()
        conf = conf_for(self.kind) if self.kind not in FAKES else mac_conf(self.kind)
        d = open_driver(REPO, conf, env={"HOME": "/nonexistent", "WK_MACHINES_DIR": str(REPO / "machines")}, via=via)
        for verb in ("probe", "media", "evidence", "reprovision", "record_read", "booted_at"):
            quiet(getattr(d, verb))
        ran = [e[1] for e in via.effects if e[0] in ("run", "run_tty")]
        if self.kind == "mac-guest":
            self.assertIs(d.ch.vm().machine, via, "a guest is reached through the vm place, over the same machine")
        else:
            self.assertTrue(ran, "the driver reached nothing")
        for argv in ran:
            self.assertNotIn(argv[0], ("bash", "env"), argv)
            self.assertFalse([w for w in argv if "machines.sh" in w or "boot_bridge" in w], argv)

    def test_the_probe_names_the_channel_that_answered(self):
        fake, d = board(self.kind)
        self.assertEqual(d.probe(), "host" if not fake.roots[fake.running]["id"] else "base rescue-1")
        self.assertEqual(fake.channel, "host")
        fake.up = False
        self.assertEqual(d.probe(), "unreachable")
        self.assertEqual(fake.channel, "none")

    def test_an_arming_boots_the_selected_system_once(self):
        fake, d = board(self.kind)
        p, ident = arm_and_boot(d, fake)
        self.assertEqual(d.probe(), "bench sys-a")
        self.assertEqual(fake.channel, "bench")
        self.assertEqual(d.boot_id(), "boot-2")
        d.reboot()
        self.assertTrue(fake.on_rescue(), "the second boot did not return to the rescue")

    def test_a_disarm_before_the_reboot_boots_the_rescue(self):
        fake, d = board(self.kind)
        d.probe()
        p, _ = d.select_system("")
        d.arm(p, d.order_image)
        if d.arming == "one-shot":
            d.arm("", d.order_normal)
        else:
            d.disarm()
        d.reboot()
        self.assertTrue(fake.on_rescue())

    def test_an_arming_that_did_not_take_is_refused(self):
        fake, d = board(self.kind)
        d.probe()
        p, _ = d.select_system("")
        fake.stuck = True
        got, err = quiet(d.arm, p, d.order_image)
        self.assertIs(got, act.Refused, err)
        self.assertIn(fake.conf["name"], err)

    def test_the_record_is_written_on_the_host_and_spent_by_the_boot(self):
        fake, d = board(self.kind)
        arm_and_boot(d, fake)
        self.assertIn("image=sys-a", fake.record)
        self.assertIn("armed_boot_id=boot-1", fake.record)
        self.assertIn("armed_at=", fake.record)
        d.probe()
        d.reboot()
        d.probe()
        _, err = quiet(d.armed_barrier, "writing now")
        self.assertNotIn("armed for system", err, "a spent arming still bars")

    def test_every_report_answers_in_every_mode(self):
        fake, d = board(self.kind)
        for mode in ("host", "bench", "unreachable"):
            with self.subTest(mode=mode):
                fake.up = mode != "unreachable"
                if mode == "bench":
                    arm_and_boot(d, fake)
                d.probe()
                for verb in ("media", "evidence", "reprovision"):
                    got, err = quiet(getattr(d, verb))
                    self.assertIsInstance(got, str, "%s %s: %s" % (verb, mode, err))
                self.assertTrue(d.reprovision().startswith("wk sysimage build " + fake.conf["profile"]))

    def test_the_failsafe_is_on_board_shell_outside_the_arming(self):
        """boot.arming_exact: a failsafe lives outside the script it guards."""
        cls = DRIVERS[self.kind]
        if self.kind in FAKES:
            # A volume's return is its bench task's hand-back, a guest's is being stopped.
            self.assertIsNone(cls.failsafe)
            self.assertIn(cls.arming, ("command", "guest"))
            self.assertIsNone(board(self.kind)[1].self_disarm_sh())
            return
        if not cls.failsafe:
            self.assertEqual(cls.arming, "one-shot", "no failsafe file, and the firmware does not revert either")
            return
        text = (ONBOARD / cls.failsafe).read_text()
        for other in ONBOARD.iterdir():
            if other.name != cls.failsafe:
                self.assertNotIn(text.strip(), other.read_text(), "%s carries the failsafe" % other.name)
        _, d = board(self.kind)
        line = d.self_disarm_sh()
        self.assertEqual(len(line.splitlines()), 1, "an ExecStart takes one line")
        for bad in ("'", "%"):
            self.assertNotIn(bad, line, "systemd splits on a single quote and expands a %")
        self.assertEqual(subprocess.run(["sh", "-n"], input=line, text=True, capture_output=True).returncode, 0)


class TestConformancePiSd(Conformance, unittest.TestCase):
    kind = "pi-sd"


class TestConformancePiTryboot(Conformance, unittest.TestCase):
    kind = "pi-tryboot"


class TestConformanceRpi5Usb(Conformance, unittest.TestCase):
    kind = "rpi5-usb"


class TestDiskOfPart(unittest.TestCase):
    def test_partition_to_disk_for_every_transport(self):
        for p, disk in (("/dev/sda2", "/dev/sda"), ("/dev/mmcblk0p2", "/dev/mmcblk0"),
                        ("/dev/nvme0n1p2", "/dev/nvme0n1"), ("/dev/sdb1", "/dev/sdb")):
            with self.subTest(part=p):
                self.assertEqual(disk_of(p), disk)
                self.assertEqual(part(disk, partno(p)), p)


class TestConformanceMacVolume(MacConformance, Conformance, unittest.TestCase):
    kind = "mac-volume"


class TestConformanceMacGuest(MacConformance, Conformance, unittest.TestCase):
    kind = "mac-guest"


class TestArmingExact(unittest.TestCase):
    """boot.arming_exact: two systems with one image id told apart by slot, the leg verified after the last arm."""

    KINDS = tuple(SLOTS_OF)

    def test_one_id_in_two_slots_is_named_by_slot(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                fake, d = board(kind, ("same", "same"))
                d.probe()
                got, err = quiet(d.select_system, "same")
                self.assertIs(got, act.Refused)
                self.assertIn("same@second", err)
                self.assertIn("@<slot>", err)
                second = part(fake.conf["device"], SLOTS_OF[kind][1])
                slot = d.slot(second)
                self.assertEqual(d.select_system("same@" + slot), (second, "same"))
                self.assertEqual(d.select_system("@" + slot), (second, "same"))
                arm_and_boot(d, fake, "same@" + slot)
                self.assertEqual(fake.running, part(fake.conf["device"], SLOTS_OF[kind][1] + 1))

    def test_the_leg_is_verified_after_the_last_arm(self):
        """a first arming that took and a second that did not: the readback is of the second."""
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                fake, d = board(kind, ("a", "b"))
                d.probe()
                d.arm(d.select_system("a")[0], d.order_image)
                fake.stuck = True
                got, err = quiet(d.arm, d.select_system("b")[0], d.order_image)
                self.assertIs(got, act.Refused, "%s: an arming that left the first system armed passed" % kind)


class TestChannel(unittest.TestCase):
    """lib/wk/boot/driver.py's Channel: which Machine each call lands on, and as whom."""

    PEERS = '{"Peer": {"a": {"DNSName": "rpi5-bench.ts.net.", "TailscaleIPs": ["100.64.0.5"], "Online": true},' \
            ' "b": {"DNSName": "rpi5.ts.net.", "TailscaleIPs": ["100.64.0.4"], "Online": %s}}}'

    def channel(self, role="workstation", channel="host", online="true", env=None, host="elsewhere"):
        via = Fake()
        via.answer(("tailscale",), out=self.PEERS % online)
        via.answer(("hostname", "-s"), out=host + "\n")
        via.answer(("ssh",), out="ok\n")
        conf = {"name": "rpi5", "ssh": "rpi5", "bench_ssh": "rpi5-bench", "role": role}
        return Channel(REPO, conf, channel, env=env or {}, via=via), via

    def sent(self, via):
        return [e[1] for e in via.effects if e[1][0] in ("ssh", "sh")]

    def test_the_bench_system_is_root_at_its_tailnet_address_unpinned(self):
        ch, via = self.channel(channel="bench")
        ch.call("r_ssh", Onboard(REPO, "boot-id.sh"))
        (argv,) = self.sent(via)
        self.assertEqual(argv[argv.index("-l") + 1], "root")
        self.assertIn("StrictHostKeyChecking=no", argv)
        self.assertEqual((argv[-2], shlex.split(argv[-1])[-1]), ("100.64.0.5", "sh -c 'cat /proc/sys/kernel/random/boot_id'"))

    def test_a_workstation_in_host_mode_is_its_person_by_name(self):
        ch, via = self.channel()
        ch.call("m_ssh", Onboard(REPO, "boot-id.sh"))
        (argv,) = self.sent(via)
        self.assertNotIn("-l", argv)
        self.assertEqual(argv[-2], "rpi5")

    def test_an_address_given_for_the_image_wins(self):
        ch, _ = self.channel(env={"WK_IMAGE_HOST": "192.0.2.9"})
        self.assertEqual(ch.image_addr(), "192.0.2.9")

    def test_a_node_the_tailnet_calls_offline_is_not_dialled(self):
        ch, via = self.channel(online="false")
        r, _ = quiet(ch.call, "m_ssh", Onboard(REPO, "boot-id.sh"))
        self.assertEqual(r.rc, 255)
        self.assertEqual(self.sent(via), [])

    def test_standing_on_the_machine_runs_it_here(self):
        ch, via = self.channel(host="RPI5")
        ch.call("m_ssh", Onboard(REPO, "boot-id.sh"))
        self.assertEqual(self.sent(via), [("sh", "-c", "cat /proc/sys/kernel/random/boot_id")])

    def test_no_channel_reaches_nothing(self):
        ch, via = self.channel(channel="none")
        self.assertEqual(ch.call("card_priv", "status").rc, 1)
        self.assertEqual(self.sent(via), [])


class TestOnboard(unittest.TestCase):
    def test_every_file_parses_as_posix_sh(self):
        for f in sorted(ONBOARD.iterdir()):
            with self.subTest(file=f.name):
                cp = subprocess.run(["sh", "-n", str(f)], capture_output=True, text=True)
                self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_the_on_board_budget(self):
        """PLAN's ~150 lines of shell that run on a board, before python3 is there to run instead."""
        lines = sum(len([l for l in f.read_text().splitlines() if l.strip()]) for f in ONBOARD.iterdir())
        self.assertLessEqual(lines, 150)

    def test_no_python_driver_builds_shell_by_string(self):
        """every remote command is an Onboard file; the only literal a driver hands the channel is a verb."""
        for name in ("driver.py", "pi.py"):
            text = (REPO / "lib" / "wk" / "boot" / name).read_text()
            with self.subTest(file=name):
                self.assertIsNone(re.search(r'call\("(r_ssh|r_sudo|m_ssh|i_ssh)", "', text))

    def test_a_parameter_is_one_literal_word(self):
        with self.assertRaises(ValueError):
            Onboard(REPO, "boot-id.sh", WK_DEV="/dev/sda; reboot")
        self.assertTrue(Onboard(REPO, "part-absent.sh", WK_DEV="/dev/sda1").text().startswith("WK_DEV=/dev/sda1; "))


if __name__ == "__main__":
    unittest.main()
