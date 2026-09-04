"""The pi-tryboot boot driver: a Pi 4 whose bench medium the bootloader will
not boot. The firmware loads the bench kernel from the SD via a tryboot
one-shot (staged at arm time from the medium's own boot partition) and the
kernel mounts the bench root on the medium by PARTUUID (boot/pi-tryboot.sh).

Everything here runs against the sourced driver with its remote halves
stubbed -- no board. The end-to-end proof is a real `wk boot rpi4`.
"""
import os
import shutil
import subprocess
import tempfile
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
        cp = bash(LOAD + 'echo "$NODE_DRIVER $NODE_DEVICE $NODE_ROOT $NODE_DTB"')
        self.assertEqual(cp.stdout.strip(),
                         "pi-tryboot /dev/sda /dev/mmcblk0p2 bcm2711-rpi-4-b.dtb",
                         cp.stdout + cp.stderr)

    def test_an_arming_reboot_refuses_where_the_flag_cannot_be_passed(self):
        """The staging is files, which any system can write; the *flag* rides
        the reboot syscall and systemd is what passes it here. On a userspace
        without systemd -- a BusyBox bench image -- the old code staged the
        arming and then quietly did not reboot, which reads as "the board came
        up as the wrong system" and cost every B leg of an rpi4 A/B
        (2026-09-02). It refuses and names the remedy instead."""
        cp = bash(LOAD + """
r_sudo() { echo "r_sudo MUST NOT be reached" >&2; }
r_ssh() { return 1; }   # no systemd on the system that answered
TRYBOOT_ARMED=1; b_reboot
""")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no systemd", cp.stderr)
        self.assertIn("--back", cp.stderr, "the refusal does not name the remedy")
        self.assertNotIn("MUST NOT", cp.stderr, "it rebooted anyway")

    def test_this_board_is_armed_from_its_rescue(self):
        """...which is why B_ARM_FROM_BENCH is no here: the rescue is the one
        system on this board that always carries systemd."""
        cp = bash(LOAD + 'echo "${B_ARM_FROM_BENCH:-unset}"')
        self.assertEqual(cp.stdout.strip(), "no", cp.stdout + cp.stderr)

    def test_the_boot_that_spends_the_staging_removes_it(self):
        """This board does not consume the tryboot flag: measured 2026-09-01, a
        plain reboot and a cold power cycle both read tryboot.txt again and came
        back as the bench system, /proc/cmdline carrying second/cmdline.txt own
        panic=10 while the SD config.txt held no os_prefix. So the boot that
        spends the staging is what removes it, or the board can never leave the
        bench system -- and b_disarm cannot help, being addressed to a rescue
        this same staging stops it from booting."""
        cp = bash(LOAD + "b_self_disarm_sh")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout
        self.assertIn("/dev/mmcblk0p1", out, "the SD boot partition, derived from NODE_ROOT")
        self.assertIn("tryboot.txt", out)
        self.assertIn("second", out)
        self.assertNotIn("'", out, "a single quote breaks the systemd ExecStart it is spliced into")
        self.assertNotIn("%", out, "systemd expands % before it parses quotes")
        self.assertEqual(
            subprocess.run(["sh", "-n"], input=out, capture_output=True, text=True).returncode,
            0, "the staged script is not POSIX sh")

    def test_the_disarm_runs_over_the_channel_that_answered(self):
        """While the staging is in force the board answers as its *bench*
        system, so a disarm addressed only to the rescue never runs. r_ssh is
        the one implementation of whichever channel answered."""
        # b_disarm silences its own channel, so the stubs record to files.
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        stubs = "\n".join([
            'r_ssh() { printf "%s" "$*" > ' + str(d / "r_ssh") + '; }',
            'm_ssh() { : > ' + str(d / "m_ssh") + '; return 1; }',
            "b_disarm",
        ])
        cp = bash(LOAD + stubs + "\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((d / "r_ssh").is_file(), "b_disarm did not go through r_ssh")
        self.assertFalse((d / "m_ssh").exists(), "b_disarm still addresses the rescue directly")
        sent = (d / "r_ssh").read_text()
        self.assertIn("tryboot.txt", sent)
        self.assertIn("/dev/mmcblk0p1", sent,
                      "the SD boot partition is named, for a mount from the bench side")
        self.assertEqual(
            subprocess.run(["sh", "-n"], input=sent, capture_output=True, text=True).returncode,
            0, "the disarm script is not POSIX sh")

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
        cp = bash(LOAD + 'tryboot_stage_sh "$(disk_part "$NODE_DEVICE" 1)" "$NODE_DTB"')
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
        # /dev/null (the real command prints nothing useful). Both reboots go
        # through boot_priv now, and this board is a bench-device, so both
        # arrive as r_ssh -- the guard call that asks whether the answering
        # system carries systemd is the one that stays quiet.
        cp = bash(LOAD + '''
m_ssh() { echo "m_ssh: $*" >&2; }
r_ssh() { case "$*" in *setsid*) echo "r_ssh: $*" >&2 ;; *) return 0 ;; esac; }
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
        # ...and the plain one carries none of it.
        self.assertNotIn("reboot-param", plain)
        self.assertNotIn("tryboot", plain)
        self.assertIn("reboot", plain)
        self.assertNotIn("tryboot", plain)

    def test_evidence_reads_the_staging_not_a_record(self):
        """...and over r_ssh, the channel the board answered on: a board whose
        medium the firmware prefers answers as its bench system, which is
        exactly when `wk boot --status` is asked what is staged."""
        cp = bash(LOAD + """
r_ssh() { echo yes; }
b_evidence
r_ssh() { return 1; }
b_evidence
""")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("tryboot_staged=yes", cp.stdout)
        self.assertIn("tryboot_staged=unreadable", cp.stdout)
        self.assertIn("bench root on /dev/sda", cp.stdout)

    def test_the_evidence_says_which_config_the_running_boot_came_from(self):
        """`--status` answering "bench mode, system X" is true and says nothing
        about *how* the board got there. This firmware does not consume the
        tryboot flag, so a plain reboot reading tryboot.txt again looks
        identical to an arming that was asked for -- the difference is the
        running root= against the two cmdlines on the SD, which is one read."""
        cp = bash(LOAD + """
r_ssh() { echo staging; }
b_systems() { :; }
b_evidence
r_ssh() { echo sd-config; }
b_evidence
r_ssh() { echo unknown; }
b_evidence
r_ssh() { return 1; }
b_evidence
""")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("boot_source=the tryboot staging now on the SD", cp.stdout)
        self.assertIn("boot_source=the SD config.txt", cp.stdout)
        # "Neither" is the diagnosis, not a probe fault: the board is running a
        # staging that has since been replaced. Reporting it as unreadable is
        # what hid an arming that staged correctly and never rebooted.
        self.assertIn("came from an earlier staging", cp.stdout)
        self.assertIn("did not reboot", cp.stdout)
        self.assertIn("boot_source=unreadable (the board did not answer", cp.stdout)

    def test_the_reporting_path_mounts_read_only(self):
        """b_evidence answers a question, so the mounts it makes are read-only:
        mounting a FAT read-write and unmounting it rewrites the dirty flag on a
        card somebody only asked about (the same rule as second-state)."""
        cp = bash(LOAD + """
r_ssh() { printf '%s' "$*" >> "$OUT"; echo unknown; }
b_systems() { :; }
OUT=$(mktemp); export OUT
b_evidence >/dev/null
cat "$OUT"
""")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn('wk_sd_boot "-o ro"', cp.stdout,
                      "a reporting path mounts the card read-write")
        self.assertNotIn("wk_sd_boot)", cp.stdout.replace('wk_sd_boot "-o ro")', ""),
                         "one of the reads mounts read-write")

    def test_no_board_level_step_names_the_rescue_channel(self):
        """The class of bug that cost 2026-09-01: every one of this driver's
        steps acts on the *board* -- the SD's staging, the EEPROM, the reboot --
        and each one is needed precisely when the board is answering as its
        bench system rather than its rescue. `m_ssh` is the rescue's channel;
        anything here that names it is a step that cannot run when it matters."""
        text = (REPO / "boot" / "pi-tryboot.sh").read_text()
        offenders = [l.strip() for l in text.splitlines()
                     if "m_ssh" in l and not l.lstrip().startswith("#")]
        self.assertEqual(offenders, [], "these reach only the rescue:\n" + "\n".join(offenders))

    def test_arm_stages_from_the_selected_system(self):
        """a medium holding two systems arms the one cmd/boot selected
        (ARM_SYS_PART, machine_select_system); an arm with no selection is
        refused loudly rather than guessing partition 1."""
        cp = bash(LOAD + '''
r_ssh() { echo "r_ssh: $*" >&2; }
ARM_SYS_PART=/dev/sda3 b_arm
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("mount -o ro '/dev/sda3'", cp.stderr)
        cp = bash(LOAD + 'r_ssh() { :; }; b_arm')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("machine_select_system", cp.stderr)

    def test_evidence_lists_the_systems_the_medium_holds(self):
        """which ids an arming can name is evidence, printed one per line."""
        cp = bash(LOAD + '''
m_ssh() { echo yes; }
b_systems() { printf "%s\\n%s\\n" "/dev/sda1 alpha-1" "/dev/sda3 beta-2"; }
b_evidence
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("system=alpha-1 (on /dev/sda1)", cp.stdout)
        self.assertIn("system=beta-2 (on /dev/sda3)", cp.stdout)

    def test_reprovision_puts_the_sd_first(self):
        """the SD is the boot authority for both roles here, so the recipe
        orders it first and never asks the firmware to boot the bench medium."""
        cp = bash(LOAD + 'b_reprovision')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk pi boot-order rpi4 sd-first", cp.stdout)
        self.assertIn("--disk <reader>:/dev/mmcblk0 --rescue", cp.stdout)
        self.assertIn("--disk rpi4:/dev/sda", cp.stdout)


class TestStagingRuns(unittest.TestCase):
    """The staging script executed for real against a fixture boot partition.

    Only `mount`, `umount` and `sync` are shimmed (they need root, and this
    does not) and `_tryboot_bootfs_sh` points at a directory standing in for
    the SD's FAT; everything else is the script the rescue runs.
    """

    ZIMAGE_MAGIC = (36, bytes((0x18, 0x28, 0x6F, 0x01)))
    ARM64_MAGIC = (56, b"ARM\x64")

    def write_kernel(self, path, magic):
        off, word = magic
        blob = bytearray(1024)
        blob[off:off + 4] = word
        path.write_bytes(bytes(blob))

    def stage(self, config, kernels):
        """Run the staging over a fixture; return (completed process, sd dir)."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        medium, sd, shim = tmp / "medium", tmp / "sd", tmp / "bin"
        for d in (medium, sd, shim):
            d.mkdir()
        (medium / "config.txt").write_text(config)
        (medium / "cmdline.txt").write_text("root=PARTUUID=aa-02 rootwait\n")
        (medium / "bcm2711-rpi-4-b.dtb").write_bytes(b"dtb")
        (medium / "overlays").mkdir()
        (medium / "overlays" / "vc4-kms-v3d.dtbo").write_bytes(b"ovl")
        for name, magic in kernels.items():
            self.write_kernel(medium / name, magic)
        for f in ("start4.elf", "fixup4.dat"):
            (sd / f).write_bytes(b"fw")
        # `mount` hands the script the fixture; `umount` empties it again so
        # the script's own rmdir succeeds.
        (shim / "mount").write_text(
            '#!/bin/sh\nfor a in "$@"; do t=$a; done\ncp -a "%s"/. "$t"\n' % medium)
        (shim / "umount").write_text(
            '#!/bin/sh\nfor a in "$@"; do t=$a; done\nfind "$t" -mindepth 1 -delete\n')
        (shim / "sync").write_text("#!/bin/sh\nexit 0\n")
        for f in shim.iterdir():
            f.chmod(0o755)
        # The SD's boot filesystem is named from the conf on a real board; here
        # the one emitter that names it is replaced by one naming the fixture.
        cp = bash(LOAD + (
            '_tryboot_sd_sh() { printf %%s "wk_sd_boot() { echo %s; }; wk_sd_drop() { :; }; "; }\n'
            "PATH=%s:$PATH\n"
            'tryboot_stage_sh /dev/sda1 "$NODE_DTB" | sh\n') % (sd, shim))
        return cp, sd

    def test_a_kernel_line_names_the_kernel_and_states_32_bit(self):
        """the fork's images say `kernel=zImage`: that file is staged, and
        arm_64bit=0 is written from its own magic (the SD's firmware would
        otherwise jump into a zImage as if it were an arm64 Image)."""
        cp, sd = self.stage("kernel=zImage\ndtparam=audio=on\n",
                            {"zImage": self.ZIMAGE_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for f in ("zImage", "bcm2711-rpi-4-b.dtb", "start4.elf", "fixup4.dat"):
            self.assertTrue((sd / "second" / f).exists(), f"{f} was not staged")
        self.assertTrue((sd / "second" / "overlays" / "vc4-kms-v3d.dtbo").exists())
        txt = (sd / "tryboot.txt").read_text()
        self.assertIn("os_prefix=second/", txt)
        self.assertIn("arm_64bit=0", txt)
        self.assertIn("panic=10", (sd / "second" / "cmdline.txt").read_text())

    def test_the_prefix_leads_the_staged_config(self):
        """os_prefix comes before any line the image carries: the firmware
        resolves each filename as it reads the directive asking for it, so a
        prefix set after `dtoverlay=` never reaches that overlay's .dtbo."""
        cp, sd = self.stage("dtoverlay=vc4-kms-v3d\n[pi4]\ndtparam=audio=on\n",
                            {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = [l for l in (sd / "tryboot.txt").read_text().splitlines() if l.strip()]
        self.assertEqual(lines[0], "os_prefix=second/")
        self.assertLess(lines.index("arm_64bit=1"), lines.index("dtoverlay=vc4-kms-v3d"))
        # ...and it is not left inside the image's own conditional section.
        self.assertLess(lines.index("os_prefix=second/"),
                        next(i for i, l in enumerate(lines) if l.startswith("[")))

    def test_a_kernel_of_neither_bitness_states_none(self):
        """a kernel whose magic says nothing leaves arm_64bit to the firmware
        rather than guessing it; the staging still completes."""
        cp, sd = self.stage("kernel=zImage\n", {"zImage": (0, b"\x00\x00\x00\x00")})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("arm_64bit", (sd / "tryboot.txt").read_text())
        self.assertTrue((sd / "second" / "zImage").exists())

    def test_no_kernel_line_resolves_the_one_default_name_there_is(self):
        """the yocto images ship a bare config.txt beside one kernel8.img --
        the firmware resolves the name from its defaults, and so does this."""
        cp, sd = self.stage('#kernel=""\ndtoverlay=vc4-kms-v3d\n',
                            {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((sd / "second" / "kernel8.img").exists())
        self.assertIn("arm_64bit=1", (sd / "tryboot.txt").read_text())

    def test_two_default_names_and_no_kernel_line_refuses(self):
        """which of several the firmware picks follows from arm_64bit and the
        board; staging another would boot a kernel nobody asked for."""
        cp, sd = self.stage("dtparam=audio=on\n",
                            {"kernel8.img": self.ARM64_MAGIC,
                             "kernel7l.img": self.ZIMAGE_MAGIC})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("kernel8.img", cp.stderr)
        self.assertIn("kernel7l.img", cp.stderr)
        self.assertIn("config.txt.append", cp.stderr)
        self.assertFalse((sd / "second").exists(), "nothing is staged on a refusal")

    def test_no_kernel_at_all_refuses(self):
        cp, sd = self.stage("dtparam=audio=on\n", {})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("holds none of", cp.stderr)
        self.assertFalse((sd / "second").exists())


if __name__ == "__main__":
    unittest.main()
