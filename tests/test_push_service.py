"""A workspace's `git push` through the host: which workspace a caller is, what an agent in it changes, and the one command a push may run.
The service and its client run for real over a unix socket; ssh is a stub, /proc a directory, podman a fake."""
import asyncio
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, pushgate, repos, secrets  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402

SERVICE = REPO / "container" / "push" / "wk-push.py"
CLIENT = REPO / "container" / "push" / "wk-push-client.py"
ID = "ab" * 32
CGROUP = "0::/user.slice/user-501.slice/user@501.service/user.slice/libpod-%s.scope/container\n" % ID
PS = ["podman", "ps", "-a", "--no-trunc", "--filter", "name=^wk-", "--format", "{{.ID}}\t{{.Names}}"]
KEY, REPO_NAME, ALIAS = secrets.forks()[0]
OTHER_KEY, OTHER_REPO, OTHER_ALIAS = secrets.forks()[1]
UPLOAD = "git-upload-pack '%s.git'" % REPO_NAME
RECEIVE = "git-receive-pack '%s.git'" % REPO_NAME

# What ssh would be to GitHub: it logs its arguments and environment, speaks first as a server does, says something on stderr, echoes stdin, and exits 7.
FAKE_SSH = """#!/bin/sh
printf '%%s\\n' "$*" >> %s
printf 'GIT_PROTOCOL=%%s\\n' "${GIT_PROTOCOL:-}" >> %s
echo oops-on-stderr >&2
echo banner
cat
exit 7
"""


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Place:
    """A place whose workspaces hold the agent pids in `agents`; a workspace not in it cannot be asked."""

    def __init__(self, machine, kind="container", agents=None, state="running"):
        self.kind, self.machine, self.agents, self.state, self.execs = kind, machine, agents or {}, state, []
        self.guests = []

    def podman(self):
        return ["podman"]

    def exec(self, ws, argv, tty=False, timeout=None):
        self.execs.append(ws)
        pids = self.agents.get(ws)
        return Result(1, "", "no such workspace") if pids is None else Result(0, "".join(p + "\r\n" for p in pids))

    def info(self, ws):
        return self.state

    def repo(self, ws):
        return repos.default()

    def tools_src(self):
        return str(REPO)

    def list(self):
        return [(g, "running") for g, _ in self.guests]

    def ip(self, ws):
        return dict(self.guests).get(ws)


class Registry:
    def __init__(self, **places):
        self.places = places

    def load(self, kind):
        return self.places[kind]


