"""`wk doctor` (lib/wk/doctor.py): every check is a row (state, what, remedy)"""
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
from unittest import mock

from tests.support import NO_REGISTRY, REPO, WkTest, clean_env
from tests.test_credcheck import FakeAnthropic, FakeLiteLLM

sys.path.insert(0, str(REPO / "lib"))
from wk import doctor  # noqa: E402
from wk.key.cli import Key  # noqa: E402
from wk.machine import HAVE, Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402

OK, MISS, UNK = doctor.OK, doctor.MISS, doctor.UNK
CMD_DOCTOR = REPO / "cmd" / "doctor"
GITCONFIG = REPO / "dotfiles" / "gitconfig"

WANT = {v: subprocess.run(["git", "config", "--file", str(GITCONFIG), "--get", "user." + v],
                          capture_output=True, text=True).stdout.strip() for v in ("name", "email")}
assert WANT["name"] and WANT["email"], "dotfiles/gitconfig no longer declares [user]"

INSPECT = ("podman", "machine", "inspect", "wk")


def machine_in(state):
    return json.dumps([{"Name": "wk", "State": state}])


def boom(*a, **kw):
    raise AssertionError("the fleet was asked without --all: %r" % (a,))


def stub_shell(**over):
    """Every question a Doctor asks outside Python (doctor.Host), answered with nothing unless the test says otherwise."""
    base = dict(gh_authenticated=lambda root, machine: False,
                priv_helpers=lambda root, env=None: [],
                priv_answers=lambda root, path, machine: False)
    base.update(over)
    return types.SimpleNamespace(**base)


def stub_keys():
    """`wk key`'s answers (lib/wk/key/): nothing stored here."""
    return types.SimpleNamespace(settable=lambda: [], stored_verdict=lambda n: "")


def stub_mc(**over):
    """lib/wk/machine_cmd/deps.py's build-machine questions, answered with nothing unless the test says otherwise."""
    base = dict(probe=lambda driver, root: "", findings=lambda root, probe, env=None, here=None: [],
                stale=lambda driver, root: None)
    base.update(over)
    return types.SimpleNamespace(**base)


def fake_doctor(macos, sh=None, env=None, machine=None, mc=None, keys=None):
    e = {"HOME": "/h", "WK_STORE": "/store", "XDG_STATE_HOME": "/h/.local/state", "PATH": os.environ["PATH"],
         "WK_MACHINES_DIR": NO_REGISTRY}
    e.update(env or {})
    return doctor.Doctor(str(REPO), env=e, machine=machine or Fake(), macos=macos, sh=sh or stub_shell(), mc=mc or stub_mc(),
                         keys=keys or stub_keys())


def build_doctor(**over):
    """A Doctor whose registry names `farbox`, `box` and `old` as build machines."""
    d = tempfile.mkdtemp(prefix="wk-test-doctor-machines-")
    for n in ("farbox", "box", "old", "fresh"):
        with open(os.path.join(d, n + ".conf"), "w") as f:
            f.write("kind=build\ndriver=remote\n")
    return fake_doctor(False, env={"WK_MACHINES_DIR": d, "XDG_STATE_HOME": os.path.join(d, "state")}, mc=stub_mc(**over))


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


class TestHostToolsZed(unittest.TestCase):
    """The zed row reads `places.zed_cli`, the answer `cmd/zed` reads too."""

    def _zed_row(self, fake):
        d = fake_doctor(True, machine=fake)
        return next(r for r in d.host_tools() if r[1] == "zed")

    def test_zed_on_path_is_ok(self):
        fake = Fake()
        fake.answer(HAVE + ("zed",))
        self.assertEqual(self._zed_row(fake)[0], OK)

    def test_a_drag_installed_bundle_with_no_path_symlink_is_ok(self):
        fake = Fake()
        fake.answer(["test", "-x", "/Applications/Zed.app/Contents/MacOS/cli"], rc=0)
        self.assertEqual(self._zed_row(fake)[0], OK)

    def test_the_bundle_directory_alone_with_no_executable_cli_is_missing(self):
        fake = Fake()
        fake.dirs.add("/Applications/Zed.app")
        fake.answer(["test", "-x", "/Applications/Zed.app/Contents/MacOS/cli"], rc=1)
        self.assertEqual(self._zed_row(fake)[0], MISS)


