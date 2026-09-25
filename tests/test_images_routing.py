"""An image workspace's commands are routed to the machine holding it: the
dispatcher asks cmd/sysimage which workspace their arguments name
(`--wsname`) and which machine a spec names (`--wstarget`), and `holds` and
`path` answer there.

Run: python3 -m unittest tests.test_images_routing -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest, run, run_here

PROFILE = "webkit-2.52-yocto-rpi5-64"
WS = "yocto-" + PROFILE
BUILDROOT_PROFILE = "wpewebkit-2.38-buildroot-rpi3-32"
WS_NOBODY_BUILT = WS + "-selftest"


def hook(*args):
    """The dispatcher's own questions, asked of the implementation directly."""
    cp = subprocess.run([str(REPO / "cmd" / args[0]), *args[1:]],
                        capture_output=True, text=True, timeout=120,
                        env={"WK_ROOT": str(REPO), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                             "HOME": "/tmp"})
    return cp.stdout.strip(), cp


class TestHoldsAnswersOnStdout(WkTest):
    """The verdict is printed, not returned as an exit status: a readonly
    command forwarded to a stopped podman machine reports that and exits 0,
    which as a status would read as `holds` and skip the build."""

    def _holds(self, *args):
        """Against a scratch store and an image workspace of this test's own, so nothing
        anyone built answers. `<builder>-<profile>-<arm>` is a workspace of its
        own, the shape the A/B's arms use."""
        cp = run_here("sysimage", "holds", *args, "--workspace", WS_NOBODY_BUILT,
                      timeout=240)
        return cp.stdout.strip(), cp

    def test_an_image_nothing_has_built_is_no(self):
        got, cp = self._holds(PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(got, "no")

    def test_a_toolchain_nothing_has_built_is_no(self):
        got, cp = self._holds(PROFILE, "--toolchain")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(got, "no")

    def test_the_toolchain_question_takes_nothing_else(self):
        got, cp = self._holds(PROFILE, "--toolchain", "--slot", "base",
                              "--commit", "a" * 40)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--toolchain", cp.stdout)

    def test_a_buildroot_profile_has_no_toolchain_to_ask_about(self):
        """buildroot builds its own toolchain inside its one build, so there
        is no separately installed SDK for a step to wait on."""
        got, cp = self._holds(BUILDROOT_PROFILE, "--toolchain")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)

    def test_a_slot_nothing_has_built_is_no(self):
        got, cp = self._holds(PROFILE, "--slot", "base", "--commit", "a" * 40)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(got, "no")

    def test_a_short_commit_is_refused_rather_than_answered_no(self):
        """A sha that cannot be the one a slot holds is an argument error, not
        evidence that the slot is missing."""
        got, cp = self._holds(PROFILE, "--slot", "base", "--commit", "abc")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("40-digit", cp.stdout)

    def test_commit_without_slot_is_refused(self):
        got, cp = self._holds(PROFILE, "--commit", "a" * 40)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--slot", cp.stdout)

    def test_an_unknown_profile_is_refused(self):
        got, cp = self._holds("nosuchprofile-at-all")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)

    def test_it_changes_nothing(self):
        """Declared readonly to the dispatcher, so it never starts the podman
        machine to answer a question about it."""
        cp = run("sysimage", "-h", timeout=120)
        self.assertIn("holds", cp.stdout)


class TestThePathQuestionIsRoutedToo(WkTest):
    """`wk sysimage path <profile>` is how `write --from <configuration>`
    finds the bytes: the workspace's machine answers in its own spelling, because on a macOS
    workstation the image is in the podman VM and the write runs out here."""

    def _path(self, *args):
        return run_here("sysimage", "path", *args, timeout=240,
                        env={"WK_STORE": str(self.tmp / "store")})

    def test_a_workspace_with_no_image_answers_nothing_and_says_so(self):
        cp = self._path(PROFILE)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(cp.stdout.strip(), "")

    def test_an_unknown_profile_is_refused(self):
        cp = self._path("nosuchprofile-at-all")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)

    def test_it_names_the_workspace_for_the_dispatcher(self):
        got, cp = hook("sysimage", "--wsname", "path", PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, WS)


class TestTheRoutingHooks(WkTest):
    """`name=derived` (the dispatcher): a command answers `--wsname` with the
    workspace its arguments act on and `--wstarget` with the target they name
    outright. Both are asked before anything runs."""

    def test_sysimage_holds_names_the_workspace(self):
        got, cp = hook("sysimage", "--wsname", "holds", PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, WS)

    def test_the_workspace_option_names_a_different_one(self):
        got, _ = hook("sysimage", "--wsname", "holds", PROFILE,
                      "--workspace", WS + "-base")
        self.assertEqual(got, WS + "-base")

    def test_the_declarations_say_both_are_routed(self):
        """The dispatcher refuses what a command does not declare, so the
        routing is the declaration and not a convention: a deploy, a board
        run and a collection name their lane."""
        self.assertIn("sub run,deploy name=required@2", (REPO / "cmd" / "bench").read_text())


class TestADeployRunsWhereItsWorkspaceIs(WkTest):
    """A deploy reads the slot's bytes and reaches the board, so it runs on the
    machine holding the workspace -- forwarded into the podman machine for a
    container workspace, handed to a peer for that peer's. Nothing refuses a macOS
    workstation for it: the podman machine is a tailnet node of its own
    (host/macos/machine.sh), so the half that can read the store is the half
    that can reach the board."""

    def test_nothing_refuses_a_workspace_for_being_in_the_podman_machine(self):
        self.assertNotIn("image_lane_readable", (REPO / "lib" / "image.sh").read_text())

    def test_a_deploy_is_forwarded_like_any_other_workspace_command(self):
        """`forward=no` would keep it on the host half, which can reach the
        board and not read the store."""
        self.assertNotIn("forward=no", (REPO / "cmd" / "bench").read_text())


if __name__ == "__main__":
    unittest.main()
