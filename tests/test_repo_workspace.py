"""`wk new --repo`: the repository a workspace holds is chosen at creation, kept in its marker, and every reader asks it."""
import os
import re
import shlex
import subprocess
import sys
import unittest

from tests import test_sync
from tests.fakes import Result
from tests.support import REPO, bash, git_commit, git_run, scratch_dir
from tests.test_wk_workspace import WK, Flow, World, said

sys.path.insert(0, str(REPO / "lib"))
from wk import repos  # noqa: E402 -- the repo tests/test_killpoints.py's "new tools" row hands the detached run

TOOLS = "wk-tools"
ORIGIN = "https://github.com/someone/wk-tools.git"
BRANCH = "python-core"
PUSH = "git@github-wk-tools:someone/wk-tools.git"


def tools():
    return repos.Repo(TOOLS)


class ToolsWorld(World):
    """This machine's wk-tools is on `head`, its origin answers `origin` and `ls-remote` of it `remote`; wkdev-create's
    first start writes the marker firstrun.sh writes, naming the repo the create flags carry. No mirror and no snapshot."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)
        self.origin, self.head, self.remote = Result(0, "git@github.com:someone/wk-tools.git\n"), Result(0, BRANCH + "\n"), Result(0)
        self.react(["git", "-c", "safe.directory=*", "-C"], lambda argv, f: f.head if "symbolic-ref" in argv else f.origin)
        self.react(["git", "ls-remote"], lambda argv, f: f.remote)
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
        self.files[os.path.join(self.ws_dir(name), "home", ".wk-workspace")] = "name=%s\nrepo=%s\n" % (name, repo)


class TestTheFront(Flow):
    world_class = ToolsWorld

    def test_another_place_is_refused_before_anything_runs(self):
        for kind in ("vm", "remote"):
            w = self.make_world(kinds={"fakebox": kind})
            rc, err = self.new("--repo", TOOLS, w=w)
            self.assertEqual((rc, said(err), [e for e in w.effects if e[0] in ("spawn", "write")]),
                             (1, "error: --repo wk-tools is container-only for now (this is place 'fakebox'): a guest or a build", []), kind)

    def test_an_unknown_repo_is_refused_naming_the_known_ones(self):
        rc, err = self.new("--repo", "nope")
        self.assertEqual((rc, said(err), self.spawned()), (1, "error: unknown repo 'nope' -- the known ones: webkit wk-tools", []))

    def test_the_detached_run_is_handed_the_repo(self):
        self.assertEqual(self.new("--repo", TOOLS, "--no-wait")[0], 0)
        (spawn,) = self.spawned()
        self.assertEqual(TOOLS, spawn[1][spawn[1].index("--repo") + 1])

    def test_kill_takes_no_repo(self):
        rc, err = self.new("--kill", "--repo", TOOLS)
        self.assertEqual((rc, said(err)), (1, "error: 'wk new ws --kill' stops the creation already running and takes"))


class TestCreation(Flow):
    world_class = ToolsWorld

    def made(self, w=None):
        return self.detach("--repo", TOOLS, w=w)

    def create_flags(self, w=None):
        return [f for argv in self.created(w) for f in argv[argv.index("--additional-flags") + 1].split()]

    def test_it_is_made_with_no_mirror_and_no_snapshot_and_a_default_one_is_not(self):
        rc, err = self.detach()
        self.assertEqual((rc, said(err), self.created()), (1, "error: no snapshot this machine can build a workspace from:  wk sync", []))
        self.assertEqual(said(self.made()[1]), "==> workspace 'ws' created")

    def test_nothing_of_the_mirror_or_the_snapshot_is_asked_for_and_its_checkout_is_reported_not_fetched(self):
        rc, err = self.made()
        self.assertEqual((rc, self.runs(head=WK), "lock store" in self.lock_takes()), (0, [], False))
        self.assertNotIn(os.path.join(self.w.ws_dir(), "base-id"), self.w.files)
        self.assertFalse([f for f in self.create_flags() if f.startswith("WK_MIRROR=") or ":O,upperdir=" in f])
        self.assertIn("==> 'ws' is on main at abc1234, up to date with origin/main", err.splitlines())

    def test_a_dry_run_asks_no_fetch_of_it_and_does_of_a_default_one(self):
        self.dry_run()
        self.made()
        default = World(self.tmp)
        self.detach(w=default)
        self.assertEqual([(WK, "sync", "ws") in w.acted for w in (self.w, default)], [False, True])

    def test_the_checkout_is_its_own_directory_cloned_from_the_tools_origin(self):
        self.made()
        flags = self.create_flags()
        for flag in ("%s/wk-tools:/src/wk-tools" % self.w.ws_dir(), "WK_CLONE=" + ORIGIN, "WK_BRANCH=" + BRANCH, "WK_PUSH=" + PUSH, "WK_REPO=wk-tools"):
            self.assertIn(flag, flags)
        self.assertIn(os.path.join(self.w.ws_dir(), "wk-tools"), self.w.dirs)

    def test_every_github_spelling_of_the_origin_is_cloned_over_https(self):
        for url in ("git@github.com:someone/wk-tools.git", "ssh://git@github.com/someone/wk-tools",
                    "https://github.com/someone/wk-tools.git", "https://github.com/someone/wk-tools/"):
            w = self.make_world()
            w.origin = Result(0, url + "\n")
            self.assertEqual((self.made(w=w)[0], "WK_CLONE=" + ORIGIN in self.create_flags(w)), (0, True), url)

    def test_an_origin_elsewhere_or_none_is_refused(self):
        for answer, origin in ((Result(0, "https://gitlab.com/someone/wk-tools.git\n"), "'https://gitlab.com/someone/wk-tools.git'"), (Result(2), "not set")):
            w = self.make_world()
            w.origin = answer
            rc, err = self.made(w=w)
            self.assertEqual((rc, said(err), self.created(w)), (1, "error: a wk-tools workspace is cloned from the GitHub origin of this machine's wk-tools (/opt/wk-tools),", []))
            self.assertIn("    and its origin is " + origin, err.splitlines())

    def test_the_branch_this_machines_wk_tools_is_on_must_be_on_the_origin(self):
        for head, remote, line, remedy in (
                (Result(128, "", "fatal: ref HEAD is not a symbolic ref"), Result(0),
                 "error: this machine's wk-tools (/opt/wk-tools) is on no branch, and a wk-tools workspace checks out the one it is on:", "    git -C /opt/wk-tools switch <branch>"),
                (Result(0, BRANCH + "\n"), Result(2),
                 "error: branch '%s' of this machine's wk-tools is not on %s, so a wk-tools workspace cannot check it out:" % (BRANCH, ORIGIN),
                 "    git -C /opt/wk-tools push -u origin " + BRANCH),
                (Result(0, BRANCH + "\n"), Result(128, "", "fatal: unable to access"),
                 "error: could not ask %s for branch '%s': fatal: unable to access" % (ORIGIN, BRANCH), None)):
            w = self.make_world()
            w.head, w.remote = head, remote
            rc, err = self.made(w=w)
            self.assertEqual((rc, said(err), self.created(w)), (1, line, []))
            if remedy:
                self.assertIn(remedy, err.splitlines())

    def test_once_made_its_record_ends_ok_and_status_reads_its_checkout(self):
        self.made()
        (t,) = self.w.records.list()
        self.w.effects = []
        rc, out, _ = self.wk(self.w, "status", "ws")
        self.assertEqual((t.verdict(), rc, row(out)), ("ok", 0, ["ws", "running", "-", "wk-tools", "clean"]))
        self.assertTrue([a for a in self.runs(head="exec") if "/src/wk-tools" in a])

    def test_the_agents_are_installed_in_its_checkout(self):
        self.made()
        configs = [a[-1] for a in self.runs(head="exec") if "workspace-config.py" in a[-1]]
        self.assertTrue(configs)
        self.assertTrue(all(c.endswith(" /src/wk-tools") for c in configs), configs)


def row(out, ws="ws"):
    """`wk status`'s row for workspace `ws`, by its columns."""
    return next(l.split() for l in out.splitlines() if l.split()[:1] == [ws])


