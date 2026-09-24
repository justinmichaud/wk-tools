"""`wk scp` -- one file or one directory in or out of a workspace.

Three things are held to here:

    the shape is refused before anything moves. Exactly one side carries the
      ':' that marks the workspace, and a bad invocation exits 2 with the
      synopsis -- the same lines the dispatcher prints for a missing operand
    the bytes arrive intact, both ways, for a file and for a tree. The local
      target is a real filesystem, so these compare the bytes rather than a
      transcript of a copy
    each driver moves them the way it already moves bytes -- `podman cp` for
      a container, scp and rsync (through the one `Machine` copy) for a guest
      or a build machine -- and never through `exec`, which is a login shell
      (or wkdev-enter) and not a byte pipe

`TestTheBytesArrive`/`TestRefusals`/`TestTheWholeCommandOnAGuest` touch no
real container, guest or build machine: `podman`, `tart`, `ssh`, `scp` and
`rsync` are stubs on PATH that log their argv, and the "workspace" is a
scratch directory. `TestDriverCopy` is one step below that: `targets.py`'s
drivers over a fake machine, the argv each one builds.

Run: python3 -m unittest tests.test_scp -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import (
    REPO, WkTest, fake_workspace, run, stub_path,
)

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402

# `podman`: logs every invocation and answers the two questions the container
# driver asks -- the container's user (its working directory) and what a path
# is. Nothing is copied; the argv is the whole answer.
FAKE_PODMAN = '''
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$*" in
    *WorkingDir*)   echo "/home/dev" ;;
    *"echo dir"*)   echo file ;;
esac
exit 0
'''

# `tart`: one running guest at an address, which is all _ip needs.
FAKE_TART = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"running","Source":"local"}]' ;;
ip)   echo 1.2.3.4 ;;
*)    exit 1 ;;
esac
'''


def _net_stub(tool, kind="absent"):
    """`ssh`/`scp`/`rsync`: log the argv under the tool's own name, and answer
    t_path_kind's one question with <kind>."""
    return (
        '#!/bin/sh\n'
        f'printf \'{tool} %s\\n\' "$*" >> "$WK_TEST_NET_LOG"\n'
        f'case "$*" in *"echo dir"*) echo {kind} ;; esac\n'
        'exit 0\n'
    )


class TestDeclaration(WkTest):
    def test_explain_answers_without_running_anything(self):
        cp = run("scp", "--explain")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("copy a file or directory", cp.stdout)

    def test_the_listing_names_it(self):
        # A bare `wk` is the listing (exit 2).
        self.assertIn("scp [<workspace>]", run().stdout)

    def test_it_runs_where_it_was_typed(self):
        """`here`, and it has to be: one side of the copy is this machine's
        filesystem. Forwarded into the podman VM (which mounts only this
        checkout and the two store directories, never where files are kept) or
        delegated to a build machine, that side would silently mean a path over
        there."""
        decl = [l for l in (REPO / "cmd" / "scp").read_text().splitlines()[:15]
                if l.startswith("# wk:")]
        self.assertEqual(len(decl), 1, decl)
        self.assertIn(" here", decl[0])
        self.assertIn("takes=2", decl[0])


class TestUsage(WkTest):
    """A bad invocation exits 2 and prints the synopsis, wherever it is
    caught: the dispatcher catches a missing operand, cmd/scp the shape."""

    def _refused(self, *args, inside=True):
        if inside:
            with fake_workspace() as ws:
                cp = ws.run("scp", *args)
        else:
            cp = run("scp", *args)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("usage: wk scp", cp.stdout)
        return cp.stdout

    def test_neither_side_names_the_workspace(self):
        out = self._refused("/tmp/a", "/tmp/b")
        self.assertIn("leading ':'", out)

    def test_both_sides_name_the_workspace(self):
        out = self._refused(":a", ":b")
        self.assertIn("exactly one side", out)

    def test_one_path_is_not_a_copy(self):
        self._refused(":a")

    def test_three_paths_are_not_a_copy(self):
        self._refused(":a", "/tmp/b", "/tmp/c")

    def test_an_unknown_option(self):
        out = self._refused("-z", ":a", "/tmp/b")
        self.assertIn("unknown option", out)

    def test_a_bare_colon_is_not_a_path(self):
        """':' marks the workspace side of a path; on its own it names
        nothing, so neither side is the workspace."""
        out = self._refused(":", "/tmp/b")
        self.assertIn("leading ':'", out)

    def test_a_missing_workspace_outside_one(self):
        """Outside a workspace the name is the first of three positionals, so
        two paths alone is the dispatcher's refusal -- and it must be the same
        exit code and the same synopsis."""
        self._refused(":a", "/tmp/b", inside=False)


