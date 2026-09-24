"""The Mac boot drivers (lib/wk/boot/mac.py) against a Mac in memory: FakeMac holds the two installs, the firmware's
boot-volume and the boot helper; FakeGuest a Tart guest. MacConformance is machine.conformance's body where a Mac
differs from a board, and tests/test_boot_driver.py runs it; the rest here is mac-volume's own behaviour.

Nothing here touches a real startup disk, bless, the privileged helpers or a guest.

Run: python3 tests/run.py --unit -k test_mac_volume
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import unittest

from tests.support import REPO, bash, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))

from wk import act, fleet  # noqa: E402
from wk.boot import __main__ as boot_main  # noqa: E402
from wk.boot import mac  # noqa: E402
from wk.boot.mac import DRIVERS, HELPER, Channel, Script  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

MACHINES = {"mac-volume": "mbp", "mac-guest": "benchvm"}
HOST_GROUP = "1981BBBF-8B67-4DED-A3E5-41A2550DE0FB"
BENCH_GROUP = "73C12614-1130-40DF-B9B9-9CA73D10F3AA"


def conf_for(kind):
    conf = fleet.Fleet(REPO, {"HOME": "/nonexistent"}).load(MACHINES[kind])
    conf = {k: v for k, v in conf.items() if k.startswith("NODE_")}
    conf.update(NODE_NAME=MACHINES[kind], NODE_DRIVER=kind)
    return conf


def quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args), err.getvalue()
        except act.Refused:
            return act.Refused, err.getvalue()


class FakeMac:
    """The host install on NODE_SSH, the bench install on NODE_BENCH_SSH, one of them running; `firmware` is the
    install the next boot enters, and only the helper moves it. `stuck` is a bless that exits 0 and changes nothing."""

    def __init__(self, conf, env=None, clock=None):
        self.conf, self.clock = dict(conf), clock or FakeClock()
        self.env = env if env is not None else {"HOME": "/nonexistent", "WK_STORE": "/nonexistent/store"}
        self.vol = "/Volumes/" + self.conf["NODE_VOLUME"]
        self.roots = {"host": {"id": "", "group": HOST_GROUP, "name": "Macintosh HD"},
                      "bench": {"id": "", "group": BENCH_GROUP, "name": self.conf["NODE_VOLUME"]}}
        self.running = self.firmware = "host"
        self.channel, self.up, self.stuck, self.attached, self.data = "none", True, False, True, True
        self.helper = "current"
        self.said, self.files, self.record, self.effects, self.asked = {}, {}, None, [], []
        self.boots, self.booted = 1, int(self.clock.now())

    def write_system(self, ident, failsafe=None):
        self.roots["bench"]["id"] = ident

    def on_rescue(self):
        return self.running == "host"

    def enter_bench(self):
        self.firmware = "bench"
        self.reboot()

    def leave_bench(self):
        """The bench install's own job ending (bench/mac-bench-autorun.sh leave_bench): the host blessed back, a reboot."""
        self.firmware = "host"
        self.reboot()

    def reboot(self):
        self.running, self.boots = self.firmware, self.boots + 1
        self.clock.sleep(40)
        self.booted = int(self.clock.now())
        return Result(0)

    # -- the channel
    def dest(self, fn):
        return Channel(self.conf, self.env).dest(fn)

    def here(self):
        return False

    def push(self, src, dest):
        self.effects.append(("push", os.path.basename(dest)))
        if src.endswith(".tar"):
            with tarfile.open(src) as t:
                self.effects.append(("tar", tuple(sorted(t.getnames()))))

    def exec_argv(self, cmd):
        return ["ssh", self.conf["NODE_SSH"], cmd]

    def call(self, fn, *args, input=None, mutates=False):
        ob = args[0]
        self.asked.append((fn, ob.name, dict(ob.params)))
        if mutates:
            self.effects.append((fn, ob.name, ob.params.get("WK_VERB") or ob.params.get("WK_DO") or ""))
        if fn == "r_ssh":
            fn = {"host": "m_ssh", "bench": "i_ssh"}.get(self.channel, "")
        side = {"m_ssh": "host", "i_ssh": "bench"}.get(fn)
        if not side or not self.up or self.running != side or (side == "bench" and not self.dest("i_ssh")):
            return Result(255, "", "ssh: connect: no route")
        return self.script(ob.name, ob.params, input)

    def script(self, name, p, input):
        me = self.roots[self.running]
        if name == "mac-probe.sh":
            return Result(0, ("%s\n" % me["id"] if me["id"] else "") + "READY\n")
        if name == "mac-boottime.sh":
            return Result(0, "{ sec = %d, usec = 451078 } Sat Aug 15 04:12:16 2026\n" % self.booted)
        if name == "mac-fact.sh":
            self.fact_input = input
            out = self.fact(p["WK_FACT"], p["WK_ARG"])
            return Result(0 if out else 1, out + "\n" if out else "")
        if name == "mac-test.sh":
            dirs = {self.vol + "/System/Library/CoreServices"} | ({self.vol + " - Data"} if self.data else set())
            hit = p["WK_PATH"] in dirs and self.attached if p["WK_TEST"] == "-d" else p["WK_PATH"] == HELPER and bool(self.helper)
            return Result(0 if hit else 1)
        if name == "mac-read.sh":
            if p["WK_PATH"] == "/etc/wk-image" or p["WK_PATH"] == self.vol + "/etc/wk-image":
                owner = me if p["WK_PATH"] == "/etc/wk-image" else self.roots["bench"]
                return Result(0, "id=%s\n" % owner["id"] if owner["id"] else "")
            return Result(0, self.files.get(p["WK_PATH"], ""))
        if name == "mac-priv.sh":
            return self.priv(p["WK_VERB"])
        if name.startswith("record-"):
            if name == "record-write.sh":
                self.record = input + "armed_at=%s\n" % self.clock.iso()
            elif name == "record-clear.sh":
                self.record = None
            return Result(0, self.record or "")
        if name == "mac-put.sh":
            return Result(0)
        return Result(127, "", "%s: not an on-board script this Mac knows" % name)

    def fact(self, what, arg):
        if what == "boot-volume":
            return "EF57347C-0000-AA11-AA11-00306543ECAC:D7E4E11B:" + self.roots[self.firmware]["group"]
        if what == "volume-name" and arg == "/":
            return self.roots[self.running]["name"]
        if what == "volume-group":
            if arg == "/":
                return self.roots[self.running]["group"]
            if arg == self.vol and self.running == "host" and self.attached:
                return BENCH_GROUP
        return ""

    def priv(self, verb):
        if not self.helper:
            return Result(1, "sudo: a password is required\n")
        if verb in self.said:
            return Result(*self.said[verb])
        if verb == "status":
            return Result(0, "wk-boot-priv: ok\n" + ("wk-boot-priv: detach=nohup\n" if self.helper == "current" else ""))
        if verb == "boot-host":
            if not self.stuck:
                self.firmware = self.running
            return Result(0, "wk-boot-priv: blessed /\n")
        if verb == "boot-volume":
            if not self.stuck:
                self.firmware = "bench"
            return Result(0, "wk-boot-priv: blessed %s\n" % self.vol)
        if verb == "reboot":
            return self.reboot()
        return Result(2, "wk-boot-priv: usage\n")


