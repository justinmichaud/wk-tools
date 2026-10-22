"""lib/wk/wall.py against a fake machine whose workspace answers each probe from HEALTHY, keyed by a substring
of the command it runs."""
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeRegistry
from tests.support import REPO, WkTest, as_dispatched, bash, clean_env, load_cmd
from tests.test_push_service import Proc

sys.path.insert(0, str(REPO / "lib"))
import credcheck  # noqa: E402
from wk import claudelogin, decl, doctor, places, pushgate, repos, wall  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402
from tests.fakes import Fake  # noqa: E402


DOCTOR_CMD = load_cmd("doctor")

OK, MISS, NOTE = doctor.OK, doctor.MISS, doctor.NOTE

# Most specific first: the first key found in the command answers it.
HEALTHY = [
    ("sk-ant-o[ar]t", ""),
    ("/.credentials.json", claudelogin.placeholder()),
    ("api.anthropic.com/v1/models", 'HTTP/1.1 200 OK\r\n\r\n{"data": []}\n200'),
    ("hosts.yml", ""),
    ("https://github.com/ 2", "200"),
    ("example.com", "curl: (56) Received HTTP code 403 from proxy after CONNECT"),
    ("192.168.1.1", "403"),
    ("1.1.1.1", "000"),
    ("swscan", "curl: (56) Received HTTP code 403\ncurl: (56) Received HTTP code 403"),
    ("/proc/net/dev", "lo "),
    ("ls -d", ""),
    ("command -v bwrap", "WKBWRAP"),
    ("mktemp -d /tmp/wk-wall", "BLOCKED\nWROTE"),
    ("PRIVATE KEY", ""),
    ("GITHUB_COM_TOKEN", "wk-injects-this"),
    ("GH_TOKEN", "wk-injects-this"),
    ("BUGS_WEBKIT_ORG_PASSWORD", "wk-injects-this"),
    ("test -r /secrets/claude-token", ""),
    ("person:git ls-remote", "0123abcd\trefs/heads/main"),
    ("git ls-remote", "error: this caller descends from a run of wk's own (pid 9: bash -lc git ls-remote), not from a shell of yours\n"
                      "    a publish is made from a shell of your own:  wk enter demo"),
    ("git config --global --get core.sshCommand", "/opt/wk-tools/container/push/wk-push-client.py"),
    ("test -S /run/wk/ssh-agent.sock", ""),
    ("https://api.github.com/ 2", "200"),
    ("api.github.com/user", "200"),
    ("person:/pulls", "422"),
    ("/pulls", "412"),
    ("/git/refs", "422"),
    ("CLAUDE_CODE_OAUTH_TOKEN:+set", ""),
    ("claude auth status", '{"loggedIn": true, "authMethod": "claude.ai"}'),
    ("webkitscmpy.setup", "true"),
    ("rest/version", "200"),
    ("http_code}' -X POST -H", "412"),
    ("-D - -X POST -H", 'HTTP/1.1 400 Bad Request\r\n\r\n{"code": 50, "message": "a product is required"}'),
    ("gpu-probe.sh", Result(0, "renderer=NVIDIA Tegra | vendor=NVIDIA\n", "")),
    ("touch /opt/wk-tools/.wk-write-probe", "touch: cannot touch '/opt/wk-tools/.wk-write-probe': Read-only file system"),
    ("rm -f /opt/wk-tools", ""),
    ("test -s", Result(0, "", "")),
    ("test -d", Result(0, "", "")),
]


def rows_text(rows):
    return "\n".join("%s %s -> %s" % r for r in rows)


ENSURE_BRIDGE = "container/proxy/ensure-bridge.sh"
CONMON, ENTRY, EXEC, BRIDGE_PID = 400, 500, 600, 601


