"""The Mac A/B's front half, `wk bench ab --devices <mac>` (lib/wk/bench/mac.py's MacAB -- preflight, build,
stage, plant, restart), against FakeMac and the fake clock; its back half is tests/test_mac_ab_rounds.py.

Nothing here touches a real Mac, startup disk or helper; the live rows read the real machines and change nothing.

Run: python3 tests/run.py --unit -k test_mac_ab_driver
"""
import contextlib
import io
import json
import os
import re
import shlex
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, requires_machine, scratch_dir, temp_store
from tests.test_mac_volume import BENCH_GROUP, FakeGuest, FakeMac, conf_for

sys.path.insert(0, str(REPO / "lib"))
from wk import act, sched, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import ab, mac  # noqa: E402
from wk.boot.mac import DRIVERS, HELPER  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

DIGEST = "d" * 64

# Interface 1's reading on the bench install and on the host install (tolken, 2026-09-07).
BENCH_DISPLAY = {"count": 1, "displays": [
    {"id": 1, "builtin": True, "main": True, "active": True, "online": True,
     "mirrored": False, "asleep": False, "points": [1470, 956]}]}
HOST_DISPLAY = {"count": 1, "displays": [
    {"id": 1, "builtin": True, "main": True, "active": False, "online": True,
     "mirrored": False, "asleep": True, "points": [1280, 832]}]}


def rendered(name, p):
    """Each bench/onboard/mac-* file as the command it stands for, so an answer is keyed on what it does."""
    if name == "mac-py.sh":
        return " ".join(shlex.quote(p[k]) for k in sorted(k for k in p if k != "WK_PY"))
    return {"mac-test.sh": lambda: "test %s %s" % (p["WK_TEST"], p["WK_PATH"]), "mac-ls.sh": lambda: "ls -1 " + p["WK_PATH"],
            "mac-defaults.sh": lambda: "defaults read %s %s" % (p["WK_PATH"], p["WK_KEY"]), "mac-size.sh": lambda: "wc -c " + p["WK_PATH"],
            "mac-scipy.sh": lambda: "pip install --target %s scipy" % p["WK_PATH"], "mac-mkdir.sh": lambda: "mkdir -p " + p["WK_PATH"],
            "mac-executable.sh": lambda: "chmod 0755 " + p["WK_PATH"], "mac-platform.sh": lambda: "ioreg",
            "mac-screensaver.sh": lambda: "defaults read %s idleTime" % p["WK_SAVER"], "mac-dnd.sh": lambda: "wk_quiet_dnd_on " + p["WK_HOME"],
            "mac-arch.sh": lambda: "uname -m", "mac-version.sh": lambda: "PlistBuddy " + p["WK_PATH"],
            "mac-gated.sh": lambda: "gated " + p["WK_PATH"], "mac-tail.sh": lambda: "tail -20 " + p["WK_PATH"],
            "mac-tar.sh": lambda: "tar %s %s" % (p["WK_PATH"], p["WK_DIR"])}[name]()


class Shell:
    """The Mac A/B's on-board files answered from `answers`, (pattern, Result or fn(cmd)) searched latest first, each
    matched against the command the file stands for; the rest of the Mac is the fake it is mixed into."""

    def sh_setup(self):
        self.answers, self.ran = [], []
        for pat, res in ((r"^test ", Result(0)), (r"^defaults read .*loginwindow", Result(0, "bench\n")),
                         (r"^test -f .*com.wk.bench-firstboot.plist", Result(1)), (r"^ls -1 .*/staged", Result(0, "sid-a\nsid-b\n")),
                         (r"--exclude", Result(0, DIGEST + "\n")), (r"^wc -c", Result(0, "  42\n")),
                         (r"^ioreg", Result(0, '  "IOPlatformUUID" = "UUID-1"\n')), (r"^mkdir|^chmod|pip install", Result(0)),
                         (r"^defaults read .*idleTime", Result(0, "0\n")), (r"wk_quiet_dnd_on", Result(0, "on\n")),
                         (r"^uname -m", Result(0, "arm64\n"))):
            self.answer(pat, res)

    def answer(self, pattern, result):
        self.answers.append((re.compile(pattern), result))

    def sh(self, cmd):
        self.ran.append(cmd)
        for pat, got in reversed(self.answers):
            if pat.search(cmd):
                return got(cmd) if callable(got) else got
        return Result(1, "", "no answer for: %s" % cmd[:80])

    def ours(self, name, p):
        return (REPO / "bench" / "onboard" / name).is_file() and name.startswith("mac-") or (
            name == "mac-test.sh" and p["WK_PATH"] not in self.known_paths())


