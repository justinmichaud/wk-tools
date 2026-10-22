"""A workspace's `git push` through the host: which workspace a caller is, who is behind it, and the one command a push may run.
The service and its client run for real over a unix socket; ssh is a stub, /proc a directory, podman a fake."""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
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

from tests import test_pr_workflow, test_wk_places
from tests.support import REPO, WkTest, load_cmd, requires_container_place

sys.path.insert(0, str(REPO / "lib"))
from wk import decl, git, pr, pushgate, repos, secrets  # noqa: E402
from wk.machine import Result  # noqa: E402
from tests.fakes import Fake, FlowTest, World  # noqa: E402
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
PERSON = "/opt/wk-tools/" + pushgate.PERSON
TOOLS_ORIGIN = ["git", "-c", "safe.directory=*", "-C", str(REPO), "remote", "get-url", "origin"]

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


class Proc:
    """A /proc directory: `chain` writes one process per row, (pid, ppid, comm, argv), the first being the caller."""

    def __init__(self, root):
        self.root = Path(root)

    def add(self, pid, ppid, comm, argv, cgroup=CGROUP):
        d = self.root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text("Name:\t%s\nPid:\t%d\nPPid:\t%d\n" % (comm, pid, ppid))
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        if cgroup is not None:
            (d / "cgroup").write_text(cgroup)

    def chain(self, rows):
        for pid, ppid, comm, argv in rows:
            self.add(pid, ppid, comm, argv)


def person_chain(pid):
    """git in a shell a person entered: git <- bash <- the person entry (a child of conmon) <- conmon <- 1."""
    return [(pid, 500, "bash", ["bash", "-lc", "git push"]), (500, 400, "sh", ["/bin/sh", PERSON, "/bin/bash", "--login"]),
            (400, 1, "conmon", ["/usr/bin/conmon", "-c", ID]), (1, 0, "systemd", ["/usr/lib/systemd/systemd"])]


def agent_chain(pid, agent=("claude", ["/home/u/.local/bin/claude", "--dangerously-skip-permissions"])):
    """The same shell, with an agent session between: git <- bash <- claude <- bwrap <- wk ai <- bash --login <- entry."""
    comm, argv = agent
    return [(pid, 600, "bash", ["bash", "-c", "git push"]), (600, 601, comm, argv),
            (601, 602, "bwrap", ["bwrap", "--dev-bind", "/", "/"]), (602, 603, "python3", ["python3", "/opt/wk-tools/wk", "ai", "claude"]),
            (603, 500, "bash", ["/bin/bash", "--login"])] + person_chain(0)[1:]


def own_chain(pid):
    """A run of wk's own: git <- bash -lc <- conmon, no person entry (a build, a probe, `wk enter --own`)."""
    return [(pid, 400, "bash", ["bash", "-lc", "git push"])] + person_chain(0)[2:]


def orphan_chain(pid):
    """A process left behind by a session that ended: reparented to init, nothing of a session behind it."""
    return [(pid, 700, "sh", ["sh", "-c", "sleep 20; git push"]), (700, 1, "sh", ["sh"]), (1, 0, "systemd", ["systemd"])]


class Place:
    """A place whose guests hold the agent pids in `agents`; a guest not in it cannot be asked. A container is never asked."""

    def __init__(self, machine, kind="container", agents=None, state="running", repo=None):
        self.kind, self.machine, self.agents, self.state, self.execs = kind, machine, agents or {}, state, []
        self.guests, self._repo = [], repo

    def podman(self):
        return ["podman"]

    def exec(self, ws, argv, tty=False, timeout=None, person=False):
        self.execs.append(ws)
        pids = self.agents.get(ws)
        return Result(1, "", "no such workspace") if pids is None else Result(0, "".join(p + "\r\n" for p in pids))

    def info(self, ws):
        return self.state

    def repo(self, ws):
        return self._repo or repos.default()

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
    """The service, serving on a socket in a thread of this process; the caller is this process, whose chain is `proc`'s."""

    def setUp(self):
        super().setUp()
        self.sockdir = tempfile.mkdtemp(prefix="wk-")
        self.addCleanup(shutil.rmtree, self.sockdir, True)
        self.module = load(SERVICE, "wkpush")
        store = self.tmp / "store"
        (store / "push-keys").mkdir(parents=True)
        for k, _, _ in repos.push_keys():
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
        self.proc = Proc(self.tmp / "proc")
        self.proc.chain(person_chain(os.getpid()))
        self.machine = Fake("here")
        self.machine.answer(PS, out="%s\twk-demo\n" % ID)
        self.machine.answer(TOOLS_ORIGIN, out="https://github.com/u/wk-tools.git\n")
        self.place = Place(self.machine)
        self.places = {"container": self.place}

    def serve(self, guest_dir=None, gate=None):
        gate = gate or pushgate.Gate(Registry(**self.places), proc=str(self.proc.root))
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

    def caller_is(self, chain):
        shutil.rmtree(self.proc.root)
        self.proc.chain(chain(os.getpid()))


