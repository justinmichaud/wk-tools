"""What a silent build says for itself (lib/detach.sh's process readings,
lib/watchdog.sh's `_stall_report`).

A full-LTO link writes nothing to the log for many minutes while one `ld`
holds a core, so silence alone cannot be reported as a stall: the process
table is the evidence, and it is read here through a stubbed `ps` so both
platforms' spellings are exercised on either.

Run: python3 -m unittest tests.test_stall_report -v
"""
import unittest

from tests.support import REPO, WkTest, bash

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


def _stub_ps(out):
    """`ps` as a shell function: what the readings see is the only thing that
    differs between a Mac mid-link and an idle Linux box."""
    return "ps() {\ncat <<'PSOUT'\n" + out + "PSOUT\n}\n"


def _with_ps(out, call):
    return bash(f'''
. "{REPO}/lib/detach.sh"
{_stub_ps(out)}
{call}
''')


class TestTheProcessReadings(WkTest):
    def test_a_linker_named_by_its_full_path_is_counted(self):
        """Darwin answers `comm` with the executable's path, so a pattern
        anchored at `^` counts zero of Xcode's linkers."""
        cp = _with_ps(DARWIN_PS, "build_processes")
        self.assertEqual(cp.stdout.strip(), "1", cp.stdout + cp.stderr)

    def test_the_busiest_one_is_named_with_its_reading(self):
        cp = _with_ps(DARWIN_PS, "busiest_process")
        self.assertEqual(cp.stdout.strip(), "ld at 99.5% CPU", cp.stdout + cp.stderr)

    def test_every_compiler_counts_not_only_the_first(self):
        cp = _with_ps(LINUX_PS, "build_processes")
        self.assertEqual(cp.stdout.strip(), "3", cp.stdout + cp.stderr)

    def test_a_machine_building_nothing_counts_nothing(self):
        cp = _with_ps(IDLE_PS, "build_processes")
        self.assertEqual(cp.stdout.strip(), "0", cp.stdout + cp.stderr)
        cp = _with_ps(IDLE_PS, "busiest_process")
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)

    def test_the_reading_never_names_its_own_pipeline(self):
        """`pcpu` is an average over a process's whole life, so a just-forked
        `ps` reads at hundreds of percent and wins every sort."""
        cp = bash(f'. "{REPO}/lib/detach.sh"\nbusiest_process')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for tool in ("ps ", "sort ", "awk ", "head ", "sed ", "grep "):
            self.assertNotIn(tool, cp.stdout, f"the reader named itself: {cp.stdout}")


class TestWhatTheReportClaims(WkTest):
    def _report(self, ps_out):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/watchdog.sh"
{_stub_ps(ps_out)}
_stall_report /dev/null 301
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

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
        text = (REPO / "cmd" / "status").read_text()
        self.assertNotIn("likely stalled or killed", text)
        self.assertIn('note_warn "no log output for ${age}s"', text)


if __name__ == "__main__":
    unittest.main()
