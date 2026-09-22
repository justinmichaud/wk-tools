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

from tests.support import REPO, WkTest, bash, func_body, scratch_dir

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


class TestImageCopiesAsideAndBack(WkTest):
    """The image directory has to be empty before the helper is called, or
    build_image() reports the previous run's copy as this one's.  Emptying it
    by deleting it leaves a killed image stage with a lane that holds no image
    at all, `wk sysimage holds` answering no, and the next A/B rebuilding
    hours of image it already had -- so it is moved aside, and the next stage
    in that lane puts it back when nothing replaced it."""

    def setUp(self):
        super().setUp()
        self.func = _lift("image_copies_aside") + _lift("image_copies_kept") \
            + _lift("image_copies_back") + "say() { :; }\n"

    def _wic(self, d, text):
        d.mkdir(parents=True, exist_ok=True)
        (d / "webkit-dev-ci-tools.wic.xz").write_text(text)

    def test_aside_empties_the_directory_the_helper_looks_at(self):
        with scratch_dir() as d:
            target = d / "image"
            self._wic(target, "old")
            cp = _run(self.func, "image_copies_aside", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse(target.exists())

    def test_aside_keeps_the_image_it_moved(self):
        with scratch_dir() as d:
            target = d / "image"
            self._wic(target, "old")
            _run(self.func, "image_copies_aside", str(target))
            self.assertEqual(
                (d / "image.previous" / "webkit-dev-ci-tools.wic.xz").read_text(),
                "old")

    def test_a_killed_stage_gets_its_previous_image_back(self):
        with scratch_dir() as d:
            target = d / "image"
            self._wic(target, "old")
            _run(self.func, "image_copies_aside", str(target))   # and then killed
            cp = _run(self.func, "image_copies_back", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((target / "webkit-dev-ci-tools.wic.xz").read_text(),
                             "old")
            self.assertFalse((d / "image.previous").exists())

    def test_a_stage_that_built_one_keeps_the_new_image(self):
        with scratch_dir() as d:
            target = d / "image"
            self._wic(target, "old")
            _run(self.func, "image_copies_aside", str(target))
            self._wic(target, "new")                             # bitbake's own
            cp = _run(self.func, "image_copies_back", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((target / "webkit-dev-ci-tools.wic.xz").read_text(),
                             "new")
            self.assertFalse((d / "image.previous").exists())

    def test_kept_drops_the_aside_copy(self):
        with scratch_dir() as d:
            target = d / "image"
            self._wic(target, "old")
            _run(self.func, "image_copies_aside", str(target))
            self._wic(target, "new")
            cp = _run(self.func, "image_copies_kept", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse((d / "image.previous").exists())

    def test_aside_is_a_noop_when_nothing_is_there_yet(self):
        with scratch_dir() as d:
            target = d / "image"
            cp = _run(self.func, "image_copies_aside", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse(target.exists())

    def test_back_is_a_noop_when_nothing_was_moved_aside(self):
        with scratch_dir() as d:
            target = d / "image"
            self._wic(target, "only")
            cp = _run(self.func, "image_copies_back", str(target))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((target / "webkit-dev-ci-tools.wic.xz").read_text(),
                             "only")


class TestRefreshGitIndex(WkTest):
    """refresh_git_index <dir>: leave an index libgit2 can open.

    dotfiles/gitconfig sets index.skipHash, which writes the trailing
    checksum as nulls.  git reads that back; the libgit2 in a cross-built
    rust does not, and cargo walks up from the sources it fingerprints into
    whichever repo encloses bitbake's TMPDIR -- the checkout, or the helper's
    own workdir.  So both indexes are rewritten with a real checksum.
    """

    def setUp(self):
        super().setUp()
        self.func = _lift("refresh_git_index")

    @staticmethod
    def _trailer_is_real(index):
        data = index.read_bytes()
        import hashlib
        return data[-20:] == hashlib.sha1(data[:-20]).digest()

    def _repo(self, d):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_SYSTEM="/dev/null")
        run = lambda *a: subprocess.run(["git", "-C", str(d), *a],
                                        capture_output=True, env=env, check=True)
        run("init", "-q")
        run("config", "index.skipHash", "true")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        (d / "a").write_text("a")
        run("add", "a")
        return d / ".git" / "index"

    def test_a_null_checksum_index_is_rewritten_with_a_real_one(self):
        with scratch_dir() as d:
            index = self._repo(d)
            self.assertEqual(index.read_bytes()[-20:], b"\x00" * 20,
                             "the fixture did not produce a skipHash index")
            cp = _run(self.func, "refresh_git_index", str(d))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertTrue(self._trailer_is_real(index),
                            "libgit2 still cannot open this index")

    def test_what_is_staged_survives_the_rewrite(self):
        with scratch_dir() as d:
            self._repo(d)
            _run(self.func, "refresh_git_index", str(d))
            out = subprocess.run(["git", "-C", str(d), "diff", "--cached",
                                  "--name-only"], capture_output=True,
                                 text=True).stdout
            self.assertEqual(out.split(), ["a"], out)

    def test_the_repo_keeps_a_readable_index_for_the_next_writer(self):
        # cross-toolchain-helper runs git in this repo again, in an
        # environment this build does not govern, between the rewrite and
        # the bitbake that reads it. The pin has to survive that.
        with scratch_dir() as d:
            index = self._repo(d)
            _run(self.func, "refresh_git_index", str(d))
            env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                       GIT_CONFIG_SYSTEM="/dev/null")
            (d / "b").write_text("b")
            subprocess.run(["git", "-C", str(d), "add", "b"],
                           capture_output=True, env=env, check=True)
            self.assertTrue(self._trailer_is_real(index),
                            "a later git write brought the null checksum back")

    def test_a_directory_that_is_not_a_repo_is_a_noop(self):
        with scratch_dir() as d:
            cp = _run(self.func, "refresh_git_index", str(d))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse((d / ".git").exists())


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
        clear_at = arm.find("image_copies_aside")
        helper_at = arm.find("--build-image")
        self.assertNotEqual(clear_at, -1, arm)
        self.assertNotEqual(helper_at, -1, arm)
        self.assertLess(clear_at, helper_at,
                         "image_copies_aside must run before --build-image, "
                         "or the helper still finds yesterday's copy")

    def test_every_stage_puts_back_an_image_a_killed_one_moved_aside(self):
        """Not the image arm: whichever stage runs next in that lane is what
        converges it, and after a killed image stage that is usually a
        toolchain or webkit stage."""
        self.assertRegex(self.text,
                         r'(?m)^image_copies_back "\$WORKDIR/build/image"$')

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
_task() {{  # one yocto record, as yocto_build writes it
    local d
    d=$(task_begin yocto target "$1" "wk sysimage build demo --stage $2 --stop" \
        "$(yocto_log "$1" "$2")" $YOCTO_STAGES)
    [ -z "${{3:-}}" ] || task_pid "$d" "$3"
    task_step_state "$d" "$(yocto_stage_index "$2")" running
}}
'''

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.ws = "yoctows"
        self.home = self.store / "ws" / self.ws / "home"
        self.home.mkdir(parents=True)

    def _record(self, stage, pid):
        """One record for the workspace, written by lib/task.sh itself."""
        cp = self._run(f'_task {self.ws} {stage} {pid}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

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

    def test_stopping_a_stage_goes_through_job_kill_with_no_pattern_kill(self):
        """One implementation for stopping a job (job_kill, lib/watchdog.sh):
        the descendants of the pid the record holds, inside the workspace. A
        wkdev container shares the host's PID namespace, so `pkill -f <build
        dir>` would match another workspace's cooker."""
        execs = self.tmp / "execs"
        termed = self.tmp / "termed"
        cp = self._run(f'''
t_exec() {{
    shift
    printf '%s\n' "$*" >> "{execs}"
    case "$*" in
        "kill -0 "*)     [ ! -f "{termed}" ]; return $? ;;
        "ps -o args="*)  printf '%s\n' "bash /opt/wk-tools/image/yocto-build.sh --stage image"; return 0 ;;
        *"kill -TERM"*)  : > "{termed}"; return 0 ;;
    esac
    return 0
}}
d=$(task_begin yocto target {self.ws} "wk sysimage build demo --stage image --stop" \
    "$(yocto_log {self.ws} image)" $YOCTO_STAGES)