class TestTheBytesArrive(WkTest):
    """Against the `local` target -- a workspace that is this machine -- so
    what is compared is the bytes, not a transcript of a copy."""

    def setUp(self):
        super().setUp()
        self._cm = fake_workspace()
        self.ws = self._cm.__enter__()
        self.addCleanup(self._cm.__exit__, None, None, None)
        self.src = self.ws.ws_dir / "WebKit"
        self.here = self.tmp / "here"
        self.here.mkdir()

    def scp(self, *args):
        return self.ws.run("scp", *args)

    def _tree(self, root):
        (root / "sub").mkdir(parents=True)
        (root / "a").write_bytes(b"a\n")
        (root / "sub" / "c").write_bytes(b"c\n")

    def test_a_file_comes_out_byte_for_byte(self):
        blob = os.urandom(4096)
        (self.src / "bin.dat").write_bytes(blob)
        cp = self.scp(":bin.dat", str(self.here / "bin.dat"))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((self.here / "bin.dat").read_bytes(), blob)

    def test_a_file_goes_in_byte_for_byte(self):
        blob = os.urandom(4096)
        (self.here / "new.dat").write_bytes(blob)
        cp = self.scp(str(self.here / "new.dat"), ":new.dat")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((self.src / "new.dat").read_bytes(), blob)

    def test_an_absolute_workspace_path_is_taken_as_it_is(self):
        blob = os.urandom(64)
        (self.here / "abs.dat").write_bytes(blob)
        dest = self.ws.ws_dir / "elsewhere.dat"
        cp = self.scp(str(self.here / "abs.dat"), f":{dest}")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(dest.read_bytes(), blob)

    def test_a_directory_comes_out_whole(self):
        self._tree(self.src / "tree")
        cp = self.scp("-r", ":tree", str(self.here / "tree"))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((self.here / "tree" / "a").read_bytes(), b"a\n")
        self.assertEqual((self.here / "tree" / "sub" / "c").read_bytes(), b"c\n")

    def test_a_directory_goes_in_whole(self):
        self._tree(self.here / "tree")
        cp = self.scp("-r", str(self.here / "tree"), ":tree")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((self.src / "tree" / "sub" / "c").read_bytes(), b"c\n")

    def test_r_on_a_file_copies_the_file(self):
        """-r says the source may be a directory, not that it must be: a file
        under -r is still one file, not a refusal."""
        (self.src / "one.txt").write_bytes(b"one\n")
        cp = self.scp("-r", ":one.txt", str(self.here / "one.txt"))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((self.here / "one.txt").read_bytes(), b"one\n")

    def test_an_existing_directory_receives_the_copy_by_name(self):
        """What cp and scp do: `wk scp <ws> :bin.dat <dir>` lands
        <dir>/bin.dat, and nothing addresses the directory itself."""
        (self.src / "bin.dat").write_bytes(b"x")
        drop = self.here / "drop"
        drop.mkdir()
        cp = self.scp(":bin.dat", str(drop))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((drop / "bin.dat").read_bytes(), b"x")


