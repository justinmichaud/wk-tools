"""The Yocto image stage's completion evidence (image/yocto-build.sh).

cross-toolchain-helper's own build_image() treats the mere presence of a file
at build/image/<recipe>.<ext> as proof the image is current, and returns
without ever invoking bitbake when one is already there -- observed on moose:
`wk sysimage build webkit-2.52-yocto-rpi3-32` printed "stage 'image' done"
after a two-minute run whose log held no bitbake NOTE lines at all, while the
newest file under .../tmp/deploy/images/raspberrypi3/ and the newest cooker
log both predated the run by almost a day, despite local.conf changing that
same day (a new IMAGE_INSTALL:append line). yocto-build.sh's image stage now
(1) clears that copy directory before calling the helper, so it cannot serve
a previous run's copy, and (2) checks the result's mtime against the stage's
own start time, so a helper that finds another way back to the same shortcut
fails loudly instead of printing "done" over a stale artifact.

Nothing here builds Yocto or touches a workspace: the two functions are
lifted out of image/yocto-build.sh with sed (the tests/test_wifi_seed.py
idiom for calling one function without sourcing a whole script) and driven
against a scratch directory.

Run: python3 -m unittest tests.test_yocto_stage -v
"""
import subprocess
import time
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, scratch_dir

YOCTO_BUILD = REPO / "image" / "yocto-build.sh"


def _lift(func):
    """A function's body, sed'd out of image/yocto-build.sh -- see
    tests/test_wifi_seed.py's _lift for why (some of what that file does at
    import time needs a real checkout and a container, which nothing here
    has)."""
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(YOCTO_BUILD)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"could not find {func}() in {YOCTO_BUILD}"
    return text


# say() and fail() are the two things both lifted functions call; stubbed
# here rather than lifted too, the same way test_wifi_seed.py's gate test
# stubs deny() -- fail() must exit non-zero for a caller to observe, and this
# makes that the whole of what it does, with its message on stderr where the
# assertions below look for it.
PRELUDE = '''
say()  { :; }
fail() { printf 'wk-yocto: error: %s\\n' "$*" >&2; exit 1; }
'''


def _run(body, *args):
    script = PRELUDE + body + "\n" + " ".join(args)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10,
    )


class TestVerifyImageFreshness(WkTest):
    """verify_image_freshness <dir> <start-epoch>: ok only when something in
    <dir> is at least as new as <start-epoch>."""

    def setUp(self):
        super().setUp()
        self.func = _lift("verify_image_freshness")

    def test_ok_when_a_file_is_newer_than_the_stage_start(self):
        with scratch_dir() as d:
            f = d / "webkit-dev-ci-tools.wic.xz"
            f.write_text("fresh")
            start = int(time.time()) - 5
            cp = _run(self.func, "verify_image_freshness", str(d), str(start))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_ok_when_a_file_matches_the_stage_start_exactly(self):
        # >=, not >: a build fast enough to land in the same second as the
        # recorded start must not be reported as stale.
        with scratch_dir() as d:
            f = d / "webkit-dev-ci-tools.wic.xz"
            f.write_text("fresh")
            import os
            now = int(time.time())
            os.utime(f, (now, now))
            cp = _run(self.func, "verify_image_freshness", str(d), str(now))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_fails_when_the_only_file_predates_the_stage_start(self):
        """the moose defect itself: a copy left over from a previous run"""
        with scratch_dir() as d:
            f = d / "webkit-dev-ci-tools.wic.xz"
            f.write_text("stale")
            import os
            yesterday = int(time.time()) - 86400
            os.utime(f, (yesterday, yesterday))
            start = int(time.time())
            cp = _run(self.func, "verify_image_freshness", str(d), str(start))
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("no new image", cp.stderr)
            self.assertIn("stale", cp.stderr)

    def test_fails_when_the_directory_is_empty(self):
        with scratch_dir() as d:
            start = int(time.time())
            cp = _run(self.func, "verify_image_freshness", str(d), str(start))
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("left nothing behind", cp.stderr)

    def test_fails_when_the_directory_does_not_exist(self):
        with scratch_dir() as d:
            missing = d / "does-not-exist"
            start = int(time.time())
            cp = _run(self.func, "verify_image_freshness", str(missing), str(start))
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("left nothing behind", cp.stderr)


class TestClearStaleImageCopies(WkTest):
    def setUp(self):
        super().setUp()
        self.func = _lift("clear_stale_image_copies")

    def test_removes_a_directory_left_by_a_previous_run(self):
        with scratch_dir() as d:
            target = d / "image"
            target.mkdir()
            (target / "webkit-dev-ci-tools.wic.xz").write_text("old")
            cp = _run(self.func, "clear_stale_image_copies", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse(target.exists())

    def test_is_a_noop_when_nothing_is_there_yet(self):
        with scratch_dir() as d:
            target = d / "image"
            cp = _run(self.func, "clear_stale_image_copies", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse(target.exists())


class TestImageStageIsEvidenceBased(unittest.TestCase):
    """Static: the image stage clears the helper's shortcut and checks its
    result before ever printing 'done', and the fix does not grow a second
    "already built, skip it" branch of its own (CLAUDE.md, "One path, not
    two") -- the file already carries exactly this shape for tmp/hosttools
    (clear_hosttools) and layer sync (init_workdir's own "already synced"
    early-return is the one deliberate skip in the file, and it is not near
    the image case arm)."""

    def setUp(self):
        self.text = YOCTO_BUILD.read_text()

    def _case_arm(self, label):
        import re
        m = re.search(
            rf"(?ms)^\s*{re.escape(label)}\)\n(.*?)\n\s*;;", self.text
        )
        self.assertIsNotNone(m, f"no '{label})' case arm in {YOCTO_BUILD}")
        return m.group(1)

    def test_image_stage_clears_the_shortcut_before_calling_build_image(self):
        arm = self._case_arm("image|all")
        clear_at = arm.find("clear_stale_image_copies")
        helper_at = arm.find("--build-image")
        self.assertNotEqual(clear_at, -1, arm)
        self.assertNotEqual(helper_at, -1, arm)
        self.assertLess(clear_at, helper_at,
                         "clear_stale_image_copies must run before --build-image, "
                         "or the helper still finds yesterday's copy")

    def test_image_stage_verifies_freshness_after_calling_build_image(self):
        arm = self._case_arm("image|all")
        helper_at = arm.find("--build-image")
        verify_at = arm.find("verify_image_freshness")
        self.assertNotEqual(verify_at, -1, arm)
        self.assertLess(helper_at, verify_at,
                         "verify_image_freshness must run after --build-image, "
                         "or there is nothing yet to check")

    def test_image_stage_has_no_second_already_built_skip_branch(self):
        """the image case arm trusts no marker or 'if already/exists' shortcut
        of its own -- that would just be a second copy of the bug this fixes,
        this time inside wk-tools instead of upstream."""
        arm = self._case_arm("image|all")
        lowered = arm.lower()
        for bad in ("already built", "already exists", "skip"):
            self.assertNotIn(bad, lowered,
                              f"'{bad}' found in the image case arm -- a skip "
                              "branch here reintroduces the moose defect")