task_set "$d" pid_match '*yocto-build.sh*'
task_pid "$d" 4242
task_step_state "$d" "$(yocto_stage_index image)" running
yocto_stop {self.ws} image
printf 'exit=%s\n' "$(task_field "$d" exit)"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("exit=stopped", cp.stdout, cp.stdout + cp.stderr)
        sent = execs.read_text()
        self.assertIn("_watched_descendants 4242", sent, sent)
        self.assertIn("kill -TERM", sent, sent)
        self.assertNotIn("pkill", sent, sent)

    def test_a_dead_pid_after_kill_9_is_not_read_as_running(self):
        """the exact scenario: killed mid-build, status says running, pid is dead"""
        self._record("image", DEAD_PID)
        cp = self._run(f'yocto_running {self.ws} image && echo RUNNING || echo NOT-RUNNING')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "NOT-RUNNING", cp.stdout + cp.stderr)

    def test_a_stage_claims_nothing_about_the_stages_before_it(self):
        """Each stage begins a record of its own and prunes the last, so the
        stages before the one running were not run by this record. Claiming
        them done reads as an image that was built when none was: `[x] image`
        against a lane `wk sysimage ls` says has no image in it."""
        self._record("webkit", DEAD_PID)
        cp = self._run(
            'd=$(task_find yocto %s); task_steps "$d"' % self.ws)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        states = dict(l.split("\t") for l in cp.stdout.splitlines() if "\t" in l)
        self.assertEqual(states["5"], "running", cp.stdout)   # webkit
        for n in ("1", "2", "3", "4", "6"):
            self.assertEqual(states[n], "pending",
                             "stage %s is claimed done by a run that never did it" % n)

    def test_a_dead_pid_means_no_stage_is_reported_running_at_all(self):
        self._record("image", DEAD_PID)
        cp = self._run(f'yocto_any_running {self.ws} && echo LIVE || echo NONE')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "NONE", cp.stdout + cp.stderr)

    def test_a_genuinely_live_pid_is_still_read_as_running(self):
        """positive control: yocto_running is not simply hard-wired to say no"""
        proc = subprocess.Popen(["sleep", "60"])
        try:
            self._record("image", proc.pid)
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
        self._record("image", DEAD_PID)
        cp = self._run_wk_sysimage_dry_run()
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("dry run", out, out)
        self.assertNotIn("already running", out, out)

    def _run_wk_sysimage_dry_run(self):
        env = dict(os.environ)
        env["WK_STORE"] = str(self.store)
        env["WK_IN_VM"] = "1"   # this machine holds the store: no forward into the podman VM
        env["WK_TARGET"] = "container"
        for var in ("WK_NAME", "WK_TARGET_KIND"):
            env.pop(var, None)
        return subprocess.run(
            [str(REPO / "wk"), "sysimage", "build", "webkit-2.52-yocto-rpi3-32",
             "--workspace", self.ws, "--dry-run"],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60,
        )