@unittest.skipUnless(sys.platform == "linux", "a container's caller is its pid by SO_PEERCRED, which only Linux has; a Mac host runs this service in the podman VM")
class TestAPush(_Service):
    def test_from_a_persons_shell_ssh_runs_with_its_deploy_key_and_its_output_comes_back(self):
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
        self.assertEqual([], self.place.execs, "a container is read in /proc, never asked")

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

    def test_an_agent_session_behind_the_caller_is_refused_naming_the_remedy_and_no_ssh_runs(self):
        self.caller_is(agent_chain)
        self.serve()
        reply, frames = self.push()
        self.assertIn("an agent session (claude, pid 600) is behind this caller", reply["refused"])
        self.assertIn("wk enter demo", reply["remedy"])
        self.assertEqual({}, frames)
        self.assertEqual("", self.ssh_log())

    def test_wk_ai_and_pi_are_agent_sessions_too(self):
        for comm, argv, word in (("python3", ["python3", "/opt/wk-tools/wk", "ai", "pi"], "wk ai"),
                                 ("node", ["node", "/home/u/.local/bin/pi", "--continue"], "pi"),
                                 ("node", ["node", "/home/u/.local/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"], "pi"),
                                 ("claude", ["/opt/homebrew/bin/claude"], "claude")):
            with self.subTest(argv=argv):
                self.caller_is(lambda pid: [(pid, 600, "bash", ["bash"]), (600, 500, comm, argv)] + person_chain(0)[1:])
                self.serve()
                self.assertIn("an agent session (%s, pid 600)" % word, self.push()[0]["refused"])
                self.doCleanups()
                self.setUp()

    def test_a_run_of_wks_own_is_refused_naming_the_entry(self):
        self.caller_is(own_chain)
        self.serve()
        reply, _ = self.push()
        self.assertIn("descends from a run of wk's own (pid %d: bash -lc git push), not from a shell of yours" % os.getpid(), reply["refused"])
        self.assertIn("'wk enter --own' never publish", reply["remedy"])
        self.assertEqual("", self.ssh_log())

    def test_a_process_left_behind_by_a_session_is_refused(self):
        self.caller_is(orphan_chain)
        self.serve()
        reply, _ = self.push()
        self.assertIn("no shell of yours is behind this caller", reply["refused"])
        self.assertIn("left behind by a session or a job that ended", reply["refused"])
        self.assertEqual("", self.ssh_log())

    def test_an_agent_elsewhere_in_the_workspace_does_not_matter(self):
        """A person's shell beside an agent session: only the caller's own chain decides."""
        self.proc.chain([(9, 500, "claude", ["/home/u/.local/bin/claude"])])
        self.serve()
        self.assertEqual({"ok": True}, self.push()[0])

    def test_the_one_repository_an_agent_publishes_is_served_to_an_agent_and_only_that_one(self):
        self.caller_is(agent_chain)
        self.place = Place(self.machine, repo=repos.Repo("wk-tools"))
        self.places = {"container": self.place}
        self.serve()
        reply, _ = self.push(command="git-receive-pack 'u/wk-tools.git'", host="git@github-wk-tools")
        self.assertEqual({"ok": True}, reply)
        self.assertIn("build_key_wk-tools", self.ssh_log())
        reply, _ = self.push()
        self.assertIn("is not a push or fetch of this workspace's repositories", reply["refused"], "a fork is not a wk-tools workspace's to push")

    def test_an_unknown_caller_is_refused(self):
        self.proc.add(os.getpid(), 500, "bash", ["bash"], cgroup="0::/user.slice/session-3.scope\n")
        self.serve()
        reply, _ = self.push()
        self.assertEqual("this caller is in no workspace", reply["refused"])
        self.proc.add(os.getpid(), 500, "bash", ["bash"], cgroup=CGROUP.replace(ID, "cd" * 32))
        reply, _ = self.push()
        self.assertEqual("this caller is in no workspace", reply["refused"])
        (self.proc.root / str(os.getpid()) / "cgroup").unlink()
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
    """The egress proxy asks, naming who connected to it; the answer is the push rule's."""

    def test_a_persons_shell_may(self):
        self.serve()
        self.assertEqual({"writes": True, "why": ""}, self.ask({"verb": "writes", "pid": os.getpid()})[0])

    def test_an_agent_session_and_a_run_of_wks_own_may_not_and_the_reason_is_named(self):
        for chain, words in ((agent_chain, "an agent session (claude, pid 600) is behind this caller"), (own_chain, "a run of wk's own")):
            with self.subTest(chain=chain.__name__):
                self.caller_is(chain)
                self.serve()
                reply = self.ask({"verb": "writes", "pid": os.getpid()})[0]
                self.assertFalse(reply["writes"])
                self.assertIn(words, reply["why"])
                self.doCleanups()
                self.setUp()

    def test_nobody_may_who_is_in_no_workspace(self):
        self.serve()
        self.assertFalse(self.ask({"verb": "writes", "pid": 1})[0]["writes"])

    def test_a_guest_is_named_by_its_address_and_one_that_cannot_be_asked_says_no(self):
        vm = Place(self.machine, kind="vm", agents={"g1": [], "g2": ["5"]})
        vm.guests = [("g1", "192.0.2.2"), ("g2", "192.0.2.3"), ("g3", "192.0.2.4")]
        self.places["vm"] = vm
        self.serve()
        self.assertTrue(self.ask({"verb": "writes", "addr": "192.0.2.2"})[0]["writes"])
        self.assertFalse(self.ask({"verb": "writes", "addr": "192.0.2.3"})[0]["writes"])
        self.assertFalse(self.ask({"verb": "writes", "addr": "192.0.2.4"})[0]["writes"])
        self.assertFalse(self.ask({"verb": "writes", "addr": "192.0.2.99"})[0]["writes"])

    def test_a_question_that_names_nobody_is_refused(self):
        self.serve()
        self.assertIn("names a pid or an addr", self.ask({"verb": "writes"})[0]["refused"])