class _Service(WkTest):
    """The service, serving on a socket in a thread of this process; the caller is this process, whose cgroup is `proc`'s."""

    def setUp(self):
        super().setUp()
        self.sockdir = tempfile.mkdtemp(prefix="wk-")
        self.addCleanup(shutil.rmtree, self.sockdir, True)
        self.module = load(SERVICE, "wkpush")
        store = self.tmp / "store"
        (store / "push-keys").mkdir(parents=True)
        for k, _, _ in secrets.forks():
            (store / "push-keys" / ("build_key_" + k)).write_text("KEY\n")
        self.env = {"WK_STORE": str(store), "WK_HOST_SECRETS": str(store / "secrets")}
        self.log = self.tmp / "ssh.log"
        (self.tmp / "bin").mkdir()
        ssh = self.tmp / "bin" / "ssh"
        ssh.write_text(FAKE_SSH % (self.log, self.log))
        ssh.chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": "%s:%s" % (self.tmp / "bin", os.environ["PATH"])})
        patch.start()
        self.addCleanup(patch.stop)
        self.proc = self.tmp / "proc"
        (self.proc / str(os.getpid())).mkdir(parents=True)
        (self.proc / str(os.getpid()) / "cgroup").write_text(CGROUP)
        self.machine = Fake("here")
        self.machine.answer(PS, out="%s\twk-demo\n" % ID)
        self.place = Place(self.machine, agents={"demo": []})
        self.places = {"container": self.place}

    def serve(self, guest_dir=None, gate=None):
        gate = gate or pushgate.Gate(Registry(**self.places), proc=str(self.proc))
        svc = self.module.Service(gate, secrets.Secrets(str(REPO), self.env, self.machine), guest_dir)
        loop, ready, started = asyncio.new_event_loop(), threading.Event(), {}

        def run():
            asyncio.set_event_loop(loop)
            started["server"] = loop.run_until_complete(asyncio.start_unix_server(svc.handle, path=self.path))
            ready.set()
            loop.run_forever()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        ready.wait(5)

        def stop():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()
        self.addCleanup(stop)
        return svc

    @property
    def path(self):
        return os.path.join(self.sockdir, "push.sock")

    def ask(self, request, path=None, stdin=b"payload"):
        """(the first reply, the frames {kind: bytes} when it was an ack)."""
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        s.connect(path or self.path)
        s.sendall((request if isinstance(request, bytes) else json.dumps(request).encode()) + b"\n")
        f = s.makefile("rb")
        reply = json.loads(f.readline())
        frames = {}
        if reply.get("ok"):
            s.sendall(stdin)
            s.shutdown(socket.SHUT_WR)
            while True:
                head = f.read(5)
                if len(head) < 5:
                    break
                kind, size = struct.unpack(">BI", head)
                frames[kind] = frames.get(kind, b"") + f.read(size)
                if kind == pushgate.EXIT:
                    break
        s.close()
        return reply, frames

    def push(self, command=UPLOAD, host="git@" + ALIAS, **kw):
        return self.ask(dict({"verb": "push", "host": host, "command": command}, **kw))

    def ssh_log(self):
        return self.log.read_text() if self.log.exists() else ""


