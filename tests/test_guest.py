"""lib/wk/guest.py against a fake host: start and stop, the guests' daemons under launchd, the forwards, the
credentials and the injector's CA a guest is given, its clock set from the host, and a build machine's provisioning hash."""
import contextlib
import io
import json
import os
import plistlib
import shlex
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import Fake, FakeClock
from tests.support import REPO, WkTest, guest_step, live_selected, owed, stub_path
from tests.test_shell_path import BUILD_DIR, _run_install_fn, compile_commands_dir
from tests.test_wk_places import DriverConformance
from tests.test_wk_secrets import SECRETFILE, SecretsTest, World, quiet

sys.path.insert(0, str(REPO / "lib"))
from wk import agents, daemons, doctor, guest, places, tools, wall  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Local, Result  # noqa: E402
from wk.machine_cmd import deps  # noqa: E402
from wk.store import GUEST_BROKER_SOCKET, Store  # noqa: E402


TART = "/fake/tart"
IP = "192.168.2.5"
ADDR = "192.168.2.1"
INSTALL_AGENTS = guest.Guest.install_agents   # GuestTest records the step; TestTheSteps runs it
BASH_STEPS = [s[0] for s in guest.STEPS if s[0] in ("write_shell_rc", "write_lldbinit", "write_checkout", "install_agents",
                                                    "write_claude_config", "settle_desktop", "report_desktop")]


SERVES = {"com.wk.proxy": "WK_PROXY_TCP", "com.wk.push": "WK_PUSH_SOCKET", "com.wk.github-inject": "WK_INJECT_SOCK"}


class GuestWorld(World):
    """One host with one guest, `wk-demo`, in `state`; `bridge` is whether this host has its guest-bridge address yet;
    launchd as the `loaded` labels and the `serving` sockets."""

    def __init__(self, base):
        super().__init__(base)
        self.env.update({"WK_VM_STORE": base + "/vmstore", "WK_LOCK_DIR": base + "/locks", "PATH": os.environ.get("PATH", "")})
        self.state, self.bridge, self.ssh_rc, self.bench, self.guest_sock = "running", True, 0, False, False
        self.guest_cmds, self.loaded, self.serving, self.listened = [], set(), set(), []
        self.react([TART, "list"], lambda a, f: Result(0, json.dumps([{"Name": "wk-demo", "State": f.state, "Source": "local"},
                                                                      {"Name": "wk-base", "State": "stopped", "Source": "local"}])))
        self.answer([TART, "ip"], out=IP + "\n")
        self.answer([TART, "get"], out='{"CPU": 4, "Memory": 8192}')
        self.answer([TART, "stop"])
        self.react([TART, "exec"], self._guest)
        self.react(["ifconfig"], lambda a, f: Result(0, "\tinet %s netmask 0xffffff00\n" % ADDR if f.bridge else "\tinet 10.0.0.2\n"))
        self.answer(["test", "-x"])
        self.answer(["hostname", "-s"], out="host\n")
        self.answer(["sh", "-c", 'printf %s "$HOME"'], out=self.env["HOME"])
        self.react(["python3", SECRETFILE, "present"], lambda a, f: Result(0 if f.files.get(a[3]) else 1))
        self.react(["python3", "-c", daemons.SOCKET_ANSWERS], lambda a, f: Result(0 if a[-1] in f.serving else 1))
        self.react(["/usr/bin/python3", "-c", guest.PUSH_LISTEN], lambda a, f: f.listened.append(a[-1]) or Result(0))
        self.react(["launchctl", "print"], lambda a, f: Result(0 if a[-1].rsplit("/", 1)[1] in f.loaded else 113))
        self.react(["launchctl", "bootout"], lambda a, f: f.loaded.discard(a[-1].rsplit("/", 1)[1]) or Result(0))
        self.react(["launchctl", "bootstrap"], lambda a, f: f._launch(plistlib.loads(f.files[a[-1]].encode()), "RunAtLoad"))
        self.react(["launchctl", "kickstart"], lambda a, f: f._launch(f.agent(a[-1].rsplit("/", 1)[1]), "Label"))

    def agent(self, label):
        return plistlib.loads(self.files[self.env["HOME"] + "/Library/LaunchAgents/%s.plist" % label].encode())

    def _launch(self, doc, starts):
        self.loaded.add(doc["Label"])
        if doc[starts]:
            self.serving.add(doc["EnvironmentVariables"].get(SERVES.get(doc["Label"])))
        return Result(0)

    def _guest(self, argv, _):
        cmd = argv[-1]
        self.guest_cmds.append((cmd, self.last_input))
        if cmd.startswith("test -f /etc/wk-image"):
            return Result(0 if self.bench else 1)
        if cmd.startswith("test -S"):
            return Result(0 if self.guest_sock else 1)
        return Result(self.ssh_rc)

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        self.guest_sock = self.guest_sock or bool(pid and "-R" in argv)
        return pid

    def spawned(self, word):
        return [e[1] for e in self.effects if e[0] == "spawn" and word in " ".join(e[1])]

    def bootstraps(self, label):
        return [e for e in self.effects if e[0] == "run" and e[1][:2] == ("launchctl", "bootstrap") and e[1][-1].endswith(label + ".plist")]

    def state_of(self):
        return dict(self.files), sorted(self.pids), sorted(self.loaded), sorted(self.serving)


