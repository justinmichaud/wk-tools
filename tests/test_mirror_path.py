"""Where a checkout fetches from, per target: Target.mirror_dir (lib/wk/targets.py).

A fetch in a workspace is either local -- against a bare mirror that already
carries every upstream -- or four fetches of four upstreams over that
workspace's egress. Which one it is depends on where that target's mirror is:
a machine keeps one, and a container sees it at the machine's own path
(bind-mounted read-only, named in its environment), a tart guest at the share
the host mounts in, a build box under its own root. A command that spells one
of those takes the network arm elsewhere and pays ~1,500 remote heads' worth
of negotiation per fetch.

So each driver names its own mirror once and every command asks the driver.
This holds each of the four to naming one, and holds the commands to asking
rather than spelling it again.

Hermetic: the drivers are made over a fake machine and asked. No container,
guest, machine or network.

Run: python3 -m unittest tests.test_mirror_path -v
"""
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
from wk import act, git, images, pr, sync, targets  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.clock import Clock  # noqa: E402

DRIVERS = ("container", "vm", "remote", "local")


def _probe(argv, fake):
    """A build box that answers the probe (home, uname, cores) and names no shared reference checkout in its MOTD."""
    return Result(0, "" if "motd" in argv[-1] else "/home/box\nLinux\n4\n0.1 0 0\n===MEM===\nMemAvailable: 1024 kB\n===IONICE===\nno\n")


def _driver(kind, env=None, system="Darwin"):
    """`kind` made over a fake machine on a host whose `uname -s` is `system`; the answer is asked inside the stub,
    since LocalWorkspace reads uname when asked (it is the only evidence a workspace has about its kind)."""
    fake = Fake("here")
    fake.react(["sh", "-c"], _probe)
    env = dict({"HOME": "/nonexistent", "WK_STORE": "/the/store", "XDG_STATE_HOME": "/state",
                "WK_MARKER": "/nonexistent/marker"}, **(env or {}))
    with mock.patch("wk.targets.os.uname", return_value=mock.Mock(sysname=system)), \
         mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname=system)):
        return targets.Registry(REPO, env, fake).load(kind).mirror_dir()


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

    def _mirror(self, target):
        if target == "remote":
            return _driver("fakebox", {"WK_MACHINES_DIR": str(self.registry)})
        if target == "local":
            return _driver("local", system="Linux")
        return _driver(target)

    def test_each_of_the_four_names_a_mirror(self):
        """The default is empty -- a driver that has one says so, and one that
        does not makes every fetch in it a network fetch. All four have one."""
        for target in DRIVERS:
            with self.subTest(target=target):
                self.assertTrue(self._mirror(target).startswith("/"),
                                f"{target} names no mirror")

    def test_the_default_is_no_mirror_rather_than_somebody_elses_path(self):
        """Inheriting a path would give a new driver a mirror it does not have,
        and a fetch against a directory that is not there."""
        self.assertEqual("", targets.Target("demo", str(REPO), {}, Fake("here")).mirror_dir())

    def test_the_three_machines_name_three_different_mirrors(self):
        """A container's is this machine's, bind-mounted at its own path; a
        guest's is the host's, on the share macOS automounts; a build box's is
        on the box. No two of them are the same path."""
        got = {t: self._mirror(t) for t in ("container", "vm", "remote")}
        self.assertEqual(len(set(got.values())), 3, got)

    def test_a_build_boxs_is_under_its_own_root(self):
        self.assertEqual(str(self.tmp / "remote-root" / "mirror"), self._mirror("remote"))

    def test_a_guests_mirror_is_the_hosts_on_the_share_the_guest_mounts(self):
        """macOS automounts every tart share under one directory, so the path
        is the share's name and nothing the guest holds."""
        mirror = self._mirror("vm")
        self.assertEqual(mirror, "/Volumes/My Shared Files/mirror/WebKit.git")
        self.assertIn('"--dir=%s:%s:ro" % (vm.mirror_share, os.path.dirname(vm.store.mirror()))',
                      (REPO / "lib" / "wk" / "guest.py").read_text(),
                      "the guest is not booted with the mirror share")


class TestAWorkspaceAnswersForTheKindItIs(WkTest):
    """LocalWorkspace runs in both kinds of workspace and each has its
    mirror somewhere else, so it answers from `uname -s`."""

    def test_in_a_container_it_is_the_path_the_container_driver_named(self):
        """The container is created with WK_MIRROR set to its driver's answer (tests/test_wk_targets.py)."""
        self.assertEqual("/some/store/git/WebKit.git",
                         _driver("local", {"WK_MIRROR": "/some/store/git/WebKit.git"}, system="Linux"))

    def test_in_a_guest_it_is_the_share_the_vm_driver_names(self):
        self.assertEqual(_driver("vm"), _driver("local"))


