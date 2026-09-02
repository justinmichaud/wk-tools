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
                             ("pi-mbr", '"1"'), ("rpi5-usb", '"1 3"')):
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
r_sudo() { echo "(dump)"; }
m_ssh() { echo "b_diag must not address the rescue alone" >&2; return 1; }
b_diag
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("== alpha-1 (/dev/sda1) ==", cp.stdout)
        self.assertIn("== beta-2 (/dev/sda3) ==", cp.stdout)


class TestNoDriverReachesOnlyTheRescue(unittest.TestCase):
    """The bug class that cost 2026-09-01, closed across every driver at once.

    A boot driver's steps act on the *board* -- the arming on its medium, its
    EEPROM, its reboot, its evidence -- and each is needed exactly when the
    board is answering as a bench system rather than as its rescue: a medium
    the firmware prefers, an arming that has to be undone, a leg switch. `m_ssh`
    is the rescue's channel alone. `r_ssh`/`r_sudo` are the one implementation
    of "the channel this machine answered on" (boot/machines.sh), so a driver
    that names m_ssh has a step that cannot run when it is needed.

    Four instances of this were live in one afternoon: pi-tryboot's staging,
    reboot, evidence and disarm; three more in pi-mbr and rpi5-usb; and the
    same shape again in record_clear and in cmd/pi's rsh/rsh_dest.
    """

    DRIVERS = sorted((REPO / "boot").glob("pi-*.sh")) + [REPO / "boot" / "rpi5-usb.sh"]

    # boot/machines.sh defines m_ssh and legitimately uses it for the things
    # that really are the rescue's: the arming *record*, which lives on the host
    # install's root, and the probe that decides which channel answered.
    LIBRARY_ALLOWED = ("m_ssh()", "m_ssh_opts", "m_reachable", "record_write",
                       "record_read", "record_clear", "b_probe", "r_ssh")

    def test_there_are_drivers_to_check(self):
        self.assertGreaterEqual(len(self.DRIVERS), 3, self.DRIVERS)

    def test_no_driver_names_the_rescue_only_channel(self):
        for path in self.DRIVERS:
            with self.subTest(driver=path.name):
                bad = [f"{n}: {l.strip()}"
                       for n, l in enumerate(path.read_text().splitlines(), 1)
                       if "m_ssh" in l and not l.lstrip().startswith("#")]
                self.assertEqual(bad, [], f"{path.name} reaches only the rescue:\n"
                                          + "\n".join(bad))

    def test_the_record_is_cleared_only_where_it_lives(self):
        """record_clear touches the host install's root, so on a board
        answering as its bench system there is nothing to clear -- and saying
        so beats failing the disarm that is trying to get the board back."""
        text = (REPO / "boot" / "machines.sh").read_text()
        body = text[text.index("record_clear()"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("MODE_CHANNEL", body,
                      "record_clear does not ask which channel answered")

    def test_a_card_edit_goes_over_the_channel_that_answered(self):
        """`card_priv` is how every card edit is made, and it named the rescue's
        channel while prefixing `sudo` unconditionally. Both are assumptions a
        bench system breaks: it arms its sibling (so the channel is not the
        rescue's), and a BusyBox one is driven as root with no sudo installed at
        all. `r_sudo` is the one implementation of both questions."""
        body = (REPO / "boot" / "disk.sh").read_text()
        block = body[body.index("card_priv()"):]
        block = block[:block.index("\n}\n")]
        self.assertNotIn("m_ssh", block, "a card edit reaches only the rescue")
        self.assertIn("r_sudo", block, "a card edit does not use the answered channel")
        self.assertNotIn("sudo -n $CARD_PRIV", block,
                         "sudo is prefixed here rather than left to r_sudo, which "
                         "knows a bench-device is already root")

    def test_privilege_never_prompts(self):
        """r_sudo runs over BatchMode ssh with no terminal, so a sudo that
        decides to prompt cannot be answered."""
        body = (REPO / "boot" / "machines.sh").read_text()
        block = body[body.index("r_sudo()"):]
        block = block[:block.index("\n}\n")]
        self.assertIn("sudo -n", block)
        self.assertNotIn('r_ssh "sudo $*"', block)

    def test_wk_pi_reaches_a_board_over_the_channel_that_answered(self):
        """`wk pi`'s own transport, both halves: the command channel and the
        scp destination. They disagreed once -- the EEPROM diff was read over
        one and the file copied over the other, which failed the write."""
        text = (REPO / "cmd" / "pi").read_text()
        for fn in ("rsh()", "rsh_dest()"):
            body = text[text.index(fn):]
            body = body[:body.index("\n}\n")]
            with self.subTest(fn=fn):
                self.assertIn("MODE_CHANNEL" if fn == "rsh_dest()" else "r_ssh", body,
                              f"{fn} does not follow the channel that answered")

    def test_the_shared_library_reads_a_medium_over_the_channel_that_answered(self):
        """The same rule one level down. `b_device_image` and `b_diag` read the
        *medium*, and both are needed while a board answers as its bench system:
        `wk boot --system <id>` resolves an id through the first, which failed
        with "could not read <device> to see what it holds" at exactly the
        moment an A/B leg switch needed it (rpi3, 2026-09-01)."""
        text = (REPO / "boot" / "machines.sh").read_text()
        for fn in ("b_device_image()", "b_diag()"):
            body = text[text.index(fn):]
            body = body[:body.index("\n}\n")]
            with self.subTest(fn=fn):
                self.assertNotIn("m_ssh", body, f"{fn} reaches only the rescue")
                self.assertIn("r_sudo", body, f"{fn} does not use the answered channel")


if __name__ == "__main__":
    unittest.main()


class TestRpi5SelectsBetweenTwoSystems(unittest.TestCase):
    """rpi5's stick can hold two systems, and which pair boots is the firmware's
    own A/B: `autoboot.txt` says partition 1 under [all] and partition 3 under
    [tryboot], so one `reboot "0 tryboot"` boots the second pair and every other
    boot lands on the first. Two one-shots, both firmware-reverting.

    Everything here runs against the sourced driver with its remote halves
    stubbed -- no board. The end-to-end proof is a real `wk boot rpi5 --system`.
    """

    LOAD = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/boot/disk.sh"
machine_load rpi5
load_driver rpi5-usb
'''

    def test_both_pairs_are_candidates(self):
        cp = bash(self.LOAD + 'echo "$B_SYSTEM_PARTS"')
        self.assertEqual(cp.stdout.strip(), "1 3", cp.stdout + cp.stderr)

    def test_arming_the_second_pair_carries_the_flag(self):
        """...and checks the selector is there first: absent, the flag is
        ignored and pair 1 boots -- the wrong system, silently."""
        cp = bash(self.LOAD + '''
rpi5_check_autoboot() { echo "checked" >&2; }
r_sudo() { echo "0x0 0x80000000"; }
b_reboot_tryboot() { echo "tryboot reboot" >&2; }
ARM_SYS_PART=/dev/sda3 b_arm 0xf64
b_reboot
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("checked", cp.stderr, "it armed pair 3 without checking the selector")
        self.assertIn("tryboot reboot", cp.stderr, "the arming reboot did not carry the flag")

    def test_arming_the_first_pair_is_a_plain_reboot(self):
        cp = bash(self.LOAD + '''
rpi5_check_autoboot() { echo "MUST NOT check" >&2; }
r_sudo() { case "$*" in *vcmailbox*) echo "0x0 0x80000000" ;; *) echo "plain reboot" >&2 ;; esac; }
b_reboot_tryboot() { echo "MUST NOT carry the flag" >&2; }
ARM_SYS_PART=/dev/sda1 b_arm 0xf64
b_reboot
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("plain reboot", cp.stderr)
        self.assertNotIn("MUST NOT", cp.stderr, cp.stderr)

    def test_a_partition_this_stick_does_not_select_is_refused(self):
        cp = bash(self.LOAD + 'r_sudo() { echo "0x0 0x80000000"; }\nARM_SYS_PART=/dev/sda5 b_arm 0xf64\n')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("partition 1 or 3", cp.stderr)

    def test_the_selector_is_only_written_where_the_firmware_uses_it(self):
        """The file that makes this work on the rpi5 would break the rpi4: its
        stick is a dedicated medium too, and an autoboot.txt there would make
        its tryboot flag boot the stick's second pair instead of the kernel
        staged on its SD. So the driver declares it and the write asks."""
        cp = bash(self.LOAD + 'command -v b_medium_selects_by_partition >/dev/null && echo yes')
        self.assertEqual(cp.stdout.strip(), "yes", "rpi5 does not declare it")
        cp = bash(self.LOAD.replace("machine_load rpi5", "machine_load rpi4")
                          .replace("load_driver rpi5-usb", "load_driver pi-tryboot")
                  + 'command -v b_medium_selects_by_partition >/dev/null && echo yes || echo no')
        self.assertEqual(cp.stdout.strip(), "no", "pi-tryboot declares a selector it must not have")
        # ...and the write asks, only when it is making a second pair.
        line = [l for l in (REPO / "cmd" / "sysimage").read_text().splitlines()
                if "_medium_autoboot_for" in l and "disk_is_second" in l]
        self.assertTrue(line, "the write does not gate the selector on both facts")
