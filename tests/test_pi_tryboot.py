"""The pi-tryboot boot driver (lib/wk/boot/pi.py, boot/onboard/tryboot.sh): a Pi 4 whose bench medium the
bootloader will not boot. The firmware loads the bench kernel from the SD via a tryboot one-shot, staged at arm
time from the medium's own boot partition, and the kernel mounts the bench root on the medium by PARTUUID.

Run: python3 tests/run.py --unit -k test_pi_tryboot
"""
import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.pi import PiTryboot  # noqa: E402
from wk.machine import Result  # noqa: E402

TRYBOOT = REPO / "boot" / "onboard" / "tryboot.sh"
CONF = {"NODE_NAME": "rpi4", "NODE_DRIVER": "pi-tryboot", "NODE_DEVICE": "/dev/sda", "NODE_ROOT": "/dev/mmcblk0p2",
        "NODE_DTB": "bcm2711-rpi-4-b.dtb", "NODE_ROLE": "bench-device", "NODE_PROFILE": "p"}


class Channel:
    def __init__(self, answers=None):
        self.answers, self.calls, self.channel = answers or {}, [], "bench"

    def call(self, fn, *args, input=None, mutates=False):
        ob = next((a for a in args if hasattr(a, "params")), None)
        self.calls.append((fn, ob.name if ob else args, dict(ob.params) if ob else {}))
        key = ob.params.get("WK_DO", ob.name) if ob else fn
        got = self.answers.get(key, Result(0))
        return got(ob) if callable(got) else got


def driver(answers=None):
    ch = Channel(answers)
    return PiTryboot(REPO, dict(CONF), ch), ch


def refused(fn, *args, **kw):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            fn(*args, **kw)
        except act.Refused:
            return err.getvalue()
    raise AssertionError("did not refuse")


class TestArrangement(unittest.TestCase):
    def test_rpi4_names_the_driver_and_keeps_its_media(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/boot/machines.sh"; machine_load rpi4; '
                  'echo "$NODE_DRIVER $NODE_DEVICE $NODE_ROOT $NODE_DTB"', env={"WK_MACHINES_DIR": str(REPO / "machines")})
        self.assertEqual(cp.stdout.strip(), "pi-tryboot /dev/sda /dev/mmcblk0p2 bcm2711-rpi-4-b.dtb", cp.stderr)

    def test_this_board_is_armed_from_its_rescue(self):
        """the rescue is the one system on this board that always carries systemd, which passes the flag."""
        self.assertFalse(PiTryboot.arm_from_bench)

    def test_an_arming_reboot_refuses_where_the_flag_cannot_be_passed(self):
        d, ch = driver({"has-systemd.sh": Result(1)})
        err = refused(d.reboot, armed=True)
        self.assertIn("no systemd", err)
        self.assertIn("--back", err)
        self.assertNotIn("boot_priv", [c[0] for c in ch.calls], "it rebooted anyway")

    def test_only_an_arming_reboot_carries_the_flag(self):
        d, ch = driver()
        d.reboot(armed=True)
        d.reboot()
        self.assertEqual([c[1] for c in ch.calls if c[0] == "boot_priv"], [("reboot-tryboot",), ("reboot",)])
        text = (REPO / "boot" / "machines.sh").read_text()
        body = text[text.index("boot_priv() {"):]
        self.assertIn('/run/systemd/reboot-param && systemctl reboot', body[:body.index("\n}\n")])

    def test_identity_still_reads_off_the_bench_medium(self):
        self.assertEqual(driver()[0].boot_part(), "/dev/sda1")

    def test_the_boot_that_spends_the_staging_removes_it(self):
        """this board does not consume the tryboot flag: a plain reboot reads tryboot.txt again."""
        out = driver()[0].self_disarm_sh()
        self.assertTrue(out.startswith("WK_SD=/dev/mmcblk0p1; "), out)
        for piece in ("tryboot.txt", "second", "rescue boots next"):
            self.assertIn(piece, out)

    def test_every_step_goes_over_the_channel_that_answered(self):
        """while the staging is in force the board answers as its bench system, so nothing addresses the rescue alone."""
        d, ch = driver({"staged-root": Result(0, "root=PARTUUID=aa-04\n"),
                        "medium-read.sh": Result(0, "root=PARTUUID=aa-04 rootwait\n")})
        d.arm("/dev/sda3")
        d.disarm()
        d.evidence()
        self.assertEqual({c[0] for c in ch.calls}, {"r_ssh", "r_sudo"})


