"""What the firmware is told, and who gets to say it."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.sysimage import write  # noqa: E402

BOARDS = REPO / "image" / "boards"
CONFIGS = sorted((REPO / "image" / "configs").glob("*.conf"))


def resolved_append(profile):
    return write.config_add(REPO, images.load(profile))


def resolved_cmdline(profile):
    return write.cmdline_add(REPO, images.load(profile))


def machine_of(conf):
    m = re.search(r"^IMG_MACHINE=(\S+)", conf.read_text(), re.M)
    return m.group(1) if m else None


class TestEveryRpi5ImageCanBoot(unittest.TestCase):
    RPI5 = [c for c in CONFIGS if machine_of(c) == "rpi5"]

    def test_every_one_of_them_gets_os_check(self):
        self.assertTrue(self.RPI5)
        for conf in self.RPI5:
            with self.subTest(profile=conf.stem):
                active = [l.strip() for l in resolved_append(conf.stem).splitlines()
                          if l.strip() and not l.strip().startswith("#")]
                self.assertIn("os_check=0", active,
                              f"{conf.stem} would produce an image the Pi 5 firmware rejects")


class TestTheSplitIsKept(unittest.TestCase):
    def test_the_board_is_appended_before_the_profile(self):
        with tempfile.TemporaryDirectory() as d:
            board, spec = Path(d, "image", "boards", "b"), Path(d, "spec")
            board.mkdir(parents=True)
            spec.mkdir()
            for where, text in ((board, "board"), (spec, "profile")):
                (where / "config.txt.append").write_text(text + "=1\n")
                (where / "cmdline.txt.append").write_text("# a comment\n" + text + "=1\n\n")
            p = {"IMG_MACHINE": "b", "IMG_SPEC_DIR": str(spec)}
            self.assertEqual(write.config_add(d, p), "board=1\nprofile=1\n")
            self.assertEqual(write.cmdline_add(d, p), "board=1 profile=1")

    def test_a_measurement_choice_stays_with_the_profile(self):
        rpi4 = resolved_append("webkit-2.52-yocto-rpi4-64")
        self.assertIn("force_turbo=1", rpi4)

    def test_the_rpi5_makes_a_stopped_boot_report_itself(self):
        out = resolved_cmdline("wpewebkit-2.46-yocto-rpi5-64")
        self.assertIn("panic=10", out)
        self.assertIn("rootwait=30", out)

    def test_a_board_with_nothing_to_say_appends_nothing(self):
        self.assertEqual("", resolved_cmdline("webkit-2.52-yocto-rpi4-64").strip())
        self.assertEqual("", resolved_append("webkit-2.52-yocto-rpi3-32").strip())

    def test_every_board_directory_belongs_to_a_real_machine(self):
        for d in sorted(BOARDS.glob("*")):
            if not d.is_dir():
                continue
            with self.subTest(board=d.name):
                self.assertTrue((REPO / "machines" / f"{d.name}.conf").exists(),
                                f"image/boards/{d.name} names no fleet machine")


if __name__ == "__main__":
    unittest.main()
