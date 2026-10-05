"""lib/wk/store.py: the paths a setup stage evals from `python3 -m wk.store paths`
are Store's, under every environment they are read in: a Linux machine, a macOS
host, the podman VM, and a test's scratch store; and its Snapshots say which
snapshot a workspace may be made from, over a fake machine.

Run: python3 tests/run.py -k tests.test_wk_store
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import record  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Snapshots, Store  # noqa: E402

PAIRS = (
    ("WK_STORE", lambda s: s.store_dir()),
    ("keyring_dir", lambda s: s.keyring_dir()),
    ("keyring_agent_rw_dir", lambda s: s.keyring_agent_rw_dir()),
    ("keyring_push_dir", lambda s: s.keyring_push_dir()),
    ("mirror_parent", lambda s: os.path.dirname(s.mirror_dir())),
)



class TestAStageAsksPython(unittest.TestCase):
    """lib/common.sh's wk_eval and wk_py, what every setup stage reads the store and the envelope through."""

    def test_a_failed_answer_ends_the_stage(self):
        cp = bash('set -e\n. "$WK_ROOT/lib/common.sh"\nwk_eval wk.store no-such-verb\necho REACHED\n')
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("REACHED", cp.stdout)
        self.assertIn("wk.store no-such-verb failed", cp.stderr)

    def test_the_stages_store_reaches_python(self):
        """A stage that evals a non-default store asks about that store, not the default one."""
        cp = bash('. "$WK_ROOT/lib/common.sh"\nWK_STORE=/tmp/wk-elsewhere\nwk_py wk.resources --os linux headless-marker\n',
                  env={"WK_STORE": ""})
        self.assertEqual(cp.stdout.strip(), "/tmp/wk-elsewhere/.headless", cp.stderr)


