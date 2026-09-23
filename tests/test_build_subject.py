"""What a running build is of, and what a watchdog is measuring.

Three readings that were wrong about the same thing -- a build that is not one
process and not one kind:

  * `wk status` said a build was running and not what it was building, so an
    instrumented slot and the measured one beside it read alike, and a number
    from the first is not this engine's (image_build_subject, lib/image.sh);
  * build/mem-watchdog.sh walked the process tree under the build, and
    bitbake's cooker detaches out of it -- so the same stage was killed on the
    machine floor at "peak 96MB of budget 12800MB" while it held gigabytes
    (measured 2026-09-16, twice);
  * `wk selftest` would start beside a build and take the machine out from
    under it, while the suite already skipped the other way round.

Run: python3 -m unittest tests.test_build_subject -v
"""
import json
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, builds_on_the_books_env, func_body, run

LIBS = "\n".join('. "%s/%s"' % (REPO, f) for f in (
    "lib/common.sh", "lib/store.sh", "lib/target.sh", "lib/image.sh"))

SHA = "a" * 40


class TestABuildSaysWhatItIsOf(WkTest):
    def _subject(self, *args):
        cp = bash(LIBS + "\nimage_build_subject " + " ".join("'%s'" % a for a in args))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_an_instrumented_slot_says_it_is_not_a_measurement(self):
        got = self._subject("yocto-p", "webkit", "base-instr", SHA, "wpe-cross-pgo-collect")
        self.assertIn("base-instr", got)
        self.assertIn("yocto-p", got)
        self.assertIn(SHA[:12], got)
        self.assertIn("not a measurement", got)

    def test_the_measured_build_says_which_it_is(self):
        got = self._subject("yocto-p", "webkit", "base", SHA, "wpe-cross-pgo-use")
        self.assertIn("the measured build", got)
        self.assertNotIn("not a measurement", got)

    def test_a_slot_built_without_a_profile_says_so(self):
        got = self._subject("yocto-p", "webkit", "base", SHA, "wpe-cross")
        self.assertIn("without a profile", got)

    def test_the_two_are_not_the_same_words(self):
        """The whole point: a reader can tell them apart at a glance."""
        collect = self._subject("yocto-p", "webkit", "base-instr", SHA,
                                "wpe-cross-pgo-collect")
        use = self._subject("yocto-p", "webkit", "base", SHA, "wpe-cross-pgo-use")
        self.assertNotEqual(collect, use)

    def test_the_mix_stage_says_which_slots_collection(self):
        got = self._subject("yocto-p", "pgo-mix", "base", "", "")
        self.assertIn("mixing", got)
        self.assertIn("base", got)

    def test_an_image_stage_says_the_stage_and_the_lane(self):
        got = self._subject("yocto-p", "image", "", "", "")
        self.assertIn("image stage", got)
        self.assertIn("yocto-p", got)

    def test_the_lane_is_named_every_time(self):
        """Two lanes of one profile build two different things, so the lane is
        the subject's first fact."""
        for stage in ("webkit", "pgo-mix", "image"):
            with self.subTest(stage=stage):
                self.assertIn("yocto-p", self._subject("yocto-p", stage, "s", SHA, "wpe-cross"))


class TestTheRecordCarriesIt(WkTest):
    def test_the_yocto_build_records_a_subject_on_its_task(self):
        spawn = func_body((REPO / "image" / "yocto.sh").read_text(), "yocto_spawn")
        self.assertIn('task_set "$YOCTO_TASK" subject "$subject"', spawn)

    def test_it_is_computed_where_the_facts_are(self):
        text = (REPO / "image" / "yocto.sh").read_text()
        self.assertIn(
            '"$(image_build_subject "$ws" "$stage" "$slot" "$commit" "$cross_config")"', text)

    def test_the_ab_records_what_it_measures(self):
        """`wk status` names a running A/B by its task, which says when it was
        requested and not what it is comparing."""
        self.assertIn('task_set "$AB_TASK" subject', (REPO / "cmd" / "ab").read_text())

    def test_status_emits_it(self):
        self.assertIn('r.opt("subject", t.field("subject"))', (REPO / "lib" / "wk" / "status.py").read_text())

    def test_both_renderers_print_it(self):
        text = (REPO / "lib" / "wk" / "statusview.py").read_text()
        self.assertIn('if t.get("subject"):', text)
        self.assertIn("k.subject ?", text)

    def test_a_reader_sees_it_against_the_running_build(self):
        """The renderer against a record of exactly the shape cmd/status
        emits: the subject is a line of its own under the task's heading and
        above its plan."""
        subject = ("slot base in yocto-p at 6f7bb97a3e06 -- instrumented, "
                   "to collect a profile from -- not a measurement")
        from tests.test_status import render
        cp = render([{"kind": "machine", "name": "moose"},
                     {"kind": "task", "machine": "moose", "task": "yocto-x", "task_kind": "yocto", "name": "yocto-p",
                      "state": "running", "since": "2026-09-16T20:00:00Z", "subject": subject, "steps": ["running"],
                      "plan": ["wk sysimage webkit ..."], "kill": "wk ... --stop"}])
        lines = [l.strip() for l in cp.stdout.splitlines() if l.strip()]
        self.assertIn(subject, lines)
        self.assertLess(lines.index(subject), lines.index("[>] wk sysimage webkit ..."))


