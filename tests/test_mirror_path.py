"""Where a checkout fetches from, per place: Driver.mirror_dir (lib/wk/places.py)."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import act, git, images, places, sync  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.clock import Clock  # noqa: E402

DRIVERS = ("container", "vm", "remote", "local")


def _probe(argv, fake):
    """A build box that answers the probe (home, uname, cores) and names no shared reference checkout in its MOTD."""
    return Result(0, "" if "motd" in argv[-1] else "/home/box\nLinux\n4\n0.1 0 0\n===MEM===\nMemAvailable: 1024 kB\n===IONICE===\nno\n")


def _driver(kind, env=None, system="Darwin"):
    """`kind` made over a fake machine on a host whose `uname -s` is `system`; the answer is asked inside the
    stub, since LocalWorkspace reads uname when asked (it is the only evidence a workspace has about its
    kind)."""
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
        if place == "local":
            return _driver("local", system="Linux")
        return _driver(place)

    def test_each_of_the_four_names_a_mirror(self):
        for place in DRIVERS:
            with self.subTest(place=place):
                self.assertTrue(self._mirror(place).startswith("/"),
                                f"{place} names no mirror")

    def test_the_default_is_no_mirror_rather_than_somebody_elses_path(self):
        self.assertEqual("", places.Driver("demo", str(REPO), {}, Fake("here")).mirror_dir())

    def test_the_three_machines_name_three_different_mirrors(self):
        got = {t: self._mirror(t) for t in ("container", "vm", "remote")}
        self.assertEqual(len(set(got.values())), 3, got)

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

    def _git(self, *args, cwd, check=True):
        return subprocess.run(["git", *args], cwd=str(cwd), text=True,
                              capture_output=True, check=check)

    def setUp(self):
        super().setUp()
        # Two upstreams: one standing in for origin (a branch the mirror
        # carries) and one for a fork (namespaced, and not `main`).
        for bare in ("up.git", "fk.git"):
            self._git("init", "-q", "--bare", "-b", "main", bare, cwd=self.tmp)
        seed = self.tmp / "seed"
        self._git("init", "-q", "-b", "main", "seed", cwd=self.tmp)
        (seed / "a").write_text("a\n")
        self._git("add", "a", cwd=seed)
        self._git("-c", "user.email=t@example.com", "-c", "user.name=T",
                  "commit", "-q", "-m", "a", cwd=seed)
        self._git("push", "-q", str(self.tmp / "up.git"), "main", cwd=seed)
        self._git("checkout", "-q", "-b", "side", cwd=seed)
        (seed / "b").write_text("b\n")
        self._git("add", "b", cwd=seed)
        self._git("-c", "user.email=t@example.com", "-c", "user.name=T",
                  "commit", "-q", "-m", "b", cwd=seed)
        self._git("push", "-q", str(self.tmp / "fk.git"), "side", cwd=seed)
        # The fork's own default branch is not the mirror's: WPEWebKit's is
        # `wpe-2.46`, and `git fetch` in a bare repository takes the fetched
        # remote's HEAD as its own (measured, git 2.48.1). A stand-in whose
        # default branch is also `main` hides that.
        self._git("symbolic-ref", "HEAD", "refs/heads/side",
                  cwd=self.tmp / "fk.git")

        # git.REMOTES is the one list of upstreams; stood in for here so nothing
        # reaches github.com, and the rest of the script is the real one.
        self.remotes = (("origin", str(self.tmp / "up.git")), ("fork", str(self.tmp / "fk.git")))
        self.mirror = self.tmp / "m.git"
        cp = subprocess.run(["sh", "-c", git.mirror_refresh_script(str(self.mirror), ["main"], self.remotes)],
                            capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.refresh_out = cp.stdout

    def wire_fetches(self, ws):
        """fetch_config's steps, rendered and run in the checkout."""
        script = git.render(str(ws), git.fetch_config(str(self.mirror), ["main"], self.remotes))
        cp = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestOneMirrorLayoutEverywhere(MirrorFixture):
    """mirror_refresh_script (lib/wk/git.py) makes every mirror in the fleet -- this machine's, a build box's, a
    guest's -- and lib/wk/sync.py's fetch_script is what a workspace fetches from one with, so the emitter and
    the consumer are held to one layout here rather than to two descriptions of it."""

    def test_the_refresh_reports_each_upstream(self):
        self.assertIn("mirror-fetch origin ok", self.refresh_out)
        self.assertIn("mirror-fetch fork ok", self.refresh_out)

    def test_origins_branches_are_the_mirrors_own_heads_and_forks_are_namespaced(self):
        refs = self._git("for-each-ref", "--format=%(refname)",
                         cwd=self.mirror).stdout.split()
        self.assertIn("refs/heads/main", refs)
        self.assertIn("refs/remotes/fork/side", refs)
        self.assertEqual(
            self._git("symbolic-ref", "HEAD", cwd=self.mirror).stdout.strip(),
            "refs/heads/main")

    def test_it_follows_no_tags_and_is_never_repacked_under_a_sharing_clone(self):
        for key, want in (("remote.origin.tagOpt", "--no-tags"), ("gc.auto", "0")):
            with self.subTest(key=key):
                self.assertEqual(self._git("config", key, cwd=self.mirror).stdout.strip(), want)

    def test_a_wired_checkout_writes_no_commit_graph_on_fetch(self):
        ws = self.tmp / "ws-graph"
        self._git("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "ws-graph", cwd=self.tmp)
        self.wire_fetches(ws)
        for key in ("fetch.writeCommitGraph", "gc.writeCommitGraph"):
            with self.subTest(key=key):
                self.assertEqual(self._git("config", key, cwd=ws).stdout.strip(), "false")

    def test_a_workspace_fetch_against_it_takes_the_mirror_arm(self):
        ws = self.tmp / "ws"
        self._git("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "ws", cwd=self.tmp)
        for remote, bare in (("origin", "up.git"), ("fork", "fk.git")):
            self._git("remote", "remove", remote, cwd=ws, check=False)
            self._git("remote", "add", remote, str(self.tmp / bare), cwd=ws)
        self.wire_fetches(ws)
        for bare in ("up.git", "fk.git"):
            shutil.rmtree(self.tmp / bare)
        out = subprocess.run(["sh", "-c", sync.fetch_script(str(ws), "")], cwd=str(self.tmp),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        refs = self._git("for-each-ref", "--format=%(refname)", cwd=ws).stdout.split()
        self.assertIn("refs/remotes/origin/main", refs)
        self.assertIn("refs/remotes/fork/side", refs)


class TestABranchIsTakenFromTheMirrorFirst(MirrorFixture):
    """`wk build <ws> <branch>` and the babysitter fetch one branch before they build."""

    def _step(self, branch, mirror):
        return git.origin_branch_fetch_step(branch, str(mirror) if mirror else "")

    def _checkout(self):
        """A workspace checkout whose origin is a real (local-path) upstream, as a workspace's is after the
        wiring."""
        ws = self.tmp / "co"
        self._git("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "co", cwd=self.tmp)
        self._git("remote", "set-url", "origin", str(self.tmp / "up.git"), cwd=ws)
        return ws

    def test_a_branch_the_mirror_carries_costs_no_network(self):
        ws = self._checkout()
        self._git("remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=ws)
        out = subprocess.run(["sh", "-c", self._step("main", self.mirror)],
                             cwd=str(ws), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(
            self._git("rev-parse", "refs/remotes/origin/main", cwd=ws).stdout.strip(),
            self._git("rev-parse", "refs/heads/main", cwd=self.mirror).stdout.strip())

    def test_a_branch_it_does_not_carry_is_asked_of_origin(self):
        ws = self._checkout()
        self._git("push", "-q", str(self.tmp / "up.git"), "HEAD:refs/heads/other",
                  cwd=self.tmp / "seed")
        out = subprocess.run(["sh", "-c", self._step("other", self.mirror)],
                             cwd=str(ws), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(
            self._git("rev-parse", "FETCH_HEAD", cwd=ws, check=False).returncode, 0)

    def test_a_target_with_no_mirror_asks_origin_and_nothing_else(self):
        step = self._step("main", "")
        self.assertEqual(step.strip(), "git fetch -q origin main")

class TestWhatTheMirrorCarries(WkTest):
    """mirror_branches (lib/wk/git.py) is what origin is narrowed to, and the narrowing is the point:
    WebKit/WebKit advertises 924 heads."""

    def _branches(self, env=None):
        return git.mirror_branches(dict(env or {}, WK_ROOT=str(REPO)))

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

    def test_a_macos_host_keeps_it_in_its_own_state_directory(self):
        self.assertEqual(self._ask(macos=True),
                         f"{self.tmp}/state/wk/git/WebKit.git")

    def test_the_podman_vm_reads_the_hosts_under_its_store(self):
        self.assertEqual(self._ask(macos=True, in_vm=True), "/var/lib/wk/git/WebKit.git")

    def test_a_linux_machine_keeps_it_in_its_store(self):
        self.assertEqual(self._ask(macos=False), "/var/lib/wk/git/WebKit.git")

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
                         self._git("rev-parse", "refs/heads/main", cwd=self.mirror).stdout.strip())
        self.assertEqual((d / "branch").read_text().strip(), "origin/main")
        self.assertEqual(self._git("symbolic-ref", "--short", "HEAD", cwd=d / "WebKit").stdout.strip(), "main")

if __name__ == "__main__":
    unittest.main()