class TestAGuestsOwnSocket(_Service):
    """A macOS guest's forward ends at a socket the service made for it, and that socket names the guest, which is asked for its agents."""

    def setUp(self):
        super().setUp()
        self.vm = Place(self.machine, kind="vm", agents={"g1": [], "g2": ["5"]})
        self.places["vm"] = self.vm
        self.guests = tempfile.mkdtemp(prefix="wk-")
        self.addCleanup(shutil.rmtree, self.guests, True)

    def test_a_guest_pushes_through_its_socket_whoever_the_caller_is(self):
        (self.proc.root / str(os.getpid()) / "cgroup").unlink()
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

    def test_a_guest_that_cannot_be_asked_is_refused(self):
        self.serve(guest_dir=self.guests)
        path = self.ask({"verb": "listen", "workspace": "gone"})[0]["listening"]
        reply = self.ask({"verb": "push", "host": "git@" + ALIAS, "command": UPLOAD}, path=path)[0]
        self.assertIn("could not ask 'gone' whether an agent runs in it", reply["refused"])
        self.assertEqual("", self.ssh_log())

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
        """The client is a subprocess: its pid is this test's chain, in the workspace 'demo'."""
        class Gate(pushgate.Gate):
            def container_of(self, pid):
                return "demo"

            def allow(self, t, ws, pid):
                super().allow(t, ws, os.getpid())
        self.serve(gate=Gate(Registry(**self.places), proc=str(self.proc.root)))

    def test_what_git_would_have_sent_ssh_is_relayed_and_the_sessions_status_is_the_exit_status(self):
        self.serve_for_demo()
        cp = self.run_client("-o", "SendEnv=GIT_PROTOCOL", "git@" + ALIAS, UPLOAD, env={"GIT_PROTOCOL": "version=2"})
        self.assertEqual(7, cp.returncode, cp.stderr)
        self.assertEqual(b"banner\nhello", cp.stdout)
        self.assertIn(b"oops-on-stderr", cp.stderr)
        self.assertIn("GIT_PROTOCOL=version=2", self.ssh_log())

    def test_a_refusal_is_printed_with_its_remedy_and_exits_1(self):
        self.caller_is(agent_chain)
        self.serve_for_demo()
        cp = self.run_client("git@" + ALIAS, UPLOAD)
        self.assertEqual(1, cp.returncode)
        self.assertIn(b"error: an agent session (claude, pid 600) is behind this caller", cp.stderr)
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
    def test_a_container_and_a_guest_are_given_the_one_client_and_a_build_box_none(self):
        client = "container/push/wk-push-client.py"
        self.assertTrue(os.access(REPO / client, os.X_OK))
        for kind in ("container", "vm", "remote"):
            with self.subTest(kind=kind):
                m = Fake("ws")
                m.answer(["sh", "-c"])
                m.answer(["git", "config"])
                with mock.patch("sys.stderr"):
                    secrets.deliver(m, kind, lambda n: "", "ws", "/opt/wk-tools")
                git = [e[1] for e in m.effects if e[1][0] == "git"]
                want = [("git", "config", "--global", "core.sshCommand", "/opt/wk-tools/" + client),
                        ("git", "config", "--global", "ssh.variant", "ssh")] if kind != "remote" else []
                self.assertEqual(want, git)
        self.assertEqual(Place(Fake()).tools_src(), str(REPO))

    def test_the_switch_is_gone_and_the_dispatcher_refuses_its_name(self):
        for argv in (("push", "on"), ("push", "status")):
            with self.subTest(argv=argv), self.assertRaises(decl.Usage) as e:
                decl.parse("key", load_cmd("key").COMMAND, list(argv))
            self.assertIn("unknown verb: push", str(e.exception))

    def test_the_sockets_a_workspace_and_the_service_use(self):
        env = {"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/501"}
        self.assertEqual("/run/user/501/wk/push.sock", Store(env).push_socket())
        self.assertEqual("/home/u/.local/state/wk/push.sock", Store({"HOME": "/home/u"}).push_socket())
        self.assertEqual("/x.sock", Store(dict(env, WK_PUSH_SOCKET="/x.sock")).workspace_push_socket())
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=False):
            self.assertEqual("/run/wk/push.sock", Store(env).workspace_push_socket())
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True):
            self.assertEqual("/home/u/.wk-push.sock", Store(env).workspace_push_socket())


