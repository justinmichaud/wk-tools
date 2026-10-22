"""The boot drivers (lib/wk/boot) against their machines in memory (tests/fake_boot.py): machine.conformance[<driver>]
over every driver, boot.arming_exact, the Channel, which system an arming boots, the armed barrier, the on-board files,
and each driver's own behaviour beyond the contract (pi-sd, pi-tryboot, rpi5-usb with its pair select and CMA rewrite,
mac-volume, mac-guest); `wk boot --status`'s quiet siblings and a buildroot image's pinned kernel."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_boot import SLOTS_OF, board, conf_for, said
from tests.fakes import Fake
from tests.support import REPO, bash, real_confs, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import act  # noqa: E402
from wk.boot import cli, driver_class, drivers, open_driver  # noqa: E402
from wk.boot.cli import load_conf  # noqa: E402
from wk.boot.driver import BOOT_PRIV, CARD_PRIV, Channel, Driver, Onboard, disk_of, part, partno  # noqa: E402
from wk.boot.mac import MacVolume  # noqa: E402
from wk.machine import Result, here  # noqa: E402
from wk.sysimage import buildroot  # noqa: E402


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

    def test_a_disarm_before_the_reboot_boots_the_rescue_unless_bench_is_the_default(self):
        fake, d = board(self.kind)
        d.armable(d.probe())
        for _, step in d.arm_sequence(*d.choose(""))[:-1]:
            step()
        d.disarm()
        fake.restart() if self.kind == "mac-volume" else d.reboot()
        self.assertEqual(fake.on_rescue(), not d.sticky)

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


def volume(**kw):
    return board("mac-volume", **kw)


class TestProbe(unittest.TestCase):
    """Host mode first, and asked for the marker too; a bench node answering without one is some other computer."""

    def test_a_marker_on_the_host_side_is_bench_mode_there(self):
        fake, d = volume()
        fake.roots["host"]["id"] = "perf-macos-tolken-2026-08"
        self.assertEqual((d.probe(), d.ch.side), ("bench perf-macos-tolken-2026-08", "host"))
        self.assertFalse([a for a in fake.asked if a[0] == "bench"])

    def test_a_bench_node_without_the_marker_or_a_machine_declaring_none_is_unreachable(self):
        fake, d = volume()
        fake.enter_bench()
        fake.roots["bench"]["id"] = ""
        self.assertEqual((d.probe(), d.ch.side), ("unreachable", "none"))
        fake.conf["bench_ssh"] = ""
        fake.asked.clear()
        self.assertEqual(d.probe(), "unreachable")
        self.assertFalse([a for a in fake.asked if a[0] == "bench"])

    def test_the_boot_id_is_the_boottime_seconds_and_not_usec(self):
        fake, d = volume()
        d.probe()
        self.assertEqual((d.boot_id(), d.booted_at()), (str(fake.booted), fake.clock.iso()))


class TestArm(unittest.TestCase):
    """The bench volume is the firmware default, set by hand: an arming reads that, asks the helper and restarts."""

    def arm(self, **fake_state):
        fake, d = volume()
        for k, v in fake_state.items():
            setattr(fake, k, v)
        d.probe()
        steps = d.arm_sequence(*d.choose(""))
        got, err = quiet(lambda: [step() for _, step in steps[:-1]] and 0)
        return fake, got, err, fake.priv_asked()

    def test_it_reads_the_default_asks_the_helper_and_blesses_nothing(self):
        fake, got, err, verbs = self.arm()
        self.assertEqual(got, 0, err)
        self.assertEqual(verbs, ["status"])
        self.assertIn("image=WK Bench", fake.record)

    def test_a_default_that_is_not_the_bench_volume_is_refused_with_the_one_time_remedy(self):
        fake, got, err, verbs = self.arm(firmware="host")
        self.assertIs(got, act.Refused)
        for words in ("the host install", "System Settings > General > Startup Disk", "wk never blesses", "Nothing was armed"):
            self.assertIn(words, err)
        self.assertEqual((verbs, fake.record), ([], None))

    def test_an_absent_volume_or_helper_is_refused_before_anything_is_recorded(self):
        for state, words, asked in (({"attached": False}, "is not attached to mbp", []),
                                    ({"helper_is": None}, "no boot helper on tolken", ["status"]),
                                    ({"helper_is": "old"}, "older than this tree", ["status"])):
            with self.subTest(state=state):
                fake, got, err, verbs = self.arm(**state)
                self.assertIs(got, act.Refused)
                self.assertIn(words, err)
                self.assertEqual((verbs, fake.record), (asked, None))


class TestTheWayBack(unittest.TestCase):
    """Leaving bench mode is the person's pick at the power button; nothing in firmware is wk's to undo."""

    def test_a_reboot_from_the_bench_side_is_refused_naming_the_power_button(self):
        fake, d = volume()
        fake.enter_bench()
        d.probe()
        got, err = quiet(d.reboot)
        self.assertIs(got, act.Refused)
        for words in ("carries no boot helper", "hold the power button"):
            self.assertIn(words, err)
        self.assertEqual(fake.priv_asked(), [])

    def test_a_disarm_asks_nothing_and_says_the_default_stays(self):
        fake, d = volume()
        d.probe()
        self.assertEqual(quiet(d.disarm)[0], 0)
        self.assertEqual(fake.priv_asked(), [])
        self.assertIn("default stays 'WK Bench', so a plain reboot still enters bench mode", d.disarm_note())


class TestReports(unittest.TestCase):
    def test_a_bench_answer_reports_the_medium_as_that_installs_own_root(self):
        fake, d = volume()
        fake.enter_bench()
        d.probe()
        self.assertIn("tolken-bench is running from it", d.media())
        self.assertEqual(d.sip(), {"bench": "enabled"})

    def test_silence_on_both_nodes_names_both_of_them(self):
        fake, d = volume()
        fake.up = False
        d.probe()
        self.assertIn("neither tolken nor tolken-bench answers", d.media())
        for want in ("booted_volume=unknown", "firmware_default=unknown", "bench_display=builtin 1280x832"):
            self.assertIn(want, d.evidence())

    def test_evidence_reads_the_facts_with_the_trees_own_reader(self):
        fake, d = volume()
        d.probe()
        ev = d.evidence()
        for want in ("booted_volume=Macintosh HD", "attached at /Volumes/WK Bench", "firmware_default=WK Bench (",
                     "a plain reboot is expected to enter bench mode"):
            self.assertIn(want, ev)
        self.assertTrue(fake.fact_input.startswith("#!/usr/bin/env python3"), "lib/wk/mac.py travels on stdin")
        self.assertEqual(d.sip(), {"host": "enabled"})

    def test_the_bench_system_is_the_volumes_marker(self):
        fake, d = volume()
        d.probe()
        self.assertEqual(d.systems(), [("/Volumes/WK Bench", "sys-a")])
        fake.attached = False
        self.assertEqual(d.systems(), [])
        fake.up = False
        self.assertIsNone(d.systems())


class TestOnTheMac(unittest.TestCase):
    @requires_machine("tolken")
    def test_what_an_arming_reads_first_holds_on_mbp(self):
        """`live boot.arm[mbp]`, read-only as `requires_machine` is: host mode, the volume attached, and the firmware
        default naming the bench volume. The restart into it is the owed half."""
        d = open_driver(REPO, conf_for("mac-volume"))
        if d.probe() != "host":
            self.skipTest("mbp answers as %s, and an arming starts from host mode" % d.mode)
        self.assertTrue(d.volume_present(), d.media())
        fw = d.firmware_default()
        self.assertTrue(fw.startswith("WK Bench ("), fw)


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


class TestStagingRuns(unittest.TestCase):
    """boot/onboard/tryboot.sh run against a fixture, with mount, umount, sync and /proc/mounts shimmed."""

    ZIMAGE_MAGIC = (36, bytes((0x18, 0x28, 0x6F, 0x01)))
    ARM64_MAGIC = (56, b"ARM\x64")

    def write_kernel(self, path, magic):
        off, word = magic
        blob = bytearray(1024)
        blob[off:off + 4] = word
        path.write_bytes(bytes(blob))

    def stage(self, config, kernels, cmdline="root=PARTUUID=aa-02 rootwait\n"):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        medium, sd, shim = tmp / "medium", tmp / "sd", tmp / "bin"
        for d in (medium, sd, shim):
            d.mkdir()
        (medium / "config.txt").write_text(config)
        (medium / "cmdline.txt").write_text(cmdline)
        (medium / "bcm2711-rpi-4-b.dtb").write_bytes(b"dtb")
        (medium / "overlays").mkdir()
        (medium / "overlays" / "vc4-kms-v3d.dtbo").write_bytes(b"ovl")
        for name, magic in kernels.items():
            self.write_kernel(medium / name, magic)
        for f in ("start4.elf", "fixup4.dat"):
            (sd / f).write_bytes(b"fw")
        real_grep = shutil.which("grep")
        (shim / "grep").write_text('#!/bin/sh\nfor a; do l=$a; done\n[ "$l" = /proc/mounts ] && { echo "/dev/fake1 %s vfat rw 0 0"; exit 0; }\n'
                                   'exec %s "$@"\n' % (sd, real_grep))
        (shim / "mount").write_text('#!/bin/sh\nfor a in "$@"; do t=$a; done\ncp -a "%s"/. "$t"\n' % medium)
        (shim / "umount").write_text('#!/bin/sh\nfor a in "$@"; do t=$a; done\nfind "$t" -mindepth 1 -delete\n')
        (shim / "sync").write_text("#!/bin/sh\nexit 0\n")
        for f in shim.iterdir():
            f.chmod(0o755)
        text = Onboard(REPO, "tryboot.sh", WK_DO="stage", WK_SD="/dev/fake1", WK_SRC="/dev/sda1", WK_DTB="bcm2711-rpi-4-b.dtb").text()
        cp = subprocess.run(["sh", "-c", text], capture_output=True, text=True,
                            env={"PATH": "%s:/usr/bin:/bin" % shim})
        return cp, sd

    def test_a_kernel_line_names_the_kernel_and_states_32_bit(self):
        cp, sd = self.stage("kernel=zImage\ndtparam=audio=on\n", {"zImage": self.ZIMAGE_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for f in ("zImage", "bcm2711-rpi-4-b.dtb", "start4.elf", "fixup4.dat"):
            self.assertTrue((sd / "second" / f).exists(), f"{f} was not staged")
        self.assertTrue((sd / "second" / "overlays" / "vc4-kms-v3d.dtbo").exists())
        txt = (sd / "tryboot.txt").read_text()
        self.assertIn("os_prefix=second/", txt)
        self.assertIn("arm_64bit=0", txt)
        self.assertIn("panic=10", (sd / "second" / "cmdline.txt").read_text())

    def test_a_cmdline_that_chose_a_panic_keeps_its_own(self):
        cp, sd = self.stage("kernel=zImage\n", {"zImage": self.ZIMAGE_MAGIC}, cmdline="root=PARTUUID=aa-02 panic=5\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((sd / "second" / "cmdline.txt").read_text().split(), ["root=PARTUUID=aa-02", "panic=5"])

    def test_the_prefix_leads_the_staged_config(self):
        """the firmware resolves each filename as it reads the directive asking for it."""
        cp, sd = self.stage("dtoverlay=vc4-kms-v3d\n[pi4]\ndtparam=audio=on\n", {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = [l for l in (sd / "tryboot.txt").read_text().splitlines() if l.strip()]
        self.assertEqual(lines[0], "os_prefix=second/")
        self.assertLess(lines.index("arm_64bit=1"), lines.index("dtoverlay=vc4-kms-v3d"))

    def test_a_kernel_of_neither_bitness_states_none(self):
        cp, sd = self.stage("kernel=zImage\n", {"zImage": (0, b"\x00\x00\x00\x00")})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("arm_64bit", (sd / "tryboot.txt").read_text())

    def test_no_kernel_line_resolves_the_one_default_name_there_is(self):
        cp, sd = self.stage('#kernel=""\ndtoverlay=vc4-kms-v3d\n', {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((sd / "second" / "kernel8.img").exists())

    def test_two_default_names_and_no_kernel_line_refuses(self):
        cp, sd = self.stage("dtparam=audio=on\n", {"kernel8.img": self.ARM64_MAGIC, "kernel7l.img": self.ZIMAGE_MAGIC})
        self.assertNotEqual(cp.returncode, 0)
        for want in ("kernel8.img", "kernel7l.img", "config.txt.append"):
            self.assertIn(want, cp.stderr)
        self.assertFalse((sd / "second").exists(), "nothing is staged on a refusal")

    def test_no_kernel_at_all_refuses(self):
        cp, sd = self.stage("dtparam=audio=on\n", {})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("holds none of", cp.stderr)
        self.assertFalse((sd / "second").exists())


CARD_HELPER = REPO / "admin" / "wk-card-priv"


class TestTheHelperTakesAPair(unittest.TestCase):
    def _run(self, args):
        body = subprocess.run(
            ["sed", "-n", "/^_autoboot_write()/,/^}/p;/^v_autoboot()/,/^}/p", str(CARD_HELPER)],
            capture_output=True, text=True).stdout
        script = (
            'say() { echo "$*"; }\n'
            'deny() { echo "REFUSED: $*" >&2; exit 3; }\n'
            'fail() { echo "$*" >&2; exit 1; }\n'
            'gate() { GATED_DEV="$1"; }\n'
            'part() { echo "$1$2"; }\n'
            'with_mount() { local m=/tmp/wk-ab-$$; mkdir -p "$m"; shift; "$@" "$m"; }\n'
            + body.replace('with_mount "$(part "$dev" 1)" _autoboot_write "$pair"',
                           'mkdir -p /tmp/wk-ab-$$; _autoboot_write /tmp/wk-ab-$$ "$pair"; cat /tmp/wk-ab-$$/autoboot.txt')
            + f"\nv_autoboot {args}\n")
        return bash(script)

    def test_pair_three_is_written_and_pair_one_is_the_default(self):
        for args, pair in (("/dev/sdX 3", "3"), ("/dev/sdX", "1")):
            with self.subTest(args=args):
                cp = self._run(args)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("boot_partition=" + pair, cp.stdout)

    def test_any_other_pair_is_refused(self):
        for bad in ("2", "4", "0", "1;rm -rf /"):
            with self.subTest(pair=bad):
                cp = self._run(f"/dev/sdX '{bad}'")
                self.assertEqual(3, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("REFUSED", cp.stderr)


SCRIPT = REPO / "host" / "linux" / "rpi5" / "rpi5-setup.sh"


# The sizes vc4-kms-v3d-pi5.dtbo defines an override for.
OVERLAY_SIZES = {64, 96, 128, 192, 256, 320, 384, 448, 512}


# The stock Ubuntu/RPi config.txt, reduced to the lines the rewrite must reason
# about: two [all] sections, and the other boards' own cma- lines.
STOCK = """\
[all]
os_check=0
os_prefix=current/

