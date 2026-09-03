"""`wk scp` -- one file or one directory in or out of a workspace.

Three things are held to here:

    the shape is refused before anything moves. Exactly one side carries the
      ':' that marks the workspace, and a bad invocation exits 2 with the
      synopsis -- the same lines the dispatcher prints for a missing operand
    the bytes arrive intact, both ways, for a file and for a tree. The local
      target is a real filesystem, so these compare the bytes rather than a
      transcript of a copy
    each driver moves them the way it already moves bytes -- `podman cp` for
      a container, scp and rsync for a guest or a build machine -- and never
      through t_exec, which is a login shell (or wkdev-enter) and not a byte
      pipe

Nothing here touches a real container, guest or build machine: `podman`,
`tart`, `ssh`, `scp` and `rsync` are stubs on PATH that log their argv, and
the "workspace" is a scratch directory.

Run: python3 -m unittest tests.test_scp -v
"""
import os
import subprocess
import unittest

from tests.support import (
    REPO, WkTest, bash, fake_workspace, run, stub_path,
)

TOUCHED = ("cmd/scp", "lib/target.sh", "targets/container.sh",
           "targets/vm.sh", "targets/remote.sh")

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


class TestScriptsParse(unittest.TestCase):
    def test_bash_n(self):
        for f in TOUCHED:
            with self.subTest(script=f):
                cp = subprocess.run(["bash", "-n", str(REPO / f)],
                                    capture_output=True, text=True, timeout=60)
                self.assertEqual(cp.returncode, 0, cp.stderr)


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