class TestTheGate(FlowTest):
    def setUp(self):
        super().setUp()
        self.machine = World()
        self.machine.answer(PS, out="%s\twk-demo\n%s\tnot-a-workspace\n" % (ID, "cd" * 32))
        self.place = Place(self.machine)
        self.proc = Proc(self.tmp)
        self.gate = pushgate.Gate(Registry(container=self.place), proc=str(self.tmp))

    def test_a_pid_is_the_workspace_whose_container_holds_it(self):
        self.proc.add(5, 1, "bash", ["bash"])
        self.assertEqual("demo", self.gate.container_of(5))

    def test_a_pid_in_no_workspace_container_is_none(self):
        self.proc.add(6, 1, "bash", ["bash"], cgroup="0::/user.slice/session-3.scope\n")
        self.proc.add(7, 1, "bash", ["bash"], cgroup=CGROUP.replace(ID, "ef" * 32))
        self.proc.add(8, 1, "bash", ["bash"], cgroup=CGROUP.replace(ID, "cd" * 32))
        for pid in (6, 7, 8, 99):
            with self.subTest(pid=pid):
                self.assertIsNone(self.gate.container_of(pid))

    def test_a_person_is_a_chain_from_the_person_entry_with_no_agent_in_it(self):
        self.proc.chain(person_chain(77))
        pushgate.person(self.gate.proc, 77, "demo")
        for chain, kind, words in ((agent_chain, "agent", "an agent session (claude, pid 600)"),
                                   (own_chain, "own", "a run of wk's own (pid 77: bash -lc git push)"),
                                   (orphan_chain, "orphan", "no shell of yours is behind this caller (pid 77)")):
            with self.subTest(kind=kind):
                shutil.rmtree(self.tmp)
                self.proc.chain(chain(77))
                with self.assertRaises(pushgate.Denied) as got:
                    pushgate.person(self.gate.proc, 77, "demo")
                self.assertEqual(kind, got.exception.kind)
                self.assertIn(words, got.exception.why)
                self.assertIn("wk enter demo", got.exception.remedy)

    def test_a_bridge_is_judged_as_the_command_its_exec_became(self):
        """The proxy asks with the pid of the bridge ensure-bridge.sh started, whose parent is the command that exec became."""
        def bridge(parent):
            return [(77, parent, "python3", ["python3", "/opt/wk-tools/container/proxy/bridge.py", str(parent)])]
        for rows, kind in ((bridge(500) + person_chain(0)[1:], ""),
                           (bridge(510) + [(510, 500, "sshd", ["/usr/sbin/sshd", "-i"])] + person_chain(0)[1:], ""),
                           (bridge(600) + agent_chain(0)[1:], "agent"),
                           (bridge(450) + [(450, 400, "bash", ["bash", "-lc", "make"])] + person_chain(0)[2:], "own")):
            with self.subTest(parent=rows[0][1]):
                shutil.rmtree(self.tmp, True)
                self.proc.chain(rows)
                try:
                    pushgate.person(self.gate.proc, 77, "demo")
                    got = ""
                except pushgate.Denied as e:
                    got = e.kind
                self.assertEqual(kind, got)

    def test_a_bridge_serves_only_its_commands_tree_and_a_persons_with_no_agent_between(self):
        """The bridge's own check, on the client of a connection: walked up to the command, pid 500, the bridge's parent. A person's
        bridge refuses a tool under claude; an agent session's (wk ai's exec, a run of wk's own under conmon) serves it."""
        under_command = [(500, 400, "bash", ["/bin/sh", PERSON, "/bin/bash", "--login"])] + person_chain(0)[2:]
        for rows, kind in (([(77, 500, "git", ["git", "fetch"])] + under_command, ""),
                           ([(77, 600, "gh", ["gh", "pr", "create"]), (600, 500, "claude", ["/home/u/.local/bin/claude"])] + under_command, "agent"),
                           (orphan_chain(77), "orphan"),
                           (own_chain(77), "orphan")):
            with self.subTest(rows=rows[:2]):
                shutil.rmtree(self.tmp, True)
                self.proc.chain(rows)
                try:
                    pushgate.bridged(self.gate.proc, 77, 500, "demo")
                    got = ""
                except pushgate.Denied as e:
                    got = e.kind
                self.assertEqual(kind, got)
        pushgate.bridged(self.gate.proc, 500, 500, "demo")
        shutil.rmtree(self.tmp, True)
        self.proc.chain([(77, 600, "gh", ["gh", "pr", "create"]), (600, 500, "claude", ["/home/u/.local/bin/claude"]),
                         (500, 400, "bash", ["bash", "-lc", "exec claude"])] + person_chain(0)[2:])
        pushgate.bridged(self.gate.proc, 77, 500, "demo")

    def test_a_gone_pid_and_a_loop_are_nobody_s(self):
        with self.assertRaises(pushgate.Denied) as got:
            pushgate.person(self.gate.proc, 77, "demo")
        self.assertEqual("orphan", got.exception.kind)
        self.proc.chain([(77, 78, "a", ["a"]), (78, 77, "b", ["b"])])
        with self.assertRaises(pushgate.Denied):
            pushgate.person(self.gate.proc, 77, "demo")

    def test_what_makes_a_process_an_agent(self):
        for words, exe, want in ((["/home/u/.local/bin/claude"], "", "claude"), (["claude", "-p", "hi"], "", "claude"),
                                 (["/tmp/x/notclaude"], "/home/u/.local/share/claude/versions/2.1.0", "claude"),
                                 (["python3", "/opt/wk-tools/wk", "ai", "claude"], "", "wk ai"), (["/opt/wk-tools/bin/wk", "ai", "pi"], "", "wk ai"),
                                 (["node", "/home/u/.local/bin/pi"], "", "pi"), (["node", "/x/pi-coding-agent/dist/cli.js"], "", "pi"),
                                 (["node", "/home/u/.local/bin/pixie"], "", ""), (["node", "/home/u/project/pi.js"], "", ""),
                                 (["bash", "-lc", "claude"], "", ""), (["/usr/bin/claudette"], "", ""), ([], "", ""),
                                 (["sleep", "/opt/wk-tools/wk", "ai"], "", ""), (["/opt/wk-tools/wk", "doctor"], "", "")):
            with self.subTest(words=words, exe=exe):
                self.assertEqual(want, pushgate.agent_word(words, exe))

    def test_the_person_entry_is_the_script_by_name_in_the_first_two_words(self):
        self.assertTrue(pushgate.is_person_entry(["/bin/sh", PERSON, "bash", "--login"]))
        self.assertTrue(pushgate.is_person_entry([PERSON, "git", "push"]))
        self.assertFalse(pushgate.is_person_entry(["bash", "-lc", PERSON]))
        self.assertFalse(pushgate.is_person_entry(["/bin/sh", "/tmp/wk-person", "bash"]))

    def test_the_container_place_is_never_asked_and_a_guest_is(self):
        self.proc.chain(person_chain(77))
        self.gate.allow(self.place, "demo", 77)
        self.assertEqual([], self.place.execs)
        vm = Place(self.machine, kind="vm", agents={"g1": ["3", "4"], "g2": []})
        with self.assertRaises(pushgate.Denied) as got:
            self.gate.allow(vm, "g1", 0)
        self.assertIn("an agent (claude or pi) runs in 'g1' (pid 3 4)", got.exception.why)
        self.gate.allow(vm, "g2", 0)
        self.assertIsNone(pushgate.agent_pids(vm, "gone"))
        vm.state = "exited"
        self.assertEqual([], pushgate.agent_pids(vm, "gone"))

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


