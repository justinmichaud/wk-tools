"""`wk doctor` (lib/wk/doctor.py): every check is a row (state, what, remedy)
and one renderer prints them, so each test calls the function that makes the
rows -- with a dict, a string, a fake machine or a stub of the bash bridge --
and asserts on the rows. Nothing here reaches podman, tart, ssh or a phone.

Run: python3 -m unittest tests.test_doctor -v
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import HTTPServer
from pathlib import Path

from tests.support import NO_REGISTRY, REPO, WkTest, clean_env, stub_path
from tests.test_credcheck import FakeAnthropic, FakeLiteLLM, RECORD, login

sys.path.insert(0, str(REPO / "lib"))
from wk import doctor, shell  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402

OK, MISS, UNK = doctor.OK, doctor.MISS, doctor.UNK
CMD_DOCTOR = REPO / "cmd" / "doctor"
GITCONFIG = REPO / "dotfiles" / "gitconfig"

WANT = {v: subprocess.run(["git", "config", "--file", str(GITCONFIG), "--get", "user." + v],
                          capture_output=True, text=True).stdout.strip() for v in ("name", "email")}
assert WANT["name"] and WANT["email"], "dotfiles/gitconfig no longer declares [user]"

PATHS = {"push_held": "/cfg/push-keys", "read_pat": "/store/read-github-pat", "tailscale_api": "/cfg/tailscale-api-key",
         "ntfy_topic": "/cfg/notify/ntfy-topic", "secret.claude": "/cfg/secrets/claude-token"}
INSPECT = ("podman", "machine", "inspect", "wk", "--format", "{{.State}}")


def boom(*a, **kw):
    raise AssertionError("the fleet was asked without --all: %r" % (a,))


def stub_shell(**over):
    """Every question a Doctor asks the bash library, answered with nothing unless the test says otherwise."""
    base = dict(gh_authenticated=lambda root, env=None: False,
                mirror_branches=lambda root, env=None: [],
                local_state_paths=lambda root, env=None: dict(PATHS),
                cred_settable=lambda root, env=None: [],
                cred_verdict=lambda root, name, env=None: "",
                peer_workstations=lambda root, env=None: [],
                peer_cred_verdict=lambda root, peer, name, env=None: "",
                priv_helpers=lambda root, env=None: [],
                priv_answers=lambda root, path, env=None: False,
                remote_probe=lambda root, target, env=None: "",
                remote_findings=lambda root, probe, env=None: "",
                remote_provision_stale=lambda root, target, env=None: None,
                vm_base_findings=lambda root, env=None: "",
                in_machine=lambda root, command, env=None, quiet=False: None)
    base.update(over)
    return types.SimpleNamespace(**base)


def fake_doctor(macos, sh=None, env=None, machine=None):
    e = {"HOME": "/h", "WK_STORE": "/store", "XDG_STATE_HOME": "/h/.local/state", "PATH": os.environ["PATH"],
         "WK_TARGET_REGISTRY": NO_REGISTRY, "WK_MACHINES_DIR": NO_REGISTRY}
    e.update(env or {})
    return doctor.Doctor(str(REPO), env=e, machine=machine or Fake(), macos=macos, sh=sh or stub_shell())


def text_of(rows):
    return "\n".join("%s %s -> %s" % r for r in rows)


class TestTheRenderer(unittest.TestCase):
    def test_it_counts_the_misses_and_that_is_the_exit_status(self):
        out = io.StringIO()
        rep = doctor.Report(out)
        rep.section("a section")
        rep.rows([doctor.ok("fine"), doctor.miss("gone", "fix it"), doctor.unk("unseen", "why"), doctor.miss("gone too", "fix that")])
        self.assertEqual(2, rep.missing)
        self.assertEqual(1, rep.exit_status())
        self.assertIn("\na section\n", out.getvalue())
        self.assertIn("-> fix it", out.getvalue())
        self.assertIn("-> why", out.getvalue())

    def test_unknown_is_not_missing(self):
        rep = doctor.Report(io.StringIO())
        rep.rows([doctor.ok("fine"), doctor.unk("unseen", "why")])
        self.assertEqual(0, rep.exit_status())


class TestGitConfigFindings(unittest.TestCase):
    def _blob(self, name=WANT["name"], email=WANT["email"], fsmonitor="true", manyfiles="true"):
        return "git.name=%s\ngit.email=%s\ngit.fsmonitor=%s\ngit.manyfiles=%s\n" % (name, email, fsmonitor, manyfiles)

    def test_matching_identity_and_speed_settings_are_all_ok(self):
        f = doctor.git_config_findings("label", self._blob(), "remedy", WANT)
        self.assertEqual({OK}, {r[0] for r in f}, f)
        self.assertEqual(3, len(f), f)
        self.assertTrue(all(what.startswith("label: ") for _, what, _ in f), f)

    def test_unset_identity_is_missing_and_names_the_remedy(self):
        f = doctor.git_config_findings("label", self._blob(name="", email=""), "the remedy", WANT)
        name_row = [r for r in f if "user.name" in r[1]][0]
        self.assertEqual((MISS, "label: git user.name is not set there", "the remedy"), name_row)

    def test_a_different_email_is_named_with_both_values(self):
        f = doctor.git_config_findings("label", self._blob(email="someone-else@example.com"), "the remedy", WANT)
        email_row = [r for r in f if "user.email" in r[1]][0]
        self.assertEqual(MISS, email_row[0])
        self.assertIn("someone-else@example.com", email_row[1])
        self.assertIn(WANT["email"], email_row[1])
        self.assertEqual("the remedy", email_row[2])

    def test_speed_settings_are_a_finding_of_their_own(self):
        ok_f = doctor.git_config_findings("l", self._blob(), "r", WANT)
        bad_f = doctor.git_config_findings("l", self._blob(fsmonitor=""), "r", WANT)
        self.assertIn((OK, "l: git speed settings (fsmonitor, manyFiles)", ""), ok_f)
        self.assertIn((MISS, "l: git speed settings (fsmonitor, manyFiles)", "r"), bad_f)

    def test_git_not_installed_is_unknown_not_missing(self):
        f = doctor.git_config_findings("label", "", "remedy", WANT)
        self.assertEqual([(UNK, "label: git not installed there", "")], f)


class StubGuests:
    """A vm target: `states` per guest, and one answer to the git probe."""

    def __init__(self, states, blob="", answers=True):
        self.states, self.blob, self.answers, self.asked = states, blob, answers, []

    def list(self):
        return sorted(self.states.items())

    def info(self, name):
        return self.states[name]

    def exec(self, name, argv):
        self.asked.append(name)
        return Result(0 if self.answers else 1, self.blob)


class TestVmGuestGitFindings(unittest.TestCase):
    GOOD = "git.name=%s\ngit.email=%s\ngit.fsmonitor=true\ngit.manyfiles=true\n" % (WANT["name"], WANT["email"])

    def test_a_matching_running_guest_is_all_ok(self):
        f = doctor.vm_guest_git_findings(StubGuests({"mac-rel": "running"}, self.GOOD), WANT)
        self.assertEqual({OK}, {r[0] for r in f}, f)
        self.assertTrue(all("mac-rel (tart guest)" in r[1] for r in f), f)

    def test_a_stopped_guest_is_skipped_not_asked(self):
        guests = StubGuests({"mac-rel": "stopped"}, self.GOOD)
        self.assertEqual([], doctor.vm_guest_git_findings(guests, WANT))
        self.assertEqual([], guests.asked)

    def test_a_running_guest_that_does_not_answer_is_unknown(self):
        f = doctor.vm_guest_git_findings(StubGuests({"mac-rel": "running"}, ""), WANT)
        self.assertEqual([(UNK, "mac-rel (tart guest): git config did not answer", "wk vm check mac-rel")], f)

    def test_an_unset_identity_names_a_start_as_the_remedy(self):
        blob = "git.name=\ngit.email=\ngit.fsmonitor=\ngit.manyfiles=\n"
        f = doctor.vm_guest_git_findings(StubGuests({"mac-rel": "running"}, blob), WANT)
        name_row = [r for r in f if "user.name" in r[1]][0]
        self.assertEqual(MISS, name_row[0])
        self.assertIn("wk start mac-rel", name_row[2])
        self.assertNotIn("base", name_row[2], "a guest's identity is not the base's to fix")

    def test_multiple_guests_only_the_running_one_is_checked(self):
        guests = StubGuests({"stopped-one": "stopped", "running-one": "running"}, self.GOOD)
        f = doctor.vm_guest_git_findings(guests, WANT)
        self.assertTrue(f and all("running-one" in r[1] for r in f), f)
        self.assertEqual(["running-one"], guests.asked)


class TestProbeStoreGit(unittest.TestCase):
    """The container machine's own git.* fields, from a fake machine that has git or does not."""

    def _probe(self, fake):
        return doctor.probe_store(Store({"WK_STORE": "/store", "WK_IN_VM": "1", "HOME": "/h"}), fake, ["main"], {})

    def test_git_present_and_configured_is_reported(self):
        fake = Fake()
        fake.answer(["which", "git"], 0, "/usr/bin/git\n")
        for key, value in (("user.name", WANT["name"]), ("user.email", WANT["email"]), ("core.fsmonitor", "true"), ("feature.manyFiles", "true")):
            fake.answer(["git", "config", "--get", key], 0, value + "\n")
        out = self._probe(fake)
        for line in ("git.name=" + WANT["name"], "git.email=" + WANT["email"], "git.fsmonitor=true", "git.manyfiles=true", "mirror=no"):
            self.assertIn(line, out.splitlines(), out)

    def test_git_absent_prints_none_of_the_git_fields(self):
        out = self._probe(Fake())
        self.assertNotIn("git.name=", out)
        self.assertNotIn("git.email=", out)


