"""`wk boot` (lib/wk/boot/cli.py, lib/wk/boot/eeprom.py) against a board in memory: what --status reads back, each
transition, the EEPROM boot order, killpoints[boot], and what `wk status` shows of an armed machine, in text and on
the --web page.

Run: python3 tests/run.py --unit -k test_boot_cmd
"""
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
from tests.test_boot_driver import SLOTS_OF, board, conf_for
from tests.test_mac_volume import mac_board

sys.path.insert(0, str(REPO / "lib"))

from wk import act, status, statusview  # noqa: E402
from wk.boot import drivers  # noqa: E402
from wk.boot import cli, eeprom  # noqa: E402
from wk.boot.driver import part  # noqa: E402
from wk.boot.fake import FakeBoard  # noqa: E402
from wk.machine import Fake, Killed, Result  # noqa: E402

PI_KINDS = ("pi-sd", "pi-tryboot", "rpi5-usb", "pi-mbr")


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
        d.record_write("sys-a", "p", "/dev/sda", d.order_image)
        rc, err = run(b.status)
        self.assertEqual(rc, 2, err)
        self.assertIn("ARMED", err)
        self.assertIn("--disarm", err)

    def test_a_record_the_boot_spent_is_0_and_names_the_clear(self):
        fake, d, b = pi("rpi5-usb")
        run(b.arm)
        d.probe()
        d.reboot()
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
        fake, d = mac_board("mac-volume")
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
        self.assertIn("plain outage", err)
        self.assertNotIn("rule the network out", err)

    def test_every_sibling_quiet_names_the_network(self):
        fake, d = board("pi-sd")
        fake.up = False
        peers = [("rpi4-rescue", "100.0.0.1", "down"), ("rpi5", "100.0.0.2", "down")]
        rc, err = run(boot_over(fake, d, peers).status)
        self.assertEqual(rc, 3, err)
        self.assertIn("rule the network out before the board", err)


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
        fake, d = mac_board("mac-volume")
        rc, err = run(boot_over(fake, d).arm)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("--keep", err)
        self.assertIn("the job it was armed for does", err)

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
        d.record_write("sys-a", "p", "/dev/sda", d.order_image)
        rc, err = run(b.back)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(fake.record)

    def test_disarm_before_the_reboot_boots_the_rescue(self):
        for kind in PI_KINDS:
            with self.subTest(kind=kind):
                fake, d, b = pi(kind)
                d.probe()
                p, ident = d.select_system("")
                d.record_write(ident, "p", "", d.order_image)
                d.arm(p, d.order_image)
                rc, err = run(b.disarm)
                self.assertEqual(rc, 0, err)
                self.assertIsNone(fake.record)
                d.reboot()
                self.assertTrue(fake.on_rescue(), err)

    def test_a_medium_armed_board_is_disarmed_whoever_armed_it(self):
        """The byte on the medium is the arming, so there is something to park with no record."""
        fake, d, b = pi("pi-mbr")
        d.probe()
        d.arm(part(fake.conf["NODE_DEVICE"], 1))
        rc, err = run(b.disarm)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.mbr[fake.conf["NODE_DEVICE"]], "83")

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


class KillBoard(FakeBoard):
    """A FakeBoard whose driving process dies before its `stop_after`-th effect, as tests/killpoints.py asks."""

    stop_after, applied = None, 0

    def call(self, fn, *args, input=None, mutates=False):
        if mutates:
            if self.stop_after is not None and self.applied >= self.stop_after:
                raise Killed(fn)
            self.applied += 1
        return FakeBoard.call(self, fn, *args, input=input, mutates=mutates)


class TestKillpoints(unittest.TestCase):
    """killpoints[boot]: a `wk boot` killed after any effect and run again reaches the state one run does."""

    def world(self, kind, start=None):
        def make():
            conf = conf_for(kind)
            fake = KillBoard(conf)
            fake.rescue("rescue-1" if conf["NODE_ROLE"] == "bench-device" else "")
            for n, ident in zip(SLOTS_OF[kind], ("sys-a",)):
                fake.write_system(part(conf["NODE_DEVICE"], n), ident, failsafe=drivers()[kind].failsafe)
            if start:
                run(getattr(self.boot(fake), start))
            fake.applied = 0
            return types.SimpleNamespace(fake=fake)
        return make

    def boot(self, fake):
        return boot_over(fake, drivers()[fake.conf["NODE_DRIVER"]](REPO, fake.conf, fake))

    def state(self, w):
        f = w.fake
        return (f.running, f.record, json.dumps(f.fat, sort_keys=True), json.dumps(f.mbr, sort_keys=True), f.one_shot)

    def flow(self, action):
        def run_once(w):
            got, err = run(getattr(self.boot(w.fake), action))
            if got != 0 and not err.count("already in bench mode"):
                raise AssertionError(err)
        return run_once

    def test_arm_back_and_disarm_converge_on_every_pi(self):
        for kind in PI_KINDS:
            for action, start in (("arm", None), ("back", "arm"), ("disarm", None)):
                with self.subTest(kind=kind, action=action):
                    converges(self, self.world(kind, start), self.flow(action), self.state)