class TestTheGuestScanRunsForReal(WkTest):
    """The shell the service runs inside a guest, which has no /proc: `ps` names Claude Code's executable, from any install, and a `node .../bin/pi`."""

    def test_ps_names_the_executable_and_the_node_script(self):
        ps = ("#!/bin/sh\ncase \"$*\" in\n"
              "*comm=*) printf '%s\\n' '11 /opt/homebrew/Caskroom/claude-code/2.1.236/claude' '12 /Users/admin/.local/bin/claude' "
              "'13 /Users/admin/.local/share/claude/versions/2.1.270' '14 /Applications/Claude.app/Contents/MacOS/Claude' "
              "'15 node' '16 claude' '17 /bin/zsh' '18 /usr/bin/claudette' ;;\n"
              "*command=*) printf '%s\\n' '21 node /Users/admin/.local/bin/pi --continue' '22 node /Users/admin/.local/bin/pixie' "
              "'23 /bin/zsh -c pi' '24 node /Users/admin/.local/bin/pi' ;;\nesac\n")
        (self.tmp / "ps").write_text(ps)
        (self.tmp / "ps").chmod(0o755)
        cp = subprocess.run(["sh", "-c", pushgate.AGENT_PS_SCAN], env={"PATH": "%s:/usr/bin:/bin" % self.tmp}, capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stderr)
        self.assertEqual(["11", "12", "13", "16", "21", "24"], cp.stdout.split())


