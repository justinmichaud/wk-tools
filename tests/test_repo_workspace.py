"""`wk new --repo`: the repository a workspace holds is chosen at creation, kept in its marker, and every reader asks it."""
import contextlib
import io
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_sync, test_wk_workspace
from tests.support import REPO, bash, git_commit, git_run, scratch_dir
from tests.test_wk_workspace import WK, World, WorkspaceTest

sys.path.insert(0, str(REPO / "lib"))
from wk import places, project, repos, status, sync, workspace  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402
from wk.store import Snapshots  # noqa: E402

TOOLS = "wk-tools"
ORIGIN = "https://github.com/someone/wk-tools.git"
BRANCH = "python-core"
PUSH = "git@github-wk-tools:someone/wk-tools.git"


def tools():
    return repos.Repo(TOOLS)


class ToolsWorld(World):
    """`git remote get-url origin` in the tooling answers with an ssh GitHub URL, and wkdev-create's first start writes
    the marker firstrun.sh writes, naming the repo the create flags carry. No mirror and no snapshot."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)
        self.react(["git", "-c", "safe.directory=*", "-C"],
                   lambda argv, f: Result(0, BRANCH + "\n" if "symbolic-ref" in argv else "git@github.com:someone/wk-tools.git\n"))
        self.answer(["git", "ls-remote"])
        store = self.driver.store
        self.dirs = {d for d in self.dirs if not d.startswith((store.mirror_dir(), store.snapshots_dir()))}
        self.files = {p: v for p, v in self.files.items() if not p.startswith(store.snapshots_dir())}

    def wkdev_create(self, argv, f):
        r = World.wkdev_create(self, argv, f)
        flags = argv[argv.index("--additional-flags") + 1]
        home = argv[argv.index("--home") + 1]
        f.files[os.path.join(home, ".wk-workspace")] = "name=ws\nsrc=%s\nrepo=%s\n" % (
            re.search(r"WK_SRC=(\S+)", flags).group(1), re.search(r"WK_REPO=(\S+)", flags).group(1))
        return r

    def mark(self, name="ws", repo=TOOLS):
        self.files[repos.marker_in(self.ws_dir(name))] = "name=%s\nrepo=%s\n" % (name, repo)


class ToolsRecording(test_wk_workspace.Recording, ToolsWorld):
    pass


class TestTheTable(unittest.TestCase):
    def test_the_default_is_overlaid_on_a_snapshot_at_the_projects_checkout(self):
        r = repos.default()
        self.assertTrue(r.snapshot)
        self.assertEqual(project.get("SRC"), r.src)
        self.assertEqual(project.get("CHECKOUT"), r.checkout)

    def test_wk_tools_is_cloned_beside_it_and_needs_no_snapshot(self):
        self.assertFalse(tools().snapshot)
        self.assertEqual("/src/wk-tools", tools().src)

    def test_an_unknown_repo_is_a_lookup_error_naming_the_known_ones(self):
        with self.assertRaises(LookupError) as cm:
            repos.Repo("nope")
        for name in repos.names():
            self.assertIn(name, str(cm.exception))

    def test_a_marker_without_a_repo_is_the_defaults(self):
        self.assertEqual(project.get("REPO"), repos.of_marker({"name": "ws"}).name)
        self.assertEqual(TOOLS, repos.of_marker({"repo": TOOLS}).name)


class TestTheOrigin(unittest.TestCase):
    def origin(self, rc, out):
        m = mock.Mock()
        m.run.return_value = Result(rc, out)
        return tools().origin(m, "/tools")

    def test_every_github_spelling_is_fetched_over_https(self):
        for url in ("git@github.com:someone/wk-tools.git", "ssh://git@github.com/someone/wk-tools",
                    "https://github.com/someone/wk-tools.git", "https://github.com/someone/wk-tools/"):
            self.assertEqual(ORIGIN, self.origin(0, url + "\n"), url)

    def test_an_origin_elsewhere_or_none_is_refused(self):
        for rc, out in ((0, "https://gitlab.com/someone/wk-tools.git\n"), (2, "")):
            with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()):
                self.origin(rc, out)

    def test_it_pushes_through_its_own_deploy_keys_alias(self):
        m = mock.Mock()
        m.run.return_value = Result(0, "git@github.com:someone/wk-tools.git\n")
        self.assertEqual(PUSH, tools().push_url(m, "/tools"))

    def test_the_defaults_origin_is_upstream_and_asks_no_machine(self):
        m = mock.Mock()
        self.assertEqual(dict(project.get("REMOTES"))["origin"], repos.default().origin(m, "/tools"))
        m.run.assert_not_called()


class TestTheBranch(unittest.TestCase):
    """A cloned workspace checks out the branch this machine's wk-tools is on, which must be on its origin."""

    def branch(self, head, remote):
        m = Machine_answering(head, remote)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return tools().branch(m, "/tools"), err.getvalue()
            except Refused:
                return None, err.getvalue()

    def test_the_current_branch_on_the_origin_is_the_one(self):
        self.assertEqual(BRANCH, self.branch(Result(0, BRANCH + "\n"), Result(0, "sha\trefs/heads/x\n"))[0])

    def test_a_detached_checkout_is_refused(self):
        name, err = self.branch(Result(128, "", "fatal: ref HEAD is not a symbolic ref"), Result(0))
        self.assertIsNone(name)
        self.assertIn("switch", err)

    def test_a_branch_not_on_the_origin_is_refused_naming_the_push(self):
        name, err = self.branch(Result(0, BRANCH + "\n"), Result(2))
        self.assertIsNone(name)
        self.assertIn("push -u origin " + BRANCH, err)

    def test_an_origin_that_does_not_answer_is_refused_with_what_git_said(self):
        name, err = self.branch(Result(0, BRANCH + "\n"), Result(128, "", "fatal: unable to access"))
        self.assertIsNone(name)
        self.assertIn("unable to access", err)


