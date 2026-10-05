"""The Mac A/B's front half (lib/wk/bench/mac_ab.py's MacAB) against FakeMac; the back half is test_mac_ab_rounds."""
import contextlib
import io
import json
import os
import re
import shlex
import sys
import types
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, requires_machine, scratch_dir, temp_store
from tests.test_mac_volume import BENCH_GROUP, FakeGuest, FakeMac, conf_for

sys.path.insert(0, str(REPO / "lib"))
from wk import act, places, sched  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import ab, cli, mac, mac_ab, record  # noqa: E402
from wk.boot.mac import DRIVERS, HELPER, Channel  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

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
    """This machine: its own digest of the tree, a cached samply, and a notifier that is told what went out."""
    here = Fake("here")
    here.answer(["hostname", "-s"], out="moose\n")
    here.answer(["python3"], out=DIGEST + "\n")
    here.answer(["sh", "-c", 'wc -c < "$1"'], out="42\n")
    here.answer(["test", "-x"])   # samply.fetch finds it in the cache
    here.notified = []
    return here


class ManagerReg(places.Registry):
    """The arms' workspace on the Mac's manager, a place of its own: its store a scratch tree this test reads in place."""

    def __init__(self, far, **kw):
        super().__init__(REPO, **kw)
        self.far = far

    def ws_place(self, ws):
        return "manager"

    def walk(self):
        return ["manager"]

    def load(self, name):
        if name != "manager":
            return super().load(name)
        return types.SimpleNamespace(kind="remote", probe=lambda: ("answering", ""), task_store=lambda: (Local(), self.far),
                                     results=lambda ws: (Local(), os.path.join(self.far, "ws", ws, "bench")))


@contextlib.contextmanager
def world(kind="mac-volume", env=None, **o):
    """A MacAB over a fake Mac, this machine a Fake, the store a scratch directory, every prompt answered yes."""
    with temp_store() as store, scratch_dir() as tree:
        for part in ("bench", "boot", "lib", "machines"):
            os.symlink(REPO / part, tree / part)
        (tree / "manager" / "ws" / "mac-rel").mkdir(parents=True)
        clock, here = FakeClock(), here_fake()
        e = {"HOME": str(tree), "WK_STORE": store["WK_STORE"]}
        e.update(env or {})
        conf = conf_for(kind)
        manager = Fake("tolken")
        fake = PlantMac(conf, env=e, clock=clock, manager=manager) if kind == "mac-volume" else PlantGuest(conf, env=e, clock=clock)
        fake.write_system("perf-macos-tolken-1")
        driver = DRIVERS[kind](REPO, conf, fake)
        reg = ManagerReg(str(tree / "manager"), env=e, machine=here)
        opts = dict({"devices": "mbp" if kind == "mac-volume" else "benchvm", "systems": "sid-a,sid-b", "workspace": "mac-rel"}, **o)
        m = mac_ab.MacAB(tree, reg, clock, "", opts, cli.Bench(tree, reg, clock), driver=lambda root, c: driver)
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

    def test_only_the_bench_volume_group_as_the_default_passes(self):
        for boot, ok, said in ((None, True, BENCH_GROUP), ("host", False, "the host install"),
                               (Result(0, "a:b:11111111-2222-3333-4444-555555555555\n"), False, "neither install"),
                               (Result(1), record.UNKNOWN, "no boot-volume")):
            with self.subTest(said=said), world() as m:
                ready(m)
                if isinstance(boot, str):
                    m.fake.firmware = boot
                elif boot is not None:
                    m.fake.answer(r"^boot-volume", boot)
                self.assertEqual(ok, m.firmware_is_bench())
                self.assertIn(said, m.fw_detail)


