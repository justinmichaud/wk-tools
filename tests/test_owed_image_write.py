"""What a write decides for itself (lib/wk/sysimage/write.py): the decompressor a builder's extension names, the
identity a second system leaves to the rescue beside it, and a `--from` image no profile explains getting the
identity marker and the driving key and nothing else. Distinct identities per disk are tests/test_sysimage_write.py.

Run: python3 tests/run.py --unit -k test_owed_image_write
"""
import contextlib
import io
import sys
import unittest

from tests.support import (REPO, TAILSCALE_KNOWS_NOTHING, WkTest, run, scratch_dir,
                           stub_path)

sys.path.insert(0, str(REPO / "lib"))
from wk.machine import Fake  # noqa: E402
from wk.sysimage import write  # noqa: E402


class TestFromFilterPicksTheDecompressor(unittest.TestCase):
    """The command that undoes a builder's own compression, by the extension the builder is known to produce --
    yocto's .wic.xz, an ad-hoc .zst or .gz, and a plain buildroot .img (or anything else) passed through unchanged."""

    def test_each_extension_names_its_tool(self):
        for path, want in (("rpi3.wic.xz", "xz -dc"), ("rpi3.img.zst", "zstd -dc"), ("rpi3.img.gz", "gzip -dc"),
                           ("rpi3.img", "cat"), ("rpi3.wic", "cat")):
            with self.subTest(path=path):
                self.assertEqual(write.from_filter(path), want)


class TestASecondSystemKeepsTheRescuesIdentity(unittest.TestCase):
    def test_no_identity_is_generated_at_all(self):
        """The second system's partitions take their PARTUUIDs from the rescue already on the card."""
        w = write.Write(REPO, {}, Fake(), None, rand=lambda: self.fail("an identity was generated"))
        w.conf, w.ch = {"NODE_NAME": "rpi3"}, None
        with contextlib.redirect_stderr(io.StringIO()) as err:
            w.unique_identity("/dev/sdX@second")
        self.assertIn("keeps the rescue disk's identity", err.getvalue())
        self.assertEqual(w.plan, [])


class TestWriteFromInstallsMarkerAndKeyOnly(WkTest):
    """lib/wk/sysimage/write.py: whether a card gets the whole fleet
    integration (identity marker, driving key, systemd units, retargeted
    root) or only the marker and key depends on whether the image came from
    a builder this repo knows (IMG_BUILDER=yocto|buildroot, set by
    --profile) -- not on --rescue, which only picks the tailnet role. A real
    profile (tests/test_card_edits.py's TestWriteDryRunIsTheWholeSequence)
    gets the units line; this is the complementary case, a `--from` write
    with no profile the checkout can identify, which does not."""

    _SSH = '''#!/bin/sh
case "$*" in
  *card-priv*status*) exit 0 ;;
  *card-priv*check*)  echo "wk-card-priv: /dev/sdX may be written: usb 64G"; exit 0 ;;
  *) exit 0 ;;
esac
'''

    def test_no_profile_gets_marker_and_key_and_not_the_fleet_steps(self):
        img = self.tmp / "unknown-source.img"
        img.write_text("not a wic, not a profile this checkout knows\n")
        key = self.tmp / "id.pub"
        key.write_text("ssh-ed25519 AAAAtest test@example\n")
        with stub_path({"ssh": self._SSH, "tailscale": TAILSCALE_KNOWS_NOTHING}) as binp, \
                scratch_dir() as store:
            cp = run(
                "sysimage", "write", "--from", str(img),
                "--disk", "rpi5:/dev/sdX", "--dry-run",
                env={"PATH": f"{binp}:{__import__('os').environ['PATH']}",
                     "WK_IMAGE_KEY": str(key), "WK_STORE": str(store)},
            )
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("identity marker and driving key only", out, out)
        self.assertNotIn("would install the fleet units", out, out)
        self.assertNotIn("would retarget", out, out)
        self.assertNotIn("would append this profile's firmware block", out, out)
        self.assertNotIn("would name the system on", out, out)
        # The marker and key step still runs, unconditionally.
        self.assertIn("would install the identity marker and the driving ssh key", out, out)


if __name__ == "__main__":
    unittest.main()