class TestRefusals(WkTest):
    """Nothing copied, exit 1, and a message that names the remedy -- as
    against a bad invocation, which is the usage and exit 2 above."""

    def setUp(self):
        super().setUp()
        self._cm = fake_workspace()
        self.ws = self._cm.__enter__()
        self.addCleanup(self._cm.__exit__, None, None, None)
        self.src = self.ws.ws_dir / "WebKit"
        self.here = self.tmp / "here"
        self.here.mkdir()
        (self.src / "tree").mkdir()
        (self.src / "tree" / "a").write_bytes(b"a\n")
        (self.src / "file.txt").write_bytes(b"f\n")

    def scp(self, *args):
        return self.ws.run("scp", *args)

    def test_a_directory_needs_r(self):
        cp = self.scp(":tree", str(self.here / "tree"))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("is a directory", cp.stdout)
        self.assertIn("wk scp -r", cp.stdout)
        self.assertFalse((self.here / "tree").exists())

    def test_a_source_that_is_not_there(self):
        cp = self.scp(":nope.txt", str(self.here / "nope.txt"))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("no such file or directory", cp.stdout)

    def test_a_directory_never_replaces_a_file(self):
        target = self.here / "occupied"
        target.write_bytes(b"mine\n")
        cp = self.scp("-r", ":tree", str(target))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("is a file, and a directory cannot replace it", cp.stdout)
        self.assertEqual(target.read_bytes(), b"mine\n")

    def test_a_file_never_replaces_a_directory(self):
        """The same refusal the other way round: the name rule lands the copy
        at <dir>/file.txt, and here that is itself a directory."""
        drop = self.here / "drop"
        (drop / "file.txt").mkdir(parents=True)
        cp = self.scp(":file.txt", str(drop))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("is a directory, and a file cannot replace it", cp.stdout)

    def test_a_transfer_that_fails_says_which_copy_failed(self):
        """The transport's own failure is exit 1 too, and names both ends --
        a `cp: No such file or directory` on its own says neither."""
        cp = self.scp(":file.txt", str(self.here / "no-such-dir" / "file.txt"))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("failed", cp.stdout)
        self.assertIn("file.txt", cp.stdout)

    def _copied_once(self):
        """A tree copied out into a directory that already holds one of that
        name -- what a second run of the same command is."""
        drop = self.here / "drop"
        drop.mkdir()
        cp = self.scp("-r", ":tree", str(drop))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        (drop / "tree" / "stale").write_bytes(b"stale\n")
        return drop

    def test_replacing_a_whole_directory_is_a_barrier(self):
        drop = self._copied_once()
        cp = self.scp("-r", ":tree", str(drop))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--force", cp.stdout)
        self.assertTrue((drop / "tree" / "stale").exists(),
                        "the barrier let a copy through")

    def test_force_replaces_its_contents(self):
        drop = self._copied_once()
        cp = self.scp("-r", ":tree", str(drop), "--force")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((drop / "tree" / "a").read_bytes(), b"a\n")
        self.assertFalse((drop / "tree" / "stale").exists(),
                         "contents are replaced, not merged")


class TestTheWholeCommandOnAGuest(WkTest):
    """The command end to end against a target that is another machine, with
    `tart` and `ssh`/`scp` stubbed: the dispatcher resolves the name here (no
    forwarding, no delegating -- `here`), and the driver moves the bytes."""

    def _run(self, ssh_body, *args):
        log = self.tmp / "net.log"
        log.write_text("")
        vmstore = self.tmp / "vmstore"
        (vmstore / "ws" / "demo").mkdir(parents=True)
        (vmstore / "ws" / "demo" / ".wk-ready").write_text("")
        with stub_path({"tart": FAKE_TART, "ssh": ssh_body,
                        "scp": _net_stub("scp"), "rsync": _net_stub("rsync")}) as binp:
            cp = run("scp", *args, env={
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_NET_LOG": str(log),
                "WK_TARGET": "vm",
                "WK_VM_STORE": str(vmstore),
                "XDG_STATE_HOME": str(self.tmp / "state"),
            })
        self.log = log.read_text()
        return cp

    def test_a_file_comes_out_of_a_guest(self):
        cp = self._run(_net_stub("ssh", kind="file"),
                       "demo", ":a.txt", str(self.tmp / "a.txt"))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        # The name came out of argv as the dispatcher's positional, so the
        # command never re-parsed it out of a `<ws>:<path>` operand.
        self.assertIn(f"copied demo:/Users/admin/WebKit/a.txt to {self.tmp / 'a.txt'}",
                      cp.stdout)
        self.assertIn(f"admin@1.2.3.4:/Users/admin/WebKit/a.txt {self.tmp / 'a.txt'}",
                      self.log)

    def test_a_probe_that_answers_nothing_is_not_an_absent_file(self):
        """A guest that cannot be asked must not read as "no such file": that
        would turn an unreachable workspace into a refusal about the path."""
        silent = '#!/bin/sh\nexit 0\n'
        cp = self._run(silent, "demo", ":a.txt", str(self.tmp / "a.txt"))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("could not tell what", cp.stdout)
        self.assertNotIn("no such file", cp.stdout)


