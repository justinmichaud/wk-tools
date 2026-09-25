"""`wk doctor <workspace>` and `wk doctor` inside one (lib/wk/wall.py): every
check of the sandbox is a method returning doctor rows, driven here against a
fake machine whose workspace answers each probe from a table keyed by a
substring of the command it runs. The defaults are a healthy container with the
switch off; a test overrides one answer and asserts the check fails -- a check
that cannot fail is not a check.

Run: python3 tests/run.py --unit -k test_doctor_wall
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, bash, clean_env

sys.path.insert(0, str(REPO / "lib"))
from wk import doctor, secrets, shell, targets, wall  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402


def _load_cmd_doctor():
    """cmd/doctor as a module: a real file with no extension needs its loader spelled out."""
    path = str(REPO / "cmd" / "doctor")
    loader = importlib.machinery.SourceFileLoader("wk_cmd_doctor", path)
    spec = importlib.util.spec_from_loader("wk_cmd_doctor", loader, origin=path)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = path
    loader.exec_module(mod)
    return mod


DOCTOR_CMD = _load_cmd_doctor()

OK, MISS, NOTE = doctor.OK, doctor.MISS, doctor.NOTE
FORK = secrets.FORKS[0][1]

# Most specific first: the first key found in the command answers it.
HEALTHY = [
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
    ("ssh-add -l", "0"),
    ("https://api.github.com/ 2", "200"),
    ("api.github.com/user", "200"),
    ("/pulls", "412"),
    ("CLAUDE_CODE_OAUTH_TOKEN:+set", ""),
    ("claude auth status", '{"loggedIn": true, "authMethod": "claude.ai"}'),
    ("webkitscmpy.setup", "true"),
    ("rest/version", "200"),
    ("http_code}' -X POST -H", "412"),
    ("curl -sS -m 20 -X POST -H", '{"code": 50, "message": "a product is required"}'),
    ("gpu-probe.sh", Result(0, "renderer=NVIDIA Tegra | vendor=NVIDIA\n", "")),
    ("touch /opt/wk-tools/.wk-write-probe", "touch: cannot touch '/opt/wk-tools/.wk-write-probe': Read-only file system"),
    ("rm -f /opt/wk-tools", ""),
    ("test -s", Result(0, "", "")),
    ("test -d", Result(0, "", "")),
]


def rows_text(rows):
    return "\n".join("%s %s -> %s" % r for r in rows)


class _Wall(unittest.TestCase):
    kind = "container"

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
        self.fake.react(["env"], self._exec)
        self.fake.react(["bash", "-lc"], self._exec)
        self.fake.answer(["python3", os.path.join(str(REPO), "lib", "secretfile.py"), "present"])
        self.fake.answer(["python3", os.path.join(str(REPO), "lib", "secretfile.py"), "read"], out="stored-value")
        self.fake.answer(["python3", os.path.join(str(REPO), "lib", "credcheck.py")], out="ok\tit works")
        self.reg = targets.Registry(str(REPO), env=self.env, machine=self.fake)
        self.target = self.load(self.kind)

    def load(self, kind):
        """A guest's store is a macOS host's own, on any platform the suite runs on."""
        if kind == "vm":
            patch = mock.patch("wk.store.Store.macos_host", new_callable=mock.PropertyMock, return_value=True)
            patch.start()
            self.addCleanup(patch.stop)
        return self.reg.load(kind)

    def _exec(self, argv, fake):
        """A container's exec ends in the command; a workspace-local one quotes it after `exec`."""
        return self._answer(shlex.split(argv[-1])[-1] if argv[:2] == ["bash", "-lc"] else argv[-1])

    def _direct(self, ws, argv, tty=False, timeout=None):
        """For a driver whose own exec would reach tart or ssh."""
        return self._answer(argv[-1])

    def _answer(self, cmd):
        self.asked.append(cmd)
        for key, value in self.answers.items():
            if key in cmd:
                return value if isinstance(value, Result) else Result(0, value, "")
        return Result(0, "", "")

    def set(self, key, value):
        self.answers[key] = value

    def wall(self, push_on=0, want_gpu=False):
        return wall.Wall(str(REPO), self.target, "demo", self.fake, push_on, want_gpu)

    def check(self, name, push_on=0, want_gpu=False):
        return getattr(self.wall(push_on, want_gpu), name)()

    def misses(self, rows):
        return [r for r in rows if r[0] == MISS]

    def assertPasses(self, rows):
        self.assertEqual([], self.misses(rows), rows_text(rows))

    def assertFails(self, rows, *words, n=1):
        self.assertEqual(n, len(self.misses(rows)), rows_text(rows))
        for w in words:
            self.assertIn(w, rows_text(rows))