class _Wall(unittest.TestCase):
    """The workspace's process table is a fake /proc: conmon, a person's entry under it, and this process -- `me`, the wk
    command a check inside a workspace runs in -- under that. Each exec's bridge is added to it as ensure-bridge.sh starts one."""
    kind = "container"
    me = ["python3", "/opt/wk-tools/wk", "ai", "claude"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-wall-"))
        self.addCleanup(os.system, "rm -rf %s" % self.tmp)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_VM_STORE": str(self.tmp / "vmstore"),
                    "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_IN_VM": "1", "WK_MARKER": str(self.tmp / "marker"),
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "marker").write_text("name=demo\nsrc=/src/WebKit\n")
        self.fake = Fake()
        self.answers = dict(HEALTHY)
        self.asked = []
        self.asked_as = []
        self.fake.react(["env"], self._exec)
        self.fake.react(["bash", "-lc"], self._exec)
        self.fake.react([os.path.join(str(REPO), ENSURE_BRIDGE)], self._exec)
        self.proc, self.proc_lock = Proc(self.tmp / "proc"), threading.Lock()
        self.proc.chain([(CONMON, 1, "conmon", ["/usr/bin/conmon"]), (ENTRY, CONMON, "bash", ["/bin/bash", "/opt/wk-tools/" + pushgate.PERSON, "/bin/bash", "--login"]),
                         (os.getpid(), ENTRY, "python3", self.me)])
        real = pushgate.Proc
        patch = mock.patch.object(pushgate, "Proc", lambda: real(str(self.proc.root)))
        patch.start()
        self.addCleanup(patch.stop)
        self.fake.answer(["python3", os.path.join(str(REPO), "lib", "secretfile.py"), "present"])
        self.fake.answer(["python3", os.path.join(str(REPO), "lib", "secretfile.py"), "read"], out="stored-value")
        verdict = mock.patch.object(credcheck, "verdict", return_value="ok\tit works")
        verdict.start()
        self.addCleanup(verdict.stop)
        self.reg = places.Registry(str(REPO), env=self.env, machine=self.fake)
        self.driver = self.load(self.kind)

    def load(self, kind):
        """A guest's store is a macOS host's own, on any platform the suite runs on."""
        if kind == "vm":
            patch = mock.patch("wk.store.Store.macos_host", new_callable=mock.PropertyMock, return_value=True)
            patch.start()
            self.addCleanup(patch.stop)
        return self.reg.load(kind)

    def _exec(self, argv, fake):
        """A container's exec ends in the command; a workspace-local one quotes it after `exec`."""
        person = self._bridges_a_person(argv)
        argv = argv[1:] if argv[0].endswith(ENSURE_BRIDGE) else argv
        return self._answer(shlex.split(argv[-1])[-1] if argv[:2] == ["bash", "-lc"] else argv[-1], bool(person))

    def _bridges_a_person(self, argv):
        """Whether the push service takes the bridge this exec starts for a person's: it is the child of the command
        ensure-bridge.sh execs into, which is conmon's child for a container's exec and this process's for one inside; None with no bridge."""
        i = next((i for i, a in enumerate(argv) if a.endswith(ENSURE_BRIDGE)), None)
        if i is None:
            return None
        with self.proc_lock:
            self.proc.add(EXEC, CONMON if self.kind == "container" else os.getpid(), "bash", argv[i + 1:])
            self.proc.add(BRIDGE_PID, EXEC, "python3", ["python3", "/opt/wk-tools/container/proxy/bridge.py", str(EXEC)])
            try:
                pushgate.person(pushgate.Proc(), BRIDGE_PID, "demo")
                return True
            except pushgate.Denied:
                return False

    def _direct(self, ws, argv, tty=False, timeout=None, person=False):
        """For a driver whose own exec would reach tart or ssh."""
        return self._answer(argv[-1], person)

    def _answer(self, cmd, person=False):
        """curl's `-D -` through the workspace's https_proxy prints the proxy's CONNECT reply first, unless suppressed.
        A `person:` key answers a person's run of that command ahead of the plain key."""
        self.asked.append(cmd)
        self.asked_as.append((cmd, person))
        connect = "HTTP/1.0 200 Connection established\r\n\r\n" if "curl" in cmd and "-D -" in cmd and "--suppress-connect-headers" not in cmd else ""
        found = [v for k, v in self.answers.items() if k.startswith("person:") and person and k[len("person:"):] in cmd]
        found += [v for k, v in self.answers.items() if not k.startswith("person:") and k in cmd]
        if found:
            return found[0] if isinstance(found[0], Result) else Result(0, connect + found[0], "")
        return Result(0, "", "")

    def set(self, key, value):
        self.answers[key] = value

    def wall(self, want_gpu=False):
        return wall.Wall(str(REPO), self.driver, "demo", self.fake, want_gpu)

    def check(self, name, want_gpu=False):
        return getattr(self.wall(want_gpu), name)()

    def misses(self, rows):
        return [r for r in rows if r[0] == MISS]

    def assertPasses(self, rows):
        self.assertEqual([], self.misses(rows), rows_text(rows))

    def assertFails(self, rows, *words, n=1):
        self.assertEqual(n, len(self.misses(rows)), rows_text(rows))
        for w in words:
            self.assertIn(w, rows_text(rows))


