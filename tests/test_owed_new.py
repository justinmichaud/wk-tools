"""`wk new` over a workspace with no `base-id` remakes it, owed by
docs/HANDOFF-test-runner.md: "catches: 'already exists' answered about a
half-made thing". The decision lives in `ws_state` (lib/target.sh): a
target that needs a base snapshot (`t_needs_base`) but whose workspace
directory has no `base-id` file reports `creating`, not `present` -- so
`wk new` resumes/remakes it instead of refusing "already exists". Driven
directly against the decision function with `t_info`/`t_needs_base`/
`wk_ws_dir` stubbed: no podman, no VM, no real driver.

Run: python3 -m unittest tests.test_owed_new -v
"""
import re
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, scratch_dir

NEW = REPO / "cmd" / "new"
CONTAINER = REPO / "targets" / "container.sh"


class TestWorkspaceWithNoBaseIdIsStillCreating(WkTest):
    def _state(self, env_present, needs_base, has_base_id):
        with scratch_dir() as ws:
            if has_base_id:
                (ws / "base-id").write_text("some-snapshot-id\n")
            cp = self.bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
t_info() {{ echo {"present" if env_present else "absent"}; }}
t_needs_base() {{ return {0 if needs_base else 1}; }}
wk_ws_dir() {{ echo "{ws}"; }}
ws_state somews
''')
        return cp

    def test_a_workspace_needing_a_base_with_none_recorded_is_creating(self):
        """The environment exists (a container/vm was made) but the base-id
        pin was never written -- an interrupted `wk new`, not a finished
        one. 'already exists' would be wrong here: `wk new` has to resume
        it, not refuse it."""
        cp = self._state(env_present=True, needs_base=True, has_base_id=False)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "creating", cp.stdout + cp.stderr)

    def test_the_same_workspace_once_base_id_is_recorded_is_present(self):
        """Contrast: once base-id exists, the same environment reports
        present -- so 'creating' above is not ws_state being broken outright."""
        cp = self._state(env_present=True, needs_base=True, has_base_id=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "present", cp.stdout + cp.stderr)

    def test_a_target_with_no_base_at_all_never_needs_the_file(self):
        """A target that does not use base snapshots (t_needs_base false --
        e.g. a remote target, whose base is a repository, not a pinned
        snapshot) is present without one."""
        cp = self._state(env_present=True, needs_base=False, has_base_id=False)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "present", cp.stdout + cp.stderr)


class TestNewRefreshesTheSdkBeforeCreating(unittest.TestCase):
    """The maintainer's defect ("wk sdk should be updated for each new
    container"): a container workspace's image tag comes from the
    webkit-container-sdk checkout on disk (get_sdk_version), so `wk new`
    has to refresh that checkout before handing off to wkdev-create --
    source-level, since driving a real container create needs podman and
    the SDK checkout this suite never touches (tests/support.py's rule)."""

    def test_the_container_branch_calls_t_sdk_refresh_before_t_create(self):
        src = NEW.read_text()
        refresh_at = src.index("t_sdk_refresh")
        create_at = src.index('t_create "$NAME" "$BASE" "$ARCH"')
        self.assertLess(
            refresh_at, create_at,
            "cmd/new must refresh the SDK checkout before t_create, so a new "
            "container is never built from a stale one",
        )

    def test_the_call_is_guarded_to_the_container_target_only(self):
        """A vm or remote target has no webkit-container-sdk checkout at
        all, so the call must not run unconditionally."""
        src = NEW.read_text()
        line = next(l for l in src.splitlines() if "t_sdk_refresh" in l)
        self.assertIn("WK_TARGET_KIND", line)
        self.assertIn("container", line)

    def test_it_runs_under_the_sdk_lock_and_outside_the_store_lock(self):
        """The refresh is a network fetch, a `git reset --hard` and a patch
        over the one SDK checkout the machine shares: two `wk new`s serialise
        on it, and holding the store lock across it would make every `wk sync`
        on the machine wait out a fetch it has nothing to do with."""
        src = NEW.read_text()
        line = next(l for l in src.splitlines() if "t_sdk_refresh" in l)
        self.assertIn("with_lock sdk -- t_sdk_refresh", line)
        self.assertLess(src.index("t_sdk_refresh"), src.index("hold_lock store"),
                        "the refresh has to happen before the store lock is taken")

    def test_t_sdk_refresh_is_defined_once_in_the_container_driver(self):
        """One function, called from cmd/new: not a second copy of the
        fetch-and-reset logic re-typed into the target driver."""
        src = CONTAINER.read_text()
        defs = re.findall(r"^t_sdk_refresh\(\)\s*\{", src, re.M)
        self.assertEqual(len(defs), 1, src)
        self.assertIn("container/sdk-refresh.sh", src)


class TestNewKillStopsTheCreation(unittest.TestCase):
    """A creation outlives its terminal, so its record names the command that
    stops it (lib/task.sh, `task_begin`), and that command is `wk new <name>
    --kill`: the same `job_stop` every other kind of job is stopped with. What
    it leaves is half-made, which is `wk rm`'s to clear."""

    def test_the_record_names_the_command_that_stops_it(self):
        self.assertIn('task_begin new here "$NAME" "wk new $NAME --kill"',
                      NEW.read_text())

    def test_kill_goes_through_the_one_stop_implementation(self):
        self.assertIn('job_stop "$NAME" new', NEW.read_text())

    def test_the_help_block_says_what_it_does(self):
        cp = run("new", "-h")
        self.assertIn("--kill", cp.stdout)

    def test_it_takes_nothing_that_belongs_to_a_creation(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill", "--no-wait")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("stops the creation already running", cp.stdout)

    def test_with_no_creation_running_it_says_so_and_ends_well(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no new is running", cp.stdout)

if __name__ == "__main__":
    unittest.main()
