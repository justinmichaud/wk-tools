"""Where a checkout fetches from, per place: Driver.mirror_dir (lib/wk/places.py)."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, GitMirror, WkTest, git_run

sys.path.insert(0, str(REPO / "lib"))
from wk import act, git, images, places, sync  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.clock import Clock  # noqa: E402


def _probe(argv, fake):
    """A build box that answers the probe (home, uname, cores) and names no shared reference checkout in its MOTD."""
    return Result(0, "" if "motd" in argv[-1] else "/home/box\nLinux\n4\n0.1 0 0\n===MEM===\nMemAvailable: 1024 kB\n===IONICE===\nno\n")


def _driver(kind, env=None, system="Darwin"):
    """Asked inside the uname stub: LocalWorkspace reads uname when asked, the only evidence of its kind."""
    fake = Fake("here")
    fake.react(["sh", "-c"], _probe)
    env = dict({"HOME": "/nonexistent", "WK_STORE": "/the/store", "XDG_STATE_HOME": "/state",
                "WK_MARKER": "/nonexistent/marker"}, **(env or {}))
    with mock.patch("wk.places.os.uname", return_value=mock.Mock(sysname=system)), \
         mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname=system)):
        return places.Registry(REPO, env, fake).load(kind).mirror_dir()


class TestEveryDriverNamesOne(WkTest):
    def setUp(self):
        super().setUp()
        # A build box driven without ssh (WK_REMOTE_LOCAL), in a registry of its own.
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        (self.registry / "fakebox.conf").write_text(
            "kind=build\ndriver=remote\nlocal=1\n"
            f"root={self.tmp / 'remote-root'}\n"
            f"store={self.tmp / 'remote-store'}\n")

    def _mirror(self, place):
        if place == "remote":
            return _driver("fakebox", {"WK_MACHINES_DIR": str(self.registry)})
        return _driver(place)

    def test_the_default_is_no_mirror_rather_than_somebody_elses_path(self):
        self.assertEqual("", places.Driver("demo", str(REPO), {}, Fake("here")).mirror_dir())

    def test_the_three_machines_name_three_different_absolute_mirrors(self):
        got = {t: self._mirror(t) for t in ("container", "vm", "remote")}
        self.assertEqual(len(set(got.values())), 3, got)
        self.assertTrue(all(m.startswith("/") for m in got.values()), got)

    def test_a_build_boxs_is_under_its_own_root(self):
        self.assertEqual(str(self.tmp / "remote-root" / "mirror"), self._mirror("remote"))

    def test_a_guests_mirror_is_the_hosts_on_the_share_the_guest_mounts(self):
        self.assertEqual(self._mirror("vm"), places.GUEST_MIRROR_MOUNT + "/mirror/WebKit.git")


class TestAWorkspaceAnswersForTheKindItIs(WkTest):
    """LocalWorkspace runs in both kinds of workspace and each has its mirror somewhere else, so it answers from
    `uname -s`."""

    def test_in_a_container_it_is_the_path_the_container_driver_named(self):
        self.assertEqual("/some/store/git/WebKit.git",
                         _driver("local", {"WK_MIRROR": "/some/store/git/WebKit.git"}, system="Linux"))

    def test_in_a_guest_it_is_the_share_the_vm_driver_names(self):
        self.assertEqual(_driver("vm"), _driver("local"))


class MirrorFixture(WkTest):
    """A mirror made by the real mirror_refresh_script (lib/wk/git.py) out of two local repositories standing in
    for the upstreams -- git takes a path as a URL, so nothing here reaches the network."""

    ENV = {"WK_MIRROR_BRANCHES": "main"}

    def setUp(self):
        super().setUp()
        m = GitMirror(self.tmp)
        self.remotes, self.mirror = m.remotes, m.mirror

    def wire_fetches(self, ws):
        script = git.render(str(ws), git.fetch_config(str(self.mirror), ["main"], self.remotes))
        cp = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestOneMirrorLayoutEverywhere(MirrorFixture):
    """mirror_refresh_script (lib/wk/git.py) makes every mirror in the fleet -- this machine's, a build box's, a
    guest's -- and lib/wk/sync.py's fetch_script is what a workspace fetches from one with, so the emitter and
    the consumer are held to one layout here rather than to two descriptions of it."""

    def test_origins_branches_are_the_mirrors_own_heads_and_forks_are_namespaced(self):
        refs = git_run("for-each-ref", "--format=%(refname)",
                         cwd=self.mirror).stdout.split()
        self.assertIn("refs/heads/main", refs)
        self.assertIn("refs/remotes/fork/side", refs)
        self.assertEqual(
            git_run("symbolic-ref", "HEAD", cwd=self.mirror).stdout.strip(),
            "refs/heads/main")

    def test_it_follows_no_tags_and_is_never_repacked_under_a_sharing_clone(self):
        for key, want in (("remote.origin.tagOpt", "--no-tags"), ("gc.auto", "0")):
            with self.subTest(key=key):
                self.assertEqual(git_run("config", key, cwd=self.mirror).stdout.strip(), want)

    def test_a_wired_checkout_writes_no_commit_graph_on_fetch(self):
        ws = self.tmp / "ws-graph"
        git_run("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "ws-graph", cwd=self.tmp)
        self.wire_fetches(ws)
        for key in ("fetch.writeCommitGraph", "gc.writeCommitGraph"):
            with self.subTest(key=key):
                self.assertEqual(git_run("config", key, cwd=ws).stdout.strip(), "false")

    def test_a_workspace_fetch_against_it_takes_the_mirror_arm(self):
        ws = self.tmp / "ws"
        git_run("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "ws", cwd=self.tmp)
        for remote, bare in (("origin", "up.git"), ("fork", "fk.git")):
            git_run("remote", "remove", remote, cwd=ws, check=False)
            git_run("remote", "add", remote, str(self.tmp / bare), cwd=ws)
        self.wire_fetches(ws)
        for bare in ("up.git", "fk.git"):
            shutil.rmtree(self.tmp / bare)
        out = subprocess.run(["sh", "-c", sync.fetch_script(str(ws), "")], cwd=str(self.tmp),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        refs = git_run("for-each-ref", "--format=%(refname)", cwd=ws).stdout.split()
        self.assertIn("refs/remotes/origin/main", refs)
        self.assertIn("refs/remotes/fork/side", refs)


class TestABranchIsTakenFromTheMirrorFirst(MirrorFixture):
    """`wk build <ws> <branch>` and the babysitter fetch one branch before they build."""

    def _step(self, branch, mirror):
        return git.origin_branch_fetch_step(branch, str(mirror) if mirror else "")

    def _checkout(self):
        ws = self.tmp / "co"
        git_run("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "co", cwd=self.tmp)
        git_run("remote", "set-url", "origin", str(self.tmp / "up.git"), cwd=ws)
        return ws

    def test_a_branch_the_mirror_carries_costs_no_network(self):
        ws = self._checkout()
        git_run("remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=ws)
        out = subprocess.run(["sh", "-c", self._step("main", self.mirror)],
                             cwd=str(ws), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(
            git_run("rev-parse", "refs/remotes/origin/main", cwd=ws).stdout.strip(),
            git_run("rev-parse", "refs/heads/main", cwd=self.mirror).stdout.strip())

    def test_a_branch_it_does_not_carry_is_asked_of_origin(self):
        ws = self._checkout()
        git_run("push", "-q", str(self.tmp / "up.git"), "HEAD:refs/heads/other",
                  cwd=self.tmp / "seed")
        out = subprocess.run(["sh", "-c", self._step("other", self.mirror)],
                             cwd=str(ws), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(
            git_run("rev-parse", "FETCH_HEAD", cwd=ws, check=False).returncode, 0)

    def test_a_target_with_no_mirror_asks_origin_and_nothing_else(self):
        step = self._step("main", "")
        self.assertEqual(step.strip(), "git fetch -q origin main")

class TestWhatTheMirrorCarries(WkTest):
    """mirror_branches (lib/wk/images.py) is what origin is narrowed to, and the narrowing is the point:
    WebKit/WebKit advertises 924 heads."""

    def _branches(self, env=None):
        return images.mirror_branches(dict(env or {}, WK_ROOT=str(REPO)))

    def _configured(self):
        return images.origin_branches({"WK_ROOT": str(REPO)})

    def test_it_carries_main_and_every_configurations_branch_and_nothing_else(self):
        self.assertTrue(self._configured(), "no image configuration names an origin branch")
        self.assertEqual(sorted(self._branches()),
                         sorted({"main", *self._configured()}))

    def test_the_override_replaces_it(self):
        self.assertEqual(self._branches(env={"WK_MIRROR_BRANCHES": "main only/this"}),
                         ["main", "only/this"])


class TestTheContainersMirrorIsTheStores(unittest.TestCase):
    def test_in_the_podman_vm_it_is_the_stores_mirror(self):
        mine = Store({"HOME": "/nonexistent", "WK_STORE": "/the/store", "WK_IN_VM": "1"}).mirror_dir()
        self.assertEqual(mine, _driver("container", {"WK_IN_VM": "1"}))

class TestOneMirrorPerMachine(WkTest):
    """Store.mirror_dir (lib/wk/store.py): a machine keeps one mirror, written where `wk sync` runs."""

    def _ask(self, macos, in_vm=False):
        env = {"WK_STORE": "/var/lib/wk", "XDG_STATE_HOME": str(self.tmp / "state"), "HOME": str(self.tmp)}
        if in_vm:
            env["WK_IN_VM"] = "1"
        with mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname="Darwin" if macos else "Linux")):
            return Store(env).mirror_dir()

    def test_a_macos_host_keeps_it_in_its_state_and_the_podman_vm_and_linux_under_the_store(self):
        for macos, in_vm, want in ((True, False, f"{self.tmp}/state/wk/git/WebKit.git"),
                                   (True, True, "/var/lib/wk/git/WebKit.git"),
                                   (False, False, "/var/lib/wk/git/WebKit.git")):
            with self.subTest(macos=macos, in_vm=in_vm):
                self.assertEqual(self._ask(macos, in_vm), want)

    def test_nothing_fetches_into_the_mirror_from_the_podman_vm(self):
        here = Fake("here")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            sync.fetch_into_mirror(here, Store({"WK_STORE": "/var/lib/wk", "WK_IN_VM": "1"}), None,
                            "https://example/x.git", "refs/heads/b", "refs/remotes/pr/b")
        self.assertIn("the host", err.getvalue())
        self.assertEqual(here.effects, [])


class TestASnapshotBorrowsTheMirrorsObjects(MirrorFixture):
    """lib/wk/sync.py's publish against the fixture mirror and a scratch store: the snapshot it publishes is a
    `--shared` clone, so its objects are the mirror's (an alternates file, no second copy), it is on main
    tracking origin/main, and its completion marker is the mirror's main."""

    def _sync(self):
        """The store's mirror is where the store says (WK_IN_VM: under the store), linked to the fixture's."""
        store = self.tmp / "store"
        (store / "git").mkdir(parents=True)
        os.symlink(str(self.mirror), str(store / "git" / "WebKit.git"))
        p = mock.patch.dict(os.environ, dict(self.ENV, WK_STORE=str(store), WK_IN_VM="1", GIT_TERMINAL_PROMPT="0",
                                             http_proxy="http://127.0.0.1:1", https_proxy="http://127.0.0.1:1"))
        p.start()
        self.addCleanup(p.stop)

        class Here(places.Container):
            def store_init(self):
                pass
        reg = places.Registry(REPO, env=dict(os.environ))
        place = Here("container", str(REPO), dict(os.environ), reg.machine)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            sync.Sync(reg, Clock(), None, "tools", place="container").sync_snapshot(place)
        ids = sorted(d.name for d in (store / "base").iterdir())
        self.assertEqual(len(ids), 1, ids)
        return store, store / "base" / ids[0], err.getvalue()

    def test_the_published_tree_shares_the_mirror_and_is_complete(self):
        store, d, err = self._sync()
        alternates = d / "WebKit" / ".git" / "objects" / "info" / "alternates"
        self.assertTrue(alternates.exists(), "the snapshot copied the history instead of borrowing it")
        self.assertEqual(os.path.realpath(alternates.read_text().strip()), os.path.realpath(str(self.mirror / "objects")))
        self.assertEqual((d / "sha").read_text().strip(),
                         git_run("rev-parse", "refs/heads/main", cwd=self.mirror).stdout.strip())
        self.assertEqual((d / "branch").read_text().strip(), "origin/main")
        self.assertEqual(git_run("symbolic-ref", "--short", "HEAD", cwd=d / "WebKit").stdout.strip(), "main")

if __name__ == "__main__":
    unittest.main()