class TestArming(unittest.TestCase):
    def test_arm_stages_from_the_selected_system_and_reads_the_staging_back(self):
        d, ch = driver({"staged-root": Result(0, "root=PARTUUID=aa-04\n"),
                        "medium-read.sh": Result(0, "root=PARTUUID=aa-04 rootwait\n")})
        d.arm("/dev/sda3")
        stage = [c for c in ch.calls if c[2].get("WK_DO") == "stage"][0]
        self.assertEqual(stage[2], {"WK_DO": "stage", "WK_SD": "/dev/mmcblk0p1", "WK_SRC": "/dev/sda3", "WK_DTB": "bcm2711-rpi-4-b.dtb"})

    def test_a_staging_of_another_system_is_refused(self):
        d, _ = driver({"staged-root": Result(0, "root=PARTUUID=aa-02\n"),
                       "medium-read.sh": Result(0, "root=PARTUUID=aa-04 rootwait\n")})
        self.assertIn("not the selected system's root=PARTUUID=aa-04", refused(d.arm, "/dev/sda3"))

    def test_an_arm_with_no_selection_is_refused(self):
        self.assertIn("machine_select_system", refused(driver()[0].arm, ""))

    def test_a_failed_staging_names_what_has_to_answer(self):
        self.assertIn("could not stage the tryboot files on rpi4", refused(driver({"stage": Result(9)})[0].arm, "/dev/sda1"))


class TestEvidence(unittest.TestCase):
    def evidence(self, source, staged="yes\n"):
        d, _ = driver({"source": Result(0, source), "staged": Result(0, staged), "card_priv": Result(0, "")})
        d.systems = lambda: [("/dev/sda1", "alpha-1"), ("/dev/sda3", "beta-2")]
        return d.evidence()

    def test_the_evidence_says_which_config_the_running_boot_came_from(self):
        self.assertIn("boot_source=the tryboot staging now on the SD", self.evidence("staging\n"))
        self.assertIn("boot_source=the SD config.txt", self.evidence("sd-config\n"))
        out = self.evidence("unknown\n")
        self.assertIn("came from an earlier staging", out)
        self.assertIn("did not reboot", out)
        self.assertIn("boot_source=unreadable (the board did not answer", self.evidence("", ""))

    def test_the_evidence_reads_the_staging_and_lists_the_systems(self):
        out = self.evidence("staging\n")
        for want in ("tryboot_staged=yes", "bench root on /dev/sda", "system=alpha-1 (on /dev/sda1)", "system=beta-2 (on /dev/sda3)"):
            self.assertIn(want, out)
        self.assertIn("tryboot_staged=unreadable", self.evidence("staging\n", ""))

    def test_the_reporting_path_mounts_read_only(self):
        """mounting a FAT read-write and unmounting it rewrites the dirty flag on a card somebody only asked about."""
        text = TRYBOOT.read_text()
        for verb in ("staged)", "staged-root)", "source)"):
            branch = text[text.index("    " + verb):]
            self.assertIn("wk_sd_mount -o ro", branch[:branch.index(";;")], verb)

    def test_reprovision_puts_the_sd_first(self):
        out = driver()[0].reprovision()
        self.assertIn("wk pi boot-order rpi4 sd-first", out)
        self.assertIn("--disk <reader>:/dev/mmcblk0 --rescue", out)
        self.assertIn("--disk rpi4:/dev/sda", out)


