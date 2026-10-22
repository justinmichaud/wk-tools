"""lib/wk/daemons.py over a fake machine: the rows' renderings (the guest's three jobs exactly), the program stamp, one
ensure under systemd and under launchd in both domains, the prune, the dry run and the kill points."""
import contextlib
import glob
import io
import os
import plistlib
import sys
from unittest import mock

from tests import fakes
from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import daemons  # noqa: E402
from tests.fakes import FakeClock  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.daemons import Daemon  # noqa: E402
from wk.machine import Result  # noqa: E402
from wk.places import GUEST_MIRROR_MOUNT, GUEST_MOUNT_MIRROR, MIRROR_TAG  # noqa: E402

HOME = "/home/u"
UNITS = HOME + "/.config/systemd/user/"
AGENTS = HOME + "/Library/LaunchAgents/"
DAEMONS = "/Library/LaunchDaemons/"
SUDO = ("sudo", "-n")


def quiet(fn, *a):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        return fn(*a), err.getvalue()


class Box(fakes.World):
    """systemd and launchd as sets: `active` units, `loaded` labels, `serving` sockets; a name in `refuse` never starts.
    launchctl answers under `sudo -n` as it does bare, and a `sudo -n tee` lands what was piped into it."""

    def __init__(self):
        super().__init__(name="box")
        self.active, self.loaded, self.serving, self.refuse, self.boots = set(), set(), set(), set(), {}
        self.answer(["sh", "-c", 'printf %s "$HOME"'], out=HOME)
        self.answer(["systemctl", "--user"])
        self.react(["systemctl", "--user", "is-active"], lambda a, f: Result(0 if a[-1] in f.active else 3))
        self.react(["systemctl", "--user", "enable"], lambda a, f: Result(1) if a[-1] in f.refuse else f.active.add(a[-1]) or Result(0))
        self.react(["systemctl", "--user", "disable"], lambda a, f: f.active.discard(a[-1]) or Result(0))
        for pre in ((), SUDO):
            self.react([*pre, "launchctl", "print"], lambda a, f: Result(0 if a[-1].rsplit("/", 1)[1] in f.loaded else 113))
            self.react([*pre, "launchctl", "bootout"], self._bootout)
            self.react([*pre, "launchctl", "bootstrap"], self._bootstrap)
            self.react([*pre, "launchctl", "kickstart"], self._kickstart)
        self.react([*SUDO, "tee"], lambda a, f: f._set_file(a[-1], f.stdin) or Result(0))
        self.react([*SUDO, "rm", "-f"], lambda a, f: f._drop(a[-1]) or Result(0))
        self.react(["python3", "-c", daemons.SOCKET_ANSWERS], lambda a, f: Result(0 if a[-1] in f.serving else 1))
        self.lock_held, self.stdin = [], None

    def run(self, argv, input=None, timeout=None, stream=False):
        self.stdin = input
        return super().run(argv, input=input, timeout=timeout, stream=stream)

    def _bootout(self, a, f):
        label = a[-1].rsplit("/", 1)[1]
        self.lock_held.append(bool(glob.glob(os.path.join(os.environ.get("WK_TEST_LOGIN_DIR", "/nonexistent"), "claude-login@*.lock"))))
        f.loaded.discard(label)
        f.serving -= {self.boots.get(label, (None,))[0]}
        return Result(0)

    def _bootstrap(self, a, f):
        doc = plistlib.loads(f.files[a[-1]].encode())
        if doc["Label"] in f.refuse:
            return Result(5, err="Bootstrap failed: 5: Input/output error")
        f.loaded.add(doc["Label"])
        self.boots[doc["Label"]] = (doc["EnvironmentVariables"].get("SOCK"), doc["RunAtLoad"])
        if doc["RunAtLoad"]:
            f.serving.add(doc["EnvironmentVariables"].get("SOCK"))
        return Result(0)

    def _kickstart(self, a, f):
        label = a[-1].rsplit("/", 1)[1]
        if label in f.loaded and label not in f.refuse:
            f.serving.add(self.boots[label][0])
        return Result(0)

    def state(self):
        return dict(self.files), sorted(self.active), sorted(self.loaded)