class TestEgress(_Wall):
    def test_github_through_the_proxy(self):
        self.assertPasses(self.check("github"))
        self.set("https://github.com/ 2", "000")
        self.assertFails(self.check("github"), "github unreachable through the proxy (got '000')")

    def test_a_host_outside_the_allowlist_is_refused(self):
        self.assertPasses(self.check("allowlist"))
        self.set("example.com", "")
        self.assertFails(self.check("allowlist"), "example.com was NOT refused")

    def test_the_lan_and_a_direct_route_are_refused(self):
        self.assertPasses(self.check("off_allowlist"))
        self.set("192.168.1.1", "200")
        self.assertFails(self.check("off_allowlist"), "reaching the LAN gateway returned '200'")
        self.set("192.168.1.1", "403")
        self.set("1.1.1.1", "200")
        self.assertFails(self.check("off_allowlist"), "direct egress succeeded")

    def test_no_route_at_all_is_no_direct_egress(self):
        self.set("1.1.1.1", "")
        self.assertPasses(self.check("off_allowlist"))

    def test_the_softwareupdate_scan_path_is_refused(self):
        self.assertPasses(self.check("softwareupdate"))
        self.set("swscan", "curl: (56) Received HTTP code 403")
        self.assertFails(self.check("softwareupdate"), "swscan/gdmf are reachable", "vm/desktop.sh")


class TestIsolation(_Wall):
    def test_loopback_only_and_no_host_path(self):
        self.assertPasses(self.check("isolation"))

    def test_another_interface_fails(self):
        self.set("/proc/net/dev", "lo eth0 ")
        self.assertFails(self.check("isolation"), "workspace has network interfaces: lo eth0")

    def test_no_answer_is_not_loopback(self):
        self.set("/proc/net/dev", "")
        self.assertFails(self.check("isolation"), "could not enumerate interfaces")

    def test_a_host_path_fails_by_name(self):
        self.set("ls -d", "/host/home")
        rows = self.check("isolation")
        self.assertEqual(len(wall.HOST_PATHS), len(self.misses(rows)), rows_text(rows))
        self.assertIn("host path visible inside the workspace: /host/home", rows_text(rows))


class TestCommitWall(_Wall):
    def test_a_commit_is_blocked_and_a_write_is_not(self):
        self.assertPasses(self.check("commit_wall"))

    def test_no_bwrap_fails(self):
        self.set("command -v bwrap", "")
        self.assertFails(self.check("commit_wall"), "no bwrap in the workspace", "refuses to start")

    def test_a_commit_that_lands_fails(self):
        self.set("mktemp -d /tmp/wk-wall", "COMMITTED\nWROTE")
        self.assertFails(self.check("commit_wall"), "commit wall did NOT block a commit")

    def test_a_wall_that_blocks_every_write_fails(self):
        self.set("mktemp -d /tmp/wk-wall", "BLOCKED\nNOWRITE")
        self.assertFails(self.check("commit_wall"), "blocks an ordinary write too")

    def test_the_paths_are_the_ones_the_session_walls(self):
        self.check("commit_wall")
        probe = [c for c in self.asked if "mktemp" in c][0]
        self.assertIn('W="%s"' % " ".join(wall.commit_wall_prefix(str(REPO), "$D")), probe)
        self.assertIn("--ro-bind-try $D/.git/ORIG_HEAD $D/.git/ORIG_HEAD", probe)