class TestAPush(_Service):
    def test_with_no_agent_in_the_workspace_ssh_runs_with_its_deploy_key_and_its_output_comes_back(self):
        self.serve()
        reply, frames = self.push(protocol="version=2")
        self.assertEqual({"ok": True}, reply)
        self.assertEqual(b"banner\npayload", frames[pushgate.STDOUT])
        self.assertIn(b"oops-on-stderr", frames[pushgate.STDERR])
        self.assertEqual(7, struct.unpack(">i", frames[pushgate.EXIT])[0])
        log = self.ssh_log()
        self.assertIn("-i %s" % (self.tmp / "store" / "push-keys" / ("build_key_" + KEY)), log)
        self.assertIn("git@github.com %s" % UPLOAD, log)
        for word in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "SendEnv=GIT_PROTOCOL", "GIT_PROTOCOL=version=2"):
            self.assertIn(word, log)

    def test_the_service_builds_the_command_and_never_runs_the_callers(self):
        self.serve()
        self.push(command="git-receive-pack '/%s'" % (REPO_NAME + ".git"))
        self.assertIn("git@github.com %s" % RECEIVE, self.ssh_log())
        self.assertNotIn("'/", self.ssh_log())

    def test_a_protocol_is_passed_only_as_a_version(self):
        self.serve()
        self.push(protocol="version=2; id")
        self.assertNotIn("SendEnv", self.ssh_log())
        self.assertIn("GIT_PROTOCOL=\n", self.ssh_log())

    def test_an_agent_in_the_workspace_is_refused_naming_the_remedy_and_no_ssh_runs(self):
        self.place.agents["demo"] = ["4242"]
        self.serve()
        reply, frames = self.push()
        self.assertIn("an agent (claude or pi) runs in 'demo' (pid 4242)", reply["refused"])
        self.assertIn("wk enter demo", reply["remedy"])
        self.assertEqual({}, frames)
        self.assertEqual("", self.ssh_log())

    def test_an_agent_in_another_workspace_does_not_matter_and_only_this_one_is_asked(self):
        self.place.agents["other"] = ["9"]
        self.serve()
        reply, _ = self.push()
        self.assertEqual({"ok": True}, reply)
        self.assertEqual(["demo"], self.place.execs)

    def test_a_workspace_that_cannot_be_asked_is_refused(self):
        del self.place.agents["demo"]
        self.serve()
        reply, _ = self.push()
        self.assertIn("could not ask 'demo' whether an agent runs in it", reply["refused"])
        self.assertEqual("", self.ssh_log())

    def test_an_unknown_caller_is_refused(self):
        (self.proc / str(os.getpid()) / "cgroup").write_text("0::/user.slice/session-3.scope\n")
        self.serve()
        reply, _ = self.push()
        self.assertEqual("this caller is in no workspace", reply["refused"])
        (self.proc / str(os.getpid()) / "cgroup").write_text(CGROUP.replace(ID, "cd" * 32))
        reply, _ = self.push()
        self.assertEqual("this caller is in no workspace", reply["refused"])
        (self.proc / str(os.getpid()) / "cgroup").unlink()
        self.assertEqual("this caller is in no workspace", self.push()[0]["refused"])
        self.assertEqual("", self.ssh_log())

    def test_a_failure_of_the_service_is_a_refusal_naming_its_log(self):
        self.serve()
        with mock.patch.object(pushgate.Gate, "container_of", side_effect=RuntimeError("podman fell over")):
            reply, _ = self.push()
        self.assertEqual("push service error: podman fell over", reply["refused"])
        self.assertIn("journalctl --user -u wk-push", reply["remedy"])

    def test_a_host_with_no_peer_credentials_knows_no_caller(self):
        self.serve()
        with mock.patch.object(self.module, "socket", types.SimpleNamespace()):
            reply, _ = self.push()
        self.assertEqual("this caller is in no workspace", reply["refused"])

    def test_a_command_outside_the_workspaces_repositories_is_refused(self):
        self.serve()
        for host, command in (("git@" + ALIAS, "git-receive-pack '/someone/else.git'"),
                              ("git@" + ALIAS, "git-receive-pack '%s.git'; id" % REPO_NAME),
                              ("git@" + ALIAS, "git-lfs-authenticate '%s.git' upload" % REPO_NAME),
                              ("git@" + ALIAS, "sh -c id"),
                              ("git@" + ALIAS, ""),
                              ("git@no-such-alias", UPLOAD),
                              ("git@" + OTHER_ALIAS, UPLOAD),
                              ("", UPLOAD)):
            with self.subTest(host=host, command=command):
                reply, frames = self.push(command=command, host=host)
                self.assertIn("is not a push or fetch of this workspace's repositories", reply["refused"])
                self.assertIn(ALIAS, reply["remedy"])
                self.assertEqual({}, frames)
        self.assertEqual("", self.ssh_log())

    def test_each_row_of_the_repo_is_pushed_with_its_own_key(self):
        self.serve()
        self.push(command="git-receive-pack '%s.git'" % OTHER_REPO, host="git@" + OTHER_ALIAS)
        self.assertIn("build_key_" + OTHER_KEY, self.ssh_log())

    def test_a_machine_with_no_deploy_key_says_so(self):
        os.remove(self.tmp / "store" / "push-keys" / ("build_key_" + KEY))
        self.serve()
        reply, _ = self.push()
        self.assertIn("holds no deploy key '%s'" % KEY, reply["refused"])
        self.assertIn("wk key deploy", reply["remedy"])

    def test_what_is_not_a_request_is_refused(self):
        self.serve()
        for line in (b"garbage", b"[]", b'{"verb": "reboot"}', b'{"verb": "listen", "workspace": "x"}', b'{"verb": "push"' + b" " * 5000 + b"}"):
            with self.subTest(line=line[:30]):
                self.assertIn("refused", self.ask(line)[0])