class TestTheSshSessionIsTheKeysAlone(unittest.TestCase):
    def test_no_agent_and_one_identity(self):
        argv = pushgate.ssh_argv("/k/key", "git-upload-pack 'a/b.git'", "version=2")
        self.assertEqual(["git@github.com", "git-upload-pack 'a/b.git'"], argv[-2:])
        for word in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "/k/key", "SendEnv=GIT_PROTOCOL"):
            self.assertIn(word, argv)
        self.assertEqual(argv[:-2], pushgate.ssh_prefix("/k/key", "version=2"))


@requires_container_place()
class TestLivePushServiceInTheMachine(unittest.TestCase):
    """Read-only: whether this machine's push service is where every container's mount says."""

    def machine(self, cmd):
        return subprocess.run(["podman", "machine", "ssh", "wk", "--", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=60).stdout

    def test_the_unit_is_active_and_the_socket_is_in_the_mounted_directory(self):
        if "YES" not in self.machine("systemctl --user cat wk-push.service >/dev/null 2>&1 && echo YES || echo NO"):
            self.skipTest("the machine has no wk-push.service yet:  ./setup --stage sdk")
        self.assertIn("active", self.machine("systemctl --user is-active wk-push.service"))
        self.assertIn("SOCKET", self.machine('test -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/push.sock" && echo SOCKET || echo NONE'))


# `unit push.remote_forwarding`: a build box holds no deploy key at rest and nothing forwards one to it, so a push is made
# from the workstation (`wk pr open` fetches the box's branch into this machine's mirror and pushes it with the deploy key).


def forwarded(argv):
    """The agent socket this ssh command line forwards, or None; ssh takes an option's first value."""
    if "-A" in argv:
        return "$SSH_AUTH_SOCK"
    values = [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv[:-1])
              if a == "-o" and argv[i + 1].lower().startswith("forwardagent=")]
    return values[0] if values and values[0].lower() != "no" else None


