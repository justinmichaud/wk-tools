"""lib/wk/guest.py against a fake host: start and stop, the host daemons, the forwards and the guests' `wk key push`."""
import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, live_selected, owed
from tests.test_wk_secrets import SECRETFILE, SecretsTest, World, quiet
from tests.test_wk_places import DriverConformance

sys.path.insert(0, str(REPO / "lib"))
from wk import agents, doctor, guest, places, secrets, tools, wall  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Killed, Result  # noqa: E402
from wk.store import GUEST_BROKER_SOCKET, Store  # noqa: E402

TART = "/fake/tart"
IP = "192.168.2.5"
ADDR = "192.168.2.1"
INSTALL_AGENTS = guest.Guest.install_agents   # GuestTest records the step; TestTheSteps runs it
BASH_STEPS = [s[0] for s in guest.STEPS if s[0] in ("write_shell_rc", "write_lldbinit", "write_checkout", "install_agents",
                                                    "write_claude_config", "settle_desktop", "report_desktop")]


class GuestWorld(World):
    """One host with one guest, `wk-demo`, in `state`; `bridge` is whether this host has its guest-bridge address yet."""

    def __init__(self, base):
        super().__init__(base)
        self.env.update({"WK_VM_STORE": base + "/vmstore", "WK_LOCK_DIR": base + "/locks", "PATH": os.environ.get("PATH", "")})
        self.state, self.bridge, self.ssh_rc, self.bench, self.guest_sock = "running", True, 0, False, False
        self.guest_cmds = []
        self.serving = {}
        self.react([TART, "list"], lambda a, f: Result(0, json.dumps([{"Name": "wk-demo", "State": f.state, "Source": "local"},
                                                                      {"Name": "wk-base", "State": "stopped", "Source": "local"}])))
        self.answer([TART, "ip"], out=IP + "\n")
        self.answer([TART, "get"], out='{"CPU": 4, "Memory": 8192}')
        self.answer([TART, "stop"])
        self.react([TART, "exec"], self._guest)
        self.react(["ifconfig"], lambda a, f: Result(0, "\tinet %s netmask 0xffffff00\n" % ADDR if f.bridge else "\tinet 10.0.0.2\n"))
        self.answer(["test", "-x"])
        self.react(["lsof", "-t"], self._lsof)
        self.answer(["find"])
        self.answer(["hostname", "-s"], out="host\n")
        self.react(["python3", SECRETFILE, "present"], lambda a, f: Result(0 if f.files.get(a[3]) else 1))
        self.react(["/usr/bin/python3", "-c"], lambda a, f: Result(0 if f.daemons(a[-1]) else 1))

    def _guest(self, argv, _):
        cmd = argv[-1]
        self.guest_cmds.append((cmd, self.last_input))
        if cmd.startswith("test -f /etc/wk-image"):
            return Result(0 if self.bench else 1)
        if cmd.startswith("test -S"):
            return Result(0 if self.guest_sock else 1)
        return Result(self.ssh_rc)

    def _lsof(self, argv, _):
        where = argv[-2] if argv[-1] == "-sTCP:LISTEN" else argv[-1]
        key = where.split("@", 1)[1] if where.startswith("-iTCP@") else where
        pids = [p for p, serves in self.serving.items() if key in serves and p in self.pids]
        return Result(0 if pids else 1, "".join("%d\n" % p for p in pids))

    def daemons(self, word):
        return sorted(p for p, serves in self.serving.items() if word in serves and p in self.pids)

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        joined = " ".join(argv)
        if not pid:
            return pid
        self.serving[pid] = joined.replace("WK_PROXY_TCP=", "").replace("WK_INJECT_SOCK=", "")
        if "wk-proxy.py" in joined:
            self.files[log] = "listening on %s:3128 (guest VMs)\n" % ADDR
        elif "github-inject.py" in joined:
            self._set_file(next(a.split("=", 1)[1] for a in argv if a.startswith("WK_INJECT_SOCK=")), "")
        elif "ssh-agent" in argv[0]:
            self.agents[argv[argv.index("-a") + 1]] = set()
        elif "-R" in argv:
            self.guest_sock = True
        return pid

    def spawned(self, word):
        return [e[1] for e in self.effects if e[0] == "spawn" and word in " ".join(e[1])]

    def state_of(self):
        return dict(self.files), {s: sorted(k) for s, k in self.agents.items()}, sorted(self.pids)


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


