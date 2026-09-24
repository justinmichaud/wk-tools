"""lib/wk/guest.py against a fake host: a guest's start and stop, what a start converges in it, the three host
daemons, the agent forwards and the guests' half of `wk push`.

Nothing here starts a guest: tart, softnet and ssh are answers on a Fake (tests/test_wk_secrets.py's World, which
is also an ssh-agent and a filesystem), the bash steps a start still streams in through targets/vm.sh are recorded
rather than run, and time is a FakeClock.

Run: python3 tests/run.py --unit -k test_guest
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, live_selected
from tests.test_wk_secrets import SECRETFILE, SecretsTest, World, quiet

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, shell, targets, tools  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Killed, Result  # noqa: E402
from wk.store import Store  # noqa: E402

TART = "/fake/tart"
IP = "192.168.2.5"
ADDR = "192.168.2.1"
BASH_STEPS = [s[0] for s in guest.STEPS if s[0] in ("write_shell_rc", "write_lldbinit", "write_checkout", "install_claude_cli",
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
        self.answer([TART, "stop"])
        self.react(["ssh"], self._guest)
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
        """Whichever live daemon serves the port or socket asked about, as the process table has it."""
        where = argv[-2] if argv[-1] == "-sTCP:LISTEN" else argv[-1]
        key = where.split("@", 1)[1] if where.startswith("-iTCP@") else where
        pids = [p for p, serves in self.serving.items() if key in serves and p in self.pids]
        return Result(0 if pids else 1, "".join("%d\n" % p for p in pids))

    def daemons(self, word):
        """The live pids of one kind of daemon."""
        return sorted(p for p, serves in self.serving.items() if word in serves and p in self.pids)

    def spawn(self, argv, log):
        """A daemon that comes up says so where its start looks: the proxy in its log, the injector at its socket."""
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
        return pid

    def spawned(self, word):
        return [e[1] for e in self.effects if e[0] == "spawn" and word in " ".join(e[1])]


class GuestTest(SecretsTest):
    def setUp(self):
        super().setUp()
        self.w = GuestWorld(self.tmp)
        self.steps, self.step_ok, self.admit_rc, self.notes = [], {}, 0, []

        def step(root, fn, name, ip, env=None):
            self.steps.append(fn)
            return self.step_ok.get(fn, True)
        for obj, name, fn in ((Store, "macos_host", mock.PropertyMock(return_value=True)),
                              (targets.Vm, "tart", lambda s: TART),
                              (shell, "guest_step", step),
                              (shell, "guest_admit", lambda root, name, env=None: self.admit_rc),
                              (shell, "vm_login_note", lambda root, env=None: self.notes.append(1)),
                              (tools, "push", lambda *a: True)):
            p = mock.patch.object(obj, name, new_callable=lambda fn=fn: fn) if isinstance(fn, mock.PropertyMock) \
                else mock.patch.object(obj, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.clock = FakeClock()
        self.vm = targets.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")

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
        self.w.symlink("pid=%d tok=beef at=2026-09-24T00:00:00Z cmd=wk vm start" % pid, path)
        return path


class TestOneLockPerGuest(GuestTest):
    def test_one_lock_per_resource_guest_start(self):
        """`unit record.one_lock_per_resource[guest start]`: a second start waits on the first and names it, and
        one that outlives the wait is refused naming it again -- never two converges at once."""
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
        self.assertEqual([], self.w.spawned("run"), "a running guest was booted again")
        self.assertEqual(["_" + s for s in BASH_STEPS], self.steps)
        self.assertEqual([1], self.notes, "the login is stated once, on the one exit")

    def test_a_stopped_guest_is_admitted_then_booted_filtered_with_both_shares(self):
        self.w.state = "stopped"
        ip, err = self.run_start()
        self.assertEqual(IP, ip, err)
        run = self.w.spawned(" run ")[0]
        self.assertIn("--net-softnet-block=0.0.0.0/0", run)
        self.assertIn("--net-softnet-allow=%s/32" % ADDR, run)
        self.assertIn("--dir=agent-rw:%s" % (self.tmp + "/store/agent-rw"), run)
        self.assertIn("--dir=mirror:%s:ro" % os.path.dirname(self.vm.store.mirror()), run)
        self.assertEqual("wk-demo", run[-1])
        self.assertTrue(run[1].startswith("PATH=/usr/local/bin:"), "tart finds softnet through PATH")
        self.assertNotIn(self.vmdir + "/demo.unfiltered", self.w.files)

    def test_a_refused_admission_boots_nothing(self):
        self.w.state, self.admit_rc = "stopped", 1
        ip, _ = self.run_start()
        self.assertIsNone(ip)
        self.assertEqual([], self.w.spawned("run"))

    def test_an_unfiltered_guest_is_marked_and_gets_no_softnet(self):
        self.w.state = "stopped"
        self.w.env["WK_VM_UNFILTERED"] = "1"
        self.vm = targets.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")
        ip, err = self.run_start()
        self.assertEqual(IP, ip, err)
        self.assertIn("open network", err)
        self.assertFalse([a for a in self.w.spawned(" run ")[0] if "softnet" in a])
        self.assertIn(self.vmdir + "/demo.unfiltered", self.w.files)
        self.assertEqual([], self.w.spawned("wk-proxy.py"), "an unfiltered guest needs no proxy")

    def test_no_softnet_refuses_before_tart_runs(self):
        """Softnet applies at `tart run` and cannot be added later: a guest booted without it is open for life."""
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

    def test_a_guest_whose_ssh_never_answers_is_refused(self):
        self.w.state, self.w.ssh_rc = "stopped", 255
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("ssh never answered", err)

    def test_a_failed_step_is_named_and_the_start_goes_on(self):
        self.step_ok["_write_shell_rc"] = False
        ip, err = self.run_start()
        self.assertEqual(IP, ip)
        self.assertIn("could not wire demo's shell", err)

    def test_a_refused_desktop_refuses_the_start(self):
        self.step_ok["_report_desktop"] = False
        ip, _ = self.run_start()
        self.assertIsNone(ip)

    def test_an_absent_guest_is_refused(self):
        self.w.react([TART, "list"], lambda a, f: Result(0, "[]"))
        ip, err = self.run_start()
        self.assertIsNone(ip)
        self.assertIn("no such workspace: demo", err)

    def test_a_dry_run_changes_nothing(self):
        self.w.state = "stopped"
        before = self.w.state_of()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            ip, err = self.run_start()
        self.assertEqual(IP, ip, err)
        self.assertEqual(before, self.w.state_of())
        self.assertIn("would run on admin@%s: env WK_ADDR=%s" % (IP, ADDR), err)

    def test_killpoints_guest_start(self):
        def world():
            w = GuestWorld(self.tmp)
            w.state = "stopped"
            return type("W", (), {"fake": w, "vm": targets.Registry(str(REPO), env=w.env, machine=w).load("vm")})

        def run_once(w):
            quiet(guest.start, w.vm, "demo", FakeClock())
        def final(w):
            """A pidfile names whichever pid the last start spawned: what must hold is that it names a live one,
            and that exactly one of each daemon is up."""
            files = {p: ("live" if int(t) in w.fake.pids else "dead") if p.endswith(".pid") else t
                     for p, t in w.fake.files.items()}
            return files, [len(w.fake.daemons(d)) for d in ("wk-proxy.py", "github-inject.py")]
        converges(self, world, run_once, final, max_effects=80)


class TestTheSteps(GuestTest):
    def test_the_marker_is_written_and_a_bench_image_keeps_none(self):
        g = guest.Guest(guest.Host(self.vm, self.clock), "demo", IP)
        quiet(g.write_marker)
        self.assertIn("name=demo", "".join(i for c, i in self.w.guest_cmds if ".wk-workspace" in c))
        self.w.bench, self.w.guest_cmds = True, []
        quiet(g.write_marker)
        self.assertTrue([c for c, _ in self.w.guest_cmds if c.startswith("rm -rf") and ".wk-workspace" in c])

    def test_the_clock_step_hands_the_guest_this_hosts_time(self):
        g = guest.Guest(guest.Host(self.vm, self.clock), "demo", IP)
        self.assertTrue(quiet(g.set_guest_clock)[0])
        cmd, script = next((c, i) for c, i in self.w.guest_cmds if "WK_NOW_EPOCH" in c)
        self.assertIn("WK_NOW_EPOCH=%d" % self.clock.now(), cmd)
        self.assertIn("WK_SKEW=30", cmd)
        self.assertEqual(guest.CLOCK, script)

    def test_a_credential_file_its_reader_refuses_stops_the_delivery(self):
        """Refused is not absent: it must not land in the guest as an empty file, nor be taken away as withdrawn."""
        self.w.answer(["python3", SECRETFILE, "present"], rc=2, err="wk: refusing to read ...\n")
        g = guest.Guest(guest.Host(self.vm, self.clock), "demo", IP)
        self.assertFalse(quiet(g.write_agent_secrets)[0])
        self.assertFalse([c for c, _ in self.w.guest_cmds if ".wk-litellm-key" in c])

    def test_the_deploy_config_names_the_proxy_and_the_forwarded_agent(self):
        self.w.seed()
        g = guest.Guest(guest.Host(self.vm, self.clock), "demo", IP)
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
        return guest.Host(targets.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm"), self.clock)

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
        self.assertEqual([], quiet(self.host(WK_VM_UNFILTERED="1").softnet_flags)[0])


class TestTheEntryPoints(GuestTest):
    """cmd/vm's and targets/vm.sh's names for a start, a stop and the base build's clock."""

    def main(self, *argv):
        with mock.patch.object(guest, "_vm", lambda root, machine, env: self.vm):
            return quiet(guest.main, list(argv), self.w.env)

    def test_start_prints_the_address_and_stop_its_verdict(self):
        with mock.patch.object(guest, "start", lambda vm, ws: IP), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, self.main("start", "demo")[0])
        self.assertEqual(IP + "\n", out.getvalue())
        with mock.patch.object(guest, "stop", lambda vm, ws: False):
            self.assertEqual(1, self.main("stop", "demo")[0])

    def test_the_clock_is_set_on_the_address_it_is_given(self):
        self.assertEqual(0, self.main("clock", "wk-base", "10.0.0.9")[0])
        self.assertTrue([c for c, _ in self.w.guest_cmds if "WK_NOW_EPOCH" in c])

    def test_anything_else_is_the_usage(self):
        rc, err = self.main("start")
        self.assertEqual(2, rc)
        self.assertIn("usage: python3 -m wk.guest", err)


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
        self.assertIn("egress proxy on %s:3128" % ADDR, err)
        self.assertEqual(1, len(self.w.spawned("github-inject.py")), "the injector goes up ahead of the proxy")

    def test_a_bridge_that_never_gets_its_address_starts_no_proxy(self):
        self.w.bridge = False
        self.w.env["WK_VM_PROXY_ADDR"] = ADDR
        self.vm = targets.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")
        ok, err = quiet(self.host().start_proxy)
        self.assertFalse(ok)
        self.assertIn("never got address", err)
        self.assertEqual([], self.w.spawned("wk-proxy.py"))

    def test_a_live_proxy_is_left_alone(self):
        self.w.pids.add(777)
        self.w._set_file(self.vmdir + "/proxy.pid", "777\n")
        self.assertTrue(quiet(self.host().start_proxy)[0])
        self.assertEqual([], self.w.spawned("wk-proxy.py"))

    def test_a_proxy_older_than_its_source_is_stopped_and_started_again(self):
        self.w.pids.add(777)
        self.w._set_file(self.vmdir + "/proxy.pid", "777\n")
        self.w.answer(["find"], out=str(REPO / "container/proxy/wk-proxy.py") + "\n")
        _, err = quiet(self.host().start_proxy)
        self.assertIn(("kill", 777, 15), self.w.effects)
        self.assertIn("restarting the egress proxy", err)
        self.assertEqual(1, len(self.w.spawned("wk-proxy.py")))

    def killed_before_the_pidfile(self, name):
        """A start of every daemon killed once, between `name`'s spawn and its pidfile, then run again."""
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
        for word, pidfile in (("wk-proxy.py", "proxy.pid"), ("github-inject.py", "github-inject.pid")):
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
        """Replacing it would drop the keys `wk push on` loaded."""
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