class GuestTest(SecretsTest):
    def setUp(self):
        super().setUp()
        self.w = GuestWorld(self.tmp)
        self.steps, self.step_ok, self.admit_rc, self.notes = [], {}, 0, []

        def step(fn):
            def run(_guest):
                self.steps.append(fn)
                return self.step_ok.get(fn, True)
            return run

        def admit(host, name, mine):
            if self.admit_rc:
                raise Refused(self.admit_rc)
        for obj, name, fn in ((Store, "macos_host", mock.PropertyMock(return_value=True)),
                              (places.Vm, "tart", lambda s: TART),
                              (guest, "admit", admit),
                              (guest, "login_note", lambda env: self.notes.append(1)),
                              (daemons, "stamp", lambda program: "1"),
                              (tools, "push", lambda *a: True)) + tuple((guest.Guest, fn, step(fn)) for fn in BASH_STEPS):
            p = mock.patch.object(obj, name, new_callable=lambda fn=fn: fn) if isinstance(fn, mock.PropertyMock) \
                else mock.patch.object(obj, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.clock = FakeClock()
        self.vm = places.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")

    @property
    def vmdir(self):
        return self.w.env["WK_VM_STORE"] + "/vm"

    def run_start(self):
        """(ip or None, stderr): a refusal is None."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                ip = guest.start(self.vm, "demo", self.clock)
            except Refused:
                ip = None
        return ip, err.getvalue()

    def hold(self, resource, pid):
        """`resource`'s lock, held by `pid` (alive where it is in the fake's process table)."""
        path = self.vm.store.lock_path(resource)
        self.w.mkdir_now(os.path.dirname(path))
        self.w.symlink("pid=%d tok=beef at=2026-09-24T00:00:00Z cmd=wk start" % pid, path)
        return path

    def host(self):
        return guest.Host(self.vm, self.clock)


class TestVmConformance(GuestTest, DriverConformance):
    cls, ws, down, platform = places.Vm, "demo", "stopped", "macos"

    def stopped(self):
        self.w.state = "stopped"
        self.w.write(os.path.join(self.vm.store.ws_dir("demo"), places.READY_MARKER), "")
        return self.vm

    def brought_up(self, t):
        return [r[-1] for r in self.w.spawned(" run ")] == ["wk-demo"]


class TestOneLockPerGuest(GuestTest):
    def test_one_lock_per_resource_guest_start_and_stop(self):
        self.w.pids.add(4242)
        self.hold("guest-demo", 4242)
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("waiting for the guest-demo lock (held by pid 4242)", err)
        self.assertEqual([], self.steps, "a step ran under another start's lock")
        with self.assertRaises(Refused):
            quiet(guest.stop, self.vm, "demo", self.clock)
        self.assertNotIn(("act", (TART, "stop", "wk-demo")), self.w.effects)

    def test_a_dead_holder_is_no_holder(self):
        self.hold("guest-demo", 4243)
        ip, err = self.run_start()
        self.assertEqual(IP, ip, err)


class TestBothArms(GuestTest):
    def test_a_running_guest_is_converged_and_not_booted(self):
        ip, err = self.run_start()
        self.assertEqual(IP, ip, err)
        self.assertEqual([], self.w.spawned(" run "), "a running guest was booted again")
        self.assertEqual(BASH_STEPS, self.steps)
        self.assertEqual([1], self.notes, "the login is stated once, on the one exit")

    def test_a_stopped_guest_is_admitted_then_booted_filtered_with_the_mirror_alone(self):
        self.w.state = "stopped"
        ip, err = self.run_start()
        self.assertEqual(IP, ip, err)
        run = self.w.spawned(" run ")[0]
        self.assertIn("--net-softnet-block=0.0.0.0/0", run)
        self.assertIn("--net-softnet-allow=%s/32" % ADDR, run)
        self.assertEqual(["--dir=mirror:%s:ro,tag=wk-mirror" % os.path.dirname(self.vm.store.mirror_dir())],
                         [a for a in run if a.startswith("--dir")])
        self.assertEqual("wk-demo", run[-1])
        self.assertTrue(run[1].startswith("PATH=/usr/local/bin:"), "tart finds softnet through PATH")
        self.assertNotIn(self.vmdir + "/demo.unfiltered", self.w.files)

    def test_a_refused_admission_boots_nothing(self):
        self.w.state, self.admit_rc = "stopped", 1
        ip, _ = self.run_start()
        self.assertIsNone(ip)
        self.assertEqual([], self.w.spawned(" run "))

    def test_an_unfiltered_guest_is_marked_and_gets_no_softnet(self):
        self.w.state = "stopped"
        self.w.env["WK_VM_UNFILTERED"] = "1"
        self.vm = places.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")
        ip, err = self.run_start()
        self.assertEqual(IP, ip, err)
        self.assertIn("open network", err)
        self.assertFalse([a for a in self.w.spawned(" run ")[0] if "softnet" in a])
        self.assertIn(self.vmdir + "/demo.unfiltered", self.w.files)
        self.assertEqual([], self.w.bootstraps("com.wk.proxy"), "an unfiltered guest needs no proxy")

    def test_no_softnet_refuses_before_tart_runs(self):
        self.w.state = "stopped"
        self.w.answer(["test", "-x"], rc=1)
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("softnet is not installed", err)
        self.assertEqual([], self.w.spawned(" run "))

    def test_a_guest_that_never_comes_up_names_its_run_log(self):
        self.w.state = "stopped"
        self.w.answer([TART, "ip"], rc=1)
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("did not come up within 180s", err)
        self.assertIn("demo.run.log is empty", err)

    def test_a_guest_whose_agent_never_answers_is_refused(self):
        self.w.state, self.w.ssh_rc = "stopped", 255
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("guest agent never answered", err)

    def test_a_failed_step_is_named_and_the_start_goes_on(self):
        self.step_ok["write_shell_rc"] = False
        ip, err = self.run_start()
        self.assertEqual(IP, ip)
        self.assertIn("could not wire demo's shell", err)

    def test_a_refused_desktop_refuses_the_start(self):
        self.step_ok["report_desktop"] = False
        ip, _ = self.run_start()
        self.assertIsNone(ip)

    def test_an_absent_guest_is_refused(self):
        self.w.react([TART, "list"], lambda a, f: Result(0, "[]"))
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("no such workspace: demo", err)

    def test_a_dry_run_changes_nothing(self):
        before = self.w.state_of()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            ip, err = self.run_start()
        self.assertEqual(IP, ip, err)
        self.assertEqual(before, self.w.state_of())
        self.assertIn("would run on wk-demo: env WK_ADDR=%s" % ADDR, err)

    def test_a_dry_run_of_a_stopped_guest_ends_at_its_boot(self):
        self.w.state = "stopped"
        before = self.w.state_of()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            ip, err = self.run_start()
        self.assertEqual("", ip, err)
        self.assertEqual(before, self.w.state_of())
        self.assertEqual("wk-demo", self.w.spawned(" run ")[0][-1])
        self.assertEqual([], [e for e in self.w.effects if e[0] == "run" and e[1][1:2] == ("ip",)])
        self.assertEqual([], self.steps)


class TestTheSteps(GuestTest):
    def the_guest(self):
        return guest.Guest(guest.Host(self.vm, self.clock), "demo", self.vm.guest("demo"))

    def test_the_marker_is_written_and_a_bench_image_keeps_none(self):
        g = self.the_guest()
        quiet(g.write_marker)
        self.assertIn("name=demo", "".join(i for c, i in self.w.guest_cmds if ".wk-workspace" in c))
        self.w.bench, self.w.guest_cmds = True, []
        quiet(g.write_marker)
        self.assertTrue([c for c, _ in self.w.guest_cmds if c.startswith("rm -rf") and ".wk-workspace" in c])

    def test_the_agents_are_installed_as_every_driver_installs_them(self):
        g = self.the_guest()
        self.assertTrue(quiet(INSTALL_AGENTS, g)[0])
        cmds = [c for c, _ in self.w.guest_cmds]
        self.assertIn(shlex.join(["bash", "-lc", "export WK_AGENTS_CACHE=%s WK_CLAUDE_VERSION=%s\n%s" % (agents.HOME_CACHE, agents.CLAUDE_VERSION, agents.script(str(REPO)))]), cmds)
        self.assertIn(shlex.join(["bash", "-lc", "python3 %s/claude/workspace-config.py %s" % (self.vm.tools("demo"), self.vm.src("demo"))]),
                      cmds)

    def test_a_container_and_a_macos_guest_each_get_their_own_helix_languages(self):
        """`unit helix.macos_languages`: firstrun gives a container glibc's --malloc-trim and jsc-release's build; a guest, neither."""
        with tempfile.TemporaryDirectory() as home:
            _run_install_fn("_install_helix", "x86_64", home, curl="exit 0\n", sudo="exit 0\n", tar='mkdir -p "$3"\n')
            tomllib.loads(Path(home, ".config/helix/config.toml").read_text())
            linux = Path(home, ".config/helix/languages.toml").read_text()
        self.assertTrue(quiet(self.the_guest().write_helix_config)[0])
        (mac,) = [i for c, i in self.w.guest_cmds if 'cat > "$HOME/.config/helix/languages.toml"' in c]
        args = [tomllib.loads(t)["language-server"]["clangd"]["args"] for t in (linux, mac)]
        self.assertEqual([(True, "--compile-commands-dir=/src/WebKit/" + BUILD_DIR), (False, "--compile-commands-dir=" + self.vm.src("demo"))],
                         [("--malloc-trim" in a, compile_commands_dir(a)) for a in args])

    def test_a_credential_file_its_reader_refuses_stops_the_delivery(self):
        self.w.answer(["python3", SECRETFILE, "read"], rc=2, err="wk: refusing to read ...\n")
        g = self.the_guest()
        self.assertFalse(quiet(g.write_agent_secrets)[0])
        self.assertFalse([c for c, _ in self.w.guest_cmds if ".wk-litellm-key" in c])

    def test_the_push_forward_listens_for_this_guest_and_ends_at_the_socket_a_workspace_dials(self):
        g = self.the_guest()
        self.assertTrue(quiet(g.push_forward)[0])
        self.assertEqual(["demo"], self.w.listened)
        (fwd,) = self.w.spawned(" -N ")
        self.assertIn("/Users/admin/.wk-push.sock:%s/demo.push.sock" % self.vmdir, fwd)
        self.assertEqual(1, len(self.w.bootstraps("com.wk.push")))
        self.assertNotIn("KEY:", "".join(i for _, i in self.w.guest_cmds))

    def test_no_forward_is_made_when_the_service_would_not_listen(self):
        self.w.react(["/usr/bin/python3", "-c", guest.PUSH_LISTEN], lambda a, f: Result(1))
        self.assertFalse(quiet(self.the_guest().push_forward)[0])
        self.assertEqual([], self.w.spawned(" -N "))


class TestTheOverrides(GuestTest):
    def host(self, **env):
        self.w.env.update(env)
        return guest.Host(places.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm"), self.clock)

    def test_the_proxy_address_is_read_off_the_live_interface_on_the_guest_subnet(self):
        self.assertEqual(ADDR, self.host().proxy_addr())
        self.w.bridge = False
        self.assertEqual("10.0.0.2", self.host(WK_VM_SUBNET="10.0.0").proxy_addr())
        self.assertEqual("172.16.9.1", self.host(WK_VM_SUBNET="172.16.9").proxy_addr(), "no address there is its .1")

    def test_each_override_reaches_what_it_names(self):
        h = self.host(WK_VM_PROXY_ADDR="203.0.113.9", WK_VM_PROXY_PORT="9999", WK_SOFTNET_BIN="/opt/softnet")
        self.assertEqual("203.0.113.9", h.proxy_addr())
        self.assertEqual("9999", guest.proxy_port(h.env))
        self.assertEqual("/opt/softnet", guest.softnet_bin(h.env))
        self.assertEqual(100, guest.vm_disk_gb({"WK_VM_DISK_GB": "100"}))
        self.assertEqual([], quiet(self.host(WK_VM_UNFILTERED="1").softnet_flags)[0])


class TestTheDaemons(GuestTest):
    def test_the_proxy_is_started_on_demand_once_the_bridge_has_its_address(self):
        ok, err = quiet(self.host().start_proxy)
        self.assertTrue(ok, err)
        env = self.w.agent("com.wk.proxy")["EnvironmentVariables"]
        self.assertEqual(("%s:3128" % ADDR, self.vmdir + "/github-inject.sock", self.vmdir + "/github-inject-read.sock",
                          Store(self.w.env).push_socket()),
                         (env["WK_PROXY_TCP"], env["WK_INJECT_SOCK"], env["WK_INJECT_READ_SOCK"], env["WK_PUSH_SOCKET"]))
        self.assertFalse(self.w.agent("com.wk.proxy")["RunAtLoad"], "no bridge, nothing to bind, until a guest starts")
        self.assertEqual([], self.w.bootstraps("com.wk.github-inject"), "setup installs the injector")
        self.w.effects.clear()
        self.assertTrue(quiet(self.host().start_proxy)[0])
        self.assertEqual([], self.w.bootstraps("com.wk.proxy"), "a serving proxy is left alone")

    def test_a_bridge_that_never_gets_its_address_starts_no_proxy(self):
        self.w.bridge = False
        self.w.env["WK_VM_PROXY_ADDR"] = ADDR
        self.vm = places.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")
        ok, err = quiet(self.host().start_proxy)
        self.assertFalse(ok)
        self.assertIn("never got address", err)
        self.assertEqual([], self.w.bootstraps("com.wk.proxy"))

    def test_an_injector_that_does_not_answer_is_named_with_its_setup_stage(self):
        ok, err = quiet(self.host().start_inject)
        self.assertFalse(ok)
        self.assertIn("./setup --stage inject", err)
        self.w.serving.add(self.vmdir + "/github-inject.sock")
        self.assertTrue(quiet(self.host().start_inject)[0])

    def test_the_injectors_row_names_its_files_the_login_it_holds_and_the_machine_it_publishes_into(self):
        row = self.host().daemons()["github-inject"]
        self.assertEqual((self.vmdir + "/github-inject.sock", self.vmdir + "/claude-inject.sock", self.vmdir + "/github-inject-read.sock",
                          self.vm.store.podman_machine()),
                         (row.env["WK_INJECT_SOCK"], row.env["WK_INJECT_PLAIN_SOCK"], row.env["WK_INJECT_READ_SOCK"],
                          row.env["WK_INJECT_PUBLISH_MACHINE"]))
        self.assertEqual((guest.Host(self.vm).secrets.cred_path("claude-login"),) * 2, (row.env["WK_INJECT_CLAUDE_LOGIN"], row.lock))
        self.assertTrue(row.boot)

    def test_the_push_service_is_kept_by_launchd_and_ensured_once(self):
        h = self.host()
        self.assertTrue(quiet(h.start_push)[0])
        self.assertTrue(quiet(h.start_push)[0])
        self.assertEqual(1, len(self.w.bootstraps("com.wk.push")))
        env = self.w.agent("com.wk.push")["EnvironmentVariables"]
        self.assertEqual((self.vmdir, Store(self.w.env).push_socket()), (env["WK_PUSH_GUEST_DIR"], env["WK_PUSH_SOCKET"]))

    def test_waiting_for_a_running_guest_starts_a_dead_proxy_only(self):
        for case, state, live, starts in (("dead proxy", "running", False, 1), ("live proxy", "running", True, 0),
                                          ("stopped guest", "stopped", False, 0)):
            with self.subTest(case):
                self.setUp()
                quiet(self.host().start_proxy)
                self.w.state, self.w.effects = state, []
                if not live:
                    self.w.serving.clear()
                ws_dir = self.vm.store.ws_dir("demo")
                self.w._set_file(os.path.join(ws_dir, places.READY_MARKER), "")
                quiet(lambda: self.vm.wait_ready("demo", self.clock))
                self.assertEqual(starts, len([e for e in self.w.effects if e[0] == "act" and e[1][:2] == ("launchctl", "kickstart")]))

    def test_a_guests_wall_rows_name_each_daemons_remedy(self):
        w = wall.Wall(str(REPO), self.vm, "demo", self.w)
        with mock.patch.object(wall.Wall, "inside", lambda self, cmd: "000"):
            self.assertEqual("wk start demo", w.github()[0][2])
            for rows in (w.github_read(), w.bugzilla_read()):
                self.assertIn("./setup --stage inject", rows[0][2], rows)


class TestTheForward(GuestTest):
    def setUp(self):
        super().setUp()
        self.h = guest.Host(self.vm, self.clock)
        self.g = self.vm.guest("demo")
        self.pidfile = self.h.forward_pidfile("demo", "push")
        self.far, self.near = "/Users/admin/.wk-push.sock", self.h.path("demo.push.sock")

    def start(self):
        return quiet(self.h.forward_start, "demo", self.g, "push", self.far, self.near)[0]

    def test_a_second_start_while_one_is_alive_is_a_no_op(self):
        self.assertTrue(self.start())
        self.assertTrue(self.start())
        forwards = self.w.spawned(" -N ")
        self.assertEqual(1, len(forwards), forwards)
        self.assertIn("/Users/admin/.wk-push.sock:%s/demo.push.sock" % self.vmdir, forwards[0])
        self.assertIn("ProxyCommand=%s/container/ssh-transport vm demo" % REPO, forwards[0], "over tart exec, not the network")
        self.assertIn(int(self.w.files[self.pidfile]), self.w.pids)

    def test_a_forward_whose_guest_end_never_serves_is_stopped(self):
        spawn = self.w.spawn

        def never_serves(argv, log):
            pid = spawn(argv, log)
            self.w.guest_sock = False
            return pid
        with mock.patch.object(self.w, "spawn", never_serves):
            self.assertFalse(self.start())
        (pid,) = [e[1] for e in self.w.effects if e[0] == "kill"]
        self.assertNotIn(self.pidfile, self.w.files)
        self.assertGreaterEqual(sum(self.clock.slept), guest.FORWARD_WAIT)

    def test_a_pidfile_whose_pid_is_gone_is_started_again(self):
        self.w._set_file(self.pidfile, "4194304\n")
        self.assertTrue(self.start())
        self.assertEqual(1, len(self.w.spawned(" -N ")))

    def test_both_ends_take_the_forwards_lock(self):
        self.w.pids.add(4242)
        self.hold("vm-push-forward-demo", 4242)
        for fn in (self.start, lambda: self.h.forward_stop("demo", "push")):
            with self.assertRaises(Refused):
                quiet(fn)
        self.assertEqual([], self.w.spawned(" -N "))

    def test_stopping_a_guest_ends_its_forward_first(self):
        self.start()
        pid = int(self.w.files[self.pidfile])
        quiet(guest.stop, self.vm, "demo", self.clock)
        kill = self.w.effects.index(("kill", pid, 15))
        self.assertLess(kill, self.w.effects.index(("act", (TART, "stop", "wk-demo"))))
        self.assertNotIn(self.pidfile, self.w.files)

    def test_a_start_forwards_the_host_broker_to_the_socket_the_guests_client_dials(self):
        self.assertTrue(quiet(guest.Guest(self.h, "demo", self.g).broker_forward)[0])
        (fwd,) = self.w.spawned(" -N ")
        self.assertIn("/Users/admin/.wk-broker.sock:%s" % Store(self.w.env).runtime_socket(), fwd)
        self.assertIn(self.h.forward_pidfile("demo", "broker"), self.w.files)

    def test_stopping_a_guest_ends_every_forward(self):
        self.start()
        quiet(guest.Guest(self.h, "demo", self.g).broker_forward)
        quiet(guest.stop, self.vm, "demo", self.clock)
        for what in guest.FORWARDS:
            self.assertNotIn(self.h.forward_pidfile("demo", what), self.w.files)


class TestTheGuestsCredentials(GuestTest):
    def creds(self):
        return [self.w.files.get(self.vmdir + "/" + f) for f in ("read-github-pat", "push-github-pat", "push-bugzilla-api-key")]

    def test_each_start_writes_every_file_the_guests_injector_reads(self):
        self.w.seed()
        self.assertTrue(quiet(guest.Host(self.vm, self.clock).credentials_converge)[0])
        self.assertEqual(["ghp-held\n", "ghp-held\n", "bz-held\n"], self.creds())

    def test_a_credential_withdrawn_here_is_withdrawn_there(self):
        self.w.seed(bz=None)
        self.w._set_file(self.vmdir + "/push-bugzilla-api-key", "stale\n")
        quiet(guest.Host(self.vm, self.clock).credentials_converge)
        self.assertEqual(["ghp-held\n", "ghp-held\n", None], self.creds())

    def test_a_far_side_that_refuses_is_named(self):
        self.w.seed()
        self.w.react(["sh", "-c"], lambda a, f: Result(1))
        ok, err = quiet(guest.Host(self.vm, self.clock).credentials_converge)
        self.assertFalse(ok)
        self.assertIn("could not converge the tokens in %s" % self.vmdir, err)

    def test_where_there_are_no_guests_nothing_is_converged(self):
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=False):
            self.assertTrue(guest.credentials_converge(str(REPO), self.w.env, self.w))
        self.assertEqual([None, None, None], self.creds())


def _a_running_guest():
    """(vm place, name) of a guest up on this host, or None; nothing is started to find one."""
    if not live_selected() or sys.platform != "darwin":
        return None
    try:
        vm = places.Registry(str(REPO)).load("vm")
        up = [n for n, state in vm.list() if state == "running"]
    except (LookupError, Refused):
        return None
    return (vm, up[0]) if up else None


class _LiveGuest(unittest.TestCase):
    """Read-only against a guest already running on this host; it starts nothing."""
    wk_tier = "live"

    def setUp(self):
        self.found = _a_running_guest()
        if self.found is None:
            self.skipTest("live tier not selected, or no macOS guest is running on this host")


class TestTheLiveGuest(_LiveGuest):
    def ask(self, script):
        vm, ws = self.found
        return vm.exec(ws, ["bash", "-lc", script], timeout=60)

    def test_vm_egress(self):
        """`live vm.egress[<check>]`: nothing reaches out bypassing the proxy, PyPI does through it, and every
        rc file sources the egress block."""
        vm, ws = self.found
        if not vm.egress_filtered(ws):
            self.skipTest("'%s' was booted with WK_VM_UNFILTERED" % ws)
        checks = {"direct": ("curl -s -o /dev/null --max-time 5 --noproxy '*' https://pypi.org/simple/", False),
                  "pypi": ("curl -sf -o /dev/null --max-time 30 https://pypi.org/simple/pip/", True),
                  "zprofile": ("grep -qF .wk-egress \"$HOME/.zprofile\"", True)}
        for check, (script, reaches) in checks.items():
            with self.subTest(check=check):
                r = self.ask(script)
                self.assertEqual(reaches, r.ok, r.out + r.err)

    @owed("live inject.claude_login[vm]: the CLI accepting the placeholder and the host injector's swap are measured only "
          "against the real CLI and api.anthropic.com")
    def test_inject_claude_login(self):
        """`live inject.claude_login[vm]`: the guest holds the placeholder and no token, and a session answers through the
        host's injector."""
        vm, ws = self.found
        rows = wall.Wall(str(REPO), vm, ws, vm.machine).claude_login()
        self.assertEqual([], [r for r in rows if r[0] != doctor.OK], rows)
        r = self.ask("claude -p 'Reply with the single word OK.'")
        self.assertIn("OK", r.out, r.err)

    def test_vm_shared_mirror(self):
        """`live vm.shared_mirror`: the checkout is --shared off the mirror share, and its alternates resolve."""
        vm, ws = self.found
        r = self.ask("cat %s/.git/objects/info/alternates && git -C %s cat-file -e HEAD"
                     % (vm.src(ws), vm.src(ws)))
        self.assertTrue(r.ok, r.out + r.err)
        self.assertIn(vm.mirror_dir() + "/objects", r.out)