class FakeGuest:
    """A Tart guest: absent, stopped or running, carrying the marker or not. `stuck` starts it with no marker."""

    def __init__(self, conf, env=None, clock=None):
        self.conf, self.clock = dict(conf), clock or FakeClock()
        self.env = env if env is not None else {"HOME": "/nonexistent"}
        self.ws = self.env.get("WK_BENCH_GUEST") or "wk-bench"
        self.roots = {"guest": {"id": ""}}
        self.running, self.st, self.marked = "guest", "stopped", True
        self.channel, self.up, self.stuck, self.tart = "none", True, False, True
        self.record, self.effects, self.asked, self.files = None, [], [], {}
        self.boots, self.booted = 1, int(self.clock.now())

    def write_system(self, ident, failsafe=None):
        self.roots["guest"]["id"] = ident

    def on_rescue(self):
        return not (self.st == "running" and self.marked)

    def enter_bench(self):
        self.start()

    def leave_bench(self):
        self.stop()

    def state(self):
        return self.st

    def start(self):
        self.effects.append(("start",))
        self.st, self.marked = "running", not self.stuck
        self.boots += 1
        self.clock.sleep(40)
        self.booted = int(self.clock.now())

    def stop(self):
        self.effects.append(("stop",))
        self.st = "stopped"

    def push(self, src, dest):
        self.effects.append(("push", os.path.basename(dest)))

    def exec_argv(self, cmd):
        return ["ssh", "admin@192.0.2.9", "bash -lc " + cmd]

    def display(self):
        return self.env.get("WK_VM_DISPLAY") or mac.GUEST_DISPLAY

    def probeable(self):
        return self.tart

    def call(self, fn, *args, input=None, mutates=False):
        ob = args[0]
        self.asked.append((fn, ob.name, dict(ob.params)))
        if mutates:
            self.effects.append((fn, ob.name))
        if fn not in ("m_ssh", "r_ssh") or self.st != "running" or not self.up:
            return Result(1, "", "'%s' is not running" % self.ws)
        ident = self.roots["guest"]["id"] if self.marked else ""
        if ob.name == "mac-read.sh":
            if ob.params["WK_PATH"] == "/etc/wk-image":
                return Result(0, "id=%s\n" % ident if ident else "")
            return Result(0, self.files.get(ob.params["WK_PATH"], ""))
        if ob.name == "mac-boottime.sh":
            return Result(0, "{ sec = %d, usec = 0 }\n" % self.booted)
        if ob.name == "mac-home.sh":
            return Result(0, "/Users/admin")
        if ob.name in ("mac-own.sh", "mac-put.sh") or ob.name.startswith("record-"):
            return Result(0, "")
        return Result(127, "", "%s: not an on-board script this guest knows" % ob.name)