[tryboot]
os_prefix=new/

[all]
arm_64bit=1
dtparam=audio=on
dtoverlay=vc4-kms-v3d
disable_fw_kms_setup=1
dtoverlay=dwc2

[pi4]
max_framebuffers=2

[pi3+]
dtoverlay=vc4-kms-v3d,cma-128

[pi02]
dtoverlay=vc4-kms-v3d,cma-128

[all]

[pi5]
dtparam=pciex1_gen=3
arm_freq=2800
[all]
"""


def awk_program():
    src = SCRIPT.read_text()
    m = re.search(r"""awk -v want="\$CMA_MB" '(.*?)' "\$CFG" > "\$_cma_tmp\"""",
                  src, re.S)
    assert m, "the CMA rewrite is no longer an awk program in " + str(SCRIPT)
    return m.group(1)


def rewrite(text, want=512):
    cp = subprocess.run(["awk", "-v", f"want={want}", awk_program()],
                        input=text, capture_output=True, text=True, check=True)
    return cp.stdout


def overlay_lines(text):
    return [l for l in text.splitlines() if l.startswith("dtoverlay=vc4-kms-v3d")]


class SizeVocabulary(unittest.TestCase):
    def test_default_is_a_size_the_overlay_defines(self):
        m = re.search(r'^CMA_MB="\$\{CMA_MB:-(\d+)\}"', SCRIPT.read_text(), re.M)
        self.assertIsNotNone(m, "rpi5-setup.sh no longer defaults CMA_MB")
        self.assertIn(int(m.group(1)), OVERLAY_SIZES)