class TestWhoMayWrite(_Service):
    """The egress proxy asks, naming who connected to it."""

    def test_a_workspace_with_no_agent_may(self):
        self.serve()
        self.assertEqual({"writes": True, "why": ""}, self.ask({"verb": "writes", "pid": os.getpid()})[0])

    def test_one_with_an_agent_may_not_and_the_reason_is_named(self):
        self.place.agents["demo"] = ["7"]
        self.serve()
        reply = self.ask({"verb": "writes", "pid": os.getpid()})[0]
        self.assertFalse(reply["writes"])
        self.assertIn("an agent (claude or pi) runs in 'demo'", reply["why"])

    def test_nobody_may_who_is_in_no_workspace_and_a_service_that_cannot_ask_says_no(self):
        self.serve()
        self.assertFalse(self.ask({"verb": "writes", "pid": 1})[0]["writes"])
        del self.place.agents["demo"]
        self.assertFalse(self.ask({"verb": "writes", "pid": os.getpid()})[0]["writes"])

    def test_a_guest_is_named_by_its_address(self):
        vm = Place(self.machine, kind="vm", agents={"g1": [], "g2": ["5"]})
        vm.guests = [("g1", "192.0.2.2"), ("g2", "192.0.2.3")]
        self.places["vm"] = vm
        self.serve()
        self.assertTrue(self.ask({"verb": "writes", "addr": "192.0.2.2"})[0]["writes"])
        self.assertFalse(self.ask({"verb": "writes", "addr": "192.0.2.3"})[0]["writes"])
        self.assertFalse(self.ask({"verb": "writes", "addr": "192.0.2.99"})[0]["writes"])

    def test_a_question_that_names_nobody_is_refused(self):
        self.serve()
        self.assertIn("names a pid or an addr", self.ask({"verb": "writes"})[0]["refused"])


class TestAGuestsOwnSocket(_Service):
    """A macOS guest's forward ends at a socket the service made for it, and that socket names the guest."""

    def setUp(self):
        super().setUp()
        self.vm = Place(self.machine, kind="vm", agents={"g1": [], "g2": ["5"]})
        self.places["vm"] = self.vm
        self.guests = tempfile.mkdtemp(prefix="wk-")
        self.addCleanup(shutil.rmtree, self.guests, True)

    def test_a_guest_pushes_through_its_socket_whoever_the_caller_is(self):
        (self.proc / str(os.getpid()) / "cgroup").unlink()
        self.serve(guest_dir=self.guests)
        reply = self.ask({"verb": "listen", "workspace": "g1"})[0]
        socket_path = os.path.join(self.guests, "g1.push.sock")
        self.assertEqual({"listening": socket_path}, reply)
        self.assertEqual(0o600, os.stat(socket_path).st_mode & 0o777)
        self.assertEqual({"ok": True}, self.ask({"verb": "push", "host": "git@" + ALIAS, "command": UPLOAD}, path=socket_path)[0])
        self.assertEqual(["g1"], self.vm.execs)

    def test_an_agent_in_that_guest_refuses_it_and_one_in_another_does_not(self):
        self.serve(guest_dir=self.guests)
        paths = {}
        for g in ("g1", "g2"):
            paths[g] = self.ask({"verb": "listen", "workspace": g})[0]["listening"]
        request = {"verb": "push", "host": "git@" + ALIAS, "command": UPLOAD}
        self.assertTrue(self.ask(request, path=paths["g1"])[0]["ok"])
        self.assertIn("an agent (claude or pi) runs in 'g2'", self.ask(request, path=paths["g2"])[0]["refused"])

    def test_a_listener_is_made_once_and_only_for_a_name(self):
        svc = self.serve(guest_dir=self.guests)
        self.ask({"verb": "listen", "workspace": "g1"})
        first = svc.listeners["g1"]
        self.ask({"verb": "listen", "workspace": "g1"})
        self.assertIs(first, svc.listeners["g1"])
        for name in ("../x", "-r", "", None, "a" * 200):
            with self.subTest(name=name):
                self.assertIn("is not a workspace name", self.ask({"verb": "listen", "workspace": name})[0]["refused"])

    def test_a_guests_own_socket_takes_neither_a_listen_nor_a_writes_question(self):
        self.serve(guest_dir=self.guests)
        path = self.ask({"verb": "listen", "workspace": "g1"})[0]["listening"]
        for request in ({"verb": "listen", "workspace": "g2"}, {"verb": "writes", "pid": 1}):
            with self.subTest(request=request):
                self.assertIn("unknown verb", self.ask(request, path=path)[0]["refused"])

    def test_a_container_s_service_makes_no_listener(self):
        self.serve()
        self.assertIn("unknown verb", self.ask({"verb": "listen", "workspace": "g1"})[0]["refused"])