FAKES = {"mac-volume": FakeMac, "mac-guest": FakeGuest}


def mac_board(kind, ids=("sys-a",), env=None):
    conf = conf_for(kind)
    fake = FAKES[kind](conf, env=env)
    fake.write_system(ids[0])
    return fake, DRIVERS[kind](REPO, conf, fake)


def arm_and_boot(d, fake):
    d.probe()
    p, ident = d.select_system("")
    d.record_write(ident, "prof", fake.conf["NODE_DEVICE"], d.order_image)
    d.arm(p, d.order_image)
    d.reboot(armed=True)
    return p, ident


class MacConformance:
    """machine.conformance[<kind>] where a Mac is not a board: no medium to read, no on-board failsafe, and the
    return from bench mode is the bench install's own job (a volume) or leaving the machine (a guest)."""

    kind = None

    def test_the_shim_defines_exactly_the_functions_the_class_has(self):
        """a bench caller asks `command -v b_bench_put` / `b_bench_root` as a board caller asks for b_disarm."""
        cls = DRIVERS[self.kind]
        text = (REPO / "boot" / ("%s.sh" % self.kind)).read_text()
        self.assertEqual(set(re.findall(r"(?m)^(\w+)\(\)", text)), set(cls.shims))
        for verb in re.findall(r"_wk_mac ([\w-]+)", text):
            self.assertIn(verb, dict(boot_main.VERBS, **mac.VERBS), verb)
        for verb in re.findall(r"_wk_boot %s ([\w-]+)" % self.kind, text):
            self.assertIn(verb, boot_main.VERBS, verb)

    def test_the_probe_names_the_channel_that_answered(self):
        fake, d = mac_board(self.kind)
        cls = DRIVERS[self.kind]
        if fake.on_rescue() and fake.running == "host":
            self.assertEqual(d.probe(), "host")
            self.assertEqual(fake.channel, "host")
        fake.enter_bench()
        self.assertEqual(d.probe(), "bench sys-a")
        self.assertEqual(fake.channel, cls.bench_channel)
        fake.up = False
        self.assertEqual(d.probe(), "unreachable")
        self.assertEqual(fake.channel, "none")

    def test_an_arming_boots_the_selected_system_once(self):
        fake, d = mac_board(self.kind)
        before = d.boot_id() if d.probe() != "unreachable" else ""
        arm_and_boot(d, fake)
        self.assertEqual(d.probe(), "bench sys-a")
        self.assertEqual(fake.channel, DRIVERS[self.kind].bench_channel)
        self.assertNotEqual(d.boot_id(), before)
        if d.arming == "command":
            got, err = quiet(d.reboot)
            self.assertIs(got, act.Refused, "a host-side reboot reached an install with no helper")
            fake.leave_bench()
        else:
            d.reboot()
        d.probe()
        self.assertTrue(fake.on_rescue(), "leaving bench mode did not return it")

    def test_the_record_is_written_on_the_host_and_spent_by_the_boot(self):
        fake, d = mac_board(self.kind)
        if d.arming == "guest":
            arm_and_boot(d, fake)
            self.assertIsNone(fake.record, "a guest either answers with a marker or it does not")
            return
        d.probe()
        armed_boot = d.boot_id()
        arm_and_boot(d, fake)
        self.assertIn("image=sys-a", fake.record)
        self.assertIn("armed_boot_id=%s" % armed_boot, fake.record)
        self.assertIn("armed_at=", fake.record)
        fake.leave_bench()
        d.probe()
        _, err = quiet(d.armed_barrier, "writing now")
        self.assertNotIn("armed for system", err, "a spent arming still bars")

    def test_every_report_answers_in_every_mode(self):
        fake, d = mac_board(self.kind)
        for mode in ("host", "bench", "unreachable"):
            with self.subTest(mode=mode):
                fake.up = mode != "unreachable"
                if mode == "bench":
                    fake.enter_bench()
                d.probe()
                for verb in ("media", "evidence", "reprovision"):
                    got, err = quiet(getattr(d, verb))
                    self.assertIsInstance(got, str, "%s %s: %s" % (verb, mode, err))
                self.assertTrue(d.reprovision().startswith("wk "), d.reprovision())

    def test_the_failsafe_is_on_board_shell_outside_the_arming(self):
        """No on-board failsafe: a volume's return is its bench job's hand-back, a guest's is being stopped."""
        cls = DRIVERS[self.kind]
        self.assertIsNone(cls.failsafe)
        self.assertIn(cls.arming, ("command", "guest"))
        self.assertIsNone(mac_board(self.kind)[1].self_disarm_sh())