class TestOnlyTheDeclaredDisplay(WkTest):

    def test_one_online_display_of_the_declared_kind(self):
        ext = {"builtin": False, "online": True, "points": [3840, 2160]}
        for doc, want, ok, said in ((BENCH_DISPLAY, "builtin", True, "builtin 1470x956"), (HOST_DISPLAY, "builtin", True, ""),
                                    ({"displays": [BENCH_DISPLAY["displays"][0], ext]}, "builtin", False, "2 online display(s)"),
                                    ({"displays": [BENCH_DISPLAY["displays"][0], ext]}, "builtin", False, "external 3840x2160"),
                                    ({"displays": [ext]}, "builtin", False, "not the builtin panel"),
                                    ({"displays": [{"online": True, "points": [1280, 800]}]}, "external", True, ""),
                                    ({"displays": []}, "builtin", False, "0 online display(s)"),
                                    ({"displays": [BENCH_DISPLAY["displays"][0], dict(ext, online=False)]}, "builtin", True, ""),
                                    ("", "builtin", None, "answered nothing"), ("not json", "builtin", None, "did not print JSON")):
            with self.subTest(said=said, doc=doc):
                got, detail = mac_ab.display_verdict(doc if isinstance(doc, str) else json.dumps(doc), want)
                self.assertEqual(ok, got)
                self.assertIn(said, detail)


class TestThePinnedDisplayIsConfig(WkTest):
    def test_a_machine_that_declares_no_display_refuses_the_plant(self):
        with world() as m:
            ready(m)
            m.d.conf["display"] = ""
            m.create_task("20260908T000000Z")
            got, err = said(m.plant)
        self.assertIs(got, Refused)
        self.assertIn("machines/mbp.conf", err)


class TestPreflight(WkTest):

    def test_each_gate_fails_by_name_and_a_ready_mac_is_clean_and_changed_nothing(self):
        two = {"displays": [BENCH_DISPLAY["displays"][0], {"online": True, "points": [3840, 2160]}]}
        empty = lambda m: m.fake.answer(r"^ls -1 .*/staged", Result(0, ""))   # noqa: E731
        for o, setup, n, named in (({}, lambda m: None, 0, "preflight clean"),
                                  ({}, lambda m: setattr(m.fake, "up", False), 1, "nothing else was checked"),
                                  ({}, lambda m: m.fake.enter_bench(), 4, "BENCH mode"),
                                  ({}, lambda m: m.fake.files.update({m.fake.firstboot_log(): ""}), 1,
                                   "wk sysimage build perf-macos-tolken --repair"),
                                  ({}, lambda m: setattr(m.fake, "helper", None), 1, "wk machine setup mbp"),
                                  ({}, empty, 1, "nothing on the volume"), ({"systems": "", "patch": "HEAD"}, empty, 0, "--patch stages both arms"),
                                  ({}, lambda m: m.fake.answer(r"^displays$", Result(0, json.dumps(two))), 1, "No --force crosses it"),
                                  ({"kind": "mac-guest"}, lambda m: None, 0, "enters bench mode"),
                                  ({"kind": "mac-guest"}, lambda m: setattr(m.fake, "marked", False), 1, "carries no /etc/wk-image")):
            with self.subTest(named), world(**o) as m:
                setup(m)
                got, err = said(ready(m).preflight)
                self.assertEqual(got, n, err)
                self.assertIn(named, err)
                self.assertEqual(mutations(m), [])


