"""Putting wk-tools on a machine (lib/wk/tools.py and each driver's `sync_tools`)."""
import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import places, tools  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

SHA = "a" * 40


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=60, check=check,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


class NoGit(Local):
    """A far machine with no git on its PATH."""

    def __init__(self, path):
        self.path = path

    def run(self, argv, input=None, timeout=None):
        return super().run(["env", "PATH=%s" % self.path] + list(argv), input=input, timeout=timeout)


class ToolsPushCase(unittest.TestCase):
    """A scratch source tree (committed), and a far directory to converge."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.far = self.tmp / "far" / "tools"
        self.src.mkdir()
        git(self.src, "init", "-q", ".")
        (self.src / "wk").write_text("#!/bin/sh\necho one\n")
        (self.src / ".gitignore").write_text("ignored-here\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "one")
        self.sha = git(self.src, "rev-parse", "HEAD").stdout.strip()
        self.env = {"HOME": str(self.tmp / "home")}

    def push(self, far=None, dest=None, src=None):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = tools.push(str(src or self.src), Local(), far or Local(), str(self.far if dest is None else dest), self.env)
        return ok, err.getvalue()

    def far_head(self):
        cp = git(self.far, "rev-parse", "HEAD", check=False)
        return cp.stdout.strip() if cp.returncode == 0 else ""


class TestConverge(ToolsPushCase):
    def test_first_push_makes_a_checkout_at_this_trees_head(self):
        ok, err = self.push()
        self.assertTrue(ok, err)
        self.assertEqual(self.far_head(), self.sha)
        self.assertTrue((self.far / ".git").is_dir(), "not a checkout")
        self.assertEqual((self.far / "wk").read_text(), "#!/bin/sh\necho one\n")
        self.assertFalse((self.far / tools.BUNDLE).exists(), "the bundle was left behind")

    def test_a_second_push_changes_nothing_and_still_reports_ok(self):
        self.assertTrue(self.push()[0])
        ok, err = self.push()
        self.assertTrue(ok, err)
        self.assertEqual(self.far_head(), self.sha)

    def test_a_far_checkout_at_another_commit_is_reset_to_the_pushed_one(self):
        self.assertTrue(self.push()[0])
        (self.far / "theirs").write_text("a commit only they have\n")
        git(self.far, "add", "-A")
        git(self.far, "commit", "-qm", "theirs")
        self.assertNotEqual(self.far_head(), self.sha)
        ok, err = self.push()
        self.assertTrue(ok, err)
        self.assertEqual(self.far_head(), self.sha)
        self.assertFalse((self.far / "theirs").exists(), "a commit of their own survived the reset")

    def test_a_directory_that_is_not_a_checkout_is_refused_without_force(self):
        self.far.mkdir(parents=True)
        (self.far / "wk").write_text("#!/bin/sh\necho months old\n")
        with mock.patch.dict(os.environ, {"WK_FORCE": ""}):
            ok, err = self.push()
        self.assertFalse(ok)
        self.assertIn("--force is what replaces it", err)
        self.assertEqual((self.far / "wk").read_text(), "#!/bin/sh\necho months old\n")

    @mock.patch.dict(os.environ, {"WK_FORCE": "1"})
    def test_a_directory_that_is_not_a_checkout_is_replaced_and_says_so_under_force(self):
        self.far.mkdir(parents=True)
        (self.far / "wk").write_text("#!/bin/sh\necho months old\n")
        (self.far / "gone.sh").write_text("a file this tree no longer has\n")
        ok, err = self.push()
        self.assertTrue(ok, err)
        self.assertEqual(self.far_head(), self.sha)
        self.assertFalse((self.far / "gone.sh").exists(), "the file copy was merged into, not replaced")
        self.assertIn("replacing", err)
        self.assertIn("not a git checkout", err)

    def test_converging_a_checkout_says_nothing_about_replacing(self):
        self.assertTrue(self.push()[0])
        self.assertNotIn("replacing", self.push()[1])

    def test_a_dirty_far_tree_is_overwritten_and_its_ignored_files_kept(self):
        self.assertTrue(self.push()[0])
        (self.far / "wk").write_text("edited over there\n")
        (self.far / "untracked").write_text("dropped in over there\n")
        (self.far / "ignored-here").write_text("this machine's own\n")
        ok, err = self.push()
        self.assertTrue(ok, err)
        self.assertEqual((self.far / "wk").read_text(), "#!/bin/sh\necho one\n")
        self.assertFalse((self.far / "untracked").exists())
        self.assertTrue((self.far / "ignored-here").exists(), "an ignored file that machine keeps for itself was deleted")

    def test_a_half_made_far_repository_converges_on_a_re_run(self):
        self.far.mkdir(parents=True)
        git(self.far, "init", "-q", ".")
        ok, err = self.push()
        self.assertTrue(ok, err)
        self.assertEqual(self.far_head(), self.sha)


class TestRefusal(ToolsPushCase):
    def test_an_uncommitted_change_here_is_refused_with_the_remedy(self):
        (self.src / "wk").write_text("#!/bin/sh\necho edited\n")
        ok, err = self.push()
        self.assertFalse(ok)
        for words in ("uncommitted changes", "compare", "commit -a"):
            self.assertIn(words, err)
        self.assertFalse(self.far.exists(), "something was sent anyway")

    def test_an_untracked_or_ignored_file_here_is_not_a_dirty_tree_and_is_not_sent(self):
        (self.src / "new.sh").write_text("not added yet\n")
        (self.src / "ignored-here").write_text("machine-local\n")
        ok, err = self.push()
        self.assertTrue(ok, err)
        self.assertFalse((self.far / "new.sh").exists())
        self.assertFalse((self.far / "ignored-here").exists())

    def test_a_tree_that_is_not_a_checkout_at_all_is_refused(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        ok, err = self.push(src=plain)
        self.assertFalse(ok)
        self.assertIn("not a git checkout", err)
        self.assertIn("git clone", err)
        self.assertFalse(self.far.exists())

    def test_a_far_side_that_answers_with_another_sha_is_not_reported_ok(self):
        far = Fake("box")
        far.answer(["sh", "-c", tools.PREPARE])
        far.answer(["sh", "-c", tools.CONVERGE], out="0" * 40 + "\n")
        ok, err = self.push(far=far)
        self.assertFalse(ok)
        self.assertIn("did not end at", err)


class TestTheDestinationHasAFloorUnderIt(ToolsPushCase):
    """The far side's convergence is an `rm -rf "$d"` whenever $d is not already a checkout, so a destination
    that is the root or the account's home is refused before the far machine is asked anything."""

    def test_each_dangerous_destination_is_refused_and_nothing_is_asked(self):
        home = self.env["HOME"]
        for dest in ("", "/", "/opt", "opt/wk-tools", "/opt/wk-tools/", home):
            with self.subTest(dest=dest):
                far = Fake("box")
                ok, err = self.push(far=far, dest=dest)
                self.assertFalse(ok)
                self.assertIn("refusing to push wk-tools", err)
                self.assertEqual(far.effects, [], "the far side was handed a command anyway")

    def test_a_two_component_absolute_path_is_allowed(self):
        self.assertTrue(tools.dest_ok("/opt/wk-tools", "/home/u"))