class TestVmConformance(GuestTest, DriverConformance):
    cls, ws, down, platform = places.Vm, "demo", "stopped", "macos"

    def stopped(self):
        self.w.state = "stopped"
        self.w.write(os.path.join(self.vm.store.ws_dir("demo"), places.READY_MARKER), "")
        return self.vm

    def brought_up(self, t):
        return [r[-1] for r in self.w.spawned(" run ")] == ["wk-demo"]


class TestOneLockPerGuest(GuestTest):
    def test_one_lock_per_resource_guest_start(self):
        self.w.pids.add(4242)
        self.hold("guest-demo", 4242)
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("waiting for the guest-demo lock (held by pid 4242)", err)
        self.assertIn("pid 4242 still holds it", err)
        self.assertEqual([], self.steps, "a step ran under another start's lock")

    def test_a_dead_holder_is_no_holder(self):
        self.hold("guest-demo", 4243)
        ip, err = self.run_start()
        self.assertEqual(IP, ip, err)

    def test_stop_takes_the_same_lock(self):
        self.w.pids.add(4242)
        self.hold("guest-demo", 4242)
        with self.assertRaises(Refused):
            quiet(guest.stop, self.vm, "demo", self.clock)
        self.assertNotIn(("act", (TART, "stop", "wk-demo")), self.w.effects)


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
        self.assertEqual([], self.w.spawned("wk-proxy.py"), "an unfiltered guest needs no proxy")

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

    def test_killpoints_guest_start(self):
        def world():
            w = GuestWorld(self.tmp)
            w.state = "stopped"
            return type("W", (), {"fake": w, "vm": places.Registry(str(REPO), env=w.env, machine=w).load("vm")})

        def run_once(w):
            quiet(guest.start, w.vm, "demo", FakeClock())
        def final(w):
            files = {p: ("live" if int(t) in w.fake.pids else "dead") if p.endswith(".pid") else t
                     for p, t in w.fake.files.items()}
            return files, [len(w.fake.daemons(d)) for d in ("wk-proxy.py", "github-inject.py")]
        converges(self, world, run_once, final, max_effects=80)


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
        self.assertIn(shlex.join(["bash", "-lc", agents.script(str(REPO))]), cmds)
        self.assertIn(shlex.join(["bash", "-lc", "python3 %s/claude/workspace-config.py %s" % (self.vm.tools("demo"), self.vm.src("demo"))]),
                      cmds)

    def test_a_credential_file_its_reader_refuses_stops_the_delivery(self):
        self.w.answer(["python3", SECRETFILE, "present"], rc=2, err="wk: refusing to read ...\n")
        g = self.the_guest()
        self.assertFalse(quiet(g.write_agent_secrets)[0])
        self.assertFalse([c for c, _ in self.w.guest_cmds if ".wk-litellm-key" in c])

    def test_the_deploy_config_names_the_proxy_and_the_forwarded_agent(self):
        self.w.seed()
        g = self.the_guest()
        self.assertTrue(quiet(g.write_deploy_keys)[0])
        cfg = next(i for c, i in self.w.guest_cmds if c.endswith('cat > "$HOME/.ssh/config"\''))
        self.assertIn("ProxyCommand /usr/bin/nc -X connect -x %s:3128 %%h %%p" % ADDR, cfg)
        self.assertIn("IdentityAgent /Users/admin/.wk-ssh-agent.sock", cfg)
        pubs = [i for c, i in self.w.guest_cmds if "id_fork.pub" in c and "cat >" in c]
        self.assertEqual(["PUB:fork\n"], pubs)
        self.assertNotIn("KEY:", "".join(i for _, i in self.w.guest_cmds))


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
        self.assertEqual("9999", h.port())
        self.assertEqual("/opt/softnet", h.softnet())
        self.assertEqual(100, guest.vm_disk_gb({"WK_VM_DISK_GB": "100"}))
        self.assertEqual([], quiet(self.host(WK_VM_UNFILTERED="1").softnet_flags)[0])