def volume(**kw):
    fake, d = mac_board("mac-volume", **kw)
    return fake, d


class TestProbe(unittest.TestCase):
    """Host mode first, and asked for the marker too; a bench node answering without one is some other computer."""

    def test_a_marker_on_the_host_channel_is_bench_mode_there(self):
        fake, d = volume()
        fake.roots["host"]["id"] = "perf-macos-tolken-2026-08"
        self.assertEqual((d.probe(), fake.channel), ("bench perf-macos-tolken-2026-08", "host"))
        self.assertNotIn("i_ssh", [a[0] for a in fake.asked])

    def test_a_bench_node_without_the_marker_is_not_this_mac(self):
        fake, d = volume()
        fake.enter_bench()
        fake.roots["bench"]["id"] = ""
        self.assertEqual((d.probe(), fake.channel), ("unreachable", "none"))

    def test_a_machine_that_declares_no_bench_node_is_not_reached_for(self):
        fake, d = volume()
        fake.conf["NODE_BENCH_SSH"] = ""
        d.conf["NODE_BENCH_SSH"] = ""
        fake.enter_bench()
        self.assertEqual(d.probe(), "unreachable")
        self.assertNotIn("i_ssh", [a[0] for a in fake.asked])

    def test_the_boot_id_is_the_boottime_seconds_and_not_usec(self):
        fake, d = volume()
        d.probe()
        self.assertEqual(d.boot_id(), str(fake.booted))
        self.assertEqual(d.booted_at(), fake.clock.iso())


class TestArm(unittest.TestCase):
    """--setBoot is sticky and Apple Silicon has no one-shot form, so the return is proven before the trip out."""

    def arm(self, **fake_state):
        fake, d = volume()
        for k, v in fake_state.items():
            setattr(fake, k, v)
        d.probe()
        got, err = quiet(d.arm)
        return fake, got, err, [a[2].get("WK_VERB") for a in fake.asked if a[1] == "mac-priv.sh"]

    def test_it_arms_and_asserts_the_firmware_afterwards(self):
        fake, got, err, verbs = self.arm()
        self.assertEqual(got, 0, err)
        self.assertIn("the firmware will boot 'WK Bench' next", err)
        self.assertLess(verbs.index("boot-host"), verbs.index("boot-volume"))
        self.assertEqual(fake.firmware, "bench")

    def test_a_mac_that_cannot_boot_itself_again_is_never_sent_away(self):
        fake, got, err, verbs = self.arm(said={"boot-host": (1, "wk-boot-priv: bless said: Error -60005: cannot sign\n")})
        self.assertIs(got, act.Refused)
        self.assertIn("Error -60005: cannot sign", err)
        self.assertNotIn("boot-volume", verbs)

    def test_a_firmware_that_will_not_take_the_volume_is_reported_verbatim(self):
        _, got, err, _ = self.arm(said={"boot-volume": (1, "wk-boot-priv: bless exited 1, so the firmware was not told\n")})
        self.assertIs(got, act.Refused)
        self.assertIn("nothing was changed", err)
        self.assertIn("bless exited 1", err)

    def test_a_return_the_firmware_does_not_confirm_arms_nothing(self):
        fake, d = volume()
        fake.firmware = "bench"
        fake.said = {"boot-host": (0, "wk-boot-priv: blessed /\n")}
        d.probe()
        got, err = quiet(d.arm)
        self.assertIs(got, act.Refused)
        self.assertIn("a return this cannot see", err)
        self.assertNotIn("boot-volume", [a[2].get("WK_VERB") for a in fake.asked])

    def test_an_arming_the_firmware_does_not_confirm_is_reported(self):
        fake, got, err, _ = self.arm(stuck=True)
        self.assertIs(got, act.Refused)
        self.assertIn("mbp's firmware still names", err)

    def test_an_absent_volume_or_helper_is_refused_before_anything_is_asked(self):
        for state, words in (({"attached": False}, "is not attached to mbp"), ({"helper": None}, "boot helper is not installed")):
            with self.subTest(state=state):
                fake, got, err, verbs = self.arm(**state)
                self.assertIs(got, act.Refused)
                self.assertIn(words, err)
                self.assertEqual(verbs, [])

    def test_the_helper_is_asked_on_the_host_install_and_nowhere_else(self):
        fake, got, err, _ = self.arm()
        self.assertEqual({a[0] for a in fake.asked if a[1] == "mac-priv.sh"}, {"m_ssh"})
        text = Script(REPO, "mac-priv.sh", WK_HELPER=HELPER, WK_VERB="boot-host").text()
        self.assertTrue(text.endswith('sudo -n "$WK_HELPER" "$WK_VERB" 2>&1'), text)


