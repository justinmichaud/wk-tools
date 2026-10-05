"""The real mbp reached, and a sysimage write dry run on a Mac."""
import os
import platform
import unittest

from tests.support import TAILSCALE_KNOWS_NOTHING, WkTest, rand_suffix, requires_machine, run, scratch_dir, stub_path


class TestBenchReachesTheRealMbp(unittest.TestCase):
    @requires_machine("tolken")
    def test_wk_boot_reaches_mbp(self):
        cp = run("boot", "mbp", "--status")
        self.assertIn(cp.returncode, (0, 2, 3), cp.stdout)


@unittest.skipUnless(platform.system() == "Darwin", "the GNU-tool risk this guards against is specific to a macOS driver")
class TestSysimageWriteDryRunOnAMac(WkTest):
    _SSH = '''#!/bin/sh
case "$*" in
  *card-priv*status*) exit 0 ;;
  *card-priv*check*)  echo "wk-card-priv: /dev/sdX may be written: usb 64G"; exit 0 ;;
  *) exit 0 ;;
esac
'''

    def test_dry_run_prints_its_plan_and_says_nothing_was_written(self):
        img = self.tmp / "fake.img"
        img.write_text("not a real image, just bytes\n")
        with stub_path({"ssh": self._SSH, "tailscale": TAILSCALE_KNOWS_NOTHING}) as binp, \
                scratch_dir() as store:
            cp = run(
                "sysimage", "write", "--from", str(img),
                "--disk", f"rpi5:/dev/sd{rand_suffix(2)}", "--dry-run",
                env={"PATH": f"{binp}:{os.environ['PATH']}", "WK_STORE": str(store)},
            )
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("dry run -- nothing was written.", out, out)
        self.assertNotIn("command not found", out, out)
        self.assertNotIn("illegal option", out, out)  # BSD stat/numfmt rejecting a GNU flag


if __name__ == "__main__":
    unittest.main()
