"""One real integration test: create a container workspace, exercise the
read-only surface against it, then destroy it. Gated on a running podman
`wk` VM (this repo's own container target) -- it never starts one itself.

Run: python3 -m unittest tests.test_container_workspace -v
"""
import time
import unittest

from tests.support import WkTest, rand_suffix, requires_podman_vm, run


@requires_podman_vm()
class TestContainerWorkspaceLifecycle(WkTest):
    """`wk new` (container target) -> `wk ls`/`wk status`/`wk build --dry-run`
    -> `wk rm`, cleaning up in tearDown even if an assertion fails midway."""

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self._created = False

    def tearDown(self):
        if self._created:
            # cmd/rm's confirm() only skips the prompt with WK_YES=1 (no
            # terminal here declines by default, which would leave the
            # workspace behind).
            cp = run("rm", self.name, env={"WK_YES": "1"})
            # Best-effort: report but do not mask the real test failure.
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout + cp.stderr}")
        super().tearDown()

    def test_create_list_status_build_dry_run_remove(self):
        """wk new -> wk ls -> wk status --text --no-fleet -> wk build --dry-run -> wk rm"""
        t0 = time.time()
        cp = run("new", self.name, "--target", "container", timeout=600)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, f"wk new failed: {cp.stdout + cp.stderr}")
        created_s = time.time() - t0

        # It may still be finishing in the background; wait for it to settle.
        run("status", self.name, "--wait", "--timeout", "300")

        cp = run("ls")
        self.assertIn(self.name, cp.stdout, f"'wk ls' does not list {self.name}: {cp.stdout}")

        cp = run("status", self.name, "--text", "--no-fleet")
        self.assertIn(cp.returncode, (0, 2), f"'wk status {self.name} --text --no-fleet' exited {cp.returncode}: {cp.stdout + cp.stderr}")

        cp = run("build", self.name, "jsc-release", "--dry-run")
        self.assertEqual(cp.returncode, 0, f"'wk build {self.name} jsc-release --dry-run' failed: {cp.stdout + cp.stderr}")
        self.assertTrue(cp.stdout.strip(), "the dry run printed no command")

        cp = run("rm", self.name, env={"WK_YES": "1"})
        self.assertEqual(cp.returncode, 0, f"'wk rm {self.name}' failed: {cp.stdout + cp.stderr}")
        self._created = False

        cp = run("ls")
        self.assertNotIn(self.name, cp.stdout, f"'wk ls' still lists {self.name} after rm")

        total_s = time.time() - t0
        print(f"[timing] wk new: {created_s:.1f}s, total lifecycle: {total_s:.1f}s")


if __name__ == "__main__":
    unittest.main()
