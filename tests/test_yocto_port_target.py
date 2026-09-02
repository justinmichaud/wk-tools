"""Giving a release branch a cross-target it does not have.

WebKit's Tools/yocto gained its rpi5 section after the 2.4x releases branched,
so those branches can build for every Pi but that one -- while the layers they
pin already support the machine. image/yocto/port-target.py derives the missing
glue from a target the branch does have: the local.conf is that target's with
one MACHINE line changed, the section is its section with one path changed.

Run: python3 -m unittest tests.test_yocto_port_target -v
"""
import configparser
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORT = REPO / "image" / "yocto" / "port-target.py"

TARGETS = """[rpi4-64bits-mesa]
repo_manifest_path = rpi/manifest.xml
conf_bblayers_path = rpi/bblayers.conf
conf_local_path = rpi/local-rpi4-64bits-mesa.conf
image_basename = webkit-dev-ci-tools
image_types = tar.xz wic.xz
patch_file_path = meta-openembedded_and_meta-webkit.patch
"""

LOCAL = """DISTRO = "webkitdevci"
# Machine selection
MACHINE = "raspberrypi4-64"
BB_DISKMON_DIRS ??= "HALT,${TMPDIR},100M,1K"
"""


class PortTest(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-port-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "rpi").mkdir()
        (self.tmp / "targets.conf").write_text(TARGETS)
        (self.tmp / "rpi" / "local-rpi4-64bits-mesa.conf").write_text(LOCAL)

    def port(self, target="rpi5-64bits-mesa", frm="rpi4-64bits-mesa",
             machine="raspberrypi5", image=None):
        return subprocess.run(
            ["python3", str(PORT), "--yocto-dir", str(self.tmp), "--target", target,
             "--from-target", frm, "--machine", machine]
            + (["--image", image] if image else []),
            capture_output=True, text=True)

    def test_a_multilib_target_names_its_own_image_recipe(self):
        # A multilib variant builds a differently-named image recipe out of
        # the same layers, and the helper reads which one from this section.
        cp = self.port(target="rpi5-32bits-mesa", image="lib32-webkit-dev-ci-tools")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        conf = configparser.ConfigParser()
        conf.read(self.tmp / "targets.conf")
        new = conf["rpi5-32bits-mesa"]
        self.assertEqual(new["image_basename"], "lib32-webkit-dev-ci-tools")
        # Everything else still comes from the target it was derived from.
        for k in ("repo_manifest_path", "conf_bblayers_path", "image_types",
                  "patch_file_path"):
            self.assertEqual(new[k], conf["rpi4-64bits-mesa"][k], k)
        self.assertIn("image lib32-webkit-dev-ci-tools", cp.stdout)

    def test_without_the_flag_the_image_is_the_derived_from_one(self):
        cp = self.port()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        conf = configparser.ConfigParser()
        conf.read(self.tmp / "targets.conf")
        self.assertEqual(conf["rpi5-64bits-mesa"]["image_basename"],
                         "webkit-dev-ci-tools")

    def test_the_section_and_its_local_conf_are_derived(self):
        cp = self.port()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        conf = configparser.ConfigParser()
        conf.read(self.tmp / "targets.conf")
        self.assertIn("rpi5-64bits-mesa", conf.sections())
        new = conf["rpi5-64bits-mesa"]
        # Only the local.conf path differs from the target it came from.
        self.assertEqual(new["conf_local_path"], "rpi/local-rpi5-64bits-mesa.conf")
        for k in ("repo_manifest_path", "conf_bblayers_path", "image_basename",
                  "image_types", "patch_file_path"):
            self.assertEqual(new[k], conf["rpi4-64bits-mesa"][k], k)
        # ...and only the MACHINE line differs in the local.conf.
        text = (self.tmp / "rpi" / "local-rpi5-64bits-mesa.conf").read_text()
        self.assertIn('MACHINE = "raspberrypi5"', text)
        self.assertNotIn("raspberrypi4-64", text)
        self.assertIn('DISTRO = "webkitdevci"', text)
        self.assertIn("BB_DISKMON_DIRS", text)
        self.assertIn("ported [rpi5-64bits-mesa]", cp.stdout)

    def test_a_branch_that_already_has_it_is_left_alone(self):
        """Idempotent, so a re-run after a killed build converges instead of
        appending a second section."""
        self.assertEqual(self.port().returncode, 0)
        before = (self.tmp / "targets.conf").read_text()
        cp = self.port()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("already has", cp.stdout)
        self.assertEqual((self.tmp / "targets.conf").read_text(), before)

    def test_no_source_target_is_refused_with_what_there_is(self):
        cp = self.port(frm="rpi9-64bits-mesa")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("rpi4-64bits-mesa", cp.stderr, "the refusal does not list what is there")

    def test_a_local_conf_with_no_machine_line_is_refused(self):
        """Deriving means changing exactly one line; anything else and this is
        not the file it was thought to be."""
        (self.tmp / "rpi" / "local-rpi4-64bits-mesa.conf").write_text('DISTRO = "x"\n')
        cp = self.port()
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("MACHINE", cp.stderr)

    def test_a_missing_local_conf_is_refused(self):
        (self.tmp / "rpi" / "local-rpi4-64bits-mesa.conf").unlink()
        cp = self.port()
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not in this checkout", cp.stderr)


if __name__ == "__main__":
    unittest.main()
