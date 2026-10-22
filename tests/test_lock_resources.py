"""Two commands mutating one resource serialise or refuse naming the holder, each driven through the command's own
entry with the resource held by a live process on the Fake. `guest start` is tests/test_guest.py's; a workspace's job
is a hold on its record (tests/test_wk_record.py)."""
import contextlib
import io
import os
import sys
import unittest

from tests import test_sync, test_vm_base
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.lock import Lock  # noqa: E402

HOLDER = 4242


def hold(fake, store, resource):
    """`resource`'s lock, held by a pid alive in `fake`'s process table."""
    path = store.lock_path(resource)
    fake.mkdir_now(os.path.dirname(path))
    fake.symlink("pid=%d tok=beef at=2000-01-01T00:00:00Z cmd=wk" % HOLDER, path)
    fake.pids.add(HOLDER)


def refusal(fn):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            fn()
        except Refused:
            return err.getvalue()
    raise AssertionError("the command ran under another command's lock")


class TestTwoSyncs(test_sync.SyncTest):
    def test_one_lock_per_resource_sync(self):
        """A mirror refresh waits on the store lock, names its holder, and refuses when the holder outlasts it."""
        hold(self.w, self.w.store, "store")
        err = refusal(self.w.sync("mirror").run)
        self.assertIn(str(HOLDER), err)
        self.assertNotIn(("run", ("sh", "-c", "REFRESH %s" % self.w.mirror)), self.w.effects)

    def test_a_dead_holder_does_not_stop_the_sync(self):
        hold(self.w, self.w.store, "store")
        self.w.pids.discard(HOLDER)
        self.stderr(self.w.sync("mirror").run)
        self.assertIn(("run", ("sh", "-c", "REFRESH %s" % self.w.mirror)), self.w.effects)


class TestBaseRefreshDuringABuild(test_vm_base.BaseTest):
    def test_one_lock_per_resource_base_refresh(self):
        """`--refresh` while the base is being built waits on the guest-base lock, then refuses naming the builder."""
        self.w.state = "stopped"
        base = self.base()
        hold(self.w, base.vm.store, "guest-base")
        rc, err = self.build("--refresh")
        self.assertEqual(1, rc)
        self.assertIn(str(HOLDER), err)
        self.assertEqual([], self.tart_acts())


if __name__ == "__main__":
    unittest.main()
