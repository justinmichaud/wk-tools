"""`wk rm --all` and a named `wk rm` over one fake local place WK_PLACE pins, so no real workspace is reached."""
import unittest

from tests.support import WkTest, fake_workspace, run

_LOCAL_CONF = (
    "kind=build\ndriver=remote\n"
    "local=1\n"
    "root={root}\n"
    "store={store}\n"
)


class RmAllFixture(WkTest):
    def setUp(self):
        super().setUp()
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        self.root = self.tmp / "root"
        self.store = self.tmp / "store"
        (self.root / "ws").mkdir(parents=True)
        (self.store / "ws").mkdir(parents=True)
        (self.registry / "fakebox.conf").write_text(
            _LOCAL_CONF.format(root=self.root, store=self.store))
        self.env = {
            "WK_MACHINES_DIR": str(self.registry),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_PLACE": "fakebox",
        }

    def make(self, name):
        (self.root / "ws" / name).mkdir()
        (self.root / "ws" / name / ".wk-ready").write_text("")
        (self.store / "ws" / name).mkdir()

    def rm(self, *args, yes=False):
        env = dict(self.env)
        if yes:
            env["WK_YES"] = "1"
        return run("rm", *args, env=env, input="", timeout=120)

    def remaining(self):
        return sorted(p.name for p in (self.root / "ws").iterdir())


class TestTheQuestionNamesEveryWorkspaceAndItsMachine(RmAllFixture):
    def test_a_named_removal_names_the_target_too(self):
        self.make("alpha")
        self.make("beta")
        cp = self.rm("alpha", "beta")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("alpha@fakebox", cp.stdout, cp.stdout)
        self.assertIn("beta@fakebox", cp.stdout, cp.stdout)
        self.assertEqual(self.remaining(), ["alpha", "beta"], cp.stdout)


class TestItDestroysEveryWorkspaceItNamed(RmAllFixture):
    def test_every_workspace_and_its_record_go(self):
        for name in ("alpha", "beta", "gamma"):
            self.make(name)
        cp = self.rm("--all", yes=True)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(self.remaining(), [], cp.stdout)
        self.assertEqual(sorted(p.name for p in (self.store / "ws").iterdir()), [])


class TestWhatItRefuses(RmAllFixture):
    def test_a_name_and_all_together_are_refused(self):
        self.make("alpha")
        cp = self.rm("alpha", "--all")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(self.remaining(), ["alpha"], cp.stdout)

    def test_inside_a_workspace_it_is_refused_with_the_host_command(self):
        with fake_workspace() as ws:
            cp = ws.run("rm", "--all", input="")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk rm --all", cp.stdout, cp.stdout)

    def test_a_named_dry_run_prints_the_removal_and_destroys_nothing(self):
        self.make("alpha")
        cp = self.rm("alpha", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("alpha@fakebox", cp.stdout, cp.stdout)
        self.assertEqual(self.remaining(), ["alpha"], cp.stdout)
        self.assertTrue((self.store / "ws" / "alpha").is_dir(), cp.stdout)


if __name__ == "__main__":
    unittest.main()
