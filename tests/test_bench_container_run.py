"""`wk bench report`, exercised end to end against a real container
benchmark run -- the maintainer's explicit ask: "test the html report
generation in a container benchmark run." tests/test_bench_report.py
already covers the report machinery itself (synthetic result.json
fixtures, plus a lighter integration test that reuses whatever local
workspace already has a jsc-release build). This one does the whole
lane for real and keeps the record of having done so: a fresh
`wk new`, a `wk build ... jsc-release` from scratch, two real
`wk bench` runs, and a `wk bench report --html` on them.

This is an hour-class test -- a JSC-only release build takes tens of
minutes inside the podman VM -- so it is gated on two things: the podman
VM being up (requires_podman_vm, same as every other container-touching
test) and WK_TEST_SLOW=1, so the default suite does not eat an hour on
every run.

CLAUDE.md ("Never build, test, or benchmark WebKit here") is not being
worked around: every step below runs `./wk`, which is what forwards the
actual build and benchmark work into the podman VM. Nothing here builds
WebKit on the host.

Run: WK_TEST_SLOW=1 python3 -m unittest tests.test_bench_container_run -v
"""
import json
import os
import subprocess
import time
import unittest

from tests.support import (
    WkTest,
    bench_ls_runs,
    podman_vm_ssh,
    rand_suffix,
    requires_podman_vm,
    run,
    scratch_dir,
)

PLAN = "sunspider1.0.2"
CONFIG = "jsc-release"


def _read_store_json(run_id, name):
    """<name> (env.json or result.json) out of a saved run, read off the
    podman VM's own store -- not the host's, since $WK_STORE for a
    container workspace lives in the VM (lib/store.sh, store_is_local),
    and `wk bench` itself forwards there rather than writing anything
    the host can read directly."""
    cp = podman_vm_ssh(f"cat /var/lib/wk/bench/{run_id}/{name}")
    assert cp.returncode == 0, f"could not read {name} for run {run_id}: {cp.stderr}"
    return json.loads(cp.stdout)


def _subtest_names(result_doc):
    """Every subtest name in a saved result.json, dug out the same way
    `wk bench report`'s own walker does: one top-level suite key (e.g.
    "SunSpider-1.0.2"), a "tests" dict keyed by subtest name."""
    names = set()
    for suite in result_doc.values():
        tests = suite.get("tests", {}) if isinstance(suite, dict) else {}
        names.update(tests.keys())
    return names


@requires_podman_vm()
@unittest.skipUnless(
    os.environ.get("WK_TEST_SLOW") == "1",
    "hour-class test (a from-scratch jsc-release build); set WK_TEST_SLOW=1 to run it",
)
class TestBenchContainerRun(WkTest):
    """New workspace -> jsc-release build -> two sunspider runs -> one
    html+text report, all driven through ./wk against the real podman VM.
    `wk rm` always runs, even on failure, so a broken run does not leave a
    workspace (and its build tree) behind."""

    def test_new_build_bench_report(self):
        ws = f"wk-test-bench-{rand_suffix()}"
        timings = {}
        try:
            t0 = time.time()
            cp = run("new", ws, timeout=300)
            self.assertEqual(cp.returncode, 0, f"'wk new {ws}' failed:\n{cp.stdout}")
            cp = run("status", ws, "--wait", "--timeout", "600", timeout=660)
            self.assertEqual(
                cp.returncode, 0,
                f"workspace '{ws}' was not ready (status={cp.returncode}):\n{cp.stdout}",
            )
            timings["new"] = time.time() - t0

            t0 = time.time()
            cp = run("build", ws, CONFIG, "--detach", timeout=120)
            self.assertEqual(cp.returncode, 0, f"'wk build {ws} {CONFIG} --detach' failed:\n{cp.stdout}")
            # A cold jsc-release build in the VM is tens of minutes; poll
            # with `wk status --wait` (it blocks while busy, one report at
            # the end) rather than a hand-rolled sleep loop.
            cp = run("status", ws, "--wait", "--timeout", "3600", timeout=3660)
            if cp.returncode != 0:
                logs = run("logs", ws, timeout=60)
                self.fail(
                    f"'{CONFIG}' build did not succeed (status={cp.returncode}):\n"
                    f"--- wk status {ws} --wait ---\n{cp.stdout}\n"
                    f"--- wk logs {ws} ---\n{logs.stdout}"
                )
            timings["build"] = time.time() - t0

            run_ids = []
            for i in (1, 2):
                t0 = time.time()
                cp = run("bench", ws, PLAN, "--config", CONFIG, "--count", "2", timeout=300)
                self.assertEqual(cp.returncode, 0, f"bench run {i} failed:\n{cp.stdout}")
                timings[f"run{i}"] = time.time() - t0

                ls = run("bench", "ls", timeout=30)
                ids = bench_ls_runs(ls.stdout)
                self.assertTrue(ids, f"'wk bench ls' is empty after run {i}:\n{ls.stdout}")
                # Store-relative (<task>/runs/<run>): the same id whether the
                # store is the host's or the podman VM's.
                run_ids.append(ids[-1].split("/bench/", 1)[1])
            a_id, b_id = run_ids
            self.assertNotEqual(a_id, b_id, f"the two bench runs recorded the same id: {ls.stdout}")

            # The subtests the report is supposed to name, read straight off
            # the saved runs rather than hard-coded -- so this checks the
            # report against its own input, not a guess at what sunspider's
            # subtest list happens to be today.
            a_doc = _read_store_json(a_id, "result.json")
            b_doc = _read_store_json(b_id, "result.json")
            subtests = _subtest_names(a_doc) | _subtest_names(b_doc)
            self.assertTrue(subtests, f"no subtests found in either run's result.json ({a_id}, {b_id})")

            with scratch_dir() as tmp:
                html_out = tmp / "report.html"
                t0 = time.time()
                cp = run("bench", "report", a_id, b_id, "--html", str(html_out), "--text", timeout=60)
                self.assertEqual(cp.returncode, 0, f"'wk bench report' failed:\n{cp.stdout}")
                timings["report"] = time.time() - t0

                self.assertTrue(html_out.exists(), "no html report was written")
                html = html_out.read_text()

                for name in sorted(subtests):
                    self.assertIn(name, html, f"subtest '{name}' missing from the html report")
                self.assertIn(">Score<", html + "".join(f"<td>{n}</td>" for n in [])
                              or html, "Score column missing from the html report")
                self.assertTrue(
                    "Score" in html, "no 'Score' column found in the html report"
                )
                self.assertTrue(
                    "Time" in html, "no 'Time' column found in the html report"
                )
                self.assertEqual(
                    html.count("<svg"), len(subtests),
                    f"expected one <svg> per subtest ({len(subtests)}), found {html.count('<svg')}",
                )

                # Copy the report out to a stable path so it survives the
                # scratch dir's cleanup, for a human to open afterwards.
                kept = "/tmp/wk-test-bench-container-run-report.html"
                kept_path_written = _copy_report(html_out, kept)

            print(f"[timing] {ws}: " + ", ".join(f"{k}={v:.1f}s" for k, v in timings.items()))
            print(f"[subtests] {sorted(subtests)}")
            print(f"[report] copied to {kept_path_written}")
        finally:
            cp = run("rm", ws, env={"WK_YES": "1"}, timeout=180)
            if cp.returncode != 0:
                print(f"[cleanup] 'wk rm {ws}' failed:\n{cp.stdout}")


def _copy_report(src_path, dest):
    import shutil
    shutil.copyfile(str(src_path), dest)
    return dest


if __name__ == "__main__":
    unittest.main()
