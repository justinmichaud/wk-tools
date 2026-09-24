"""`wk rm --all`: every workspace there is, named with the machine it is on
before anything is destroyed.

The properties:

  * the list `--all` acts on is `wk ls`'s, so nothing is a second answer to
    "what workspaces exist where";
  * the question states every workspace as `<name>@<target>`, so which machine
    each one is on is part of what is answered -- and it is asked once, here,
    for the whole fleet;
  * each removal then goes through the one path a named `wk rm` takes, with the
    target it was found on, so the dispatcher routes it (into the podman VM,
    over ssh, or to the machine that keeps its record) rather than `--all`
    deciding that a second time;
  * `--all` and a name together are refused, and `--all` inside a workspace is
    refused with the command to run on the host.

Every test here pins WK_TARGET at one fake machine of its own (a `remote`
target with WK_REMOTE_LOCAL=1, so the driver's real code runs against
directories in a scratch tree and no ssh is made). That is not decoration:
`walk_targets` honours WK_TARGET, so the listing `--all` asks for cannot
reach this machine's own containers, guests or fleet -- a suite that let it
would destroy the maintainer's workspaces on the first run.

Run: python3 -m unittest tests.test_rm_all -v
"""
import unittest

from tests.support import WkTest, fake_workspace, run

_LOCAL_CONF = (
    "KIND=build\nWK_TARGET_KIND=remote\n"
    "WK_REMOTE_LOCAL=1\n"
    "WK_REMOTE_ROOT={root}\n"
    "WK_REMOTE_STORE={store}\n"
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
            "WK_TARGET": "fakebox",
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
    def test_the_dry_run_names_each_one_and_the_removal_it_would_run(self):
        self.make("alpha")
        self.make("beta")
        cp = self.rm("--all", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("alpha@fakebox", cp.stdout, cp.stdout)
        self.assertIn("beta@fakebox", cp.stdout, cp.stdout)
        self.assertIn("would ask:", cp.stdout, cp.stdout)
        self.assertIn("would run: env WK_TARGET=fakebox", cp.stdout, cp.stdout)
        self.assertIn("rm alpha --yes", cp.stdout, cp.stdout)
        self.assertEqual(self.remaining(), ["alpha", "beta"],
                         "a dry run destroyed something")

    def test_the_question_is_asked_once_and_declining_destroys_nothing(self):
        """no terminal is a No, and the whole list is in what it declined"""
        self.make("alpha")
        self.make("beta")
        cp = self.rm("--all")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(cp.stdout.count("destroy them?"), 1, cp.stdout)
        self.assertIn("alpha@fakebox", cp.stdout, cp.stdout)
        self.assertIn("beta@fakebox", cp.stdout, cp.stdout)
        self.assertEqual(self.remaining(), ["alpha", "beta"], cp.stdout)

    def test_a_named_removal_names_the_target_too(self):
        """the same question, for the names a person typed"""
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
        for name in ("alpha", "beta", "gamma"):
            self.assertIn(f"workspace '{name}' destroyed", cp.stdout, cp.stdout)
        self.assertEqual(self.remaining(), [], cp.stdout)
        self.assertEqual(sorted(p.name for p in (self.store / "ws").iterdir()), [],
                         "a record outlived the workspace it belonged to")

    def test_with_nothing_to_destroy_it_says_so_and_asks_nobody(self):
        cp = self.rm("--all")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("nothing to destroy", cp.stdout, cp.stdout)
        self.assertNotIn("destroy them?", cp.stdout, cp.stdout)


class TestWhatItRefuses(RmAllFixture):
    def test_a_name_and_all_together_are_refused(self):
        self.make("alpha")
        cp = self.rm("alpha", "--all")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("one or the other", cp.stdout, cp.stdout)
        self.assertEqual(self.remaining(), ["alpha"], cp.stdout)

    def test_inside_a_workspace_it_is_refused_with_the_host_command(self):
        """`--all` is the host's: in here there is one workspace, this one"""
        with fake_workspace() as ws:
            cp = ws.run("rm", "--all", input="")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("acts on a host", cp.stdout, cp.stdout)
        self.assertIn("wk rm --all", cp.stdout, cp.stdout)

    def test_a_named_dry_run_prints_the_removal_and_destroys_nothing(self):
        self.make("alpha")
        cp = self.rm("alpha", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("alpha@fakebox", cp.stdout, cp.stdout)
        self.assertIn("would ", cp.stdout, cp.stdout)
        self.assertNotIn("workspace 'alpha' destroyed", cp.stdout, cp.stdout)
        self.assertEqual(self.remaining(), ["alpha"], cp.stdout)
        self.assertTrue((self.store / "ws" / "alpha").is_dir(), cp.stdout)


if __name__ == "__main__":
    unittest.main()