class TestTheForward(GuestTest):
    """`wk push on`, `wk vm start` and a second `wk push on` all start one guest's forward; the tunnel is its task
    record, open while it carries that guest's push, and `wk push off` is what ends it whoever started it."""

    def setUp(self):
        super().setUp()
        self.h = guest.Host(self.vm, self.clock)
        self.g = self.vm.guest_at(IP)

    def record(self):
        return self.h.records().find(guest.FORWARD, "demo")

    def test_a_second_start_while_one_is_alive_is_a_no_op(self):
        self.assertTrue(self.h.forward_start("demo", self.g))
        self.assertTrue(self.h.forward_start("demo", self.g))
        forwards = self.w.spawned(" -N ")
        self.assertEqual(1, len(forwards), forwards)
        self.assertIn("/Users/admin/.wk-ssh-agent.sock:%s/ssh-agent.sock" % self.vmdir, forwards[0])
        self.assertEqual("", self.record().field("exit"))
        self.assertEqual("wk push off", self.record().field("kill"))

    def test_a_failed_start_ends_its_own_record(self):
        spawn = self.w.spawn

        def dies(argv, log):
            pid = spawn(argv, log)
            self.w.pids.discard(pid)
            return pid
        with mock.patch.object(self.w, "spawn", dies):
            self.assertFalse(self.h.forward_start("demo", self.g))
        self.assertEqual("failed", self.record().field("exit"))

    def test_a_record_whose_pid_is_gone_is_started_again(self):
        t = self.h.records().begin(guest.FORWARD, "here", "demo", "wk push off", "/nolog", ["start forward", "verify"])
        t.pid(4194304)
        self.assertTrue(self.h.forward_start("demo", self.g))
        self.assertEqual(1, len(self.w.spawned(" -N ")))

    def test_both_ends_take_the_forwards_lock(self):
        """A stop while a start is mid-flight would kill a pid the start is about to overwrite."""
        self.w.pids.add(4242)
        self.hold("vm-agent-forward-demo", 4242)
        for fn in (lambda: self.h.forward_start("demo", self.g), lambda: self.h.forward_stop("demo")):
            with self.assertRaises(Refused):
                quiet(fn)
        self.assertEqual([], self.w.spawned(" -N "))

    def test_stopping_a_guest_ends_its_forward_first(self):
        self.h.forward_start("demo", self.g)
        pid = int(self.record().field("pid"))
        quiet(guest.stop, self.vm, "demo", self.clock)
        kill = self.w.effects.index(("kill", pid, 15))
        self.assertLess(kill, self.w.effects.index(("act", (TART, "stop", "wk-demo"))))
        self.assertEqual("stopped", self.record().field("exit"))

    def test_a_converge_with_an_empty_agent_ends_the_forward(self):
        self.h.forward_start("demo", self.g)
        g = guest.Guest(self.h, "demo", IP)
        self.assertTrue(quiet(g.agent_converge_guest)[0])
        self.assertEqual("stopped", self.record().field("exit"))
        self.assertIn(("rm -f /Users/admin/.wk-ssh-agent.sock", ""), self.w.guest_cmds)


