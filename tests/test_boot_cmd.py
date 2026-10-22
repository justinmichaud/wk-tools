"""`wk boot` (lib/wk/boot/cli.py, lib/wk/boot/eeprom.py) against a board in memory: what --status reads back, each
transition, the EEPROM boot order, killpoints[boot], and what `wk doctor` reads of an armed machine."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WkTest, requires_machine, run as wk_run
from tests.fake_boot import board

sys.path.insert(0, str(REPO / "lib"))

from wk import act, doctor  # noqa: E402
from wk.boot import cli, eeprom  # noqa: E402
from tests.fakes import FakeClock  # noqa: E402
from wk.machine import Result  # noqa: E402
from tests.fakes import Fake  # noqa: E402

PI_KINDS = ("pi-sd", "pi-tryboot", "rpi5-usb")


def run(fn, *args, env=None):
    """(return value or act.Refused, stderr), with this process's environment restored afterwards."""
    with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args), err.getvalue()
        except act.Refused as e:
            return (act.Refused, e.status), err.getvalue()


def boot_over(fake, d, peers=()):
    conf = dict(cli.CONF_DEFAULTS, **d.conf)
    return cli.Boot(REPO, conf, d, env={"HOME": "/nonexistent", "WK_MACHINES_DIR": str(REPO / "machines")},
                    peers=types.SimpleNamespace(peers=lambda: list(peers)))


def pi(kind, ids=("sys-a",)):
    fake, d = board(kind, ids)
    return fake, d, boot_over(fake, d)


class TestStatus(unittest.TestCase):
    def test_host_mode_with_no_record_is_0(self):
        _, _, b = pi("rpi5-usb")
        rc, err = run(b.status)
        self.assertEqual(rc, 0, err)
        self.assertIn("host mode", err)

    def test_armed_and_not_yet_rebooted_is_2(self):
        fake, d, b = pi("rpi5-usb")
        d.probe()
        d.record_write("sys-a")
        rc, err = run(b.status)
        self.assertEqual(rc, 2, err)
        self.assertIn("ARMED", err)
        self.assertIn("--disarm", err)

    def test_a_record_the_boot_spent_is_0_and_names_the_clear(self):
        fake, d, b = pi("rpi5-usb")
        run(b.arm)
        run(b.back)
        fake.record = "image=sys-a\narmed_boot_id=boot-1\n"
        rc, err = run(b.status)
        self.assertEqual(rc, 0, err)
        self.assertIn("a spent arming record remains", err)

    def test_a_record_with_no_boot_id_is_unknown_and_reads_as_armed_whatever_the_clocks_say(self):
        fake, d, b = pi("rpi5-usb")
        d.probe()
        fake.record = "image=sys-a\narmed_by=x\narmed_at=2000-01-01T00:00:00Z\n"
        rc, err = run(b.status)
        self.assertEqual(rc, 2, err)
        self.assertIsNone(b.spent)
        self.assertIn("--disarm", err)

    def test_a_one_shot_in_bench_mode_is_returned_by_a_reboot(self):
        fake, d, b = pi("rpi5-usb")
        run(b.arm)
        rc, err = run(b.status)
        self.assertEqual(rc, 0, err)
        self.assertIn("bench mode -- system sys-a", err)
        self.assertIn("a plain reboot returns it to host mode", err)

    def test_a_sticky_firmware_default_is_returned_by_its_job(self):
        """The Mac's next boot enters what the evidence line names; a reboot does not undo it."""
        fake, d = board("mac-volume")
        fake.enter_bench()
        rc, err = run(boot_over(fake, d).status)
        self.assertEqual(rc, 0, err)
        self.assertIn("hands the machine back when it ends", err)
        self.assertNotIn("a plain reboot returns it", err)

    def test_unreachable_is_3(self):
        fake, d, b = pi("pi-sd")
        fake.up = False
        rc, err = run(b.status)
        self.assertEqual(rc, 3, err)
        self.assertIn("an outage", err)
        self.assertNotIn("rule the network out", err)

    def test_every_sibling_quiet_names_the_network(self):
        fake, d = board("pi-sd")
        fake.up = False
        peers = [("rpi4-rescue", "100.0.0.1", "down"), ("rpi5", "100.0.0.2", "down")]
        rc, err = run(boot_over(fake, d, peers).status)
        self.assertEqual(rc, 3, err)
        self.assertIn("rule the network out", err)