class DaemonTest(WkTest):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(daemons, "stamp", lambda program: self.stamps.get(program, "1"))
        p.start()
        self.addCleanup(p.stop)
        self.stamps = {}
        self.box = Box()
        self.clock = FakeClock()

    def systemd(self, box=None):
        return daemons.Systemd(box or self.box, "/opt/wk-tools", "podman machine ssh wk -- ", self.clock)

    def rows(self):
        return daemons.systemd_rows("/opt/wk-tools", "/var/lib/wk", "")


class TestTheRenderings(DaemonTest):
    def test_a_unit_runs_its_program_from_the_tree_with_its_store_and_keeps_the_directory_workspaces_mount(self):
        for word, row in self.rows().items():
            with self.subTest(word):
                unit = self.systemd().render(row, dict(row.env, WK_PROGRAM_STAMP="7"))
                self.assertIn("ExecStart=/usr/bin/python3 /opt/wk-tools/%s\n" % daemons.PROGRAMS[word], unit)
                self.assertIn("Environment=WK_STORE=/var/lib/wk\n", unit)
                self.assertIn("Environment=WK_PROGRAM_STAMP=7\n", unit)
                self.assertIn("Type=notify\n", unit)
                self.assertIn("RuntimeDirectoryPreserve=yes\n", unit)

    def test_the_push_service_takes_no_user_namespace_and_the_injector_writes_only_its_store(self):
        rows = daemons.systemd_rows("/r", "/s", "/s/login/.credentials.json")
        self.assertNotIn("PrivateTmp", self.systemd().render(rows["push"], {}))
        inject = self.systemd().render(rows["github-inject"], rows["github-inject"].env)
        self.assertIn("ReadWritePaths=/s\n", inject)
        self.assertIn("Environment=WK_INJECT_CLAUDE_LOGIN=/s/login/.credentials.json\n", inject)
        self.assertEqual("/s/login/.credentials.json", rows["github-inject"].lock)
        self.assertIsNone(self.rows()["github-inject"].lock, "the podman machine's injector holds no login")

    def test_an_agent_is_kept_alive_from_login_or_started_on_demand(self):
        sup = daemons.Launchd(self.box, "/r", "/logs", self.clock)
        for boot in (True, False):
            doc = plistlib.loads(sup.render(Daemon("proxy", {"A": "1"}, "/s", boot=boot, keep=boot), {"A": "1"}).encode())
            self.assertEqual((doc["Label"], doc["RunAtLoad"], doc["KeepAlive"]), ("com.wk.proxy", boot, boot))
            self.assertEqual(["/usr/bin/python3", "/r/container/proxy/wk-proxy.py"], doc["ProgramArguments"])
            self.assertEqual(("1", daemons.LAUNCHD_PATH), (doc["EnvironmentVariables"]["A"], doc["EnvironmentVariables"]["PATH"]))
            self.assertEqual("/logs/proxy.log", doc["StandardErrorPath"])

    def test_the_guests_three_jobs_render_exactly_and_carry_no_stamp(self):
        sup = daemons.Launchd(self.box, "/r", "/logs", self.clock)
        rows = daemons.guest_rows("/Users/admin", "1280x800")
        self.assertEqual(daemons.GUEST_WORDS, tuple(rows))
        self.assertEqual(("system", "system", "user"), tuple(rows[w].domain for w in daemons.GUEST_WORDS))
        jobs = {"nosleep": (["/usr/bin/caffeinate", "-dimsu"], True),
                "mirror": ([GUEST_MOUNT_MIRROR, MIRROR_TAG, GUEST_MIRROR_MOUNT], {"SuccessfulExit": False}),
                "display": (["/Users/admin/.local/bin/wk-set-display", "1280", "800"], False)}
        for word, (argv, keep) in jobs.items():
            with self.subTest(word):
                want = plistlib.dumps({"Label": "com.wk." + word, "ProgramArguments": argv,
                                       "EnvironmentVariables": {"PATH": daemons.LAUNCHD_PATH}, "RunAtLoad": True, "KeepAlive": keep,
                                       "StandardOutPath": "/logs/%s.log" % word, "StandardErrorPath": "/logs/%s.log" % word}).decode()
                self.assertEqual(want, sup.body(rows[word]))
                self.assertNotIn("WK_PROGRAM_STAMP", want)

    def test_a_systemd_user_unit_refuses_a_command_outside_the_programs_and_the_system_domain(self):
        for row, why in ((daemons.guest_rows("/h", "1x1")["nosleep"], "runs a command outside PROGRAMS"),
                         (Daemon("proxy", {}, domain="system"), "is in the system domain")):
            err = io.StringIO()
            with self.subTest(why), contextlib.redirect_stderr(err), self.assertRaises(Refused):
                self.systemd().render(row, {})
            self.assertIn("a systemd user unit cannot hold %s: it %s" % (row.word, why), err.getvalue())