GPU = {"want_gpu": True}
LOGIN = '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-x", "refreshToken": "wk-injects-this"}}'
# A check passing on HEALTHY changed by `answers`: (check, its arguments, answers, words its rows say).
PASSES = (
    ("github", {}, {}, ()), ("allowlist", {}, {}, ()), ("off_allowlist", {}, {}, ()),
    ("off_allowlist", {}, {"1.1.1.1": ""}, ()), ("softwareupdate", {}, {}, ()), ("isolation", {}, {}, ()),
    ("commit_wall", {}, {}, ()),
    ("secrets_view", {}, {}, ("no credential this kind is not given is readable in here (claude)",)),
    ("push_refused", {}, {}, ("no ssh-agent socket", "the push client", "a push from wk's own run is refused: this caller descends from a run of wk's own",
                              "a push from a person's shell is served")),
    ("github_read", {}, {}, ("a read is authenticated (HTTP 200)",)),
    ("github_write", {}, {}, ("a write from wk's own run is refused by the injector (HTTP 412)", "a write from a person's shell reaches GitHub (HTTP 422")),
    ("pr_tool_setup", {}, {}, ()), ("bugzilla_read", {}, {}, ()), ("bugzilla_write", {}, {}, ("a Bugzilla write from wk's own run is refused",)),
    ("gpu", {}, {"gpu-probe.sh": Result(1, "renderer=llvmpipe\n", "")}, ("llvmpipe",)),
    ("gpu", {}, {"gpu-probe.sh": Result(2, "", "")}, ()),
)
# A check's one miss when HEALTHY is changed by `answers`, and the words it says.
FAILS = (
    ("github", {}, {"https://github.com/ 2": "000"}, ("github unreachable through the proxy (got '000')",)),
    ("allowlist", {}, {"example.com": ""}, ("example.com was NOT refused",)),
    ("off_allowlist", {}, {"192.168.1.1": "200"}, ("reaching the LAN gateway returned '200'",)),
    ("off_allowlist", {}, {"1.1.1.1": "200"}, ("direct egress succeeded",)),
    ("softwareupdate", {}, {"swscan": "curl: (56) Received HTTP code 403"}, ("swscan/gdmf are reachable", "vm/desktop.sh")),
    ("isolation", {}, {"/proc/net/dev": "lo eth0 "}, ("workspace has network interfaces: lo eth0",)),
    ("isolation", {}, {"/proc/net/dev": ""}, ("could not enumerate interfaces",)),
    ("commit_wall", {}, {"command -v bwrap": ""}, ("no bwrap in the workspace", "refuses to start")),
    ("commit_wall", {}, {"mktemp -d /tmp/wk-wall": "COMMITTED\nWROTE"}, ("commit wall did NOT block a commit",)),
    ("commit_wall", {}, {"mktemp -d /tmp/wk-wall": "BLOCKED\nNOWRITE"}, ("blocks an ordinary write too",)),
    ("no_credentials_inside", {}, {"PRIVATE KEY": "/home/u/.ssh/id_fork"}, ("private key material inside the workspace", "id_fork")),
    ("no_credentials_inside", {}, {"hosts.yml": "/home/u/.config/gh/hosts.yml"}, ("GitHub credential inside the workspace",)),
    ("secrets_view", {}, {"test -r /secrets/claude-token": "yes"}, ("/secrets/claude-token is readable in 'demo'", "Secrets.publish_view")),
    ("push_refused", {}, {"test -S /run/wk/ssh-agent.sock": "agent"}, ("an ssh-agent reaches this workspace",)),
    ("push_refused", {}, {"git config --global --get core.sshCommand": ""}, ("is not the push client", "'wk rm demo' and 'wk new'")),
    ("push_refused", {"n": 2}, {"git ls-remote": "Permission denied (publickey).", "person:git ls-remote": "Permission denied (publickey)."},
     ("a push from wk's own run was not refused", "Permission denied", "a push from a person's shell was refused or failed")),
    ("push_refused", {"n": 2}, {"git ls-remote": "error: this caller is in no workspace\n    ask the host",
                                "person:git ls-remote": "error: this caller is in no workspace\n    ask the host"},
     ("refused wk's own run for 'this caller is in no workspace'", "unmeasured", "journalctl --user -u wk-push",
      "a push from a person's shell was refused or failed")),
    ("push_refused", {}, {"person:git ls-remote": "error: no shell of yours is behind this caller (pid 9)"},
     ("a push from a person's shell was refused or failed: error: no shell of yours",)),
    ("github_read", {}, {"api.github.com/user": "000"}, ("rather than 200 or 401",)),
    ("github_read", {}, {"api.github.com/user": "000", "https://api.github.com/ 2": "000"},
     ("the injector is not in the path", "systemctl --user status wk-github-inject", "/run/wk/wk-github-ca.pem")),
    ("github_write", {}, {"/pulls": "422"}, ("a GitHub write from wk's own run answered '422'", "expected 412")),
    ("github_write", {}, {"person:/pulls": "412"}, ("a write from a person's shell answered '412'", "fails for a person too")),
    ("github_write", {"n": 2}, {"/pulls": "", "person:/pulls": ""}, ("a GitHub write from wk's own run answered 'nothing'",
                                                                      "a write from a person's shell answered 'nothing'")),
    ("agent_credential", {}, {"claude auth status": '{"loggedIn": false}'}, ("not logged in", "'wk rm demo' and 'wk new'", "'wk start demo'")),
    ("agent_credential", {}, {"claude auth status": ""}, ("It answered (first 80 bytes): b''", "claude --version")),
    ("claude_login", {}, {"/.credentials.json": LOGIN}, ("is not the placeholder login", "'wk start demo'")),
    ("claude_login", {}, {"/.credentials.json": ""}, ("is not the placeholder login", "'wk start demo'")),
    ("claude_login", {}, {"/.credentials.json": "[]"}, ("is not the placeholder login", "'wk start demo'")),
    ("claude_login", {}, {"sk-ant-o[ar]t": "/run/wk/.credentials.json"},
     ("a claude.ai token is readable in 'demo': /run/wk/.credentials.json", "'wk key check claude-login'")),
    ("claude_login", {}, {"api.anthropic.com/v1/models": 'HTTP/1.1 401 Unauthorized\r\n\r\n{"type": "error"}\n401'},
     ("Anthropic refused", "wk key set claude-login --replace")),
    ("claude_login", {}, {"api.anthropic.com/v1/models": "\n000"}, ("not in the path", "wk-github-inject")),
    ("pr_tool_setup", {}, {"webkitscmpy.setup": ""}, ("has not completed", "wk sync demo --fix")),
    ("pr_tool_setup", {}, {"webkitscmpy.setup": "false"}, ("has not completed", "wk sync demo --fix")),
    ("bugzilla_read", {}, {"rest/version": "000"}, ("not in the path for it", "systemctl --user status wk-github-inject")),
    ("bugzilla_write", {}, {"http_code}' -X POST -H": "410"}, ("from wk's own run answered '410'", "serving what is not a person's shell")),
    ("gpu", GPU, {"gpu-probe.sh": Result(1, "renderer=llvmpipe\n", "")}, ("only software rendering",)),
    ("gpu", GPU, {"gpu-probe.sh": Result(2, "", "")}, ("no usable EGL inside the workspace (probe exit 2)",)),
)

OUTAGE = "did not answer through the injector (HTTP 504) -- an upstream outage, not the sandbox"
# A check that passes with a note saying `words`: no standing token, or an upstream outage behind the injector.
NOTES = (
    ("github_read", {}, {"api.github.com/user": "401"}, ("60 requests an hour",)),
    ("github_read", {}, {"api.github.com/user": "HTTP/1.1 502 Bad Gateway\r\nContent-Length: 5\r\n\r\n502"}, ("an upstream outage",)),
    ("github_read", {}, {"api.github.com/user": "504"}, ("GitHub " + OUTAGE,)),
    ("github_write", {}, {"/pulls": "504"}, ("GitHub " + OUTAGE,)),
    ("bugzilla_read", {}, {"rest/version": "504"}, ("Bugzilla " + OUTAGE,)),
    ("bugzilla_write", {}, {"http_code}' -X POST -H": "504"}, ("Bugzilla " + OUTAGE,)),
    ("claude_login", {}, {"api.anthropic.com/v1/models": "HTTP/1.1 401 Unauthorized\r\n\r\nthe wk credential injector put no "
                          "claude.ai login on this request: this machine holds no claude.ai login\n401"}, ("holds no claude.ai login",)),
    ("claude_login", {}, {"api.anthropic.com/v1/models": "HTTP/1.1 504 Gateway Timeout\r\n\r\napi.anthropic.com did not answer\n504"},
     ("an upstream outage",)),
)
# A 502 carrying the injector's `X-Wk-Injector` header (TLS or DNS toward the host failed) is a miss naming the injector.
FAULT = "HTTP/1.1 502 Bad Gateway\r\nX-Wk-Injector: SSLCertVerificationError\r\nContent-Length: 5\r\n\r\n502"
FAULTS = (
    ("github_read", {}, {"api.github.com/user": FAULT}, "GitHub"),
    ("bugzilla_read", {}, {"rest/version": FAULT}, "Bugzilla"),
    ("bugzilla_write", {}, {"http_code}' -X POST -H": FAULT}, "Bugzilla"),
)


