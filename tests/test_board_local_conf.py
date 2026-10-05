"""What the *build* is told about a board, and by whom."""
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

BOARDS = REPO / "image" / "boards"
sys.path.insert(0, str(REPO / "lib"))
from wk.sysimage import yocto_ws  # noqa: E402

ENV = {"DL_DIR": "/cache/dl", "SSTATE_DIR": "/cache/sstate"}


def conf(board, append):
    a = yocto_ws.parse(["--target", "rpi5-64bits-mesa", "--rm-work", "1"] + (["--board", board] if board else []))
    return yocto_ws.local_conf(a, ENV, 8, append)


class TestTheBoardHalfIsWired(unittest.TestCase):
    def test_the_board_file_is_appended_last(self):
        text = conf("rpi5", 'X = "1"\n')
        self.assertLess(text.index("RM_WORK_EXCLUDE"), text.index("image/boards/rpi5"),
                        "a board fact is appended before knobs that could override it")
        self.assertTrue(text.rstrip("\n").endswith('X = "1"'))

    def test_no_board_file_appends_nothing(self):
        self.assertNotIn("image/boards/", conf("", None))
        self.assertNotIn("image/boards/", conf("rpi4", None))


class TestTheRpi3SwapsToZram(unittest.TestCase):

    UNIT = REPO / "boot" / "firstboot" / "wk-no-swap.service"

    def swapped_off(self, swaps):
        line = next(l for l in self.UNIT.read_text().splitlines() if l.startswith("ExecStart="))
        argv = shlex.split(line[len("ExecStart="):].replace("$$", "$"))
        with tempfile.TemporaryDirectory() as d:
            Path(d, "swaps").write_text("Filename Type Size Used Priority\n" + swaps)
            Path(d, "swapoff").write_text('#!/bin/sh\necho "$@" >> "%s/off"\n' % d)
            os.chmod(Path(d, "swapoff"), 0o755)
            subprocess.run(argv[:2] + [argv[2].replace("/proc/swaps", d + "/swaps")], check=True,
                           env={"PATH": d + ":/usr/bin:/bin"})
            off = Path(d, "off")
            return off.read_text().split() if off.exists() else []

    def test_disk_swap_goes_and_zram_stays(self):
        self.assertEqual(["/dev/mmcblk0p3", "/swapfile"],
                         self.swapped_off("/dev/zram0 partition 474000 0 100\n/dev/mmcblk0p3 partition 1 0 -2\n"
                                          "/swapfile file 1 0 -3\n"))

    def test_no_swap_runs_nothing(self):
        self.assertEqual([], self.swapped_off(""))


class TestTheRpi5Board(unittest.TestCase):
    def test_it_appends_the_d0_overlay_and_a_4k_page_kernel_and_nothing_else(self):
        active = [l.strip() for l in (BOARDS / "rpi5" / "local.conf.append").read_text().splitlines()
                  if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(sorted(active), ['KBUILD_DEFCONFIG:raspberrypi5 = "bcm2711_defconfig"',
                                          'RPI_KERNEL_DEVICETREE_OVERLAYS:append = " overlays/bcm2712d0.dtbo"'])


if __name__ == "__main__":
    unittest.main()