class TestTheClient(_Service):
    def run_client(self, *argv, stdin=b"hello", path=None, env=None):
        return subprocess.run([sys.executable, str(CLIENT), *argv], input=stdin, capture_output=True,
                              env=dict(os.environ, WK_PUSH_SOCKET=path or self.path, **(env or {})))

    def serve_for_demo(self):
        class Gate(pushgate.Gate):
            def container_of(self, pid):
                return "demo"
        self.serve(gate=Gate(Registry(**self.places)))

    def test_what_git_would_have_sent_ssh_is_relayed_and_the_sessions_status_is_the_exit_status(self):
        self.serve_for_demo()
        cp = self.run_client("-o", "SendEnv=GIT_PROTOCOL", "git@" + ALIAS, UPLOAD, env={"GIT_PROTOCOL": "version=2"})
        self.assertEqual(7, cp.returncode, cp.stderr)
        self.assertEqual(b"banner\nhello", cp.stdout)
        self.assertIn(b"oops-on-stderr", cp.stderr)
        self.assertIn("GIT_PROTOCOL=version=2", self.ssh_log())

    def test_a_refusal_is_printed_with_its_remedy_and_exits_1(self):
        self.place.agents["demo"] = ["4242"]
        self.serve_for_demo()
        cp = self.run_client("git@" + ALIAS, UPLOAD)
        self.assertEqual(1, cp.returncode)
        self.assertIn(b"error: an agent (claude or pi) runs in 'demo'", cp.stderr)
        self.assertIn(b"wk enter demo", cp.stderr)
        self.assertEqual(b"", cp.stdout)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_git_itself_runs_it_where_it_would_run_ssh(self):
        self.serve_for_demo()
        env = dict(os.environ, WK_PUSH_SOCKET=self.path, HOME=str(self.tmp), GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "-c", "core.sshCommand=%s" % CLIENT, "-c", "ssh.variant=ssh", "ls-remote", "git@%s:%s.git" % (ALIAS, REPO_NAME)],
                       env=env, capture_output=True, timeout=60)
        self.assertIn("git@github.com %s" % UPLOAD, self.ssh_log())

    def test_no_service_is_255_naming_the_socket(self):
        cp = self.run_client("git@" + ALIAS, UPLOAD, path=os.path.join(self.sockdir, "none.sock"))
        self.assertEqual(255, cp.returncode)
        self.assertIn(b"no push service at", cp.stderr)

    def test_the_command_line_is_options_then_the_host_then_the_command(self):
        words = load(CLIENT, "wkpushclient").words
        for argv, want in ((["-o", "SendEnv=GIT_PROTOCOL", "git@h", "git-upload-pack 'a/b.git'"], ("git@h", "git-upload-pack 'a/b.git'")),
                           (["-p", "22", "-4", "git@h", "cmd", "arg"], ("git@h", "cmd arg")),
                           (["git@h"], ("git@h", "")), ([], ("", ""))):
            with self.subTest(argv=argv):
                self.assertEqual(want, words(argv))


