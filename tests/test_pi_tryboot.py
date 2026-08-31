"""The pi-tryboot boot driver: a Pi 4 whose bench medium the bootloader will
not boot. The firmware loads the bench kernel from the SD via a tryboot
one-shot (staged at arm time from the medium's own boot partition) and the
kernel mounts the bench root on the medium by PARTUUID (boot/pi-tryboot.sh).

Everything here runs against the sourced driver with its remote halves
stubbed -- no board. The end-to-end proof is a real `wk boot rpi4`.
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


class TestArrangement(unittest.TestCase):
    def test_rpi4_names_the_driver_and_keeps_its_media(self):
        """rpi4.conf: driver pi-tryboot, bench root on the USB drive, rescue
        root on the SD -- the media did not move, only how one boot is chosen."""
        cp = bash(LOAD + 'echo "$MACH_DRIVER $MACH_DEVICE $MACH_ROOT $MACH_DTB"')
        self.assertEqual(cp.stdout.strip(),
                         "pi-tryboot /dev/sda /dev/mmcblk0p2 bcm2711-rpi-4-b.dtb",
                         cp.stdout + cp.stderr)

    def test_no_self_disarm(self):
        """tryboot's flag is cleared by the firmware, so there is nothing for
        the image to park: the driver defines no b_self_disarm_sh, and
        `wk sysimage write` stages no disarm unit for this machine."""
        cp = bash(LOAD + 'command -v b_self_disarm_sh || echo none')
        self.assertEqual(cp.stdout.strip(), "none", cp.stdout + cp.stderr)

    def test_identity_still_reads_off_the_bench_medium(self):
        """wk-image.id and wk-diag.txt live on the medium's own boot
        partition (b_boot_part default), not on the SD staging."""
        cp = bash(LOAD + 'b_boot_part')
        self.assertEqual(cp.stdout.strip(), "/dev/sda1", cp.stdout + cp.stderr)


class TestStaging(unittest.TestCase):
    def test_stage_script_parses_and_names_every_piece(self):
        """the arm script is POSIX sh: mounts the medium's boot partition
        read-only, stages kernel+dtb+overlays+cmdline into second.new, writes
        tryboot.txt from the medium's config.txt plus one os_prefix line, and
        renames both into place last (a kill mid-arm leaves the old staging whole)."""
        cp = bash(LOAD + 'tryboot_stage_sh "$(disk_part "$MACH_DEVICE" 1)" "$MACH_DTB"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        s = cp.stdout
        # The OS files come from the mounted medium; the firmware pair from
        # the SD's own root (the pair proven on this exact board -- an
        # image's own can predate the board revision). The references must
        # expand on the board, not arrive pre-quoted as literals.
        self.assertIn('"$src/bcm2711-rpi-4-b.dtb"', cp.stdout, "the dtb is the kernel's, from the medium")
        for f in ("start4.elf", "fixup4.dat"):
            self.assertIn(f'"$boot/{f}"', cp.stdout, f"{f} must be the SD's own, not the image's")
            self.assertNotIn(f'"$src/{f}"', cp.stdout, f"{f} must not come from the medium")
        for piece in ("mount -o ro", "/dev/sda1", "bcm2711-rpi-4-b.dtb",
                      "os_prefix=second/", "tryboot.txt", "second.new",
                      "start4.elf", "cmdline.txt", "sync"):
            self.assertIn(piece, s, f"stage script is missing {piece!r}")
        # A config.txt that already carries an os_prefix is stripped first:
        # two os_prefix lines would select nothing predictable.
        self.assertIn("/^os_prefix=/d", s)
        # The kernel's bitness is stated, read off its own magic: the SD's
        # modern firmware defaults to 64-bit and would jump into a 32-bit
        # zImage as an arm64 Image -- a silent hang before any kernel code.
        self.assertIn("016f2818", s)
        self.assertIn("644d5241", s)
        self.assertIn("arm_64bit=", s)
        cp = subprocess.run(["sh", "-n"], input=s, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_cmdline_gains_panic_exactly_once(self):
        """panic=10 is appended so a panicking kernel reboots into the rescue
        instead of hanging; a cmdline that already chose a panic keeps its own."""
        script = LOAD + 'printf %s "$1" | sed "$TRYBOOT_CMDLINE_SED"'
        cp = bash(f'bash -c \'{script}\' -- "root=PARTUUID=x rootwait console=tty1"')
        self.assertEqual(cp.stdout.strip(), "root=PARTUUID=x rootwait console=tty1 panic=10",
                         cp.stdout + cp.stderr)
        cp = bash(f'bash -c \'{script}\' -- "root=PARTUUID=x panic=5 rootwait"')
        self.assertEqual(cp.stdout.strip(), "root=PARTUUID=x panic=5 rootwait", cp.stdout + cp.stderr)


class TestReboot(unittest.TestCase):
    def test_only_an_arming_reboot_carries_the_flag(self):
        """b_arm marks the process; the reboot that follows carries
        "0 tryboot". Every other reboot (--back from bench mode) is plain, so
        the firmware reads the rescue's config.txt."""
        # The stubs speak on stderr: b_reboot sends the ssh's stdout to
        # /dev/null (the real command prints nothing useful).
        cp = bash(LOAD + '''
m_ssh() { echo "m_ssh: $*" >&2; }
r_sudo() { echo "r_sudo: $*" >&2; }
TRYBOOT_ARMED=1; b_reboot
TRYBOOT_ARMED=""; MODE="bench x-1"; b_reboot
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        armed, plain = cp.stderr.strip().splitlines()
        # Through systemd's reboot parameter file, not the positional
        # argument, which newer systemd drops silently.
        self.assertIn('/run/systemd/reboot-param', armed)
        self.assertIn('"0 tryboot"', armed)
        self.assertIn("systemctl reboot", armed)
        self.assertTrue(plain.startswith("r_sudo:"), plain)
        self.assertNotIn("tryboot", plain)

    def test_evidence_reads_the_staging_not_a_record(self):
        cp = bash(LOAD + '''
m_ssh() { echo yes; }
b_evidence
m_ssh() { return 1; }
b_evidence
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("tryboot_staged=yes", cp.stdout)
        self.assertIn("tryboot_staged=unreadable", cp.stdout)
        self.assertIn("bench root on /dev/sda", cp.stdout)

    def test_reprovision_puts_the_sd_first(self):
        """the SD is the boot authority for both roles here, so the recipe
        orders it first and never asks the firmware to boot the bench medium."""
        cp = bash(LOAD + 'b_reprovision')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk pi boot-order rpi4 sd-first", cp.stdout)
        self.assertIn("--disk <reader>:/dev/mmcblk0 --rescue", cp.stdout)
        self.assertIn("--disk rpi4:/dev/sda", cp.stdout)


if __name__ == "__main__":
    unittest.main()