class TestItSharesTheBoardABsRefusals(WkTest):
    def refused(self, spec="", **o):
        with world(**o) as m:
            m.spec = spec
            got, err = said(m.check)
        self.assertIs(got, Refused, err)
        return err

    def test_each_refusal_names_its_reason(self):
        for said, o in (("two different arms", {"systems": "sid-a,sid-a"}), ("staged builds", {"spec": "wpe:1725"}),
                        ("--slot is a board A/B's", {"slot": "a"}), ("--rounds takes a number", {"rounds": "0"}),
                        ("is not a plan name", {"plans": ["a b"]}), ("below --rounds", {"rounds": "9", "max_rounds": "4"}),
                        ("--workspace <ws>", {"systems": "", "patch": "HEAD", "workspace": ""}), ("--workspace <ws>", {"workspace": ""}),
                        ("One or the other", {"patch": "HEAD", "workspace": "mac-rel"})):
            with self.subTest(said=said, o=o):
                self.assertIn(said, self.refused(**o))

    def test_the_task_is_written_in_its_workspace_on_the_machine_holding_it(self):
        with world() as m:
            ready(m)
            m.create_task("20260101T000000Z")
            m.lock.release_all()
            self.assertEqual(m.taskdir, os.path.join(m.reg.far, "ws", "mac-rel", "bench", "20260101T000000Z-mbp-mac-ab"))
            self.assertTrue(os.path.isfile(os.path.join(m.taskdir, "task.json")))
            self.assertEqual(m.logs, os.path.join(m.reg.store.records_dir(), "log", "20260101T000000Z-mbp-mac-ab"))
        with world(workspace="gone") as m:
            ready(m)
            got, err = said(m.create_task, "20260101T000000Z")
        self.assertIs(got, Refused)
        self.assertIn("no workspace 'gone'", err)

    def test_a_board_is_refused_a_macs_option(self):
        with temp_store() as store:
            reg = places.Registry(REPO, env={"WK_STORE": store["WK_STORE"], "HOME": "/nonexistent"}, machine=Fake())
            got, err = said(ab.run, REPO, reg, FakeClock(), "", {"devices": "rpi5", "systems": "a,b", "patch": "x"})
        self.assertIs(got, Refused)
        self.assertIn("--patch is a Mac A/B's", err)


