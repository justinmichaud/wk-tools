"""`wk bench run --cores`: the cpu-list syntax, the pin a run is exec'd through and records
(lib/wk/bench/pipeline.py), which systems refuse one (lib/wk/bench/systems.py), and the warning
`wk bench compare` gives two runs pinned differently.

Rows landed here: `unit bench.pins_cores`, `live bench.pins_cores[container]`.

Run: python3 tests/run.py -k test_bench_cores
"""
import json
import unittest

from tests.support import WkTest, bench_ls_runs, podman_vm_ssh, requires_podman_vm, run, scratch_dir
from tests.test_bench_pipeline import BenchTest, World
from tests.test_bench_report import rep

from wk.bench import pipeline, record, systems


def env_record(path, *fields):
    record.write_env(str(path), list(fields))


class TestCoresValid(unittest.TestCase):
    """The syntax `--cores` accepts, checked before a preflight is spent on it; a cpu the machine does
    not have is taskset's to refuse."""

    def test_accepts_every_documented_shape(self):
        for spec in ("0-3", "2,3", "0-1,4", "7"):
            with self.subTest(spec=spec):
                self.assertTrue(pipeline.cores_valid(spec))

    def test_refuses_garbage(self):
        for spec in ("", "a", "1-", "-1", "1,,2"):
            with self.subTest(spec=spec):
                self.assertFalse(pipeline.cores_valid(spec))


class TestAPinnedRun(BenchTest):
    """`bench.pins_cores`: the run is exec'd through the pin it records."""

    def test_a_container_run_is_exec_d_under_taskset_and_records_it(self):
        rc, err = self.run_(None, "run", "jetstream3", "--config", "jsc-release", "--cores", "0-3")
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
                      self.said("run", "jetstream3", "--config", "jsc-release", "--cores", "0-1", w=w))
        self.run_(w, "run", "jetstream3", "--config", "jsc-release")
        self.assertEqual(self.env_json(w)["host"]["cores"], "4")

    def test_an_invalid_set_is_refused_before_anything_runs(self):
        self.assertIn("is not a valid Linux cpu list", self.said("run", "jetstream3", "--cores", "a-b"))
        self.assertEqual(self.w.effects, [])

    def test_only_the_container_pins(self):
        self.assertEqual(systems.ContainerSystem.cores_refusal(None), "")
        self.assertIn("no pin exists on macOS", systems.GuestSystem.cores_refusal(None))


@requires_podman_vm()
class TestAPinnedRunLive(WkTest):
    """`live bench.pins_cores[container]`: a real run in a container workspace that already has a
    jsc-release build records the pin it ran under."""

    def test_the_record_carries_the_pin(self):
        cp = run("ls", timeout=45)
        names = [l.split()[0] for l in cp.stdout.splitlines() if len(l.split()) > 1 and l.split()[1] == "container"]
        for ws in names:
            b = run("bench", "run", ws, "sunspider1.0.2", "--config", "jsc-release", "--count", "1", "--cores", "0", timeout=300)
            if b.returncode == 0:
                break
        else:
            self.skipTest("no container workspace with a jsc-release build to bench in")
        rid = bench_ls_runs(run("bench", "ls", timeout=60).stdout)[-1].split("/bench/", 1)[1]
        env = json.loads(podman_vm_ssh("cat /var/lib/wk/bench/%s/env.json" % rid).stdout)
        self.assertEqual(env["cores"], {"set": "0", "pinned": True})


class TestCoresAxisWarning(WkTest):
    """`wk bench compare` (lib/wk/bench/report.py's axis check) warns when two
    runs' `cores.set` differ, the same way it already warns on a runner or
    session-mode mismatch -- and stays quiet when they agree."""

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

    def test_different_core_sets_warn(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, "0-3", "4-7")
            cp = rep(a, b)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("different core pins", cp.stdout)
            self.assertIn("0-3", cp.stdout)
            self.assertIn("4-7", cp.stdout)

    def test_equal_core_sets_do_not_warn(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, "0-3", "0-3")
            cp = rep(a, b)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertNotIn("different core pins", cp.stdout)

    def test_unpinned_vs_pinned_warns(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, None, "0-3")
            cp = rep(a, b)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("different core pins", cp.stdout)
            self.assertIn("unpinned", cp.stdout)

    def test_both_unpinned_does_not_warn(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, None, None)
            cp = rep(a, b)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertNotIn("different core pins", cp.stdout)


if __name__ == "__main__":
    unittest.main()