class TestTheLiveRemount(_LiveGuest):
    def test_sync_guest_remount(self):
        """`live sync.guest_remount`: after the remount the guest reads the host mirror's main as the host does."""
        vm, ws = self.found
        host = vm.machine.run(["git", "-C", vm.store.mirror_dir(), "rev-parse", "refs/heads/main"])
        self.assertEqual(vm.remount_mirror(ws), "")
        r = vm.exec(ws, ["git", "-C", vm.mirror_dir(), "rev-parse", "refs/heads/main"], timeout=60)
        self.assertEqual((r.ok, r.out.strip()), (True, host.out.strip()), r.err)


class TestTheLiveWayIn(_LiveGuest):
    """`tart exec` as the one way into a real guest, and what rides it; it writes only under /tmp in there."""

    def test_vm_tart_exec(self):
        """`live vm.tart_exec`: a command runs as the guest's user with its home, its status comes back, a binary file
        crosses both ways byte for byte, and a detached job outlives the exec that started it."""
        vm, ws = self.found
        g = vm.guest(ws)
        r = g.run(["sh", "-c", 'echo "$(id -un) $HOME"; exit 3'])
        self.assertEqual((r.rc, r.out.strip()), (3, "%s %s" % (vm.user(), vm.home())), r.err)
        with tempfile.TemporaryDirectory() as d:
            data = bytes(range(256)) * 64
            with open(os.path.join(d, "in"), "wb") as f:
                f.write(data)
            g.copy_in(os.path.join(d, "in"), "/tmp/wk-live-copy")
            g.copy_out("/tmp/wk-live-copy", os.path.join(d, "out"))
            with open(os.path.join(d, "out"), "rb") as f:
                self.assertEqual(f.read(), data)
        pid = g.spawn(["sleep", "30"], "/tmp/wk-live-spawn.log")
        self.assertTrue(g.alive(pid), "a detached job died with its tart exec")
        g.kill(pid)

    def test_vm_mirror_tag_mount(self):
        """`live vm.mirror_tag_mount`: the base's mirror job mounted the mirror's own tag at boot, where mirror_dir is."""
        vm, ws = self.found
        r = vm.exec(ws, ["sh", "-c", "mount | grep -F %s && test -d %s" % (shlex.quote(" on %s (" % places.GUEST_MIRROR_MOUNT),
                                                                        shlex.quote(vm.mirror_dir()))], timeout=60)
        self.assertTrue(r.ok, r.out + r.err)

    def test_vm_guest_jobs(self):
        """`live vm.guest_jobs[<job>]`: the three jobs the base renders for itself (daemons.guest_rows) are loaded in a
        running guest, nosleep and mirror in launchd's system domain and display in the user's, running what the row says."""
        vm, ws = self.found
        for word, row in daemons.guest_rows(vm.home(), guest.display(vm.env)).items():
            with self.subTest(job=word):
                sudo, domain = ("sudo -n ", "system") if row.domain == "system" else ("", 'gui/$(id -u)')
                r = vm.exec(ws, ["sh", "-c", "%slaunchctl print %s/com.wk.%s" % (sudo, domain, word)], timeout=60)
                self.assertTrue(r.ok, r.out + r.err)
                self.assertIn(" ".join(row.argv), " ".join(r.out.split()))

    def test_vm_ssh_transport(self):
        """`live vm.ssh_transport`: ssh over the guest's sshd on tart exec answers, as the editor's alias and the
        socket forwards use it."""
        vm, ws = self.found
        r = vm.machine.run(vm.ssh_argv(ws) + ["id", "-un"], timeout=60)
        self.assertEqual((r.rc, r.out.strip()), (0, vm.user()), r.err)

    def test_vm_broker_forward(self):
        """`live vm.broker_forward`: `wk sync`'s broker client in a guest reaches the host's broker."""
        vm, ws = self.found
        r = vm.exec(ws, ["env", "WK_BROKER_SOCKET=%s/%s" % (vm.home(), GUEST_BROKER_SOCKET), "python3",
                         vm.tools(ws) + "/container/broker/wk-broker-client.py", "capabilities"], timeout=60)
        self.assertTrue(r.ok, r.out + r.err)


