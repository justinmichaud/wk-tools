"""`wk bench run <ws> <plan> --a-args X --b-args Y`: one build measured under two argument sets (lib/wk/bench/board_ab.py's ArgsAB)."""
import io
import json
import os

from tests.test_bench_pipeline import BenchTest
from wk.bench import record as brecord, report


class TestOptionsAB(BenchTest):
    def jsc_scripts(self):
        return [a[-1] for a in self.w.watched if "cli.js" in a[-1]]

    def test_rounds_alternate_the_lead_and_each_arm_runs_its_jsc_options(self):
        start = self.w.start
        self.w.start = lambda *a, **kw: (self.w.clock.sleep(60), start(*a, **kw))[1]   # a leg takes a minute
        rc, err = self.run_(None, "run", "jetstream3", "--config", "jsc-release", "--a-args", "--useFoo=0",
                            "--b-args", "--useFoo=1 --useBar=2", "--rounds", "2")
        self.assertEqual(rc, 0, err)
        arms = ["A" if "--useFoo=0 cli.js" in s else "B" if "--useFoo=1 --useBar=2 cli.js" in s else "?" for s in self.jsc_scripts()]
        self.assertEqual(arms, ["A", "B", "B", "A"])
        task = str(self.w.bench_dir() / self.w.tasks()[-1])
        doc = brecord.task_doc(task)
        self.assertEqual(doc["subject"], {"kind": "options", "a": "--useFoo=0", "b": "--useFoo=1 --useBar=2"})
        self.assertEqual(brecord.task_arms(doc), (["--useFoo=0", "--useFoo=1 --useBar=2"], "options"))
        self.assertIn("--a-args --useFoo=0 --b-args '--useFoo=1 --useBar=2' --rounds 2", doc["restart"])
        st = brecord.task_state(task, False)
        self.assertEqual((st["state"], st["ok"]), ("complete", 4))
        a, b, dropped = brecord.paired(brecord.task_rounds(doc, st["runs"])[("ws", "jetstream3")], ["A", "B"])
        self.assertEqual((len(a), len(b), dropped), (2, 2, []))
        out = io.StringIO()
        report.task_report(task, False, text=True, out=out)
        self.assertIn("A = options --useFoo=0, B = options --useFoo=1 --useBar=2", out.getvalue())
        self.assertNotIn("not counterbalanced", out.getvalue())

    def test_a_restart_runs_only_the_rounds_the_task_lacks(self):
        self.run_(None, "run", "jetstream3", "--config", "jsc-release", "--a-args", "", "--b-args", "--useFoo=1", "--rounds", "1")
        task = self.w.tasks()[-1]
        self.w.watched.clear()
        rc, err = self.run_(None, "run", "jetstream3", "--config", "jsc-release", "--a-args", "", "--b-args", "--useFoo=1",
                            "--rounds", "2", "--task", task)
        self.assertEqual(rc, 0, err)
        self.assertIn("round 1/2 -- recorded already", err)
        self.assertEqual(len(self.jsc_scripts()), 2)

    def test_the_restart_measures_under_the_same_options(self):
        rc, err = self.run_(None, "run", "jetstream3", "--config", "jsc-release", "--a-args", "", "--b-args", "--useFoo=1",
                            "--rounds", "1", "--count", "3", "--timeout", "90", "--subtests", "a b", "--cores", "0-1")
        self.assertEqual(rc, 0, err)
        restart = brecord.task_doc(str(self.w.bench_dir() / self.w.tasks()[-1]))["restart"]
        for flag in ("--count 3", "--timeout 90", "--subtests 'a b'", "--cores 0-1"):
            self.assertIn(flag, restart)

    def test_a_browser_config_gives_each_arm_minibrowser_arguments(self):
        rc, err = self.run_(None, "run", "speedometer3", "--config", "wpe-release", "--a-args", "", "--b-args", "--bar",
                            "--rounds", "1")
        self.assertEqual(rc, 0, err)
        scripts = [a[-1] for a in self.w.watched]
        self.assertEqual(["--bar" in s for s in scripts], [False, True])

    def test_a_dry_run_shows_the_first_round_and_writes_no_task(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.run_(None, "run", "jetstream3", "--config", "jsc-release", "--a-args", "", "--b-args", "--useFoo=1")
        self.assertEqual(rc, 0, err)
        self.assertIn("round 1/3 -- options (none) (first)", err)
        self.assertIn("round 1/3 -- options --useFoo=1", err)
        self.assertEqual(self.w.tasks(), [])

    def test_refusals(self):
        for argv, said in (
                (("--a-args", "--x", "--b-args", "--x"), "--a-args and --b-args are the same"),
                (("--a-args", "", "--b-args", "--x", "--system", "rpi5"), "an A/B of one build in this workspace"),
                (("--a-args", "", "--b-args", "--x", "--exclude-subtests", "t"), "--exclude-subtests belongs to an A/B on a board"),
                (("--a-args", "", "--b-args", "--x", "--rounds", "0"), "--rounds takes a number of at least 1")):
            with self.subTest(argv=argv):
                self.assertIn(said, self.said("run", "jetstream3", "--config", "jsc-release", *argv))
        self.assertEqual(self.w.tasks(), [])
        self.assertEqual(json.dumps(self.w.watched), "[]")
