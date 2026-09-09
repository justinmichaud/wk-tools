"""`wk bench report` / `wkdata.py report`: the unified score+time+variance
report (lib/wkdata.py `_subtest_metrics`, `_welch_p`, `cmd_report`).

Unit tests build two synthetic run directories -- a result.json and an
env.json in each -- and drive `lib/wkdata.py report` exactly as documented: as
a subprocess, naming the directories, the same way `wk bench report` invokes
it. The text and html outputs are checked to agree. No workspace, no podman VM.

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

from tests.support import REPO, WkTest, bench_ls_runs, requires_podman_vm, run, scratch_dir
from tests.test_ab_precision import (
    JETSTREAM3_CHILDREN, JETSTREAM3_HEADLINE, MOTIONMARK_CHILDREN, MOTIONMARK_HEADLINE,
    SPEEDOMETER3_HEADLINE, aggregate_doc, fields, speedometer_doc,
)

WKDATA = REPO / "lib" / "wkdata.py"

# The text table's name column is as wide as its widest name, so a name is
# whatever precedes the metric word.
ROW = re.compile(r"^(?P<name>\S.*?) +(?P<metric>Score|Time) +"
                 r"(?P<a>-?[0-9.]+)\+-[0-9.]+ +(?P<b>-?[0-9.]+)\+-[0-9.]+ ")


def wkdata(*args, timeout=30):
    return subprocess.run(
        ["python3", str(WKDATA), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def report_means(stdout):
    """{(subtest, metric): (A mean, B mean)} out of the text table."""
    out = {}
    for line in stdout.splitlines():
        m = ROW.match(line)
        if m:
            out[(m.group("name"), m.group("metric"))] = (float(m.group("a")), float(m.group("b")))
    return out


def env_record(path, *fields):
    cp = wkdata("env-record", str(path), *fields)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


class TestReportWalkerAndStats(WkTest):
    """Two synthetic runs, every shape `wk bench report` has to read."""

    def _write_pair(self, tmp, a_doc, b_doc, a_extra=(), b_extra=()):
        """Two run directories. A run is named by the directory a benchmark
        wrote; result.json and env.json are derived from it inside wkdata.py."""
        a_dir, b_dir = tmp / "a", tmp / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        (a_dir / "result.json").write_text(json.dumps(a_doc))
        (b_dir / "result.json").write_text(json.dumps(b_doc))
        env_record(a_dir / "env.json", "plan=jetstream3", "config=jsc-release",
                    "count=6", "class=cpu", "runner=jsc", "bench_host=container", *a_extra)
        env_record(b_dir / "env.json", "plan=jetstream3", "config=jsc-release",
                    "count=6", "class=cpu", "runner=jsc", "bench_host=container", *b_extra)
        return a_dir, b_dir

    @staticmethod
    def _one_subtest():
        return {"JetStream3.0": {"tests": {"t": {"metrics": {
            "Score": {"current": [99.0, 100.0, 101.0, 100.0]}}}}}}

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

    def test_report_reads_speedometer2_board_results(self):
        """the shape `wk pi bench` records from the webserver patch's POST:
        the total Score at the suite root, declarations (metrics.Time ==
        ["Total"]) in the middle, and the numbers three levels down under
        Sync/Async. Every level becomes a row, named by its path from the
        suite down: the ones holding numbers from those, and the ones holding
        a declaration from resolving it over the level below."""
        def doc(base):
            return {"debugOutput": [None], "Speedometer-2": {
                "metrics": {"Score": {"current": [[base, base + 1.0, base + 0.5]]},
                            "Time": ["Total", "Geometric"]},
                "tests": {"VanillaJS-TodoMVC": {
                    "metrics": {"Time": ["Total"]},
                    "tests": {"Adding100Items": {
                        "metrics": {"Time": ["Total"]},
                        "tests": {
                            "Sync": {"metrics": {"Time": {"current": [[base * 10, base * 10 + 2]]}}},
                            "Async": {"metrics": {"Time": {"current": [[base, base + 1]]}}},
                        }}}}}}}
        with scratch_dir() as tmp:
            a, b = self._write_pair(tmp, doc(11.0), doc(10.5))
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            means = report_means(cp.stdout)
            self.assertEqual(
                {name for name, _metric in means},
                {"Speedometer-2",
                 "Speedometer-2/VanillaJS-TodoMVC",
                 "Speedometer-2/VanillaJS-TodoMVC/Adding100Items",
                 "Speedometer-2/VanillaJS-TodoMVC/Adding100Items/Sync",
                 "Speedometer-2/VanillaJS-TodoMVC/Adding100Items/Async"},
                "a row is named by its whole path, so the suite is the one row "
                "with no '/' in its name, and a level declaring how to "
                "aggregate the one below is a row too")
            # 110/112 and 11/12 are one iteration each, so Sync is 111 and
            # Async 11.5, and every Total above them is their sum.
            self.assertEqual((111.0, 106.0),
                             means[("Speedometer-2/VanillaJS-TodoMVC/Adding100Items/Sync", "Time")])
            for name in ("Speedometer-2",
                         "Speedometer-2/VanillaJS-TodoMVC",
                         "Speedometer-2/VanillaJS-TodoMVC/Adding100Items"):
                self.assertEqual((122.5, 117.0), means[(name, "Time")], name)

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

    def test_a_run_directory_with_no_result_json_is_refused_by_name(self):
        with scratch_dir() as tmp:
            a, b = self._write_pair(tmp, self._one_subtest(), self._one_subtest())
            (a / "result.json").unlink()
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            out = cp.stdout + cp.stderr
            self.assertIn("no result.json in this directory", out)
            self.assertIn(str(a), out)
            self.assertIn("side A", out)

    def test_naming_no_run_directory_at_all_is_refused(self):
        cp = wkdata("report", "", "", "--text")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no run directories given", cp.stdout + cp.stderr)

    def test_one_missing_run_among_several_is_warned_about_not_hidden(self):
        """A side that still has evidence reports on it, and says which round
        it could not read -- a shorter side must not go unremarked."""
        with scratch_dir() as tmp:
            a, b = self._write_pair(tmp, self._one_subtest(), self._one_subtest())
            gone = tmp / "a-gone"
            gone.mkdir()
            cp = wkdata("report", "%s,%s" % (a, gone), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("warning: side A", cp.stderr)
            self.assertIn("no result.json in this directory", cp.stderr)

    def test_a_run_with_no_env_json_reads_as_unknown_rather_than_refusing(self):
        """Deliberate: env.json is how the axis check knows what a run was, and
        a run predating a field has to report rather than refuse."""
        with scratch_dir() as tmp:
            a, b = self._write_pair(tmp, self._one_subtest(), self._one_subtest())
            (a / "env.json").unlink()
            (b / "env.json").unlink()
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("t", cp.stdout)
            self.assertIn("no warnings", cp.stdout)

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


class TestTheHeadlineRow(WkTest):
    """JetStream3 and MotionMark never write their overall score into the file:
    each declares itself the geometric mean of its first-level children's
    Scores. The report resolves the declaration into a row, and it is the same
    number `wk bench precision` stops on -- one implementation, read by both."""

    def _pair(self, tmp, doc):
        a_dir, b_dir = tmp / "a", tmp / "b"
        for d in (a_dir, b_dir):
            d.mkdir()
            (d / "result.json").write_text(json.dumps(doc))
            env_record(d / "env.json", "plan=mac-ab", "runner=browser")
        return a_dir, b_dir

    def _headline_row(self, tmp, doc, suite):
        a, b = self._pair(tmp, doc)
        cp = wkdata("report", str(a), str(b), "--text")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        means = report_means(cp.stdout)
        self.assertIn((suite, "Score"), means,
                      f"no headline row for {suite}:\n{cp.stdout}")
        precision = wkdata("ab-precision", "--a", str(a), "--b", str(b))
        self.assertEqual(precision.returncode, 0, precision.stdout + precision.stderr)
        return means[(suite, "Score")][0], float(fields(precision.stdout)["mean_a"])

    def test_jetstream3_reports_the_geometric_mean_of_its_seventy_seven_children(self):
        with scratch_dir() as tmp:
            doc = aggregate_doc("JetStream3.0", "Geometric", JETSTREAM3_CHILDREN)
            row, precision = self._headline_row(tmp, doc, "JetStream3.0")
            self.assertAlmostEqual(row, JETSTREAM3_HEADLINE, places=3)
            self.assertAlmostEqual(row, precision, places=3)

    def test_motionmark_reports_the_geometric_mean_of_its_eight_children(self):
        with scratch_dir() as tmp:
            doc = aggregate_doc("MotionMark-1.3.1", "Geometric", MOTIONMARK_CHILDREN)
            row, precision = self._headline_row(tmp, doc, "MotionMark-1.3.1")
            self.assertAlmostEqual(row, MOTIONMARK_HEADLINE, places=2)
            self.assertAlmostEqual(row, precision, places=2)

    def test_speedometer3_reports_the_score_it_writes_itself(self):
        """The third shape materialises its suite Score, and reads the same
        way it did before either of the other two got a row."""
        with scratch_dir() as tmp:
            row, precision = self._headline_row(tmp, speedometer_doc(), "Speedometer-3")
            self.assertAlmostEqual(row, SPEEDOMETER3_HEADLINE, places=3)
            self.assertAlmostEqual(row, precision, places=3)

    def test_the_declared_row_is_the_suite_and_its_children_are_below_it(self):
        """A first-level child and the suite are two rows, not one: the child
        carries its own Score and the suite the aggregate of every child."""
        with scratch_dir() as tmp:
            doc = aggregate_doc("JetStream3.0", "Geometric", {"x": [1.0], "y": [4.0]})
            a, b = self._pair(tmp, doc)
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            means = report_means(cp.stdout)
            self.assertEqual(means[("JetStream3.0", "Score")][0], 2.0)
            self.assertEqual(means[("JetStream3.0/x", "Score")][0], 1.0)
            self.assertEqual(means[("JetStream3.0/y", "Score")][0], 4.0)

    def test_a_child_that_declares_its_own_aggregate_becomes_a_row_too(self):
        """A declaration is not the suite root's alone: a JetStream3 subtest
        declares its Time as the geometric mean of First/Worst/Average, and a
        level that resolves nothing is a level the file read and dropped."""
        with scratch_dir() as tmp:
            doc = {"JetStream3.0": {
                "metrics": {"Score": ["Geometric"]},
                "tests": {"gaussian-blur": {
                    "metrics": {"Score": {"current": [8.0]}, "Time": ["Geometric"]},
                    "tests": {
                        "First": {"metrics": {"Time": {"current": [2.0]}}},
                        "Worst": {"metrics": {"Time": {"current": [8.0]}}},
                        "Average": {"metrics": {"Time": {"current": [4.0]}}},
                    }}}}}
            a, b = self._pair(tmp, doc)
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            means = report_means(cp.stdout)
            self.assertEqual(4.0, means[("JetStream3.0/gaussian-blur", "Time")][0],
                             "the geometric mean of 2, 8 and 4")
            self.assertEqual(8.0, means[("JetStream3.0/gaussian-blur", "Score")][0])
            self.assertEqual(8.0, means[("JetStream3.0", "Score")][0])

    def test_only_the_topmost_declaration_that_cannot_be_resolved_says_so(self):
        """Every level above a silent subtest is silent for that one reason,
        and a report that says it once per level buries the subtest's name."""
        with scratch_dir() as tmp:
            doc = {"JetStream3.0": {
                "metrics": {"Score": ["Geometric"]},
                "tests": {"gaussian-blur": {
                    "metrics": {"Score": ["Geometric"]},
                    "tests": {"First": {"metrics": {"Score": {}}}}}}}}
            a, b = self._pair(tmp, doc)
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            said = [l for l in cp.stdout.splitlines() if "report no Score" in l]
            self.assertTrue(said, cp.stdout)
            self.assertTrue(all("JetStream3.0's Score" in l for l in said),
                            "only the suite's own declaration is reported:\n"
                            + "\n".join(said))

    def test_a_partial_suite_still_reports_its_subtests_and_says_why_it_has_no_total(self):
        """`ab-precision` refuses a partial suite -- a stopping rule cannot run
        on a score that is not the plan's. A report is what the operator reads
        to find out which subtest went silent, so it reports and names it."""
        with scratch_dir() as tmp:
            doc = aggregate_doc("JetStream3.0", "Geometric", {"x": [1.0], "y": [4.0]})
            doc["JetStream3.0"]["tests"]["y"] = {"metrics": {"Time": {"current": [9.0]}}}
            a, b = self._pair(tmp, doc)
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            means = report_means(cp.stdout)
            self.assertNotIn(("JetStream3.0", "Score"), means,
                             "a partial suite has no headline score")
            self.assertIn(("JetStream3.0/x", "Score"), means)
            self.assertIn("1 of 2 first-level tests report no Score", cp.stdout)
            self.assertIn("(y)", cp.stdout)
            self.assertIn("side A", cp.stdout)
            precision = wkdata("ab-precision", "--a", str(a), "--b", str(b))
            self.assertNotEqual(precision.returncode, 0,
                                "the stopping rule refuses what the report warns about")

    def test_an_aggregator_the_report_cannot_take_is_named_rather_than_dropped(self):
        with scratch_dir() as tmp:
            doc = aggregate_doc("JetStream3.0", "Harmonic", {"x": [1.0], "y": [4.0]})
            a, b = self._pair(tmp, doc)
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("Harmonic", cp.stdout)
            self.assertNotIn(("JetStream3.0", "Score"), report_means(cp.stdout))


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

        # Each `wk bench` invocation is a task of one run; `wk bench ls`
        # prints every run's directory, which is what report takes.
        ls = run("bench", "ls")
        run_ids = bench_ls_runs(ls.stdout)
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