class MirrorFixture(WkTest):
    """A mirror made by the real mirror_refresh_script (lib/wk/git.py) out of
    two local repositories standing in for the upstreams -- git takes a path
    as a URL, so nothing here reaches the network.

    The stand-in origin carries `main` alone, so the branch list is pinned to
    it: what mirror_branches derives from this checkout's image
    configurations is TestWhatTheMirrorCarries's question, not the layout's."""

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
    """mirror_refresh_script (lib/wk/git.py) makes every mirror in the fleet --
    this machine's, a build box's, a guest's -- and lib/wk/sync.py's fetch_script
    is what a workspace fetches from one with, so the emitter and the consumer
    are held to one layout here rather than to two descriptions of it."""

    def test_the_refresh_reports_each_upstream(self):
        self.assertIn("mirror-fetch origin ok", self.refresh_out)
        self.assertIn("mirror-fetch fork ok", self.refresh_out)

    def test_origins_branches_are_the_mirrors_own_heads_and_forks_are_namespaced(self):
        """`git clone` copies refs/heads and ignores refs/remotes, which is why
        the asymmetry exists: a snapshot or a `--shared` clone taken from this
        starts on main, and a fork's branch is still reachable."""
        refs = self._git("for-each-ref", "--format=%(refname)",
                         cwd=self.mirror).stdout.split()
        self.assertIn("refs/heads/main", refs)
        self.assertIn("refs/remotes/fork/side", refs)
        self.assertEqual(
            self._git("symbolic-ref", "HEAD", cwd=self.mirror).stdout.strip(),
            "refs/heads/main")

    def test_it_follows_no_tags(self):
        self.assertEqual(
            self._git("config", "remote.origin.tagOpt", cwd=self.mirror).stdout.strip(),
            "--no-tags")

    def test_nothing_repacks_it_under_a_clone_that_shares_its_objects(self):
        self.assertEqual(
            self._git("config", "gc.auto", cwd=self.mirror).stdout.strip(), "0")

    def test_a_wired_checkout_writes_no_commit_graph_on_fetch(self):
        """git-webkit setup fetches every remote in parallel, and two fetches
        writing the commit graph at once collide on its lock, which failed
        the setup in every fresh container (measured 2026-09-12)."""
        ws = self.tmp / "ws-graph"
        self._git("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "ws-graph", cwd=self.tmp)
        self.wire_fetches(ws)
        for key in ("fetch.writeCommitGraph", "gc.writeCommitGraph"):
            with self.subTest(key=key):
                self.assertEqual(self._git("config", key, cwd=ws).stdout.strip(), "false")

    def test_a_workspace_fetch_against_it_takes_the_mirror_arm(self):
        """The pair under test: a checkout made the way a guest's and a build
        box's are (`--shared` off the mirror) and wired by fetch_config
        fetches every upstream the mirror carries in one local fetch. The
        upstreams are deleted first, so a fetch that reaches one fails."""
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
    """`wk build <ws> <branch>` and the babysitter fetch one branch before
    they build. origin_branch_fetch_step (lib/wk/git.py) is that fetch: the
    mirror when it carries the branch, origin when it does not -- WebKit has
    ~920 branches and a mirror carries the handful mirror_branches names,
    so both arms are real."""

    def _step(self, branch, mirror):
        return git.origin_branch_fetch_step(branch, str(mirror) if mirror else "")

    def _checkout(self):
        """A workspace checkout whose origin is a real (local-path) upstream,
        as a workspace's is after the wiring."""
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
        """The mirror has `side` only under refs/remotes/fork, so what a
        `git fetch origin side` means here is a fetch from origin."""
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

    def test_both_callers_use_it(self):
        text = (REPO / "lib" / "wk" / "build.py").read_text()
        self.assertIn("git.origin_branch_fetch_step(", text)
        self.assertNotIn("git fetch -q origin", text)


class TestWhatTheMirrorCarries(WkTest):
    """mirror_branches (lib/wk/git.py) is what origin is narrowed to, and
    the narrowing is the point: WebKit/WebKit advertises 924 heads. An image
    workspace reads its release branch from the mirror and from nowhere else
    (lib/wk/sysimage/yocto.py), so the list is main plus the branch of every image
    configuration this checkout defines on origin -- derived from the
    configurations, never a second list to keep in step with them."""

    def _branches(self, env=None):
        return git.mirror_branches(dict(env or {}, WK_ROOT=str(REPO)))

    def _configured(self):
        return images.origin_branches({"WK_ROOT": str(REPO)})

    def test_main_is_always_carried(self):
        self.assertIn("main", self._branches())

    def test_every_origin_configurations_branch_is_carried(self):
        got = self._branches()
        configured = self._configured()
        self.assertTrue(configured, "no image configuration names an origin branch")
        for branch in configured:
            with self.subTest(branch=branch):
                self.assertIn(branch, got)

    def test_it_carries_no_branch_no_configuration_names(self):
        self.assertEqual(sorted(self._branches()),
                         sorted({"main", *self._configured()}))

    def test_another_upstreams_branch_is_not_one_of_them(self):
        """Only origin is narrowed; every other upstream is mirrored whole
        (fetch_refspecs), so a wpe-* branch has nothing to be added to."""
        for branch in self._branches():
            self.assertFalse(branch.startswith("wpe-"), branch)

    def test_the_override_replaces_it(self):
        self.assertEqual(self._branches(env={"WK_MIRROR_BRANCHES": "main only/this"}),
                         ["main", "only/this"])


class TestTheCommandsAskTheDriver(unittest.TestCase):
    """Every command that fetches a workspace's mirror asks Target.mirror_dir;
    only the drivers name a path. One path spelled into several commands is
    fixed in one of them and wrong in the rest."""

    # lib/wk/git.py takes the mirror directory as an argument from those
    # callers and never resolves one itself, and `wk pr` fetches the one ref
    # from the upstream rather than through any mirror -- so neither has a
    # mirror_dir call to make, and both are still held to spelling no path.
    SPELLS_NO_PATH = ("lib/wk/store.py", "lib/wk/git.py", "lib/wk/pr.py", "cmd/pr", "lib/wk/workspace.py",
                      "lib/wk/sync.py", "lib/wk/build.py")

    def test_no_command_spells_a_mirror_path_of_its_own(self):
        for rel in self.SPELLS_NO_PATH:
            with self.subTest(file=rel):
                self.assertNotIn("/mirror/WebKit.git", (REPO / rel).read_text(),
                                 f"{rel} names a container's mirror itself")

    def test_every_command_that_fetches_a_mirror_asks_the_driver_for_it(self):
        self.assertIn("target.mirror_dir()", (REPO / "lib" / "wk" / "workspace.py").read_text(),
                      "wk new fetches without asking the driver")
        self.assertIn("target.mirror_dir()", (REPO / "lib" / "wk" / "sync.py").read_text(),
                      "wk sync fetches without asking the driver")
        self.assertIn("self.target.mirror_dir()", (REPO / "lib" / "wk" / "build.py").read_text(),
                      "wk build fetches without asking the driver")

    def test_the_in_workspace_builders_read_the_drivers_answer_from_the_environment(self):
        for rel in ("yocto_target.py", "buildroot_target.py"):
            text = (REPO / "lib" / "wk" / "sysimage" / rel).read_text()
            with self.subTest(file=rel):
                self.assertIn('self.env.get("WK_MIRROR")', text)
                self.assertNotIn("/mirror/WebKit.git", text)

    def test_each_mirror_path_is_spelled_in_exactly_one_place(self):
        """A driver *answers* for a mirror; it does not spell one. Two of the
        four share each answer -- the driver that mounts the mirror in, and
        LocalWorkspace answering from inside a workspace of that kind -- so
        the guest's is one constant and the container's is Store.mirror."""
        text = (REPO / "lib" / "wk" / "targets.py").read_text()
        self.assertEqual(1, text.count("/mirror/WebKit.git"), "the guest mirror is spelled twice")
        self.assertEqual((targets.GUEST_MIRROR, targets.GUEST_MIRROR), (_driver("vm"), _driver("local")))
        mine = Store({"HOME": "/nonexistent", "WK_STORE": "/the/store", "WK_IN_VM": "1"}).mirror()
        self.assertEqual(mine, _driver("container", {"WK_IN_VM": "1"}))

    def test_only_a_driver_names_a_path(self):
        named = sorted(n for n, c in vars(targets).items() if isinstance(c, type) and "mirror_dir" in vars(c))
        self.assertEqual(["Container", "LocalWorkspace", "Remote", "Target", "Vm"], named)
        others = [p.relative_to(REPO).as_posix() for p in sorted((REPO / "lib" / "wk").rglob("*.py"))
                  if p.name != "targets.py" and "def mirror_dir(" in p.read_text()]
        self.assertEqual([], others)


class TestOneMirrorPerMachine(WkTest):
    """Store.mirror (lib/wk/store.py): a machine keeps one mirror,
    written where `wk sync` runs. On a macOS host that is the host's own state
    directory, which the podman VM mounts read-only at its store's git/ and
    every tart guest mounts as a share; in the VM the same bytes are read
    under $WK_STORE and never written. Elsewhere the store is the machine's
    own and the mirror sits in it."""

    def _ask(self, macos, in_vm=False):
        env = {"WK_STORE": "/var/lib/wk", "XDG_STATE_HOME": str(self.tmp / "state"), "HOME": str(self.tmp)}
        if in_vm:
            env["WK_IN_VM"] = "1"
        with mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname="Darwin" if macos else "Linux")):
            return Store(env).mirror()

    def test_a_macos_host_keeps_it_in_its_own_state_directory(self):
        self.assertEqual(self._ask(macos=True),
                         f"{self.tmp}/state/wk/git/WebKit.git")

    def test_the_podman_vm_reads_the_hosts_under_its_store(self):
        self.assertEqual(self._ask(macos=True, in_vm=True), "/var/lib/wk/git/WebKit.git")

    def test_a_linux_machine_keeps_it_in_its_store(self):
        self.assertEqual(self._ask(macos=False), "/var/lib/wk/git/WebKit.git")

    def test_nothing_fetches_into_the_mirror_from_the_podman_vm(self):
        """A pull request head is fetched into the mirror (`wk ab`), and the
        mount in the VM is read-only: refused with the machine that can, before
        any lock or fetch."""
        here = Fake("here")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            pr.mirror_fetch(here, Store({"WK_STORE": "/var/lib/wk", "WK_IN_VM": "1"}), None,
                            "https://example/x.git", "refs/heads/b", "refs/remotes/pr/b")
        self.assertIn("the host", err.getvalue())
        self.assertEqual(here.effects, [])


