"""`wk scp` -- one file or one directory in or out of a workspace."""
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
from wk import places  # noqa: E402
from wk.decl import leading_block  # noqa: E402
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


def _tart(kind="absent"):
    """`tart`: one running guest whose `exec` logs its argv and answers t_path_kind's one question with <kind>."""
    return ('#!/bin/sh\n'
            'case "$1" in\n'
            'list) echo \'[{"Name":"wk-demo","State":"running","Source":"local"}]\' ;;\n'
            'exec) printf \'tart %s\\n\' "$*" >> "$WK_TEST_NET_LOG"\n'
            f'      case "$*" in *"echo dir"*) echo {kind} ;; esac ;;\n'
            '*) exit 1 ;;\n'
            'esac\n')


def _net_stub(tool, kind="absent"):
    return (
        '#!/bin/sh\n'
        f'printf \'{tool} %s\\n\' "$*" >> "$WK_TEST_NET_LOG"\n'
        f'case "$*" in *"echo dir"*) echo {kind} ;; esac\n'
        'exit 0\n'
    )


class TestUsage(WkTest):
    def test_a_bad_invocation_exits_2_with_the_synopsis(self):
        for args, why, inside in (((":a", "/tmp/b"), "", False), (("/tmp/a", "/tmp/b"), "leading ':'", True),
                                  ((":", "/tmp/b"), "leading ':'", True), ((":a", ":b"), "exactly one side", True),
                                  ((":a",), "", True), ((":a", "/tmp/b", "/tmp/c"), "", True),
                                  (("-z", ":a", "/tmp/b"), "unknown option", True)):
            with self.subTest(args=args):
                if inside:
                    with fake_workspace() as ws:
                        cp = ws.run("scp", *args)
                else:
                    cp = run("scp", *args)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("usage: wk scp", cp.stdout)
                self.assertIn(why, cp.stdout)


class TestTheBytesArrive(WkTest):

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
        (self.src / "one.txt").write_bytes(b"one\n")
        cp = self.scp("-r", ":one.txt", str(self.here / "one.txt"))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((self.here / "one.txt").read_bytes(), b"one\n")

    def test_an_existing_directory_receives_the_copy_by_name(self):
        (self.src / "bin.dat").write_bytes(b"x")
        drop = self.here / "drop"
        drop.mkdir()
        cp = self.scp(":bin.dat", str(drop))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((drop / "bin.dat").read_bytes(), b"x")


class TestRefusals(WkTest):

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
        driver = self.here / "occupied"
        driver.write_bytes(b"mine\n")
        cp = self.scp("-r", ":tree", str(driver))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("is a file, and a directory cannot replace it", cp.stdout)
        self.assertEqual(driver.read_bytes(), b"mine\n")

    def test_a_file_never_replaces_a_directory(self):
        drop = self.here / "drop"
        (drop / "file.txt").mkdir(parents=True)
        cp = self.scp(":file.txt", str(drop))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("is a directory, and a file cannot replace it", cp.stdout)

    def test_a_transfer_that_fails_says_which_copy_failed(self):
        cp = self.scp(":file.txt", str(self.here / "no-such-dir" / "file.txt"))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("failed", cp.stdout)
        self.assertIn("file.txt", cp.stdout)

    def _copied_once(self):
        drop = self.here / "drop"
        drop.mkdir()
        cp = self.scp("-r", ":tree", str(drop))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        (drop / "tree" / "stale").write_bytes(b"stale\n")
        return drop

    def test_replacing_a_whole_directory_is_asked_and_declines_without_a_terminal(self):
        drop = self._copied_once()
        cp = self.scp("-r", ":tree", str(drop))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--yes", cp.stdout)
        self.assertTrue((drop / "tree" / "stale").exists(),
                        "a copy went through unasked")

    def test_replacing_a_file_is_asked_too(self):
        (self.here / "file.txt").write_bytes(b"mine\n")
        cp = self.scp(":file.txt", str(self.here / "file.txt"))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertEqual((self.here / "file.txt").read_bytes(), b"mine\n")
        self.assertEqual(self.scp(":file.txt", str(self.here / "file.txt"), "--yes").returncode, 0)
        self.assertEqual((self.here / "file.txt").read_bytes(), b"f\n")

    def test_a_dry_run_names_the_question_and_the_copy_and_changes_nothing(self):
        drop = self._copied_once()
        cp = self.scp("-r", ":tree", str(drop), "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("would ask:", cp.stdout)
        self.assertIn("would copy", cp.stdout)
        self.assertNotIn("copied", cp.stdout)
        self.assertTrue((drop / "tree" / "stale").exists(), "a dry run copied")

    def test_yes_replaces_its_contents(self):
        drop = self._copied_once()
        cp = self.scp("-r", ":tree", str(drop), "--yes")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((drop / "tree" / "a").read_bytes(), b"a\n")
        self.assertFalse((drop / "tree" / "stale").exists(),
                         "contents are replaced, not merged")


class TestTheWholeCommandOnAGuest(WkTest):

    def _run(self, tart_body, *args):
        log = self.tmp / "net.log"
        log.write_text("")
        vmstore = self.tmp / "vmstore"
        (vmstore / "ws" / "demo").mkdir(parents=True)
        (vmstore / "ws" / "demo" / ".wk-ready").write_text("")
        with stub_path({"tart": tart_body, "ssh": _net_stub("ssh"),
                        "scp": _net_stub("scp"), "rsync": _net_stub("rsync")}) as binp:
            cp = run("scp", *args, env={
                "PATH": f"{binp}:{os.environ['PATH']}",
                "WK_TEST_NET_LOG": str(log),
                "WK_PLACE": "vm",
                "WK_VM_STORE": str(vmstore),
                "XDG_STATE_HOME": str(self.tmp / "state"),
            })
        self.log = log.read_text()
        return cp

    def test_a_file_comes_out_of_a_guest(self):
        cp = self._run(_tart(kind="file"),
                       "demo", ":a.txt", str(self.tmp / "a.txt"))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        # The name came out of argv as the dispatcher's positional, so the
        # command never re-parsed it out of a `<ws>:<path>` operand.
        self.assertIn(f"copied demo:/Users/admin/WebKit/a.txt to {self.tmp / 'a.txt'}",
                      cp.stdout)
        self.assertIn("tart exec wk-demo /bin/cat /Users/admin/WebKit/a.txt", self.log)
        self.assertNotIn("ssh ", self.log, "a guest is reached through tart exec")

    def test_a_probe_that_answers_nothing_is_not_an_absent_file(self):
        cp = self._run(_tart(kind=""), "demo", ":a.txt", str(self.tmp / "a.txt"))
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("could not tell what", cp.stdout)
        self.assertNotIn("no such file", cp.stdout)


class DriverCopyTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-scp-drivers-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_IN_VM": "1",
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "home").mkdir()
        (self.tmp / "hosts").mkdir()
        self.fake = Fake("here")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)

    def conf(self, name, text):
        kind = "" if "kind=build" in text else "kind=%s\n" % ("peer" if "peer=1" in text else "build")
        (self.tmp / "hosts" / (name + ".conf")).write_text(kind + text)