class TestNoCredentialsInside(_Wall):
    def test_a_healthy_workspace_passes(self):
        rows = self.check("no_credentials_inside")
        self.assertPasses(rows)
        for var in wall.PLACEHOLDERS:
            self.assertIn("%s in the workspace is the placeholder" % var, rows_text(rows))

    def test_private_key_material_fails_by_path(self):
        self.set("PRIVATE KEY", "/home/u/.ssh/id_fork")
        self.assertFails(self.check("no_credentials_inside"), "private key material inside the workspace", "id_fork")

    def test_an_unset_placeholder_names_what_exports_it(self):
        for var in wall.PLACEHOLDERS:
            with self.subTest(var=var):
                self.set(var, "")
                self.assertFails(self.check("no_credentials_inside"), "%s is unset in here" % var, "container/proxy/ensure-bridge.sh")
                self.set(var, "wk-injects-this")

    def test_a_real_token_fails_and_is_never_printed(self):
        for var in wall.PLACEHOLDERS:
            with self.subTest(var=var):
                self.set(var, "ghp-a-real-one")
                rows = self.check("no_credentials_inside")
                self.assertFails(rows, "%s in the workspace is not the placeholder" % var)
                self.assertNotIn("ghp-a-real-one", rows_text(rows))
                self.set(var, "wk-injects-this")

    def test_a_stored_gh_credential_fails(self):
        self.set("hosts.yml", "/home/u/.config/gh/hosts.yml")
        self.assertFails(self.check("no_credentials_inside"), "GitHub credential inside the workspace")

    def test_the_checkout_is_never_scanned(self):
        self.assertIn("/secrets /run/wk", wall.KEY_SCAN)
        self.assertNotIn("grep -rl 'PRIVATE KEY' $HOME ", wall.KEY_SCAN)


class TestTheKeyScanRunsForReal(WkTest):
    """The scan against a real tree shaped like a guest's home, whose checkout
    (with WebKit's PEM fixtures) is inside it: what it does not walk is the point."""

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


class TestSecretsView(_Wall):
    def test_a_row_this_kind_is_not_given_is_named_unreadable(self):
        rows = self.check("secrets_view")
        self.assertPasses(rows)
        self.assertIn("no credential this kind is not given is readable in here (claude)", rows_text(rows))

    def test_reading_one_from_inside_fails(self):
        self.set("test -r /secrets/claude-token", "yes")
        self.assertFails(self.check("secrets_view"), "/secrets/claude-token is readable in 'demo'", "secrets_publish_view")


class TestAgentIdentities(_Wall):
    def test_push_off_and_an_empty_agent_passes(self):
        self.assertPasses(self.check("agent_identities"))
        self.assertIn("ssh-add -l", [c for c in self.asked if "SSH_AUTH_SOCK=/run/wk/ssh-agent.sock" in c][0])

    def test_an_identity_while_push_is_off_fails(self):
        self.set("ssh-add -l", "1")
        self.assertFails(self.check("agent_identities"), "1 identity/identities reach this workspace", "wk push off")

    def test_an_unmeasured_switch_is_compared_as_off(self):
        self.set("ssh-add -l", "1")
        self.assertFails(self.check("agent_identities", push_on=None), "does not say push is on")

    def test_push_on_wants_a_key(self):
        self.set("ssh-add -l", "2")
        self.assertPasses(self.check("agent_identities", push_on=1))
        self.set("ssh-add -l", "0")
        self.assertFails(self.check("agent_identities", push_on=1), "push is ON but no identity reaches")

    def test_no_ssh_add_is_not_an_empty_agent(self):
        self.set("ssh-add -l", "MISSING")
        self.assertFails(self.check("agent_identities"), "no ssh-add in the workspace")

    def test_a_target_with_no_socket_fails(self):
        self.target = self.reg.load("remote")
        self.assertFails(self.check("agent_identities"), "names no ssh-agent socket")