class TestProbeStoreMirror(unittest.TestCase):
    """The mirror is reported by the branches it carries: a workspace asks it for
    a head per declared branch (wk_fetch_refspecs), so one it lacks fails every
    fetch, and a directory that exists says nothing about that."""

    def _store(self, heads):
        d = Path(tempfile.mkdtemp(prefix="wk-test-doctor-mirror-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        mirror = d / "git" / "WebKit.git"
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=Test"]
        subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
        tree = subprocess.run(git + ["-C", str(mirror), "hash-object", "-t", "tree", "-w", "/dev/null"],
                              capture_output=True, text=True, check=True).stdout.strip()
        for head in heads:
            sha = subprocess.run(git + ["-C", str(mirror), "commit-tree", tree, "-m", head],
                                 capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(git + ["-C", str(mirror), "update-ref", "refs/heads/" + head, sha], check=True)
        return d

    def _mirror_line(self, branches, heads):
        store = Store({"WK_STORE": str(self._store(heads)), "WK_IN_VM": "1", "HOME": "/h"})
        out = doctor.probe_store(store, Local(), branches, {})
        return [l for l in out.splitlines() if l.startswith("mirror=")][0]

    def test_every_declared_branch_present_is_ok(self):
        self.assertEqual("mirror=ok", self._mirror_line(["main", "webkitglib/2.52"], ["main", "webkitglib/2.52"]))

    def test_a_declared_branch_the_mirror_lacks_is_named(self):
        self.assertEqual("mirror=gap webkitglib/2.52", self._mirror_line(["main", "webkitglib/2.52"], ["main"]))

    def test_the_probe_subverb_runs_where_the_store_is(self):
        """`cmd/doctor --probe-store` is what the macOS host runs inside the podman VM."""
        d = tempfile.mkdtemp(prefix="wk-test-doctor-nomirror-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        cp = subprocess.run([str(CMD_DOCTOR), "--probe-store"], capture_output=True, text=True, timeout=60,
                            env=clean_env({"WK_STORE": d, "WK_IN_VM": "1", "WK_MIRROR_BRANCHES": "main"}))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("mirror=no", cp.stdout.splitlines())
        self.assertIn("base=no", cp.stdout.splitlines())


FULL_STORE_BLOB = ("mirror=ok\nbase=ok\nskills=ok\nproxy=ok\npihosts=ok\nbroker=ok\n"
                   "git.name=%s\ngit.email=%s\ngit.fsmonitor=true\ngit.manyfiles=true\n" % (WANT["name"], WANT["email"]))


class TestReportStore(unittest.TestCase):
    def rows(self, blob, gitremedy):
        return doctor.report_store(blob, gitremedy, True, False, WANT)

    def test_a_remedy_reports_the_container_machine(self):
        text = text_of(self.rows(FULL_STORE_BLOB, "podman machine ssh wk -- git config ..."))
        self.assertIn("container machine", text)
        self.assertIn("git user.name", text)
        self.assertIn("git speed settings", text)

    def test_no_remedy_means_the_same_machine_already_checked(self):
        text = text_of(self.rows(FULL_STORE_BLOB, ""))
        self.assertNotIn("container machine", text)
        self.assertNotIn("git user.name", text)

    def test_a_mirror_missing_a_branch_is_a_row_naming_it_and_the_refresh(self):
        """Not `wk sync`, which fetches in a workspace against the mirror as it
        is: the branch arrives only where the mirror is refreshed."""
        rows = self.rows(FULL_STORE_BLOB.replace("mirror=ok", "mirror=gap webkitglib/2.52"), "")
        self.assertEqual((MISS, "WebKit mirror carries no webkitglib/2.52", "wk sync --mirror"), rows[0])

    def test_no_mirror_at_all_names_the_command_that_clones_one(self):
        rows = self.rows(FULL_STORE_BLOB.replace("mirror=ok", "mirror=no"), "")
        self.assertEqual((MISS, "WebKit mirror", "wk sync"), rows[0])

    def test_everything_present_is_all_ok(self):
        self.assertEqual({OK}, {r[0] for r in self.rows(FULL_STORE_BLOB, "")})


class TestTheStoreOnAMacHost(unittest.TestCase):
    """The store lives inside the podman VM, and doctor never starts it."""

    def test_a_stopped_machine_is_unknown_and_nothing_is_started(self):
        fake = Fake()
        fake.answer(INSPECT, 0, "stopped\n")
        doc = fake_doctor(True, machine=fake, sh=stub_shell(in_machine=boom))
        rows = list(doc.workspaces_store()) + list(doc.machine_local())
        self.assertIn((UNK, "podman machine 'wk' is stopped", "wk start, then re-run wk doctor for the store checks"), rows)
        self.assertTrue(any("read-github-pat (podman VM) -- not visible while the podman machine is stopped" in r[1] for r in rows), rows)
        self.assertEqual([], [r for r in rows if r[0] == MISS], rows)
        podman = [e[1] for e in fake.effects if e[0] == "run" and e[1][0] == "podman"]
        self.assertTrue(podman)
        self.assertEqual({INSPECT}, set(podman), "only the state was asked")

    def test_no_machine_at_all_is_missing_with_the_stage_that_makes_one(self):
        rows = list(fake_doctor(True, sh=stub_shell(in_machine=boom)).workspaces_store())
        self.assertEqual([(MISS, "podman machine 'wk'", "./setup --stage machine")], rows)

    def test_a_running_machine_is_asked_for_the_probe_and_its_git_identity(self):
        fake = Fake()
        fake.answer(INSPECT, 0, "running\n")
        asked = []

        def in_machine(root, command, env=None, quiet=False):
            asked.append(command)
            return FULL_STORE_BLOB.strip()
        rows = list(fake_doctor(True, machine=fake, sh=stub_shell(in_machine=in_machine)).workspaces_store())
        self.assertEqual(["WK_STORE=/var/lib/wk python3 /opt/wk-tools/cmd/doctor --probe-store"], asked)
        self.assertEqual((OK, "podman machine 'wk' running", ""), rows[0])
        self.assertTrue(any("container machine: git user.name" in r[1] for r in rows), rows)

    def test_a_machine_whose_tooling_answers_nothing_is_unknown(self):
        fake = Fake()
        fake.answer(INSPECT, 0, "running\n")
        rows = list(fake_doctor(True, machine=fake).workspaces_store())
        self.assertEqual((UNK, "store inside the VM", "/opt/wk-tools missing in the VM? run ./setup --stage sdk"), rows[-1])


class TestTheCredentialsSection(unittest.TestCase):
    """Each row is one verdict of lib/credcheck.py's rules, taken through the
    bash bridge (shell.cred_verdict) from what is in a scratch store."""

    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for handler in (FakeAnthropic, FakeLiteLLM):
            server = HTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.servers.append(server)
        cls.anthropic = "http://127.0.0.1:%d" % cls.servers[0].server_port
        cls.litellm = "http://127.0.0.1:%d" % cls.servers[1].server_port

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown()
            server.server_close()

    def setUp(self):
        FakeAnthropic.status = 200
        FakeAnthropic.policy_status = 200
        FakeAnthropic.policy = {"restrictions": {}, "compliance_taints": []}
        FakeLiteLLM.models_status = 200
        FakeLiteLLM.info_status = 403

    def rows(self, secrets, online=True):
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-doctor-cred-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel, value in secrets.items():
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n")
        env = {"WK_HOST_SECRETS": str(tmp / "secrets"), "WK_STORE": str(tmp),
               "WK_TS_AUTHKEY": str(tmp / "tailscale-authkey"), "WK_TS_API_SECRET": str(tmp / "tailscale-api-key"),
               "WK_GITHUB_API": "http://127.0.0.1:1", "WK_TAILNET_API": "http://127.0.0.1:1"}
        if online:
            env.update({"WK_ANTHROPIC_API": self.anthropic, "WK_CLAUDE_OAUTH": self.anthropic, "WK_LITELLM_API": self.litellm})
        e = clean_env(env)
        return doctor.credentials_section(shell.cred_settable(str(REPO), env=e), lambda n: shell.cred_verdict(str(REPO), n, env=e))

    LOGIN = {"agent-rw/.credentials.json": login(), "agent-rw/.claude.json": json.dumps(RECORD)}

    def test_nothing_stored_is_reported_and_is_not_a_fault(self):
        rows = self.rows({})
        text = text_of(rows)
        for name in ("github-pat", "bugzilla-api-key", "claude", "litellm", "claude-login", "tailnet", "tailnet-api"):
            self.assertIn(name, text)
        self.assertIn("nothing stored", text)
        self.assertEqual([], [r for r in rows if r[0] == MISS], text)

    def test_a_credential_that_breaks_its_rule_is_a_finding_with_the_remedy(self):
        rows = self.rows({"tailscale-authkey": "tskey-api-k1-abc"})
        row = [r for r in rows if r[1].startswith("tailnet:")][0]
        self.assertEqual(MISS, row[0], row)
        self.assertIn("administers the whole tailnet", row[1])
        self.assertIn("login.tailscale.com", row[2])

    def test_a_credential_that_could_not_be_judged_is_reported_unverified(self):
        """No network: the token is there, nothing about it was established, and the next run asks again."""
        row = [r for r in self.rows({"push-keys/github-pat": "github_pat_11ABC_x"}) if r[1].startswith("github-pat")][0]
        self.assertEqual(UNK, row[0], row)
        self.assertIn("could not reach", row[1])

    def test_an_acceptable_credential_reports_what_it_can_do(self):
        rows = self.rows({"secrets/litellm-key": "sk-litellm-abc"})
        self.assertTrue(any(r[0] == OK and "restricted to the LLM API routes" in r[1] for r in rows), text_of(rows))
        self.assertEqual([], [r for r in rows if r[0] == MISS], text_of(rows))

    def test_a_login_the_policy_allows_remote_control_has_an_ok_row_for_it(self):
        rows = self.rows(self.LOGIN)
        self.assertTrue(any(r[0] == OK and r[1].startswith("claude-login -- scopes:") for r in rows), text_of(rows))
        self.assertIn((OK, "remote control in the workspaces this login reaches: allowed by the organization's policy", ""), rows)

    def test_a_login_the_policy_denies_remote_control_is_a_red_row_with_the_remedy(self):
        """The credential is fine and every plain session works, so its own row
        stays green; the thing `wk new` will refuse on gets a row of its own,
        red, naming who can change it."""
        FakeAnthropic.policy = {"restrictions": {"allow_remote_control": {"allowed": False}}, "compliance_taints": []}
        rows = self.rows(self.LOGIN)
        row = [r for r in rows if r[1].startswith("remote control in the workspaces this login reaches")][0]
        self.assertEqual(MISS, row[0], row)
        self.assertIn("denied by the organization's policy", row[1])
        self.assertIn("an owner of the Example Org organization", row[2])

    def test_a_login_nobody_could_ask_about_says_so_on_both_rows(self):
        rows = self.rows(self.LOGIN, online=False)
        self.assertTrue(any(r[0] == UNK and r[1].startswith("claude-login: could not reach") for r in rows), text_of(rows))
        self.assertTrue(any(r[0] == UNK and r[1] == "remote control in the workspaces this login reaches: unverified" for r in rows), text_of(rows))

    def test_a_dead_login_is_red_with_the_replacement_and_no_policy_row(self):
        FakeAnthropic.status = 401
        rows = self.rows(self.LOGIN)
        row = [r for r in rows if r[1].startswith("claude-login:")][0]
        self.assertEqual(MISS, row[0], row)
        self.assertIn("Anthropic does not accept this login", row[1])
        self.assertIn("wk key set claude-login --replace", row[2])
        self.assertFalse(any("remote control in the workspaces" in r[1] for r in rows), text_of(rows))

    def test_nothing_stored_is_ever_printed(self):
        secret = "sk-ant-oat01-do-not-print-this"
        self.assertNotIn(secret, text_of(self.rows({"secrets/claude-token": secret})))


class TestTheOtherWorkstationsLogins(unittest.TestCase):
    """`wk doctor --all` asks each peer workstation for its own login's verdict, and renders it as rows."""

    VERDICTS = {
        "goodbox": "ok\tscopes: user:profile; organization: Example Org; remote control allowed by the organization's policy.\n    remote-control: allowed",
        "deniedbox": "ok\tscopes: user:profile; remote control DENIED: allow_remote_control is off.\n    remote-control: denied\n    fix: an owner of the Example Org organization turns Remote Control on",
        "emptybox": "bad\tno accessToken.",
        "newbox": "absent\tnothing stored -- wk key set claude-login",
        "farbox": "unverified\tfarbox did not answer: unreachable",
    }

    def rows(self, *peers):
        return doctor.fleet_logins_section(peers, self.VERDICTS.__getitem__)

    def test_a_peer_with_a_usable_login_is_ok_and_its_policy_is_a_row(self):
        rows = self.rows("goodbox")
        self.assertEqual(OK, rows[0][0])
        self.assertTrue(rows[0][1].startswith("goodbox: scopes: user:profile"), rows)
        self.assertEqual((OK, "remote control in the workspaces goodbox makes: allowed by the organization's policy", ""), rows[1])

    def test_a_peer_whose_organization_denies_remote_control_is_red_with_the_owner_named(self):
        rows = self.rows("deniedbox")
        self.assertEqual(MISS, rows[1][0], rows)
        self.assertEqual("remote control in the workspaces deniedbox makes: denied by the organization's policy", rows[1][1])
        self.assertIn("an owner of the Example Org", rows[1][2])
        self.assertEqual(1, len([r for r in rows if r[0] == MISS]))

    def test_a_peer_without_a_usable_login_is_red_with_the_one_command(self):
        rows = self.rows("emptybox", "newbox")
        self.assertEqual([MISS, MISS], [r[0] for r in rows])
        self.assertEqual("emptybox: no accessToken.", rows[0][1])
        self.assertEqual("newbox: nothing stored -- wk key set claude-login", rows[1][1])
        self.assertTrue(all("wk key setup" in r[2] for r in rows), rows)

    def test_a_peer_that_does_not_answer_is_unknown_never_broken(self):
        self.assertEqual([(UNK, "farbox: farbox did not answer: unreachable", "wk key check")], self.rows("farbox"))


class TestTheFleetIsWalkedOnlyWhenAsked(unittest.TestCase):
    def _run(self, macos, everything):
        sh = stub_shell(peer_workstations=boom, peer_cred_verdict=boom, remote_probe=boom, remote_findings=boom, remote_provision_stale=boom)
        return [(title, list(rows)) for title, rows in fake_doctor(macos, sh=sh).sections(everything)]

    def test_without_all_no_machine_is_asked(self):
        for macos in (True, False):
            with self.subTest(macos=macos):
                titles = [t for t, _ in self._run(macos, False)]
                self.assertIn("host tools", titles)
                self.assertNotIn("battery", titles)
                self.assertFalse(any(t.startswith("claude.ai login") for t in titles), titles)

    def test_with_all_the_fleet_is_asked(self):
        with self.assertRaises(AssertionError):
            self._run(True, True)


class TestAMachineThatDoesNotAnswer(unittest.TestCase):
    def test_a_build_machine_is_unknown_never_missing(self):
        rows = list(fake_doctor(False).build_machine("farbox"))
        self.assertEqual([(UNK, "farbox did not answer", "ssh farbox true  -- then re-run; nothing was changed")], rows)

    def test_a_build_machine_that_answers_gets_the_drivers_findings_and_its_provisioning_age(self):
        sh = stub_shell(remote_probe=lambda root, t, env=None: "family=debian\n",
                        remote_findings=lambda root, probe, env=None: "ok\tgit (/usr/bin/git)\t\nrequired\tninja\tapt install ninja\nnote\tcores: 4\t\n",
                        remote_provision_stale=lambda root, t, env=None: "remote/provision.sh or remote/deps.sh has changed since it ran")
        rows = list(fake_doctor(False, sh=sh).build_machine("box"))
        self.assertEqual([(OK, "git (/usr/bin/git)", ""), (MISS, "ninja", "apt install ninja"), (UNK, "cores: 4", ""),
                          (MISS, "provisioning on box predates its inputs: remote/provision.sh or remote/deps.sh has changed since it ran",
                           "wk remote setup box")], rows)

    def test_a_missing_remedy_names_the_setup_command(self):
        sh = stub_shell(remote_probe=lambda root, t, env=None: "family=debian\n",
                        remote_findings=lambda root, probe, env=None: "wanted\tccache\t\n")
        rows = list(fake_doctor(False, sh=sh).build_machine("box"))
        self.assertEqual((MISS, "ccache", "see 'wk remote setup box'"), rows[0])
        self.assertEqual((OK, "provisioned from this tree's remote/provision.sh + remote/deps.sh", ""), rows[1])

    def test_a_bridge_phone_that_does_not_answer_is_unknown(self):
        fake = Fake()
        fake.answer([str(REPO / "cmd" / "bridge"), "ls", "--names"], 0, "phone-a\nphone-b\n")
        fake.answer([str(REPO / "cmd" / "bridge"), "battery", "phone-b"], 0, "percent=87\nstatus=Charging\nlimit=80\ncurrent=80\n")
        rows = list(fake_doctor(False, machine=fake).battery())
        self.assertEqual([(UNK, "phone-a: did not answer", "wk bridge status phone-a"),
                          (OK, "phone-b: 87% Charging, capped at 80%", "")], rows)


class TestRootAccess(unittest.TestCase):
    def test_sudo_is_asked_quietly_through_the_environment(self):
        """The dispatcher strips --quiet into WK_QUIET for every other caller; a direct call sets it the same way."""
        fake = Fake()
        sudo = str(REPO / "cmd" / "sudo")
        fake.answer(["env", "WK_QUIET=1", sudo, "status"], 1, "a password is required, but sudo keeps a timestamp\n")
        rows = list(fake_doctor(True, machine=fake).root_access())
        self.assertEqual([(MISS, "sudo: a password is required, but sudo keeps a timestamp", "wk sudo setup")], rows)
        self.assertEqual([("run", ("env", "WK_QUIET=1", sudo, "status"))], fake.effects)


class TestPrivilegedHelpers(unittest.TestCase):
    """A helper whose sudoers rule is out-ranked is installed, executable and
    useless, so the property asked is whether it answers -- of every helper,
    on the platform each applies to."""

    def setUp(self):
        self.helpers = shell.priv_helpers(str(REPO), env=clean_env())

    def test_the_table_names_all_three(self):
        self.assertEqual(["wk-quiesce-priv", "wk-card-priv", "wk-boot-priv"], [h[0] for h in self.helpers])

    def test_only_the_card_helper_is_platform_bound(self):
        bound = {h[0]: h[1] for h in self.helpers}
        self.assertEqual({"wk-card-priv": "linux", "wk-boot-priv": "any", "wk-quiesce-priv": "any"}, bound)

    def test_the_sudoers_name_is_derived_from_the_helper(self):
        for name, path, sudoers in ((h[0], h[3], h[4]) for h in self.helpers):
            self.assertEqual("/usr/local/libexec/" + name, path)
            self.assertEqual("/etc/sudoers.d/zzz-wk-" + name[3:-5], sudoers)

    def _rows(self, answers, executable=True):
        fake = Fake()
        if executable:
            for h in self.helpers:
                fake.answer(["test", "-x", h[3]], 0)
        sh = stub_shell(priv_helpers=lambda root, env=None: self.helpers, priv_answers=lambda root, path, env=None: answers)
        return list(fake_doctor(True, machine=fake, sh=sh).privileged_helpers())

    def test_a_helper_whose_grant_does_not_answer_is_missing_with_the_rule_named(self):
        rows = self._rows(False)
        boot = [r for r in rows if r[1].startswith("wk-boot-priv")][0]
        self.assertEqual(MISS, boot[0])
        self.assertIn("still asks for a password", boot[1])
        self.assertIn("zzz-wk-boot", boot[2])
        self.assertFalse(any("wk-card-priv" in r[1] for r in rows), "the card helper is linux-only")

    def test_one_that_answers_is_ok(self):
        rows = self._rows(True)
        self.assertEqual([OK, OK], [r[0] for r in rows], rows)
        self.assertIn((OK, "wk-boot-priv (wk boot (arming the firmware, restarting a machine))", ""), rows)

    def test_one_not_installed_names_the_stage(self):
        rows = self._rows(True, executable=False)
        self.assertEqual({MISS}, {r[0] for r in rows})
        self.assertTrue(all(r[2] == "./setup --stage quiesce  (interactive sudo)" for r in rows), rows)


class ACachedCredentialIsNotAGrant(WkTest):
    """`./setup` authenticates once and holds the sudo window open, so `sudo -n
    <helper>` succeeds for anything while it runs. The rule is the evidence, so
    `sudo -l` is what wk_priv_answers reads."""

    HELPER = "/usr/local/libexec/wk-boot-priv"

    def _answers(self, listing, run_succeeds=True):
        with stub_path({"sudo": '#!/bin/sh\n'
                                'for a in "$@"; do [ "$a" = -l ] && { cat <<EOF\n'
                                + listing + '\nEOF\nexit 0; }; done\n'
                                'exit %d\n' % (0 if run_succeeds else 1)}) as binp:
            return shell.priv_answers(str(REPO), self.HELPER, env=clean_env({"PATH": "%s:%s" % (binp, os.environ["PATH"])}))

    def test_a_listing_without_the_path_is_no_grant_even_though_it_runs(self):
        listing = ("User justinmichaud may run the following commands on Tolken:\n"
                   "    (ALL) ALL\n"
                   "    (root) NOPASSWD: /usr/local/libexec/wk-quiesce-priv")
        self.assertFalse(self._answers(listing, run_succeeds=True))

    def test_a_listing_with_the_path_is_a_grant(self):
        listing = ("User justinmichaud may run the following commands on Tolken:\n"
                   "    (ALL) ALL\n"
                   "    (root) NOPASSWD: /usr/local/libexec/wk-boot-priv")
        self.assertTrue(self._answers(listing))

    def test_a_blanket_all_is_not_a_grant(self):
        """`(ALL) ALL` lets the helper run with a password, which is exactly what an unattended lane cannot do."""
        self.assertFalse(self._answers("    (ALL) ALL"))

    def test_the_path_must_match_exactly(self):
        self.assertFalse(self._answers("    (root) NOPASSWD: /usr/local/libexec/wk-boot-priv-old"))

    def test_no_listing_at_all_is_reported_as_no_grant(self):
        """Unknown must not read as working -- the safe direction is to refuse."""
        self.assertFalse(self._answers(""))


if __name__ == "__main__":
    unittest.main()