class TestReturn(unittest.TestCase):
    def test_a_reboot_from_the_bench_side_is_refused_with_the_reason(self):
        fake, d = volume()
        fake.enter_bench()
        d.probe()
        got, err = quiet(d.reboot)
        self.assertIs(got, act.Refused)
        self.assertIn("carries no boot helper", err)
        self.assertNotIn("mac-priv.sh", [a[1] for a in fake.asked])

    def test_a_disarm_that_cannot_bless_back_is_refused(self):
        fake, d = volume()
        fake.said = {"boot-host": (1, "wk-boot-priv: bless exited 1\n")}
        d.probe()
        got, err = quiet(d.disarm)
        self.assertIs(got, act.Refused)
        for remedy in ("wk boot mbp --prepare", "wk boot mbp --disarm", "Startup Disk"):
            self.assertIn(remedy, err)

    def test_the_restart_is_ready_only_when_the_helper_names_its_detach(self):
        fake, d = volume()
        d.probe()
        self.assertTrue(d.restart_ready())
        fake.helper = "old"
        self.assertFalse(d.restart_ready())
        self.assertIn("older than this tree", d.restart_detail())
        fake.helper = None
        self.assertIn("plain sudo there wants a password", d.restart_detail())


class TestReports(unittest.TestCase):
    def test_a_bench_answer_reports_the_medium_as_that_installs_own_root(self):
        fake, d = volume()
        fake.enter_bench()
        d.probe()
        self.assertIn("tolken-bench is running from it", d.media())
        self.assertIn("under no /Volumes path", d.media())

    def test_silence_on_both_nodes_names_both_of_them(self):
        fake, d = volume()
        fake.up = False
        d.probe()
        self.assertIn("neither tolken nor tolken-bench answers", d.media())
        ev = d.evidence()
        for want in ("booted_volume=unknown", "firmware_default=unknown", "bench_display=builtin 1280x832", "planted_job=none"):
            self.assertIn(want, ev)

    def test_evidence_over_ssh_reads_the_same_facts_as_it_does_locally(self):
        fake, d = volume()
        fake.firmware = "bench"
        d.probe()
        ev = d.evidence()
        self.assertIn("booted_volume=Macintosh HD", ev)
        self.assertIn("attached at /Volumes/WK Bench", ev)
        self.assertIn("a plain reboot is expected to enter bench mode", ev)
        self.assertTrue(fake.fact_input.startswith("#!/usr/bin/env python3"), "lib/wk/mac.py travels on stdin")

    def test_the_newest_planted_task_is_the_one_reported(self):
        with scratch_dir() as tmp:
            for stamp in ("20260901T000000Z", "20260908T010203Z", "20260909T000000Z"):
                task = tmp / "bench" / (stamp + "-mbp-mac-ab")
                task.mkdir(parents=True)
                if stamp != "20260909T000000Z":
                    (task / "job.json").write_text("{}")
            fake, d = mac_board("mac-volume", env={"HOME": str(tmp), "WK_STORE": str(tmp)})
            fake.up = False
            d.probe()
            self.assertIn("20260908T010203Z-mbp-mac-ab (planted 20260908T010203Z)", d.evidence())

    def test_the_bench_system_is_the_volumes_marker(self):
        fake, d = volume()
        d.probe()
        self.assertEqual(d.systems(), [("/Volumes/WK Bench", "sys-a")])
        fake.attached = False
        self.assertEqual(d.systems(), [])
        fake.up = False
        self.assertIsNone(d.systems())

    def test_the_facts_a_bash_caller_reads(self):
        _, d = volume()
        facts = d.facts()
        self.assertEqual(facts["BOOT_ARMING"], "command")
        self.assertEqual(facts["NODE_RECORD"], "${XDG_STATE_HOME:-$HOME/.local/state}/wk/boot-armed")
        self.assertEqual(facts["BOOT_HELPER"], HELPER)
        self.assertEqual(facts["B_MEASURES"], "yes")
        self.assertIsNone(d.check_measurement())


