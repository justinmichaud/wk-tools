"""The boot-driver core (lib/wk/boot): machine.conformance[<driver>] over every driver against its machine in memory
(tests/fake_boot.py), boot.arming_exact, the Channel, which system an arming boots, the armed barrier and the on-board
files."""
import contextlib
import io
import json
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, real_confs, scratch_dir

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot import driver_class, drivers, open_driver  # noqa: E402
from wk.boot.cli import load_conf  # noqa: E402
from wk.boot.driver import BOOT_PRIV, CARD_PRIV, Channel, Driver, Onboard, disk_of, part, partno  # noqa: E402
from wk.boot.mac import MacVolume  # noqa: E402
from tests.fake_boot import SLOTS_OF, board, conf_for, said  # noqa: E402
from wk.machine import Result  # noqa: E402
from tests.fakes import Fake  # noqa: E402

ONBOARD = REPO / "boot" / "onboard"


def quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args), err.getvalue()
        except act.Refused:
            return act.Refused, err.getvalue()


def arm(d, want=""):
    """The driver's own arm_sequence, run as `wk boot` runs it."""
    d.armable(d.probe())
    p, image = d.choose(want)
    for _, step in d.arm_sequence(p, image):
        step()
    return p, image


class Conformance:
    """machine.conformance[<driver>]: one body, run for every driver."""

    kind = None

    def test_the_driver_implements_the_whole_interface(self):
        cls = drivers()[self.kind]
        for verb in ("arm_sequence", "evidence", "media", "reprovision"):
            with self.subTest(verb=verb):
                self.assertIsNot(getattr(cls, verb), getattr(Driver, verb), "%s forgot %s" % (self.kind, verb))
        self.assertIs(driver_class(self.kind), cls)

    def test_the_production_channel_is_machines_and_no_bash(self):
        via = Fake()
        d = open_driver(REPO, conf_for(self.kind), env={"HOME": "/nonexistent", "WK_MACHINES_DIR": str(REPO / "machines")}, via=via)
        for verb in ("probe", "media", "evidence", "reprovision", "record_read", "booted_at"):
            quiet(getattr(d, verb))
        ran = [e[1] for e in via.effects if e[0] in ("run", "run_tty")]
        if self.kind == "mac-guest":
            self.assertIs(d.vm().machine, via, "a guest is reached through the vm place, over the same machine")
        else:
            self.assertTrue(ran, "the driver reached nothing")
        for argv in ran:
            self.assertNotIn(argv[0], ("bash", "env"), argv)

    def test_the_probe_names_the_side_that_answered(self):
        fake, d = board(self.kind)
        if fake.on_rescue() and self.kind != "mac-guest":
            self.assertIn(d.probe(), ("host", "base rescue-1"))
            self.assertEqual(d.ch.side, "host")
        fake.enter_bench() if hasattr(fake, "enter_bench") else arm(d)
        self.assertEqual(d.probe(), "bench sys-a")
        fake.up = False
        self.assertEqual((d.probe(), d.ch.side), ("unreachable", "none"))

    def test_an_arming_boots_the_selected_system_once(self):
        fake, d = board(self.kind)
        before = fake.boots
        arm(d)
        self.assertEqual(d.probe(), "bench sys-a")
        self.assertEqual(fake.boots, before + 1)
        if d.sticky:
            self.assertIs(quiet(d.reboot)[0], act.Refused, "a host-side reboot reached an install with no helper")
            fake.leave_bench()
        else:
            d.reboot()
        d.probe()
        self.assertTrue(fake.on_rescue(), "leaving bench mode did not return it")

    def test_a_disarm_before_the_reboot_boots_the_rescue(self):
        fake, d = board(self.kind)
        d.armable(d.probe())
        for _, step in d.arm_sequence(*d.choose(""))[:-1]:
            step()
        d.disarm()
        fake.restart() if self.kind == "mac-volume" else d.reboot()
        self.assertTrue(fake.on_rescue())

    def test_an_arming_that_did_not_take_is_refused(self):
        fake, d = board(self.kind)
        fake.stuck = True
        got, err = quiet(arm, d)
        self.assertIs(got, act.Refused, err)
        self.assertIn(fake.conf["name"], err)

    def test_the_record_is_written_on_the_host_and_spent_by_the_boot(self):
        fake, d = board(self.kind)
        d.probe()
        armed_boot = d.boot_id()
        arm(d)
        if self.kind == "mac-guest":
            self.assertIsNone(fake.record, "a guest either answers with a marker or it does not")
            return
        for want in ("image=sys-a" if self.kind != "mac-volume" else "image=WK Bench", "armed_boot_id=" + armed_boot, "armed_at="):
            self.assertIn(want, fake.record)
        d.probe()
        fake.leave_bench() if hasattr(fake, "leave_bench") else d.reboot()
        d.probe()
        self.assertNotIn("armed for system", quiet(d.armed_barrier, "writing now")[1], "a spent arming still bars")

    def test_every_report_answers_in_every_mode(self):
        fake, d = board(self.kind)
        for mode in ("host", "bench", "unreachable"):
            with self.subTest(mode=mode):
                fake.up = mode != "unreachable"
                if mode == "bench":
                    quiet(arm, d)
                d.probe()
                for verb in ("media", "evidence", "reprovision"):
                    got, err = quiet(getattr(d, verb))
                    self.assertIsInstance(got, str, "%s %s: %s" % (verb, mode, err))
                self.assertTrue(d.reprovision().startswith("wk "), d.reprovision())

    def test_the_failsafe_is_on_board_shell_outside_the_arming(self):
        """boot.arming_exact: a failsafe lives outside the script it guards, on one ExecStart line."""
        cls = drivers()[self.kind]
        if not cls.failsafe:
            self.assertFalse(cls.on_medium, "an arming on the medium with nothing to take it back")
            self.assertIsNone(board(self.kind)[1].self_disarm_sh())
            return
        text = (ONBOARD / cls.failsafe).read_text()
        for other in ONBOARD.iterdir():
            if other.name != cls.failsafe:
                self.assertNotIn(text.strip(), other.read_text(), "%s carries the failsafe" % other.name)
        line = board(self.kind)[1].self_disarm_sh()
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