class TestArm(unittest.TestCase):
    def test_each_pi_boots_the_selected_system_and_names_its_watchdog(self):
        for kind in PI_KINDS:
            with self.subTest(kind=kind):
                fake, d, b = pi(kind)
                rc, err = run(b.arm)
                self.assertEqual(rc, 0, err)
                self.assertEqual(d.probe(), "bench sys-a")
                self.assertRegex(err, r"returns by itself in \d+s")
                self.assertIn("--keep", err)

    def test_a_second_system_is_named_by_id(self):
        fake, d, b = pi("pi-sd", ids=("sys-a", "sys-b"))
        rc, err = run(b.arm, "sys-b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(d.probe(), "bench sys-b")

    def test_a_mac_arming_writes_no_watchdog_and_offers_none(self):
        fake, d = board("mac-volume")
        rc, err = run(boot_over(fake, d).arm)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("--keep", err)
        self.assertIn("the job it was armed for returns it", err)

    def test_the_dry_run_arms_nothing(self):
        fake, d, b = pi("pi-sd")
        rc, err = run(b.arm, env={"WK_DRY_RUN": "1"})
        self.assertEqual(rc, 0, err)
        self.assertIn("would arm", err)
        self.assertEqual(fake.effects, [])
        self.assertIsNone(fake.record)
        self.assertTrue(fake.on_rescue())

    def test_an_unreachable_board_is_refused(self):
        fake, d, b = pi("pi-sd")
        fake.up = False
        rc, err = run(b.arm)
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertEqual(fake.effects, [])

    def test_a_register_armed_board_in_bench_mode_goes_back_first(self):
        fake, d, b = pi("rpi5-usb")
        run(b.arm)
        rc, err = run(b.arm)
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("--back", err)


class TestKeep(unittest.TestCase):
    def test_a_system_carrying_the_watchdog_is_claimed(self):
        """Asked of the running system, not its driver: rpi5-usb has no self-disarm and its cards carry the watchdog."""
        fake, d, b = pi("rpi5-usb")
        run(b.arm)
        rc, err = run(b.keep)
        self.assertEqual(rc, 0, err)
        self.assertTrue(fake.kept)

    def test_a_system_carrying_none_has_nothing_to_claim(self):
        fake, d, b = pi("pi-sd")
        run(b.arm)
        fake.roots[fake.running]["watchdog"] = False
        rc, err = run(b.keep)
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("no self-return watchdog", err)
        self.assertFalse(fake.kept)

    def test_host_mode_has_no_watchdog_to_cancel(self):
        fake, d, b = pi("pi-sd")
        rc, err = run(b.keep)
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("not in bench mode", err)


class TestBackAndDisarm(unittest.TestCase):
    def test_back_returns_the_board_to_its_rescue(self):
        for kind in PI_KINDS:
            with self.subTest(kind=kind):
                fake, d, b = pi(kind)
                run(b.arm)
                rc, err = run(b.back)
                self.assertEqual(rc, 0, err)
                self.assertTrue(fake.on_rescue(), err)

    def test_back_from_host_mode_clears_the_record_first(self):
        fake, d, b = pi("rpi5-usb")
        d.probe()
        d.record_write("sys-a")
        rc, err = run(b.back)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(fake.record)

    def test_disarm_before_the_reboot_boots_the_rescue(self):
        for kind in PI_KINDS:
            with self.subTest(kind=kind):
                fake, d, b = pi(kind)
                d.probe()
                for _, step in d.arm_sequence(*d.select_system(""))[:-1]:
                    step()
                rc, err = run(b.disarm)
                self.assertEqual(rc, 0, err)
                self.assertIsNone(fake.record)
                d.reboot()
                self.assertTrue(fake.on_rescue(), err)

    def test_a_one_shot_board_with_no_record_has_nothing_to_disarm(self):
        fake, d, b = pi("rpi5-usb")
        rc, err = run(b.disarm)
        self.assertEqual(rc, 0, err)
        self.assertIn("no arming record", err)
        self.assertEqual(fake.effects, [])


class TestDiag(unittest.TestCase):
    def test_it_is_read_from_host_mode_only(self):
        fake, d, b = pi("rpi5-usb")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc, err = run(b.diag)
        self.assertEqual(rc, 0, err)
        self.assertIn("== sys-a", out.getvalue())
        run(b.arm)
        rc, err = run(b.diag)
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("--back", err)


class TestKillpoints(unittest.TestCase):
    """killpoints[boot]: a `wk boot` killed after any effect and run again reaches the state one run does."""

    def world(self, kind, start=None):
        def make():
            fake, d = board(kind)
            if start:
                run(getattr(boot_over(fake, d), start))
            fake.applied = 0
            return types.SimpleNamespace(fake=fake, d=d)
        return make

    def state(self, w):
        f = w.fake
        return (f.running, f.record, json.dumps(f.fat, sort_keys=True), f.one_shot)

    def flow(self, action):
        def run_once(w):
            got, err = run(getattr(boot_over(w.fake, w.d), action))
            if got != 0 and not err.count("already in bench mode"):
                raise AssertionError(err)
        return run_once

    def test_arm_back_and_disarm_converge_on_every_pi(self):
        for kind in PI_KINDS:
            for action, start in (("arm", None), ("back", "arm"), ("disarm", None)):
                with self.subTest(kind=kind, action=action):
                    converges(self, self.world(kind, start), self.flow(action), self.state)


class TestEnter(unittest.TestCase):
    """Boot.enter, the one retry rule (a board A/B's legs): the machine running the system its own marker names, or a
    False after the last arming is read."""

    def world(self, kind="rpi5-usb", running="sys-a", from_bench=False):
        fake, d = board(kind, ("sys-a", "sys-b"))
        d.arm_from_bench = from_bench
        if running:
            run(boot_over(fake, d).arm, running)
        fake.effects.clear()
        self.landed, self.misroute, restart = [], 0, fake.restart

        def logged(tryboot=False):
            r = restart(tryboot)
            if self.misroute and fake.running == "/dev/sda4":
                self.misroute -= 1
                fake.running = "/dev/sda2"
            self.landed.append(fake.running)
            return r
        fake.restart, self.fake, self.clock = logged, fake, FakeClock()
        return boot_over(fake, d)

    def enter(self, b, want):
        return run(b.enter, want, self.clock)

    def armings(self):
        return sum(1 for e in self.fake.effects if e[:2] == ("card", "autoboot"))

    def test_the_machine_reaches_the_system_from_wherever_it_stands(self):
        """On it already, armed where it stands, back to its rescue first, from the rescue, or armed again after a wrong landing."""
        for from_bench, running, misroute, want, landed, armings in (
                (False, "sys-a", 0, "sys-a", [], 0), (True, "sys-a", 0, "sys-b", ["/dev/sda4"], 1),
                (False, "sys-a", 0, "sys-b", ["rescue", "/dev/sda4"], 1), (False, "", 0, "sys-b", ["/dev/sda4"], 1),
                (True, "sys-a", 2, "sys-b", ["/dev/sda2", "/dev/sda2", "/dev/sda4"], 3)):
            with self.subTest(from_bench=from_bench, running=running, misroute=misroute, want=want):
                b = self.world(running=running, from_bench=from_bench)
                self.misroute = misroute
                ok, err = self.enter(b, want)
                self.assertTrue(ok, err)
                self.assertEqual(self.landed, [self.fake.conf["root"] if x == "rescue" else x for x in landed])
                self.assertEqual(self.armings(), armings)

    def test_a_failed_back_is_retried_not_ignored(self):
        b = self.world()
        back, calls = b.back, []
        b.back = lambda: calls.append(1) or (act.die("no") if len(calls) == 1 else back())
        ok, err = self.enter(b, "sys-b")
        self.assertTrue(ok, err)
        self.assertIn("could not send", err)
        self.assertEqual((self.landed, len(calls)), ([self.fake.conf["root"], "/dev/sda4"], 2))

    def test_the_last_arming_is_read_before_it_gives_up_and_a_never_landing_one_is_bounded(self):
        for misroute, ok in ((cli.TRIES - 1, True), (99, False)):
            with self.subTest(misroute=misroute):
                b = self.world(from_bench=True)
                self.misroute = misroute
                self.assertEqual(self.enter(b, "sys-b")[0], ok)
                self.assertEqual(self.armings(), cli.TRIES)
                self.assertFalse(self.fake.kept)

    def test_an_arming_that_will_not_take_is_retried_after_a_pause_and_bounded(self):
        b = self.world(from_bench=True)
        self.fake.stuck = True
        self.assertFalse(self.enter(b, "sys-b")[0])
        self.assertEqual((self.clock.slept.count(cli.ARM_RETRY), self.landed), (cli.TRIES, []))

    def test_a_machine_between_systems_is_waited_for_not_counted(self):
        b = self.world()
        back, answer = self.clock.now() + 100, self.fake.reply
        self.fake.reply = lambda side, argv, input=None: (answer(side, argv, input) if self.clock.now() >= back
                                                           else Result(255, "", "ssh: connect: no route"))
        ok, err = self.enter(b, "sys-a")
        self.assertTrue(ok, err)
        self.assertIn("not answering yet", err)
        self.assertEqual((self.landed, self.armings()), ([], 0))
        self.assertIn(cli.POLL, self.clock.slept)

    def test_a_dry_run_arms_nothing(self):
        for from_bench, said in ((True, "would arm rpi5"), (False, "would reboot rpi5 back")):
            with self.subTest(from_bench=from_bench):
                b = self.world(from_bench=from_bench)
                ok, err = run(b.enter, "sys-b", self.clock, env={"WK_DRY_RUN": "1"})
                self.assertFalse(ok)
                self.assertIn(said, err)
                self.assertEqual((self.landed, self.armings()), ([], 0))

    def test_release_hands_it_back_with_its_record_cleared(self):
        b = self.world()
        ok, err = run(b.release, self.clock)
        self.assertTrue(self.fake.on_rescue(), err)
        self.assertIsNone(self.fake.record)


class EepromTest(unittest.TestCase):
    def rig(self, kind="pi-tryboot", config="BOOT_ORDER=0xf41\nTFTP_IP=10.0.0.1\n", tool=True):
        fake, d = board(kind)
        fake.eeprom, fake.eeprom_tool = config, tool
        return fake, eeprom.BootOrder(d, env={"HOME": "/nonexistent"})

    def order(self, bo, name, env=None):
        return run(bo.run, name, env=dict({"WK_YES": "1"}, **(env or {})))


class TestTheOrderArithmetic(unittest.TestCase):
    def test_the_named_nibble_moves_last_which_is_first_and_the_rest_keep_their_place(self):
        for order, nibble, want in (("0xf412", "4", "0xf14"), ("0xf14", "1", "0xf41"), ("0xf41", "4", "0xf14"),
                                    ("0xf641", "4", "0xf614"), ("0xf421", "", "0xf41")):
            with self.subTest(order=order, nibble=nibble):
                self.assertEqual(eeprom.first(order, nibble), want)

    def test_the_network_settings_go_and_a_missing_order_is_added(self):
        self.assertEqual(eeprom.reorder("TFTP_IP=1.2.3.4\nX=1\nBOOT_ORDER=0xf41", "usb-first"), "X=1\nBOOT_ORDER=0xf14")
        self.assertEqual(eeprom.reorder("X=1", "sd-first"), "X=1\nBOOT_ORDER=0xf41")


class TestBootOrder(EepromTest):
    def test_an_unknown_order_is_refused_naming_all_three(self):
        _, bo = self.rig()
        rc, err = self.order(bo, "bogus")
        self.assertEqual(rc, (act.Refused, 1), err)
        for name in eeprom.ORDERS:
            self.assertIn(name, err)

    def test_it_writes_the_new_order_in_place_and_names_the_undo(self):
        fake, bo = self.rig()
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.eeprom, "BOOT_ORDER=0xf14\n")
        self.assertIn("--boot-order local", err)

    def test_without_a_yes_nothing_is_written(self):
        fake, bo = self.rig()
        with mock.patch.dict(os.environ, {"WK_YES": ""}):
            rc, err = run(bo.run, "usb-first")
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("BOOT_ORDER=0xf41", fake.eeprom)

    def test_the_dry_run_shows_the_change_and_writes_nothing(self):
        fake, bo = self.rig()
        rc, err = self.order(bo, "usb-first", env={"WK_DRY_RUN": "1"})
        self.assertEqual(rc, 0, err)
        self.assertIn("+BOOT_ORDER=0xf14", err)
        self.assertIn("BOOT_ORDER=0xf41", fake.eeprom)

    def test_an_order_already_set_writes_nothing(self):
        fake, bo = self.rig(config="BOOT_ORDER=0xf41\n")
        rc, err = self.order(bo, "sd-first")
        self.assertEqual(rc, 0, err)
        self.assertIn("nothing to write", err)
        self.assertEqual(fake.effects, [])

    def test_a_board_armed_for_a_reboot_is_not_written_under(self):
        """status.armed_transition: a mutating command aimed at an armed machine refuses."""
        fake, bo = self.rig(kind="rpi5-usb")
        bo.d.probe()
        bo.d.record_write("sys-a")
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("is armed for system 'sys-a'", err)
        self.assertIn("BOOT_ORDER=0xf41", fake.eeprom)

    def test_an_unreachable_board_is_refused(self):
        fake, bo = self.rig()
        fake.up = False
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("cannot ssh", err)


