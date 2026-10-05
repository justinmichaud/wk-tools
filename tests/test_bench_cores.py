"""`wk bench run --cores`: the cpu-list syntax, the pin a run records, and the warning for two runs pinned differently."""
import json
import unittest

from tests.support import WkTest, scratch_dir
from tests.test_bench_pipeline import BenchTest, World
from tests.test_bench_report import rep

from wk.bench import pipeline, record


def env_record(path, *fields):
    record.write_env(str(path), list(fields))


class TestCoresValid(unittest.TestCase):

    def test_the_documented_shapes_and_nothing_else(self):
        for spec, ok in (("0-3", True), ("2,3", True), ("0-1,4", True), ("7", True),
                         ("", False), ("a", False), ("1-", False), ("-1", False), ("1,,2", False)):
            with self.subTest(spec=spec):
                self.assertEqual(bool(pipeline.cores_valid(spec)), ok)


class TestAPinnedRun(BenchTest):

    def test_a_container_run_is_exec_d_under_taskset_and_records_it(self):
        rc, err = self.run_(None, "run", "jetstream3", "--preset", "jsc-release", "--cores", "0-3")
        self.assertEqual(rc, 0, err)
        self.assertIn("exec taskset -c 0-3 ", self.w.watched[0][-1])
        self.assertEqual(self.env_json()["cores"], {"set": "0-3", "pinned": True})

    def test_an_unpinned_run_records_that_it_was_not(self):
        self.run_()
        self.assertNotIn("taskset", self.w.watched[0][-1])
        self.assertEqual(self.env_json()["cores"], {"set": "", "pinned": False})

    def test_a_guest_records_its_vcpu_count_and_refuses_a_pin(self):
        w = World(self.tmp, "vm")
        self.assertIn("no pin exists on macOS; the guest's vCPU count is not a pin",
                      self.said("run", "jetstream3", "--preset", "jsc-release", "--cores", "0-1", w=w))
        self.run_(w, "run", "jetstream3", "--preset", "jsc-release")
        self.assertEqual(self.env_json(w)["host"]["cores"], "4")

    def test_an_invalid_set_is_refused_before_anything_runs(self):
        self.assertIn("is not a valid Linux cpu list", self.said("run", "jetstream3", "--cores", "a-b"))
        self.assertEqual(self.w.effects, [])


class TestCoresAxisWarning(WkTest):

    def _pair(self, tmp, a_cores, b_cores):
        a_dir, b_dir = tmp / "a", tmp / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        doc = {"JetStream3.0": {"tests": {"t": {"metrics": {"Score": {"current": [1.0, 2.0]}}}}}}
        (a_dir / "result.json").write_text(json.dumps(doc))
        (b_dir / "result.json").write_text(json.dumps(doc))
        base = ["plan=jetstream3", "class=cpu", "runner=jsc", "bench_host=container"]
        a_extra = [f"cores.set={a_cores}"] if a_cores is not None else []
        b_extra = [f"cores.set={b_cores}"] if b_cores is not None else []
        env_record(a_dir / "env.json", *base, *a_extra)
        env_record(b_dir / "env.json", *base, *b_extra)
        return a_dir, b_dir

    def test_two_runs_pinned_differently_warn(self):
        for a_cores, b_cores, warns in (("0-3", "4-7", ("0-3", "4-7")), ("0-3", "0-3", None),
                                        (None, "0-3", ("unpinned",)), (None, None, None)):
            with self.subTest(a=a_cores, b=b_cores), scratch_dir() as tmp:
                cp = rep(*self._pair(tmp, a_cores, b_cores))
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual("different core pins" in cp.stdout, bool(warns), cp.stdout)
                for word in warns or ():
                    self.assertIn(word, cp.stdout)


if __name__ == "__main__":
    unittest.main()
