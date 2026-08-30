"""`wk logs <ws>` (cmd/logs): which log it shows. A workspace that has been
built has build.log; an image workspace mid-stage (or one whose last image
build never reached `wk build`) has none, only the stage log its builder
wrote under home/ -- yocto_log in image/yocto.sh (home/yocto-<stage>.log)
or buildroot_log in image/buildroot.sh (home/buildroot-<stage>.log). This
drives cmd/logs directly with WK_NAME/WK_TARGET/WK_VM_STORE set, the way a
dispatcher-resolved `wk logs <ws>` would leave it for a `vm`-kind
workspace, but without going through `./wk`: a maintainer's interactive
shell can have WK_NAME/WK_TARGET/WK_TARGET_KIND exported for a real
workspace (left over from working inside one), and `ws_target`/`resolve_target`
(lib/target.sh) return an already-set WK_TARGET unasked -- so a bare `./wk
logs <ws>` in such a shell resolves against whatever that leftover target is,
not this test's scratch store. tests.support strips those three at import
(see the top of tests/support.py), which is what makes `bash()` below safe
regardless of the environment this suite is run from; setting WK_TARGET
ourselves is the same seam, driven directly.

Run: python3 -m unittest tests.test_logs -v
"""
import unittest

from tests.support import REPO, WkTest, bash

CMD_LOGS = REPO / "cmd" / "logs"


class TestLogsPicksTheRightFile(WkTest):
    def _run(self, name, store, args=()):
        env = {"WK_NAME": name, "WK_TARGET": "vm", "WK_VM_STORE": str(store)}
        return bash(f'exec "{CMD_LOGS}" {" ".join(args)}', env=env)

    def test_only_an_image_stage_log_is_shown_and_named(self):
        """no build.log, a yocto stage log present -> its tail, and an info
        line naming the file (it is not build.log, so which one matters)"""
        name = "yoctows"
        home = self.tmp / "ws" / name / "home"
        home.mkdir(parents=True)
        log = home / "yocto-image.log"
        log.write_text("bitbake is fetching layers\n")
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("bitbake is fetching layers", out, out)
        self.assertIn(str(log), out, out)

    def test_a_buildroot_stage_log_is_treated_the_same(self):
        """buildroot_log's home/buildroot-<stage>.log is the same kind of
        answer as yocto's -- one code path, not a yocto special case"""
        name = "buildrootws"
        home = self.tmp / "ws" / name / "home"
        home.mkdir(parents=True)
        log = home / "buildroot-image.log"
        log.write_text("building the buildroot rootfs\n")
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("building the buildroot rootfs", out, out)
        self.assertIn(str(log), out, out)

    def test_build_log_wins_when_both_exist(self):
        """a real build.log is the answer even mid-image-rebuild, and gets
        no "showing ..." line -- it is the default, not a second guess"""
        name = "bothws"
        wsdir = self.tmp / "ws" / name
        home = wsdir / "home"
        home.mkdir(parents=True)
        (home / "yocto-image.log").write_text("stale image-stage output\n")
        (wsdir / "build.log").write_text("ninja: building targets\n")
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("ninja: building targets", out, out)
        self.assertNotIn("stale image-stage output", out, out)
        self.assertNotIn("showing", out, out)

    def test_neither_log_names_both_kinds_in_the_refusal(self):
        """no build.log, no image-stage log: the refusal names both kinds
        and the remedy, not just "no build log" """
        name = "emptyws"
        (self.tmp / "ws" / name).mkdir(parents=True)
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("no build log", out, out)
        self.assertIn("image-stage log", out, out)
        self.assertIn("wk build", out, out)
        self.assertIn("wk sysimage build", out, out)


if __name__ == "__main__":
    unittest.main()
