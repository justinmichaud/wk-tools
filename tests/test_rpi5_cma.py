"""Which config.txt line the rpi5's CMA size lands on.

The Pi 5 DTB reserves 64M of CMA and vc4-kms-v3d-pi5's own default matches it,
which is less than one 3840x2160 scanout buffer needs twice over. Raising it is
a `cma-<N>` parameter on the KMS overlay, and three properties of config.txt
decide whether the parameter takes effect at all:

The overlay defines the sizes 64/96/128/192/256/320/384/448/512 and nothing
else, so an arbitrary number is dropped and the board boots on the DTB default
with no diagnostic.

`dtoverlay` accumulates, so a second `dtoverlay=vc4-kms-v3d` line in [pi5]
applies the whole overlay twice rather than amending the first. The size has to
ride the [all] line that is already there.

The stock config.txt carries `dtoverlay=vc4-kms-v3d,cma-128` under [pi3+] and
[pi02] for a 512MB board's boot-time OOM. Those are a different board's and
must survive untouched, which is what makes the rewrite section-aware.

Run: python3 -m unittest tests.test_rpi5_cma -v
"""
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
    """The rewrite as the script actually spells it."""
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

    def test_advertised_sizes_are_the_overlays(self):
        """The knob's comment must not offer a size the firmware would drop."""
        line = [l for l in SCRIPT.read_text().splitlines()
                if l.startswith("CMA_MB=")][0]
        offered = {int(n) for n in re.findall(r"\b(\d{2,3})\b", line.split("#", 1)[1])}
        self.assertEqual(offered, OVERLAY_SIZES)


class Rewrite(unittest.TestCase):
    def test_the_all_line_gains_the_size(self):
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-512", rewrite(STOCK).splitlines())

    def test_other_boards_keep_their_own_size(self):
        got = rewrite(STOCK)
        self.assertEqual(got.count("dtoverlay=vc4-kms-v3d,cma-128"), 2)
        for section in ("[pi3+]", "[pi02]"):
            after = got.split(section, 1)[1].splitlines()[1]
            self.assertEqual(after, "dtoverlay=vc4-kms-v3d,cma-128")

    def test_exactly_one_line_changes(self):
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

    def test_dwc2_and_the_rest_are_left_alone(self):
        self.assertIn("dtoverlay=dwc2", rewrite(STOCK).splitlines())


class Guards(unittest.TestCase):
    """The greps that decide whether the rewrite runs at all."""

    def grep(self, pattern, text):
        return subprocess.run(["grep", "-qE", pattern],
                              input=text, text=True).returncode == 0

    def test_absent_overlay_is_detected(self):
        pattern = r'^dtoverlay=vc4-kms-v3d([,[:space:]]|$)'
        self.assertTrue(self.grep(pattern, STOCK))
        self.assertFalse(self.grep(pattern, "[all]\ndtoverlay=dwc2\n"))

    def test_already_set_is_detected_only_at_the_wanted_size(self):
        pattern = r'^dtoverlay=vc4-kms-v3d,cma-512([,[:space:]]|$)'
        self.assertFalse(self.grep(pattern, STOCK))
        self.assertTrue(self.grep(pattern, rewrite(STOCK)))

    def test_another_boards_size_does_not_read_as_already_set(self):
        """[pi3+]'s cma-128 must not satisfy a request for 512."""
        pattern = r'^dtoverlay=vc4-kms-v3d,cma-512([,[:space:]]|$)'
        self.assertFalse(self.grep(pattern, STOCK))


if __name__ == "__main__":
    unittest.main()
