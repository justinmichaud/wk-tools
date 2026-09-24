"""What a silent build says for itself (lib/wk/job.py's `build_processes` and `stall_report`).

A full-LTO link writes nothing to the log for many minutes while one `ld`
holds a core, so silence alone cannot be reported as a stall: the process
table is the evidence, read here from a fake machine's `ps` so both
platforms' spellings are exercised on either.

Run: python3 tests/run.py -k tests.test_stall_report
"""
import contextlib
import io
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import job  # noqa: E402
from wk.machine import Fake, here  # noqa: E402

# Darwin's `comm` is the executable's full path; Linux's is the bare name.
DARWIN_PS = """\
 99.5 /Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ld
  0.4 /usr/libexec/xpcproxy
  0.0 /bin/bash
"""
LINUX_PS = """\
 12.0 cc1plus
 11.5 cc1plus
  9.0 clang++
  0.1 bash
  0.0 sshd
"""
IDLE_PS = """\
  0.3 /bin/bash
  0.0 /usr/sbin/sshd
"""


def _machine(ps_out):
    m = Fake()
    m.answer(["ps", "-A", "-o", "pcpu=,comm="], out=ps_out)
    return m


class TestTheProcessReadings(unittest.TestCase):
    def test_a_linker_named_by_its_full_path_is_counted(self):
        """Darwin answers `comm` with the executable's path, so a pattern
        anchored at `^` counts zero of Xcode's linkers."""
        self.assertEqual(job.build_processes(_machine(DARWIN_PS)), [(99.5, "ld")])

    def test_every_compiler_counts_not_only_the_first(self):
        self.assertEqual(len(job.build_processes(_machine(LINUX_PS))), 3)

    def test_the_busiest_comes_first(self):
        self.assertEqual(job.build_processes(_machine(LINUX_PS))[0], (12.0, "cc1plus"))

    def test_a_machine_building_nothing_counts_nothing(self):
        self.assertEqual(job.build_processes(_machine(IDLE_PS)), [])

    def test_the_reading_never_names_its_own_reader(self):
        """`pcpu` is an average over a process's whole life, so a just-forked
        `ps` reads at hundreds of percent and wins every sort."""
        names = [n for _, n in job.build_processes(here())]
        for tool in ("ps", "python3", "sh"):
            self.assertNotIn(tool, names)


class TestWhatTheReportClaims(unittest.TestCase):
    def _report(self, ps_out):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            job.stall_report(_machine(ps_out), "/dev/null", 301)
        return err.getvalue()

    def test_a_working_linker_is_not_reported_as_a_stall(self):
        out = self._report(DARWIN_PS)
        self.assertIn("301s", out)
        self.assertIn("full-LTO link", out, out)
        self.assertIn("ld at 99.5% CPU", out, out)

    def test_a_machine_doing_nothing_is_reported_as_doing_nothing(self):
        out = self._report(IDLE_PS)
        self.assertIn("nothing here is compiling or linking", out, out)
        self.assertNotIn("full-LTO", out, out)

    def test_the_status_report_leaves_the_verdict_to_the_evidence(self):
        """The log's age is not evidence of a stall, so the verdict belongs
        to the report that takes the process reading."""
        text = (REPO / "lib" / "wk" / "status.py").read_text()
        self.assertNotIn("likely stalled or killed", text)
        self.assertIn('"no log output for %ss -- counted as busy, since nothing', text)


if __name__ == "__main__":
    unittest.main()