class TestEachCheck(_Wall):
    def test_each_passes_on_a_healthy_answer(self):
        for name, kw, answers, words in PASSES:
            with self.subTest(check=name, answers=answers, **kw):
                self.answers = {**dict(HEALTHY), **answers}
                rows = self.check(name, **kw)
                self.assertPasses(rows)
                for w in words:
                    self.assertIn(w, rows_text(rows))

    def test_each_wrong_answer_is_one_miss_naming_it(self):
        """One miss per wrong answer: a check asked as two publishers misses once for each."""
        for name, kw, answers, words in FAILS:
            with self.subTest(check=name, answers=answers, **kw):
                self.answers = {**dict(HEALTHY), **answers}
                self.assertFails(self.check(name, **{k: v for k, v in kw.items() if k != "n"}), *words, n=kw.get("n", 1))

    def test_each_answer_the_sandbox_is_not_at_fault_for_is_a_note(self):
        for name, kw, answers, words in NOTES:
            with self.subTest(check=name, answers=answers, **kw):
                self.answers = {**dict(HEALTHY), **answers}
                rows = self.check(name, **kw)
                self.assertPasses(rows)
                for w in words:
                    self.assertIn(w, "\n".join(r[1] for r in rows if r[0] == NOTE))

    def test_an_injector_fault_is_a_miss_naming_the_injector_not_an_outage(self):
        for name, kw, answers, host in FAULTS:
            with self.subTest(check=name, **kw):
                self.answers = {**dict(HEALTHY), **answers}
                rows = self.check(name, **kw)
                self.assertEqual(MISS, rows[0][0])
                self.assertIn("%s: the injector failed to verify or resolve the host (SSLCertVerificationError)" % host, rows[0][1])
                self.assertIn("wk-github-inject", rows[0][2])
                self.assertNotIn("upstream outage", rows[0][1])


class TestIsolation(_Wall):
    def test_a_host_path_fails_by_name(self):
        self.set("ls -d", "/host/home")
        rows = self.check("isolation")
        self.assertEqual(len(wall.HOST_PATHS), len(self.misses(rows)), rows_text(rows))
        self.assertIn("host path visible inside the workspace: /host/home", rows_text(rows))


class TestCommitWall(_Wall):
    def test_the_paths_are_the_ones_the_session_walls(self):
        self.check("commit_wall")
        probe = [c for c in self.asked if "mktemp" in c][0]
        self.assertIn('W="%s"' % " ".join(wall.commit_wall_prefix(str(REPO), "$D")), probe)
        self.assertIn("--ro-bind-try $D/.git/ORIG_HEAD $D/.git/ORIG_HEAD", probe)


class TestNoCredentialsInside(_Wall):
    def test_a_healthy_workspace_passes(self):
        rows = self.check("no_credentials_inside")
        self.assertPasses(rows)
        for var in wall.placeholders():
            self.assertIn("%s in the workspace is the placeholder" % var, rows_text(rows))

    def test_an_unset_placeholder_names_what_exports_it(self):
        for var in wall.placeholders():
            with self.subTest(var=var):
                self.set(var, "")
                self.assertFails(self.check("no_credentials_inside"), "%s is unset in here" % var, "container/proxy/ensure-bridge.sh")
                self.set(var, "wk-injects-this")

    def test_a_real_token_fails_and_is_never_printed(self):
        for var in wall.placeholders():
            with self.subTest(var=var):
                self.set(var, "ghp-a-real-one")
                rows = self.check("no_credentials_inside")
                self.assertFails(rows, "%s in the workspace is not the placeholder" % var)
                self.assertNotIn("ghp-a-real-one", rows_text(rows))
                self.set(var, "wk-injects-this")



class TestTheKeyScanRunsForReal(WkTest):
    """The scan against a real tree shaped like a guest's home, whose checkout (with WebKit's PEM fixtures) is
    inside it: what it does not walk is the point."""

    PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot a real key\n"

    def home(self):
        h = self.tmp / "home"
        (h / "WebKit" / "Source" / "test").mkdir(parents=True)
        (h / "WebKit" / "Source" / "test" / "cert.pem").write_text(self.PEM)
        (h / ".ssh").mkdir()
        return h

    def scan(self, home):
        cp = bash("HOME=%s\n%s" % (home, wall.KEY_SCAN))
        self.assertEqual(0, cp.returncode, cp.stderr)
        return cp.stdout.strip()

    def test_a_pem_in_the_checkout_is_not_reported(self):
        self.assertEqual("", self.scan(self.home()))

    def test_a_key_in_dot_ssh_or_the_top_of_home_or_under_dot_claude_is(self):
        home = self.home()
        (home / ".ssh" / "id_fork").write_text(self.PEM)
        (home / "leaked_key").write_text(self.PEM)
        (home / ".claude" / "deep").mkdir(parents=True)
        (home / ".claude" / "deep" / "k").write_text(self.PEM)
        out = self.scan(home)
        for name in ("id_fork", "leaked_key", "deep/k"):
            self.assertIn(name, out)

    def test_a_transcript_quoting_the_words_is_not_reported_and_one_holding_a_key_is(self):
        home = self.home()
        (home / ".claude" / "projects").mkdir(parents=True)
        quoted = home / ".claude" / "projects" / "quoted.jsonl"
        quoted.write_text(json.dumps({"text": "KEY_SCAN greps 'PRIVATE KEY'\n" + self.PEM + "   300\tPEM = " + repr(self.PEM)}) + "\n")
        self.assertEqual("", self.scan(home))
        body = "A" * 64
        for name, text in (("cat.jsonl", self.PEM.splitlines()[0] + "\n" + body),
                           ("read.jsonl", "     1\t" + self.PEM.splitlines()[0] + "\n     2\t" + body)):
            (home / ".claude" / "projects" / name).write_text(json.dumps({"text": text}) + "\n")
        out = self.scan(home)
        self.assertNotIn("quoted", out)
        for name in ("cat.jsonl", "read.jsonl"):
            self.assertIn(name, out)