def Machine_answering(head, remote):
    m = mock.Mock()
    m.run.side_effect = lambda argv: (head if "symbolic-ref" in argv else remote if argv[1] == "ls-remote"
                                      else Result(0, "git@github.com:someone/wk-tools.git\n"))
    return m


class TestTheFront(WorkspaceTest):
    world_class = ToolsWorld

    def test_another_place_is_refused_before_anything_runs(self):
        for kind in ("vm", "remote"):
            w = self.make_world(kinds={"fakebox": kind})
            self.refused(lambda: self.front(w, repo=TOOLS))
            self.assertEqual([], [e for e in w.effects if e[0] in ("spawn", "write")], kind)

    def test_an_unknown_repo_is_refused(self):
        self.refused(lambda: self.front(repo="nope"))
        self.assertEqual([], [e for e in self.w.effects if e[0] == "spawn"])

    def test_the_detached_run_is_handed_the_repo(self):
        self.stderr(lambda: self.front(no_wait=True, repo=TOOLS))
        (spawn,) = [e for e in self.w.effects if e[0] == "spawn"]
        self.assertEqual(TOOLS, spawn[1][spawn[1].index("--repo") + 1])

    def test_kill_takes_no_repo(self):
        self.refused(lambda: self.front(kill=True, repo=TOOLS))


class TestCreation(WorkspaceTest):
    world_class = ToolsWorld

    def create_flags(self):
        (argv,) = self.runs(head="wkdev-create")
        return argv[argv.index("--additional-flags") + 1].split()

    def test_it_is_made_with_no_mirror_and_no_snapshot_and_a_default_one_is_not(self):
        self.refused(lambda: self.detached())
        rc, err = self.stderr(lambda: self.detached(self.make_world(), repo=tools()))
        self.assertEqual(0, rc, err)

    def test_nothing_of_the_mirror_or_the_snapshot_is_asked_for(self):
        self.stderr(lambda: self.detached(repo=tools()))
        self.assertEqual([], self.runs(head=WK))
        self.assertNotIn("lock store", self.lock_takes())
        self.assertNotIn(os.path.join(self.w.ws_dir(), "base-id"), self.w.files)
        flags = self.create_flags()
        self.assertFalse([f for f in flags if f.startswith("WK_MIRROR=") or ":O,upperdir=" in f])

    def test_the_checkout_is_its_own_directory_cloned_from_the_tools_origin(self):
        self.stderr(lambda: self.detached(repo=tools()))
        flags = self.create_flags()
        self.assertIn("%s/wk-tools:/src/wk-tools" % self.w.ws_dir(), flags)
        self.assertIn("WK_CLONE=" + ORIGIN, flags)
        self.assertIn("WK_BRANCH=" + BRANCH, flags)
        self.assertIn("WK_PUSH=" + PUSH, flags)
        self.assertIn("WK_REPO=wk-tools", flags)
        self.assertIn(os.path.join(self.w.ws_dir(), "wk-tools"), self.w.dirs)

    def test_once_made_it_is_present_and_every_reader_finds_its_checkout(self):
        self.stderr(lambda: self.detached(repo=tools()))
        d = self.w.driver
        self.assertEqual("present", d.state("ws"))
        self.assertEqual(TOOLS, d.repo("ws").name)
        self.assertEqual("/src/wk-tools", d.src("ws"))
        (t,) = self.w.records.list()
        self.assertEqual("ok", t.verdict())

    def test_the_agents_are_installed_in_its_checkout(self):
        self.stderr(lambda: self.detached(repo=tools()))
        configs = [a[-1] for a in self.runs(head="exec") if "workspace-config.py" in a[-1]]
        self.assertTrue(configs)
        self.assertTrue(all(c.endswith(" /src/wk-tools") for c in configs), configs)

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        self.dry_as_wet(lambda: ToolsRecording(self.tmp), lambda w: self.detached(w, repo=tools()))