class TestTheSwitchMeasuredInside(_Wall):
    def ssh(self, sock, idents):
        self.fake.answer(["ssh", "-G", "github-webkit"], out=("identityagent %s\n" % sock) if sock else "user me\n")
        self.fake.answer(["env", "SSH_AUTH_SOCK=" + sock, "ssh-add", "-l"], rc=0 if idents else 1,
                         out="".join("256 SHA256:x k%d (ED25519)\n" % i for i in range(idents)) or "The agent has no identities.\n")

    def test_an_empty_agent_holds_nothing(self):
        self.ssh("/run/wk/ssh-agent.sock", 0)
        self.assertPasses(self.check("push_here"))

    def test_no_socket_at_all_holds_nothing(self):
        self.ssh("", 0)
        self.assertPasses(self.check("push_here"))

    def test_a_key_names_the_socket_and_the_hosts_remedy(self):
        self.ssh("/run/wk/ssh-agent.sock", 2)
        self.assertFails(self.check("push_here"), "2 deploy key(s) reach this workspace through /run/wk/ssh-agent.sock", "wk push off")


class TestGitHubRead(_Wall):
    def test_an_authenticated_read_passes(self):
        self.assertIn("a read is authenticated (HTTP 200)", rows_text(self.check("github_read")))

    def test_no_standing_token_is_a_note_not_a_failure(self):
        self.set("api.github.com/user", "401")
        rows = self.check("github_read")
        self.assertPasses(rows)
        self.assertEqual(NOTE, rows[0][0])
        self.assertIn("60 requests an hour", rows[0][1])

    def test_a_refused_standing_token_is_a_note_naming_both_remedies(self):
        self.set("https://api.github.com/ 2", "401")
        self.set("api.github.com/user", "401")
        rows = self.check("github_read")
        self.assertPasses(rows)
        for w in ("every API read in here is refused", "wk key check github-pat", "wk start demo", "wk key set github-pat --replace"):
            self.assertIn(w, rows[0][1])
        self.assertNotIn("60 requests an hour", rows[0][1])

    def test_any_other_answer_fails(self):
        self.set("api.github.com/user", "000")
        self.assertFails(self.check("github_read"), "rather than 200 or 401")
        self.set("https://api.github.com/ 2", "000")
        self.assertFails(self.check("github_read"), "the injector is not in the path")


class TestGitHubWrite(_Wall):
    def test_off_wants_the_injectors_412(self):
        self.assertPasses(self.check("github_write"))
        self.assertIn("-X POST -d '{}' https://api.github.com/repos/%s/pulls" % FORK, [c for c in self.asked if "/pulls" in c][0])
        self.set("/pulls", "422")
        self.assertFails(self.check("github_write"), "where the host does not say push is on", "wk push off")

    def test_on_wants_githubs_422(self):
        self.set("/pulls", "422")
        self.assertPasses(self.check("github_write", push_on=1))

    def test_on_and_refused_names_each_cause(self):
        for code, words in (("403", ("Pull requests: write",)),
                            ("401", ("no write token", "wk key set github-pat --replace")),
                            ("", ("answered 'nothing' rather than 422",))):
            with self.subTest(code=code):
                self.set("/pulls", code)
                self.assertFails(self.check("github_write", push_on=1), *words)


class TestAgentCredential(_Wall):
    TOKEN = '{"loggedIn": true, "authMethod": "oauth_token"}'

    def test_a_container_with_the_login_passes(self):
        rows = self.check("agent_credential")
        self.assertPasses(rows)
        self.assertIn("from the claude-login credential", rows_text(rows))

    def test_the_token_beside_the_login_wins_and_fails(self):
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "set")
        self.set("claude auth status", self.TOKEN)
        self.assertFails(self.check("agent_credential"), "the token wins", n=2)

    def test_a_build_box_is_given_the_token(self):
        self.target = self.reg.load("remote")
        self.target.exec = self._direct
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "set")
        self.set("claude auth status", self.TOKEN)
        self.assertPasses(self.check("agent_credential"))
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "")
        self.assertFails(self.check("agent_credential"), "no $CLAUDE_CODE_OAUTH_TOKEN", "usable claude")

    def test_not_logged_in_names_the_targets_remedy(self):
        self.set("claude auth status", '{"loggedIn": false}')
        self.assertFails(self.check("agent_credential"), "not logged in", "usable claude-login")

    def test_an_unreadable_answer_is_quoted(self):
        self.set("claude auth status", "")
        self.assertFails(self.check("agent_credential"), "It answered (first 80 bytes): b''", "claude --version")

    def test_no_cli_is_missing_not_unreadable(self):
        self.set("claude auth status", "wk-no-claude-cli")
        rows = self.check("agent_credential")
        self.assertFails(rows, "no 'claude' on $PATH")
        self.assertNotIn("It answered", rows_text(rows))

    def test_the_control_sequences_a_tty_wraps_the_json_in_are_read_through(self):
        raw = (b'\x1b7\x1b[r\x1b8\x1b[?25h{\r\r\n\x1b[3G"loggedIn":\x1b[15Gtrue,\r\r\n\x1b[3G"authMethod":\x1b[17G"claude.ai"\r\r\n}\r\r\n'
               b'\x1b[?25h\x1b[?1006l\x1b(B\x0f\x1b[>4m\x1b[<u\x1b[?1004l\x1b7\x1b[r\x1b8\x1b[?25h\n')
        self.assertEqual("True claude.ai", wall.claude_status(raw.decode("utf-8", "surrogateescape")))