class TestTheFarSideRefusesBeforeItDeletes(ToolsPushCase):
    def test_a_machine_with_no_git_keeps_its_checkout(self):
        self.assertTrue(self.push()[0])
        nogit = self.tmp / "nogit-bin"
        nogit.mkdir()
        for tool in ("sh", "cat", "rm", "mkdir", "env"):
            os.symlink(shutil.which(tool), nogit / tool)
        ok, err = self.push(far=NoGit(str(nogit)))
        self.assertFalse(ok)
        self.assertIn("no git on this machine", err)
        self.assertTrue((self.far / ".git").is_dir(), "the checkout was deleted by a machine that has no git")

    def test_a_symlinked_destination_is_refused_by_name(self):
        real = self.tmp / "real-tools"
        real.mkdir()
        (real / "keep").write_text("a real directory somebody linked to\n")
        self.far.parent.mkdir(parents=True)
        os.symlink(real, self.far)
        ok, err = self.push()
        self.assertFalse(ok)
        self.assertIn("symlink", err)
        self.assertTrue(self.far.is_symlink(), "the link was replaced by a directory")
        self.assertTrue((real / "keep").exists())


class TestDryRun(ToolsPushCase):
    def test_a_dry_run_names_the_push_and_asks_the_far_side_nothing(self):
        far = Fake("box")
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            ok, err = self.push(far=far)
        self.assertTrue(ok)
        self.assertEqual([e for e in far.effects if e[0] == "run"], [])
        self.assertFalse(self.far.exists())