class TestEveryWorkspaceIsWiredToTheClient(WkTest):
    def test_a_container_and_a_guest_name_the_one_client_and_the_wall_expects_that_path(self):
        client = "container/push/wk-push-client.py"
        self.assertTrue(os.access(REPO / client, os.X_OK))
        self.assertIn('git config --global core.sshCommand "$WK_TOOLS/%s"' % client, (REPO / "container" / "firstrun.sh").read_text())
        self.assertIn('git config --global core.sshCommand "$WK_TOOLS/%s"' % client, guest.CHECKOUT)
        for text in ((REPO / "container" / "firstrun.sh").read_text(), guest.CHECKOUT):
            self.assertIn("git config --global ssh.variant ssh", text, "git would probe the client with -G")
        self.assertEqual(Place(Fake()).tools_src(), str(REPO))

    def test_the_switch_is_gone_and_the_dispatcher_refuses_its_name(self):
        for argv in (("key", "push", "on"), ("key", "push", "status")):
            with self.subTest(argv=argv):
                cp = self.run_wk(*argv)
                self.assertEqual(2, cp.returncode, cp.stdout)
                self.assertIn("unknown verb: push", cp.stdout)

    def test_the_sockets_a_workspace_and_the_service_use(self):
        env = {"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/501"}
        self.assertEqual("/run/user/501/wk/push.sock", Store(env).push_socket())
        self.assertEqual("/home/u/.local/state/wk/push.sock", Store({"HOME": "/home/u"}).push_socket())
        self.assertEqual("/x.sock", Store(dict(env, WK_PUSH_SOCKET="/x.sock")).workspace_push_socket())
        self.assertEqual("/run/wk/push.sock", Store(env).workspace_push_socket())
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True):
            self.assertEqual("/home/u/.wk-push.sock", Store(env).workspace_push_socket())


class TestTheGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-gate-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.machine = Fake("here")
        self.machine.answer(PS, out="%s\twk-demo\n%s\tnot-a-workspace\n" % (ID, "cd" * 32))
        self.place = Place(self.machine)
        self.gate = pushgate.Gate(Registry(container=self.place), proc=str(self.tmp))

    def cgroup(self, pid, text):
        (self.tmp / str(pid)).mkdir(exist_ok=True)
        (self.tmp / str(pid) / "cgroup").write_text(text)

    def test_a_pid_is_the_workspace_whose_container_holds_it(self):
        self.cgroup(5, CGROUP)
        self.assertEqual("demo", self.gate.container_of(5))

    def test_a_pid_in_no_workspace_container_is_none(self):
        self.cgroup(6, "0::/user.slice/session-3.scope\n")
        self.cgroup(7, CGROUP.replace(ID, "ef" * 32))
        self.cgroup(8, CGROUP.replace(ID, "cd" * 32))
        for pid in (6, 7, 8, 99):
            with self.subTest(pid=pid):
                self.assertIsNone(self.gate.container_of(pid))

    def test_the_scan_names_every_agent_and_a_stopped_workspace_runs_none(self):
        self.place.agents["demo"] = ["3", "4"]
        self.assertEqual(["3", "4"], pushgate.agent_pids(self.place, "demo"))
        self.assertIsNone(pushgate.agent_pids(self.place, "gone"))
        self.place.state = "exited"
        self.assertEqual([], pushgate.agent_pids(self.place, "gone"))

    def test_the_command_a_push_may_run(self):
        rows = [["k", "a/b", "alias"], ["k2", "c/d", "alias2"]]
        self.assertEqual(("k", "git-receive-pack 'a/b.git'"), pushgate.command(rows, "git@alias", "git-receive-pack 'a/b.git'"))
        self.assertEqual(("k", "git-upload-pack 'a/b.git'"), pushgate.command(rows, "root@alias", "git-upload-pack '/a/b'"))
        self.assertEqual(("k2", "git-upload-pack 'c/d.git'"), pushgate.command(rows, "alias2", "git-upload-pack 'c/d.git'"))
        for host, line in (("alias", "git-upload-pack 'c/d.git'"), ("alias2", "git-upload-pack 'a/b.git'"), ("alias", "git-upload-pack 'a/b.git' x"),
                           ("alias", "git-upload-pack 'a/b.git'; id"), ("alias", "git-upload-pack `id`"), ("other", "git-upload-pack 'a/b.git'")):
            with self.subTest(host=host, line=line), self.assertRaises(pushgate.Denied):
                pushgate.command(rows, host, line)

    def test_a_guest_is_the_running_one_at_that_address(self):
        vm = Place(self.machine, kind="vm")
        vm.guests = [("g1", "192.0.2.2"), ("g2", None)]
        gate = pushgate.Gate(Registry(vm=vm))
        self.assertEqual("g1", gate.guest_at("192.0.2.2"))
        self.assertIsNone(gate.guest_at("192.0.2.5"))