class TestPushRefused(_Wall):
    def test_a_container_is_asked_as_wks_own_run_then_as_a_persons_shell(self):
        rows = self.check("push_refused")
        probes = [(c, p) for c, p in self.asked_as if "git ls-remote" in c]
        self.assertEqual([False, True], [p for _, p in probes])
        self.assertIn("git@github-webkit:", probes[0][0])
        self.assertNotIn('"$d/claude"', probes[0][0], "no stand-in agent: the service reads a container's ancestry")
        self.assertPasses(rows)

    def test_the_one_repository_an_agent_publishes_is_served_to_wks_own_run(self):
        self.fake.answer(["git", "-c", "safe.directory=*", "-C", self.driver.tools_src(), "remote", "get-url", "origin"], out="https://github.com/u/wk-tools.git\n")
        self.set("git ls-remote", "0123abcd\trefs/heads/main")
        with mock.patch.object(self.driver, "repo", return_value=repos.Repo("wk-tools")):
            rows = self.check("push_refused")
        self.assertPasses(rows)
        self.assertIn("a push of u/wk-tools from wk's own run is served: the one repository an agent publishes", rows_text(rows))
        self.assertIn("git@github-wk-tools:u/wk-tools.git", self.asked[-1])


class TestAGuestsPublishers(_Wall):
    """A guest has no chain the service can read: a stand-in agent is run beside the probe, and a person's write is measured after the parallel pass."""
    kind = "vm"

    def setUp(self):
        super().setUp()
        self.driver.exec = self._direct
        self.driver.info = lambda ws: "running"
        self.set("git config --global --get core.sshCommand", self.driver.tools("demo") + "/container/push/wk-push-client.py")

    def test_the_push_probe_runs_beside_a_stand_in_agent(self):
        rows = self.check("push_refused")
        probe = next(c for c in self.asked if "git ls-remote" in c)
        self.assertIn('cp "$(command -v sh)" "$d/claude"', probe)
        self.assertLess(probe.index('"$d/claude" -c'), probe.index("git ls-remote"))
        self.assertIn("a push from an agent in the guest is refused", rows_text(rows))

    def test_a_persons_write_is_unmeasured_while_an_agent_runs_or_with_no_fork(self):
        self.set("ps -Ao", "4242")
        rows = self.check("person_write")
        self.assertEqual([], self.misses(rows))
        self.assertIn("an agent runs in 'demo'", rows_text(rows))
        self.assertFalse([c for c in self.asked if "/git/refs" in c])
        with mock.patch.object(wall.secrets, "forks", return_value=[]):
            self.assertIn("no fork is configured", rows_text(self.check("person_write")))

    def test_a_persons_write_reaches_github_and_a_412_names_the_push_service(self):
        self.assertIn("a write from a person's shell reaches GitHub (HTTP 422", rows_text(self.check("person_write")))
        self.set("/git/refs", "412")
        self.assertFails(self.check("person_write"), "answered '412'", "fails for a person too")

    def test_a_persons_write_is_probed_after_every_stand_in_agent(self):
        self.fake.files[os.path.join(self.driver.vm_dir(), "demo.unfiltered")] = ""
        wall.from_host(str(REPO), self.driver, "demo", self.fake, doctor.Report(io.StringIO()))
        last_agent = max(i for i, c in enumerate(self.asked) if '"$d/claude"' in c)
        self.assertGreater(min(i for i, c in enumerate(self.asked) if "/git/refs" in c), last_agent)


class TestGitHubRead(_Wall):
    def test_a_refused_standing_token_is_a_note_naming_both_remedies(self):
        self.set("https://api.github.com/ 2", "401")
        self.set("api.github.com/user", "401")
        rows = self.check("github_read")
        self.assertPasses(rows)
        for w in ("every API read in here is refused", "wk key check github-pat", "wk start demo", "wk key set github-pat --replace"):
            self.assertIn(w, rows[0][1])
        self.assertNotIn("60 requests an hour", rows[0][1])



class TestAgentCredential(_Wall):
    TOKEN = '{"loggedIn": true, "authMethod": "oauth_token"}'

    def test_a_container_with_the_placeholder_login_passes(self):
        rows = self.check("agent_credential")
        self.assertPasses(rows)
        self.assertIn("from the placeholder claude.ai login credential", rows_text(rows))

    def test_the_token_beside_the_login_wins_and_fails(self):
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "set")
        self.set("claude auth status", self.TOKEN)
        self.assertFails(self.check("agent_credential"), "the token wins", n=2)

    def test_a_build_box_is_given_the_token(self):
        self.driver = self.reg.load("remote")
        self.driver.exec = self._direct
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "set")
        self.set("claude auth status", self.TOKEN)
        self.assertPasses(self.check("agent_credential"))
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "")
        self.assertFails(self.check("agent_credential"), "no $CLAUDE_CODE_OAUTH_TOKEN", "usable claude")

    def test_no_cli_is_missing_not_unreadable(self):
        self.set("claude auth status", "wk-no-claude-cli")
        rows = self.check("agent_credential")
        self.assertFails(rows, "no 'claude' on $PATH")
        self.assertNotIn("It answered", rows_text(rows))

    def test_the_control_sequences_a_tty_wraps_the_json_in_are_read_through(self):
        raw = (b'\x1b7\x1b[r\x1b8\x1b[?25h{\r\r\n\x1b[3G"loggedIn":\x1b[15Gtrue,\r\r\n\x1b[3G"authMethod":\x1b[17G"claude.ai"\r\r\n}\r\r\n'
               b'\x1b[?25h\x1b[?1006l\x1b(B\x0f\x1b[>4m\x1b[<u\x1b[?1004l\x1b7\x1b[r\x1b8\x1b[?25h\n')
        self.assertEqual("True claude.ai", wall.claude_status(raw.decode("utf-8", "surrogateescape")))