class TestStaging(unittest.TestCase):
    """The staging root and the home, as the answering channel reaches them; nothing lands on a running measurement."""

    def test_the_staging_root_and_the_home_answer_on_that_channel(self):
        fake, d = volume()
        d.probe()
        self.assertEqual((d.bench_root(), d.bench_home()),
                         ("/Volumes/WK Bench - Data/private/var/wk", "/Volumes/WK Bench - Data/Users/bench"))
        fake.data = False
        self.assertEqual(d.bench_root(), "/Volumes/WK Bench/var/wk")
        fake.attached = False
        self.assertIsNone(d.bench_root())
        fake.enter_bench()
        d.probe()
        self.assertEqual((d.bench_root(), d.bench_home()), ("/var/wk", "/Users/bench"))

    def test_nothing_is_staged_onto_a_running_measurement(self):
        fake, d = volume()
        fake.enter_bench()
        d.probe()
        for put in (d.bench_put, d.bench_put_file):
            with self.subTest(put=put.__name__):
                got, err = quiet(put, "/tmp", "/var/wk/x")
                self.assertIs(got, act.Refused)
                self.assertIn("running measurement", err)
        self.assertEqual(fake.effects, [])

    def test_a_tree_travels_as_a_tar_without_history_or_bytecode(self):
        with scratch_dir() as tmp:
            for rel in ("a.txt", ".git/HEAD", "lib/__pycache__/x.pyc", "lib/x.py"):
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_text("x")
            fake, d = volume()
            d.probe()
            self.assertEqual(d.bench_put(str(tmp), "/Volumes/WK Bench - Data/private/var/wk/wk-tools", ".git", "__pycache__"), 0)
        tar = [e for e in fake.effects if e[0] == "tar"][0][1]
        self.assertIn("./lib/x.py", tar)
        self.assertFalse([n for n in tar if ".git" in n or "__pycache__" in n], tar)
        self.assertEqual(fake.effects[-1], ("m_ssh", "mac-put.sh", "tree"))
        put = [a[2] for a in fake.asked if a[1] == "mac-put.sh"][0]
        self.assertEqual(put["WK_DEST"], "/Volumes/WK Bench - Data/private/var/wk/wk-tools")
        self.assertTrue(put["WK_TMP"].startswith("/tmp/wk-put-"))

    def test_a_path_with_a_space_reaches_the_far_shell_as_one_word(self):
        text = Script(REPO, "mac-test.sh", WK_TEST="-d", WK_PATH="/Volumes/WK Bench - Data").text()
        cp = subprocess.run(["sh", "-c", text.replace('test "$WK_TEST" "$WK_PATH"', 'printf "[%s]" "$WK_PATH"')],
                            capture_output=True, text=True)
        self.assertEqual(cp.stdout, "[/Volumes/WK Bench - Data]")

    def test_the_put_script_replaces_the_tree_and_removes_its_tarball(self):
        with scratch_dir() as tmp:
            (tmp / "src").mkdir()
            (tmp / "src" / "new").write_text("n")
            subprocess.run(["tar", "-cf", str(tmp / "t.tar"), "-C", str(tmp / "src"), "."], check=True)
            dest = tmp / "dest dir"
            dest.mkdir()
            (dest / "stale").write_text("s")
            text = Script(REPO, "mac-put.sh", WK_DO="tree", WK_TMP=str(tmp / "t.tar"), WK_DEST=str(dest)).text()
            self.assertEqual(subprocess.run(["sh", "-c", text]).returncode, 0)
            self.assertEqual(sorted(os.listdir(dest)), ["new"])
            self.assertFalse((tmp / "t.tar").exists())


