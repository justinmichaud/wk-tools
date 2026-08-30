"""The Yocto image stage's completion evidence (image/yocto-build.sh), and
the driving side's own running/not-running decision (image/yocto.sh).

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

Nothing in TestVerifyImageFreshness/TestClearStaleImageCopies/
TestImageStageIsEvidenceBased builds Yocto or touches a workspace: the two
functions are lifted out of image/yocto-build.sh with sed (the
tests/test_wifi_seed.py idiom for calling one function without sourcing a
whole script) and driven against a scratch directory.

YoctoStageTest and YoctoSpawnRefusesASecondBuild cover a related handoff
item on the driving side, in image/yocto.sh: does `kill -9` mid-`wk sysimage
build` converge on re-run, and does a second `wk sysimage build` of the same
profile refuse rather than race the first's cleanup? `yocto_running` is
documented as reading evidence, not the status file -- driven directly here,
with `t_exec` stubbed to run its check on this host instead of inside a
container workspace, against a status file left saying `state=running` and
a pid that no longer exists (the exact shape a killed driver leaves).

Run: python3 -m unittest tests.test_yocto_stage -v
"""
import os
import subprocess
import time
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, scratch_dir

YOCTO_BUILD = REPO / "image" / "yocto-build.sh"
DEAD_PID = "99999999"  # a pid essentially guaranteed not to exist


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
            now = int(time.time())
            os.utime(f, (now, now))
            cp = _run(self.func, "verify_image_freshness", str(d), str(now))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_fails_when_the_only_file_predates_the_stage_start(self):
        """the moose defect itself: a copy left over from a previous run"""
        with scratch_dir() as d:
            f = d / "webkit-dev-ci-tools.wic.xz"
            f.write_text("stale")
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


class YoctoStageTest(WkTest):
    """Sources lib/common.sh, lib/store.sh (wk_ws_dir) and image/yocto.sh,
    with `t_exec` stubbed to run its command on this host -- the same
    'un-managed clobbering' technique test_disk_logic.py uses, standing in
    for a container workspace's pid namespace without one."""

    PRELUDE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/image/yocto.sh"
t_exec() {{ shift; "$@"; }}
'''

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.ws = "yoctows"
        self.home = self.store / "ws" / self.ws / "home"
        self.home.mkdir(parents=True)

    def _write_status(self, state="running"):
        (self.store / "ws" / self.ws / "yocto.status").write_text(
            f"state={state}\nprofile=demo\nid=demo-1\ntarget=rpi3-32\n"
            f"branch=main\nstage=image\nstarted=2026-01-01T00:00:00Z\n"
        )

    def _write_pidfile(self, stage, pid):
        (self.home / f"yocto-{stage}.pid").write_text(f"{pid}\n")

    def _run(self, script):
        env = dict(os.environ)
        env["WK_STORE"] = str(self.store)
        env["WK_ROOT"] = str(REPO)
        for var in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND"):
            env.pop(var, None)
        return subprocess.run(
            ["bash", "-c", self.PRELUDE + script],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30,
        )

    def test_a_dead_pid_after_kill_9_is_not_read_as_running(self):
        """the exact scenario: killed mid-build, status says running, pid is dead"""
        self._write_status(state="running")
        self._write_pidfile("image", DEAD_PID)
        cp = self._run(f'yocto_running {self.ws} image && echo RUNNING || echo NOT-RUNNING')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "NOT-RUNNING", cp.stdout + cp.stderr)

    def test_a_dead_pid_means_no_stage_is_reported_running_at_all(self):
        self._write_status(state="running")
        self._write_pidfile("image", DEAD_PID)
        cp = self._run(f'yocto_any_running {self.ws} && echo LIVE || echo NONE')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "NONE", cp.stdout + cp.stderr)

    def test_a_genuinely_live_pid_is_still_read_as_running(self):
        """positive control: yocto_running is not simply hard-wired to say no"""
        proc = subprocess.Popen(["sleep", "60"])
        try:
            self._write_status(state="running")
            self._write_pidfile("image", proc.pid)
            cp = self._run(f'yocto_running {self.ws} image && echo RUNNING || echo NOT-RUNNING')
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), "RUNNING", cp.stdout + cp.stderr)
        finally:
            proc.kill()
            proc.wait()

    def test_dry_run_after_a_kill_9_does_not_claim_a_build_is_running(self):
        """the CLI end to end: `wk sysimage build <profile> --dry-run` against
        a workspace left by a killed build prints its plan, not a claim that
        a build is already running (yocto_build's --dry-run path never even
        asks -- it is the decision path above that a real re-run relies on)."""
        self._write_status(state="running")
        self._write_pidfile("image", DEAD_PID)
        cp = self._run_wk_sysimage_dry_run()
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("dry run", out, out)
        self.assertNotIn("already running", out, out)

    def _run_wk_sysimage_dry_run(self):
        env = dict(os.environ)
        env["WK_STORE"] = str(self.store)
        env["WK_TARGET"] = "container"
        for var in ("WK_NAME", "WK_TARGET_KIND"):
            env.pop(var, None)
        return subprocess.run(
            [str(REPO / "wk"), "sysimage", "build", "webkit-2.52-yocto-rpi3-32",
             "--workspace", self.ws, "--dry-run"],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60,
        )


class YoctoSpawnRefusesASecondBuild(WkTest):
    """Item: two `wk sysimage build` of the same profile at once. Yocto's
    guard is `yocto_spawn`'s early check (`yocto_any_running`): a live pid
    for any stage refuses starting a new one, since every stage shares one
    bitbake build directory."""

    PRELUDE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/image/yocto.sh"
t_exec() {{ shift; "$@"; }}
IMG_PROFILE=demo-profile
'''

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.ws = "yoctows"
        self.home = self.store / "ws" / self.ws / "home"
        self.home.mkdir(parents=True)

    def test_second_spawn_refuses_and_names_the_live_stage_and_log(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            (self.store / "ws" / self.ws / "yocto.status").write_text("state=running\n")
            (self.home / "yocto-image.pid").write_text(f"{proc.pid}\n")
            env = dict(os.environ)
            env["WK_STORE"] = str(self.store)
            env["WK_ROOT"] = str(REPO)
            cp = subprocess.run(
                ["bash", "-c", self.PRELUDE + f'yocto_spawn {self.ws} image'],
                cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30,
            )
            out = cp.stdout + cp.stderr
            self.assertNotEqual(cp.returncode, 0, out)
            self.assertIn("already running", out, out)
            self.assertIn("image", out, out)
            self.assertIn(str(self.home / "yocto-image.log"), out, out)
            self.assertIn("--stop", out, out)
        finally:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    unittest.main()