class TestGitWebkitSetup(_Wall):
    def test_the_marker_true_passes_and_is_asked_of_the_checkout(self):
        self.assertPasses(self.check("gitwebkit_setup"))
        self.assertIn("git -C /src/WebKit config --get webkitscmpy.setup", self.asked)

    def test_anything_else_fails_with_the_converging_command(self):
        for answer in ("", "false"):
            with self.subTest(answer=answer):
                self.set("webkitscmpy.setup", answer)
                self.assertFails(self.check("gitwebkit_setup"), "has not completed", "wk sync demo --fix")


class TestBugzilla(_Wall):
    def test_the_read_goes_through_the_injector(self):
        self.assertPasses(self.check("bugzilla_read"))
        self.set("rest/version", "000")
        self.assertFails(self.check("bugzilla_read"), "not in the path for it")

    def test_off_wants_the_injectors_412(self):
        self.assertPasses(self.check("bugzilla_write"))
        self.set("http_code}' -X POST -H", "410")
        self.assertFails(self.check("bugzilla_write"), "Bugzilla key still on the machine", "wk push off")

    def test_on_reads_bugzillas_own_code(self):
        self.assertIn("error 50", rows_text(self.check("bugzilla_write", push_on=1)))
        for body, words in (('{"code": 410}', ("no Bugzilla API key", "wk key set bugzilla-api-key")),
                            ('{"code": 306}', ("does not know it", "--replace")),
                            ("<html>", ("nothing Bugzilla-shaped",))):
            with self.subTest(body=body):
                self.set("curl -sS -m 20 -X POST -H", body)
                self.assertFails(self.check("bugzilla_write", push_on=1), *words)