class TestTheWatchdogMeasuresWhatDetached(WkTest):
    """The cgroup holds the workspace and nothing else, so it counts what left
    the process tree. Where there is no cgroup -- a macOS guest -- the tree is
    still the whole answer, and the watchdog says which it is using."""

    WATCHDOG = REPO / "build" / "mem-watchdog.sh"

    def _lift(self, *funcs):
        out = []
        for f in funcs:
            out.append(func_body(self.WATCHDOG.read_text(), f))
        return "\n".join(out)

    def test_the_cgroup_reading_is_in_megabytes(self):
        cp = bash('_cgroup_read() { echo 13421772800; }\n'
                  + self._lift("_cgroup_mb").replace(
                      '[ -r /sys/fs/cgroup/memory.current ] || return 0\n'
                      "    awk '{ printf \"%d\\n\", $1 / 1048576 }' /sys/fs/cgroup/memory.current",
                      "_cgroup_read | awk '{ printf \"%d\\n\", $1 / 1048576 }'")
                  + "\n_cgroup_mb")
        self.assertEqual(cp.stdout.strip(), "12800", cp.stdout + cp.stderr)

    def test_nothing_comes_back_where_there_is_no_cgroup(self):
        """macOS guests have none, and an empty reading is what picks the tree."""
        cp = bash(self._lift("_cgroup_mb") + "\necho \"[$(_cgroup_mb)]\"")
        text = self.WATCHDOG.read_text()
        self.assertIn("[ -r /sys/fs/cgroup/memory.current ] || return 0", text)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_it_says_which_of_the_two_it_is_measuring(self):
        text = self.WATCHDOG.read_text()
        self.assertIn('SOURCE=cgroup', text)
        self.assertIn('[ -n "$(_cgroup_mb)" ] || SOURCE=tree', text)
        self.assertIn('echo "wk: memory: measuring the $SOURCE"', text)

    def test_the_sample_takes_the_cgroups_figure_when_there_is_one(self):
        self.assertIn('[ "$SOURCE" = tree ] || rss=$(_cgroup_mb)',
                      self.WATCHDOG.read_text())

    def test_a_kill_that_left_something_behind_says_so(self):
        """The kill is over the process tree, and what detached is exactly what
        the tree cannot reach -- so a record claiming the build stopped would
        be claiming work no run of it did."""
        text = self.WATCHDOG.read_text()
        self.assertIn("_report_survivors", text)
        self.assertEqual(text.count("_report_survivors"), 3, "both kills, and the definition")
        self.assertIn("--stop", func_body(text, "_report_survivors"))

    def test_the_reason_the_tree_is_not_enough_is_recorded_with_its_measurement(self):
        head = self.WATCHDOG.read_text().split("\nset -uo", 1)[0]
        self.assertIn("bitbake's cooker detaches", head)
        self.assertIn("2026-09-16", head)


class TestSelftestRefusesBesideABuild(WkTest):
    """Both directions or neither: a test that makes a real workspace already
    skips while a build is on the machine's books (tests/support.py), so a
    live run refuses to start beside one. Driven against a fake reading
    (builds_on_the_books_env), never this machine's books."""

    def test_it_asks_the_suites_own_reading(self):
        """One implementation: the suite already reads the records where a
        container workspace is really built, and this asks that rather than
        spelling the location a second time."""
        self.assertIn("from tests.support import builds_on_the_books",
                      (REPO / "cmd" / "selftest").read_text())

    def test_a_live_run_is_a_barrier_naming_the_build_and_the_way_on(self):
        env = builds_on_the_books_env(self.tmp, "wk-test-fake-build")
        cp = run("selftest", "--live", "nosuchtestzz", env=env)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("a build is on this machine's books", cp.stdout)
        self.assertIn("wk-test-fake-build", cp.stdout)
        self.assertIn("--force proceeds anyway", cp.stdout)
        self.assertIn("wk selftest\n", cp.stdout)
        self.assertNotIn("tiers:", cp.stdout)

    def test_a_killed_builds_record_is_not_on_the_books(self):
        """A build killed with -9 leaves its record; a dead `pid:` holder is
        no build, while a live one and one held in a workspace still count."""
        from tests.support import _BUILD_RECORDS
        books = self.tmp / "state" / "wk" / "builds"
        books.mkdir(parents=True)
        dead = subprocess.Popen(["true"]); dead.wait()
        (books / "a").write_text("label=killed\nholder=pid:%d\n" % dead.pid)
        (books / "b").write_text("label=running\nholder=pid:%d\n" % os.getpid())
        (books / "c").write_text("label=in-a-workspace\nholder=ws:w:build.pid\n")
        out = subprocess.run(["bash", "-c", _BUILD_RECORDS], capture_output=True, text=True,
                             env=dict(os.environ, XDG_STATE_HOME=str(self.tmp / "state"))).stdout
        labels = [l.split("=", 1)[1] for l in out.splitlines() if l.startswith("label=")]
        self.assertEqual(sorted(labels), ["in-a-workspace", "running"])

    def test_the_tiers_that_need_no_machine_run_beside_a_build(self):
        env = builds_on_the_books_env(self.tmp, "wk-test-fake-build")
        cp = run("selftest", "test_the_two_are_not_the_same_words", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("tiers: lint,unit  tests: 1 ", cp.stdout)


if __name__ == "__main__":
    unittest.main()
