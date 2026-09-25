"""`wk gc` trims tart's pulled-image cache (lib/wk/sysimage/guestbase.py's rubble): the guests are pulled from an OCI
registry, so what tart keeps beside them is re-downloadable and belongs to a budget the way ccache does. A local VM is
a workspace and goes with `wk rm`, so the prune is tart's own default, caches only; the golden base is named with the
command that erases it. Driven over the fake machine and fake vm target of tests/test_owed_gc.py.

Run: python3 tests/run.py --unit -k test_gc_tart_cache
"""
import unittest

from tests.test_owed_gc import FakeVm, GcTest, _seed


class TestTheCacheIsTrimmedToItsBudget(GcTest):
    def prunes(self):
        return [a for a in self.w.acts() if a[:2] == ("/bin/tart", "prune")]

    def with_cache(self, **env):
        self.w.env.update(env)
        self.w.vm = FakeVm(self.w, [])
        self.w.mkdirs("/tart/cache")

    def test_it_prunes_to_the_declared_budget(self):
        self.with_cache(WK_TART_CACHE_GB="7")
        rc, err = self.run_gc()
        self.assertEqual((rc, self.prunes()), (0, [("/bin/tart", "prune", "--space-budget", "7")]), err)

    def test_it_prunes_caches_and_never_the_guests(self):
        self.with_cache()
        self.run_gc()
        self.assertEqual(self.prunes(), [("/bin/tart", "prune", "--space-budget", "20")])

    def test_a_failing_tart_is_reported_and_the_rest_still_goes(self):
        self.with_cache()
        self.w.answer(["/bin/tart", "prune"], rc=1)
        gone = _seed(self.w)
        rc, err = self.run_gc()
        self.assertEqual(rc, 1)
        self.assertTrue(gone(), err)

    def test_without_tart_there_is_nothing_to_trim(self):
        self.w.vm = FakeVm(self.w, [])
        self.w.vm.tart = lambda: None
        self.w.mkdirs("/tart/cache")
        self.assertEqual([r.kind for r in self.rows()], [])

    def test_the_golden_base_is_named_never_taken(self):
        self.w.vm = FakeVm(self.w, ["wk-base"])
        (r,) = self.rows()
        self.assertEqual((r.kind, r.flag, r.take), ("guest-base", "wk sysimage build macos-guest-base --rm", None))


if __name__ == "__main__":
    unittest.main()