# Pins `date -u +%s` to FAKE_EPOCH, so the skew is arithmetic rather than two reads of a moving clock.
FAKE_DATE = """#!/bin/sh
if [ "$1" = "-u" ] && [ "$2" = "+%s" ]; then echo "$FAKE_EPOCH"; exit 0; fi
exec /bin/date "$@"
"""


HOST_EPOCH = 1788900000


# `printf`, not `echo`: the first argument is `-n`.
FAKE_SUDO = """#!/bin/sh
printf '%s\\n' "$*" >> "$SUDO_LOG"
exit ${SUDO_RC:-0}
"""


class TestGuestClock(WkTest):
    def _drive(self, skew, sudo_rc=0, tolerance=None):
        """Set the clock of a guest whose clock is `skew` seconds behind the host's (negative for ahead).
        Returns (rc=<0|1> and what was said, sudo log text)."""
        datebin = self.tmp / "date-bin"
        sudobin = self.tmp / "sudo-bin"
        for d, name, body in ((datebin, "date", FAKE_DATE),
                              (sudobin, "sudo", FAKE_SUDO)):
            d.mkdir(exist_ok=True)
            (d / name).write_text(body)
            (d / name).chmod(0o755)
        log = self.tmp / "sudo.log"
        log.write_text("")
        env = {"HOME": str(self.tmp), "WK_VM_STORE": str(self.tmp / "vmstore"), "WK_STORE": str(self.tmp / "store")}
        if tolerance:
            env["WK_VM_CLOCK_SKEW"] = tolerance
        guest_env = {"PATH": "%s:%s:%s" % (datebin, sudobin, os.environ["PATH"]), "FAKE_EPOCH": str(HOST_EPOCH - skew),
                     "SUDO_LOG": str(log), "SUDO_RC": str(sudo_rc)}
        err = io.StringIO()
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch.dict(os.environ, guest_env), contextlib.redirect_stderr(err):
            vm = places.Registry(str(REPO), env=env).load("vm")
            g = guest.Guest(guest.Host(vm, FakeClock(HOST_EPOCH)), "testguest", Local())
            rc = 0 if g.set_guest_clock() else 1
        return "rc=%d\n%s" % (rc, err.getvalue()), log.read_text()

    def test_a_guest_behind_or_ahead_of_the_host_is_set_from_it(self):
        for skew, said in ((11 * 86400, "950400s"), (-3600, "3600s")):
            with self.subTest(skew=skew):
                out, sudolog = self._drive(skew)
                self.assertIn("rc=0", out, out)
                self.assertIn("clock was", out, out)
                self.assertIn(said, out, out)
                self.assertIn("-n date -u ", sudolog, sudolog)

    def test_a_guest_already_in_time_is_left_alone(self):
        out, sudolog = self._drive(0)
        self.assertIn("rc=0", out, out)
        self.assertNotIn("clock was", out, out)
        self.assertEqual("", sudolog.strip(), sudolog)

    def test_a_guest_that_refuses_the_write_fails(self):
        out, _ = self._drive(86400, sudo_rc=1)
        self.assertIn("rc=1", out, out)

    def test_wk_vm_clock_skew_sets_the_tolerance(self):
        out, sudolog = self._drive(300, tolerance="600")
        self.assertEqual("", sudolog.strip(), out)
        out, sudolog = self._drive(300)
        self.assertIn("-n date -u ", sudolog, out)


