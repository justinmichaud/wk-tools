"""`wk sync --tools <place>`: this checkout's commit put where a place runs wk-tools from -- a build box takes it as a
git bundle into a checkout of its own, which the scripts handed to the box converge, run for real in a sandbox."""
import os
import shlex
import shutil
import subprocess

from tests.fakes import FakeRegistry, Result
from tests.test_sync import Flow, Host, warned
from tests.test_wk_workspace import DRIVERS, Flow as WsFlow, said

TOOLS = "/home/u/wk/tools"
BUNDLE = ".git/wk-tools-push.bundle"


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60, check=check,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def ran(argv, path=None):
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=dict(os.environ, PATH=path or os.environ["PATH"]))
    return Result(cp.returncode, cp.stdout, cp.stderr)


class Box(Host):
    """This host and build box `box`: wk-tools here is `src`, a committed scratch checkout git answers for, and the
    box's / is the directory `far`, where the scripts wk-tools hands it run for real with `far_path` as their PATH
    and a bundle copied to it lands; `converged`, when set, is what the last script answers instead."""

    def __init__(self, tmp):
        super().__init__(tmp, kinds={"box": "remote"}, in_vm=False)
        self.src, self.far, self.far_path, self.converged = self.tmp / "src", self.tmp / "far", os.environ["PATH"], None
        self.src.mkdir()
        git(self.src, "init", "-q", ".")
        (self.src / "wk").write_text("#!/bin/sh\necho one\n")
        (self.src / ".gitignore").write_text("ignored-here\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "one")
        self.sha = git(self.src, "rev-parse", "HEAD").stdout.strip()
        self.env.update(WK_REMOTE_ROOT="/home/u/wk", WK_REMOTE_TOOLS=TOOLS)
        self.reg = FakeRegistry(self.env, self, lambda n, e: DRIVERS["remote"](n, str(self.src), e, self), names=["box"])
        self.react(["git", "-C", str(self.src)], lambda a, f: ran(a))
        self.react(["bash", "-c"], self._copy)

    def tools(self, dest=TOOLS):
        return self.far / dest.lstrip("/")

    def head(self, dest=TOOLS):
        cp = git(self.tools(dest), "rev-parse", "HEAD", check=False)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def asked(self):
        """What the box was handed for its tools directory: a script, or the bundle."""
        return [e for e in self.effects if e[0] == "run" and ("d=$1" in e[1][-1] or "cat > " in e[1][-1])]

    def _ssh(self, argv, f):
        words = shlex.split(argv[-1])
        if words[:2] == ["sh", "-c"] and words[2].startswith("set -e\nd=$1"):
            if 'fetch -q "$d/' in words[2] and self.converged is not None:
                return self.converged
            return ran(["sh", "-c", words[2], "sh", str(self.tools(words[4]))] + words[5:], self.far_path)
        return super()._ssh(argv, f)

    def _copy(self, argv, f):
        dest = shlex.split(shlex.split(argv[-1])[-1])[-1]
        shutil.copyfile(argv[4], str(self.tools(dest)))
        return Result(0)


class ToolsTest(Flow):
    world_class = Box

    def push(self, *argv, w=None):
        return self.sync("--tools", "box", *argv, w=w)


class TestTheBoxConverges(ToolsTest):
    def test_the_first_push_makes_a_checkout_at_this_trees_head_and_leaves_no_bundle(self):
        rc, err = self.push()
        far = self.w.tools()
        self.assertEqual((rc, self.w.head(), (far / "wk").read_text(), (far / BUNDLE).exists()), (0, self.w.sha, "#!/bin/sh\necho one\n", False))
        self.assertIn("  box                      pushed " + self.w.sha, err.splitlines())

    def test_a_second_push_changes_nothing_and_says_nothing_of_replacing(self):
        self.push()
        rc, err = self.push()
        self.assertEqual((rc, self.w.head(), "replacing" in err), (0, self.w.sha, False))

    def test_a_far_checkout_at_another_commit_is_reset_to_this_one(self):
        self.push()
        (self.w.tools() / "theirs").write_text("a commit only they have\n")
        git(self.w.tools(), "add", "-A")
        git(self.w.tools(), "commit", "-qm", "theirs")
        rc, err = self.push()
        self.assertEqual((rc, self.w.head(), (self.w.tools() / "theirs").exists()), (0, self.w.sha, False))

    def test_a_dirty_far_tree_is_overwritten_and_its_ignored_files_kept(self):
        self.push()
        far = self.w.tools()
        for name, text in (("wk", "edited over there\n"), ("untracked", "dropped in over there\n"), ("ignored-here", "this machine's own\n")):
            (far / name).write_text(text)
        rc, err = self.push()
        self.assertEqual((rc, (far / "wk").read_text(), (far / "untracked").exists(), (far / "ignored-here").exists()),
                         (0, "#!/bin/sh\necho one\n", False, True))

    def test_a_half_made_far_repository_converges(self):
        self.w.tools().mkdir(parents=True)
        git(self.w.tools(), "init", "-q", ".")
        self.assertEqual((self.push()[0], self.w.head()), (0, self.w.sha))

    def test_a_directory_that_is_not_a_checkout_is_refused_and_force_replaces_it_saying_so(self):
        far = self.w.tools()
        far.mkdir(parents=True)
        (far / "wk").write_text("#!/bin/sh\necho months old\n")
        (far / "gone.sh").write_text("a file this tree no longer has\n")
        rc, err = self.push()
        self.assertEqual((rc, said(err), (far / "wk").read_text()),
                         (1, "error: %s on box is not a git checkout, and --force is what replaces it" % TOOLS, "#!/bin/sh\necho months old\n"))
        rc, err = self.push("--force")
        self.assertEqual((rc, self.w.head(), (far / "gone.sh").exists()), (0, self.w.sha, False))
        self.assertIn("replacing %s: it is not a git checkout" % far, err.splitlines())


class TestWhatIsRefused(ToolsTest):
    def test_an_uncommitted_change_here_is_refused_with_the_remedy_and_nothing_is_sent(self):
        (self.w.src / "wk").write_text("#!/bin/sh\necho edited\n")
        rc, err = self.push()
        self.assertEqual((rc, warned(err), self.w.asked(), self.w.far.exists()),
                         (1, "warning: wk-tools here has uncommitted changes, so there is no commit to put on a machine.", [], False))
        self.assertIn("        git -C %s commit -a" % self.w.src, err.splitlines())

    def test_an_untracked_or_ignored_file_here_is_no_change_and_is_not_sent(self):
        (self.w.src / "new.sh").write_text("not added yet\n")
        (self.w.src / "ignored-here").write_text("machine-local\n")
        rc, err = self.push()
        self.assertEqual((rc, (self.w.tools() / "new.sh").exists(), (self.w.tools() / "ignored-here").exists()), (0, False, False))

    def test_a_tree_that_is_not_a_checkout_at_all_is_refused_naming_git_clone(self):
        shutil.rmtree(str(self.w.src / ".git"))
        rc, err = self.push()
        self.assertEqual((rc, warned(err), self.w.far.exists()),
                         (1, "warning: %s is not a git checkout, so there is no commit to put on a machine." % self.w.src, False))
        self.assertIn("    Run wk from a clone:  git clone https://github.com/justinmichaud/wk-tools", err.splitlines())

    def test_a_box_that_answers_another_sha_has_not_taken_it(self):
        self.w.converged = Result(0, "0" * 40 + "\n")
        rc, err = self.push()
        self.assertEqual((rc, warned(err)), (1, "warning: wk-tools at %s did not end at %s (it answers '%s')" % (TOOLS, self.w.sha, "0" * 40)))

    def test_a_destination_that_is_the_root_a_top_directory_or_the_home_is_refused_and_the_box_asked_nothing(self):
        """The far side's `rm -rf "$d"` replaces any $d that is not a checkout, so this is its floor."""
        for dest in ("/", "/opt", "/opt/wk-tools/", "home"):
            w = self.make_world()
            dest = w.env["WK_REMOTE_TOOLS"] = w.env["HOME"] if dest == "home" else dest
            rc, err = self.push(w=w)
            self.assertEqual((rc, warned(err), w.asked()), (1, "warning: refusing to push wk-tools to '%s'. The far side replaces that directory" % dest, []), dest)
        w = self.make_world()
        w.env["WK_REMOTE_TOOLS"] = "/opt/wk-tools"
        self.assertEqual((self.push(w=w)[0], w.head("/opt/wk-tools")), (0, w.sha))

    def test_a_box_with_no_git_keeps_its_checkout(self):
        self.push()
        nogit = self.tmp / "nogit-bin"
        nogit.mkdir()
        for tool in ("sh", "cat", "rm", "mkdir", "env"):
            os.symlink(shutil.which(tool), str(nogit / tool))
        self.w.far_path = str(nogit)
        rc, err = self.push()
        self.assertEqual((rc, (self.w.tools() / ".git").is_dir()), (1, True))
        self.assertIn("no git on this machine, so wk-tools cannot be a checkout here", err.splitlines())

    def test_a_symlinked_destination_is_refused_by_name_and_left_a_link(self):
        real = self.tmp / "real-tools"
        real.mkdir()
        (real / "keep").write_text("a real directory somebody linked to\n")
        self.w.tools().parent.mkdir(parents=True)
        os.symlink(str(real), str(self.w.tools()))
        rc, err = self.push()
        self.assertEqual((rc, self.w.tools().is_symlink(), (real / "keep").exists()), (1, True, True))
        self.assertIn("%s is a symlink, and wk-tools here is a directory; not replacing it" % self.w.tools(), err.splitlines())

    def test_a_dry_run_names_the_push_and_asks_the_box_nothing(self):
        rc, err = self.push("--dry-run")
        self.assertEqual((rc, self.w.asked(), self.w.far.exists()), (0, [], False))
        self.assertRegex(err, r"would copy on box: \S+ -> %s/%s" % (TOOLS, BUNDLE))


class TestAGuest(Flow):
    def test_a_guest_gets_the_bundle_through_its_guest_agent_and_the_marker_its_tooling_reads(self):
        w = self.make_world(kinds={"vm": "vm"}, in_vm=False)
        w.make_guest("mac")
        rc, err = self.sync("--tools", "vm", w=w)
        guest = [e[1][-1] for e in w.effects if e[0] == "run" and "wk-mac" in e[1]]
        self.assertEqual((rc, [c for c in guest if c.startswith("cat > ")], [c for c in guest if c.endswith(" sh /Users/admin/.wk-workspace")] != []),
                         (0, ["cat > /Users/admin/wk-tools/" + BUNDLE], True))


class TestTheContainersMount(WsFlow):
    def test_a_container_mounts_the_podman_vms_wk_tools_unless_the_env_names_another(self):
        for env, src in (({}, "/opt/wk-tools"), ({"WK_TOOLS_SRC": "/x"}, "/x")):
            w = self.make_world()
            w.env.update(env)
            rc, err = self.detach(w=w)
            self.assertIn("--volume %s:/opt/wk-tools:ro " % src, " ".join(self.created(w)[0]), env)
