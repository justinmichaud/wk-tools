"""A workstation's setup runs itself: lib/wk/setupstate.py against a scratch git tree, and the dispatcher's
converge_setup over a fake machine."""
import contextlib
import io
import os
import subprocess
import sys
from unittest import mock

from tests import fakes
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import dispatch, setupstate  # noqa: E402
from wk.decl import Command  # noqa: E402
from wk.machine import Result  # noqa: E402
from wk.store import Store  # noqa: E402


class Here(fakes.World):
    """git and the tree's files are this machine's; the record, sysctl and launchctl are the fake's."""

    def __init__(self):
        super().__init__(name="fake")
        self.react(["git"], lambda argv, f: Result(*(lambda cp: (cp.returncode, cp.stdout, cp.stderr))(
            subprocess.run(argv, capture_output=True, text=True))))

    def exists(self, path):
        return path in self.files or os.path.exists(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def read_bytes(self, path, start=0):
        with open(path, "rb") as f:
            return f.read()


class SetupTest(fakes.FlowTest):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "tree"
        self.root.mkdir()
        for argv in (["init", "-q"], ["config", "user.email", "a@b"], ["config", "user.name", "a"]):
            subprocess.run(["git", "-C", str(self.root)] + argv, check=True)
        (self.root / "setup").write_text("one\n")
        subprocess.run(["git", "-C", str(self.root), "add", "setup"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "one"], check=True)
        self.env = {"HOME": str(self.tmp / "home"), "XDG_STATE_HOME": str(self.tmp / "state")}
        self.m = Here()
        self.m.answer(["sysctl"], out="{ sec = 100, usec = 0 }, Mon")
        self.m.answer(["cat", "/proc/sys/kernel/random/boot_id"], out="boot-1\n")

    def behind(self):
        return setupstate.behind(str(self.root), self.env, self.m)

    def record(self):
        setupstate.write(self.env, self.m, setupstate.tree(str(self.root), self.m))


class TestTheRecord(SetupTest):
    def test_a_machine_no_person_has_set_up_is_never_behind(self):
        self.assertEqual("", self.behind())

    def test_a_run_from_this_tree_is_current_and_any_change_to_it_is_behind(self):
        self.record()
        self.assertEqual("", self.behind())
        for change in (lambda: (self.root / "setup").write_text("two\n"), lambda: (self.root / "new.sh").write_text("x\n"),
                       lambda: subprocess.run(["git", "-C", str(self.root), "commit", "-qam", "two"], check=True)):
            change()
            self.assertEqual("the tree changed since it last ran", self.behind())
            self.record()
            self.assertEqual("", self.behind())


@mock.patch.object(setupstate, "is_macos", return_value=True)
class TestAMacAfterABoot(SetupTest):
    def setUp(self):
        super().setUp()
        agents = self.tmp / "home" / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.wk.github-inject.plist").write_text("<plist/>")
        self.record()
        self.m.answer(["sysctl"], out="{ sec = 200, usec = 0 }, Tue")

    def test_an_agent_launchd_did_not_load_is_behind_once_per_boot(self, _):
        self.m.answer(["launchctl", "print"], rc=113)
        self.assertEqual("launchd has not loaded com.wk.github-inject since this boot", self.behind())
        self.record()
        self.assertEqual("", self.behind(), "a run in this boot that could not load it is not repeated")

    def test_loaded_agents_and_ones_never_installed_are_not(self, _):
        self.m.answer(["launchctl", "print"], rc=0)
        self.assertEqual("", self.behind())


class TestTheDispatcherRunsIt(SetupTest):
    def converge(self, why=("behind", "behind"), readonly=False, inside=False):
        self.m.answer([str(dispatch.ROOT / "setup")], out="setup ran\n")
        reg = mock.Mock(store=Store(self.env), in_remote_host=lambda: False)
        err = io.StringIO()
        with mock.patch.object(dispatch.setupstate, "behind", side_effect=list(why)), \
                mock.patch.object(dispatch, "registry", return_value=reg), \
                mock.patch.object(dispatch, "in_workspace", return_value=inside), \
                mock.patch.object(dispatch, "in_vm", return_value=False), \
                mock.patch.dict(os.environ, self.env), contextlib.redirect_stderr(err):
            dispatch.converge_setup("new", Command(readonly=readonly), self.m)
        return [e[1] for e in self.m.effects if e[0] == "run"], err.getvalue()

    def test_behind_it_runs_setup_first_and_says_why(self):
        runs, err = self.converge()
        self.assertEqual([(str(dispatch.ROOT / "setup"),)], runs)
        self.assertEqual([(str(dispatch.ROOT / "setup"),)], self.m.streamed)
        self.assertIn("setup is behind (behind), so ./setup runs before 'wk new'", err)

    def test_one_another_command_converged_while_this_waited_does_not_run_it_again(self):
        self.assertEqual([], self.converge(why=("behind", ""))[0])

    def test_a_readonly_command_or_one_inside_a_workspace_never_runs_it(self):
        for kw in ({"readonly": True}, {"inside": True}):
            with self.subTest(**kw):
                self.assertEqual(([], ""), self.converge(**kw))

    def test_a_dry_run_says_it_would(self):
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            runs, err = self.converge()
        self.assertEqual([], runs)
        self.assertIn("would run on fake: %s" % (dispatch.ROOT / "setup"), err)


class TestSetupRecordsItsRun(SetupTest):
    def py(self, *argv):
        env = dict(os.environ, WK_ROOT=str(self.root), PYTHONPATH=str(REPO / "lib"), **self.env)
        return subprocess.run([sys.executable, "-m", "wk.setupstate", *argv], env=env, capture_output=True, text=True)

    def test_the_tree_taken_at_the_start_is_what_is_recorded(self):
        start = self.py("tree").stdout.strip()
        (self.root / "setup").write_text("edited while it ran\n")
        cp = self.py("record", start)
        self.assertEqual(0, cp.returncode, cp.stderr)
        self.assertEqual("the tree changed since it last ran", setupstate.behind(str(self.root), self.env))
        self.assertEqual((2, "usage: python3 -m wk.setupstate tree | record <tree>\n"), (self.py("record").returncode, self.py("record").stderr))

    def test_an_unreadable_untracked_file_is_hashed_by_name(self):
        (self.root / "locked").write_text("x")
        (self.root / "locked").chmod(0)
        self.addCleanup((self.root / "locked").chmod, 0o644)
        self.assertTrue(self.py("tree").stdout.strip())