class TestReaders(Flow):
    world_class = ToolsWorld

    def setUp(self):
        super().setUp()
        self.w.make(base=False)
        self.w.mark()

    def test_status_reads_its_branch_from_the_clone_and_names_the_repo_as_its_base_held_to_its_own_origin(self):
        self.w.files[os.path.join(self.w.ws_dir(), "wk-tools", ".git", "HEAD")] = "ref: refs/heads/topic\n"
        self.w.checkout = Result(0, "origin=%s\nwsbase=main\n" % ORIGIN)
        rc, out, _ = self.wk(self.w, "status", "ws")
        self.assertEqual((rc, row(out), [l for l in out.splitlines() if "! origin" in l]), (0, ["ws", "running", "topic", "wk-tools", "clean"], []))

    def test_status_names_a_default_ones_origin_other_than_upstream(self):
        w = World(self.tmp)
        w.make()
        for origin, notes in (("https://github.com/WebKit/WebKit.git", []), (ORIGIN, ["      ! origin is %s, not upstream -- 'wk sync ws --fix'" % ORIGIN])):
            w.checkout = Result(0, "origin=%s\nwsbase=main\n" % origin)
            rc, out, _ = self.wk(w, "status", "ws")
            self.assertEqual((rc, row(out)[3], [l for l in out.splitlines() if "! origin" in l]), (0, "main", notes), origin)

    def test_ls_names_the_repo_as_its_base_and_a_marker_naming_none_its_snapshot(self):
        self.w.make("old")
        self.w.mark("old", "")
        self.w.answer(["podman", "ps"], out="wk-ws\tUp 2 hours\nwk-old\tUp 1 hour\n")
        self.w.checkout = Result(0, "branch=main\nwsbase=main\n")
        rc, out, _ = self.wk(self.w, "ls")
        self.assertEqual((rc, [l.split()[:4] for l in out.splitlines()[1:]]), (0, [["old", "fakebox", "running", "main"], ["ws", "fakebox", "running", "wk-tools"]]))

    def test_a_workspace_on_no_snapshot_does_not_hold_back_the_snapshots_gc_takes(self):
        for old, line in ((True, "snapshots kept -- old never recorded a snapshot, so any snapshot may be under it: 'wk new <name>' remakes one, 'wk rm <name>' removes it"),
                          (False, "snapshot main-0, no workspace on it goes")):
            w = self.make_world(kinds={"fakebox": "container", "container": "container"})
            w.make(base=False)
            w.mark()
            w.dirs.add(os.path.dirname(w.ws_dir()))
            if old:
                w.make("old", base=False)
            w.publish("main-0")
            w.publish("main-1")
            rc, out, err = self.wk(w, "gc")
            self.assertEqual([" ".join(l.split()[1:]) for l in err.splitlines() if l[11:].startswith("snapshot")], [line], old)


