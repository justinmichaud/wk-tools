"""A container benchmark from nothing, the way a person takes one: a fresh `wk new` of the test's own, a
jsc-release build, two runs of the shortest plan a JavaScript shell drives (one JetStream3 subtest; a plan with no
cli.js, SunSpider's, is a browser's), the second pinned, then the two-run and the task report from this host, the
export, and `wk rm` -- refused while a task is unexported, allowed once both are.

Every step goes through ./wk, which forwards the build and the runs into the podman machine. Gated on
WK_TEST_SLOW=1 as well as the container target: the build is minutes with a warm ccache, tens of them cold. The
preflight's cpu governor is unknown in the podman machine, so the runs are --force'd, which they record.

Run: WK_TEST_SLOW=1 python3 tests/run.py --live -k TestBenchContainerRun
"""
import json
import os
import sys
import time
import unittest
import zipfile

from tests.support import (
    REPO,
    WkTest,
    bench_ls_runs,
    container_side,
    rand_suffix,
    requires_container_target,
    run,
    scratch_dir,
)

sys.path.insert(0, str(REPO / "lib"))
from wk import screen  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.machine import Fake, Local, lib_argv  # noqa: E402

PLAN, SUBTEST, CONFIG = "jetstream3", "richards", "jsc-release"


def _read(path):
    """A file in the container target's store."""
    cp = container_side("cat %s" % path)
    assert cp.returncode == 0, "could not read %s: %s" % (path, cp.stderr)
    return cp.stdout


@requires_container_target()
@unittest.skipUnless(os.environ.get("WK_TEST_SLOW") == "1", "a jsc-release build; set WK_TEST_SLOW=1 to run it")
class TestBenchContainerRun(WkTest):
    def bench(self, ws, *extra):
        cp = run("bench", "run", ws, PLAN, "--config", CONFIG, "--count", "2", "--subtests", SUBTEST, "--force", *extra,
                 timeout=900)
        self.assertEqual(cp.returncode, 0, "wk bench run failed:\n%s" % cp.stdout)
        return cp

    def test_new_build_bench_report_export_rm(self):
        ws = "wk-test-bench-%s" % rand_suffix()
        try:
            self.assertEqual(0, run("new", ws, timeout=900).returncode)
            run("status", ws, "--wait", "--timeout", "600", timeout=660)
            self.assertEqual(0, run("build", ws, CONFIG, "--detach", timeout=120).returncode)
            cp = run("status", ws, "--wait", "--timeout", "1500", timeout=1560)
            self.assertEqual(0, cp.returncode, "the %s build did not succeed:\n%s\n%s" % (CONFIG, cp.stdout, run("logs", ws).stdout))

            self.bench(ws)
            self.bench(ws, "--cores", "0")
            a, b = bench_ls_runs(run("bench", "ls", timeout=120).stdout)[-2:]
            self.assertIn("/ws/%s/bench/" % ws, a, "a container's task lives under its workspace's own directory")
            env = json.loads(_read(b + "/env.json"))
            self.assertEqual(env["cores"], {"set": "0", "pinned": True})
            self.assertIn(SUBTEST, _read(a + "/result.json"))
            tasks = [p.split("/bench/", 1)[1].split("/runs/")[0] for p in (a, b)]

            with scratch_dir() as tmp:
                cp = run("bench", "report", a, b, "--html", str(tmp / "r.html"), timeout=120)
                self.assertEqual(0, cp.returncode, "the two-run report from this host:\n%s" % cp.stdout)
                self.assertIn("<svg", (tmp / "r.html").read_text())
                cp = run("bench", "report", tasks[0], timeout=120)
                self.assertEqual(0, cp.returncode, "the task report from this host:\n%s" % cp.stdout)
                self.assertIn("complete", cp.stdout)

                cp = run("rm", ws, env={"WK_YES": "1"}, timeout=300)
                self.assertNotEqual(0, cp.returncode, "an unexported task was destroyed:\n%s" % cp.stdout)
                self.assertIn("wk bench export %s" % tasks[0], cp.stdout)
                for t in tasks:
                    cp = run("bench", "export", t, "--to", str(tmp), "--yes", timeout=300)
                    self.assertEqual(0, cp.returncode, cp.stdout)
                    names = zipfile.ZipFile(str(tmp / (t + ".zip"))).namelist()
                    self.assertIn(t + "/report.txt", names)
                    self.assertIn(t + "/task.json", names)
                    self.assertTrue([n for n in names if n.endswith("/result.json")], names)
                cp = run("rm", ws, env={"WK_YES": "1"}, timeout=300)
                self.assertEqual(0, cp.returncode, "both tasks are exported, and the removal was refused:\n%s" % cp.stdout)
        finally:
            if ws in run("ls", timeout=120).stdout:
                run("rm", ws, "--force", env={"WK_YES": "1"}, timeout=300)


class TestTheWatchIsInertInAContainer(unittest.TestCase):
    """The pipeline brackets every browser run with lib/wk/screen.py's Watch
    (tests/test_bench_pipeline.py); where there is no window server it must record nothing."""

    def watch(self, system):
        """Run against a real ps and window probe this would fail on the
        maintainer's own desktop -- open windows and daemons this host never
        paused are exactly what a container has none of, so both are answered
        the way one does; the must-not-run table is bench/mac-quiet-desktop.sh's own."""
        m = Fake()
        m.answer(["uname", "-s"], out=system + "\n")
        m.answer(lib_argv(REPO, screen.WINDOWS, "wk_window_probe"), out="windows=?\n")
        m.react(lib_argv(REPO, screen.DESKTOP, "wk_quiet_desktop_stopped"), lambda argv, f: Local().run(argv))
        m.react(lib_argv(REPO, screen.DESKTOP, "wk_quiet_desktop_unstoppable"), lambda argv, f: Local().run(argv))
        m.answer(["ps"], out="")
        w = screen.Watch(m, REPO, Clock(), {"WK_SCREEN_WATCH_SECONDS": "0.05"})
        w.start()
        time.sleep(0.3)
        return w.stop(), [e[1][0] for e in m.effects]

    def test_a_container_is_never_asked(self):
        """Every container run goes through the same line, and the watcher records nothing rather than refuse."""
        seen, ran = self.watch("Linux")
        self.assertEqual([], seen)
        self.assertEqual(["uname"], ran)

    def test_a_mac_whose_window_server_cannot_be_asked_says_so(self):
        """The probe answers `?` there: not "nothing is there", so it is a finding of its own."""
        seen, ran = self.watch("Darwin")
        self.assertEqual([screen.UNASKED], [l.split("\t", 1)[1] for l in seen])
        self.assertIn("ps", ran, "the watch never looked")


if __name__ == "__main__":
    unittest.main()
