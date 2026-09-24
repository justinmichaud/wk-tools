"""The remote driver's probe (lib/wk/targets.py): `parse_probe` turns the raw
text the far machine answers with into cores/load/memory/ionice/os, and the
one round trip that fetches it runs under a ceiling of its own.

Tested on captured samples for both shapes the probe has to understand:
Linux (`nproc`, `/proc/loadavg`, `/proc/meminfo`) and Darwin
(`sysctl -n hw.ncpu`, `sysctl -n vm.loadavg`, `vm_stat`) -- the same shape a
macOS or BSD remote target answers with, which is otherwise unverifiable
without one in hand.

Run: python3 -m unittest tests.test_remote_driver -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import TIMED_OUT, Fake, Result  # noqa: E402

# A real /proc/loadavg line and a real /proc/meminfo excerpt, after the
# `$HOME` line every probe starts with.
LINUX_SAMPLE = """/home/t
Linux
8
0.52 0.58 0.61 2/1234 56789
===MEM===
MemTotal:       32806140 kB
MemFree:         1234567 kB
MemAvailable:   20480000 kB
Buffers:          123456 kB
Cached:          8901234 kB
===IONICE===
yes
"""

# A real `sysctl -n vm.loadavg` line and `vm_stat` excerpt. macOS has no
# ionice (util-linux only).
DARWIN_SAMPLE = """/Users/t
Darwin
10
{ 1.23 1.87 2.01 }
===MEM===
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              123456.
Pages active:                            234567.
Pages inactive:                          345678.
Pages speculative:                        45678.
Pages throttled:                              0.
Pages wired down:                        567890.
===IONICE===
no
"""


def fields(parsed):
    return tuple(parsed[k] for k in ("cores", "load", "mem_mb", "ionice", "os"))


class TestRemoteProbeParseLinux(unittest.TestCase):
    def test_parses_cores_load_mem_ionice_os_from_proc(self):
        """cores, load and MemAvailable come out of /proc/loadavg and
        /proc/meminfo, ionice is reported yes when present, and the platform
        the sample's `uname -s` named comes back out as os()'s answer"""
        p = targets.parse_probe(LINUX_SAMPLE)
        self.assertEqual(fields(p), (8, 0, 20000, "yes", "linux"))   # int(0.52); int(20480000 / 1024)
        self.assertEqual((p["home"], p["root"]), ("/home/t", "/home/t/wk"))
        self.assertEqual(targets.parse_probe(LINUX_SAMPLE, "/srv/wk")["root"], "/srv/wk")


class TestRemoteProbeParseDarwin(unittest.TestCase):
    def test_parses_cores_load_mem_ionice_os_from_sysctl_vm_stat(self):
        """cores, load and free memory come out of `sysctl -n hw.ncpu`,
        `sysctl -n vm.loadavg` and `vm_stat`, ionice is reported no --
        util-linux has no Darwin equivalent -- and the platform is macos,
        which is what decides the build system a config uses there"""
        # int(1.23), the 2nd field of "{ ... }"; (123456 free + 345678 inactive
        # + 45678 speculative) pages * 16384 bytes/page, in MB.
        self.assertEqual(fields(targets.parse_probe(DARWIN_SAMPLE)), (10, 1, 8043, "no", "macos"))

    def test_missing_page_size_yields_no_mem_answer_not_a_crash(self):
        """a vm_stat excerpt with no page-size header parses cores/load/ionice
        fine and answers 0 for memory rather than dividing by an empty page
        size"""
        sample = "/Users/t\nDarwin\n4\n{ 0.10 0.20 0.30 }\n===MEM===\nPages free:   123456.\n===IONICE===\nno\n"
        self.assertEqual(fields(targets.parse_probe(sample)), (4, 0, 0, "no", "macos"))


class TestRemoteProbeParseRobustness(unittest.TestCase):
    def test_trailing_blank_line_does_not_erase_ionice(self):
        """a sample whose last line is a bare newline must not clobber the
        ionice answer that came before it"""
        p = targets.parse_probe(LINUX_SAMPLE + "\n")
        self.assertEqual((p["ionice"], p["os"]), ("yes", "linux"))

    def test_a_machine_that_answered_nothing_useful_still_parses(self):
        """a far shell that printed only its home is a machine with one core and no load, not a crash"""
        self.assertEqual(fields(targets.parse_probe("/home/t\n")), (1, 0, 0, "no", "linux"))


class TimingFake(Fake):
    """This host, remembering the ceiling each ssh was given."""

    def __init__(self, result):
        super().__init__("host")
        self.result = result
        self.timeouts = []

    def run(self, argv, input=None, timeout=None):
        self.effects.append(("run", tuple(argv)))
        self.timeouts.append(timeout)
        return self.result


class TestTheProbeIsBounded(unittest.TestCase):
    """Every report of the fleet waits on one ssh round trip, and
    ConnectTimeout bounds the TCP connect and nothing after it: a machine
    that accepts the connection and then answers nothing -- a wedged sshd, a
    box deep in swap -- held `wk status <ws>` and `wk logs <ws>` past a 300s
    wait (measured 2026-09-17, with moose down). So the probe runs under a
    ceiling of its own (WK_PROBE_SECONDS), the way lib/reach.sh reads the
    tailnet."""

    def remote(self, fake, seconds):
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-probe-"))
        self.addCleanup(os.system, "rm -rf %s" % tmp)
        (tmp / "hosts").mkdir()
        (tmp / "hosts" / "hangs.conf").write_text("KIND=build\nWK_REMOTE_HOST=hangs.example\n")
        env = {"HOME": str(tmp), "XDG_STATE_HOME": str(tmp / "state"), "WK_MACHINES_DIR": str(tmp / "hosts"),
               "WK_PROBE_SECONDS": seconds, "PATH": os.environ.get("PATH", "")}
        return targets.Registry(REPO, env=env, machine=fake).load("hangs")

    def test_a_machine_that_connects_and_says_nothing_is_given_up_on(self):
        fake = TimingFake(Result(TIMED_OUT, "", "timed out after 2s"))
        t = self.remote(fake, "2")
        self.assertEqual(t.probe(), ("unreachable", "timed out after 2s"))
        self.assertEqual((t.info("a"), t.far_side()), ("unreachable", "unreachable"))
        self.assertEqual(fake.timeouts, [2])   # one round trip, under the ceiling; nothing asked again

    def test_the_ceiling_is_not_reached_when_the_machine_answers(self):
        """A bound that also delays a machine that does answer would make
        every report slower than the thing it reports on: the answer is taken
        as it comes, and the machine is not asked a second time."""
        fake = TimingFake(Result(0, LINUX_SAMPLE))
        t = self.remote(fake, "20")
        self.assertEqual(t.answers(), (True, ""))
        self.assertEqual((t.cores(), t.home()), (8, "/home/t"))
        self.assertEqual(fake.timeouts, [20])


if __name__ == "__main__":
    unittest.main()