class Clone(test_sync.Host):
    """A container workspace `tools` holding wk-tools, whose checkout is the real git clone `co`: the fetch and the check
    run there."""

    def __init__(self, tmp, co=None, **kw):
        super().__init__(tmp, **kw)
        self.co = co
        self.make("tools")
        self.files[os.path.join(self.store.ws_dir("tools"), "home", ".wk-workspace")] = "repo=wk-tools\n"
        self.answer(["git", "-c", "safe.directory=*", "-C", str(REPO), "remote", "get-url", "origin"], out="git@github.com:someone/wk-tools.git\n")

    def in_ws(self, ws, argv):
        if self.co and argv[:2] == ["sh", "-c"] and "echo check=$check" in argv[2]:
            cp = subprocess.run(["sh", "-c", argv[2].replace("cd /src/wk-tools ", "cd %s " % shlex.quote(str(self.co)), 1)], capture_output=True, text=True)
            return Result(cp.returncode, cp.stdout, cp.stderr)
        return super().in_ws(ws, argv)


class TestSync(test_sync.Flow):
    world_class = Clone

    def test_inside_it_no_mirror_refresh_is_asked_for(self):
        w = self.make_world(kinds={"local": "local"}, in_vm=False)
        with open(w.env["WK_MARKER"], "w") as f:
            f.write("name=tools\nsrc=/src/wk-tools\nrepo=wk-tools\n")
        w.env["WK_BROKER_SOCKET"] = "/run/x.sock"
        w.answer(["test", "-S"])
        w.files[test_sync.CLIENT] = ""
        rc, _ = self.sync(w=w)
        self.assertEqual((rc, [e for e in w.effects if test_sync.CLIENT in e[1]], w.steps), (0, [], ["fetch ws"]))

    def test_its_fetch_reads_its_origin_and_its_check_names_an_origin_or_a_push_url_not_its_own(self):
        """Run in a real clone of a local upstream; GitHub, which a test does not reach, is skipped by `fetch --all`."""
        with scratch_dir(prefix="wk-clone-check-") as d:
            up, co = d / "up", d / "co"
            git_run("init", "-q", "-b", "main", str(up), cwd=str(d))
            git_commit(up, "one")
            git_run("clone", "-q", str(up), str(co), cwd=str(d))
            git_run("remote", "set-url", "--push", "origin", PUSH, cwd=str(co))
            git_commit(up, "two")
            ok, wrong = "  %-24s ok  (over the network: this checkout reads no mirror)" % "tools", " -- wired wrong:"
            for setup, rc, lines in (((), 1, [ok + wrong, "    - origin is %s, not %s" % (up, ORIGIN)]),
                                     ((("remote", "set-url", "--push", "origin", "git@elsewhere:x.git"),), 1,
                                      [ok + wrong, "    - origin is %s, not %s" % (up, ORIGIN), "    - origin pushes to git@elsewhere:x.git, not " + PUSH]),
                                     ((("remote", "set-url", "origin", ORIGIN), ("remote", "set-url", "--push", "origin", PUSH),
                                       ("config", "remote.origin.skipFetchAll", "true")), 0, [ok])):
                for args in setup:
                    git_run(*args, cwd=str(co))
                w = self.make_world(co=co)
                got, err = self.sync("tools", w=w)
                i = err.splitlines().index(lines[0])
                self.assertEqual((got, err.splitlines()[i:i + len(lines)]), (rc, lines), setup)
            self.assertEqual(git_run("rev-parse", "origin/main", cwd=str(co)).stdout, git_run("rev-parse", "HEAD", cwd=str(up)).stdout)


class TestFirstRun(unittest.TestCase):
    """container/firstrun.sh's checkout block, lifted and run: a cloned repo is cloned at its branch, pushes through its
    deploy key's alias, and is never given the snapshot's wiring."""
    TEXT = (REPO / "container" / "firstrun.sh").read_text()
    BLOCK = TEXT[TEXT.index('if [ -n "${WK_CLONE:-}" ]'):TEXT.index("    _setup=$(_git_py pr-tool-setup-script")] + "fi\n"

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