class TestClaudeLogin(_Wall):
    def test_the_placeholder_no_token_and_an_injected_request_pass(self):
        rows = self.check("claude_login")
        self.assertPasses(rows)
        self.assertIn("HTTP 200", rows_text(rows))
        self.assertIn("Authorization: Bearer %s" % claudelogin.PLACEHOLDER,
                      next(c for c in self.asked if "api.anthropic.com" in c))

    def test_the_scan_reads_no_mount_an_older_workspace_had(self):
        for old in ("/agent-rw", "My Shared Files"):
            with self.subTest(old=old):
                self.assertNotIn(old, wall.CLAUDE_TOKEN_SCAN)

    def test_a_build_box_is_not_asked(self):
        self.driver = self.reg.load("remote")
        self.assertNotIn("claude-login", [n for n, _ in self.wall().from_host()])


class TestGitWebkitSetup(_Wall):
    def test_the_marker_true_passes_and_is_asked_of_the_checkout(self):
        self.assertPasses(self.check("pr_tool_setup"))
        self.assertIn("git -C /src/WebKit config --get webkitscmpy.setup", self.asked)

    def test_only_a_repo_that_uses_the_pr_tool_is_asked(self):
        from wk import repos
        self.assertIn("pr-tool-setup", [n for n, _ in self.wall().from_host()])
        self.driver.repo = lambda ws: repos.Repo("wk-tools")
        self.assertNotIn("pr-tool-setup", [n for n, _ in self.wall().from_host()])


class TestGpu(_Wall):
    def test_hardware_passes_and_the_probe_output_is_kept(self):
        rows = self.check("gpu", want_gpu=True)
        self.assertPasses(rows)
        self.assertIn((NOTE, "renderer=NVIDIA Tegra | vendor=NVIDIA", ""), rows)

    def test_an_arch_with_no_gpu(self):
        self.fake.files[os.path.join(self.driver.store.ws_dir("demo"), "arch")] = "armhf\n"
        self.assertPasses(self.check("gpu"))
        self.assertFails(self.check("gpu", want_gpu=True), "--gpu on an armhf workspace")

    def test_a_guests_gpu_is_the_benchmarks_to_measure(self):
        with mock.patch.object(self.driver, "os", return_value="macos"):
            rows = self.check("gpu", want_gpu=True)
        self.assertPasses(rows)
        self.assertIn("its desktop rows above say whether its window is covered", rows_text(rows))