class TestTheRecoveryPath(EepromTest):
    """A system with no rpi-eeprom-config: the pinned bootloader, carrying the new configuration, is staged for
    recovery.bin, which is copied last because it is the trigger."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="wk-test-eeprom-")
        self.addCleanup(shutil.rmtree, self.store, True)
        cache = os.path.join(self.store, "rpi-eeprom", eeprom.COMMIT)
        os.makedirs(cache)
        pins = []
        for name, path, _ in eeprom.PINS:
            with open(os.path.join(cache, name), "wb") as f:
                f.write(name.encode())
            pins.append((name, path, hashlib.sha256(name.encode()).hexdigest()))
        patch = mock.patch.object(eeprom, "PINS", tuple(pins))
        patch.start()
        self.addCleanup(patch.stop)

    def rig(self, corrupt=False, **kw):
        fake, bo = EepromTest.rig(self, tool=False, **kw)
        bo.env = {"WK_STORE": self.store}
        here = Fake("here")

        def config(argv, _):
            out = argv[argv.index("--out") + 1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(b"upd")
            return Result(0)
        here.react(["python3"], config)
        bo.here = here
        if corrupt:
            def mangled(src, dest):
                with open(src, "rb") as f:
                    fake.bootfs[dest] = b"x" + f.read()
            for side in fake.machines.values():
                side.copy_in = mangled
        return fake, bo

    def copied(self, fake):
        return [os.path.basename(p) for p in fake.bootfs]

    def test_it_stages_the_update_and_copies_the_trigger_last(self):
        fake, bo = self.rig()
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.copied(fake), ["pieeprom.upd", "pieeprom.sig", "recovery.bin"])
        self.assertIn(hashlib.sha256(b"upd").hexdigest(), fake.bootfs["/boot/pieeprom.sig"].decode())

    def test_a_dry_copy_to_the_board_records_it_and_lands_nothing(self):
        fake, bo = self.rig()
        side = fake.machines["host"]
        src = os.path.join(self.store, "rpi-eeprom", eeprom.COMMIT, "recovery.bin")
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            side.copy_in(src, "/boot/recovery.bin")
            side.write("/boot/pieeprom.sig", "sig")
        self.assertEqual(self.copied(fake), [])
        self.assertIn(("copy_in", src, "/boot/recovery.bin"), side.effects)
        self.assertFalse(bo.eeprom("sum", WK_PATH="/boot/recovery.bin").ok)

    def test_the_local_staging_is_removed_when_it_ends(self):
        fake, bo = self.rig()
        self.order(bo, "usb-first")
        work = os.path.join(self.store, "rpi-eeprom", "stage-" + bo.name)
        self.assertEqual([e for e in bo.here.effects if e[0] == "remove"][-1], ("remove", work))

    def test_a_pin_that_mismatches_is_removed_and_refused(self):
        fake, bo = self.rig()
        bad = os.path.join(self.store, "rpi-eeprom", eeprom.COMMIT, "recovery.bin")
        with open(bad, "wb") as f:
            f.write(b"stale")
        bo.here.answer(["curl"], 0)
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn(("remove", bad), bo.here.effects)
        self.assertEqual(self.copied(fake), [])

    def test_a_copy_that_did_not_survive_never_arms_the_trigger(self):
        fake, bo = self.rig(corrupt=True)
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertNotIn("recovery.bin", self.copied(fake))

    def test_another_soc_is_refused_before_anything_is_read(self):
        fake, bo = self.rig()
        fake.soc = "raspberrypi,5-model-b\nbrcm,bcm2712\n"
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("not a BCM2711", err)
        self.assertEqual(self.copied(fake), [])

    def test_a_system_without_vcgencmd_names_the_rescue(self):
        fake, bo = self.rig()
        fake.vc = False
        rc, err = self.order(bo, "usb-first")
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("--back", err)


class TestFleetProbe(unittest.TestCase):
    def test_a_conf_that_does_not_parse_is_refused_by_its_line_not_taken_for_no_bench_machine(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "x.conf"), "w") as f:
                f.write("kind=board\nnot a line\n")
            with self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()) as err:
                cli.load_conf(REPO, "x", {"HOME": "/nonexistent", "WK_MACHINES_DIR": d})
        self.assertIn("x.conf:2:", err.getvalue())

    def test_a_name_that_is_no_bench_machine_is_empty(self):
        self.assertEqual(cli.fleet_probe(REPO, "no-such-machine", {"WK_MACHINES_DIR": str(REPO / "machines")}), {})

    def test_every_field_the_status_record_reads_is_answered(self):
        fake, d = board("rpi5-usb")
        with mock.patch.object(cli, "load_conf", return_value=dict(cli.CONF_DEFAULTS, **d.conf)), \
                mock.patch.object(cli, "open_driver", return_value=d), \
                mock.patch.object(cli.reach, "Reach", return_value=types.SimpleNamespace(
                    fleet_line=lambda m: "rpi5 100.0.0.1 (up)", without_tailnet=lambda m: "")):
            d.probe()
            d.record_write("sys-a")
            fields = cli.fleet_probe(REPO, "rpi5", {})
        self.assertEqual((doctor.fleet_mode(fields["probeable"], fields["mode"]), fields["armed"]), ("host mode", "sys-a"))
        self.assertTrue(fields["armed_by"])
        self.assertTrue(fields["armed_at"] and fields["boot_id"] == fields["armed_boot"] == "boot-1")


class TestAnArmIsDesyncOnceSpentOrStale(unittest.TestCase):
    """doctor.armed_desync: a record its boot spent, or one unconsumed past the stale window, is desync."""

    def test_spent_stale_and_current(self):
        now = types.SimpleNamespace(now=lambda: 1767225600)
        at = dict(armed="img-1", armed_at="2026-01-01T00:00:00Z")
        self.assertFalse(doctor.armed_desync(dict(at, armed_boot="a", boot_id="a"), now))
        self.assertTrue(doctor.armed_desync(dict(at, armed_boot="before", boot_id="after"), now))
        self.assertTrue(doctor.armed_desync(dict(at, armed_at="2025-12-31T00:00:00Z"), now))


class TestTheCommand(WkTest):
    """cmd/boot through the dispatcher: the declaration and the refusals that need no board."""

    def test_list_names_every_bench_machine(self):
        cp = self.run_wk("boot", "--list", timeout=15)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(sorted(l.split()[0] for l in cp.stdout.splitlines() if l.strip()),
                         sorted(b.stem for b in (REPO / "machines").glob("*.conf")
                                if cli.load_conf(REPO, b.stem, {"WK_MACHINES_DIR": str(REPO / "machines")})))

    def test_an_unknown_machine_is_refused_by_name(self):
        cp = self.run_wk("boot", "no-such-machine", "--status", timeout=15)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("unknown machine 'no-such-machine'", cp.stdout)

    def test_two_actions_are_a_usage_error(self):
        cp = self.run_wk("boot", "rpi4", "--status", "--back", timeout=15)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("usage: wk boot", cp.stdout)

    def test_the_boot_order_is_the_one_destructive_action(self):
        from wk import decl
        from tests.support import load_cmd
        table = load_cmd("boot").COMMAND
        self.assertTrue(decl.parse("boot", table, ["rpi4", "--boot-order", "sd-first"])[0].destructive)
        self.assertFalse(decl.parse("boot", table, ["rpi4", "--back"])[0].destructive)


class TestOnTheBoards(unittest.TestCase):
    """The live bodies of boot[rpi3|rpi4|rpi5]: each board read back through its own driver, from the workstation that
    drives it. Arming one reboots it, which the live tier never does, so the transitions are read, not made."""

    def status(self, name):
        cp = wk_run("boot", name, "--status", timeout=120)
        self.assertIn(cp.returncode, (0, 2), cp.stdout)
        self.assertRegex(cp.stdout, r"%s: (.*host mode|bench mode|base image)" % name)

    @requires_machine("rpi3-rescue")
    def test_boot_rpi3(self):
        self.status("rpi3")

    @requires_machine("rpi4-rescue")
    def test_boot_rpi4(self):
        self.status("rpi4")

    @requires_machine("rpi5")
    def test_boot_rpi5(self):
        self.status("rpi5")


class TestBroker(unittest.TestCase):
    """A sandboxed `wk boot`: one probe of the socket, in broker_request, and an action the broker does not serve is
    refused before anything is probed."""

    def world(self, socket=True):
        here = Fake("here")
        here.answer(["test", "-S"], 0 if socket else 1)
        here.files[str(REPO / "container" / "broker" / "wk-broker-client.py")] = ""
        here.answer(["env"], 0)
        return here

    def test_a_served_action_probes_the_socket_once(self):
        here = self.world()
        rc, err = run(cli.broker, str(REPO), "rpi4", "status", "", {"WK_BROKER_SOCKET": "/s"}, here)
        self.assertEqual(rc, 0, err)
        self.assertEqual([e for e in here.effects if e[1][:2] == ("test", "-S")], [("run", ("test", "-S", "/s"))])

    def test_an_unserved_action_is_refused_without_a_probe(self):
        here = self.world()
        with mock.patch("wk.boot.cli.kv_file", return_value={"name": "ws"}):
            rc, err = run(cli.broker, str(REPO), "rpi4", "boot-order", "", {"WK_BROKER_SOCKET": "/s"}, here)
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertEqual(here.effects, [])

    def test_no_socket_is_refused_naming_the_stage(self):
        rc, err = run(cli.broker, str(REPO), "rpi4", "arm", "", {"WK_BROKER_SOCKET": "/s"}, self.world(socket=False))
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("./setup --stage broker", err)


if __name__ == "__main__":
    unittest.main()