class TestASnapshotBorrowsTheMirrorsObjects(MirrorFixture):
    """lib/wk/sync.py's publish against the fixture mirror and a scratch
    store: the snapshot it publishes is a `--shared` clone, so its objects
    are the mirror's (an alternates file, no second copy), it is on main
    tracking origin/main, and its completion marker is the mirror's main."""

    def _sync(self, mirror=True):
        """The store's mirror is where the store says (WK_IN_VM: under the store), linked to the fixture's. The
        real git.REMOTES: the wiring rewrites each upstream's URL to the mirror, so the fetch after it reads the
        mirror and the network (refused at port 1) is never asked."""
        store = self.tmp / "store"
        if mirror:
            (store / "git").mkdir(parents=True)
            os.symlink(str(self.mirror), str(store / "git" / "WebKit.git"))
        p = mock.patch.dict(os.environ, dict(self.ENV, WK_STORE=str(store), WK_IN_VM="1", GIT_TERMINAL_PROMPT="0",
                                             http_proxy="http://127.0.0.1:1", https_proxy="http://127.0.0.1:1"))
        p.start()
        self.addCleanup(p.stop)

        class Here(targets.Container):
            def store_init(self):
                pass
        reg = targets.Registry(REPO, env=dict(os.environ))
        target = Here("container", str(REPO), dict(os.environ), reg.machine)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                sync.Sync(reg, Clock(), None, "tools", target="container").sync_snapshot(target)
            except act.Refused:
                return store, None, err.getvalue()
        ids = sorted(d.name for d in (store / "base").iterdir())
        self.assertEqual(len(ids), 1, ids)
        return store, store / "base" / ids[0], err.getvalue()

    def test_the_published_tree_shares_the_mirror_and_is_complete(self):
        store, d, err = self._sync()
        self.assertIsNotNone(d, err)
        alternates = d / "WebKit" / ".git" / "objects" / "info" / "alternates"
        self.assertTrue(alternates.exists(), "the snapshot copied the history instead of borrowing it")
        self.assertEqual(os.path.realpath(alternates.read_text().strip()), os.path.realpath(str(self.mirror / "objects")))
        self.assertEqual((d / "sha").read_text().strip(),
                         self._git("rev-parse", "refs/heads/main", cwd=self.mirror).stdout.strip())
        self.assertEqual((d / "branch").read_text().strip(), "origin/main")
        self.assertEqual(self._git("symbolic-ref", "--short", "HEAD", cwd=d / "WebKit").stdout.strip(), "main")

    def test_no_mirror_is_refused_naming_the_host(self):
        store, d, err = self._sync(mirror=False)
        self.assertIsNone(d)
        self.assertIn("'wk sync' on the host makes it", err)


if __name__ == "__main__":
    unittest.main()