class TestConformanceMacVolume(Conformance, unittest.TestCase):
    kind = "mac-volume"


class TestConformanceMacGuest(Conformance, unittest.TestCase):
    kind = "mac-guest"


class TestArmingExact(unittest.TestCase):
    """boot.arming_exact: two systems with one image id told apart by slot, the leg verified after the last arm."""

    def test_one_id_in_two_slots_is_named_by_slot(self):
        for kind in SLOTS_OF:
            with self.subTest(kind=kind):
                fake, d = board(kind, ("same", "same"))
                d.probe()
                got, err = quiet(d.select_system, "same")
                self.assertIs(got, act.Refused)
                self.assertIn("same@second", err)
                second = part(fake.conf["device"], SLOTS_OF[kind][1])
                slot = d.slot(second)
                self.assertEqual(d.select_system("@" + slot), (second, "same"))
                arm(d, "same@" + slot)
                self.assertEqual(fake.running, part(fake.conf["device"], SLOTS_OF[kind][1] + 1))

    def test_the_leg_is_verified_after_the_last_arm(self):
        """a first arming that took and a second that did not: the readback is of the second."""
        for kind in SLOTS_OF:
            with self.subTest(kind=kind):
                fake, d = board(kind, ("a", "b"))
                d.probe()
                for _, step in d.arm_sequence(*d.select_system("a"))[:-1]:
                    step()
                fake.stuck = True
                got, err = quiet(lambda: [s() for _, s in d.arm_sequence(*d.select_system("b"))[:-1]])
                self.assertIs(got, act.Refused, "%s: an arming that left the first system armed passed" % kind)