class PlantMac(Shell, FakeMac):
    """A Mac in host mode, armed and ready: every gate the preflight reads passes until a test says otherwise."""

    def __init__(self, conf, env=None, clock=None, manager=None):
        FakeMac.__init__(self, conf, env=env, clock=clock)
        self.sh_setup()
        self.manager = manager
        self.firmware = "bench"
        self.files[self.firstboot_log()] = "provisioning complete\n"
        for pat, res in ((r"^displays$", Result(0, json.dumps(BENCH_DISPLAY))), (r"^(boot-volume|volume-group)", lambda key: self.wkmac(key))):
            self.answer(pat, res)

    def firstboot_log(self):
        return self.vol + " - Data/private/var/log/wk-bench-firstboot.log"

    def known_paths(self):
        return {self.vol + "/System/Library/CoreServices", self.vol + " - Data", HELPER}

    def wkmac(self, key):
        words = shlex.split(key)
        out = self.fact(words[0], words[1] if len(words) > 1 else "")
        return Result(0 if out else 1, out + "\n" if out else "")

    def script(self, name, p, input):
        if self.ours(name, p):
            return self.sh(rendered(name, p))
        return FakeMac.script(self, name, p, input)

    def machine(self, fn="m_ssh"):
        return self.manager


class PlantGuest(Shell, FakeGuest):
    def __init__(self, conf, env=None, clock=None):
        FakeGuest.__init__(self, conf, env=env, clock=clock)
        self.sh_setup()
        self.st = "running"
        self.answer(r"^displays$", Result(0, json.dumps({"displays": [{"online": True, "points": [1280, 800]}]})))

    def known_paths(self):
        return set()

    def call(self, fn, *args, input=None, mutates=False):
        ob = args[0]
        if self.ours(ob.name, ob.params) and self.st == "running" and self.up:
            self.asked.append((fn, ob.name, dict(ob.params)))
            if mutates:
                self.effects.append((fn, ob.name))
            return self.sh(rendered(ob.name, ob.params))
        return FakeGuest.call(self, fn, *args, input=input, mutates=mutates)


def here_fake():
    """This machine: its own digest of the tree, a samply, and a notifier that is told what went out."""
    here = Fake("here")
    here.answer(["hostname", "-s"], out="moose\n")
    here.answer(["python3"], out=DIGEST + "\n")
    here.answer(["sh", "-c", 'wc -c < "$1"'], out="42\n")
    here.notified = []

    def bash_fn(argv, fake):
        if "samply" in argv[2]:
            return Result(0, "0.13.1\naarch64-apple-darwin\n/cache/samply\n")
        return Result(1)
    here.react(["bash", "-c"], bash_fn)
    return here


@contextlib.contextmanager
def world(kind="mac-volume", env=None, **o):
    """A MacAB over a fake Mac, this machine a Fake, the store a scratch directory, every prompt answered yes."""
    with temp_store() as store, scratch_dir() as tree:
        for part in ("bench", "boot", "lib", "machines"):
            os.symlink(REPO / part, tree / part)
        clock, here = FakeClock(), here_fake()
        e = {"HOME": str(tree), "WK_STORE": store["WK_STORE"]}
        e.update(env or {})
        conf = conf_for(kind)
        manager = Fake("tolken")
        fake = PlantMac(conf, env=e, clock=clock, manager=manager) if kind == "mac-volume" else PlantGuest(conf, env=e, clock=clock)
        fake.write_system("perf-macos-tolken-1")
        driver = DRIVERS[kind](REPO, conf, fake)
        reg = targets.Registry(REPO, env=e, machine=here)
        opts = dict({"devices": "mbp" if kind == "mac-volume" else "benchvm", "systems": "sid-a,sid-b"}, **o)
        m = mac.MacAB(tree, reg, clock, "", opts, driver=lambda root, c: driver)
        m.fake, m.here_fake, m.manager_fake, m.store, m.clock_ = fake, here, manager, store["path"], clock
        def sent(root, headline, *a, **k):
            here.notified.append(headline)
            return not getattr(here, "notify_fails", False)
        with mock.patch.dict(os.environ, {"WK_YES": "1"}), mock.patch("wk.notify.send", side_effect=sent), \
                mock.patch("wk.sysimage.mactailnet.Tailnet.collect", return_value=""):
            os.environ.pop("WK_DRY_RUN", None)
            os.environ.pop("WK_FORCE", None)
            yield m


def said(fn, *args):
    """(value or Refused, stderr)."""
    with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
        try:
            return fn(*args), err.getvalue()
        except Refused:
            return Refused, err.getvalue()


def ready(m):
    m.check()
    m.resolve()
    m.boot_wait = 600
    return m


def mutations(m):
    return [e for e in m.fake.effects if e[0] in ("m_ssh", "r_ssh", "push")]


