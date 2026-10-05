"""A multilib image installs one width: nothing else rewrites IMAGE_INSTALL, so unmapped it assembles a 64-bit
rootfs under a 32-bit name. The bbappend's anonymous python is lifted and run against a fake datastore, since bitbake
is not importable here."""
import re
import unittest

from tests.support import REPO

BBAPPEND = (REPO / "image" / "yocto" / "meta-wk-multilib" / "recipes-webkit"
            / "images" / "webkit-dev-ci-tools.bbappend")


class FakeD(dict):
    """Enough of bitbake's datastore for the lifted function."""

    def getVar(self, k):
        return self.get(k)

    def setVar(self, k, v):
        self[k] = v


def run_bbappend(d):
    text = BBAPPEND.read_text()
    m = re.search(r"(?ms)^python \(\) \{\n(.*)^\}", text)
    assert m, "the bbappend no longer has an anonymous python function"
    body = "\n".join(l[4:] if l.startswith("    ") else l
                     for l in m.group(1).splitlines())
    exec(compile("def _f(d):\n" + "\n".join("    " + l for l in body.splitlines()),
                 str(BBAPPEND), "exec"), globals())
    return _f(d)  # noqa: F821


def a_lib32_image(**over):
    d = FakeD({
        "MLPREFIX": "lib32-",
        "IMAGE_INSTALL": "wpewebkit cog linux-raspberrypi",
        "WK_MULTILIB_KEEP": "linux-raspberrypi",
        "NON_MULTILIB_RECIPES": "",
    })
    d.update(over)
    return d


class TestTheImageInstallsOneWidth(unittest.TestCase):
    def test_every_package_takes_the_prefix_but_what_has_one_build_per_machine(self):
        d = a_lib32_image()
        run_bbappend(d)
        self.assertEqual(sorted(d["IMAGE_INSTALL"].split()), ["lib32-cog", "lib32-wpewebkit", "linux-raspberrypi"])

    def test_a_plain_image_is_left_alone(self):
        """No MLPREFIX, no rewriting -- the 64-bit build shares this recipe."""
        d = a_lib32_image(MLPREFIX="")
        run_bbappend(d)
        self.assertEqual("wpewebkit cog linux-raspberrypi", d["IMAGE_INSTALL"])


if __name__ == "__main__":
    unittest.main()