def steps(d, want="", upto=None):
    """Run d's arm_sequence for `want` through step `upto` (all but the reboot by default)."""
    d.probe()
    seq = d.arm_sequence(*d.select_system(want))
    for _, step in seq[:len(seq) - 1 if upto is None else upto]:
        step()


class TestPiSd(unittest.TestCase):
    """The rescue's config.txt names the system that boots next, through the card helper on the rescue."""

    def arms(self, fake):
        return [e[2] for e in fake.effects if e[:2] == ("card", "second-arm")]

    def test_arm_selects_the_named_system_and_skips_only_when_armed_for_it(self):
        fake, d = board("pi-sd", ("img-a", "img-b"))
        d.disarm()
        self.assertFalse([e for e in fake.effects if "second-disarm" in e], "disarmed a board that was not armed")
        self.assertIn("is not a bench system's boot partition", quiet(d.slot, "/dev/mmcblk0p2")[1])
        for want in ("img-b", "img-a", "img-a"):
            steps(d, want)
        self.assertEqual(self.arms(fake), ["/dev/mmcblk0@third", "/dev/mmcblk0@second"], "armed already: a no-op")

    def test_a_card_with_no_second_system_is_refused(self):
        fake, d = board("pi-sd", ())
        d.probe()
        self.assertIn("@second", quiet(d.arm, "second")[1])
        self.assertEqual(self.arms(fake), [])

    def test_evidence_comes_from_the_card_not_the_record_and_reprovisioning_from_a_reader(self):
        fake, d = board("pi-sd")
        d.probe()
        d.arm("second")
        out = d.evidence()
        self.assertIn("armed=yes", out)
        self.assertIn("system=sys-a (on /dev/mmcblk0p5)", out)
        self.assertIsNone(fake.record, "the evidence wrote or needed a record")
        for want in ("--disk <reader>:/dev/mmcblk0 --rescue", "--disk <reader>:/dev/mmcblk0@second", "wk boot rpi3"):
            self.assertIn(want, d.reprovision())
        self.assertEqual(d.boot_part(), "/dev/mmcblk0p3")


class TestPiTryboot(unittest.TestCase):
    """The selected system's kernel is staged onto the rescue's SD, and only an arming reboot carries the flag."""

    def test_an_arming_reboot_refuses_where_the_flag_cannot_be_passed(self):
        fake, d = board("pi-tryboot")
        steps(d)
        fake.roots[fake.running]["systemd"] = False
        _, err = quiet(d.arm_sequence(*d.select_system(""))[-1][1])
        self.assertIn("no systemd", err)
        self.assertIn("--back", err)
        self.assertFalse([e for e in fake.effects if e[0] == "reboot"], "it rebooted anyway")

    def test_only_an_arming_reboot_carries_the_flag_and_the_boot_spends_the_staging(self):
        fake, d = board("pi-tryboot")
        steps(d, upto=99)
        d.probe()
        d.reboot()
        self.assertEqual([e for e in fake.effects if e[0] == "reboot"], [("reboot", True), ("reboot", False)])
        self.assertNotIn("tryboot.txt", fake.fat[fake.sd()], "the self-disarm did not run")
        self.assertTrue(d.self_disarm_sh().startswith("WK_SD=/dev/mmcblk0p1; "))

    def test_the_staging_is_of_the_selected_system_and_every_step_is_on_the_side_that_answered(self):
        fake, d = board("pi-tryboot", ("a", "b"))
        steps(d, "b")
        self.assertIn(("sh", "tryboot.sh", "stage"), fake.effects)
        self.assertEqual(fake.fat[fake.sd()]["second/cmdline.txt"], "root=/dev/sda4 rootwait panic=10\n")
        d.disarm()
        d.evidence()
        self.assertEqual(fake.machines["bench"].effects, [])
        fake.stuck = True
        steps(d, "a", upto=1)
        self.assertIn("not the selected system's root=/dev/sda2", quiet(d.arm, "/dev/sda1")[1])
        fake.fat.pop("/dev/sda1")
        self.assertIn("could not stage the tryboot files on rpi4", quiet(d.arm, "/dev/sda1")[1])

    def test_the_evidence_says_which_config_the_running_boot_came_from(self):
        fake, d = board("pi-tryboot", ("alpha-1", "beta-2"))
        d.probe()
        out = d.evidence()
        for want in ("boot_source=the SD config.txt", "tryboot_staged=no", "bench root on /dev/sda", "system=beta-2 (on /dev/sda3)"):
            self.assertIn(want, out)
        fake.up = False
        self.assertIn("boot_source=unreadable", d.evidence())
        self.assertIn("wk boot rpi4 --boot-order sd-first", d.reprovision())