class TestTheFirmwareDefaultIsAsserted(WkTest):
    """A restart only starts an A/B if the firmware's own default is the bench volume."""

    def _fw(self, firmware=None, blank=False):
        with world() as m:
            ready(m)
            if firmware:
                m.fake.firmware = firmware
            if blank:
                m.fake.answer(r"^boot-volume", Result(1))
            return m.firmware_is_bench(), m.fw_detail

    def test_the_bench_volume_group_as_the_default_passes(self):
        ok, detail = self._fw()
        self.assertTrue(ok, detail)
        self.assertIn(BENCH_GROUP, detail)

    def test_the_host_install_as_the_default_fails(self):
        ok, detail = self._fw("host")
        self.assertFalse(ok)
        self.assertIn("the host install", detail)

    def test_a_default_matching_neither_install_fails(self):
        with world() as m:
            ready(m)
            m.fake.answer(r"^boot-volume", Result(0, "a:b:11111111-2222-3333-4444-555555555555\n"))
            self.assertFalse(m.firmware_is_bench())
            self.assertIn("neither install", m.fw_detail)

    def test_an_unreadable_boot_volume_fails(self):
        ok, detail = self._fw(blank=True)
        self.assertFalse(ok)
        self.assertIn("no boot-volume", detail)


class TestOnlyTheDeclaredDisplay(WkTest):
    """An external monitor changes the compositing, the refresh rate and which GPU the window lands on, and
    MotionMark's score is the area it draws."""

    def v(self, doc, want="builtin"):
        return mac.display_verdict(doc if isinstance(doc, str) else json.dumps(doc), want)

    def test_the_measured_reading_passes(self):
        ok, detail = self.v(BENCH_DISPLAY)
        self.assertTrue(ok)
        self.assertIn("builtin 1470x956", detail)

    def test_the_host_installs_own_reading_passes_too(self):
        """The mode differs between the two installs; what this asks is the count and which panel."""
        self.assertTrue(self.v(HOST_DISPLAY)[0])

    def test_two_online_displays_fail(self):
        doc = {"displays": [BENCH_DISPLAY["displays"][0], {"builtin": False, "online": True, "points": [3840, 2160]}]}
        ok, detail = self.v(doc)
        self.assertFalse(ok)
        self.assertIn("2 online display(s)", detail)
        self.assertIn("external 3840x2160", detail)

    def test_a_display_that_is_not_the_declared_kind_fails(self):
        ok, detail = self.v({"displays": [{"builtin": False, "online": True, "points": [2560, 1440]}]})
        self.assertFalse(ok)
        self.assertIn("not the builtin panel", detail)

    def test_a_guest_is_measured_on_the_paravirtual_panel_it_declares(self):
        self.assertTrue(self.v({"displays": [{"online": True, "points": [1280, 800]}]}, "external")[0])

    def test_no_display_and_an_offline_one(self):
        self.assertIn("0 online display(s)", self.v({"displays": []})[1])
        doc = {"displays": [BENCH_DISPLAY["displays"][0], {"builtin": False, "online": False}]}
        self.assertTrue(self.v(doc)[0])

    def test_a_reading_that_could_not_be_taken_fails(self):
        self.assertIn("answered nothing", self.v("")[1])
        self.assertIn("did not print JSON", self.v("not json")[1])


class TestThePinnedDisplayIsConfig(WkTest):
    def test_mbp_declares_the_bench_installs_measured_mode(self):
        """1280x832 at scale 2 is exactly the 2560x1664 panel: no frame is rendered larger and downsampled."""
        self.assertEqual(conf_for("mac-volume")["NODE_DISPLAY"], "builtin 1280x832")

    def test_a_machine_that_declares_no_display_refuses_the_plant(self):
        with world() as m:
            ready(m)
            m.d.conf["NODE_DISPLAY"] = ""
            m.create_task("20260908T000000Z")
            got, err = said(m.plant)
        self.assertIs(got, Refused)
        self.assertIn("declares no display", err)
        self.assertIn("machines/mbp.conf", err)


