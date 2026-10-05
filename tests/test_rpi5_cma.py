"""rpi5-setup.sh's CMA rewrite: the size rides the existing [all] vc4-kms-v3d line (dtoverlay accumulates), only
as a size the overlay defines (another is silently dropped), and other boards' own cma- lines survive."""
import re
import subprocess
import unittest

from tests.support import REPO

SCRIPT = REPO / "host" / "linux" / "rpi5" / "rpi5-setup.sh"

# The sizes vc4-kms-v3d-pi5.dtbo defines an override for.
OVERLAY_SIZES = {64, 96, 128, 192, 256, 320, 384, 448, 512}

# The stock Ubuntu/RPi config.txt, reduced to the lines the rewrite must reason
# about: two [all] sections, and the other boards' own cma- lines.
STOCK = """\
[all]
os_check=0
os_prefix=current/

[tryboot]
os_prefix=new/

[all]
arm_64bit=1
dtparam=audio=on
dtoverlay=vc4-kms-v3d
disable_fw_kms_setup=1
dtoverlay=dwc2

[pi4]
max_framebuffers=2

[pi3+]
dtoverlay=vc4-kms-v3d,cma-128

[pi02]
dtoverlay=vc4-kms-v3d,cma-128

[all]

[pi5]
dtparam=pciex1_gen=3
arm_freq=2800
[all]
"""


def awk_program():
    src = SCRIPT.read_text()
    m = re.search(r"""awk -v want="\$CMA_MB" '(.*?)' "\$CFG" > "\$_cma_tmp\"""",
                  src, re.S)
    assert m, "the CMA rewrite is no longer an awk program in " + str(SCRIPT)
    return m.group(1)


def rewrite(text, want=512):
    cp = subprocess.run(["awk", "-v", f"want={want}", awk_program()],
                        input=text, capture_output=True, text=True, check=True)
    return cp.stdout


def overlay_lines(text):
    return [l for l in text.splitlines() if l.startswith("dtoverlay=vc4-kms-v3d")]


class SizeVocabulary(unittest.TestCase):
    def test_default_is_a_size_the_overlay_defines(self):
        m = re.search(r'^CMA_MB="\$\{CMA_MB:-(\d+)\}"', SCRIPT.read_text(), re.M)
        self.assertIsNotNone(m, "rpi5-setup.sh no longer defaults CMA_MB")
        self.assertIn(int(m.group(1)), OVERLAY_SIZES)


class Rewrite(unittest.TestCase):
    def test_the_all_line_gains_the_size(self):
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-512", rewrite(STOCK).splitlines())

    def test_exactly_one_line_changes_and_other_boards_keep_their_own_size(self):
        before, after = STOCK.splitlines(), rewrite(STOCK).splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual([i for i, (a, b) in enumerate(zip(before, after)) if a != b],
                         [before.index("dtoverlay=vc4-kms-v3d")])

    def test_an_existing_size_is_replaced_not_appended(self):
        got = rewrite(STOCK.replace("dtoverlay=vc4-kms-v3d\n",
                                    "dtoverlay=vc4-kms-v3d,cma-256\n", 1))
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-512", overlay_lines(got))
        self.assertNotIn("cma-256", got)

    def test_other_overlay_parameters_survive(self):
        got = rewrite(STOCK.replace("dtoverlay=vc4-kms-v3d\n",
                                    "dtoverlay=vc4-kms-v3d,noaudio\n", 1))
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-512,noaudio", overlay_lines(got))

    def test_rerunning_changes_nothing(self):
        once = rewrite(STOCK)
        self.assertEqual(rewrite(once), once)


if __name__ == "__main__":
    unittest.main()
