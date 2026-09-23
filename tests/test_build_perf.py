"""Build performance fixes: JSC-only configs ask ccache for explicitly
(lib/wk/buildconf.py), and cmd/sudo requires visudo outright rather than
falling back to a second lookup path. A workspace's WebKitBuild being a
bind-mounted plain directory is tests/test_wk_targets.py's.

Run: python3 -m unittest tests.test_build_perf -v
"""
import re
import sys
import unittest

from tests.support import REPO, WkTest, fake_workspace

sys.path.insert(0, str(REPO / "lib"))
from wk import buildconf  # noqa: E402


# --------------------------------------------------------------------------- #
# Item 1: the JSC-only configs (lib/wk/buildconf.py) state WK_USE_CCACHE=YES in
# the environment buildconf.build_env assembles, rather than depending on
# build-webkit's own default -- the same channel build-webkit's own
# --use-ccache writes (WK_USE_CCACHE, read directly by
# Source/cmake/WebKitCCache.cmake), so it does not disturb WK_BUILD_ARGS,
# the literal build-webkit command line other tests (tests/test_build.py)
# already pin exactly.
#
# A --dry-run's `running:` line is checked too, for the negative: it must
# never carry --no-use-ccache, which is the one spelling that would turn
# caching off regardless of WK_USE_CCACHE.
# --------------------------------------------------------------------------- #

class TestJscConfigsUseCcache(WkTest):
    def _wk_use_ccache(self, config, os="linux"):
        c = buildconf.resolve(config, os, "container", {})
        return "".join(e for e in buildconf.build_env(c, "/src/WebKit", 4, 10, "native", "/ccache", {}) if e.startswith("WK_USE_CCACHE="))

    def _running_line(self, config):
        with fake_workspace() as ws:
            cp = ws.run("build", config, "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            lines = [l for l in cp.stdout.splitlines() if "running:" in l]
            self.assertEqual(len(lines), 1, cp.stdout)
            return lines[0]

    def test_jsc_debug_asks_for_ccache(self):
        """jsc-debug's environment carries WK_USE_CCACHE=YES, and the dry-run line never disables it"""
        self.assertEqual(self._wk_use_ccache("jsc-debug"), "WK_USE_CCACHE=YES")
        self.assertNotIn("--no-use-ccache", self._running_line("jsc-debug"))

    def test_jsc_release_asks_for_ccache(self):
        """jsc-release's environment carries WK_USE_CCACHE=YES, and the dry-run line never disables it"""
        self.assertEqual(self._wk_use_ccache("jsc-release"), "WK_USE_CCACHE=YES")
        self.assertNotIn("--no-use-ccache", self._running_line("jsc-release"))

    def test_jsc_release_asan_asks_for_ccache(self):
        """jsc-release-asan's environment carries WK_USE_CCACHE=YES, and the dry-run line never disables it"""
        self.assertEqual(self._wk_use_ccache("jsc-release-asan"), "WK_USE_CCACHE=YES")
        self.assertNotIn("--no-use-ccache", self._running_line("jsc-release-asan"))

    def test_other_ports_are_untouched(self):
        """gtk/wpe configs get no WK_USE_CCACHE -- this fixes the JSC path, not every port"""
        self.assertEqual(self._wk_use_ccache("gtk-release"), "")


# --------------------------------------------------------------------------- #
# Item 4: cmd/sudo requires visudo outright -- no second, hardcoded lookup
# path tried only when `have visudo` fails.
# --------------------------------------------------------------------------- #

class TestSudoRequiresVisudo(WkTest):
    def test_no_have_visudo_fallback(self):
        """static: cmd/sudo no longer branches on `have visudo` with a second lookup"""
        text = (REPO / "cmd" / "sudo").read_text()
        self.assertNotIn("have visudo", text)

    def test_visudo_resolve_refuses_outright_when_absent(self):
        """visudo_resolve dies naming visudo/sudo when it is not on PATH"""
        text = (REPO / "cmd" / "sudo").read_text()
        m = re.search(r"(?ms)^visudo_resolve\(\).*?^\}", text)
        self.assertIsNotNone(m, "visudo_resolve not found in cmd/sudo")
        body = m.group(0)
        self.assertNotIn("/usr/sbin/visudo", body, "a second, hardcoded lookup path is still there")
        self.assertIn("visudo", body)


if __name__ == "__main__":
    unittest.main()