class TestRpi5Usb(unittest.TestCase):
    """The stick's pair is autoboot.txt's boot_partition= and a plain reboot: its tryboot flag belongs to flash-kernel
    on the NVMe, and a pair selected with it does not boot (rpi5, measured). The firmware is the helper's to touch."""

    def test_arming_never_uses_the_tryboot_flag_nor_sudoes_the_firmware_itself(self):
        fake, d = board("rpi5-usb", ("a", "b"))
        steps(d, "b", upto=99)
        d.probe()
        d.reboot()
        self.assertEqual([("reboot", False)] * 2, [e for e in fake.effects if e[0] == "reboot"])
        self.assertIn(("priv", "order", "0xf64"), fake.effects)
        sudoed = {a[1][2] for a in fake.machines["host"].effects if a[0] == "run" and a[1][:2] == ("sudo", "-n")}
        self.assertTrue(sudoed and sudoed <= {BOOT_PRIV, CARD_PRIV}, sudoed)

    def test_only_the_two_pairs_are_selectable_and_the_selection_is_read_back(self):
        fake, d = board("rpi5-usb", ("a", "b"))
        d.probe()
        self.assertIn("is not a boot partition this stick selects between", quiet(d.arm_sequence, "/dev/sda5", "x")[1])
        fake.stuck = True
        _, err = quiet(steps, d, "b")
        self.assertIn("does not select pair 3", err)
        self.assertIn("./setup --stage quiesce", err)
        self.assertNotIn(("priv", "order", "0xf64"), fake.effects, "the firmware was told anyway")

    def test_it_asks_for_the_helper_before_the_firmware_and_names_the_remedy(self):
        fake, d = board("rpi5-usb")
        reply = fake.reply
        fake.reply = lambda side, argv, input=None: Result(1) if argv[-1:] == ["status"] else reply(side, argv, input)
        _, err = quiet(steps, d)
        self.assertIn("./setup --stage quiesce", err)
        self.assertFalse([e for e in fake.effects if e[0] == "priv"])


