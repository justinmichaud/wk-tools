"""What the firmware is told, and who gets to say it.

`wk sysimage write` appends to the card's own config.txt from two places: the
board's, then the profile's. The split is the point. `os_check=0` is a fact
about the Pi 5's firmware -- it rejects a kernel lacking Ubuntu's trailer, and
everything built here is "locally built" by that test -- so every image written
for that board needs it. Clock pinning is a choice a measurement makes, so it
belongs to the profile.

Held per profile, exactly one of six rpi5 profiles carried it. The other five
produced images that could not boot, identically and silently: armed, no
kernel, no userspace, no wk-diag.txt, and a board needing a power cycle because
the watchdog that hands it back is itself in the userspace never reached
(wpewebkit-2.46-yocto-rpi5-64, 2026-09-04).

Run: python3 -m unittest tests.test_board_config_append -v
"""
import re
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO

BOARDS = REPO / "image" / "boards"
CONFIGS = sorted((REPO / "image" / "configs").glob("*.conf"))


def resolved_append(profile):
    """The two files `config_append_text` concatenates, in its order."""
    cp = subprocess.run(["bash", "-c", f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/image/profiles.sh"
image_profile_load {profile} >/dev/null 2>&1
b="$WK_ROOT/image/boards/${{IMG_MACHINE:-}}/config.txt.append"
f="${{IMG_SPEC_DIR:-}}/config.txt.append"
if [ -f "$b" ]; then cat "$b"; fi
if [ -f "$f" ]; then cat "$f"; fi
'''], capture_output=True, text=True)
    return cp.stdout


def machine_of(conf):
    m = re.search(r"^IMG_MACHINE=(\S+)", conf.read_text(), re.M)
    return m.group(1) if m else None


class TestEveryRpi5ImageCanBoot(unittest.TestCase):
    RPI5 = [c for c in CONFIGS if machine_of(c) == "rpi5"]

    def test_there_are_rpi5_profiles_to_check(self):
        self.assertTrue(self.RPI5, "no rpi5 profiles found")

    def test_every_one_of_them_gets_os_check(self):
        """Not one of them, and not the ones somebody remembered."""
        for conf in self.RPI5:
            with self.subTest(profile=conf.stem):
                active = [l.strip() for l in resolved_append(conf.stem).splitlines()
                          if l.strip() and not l.strip().startswith("#")]
                self.assertIn("os_check=0", active,
                              f"{conf.stem} would produce an image the Pi 5 firmware rejects")

    def test_no_profile_restates_it(self):
        """It is a board fact now; a profile repeating it is how the drift
        started."""
        for spec in sorted((REPO / "image").glob("*/config.txt.append")):
            if spec.parent.parent.name == "boards":
                continue
            with self.subTest(spec=str(spec.relative_to(REPO))):
                self.assertNotIn("os_check", spec.read_text())


class TestTheSplitIsKept(unittest.TestCase):
    def test_the_board_is_appended_before_the_profile(self):
        """Later wins in config.txt, so a profile can override a board default
        -- not the other way round."""
        body = (REPO / "cmd" / "sysimage").read_text()
        fn = body[body.index("config_append_text()"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertLess(fn.index('image/boards/'), fn.index("IMG_SPEC_DIR"),
                        "the profile's append comes first, so a board fact could be lost")

    def test_a_measurement_choice_stays_with_the_profile(self):
        """rpi4's clock pinning is not a board fact: another profile on the
        same board may want to measure without it."""
        rpi4 = resolved_append("webkit-2.52-yocto-rpi4-64")
        self.assertIn("force_turbo=1", rpi4)
        self.assertNotIn("force_turbo", (BOARDS / "rpi5" / "config.txt.append").read_text())

    def test_a_board_with_nothing_to_say_appends_nothing(self):
        out = resolved_append("webkit-2.52-yocto-rpi3-32")
        self.assertEqual("", out.strip(), out)

    def test_boards_is_not_also_a_profile_name(self):
        """image/boards/ shares its namespace with image/<profile>/."""
        self.assertFalse((REPO / "image" / "configs" / "boards.conf").exists(),
                         "a profile named 'boards' would collide with the board directory")

    def test_every_board_directory_belongs_to_a_real_machine(self):
        for d in sorted(BOARDS.glob("*")):
            if not d.is_dir():
                continue
            with self.subTest(board=d.name):
                self.assertTrue((REPO / "boot" / "machines" / f"{d.name}.conf").exists(),
                                f"image/boards/{d.name} names no fleet machine")


if __name__ == "__main__":
    unittest.main()