class TestPreflight(WkTest):
    """Every check is something that, if wrong, is discovered after the reboot where nothing can report it."""

    def pf(self, m, setup=lambda m: None):
        ready(m)
        setup(m)
        n, err = said(m.preflight)
        return n, err

    def test_a_ready_mac_is_clean_and_changes_nothing(self):
        with world() as m:
            n, err = self.pf(m)
            self.assertEqual(n, 0, err)
            self.assertEqual(mutations(m), [])
        self.assertIn("preflight clean", err)

    def test_an_unreachable_mac_stops_at_the_first_row(self):
        with world() as m:
            m.fake.up = False
            n, err = self.pf(m)
        self.assertEqual(n, 1)
        self.assertIn("nothing else was checked", err)

    def test_bench_mode_is_refused_since_the_arms_are_on_the_host_install(self):
        with world() as m:
            m.fake.enter_bench()
            n, err = self.pf(m)
        self.assertGreaterEqual(n, 1)
        self.assertIn("BENCH mode", err)

    def test_an_unprovisioned_volume_fails_and_names_the_repair(self):
        with world() as m:
            n, err = self.pf(m, lambda m: m.fake.files.update({m.fake.firstboot_log(): ""}))
        self.assertEqual(n, 1, err)
        self.assertIn("wk sysimage build perf-macos-tolken --repair", err)

    def test_a_mac_it_cannot_restart_names_machine_setup(self):
        with world() as m:
            m.fake.helper = None
            n, err = self.pf(m)
        self.assertEqual(n, 1, err)
        self.assertIn("wk machine setup mbp", err)
        self.assertIn("planted and", err)

    def test_nothing_staged_fails_unless_patch_will_stage(self):
        empty = lambda m: m.fake.answer(r"^ls -1 .*/staged", Result(0, ""))   # noqa: E731
        with world() as m:
            self.assertEqual(self.pf(m, empty)[0], 1)
        with world(systems="", patch="HEAD", workspace="mac-rel") as m:
            self.assertEqual(self.pf(m, empty)[0], 0)

    def test_a_second_display_fails_and_no_force_crosses_it(self):
        with world() as m:
            two = {"displays": [BENCH_DISPLAY["displays"][0], {"online": True, "points": [3840, 2160]}]}
            n, err = self.pf(m, lambda m: m.fake.answer(r"^displays$", Result(0, json.dumps(two))))
        self.assertEqual(n, 1)
        self.assertIn("No --force crosses it", err)

    def test_a_guest_needs_its_marker_and_has_no_firmware_to_ask(self):
        with world("mac-guest") as m:
            n, err = self.pf(m)
            self.assertEqual(n, 0, err)
            self.assertIn("enters bench mode", err)
        with world("mac-guest") as m:
            m.fake.marked = False
            n, err = self.pf(m)
            self.assertIn("carries no /etc/wk-image", err)


class TestItSharesTheBoardABsRefusals(WkTest):
    def refused(self, spec="", **o):
        with world(**o) as m:
            m.spec = spec
            got, err = said(m.check)
        self.assertIs(got, Refused, err)
        return err

    def test_the_two_arms_are_two_different_staged_builds(self):
        self.assertIn("two different arms", self.refused(systems="sid-a,sid-a"))

    def test_a_change_to_resolve_in_the_mirror_is_not_a_macs_arm(self):
        self.assertIn("staged builds", self.refused(spec="wpe:1725"))

    def test_a_board_only_option_is_refused(self):
        self.assertIn("--slot is a board A/B's", self.refused(slot="a"))

    def test_the_plan_refusals_are_the_board_ones(self):
        self.assertIn("--rounds takes a number", self.refused(rounds="0"))
        self.assertIn("is not a plan name", self.refused(plans=["a b"]))

    def test_the_ceiling_is_not_below_the_floor(self):
        self.assertIn("below --rounds", self.refused(rounds="9", max_rounds="4"))

    def test_patch_builds_in_a_workspace_and_excludes_systems(self):
        self.assertIn("--workspace <ws>", self.refused(systems="", patch="HEAD"))
        self.assertIn("One or the other", self.refused(patch="HEAD", workspace="mac-rel"))

    def test_a_board_is_refused_a_macs_option(self):
        with temp_store() as store:
            reg = targets.Registry(REPO, env={"WK_STORE": store["WK_STORE"], "HOME": "/nonexistent"}, machine=Fake())
            got, err = said(ab.run, REPO, reg, FakeClock(), "", {"devices": "rpi5", "systems": "a,b", "patch": "x"})
        self.assertIs(got, Refused)
        self.assertIn("--patch is a Mac A/B's", err)