class TestTheProgramStamp(WkTest):
    def stamp(self, prog):
        with mock.patch.object(daemons, "TREE", str(self.tmp)):
            return daemons.stamp(prog)

    def test_an_imported_module_changes_it_and_an_unimported_one_does_not(self):
        (self.tmp / "lib" / "wk").mkdir(parents=True)
        (self.tmp / "lib" / "wk" / "__init__.py").write_text("")
        (self.tmp / "proxy").mkdir()
        (self.tmp / "proxy" / "relay.py").write_text("X = 1\n")
        for mod in ("held", "late", "other"):
            (self.tmp / "lib" / "wk" / (mod + ".py")).write_text("X = 1\n")
        (self.tmp / "proxy" / "prog.py").write_text("import json\nfrom relay import X\nfrom wk import held\n\ndef f():\n    from wk import late\n")
        first = self.stamp("proxy/prog.py")
        (self.tmp / "lib" / "wk" / "other.py").write_text("X = 2\n")
        self.assertEqual(first, self.stamp("proxy/prog.py"))
        for path in ("lib/wk/held.py", "lib/wk/late.py", "proxy/relay.py"):
            with self.subTest(path):
                before = self.stamp("proxy/prog.py")
                (self.tmp / path).write_text("X = 3\n")
                self.assertNotEqual(before, self.stamp("proxy/prog.py"))


class TestTheReadinessProbe(WkTest):
    def test_only_a_served_socket_or_listener_answers(self):
        import socket
        from wk.machine import Local
        served, dead, tcp = socket.socket(socket.AF_UNIX), socket.socket(socket.AF_UNIX), socket.socket()
        for s in (served, dead, tcp):
            self.addCleanup(s.close)
        served.bind(str(self.tmp / "served.sock"))
        served.listen(1)
        dead.bind(str(self.tmp / "dead.sock"))
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        for where, want in ((str(self.tmp / "served.sock"), True), (str(self.tmp / "dead.sock"), False),
                            (str(self.tmp / "absent.sock"), False), ("127.0.0.1:%d" % tcp.getsockname()[1], True)):
            with self.subTest(where):
                self.assertEqual(want, daemons.answers(Local(), where))


class RendererConformance:
    """What every renderer does with a row: systemd units in a Linux machine, launchd agents on a Mac. Each class names
    its renderer's unit for a word, converges one, plants one no row names, and makes one never answer."""

    def test_a_first_run_starts_it_and_a_second_changes_nothing(self):
        self.assertEqual((True, ["start " + self.name()]), self.converge()[::2])
        self.box.effects.clear()
        self.assertEqual((True, []), self.converge()[::2])
        self.assertEqual([], [e for e in self.box.effects if e[0] not in ("run",)])

    def test_a_changed_program_restarts_it_with_the_new_stamp(self):
        self.converge()
        self.stamps[self.PROGRAM] = "2"
        self.assertEqual((True, ["restart " + self.name()]), self.converge()[::2])
        self.assertIn(self.STAMPED, self.box.files[self.unit_file()])

    def test_one_that_never_answers_is_named_and_not_called_started(self):
        self.never_answers()
        ok, err, changes = self.converge("push")
        self.assertEqual((False, []), (ok, changes))
        self.assertIn("%s did not reach readiness -- no workspace here can push" % self.name("push"), err)

    def test_one_no_row_names_is_stopped_and_removed_and_the_rest_are_left(self):
        other = self.plant_stale()
        quiet(self.sup().prune, ("proxy", "push", "github-inject"))
        self.assertEqual(other, set(self.box.files))
        self.assertEqual((set(), set()), (self.box.active, self.box.loaded))