class TestHostToolsGitLfs(unittest.TestCase):
    """git-lfs is on every host: its absence is a miss naming the setup stage, on either OS."""

    def _row(self, macos, fake):
        return next(r for r in fake_doctor(macos, machine=fake).host_tools() if r[1] == "git-lfs")

    def test_absent_is_a_miss_with_the_remedy(self):
        for macos in (True, False):
            fake = Fake()
            fake.answer(HAVE + ("git-lfs",), rc=1)
            fake.answer(["test", "-x", "/h/.local/bin/git-lfs"], rc=1)
            self.assertEqual((MISS, "git-lfs", "./setup --stage tools"), self._row(macos, fake))

    def test_the_one_tools_sh_installs_off_path_is_ok(self):
        fake = Fake()
        fake.answer(HAVE + ("git-lfs",), rc=1)
        fake.answer(["test", "-x", "/h/.local/bin/git-lfs"])
        self.assertEqual(OK, self._row(True, fake)[0])

    def test_present_is_ok(self):
        fake = Fake()
        fake.answer(HAVE + ("git-lfs",))
        self.assertEqual(OK, self._row(False, fake)[0])

    def test_the_shared_gitconfig_runs_the_filter_and_a_place_without_git_lfs_still_adds(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-lfs-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "gitconfig"), "w") as f:
            f.write("[include]\n\tpath = %s\n" % (REPO / "dotfiles" / "gitconfig"))
        bare = os.path.join(tmp, "bin")
        os.mkdir(bare)
        os.symlink(shutil.which("git"), os.path.join(bare, "git"))
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.path.join(tmp, "gitconfig"), GIT_CONFIG_NOSYSTEM="1", PATH=bare + ":/bin")
        git = lambda *a: subprocess.run(["git", "-C", tmp + "/r"] + list(a), env=env, capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", tmp + "/r"], env=env, check=True)
        self.assertEqual("git-lfs clean -- %f", git("config", "filter.lfs.clean").stdout.strip())
        Path(tmp, "r", ".gitattributes").write_text("* filter=lfs\n")
        Path(tmp, "r", "a").write_text("a\n")
        self.assertEqual(0, git("add", "a").returncode)


PEERS = json.dumps({"Peer": {"a": {"DNSName": "pi-rescue.tail.ts.net.", "TailscaleIPs": ["100.1.1.1"], "Online": True},
                             "b": {"DNSName": "pi-bench.tail.ts.net.", "TailscaleIPs": ["100.1.1.2"], "Online": False}}})


def probed(mode="host", armed="", **over):
    f = dict(role="bench-device", probeable="yes", mode=mode, bridge="", armed=armed, armed_by="me",
             armed_at="2099-01-01T00:00:00Z", armed_boot="", boot_id="", media="sd")
    f.update(over)
    return f


class TestDeviceRows(unittest.TestCase):
    """`wk doctor <machine>`: rows from the tailnet, the fleet probe and a board's sysfs, asked through a fake."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wk-test-doctor-device-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for name, text in (("pi", "kind=board\nssh=pi-rescue\nbench_ssh=pi-bench\n"),
                           ("box", "kind=build\ndriver=remote\n")):
            Path(self.dir, name + ".conf").write_text(text)
        self.env = {"WK_MACHINES_DIR": self.dir, "HOME": self.dir, "PATH": os.environ["PATH"]}
        self.fake = Fake()
        self.fake.answer(["tailscale", "status", "--json"], out=PEERS)
        self.fake.answer(["ssh"], out="performance\n51234\n")

    def rows(self, name, fields=None, answers=None):
        return list(doctor.device(str(REPO), name, self.env, self.fake, probe=lambda *a: fields, answers=answers))

    def row(self, rows, word):
        return next(r for r in rows if word in r[1])

    def test_each_tailnet_name_is_a_row_and_an_offline_one_is_missing(self):
        rows = self.rows("pi", probed())
        self.assertEqual(OK, self.row(rows, "pi-rescue on the tailnet")[0])
        self.assertEqual(MISS, self.row(rows, "pi-bench on the tailnet")[0])

    def test_a_board_answering_in_host_mode_reports_its_governor_and_temperature(self):
        rows = self.rows("pi", probed())
        self.assertEqual(OK, self.row(rows, "answers in host mode")[0])
        self.assertEqual(OK, self.row(rows, "not armed")[0])
        self.assertIn("performance", self.row(rows, "cpu governor on pi-rescue")[1])
        self.assertIn("51C", self.row(rows, "temperature on pi-rescue")[1])

    def test_bench_mode_reads_the_bench_name(self):
        self.assertTrue(any("on pi-bench" in r[1] for r in self.rows("pi", probed(mode="bench"))))

    def test_unreachable_is_missing_and_asks_nothing_further(self):
        rows = self.rows("pi", probed(mode=""))
        self.assertEqual(MISS, self.row(rows, "unreachable")[0])
        self.assertFalse(any("governor" in r[1] for r in rows))

    def test_no_answer_within_the_ceiling_is_unknown(self):
        self.assertEqual(UNK, self.row(self.rows("pi", None), "did not answer")[0])

    def test_a_current_arm_is_a_note_and_a_stale_one_is_missing(self):
        self.assertEqual(doctor.NOTE, self.row(self.rows("pi", probed(armed="bench")), "armed for bench")[0])
        stale = probed(armed="bench", armed_boot="b1", boot_id="b2")
        self.assertEqual(MISS, self.row(self.rows("pi", stale), "armed for bench")[0])

    def test_a_name_the_tailnet_lacks_and_a_failed_probe_are_unknown(self):
        self.fake.answer(["tailscale", "status", "--json"], out="{}")
        rows = self.rows("pi", {"error": "no driver"})
        self.assertEqual(UNK, self.row(rows, "pi-rescue is not a tailnet node")[0])
        self.assertEqual(UNK, self.row(rows, "failed: no driver")[0])

    def test_a_build_machine_is_asked_whether_it_answers(self):
        self.assertEqual(OK, self.row(self.rows("box", answers=lambda n, c: (True, "")), "box answers")[0])
        self.assertEqual(MISS, self.row(self.rows("box", answers=lambda n, c: (False, "timed out")), "timed out")[0])


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
    """A vm place: `states` per guest, and one answer to the git probe."""

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

    def test_a_running_guest_that_does_not_answer_is_unknown(self):
        f = doctor.vm_guest_git_findings(StubGuests({"mac-rel": "running"}, ""), WANT)
        self.assertEqual([(UNK, "mac-rel (tart guest): git config did not answer", "wk doctor mac-rel")], f)

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
        fake.answer(HAVE + ("git",))
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
    """The mirror is reported by the branches it carries: one it lacks fails every fetch."""

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
        fake.answer(INSPECT, 0, machine_in("stopped"))
        doc = fake_doctor(True, machine=fake, sh=stub_shell())
        rows = list(doc.workspaces_store()) + list(doc.machine_local())
        self.assertIn((UNK, "podman machine 'wk' is stopped", "wk start, then re-run wk doctor for the store checks"), rows)
        self.assertTrue(any("read-github-pat (podman VM) -- not visible while the podman machine is stopped" in r[1] for r in rows), rows)
        self.assertEqual([], [r for r in rows if r[0] == MISS], rows)
        podman = [e[1] for e in fake.effects if e[0] == "run" and e[1][0] == "podman"]
        self.assertTrue(podman)
        self.assertEqual({INSPECT}, set(podman), "only the state was asked")

    def test_no_machine_at_all_is_missing_with_the_stage_that_makes_one(self):
        rows = list(fake_doctor(True, sh=stub_shell()).workspaces_store())
        self.assertEqual([(MISS, "podman machine 'wk'", "./setup --stage machine")], rows)

    def test_a_running_machine_is_asked_for_the_probe_and_its_git_identity(self):
        fake = Fake()
        fake.answer(INSPECT, 0, machine_in("running"))
        asked = []

        fake.react(("podman", "machine", "ssh", "wk", "--"), lambda a, f: asked.append(a[5]) or Result(0, FULL_STORE_BLOB))
        rows = list(fake_doctor(True, machine=fake).workspaces_store())
        self.assertEqual(["WK_STORE=/var/lib/wk python3 /opt/wk-tools/cmd/doctor --probe-store"], asked)
        self.assertEqual((OK, "podman machine 'wk' running", ""), rows[0])
        self.assertTrue(any("container machine: git user.name" in r[1] for r in rows), rows)

    def test_a_machine_whose_tooling_answers_nothing_is_unknown(self):
        fake = Fake()
        fake.answer(INSPECT, 0, machine_in("running"))
        rows = list(fake_doctor(True, machine=fake).workspaces_store())
        self.assertEqual((UNK, "store inside the VM", "/opt/wk-tools missing in the VM? run ./setup --stage sdk"), rows[-1])


class TestTheCredentialsSection(unittest.TestCase):
    """Each row is one verdict of lib/credcheck.py's rules, read through Key.stored_verdict from a scratch store."""

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
            env.update({"WK_ANTHROPIC_API": self.anthropic, "WK_LITELLM_API": self.litellm})
        e = clean_env(env)
        with mock.patch.dict(os.environ, e, clear=True):   # Local's processes inherit this one's environment
            k = Key(str(REPO), env=e, machine=Local())
            return doctor.credentials_section(k.settable(), k.stored_verdict)

    def test_nothing_stored_is_reported_and_is_not_a_fault(self):
        rows = self.rows({})
        text = text_of(rows)
        for name in ("github-pat", "bugzilla-api-key", "claude", "litellm", "tailnet", "tailnet-api"):
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
        row = [r for r in self.rows({"push-keys/github-pat": "github_pat_11ABC_x"}) if r[1].startswith("github-pat")][0]
        self.assertEqual(UNK, row[0], row)
        self.assertIn("could not reach", row[1])

    def test_an_acceptable_credential_reports_what_it_can_do(self):
        rows = self.rows({"secrets/litellm-key": "sk-litellm-abc"})
        self.assertTrue(any(r[0] == OK and "restricted to the LLM API routes" in r[1] for r in rows), text_of(rows))
        self.assertEqual([], [r for r in rows if r[0] == MISS], text_of(rows))

    def test_nothing_stored_is_ever_printed(self):
        secret = "sk-ant-oat01-do-not-print-this"
        self.assertNotIn(secret, text_of(self.rows({"secrets/claude-token": secret})))


class TestTheFleetIsWalkedOnlyWhenAsked(unittest.TestCase):
    def _run(self, macos, everything):
        return [(title, list(rows)) for title, rows in fake_doctor(macos, mc=stub_mc(probe=boom, findings=boom, stale=boom)).sections(everything)]

    def test_without_all_no_machine_is_asked(self):
        for macos in (True, False):
            with self.subTest(macos=macos):
                titles = [t for t, _ in self._run(macos, False)]
                self.assertIn("host tools", titles)
                self.assertNotIn("battery", titles)

    def test_with_all_the_fleet_is_asked(self):
        with self.assertRaises(AssertionError):
            [list(rows) for _, rows in build_doctor(probe=boom).sections(True)]


class TestAMachineThatDoesNotAnswer(unittest.TestCase):
    def test_a_build_machine_is_unknown_never_missing(self):
        rows = list(build_doctor().build_machine("farbox"))
        self.assertEqual([(UNK, "farbox did not answer", "ssh farbox true  -- then re-run; nothing was changed")], rows)

    def test_a_build_machine_that_answers_gets_the_drivers_findings_and_its_provisioning_age(self):
        rows = list(build_doctor(probe=lambda t, root: "family=debian\n",
                                 findings=lambda root, probe, env=None, here=None: [("ok", "git (/usr/bin/git)", ""), ("required", "ninja", "apt install ninja"), ("note", "cores: 4", "")],
                                 stale=lambda t, root: "remote/provision.sh or remote/deps.sh has changed since it ran").build_machine("box"))
        self.assertEqual([(OK, "git (/usr/bin/git)", ""), (MISS, "ninja", "apt install ninja"), (UNK, "cores: 4", ""),
                          (MISS, "provisioning on box predates its inputs: remote/provision.sh or remote/deps.sh has changed since it ran",
                           "wk machine setup box")], rows)

    def test_a_missing_remedy_names_the_setup_command(self):
        rows = list(build_doctor(probe=lambda t, root: "family=debian\n",
                                 findings=lambda root, probe, env=None, here=None: [("wanted", "ccache", "")]).build_machine("box"))
        self.assertEqual((MISS, "ccache", "see 'wk machine setup box'"), rows[0])
        self.assertEqual((OK, "provisioned from this tree's remote/provision.sh + remote/deps.sh", ""), rows[1])

    def test_a_bridge_phone_that_does_not_answer_is_unknown(self):
        d = tempfile.mkdtemp(prefix="wk-test-doctor-bridges-")
        self.addCleanup(shutil.rmtree, d)
        for n in ("phone-a", "phone-b"):
            with open(os.path.join(d, n + ".conf"), "w") as f:
                f.write("kind=bridge\ndevice=pinephone\nsegment=10.9.0.0/24\nrouter=10.9.0.1\n")
        fake = Fake()
        fake.react(["ssh"], lambda argv, fk: Result(255, "", "No route to host") if "phone-a" in " ".join(argv)
                   else Result(0, "percent=87\nstatus=Charging\nlimit=80\ncurrent=80\n"))
        rows = list(fake_doctor(False, env={"WK_MACHINES_DIR": d}, machine=fake).battery())
        self.assertEqual([(UNK, "phone-a: did not answer", "wk machine status phone-a"),
                          (OK, "phone-b: 87% Charging, capped at 80%", "")], rows)


class TestRootAccess(unittest.TestCase):
    def test_sudo_is_asked_quietly_through_the_environment(self):
        fake = Fake()
        key = str(REPO / "cmd" / "key")
        fake.answer(["env", "WK_QUIET=1", key, "sudo", "status"], 1, "a password is required, but sudo keeps a timestamp\n")
        rows = list(fake_doctor(True, machine=fake).root_access())
        self.assertEqual([(MISS, "sudo: a password is required, but sudo keeps a timestamp", "wk key sudo setup")], rows)


class TestPrivilegedHelpers(unittest.TestCase):
    """A helper whose sudoers rule is out-ranked is installed and useless, so what is asked is whether it answers."""

    def setUp(self):
        self.helpers = doctor.Host.priv_helpers(str(REPO), env=clean_env())

    def _rows(self, answers, executable=True):
        fake = Fake()
        if executable:
            for h in self.helpers:
                fake.answer(["test", "-x", h[3]], 0)
        sh = stub_shell(priv_helpers=lambda root, env=None: self.helpers, priv_answers=lambda root, path, machine: answers)
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
    """`./setup` authenticates once and holds the sudo window open, so `sudo -n <helper>` succeeds for anything
    while it runs."""

    HELPER = "/usr/local/libexec/wk-boot-priv"

    def _answers(self, listing, run_succeeds=True):
        fake = Fake()
        fake.answer(["sudo", "-n", "-l"], out=listing + "\n")
        fake.answer(["sudo", "-n", self.HELPER], rc=0 if run_succeeds else 1)
        return doctor.Host.priv_answers(str(REPO), self.HELPER, fake)

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
        self.assertFalse(self._answers("    (ALL) ALL"))

    def test_the_path_must_match_exactly(self):
        self.assertFalse(self._answers("    (root) NOPASSWD: /usr/local/libexec/wk-boot-priv-old"))

    def test_no_listing_at_all_is_reported_as_no_grant(self):
        self.assertFalse(self._answers(""))


class TestTheMachineOverlay(unittest.TestCase):
    """~/.config/wk/machines/ and bench tasks are machine-local."""

    def rows(self, fake):
        return list(fake_doctor(False, machine=fake).machine_local())

    def test_the_overlay_is_a_backed_up_row(self):
        rows = self.rows(Fake())
        self.assertTrue(any(r[1].startswith("~/.config/wk/machines (absent)") and r[2].startswith("backed-up") for r in rows), rows)

    def test_each_workspace_holding_tasks_is_backed_up(self):
        fake = Fake()
        doc = fake_doctor(False, machine=fake)
        root = doc.store.records_dir()
        fake.dirs.update({root + "/ws", root + "/ws/w", root + "/ws/w/bench", root + "/ws/bare"})
        rows = [" ".join(r[1:]) for r in doc.machine_local() if "benchmark runs" in " ".join(r[1:])]
        self.assertEqual([r.split(" ")[0] for r in rows], [root + "/ws/w/bench"])
        self.assertIn("(backed-up)", rows[0])
        self.assertIn("wk bench export <task> copies one out", rows[0])

    def test_tasks_outside_any_workspace_are_backed_up_while_any_is_there(self):
        fake = Fake()
        doc = fake_doctor(False, machine=fake)
        legacy = doc.store.records_dir() + "/bench"
        fake.dirs.add(legacy)
        named = lambda: [" ".join(r[1:]) for r in doc.machine_local() if r[1].startswith(legacy + " ")]   # noqa: E731
        self.assertEqual([], named())
        fake._set_file(legacy + "/t/task.json", "{}")
        (row,) = named()
        self.assertIn("(backed-up)", row)
        self.assertIn("wk gc names", row)


if __name__ == "__main__":
    unittest.main()