class TestChannel(unittest.TestCase):
    """Host mode is NODE_SSH, or this machine when it is that one; bench mode is its own node, or WK_MAC_BENCH_SSH."""

    CONF = {"NODE_SSH": "tolken", "NODE_BENCH_SSH": "tolken-bench"}

    def test_wk_mac_bench_ssh_moves_the_destination(self):
        self.assertEqual(Channel(self.CONF, {}).dest("i_ssh"), "tolken-bench")
        self.assertEqual(Channel(self.CONF, {"WK_MAC_BENCH_SSH": "elsewhere"}).dest("i_ssh"), "elsewhere")

    def test_the_bench_channel_neither_forces_root_nor_unpins_the_host_key(self):
        via = Fake("here")
        via.answer(["hostname", "-s"], out="moose\n")
        via.answer(["ssh"], out="READY\n")
        ch = Channel(self.CONF, {}, channel="bench", via=via)
        ch.call("r_ssh", Script(REPO, "mac-probe.sh"))
        argv = [e[1] for e in via.effects if e[0] == "run" and e[1][0] == "ssh"][0]
        self.assertIn("tolken-bench", argv)
        self.assertNotIn("root", argv)
        self.assertFalse([a for a in argv if "StrictHostKeyChecking" in a])

    def test_standing_on_the_mac_runs_it_here(self):
        via = Fake("here")
        via.answer(["hostname", "-s"], out="Tolken\n")
        via.answer(["sh", "-c"], out="READY\n")
        ch = Channel(self.CONF, {}, channel="host", via=via)
        self.assertTrue(ch.here())
        self.assertEqual(ch.call("r_ssh", Script(REPO, "mac-probe.sh")).out, "READY\n")
        self.assertEqual(ch.exec_argv("true"), ["bash", "-c", "true"])
        self.assertTrue(DRIVERS["mac-volume"](REPO, dict(self.CONF, NODE_NAME="mbp"), ch).bench_local(),
                        "`wk bench stage --to mbp` on that Mac sends nothing")

    def _down(self, peers):
        """`peers` as name<TAB>ip<TAB>up|down rows, answered as `tailscale status --json` (lib/wk/reach.py reads it)."""
        via = Fake("here")
        via.answer(["hostname", "-s"], out="moose\n")
        rows = [line.split("\t") for line in peers.splitlines()]
        doc = {"Peer": {n: {"DNSName": n + ".tail.ts.net.", "TailscaleIPs": [ip], "Online": st == "up"} for n, ip, st in rows}}
        via.answer(["tailscale", "status", "--json"], rc=0 if rows else 1, out=json.dumps(doc) if rows else "")
        via.answer(["ssh"], out="READY\n")
        return via

    def test_a_node_the_tailnet_reports_down_is_refused_at_once(self):
        via = self._down("tolken\t100.64.0.2\tdown\ntolken-bench\t100.64.0.3\tup\n")
        ch = Channel(self.CONF, {}, channel="host", via=via)
        r, err = quiet(ch.call, "m_ssh", Script(REPO, "mac-probe.sh"))
        self.assertEqual(r.rc, 255)
        self.assertIn("the tailnet says tolken is offline -- power it on, or 'wk machine probe tolken'", err)
        self.assertEqual([e for e in via.effects if e[1][0] == "ssh"], [])
        self.assertTrue(ch.call("i_ssh", Script(REPO, "mac-probe.sh")).ok, "the node that is up is still asked")
        self.assertEqual(len([e for e in via.effects if e[1][0] == "tailscale"]), 1, "the tailnet is read once")

    def test_a_powered_off_mac_probes_unreachable_without_an_ssh(self):
        via = self._down("tolken\t100.64.0.2\tdown\ntolken-bench\t100.64.0.3\tdown\n")
        conf = conf_for("mac-volume")
        d = DRIVERS["mac-volume"](REPO, conf, Channel(conf, {}, via=via))
        got, err = quiet(d.probe)
        self.assertEqual(got, "unreachable")
        self.assertEqual([e for e in via.effects if e[1][0] == "ssh"], [])
        self.assertIn("tolken-bench is offline", err)

    def test_a_tailnet_that_cannot_be_read_leaves_ssh_to_answer(self):
        via = self._down("")
        ch = Channel(self.CONF, {}, channel="host", via=via)
        self.assertTrue(ch.call("m_ssh", Script(REPO, "mac-probe.sh")).ok)

    def test_from_elsewhere_the_manager_is_reached_over_ssh(self):
        via = Fake("here")
        via.answer(["hostname", "-s"], out="moose\n")
        argv = Channel(self.CONF, {}, via=via).exec_argv("cat > /tmp/wk-ab.patch")
        self.assertEqual((argv[0], argv[-2:]), ("ssh", ["tolken", "cat > /tmp/wk-ab.patch"]))
        self.assertIn("BatchMode=yes", argv)

    def test_no_channel_is_no_call(self):
        ch = Channel({"NODE_SSH": "tolken"}, {}, via=Fake("here"))
        self.assertEqual(ch.call("r_ssh", Script(REPO, "mac-probe.sh")).rc, 255)
        self.assertEqual(ch.call("i_ssh", Script(REPO, "mac-probe.sh")).rc, 255)

    def test_a_bash_caller_gets_the_mac_channel_and_not_boot_machines_sh(self):
        d = boot_main.build("mac-volume", {"NODE_SSH": "tolken", "NODE_NAME": "mbp", "MODE_CHANNEL": "bench"}, REPO)
        self.assertIsInstance(d.ch, Channel)
        self.assertEqual(d.ch.channel, "bench")


