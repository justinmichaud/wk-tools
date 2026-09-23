"""lib/wk/store.py agrees with lib/store.sh on every path, under every
environment the two are read in: a Linux machine, a macOS host, the podman
VM, and a test's scratch store.

Run: python3 tests/run.py -k tests.test_wk_store
"""
import os
import sys
import tempfile
import unittest

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import record  # noqa: E402
from wk.store import Store  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
