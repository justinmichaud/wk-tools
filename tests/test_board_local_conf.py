"""What the *build* is told about a board, and by whom.

`image/boards/<board>/local.conf.append` is the build-time sibling of that
board's `config.txt.append`: what the board's own silicon needs of every image
built for it, keyed on the board so a new profile for a board already known is
right without being told.

Today that is one line. The Pi 5 exists in two steppings; meta-raspberrypi at
the rev these branches pin lists only the C0 device tree, whose pinctrl nodes
carry the stepping-agnostic compatible that the same kernel's driver maps to
C0 pin data. On the D0 board that is a fatal SError and a panic in
bcm2712_pull_config_set before rootfs (rpi5, 2026-09-04).

Run: python3 -m unittest tests.test_board_local_conf -v
"""
import re
import unittest
from pathlib import Path

from tests.support import REPO

BOARDS = REPO / "image" / "boards"
BUILD = REPO / "image" / "yocto-build.sh"
DRIVER = REPO / "image" / "yocto.sh"


class TestTheBoardHalfIsWired(unittest.TestCase):
    def test_the_builder_takes_a_board(self):
        self.assertIn("--board)", BUILD.read_text(),
                      "yocto-build.sh has no --board, so no board file can be found")

    def test_the_driver_passes_the_board_it_already_knows(self):
        """IMG_MACHINE is the board name every profile already carries."""
        self.assertIn('--board "$IMG_MACHINE"', DRIVER.read_text())

    def test_the_board_file_is_appended_last(self):
        """bitbake takes the last assignment, so a board fact must land after
        the knobs above it -- and inside the block that writes local.conf."""
        body = BUILD.read_text()
        fn = body[body.index("configure_local_conf()"):]
        fn = fn[:fn.index('\n    } >> "$CONF"')]
        self.assertIn("image/boards/", fn, "the board file is not read here")
        self.assertLess(fn.index('RM_WORK_EXCLUDE'), fn.index("image/boards/"),
                        "a board fact is appended before knobs that could override it")

    def test_a_board_with_nothing_to_say_appends_nothing(self):
        """Absent is the normal case: rpi3 and rpi4 need no build-time fact."""
        for board in ("rpi3", "rpi4"):
            with self.subTest(board=board):
                self.assertFalse((BOARDS / board / "local.conf.append").exists())

    def test_the_read_is_guarded_on_both_the_name_and_the_file(self):
        """An empty BOARD must not read image/boards//local.conf.append."""
        body = BUILD.read_text()
        self.assertIn('[ -n "${BOARD:-}" ] && [ -f "$_board_conf" ]', body)


class TestTheRpi5NeedsTheD0Overlay(unittest.TestCase):
    """The firmware loads one base tree for both steppings and adapts it on
    D0 silicon with overlays/bcm2712d0.dtbo. meta-raspberrypi installs a
    hand-curated 52 of the 367 overlays the kernel compiles, and that one is
    not among them."""

    APPEND = BOARDS / "rpi5" / "local.conf.append"

    def test_the_file_exists(self):
        self.assertTrue(self.APPEND.exists())

    def test_it_asks_for_the_d0_overlay(self):
        active = [l.strip() for l in self.APPEND.read_text().splitlines()
                  if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(
            ['RPI_KERNEL_DEVICETREE_OVERLAYS:append = " overlays/bcm2712d0.dtbo"'],
            active, "the rpi5 board append says something other than the D0 overlay")

    def test_it_appends_rather_than_replaces(self):
        """The other 52 overlays are the branch's own choice; this adds one."""
        text = self.APPEND.read_text()
        self.assertIn("RPI_KERNEL_DEVICETREE_OVERLAYS:append", text)
        self.assertNotIn("RPI_KERNEL_DEVICETREE_OVERLAYS = ", text)

    def test_it_does_not_ship_a_per_stepping_base_tree(self):
        """Measured: a card carrying bcm2712d0-rpi-5-b.dtb panics identically,
        so the firmware does not ask for it by name and shipping it is weight
        nothing loads."""
        self.assertNotIn("bcm2712d0-rpi-5-b.dtb",
                         [l.strip() for l in self.APPEND.read_text().splitlines()
                          if l.strip() and not l.strip().startswith("#")])

    def test_the_machine_conf_names_the_tree_the_firmware_loads(self):
        """NODE_DTB is what boot-check verifies resolves on the card, so a
        name the firmware never requests passes a card that cannot boot."""
        conf = (REPO / "boot" / "machines" / "rpi5.conf").read_text()
        m = re.search(r"^NODE_DTB=(\S+)", conf, re.M)
        self.assertIsNotNone(m)
        self.assertEqual("bcm2712-rpi-5-b.dtb", m.group(1))


if __name__ == "__main__":
    unittest.main()
