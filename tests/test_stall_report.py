"""What a silent build says for itself (job.build_processes, job.stall_report), from a fake machine's `ps` in both
platforms' spellings: a full-LTO link is silent for minutes while one `ld` holds a core."""
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
    def test_every_compiler_and_linker_counts_busiest_first_by_either_spelling(self):
        self.assertEqual(job.build_processes(_machine(DARWIN_PS)), [(99.5, "ld")])
        self.assertEqual(job.build_processes(_machine(LINUX_PS)), [(12.0, "cc1plus"), (11.5, "cc1plus"), (9.0, "clang++")])
        self.assertEqual(job.build_processes(_machine(IDLE_PS)), [])

    def test_the_reading_never_names_its_own_reader(self):
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
        self.assertIn("ld at 99.5% CPU", out, out)

    def test_a_machine_doing_nothing_is_reported_as_doing_nothing(self):
        out = self._report(IDLE_PS)
        self.assertNotIn("% CPU", out, out)


if __name__ == "__main__":
    unittest.main()