class TestFreshen(WorkspaceTest):
    def test_a_cloned_repo_is_reported_and_not_fetched(self):
        self.stderr(lambda: workspace.freshen(self.w.driver, "ws", self.w, tools()))
        self.assertEqual([], self.runs(head=WK))
        (probe,) = self.runs(head="exec")
        self.assertIn("git symbolic-ref", probe[-1])

    def test_a_dry_run_runs_nothing_for_it(self):
        self.dry_run()
        self.stderr(lambda: workspace.freshen(self.w.driver, "ws", self.w, tools()))
        self.assertEqual([], self.runs())


class TestReaders(WorkspaceTest):
    world_class = ToolsWorld

    def test_the_branch_is_read_from_the_clone(self):
        self.w.make(base=False)
        self.w.mark()
        self.w.files[os.path.join(self.w.ws_dir(), "wk-tools", ".git", "HEAD")] = "ref: refs/heads/topic\n"
        self.assertEqual("topic", self.w.driver.branch("ws"))

    def test_a_workspace_on_no_snapshot_does_not_hold_back_the_snapshots_gc_takes(self):
        self.w.make(base=False)
        self.w.mark()
        self.w.make("old", base=False)
        self.w.dirs.add(os.path.dirname(self.w.ws_dir()))
        snaps = Snapshots(self.w.driver.store, self.w)
        self.assertEqual(["old"], snaps.unpinned())

    def test_inside_the_workspace_the_marker_names_it(self):
        w = self.make_world(kinds={"fakebox": "local"})
        (w.tmp / "home" / ".wk-workspace").write_text("name=ws\nsrc=/src/wk-tools\nrepo=wk-tools\n")
        self.assertEqual(TOOLS, places.LocalWorkspace("fakebox", str(REPO), w.env, w).repo("ws").name)

    def test_ls_names_the_repo_as_its_base(self):
        from tests.support import load_cmd
        self.w.make(base=False)
        self.w.mark()
        self.assertEqual(TOOLS, load_cmd("ls").base_of(self.w.driver, "ws"))


class TestStatus(WorkspaceTest):
    world_class = ToolsWorld

    def status(self, origin):
        self.w.checkout = Result(0, "origin=%s\nwsbase=main\n" % origin)
        walk = status.Walk(REPO, env=self.w.env, reg=self.w.reg, fleet=False, devices=False, clock=self.w.clock)
        rec, _ = walk.workspace(self.w.driver, "here", "native", "ws", self.w.records)
        return rec.get("base"), [n["text"] for n in rec.get("notes", []) if n["text"].startswith("origin is")]

    def test_an_origin_other_than_the_repos_is_named(self):
        self.w.make()
        self.assertEqual(("main", []), self.status(dict(project.get("REMOTES"))["origin"]))
        self.assertEqual(1, len(self.status(ORIGIN)[1]))

    def test_a_cloned_repo_is_held_to_its_own_origin_and_named_as_its_base(self):
        self.w.make(base=False)
        self.w.mark()
        self.assertEqual((TOOLS, []), self.status(ORIGIN))


class ToolsDriver(test_sync.SyncDriver):
    def repo(self, ws):
        return tools()

    def src(self, ws):
        return tools().src