class Rewrite(unittest.TestCase):
    def test_the_all_line_gains_the_size(self):
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-512", rewrite(STOCK).splitlines())

    def test_exactly_one_line_changes_and_other_boards_keep_their_own_size(self):
        before, after = STOCK.splitlines(), rewrite(STOCK).splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual([i for i, (a, b) in enumerate(zip(before, after)) if a != b],
                         [before.index("dtoverlay=vc4-kms-v3d")])

    def test_an_existing_size_is_replaced_not_appended(self):
        got = rewrite(STOCK.replace("dtoverlay=vc4-kms-v3d\n",
                                    "dtoverlay=vc4-kms-v3d,cma-256\n", 1))
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-512", overlay_lines(got))
        self.assertNotIn("cma-256", got)

    def test_other_overlay_parameters_survive(self):
        got = rewrite(STOCK.replace("dtoverlay=vc4-kms-v3d\n",
                                    "dtoverlay=vc4-kms-v3d,noaudio\n", 1))
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-512,noaudio", overlay_lines(got))

    def test_rerunning_changes_nothing(self):
        once = rewrite(STOCK)
        self.assertEqual(rewrite(once), once)


REPO = Path(__file__).resolve().parent.parent


class TestQuietSiblings(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wk-test-quiet-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)

    def _machine(self, name, net="wifi", bench=True):
        (self.dir / f"{name}.conf").write_text(
            f'ssh="{name}-rescue"\n'
            + (f'bench_ssh="{name}-bench"\n' if bench else "")
            + f'net={net}\n'
            'kind=board\ndriver=pi-sd\ndevice=/dev/mmcblk0\nroot=/dev/mmcblk0p2\n'
            f'note="{name}, a test fixture"\n'
        )

    def _run(self, me, peers):
        """peers: (name, up?) as the tailnet reports them."""
        env = {"WK_MACHINES_DIR": str(self.dir), "HOME": "/nonexistent"}
        rows = [(n, "100.0.0.1", "up" if u else "down") for n, u in peers]
        b = cli.Boot(REPO, cli.load_conf(REPO, me, env), None, env=env, peers=types.SimpleNamespace(peers=lambda: rows))
        return b.quiet_siblings()

    def test_the_quiet_siblings_are_counted_of_all_of_them(self):
        for n in ("rpi3", "rpi4", "rpi5"):
            self._machine(n)
        for rpi5_up, want in ((False, (2, 2)), (True, (1, 2))):
            with self.subTest(rpi5_up=rpi5_up):
                got = self._run("rpi4", [("rpi3-rescue", False), ("rpi3-bench", False), ("rpi5-rescue", rpi5_up), ("rpi5-bench", False)])
                self.assertEqual(got, want)

    def test_either_of_a_boards_two_names_counts_as_up(self):
        self._machine("rpi3")
        self._machine("rpi4")
        self.assertEqual(self._run("rpi4", [("rpi3-bench", True)]), (0, 1))

    def test_only_devices_on_the_same_network_are_siblings(self):
        self._machine("rpi3", net="wifi")
        self._machine("rpi4", net="wifi")
        self._machine("moose", net="cable")
        got = self._run("rpi4", [("rpi3-rescue", False), ("rpi3-bench", False), ("moose-rescue", False)])
        self.assertEqual(got, (1, 1))

    def test_no_tailnet_view_claims_nothing(self):
        self._machine("rpi3")
        self._machine("rpi4")
        self.assertEqual(self._run("rpi4", []), (0, 0))