class TestTheScanOfAWorkspaceRunsForReal(WkTest):
    """The shell the service runs inside a workspace, over this machine's /proc: an agent is a process whose exe is claude or whose node script is pi."""

    def scan(self, script=pushgate.AGENT_PID_SCAN, env=None):
        cp = subprocess.run(["sh", "-c", script], env=env or os.environ, capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stderr)
        return cp.stdout.split()

    def spawn(self, argv, executable=None):
        p = subprocess.Popen(argv, executable=executable, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return str(p.pid)

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_a_claude_exe_and_a_node_pi_are_agents_and_nothing_else_is(self):
        claude = self.tmp / "claude"
        shutil.copy(shutil.which("sh"), claude)
        node = "import time; time.sleep(30)"
        agents = [self.spawn([str(claude), "-c", "sleep 30"]),
                  self.spawn(["node", "-c", node, "/home/u/.local/bin/pi"], executable=sys.executable),
                  self.spawn(["node", "-c", node, "/home/u/.local/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"],
                             executable=sys.executable)]
        others = [self.spawn(["sleep", "30"]),
                  self.spawn(["node", "-c", node, "/home/u/.local/bin/pixie"], executable=sys.executable),
                  self.spawn(["node", "-c", node, "/home/u/project/pi.js"], executable=sys.executable)]
        found = self.scan()
        for pid in agents:
            self.assertIn(pid, found)
        for pid in others:
            self.assertNotIn(pid, found)

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_the_scan_is_not_its_own_agent(self):
        found = self.scan()
        me = subprocess.run(["sh", "-c", 'echo $$; ' + pushgate.AGENT_PID_SCAN], capture_output=True, text=True).stdout.split()
        self.assertNotIn(me[0], me[1:])
        self.assertNotIn(me[0], found)

    def test_where_there_is_no_proc_ps_names_the_executable_and_the_node_script(self):
        """A macOS guest: only Claude Code's executable, from any install, and a `node .../bin/pi`, is a session."""
        ps = ("#!/bin/sh\ncase \"$*\" in\n"
              "*comm=*) printf '%s\\n' '11 /opt/homebrew/Caskroom/claude-code/2.1.236/claude' '12 /Users/admin/.local/bin/claude' "
              "'13 /Users/admin/.local/share/claude/versions/2.1.270' '14 /Applications/Claude.app/Contents/MacOS/Claude' "
              "'15 node' '16 claude' '17 /bin/zsh' '18 /usr/bin/claudette' ;;\n"
              "*command=*) printf '%s\\n' '21 node /Users/admin/.local/bin/pi --continue' '22 node /Users/admin/.local/bin/pixie' "
              "'23 /bin/zsh -c pi' '24 node /Users/admin/.local/bin/pi' ;;\nesac\n")
        (self.tmp / "ps").write_text(ps)
        (self.tmp / "ps").chmod(0o755)
        script = pushgate.AGENT_PID_SCAN.replace("if [ -d /proc/self ]", "if false")
        self.assertEqual(["11", "12", "13", "16", "21", "24"], self.scan(script, env={"PATH": "%s:/usr/bin:/bin" % self.tmp}))


class TestTheSshSessionIsTheKeysAlone(unittest.TestCase):
    def test_no_agent_and_one_identity(self):
        argv = pushgate.ssh_argv("/k/key", "git-upload-pack 'a/b.git'", "version=2")
        self.assertEqual(["git@github.com", "git-upload-pack 'a/b.git'"], argv[-2:])
        for word in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "/k/key", "SendEnv=GIT_PROTOCOL"):
            self.assertIn(word, argv)
        self.assertEqual(argv[:-2], pushgate.ssh_prefix("/k/key", "version=2"))
        for bad in ("", "version=22", "version=2 -o ProxyCommand=id", None):
            self.assertNotIn("SendEnv=GIT_PROTOCOL", pushgate.ssh_argv("/k/key", "x", bad))


if __name__ == "__main__":
    unittest.main()