class TheDetachedArmEndsItsOwnRecord(unittest.TestCase):
    """A detached stage is a copy of the same command without `--detach`, and
    that copy holds the workspace lock, waits, and ends the record -- the shape
    cmd/build and image/pgo.sh already use.

    Detaching *after* the spawn left nobody to write the exit, so a build that
    printed "stage 'webkit' done" and produced its slot read as `died -- no
    exit recorded` (2026-09-16), and the lock the spawn took died with the
    process that returned. Source-level, since driving it wants a container."""

    def setUp(self):
        self.body = func_body((REPO / "image" / "yocto.sh").read_text(), "yocto_build")

    def test_it_detaches_before_taking_the_lock_and_spawning(self):
        detach = self.body.index('if [ -n "$detach" ]')
        lock = self.body.index('hold_lock "ws-$ws"')
        spawn = self.body.index("yocto_spawn ")
        self.assertLess(detach, lock,
                        "the detached copy does not hold the workspace lock")
        self.assertLess(detach, spawn,
                        "it detaches after the spawn, so nothing ends the record")

    def test_the_detached_copy_is_the_same_command_without_detach(self):
        arm = self.body[self.body.index('if [ -n "$detach" ]'):]
        arm = arm[:arm.index("\n    fi\n")]
        self.assertIn("detach_run", arm)
        self.assertIn("sysimage build", arm)
        self.assertIn('[ "$_a" = --detach ] ||', arm,
                      "--detach is not stripped, so the copy would detach again")

    def test_every_path_out_of_the_wait_ends_the_record(self):
        """Once it waits, the record is ended whether the stage failed or not."""
        after = self.body[self.body.index("yocto_wait "):]
        self.assertGreaterEqual(after.count("task_end"), 2,
                                "a path out of the wait leaves the record open")


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
_task() {{  # one yocto record, as yocto_build writes it
    local d
    d=$(task_begin yocto target "$1" "wk sysimage build demo --stage $2 --stop" \
        "$(yocto_log "$1" "$2")" $YOCTO_STAGES)
    [ -z "${{3:-}}" ] || task_pid "$d" "$3"
    task_step_state "$d" "$(yocto_stage_index "$2")" running
}}
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
            env = dict(os.environ)
            env["WK_STORE"] = str(self.store)
            env["WK_ROOT"] = str(REPO)
            subprocess.run(
                ["bash", "-c", self.PRELUDE + f'_task {self.ws} image {proc.pid}'],
                cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30,
                check=True)
            cp = subprocess.run(
                ["bash", "-c", self.PRELUDE
                 + f'yocto_spawn {self.ws} image 8 20480 "image stage of {self.ws}"'],
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


class TheBranchCheckoutReadsTheMirror(WkTest):
    """A workspace's remotes are rewritten to read the machine's mirror, so
    the release-branch checkout `yocto_ensure_ws` makes is a local read and a
    branch the mirror does not carry is absent however reachable it is
    elsewhere. Measured 2026-09-16 on a machine whose mirror carried `main`:
    `git fetch origin webkitglib/2.52` answered "couldn't find remote ref"
    while the refusal blamed egress. A lane's branch is one wk_mirror_branches
    derives from the image configurations, so the remedy is the sync that
    brings the mirror up to this checkout."""

    PRELUDE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/image/yocto.sh"
yocto_ensure_image() {{ :; }}
t_info() {{ echo present; }}
podman() {{ echo "$WK_SDK_IMAGE"; }}
IMG_PROFILE=demo-profile
YOC_REMOTE=origin
'''

    def _refusal(self, on_branch, mirror_branches="main"):
        # t_exec answers the "what branch is it on" read and fails the fetch,
        # which is the shape a mirror without the branch produces.
        return bash(self.PRELUDE + f'''
t_exec() {{ case "$*" in *rev-parse*) echo {on_branch} ;; *) return 1 ;; esac; }}
WK_MIRROR_BRANCHES='{mirror_branches}' yocto_ensure_ws ws webkitglib/2.52
''', env={"WK_STORE": str(self.tmp / "store")})

    def test_it_names_the_mirror_and_the_sync_that_fills_it(self):
        cp = self._refusal("main")
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("reads this machine's mirror", out, out)
        self.assertIn("wk sync", out, out)
        self.assertNotIn("GitHub", out, "the fetch never reaches an upstream")

    def test_it_names_the_branches_the_mirror_is_declared_to_carry(self):
        cp = self._refusal("main", mirror_branches="main webkitglib/2.52")
        self.assertIn("main webkitglib/2.52 of", cp.stdout + cp.stderr)

    def test_a_workspace_already_on_the_branch_is_left_alone(self):
        cp = self._refusal("webkitglib/2.52")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestTheBudgetIsBookedAndEnforcedOnce(WkTest):
    """A stage is admitted against `yocto_stage_budget`'s MB and the watchdog
    inside the target has to enforce that same number. Two formulas -- the
    booking's, and guard.sh's jobs*WK_MB_PER_JOB -- killed a populate_sdk at
    15324MB that had been booked 17899MB (2026-09-17)."""

    def test_the_spawn_hands_the_booked_budget_to_the_build(self):
        text = (REPO / "image" / "yocto.sh").read_text()
        self.assertIn('--mem-budget "$stage_mb"', text,
                      "yocto_spawn books stage_mb but the build never sees it")

    def test_the_build_turns_it_into_the_guard_s_budget(self):
        text = (REPO / "image" / "yocto-build.sh").read_text()
        self.assertIn("--mem-budget) WK_MEM_BUDGET_MB=", text)
        self.assertIn("export WK_MEM_BUDGET_MB", text)

    def test_the_guard_prefers_it_over_the_jobs_product(self):
        text = (REPO / "build" / "guard.sh").read_text()
        i = text.index("_guard_watch()")
        body = text[i:i + 600]
        self.assertLess(body.index("WK_MEM_BUDGET_MB"),
                        body.index("WK_MB_PER_JOB"),
                        "the jobs product would win over the booked budget")


class TestTheLaneKeepsWhatItBuilt(WkTest):
    """cross-toolchain-helper hashes the target's section of
    Tools/yocto/targets.conf, the local.conf it names and its own source, and
    wipes WebKitBuild/CrossToolChains/<target> whole when that hash moves.
    The webkit stage checks out the slot's commit in the lane, and those
    three files differ between an image's release branch and a commit on
    main -- measured 2026-09-17, when a webkit stage took the lane's image
    and SDK with it (the workdir re-created at 23:33, nine minutes after that
    stage's last output) and then rebuilt the nativesdk stack inside a budget
    sized for one WebKit compile, where the memory watchdog killed it."""

    def test_the_helpers_wipe_on_change_is_off(self):
        self.assertRegex(YOCTO_BUILD.read_text(),
                         r"(?m)^export WEBKIT_CROSS_WIPE_ON_CHANGE=0$")


class TestRequireToolchain(WkTest):
    """require_toolchain: the webkit stage builds against an installed SDK or
    refuses, naming the stage that installs one.  Without it, build-webkit
    has the helper bitbake the whole nativesdk stack under the webkit
    stage's own memory budget."""

    def setUp(self):
        super().setUp()
        self.func = _lift("require_toolchain")

    def _sdk(self, workdir, configured=True, env_setup=True):
        d = workdir / "build" / "toolchain"
        d.mkdir(parents=True)
        if configured:
            (d / ".toolchain_path_configured").write_text(str(d))
        if env_setup:
            (d / "environment-setup-cortexa76-poky-linux").write_text("")

    def _run_in(self, workdir):
        return _run(f'WORKDIR={workdir}\n' + self.func, "require_toolchain")

    def test_ok_when_the_sdk_is_installed(self):
        with scratch_dir() as d:
            self._sdk(d)
            cp = self._run_in(d)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_refuses_when_no_toolchain_was_ever_built(self):
        with scratch_dir() as d:
            cp = self._run_in(d)
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("--stage toolchain", cp.stderr)

    def test_refuses_a_half_installed_sdk(self):
        """The helper's own two files, both of them: a marker with no
        environment-setup script beside it is an install that did not finish,
        and build-webkit cannot build against it."""
        for missing in ("configured", "env_setup"):
            with self.subTest(missing=missing), scratch_dir() as d:
                self._sdk(d, **{missing: False})
                cp = self._run_in(d)
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn("--stage toolchain", cp.stderr)

    def test_the_webkit_stage_asks_before_it_builds(self):
        text = YOCTO_BUILD.read_text()
        import re
        m = re.search(r"(?ms)^\s*webkit\)\n(.*?)\n\s*;;", text)
        self.assertIsNotNone(m)
        arm = m.group(1)
        self.assertLess(arm.find("require_toolchain"), arm.find("build-webkit"),
                        "require_toolchain must run before build-webkit is "
                        "reached, or the SDK is built under this stage's budget")


class TestRefreshGitIndexRepairsWhatAKillLeft(WkTest):
    """A killed stage can leave an index no git can read -- 0 bytes in this
    lane, measured 2026-09-21 -- and then every command in that checkout
    fails on it ("index file smaller than expected") and nothing in wk
    converged it. Every stage calls refresh_git_index, so whichever runs next
    is what repairs it."""

    def setUp(self):
        super().setUp()
        self.func = _lift("refresh_git_index")

    def _repo(self, d):
        def git(*a):
            subprocess.run(["git", "-C", str(d), *a], check=True,
                           capture_output=True, text=True)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (d / "tracked").write_text("one")
        git("add", "-A"); git("commit", "-qm", "one")

    def _readable(self, d):
        return subprocess.run(["git", "-C", str(d), "ls-files"],
                              capture_output=True, text=True).returncode == 0

    def test_a_truncated_index_is_rebuilt_from_head(self):
        with scratch_dir() as d:
            self._repo(d)
            (d / ".git" / "index").write_bytes(b"")
            self.assertFalse(self._readable(d), "a 0-byte index read fine")
            cp = _run(self.func, "refresh_git_index", str(d))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertTrue(self._readable(d), cp.stdout + cp.stderr)
            self.assertEqual(
                subprocess.run(["git", "-C", str(d), "ls-files"],
                               capture_output=True, text=True).stdout.split(),
                ["tracked"])

    def test_a_readable_index_is_left_alone(self):
        with scratch_dir() as d:
            self._repo(d)
            before = (d / ".git" / "index").stat().st_size
            cp = _run(self.func, "refresh_git_index", str(d))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertTrue(self._readable(d))
            self.assertEqual((d / ".git" / "index").stat().st_size, before)

    def test_a_directory_that_is_no_repository_is_a_noop(self):
        with scratch_dir() as d:
            cp = _run(self.func, "refresh_git_index", str(d))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestCheckoutSlotCommit(WkTest):
    """checkout_slot_commit: the lane's checkout converges onto the slot's
    commit however the last run left it.  A killed webkit stage left 4819
    modified and 741 untracked paths (measured 2026-09-17) and every later
    command refused with "your local changes would be overwritten by
    checkout"; nothing in wk repaired it."""

    def setUp(self):
        super().setUp()
        self.func = _lift("checkout_slot_commit") + _lift("refresh_git_index")

    def _repo(self, d):
        def git(*a):
            subprocess.run(["git", "-C", str(d), *a], check=True,
                           capture_output=True, text=True)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (d / ".gitignore").write_text("/WebKitBuild/\n")
        (d / "tracked").write_text("one")
        git("add", "-A"); git("commit", "-qm", "one")
        first = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        (d / "tracked").write_text("two")
        git("commit", "-aqm", "two")
        return first

    def _run_in(self, d, commit):
        script = (f'SRC={d}\nCOMMIT={commit}\nWK_MIRROR={d}\n'
                  + self.func + '\ncd "$SRC"\ncheckout_slot_commit')
        return subprocess.run(["bash", "-c", PRELUDE + script],
                              capture_output=True, text=True, timeout=30)

    def test_it_checks_out_the_commit(self):
        with scratch_dir() as d:
            first = self._repo(d)
            cp = self._run_in(d, first)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((d / "tracked").read_text(), "one")

    def test_it_converges_a_tree_a_killed_checkout_left(self):
        with scratch_dir() as d:
            first = self._repo(d)
            (d / "tracked").write_text("half-written by a killed checkout")
            (d / "LayoutTests").mkdir()
            (d / "LayoutTests" / "stray").write_text("untracked")
            cp = self._run_in(d, first)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((d / "tracked").read_text(), "one")
            self.assertFalse((d / "LayoutTests" / "stray").exists())

    def test_it_keeps_the_build_products_the_lane_holds(self):
        """`clean -fd`, never `-fdx`: WebKit's .gitignore carries
        /WebKitBuild/, where the image, the SDK and the slots live."""
        with scratch_dir() as d:
            first = self._repo(d)
            build = d / "WebKitBuild" / "CrossToolChains"
            build.mkdir(parents=True)
            (build / "sdk").write_text("hours of it")
            cp = self._run_in(d, first)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((build / "sdk").read_text(), "hours of it")
