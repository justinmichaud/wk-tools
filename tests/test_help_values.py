"""Every closed-set value a command's table names (`values`) appears in its `-h`."""
import subprocess
import sys
from pathlib import Path
import unittest

from tests.support import REPO, run_here, scratch_dir, temp_store

sys.path.insert(0, str(REPO / "lib"))
from wk import decl  # noqa: E402
from wk.completion import values_cmd  # noqa: E402
from wk.store import Store  # noqa: E402

TIER = "lint"

DECLARED_VALUES = {name: m.COMMAND for name, m in decl.commands(REPO) if values_cmd(m.COMMAND)}


class TestDeclaredValuesReachTheHelp(unittest.TestCase):
    def test_the_help_prints_what_the_verb_or_flag_prints_and_it_changes_nothing(self):
        self.assertTrue(DECLARED_VALUES)
        for cmd, t in DECLARED_VALUES.items():
            listed = subprocess.run([str(REPO / "wk"), cmd, t.values], cwd=str(REPO), capture_output=True, text=True, timeout=60)
            self.assertEqual(listed.returncode, 0, f"wk {cmd} {t.values} failed: {listed.stdout}{listed.stderr}")
            values = [l.split()[0] for l in (listed.stdout + listed.stderr).splitlines() if l[:1].isalnum()]
            self.assertTrue(values, f"wk {cmd} {t.values} listed nothing")
            cp = subprocess.run([str(REPO / "wk"), cmd, "-h"], cwd=str(REPO), capture_output=True, text=True, timeout=30)
            with self.subTest(cmd=cmd):
                self.assertIn("valid values", cp.stdout + cp.stderr)
                self.assertEqual([v for v in values if v not in cp.stdout + cp.stderr], [])
                self.assertTrue(decl.parse(cmd, t, [t.values])[0].readonly)


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