class TestChannel(unittest.TestCase):
    """Which Machine each run lands on, and as whom."""

    PEERS = {"Peer": {"a": {"DNSName": "rpi5-bench.ts.net.", "TailscaleIPs": ["100.64.0.5"], "Online": True},
                      "b": {"DNSName": "rpi5.ts.net.", "TailscaleIPs": ["100.64.0.4"], "Online": True},
                      "c": {"DNSName": "tolken.ts.net.", "TailscaleIPs": ["100.64.0.2"], "Online": True},
                      "d": {"DNSName": "tolken-bench.ts.net.", "TailscaleIPs": ["100.64.0.3"], "Online": True}}}

    def channel(self, role="workstation", side="host", down=(), env=None, host="elsewhere", conf=None, board=True):
        via = Fake()
        peers = json.loads(json.dumps(self.PEERS))
        for p in peers["Peer"].values():
            p["Online"] = p["DNSName"].split(".")[0] not in down
        via.answer(("tailscale",), out=json.dumps(peers))
        via.answer(("hostname", "-s"), out=host + "\n")
        via.answer(("ssh",), out="ok\n")
        conf = conf or {"name": "rpi5", "ssh": "rpi5", "bench_ssh": "rpi5-bench", "role": role}
        ch = Channel(REPO, conf, env=env or {}, via=via, board=board)
        ch.side = side
        return ch, via

    def sent(self, via):
        return [list(e[1]) for e in via.effects if e[1][0] in ("ssh", "sh", "echo")]

    def test_a_boards_bench_system_is_root_at_its_tailnet_address_unpinned_whatever_its_role(self):
        for role in ("bench-device", "workstation", ""):
            with self.subTest(role=role):
                ch, via = self.channel(role, "bench")
                ch.run(["cat", "/x"])
                (argv,) = self.sent(via)
                self.assertEqual(argv[argv.index("-l") + 1], "root")
                self.assertIn("StrictHostKeyChecking=no", argv)
                self.assertEqual((argv[-2], shlex.split(argv[-1])[-1]), ("100.64.0.5", "cat /x"))

    def test_privilege_follows_the_side_and_the_role(self):
        for side, role, root in (("bench", "workstation", True), ("host", "bench-device", True), ("host", "workstation", False)):
            with self.subTest(side=side, role=role):
                ch, via = self.channel(role, side)
                ch.card("status")
                ch.priv("order", "0xf64")
                said = [shlex.split(a[-1])[-1] for a in self.sent(via)]
                self.assertEqual([s.startswith("sudo -n ") for s in said], [not root] * 2, said)
                self.assertIn("vcmailbox" if root else "wk-boot-priv order 0xf64", said[1])
                self.assertEqual(ch.priv("status").ok, True)

    def test_an_address_given_for_the_image_wins(self):
        ch, _ = self.channel(env={"WK_IMAGE_HOST": "192.0.2.9"})
        self.assertEqual(ch.machine("bench").dest, "192.0.2.9")

    def test_a_node_the_tailnet_calls_offline_is_not_dialled_and_the_tailnet_is_read_once(self):
        ch, via = self.channel(down=("rpi5",))
        r, err = quiet(ch.run, ["true"])
        self.assertEqual(r.rc, 255)
        self.assertIn("wk machine probe rpi5", err)
        self.assertEqual(self.sent(via), [])
        self.assertTrue(ch.run(["true"], "bench").ok, "the node that is up is still asked")
        self.assertEqual(len([e for e in via.effects if e[1][0] == "tailscale"]), 1)

    def test_it_runs_here_on_the_named_machine_and_over_ssh_from_any_other(self):
        mbp = load_conf(REPO, "mbp", {"WK_MACHINES_DIR": str(REPO / "machines")})
        for conf, host, over in (({"ssh": "othermach"}, "moose", "othermach"), ({"ssh": "moose"}, "MOOSE", None),
                                 (mbp, "moose", "tolken"), (mbp, "tolken", None), ({"ssh": ""}, "", None)):
            with self.subTest(ssh=conf["ssh"], host=host):
                ch, via = self.channel(conf=conf, host=host)
                ch.run(["echo", "RAN-HERE"])
                ran = self.sent(via)
                if over:
                    self.assertEqual([(a[-2], shlex.split(a[-1])[-1]) for a in ran], [(over, "echo RAN-HERE")])
                else:
                    self.assertEqual(ran, [["echo", "RAN-HERE"]] if conf["ssh"] else [])

    def test_a_macs_bench_side_is_its_node_by_name_neither_root_nor_unpinned(self):
        conf = {"name": "mbp", "ssh": "tolken", "bench_ssh": "tolken-bench"}
        ch, via = self.channel(conf=conf, side="bench", board=False)
        ch.run(["true"])
        (argv,) = self.sent(via)
        self.assertEqual(argv[-2], "tolken-bench")
        self.assertNotIn("root", argv)
        self.assertFalse([a for a in argv if "StrictHostKeyChecking" in a])
        self.assertEqual(self.channel(conf=conf, board=False, env={"WK_MAC_BENCH_SSH": "elsewhere"})[0].bench_name(), "elsewhere")
        self.assertIsInstance(self.channel(conf=dict(conf, bench_ssh=""), board=False)[0].machine("bench"), Result)

    def test_a_powered_off_mac_probes_unreachable_without_an_ssh(self):
        conf = conf_for("mac-volume")
        ch, via = self.channel(conf=conf, down=("tolken", "tolken-bench"), board=False)
        got, err = quiet(MacVolume(REPO, conf, ch).probe)
        self.assertEqual(got, "unreachable")
        self.assertEqual(self.sent(via), [])
        self.assertIn("tolken-bench is offline", err)

    def test_the_reboot_without_a_helper_detaches_with_what_both_platforms_ship(self):
        """macOS ships no `setsid` (measured on tolken, macOS 26.6.2), so the reboot detaches with nohup."""
        text = (ONBOARD / "reboot.sh").read_text()
        self.assertIn("nohup ", text)
        self.assertNotIn("setsid", text)


