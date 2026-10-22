"""The unit every board image carries that takes disk swap away and leaves zram (boot/firstboot/wk-no-swap.service)."""
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

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


if __name__ == "__main__":
    unittest.main()