class TestThePlant(WkTest):
    """Recorded before the Mac is touched, then everything the run needs written onto the volume while it is
    merely mounted, each write judged by reading it back."""

    def plant(self, m, setup=lambda m: None):
        ready(m)
        setup(m)
        m.create_task(m.clock.stamp())
        return said(m.plant)

    def job(self, m):
        return json.loads(m.here_fake.files[os.path.join(m.taskdir, "job.json")])

    def test_the_job_carries_the_plan_the_arms_and_the_declared_display(self):
        with world(rehearse=True) as m:
            got, err = self.plant(m)
            self.assertIsNone(got, err)
            job = self.job(m)
        self.assertEqual(job["plans"], ["jetstream3", "speedometer3", "motionmark"])
        self.assertEqual((job["rounds"], job["max_rounds"], job["detect_pct"], job["count"]), (5, 40, 0.3, "2"))
        self.assertEqual([a["id"] for a in job["arms"]], ["sid-a", "sid-b"])
        self.assertEqual(job["display"], "builtin 1280x832")
        self.assertEqual(job["rehearsal"], "1")

    def test_the_job_carries_no_force(self):
        """Crossing a preflight barrier must not reach each leg's own quiet-machine gate."""
        with world(env={"WK_FORCE": "1"}) as m:
            self.plant(m)
            job = self.job(m)
        self.assertNotIn("force", job)
        self.assertEqual(job["rehearsal"], "")

    def test_the_task_it_writes_is_one_wk_status_can_read(self):
        with world() as m:
            self.plant(m)
            import subprocess
            cp = subprocess.run(["python3", str(REPO / "lib" / "wkdata.py"), "task-status", m.taskdir],
                                capture_output=True, text=True, timeout=30)
        self.assertIn("subject=sid-a vs sid-b", cp.stdout, cp.stderr)
        self.assertIn("mbp", cp.stdout)

    def test_an_arm_that_is_not_staged_is_refused_before_anything_lands(self):
        with world(systems="sid-a,sid-x") as m:
            got, err = self.plant(m)
            self.assertIs(got, Refused)
            self.assertEqual(mutations(m), [])
        self.assertIn("no staged build 'sid-x'", err)

    def test_the_tree_is_verified_file_for_file(self):
        with world() as m:
            got, err = self.plant(m, lambda m: m.fake.answer(r"wk-tools'? --exclude", Result(0, "e" * 64 + "\n")))
        self.assertIs(got, Refused)
        self.assertIn("what landed is not this tree", err)

    def test_both_digests_leave_out_the_same_names(self):
        with world() as m:
            self.plant(m)
            remote = [c for c in m.fake.ran if re.search(r"wk-tools'? --exclude", c)][0]
            here = [e[1] for e in m.here_fake.effects if e[0] == "run" and e[1][0] == "python3"][0]
            pushed = [e[1] for e in m.fake.effects if e[0] == "tar"][0]
        for name in mac.PUT_SKIP:
            self.assertIn("--exclude %s" % name, remote)
            self.assertIn(name, here)
            self.assertFalse([n for n in pushed if name in n], pushed)

    def test_a_file_that_landed_short_is_refused(self):
        with world() as m:
            got, err = self.plant(m, lambda m: m.fake.answer(r"^wc -c .*job.json", Result(0, "7\n")))
        self.assertIs(got, Refused)
        self.assertIn("could not write the job", err)

    def test_a_screensaver_it_cannot_turn_off_is_refused_unless_forced(self):
        idle = lambda m: m.fake.answer(r"^defaults read .*idleTime", Result(0, "300\n"))   # noqa: E731
        with world() as m:
            got, err = self.plant(m, idle)
        self.assertIs(got, Refused)
        self.assertIn("To plant anyway:  --force", err)
        with world(env={"WK_FORCE": "1"}) as m:
            got, err = self.plant(m, idle)
        self.assertIsNone(got, err)
        self.assertIn("the screen may lock", err)

    def test_do_not_disturb_is_read_back(self):
        with world() as m:
            got, err = self.plant(m, lambda m: m.fake.answer(r"wk_quiet_dnd_on", Result(0, "off\n")))
        self.assertIs(got, Refused)
        self.assertIn("Do Not Disturb", err)

    def test_the_agent_runs_the_tree_the_plant_verified(self):
        with world() as m:
            self.plant(m)
            plist = m.here_fake.files[os.path.join(m.taskdir, mac.AGENT + ".plist")]
        self.assertIn("/var/wk/wk-tools/lib/wk/bench/autorun.py", plist)
        self.assertNotIn("KeepAlive", plist)

    def test_a_tree_with_no_autorun_is_refused(self):
        with world() as m:
            got, err = self.plant(m, lambda m: m.fake.answer(r"^test -r .*lib/wk/bench/autorun.py", Result(1)))
        self.assertIs(got, Refused)
        self.assertIn("carries no lib/wk/bench/autorun.py", err)

    def test_the_autorun_state_is_reset_to_this_job(self):
        with world() as m:
            self.plant(m)
            state = m.here_fake.files[os.path.join(m.taskdir, "planted.state")]
        self.assertIn("phase=planted\njob_stamp=", state)
        self.assertIn("attempts=0", state)