class TestStoreInitSaysWhatItChanged(unittest.TestCase):
    """`python3 -m wk wk.places store-init` prints a line per path it made or changed, so the setup stage can count it."""

    def run_init(self, tmp):
        env = {"WK_STORE": tmp + "/store", "WK_HOST_SECRETS": tmp + "/secrets", "XDG_STATE_HOME": tmp + "/state",
               "HOME": tmp, "WK_MACHINES_DIR": tmp + "/machines", "XDG_CONFIG_HOME": tmp + "/config"}
        cp = bash('PYTHONPATH="$WK_ROOT/lib" python3 -m wk wk.places store-init', env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.splitlines()

    def test_a_first_run_names_what_it_made_and_a_second_names_nothing(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-store-init-")
        self.addCleanup(lambda: os.system("rm -rf '%s'" % tmp))
        first = self.run_init(tmp)
        self.assertIn(tmp + "/store/cache/ccache/ccache.conf", first)
        self.assertEqual([], self.run_init(tmp))

class TestTheStagesReadTheStore(unittest.TestCase):
    def envs(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-store- x")
        self.addCleanup(lambda: os.system("rm -rf '%s'" % tmp))
        base = {"HOME": tmp, "XDG_STATE_HOME": tmp + "/state", "WK_HOST_SECRETS": tmp + "/host secrets"}
        yield "scratch store", dict(base, WK_STORE=tmp + "/store")
        yield "in the podman VM", dict(base, WK_IN_VM="1", WK_STORE="/var/lib/wk")
        yield "the default store", dict(base)

    def bash_paths(self, env):
        script = '. "$WK_ROOT/lib/common.sh"; eval "$(wk_py wk.store paths)"\n' + "".join(
            "printf '%%s\\n' \"$%s\"\n" % name for name, _ in PAIRS)
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
            for name, path in PAIRS:
                with self.subTest(env=label, path=name):
                    self.assertEqual(path(store), theirs[name])

    def test_any_other_verb_is_refused_with_the_usage(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; wk_py wk.store mirror')
        self.assertEqual(cp.returncode, 2)
        self.assertIn("usage: python3 -m wk.store [-h] {paths}", cp.stderr)

    def test_workspace_paths(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-store-")
        self.addCleanup(lambda: os.system("rm -rf %s" % tmp))
        s = Store({"WK_STORE": tmp, "HOME": tmp})
        os.makedirs(s.ws_dir("a"))
        with open(os.path.join(s.ws_dir("a"), "base-id"), "w") as f:
            f.write("main-1\n")
        self.assertEqual(s.workspaces(), ["a"])
        self.assertEqual(s.ws_snapshot_id("a"), "main-1")
        self.assertIsNone(s.ws_snapshot_id("b"))
        self.assertEqual(s.snapshot_tree("main-1"), os.path.join(tmp, "base", "main-1", "WebKit"))
        self.assertEqual(s.keyring_view_dir("container"), os.path.join(s.keyring_dir(), "view", "container"))

    def test_a_lock_path_is_per_resource_and_per_host(self):
        store = Store({"HOME": "/h", "WK_LOCK_DIR": "/l"})
        self.assertEqual("/l/ws-a@%s.lock" % (record.host_name() or "local"), store.lock_path("ws-a"))


class TestRecords(unittest.TestCase):
    """The records and the cache live in the store, except on a macOS host whose store is the podman
    VM's: there they are this host's own, under ~/.local/state/wk."""

    def paths(self, env, system):
        with mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname=system)):
            s = Store(dict({"HOME": "/h", "XDG_STATE_HOME": "/state"}, **env))
            return s.records_dir(), s.cache_dir()

    def test_a_named_store_holds_them(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                self.assertEqual(("/s", "/s/cache"), self.paths({"WK_STORE": "/s"}, system))

    def test_a_macos_hosts_default_store_leaves_them_on_the_host(self):
        self.assertEqual(("/state/wk", "/state/wk/cache"), self.paths({}, "Darwin"))

    def test_in_the_podman_vm_they_are_the_stores(self):
        self.assertEqual(("/var/lib/wk", "/var/lib/wk/cache"), self.paths({"WK_IN_VM": "1"}, "Darwin"))



class TestSnapshots(unittest.TestCase):
    """A snapshot is handed out only when it finished publishing, is untouched since, and is on the branch it
    records; `wk gc` keeps the newest finished one whatever it records."""

    SHA = "a" * 40

    def setUp(self):
        self.m = Fake()
        self.store = Store({"WK_STORE": "/s", "HOME": "/h"})
        self.bases = Snapshots(self.store, self.m)
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
        self.assertEqual(Snapshots(Store({"WK_STORE": "/none"}), self.m).current(), "")

    def test_a_pin_is_the_record_and_its_absence_is_unpinned(self):
        self.m.dirs.update({"/s/ws", "/s/ws/a", "/s/ws/b"})
        self.m.files["/s/ws/a/base-id"] = "20260101\n"
        self.assertEqual((self.bases.workspaces(), self.bases.pin("a"), self.bases.pin("b")), (["a", "b"], "20260101", None))
        self.assertEqual(self.bases.unpinned(), ["b"])


class TestStoreOverrides(unittest.TestCase):
    def test_the_broker_socket_is_named_or_where_each_side_finds_it(self):
        self.assertEqual(Store({"WK_BROKER_SOCKET": "/s"}).runtime_socket(), "/s")
        self.assertEqual(Store({"WK_BROKER_SOCKET": "/s"}).workspace_runtime_socket(), "/s")
        self.assertEqual(Store({"XDG_RUNTIME_DIR": "/run/u"}).runtime_socket(), "/run/u/wk/broker.sock")
        for mac, sock in ((False, "/run/wk/broker.sock"), (True, "/h/.wk-broker.sock")):
            with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=mac):
                self.assertEqual(Store({"HOME": "/h"}).workspace_runtime_socket(), sock, "a macOS workspace is a guest, with no /run")

    def test_a_disk_admission_measures_the_named_store_else_home(self):
        self.assertEqual(Store({"WK_STORE": "/st", "HOME": "/h"}).admission_dir(), "/st")
        self.assertEqual(Store({"HOME": "/h"}).admission_dir(), "/h")

    def test_the_container_mirror_is_only_what_the_env_names(self):
        self.assertIsNone(Store({}).container_mirror_dir())
        self.assertEqual(Store({"WK_MIRROR": "/m"}).container_mirror_dir(), "/m")


if __name__ == "__main__":
    unittest.main()
