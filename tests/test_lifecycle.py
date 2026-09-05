"""Lifecycle-command tests: `wk enter` across a target (defect 1), the
per-workspace `wk stop`/`wk start` (defect 2), `wk rm`'s multiple-name form
(defect 3), a real `wk new` leaving no workspace->target registry entry
behind (there is no registry any more -- lib/target.sh derives it), and the
static --explain/-h checks that need no workspace at all.

Run: python3 -m unittest tests.test_lifecycle -v
Run just the static checks (no podman VM needed): they self-select, since
the one integration test class self-skips without a running `wk` podman
machine (see requires_podman_vm in tests/support.py).
"""
import os
import unittest
from pathlib import Path

from tests.support import WkTest, rand_suffix, requires_podman_vm, run


class TestExplainStatic(unittest.TestCase):
    """No workspace needed: these only read each command's own header."""

    def test_rm_explain_shows_multiple_names(self):
        cp = run("rm", "--explain")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(
            "wk rm <name>...",
            cp.stdout,
            "cmd/rm's synopsis should read '<name>...' now that it takes more than one",
        )

    def test_stop_h_mentions_workspace_form(self):
        cp = run("stop", "-h")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(
            "wk stop <workspace>",
            cp.stdout,
            "'wk stop -h' should document the per-workspace form",
        )

    def test_start_h_mentions_workspace_form(self):
        cp = run("start", "-h")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(
            "wk start <workspace>",
            cp.stdout,
            "'wk start -h' should document the per-workspace form",
        )

    def test_enter_explain_runs_on_line(self):
        cp = run("enter", "--explain")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("runs on:", cp.stdout)
        self.assertIn(
            "workspace's target",
            cp.stdout,
            "'wk enter --explain' should say it runs on the workspace's own target",
        )

    def test_enter_dash_dash_command_reaches_the_command(self):
        """`wk enter <ws> -- <cmd>` must not hand '--' to the workspace as
        the first word of its command (the bug this fixes: cmd/enter never
        stripped it, so the workspace shell reported 'command not found:
        --'). No workspace is needed to see this: an absent one still fails
        past argument parsing, at 'no such workspace'."""
        # WK_TARGET: an absent name resolves to the container target, which a
        # macOS host forwards into the podman VM; the parsing under test is this
        # dispatcher's and needs no machine at all.
        cp = run("enter", "zz-does-not-exist-xyz", "--", "true",
                 env={"WK_TARGET": "vm"})
        self.assertNotIn(
            "--: command not found",
            cp.stdout,
            "'--' reached the workspace as a command word instead of being stripped",
        )
        self.assertIn("no such workspace", cp.stdout)


@requires_podman_vm()
class TestContainerLifecycle(WkTest):
    """One real container workspace, exercised end to end:
    wk new -> wk stop <ws> -> wk ls (not running) -> wk start <ws> ->
    wk ls (running again) -> the state-sharing mount t_create adds ->
    wk rm <ws> <bogus-second-name>, cleaning up in tearDown either way."""

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
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout}")
        super().tearDown()

    def _state_line(self, ls_output):
        return next((l for l in ls_output.splitlines() if self.name in l), "")

    def test_stop_start_shared_mount_and_multi_rm(self):
        cp = run("new", self.name, "--target", "container", timeout=600)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, f"wk new failed: {cp.stdout}")

        # No registry: which target a workspace lives on is derived from the
        # store (or, mid-creation, the status file), never recorded in a
        # per-workspace file (lib/target.sh, ws_target). This machine may
        # still carry the directory itself, as rubble from before this --
        # 'wk gc' is the migration off it (cmd/gc) -- so what is asserted is
        # narrower: no file *for this workspace* appears in it.
        state_dir = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))) / "wk"
        registry_entry = state_dir / "targets" / self.name
        self.assertFalse(
            registry_entry.exists(),
            f"'wk new' should not leave a workspace->target registry entry behind: {registry_entry}",
        )

        # It may still be finishing in the background; wait for it to settle.
        run("status", self.name, "--wait", "--timeout", "300")

        # --- wk stop <workspace>: only this one, and only its container ---
        cp = run("stop", self.name)
        self.assertEqual(cp.returncode, 0, f"'wk stop {self.name}' failed: {cp.stdout}")

        cp = run("ls")
        self.assertIn(self.name, cp.stdout, f"'wk ls' does not list {self.name} after 'wk stop': {cp.stdout}")
        line = self._state_line(cp.stdout)
        self.assertNotIn(
            "running", line,
            f"'{self.name}' still shows running after 'wk stop {self.name}': {line!r}",
        )

        # --- wk start <workspace>: brings just this one back ---
        cp = run("start", self.name)
        self.assertEqual(cp.returncode, 0, f"'wk start {self.name}' failed: {cp.stdout}")

        cp = run("ls")
        line = self._state_line(cp.stdout)
        self.assertIn(
            "running", line,
            f"'{self.name}' does not show running after 'wk start {self.name}': {line!r}",
        )

        # --- the shared state mount (targets/container.sh, t_create): a
        # `wk build` run from *inside* the workspace (the only way Claude
        # ever builds, CLAUDE.md) writes build.status where the host's own
        # `wk status` can see it, at the same absolute path on both sides. ---
        cp = run("enter", self.name, "--", "test", "-d", f"/var/lib/wk/ws/{self.name}")
        self.assertEqual(
            cp.returncode, 0,
            f"/var/lib/wk/ws/{self.name} is not mounted in the container -- "
            f"build.status written by an in-workspace 'wk build' would be invisible "
            f"to 'wk status' run on the host: {cp.stdout}",
        )

        # --- wk rm <real> <bogus>: removes the real one, fails overall ---
        bogus = f"wk-test-{rand_suffix()}-nonexistent"
        cp = run("rm", self.name, bogus, env={"WK_YES": "1"})
        self.assertNotEqual(
            cp.returncode, 0,
            f"'wk rm {self.name} {bogus}' should exit nonzero: the second name does not exist",
        )
        self.assertIn("no such workspace", cp.stdout)

        cp = run("ls")
        self.assertNotIn(
            self.name, cp.stdout,
            f"'wk rm {self.name} {bogus}' left {self.name} behind: {cp.stdout}",
        )
        self._created = False


if __name__ == "__main__":
    unittest.main()