# `tart`: one running guest called wk-demo, and the guest itself as a directory. `tart exec` hands the guest's
# command as the last argument for a login shell, and everything it writes it writes under $HOME, so running that command with HOME
# pointed at a scratch directory exercises the real umask, mkdir, redirect and rm -- not a transcript of them.
# /Users/admin is rewritten to that directory for the same reason. `tart list` mixes local VMs with cached OCI images, so
# Source matters. Every invocation is appended to $WK_TEST_LOG.
FAKE_TART = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"running","Source":"local"}]' ;;
ip)   echo 1.2.3.4 ;;
exec)
    printf '%s\\n' "$*" >> "$WK_TEST_LOG"
    for a in "$@"; do last="$a"; done
    cmd=$(printf '%s' "$last" | sed "s|/Users/admin|$WK_TEST_GUEST|g")
    HOME="$WK_TEST_GUEST" SHELL=$(command -v bash) bash -lc "$cmd"; rc=$?
    [ -t 0 ] || cat >/dev/null
    exit $rc ;;
*)    exit 1 ;;
esac
'''


def _guest(tmp, name="demo"):
    """A scratch guest home, and the ready marker without which Vm.info says `creating`."""
    home = tmp / "guest-home"
    home.mkdir(parents=True, exist_ok=True)
    vmstore = tmp / "vmstore"
    ws = vmstore / "ws" / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".wk-ready").write_text("")
    return home, vmstore


def _stub_env(binp, home, log, store, vmstore, **extra):
    return {"PATH": f"{binp}:{os.environ['PATH']}", "WK_TEST_GUEST": str(home), "WK_TEST_LOG": str(log),
            "WK_STORE": str(store), "WK_HOST_SECRETS": str(store / "secrets"), "WK_VM_STORE": str(vmstore),
            "WK_VM_PROXY_ADDR": "192.0.2.1", **extra}


class TestTheGuestGetsTheInjectorsCa(WkTest):
    def _egress(self, home, vmstore, ca_text=None, bugzilla=None, **extra):
        log = self.tmp / "guest.log"
        log.write_text("")
        vmdir = vmstore / "vm"
        vmdir.mkdir(parents=True, exist_ok=True)
        if ca_text is not None:
            (vmdir / "wk-github-ca.pem").write_text(ca_text)
        with stub_path({"tart": FAKE_TART}) as binp:
            env = _stub_env(binp, home, log, self.tmp / "store", vmstore, XDG_STATE_HOME=str(self.tmp / "state"), **extra)
            return guest_step(env, "set_guest_egress", secrets={"bugzilla_user": lambda s: bugzilla})

    def test_the_bugzilla_placeholder_goes_in_with_the_login_and_not_without(self):
        home, vmstore = _guest(self.tmp)
        ca = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
        self._egress(home, vmstore, ca_text=ca)
        self.assertNotIn("BUGS_WEBKIT_ORG", (home / ".wk-egress").read_text())
        self._egress(home, vmstore, ca_text=ca, bugzilla="me@example.test")
        rc = (home / ".wk-egress").read_text()
        self.assertIn("export BUGS_WEBKIT_ORG_USERNAME=me@example.test\n", rc)
        self.assertIn("export BUGS_WEBKIT_ORG_PASSWORD=wk-injects-this\n", rc)

    def test_the_ca_and_the_placeholder_go_in_with_the_proxy(self):
        home, vmstore = _guest(self.tmp)
        self._egress(home, vmstore, ca_text="-----BEGIN CERTIFICATE-----\nx\n"
                                            "-----END CERTIFICATE-----\n")
        rc = (home / ".wk-egress").read_text()
        self.assertIn("http_proxy=http://192.0.2.1:3128", rc)
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", rc)
        self.assertIn("GITHUB_COM_USERNAME=justinmichaud", rc)
        self.assertIn("PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring", rc)
        self.assertIn("REQUESTS_CA_BUNDLE=", rc)
        self.assertIn("CURL_CA_BUNDLE=", rc)
        self.assertIn("GIT_SSL_CAINFO=", rc)
        self.assertIn("BEGIN CERTIFICATE", (home / ".wk-github-ca.pem").read_text())

    def test_an_unfiltered_guest_gets_neither(self):
        home, vmstore = _guest(self.tmp)
        (home / ".wk-github-ca.pem").write_text("stale\n")
        self._egress(home, vmstore, ca_text="x\n", WK_VM_UNFILTERED="1")
        self.assertFalse((home / ".wk-github-ca.pem").exists())
        self.assertFalse((home / ".wk-ca-bundle.pem").exists())


def _host(case, **env):
    """A Host over this machine and a scratch vm store, as a macOS host has one."""
    mac = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
    mac.start()
    case.addCleanup(mac.stop)
    base = {"HOME": str(case.tmp / "home"), "PATH": os.environ["PATH"], "WK_STORE": str(case.tmp / "store"),
            "WK_HOST_SECRETS": str(case.tmp / "store" / "secrets"), "WK_VM_STORE": str(case.tmp / "vmstore"),
            "WK_LOCK_DIR": str(case.tmp / "locks"), "XDG_STATE_HOME": str(case.tmp / "state")}
    base.update(env)
    return guest.Host(places.Registry(str(REPO), env=base).load("vm"))


def _quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        result = fn(*args)
    return result, err.getvalue()


class TestTheGuestsInjectorGetsTheStandingCredentials(WkTest):
    def _start_inject(self, vmstore, pat=None, bz=None):
        store = self.tmp / "store"
        held = store / "push-keys"
        held.mkdir(parents=True, exist_ok=True)
        (store / "secrets").mkdir(parents=True, exist_ok=True)
        for name, value in (("github-pat", pat), ("bugzilla-api-key", bz)):
            if value is None:
                (held / name).unlink(missing_ok=True)
            else:
                (held / name).write_text(value)
        host = _host(self, WK_VM_STORE=str(vmstore))
        with mock.patch.object(daemons, "answers", lambda m, sock: True):
            return _quiet(host.start_inject)

    def files(self, vmstore):
        return {n: (vmstore / "vm" / n) for n in ("read-github-pat", "push-github-pat", "push-bugzilla-api-key")}

    def test_a_start_writes_them_from_what_this_host_holds(self):
        _, vmstore = _guest(self.tmp)
        ok, err = self._start_inject(vmstore, pat="ghp-not-a-real-token\n", bz="bz-not-a-real-key\n")
        self.assertTrue(ok, err)
        got = self.files(vmstore)
        self.assertEqual("ghp-not-a-real-token\n", got["read-github-pat"].read_text())
        self.assertEqual("ghp-not-a-real-token\n", got["push-github-pat"].read_text())
        self.assertEqual("bz-not-a-real-key\n", got["push-bugzilla-api-key"].read_text())
        self.assertEqual([0o600] * 3, [p.stat().st_mode & 0o777 for p in got.values()])

    def test_one_withdrawn_on_this_host_is_gone_at_the_next_start(self):
        _, vmstore = _guest(self.tmp)
        self._start_inject(vmstore, pat="ghp-not-a-real-token\n", bz="bz\n")
        self._start_inject(vmstore, pat=None, bz=None)
        self.assertEqual([False] * 3, [p.exists() for p in self.files(vmstore).values()])


class TestEveryGuestStartConvergesTheCredentials(WkTest):
    def test_a_start_that_finds_the_proxy_up_still_delivers_the_token(self):
        _, vmstore = _guest(self.tmp)
        store = self.tmp / "store"
        (store / "push-keys").mkdir(parents=True, exist_ok=True)
        (store / "secrets").mkdir(parents=True, exist_ok=True)
        (store / "push-keys" / "github-pat").write_text("ghp-todays\n")
        read_pat = vmstore / "vm" / "read-github-pat"
        (vmstore / "vm").mkdir(parents=True, exist_ok=True)
        read_pat.write_text("ghp-yesterdays\n")
        host = _host(self, WK_VM_PROXY_ADDR="192.0.2.1")
        with mock.patch.object(host, "ensure", lambda word: True), mock.patch.object(daemons, "answers", lambda m, sock: True), \
                mock.patch.object(host.clock, "wait_until", lambda *a: True):
            ok, err = _quiet(host.start_proxy)
        self.assertTrue(ok, err)
        self.assertEqual("ghp-todays\n", read_pat.read_text())


class TestTheBuildMachineRecord(unittest.TestCase):
    def _stale(self, marker_text):
        far = Fake("box")
        far.answer(["sh", "-c"], out=marker_text)
        t = places.Remote("box", str(REPO), {"HOME": "/tmp/wk-test-unused", "XDG_STATE_HOME": "/tmp/wk-test-unused/state"}, far)
        t.machine = far
        return deps.stale(t, REPO)

    def test_a_machine_provisioned_from_these_inputs_reads_fresh(self):
        self.assertIsNone(self._stale("place=otherbox\nroot=/home/x/wk\ninputs=%s\n" % deps.inputs_hash(REPO)))

    def test_a_machine_provisioned_before_the_record_or_not_at_all_says_so(self):
        for marker in ("place=otherbox\nroot=/home/x/wk\n", ""):
            with self.subTest(marker=marker):
                self.assertTrue(self._stale(marker))

    def test_a_changed_provisioning_script_makes_it_stale(self):
        self.assertIn("remote/provision.sh", self._stale("place=otherbox\nroot=/home/x/wk\ninputs=0000000000000000\n"))

    def _copy(self, edit=None):
        root = Path(tempfile.mkdtemp(prefix="wk-test-inputs-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "remote").mkdir()
        for f in ("provision.sh", "probe.sh", "deps.sh"):
            text = (REPO / "remote" / f).read_text()
            (root / "remote" / f).write_text(edit(f, text) if edit else text)
        return deps.inputs_hash(root)

    def test_what_the_box_runs_is_in_it(self):
        for f in ("deps.sh", "probe.sh", "provision.sh"):
            with self.subTest(f=f):
                self.assertNotEqual(self._copy(lambda g, t: t + "\n" if g == f else t), deps.inputs_hash(REPO))


class TestDoctorReportsIt(unittest.TestCase):
    def test_doctor_reports_the_machine_through_that_one_function(self):
        from tests.test_doctor import MISS, OK, build_doctor, deps
        asked = []

        def stale(driver, root):
            asked.append(driver.name)
            return "provisioned before this record existed" if driver.name == "old" else None
        with deps(probe=lambda t, root: "family=debian\n", stale=stale):
            doc = build_doctor()
            (old,), (fresh,) = list(doc.build_machine("old")), list(doc.build_machine("fresh"))
        self.assertEqual((old[0], old[2]), (MISS, "wk machine setup old"))
        self.assertEqual(fresh[0], OK)
        self.assertEqual(["old", "fresh"], asked)
