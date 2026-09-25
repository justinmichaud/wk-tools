"""Putting wk-tools on a machine (lib/wk/tools.py, each driver's `sync_tools`, and the bash shim in lib/target.sh).

The rule under test: a machine is given a *commit*, never a file copy. The copy over there is a real checkout whose
HEAD is this tree's HEAD, converged from whatever was there before -- nothing, a directory an older tooling copied
file by file, another commit, a dirty tree -- and an uncommitted tree here is refused by name instead of being copied.

Nothing here reaches a machine. The far side is a directory in a scratch tree, reached as this host's own `Local`
machine, so the far scripts and the bundle copy are the real ones; each driver's push is checked over a Fake.

Run: python3 tests/run.py -k tests.test_tools_sync
"""
import contextlib
import inspect
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, targets, tools  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

VM = REPO / "targets" / "vm.sh"
REMOTE = REPO / "targets" / "remote.sh"
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

    def test_a_directory_that_is_not_a_checkout_is_replaced_and_says_so(self):
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
        """Crash-only: killed between `git init` and the fetch, the far side is an empty repository with no HEAD."""
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
        """The verdict is the far side's own `git rev-parse HEAD`, not the exit status of the copy."""
        far = Fake("box")
        far.answer(["sh", "-c", tools.PREPARE])
        far.answer(["sh", "-c", tools.CONVERGE], out="0" * 40 + "\n")
        ok, err = self.push(far=far)
        self.assertFalse(ok)
        self.assertIn("did not end at", err)