class TestContainerCopy(DriverCopyTest):
    """`podman cp`: a shell wrapper (wkdev-enter) corrupts a binary pipe -- 1396 bytes arrived as 1399."""

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
        self.assertIn("rm -rf /src/WebKit/tree && mkdir -p /src/WebKit/tree", clear[-1])
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


class TestVmCopy(DriverCopyTest):
    def setUp(self):
        super().setUp()
        del self.env["WK_IN_VM"]
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "tart").write_text("")
        (self.tmp / "bin" / "tart").chmod(0o755)
        self.env["PATH"] = str(self.tmp / "bin")
        from unittest import mock
        from wk.store import Store
        p = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)
        self.t = self.reg.load("vm")
        self.fake.answer([self.t.tart(), "list"], out=json.dumps([{"Name": "wk-demo", "State": "running", "Source": "local"}]))
        self.fake.answer([self.t.tart(), "ip"], out="1.2.3.4\n")

    def test_every_copy_is_a_pipe_through_tart_exec(self):
        self.fake.answer(["sh", "-c"], out="")
        for fn, args in ((self.t.push, ("/tmp/a", "/Users/admin/WebKit/a")), (self.t.pull, ("/Users/admin/WebKit/a", "/tmp/a")),
                         (self.t.push_dir, ("/tmp/t", "/Users/admin/WebKit/t")), (self.t.pull_dir, ("/Users/admin/WebKit/t", "/tmp/t"))):
            with self.subTest(fn=fn.__name__):
                fn("demo", *args)
                argv = self.fake.effects[-1][1]
                self.assertEqual((argv[:2], argv[3:7]), (("sh", "-c"), (self.t.tart(), "wk-demo") + args))
                self.assertRegex(argv[2], r'" exec (-i )?"\$')

    def test_path_kind_asks_through_the_guest_agent(self):
        self.fake.answer([self.t.tart(), "exec"], out="file\n")
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
        self.conf("box", "host=box.example\nroot=/home/u/wk\n")
        self.reg = places.Registry(REPO, env=self.env, machine=self.fake)
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

    def setUp(self):
        super().setUp()
        del self.env["WK_IN_VM"]
        self.env["XDG_STATE_HOME"] = str(self.tmp / "state")
        self.box = self.tmp / "box"
        self.conf("fakebox", "kind=build\ndriver=remote\nlocal=1\nroot=%s\n" % self.box)
        self.reg = places.Registry(REPO, env=self.env, machine=Local())
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