class TestWhichSystem(unittest.TestCase):
    """`systems` enumerates the medium's candidate partitions, `select_system` resolves --system against them, and
    `medium_read` is the one reader of a boot partition's files."""

    def test_a_slot_the_medium_does_not_have_reads_as_empty_and_says_nothing(self):
        fake, d = board("rpi5-usb")
        d.probe()
        got, err = quiet(d.systems)
        self.assertEqual(got, [("/dev/sda1", "sys-a")])
        self.assertNotIn("card helper is older", err)

    def test_a_slot_that_is_present_but_unreadable_warns_and_fails(self):
        fake, d = board("rpi5-usb")
        d.probe()
        fake.card = lambda verb, *a: Result(1)
        got, err = quiet(d.systems)
        self.assertIsNone(got)
        self.assertIn("./setup --stage quiesce", err)

    def test_a_bench_device_reads_the_medium_itself_and_a_workstation_through_the_helper(self):
        for kind, first in (("pi-tryboot", ("sh", "medium-read.sh", "")), ("rpi5-usb", ("card", "boot-read", "/dev/sda", "1", "wk-image.id"))):
            with self.subTest(kind=kind):
                fake, d = board(kind)
                d.probe()
                d.device_image()
                self.assertEqual(said(fake.machines["host"].effects[-1][1]), first)

    def test_selection_refuses_to_guess(self):
        fake, d = board("pi-tryboot", ("alpha-1", "beta-2"))
        d.probe()
        self.assertEqual(d.select_system("beta-2"), ("/dev/sda3", "beta-2"))
        for want, words in (("", ("holds 2 systems", "alpha-1", "beta-2", "--system")), ("gamma-3", ("gamma-3", "@second"))):
            got, err = quiet(d.select_system, want)
            self.assertIs(got, act.Refused)
            for w in words:
                self.assertIn(w, err)

    def test_an_empty_medium_names_the_write_remedy_and_an_unreadable_one_is_not_empty(self):
        fake, d = board("pi-tryboot", ())
        d.probe()
        self.assertIn("wk sysimage write", quiet(d.select_system, "")[1])
        fake.up = False
        self.assertIn("could not read", quiet(d.select_system, "")[1])

    def test_diag_reads_every_system_and_says_which_is_which(self):
        fake, d = board("pi-tryboot", ("alpha-1", "beta-2"))
        fake.fat["/dev/sda1"]["wk-diag.txt"] = "(dump)\n"
        d.probe()
        out = d.diag()
        self.assertIn("== alpha-1 (/dev/sda1) ==\n(dump)", out)
        self.assertIn("== beta-2 (/dev/sda3) ==\n(no wk-diag.txt", out)

    def test_partition_to_disk_for_every_transport(self):
        for p, disk in (("/dev/sda2", "/dev/sda"), ("/dev/mmcblk0p2", "/dev/mmcblk0"), ("/dev/nvme0n1p2", "/dev/nvme0n1")):
            with self.subTest(part=p):
                self.assertEqual(disk_of(p), disk)
                self.assertEqual(part(disk, partno(p)), p)

    def test_every_conf_loads(self):
        """A conf `load_conf` cannot load is a machine that silently leaves the fleet."""
        confs = real_confs("board", "mac", "guest")
        self.assertTrue(confs, "no machine confs found")
        for conf in confs:
            with self.subTest(machine=conf.stem):
                self.assertEqual((load_conf(REPO, conf.stem, {"WK_MACHINES_DIR": str(REPO / "machines")}) or {}).get("name"), conf.stem)