class TestTheSwitchForTheGuests(GuestTest):
    def creds(self):
        return {p: self.w.files.get(p) for p in (self.vmdir + "/push-github-pat", self.vmdir + "/push-bugzilla-api-key")}

    def test_on_loads_the_agent_hands_the_injector_both_and_forwards_into_each_running_guest(self):
        self.w.seed()
        self.w._set_file(self.vm.store.ws_dir("demo") + "/.wk-ready", "")
        ok, err = quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        self.assertTrue(ok, err)
        self.assertEqual({"KEY:fork", "KEY:forkwpe"}, self.w.agents[self.vmdir + "/ssh-agent.sock"])
        self.assertEqual(["ghp-held\n", "bz-held\n"], list(self.creds().values()))
        self.assertIn("reaches the agent on this host", err)
        self.assertEqual(1, len(self.w.spawned(" -N ")))

    def test_on_without_a_bugzilla_key_leaves_no_file(self):
        self.w.seed(bz=None)
        self.w._set_file(self.vmdir + "/push-bugzilla-api-key", "stale\n")
        quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        self.assertIsNone(self.creds()[self.vmdir + "/push-bugzilla-api-key"])

    def test_off_empties_the_agent_clears_both_and_names_each_guest(self):
        self.w.seed()
        self.w._set_file(self.vm.store.ws_dir("demo") + "/.wk-ready", "")
        quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        ok, err = quiet(guest.vm_push_keys_converge, str(REPO), self.w, "off", self.w.env)
        self.assertTrue(ok, err)
        self.assertEqual(set(), self.w.agents[self.vmdir + "/ssh-agent.sock"])
        self.assertEqual([None, None], list(self.creds().values()))
        self.assertIn("no agent socket -- a push in there is refused", err)

    def test_a_guest_that_did_not_answer_fails_the_switch_and_is_named(self):
        self.w.seed()
        self.w._set_file(self.vm.store.ws_dir("demo") + "/.wk-ready", "")
        self.w.ssh_rc = 255
        ok, err = quiet(guest.vm_push_keys_converge, str(REPO), self.w, "off", self.w.env)
        self.assertFalse(ok)
        self.assertIn("demo", err)
        self.assertIn("FAILED", err)

    def test_status_reads_and_writes_nothing(self):
        self.w.seed()
        self.w._set_file(self.vm.store.ws_dir("demo") + "/.wk-ready", "")
        quiet(guest.vm_push_keys_converge, str(REPO), self.w, "on", self.w.env)
        self.w.guest_sock = True
        before = self.w.state_of()
        rows = guest.vm_push_keys_state(str(REPO), self.w, self.w.env)
        self.assertEqual([("demo", "running", "2 key(s) through the agent on this host")], rows)
        self.assertEqual(before, self.w.state_of())

    def test_a_host_whose_vm_target_has_no_store_of_its_own_has_no_guests_to_switch(self):
        self.w.env["WK_VM_STORE"] = self.w.env["WK_STORE"]
        self.assertTrue(guest.vm_push_keys_converge(str(REPO), self.w, "on", self.w.env))
        self.assertIsNone(guest.vm_push_agent_keys(str(REPO), self.w, self.w.env))
        self.assertEqual([], guest.vm_push_keys_state(str(REPO), self.w, self.w.env))


