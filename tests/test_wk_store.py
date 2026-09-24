"""lib/wk/store.py agrees with lib/store.sh on every path, under every
environment the two are read in: a Linux machine, a macOS host, the podman
VM, and a test's scratch store; and its Bases say which snapshot a workspace
may be made from, over a fake machine.

Run: python3 tests/run.py -k tests.test_wk_store
"""
import os
import sys
import tempfile
import unittest

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import record  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Bases, Store  # noqa: E402

PAIRS = (
    ("root", "printf %s \"$WK_STORE\""),
    ("record_dir", "wk_record_dir"),
    ("mirror", "wk_mirror"),
    ("base_dir", "wk_base_dir"),
    ("secrets_dir", "wk_secrets_dir"),
    ("agent_rw_dir", "wk_agent_rw_dir"),
    ("bench_dir", "wk_bench_dir"),
    ("artifact_dir", "wk_artifact_dir"),
    ("machine_store", "wk_machine_store"),
)


class TestAgreesWithBash(unittest.TestCase):
    def envs(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-store-")
        self.addCleanup(lambda: os.system("rm -rf %s" % tmp))
        base = {"HOME": tmp, "XDG_STATE_HOME": tmp + "/state", "WK_HOST_SECRETS": tmp + "/hostsecrets"}
        yield "scratch store", dict(base, WK_STORE=tmp + "/store")
        yield "in the podman VM", dict(base, WK_IN_VM="1", WK_STORE="/var/lib/wk")
        yield "the default store", dict(base)

    def bash_paths(self, env):
        script = ". \"$WK_ROOT/lib/common.sh\"; . \"$WK_ROOT/lib/store.sh\"\n" + "".join(
            "printf '%%s\\n' \"$(%s)\"\n" % fn for _, fn in PAIRS)
        cp = bash(script, env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return dict(zip((name for name, _ in PAIRS), cp.stdout.splitlines()))

    def test_every_path_under_every_environment(self):
        for label, env in self.envs():
            full = dict(os.environ)
            for k in ("WK_STORE", "WK_IN_VM", "WK_STORE_DEFAULT"):
                full.pop(k, None)
            full.update(env)
            store = Store(full)
            theirs = self.bash_paths(env)
            for name, _ in PAIRS:
                with self.subTest(env=label, path=name):
                    self.assertEqual(getattr(store, name)(), theirs[name])

    def test_workspace_paths(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-store-")
        self.addCleanup(lambda: os.system("rm -rf %s" % tmp))
        s = Store({"WK_STORE": tmp, "HOME": tmp})
        os.makedirs(s.ws_dir("a"))
        with open(os.path.join(s.ws_dir("a"), "base-id"), "w") as f:
            f.write("main-1\n")
        self.assertEqual(s.workspaces(), ["a"])
        self.assertEqual(s.ws_base_id("a"), "main-1")
        self.assertIsNone(s.ws_base_id("b"))
        self.assertEqual(s.base_path("main-1"), os.path.join(tmp, "base", "main-1", "WebKit"))
        self.assertEqual(s.secrets_view_dir("container"), os.path.join(s.secrets_dir(), "view", "container"))

    def test_a_lock_path_is_the_one_bash_takes(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-store-")
        self.addCleanup(lambda: os.system("rm -rf %s" % tmp))
        env = {"HOME": tmp, "WK_LOCK_DIR": tmp + "/locks"}
        cp = bash('. "$WK_ROOT/lib/common.sh"; _lock_path ws-a', env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(Store(dict(os.environ, **env)).lock_path("ws-a"), cp.stdout.strip())
        self.assertEqual(cp.stdout.strip(), tmp + "/locks/ws-a@%s.lock" % (record.host_name() or "local"))



class TestBases(unittest.TestCase):
    """A snapshot is handed out only when it finished publishing, is untouched since, and is on the branch it
    records; `wk gc` keeps the newest finished one whatever it records."""

    SHA = "a" * 40

    def setUp(self):
        self.m = Fake()
        self.store = Store({"WK_STORE": "/s", "HOME": "/h"})
        self.bases = Bases(self.store, self.m)
        self.heads, self.ups = {}, {}
        self.m.react(["git", "-C"], self._git)

    def _git(self, argv, f):
        bid = argv[2].split("/")[-2]
        if argv[3:] == ["rev-parse", "HEAD"]:
            return Result(0, self.heads.get(bid, self.SHA) + "\n")
        if argv[3:5] == ["symbolic-ref", "--quiet"]:
            return Result(0, "refs/heads/main\n") if bid not in self.ups else Result(1)
        return Result(0, self.ups.get(bid, "origin/main") + "\n")

    def publish(self, bid, sha=SHA, branch="origin/main", git=True):
        d = "/s/base/" + bid
        self.m.dirs.update({"/s/base", d, d + "/WebKit"} | ({d + "/WebKit/.git"} if git else set()))
        if sha is not None:
            self.m.files[d + "/sha"] = sha + "\n"
        if branch is not None:
            self.m.files[d + "/branch"] = branch + "\n"

    def test_a_good_one_says_nothing(self):
        self.publish("1")
        self.assertEqual(self.bases.verify("1"), "")

    def test_each_refusal_names_what_is_wrong(self):
        self.publish("unfinished", sha=None)
        self.publish("nogit", git=False)
        self.publish("moved")
        self.heads["moved"] = "b" * 40
        self.publish("nobranch", branch=None)
        self.publish("detached")
        self.ups["detached"] = ""
        for bid, words in (("absent", "does not exist"), ("unfinished", "never finished publishing"),
                           ("nogit", "is not a git checkout"), ("moved", "no longer matches what was published"),
                           ("nobranch", "does not record the branch"), ("detached", "HEAD is\n    a raw sha")):
            with self.subTest(bid=bid):
                self.assertIn(words, self.bases.verify(bid))

    def test_current_is_the_newest_that_verifies_and_newest_complete_ignores_the_branch(self):
        self.publish("20260101")
        self.publish("20260202", branch=None)
        self.publish("20260303", sha=None)
        self.assertEqual(self.bases.current(), "20260101")
        self.assertEqual(self.bases.newest_complete(), "20260202")
        self.assertEqual(Bases(Store({"WK_STORE": "/none"}), self.m).current(), "")

    def test_a_pin_is_the_record_and_its_absence_is_unpinned(self):
        self.m.dirs.update({"/s/ws", "/s/ws/a", "/s/ws/b"})
        self.m.files["/s/ws/a/base-id"] = "20260101\n"
        self.assertEqual((self.bases.workspaces(), self.bases.pin("a"), self.bases.pin("b")), (["a", "b"], "20260101", None))
        self.assertEqual(self.bases.unpinned(), ["b"])

    def test_the_bash_names_answer_from_the_same_store(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-store-")
        self.addCleanup(lambda: os.system("rm -rf %s" % tmp))
        os.makedirs(tmp + "/ws/a")
        os.makedirs(tmp + "/base/1/WebKit")
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; WK_STORE=%s\n'
                  'list_workspaces; unpinned_workspaces; base_verify 1 || echo refused' % tmp)
        self.assertEqual(cp.stdout.splitlines()[:2], ["a", "a"], cp.stderr)
        self.assertIn("never finished publishing", cp.stdout)
        self.assertEqual(cp.stdout.splitlines()[-1], "refused")

if __name__ == "__main__":
    unittest.main()