class TestArmedBarrier(unittest.TestCase):
    """The one check that stops a mutating command racing a machine between `wk boot`, which leaves an arming record,
    and the reboot that record waits for."""

    def barrier(self, setup, force=False):
        fake, d = board("rpi5-usb")
        d.probe()
        setup(fake, d)
        with mock.patch.dict("os.environ", {"WK_FORCE": "1" if force else ""}):
            try:
                return quiet(d.armed_barrier, "Doing the thing now would race the reboot.")
            finally:
                act._forced.clear()

    def armed(self, fake, d):
        fake.record = "image=demo-system\narmed_boot_id=boot-1\n"

    def test_an_unarmed_a_spent_or_a_bench_answer_passes(self):
        for setup in (lambda f, d: None, lambda f, d: (self.armed(f, d), setattr(f, "boots", 2)),
                      lambda f, d: (self.armed(f, d), setattr(d, "mode", "bench demo-system"))):
            self.assertIsNone(self.barrier(setup)[0])

    def test_armed_and_not_yet_rebooted_or_unprobed_refuses_with_remedy_and_force_crosses(self):
        for setup, said in ((self.armed, "is armed for system 'demo-system'"),
                            (lambda f, d: (setattr(f, "up", False), setattr(d, "mode", "unreachable")), "could not tell what rpi5")):
            got, err = self.barrier(setup)
            self.assertIs(got, act.Refused, err)
            for want in (said, "Doing the thing now", "wk boot rpi5 --"):
                self.assertIn(want, err)
            self.assertIn("FORCED past a barrier", self.barrier(setup, force=True)[1])


class TestOnboard(unittest.TestCase):
    def test_every_file_parses_as_posix_sh(self):
        for f in sorted(ONBOARD.iterdir()):
            with self.subTest(file=f.name):
                cp = subprocess.run(["sh", "-n", str(f)], capture_output=True, text=True)
                self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_the_on_board_budget(self):
        """PLAN's ~150 lines of shell that run on a board, before python3 is there to run instead."""
        self.assertLessEqual(sum(len([l for l in f.read_text().splitlines() if l.strip()]) for f in ONBOARD.iterdir()), 150)

    def test_a_parameter_is_one_literal_word(self):
        with self.assertRaises(ValueError):
            Onboard(REPO, "boot-id.sh", WK_DEV="/dev/sda; reboot")
        self.assertTrue(Onboard(REPO, "keep.sh", WK_DEV="/dev/sda1").text().startswith("WK_DEV=/dev/sda1; "))

    def test_the_record_lives_where_the_driver_says(self):
        """A board's at /var/lib/wk/boot/armed, a Mac's under its user's state directory."""
        with scratch_dir() as tmp:
            for params, at in (({"WK_RECORD": str(tmp / "b" / "armed")}, tmp / "b" / "armed"), ({}, tmp / "state" / "wk" / "boot-armed")):
                env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "XDG_STATE_HOME": str(tmp / "state")}
                for do, inp in (("write", "image=x\n"), ("read", None)):
                    cp = subprocess.run(Onboard(REPO, "record.sh", WK_DO=do, **params).argv(), input=inp or "", env=env, text=True, capture_output=True)
                self.assertTrue(cp.stdout.startswith("image=x\narmed_at="), cp.stdout + cp.stderr)
                subprocess.run(Onboard(REPO, "record.sh", WK_DO="clear", **params).argv(), env=env)
                self.assertFalse(at.exists())


if __name__ == "__main__":
    unittest.main()