def _state_of(self):
    return dict(self.files), {s: sorted(k) for s, k in self.agents.items()}, sorted(self.pids)


GuestWorld.state_of = _state_of


def _a_running_guest():
    """(vm target, name) of a guest up on this host, or None; nothing is started to find one."""
    if not live_selected() or sys.platform != "darwin":
        return None
    try:
        vm = targets.Registry(str(REPO)).load("vm")
        up = [n for n, state in vm.list() if state == "running"]
    except (LookupError, Refused):
        return None
    return (vm, up[0]) if up else None


class TestTheLiveGuest(unittest.TestCase):
    """What only a real guest behind Softnet shows. Read-only: it asks a running guest and starts nothing."""
    wk_tier = "live"

    def setUp(self):
        self.found = _a_running_guest()
        if self.found is None:
            self.skipTest("live tier not selected, or no macOS guest is running on this host")

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

    def test_vm_shared_mirror(self):
        """`live vm.shared_mirror`: the checkout is --shared off the mirror share, and its alternates resolve."""
        vm, ws = self.found
        r = self.ask("cat %s/.git/objects/info/alternates && git -C %s cat-file -e HEAD"
                     % (vm.src(ws), vm.src(ws)))
        self.assertTrue(r.ok, r.out + r.err)
        self.assertIn(guest_mirror_objects(vm), r.out)


def guest_mirror_objects(vm):
    return vm.mirror_dir() + "/objects"


if __name__ == "__main__":
    unittest.main()