class TestGpu(_Wall):
    def test_hardware_passes_and_the_probe_output_is_kept(self):
        rows = self.check("gpu", want_gpu=True)
        self.assertPasses(rows)
        self.assertIn((NOTE, "renderer=NVIDIA Tegra | vendor=NVIDIA", ""), rows)

    def test_software_rendering_is_a_note_unless_asked_for(self):
        self.set("gpu-probe.sh", Result(1, "renderer=llvmpipe\n", ""))
        self.assertPasses(self.check("gpu"))
        self.assertIn("llvmpipe", rows_text(self.check("gpu")))
        self.assertFails(self.check("gpu", want_gpu=True), "only software rendering")

    def test_no_egl_is_a_note_unless_asked_for(self):
        self.set("gpu-probe.sh", Result(2, "", ""))
        self.assertPasses(self.check("gpu"))
        self.assertFails(self.check("gpu", want_gpu=True), "no usable EGL inside the workspace (probe exit 2)")

    def test_an_arch_with_no_gpu(self):
        self.fake.files[os.path.join(self.target.store.ws_dir("demo"), "arch")] = "armhf\n"
        self.assertPasses(self.check("gpu"))
        self.assertFails(self.check("gpu", want_gpu=True), "--gpu on an armhf workspace")

    def test_a_guests_gpu_is_the_benchmarks_to_measure(self):
        with mock.patch.object(self.target, "os", return_value="macos"):
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
    def test_the_wall_clock_is_the_slowest_and_the_order_is_kept(self):
        def slow(n):
            return lambda: (time.sleep(0.3), [doctor.ok(n)])[1]
        started = time.monotonic()
        out = wall.run_at_once([(str(i), slow(str(i))) for i in range(6)])
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual([str(i) for i in range(6)], [name for name, _ in out])

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
        self.fake.files[os.path.join(self.target.store.ws_dir("demo"), "home", targets.READY_MARKER)] = ""
        self.fake.answer([str(REPO / "wk"), "push", "status"], rc=1)
        self.fake.answer(["podman", "info"], out="true\n")
        self.fake.answer(["systemctl", "--user"])

    def report(self):
        out = io.StringIO()
        rep = doctor.Report(out)
        wall.from_host(str(REPO), self.target, "demo", self.fake, rep)
        return rep, out.getvalue()

    def test_a_healthy_container_passes_every_check(self):
        rep, out = self.report()
        self.assertEqual(0, rep.missing, out)
        for w in ("workspace running", "the host says push is OFF", "account scope", "commit wall", "podman is rootless",
                  "no network interface but loopback", "github reachable"):
            self.assertIn(w, out)

    def test_the_switch_is_read_once_and_only_a_measured_off_is_off(self):
        for rc, said in ((0, "push is ON"), (4, "push is OFF"), (3, "could not measure the switch ('wk push status' exited 3)")):
            with self.subTest(rc=rc):
                self.fake.answer([str(REPO / "wk"), "push", "status"], rc=rc)
                self.assertIn(said, wall.push_switch(str(REPO), self.fake)[1])
        self.assertEqual(None, wall.push_switch(str(REPO), self.fake)[0])

    def test_the_switch_is_asked_once_and_the_write_probe_after_the_parallel_pass(self):
        self.report()
        runs = [e[1] for e in self.fake.effects if e[0] == "run"]
        self.assertEqual(1, runs.count((str(REPO / "wk"), "push", "status")))
        self.assertEqual("rm -f /opt/wk-tools/.wk-write-probe" in self.asked, False)
        self.assertEqual("touch /opt/wk-tools/.wk-write-probe 2>&1", self.asked[-1])

    def test_a_workspace_without_the_login_gets_the_targets_remedy(self):
        self.set("test -s", Result(1, "", ""))
        _, out = self.report()
        self.assertIn("remote control refuses to start without one", out)
        self.assertIn("usable claude-login", out)
        self.assertIn('test -s "$CLAUDE_SECURESTORAGE_CONFIG_DIR/.credentials.json"', self.asked)

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
        self.target = self.reg.load("remote")
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.report()
        self.assertIn("a remote target has none", err.getvalue())

    def test_the_egress_checks_are_gated_on_the_driver(self):
        names = [n for n, _ in self.wall().from_host()]
        self.assertIn("egress-github", names)
        self.assertNotIn("egress-softwareupdate", names)
        with mock.patch.object(self.target, "egress_filtered", return_value=False):
            names = [n for n, _ in self.wall().from_host()]
        self.assertFalse([n for n in names if n.startswith("egress-")], names)

    def test_the_credential_checks_are_asked_of_every_kind_and_isolation_of_a_container_only(self):
        self.assertIn("isolation", [n for n, _ in self.wall().from_host()])
        self.target = self.load("vm")
        names = [n for n, _ in self.wall().from_host()]
        self.assertNotIn("isolation", names)
        self.assertNotIn("commit-wall", names)
        for n in ("no-credentials-inside", "agent-identities", "github-read", "github-write", "egress-softwareupdate"):
            self.assertIn(n, names)


class TestAGuest(_Wall):
    kind = "vm"

    def test_an_unfiltered_guest_is_a_failure(self):
        self.target.info = lambda ws: "running"
        self.target.exec = self._direct
        self.fake.answer([str(REPO / "wk"), "push", "status"], rc=1)
        self.fake.files[os.path.join(self.target.vm_dir(), "demo.unfiltered")] = ""
        out = io.StringIO()
        rep = doctor.Report(out)
        wall.from_host(str(REPO), self.target, "demo", self.fake, rep)
        self.assertIn("booted with WK_VM_UNFILTERED", out.getvalue())
        self.assertIn("wk stop demo && wk start demo", out.getvalue())
        self.assertNotIn("github reachable", out.getvalue())
        self.assertIn("no golden base VM 'wk-base'", out.getvalue(), "the guest's own rows are not in its doctor")
        self.assertIn("'demo' is not running, so its desktop and its load cannot be read", out.getvalue())