class TestSync(test_sync.SyncTest):
    def setUp(self):
        super().setUp()
        for name, fn in (("origin", lambda r, machine, root: ORIGIN if not r.snapshot else "u1"),
                         ("push_url", lambda r, machine, root: PUSH)):
            p = mock.patch.object(repos.Repo, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.w.reg = test_sync.FakeRegistry(self.w.env, self.w, lambda n, env: ToolsDriver(n, str(REPO), env, self.w, "container"),
                                            names=["container"])
        self.w.workspaces["container"] = ["tools"]

    def fetch(self, fix=False):
        driver = self.w.reg.load("container")
        return self.stderr(lambda: self.w.sync("ws", "tools", fix=fix).fetch_one(driver, "tools"))[0]

    def test_it_fetches_its_origin_and_checks_nothing_of_the_mirror(self):
        self.w.fetched["tools"] = Result(0, "from=github\nfetch=0\ncheck=0\n")
        code, text = self.fetch()
        self.assertEqual("ok", code, text)
        (script,) = [e[1][-1] for e in self.w.effects if e[0] == "run" and e[1][0] == "exec"]
        self.assertIn(ORIGIN, script)
        self.assertNotIn("CHECK", script)

    def test_fix_points_origin_back_at_the_tools_origin_and_not_the_wiring(self):
        self.w.fetched["tools"] = Result(0, "from=github\nfetch=0\ncheck=0\n")
        code, _ = self.fetch(fix=True)
        self.assertEqual("ok", code)
        runs = [e[1] for e in self.w.effects if e[0] == "run"]
        self.assertIn(("exec", "container", "tools", "git", "-C", "/src/wk-tools", "remote", "set-url", "origin", ORIGIN), runs)
        self.assertIn(("exec", "container", "tools", "git", "-C", "/src/wk-tools", "remote", "set-url", "--push", "origin", PUSH), runs)
        self.assertFalse([s for s in self.w.steps if s.startswith(("WIRING", "GITWEBKIT"))])


    def test_inside_it_no_mirror_refresh_is_asked_for(self):
        with open(self.w.env["WK_MARKER"], "w") as f:
            f.write("name=tools\nrepo=wk-tools\n")
        self.w.reg.env["WK_PLACE"] = "container"
        self.w.fetched["tools"] = Result(0, "from=github\nfetch=0\ncheck=0\n")
        with mock.patch.object(sync.Sync, "mirror_refresh_request") as ask:
            rc, _ = self.stderr(self.w.sync("ws", "tools").sync_one)
        self.assertEqual(0, rc)
        ask.assert_not_called()


class TestTheCloneCheckScript(unittest.TestCase):
    def test_a_matching_origin_and_push_url_check_and_each_other_one_is_named(self):
        with scratch_dir(prefix="wk-clone-check-") as d:
            up = d / "up"
            git_run("init", "-q", "-b", "main", str(up), cwd=str(d))
            git_commit(up, "one")
            git_run("clone", "-q", str(up), str(d / "co"), cwd=str(d))
            git_run("remote", "set-url", "--push", "origin", PUSH, cwd=str(d / "co"))
            run = lambda origin, push: subprocess.run(["sh", "-c", sync.clone_fetch_and_check_script(str(d / "co"), origin, push)],
                                                      capture_output=True, text=True).stdout
            self.assertIn("check=0", run(str(up), PUSH))
            out = run(ORIGIN, PUSH)
            self.assertIn("check=1", out)
            self.assertIn("problem: origin is %s, not %s" % (up, ORIGIN), out)
            out = run(str(up), "git@elsewhere:x.git")
            self.assertIn("check=1", out)
            self.assertIn("problem: origin pushes to %s, not git@elsewhere:x.git" % PUSH, out)


class TestFirstRun(unittest.TestCase):
    """container/firstrun.sh's checkout block, lifted and run: a cloned repo is cloned at its branch, pushes through its
    deploy key's alias, and is never given the snapshot's wiring."""
    TEXT = (REPO / "container" / "firstrun.sh").read_text()
    BLOCK = TEXT[TEXT.index('if [ -n "${WK_CLONE:-}" ]'):TEXT.index("    # Through ensure-bridge.sh")] + "fi\n"

    def test_the_branch_is_cloned_with_its_push_url_and_the_wiring_never_runs(self):
        with scratch_dir(prefix="wk-firstrun-clone-") as d:
            up = d / "up"
            git_run("init", "-q", "-b", "main", str(up), cwd=str(d))
            git_commit(up, "one")
            git_run("branch", BRANCH, cwd=str(up))
            (d / "src").mkdir()
            harness = ("set -eu\nSRC=%s\nlog() { printf '[firstrun] %%s\\n' \"$*\"; }\nwarn() { log \"$*\"; }\n"
                       "_git_py() { touch %s; }\n" % (d / "src", d / "wired"))
            cp = bash(harness + self.BLOCK, env={"WK_CLONE": str(up), "WK_BRANCH": BRANCH, "WK_PUSH": PUSH})
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            src = str(d / "src")
            self.assertEqual(BRANCH, git_run("symbolic-ref", "--short", "HEAD", cwd=src).stdout.strip())
            self.assertEqual(PUSH, git_run("config", "remote.origin.pushurl", cwd=src).stdout.strip())
            self.assertFalse((d / "wired").exists())


if __name__ == "__main__":
    unittest.main()