class EachKind(unittest.TestCase):
    """Each driver's `sync_tools` over a Fake host: the push goes to where that place runs wk-tools from."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-tools-kind-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "hosts").mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_MACHINES_DIR": str(self.tmp / "hosts"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "PATH": os.environ.get("PATH", "")}
        self.fake = Fake("here")
        self.fake.answer(["git", "-C", str(REPO), "rev-parse", "--git-dir"], out=".git\n")
        self.fake.answer(["git", "-C", str(REPO), "status"], out="")
        self.fake.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out=SHA + "\n")
        self.fake.answer(["git", "-C", str(REPO), "bundle"])
        self.fake.answer(["scp"])
        self.fake.react(["ssh"], lambda a, f: Result(1) if "wk-image" in a[-1] else
                        Result(0, SHA + "\n" if "rev-parse HEAD" in a[-1] else ""))
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)

    def conf(self, name, text):
        (self.tmp / "hosts" / (name + ".conf")).write_text(text)

    def pushed(self):
        return [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] in ("ssh", "scp")]

    def test_a_build_box_gets_the_bundle_at_its_tools_directory(self):
        self.conf("box", "kind=build\nhost=box.example\nroot=/home/u/wk\ntools=/home/u/wk/tools\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self.reg.load("box").sync_tools(""))
        runs = self.pushed()
        self.assertEqual([a[0] for a in runs], ["ssh", "scp", "ssh"])
        self.assertIn("box.example", runs[0])
        self.assertTrue(shlex.split(runs[0][-1])[-1].endswith("sh /home/u/wk/tools"), runs[0][-1])
        self.assertEqual(runs[1][-1], "box.example:/home/u/wk/tools/" + tools.BUNDLE)

    def test_a_peer_is_not_pushed_to_at_all(self):
        self.conf("pal", "kind=peer\nhost=pal.example\npeer=1\n")
        self.assertTrue(self.reg.load("pal").sync_tools(""))
        self.assertEqual(self.pushed(), [])

    def test_a_container_bind_mounts_this_checkout_so_nothing_is_pushed(self):
        self.assertTrue(self.reg.load("container").sync_tools("ws"))
        self.assertEqual(self.fake.effects, [])

    def test_a_guest_gets_the_same_bundle_through_its_guest_agent(self):
        (self.tmp / "vmstore").mkdir()
        self.env.update({"WK_VM_STORE": str(self.tmp / "vmstore"), "WK_VM_USER": "admin"})
        t = places.Vm("vm", str(REPO), dict(self.env), self.fake)
        self.fake.react(["/t/tart", "exec"], lambda a, f: Result(1) if "wk-image" in a[-1] else
                        Result(0, SHA + "\n" if "rev-parse HEAD" in a[-1] else ""))
        self.fake.answer(["sh", "-c"])
        with mock.patch.object(places.Vm, "tart", lambda s: "/t/tart"), mock.patch.object(places.Vm, "vm_state", return_value="running"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(t.sync_tools("mac"))
        runs = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] in ("/t/tart", "sh")]
        self.assertEqual(runs[0][:4], ("/t/tart", "exec", "-i", "wk-mac"))
        self.assertEqual((runs[1][3:5], runs[1][-1]), (("/t/tart", "wk-mac"), "/Users/admin/wk-tools/" + tools.BUNDLE))
        self.assertIn("/Users/admin/.wk-workspace", runs[-1][-1], "the marker the pushed tooling reads was not rewritten")


class TestStatusToolsRow(unittest.TestCase):
    """`wk status`'s wk-tools row for a machine across ssh: every copy is compared by commit -- a checkout's own,
    or, in the podman VM, this very checkout mounted in."""

    full = "4f1c2b9e0d3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e"
    short = full[:7]

    def row(self, ver, in_vm=False):
        from wk import status
        fields = dict(l.split("=", 1) for l in ver.splitlines() if "=" in l)
        return status.tools_fact(fields, self.short, "fakebox", "fakebox", in_vm=in_vm)

    def test_a_checkout_at_this_trees_commit_reads_in_sync(self):
        row = self.row(f"sha={self.full}\ndirty=no\n")
        self.assertEqual((row["sha"], row["expect"], row["insync"]), (self.full, self.short, True))
        self.assertNotIn("fix", row)

    def test_a_checkout_at_another_commit_differs_and_names_the_push(self):
        row = self.row("sha=0000000\ndirty=no\n")
        self.assertEqual((row["sha"], row["expect"], row["insync"]), ("0000000", self.short, False))
        self.assertEqual(row["fix"], "wk sync --tools fakebox")

    def test_a_dirty_checkout_over_there_is_reported_as_dirty(self):
        row = self.row(f"sha={self.short}\ndirty=yes\n")
        self.assertEqual((row["dirty"], row["insync"]), (True, True))

    def test_a_copy_with_no_commit_is_never_in_sync(self):
        row = self.row("sha=-\ndirty=unknown\n", in_vm=True)
        self.assertEqual(row["insync"], False)
        self.assertEqual(row["fix"], "./setup   (recreates the machine with this checkout mounted at /opt/wk-tools)")

class TestTheToolsSource(unittest.TestCase):
    def test_a_container_mounts_this_checkout_unless_the_env_names_another(self):
        c = places.Container("container", str(REPO), {"HOME": "/nonexistent"}, Fake("here"))
        self.assertEqual(c.tools_src(), str(REPO))
        c = places.Container("container", str(REPO), {"HOME": "/nonexistent", "WK_TOOLS_SRC": "/x"}, Fake("here"))
        self.assertEqual(c.tools_src(), "/x")


if __name__ == "__main__":
    unittest.main()
