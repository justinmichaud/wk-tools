"""An image workspace's commands are routed to the machine holding it: the
dispatcher asks cmd/sysimage and cmd/pi which workspace their arguments name
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

    def test_a_deploy_is_routed_by_the_workspace_that_built_the_slot(self):
        got, cp = hook("pi", "--wsname", "deploy", PROFILE, "rpi5", "--slot", "base")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, WS)

    def test_a_deploy_names_the_machine_the_spec_names(self):
        got, cp = hook("pi", "--wstarget", "deploy", PROFILE + "@moose", "rpi5")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, "moose")

    def test_a_collection_is_routed_by_the_workspace_it_writes_into(self):
        got, cp = hook("pi", "--wsname", "bench", "rpi5", "speedometer3",
                       "--slot", "base-instr", "--pgo", PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, WS)

    def test_a_measured_run_names_no_workspace(self):
        """It reads the slot off the board and touches no store, so it runs
        where it was typed."""
        got, cp = hook("pi", "--wsname", "bench", "rpi5", "speedometer3", "--slot", "base")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, "")

    def test_the_other_verbs_name_no_workspace(self):
        for args in (["setup", "rpi5"], ["boot-order", "rpi5", "usb-first"], ["helper", "rpi5"]):
            with self.subTest(verb=args[0]):
                got, cp = hook("pi", "--wsname", *args)
                self.assertEqual(cp.returncode, 0, cp.stderr)
                self.assertEqual(got, "")

    def test_the_declarations_say_both_are_routed(self):
        """The dispatcher refuses what a command does not declare, so the
        routing is the declaration and not a convention."""
        text = (REPO / "cmd" / "pi").read_text()
        self.assertIn("sub deploy where=workspace name=derived", text)
        self.assertIn("flag --pgo where=workspace name=derived", text)


class TestADeployRunsWhereItsWorkspaceIs(WkTest):
    """A deploy reads the slot's bytes and reaches the board, so it runs on the
    machine holding the workspace -- forwarded into the podman machine for a
    container workspace, handed to a peer for that peer's. Nothing refuses a macOS
    workstation for it: the podman machine is a tailnet node of its own
    (host/macos/machine.sh), so the half that can read the store is the half
    that can reach the board."""

    def test_nothing_refuses_a_workspace_for_being_in_the_podman_machine(self):
        for rel in ("cmd/pi", "lib/image.sh"):
            with self.subTest(file=rel):
                self.assertNotIn("image_lane_readable", (REPO / rel).read_text())

    def test_a_deploy_is_forwarded_like_any_other_workspace_command(self):
        """`forward=no` would keep it on the host half, which can reach the
        board and not read the store."""
        self.assertNotIn("forward=no", (REPO / "cmd" / "pi").read_text())


class TestASpecThatNamedAnotherMachine(WkTest):
    def test_the_two_commands_ask_it_before_they_touch_the_board(self):
        text = (REPO / "cmd" / "pi").read_text()
        self.assertIn('image_lane_here "$spec"', text)
        self.assertIn('image_lane_here "$PI_PGO_SPEC"', text)


class TestACollectionNamesItsWorkspaceNotAPath(WkTest):
    """`--pgo <profile>[@<machine>]`: the collection lands in the build
    directory the next phase reads it back through, and which workspace that is has
    to survive the hop to the machine holding it."""

    def test_the_help_and_the_usage_name_a_profile(self):
        text = (REPO / "cmd" / "pi").read_text()
        self.assertIn("--pgo <profile>[@<machine>] [--workspace LANE]", text)
        self.assertNotIn("[--pgo DIR]", text)

    def test_the_workspace_option_means_nothing_without_pgo(self):
        cp = run_here("pi", "bench", "rpi5", "speedometer3", "--workspace", WS, timeout=240)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--workspace names the lane", cp.stdout)


if __name__ == "__main__":
    unittest.main()