class TestTheDestinationHasAFloorUnderIt(ToolsPushCase):
    """The far side's convergence is an `rm -rf "$d"` whenever $d is not already a checkout, so a destination that is
    the root or the account's home is refused before the far machine is asked anything."""

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
    """Each driver's `sync_tools` over a Fake host: the push goes to where that target runs wk-tools from."""

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
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)

    def conf(self, name, text):
        (self.tmp / "hosts" / (name + ".conf")).write_text(text)

    def pushed(self):
        return [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] in ("ssh", "scp")]

    def test_a_build_box_gets_the_bundle_at_its_tools_directory(self):
        self.conf("box", "KIND=build\nWK_REMOTE_HOST=box.example\nWK_REMOTE_ROOT=/home/u/wk\nWK_REMOTE_TOOLS=/home/u/wk/tools\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self.reg.load("box").sync_tools(""))
        runs = self.pushed()
        self.assertEqual([a[0] for a in runs], ["ssh", "scp", "ssh"])
        self.assertIn("box.example", runs[0])
        self.assertTrue(runs[0][-1].endswith("sh /home/u/wk/tools"), runs[0][-1])
        self.assertEqual(runs[1][-1], "box.example:/home/u/wk/tools/" + tools.BUNDLE)

    def test_a_peer_is_not_pushed_to_at_all(self):
        self.conf("pal", "KIND=peer\nWK_REMOTE_HOST=pal.example\nWK_REMOTE_PEER=1\n")
        self.assertTrue(self.reg.load("pal").sync_tools(""))
        self.assertEqual(self.pushed(), [])

    def test_a_container_bind_mounts_this_checkout_so_nothing_is_pushed(self):
        self.assertTrue(self.reg.load("container").sync_tools("ws"))
        self.assertEqual(self.fake.effects, [])

    def test_a_guest_gets_the_same_bundle_over_its_own_address(self):
        (self.tmp / "vmstore").mkdir()
        self.env.update({"WK_VM_STORE": str(self.tmp / "vmstore"), "WK_VM_USER": "admin"})
        t = targets.Vm("vm", str(REPO), dict(self.env), self.fake)
        with mock.patch.object(targets.Vm, "ip", return_value="192.168.64.9"), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(t.sync_tools("mac"))
        runs = self.pushed()
        self.assertIn("admin@192.168.64.9", runs[0])
        self.assertEqual(runs[1][-1], "admin@192.168.64.9:/Users/admin/wk-tools/" + tools.BUNDLE)
        self.assertIn("/Users/admin/.wk-workspace", runs[-1][-1], "the marker the pushed tooling reads was not rewritten")


class TestTheBashShim(ToolsPushCase):
    """One push for every kind: lib/target.sh's `t_sync_tools` is the only bash caller left, and it asks
    the Python. `boot/machines.sh`'s own push went with `machine_prepare` (`wk machine setup mbp`,
    lib/wk/machine_cmd/mac.py, calls `tools.push` directly), so the bash `tools_push` shim it once sourced
    has no caller left."""

    def test_no_driver_pushes_of_its_own(self):
        for f in (REMOTE, VM, REPO / "targets" / "container.sh", REPO / "targets" / "local.sh"):
            self.assertNotIn("\nt_sync_tools()", f.read_text(), f)
        self.assertIn('t_sync_tools()    { _ws_py sync-tools "$WK_TARGET" "$1"; }', (REPO / "lib" / "target.sh").read_text())

    def test_lib_tools_sh_is_gone(self):
        self.assertFalse((REPO / "lib" / "tools.sh").exists(), "the bash push shim has no caller left")
        self.assertNotIn("tools_push", (REPO / "boot" / "machines.sh").read_text())


class TestNoFileCopyLeft(unittest.TestCase):
    def test_no_driver_rsyncs_the_wk_root(self):
        hits = []
        for f in (REMOTE, VM):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if not line.lstrip().startswith("#") and "rsync" in line and "WK_ROOT" in line:
                    hits.append(f"{f}:{i}:{line.strip()}")
        self.assertEqual(hits, [], "wk-tools is still being file-copied")


class TestGuestStartConverges(unittest.TestCase):
    """lib/wk/guest.py converges everything a guest gets through one `Guest.converge`, the tooling checkout among
    it; both of a start's arms reach it."""

    def test_both_arms_converge_through_the_one_function(self):
        body = inspect.getsource(guest.start)
        self.assertEqual(1, body.count(".converge()"))
        for step in ("guest_tools_push", "write_deploy_keys", "settle_desktop"):
            self.assertNotIn(step, body, f"a start still runs {step} itself")

    def test_the_one_function_pushes_the_tools_once_and_only_warns(self):
        rows = [s for s in guest.STEPS if s[0] == "guest_tools_push"]
        self.assertEqual(1, len(rows), rows)
        self.assertIsNotNone(rows[0][1], "a failed tools push fails the start")
        self.assertIn("wk sync --tools", rows[0][2], "the warning names no remedy")

    def test_every_step_is_run_exactly_once(self):
        steps = [s[0] for s in guest.STEPS]
        for step in ("guest_tools_push", "write_marker", "write_shell_rc", "write_lldbinit", "set_guest_clock",
                     "set_guest_egress", "write_claude_config", "write_agent_secrets", "write_deploy_keys",
                     "settle_desktop", "report_desktop"):
            with self.subTest(step=step):
                self.assertEqual(1, steps.count(step), steps)


class TestStatusToolsRow(unittest.TestCase):
    """`wk status`'s wk-tools row for a machine across ssh: every copy is compared by commit -- a checkout's own, or,
    in the podman VM, this very checkout mounted in. A `-` sha is neither, and is never in sync."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-toolsrow-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", ".")
        (self.src / "f").write_text("one\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "one")
        self.short = git(self.src, "rev-parse", "--short", "HEAD").stdout.strip()
        self.full = git(self.src, "rev-parse", "HEAD").stdout.strip()

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

    def test_reporting_reaches_no_machine_and_syncs_nothing(self):
        import inspect
        from wk import status
        code = inspect.getsource(status.tools_fact) + inspect.getsource(status.Walk.report_machine)
        for writer in ("tools_push", "t_sync", "sync_tools", "rsync", "rev-parse HEAD --"):
            self.assertNotIn(writer, code, f"the status path runs {writer}")
        self.assertIn('wk("version"', code)


if __name__ == "__main__":
    unittest.main()
