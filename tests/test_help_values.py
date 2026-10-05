"""Every closed-set value a command declares appears in its `-h`."""
import subprocess
import sys
from pathlib import Path
import unittest

from tests.support import REPO, run_here, scratch_dir, temp_store

sys.path.insert(0, str(REPO / "lib"))
from wk.completion import values_cmd  # noqa: E402
from wk.decl import all_commands, leading_block  # noqa: E402
from wk.store import Store  # noqa: E402

TIER = "lint"

DECLARED_VALUES = {d.name: d for d in all_commands(REPO) if values_cmd(d)}


class TestDeclaredValuesReachTheHelp(unittest.TestCase):
    def test_the_help_prints_what_the_flag_prints(self):
        self.assertTrue(DECLARED_VALUES)
        for cmd, d in DECLARED_VALUES.items():
            listed = subprocess.run([str(REPO / "wk"), cmd, d.values], cwd=str(REPO), capture_output=True, text=True, timeout=60)
            self.assertEqual(listed.returncode, 0, f"wk {cmd} {d.values} failed: {listed.stdout}{listed.stderr}")
            values = [l.split()[0] for l in (listed.stdout + listed.stderr).splitlines() if l[:1].isalnum()]
            self.assertTrue(values, f"wk {cmd} {d.values} listed nothing")
            cp = subprocess.run([str(REPO / "wk"), cmd, "-h"], cwd=str(REPO), capture_output=True, text=True, timeout=30)
            with self.subTest(cmd=cmd):
                self.assertIn("valid values", cp.stdout + cp.stderr)
                for v in values:
                    self.assertIn(v, cp.stdout + cp.stderr, f"wk {cmd} -h omits '{v}'")

    def test_a_value_flag_is_read_only(self):
        for cmd, d in DECLARED_VALUES.items():
            with self.subTest(cmd=cmd):
                head = "".join(leading_block(d.path))
                self.assertTrue("readonly" in head or f"{d.values} where=local" in head,
                                f"cmd/{cmd}'s {d.values} is neither readonly nor local, and `wk {cmd} -h` runs it")


class TestBenchListPlans(unittest.TestCase):
    def test_lists_plan_names_from_a_fake_mirror(self):
        with scratch_dir("wk-test-bench-src-") as src, temp_store() as store:
            plans = src / "Tools/Scripts/webkitpy/benchmark_runner/data/plans"
            plans.mkdir(parents=True)
            (plans / "speedometer3.1.plan").write_text("{}")
            (plans / "jetstream2.2.plan").write_text("{}")
            subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
            subprocess.run(["git", "-C", str(src), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(src), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "plans"], check=True)

            env = {"WK_STORE": store["WK_STORE"], "WK_IN_VM": "1"}
            mirror = Path(Store(env).mirror_dir())
            mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--bare", str(src), str(mirror)], check=True)

            cp = run_here("bench", "plans", env=env)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertEqual(sorted(cp.stdout.split()),
                              ["jetstream2.2", "speedometer3.1"])


if __name__ == "__main__":
    unittest.main()
