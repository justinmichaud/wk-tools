"""The remote driver's probe (lib/wk/places.py): `parse_probe` over captured Linux and Darwin samples, and the
one round trip that fetches it, under a ceiling of its own."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.act import Refused  # noqa: E402
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


class TestRemoteProbeParse(unittest.TestCase):
    def test_parses_cores_load_mem_ionice_os_from_proc_past_a_trailing_blank_line(self):
        p = places.parse_probe(LINUX_SAMPLE)
        self.assertEqual(fields(p), (8, 0, 20000, "yes", "linux"))   # int(0.52); int(20480000 / 1024)
        self.assertEqual(fields(places.parse_probe(LINUX_SAMPLE + "\n")), fields(p))
        self.assertEqual((p["home"], p["root"]), ("/home/t", "/home/t/wk"))
        self.assertEqual(places.parse_probe(LINUX_SAMPLE, "/srv/wk")["root"], "/srv/wk")


class TestTheDefaultRoot(unittest.TestCase):
    def test_the_probe_the_far_end_and_the_driver_agree(self):
        self.assertEqual(places.parse_probe(LINUX_SAMPLE)["root"], places.default_root("/home/t"))
        env = {"HOME": "/h", "WK_REMOTE_MARKER": "/nonexistent/.wk-remote"}
        reg = places.Registry(REPO, env=env, machine=Fake())
        reg.fleet.load = lambda name: {}
        self.assertEqual(reg.far_root(), places.default_root("/h"))
        local = places.Remote("box", REPO, dict(env, WK_REMOTE_LOCAL="1"), Fake())
        self.assertEqual(local.store.store_dir(), places.default_root("/h"))


class TestRemoteProbeParseDarwin(unittest.TestCase):
    def test_parses_cores_load_mem_ionice_os_from_sysctl_vm_stat(self):
        # int(1.23), the 2nd field of "{ ... }"; (123456 free + 345678 inactive
        # + 45678 speculative) pages * 16384 bytes/page, in MB.
        self.assertEqual(fields(places.parse_probe(DARWIN_SAMPLE)), (10, 1, 8043, "no", "macos"))

    def test_a_missing_page_size_is_refused_not_read_as_no_memory(self):
        sample = "/Users/t\nDarwin\n4\n{ 0.10 0.20 0.30 }\n===MEM===\nPages free:   123456.\n===IONICE===\nno\n"
        with self.assertRaisesRegex(ValueError, "vm_stat printed no page size"):
            places.parse_probe(sample)


    def test_an_answer_missing_a_figure_is_refused_not_read_as_one_core_and_no_memory(self):
        with self.assertRaisesRegex(ValueError, "the core count is '', not a number"):
            places.parse_probe("/home/t\n")
        with self.assertRaisesRegex(ValueError, "MemAvailable is '', not a number"):
            places.parse_probe(LINUX_SAMPLE.replace("MemAvailable:", "MemGone:"))


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
    """ConnectTimeout bounds only the connect, so the probe runs under WK_PROBE_SECONDS of its own."""

    def remote(self, fake, seconds):
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-probe-"))
        self.addCleanup(os.system, "rm -rf %s" % tmp)
        (tmp / "hosts").mkdir()
        (tmp / "hosts" / "hangs.conf").write_text("kind=build\nhost=hangs.example\n")
        env = {"HOME": str(tmp), "XDG_STATE_HOME": str(tmp / "state"), "WK_MACHINES_DIR": str(tmp / "hosts"),
               "WK_PROBE_SECONDS": seconds, "PATH": os.environ.get("PATH", "")}
        return places.Registry(REPO, env=env, machine=fake).load("hangs")

    def test_a_machine_that_connects_and_says_nothing_is_given_up_on(self):
        fake = TimingFake(Result(TIMED_OUT, "", "timed out after 2s"))
        t = self.remote(fake, "2")
        self.assertEqual(t.probe(), ("unreachable", "timed out after 2s"))
        self.assertEqual((t.info("a"), t.far_side()), ("unreachable", "unreachable"))
        self.assertEqual(fake.timeouts, [2])   # one round trip, under the ceiling; nothing asked again

    def test_the_ceiling_is_not_reached_when_the_machine_answers(self):
        fake = TimingFake(Result(0, LINUX_SAMPLE))
        t = self.remote(fake, "20")
        self.assertEqual(t.answers(), (True, ""))
        self.assertEqual((t.cores(), t.home()), (8, "/home/t"))
        self.assertEqual(fake.timeouts, [20])

    def test_an_unreadable_answer_is_no_answer_and_a_command_needing_it_says_why(self):
        t = self.remote(TimingFake(Result(0, "/home/t\nLinux\neight\n")), "20")
        self.assertEqual(t.answers(), (False, "it answered the probe with what this end cannot read: the core count is 'eight', not a number"))
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
            t.cores()
        self.assertIn("PROBE_SCRIPT (lib/wk/places.py) is what ran", err.getvalue())


if __name__ == "__main__":
    unittest.main()