class TestTheWholeTrip(WkTest):
    def go(self, m):
        return said(m.go)

    def test_a_dry_run_changes_nothing_and_names_the_gate_it_did_not_evaluate(self):
        with world() as m, mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            rc, err = self.go(m)
            self.assertEqual(rc, 0, err)
            self.assertEqual(mutations(m), [])
            self.assertFalse(os.path.exists(m.taskdir))
        self.assertIn("dry run -- nothing on mbp was changed", err)
        self.assertIn("wk bench staged --gates", err)

    def test_a_failed_preflight_is_a_barrier_that_force_crosses(self):
        with world() as m:
            m.fake.firmware = "host"
            got, err = self.go(m)
            self.assertIs(got, Refused)
            self.assertEqual(mutations(m), [])
        self.assertIn("--force proceeds anyway", err)
        with world(env={"WK_FORCE": "1"}, plant=True) as m:
            os.environ["WK_FORCE"] = "1"
            m.fake.firmware = "host"
            rc, err = self.go(m)
        act._forced.clear()
        self.assertEqual(rc, 0, err)
        self.assertIn("FORCED past a barrier", err)

    def test_plant_restarts_nothing(self):
        with world(plant=True) as m:
            rc, err = self.go(m)
            self.assertEqual(rc, 0, err)
            self.assertEqual(m.fake.boots, 1)
        self.assertIn("planted and not started", err)

    def test_it_restarts_through_the_helper_and_sees_the_bench_install_answer(self):
        with world() as m:
            rc, err = self.go(m)
            self.assertEqual(rc, 0, err)
            self.assertEqual([p.get("WK_VERB") for fn, n, p in m.fake.asked if n == "mac-priv.sh" and p.get("WK_VERB") == "reboot"], ["reboot"])
            self.assertEqual(m.fake.running, "bench")
            self.assertEqual(len(m.here_fake.notified), 1, "the plant, and nothing about a run that is going as asked")
        self.assertIn("answers in BENCH mode", err)

    def test_the_display_is_asked_again_before_the_restart_and_force_does_not_cross_it(self):
        with world(env={"WK_FORCE": "1"}) as m:
            ready(m)
            m.fake.answer(r"^displays$", Result(0, json.dumps({"displays": []})))
            got, err = said(m.restart)
            self.assertIs(got, Refused)
            self.assertEqual(m.fake.boots, 1)
        self.assertIn("0 online display(s)", err)

    def test_a_restart_the_helper_did_not_make_is_refused(self):
        with world() as m:
            ready(m)
            m.fake.said["reboot"] = (0, "")
            got, err = said(m.restart)
        self.assertIs(got, Refused)
        self.assertIn("still answering", err)

    def test_a_guests_restart_is_its_stop_and_start(self):
        with world("mac-guest") as m:
            rc, err = self.go(m)
            self.assertEqual(rc, 0, err)
            self.assertEqual([e for e in m.fake.effects if e in (("stop",), ("start",))], [("stop",), ("start",)])
        self.assertIn("answers in BENCH mode", err)

    def test_it_is_not_driven_from_the_mac_it_reboots(self):
        with world() as m:
            m.fake.here = lambda: True
            got, err = self.go(m)
        self.assertIs(got, Refused)
        self.assertIn("cannot be driven from mbp", err)


class TestTheWaitReadsBothNodes(WkTest):
    """Bench mode is a positive reading: the install answers as its own node while it measures."""

    def wait(self, m, same_boot=False):
        ready(m)
        m.boot_before = m.d.boot_id() if same_boot else "1"
        return said(m.wait)

    def test_a_bench_answer_is_the_run(self):
        with world() as m:
            m.fake.enter_bench()
            self.assertEqual(self.wait(m)[0], "bench")

    def test_a_host_answer_on_a_new_boot_is_the_way_back(self):
        with world() as m:
            self.assertEqual(self.wait(m)[0], "host")

    def test_a_host_answer_on_the_same_boot_never_rebooted(self):
        with world() as m:
            self.assertEqual(self.wait(m, same_boot=True)[0], "noreboot")

    def test_silence_on_both_nodes_is_bounded_by_the_clock(self):
        with world() as m:
            m.fake.up = False
            got, err = self.wait(m)
            self.assertEqual(got, "silent")
            self.assertLessEqual(sum(m.clock_.slept), 45 + 600 + 20 + 40)
        self.assertIn("neither node", err)


class TestAFailedNotifyCostsNothing(WkTest):
    def test_a_failing_notify_warns_and_carries_on(self):
        with world() as m:
            m.here_fake.notify_fails = True
            got, err = said(m.notify, "a headline", "a detail")
        self.assertIsNone(got)
        self.assertIn("could not send the notification 'a headline'", err)

    def test_the_ways_back_are_notified_and_silence_is_not(self):
        with world() as m:
            ready(m)
            for came in ("host", "noreboot", "silent", "bench"):
                said(m.outcome, came)
            self.assertEqual(len(m.here_fake.notified), 2)