class TestSystemd(RendererConformance, DaemonTest):
    PROGRAM, STAMPED = "container/proxy/wk-proxy.py", "WK_PROGRAM_STAMP=2"

    def sup(self):
        return self._sup if hasattr(self, "_sup") else self.systemd()

    def name(self, word="proxy"):
        return "wk-%s.service" % word

    def unit_file(self):
        return UNITS + self.name()

    def converge(self, word="proxy", sup=None):
        sup = sup or self.systemd()
        ok, err = quiet(sup.ensure, self.rows()[word])
        return ok, err, sup.changes

    def never_answers(self):
        self.box.refuse.add(self.name("push"))

    def plant_stale(self):
        for name in ("wk-old.service", "wk-proxy.service", "other.service"):
            self.box.write(UNITS + name, "x")
        self.box.active.add("wk-old.service")
        return {UNITS + "wk-proxy.service", UNITS + "other.service"}

    def test_a_changed_program_stops_it_before_the_unit_is_rewritten(self):
        self.converge()
        self.box.effects.clear()
        self.stamps[self.PROGRAM] = "2"
        self.converge()
        order = [e[0] if e[0] != "run" else e[1][2] for e in self.box.effects if e[0] in ("write", "run") and e[1][:1] != ("sh",)]
        self.assertLess(order.index("disable"), order.index("write"))
        self.assertLess(order.index("write"), order.index("enable"))

    def test_a_unit_that_does_not_reach_readiness_names_where_to_look(self):
        self.never_answers()
        self.assertIn("podman machine ssh wk -- journalctl --user -u wk-push.service -e", self.converge("push")[1])

    def test_a_dry_run_says_what_it_would_do_and_does_none_of_it(self):
        self.box.write(UNITS + "wk-old.service", "x")
        before = self.box.state()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            sup = self.systemd()
            quiet(sup.prune, daemons.WORDS)
            ok, err, changes = self.converge("proxy", sup)
        self.assertEqual(before, self.box.state())
        self.assertEqual(["would have stopped and removed wk-old.service (no row names it)", "would start wk-proxy.service"], changes)
        self.assertIn("would run on box: systemctl --user enable --now wk-proxy.service", err)