class TestTheShim(unittest.TestCase):
    """boot/mac-volume.sh over a shell's NODE_*: what bench/mac-ab.sh and cmd/boot read."""

    PRE = ('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; . "$WK_ROOT/lib/bench.sh"; . "$WK_ROOT/boot/machines.sh"\nNODE_NAME=mbp NODE_SSH=fakemac '
           'NODE_BENCH_SSH=fakemac-bench NODE_VOLUME="WK Bench" NODE_DRIVER=mac-volume\n. "$WK_ROOT/boot/mac-volume.sh"\n')

    def test_the_facts_and_a_verb_that_reaches_no_machine(self):
        cp = bash(self.PRE + 'MODE_CHANNEL=bench\necho "$BOOT_ARMING|$BOOT_HELPER|$(b_bench_root)|$(b_bench_home)"')
        self.assertEqual(cp.stdout.strip(), "command|%s|/var/wk|/Users/bench" % HELPER, cp.stderr)

    def test_the_bench_channel_is_the_alias_for_boot_machines_sh_r_ssh(self):
        cp = bash(self.PRE + 'ssh() { printf "%s\\n" "$*"; }\ni_ssh true\n', env={"WK_MAC_BENCH_SSH": "somewhere-else"})
        self.assertTrue(cp.stdout.strip().endswith("somewhere-else true"), cp.stdout + cp.stderr)
        self.assertNotIn("-l root", cp.stdout)

    def test_a_put_carries_lib_bench_sh_s_one_list_of_what_it_skips(self):
        text = (REPO / "boot" / "mac-volume.sh").read_text()
        self.assertIn('bench-put "$1" "$2" ${BENCH_PUT_SKIP:?', text)
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/boot/machines.sh"\nNODE_VOLUME="WK Bench"\n'
                  '. "$WK_ROOT/boot/mac-volume.sh"\nb_bench_put /tmp /var/wk; echo "MUST NOT"')
        self.assertIn("lib/bench.sh sets it", cp.stderr)
        self.assertNotIn("MUST NOT", cp.stdout)

    def test_a_refusal_reaches_the_caller(self):
        cp = bash(self.PRE + 'MODE_CHANNEL=bench\nif b_bench_put /tmp /var/wk; then echo PUT; fi')
        self.assertNotIn("PUT", cp.stdout)
        self.assertIn("running measurement", cp.stderr)


class TestOnTheRealMac(unittest.TestCase):
    """Read-only, as `requires_machine` is: the bless round trip itself is the owed half of `live boot.arm[mbp]`."""

    @requires_machine("tolken")
    def test_what_an_arming_proves_first_reads_true_on_mbp(self):
        """`live boot.arm[mbp]`: host mode, the volume attached, and a firmware default naming one of its installs."""
        conf = conf_for("mac-volume")
        d = DRIVERS["mac-volume"](REPO, conf, Channel(conf))
        if d.probe() != "host":
            self.skipTest("mbp answers as %s, and an arming starts from host mode" % d.mode)
        self.assertTrue(d.volume_present(), d.media())
        fw = d.firmware_default()
        self.assertTrue("the host install" in fw or "'WK Bench'" in fw, fw)


if __name__ == "__main__":
    unittest.main()