class TestTheDaemons(GuestTest):
    def host(self):
        return guest.Host(self.vm, self.clock)

    def test_the_proxy_is_started_once_the_bridge_has_its_address(self):
        ok, err = quiet(self.host().start_proxy)
        self.assertTrue(ok, err)
        argv = self.w.spawned("wk-proxy.py")[0]
        self.assertIn("WK_PROXY_TCP=%s:3128" % ADDR, argv)
        self.assertIn("WK_INJECT_SOCK=%s/github-inject.sock" % self.vmdir, argv)
        self.assertIn(self.vmdir + "/proxy.pid", self.w.files)
        self.assertEqual([], self.w.spawned("github-inject.py"), "launchd keeps the injector")

    def test_an_injector_that_does_not_answer_is_named_with_its_setup_stage(self):
        ok, err = quiet(self.host().start_inject)
        self.assertFalse(ok)
        self.assertIn("./setup --stage inject", err)
        self.w.serving[999] = self.vmdir + "/github-inject.sock"
        self.w.pids.add(999)
        self.w._set_file(self.vmdir + "/github-inject.sock", "")
        ok, err = quiet(self.host().start_inject)
        self.assertTrue(ok, err)
        self.assertNotIn("not running", err)

    def test_an_injector_wk_start_spawned_is_named_by_gc_and_stopped_by_setup(self):
        pidfile = self.vmdir + "/github-inject.pid"
        self.w.pids.add(555)
        self.w._set_file(pidfile, "555\n")
        self.w.answer(["ps", "-o", "command=", "-p", "555"], out="/usr/bin/python3 /t/container/proxy/github-inject.py\n")
        self.assertEqual([("inject", None, "./setup --stage inject")], [(r.kind, r.take, r.flag) for r in guest.rubble(self.vm)])
        self.assertTrue(quiet(self.host().retire_spawned_inject)[0])
        self.assertIn(("kill", 555, 15), self.w.effects)
        self.assertNotIn(pidfile, self.w.files)
        self.assertEqual([], guest.rubble(self.vm))
        self.assertFalse(quiet(self.host().retire_spawned_inject)[0])

    def test_a_pidfile_naming_another_process_stops_nothing_and_gc_takes_it(self):
        pidfile = self.vmdir + "/github-inject.pid"
        self.w.pids.add(556)
        self.w._set_file(pidfile, "556\n")
        self.w.answer(["ps", "-o", "command=", "-p", "556"], out="/usr/bin/vim\n")
        (row,) = guest.rubble(self.vm)
        self.assertIsNotNone(row.take)
        self.assertFalse(quiet(self.host().retire_spawned_inject)[0])
        self.assertNotIn(("kill", 556, 15), self.w.effects)
        self.assertNotIn(pidfile, self.w.files)

    def test_the_launch_agent_names_the_injectors_files_and_the_login_it_holds(self):
        import plistlib
        doc = plistlib.loads(guest.inject_plist(self.host(), "com.wk.inject", "/log", "/bin").encode())
        env = doc["EnvironmentVariables"]
        self.assertEqual(self.vmdir + "/github-inject.sock", env["WK_INJECT_SOCK"])
        self.assertEqual(self.vmdir + "/claude-inject.sock", env["WK_INJECT_PLAIN_SOCK"])
        self.assertEqual(guest.Host(self.vm).secrets.cred_path("claude-login"), env["WK_INJECT_CLAUDE_LOGIN"])
        self.assertEqual(self.vm.store.podman_machine(), env["WK_INJECT_PUBLISH_MACHINE"])
        self.assertTrue(doc["ProgramArguments"][-1].endswith("container/proxy/github-inject.py"))
        self.assertTrue(doc["KeepAlive"])

    def test_a_bridge_that_never_gets_its_address_starts_no_proxy(self):
        self.w.bridge = False
        self.w.env["WK_VM_PROXY_ADDR"] = ADDR
        self.vm = places.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")
        ok, err = quiet(self.host().start_proxy)
        self.assertFalse(ok)
        self.assertIn("never got address", err)
        self.assertEqual([], self.w.spawned("wk-proxy.py"))

    def test_a_live_proxy_is_left_alone(self):
        self.w.pids.add(777)
        self.w._set_file(self.vmdir + "/proxy.pid", "777\n")
        self.assertTrue(quiet(self.host().start_proxy)[0])
        self.assertEqual([], self.w.spawned("wk-proxy.py"))

    def ready(self):
        ws_dir = self.vm.store.ws_dir("demo")
        self.w.mkdir_now(ws_dir)
        self.w._set_file(os.path.join(ws_dir, places.READY_MARKER), "")
        quiet(lambda: self.vm.wait_ready("demo", self.clock))

    def test_waiting_for_a_running_guest_respawns_a_dead_proxy_only(self):
        for case, state, live, spawns in (("dead proxy", "running", False, 1), ("live proxy", "running", True, 0),
                                          ("stopped guest", "stopped", False, 0)):
            with self.subTest(case):
                self.setUp()
                self.w.state = state
                self.w._set_file(self.vmdir + "/proxy.pid", "777\n")
                if live:
                    self.w.pids.add(777)
                self.ready()
                self.assertEqual(spawns, len(self.w.spawned("wk-proxy.py")))

    def test_a_guests_wall_rows_name_each_daemons_remedy(self):
        from wk import wall
        w = wall.Wall(str(REPO), self.vm, "demo", self.w)
        with mock.patch.object(wall.Wall, "inside", lambda self, cmd: "000"):
            self.assertEqual("wk start demo", w.github()[0][2])
            for rows in (w.github_read(), w.bugzilla_read()):
                self.assertIn("./setup --stage inject", rows[0][2], rows)

    def test_a_proxy_older_than_its_source_is_stopped_and_started_again(self):
        self.w.pids.add(777)
        self.w._set_file(self.vmdir + "/proxy.pid", "777\n")
        self.w.answer(["find"], out=str(REPO / "container/proxy/wk-proxy.py") + "\n")
        _, err = quiet(self.host().start_proxy)
        self.assertIn(("kill", 777, 15), self.w.effects)
        self.assertEqual(1, len(self.w.spawned("wk-proxy.py")))

    def killed_before_the_pidfile(self, name):
        write = self.w.write

        def dies(path, text):
            if path.endswith(name) and not dies.done:
                dies.done = True
                raise Killed(("write", path))
            return write(path, text)
        dies.done = False
        with mock.patch.object(self.w, "write", dies):
            with self.assertRaises(Killed):
                quiet(self.host().start_proxy)
        return quiet(self.host().start_proxy)

    def test_a_daemon_whose_pidfile_a_kill_lost_is_replaced_not_left_running(self):
        for word, pidfile in (("wk-proxy.py", "proxy.pid"),):
            with self.subTest(daemon=word):
                self.setUp()
                ok, err = self.killed_before_the_pidfile(pidfile)
                self.assertTrue(ok, err)
                live = self.w.daemons(word)
                self.assertEqual(1, len(live), "one %s, not an orphan beside it" % word)
                self.assertEqual(live, [max(p for p, s in self.w.serving.items() if word in s)], "not the newest spawn")
                self.assertEqual("%d\n" % live[0], self.w.files[self.vmdir + "/" + pidfile])
                self.assertIn("no pidfile names it", err)

    def test_an_agent_whose_pidfile_a_kill_lost_is_adopted(self):
        h = self.host()
        write = self.w.write
        with mock.patch.object(self.w, "write", lambda p, t: (_ for _ in ()).throw(Killed(p)) if p.endswith("ssh-agent.pid")
                               else write(p, t)):
            with self.assertRaises(Killed):
                quiet(h.start_agent)
        self.assertTrue(quiet(self.host().start_agent)[0])
        (pid,) = self.w.daemons("ssh-agent")
        self.assertEqual("%d\n" % pid, self.w.files[self.vmdir + "/ssh-agent.pid"])
        self.assertEqual(1, len(self.w.spawned("ssh-agent")))

    def test_the_guests_agent_is_started_once_and_then_answers(self):
        h = self.host()
        self.assertTrue(quiet(h.start_agent)[0])
        self.assertTrue(quiet(h.start_agent)[0])
        self.assertEqual(1, len(self.w.spawned("ssh-agent")))


