"""Where a checkout fetches from, per target: t_mirror_dir (lib/target.sh).

A fetch in a workspace is either local -- against a bare mirror that already
carries every upstream -- or four fetches of four upstreams over that
workspace's egress. Which one it is depends on where that target's mirror is,
and a container's bind mount `/mirror/WebKit.git` is the answer for exactly one
of the four: a guest's mirror cannot be that path (macOS's system volume is
read-only and nothing of the host's is mounted in there), so a command that
spells it takes the network arm in there and pays ~1,500 remote heads' worth of
negotiation per fetch.

So each driver names its own mirror once and every command asks the driver.
This holds each of the four to naming one, holds the path in a guest to being
the one the guest's own driver builds, and holds the commands to asking rather
than spelling it again.

Hermetic: the drivers are sourced and asked, the way tests/test_target_os.py
asks each of them for t_os. No container, guest, machine or network.

Run: python3 -m unittest tests.test_mirror_path -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, fake_workspace, stub_path

DRIVERS = ("container", "vm", "remote", "local")

# uname is the only evidence a workspace has about which kind it is
# (targets/local.sh), so both of that driver's arms are reachable from here.
UNAME = '''#!/bin/sh
case "$1" in
  -s) echo %s ;;
  -m) echo arm64 ;;
  *)  echo %s ;;
esac
'''


def _ask(target, env=None, body="t_mirror_dir demo"):
    cp = bash(f'''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target {target} >/dev/null 2>&1
{body}
''', env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip()


class TestEveryDriverNamesOne(WkTest):
    def setUp(self):
        super().setUp()
        # A build box driven without ssh (WK_REMOTE_LOCAL, targets/remote.sh),
        # and a workspace that is not one (a marker naming a checkout).
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        self.root = self.tmp / "remote-root"
        (self.registry / "fakebox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_LOCAL=1\n"
            f"WK_REMOTE_ROOT={self.root}\n"
            f"WK_REMOTE_STORE={self.tmp / 'remote-store'}\n"
        )

    def _mirror(self, target):
        if target == "remote":
            return _ask("fakebox", env={"WK_TARGET_REGISTRY": str(self.registry),
                                        "XDG_STATE_HOME": str(self.tmp / "state")})
        if target == "local":
            with fake_workspace() as ws:
                return _ask("local", env=ws.env())
        return _ask(target)

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
        cp = bash('set -euo pipefail\n. "$WK_ROOT/lib/common.sh"\n'
                  '. "$WK_ROOT/lib/target.sh"\nt_mirror_dir demo\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "")

    def test_the_three_machines_name_three_different_mirrors(self):
        """A container's is this machine's, bind-mounted; a guest's is inside
        the guest; a build box's is on the box. Nothing of one is reachable
        from another, so no two of them can be the same path."""
        got = {t: self._mirror(t) for t in ("container", "vm", "remote")}
        self.assertEqual(len(set(got.values())), 3, got)

    def test_a_guests_mirror_sits_beside_its_checkout(self):
        """Where in the guest is the vm driver's to say; that it is under the
        account's own home is what makes it possible at all -- macOS's system
        volume is read-only, so /mirror cannot be created in there."""
        mirror = self._mirror("vm")
        src = _ask("vm", body="t_src demo")
        self.assertEqual(mirror, f"{src}.git")
        self.assertTrue(mirror.startswith("/Users/"), mirror)


class TestAWorkspaceAnswersForTheKindItIs(WkTest):
    """targets/local.sh runs in both kinds of workspace and each has its
    mirror somewhere else, so it answers from the same evidence t_os does."""

    def _in_workspace(self, uname_s):
        with fake_workspace() as ws, \
             stub_path({"uname": UNAME % (uname_s, uname_s)}) as binp:
            return _ask("local", env=ws.env({
                "PATH": f"{binp}:{os.environ['PATH']}",
            })), str(ws.ws_dir / "WebKit")

    def test_in_a_container_it_is_the_bind_mount_the_container_driver_names(self):
        got, _ = self._in_workspace("Linux")
        self.assertEqual(got, _ask("container"))

    def test_in_a_guest_it_is_the_path_the_vm_driver_names(self):
        """Two files spell this path, and they cannot be allowed to drift: the
        vm driver builds the mirror there and the workspace inside that guest
        fetches from there. Both derive it from the checkout, so this compares
        the rule rather than the string."""
        got, src = self._in_workspace("Darwin")
        self.assertEqual(got, f"{src}.git")
        vm_src = _ask("vm", body="t_src demo")
        self.assertEqual(_ask("vm"), f"{vm_src}.git")


class MirrorFixture(WkTest):
    """A mirror made by the real mirror_refresh_script out of two local
    repositories standing in for the upstreams -- git takes a path as a URL,
    so nothing here reaches the network."""

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

        # wk_remotes is the one list of upstreams; overridden here so nothing
        # reaches github.com, and the rest of the snippet is the real one.
        self.remotes = (f'wk_remotes() {{ printf "origin {self.tmp}/up.git\\n'
                        f'fork {self.tmp}/fk.git\\n"; }}\n')
        self.mirror = self.tmp / "m.git"
        cp = bash('set -euo pipefail\n. "$WK_ROOT/lib/common.sh"\n'
                  '. "$WK_ROOT/lib/store.sh"\n' + self.remotes
                  + f'sh -c "$(mirror_refresh_script {str(self.mirror)!r})"\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.refresh_out = cp.stdout


class TestOneMirrorLayoutEverywhere(MirrorFixture):
    """mirror_refresh_script (lib/store.sh) makes every mirror in the fleet --
    this machine's, a build box's, a guest's -- and cmd/sync's ws_fetch_script
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

    def test_a_workspace_fetch_against_it_takes_the_mirror_arm(self):
        """The pair under test: a checkout made the way a guest's and a build
        box's are (`--shared` off the mirror) fetches every upstream the mirror
        carries in one local fetch, with its own remotes pointed at nothing."""
        ws = self.tmp / "ws"
        self._git("clone", "-q", "--shared", "--branch", "main",
                  str(self.mirror), "ws", cwd=self.tmp)
        for remote in ("origin", "fork"):
            self._git("remote", "remove", remote, cwd=ws, check=False)
            self._git("remote", "add", remote, str(self.tmp / "gone.git"), cwd=ws)
        cp = bash('set -euo pipefail\ncd "$WK_ROOT"\n. cmd/sync functions\n'
                  + self.remotes
                  + f'ws_fetch_script {str(ws)!r} {str(self.mirror)!r}\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = subprocess.run(["sh", "-c", cp.stdout], cwd=str(self.tmp),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("from=mirror", out.stdout)
        refs = self._git("for-each-ref", "--format=%(refname)", cwd=ws).stdout.split()
        self.assertIn("refs/remotes/origin/main", refs)
        self.assertIn("refs/remotes/fork/side", refs)


class TestABranchIsTakenFromTheMirrorFirst(MirrorFixture):
    """`wk build <ws> <branch>` and the babysitter fetch one branch before
    they build. origin_branch_fetch_step (lib/store.sh) is that fetch: the
    mirror when it carries the branch, origin when it does not -- WebKit has
    ~920 branches and a mirror carries the handful wk_mirror_branches names,
    so both arms are real."""

    def _step(self, branch, mirror):
        cp = bash('set -euo pipefail\n. "$WK_ROOT/lib/common.sh"\n'
                  '. "$WK_ROOT/lib/store.sh"\n'
                  f'origin_branch_fetch_step {branch!r} {str(mirror)!r}\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def _checkout(self):
        """A workspace checkout whose origin is a real (local-path) upstream,
        as a workspace's is after wk_wiring_script."""
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
        self.assertEqual(step.strip(), "git fetch -q origin 'main'")

    def test_both_callers_use_it(self):
        for rel in ("cmd/build", "build/babysit.sh"):
            text = (REPO / rel).read_text()
            with self.subTest(file=rel):
                self.assertIn("origin_branch_fetch_step", text)
                self.assertNotIn("git fetch -q origin $(sh_quote", text)


class TestTheCommandsAskTheDriver(unittest.TestCase):
    """Every command that fetches in a workspace reads t_mirror_dir; only the
    drivers name a path. One path spelled into several commands is fixed in
    one of them and wrong in the rest."""

    ASKS = ("cmd/sync", "cmd/new", "cmd/pr", "cmd/build", "build/babysit.sh",
            "lib/store.sh")

    def test_no_command_spells_a_mirror_path_of_its_own(self):
        for rel in self.ASKS:
            text = (REPO / rel).read_text()
            with self.subTest(file=rel):
                self.assertNotIn("/mirror/WebKit.git", text,
                                 f"{rel} names a container's mirror itself")
                self.assertIn("t_mirror_dir", text,
                              f"{rel} fetches without asking the driver")

    # Named exceptions: t_spawn (targets/container.sh) execs these directly,
    # with no WK_ROOT and no lib/target.sh sourced, so there is no
    # t_mirror_dir for them to ask -- each carries a comment saying so at the
    # literal.
    CONTAINER_ONLY_EXCEPTIONS = ("image/buildroot-webkit.sh", "image/yocto-build.sh")

    def test_the_container_only_exceptions_are_still_explained(self):
        for rel in self.CONTAINER_ONLY_EXCEPTIONS:
            text = (REPO / rel).read_text()
            with self.subTest(file=rel):
                self.assertIn("/mirror/WebKit.git", text,
                              f"{rel} no longer needs its named exception -- drop it from the list")
                self.assertIn("no t_mirror_dir to ask", text,
                              f"{rel} spells the mirror literal with no comment explaining why")

    def test_each_mirror_path_is_spelled_in_exactly_one_place(self):
        """A driver *answers* for a mirror; it does not spell one. Two of the
        four share each answer -- the driver that makes the mirror, and
        targets/local.sh answering from inside a workspace of that kind -- so
        both paths live in lib/target.sh and every driver calls them."""
        target_sh = (REPO / "lib" / "target.sh").read_text()
        for func in ("mirror_in_container", "mirror_beside_checkout"):
            with self.subTest(func=func):
                self.assertRegex(target_sh, rf"(?m)^{func}\(\)\s*\{{")
        for rel in ("targets/local.sh", "targets/container.sh"):
            with self.subTest(file=rel):
                self.assertNotIn("/mirror/WebKit.git", (REPO / rel).read_text(),
                                 f"{rel} spells the container mirror instead of asking")
                self.assertIn("mirror_in_container", (REPO / rel).read_text())
        for rel in ("targets/local.sh", "targets/vm.sh"):
            with self.subTest(file=rel):
                self.assertIn("mirror_beside_checkout", (REPO / rel).read_text(),
                              f"{rel} derives the guest mirror itself")

    def test_only_a_driver_names_a_path(self):
        named = sorted(
            f.relative_to(REPO).as_posix() for f in REPO.rglob("*")
            if f.is_file() and ".git" not in f.parts
            and "__pycache__" not in f.parts and f.suffix not in (".py", ".pyc")
            and "t_mirror_dir() {" in f.read_text(errors="replace"))
        self.assertEqual(named, ["lib/target.sh", "targets/container.sh",
                                 "targets/local.sh", "targets/remote.sh",
                                 "targets/vm.sh"], named)


if __name__ == "__main__":
    unittest.main()