class TestStagingRuns(unittest.TestCase):
    """boot/onboard/tryboot.sh executed for real against a fixture: `mount`, `umount`, `sync` and the /proc/mounts
    read are shimmed (they need root and a board), and the SD's boot partition answers as already mounted."""

    ZIMAGE_MAGIC = (36, bytes((0x18, 0x28, 0x6F, 0x01)))
    ARM64_MAGIC = (56, b"ARM\x64")

    def write_kernel(self, path, magic):
        off, word = magic
        blob = bytearray(1024)
        blob[off:off + 4] = word
        path.write_bytes(bytes(blob))

    def stage(self, config, kernels):
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
        real_grep = shutil.which("grep")
        (shim / "grep").write_text('#!/bin/sh\nfor a; do l=$a; done\n[ "$l" = /proc/mounts ] && { echo "/dev/fake1 %s vfat rw 0 0"; exit 0; }\n'
                                   'exec %s "$@"\n' % (sd, real_grep))
        (shim / "mount").write_text('#!/bin/sh\nfor a in "$@"; do t=$a; done\ncp -a "%s"/. "$t"\n' % medium)
        (shim / "umount").write_text('#!/bin/sh\nfor a in "$@"; do t=$a; done\nfind "$t" -mindepth 1 -delete\n')
        (shim / "sync").write_text("#!/bin/sh\nexit 0\n")
        for f in shim.iterdir():
            f.chmod(0o755)
        d = PiTryboot(REPO, dict(CONF, NODE_ROOT="/dev/fake2"), None)
        text = d.ob("tryboot.sh", WK_DO="stage", WK_SD="/dev/fake1", WK_SRC="/dev/sda1", WK_DTB="bcm2711-rpi-4-b.dtb").text()
        cp = subprocess.run(["sh", "-c", text], capture_output=True, text=True,
                            env={"PATH": "%s:/usr/bin:/bin" % shim})
        return cp, sd

    def test_a_kernel_line_names_the_kernel_and_states_32_bit(self):
        cp, sd = self.stage("kernel=zImage\ndtparam=audio=on\n", {"zImage": self.ZIMAGE_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for f in ("zImage", "bcm2711-rpi-4-b.dtb", "start4.elf", "fixup4.dat"):
            self.assertTrue((sd / "second" / f).exists(), f"{f} was not staged")
        self.assertTrue((sd / "second" / "overlays" / "vc4-kms-v3d.dtbo").exists())
        txt = (sd / "tryboot.txt").read_text()
        self.assertIn("os_prefix=second/", txt)
        self.assertIn("arm_64bit=0", txt)
        self.assertIn("panic=10", (sd / "second" / "cmdline.txt").read_text())

    def test_a_cmdline_that_chose_a_panic_keeps_its_own(self):
        cp, sd = self.stage("kernel=zImage\n", {"zImage": self.ZIMAGE_MAGIC})
        self.assertEqual((sd / "second" / "cmdline.txt").read_text().split(), ["root=PARTUUID=aa-02", "rootwait", "panic=10"])
        self.assertIn("/ panic=[0-9]/!", TRYBOOT.read_text())

    def test_the_prefix_leads_the_staged_config(self):
        """the firmware resolves each filename as it reads the directive asking for it."""
        cp, sd = self.stage("dtoverlay=vc4-kms-v3d\n[pi4]\ndtparam=audio=on\n", {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = [l for l in (sd / "tryboot.txt").read_text().splitlines() if l.strip()]
        self.assertEqual(lines[0], "os_prefix=second/")
        self.assertLess(lines.index("arm_64bit=1"), lines.index("dtoverlay=vc4-kms-v3d"))

    def test_a_kernel_of_neither_bitness_states_none(self):
        cp, sd = self.stage("kernel=zImage\n", {"zImage": (0, b"\x00\x00\x00\x00")})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("arm_64bit", (sd / "tryboot.txt").read_text())

    def test_no_kernel_line_resolves_the_one_default_name_there_is(self):
        cp, sd = self.stage('#kernel=""\ndtoverlay=vc4-kms-v3d\n', {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((sd / "second" / "kernel8.img").exists())

    def test_two_default_names_and_no_kernel_line_refuses(self):
        cp, sd = self.stage("dtparam=audio=on\n", {"kernel8.img": self.ARM64_MAGIC, "kernel7l.img": self.ZIMAGE_MAGIC})
        self.assertNotEqual(cp.returncode, 0)
        for want in ("kernel8.img", "kernel7l.img", "config.txt.append"):
            self.assertIn(want, cp.stderr)
        self.assertFalse((sd / "second").exists(), "nothing is staged on a refusal")

    def test_no_kernel_at_all_refuses(self):
        cp, sd = self.stage("dtparam=audio=on\n", {})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("holds none of", cp.stderr)
        self.assertFalse((sd / "second").exists())


if __name__ == "__main__":
    unittest.main()
