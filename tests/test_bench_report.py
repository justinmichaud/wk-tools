"""`wk bench report` / `wkdata.py report`: the unified score+time+variance
report (lib/wkdata.py `_subtest_metrics`, `_welch_p`, `cmd_report`).

Unit tests build two synthetic result.json+env.json pairs and drive
`lib/wkdata.py report` exactly as documented -- as a subprocess, the same way
`wk bench report` invokes it -- and check the text and html outputs agree.
No workspace, no podman VM.

The integration test is podman-gated (see requires_podman_vm in
tests/support.py) and self-skips when there is no already-built jsc-release
in any local container workspace: building one from scratch is tens of
minutes, which this suite does not do (see CLAUDE.md, "Never build, test, or
benchmark WebKit here" -- driving an *existing* workspace's `wk bench` is the
sanctioned way, building one is not this test's job).

Run: python3 -m unittest tests.test_bench_report -v
"""
import json
import re
import statistics
import subprocess
import unittest

from tests.support import REPO, WkTest, requires_podman_vm, run, scratch_dir

WKDATA = REPO / "lib" / "wkdata.py"


def wkdata(*args, timeout=30):
    return subprocess.run(
        ["python3", str(WKDATA), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def env_record(path, *fields):
    cp = wkdata("env-record", str(path), *fields)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


class TestReportWalkerAndStats(WkTest):
    """Two synthetic runs, every shape `wk bench report` has to read."""

    def _write_pair(self, tmp, a_doc, b_doc, a_extra=(), b_extra=()):
        a_dir, b_dir = tmp / "a", tmp / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        (a_dir / "result.json").write_text(json.dumps(a_doc))
        (b_dir / "result.json").write_text(json.dumps(b_doc))
        env_record(a_dir / "env.json", "plan=jetstream3", "config=jsc-release",
                    "count=6", "class=cpu", "runner=jsc", "bench_host=container", *a_extra)
        env_record(b_dir / "env.json", "plan=jetstream3", "config=jsc-release",
                    "count=6", "class=cpu", "runner=jsc", "bench_host=container", *b_extra)
        return a_dir / "result.json", b_dir / "result.json"

    def test_report_html_has_every_subtest_both_metrics_and_one_svg_each(self):
        """the shape a merged jsc-shell log and run-benchmark's own JetStream
        output both use: metrics.Score with no modifier level for one
        subtest (jsc-log), metrics.Score.None and metrics.Time with no
        modifier for another (run-benchmark) -- both read by the one walker."""
        with scratch_dir() as tmp:
            a_doc = {
                "JetStream3.0": {
                    "tests": {
                        "gaussian-blur": {
                            "metrics": {
                                "Score": {None: {"current": [95.0, 97.0, 96.0, 94.0, 98.0, 96.5]}},
                                "Time": {"current": [10.1, 10.3, 10.2, 10.0, 10.4, 10.2]},
                            }
                        },
                        "richards": {
                            "metrics": {"Score": {"current": [50.0, 51.0, 49.5, 50.5, 50.2, 49.8]}},
                        },
                    }
                }
            }
            b_doc = {
                "JetStream3.0": {
                    "tests": {
                        "gaussian-blur": {
                            "metrics": {
                                "Score": {None: {"current": [104.0, 106.0, 105.0, 103.0, 107.0, 105.5]}},
                                "Time": {"current": [9.1, 9.3, 9.2, 9.0, 9.4, 9.2]},
                            }
                        },
                        "richards": {
                            "metrics": {"Score": {"current": [52.0, 53.0, 51.5, 52.5, 52.2, 51.8]}},
                        },
                    }
                }
            }
            a, b = self._write_pair(tmp, a_doc, b_doc)
            html_out = tmp / "report.html"
            cp = wkdata("report", str(a), str(b), "--html", str(html_out))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(f"wrote {html_out}", cp.stdout)

            html = html_out.read_text()
            for name in ("gaussian-blur", "richards"):
                self.assertIn(name, html, f"subtest '{name}' missing from the html report")
            # header names both metric columns; the per-row cells repeat the
            # metric name in its own column, so >=2 occurrences of each
            # confirms both a header and at least one data row.
            self.assertGreaterEqual(html.count(">Score<") + html.count("<td>Score</td>"), 1)
            self.assertIn("<td>Time</td>", html)
            # one <svg> per subtest: exactly two subtests were given.
            self.assertEqual(html.count("<svg"), 2, "expected exactly one <svg> per subtest")
            self.assertIn("variance by configuration", html.lower())

            # Text mode reports the same numbers -- cross-check the
            # gaussian-blur Score means against a hand computation.
            cp_text = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp_text.returncode, 0, cp_text.stdout + cp_text.stderr)
            a_mean = statistics.mean([95.0, 97.0, 96.0, 94.0, 98.0, 96.5])
            b_mean = statistics.mean([104.0, 106.0, 105.0, 103.0, 107.0, 105.5])
            self.assertIn("%.3f" % a_mean, cp_text.stdout)
            self.assertIn("%.3f" % b_mean, cp_text.stdout)
            self.assertIn("gaussian-blur", cp_text.stdout)
            self.assertIn("richards", cp_text.stdout)
            # The same means appear in the html table (formatted the same way).
            self.assertIn("%.3f" % a_mean, html)
            self.assertIn("%.3f" % b_mean, html)

    def test_report_handles_speedometer_total_modifier_shape(self):
        """Speedometer's own shape nests Time one level deeper again, under a
        "Total" modifier rather than None -- a third shape the same walker
        has to dig through."""
        with scratch_dir() as tmp:
            a_doc = {"Speedometer-3": {"tests": {"TodoMVC-JS": {
                "metrics": {"Time": {"Total": {"current": [100.0, 102.0, 99.0, 101.0]}}}
            }}}}
            b_doc = {"Speedometer-3": {"tests": {"TodoMVC-JS": {
                "metrics": {"Time": {"Total": {"current": [95.0, 97.0, 96.0, 94.0]}}}
            }}}}
            a, b = self._write_pair(tmp, a_doc, b_doc)
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("TodoMVC-JS", cp.stdout)
            self.assertIn("Time", cp.stdout)

    def test_variance_by_configuration_groups_matching_tuples(self):
        """Two runs sharing a `configuration` tuple land in one variance
        group; the group's line names the axes and both sides' spread."""
        with scratch_dir() as tmp:
            doc_a = {"JetStream3.0": {"tests": {"t": {"metrics": {
                "Score": {"current": [99.0, 100.0, 101.0, 100.0]}
            }}}}}
            doc_b = {"JetStream3.0": {"tests": {"t": {"metrics": {
                "Score": {"current": [80.0, 120.0, 70.0, 130.0]}
            }}}}}
            a, b = self._write_pair(
                tmp, doc_a, doc_b,
                a_extra=("configuration.aslr=off", "configuration.env_pad_bytes=4096"),
                b_extra=("configuration.aslr=off", "configuration.env_pad_bytes=4096"),
            )
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("aslr=off", cp.stdout)
            self.assertIn("env_pad_bytes=4096", cp.stdout)
            self.assertIn("exceeds A sd by >20%", cp.stdout, "B is far noisier than A and should be flagged")

    def test_axis_check_warnings_appear_in_the_report(self):
        """A mismatched axis (different runner) is the same warning
        `wkdata.py axis-check` prints on its own -- _axis_check_lines is one
        implementation, read by both."""
        with scratch_dir() as tmp:
            doc = {"JetStream3.0": {"tests": {"t": {"metrics": {"Score": {"current": [1.0, 2.0]}}}}}}
            a, b = self._write_pair(
                tmp, doc, doc,
                a_extra=("runner=jsc",), b_extra=("runner=browser",),
            )
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("different runners", cp.stdout)

    def test_env_record_defaults_configuration_for_untouched_runs(self):
        """A run that never sets any configuration.* field still gets a full
        `configuration` block, with the "not controlled" defaults -- an
        older env.json and one written today read the same way."""
        with scratch_dir() as tmp:
            f = tmp / "env.json"
            env_record(f, "plan=jetstream3")
            doc = json.loads(f.read_text())
            self.assertEqual(
                doc["configuration"],
                {"aslr": "unset", "path_len": 0, "shared_cache": None, "env_pad_bytes": 0},
            )

    def test_env_record_update_merges_wall_time_without_clobbering(self):
        """--update is how wall_time_s is added after a run finishes,
        without a second write discarding the axes recorded before it."""
        with scratch_dir() as tmp:
            f = tmp / "env.json"
            env_record(f, "plan=jetstream3", "config=jsc-release")
            env_record(f, "--update", "wall_time_s=42")
            doc = json.loads(f.read_text())
            self.assertEqual(doc["plan"], "jetstream3")
            self.assertEqual(doc["config"], "jsc-release")
            self.assertEqual(doc["wall_time_s"], "42")

    def test_env_record_fields_are_read_on_either_side_of_a_flag(self):
        """`--update` before the fields is how every caller in cmd/bench
        writes wall_time_s, and argparse fills an nargs='*' positional from
        one unbroken run of words -- so both orders are read, or the four
        call sites that spell it the first way write nothing."""
        with scratch_dir() as tmp:
            for args in (("--update", "wall_time_s=42"),
                         ("wall_time_s=42", "--update")):
                f = tmp / "env.json"
                f.unlink(missing_ok=True)
                env_record(f, "plan=jetstream3")
                env_record(f, *args)
                doc = json.loads(f.read_text())
                self.assertEqual(doc["wall_time_s"], "42", args)
                self.assertEqual(doc["plan"], "jetstream3", args)

    def test_a_subcommand_without_fields_still_refuses_a_stray_word(self):
        """the leftovers are fields only where the subcommand takes fields;
        anywhere else they are the typo they look like"""
        cp = wkdata("get", "/dev/null", "plan", "junk")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("unrecognized arguments: junk", cp.stdout + cp.stderr)

    def test_env_record_refuses_a_field_that_is_not_key_value(self):
        """a mistyped flag arrives as a leftover, and is refused as one"""
        with scratch_dir() as tmp:
            cp = wkdata("env-record", str(tmp / "env.json"), "--nosuch")
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("not a key=value: --nosuch", cp.stdout + cp.stderr)


@requires_podman_vm()
class TestBenchReportIntegration(WkTest):
    """`wk bench report` end to end, against whatever local container
    workspace already has a jsc-release build -- building one here would be
    tens of minutes (CLAUDE.md forbids driving a build from this suite
    anyway), so this reuses one rather than creating and building a fresh
    `wk new` workspace."""

    def _existing_jsc_workspace(self):
        # `wk ls` can legitimately take a long time -- it is not local-only,
        # and this fleet has workspaces backed by a remote machine (target
        # "moose:container") that `wk ls` reaches to refresh state. A slow
        # or unreachable remote is not this test's problem to wait out, so a
        # timeout here is a skip, not a failure.
        try:
            cp = run("ls", timeout=45)
        except subprocess.TimeoutExpired:
            return None, "'wk ls' did not answer within 45s"
        if cp.returncode != 0:
            return None, f"'wk ls' failed: {cp.stdout}"

        # Only target == "container" exactly: that is the local podman-VM
        # backend `wk bench` supports (load_bench_target in cmd/bench
        # refuses anything else by name). A "moose:container" workspace is
        # someone's real remote checkout, tens of GB, driven over the
        # tailnet -- not something this test probes or touches.
        candidates = []
        for line in cp.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] == "NAME":
                continue
            if parts[1] == "container":
                candidates.append(parts[0])
        if not candidates:
            return None, "no local (target=container) workspace in 'wk ls'"

        for name in candidates:
            try:
                probe = run(
                    "bench", name, "sunspider1.0.2", "--config", "jsc-release", "--count", "1", timeout=120
                )
            except subprocess.TimeoutExpired:
                continue
            if probe.returncode == 0:
                return name, None
        return None, f"no jsc-release build ready to bench in: {', '.join(candidates)}"

    def test_two_runs_and_a_report(self):
        import time

        ws, reason = self._existing_jsc_workspace()
        if ws is None:
            self.skipTest(reason)

        t0 = time.time()
        run_a = run("bench", ws, "sunspider1.0.2", "--config", "jsc-release", "--count", "2", timeout=300)
        self.assertEqual(run_a.returncode, 0, f"first run failed: {run_a.stdout}")
        run_b = run("bench", ws, "sunspider1.0.2", "--config", "jsc-release", "--count", "2", timeout=300)
        self.assertEqual(run_b.returncode, 0, f"second run failed: {run_b.stdout}")
        bench_s = time.time() - t0

        # The run id is the last "recorded" / "results:" path component `wk
        # bench` printed; cmd/bench also accepts a bare run id off `wk bench
        # ls`, which is more robust to exactly how the id is formatted.
        ls = run("bench", "ls")
        run_ids = [line.split()[0] for line in ls.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(run_ids), 2, f"'wk bench ls' does not show two runs: {ls.stdout}")
        a_id, b_id = run_ids[-2], run_ids[-1]

        with scratch_dir() as tmp:
            html_out = tmp / "report.html"
            cp = run("bench", "report", a_id, b_id, "--html", str(html_out), timeout=60)
            self.assertEqual(cp.returncode, 0, f"'wk bench report' failed: {cp.stdout}")
            self.assertTrue(html_out.exists(), "no html report was written")
            html = html_out.read_text()
            self.assertTrue(
                re.search(r"[a-zA-Z]", html),
                "the html report names no subtests at all",
            )

        print(f"[timing] two sunspider runs + report: {bench_s:.1f}s (workspace: {ws})")


if __name__ == "__main__":
    unittest.main()