class EepromTest(unittest.TestCase):
    def rig(self, kind="pi-tryboot", config="BOOT_ORDER=0xf41\nTFTP_IP=10.0.0.1\n", tool=True):
        fake, d = board(kind)
        fake.eeprom, fake.eeprom_tool = config, tool
        return fake, eeprom.BootOrder(d, env={"HOME": "/nonexistent"})

    def order(self, bo, name, env=None):
        return run(bo.run, name, env=dict({"WK_YES": "1"}, **(env or {})))


class TestTheOrderArithmetic(unittest.TestCase):
    def test_the_named_nibble_moves_last_which_is_first(self):
        self.assertEqual(eeprom.first("0xf412", "4"), "0xf14")
        self.assertEqual(eeprom.first("0xf14", "1"), "0xf41")
        self.assertEqual(eeprom.first("0xf41", "4"), "0xf14")

    def test_an_entry_it_does_not_name_keeps_its_place(self):
        self.assertEqual(eeprom.first("0xf641", "4"), "0xf614")

    def test_local_drops_the_network_only(self):
        self.assertEqual(eeprom.without_net("0xf421"), "0xf41")

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
        self.assertNotIn(("r_sudo", "eeprom.sh"), fake.effects)

    def test_a_board_armed_for_a_reboot_is_not_written_under(self):
        """status.armed_transition: a mutating command aimed at an armed machine refuses."""
        fake, bo = self.rig(kind="rpi5-usb")
        bo.d.probe()
        bo.d.record_write("sys-a", "p", "/dev/sda", bo.d.order_image)
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
            for side in fake.sides.values():
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
        side = fake.sides["m_ssh"]
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
    def test_a_name_that_is_no_bench_machine_is_empty(self):
        self.assertEqual(cli.fleet_probe(REPO, "no-such-machine", {"WK_MACHINES_DIR": str(REPO / "machines")}), {})

    def test_every_field_the_status_record_reads_is_answered(self):
        fake, d = board("rpi5-usb")
        with mock.patch.object(cli, "load_conf", return_value=dict(cli.CONF_DEFAULTS, **d.conf)), \
                mock.patch.object(cli, "open_driver", return_value=d), \
                mock.patch.object(cli.reach, "Reach", return_value=types.SimpleNamespace(
                    fleet_line=lambda m: "rpi5 100.0.0.1 (up)", without_tailnet=lambda m: "")):
            fake.channel = "none"
            d.probe()
            d.record_write("sys-a", "p", "/dev/sda", d.order_image)
            fields = cli.fleet_probe(REPO, "rpi5", {})
        rec = status.fleet_record("rpi5", {}, fields, 4)
        self.assertEqual((rec["mode"], rec["armed"], rec["armed_by"]), ("host mode", "sys-a", fields["armed_by"]))
        self.assertTrue(fields["armed_at"] and fields["boot_id"] == fields["armed_boot"] == "boot-1")


class TestStatusArmedTransition(unittest.TestCase):
    """status.armed_transition: an armed machine's line shows the system, who and when; a record its boot spent reads
    desync; the refusal of a mutating command is TestBootOrder's."""

    FIELDS = dict(role="workstation", probeable="yes", mode="host", bridge="", armed="img-1", media="usb",
                  reprovision="", tailnet="", direct="", armed_by="tolken", armed_at="2026-01-01T00:00:00Z")

    def text(self, rec):
        doc = statusview.merge([rec])
        return statusview.render_text(doc, False)

    def test_the_line_names_the_system_who_and_when(self):
        rec = status.fleet_record("rpi5", {}, dict(self.FIELDS, armed_boot="a", boot_id="a"), 4,
                                  clock=types.SimpleNamespace(now=lambda: 1767225600))
        out = self.text(rec)
        self.assertIn("armed for img-1 by tolken since 2026-01-01T00:00:00Z", out)
        self.assertNotIn("desync", out)

    def test_a_spent_record_reads_desync(self):
        rec = status.fleet_record("rpi5", {}, dict(self.FIELDS, armed_boot="before", boot_id="after"), 4)
        self.assertIn("desync", self.text(rec))


class TestStatusWebMirrorsText(unittest.TestCase):
    """status.web_mirrors_text: the --web page reads every field the text renderer shows of an armed machine and of a
    workspace two views disagree about."""

    def test_the_page_reads_what_the_text_shows(self):
        page = statusview.page({"machines": [], "fleet": [], "bridges": []}, False)
        for field in ("f.armed_by", "f.armed_at", "f.armed_desync", "w.disagree"):
            with self.subTest(field=field):
                self.assertIn(field, page)


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
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("usage: wk boot", cp.stdout)

    def test_the_boot_order_is_the_one_destructive_action(self):
        from wk import decl
        d = decl.Decl(REPO / "cmd" / "boot")
        self.assertTrue(d.is_destructive(["rpi4", "--boot-order", "sd-first"]))
        self.assertFalse(d.is_destructive(["rpi4", "--back"]))


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
        with mock.patch("wk.targets.read_conf", return_value={"name": "ws"}):
            rc, err = run(cli.broker, str(REPO), "rpi4", "boot-order", "", {"WK_BROKER_SOCKET": "/s"}, here)
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertEqual(here.effects, [])

    def test_no_socket_is_refused_naming_the_stage(self):
        rc, err = run(cli.broker, str(REPO), "rpi4", "arm", "", {"WK_BROKER_SOCKET": "/s"}, self.world(socket=False))
        self.assertEqual(rc, (act.Refused, 1), err)
        self.assertIn("./setup --stage broker", err)


if __name__ == "__main__":
    unittest.main()