class TestThePlant(WkTest):

    def plant(self, m, setup=lambda m: None):
        ready(m)
        setup(m)
        m.create_task(m.clock.stamp())
        return said(m.plant)

    def job(self, m):
        return json.loads(m.here_fake.files[os.path.join(m.logs, "job.json")])

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
        with world(env={"WK_FORCE": "1"}) as m:
            self.plant(m)
            job = self.job(m)
        self.assertNotIn("force", job)
        self.assertEqual(job["rehearsal"], "")

    def test_the_task_it_writes_is_one_wk_status_can_read(self):
        with world() as m:
            self.plant(m)
            st = record.task_state(m.taskdir, False)
        self.assertEqual(record.subject_line(st["doc"]).split(" · ")[0], "sid-a vs sid-b")
        self.assertIn("mbp", record.subject_line(st["doc"]))

    def test_what_did_not_land_or_read_back_as_asked_is_refused(self):
        ans = lambda pat, res: lambda m: m.fake.answer(pat, res)   # noqa: E731
        for o, setup, named in (({"systems": "sid-a,sid-x"}, lambda m: None, "no staged build 'sid-x'"),
                                ({}, ans(r"wk-tools'? --exclude", Result(0, "e" * 64 + "\n")), "what landed is not this tree"),
                                ({}, ans(r"^wc -c .*job.json", Result(0, "7\n")), "could not write the job"),
                                ({}, ans(r"wk_quiet_dnd_on", Result(0, "off\n")), "Do Not Disturb"),
                                ({}, ans(r"^test -r .*lib/wk/bench/autorun.py", Result(1)), "carries no lib/wk/bench/autorun.py")):
            with self.subTest(named), world(**o) as m:
                got, err = self.plant(m, setup)
                self.assertIs(got, Refused)
                self.assertIn(named, err)
                if o:
                    self.assertEqual(mutations(m), [], "an arm not staged is refused before anything lands")

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

    def test_a_screensaver_it_cannot_turn_off_is_refused_unless_forced(self):
        idle = lambda m: m.fake.answer(r"^defaults read .*idleTime", Result(0, "300\n"))   # noqa: E731
        with world() as m:
            got, err = self.plant(m, idle)
        self.assertIs(got, Refused)
        self.assertIn("--force proceeds anyway", err)
        with world(env={"WK_FORCE": "1"}) as m:
            got, err = self.plant(m, idle)
        self.assertIsNone(got, err)
        self.assertIn("FORCED past a barrier: could not disable the screensaver", err)

    def test_the_planted_samply_is_made_executable_over_there(self):
        with world() as m:
            self.plant(m)
        self.assertTrue([c for c in m.fake.ran if re.match(r"chmod 0755 .*/cache/samply/.*/samply$", c)], m.fake.ran)

    def test_the_agent_runs_the_tree_the_plant_verified(self):
        with world() as m:
            self.plant(m)
            plist = m.here_fake.files[os.path.join(m.logs, mac_ab.AGENT + ".plist")]
        self.assertIn("/var/wk/wk-tools/lib/wk/bench/autorun.py", plist)
        self.assertNotIn("KeepAlive", plist)

    def test_the_autorun_state_is_reset_to_this_job(self):
        with world() as m:
            self.plant(m)
            state = m.here_fake.files[os.path.join(m.logs, "planted.state")]
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

    def wait(self, m, same_boot=False):
        ready(m)
        m.boot_before = m.d.boot_id() if same_boot else "1"
        return said(m.wait)

    def test_the_answer_and_the_boot_name_the_outcome(self):
        for bench, same_boot, came in ((True, False, "bench"), (False, False, "host"), (False, True, "noreboot")):
            with self.subTest(came=came), world() as m:
                if bench:
                    m.fake.enter_bench()
                self.assertEqual(self.wait(m, same_boot)[0], came)

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
    """Read-only: the preflight of each machine the Mac A/B plants on."""

    def preflight(self, machine):
        reg = places.Registry(REPO, machine=None)
        clock = FakeClock()
        m = mac_ab.MacAB(REPO, reg, clock, "", {"devices": machine}, cli.Bench(REPO, reg, clock))
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

    def _legs(self, started="2026-09-09T18:08:20Z"):
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
            for name, wall in (("20260101T000000Z-speedometer3-sid-a", 42),
                               ("20260909T181045Z-speedometer3-sid-a", 91),
                               ("20260909T181218Z-speedometer3-sid-b", 92),
                               ("20260909T181351Z-speedometer3-sid-a", 31),
                               ("20260909T181500Z-motionmark-sid-b", None)):
                d = root / "results" / name
                d.mkdir(parents=True)
                env = {"plan": name.split("-")[1]}
                if wall is not None:
                    env["wall_time_s"] = str(wall)
                (d / "env.json").write_text(json.dumps(env))
            runs = root / "ab" / "20260909T180544Z" / "runs.tsv"
            runs.parent.mkdir(parents=True)
            runs.write_text("1\tA\tsid-a\t20260909T181351Z-speedometer3-sid-a\tclean\tspeedometer3\n")
            cp = bash('python3 "$WK_ROOT/lib/wkdata.py" ab-legs %s' % shlex.quote(str(root)))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            return cp.stdout

    def test_each_leg_of_this_job_is_named_by_round_and_arm(self):
        out = self._legs()
        self.assertIn("3 of 14 planned", out)
        self.assertNotIn("20260101", out, "an older A/B task's results are not this job's")
        self.assertNotIn(" 42s", out)
        self.assertRegex(out, r"1\s+A\s+speedometer3\s+31s\s+clean")
        self.assertRegex(out, r"warmup\s+A\s+speedometer3\s+91s")
        self.assertRegex(out, r"warmup\s+B\s+speedometer3\s+92s")
        self.assertRegex(out, r"-\s+B\s+motionmark\s+running")
        self.assertEqual(2, len([l for l in out.splitlines() if re.match(r"warmup\s+[AB]\s", l)]))
        self.assertRegex(out, r"warmup captures: none in .*/warmup")

    def test_a_job_that_has_not_started_says_so_rather_than_listing_the_volume(self):
        self.assertIn("no leg of this job", self._legs(started=""))

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
    def test_it_is_probeable_off_the_mac(self):
        conf = {"name": "mbp", "ssh": "fakemac", "bench_ssh": "fakemac-bench", "volume": "WK Bench"}
        self.assertTrue(DRIVERS["mac-volume"](REPO, conf, Channel(conf, {}, via=Fake("here"))).probeable())


if __name__ == "__main__":
    unittest.main()