class TestTheArmsAreBuiltInTheWorkspace(WkTest):
    """--patch: the baseline and the patched tree are built and staged in the guest, each reclaimed once staged."""

    def build(self, patch="refs/heads/pr", setup=lambda m: None, **o):
        with world(systems="", patch=patch, workspace="mac-rel", **o) as m:
            mgr = m.manager_fake
            mgr.answer(["test", "-x"], rc=0)
            mgr.answer(["sh", "-c"], out="/seed/payload\n")
            mgr.dirs.add("/seed/payload")
            mgr.answer(["ssh"], out="/Users/admin/WebKit\n")
            m.here_fake.answer(["sh", "-c", sched.LOGGED], rc=0)
            ready(m)
            m.create_task("20260908T000000Z")
            setup(m)
            steps, rwk = [], m.rwk

            def recorded(*words, logged=""):
                steps.append(words[1] if words[:1] == ("bench",) else words[0])
                return rwk(*words, logged=logged)
            m.rwk = recorded
            m.staged_ids = lambda root: ["sid-new-a", "sid-new-b"][:steps.count("stage")]
            got, err = said(m.build_ab)
            return m, got, err, steps

    def guest_scripts(self, m):
        return [guest_script(e[1][-1]) for e in m.manager_fake.effects if e[0] == "run" and e[1][0] == "ssh"]

    def test_both_arms_are_built_staged_and_reclaimed_in_order(self):
        m, got, err, steps = self.build()
        self.assertIsNone(got, err)
        self.assertEqual((m.a, m.b), ("sid-new-a", "sid-new-b"))
        self.assertEqual(steps, ["start", "build", "seed", "seed", "seed", "stage", "build", "seed", "seed", "seed", "stage", "stop"])
        self.assertIn("reclaimed baseline", err)
        self.assertTrue([s for s in self.guest_scripts(m) if "rm -rf" in s and "Release-pgo-instr" in s])

    def test_a_diff_travels_inside_the_guest_script(self):
        """The guest's /tmp is not the manager's, so the patch crosses in the script that applies it."""
        with scratch_dir() as tmp:
            (tmp / "x.diff").write_text("--- a\n+++ b\n")
            m, got, err, _ = self.build(patch=str(tmp / "x.diff"))
        self.assertIsNone(got, err)
        self.assertTrue([s for s in self.guest_scripts(m) if "base64 -d > /tmp/wk-ab.patch; git -C /Users/admin/WebKit apply" in s])

    def test_an_unpinnable_payload_stages_nothing_unless_told(self):
        def unpinned(m):
            m.manager_fake.answer(["sh", "-c"], out="\n")
        m, got, err, steps = self.build(setup=unpinned)
        self.assertIs(got, Refused)
        self.assertNotIn("stage", steps)
        self.assertIn("--allow-network-fetch", err)
        self.assertIsNone(self.build(setup=unpinned, allow_network_fetch=True)[1])

    def test_a_patch_that_does_not_apply_puts_the_tree_back(self):
        def failing(m):
            m.manager_fake.react(["ssh"], lambda argv, f: Result(1 if "checkout -q refs/heads/pr" in guest_script(argv[-1]) else 0,
                                                                 "/Users/admin/WebKit\n"))
        m, got, err, steps = self.build(setup=failing)
        self.assertIs(got, Refused)
        self.assertIn("the tree has been put back", err)
        self.assertEqual(steps.count("stage"), 1)
        self.assertIn("checkout -q -f", self.guest_scripts(m)[-1])


def guest_script(inner):
    import base64
    return base64.b64decode(inner.split()[2]).decode()


class TestTheLiveRows(unittest.TestCase):
    """Read-only, as `requires_machine` is: the preflight of each machine the Mac A/B plants on, which changes nothing.
    The end-to-end halves -- building, staging and measuring a real `mac-release-pgo` pair on mbp and the rehearsal
    on benchvm -- spend hours of those machines, and are the live tier's to run."""

    def preflight(self, machine):
        reg = targets.Registry(REPO, machine=None)
        m = mac.MacAB(REPO, reg, FakeClock(), "", {"devices": machine})
        m.resolve()
        n, err = said(m.preflight)
        self.assertIsInstance(n, int, err)
        self.assertIn("preflight for an unattended A/B on %s" % machine, err)

    @requires_machine("tolken")
    def test_ab_pgo_pair_mbp(self):
        """`live ab.pgo_pair[mbp]`, its read-only half."""
        self.preflight("mbp")

    @requires_machine("benchvm")
    def test_bench_rehearsal_benchvm(self):
        """`live bench.rehearsal[benchvm]`, its read-only half."""
        self.preflight("benchvm")