def run(deb, release, out, machine=None):
    """kernel_pin as a process would report it: the tarball on stdout, the refusal on stderr."""
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            got = buildroot.kernel_pin(machine or here(), str(deb), release, str(out))
        except act.Refused as e:
            return subprocess.CompletedProcess(None, e.status, "", err.getvalue())
    return subprocess.CompletedProcess(None, 0, got, err.getvalue())


class TestPrepare(unittest.TestCase):
    """Against a synthetic .deb, so this needs no network and no kernel."""

    RELEASE = "9.9.9+rpt-rpi-v7l"

    def setUp(self):
        if not shutil.which("dpkg-deb") or not shutil.which("depmod"):
            self.skipTest("needs dpkg-deb and depmod (the machine that prepares a pinned kernel has both)")
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-kpin-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.out = self.tmp / "out"

    def _deb(self):
        root = self.tmp / "pkg"
        shutil.rmtree(root, ignore_errors=True)
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN" / "control").write_text(
            f"Package: linux-image-test\nVersion: 1\nArchitecture: armhf\n"
            f"Maintainer: t <t@t>\nDescription: test\n")
        (root / "boot").mkdir(parents=True)
        # A 32-bit ARM zImage is recognised by its magic at offset 36.
        blob = bytearray(b"\0" * 64)
        blob[36:40] = (0x016f2818).to_bytes(4, "little")
        (root / "boot" / f"vmlinuz-{self.RELEASE}").write_bytes(bytes(blob))
        md = root / "lib" / "modules" / self.RELEASE / "kernel" / "drivers" / "net"
        md.mkdir(parents=True)
        ko = md / "brcmfmac.ko"
        ko.write_bytes(b"\x7fELF" + b"\0" * 64)
        subprocess.run(["xz", str(ko)], check=True)
        for f in ("modules.order", "modules.builtin"):
            (root / "lib" / "modules" / self.RELEASE / f).write_text("")
        dd = root / "usr" / "lib" / f"linux-image-{self.RELEASE}"
        (dd / "overlays").mkdir(parents=True)
        (dd / "bcm2711-rpi-4-b.dtb").write_bytes(b"\xd0\x0d\xfe\xed")
        (dd / "overlays" / "vc4-kms-v3d-pi4.dtbo").write_bytes(b"\xd0\x0d\xfe\xed")
        deb = self.tmp / "k.deb"
        subprocess.run(["dpkg-deb", "--build", "--nocheck", str(root), str(deb)],
                       check=True, capture_output=True)
        return deb

    def test_the_prepared_tree_carries_all_four_halves_with_modules_decompressed(self):
        cp = run(self._deb(), self.RELEASE, self.out)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        names = subprocess.run(["tar", "-tf", cp.stdout.strip()], capture_output=True, text=True).stdout
        for want in ("./boot/zImage",
                     f"./lib/modules/{self.RELEASE}/modules.dep",
                     "./dtb/bcm2711-rpi-4-b.dtb",
                     "./dtb/overlays/vc4-kms-v3d-pi4.dtbo"):
            self.assertIn(want, names, f"the prepared tree is missing {want}")
        self.assertIn("brcmfmac.ko", names)
        self.assertNotIn("brcmfmac.ko.xz", names, "a module was left compressed")
        dep = subprocess.run(["tar", "-xOf", cp.stdout.strip(),
                              f"./lib/modules/{self.RELEASE}/modules.dep"],
                             capture_output=True, text=True).stdout
        self.assertIn("brcmfmac.ko", dep)
        self.assertNotIn(".ko.xz", dep, "modules.dep still names the compressed paths")