class TestABoxHoldsNoKey(test_wk_places.RemoteTest):
    def test_no_ssh_to_a_box_forwards_an_agent(self):
        """No argv wk builds forwards an agent; dotfiles/ssh/config is lint.ssh_config_forwards_no_agent."""
        for argv in (self.t.machine.argv("true"), self.t.build_argv("a", ["ninja"])[0], self.t.exec_argv("a", ["true"])[0],
                     self.t.exec_argv("a", ["claude"], tty=True)[0], self.t.enter_argv("a")[0]):
            self.assertEqual("ssh", argv[0])
            self.assertIsNone(forwarded(argv), argv)


class TestAPushFromABoxIsMadeHere(test_wk_places.RemoteTest):
    def test_wk_pr_open_fetches_the_box_branch_into_the_mirror_and_pushes_from_here(self):
        self.fake.answer(["git"])
        with mock.patch.object(pr, "pr_open_target", return_value=("WebKit/WebKit", "me:eng/b", "fork", "eng/b")), \
                mock.patch.object(pr.secrets, "Secrets", lambda *a: test_pr_workflow.BoxKeys()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.fake.files["/s/push-keys/build_key_fork"] = "KEY\n"
            pr.pr_open(self.t, "a", False, False)
        mirror = Store(self.env).mirror_dir()
        here = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "git"]
        self.assertIn(("git", "-C", mirror, "fetch", "--quiet", "box.example:/home/u/wk/ws/a/WebKit",
                       "+refs/heads/eng/b:refs/wk/push/box/eng/b"), here)
        push = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "git" and "push" in e[1]]
        self.assertEqual(1, len(push), push)
        self.assertEqual(("-C", mirror), push[0][1:3])
        self.assertIn("-i /s/push-keys/build_key_fork", push[0][4])
        self.assertEqual(("push", "git@github.com:alice/WebKit.git", "refs/wk/push/box/eng/b:refs/heads/eng/b"), push[0][-3:])
        self.assertIn(("git", "-C", mirror, "update-ref", "-d", "refs/wk/push/box/eng/b"), here)
        self.assertEqual([], [c for c in self.fake.ssh_calls() if "push" in c[-1]])
        self.assertEqual(1, len([e for e in self.fake.effects if e[0] == "exec" and e[1][:3] == ("gh", "pr", "create")]))


class TestAPushOnTheBoxIsRefused(test_wk_places.RemoteTest):
    def test_every_fork_alias_on_a_box_stops_on_the_refusal_naming_wk_pr_open(self):
        """remote/provision.sh writes these blocks; the ProxyCommand ssh would run for `git push` is run here as ssh runs it."""
        text = secrets.box_alias_blocks(secrets.forks())
        proxies = re.findall(r"^    ProxyCommand (.+)$", text, re.M)
        self.assertEqual(len(secrets.forks()), len(proxies))
        cp = subprocess.run(shlex.split(proxies[0]), capture_output=True, text=True)
        self.assertEqual(1, cp.returncode)
        self.assertIn("wk pr open", cp.stderr)


# The pre-push hook's remote classification (git.hook_levels).
def _levels(forks):
    return {key: int(value) for key, value in re.findall(r"--level (\S+)=(\d+)", git.hook_levels(forks))}


class TestHookLevels(unittest.TestCase):
    def test_every_fork_is_named_by_its_ssh_alias_and_every_level_is_public(self):
        forks = list(secrets.forks()) + [("extra", "someone/WebKit", "github-extra")]
        levels = _levels(forks)
        self.assertEqual({"%s:%s" % (alias, repo) for _, repo, alias in forks} | {git.NO_PUSH}, set(levels))
        self.assertEqual(set(levels.values()), {0})

    def test_the_hooks_are_installed_even_in_a_checkout_already_set_up(self):
        before, _, after = git.pr_tool_setup_script("/src/WebKit", list(secrets.forks())).partition("state=already")
        self.assertNotIn("exit 0", before)
        self.assertIn("install-hooks $WK_HOOK_LEVELS", after)


if __name__ == "__main__":
    unittest.main()