class TestFromInside(_Wall):
    kind = "local"

    def setUp(self):
        super().setUp()
        self.fake.answer(["ssh", "-G", "github-webkit"], out="user me\n")

    def results(self):
        out = io.StringIO()
        rep = doctor.Report(out)
        return wall.from_inside(str(REPO), self.target, "demo", self.fake, rep), rep, out.getvalue()

    def test_a_healthy_workspace_passes_and_nothing_can_publish(self):
        publishing, rep, out = self.results()
        self.assertFalse(publishing)
        self.assertEqual(0, rep.missing, out)
        self.assertIn("the agent holds nothing", out)

    def test_each_way_to_publish_is_publishing(self):
        for key, value in (("/pulls", "422"), ("http_code}' -X POST -H", "410")):
            with self.subTest(key=key):
                self.set(key, value)
                self.assertTrue(self.results()[0])
                self.answers = dict(HEALTHY)
        self.fake.answer(["ssh", "-G", "github-webkit"], out="identityagent /s\n")
        self.fake.answer(["env", "SSH_AUTH_SOCK=/s", "ssh-add", "-l"], out="256 SHA256:x k (ED25519)\n")
        self.assertTrue(self.results()[0])

    def test_a_read_that_fails_is_the_sandbox_not_publishing(self):
        self.set("rest/version", "000")
        publishing, rep, _ = self.results()
        self.assertFalse(publishing)
        self.assertEqual(1, rep.missing)

    def test_the_host_half_is_not_asked(self):
        names = [n for n, _ in self.wall().from_inside()]
        for n in ("secrets-view", "agent-identities", "isolation", "gpu", "agent-credential"):
            self.assertNotIn(n, names)

    def test_the_commit_wall_is_probed_where_bwrap_is(self):
        with mock.patch.object(self.target, "os", return_value="linux"):
            self.assertIn("commit-wall", [n for n, _ in self.wall().from_inside()])
        with mock.patch.object(self.target, "os", return_value="macos"):
            self.assertNotIn("commit-wall", [n for n, _ in self.wall().from_inside()])


class TestTheDriversAnswer(_Wall):
    def test_egress_filtered(self):
        self.assertTrue(self.reg.load("container").egress_filtered("demo"))
        self.assertFalse(self.reg.load("remote").egress_filtered("demo"))
        self.assertFalse(self.reg.load("local").egress_filtered("demo"))
        vm = self.load("vm")
        self.assertTrue(vm.egress_filtered("demo"))
        self.fake.files[os.path.join(str(self.tmp / "vmstore"), "vm", "demo.unfiltered")] = ""
        self.assertFalse(vm.egress_filtered("demo"))

    def test_agent_sock(self):
        self.assertEqual("/run/wk/ssh-agent.sock", self.reg.load("container").agent_sock())
        self.assertEqual("/Users/admin/.wk-ssh-agent.sock", self.load("vm").agent_sock())
        self.assertIsNone(self.reg.load("remote").agent_sock())

    def test_agent_secret_present_asks_where_each_kind_of_row_lives(self):
        self.assertTrue(self.target.agent_secret_present("demo", "claude-login"))
        self.target.agent_secret_present("demo", "litellm")
        self.assertIn('test -s "$HOME/.wk-litellm-key"', self.asked)
        self.set("test -s", Result(1, "", ""))
        self.assertFalse(self.target.agent_secret_present("demo", "claude-login"))

    def test_a_guest_without_the_share_is_told_to_boot_with_it(self):
        vm = self.load("vm")
        vm.exec = self._direct
        self.assertIn("usable claude-login", vm.agent_secret_remedy("demo", "claude-login"))
        self.set("test -d", Result(1, "", ""))
        self.assertIn("the agent-rw share is not mounted in 'demo'", vm.agent_secret_remedy("demo", "claude-login"))
        self.assertIn("usable litellm", vm.agent_secret_remedy("demo", "litellm"))

    def test_rootless_is_podmans_word(self):
        self.fake.answer(["podman", "info"], out="true\n")
        self.assertEqual("true", self.target.rootless())
        self.fake.answer(["podman", "info"], rc=125)
        self.assertEqual("unknown", self.target.rootless())