class TestTheContainersHostSide(_Wall):
    def setUp(self):
        super().setUp()
        self.fake.answer(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], out="true\n")
        self.fake.answer(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"])

    def test_rootless_proxied_and_read_only(self):
        self.assertPasses(self.check("rootless_proxy"))

    def test_each_fails_on_its_own(self):
        self.fake.answer(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], out="false\n")
        self.assertFails(self.check("rootless_proxy"), "podman is NOT rootless")
        self.fake.answer(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], out="true\n")
        self.fake.answer(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"], rc=3)
        self.assertFails(self.check("rootless_proxy"), "egress proxy is not running")

    def test_a_writable_tools_mount_fails_and_the_probe_is_removed(self):
        self.set("touch /opt/wk-tools/.wk-write-probe", "")
        self.assertFails(self.check("rootless_proxy"), "/opt/wk-tools is writable")
        self.assertIn("rm -f /opt/wk-tools/.wk-write-probe", self.asked)


class TestTheChecksRunAtOnce(unittest.TestCase):
    def test_every_check_runs_at_once_and_the_order_is_kept(self):
        met = threading.Barrier(6, timeout=10)

        def meets(n):
            return lambda: (met.wait(), [doctor.ok(n)])[1]
        out = wall.run_at_once([(str(i), meets(str(i))) for i in range(6)])
        self.assertEqual([(str(i), [doctor.ok(str(i))]) for i in range(6)], out)

    def test_a_check_that_dies_is_unmeasured_not_passed(self):
        def dies():
            raise OSError("ssh went away")
        out = wall.run_at_once([("gpu", dies)])
        self.assertEqual(MISS, out[0][1][0][0])
        self.assertIn("the 'gpu' probe died before it reported anything (ssh went away)", out[0][1][0][1])


class TestFromTheHost(_Wall):
    def setUp(self):
        super().setUp()
        self.fake.answer(["podman", "inspect", "wk-demo"], out="running\n")
        self.fake.files[os.path.join(self.driver.store.ws_dir("demo"), "home", places.READY_MARKER)] = ""
        self.fake.answer(["podman", "info"], out="true\n")
        self.fake.answer(["systemctl", "--user"])

    def report(self):
        out = io.StringIO()
        rep = doctor.Report(out)
        self.publishing = wall.from_host(str(REPO), self.driver, "demo", self.fake, rep)
        return rep, out.getvalue()

    def test_a_healthy_container_passes_every_check(self):
        rep, out = self.report()
        self.assertEqual(0, rep.missing, out)
        self.assertFalse(self.publishing)
        for w in ("workspace running", "a push from wk's own run is refused", "a push from a person's shell is served",
                  "the placeholder login is authenticated", "commit wall", "podman is rootless", "no network interface but loopback", "github reachable"):
            self.assertIn(w, out)

    def test_the_write_probe_runs_after_the_parallel_pass(self):
        self.report()
        self.assertNotIn("rm -f /opt/wk-tools/.wk-write-probe", self.asked)
        self.assertEqual("touch /opt/wk-tools/.wk-write-probe 2>&1", self.asked[-1])

    def test_a_containers_person_is_measured_in_the_pass_and_no_stand_in_agent_runs(self):
        self.assertEqual([], self.check("person_write"))
        self.report()
        self.assertFalse([c for c in self.asked if '"$d/claude"' in c])
        self.assertEqual([False, True], [p for c, p in self.asked_as if "/pulls" in c])

    def test_a_push_that_is_not_refused_is_publishing_and_a_failed_read_is_not(self):
        self.set("git ls-remote", "")
        self.report()
        self.assertTrue(self.publishing)
        self.answers = dict(HEALTHY)
        self.set("rest/version", "000")
        rep, _ = self.report()
        self.assertEqual((False, 1), (self.publishing, rep.missing))

    def test_a_stopped_workspace_fails(self):
        self.fake.answer(["podman", "inspect", "wk-demo"], out="exited\n")
        rep, out = self.report()
        self.assertIn("workspace state: exited", out)
        self.assertGreaterEqual(rep.missing, 1)

    def test_an_absent_workspace_and_a_remote_target_are_refused(self):
        self.fake.answer(["podman", "inspect", "wk-demo"], rc=125)
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.report()
        self.assertIn("no such workspace: demo", err.getvalue())
        self.driver = self.reg.load("remote")
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.report()
        self.assertIn("a remote place has none", err.getvalue())

    def test_the_egress_checks_are_gated_on_the_driver(self):
        names = [n for n, _ in self.wall().from_host()]
        self.assertIn("egress-github", names)
        self.assertNotIn("egress-softwareupdate", names)
        with mock.patch.object(self.driver, "egress_filtered", return_value=False):
            names = [n for n, _ in self.wall().from_host()]
        self.assertFalse([n for n in names if n.startswith("egress-")], names)

    def test_the_credential_checks_are_asked_of_every_kind_and_isolation_of_a_container_only(self):
        self.assertIn("isolation", [n for n, _ in self.wall().from_host()])
        self.driver = self.load("vm")
        names = [n for n, _ in self.wall().from_host()]
        self.assertNotIn("isolation", names)
        self.assertNotIn("commit-wall", names)
        for n in ("no-credentials-inside", "push", "github-read", "github-write", "egress-softwareupdate"):
            self.assertIn(n, names)


class TestAGuest(_Wall):
    kind = "vm"

    def test_an_unfiltered_guest_is_a_failure(self):
        self.driver.info = lambda ws: "running"
        self.driver.exec = self._direct
        self.fake.files[os.path.join(self.driver.vm_dir(), "demo.unfiltered")] = ""
        out = io.StringIO()
        rep = doctor.Report(out)
        wall.from_host(str(REPO), self.driver, "demo", self.fake, rep)
        self.assertIn("booted with WK_VM_UNFILTERED", out.getvalue())
        self.assertIn("wk stop demo && wk start demo", out.getvalue())
        self.assertNotIn("github reachable", out.getvalue())
        self.assertIn("no golden base VM 'wk-base'", out.getvalue(), "the guest's own rows are not in its doctor")
        self.assertIn("'demo' is not running, so its desktop and its load cannot be read", out.getvalue())


class TestFromInside(_Wall):
    """`wk doctor` in a workspace, its probes run as whatever this process is to the push service: an agent session (the
    usual case), a person's own shell (whose push is served, which is not publishing), and the agent of a wk-tools
    workspace (the one repository an agent publishes, so no commit wall and its push served)."""
    kind = "local"
    CLIENT = str(REPO / "container" / "push" / "wk-push-client.py")
    PUBLISHING = {"/pulls": "422", "http_code}' -X POST -H": "410", "git ls-remote": "", "test -S /run/wk/ssh-agent.sock": "agent"}
    # who: (what a healthy pass says, the answers each of which is publishing, whether a Linux checkout is walled)
    CASES = {"agent": (("no ssh-agent socket", "a push from an agent session is refused",
                        "a write from an agent session is refused by the injector"), tuple(PUBLISHING), True),
             "person": (("a push from a person's shell is served", "a write from a person's shell reaches GitHub",
                         "a Bugzilla write is not measured from a person's shell"), (), True),
             "tools": (("a push of u/wk-tools from an agent session is served: the one repository an agent publishes",
                        "a write from an agent session is refused by the injector"),
                       ("/pulls", "http_code}' -X POST -H", "test -S /run/wk/ssh-agent.sock"), False)}

    def setUp(self):
        super().setUp()
        self.set("git config --global --get core.sshCommand", self.CLIENT)
        patch = mock.patch.object(places.LocalWorkspace, "os", lambda _: "linux")
        patch.start()
        self.addCleanup(patch.stop)

    def as_(self, who):
        """CASES in order: each starts from the healthy answers, and the wk-tools workspace comes last."""
        self.answers = dict(HEALTHY)
        self.set("git config --global --get core.sshCommand", self.CLIENT)
        me = ["python3", "/opt/wk-tools/wk", "doctor"] if who == "person" else self.me
        self.proc.chain([(os.getpid(), ENTRY, "python3", me)])
        if who == "tools":
            (self.tmp / "marker").write_text("name=demo\nsrc=/src/wk-tools\nrepo=wk-tools\n")
            self.driver = self.load("local")
            self.fake.answer(["git", "-c", "safe.directory=*", "-C", self.driver.tools_src(), "remote", "get-url", "origin"],
                             out="https://github.com/u/wk-tools.git\n")
            self.set("git ls-remote", "0123abcd\trefs/heads/main")
        return self.CASES[who]

    def results(self):
        out = io.StringIO()
        rep = doctor.Report(out)
        return wall.from_inside(str(REPO), self.driver, "demo", self.fake, rep), rep, out.getvalue()

    def test_a_healthy_workspace_passes_and_nothing_can_publish(self):
        for who in self.CASES:
            with self.subTest(who=who):
                says = self.as_(who)[0]
                publishing, rep, out = self.results()
                self.assertEqual((False, 0), (publishing, rep.missing), out)
                for phrase in says:
                    self.assertIn(phrase, out)

    def test_each_way_to_publish_is_publishing(self):
        for who in self.CASES:
            for key in self.as_(who)[1]:
                with self.subTest(who=who, key=key):
                    held = self.answers.get(key)
                    self.set(key, self.PUBLISHING[key])
                    self.assertTrue(self.results()[0])
                    self.set(key, held)

    def test_the_commit_wall_is_probed_where_bwrap_is(self):
        for who in self.CASES:
            walled = self.as_(who)[2]
            for os_name in ("linux", "macos"):
                with self.subTest(who=who, os=os_name), mock.patch.object(self.driver, "os", return_value=os_name):
                    self.assertEqual(walled and os_name == "linux", "commit-wall" in [n for n, _ in self.wall().from_inside()])

    def test_a_read_that_fails_is_the_sandbox_not_publishing(self):
        self.set("rest/version", "000")
        publishing, rep, _ = self.results()
        self.assertEqual((False, 1), (publishing, rep.missing))

    def test_the_host_half_is_not_asked(self):
        names = [n for n, _ in self.wall().from_inside()]
        for n in ("secrets-view", "isolation", "gpu", "agent-credential"):
            self.assertNotIn(n, names)


class TestTheDriversAnswer(_Wall):
    def test_egress_filtered(self):
        self.assertTrue(self.reg.load("container").egress_filtered("demo"))
        self.assertFalse(self.reg.load("remote").egress_filtered("demo"))
        self.assertFalse(self.reg.load("local").egress_filtered("demo"))
        vm = self.load("vm")
        self.assertTrue(vm.egress_filtered("demo"))
        self.fake.files[os.path.join(str(self.tmp / "vmstore"), "vm", "demo.unfiltered")] = ""
        self.assertFalse(vm.egress_filtered("demo"))

    def test_a_guests_remedy_is_this_machines_store(self):
        vm = self.load("vm")
        vm.exec = self._direct
        self.assertIn("usable litellm", vm.agent_secret_remedy("demo", "litellm"))

    def test_rootless_is_podmans_word(self):
        self.fake.answer(["podman", "info"], out="true\n")
        self.assertEqual("true", self.driver.rootless())
        self.fake.answer(["podman", "info"], rc=125)
        self.assertEqual("unknown", self.driver.rootless())


class TestTheCommand(WkTest):
    def env(self, **env):
        return clean_env(dict({"WK_MARKER": str(self.tmp / "no-marker")}, **env))

    def machines(self, **env):
        (self.tmp / "machines").mkdir(exist_ok=True)
        (self.tmp / "machines" / "pi.conf").write_text("kind=board\n")
        return dict(env, WK_MACHINES_DIR=str(self.tmp / "machines"))

    def test_it_answers_where_it_runs(self):
        for words, env, want in ((["demo", "--gpu"], {}, "workspace"), (["--all"], {}, "local"), (["pi"], self.machines(), "local")):
            args = decl.parse("doctor", DOCTOR_CMD.COMMAND, words)[1]
            self.assertEqual(want, DOCTOR_CMD.where(args, mock.Mock(env=self.env(**env))))

    def test_each_question_is_refused_where_it_does_not_belong(self):
        marker = self.tmp / "marker"
        marker.write_text("name=demo\nsrc=/src\n")
        for words, env, said in ((["pi", "--all"], self.machines(), "is not asked of a machine"),
                                 (["--gpu"], {}, "wk doctor <workspace> --gpu"),
                                 (["--all"], {"WK_NAME": "demo", "WK_PLACE": "container"}, "drop the workspace name"),
                                 (["--all"], {"WK_MARKER": str(marker)}, "'wk doctor' here checks this one")):
            with self.subTest(words=words), contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused) as cm:
                DOCTOR_CMD.main(decl.parse("doctor", DOCTOR_CMD.COMMAND, words)[1], None, env=self.env(**env))
            self.assertEqual(1, cm.exception.status)
            self.assertIn(said, err.getvalue())