FAKE_LAUNCHCTL = """#!/bin/sh
held() { for f in "$WK_FAKE_LOCKS"/claude-login@*.lock; do [ -L "$f" ] && { echo held; return; }; done; echo free; }
case "$1" in
    bootout) echo "bootout $(held)" >> "$WK_FAKE_LOG"; echo 3 > "$WK_FAKE_LEFT" ;;
    print) n=$(cat "$WK_FAKE_LEFT" 2>/dev/null || echo 0); echo "print $n" >> "$WK_FAKE_LOG"
           [ "$n" -gt 0 ] || exit 1; echo $((n - 1)) > "$WK_FAKE_LEFT" ;;
    bootstrap) echo "bootstrap $(held)" >> "$WK_FAKE_LOG" ;;
esac
"""


class TestTheInjectorsRestart(unittest.TestCase):
    """launchd's restart of the Mac's injector, against a launchctl that keeps the label for three prints after a bootout."""

    def test_it_boots_out_under_the_logins_lock_and_bootstraps_once_launchd_lets_go(self):
        from types import SimpleNamespace
        from wk.clock import Clock
        from wk.machine import Local
        d = tempfile.mkdtemp(prefix="wk-test-restart-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        login, log = os.path.join(d, "claude-login", ".credentials.json"), os.path.join(d, "log")
        with open(os.path.join(d, "launchctl"), "w") as f:
            f.write(FAKE_LAUNCHCTL)
        os.chmod(os.path.join(d, "launchctl"), 0o755)
        host = SimpleNamespace(machine=Local(), clock=Clock(), secrets=SimpleNamespace(
            cred_path=lambda name: login, ensure_dir=lambda path, mode: os.makedirs(path, exist_ok=True)))
        env = {"PATH": d + os.pathsep + os.environ["PATH"], "WK_FAKE_LOG": log, "WK_FAKE_LEFT": os.path.join(d, "left"),
               "WK_FAKE_LOCKS": os.path.dirname(login)}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(guest.inject_restart(host, "com.wk.inject", "/p.plist"))
        with open(log) as f:
            self.assertEqual(["bootout held", "print 3", "print 2", "print 1", "print 0", "bootstrap free"], f.read().splitlines())


class TestTheForward(GuestTest):
    def setUp(self):
        super().setUp()
        self.h = guest.Host(self.vm, self.clock)
        self.g = self.vm.guest("demo")
        self.pidfile = self.h.forward_pidfile("demo", "agent")
        self.far, self.near = "/Users/admin/.wk-ssh-agent.sock", self.h.agent_sock()

    def start(self):
        return quiet(self.h.forward_start, "demo", self.g, "agent", self.far, self.near)[0]

    def test_a_second_start_while_one_is_alive_is_a_no_op(self):
        self.assertTrue(self.start())
        self.assertTrue(self.start())
        forwards = self.w.spawned(" -N ")
        self.assertEqual(1, len(forwards), forwards)
        self.assertIn("/Users/admin/.wk-ssh-agent.sock:%s/ssh-agent.sock" % self.vmdir, forwards[0])
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
        self.hold("vm-agent-forward-demo", 4242)
        for fn in (self.start, lambda: self.h.forward_stop("demo", "agent")):
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

    def test_a_converge_with_an_empty_agent_ends_the_forward(self):
        self.start()
        g = guest.Guest(self.h, "demo", self.g)
        self.assertTrue(quiet(g.agent_converge_guest)[0])
        self.assertNotIn(self.pidfile, self.w.files)
        self.assertIn(("rm -f /Users/admin/.wk-ssh-agent.sock", ""), self.w.guest_cmds)

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


class TestTheSwitchForTheGuests(GuestTest):
    def seed_ready(self):
        self.w.seed()
        self.w._set_file(self.vm.store.ws_dir("demo") + "/.wk-ready", "")

    def creds(self):
        return {p: self.w.files.get(p) for p in (self.vmdir + "/push-github-pat", self.vmdir + "/push-bugzilla-api-key")}

    def test_on_loads_the_agent_hands_the_injector_both_and_forwards_into_each_running_guest(self):
        self.seed_ready()
        ok, err = quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        self.assertTrue(ok, err)
        self.assertEqual({"KEY:" + k[0] for k in secrets.push_keys()}, self.w.agents[self.vmdir + "/ssh-agent.sock"])
        self.assertEqual(["ghp-held\n", "bz-held\n"], list(self.creds().values()))
        self.assertEqual(1, len(self.w.spawned(" -N ")))

    def test_on_without_a_bugzilla_key_leaves_no_file(self):
        self.w.seed(bz=None)
        self.w._set_file(self.vmdir + "/push-bugzilla-api-key", "stale\n")
        quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        self.assertIsNone(self.creds()[self.vmdir + "/push-bugzilla-api-key"])

    def test_off_empties_the_agent_clears_both_and_names_each_guest(self):
        self.seed_ready()
        quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        ok, err = quiet(guest.vm_push_keys_converge, str(REPO), self.w, "off", self.w.env)
        self.assertTrue(ok, err)
        self.assertEqual(set(), self.w.agents[self.vmdir + "/ssh-agent.sock"])
        self.assertEqual([None, None], list(self.creds().values()))
        self.assertIn("no agent socket -- a push in there is refused", err)

    def test_a_guest_that_did_not_answer_fails_the_switch_and_is_named(self):
        self.seed_ready()
        self.w.ssh_rc = 255
        ok, err = quiet(guest.vm_push_keys_converge, str(REPO), self.w, "off", self.w.env)
        self.assertFalse(ok)
        self.assertIn("demo", err)
        self.assertIn("FAILED", err)

    def test_status_reads_and_writes_nothing(self):
        self.seed_ready()
        quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        self.w.guest_sock = True
        before = self.w.state_of()
        n = len(secrets.push_keys())
        self.assertEqual((n, [("demo", "running", "%d key(s) through the agent on this host" % n)]),
                         guest.vm_push_status(str(REPO), self.w, self.w.env))
        self.assertEqual(before, self.w.state_of())

    def test_a_host_with_no_guests_still_loads_the_agent_its_own_pushes_use(self):
        self.w.seed()
        self.w.env["WK_VM_STORE"] = self.w.env["WK_STORE"]
        ok, err = quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        self.assertTrue(ok, err)
        self.assertEqual({"KEY:" + k[0] for k in secrets.push_keys()}, self.w.agents[self.vmdir + "/ssh-agent.sock"])
        self.assertEqual((len(secrets.push_keys()), []), guest.vm_push_status(str(REPO), self.w, self.w.env))
        self.assertEqual([], self.w.spawned(" -N "))


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
        """`live vm.mirror_tag_mount`: the base's LaunchDaemon mounted the mirror's own tag at boot, where mirror_dir is."""
        vm, ws = self.found
        r = vm.exec(ws, ["sh", "-c", "mount | grep -F %s && test -d %s" % (shlex.quote(" on %s (" % places.GUEST_MIRROR_MOUNT),
                                                                        shlex.quote(vm.mirror_dir()))], timeout=60)
        self.assertTrue(r.ok, r.out + r.err)

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


if __name__ == "__main__":
    unittest.main()