class TestLaunchd(RendererConformance, DaemonTest):
    PROGRAM, STAMPED = "container/proxy/wk-proxy.py", "<key>WK_PROGRAM_STAMP</key>\n\t\t<string>2</string>"

    def setUp(self):
        super().setUp()
        self.login = str(self.tmp / "login" / ".credentials.json")
        os.makedirs(os.path.dirname(self.login))
        p = mock.patch.dict(os.environ, {"WK_TEST_LOGIN_DIR": os.path.dirname(self.login)})
        p.start()
        self.addCleanup(p.stop)
        self._sup = daemons.Launchd(self.box, "/r", "/logs", self.clock)

    def sup(self):
        return self._sup

    def name(self, word="proxy"):
        return "com.wk." + word

    def unit_file(self):
        return AGENTS + self.name() + ".plist"

    def row(self, word="github-inject", boot=True):
        return Daemon(word, {"SOCK": "/s/%s.sock" % word}, "/s/%s.sock" % word, boot=boot, keep=boot,
                      lock=self.login if word == "github-inject" else None)

    def acts(self):
        return [e[1] for e in self.box.effects if e[0] == "act"]

    def converge(self, word="proxy"):
        self._sup.changes = []
        ok, err = quiet(self._sup.ensure, self.row(word))
        return ok, err, self._sup.changes

    def never_answers(self):
        self.converge("push")
        self.box.refuse.add(self.name("push"))
        self.box.serving.clear()

    def plant_stale(self):
        self.box.write(AGENTS + "com.wk.old.plist", "x")
        self.box.write(AGENTS + "org.other.plist", "x")
        self.box.loaded.add("com.wk.old")
        return {AGENTS + "org.other.plist"}

    def test_a_changed_agent_is_booted_out_under_the_logins_lock_and_bootstrapped_back(self):
        self.converge("github-inject")
        self.assertEqual([], self.box.lock_held, "nothing was loaded to boot out")
        self.stamps["container/proxy/github-inject.py"] = "2"
        self.converge("github-inject")
        self.assertEqual([True], self.box.lock_held)
        self.assertIn("/logs", self.box.dirs)

    def test_an_on_demand_agent_is_bootstrapped_then_kickstarted(self):
        self.assertTrue(quiet(self._sup.ensure, self.row("proxy", boot=False))[0])
        self.assertIn(("run", ("launchctl", "kickstart", self._sup.svc(self.row("proxy")))), self.box.effects)
        self.assertIn("/s/proxy.sock", self.box.serving)

    def test_a_bootstrap_launchd_refuses_is_named_with_where_to_look(self):
        self.box.refuse.add("com.wk.github-inject")
        ok, err, _ = self.converge("github-inject")
        self.assertFalse(ok)
        self.assertIn("launchctl print gui/%d/com.wk.github-inject; the log is /logs/github-inject.log" % os.getuid(), err)

    def test_a_loaded_agent_that_never_answers_is_waited_for_its_whole_bound(self):
        self.never_answers()
        self.converge("push")
        self.assertGreaterEqual(sum(self.clock.slept), daemons.READY_WAIT)

    def guest(self, word, display="1280x800"):
        self._sup.changes = []
        ok, err = quiet(self._sup.ensure, daemons.guest_rows(HOME, display)[word])
        return ok, err, self._sup.changes

    def test_a_system_job_is_written_and_bootstrapped_under_sudo_and_is_served_once_loaded(self):
        plist = DAEMONS + "com.wk.nosleep.plist"
        self.assertEqual((True, ["start com.wk.nosleep"]), self.guest("nosleep")[::2])
        self.assertEqual([(*SUDO, "tee", plist), (*SUDO, "launchctl", "bootstrap", "system", plist)], self.acts())
        self.assertEqual(self._sup.body(daemons.guest_rows(HOME, "1280x800")["nosleep"]), self.box.files[plist])
        self.assertIn(("run", (*SUDO, "launchctl", "print", "system/com.wk.nosleep")), self.box.effects)
        self.assertNotIn("kickstart", str(self.box.effects), "a job without a socket is serving once loaded")
        self.box.effects.clear()
        self.assertEqual((True, []), self.guest("nosleep")[::2])
        self.assertEqual([], self.acts())

    def test_a_changed_system_job_is_booted_out_rewritten_and_bootstrapped_in_that_order(self):
        self.guest("mirror")
        self.box.effects.clear()
        with mock.patch("wk.places.GUEST_MIRROR_MOUNT", "/Volumes/elsewhere"):
            self.assertEqual((True, ["restart com.wk.mirror"]), self.guest("mirror")[::2])
        plist = DAEMONS + "com.wk.mirror.plist"
        self.assertEqual([(*SUDO, "launchctl", "bootout", "system/com.wk.mirror"), (*SUDO, "tee", plist),
                          (*SUDO, "launchctl", "bootstrap", "system", plist)], self.acts())
        self.assertIn("/Volumes/elsewhere", self.box.files[plist])

    def test_the_display_agent_is_the_users_own_and_set_from_the_display_asked_for(self):
        self.assertEqual((True, ["start com.wk.display"]), self.guest("display", "1920x1200")[::2])
        plist = AGENTS + "com.wk.display.plist"
        self.assertEqual([("launchctl", "bootstrap", "gui/%d" % os.getuid(), plist)], self.acts())
        doc = plistlib.loads(self.box.files[plist].encode())
        self.assertEqual(([HOME + "/.local/bin/wk-set-display", "1920", "1200"], False), (doc["ProgramArguments"], doc["KeepAlive"]))

    def test_a_system_job_launchd_refuses_names_sudo_launchctl_print(self):
        self.box.refuse.add("com.wk.nosleep")
        ok, err, _ = self.guest("nosleep")
        self.assertFalse(ok)
        self.assertIn("com.wk.nosleep did not reach readiness -- the guest sleeps between builds", err)
        self.assertIn("sudo -n launchctl print system/com.wk.nosleep; the log is /logs/nosleep.log", err)

    def test_a_stale_system_job_is_booted_out_and_removed_under_sudo(self):
        self.box.write(DAEMONS + "com.wk.old.plist", "x")
        self.box.write(DAEMONS + "org.other.plist", "x")
        self.box.loaded.add("com.wk.old")
        self.box.effects.clear()
        quiet(self._sup.prune, daemons.GUEST_WORDS)
        self.assertEqual([(*SUDO, "launchctl", "bootout", "system/com.wk.old"), (*SUDO, "rm", "-f", DAEMONS + "com.wk.old.plist")],
                         self.acts())
        self.assertEqual(({DAEMONS + "org.other.plist"}, set()), (set(self.box.files), self.box.loaded))