class TestStatusCarriesTheLegs(WkTest):
    """`--status` is the command that answers "how far has it got", so the
    per-leg timings belong in it. Reaching past it with an ssh of one's own
    leaves the gap in place for the next person."""

    def _legs(self, started="2026-09-09T18:08:20Z", tsv=None, older=True):
        with scratch_dir() as root:
            (root / "job.json").write_text(json.dumps({
                "plans": ["speedometer3", "jetstream3", "motionmark"],
                "rounds": 2,
                "arms": [{"label": "A", "id": "sid-a"}, {"label": "B", "id": "sid-b"}]}))
            state = "job_stamp=20260909T180544Z\n"
            if started:
                state += "started_at=%s\n" % started
            state += "ok_speedometer3_A_0=1\nok_speedometer3_B_0=1\nok_speedometer3_A_1=1\n"
            (root / "autorun.state").write_text(state)
            legs = [("20260909T181045Z-speedometer3-sid-a", 91),
                    ("20260909T181218Z-speedometer3-sid-b", 92),
                    ("20260909T181351Z-speedometer3-sid-a", 31),
                    ("20260909T181500Z-motionmark-sid-b", None)]
            if older:
                legs.insert(0, ("20260101T000000Z-speedometer3-sid-a", 42))
            for name, wall in legs:
                d = root / "results" / name
                d.mkdir(parents=True)
                env = {"plan": name.split("-")[1]}
                if wall is not None:
                    env["wall_time_s"] = str(wall)
                (d / "env.json").write_text(json.dumps(env))
            runs = root / "ab" / "20260909T180544Z" / "runs.tsv"
            runs.parent.mkdir(parents=True)
            runs.write_text(tsv if tsv is not None else
                            "1\tA\tsid-a\t20260909T181351Z-speedometer3-sid-a\tclean\tspeedometer3\n")
            cp = bash('python3 "$WK_ROOT/lib/wkdata.py" ab-legs %s' % shlex.quote(str(root)))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            return cp.stdout

    def test_it_counts_what_ran_against_what_the_job_planned(self):
        """Two warmup legs, then rounds x plans x arms -- the warmup round runs
        the first plan only, one leg per arm."""
        self.assertIn("3 of 14 planned", self._legs())

    def test_an_older_experiments_results_are_not_this_jobs(self):
        """The volume keeps every result it has ever produced."""
        out = self._legs()
        self.assertNotIn("20260101", out)
        self.assertNotIn(" 42s", out)

    def test_a_leg_the_map_names_carries_its_round_and_arm(self):
        self.assertRegex(self._legs(), r"1\s+A\s+speedometer3\s+31s\s+clean")

    def test_the_warmup_legs_are_the_ones_before_any_measured_round(self):
        out = self._legs()
        self.assertRegex(out, r"warmup\s+A\s+speedometer3\s+91s")
        self.assertRegex(out, r"warmup\s+B\s+speedometer3\s+92s")

    def test_the_leg_in_flight_is_not_called_a_warmup(self):
        """A row reaches the map when its leg ends, so the running leg is never
        in it -- and calling it a warmup misreports which round is under way."""
        out = self._legs()
        self.assertRegex(out, r"-\s+B\s+motionmark\s+running")
        import re as _re
        self.assertEqual(2, len([l for l in out.splitlines()
                                 if _re.match(r"warmup\s+[AB]\s", l)]))

    def test_a_job_that_has_not_started_says_so_rather_than_listing_the_volume(self):
        out = self._legs(started="")
        self.assertIn("no leg of this job", out)

    def test_an_empty_warmup_directory_is_reported_and_not_passed_over(self):
        """The warmup round exists to carry a profile the measured rounds
        cannot take, so an empty capture directory is that round wasted --
        and it is the command's job to say so, not a person's to go and look."""
        self.assertRegex(self._legs(), r"warmup captures: none in .*/warmup")

    def test_a_capture_that_landed_is_named(self):
        with scratch_dir() as root:
            (root / "job.json").write_text(json.dumps({"plans": ["speedometer3"], "rounds": 1, "arms": []}))
            (root / "autorun.state").write_text("job_stamp=S\nstarted_at=2026-01-01T00:00:00Z\n")
            leg = root / "results" / "20260101T000100Z-speedometer3-sid-a"
            leg.mkdir(parents=True)
            (leg / "env.json").write_text(json.dumps({"plan": "speedometer3", "wall_time_s": "30"}))
            w = root / "ab" / "S" / "warmup"
            w.mkdir(parents=True)
            (w / "speedometer3-A.json.gz").write_bytes(b"")
            cp = bash('python3 "$WK_ROOT/lib/wkdata.py" ab-legs %s' % shlex.quote(str(root)))
            self.assertIn("warmup captures: speedometer3-A.json.gz", cp.stdout)


class TestTheDriverAnswersFromAnotherMachine(WkTest):
    """Every board's driver probes over the tailnet from anywhere; this one
    reports `unknown from here` only if it refuses to try."""

    PRE = """. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/boot/machines.sh"
NODE_NAME=mbp
NODE_SSH=fakemac
NODE_VOLUME="WK Bench"
NODE_DISPLAY="builtin 1470x956"
NODE_BENCH_SSH=fakemac-bench
. "$WK_ROOT/boot/mac-volume.sh"
"""

    def _driver(self, script, env=None):
        return bash(self.PRE + script, env=env)

    def test_it_is_probeable_off_the_mac(self):
        cp = self._driver('if b_probeable; then echo YES; else echo NO; fi')
        self.assertEqual(cp.stdout.strip(), "YES", cp.stdout + cp.stderr)

    def test_nothing_about_the_mac_is_stored_between_reads(self):
        """Every fact above is recomputed; the only file the driver keeps is
        the record of a person's arming."""
        text = (REPO / "lib" / "wk" / "boot" / "mac.py").read_text()
        self.assertNotIn("cache", text.lower())
        self.assertEqual(1, text.count('lead=("mac-record.sh",)'))


if __name__ == "__main__":
    unittest.main()
