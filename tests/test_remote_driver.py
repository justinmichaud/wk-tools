"""_remote_probe_parse (targets/remote.sh): the pure half of the remote
capacity probe -- cores/load/memory/ionice out of the raw text
_remote_probe_cmd gathers from the far machine, with no ssh involved.

Tested on captured samples for both shapes the probe has to understand:
Linux (`nproc`, `/proc/loadavg`, `/proc/meminfo`) and Darwin
(`sysctl -n hw.ncpu`, `sysctl -n vm.loadavg`, `vm_stat`) -- the same shape a
macOS or BSD remote target answers with, which is otherwise unverifiable
without one in hand.

Run: python3 -m unittest tests.test_remote_driver -v
"""
import subprocess
import unittest

from tests.support import REPO, _clean_env

_SOURCE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/targets/remote.sh"
'''

# A real /proc/loadavg line and a real /proc/meminfo excerpt, as
# `_remote_probe_cmd` would return them from a Linux remote.
LINUX_SAMPLE = """Linux
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

# A real `sysctl -n vm.loadavg` line and `vm_stat` excerpt, as
# `_remote_probe_cmd` would return them from a macOS remote. macOS has no
# ionice (util-linux only).
DARWIN_SAMPLE = """Darwin
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


def parse(sample):
    """Feed `sample` on stdin to _remote_probe_parse, exactly the interface
    the driver's own comment documents:
        bash -c '. targets/remote.sh; _remote_probe_parse <<<"$sample"'
    """
    cp = subprocess.run(
        ["bash", "-c", _SOURCE + "\n_remote_probe_parse"],
        cwd=str(REPO),
        env=_clean_env(wk_root=True),
        input=sample,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return cp


class TestRemoteProbeParseLinux(unittest.TestCase):
    def test_parses_cores_load_mem_ionice_from_proc(self):
        """cores, load and MemAvailable come out of /proc/loadavg and
        /proc/meminfo, and ionice is reported yes when present"""
        cp = parse(LINUX_SAMPLE)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        cores, load, mem, ionice = cp.stdout.splitlines()
        self.assertEqual(cores, "8")
        self.assertEqual(load, "0")  # int(0.52)
        self.assertEqual(mem, "20000")  # int(20480000 / 1024)
        self.assertEqual(ionice, "yes")


class TestRemoteProbeParseDarwin(unittest.TestCase):
    def test_parses_cores_load_mem_ionice_from_sysctl_vm_stat(self):
        """cores, load and free memory come out of `sysctl -n hw.ncpu`,
        `sysctl -n vm.loadavg` and `vm_stat`, and ionice is reported no --
        util-linux has no Darwin equivalent"""
        cp = parse(DARWIN_SAMPLE)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        cores, load, mem, ionice = cp.stdout.splitlines()
        self.assertEqual(cores, "10")
        self.assertEqual(load, "1")  # int(1.23), the 2nd field of "{ ... }"
        # (123456 free + 345678 inactive + 45678 speculative) pages * 16384
        # bytes/page, in MB.
        self.assertEqual(mem, "8043")
        self.assertEqual(ionice, "no")

    def test_missing_page_size_yields_no_mem_answer_not_a_crash(self):
        """a vm_stat excerpt with no page-size header parses cores/load/ionice
        fine and answers 0 for memory rather than dividing by an empty page
        size"""
        sample = """Darwin
4
{ 0.10 0.20 0.30 }
===MEM===
Pages free:                              123456.
===IONICE===
no
"""
        cp = parse(sample)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        cores, load, mem, ionice = cp.stdout.splitlines()
        self.assertEqual(cores, "4")
        self.assertEqual(load, "0")
        self.assertEqual(mem, "0")
        self.assertEqual(ionice, "no")


class TestRemoteProbeParseRobustness(unittest.TestCase):
    def test_trailing_blank_line_does_not_erase_ionice(self):
        """a sample whose last line is a bare newline (what a here-string
        appends to a variable that already ends in one) must not clobber the
        ionice answer that came before it"""
        cp = parse(LINUX_SAMPLE + "\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        *_, ionice = cp.stdout.splitlines()
        self.assertEqual(ionice, "yes")


if __name__ == "__main__":
    unittest.main()