class TestPrepareOnTheFake(unittest.TestCase):
    """The refusals and the reuse, on any host: the Fake answers as dpkg-deb, depmod and od would."""

    R = "9.9.9"
    DEB, OUT = "/cache/k.deb", "/cache/dl"
    TAR = OUT + "/wk-kernel-9.9.9.tar"

    def world(self, magic="016f2818", modules=True, dep="kernel/brcmfmac.ko:\n"):
        w = Fake("here")
        x = self.TAR + ".work/x"
        tree = self.TAR + ".work/tree"
        w.answer(["sh", "-c"], out="")
        w.answer(["sha256sum", self.DEB], out="ab  k.deb\n")
        w.answer(["od"], out=" %s\n" % magic)
        for p in (["cp"], ["find"], ["tar"], ["mv"]):
            w.answer(p)

        def unpack(argv, f):
            f._set_file(x + "/boot/vmlinuz-" + self.R, "")
            f._set_file(x + "/usr/lib/linux-image-%s/bcm2711-rpi-4-b.dtb" % self.R, "")
            if modules:
                f.dirs.add(x + "/lib/modules/" + self.R)
            return Result(0)
        w.react(["dpkg-deb", "-x"], unpack)
        w.react(["depmod"], lambda a, f: (f._set_file(tree + "/lib/modules/%s/modules.dep" % self.R, dep), Result(0))[-1])
        return w

    def test_a_prepared_tree_is_packed_and_stamped(self):
        w = self.world()
        cp = run(self.DEB, self.R, self.OUT, w)
        self.assertEqual((cp.returncode, cp.stdout), (0, self.TAR), cp.stderr)
        self.assertEqual(w.files[self.TAR + ".from"], "ab")
        self.assertNotIn(self.TAR + ".work", w.dirs)

    def test_an_unchanged_package_is_not_prepared_again(self):
        w = self.world()
        w.files.update({self.TAR: "", self.TAR + ".from": "ab"})
        cp = run(self.DEB, self.R, self.OUT, w)
        self.assertEqual(cp.stdout, self.TAR)
        self.assertFalse([e for e in w.effects if e[0] == "run" and e[1][0] == "dpkg-deb"])

    def test_each_refusal_names_what_is_wrong(self):
        tool_missing = self.world()
        tool_missing.answer(["sh", "-c"], out="depmod\n")
        for world, why in ((self.world(magic="00000000"), "not a 32-bit ARM zImage"),
                           (self.world(modules=False), "no modules"),
                           (self.world(dep="kernel/brcmfmac.ko.xz:\n"), "still names .xz"),
                           (tool_missing, "depmod")):
            with self.subTest(why=why):
                cp = run(self.DEB, self.R, self.OUT, world)
                self.assertNotEqual(cp.returncode, 0)
                self.assertIn(why, cp.stderr)
