"""`wk scp` -- one file or one directory in or out of a workspace."""
import os
import re
import shlex
import unittest

from tests.fakes import Result
from tests.support import WkTest, fake_workspace, requires_tool, run
from tests import test_wk_workspace as wsworld


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


class ScpTest(WkTest):
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


class TestTheBytesArrive(ScpTest):
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

    @requires_tool("rsync")
    def test_a_directory_comes_out_whole(self):
        self._tree(self.src / "tree")
        cp = self.scp("-r", ":tree", str(self.here / "tree"))
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((self.here / "tree" / "a").read_bytes(), b"a\n")
        self.assertEqual((self.here / "tree" / "sub" / "c").read_bytes(), b"c\n")

    @requires_tool("rsync")
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


class TestRefusals(ScpTest):
    def setUp(self):
        super().setUp()
        (self.src / "tree").mkdir()
        (self.src / "tree" / "a").write_bytes(b"a\n")
        (self.src / "file.txt").write_bytes(b"f\n")

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

    @requires_tool("rsync")
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

    @requires_tool("rsync")
    def test_a_dry_run_names_the_question_and_the_copy_and_changes_nothing(self):
        drop = self._copied_once()
        cp = self.scp("-r", ":tree", str(drop), "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("would ask:", cp.stdout)
        self.assertIn("would copy", cp.stdout)
        self.assertNotIn("copied", cp.stdout)
        self.assertTrue((drop / "tree" / "stale").exists(), "a dry run copied")

    @requires_tool("rsync")
    def test_yes_replaces_its_contents(self):
        drop = self._copied_once()
        cp = self.scp("-r", ":tree", str(drop), "--yes")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual((drop / "tree" / "a").read_bytes(), b"a\n")
        self.assertFalse((drop / "tree" / "stale").exists(),
                         "contents are replaced, not merged")


PROBE = re.compile(r"if \[ -d (\S+) \]; then echo dir")
SSH = ("ssh", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "ControlMaster=auto", "-o",
       "ControlPath=/state/wk/ssh/%h-%p-%r", "-o", "ControlPersist=60", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "box.example")
PIPE_OUT, PIPE_IN = 'set -o pipefail; f=$1; shift; "$@" < /dev/null > "$f"', 'set -o pipefail; f=$1; shift; "$@" < "$f"'


def probe(path):
    return "if [ -d %s ]; then echo dir; elif [ -e %s ]; then echo file; else echo absent; fi" % (path, path)


class World(wsworld.World):
    """Workspace `ws`'s side answers what is at a path from `there` (path: "dir" | "file", else absent; `mute` answers
    nothing), wherever it is asked: in the container, through the guest agent, over the box's ssh or, on a box that is
    this machine, in a shell here. Every copy succeeds."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        self.there, self.mute = {}, False
        self.answer(["podman", "inspect", "wk-ws", "--format", "{{.Config.WorkingDir}}"], out="/home/dev\n")
        self.react(["podman", "exec"], self._kind)
        self.answer(["podman", "cp"])
        self.bash["set -o pipefail"] = Result(0)
        if hasattr(self, "tart"):
            self.react([self.tart, "exec"], self._kind)
        for sh in ("sh", "/bin/sh"):
            self.react([sh, "-c"], lambda a, f: self._ssh(["ssh", shlex.join(a)], f))

    def _kind(self, argv, f):
        m = PROBE.search(argv[-1])
        return Result(0) if not m else Result(0, "" if f.mute else f.there.get(m.group(1), "absent") + "\n")

    def _ssh(self, argv, f):
        return self._kind(argv, f) if PROBE.search(argv[-1]) else super()._ssh(argv, f)


class ScpFlow(wsworld.WorkspaceTest):
    """`wk scp ws ...` typed on the host whose place `fakebox` holds workspace `ws`; `here` (shown as /here) holds a.txt
    and tree/sub/c."""
    world_class = World
    KIND = "container"

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"fakebox": self.KIND})
        self.w.make("ws")
        self.w.effects = []
        self.here = str(self.w.tmp / "here")
        os.makedirs(os.path.join(self.here, "tree", "sub"))
        for rel, text in (("a.txt", "one\n"), ("tree/sub/c", "c\n")):
            with open(os.path.join(self.here, rel), "w") as f:
                f.write(text)

    def scp(self, *argv):
        rc, _, err = self.wk(self.w, "scp", "ws", *[a.replace("/here", self.here) for a in argv])
        return rc, self.w.rel(err.splitlines()[0]) if err else ""

    def ran(self):
        return [self.w.rel(e) for e in self.w.effects if e[0] == "run"]

    def copies(self):
        """The effects that move or clear bytes."""
        return [self.w.rel(e if e[0].startswith("copy") else e[:2]) for e in self.w.effects
                if e[0] in ("act", "remove", "copy_in", "copy_tree_in") or (e[0] == "mkdir" and not e[1].endswith("/wk/ssh"))
                or (e[0] == "run" and e[1][:2] == ("bash", "-c"))]


class TestContainerRoute(ScpFlow):
    """`podman cp` carries the bytes: a shell wrapper (wkdev-enter) corrupts a binary pipe -- 1396 bytes arrived as 1399."""

    def test_a_file_comes_out_and_goes_in_by_podman_cp_after_asking_the_container_as_its_own_user(self):
        self.w.there["/src/WebKit/a.txt"] = "file"
        self.assertEqual(self.scp(":a.txt", "/here/out.txt"), (0, "==> copied ws:/src/WebKit/a.txt to /here/out.txt"))
        self.assertIn(("podman", "exec", "--user", "dev", "wk-ws", "/bin/sh", "-c", probe("/src/WebKit/a.txt")), [r[1] for r in self.ran()])
        self.assertEqual(self.scp("/here/a.txt", ":b.txt")[0], 0)
        self.assertEqual(self.copies(), [("act", ("podman", "cp", "wk-ws:/src/WebKit/a.txt", "/here/out.txt")),
                                         ("act", ("podman", "cp", "/here/a.txt", "wk-ws:/src/WebKit/b.txt"))])

    def test_a_directory_going_in_clears_the_containers_side_first_as_its_own_user(self):
        self.assertEqual(self.scp("-r", "/here/tree", ":tree")[0], 0)
        self.assertEqual(self.copies(), [("act", ("podman", "exec", "--user", "dev", "wk-ws", "/bin/sh", "-c",
                                                  "rm -rf /src/WebKit/tree && mkdir -p /src/WebKit/tree")),
                                         ("act", ("podman", "cp", "/here/tree/.", "wk-ws:/src/WebKit/tree"))])

    def test_a_directory_coming_out_clears_the_destination_here_first(self):
        self.w.there["/src/WebKit/tree"] = "dir"
        self.assertEqual(self.scp("-r", ":tree", "/here/out")[0], 0)
        self.assertEqual(self.copies(), [("remove", "/here/out"), ("mkdir", "/here/out"),
                                         ("act", ("podman", "cp", "wk-ws:/src/WebKit/tree/.", "/here/out"))])


class TestGuestRoute(ScpFlow):
    """A macOS guest: every question and every copy goes through `tart exec`, the guest agent; nothing over ssh."""
    KIND = "vm"

    def tart(self, cmd):
        return (self.w.rel(self.w.tart), "exec", "-i", "wk-ws", "/bin/zsh", "-lc", cmd)

    def test_every_copy_is_a_pipe_through_tart_exec(self):
        self.w.there.update({"/Users/admin/WebKit/a.txt": "file", "/Users/admin/WebKit/tree": "dir"})
        self.assertEqual(self.scp(":a.txt", "/here/out.txt"), (0, "==> copied ws:/Users/admin/WebKit/a.txt to /here/out.txt"))
        for argv in (["/here/a.txt", ":b.txt"], ["-r", ":tree", "/here/out"], ["-r", "/here/tree", ":t"]):
            self.assertEqual(self.scp(*argv)[0], 0, argv)
        self.assertIn(self.tart("/bin/sh -c '%s'" % probe("/Users/admin/WebKit/a.txt")), [r[1] for r in self.ran()])
        runs = [e[1] for e in self.copies()]
        self.assertEqual(runs[:2], [("bash", "-c", PIPE_OUT, "bash", "/here/out.txt") + self.tart("cat /Users/admin/WebKit/a.txt"),
                                    ("bash", "-c", PIPE_IN, "bash", "/here/a.txt") + self.tart("cat > /Users/admin/WebKit/b.txt")])
        self.assertEqual([r[5:9] for r in runs], [self.tart("")[:4]] * 4)
        self.assertEqual([r for r in self.ran() if r[1][0] == "ssh"], [])

    def test_a_probe_the_guest_answers_nothing_to_is_not_an_absent_file(self):
        self.w.mute = True
        self.assertEqual(self.scp(":a.txt", "/here/out.txt"),
                         (1, "error: could not tell what ws:/Users/admin/WebKit/a.txt is: workspace 'ws' did not answer."))
        self.assertEqual(self.copies(), [])

    def test_a_guest_that_is_not_running_is_refused_naming_start(self):
        self.w.vms["wk-ws"] = "stopped"
        self.assertEqual(self.scp(":a.txt", "/here/out.txt"), (1, "error: 'ws' is not running (wk start ws)"))
        self.assertEqual(self.copies(), [])


class TestBuildBoxRoute(ScpFlow):
    """A build box: every question and every copy goes through the box's own ssh, the argv `wk exec` uses."""
    KIND = "remote"
    SRC = "/home/u/wk/ws/ws/WebKit"

    def ssh(self, cmd):
        return SSH + ("\"$SHELL\" -lc %s" % shlex.quote(cmd),)

    def test_a_file_goes_in_and_a_directory_comes_out_through_the_boxs_ssh_after_asking_over_it(self):
        self.w.there[self.SRC + "/tree"] = "dir"
        self.assertEqual(self.scp("/here/a.txt", ":b.txt"), (0, "==> copied /here/a.txt to ws:%s/b.txt" % self.SRC))
        self.assertEqual(self.scp("-r", ":tree", "/here/out")[0], 0)
        self.assertIn(self.ssh("/bin/sh -c '%s'" % probe(self.SRC + "/tree")), [r[1] for r in self.ran()])
        (put, get) = self.copies()
        self.assertEqual(put, ("run", ("bash", "-c", PIPE_IN, "bash", "/here/a.txt") + self.ssh("cat > %s/b.txt" % self.SRC)))
        self.assertEqual(get[1][5:-1], SSH)
        self.assertIn("tar -cf - -C %s/tree" % self.SRC, get[1][-1])

    def test_on_a_box_that_is_this_machine_the_bytes_are_copied_here(self):
        self.w.env["WK_REMOTE_LOCAL"] = "1"
        self.assertEqual(self.scp("/here/a.txt", ":b.txt")[0], 0)
        self.assertEqual(self.scp("-r", "/here/tree", ":tree")[0], 0)
        self.assertEqual(self.copies(), [("copy_in", "/here/a.txt", self.SRC + "/b.txt"), ("copy_tree_in", "/here/tree", self.SRC + "/tree")])
        self.assertEqual((self.w.files[self.SRC + "/b.txt"], self.w.files[self.SRC + "/tree/sub/c"]), (b"one\n", b"c\n"))


if __name__ == "__main__":
    unittest.main()