def sim_registry(in_ws):
    def driver(name, env):
        t = mock.Mock(ws_name="demo")
        t.name = name
        return t
    return FakeRegistry({}, Fake(), driver, ws_place=lambda ws: "container", in_workspace=lambda: in_ws)


class TestExitCodes(unittest.TestCase):
    """0 intact | 1 broken | 3 publishing."""

    def _inside(self, missing, publishing):
        def fake_from_inside(root, driver, ws, machine, rep):
            rep.missing = missing
            return publishing
        with mock.patch.object(DOCTOR_CMD.wall, "from_inside", side_effect=fake_from_inside), \
                mock.patch.object(DOCTOR_CMD.wall, "from_host", side_effect=lambda *a, **k: fake_from_inside(*a[:5])), \
                contextlib.redirect_stderr(io.StringIO()):
            return DOCTOR_CMD.inside(sim_registry(True))

    def _workspace(self, missing, publishing=False):
        def fake_from_host(root, driver, ws, machine, rep, want_gpu=False):
            rep.missing = missing
            return publishing
        with mock.patch.object(DOCTOR_CMD.wall, "from_host", side_effect=fake_from_host), \
                contextlib.redirect_stderr(io.StringIO()):
            return DOCTOR_CMD.workspace(sim_registry(False), "demo", as_dispatched("doctor", []))

    def test_inside_and_from_the_host(self):
        self.assertEqual([0, 1, 3], [self._inside(0, False), self._inside(2, False), self._inside(2, True)])
        self.assertEqual([0, 1, 3], [self._workspace(0), self._workspace(3), self._workspace(3, True)])


if __name__ == "__main__":
    unittest.main()
