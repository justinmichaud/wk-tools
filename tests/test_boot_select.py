"""Which system an arming boots (boot/machines.sh): b_systems enumerates the
medium's candidate partitions (B_SYSTEM_PARTS, a driver fact), and
machine_select_system resolves --system against that evidence -- the sole
system when none is named, a refusal that lists the candidates when two are
there to choose from or the name matches nothing.

Everything runs against the sourced library with the remote reads stubbed.
The end-to-end proof is a real `wk boot rpi4 --system <id>` against a stick
holding two systems.

Run: python3 -m unittest tests.test_boot_select -v
"""
import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def bash(script, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)


LOAD = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/boot/disk.sh"
machine_load rpi4
load_driver pi-tryboot
'''


class TestEnumeration(unittest.TestCase):
    def test_default_is_the_first_partition_only(self):
        """machines.sh's default: one candidate, partition 1. Drivers whose
        media hold more say so themselves (pi-tryboot: 1 3; pi-sd: 3)."""
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
echo "$B_SYSTEM_PARTS"
''')
        self.assertEqual(cp.stdout.strip(), "1", cp.stdout + cp.stderr)

    def test_every_driver_states_its_candidates(self):
        """B_SYSTEM_PARTS is a scalar a fleet walk carries from one driver to
        the next, so every medium-bearing driver states its own (the
        BOOT_ORDER_* convention)."""
        for driver, want in (("pi-tryboot", '"1 3"'), ("pi-sd", '"3 5 7"'),
                             ("pi-mbr", '"1"'), ("rpi5-usb", '"1"')):
            text = (REPO / "boot" / f"{driver}.sh").read_text()
            self.assertIn(f"B_SYSTEM_PARTS={want}", text,
                          f"{driver}.sh does not state B_SYSTEM_PARTS={want}")

    def test_b_systems_reads_each_candidate(self):
        """one line per system, `<boot partition> <id>`; a partition with no
        id is skipped, not an error."""
        cp = bash(LOAD + '''
b_device_image() {
    case "$1" in
        /dev/sda1) echo "alpha-111111111111" ;;
        /dev/sda3) echo "" ;;
    esac
}
b_systems
''')
        self.assertEqual(cp.stdout.strip(), "/dev/sda1 alpha-111111111111",
                         cp.stdout + cp.stderr)

    def test_b_systems_fails_when_the_machine_cannot_be_asked(self):
        """an unreachable machine is not an empty medium."""
        cp = bash(LOAD + '''
b_device_image() { return 1; }
if b_systems; then echo no-failure; else echo failed; fi
''')
        self.assertEqual(cp.stdout.strip(), "failed", cp.stdout + cp.stderr)


class TestSelection(unittest.TestCase):
    def _select(self, systems_body, arg):
        return bash(LOAD + f'''
b_systems() {{ {systems_body}; }}
machine_select_system "{arg}"
''')

    def test_sole_system_is_the_default(self):
        cp = self._select('printf "%s\\n" "/dev/sda1 alpha-1"', "")
        self.assertEqual(cp.stdout.strip(), "/dev/sda1 alpha-1", cp.stdout + cp.stderr)

    def test_two_systems_refuse_to_guess(self):
        cp = self._select('printf "%s\\n%s\\n" "/dev/sda1 alpha-1" "/dev/sda3 beta-2"', "")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("holds 2 systems", cp.stderr)
        self.assertIn("alpha-1", cp.stderr)
        self.assertIn("beta-2", cp.stderr)
        self.assertIn("--system", cp.stderr)

    def test_named_system_is_matched_against_the_medium(self):
        cp = self._select('printf "%s\\n%s\\n" "/dev/sda1 alpha-1" "/dev/sda3 beta-2"', "beta-2")
        self.assertEqual(cp.stdout.strip(), "/dev/sda3 beta-2", cp.stdout + cp.stderr)

    def test_a_name_the_medium_does_not_hold_is_refused_with_the_list(self):
        cp = self._select('printf "%s\\n" "/dev/sda1 alpha-1"', "gamma-3")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("alpha-1", cp.stderr)
        self.assertIn("gamma-3", cp.stderr)
        self.assertIn("@second", cp.stderr)

    def test_an_empty_medium_names_the_write_remedy(self):
        cp = self._select("return 0", "")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("holds no wk system yet", cp.stderr)
        self.assertIn("wk sysimage write", cp.stderr)

    def test_an_unreadable_medium_is_not_an_empty_one(self):
        cp = self._select("return 1", "")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("could not read", cp.stderr)


class TestDiag(unittest.TestCase):
    def test_diag_reads_every_system_and_says_which_is_which(self):
        """after a failed boot of the second system, the first one's dump is
        the stale one -- an unlabeled dump misleads."""
        cp = bash(LOAD + '''
b_systems() { printf "%s\\n%s\\n" "/dev/sda1 alpha-1" "/dev/sda3 beta-2"; }
m_ssh() { echo "(dump)"; }
b_diag
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("== alpha-1 (/dev/sda1) ==", cp.stdout)
        self.assertIn("== beta-2 (/dev/sda3) ==", cp.stdout)


if __name__ == "__main__":
    unittest.main()