class TestDriverTransports(WkTest):
    """What each driver actually runs. The bytes never go through t_exec: a
    container's exec is wkdev-enter and a guest's is a login shell, and
    neither is a byte pipe (targets/container.sh's t_pull says what that cost
    the last time -- 1396 bytes arrived as 1399)."""

    def _lift(self, target, calls, env):
        script = ('. "$WK_ROOT/lib/common.sh"\n'
                  '. "$WK_ROOT/lib/store.sh"\n'
                  '. "$WK_ROOT/lib/target.sh"\n'
                  f'load_target {target} >/dev/null 2>&1\n' + calls)
        cp = bash(script, env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp

    def test_a_container_moves_bytes_with_podman_cp(self):
        log = self.tmp / "podman.log"
        log.write_text("")
        with stub_path({"podman": FAKE_PODMAN}) as binp:
            cp = self._lift("container", '''
echo "kind=$(t_path_kind demo /src/WebKit/a.txt)"
t_push demo /tmp/local.txt /src/WebKit/a.txt
t_pull demo /src/WebKit/a.txt /tmp/out.txt
t_push_dir demo /tmp/tree /src/WebKit/tree
t_pull_dir demo /src/WebKit/tree /tmp/tree
''', env={"PATH": f"{binp}:{os.environ['PATH']}",
          "WK_TEST_PODMAN_LOG": str(log)})
        text = log.read_text()
        self.assertIn("kind=file", cp.stdout)
        # In, out, and both directions of a tree -- `podman cp` each time.
        self.assertIn("cp /tmp/local.txt wk-demo:/src/WebKit/a.txt", text)
        self.assertIn("cp wk-demo:/src/WebKit/a.txt /tmp/out.txt", text)
        self.assertIn("cp /tmp/tree/. wk-demo:/src/WebKit/tree", text)
        self.assertIn("cp wk-demo:/src/WebKit/tree/. /tmp/tree", text)
        # A tree's destination is emptied first, as the container's own user,
        # so the copy is a copy and the directory is not root's.
        self.assertIn("exec --user dev wk-demo /bin/sh -c rm -rf "
                      "'/src/WebKit/tree' && mkdir -p '/src/WebKit/tree'", text)
        for word in ("tar", "wkdev-enter"):
            self.assertNotIn(word, text, f"bytes went through {word}")

    def test_a_container_reaches_podman_the_way_the_host_does(self):
        """`_hpodman`, not `_podman`: on a macOS host this runs outside the
        podman VM, where the default connection is the rootful one."""
        text = (REPO / "targets" / "container.sh").read_text()
        for fn in ("t_push()", "t_push_dir()", "t_pull()", "t_pull_dir()",
                   "t_path_kind()"):
            body = text.split(fn, 1)[1].split("\n}\n", 1)[0]
            self.assertNotIn("_podman ", body, f"{fn} bypasses _hpodman")

    def test_a_guest_moves_bytes_with_scp_and_rsync(self):
        log = self.tmp / "net.log"
        log.write_text("")
        with stub_path({"tart": FAKE_TART, "ssh": _net_stub("ssh"),
                        "scp": _net_stub("scp"), "rsync": _net_stub("rsync")}) as binp:
            cp = self._lift("vm", '''
echo "kind=$(t_path_kind demo /Users/admin/WebKit/a.txt)"
t_push demo /tmp/local.txt /Users/admin/WebKit/a.txt
t_push_dir demo /tmp/tree /Users/admin/WebKit/tree
''', env={"PATH": f"{binp}:{os.environ['PATH']}",
          "WK_TEST_NET_LOG": str(log),
          "WK_VM_STORE": str(self.tmp / "vmstore")})
        text = log.read_text()
        self.assertIn("kind=absent", cp.stdout)
        self.assertIn("/tmp/local.txt admin@1.2.3.4:/Users/admin/WebKit/a.txt",
                      text)
        self.assertTrue(
            any(l.startswith("scp ") for l in text.splitlines()), text)
        self.assertIn("rsync -a --delete -e ssh", text)
        self.assertIn("/tmp/tree/ admin@1.2.3.4:/Users/admin/WebKit/tree/", text)
        # The question is asked over ssh, and answered with one word.
        self.assertTrue(
            any(l.startswith("ssh ") and "echo dir" in l
                for l in text.splitlines()), text)

    def test_a_build_machine_moves_bytes_with_scp_and_rsync(self):
        log = self.tmp / "net.log"
        log.write_text("")
        with stub_path({"ssh": _net_stub("ssh"), "scp": _net_stub("scp"),
                        "rsync": _net_stub("rsync")}) as binp:
            self._lift("remote", '''
t_push demo /tmp/local.txt /wk/ws/demo/WebKit/a.txt
t_push_dir demo /tmp/tree /wk/ws/demo/WebKit/tree
''', env={"PATH": f"{binp}:{os.environ['PATH']}",
          "WK_TEST_NET_LOG": str(log),
          "XDG_STATE_HOME": str(self.tmp / "state"),
          "WK_REMOTE_HOST": "fakebox", "WK_REMOTE_ROOT": "/wk"})
        text = log.read_text()
        self.assertIn("/tmp/local.txt fakebox:/wk/ws/demo/WebKit/a.txt", text)
        self.assertIn("/tmp/tree/ fakebox:/wk/ws/demo/WebKit/tree/", text)

    def test_a_machine_that_is_its_own_host_copies_locally(self):
        """On the build machine itself there is nothing to connect to
        (WK_REMOTE_LOCAL in its conf), the same split every other function
        there makes -- and these are real copies on a real filesystem."""
        d = self.tmp / "box"
        (d / "src").mkdir(parents=True)
        (d / "src" / "a").write_bytes(b"a\n")
        (d / "one.txt").write_bytes(b"one\n")
        registry = self.tmp / "hosts"
        registry.mkdir()
        (registry / "fakebox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_LOCAL=1\n"
            f"WK_REMOTE_ROOT={d}\n"
        )
        self._lift("fakebox", f'''
t_push demo {d}/one.txt {d}/two.txt
t_push_dir demo {d}/src {d}/dst
echo "kind=$(t_path_kind demo {d}/one.txt)"
''', env={"XDG_STATE_HOME": str(self.tmp / "state"),
          "WK_TARGET_REGISTRY": str(registry)})
        self.assertEqual((d / "two.txt").read_bytes(), b"one\n")
        self.assertEqual((d / "dst" / "a").read_bytes(), b"a\n")


if __name__ == "__main__":
    unittest.main()