class DriverCopyTest(unittest.TestCase):
    """A `Registry` over a `Fake` machine: no bash, no real process, the
    argv each driver's `pull`/`push`/`pull_dir`/`push_dir`/`path_kind` builds."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-scp-drivers-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_IN_VM": "1",
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        (self.tmp / "hosts").mkdir()
        self.fake = Fake("here")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)

    def conf(self, name, text):
        kind = "" if "KIND=build" in text else "KIND=%s\n" % ("peer" if "WK_REMOTE_PEER=1" in text else "build")
        (self.tmp / "hosts" / (name + ".conf")).write_text(kind + text)


class TestContainerCopy(DriverCopyTest):
    """`podman cp`: wkdev-enter is a shell wrapper and not a byte pipe, and a
    shell wrapper corrupts a binary copy piped through it -- 1396 bytes
    arriving as 1399, measured against `Container.pull` (lib/wk/targets.py)."""

    def setUp(self):
        super().setUp()
        self.t = self.reg.load("container")
        self.fake.answer(["podman", "inspect", "wk-demo", "--format", "{{.Config.WorkingDir}}"], out="/home/dev\n")
        self.fake.answer(["podman", "cp"], out="")

    def test_push_and_pull_are_podman_cp(self):
        self.t.push("demo", "/tmp/local.txt", "/src/WebKit/a.txt")
        self.assertEqual(self.fake.effects[-1][1], ("podman", "cp", "/tmp/local.txt", "wk-demo:/src/WebKit/a.txt"))
        self.t.pull("demo", "/src/WebKit/a.txt", "/tmp/out.txt")
        self.assertEqual(self.fake.effects[-1][1], ("podman", "cp", "wk-demo:/src/WebKit/a.txt", "/tmp/out.txt"))

    def test_push_dir_clears_the_containers_own_side_first_as_its_own_user(self):
        self.fake.answer(["podman", "exec", "--user", "dev", "wk-demo", "/bin/sh"], out="")
        self.t.push_dir("demo", "/tmp/tree", "/src/WebKit/tree")
        clear, cp = self.fake.effects[-2][1], self.fake.effects[-1][1]
        self.assertEqual(clear[:5], ("podman", "exec", "--user", "dev", "wk-demo"))
        self.assertIn("rm -rf '/src/WebKit/tree' && mkdir -p '/src/WebKit/tree'", clear[-1])
        self.assertEqual(cp, ("podman", "cp", "/tmp/tree/.", "wk-demo:/src/WebKit/tree"))

    def test_pull_dir_clears_the_destination_here_first(self):
        self.fake.mkdir("/tmp/out")
        self.fake.write("/tmp/out/stale", "x")
        self.t.pull_dir("demo", "/src/WebKit/tree", "/tmp/out")
        self.assertFalse(self.fake.exists("/tmp/out/stale"))
        self.assertEqual(self.fake.effects[-1][1], ("podman", "cp", "wk-demo:/src/WebKit/tree/.", "/tmp/out"))

    def test_path_kind_asks_inside_the_container_as_its_own_user(self):
        self.fake.answer(["podman", "exec", "--user", "dev", "wk-demo", "/bin/sh"], out="dir\n")
        self.assertEqual(self.t.path_kind("demo", "/src/WebKit/tree"), "dir")

    def test_no_copy_goes_through_wkdev_enter(self):
        self.fake.answer(["podman", "exec", "--user", "dev", "wk-demo", "/bin/sh"], out="file\n")
        self.t.push("demo", "/tmp/a", "/src/WebKit/a")
        self.t.pull("demo", "/src/WebKit/a", "/tmp/a")
        self.t.path_kind("demo", "/src/WebKit/a")
        for kind, argv in self.fake.effects:
            self.assertNotIn("wkdev-enter", " ".join(str(a) for a in argv))


class TestVmCopy(DriverCopyTest):
    def setUp(self):
        super().setUp()
        del self.env["WK_IN_VM"]
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "tart").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "tart").chmod(0o755)
        self.env["PATH"] = "%s:%s" % (bin_dir, os.environ.get("PATH", ""))
        from unittest import mock
        from wk.store import Store
        p = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("vm")
        self.fake.answer([self.t.tart(), "list"], out=json.dumps([{"Name": "wk-demo", "State": "running", "Source": "local"}]))
        self.fake.answer([self.t.tart(), "ip"], out="1.2.3.4\n")

    def test_push_and_pull_are_scp_with_the_guests_own_key(self):
        self.fake.answer(["scp"], out="")
        self.t.push("demo", "/tmp/local.txt", "/Users/admin/WebKit/a.txt")
        argv = self.fake.effects[-1][1]
        self.assertEqual(argv[0], "scp")
        self.assertEqual(argv[-2:], ("/tmp/local.txt", "admin@1.2.3.4:/Users/admin/WebKit/a.txt"))
        self.assertIn("-i", argv)
        self.assertIn(self.t.key(), argv)
        self.t.pull("demo", "/Users/admin/WebKit/a.txt", "/tmp/out.txt")
        self.assertEqual(self.fake.effects[-1][1][-2:], ("admin@1.2.3.4:/Users/admin/WebKit/a.txt", "/tmp/out.txt"))

    def test_push_dir_and_pull_dir_are_rsync_with_chmod(self):
        self.fake.answer(["rsync"], out="")
        self.t.push_dir("demo", "/tmp/tree", "/Users/admin/WebKit/tree")
        argv = self.fake.effects[-1][1]
        self.assertEqual(argv[0], "rsync")
        self.assertIn("--chmod=go-w", argv)
        self.assertIn("--delete", argv)
        self.assertEqual(argv[-2:], ("/tmp/tree/", "admin@1.2.3.4:/Users/admin/WebKit/tree/"))

    def test_path_kind_asks_over_ssh(self):
        self.fake.answer(["ssh"], out="file\n")
        self.assertEqual(self.t.path_kind("demo", "/Users/admin/WebKit/a.txt"), "file")

    def test_a_guest_that_is_not_running_dies_naming_start(self):
        from wk.act import Refused
        with self.assertRaises(Refused):
            self.t.pull("gone", "/x", "/y")


class TestRemoteCopy(DriverCopyTest):
    """A build machine reached over ssh: scp and rsync, over the same opts `exec` uses."""

    def setUp(self):
        super().setUp()
        del self.env["WK_IN_VM"]
        self.env["XDG_STATE_HOME"] = str(self.tmp / "state")
        self.conf("box", "WK_REMOTE_HOST=box.example\nWK_REMOTE_ROOT=/home/u/wk\n")
        self.reg = targets.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("box")

    def test_push_and_pull_are_scp(self):
        self.fake.answer(["scp"], out="")
        self.t.push("demo", "/tmp/local.txt", "/wk/ws/demo/WebKit/a.txt")
        self.assertEqual(self.fake.effects[-1][1][0], "scp")
        self.assertEqual(self.fake.effects[-1][1][-2:], ("/tmp/local.txt", "box.example:/wk/ws/demo/WebKit/a.txt"))
        self.t.pull("demo", "/wk/ws/demo/WebKit/a.txt", "/tmp/out.txt")
        self.assertEqual(self.fake.effects[-1][1][-2:], ("box.example:/wk/ws/demo/WebKit/a.txt", "/tmp/out.txt"))

    def test_push_dir_and_pull_dir_are_rsync_with_chmod(self):
        self.fake.answer(["rsync"], out="")
        self.t.push_dir("demo", "/tmp/tree", "/wk/ws/demo/WebKit/tree")
        argv = self.fake.effects[-1][1]
        self.assertEqual(argv[0], "rsync")
        self.assertIn("--chmod=go-w", argv)
        self.assertEqual(argv[-2:], ("/tmp/tree/", "box.example:/wk/ws/demo/WebKit/tree/"))

    def test_path_kind_asks_over_ssh(self):
        self.fake.answer(["ssh"], out="dir\n")
        self.assertEqual(self.t.path_kind("demo", "/wk/ws/demo/WebKit/tree"), "dir")


class TestRemoteLocalCopy(DriverCopyTest):
    """A build machine that is this one (WK_REMOTE_LOCAL): the same split
    every other function of this driver makes, and real copies on a real
    filesystem -- no ssh, no scp, no rsync."""

    def setUp(self):
        super().setUp()
        del self.env["WK_IN_VM"]
        self.env["XDG_STATE_HOME"] = str(self.tmp / "state")
        self.box = self.tmp / "box"
        self.conf("fakebox", "KIND=build\nWK_TARGET_KIND=remote\nWK_REMOTE_LOCAL=1\nWK_REMOTE_ROOT=%s\n" % self.box)
        self.reg = targets.Registry(REPO, env=self.env, machine=Local())
        self.t = self.reg.load("fakebox")

    def test_push_and_push_dir_copy_on_a_real_filesystem(self):
        (self.box / "src").mkdir(parents=True)
        (self.box / "src" / "a").write_bytes(b"a\n")
        (self.box / "one.txt").write_bytes(b"one\n")
        self.t.push("demo", str(self.box / "one.txt"), str(self.box / "two.txt"))
        self.assertEqual((self.box / "two.txt").read_bytes(), b"one\n")
        self.t.push_dir("demo", str(self.box / "src"), str(self.box / "dst"))
        self.assertEqual((self.box / "dst" / "a").read_bytes(), b"a\n")
        self.assertEqual(self.t.path_kind("demo", str(self.box / "one.txt")), "file")


if __name__ == "__main__":
    unittest.main()