class TestTheCommand(DaemonTest):
    def test_the_podman_machine_runs_three_rows_from_its_mount_over_podman_machine_ssh(self):
        sup, rows, words = daemons.place("vm", {"WK_MACHINE": "m1"})
        self.assertEqual(sup.m.kind, "podman")
        self.assertEqual(("/opt/wk-tools", "podman machine ssh m1 -- ", ("proxy", "push", "github-inject")), (sup.root, sup.journal, words))
        self.assertEqual("", rows["github-inject"].env["WK_INJECT_CLAUDE_LOGIN"])

    def test_a_guest_runs_its_three_jobs_from_its_own_tree_and_home(self):
        sup, rows, words = daemons.place("guest", {"HOME": "/Users/admin", "WK_VM_DISPLAY": "1920x1200"})
        self.assertEqual(("here", daemons.TREE, "/Users/admin/.local/state/wk", daemons.GUEST_WORDS), (sup.m.name, sup.root, sup.logs, words))
        self.assertEqual(("/Users/admin/.local/bin/wk-set-display", "1920", "1200"), rows["display"].argv)
        self.assertEqual((GUEST_MOUNT_MIRROR, MIRROR_TAG, GUEST_MIRROR_MOUNT), rows["mirror"].argv)
        with mock.patch.object(daemons, "place", lambda where, env: (daemons.Launchd(self.box, "/r", "/logs", self.clock), rows, words)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                daemons.main(["guest", *words], {})
            self.assertEqual("changed 'start com.wk.nosleep'\nchanged 'start com.wk.mirror'\nchanged 'start com.wk.display'\n", out.getvalue())
            with self.assertRaises(SystemExit) as cm:
                daemons.main(["guest", "proxy"], {})
        self.assertIn("guest runs no proxy", str(cm.exception))

    def test_a_stage_reads_one_changed_line_per_change_and_an_unknown_word_is_refused(self):
        sup = self.systemd()
        with mock.patch.object(daemons, "place", lambda where, env: (sup, self.rows(), ("proxy", "push", "github-inject"))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                daemons.main(["vm", "proxy"], {})
            self.assertEqual("changed 'start wk-proxy.service'\n", out.getvalue())
            with self.assertRaises(SystemExit) as cm:
                daemons.main(["vm", "broker"], {})
        self.assertIn("vm runs no broker", str(cm.exception))
