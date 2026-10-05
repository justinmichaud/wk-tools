"""The README's getting-started sequence, walked as a newcomer on a bare"""
import re
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, _clean_env, run, run_here

sys.path.insert(0, str(REPO / "lib"))
from wk import status  # noqa: E402

REMEDY = re.compile(r"(?:^\s*|-> |run |['`])(wk [a-z][\w-]*|\./setup)", re.M)
USAGE = re.compile(r"^\s*(usage: .*|wk \S+ -h for the rest)$", re.M)
WS = "bug-238"
WORKSPACE_STEPS = (
    ("new", WS),
    ("build", WS, "jsc-release", "--detach"),
    ("status", WS),
    ("logs", WS),
    ("enter", WS, "--", "ls"),
    ("stop", WS),
    ("start", WS),
    ("rm", WS),
)


class NewcomerTest(WkTest):
    def assert_names_a_remedy(self, step, cp):
        if cp.returncode == 0:
            return
        own = " ".join(step.split()[:2])
        offered = [m for m in REMEDY.findall(USAGE.sub("", cp.stdout)) if not m.startswith(own)]
        self.assertTrue(offered, "'%s' exited %d naming no other command to run:\n%s" % (step, cp.returncode, cp.stdout))

    def setup_script(self, *args):
        cp = subprocess.run([str(REPO / "setup"), *args], env=_clean_env({"WK_STORE": str(self.tmp)}),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        return cp


class TestTheRemedyCheckIsStrict(NewcomerTest):
    def failed(self, step, out):
        return subprocess.CompletedProcess([], 1, stdout=out)

    def test_a_command_echoing_its_own_name_names_no_remedy(self):
        for out in ("error: wk new failed", "==> wk new bug-1\nerror: nope", "error: 'wk new bug-1' failed"):
            with self.assertRaises(AssertionError):
                self.assert_names_a_remedy("wk new bug-1", self.failed("new", out))

    def test_another_command_on_its_own_line_after_an_arrow_or_quoted_is_one(self):
        for out in ("error: x\n    wk sync", "error: x -> wk sync", "error: x; 'wk sync' first", "error: run ./setup first"):
            self.assert_names_a_remedy("wk new bug-1", self.failed("new", out))

    def test_a_command_named_mid_sentence_is_not_one(self):
        with self.assertRaises(AssertionError):
            self.assert_names_a_remedy("wk new bug-1", self.failed("new", "error: the wk sync mirror is old"))


class TestTheFirstCommands(NewcomerTest):
    def test_bare_wk_names_wk_help(self):
        cp = run()
        self.assertIn("wk help", cp.stdout)


    def test_doctor_names_the_fix_for_what_it_finds_missing(self):
        self.assert_names_a_remedy("wk doctor", run("doctor", env={"WK_STORE": str(self.tmp)}))


class TestSetupRefusals(NewcomerTest):

    def test_an_unknown_stage_is_refused_not_reported_as_no_changes(self):
        cp = self.setup_script("--stage", "no-such-stage")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no stage 'no-such-stage'", cp.stdout)
        self.assertIn("./setup -h", cp.stdout)
        self.assertNotIn("no changes", cp.stdout)
        self.assertIn("./setup -h", self.setup_script("--no-such-option").stdout)


class TestAWorkspaceFromNothing(NewcomerTest):

    def walk(self, runner):
        for step in WORKSPACE_STEPS:
            with self.subTest(step=" ".join(step)):
                self.assert_names_a_remedy("wk " + " ".join(step), runner(*step, env={"WK_STORE": str(self.tmp)}))

    def test_from_the_workstation(self):
        self.walk(run)

    def test_from_the_machine_holding_the_store(self):
        self.walk(run_here)

    def test_status_answers_on_a_bare_machine(self):
        self.assertEqual(run("status", "--no-fleet", env={"WK_STORE": str(self.tmp)}).returncode, 0)


class TestAnAbsentPodmanMachineNamesSetup(unittest.TestCase):
    def test_status_says_to_make_it_rather_than_start_it(self):
        class Absent:
            def machine_state(self):
                return "absent"

            def podman_machine(self):
                return "wk"
        self.assertIn("./setup", status.far_side_reason(Absent(), "stopped", ""))


if __name__ == "__main__":
    unittest.main()