class TestTheCommand(WkTest):
    DOCTOR = REPO / "cmd" / "doctor"

    def run_doctor(self, *args, env=None):
        return subprocess.run([sys.executable, str(self.DOCTOR), *args], capture_output=True, text=True,
                              env=clean_env(dict({"WK_MARKER": str(self.tmp / "no-marker")}, **(env or {}))))

    def test_it_answers_where_it_runs(self):
        self.assertEqual("workspace", self.run_doctor("--where", "demo", "--gpu").stdout.strip())
        self.assertEqual("local", self.run_doctor("--where", "--all").stdout.strip())

    def test_gpu_is_a_workspaces_question(self):
        cp = self.run_doctor("--gpu")
        self.assertEqual(1, cp.returncode, cp.stderr)
        self.assertIn("wk doctor <workspace> --gpu", cp.stderr)

    def test_all_is_this_machines_question(self):
        cp = self.run_doctor("--all", env={"WK_NAME": "demo", "WK_TARGET": "container"})
        self.assertEqual(1, cp.returncode, cp.stderr)
        self.assertIn("drop the workspace name", cp.stderr)

    def test_inside_a_workspace_it_takes_no_option(self):
        marker = self.tmp / "marker"
        marker.write_text("name=demo\nsrc=/src\n")
        cp = self.run_doctor("--all", env={"WK_MARKER": str(marker)})
        self.assertEqual(1, cp.returncode, cp.stderr)
        self.assertIn("'wk doctor' here checks this one", cp.stderr)

    def test_verify_is_a_tombstone_naming_doctor(self):
        cp = subprocess.run([str(REPO / "wk"), "verify", "demo"], capture_output=True, text=True, env=clean_env())
        self.assertEqual(1, cp.returncode)
        self.assertIn("'wk verify' is merged into doctor: wk doctor <workspace>", cp.stderr)
        self.assertFalse((REPO / "cmd" / "verify").exists())


class _SimReg:
    """Just enough of a Registry for `inside`/`workspace`'s own exit-code
    translation: a report's `.missing` and (inside) whether it is publishing
    are what decide 0 | 1 | 3, not what a target actually measures."""

    def __init__(self, in_ws):
        self._in_ws = in_ws
        self.machine = Fake()

    def in_workspace(self):
        return self._in_ws

    def load(self, name):
        t = mock.Mock()
        t.name, t.ws_name = name, "demo"
        return t

    def ws_target(self, name):
        return "container"


class TestExitCodes(unittest.TestCase):
    """`inside`/`workspace` (cmd/doctor) turn a Report -- and, inside, whether
    an agent could publish -- into 0 intact | 1 broken | 3 publishing; nothing
    here drives a real check."""

    def _inside(self, missing, publishing):
        def fake_from_inside(root, target, ws, machine, rep):
            rep.missing = missing
            return publishing
        with mock.patch.object(DOCTOR_CMD.wall, "from_inside", side_effect=fake_from_inside), \
                contextlib.redirect_stderr(io.StringIO()):
            return DOCTOR_CMD.inside(_SimReg(True))

    def _workspace(self, missing):
        def fake_from_host(root, target, ws, machine, rep, want_gpu=False):
            rep.missing = missing
        with mock.patch.object(DOCTOR_CMD.wall, "from_host", side_effect=fake_from_host), \
                contextlib.redirect_stderr(io.StringIO()):
            return DOCTOR_CMD.workspace(_SimReg(False), "demo", [])

    def test_inside_intact_is_0(self):
        self.assertEqual(0, self._inside(0, False))

    def test_inside_broken_is_1(self):
        self.assertEqual(1, self._inside(2, False))

    def test_inside_publishing_is_3_even_with_other_checks_missing_too(self):
        self.assertEqual(3, self._inside(2, True))

    def test_workspace_intact_is_0(self):
        self.assertEqual(0, self._workspace(0))

    def test_workspace_broken_is_1(self):
        self.assertEqual(1, self._workspace(3))


if __name__ == "__main__":
    unittest.main()
