"""A lane is the image workspace, and a command that acts on one is routed to
the machine holding it.

Three questions, each answered in one place:

  * what a lane is called, and what it is called after -- image_lane_ws /
    image_lane_profile / image_slot_dir / image_pgo_dir (lib/image.sh), all
    keyed on the lane's name so a profile may have one lane per arm;
  * which machine holds it, and what the scheduler serialises it by --
    image_lane_machine / image_lane_resource;
  * whether it already holds what a step would build -- `wk sysimage holds`,
    which the dispatcher routes to that machine like the build itself.

Nothing here builds, boots or reaches a board: the libraries are sourced and
asked, and the two routing hooks are the ones the dispatcher calls.

Run: python3 -m unittest tests.test_lane_routing -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, run

LIBS = "\n".join('. "%s/%s"' % (REPO, f) for f in (
    "lib/common.sh", "lib/store.sh", "lib/target.sh", "lib/image.sh",
    "image/profiles.sh"))

PROFILE = "webkit-2.52-yocto-rpi5-64"
LANE = "yocto-" + PROFILE
BUILDROOT_PROFILE = "wpewebkit-2.38-buildroot-rpi3-32"
BUILDROOT_LANE = "buildroot-" + BUILDROOT_PROFILE


def ask(snippet, env=None):
    return bash(LIBS + "\n" + snippet + "\n", env=env)


def hook(*args):
    """The dispatcher's own questions, asked of the implementation directly."""
    cp = subprocess.run([str(REPO / "cmd" / args[0]), *args[1:]],
                        capture_output=True, text=True, timeout=120,
                        env={"WK_ROOT": str(REPO), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                             "HOME": "/tmp"})
    return cp.stdout.strip(), cp


class TestWhatALaneIsCalled(WkTest):
    def test_a_lane_is_its_builder_and_its_profile(self):
        self.assertEqual(ask("image_lane_ws %s" % PROFILE).stdout.strip(), LANE)
        self.assertEqual(ask("image_lane_ws %s" % BUILDROOT_PROFILE).stdout.strip(),
                         BUILDROOT_LANE)

    def test_the_machine_half_of_a_spec_is_not_part_of_the_name(self):
        """`<profile>@<machine>` routes the command; what is built is the
        profile, and the lane is that machine's copy of the same name."""
        self.assertEqual(ask("image_lane_ws %s@moose" % PROFILE).stdout.strip(), LANE)

    def test_a_builder_with_no_lane_names_none(self):
        cp = ask("image_lane_ws bridge-pinephone; echo '[end]'")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "[end]")

    def test_a_name_no_configuration_answers_to_is_not_an_error(self):
        """image_profile_load dies on one, and that die is the subshell's: a
        question with no lane in it is answered with nothing."""
        cp = ask("image_lane_ws nosuchprofile-at-all; echo '[end]'")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "[end]")


class TestWhatALaneIsCalledAfter(WkTest):
    def test_the_profile_comes_back_out_of_the_lanes_name(self):
        self.assertEqual(ask("image_lane_profile %s" % LANE).stdout.strip(), PROFILE)
        self.assertEqual(ask("image_lane_profile %s-base" % LANE).stdout.strip(), PROFILE)

    def test_a_lane_that_is_not_one_is_refused(self):
        cp = ask("image_lane_profile jsc-release")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)


class TestEveryPathUnderALaneIsKeyedOnIt(WkTest):
    """The point of the whole vocabulary: one profile may have two lanes, and
    a slot, a collection and an image in one of them are not the other's."""

    def _dir(self, call):
        cp = ask(call, env={"WK_STORE": "/store"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_a_yocto_slot_lives_under_its_own_lane(self):
        self.assertEqual(self._dir("image_slot_dir %s base" % LANE),
                         "/store/ws/%s/build/wk-slots/base" % LANE)
        self.assertEqual(self._dir("image_slot_dir %s-pr base" % LANE),
                         "/store/ws/%s-pr/build/wk-slots/base" % LANE)

    def test_a_buildroot_slot_keeps_the_profile_inside_the_lane(self):
        """buildroot's own output tree is named for the configuration, so the
        lane's name and the profile's are both needed and both derived."""
        self.assertEqual(
            self._dir("image_slot_dir %s-pr base" % BUILDROOT_LANE),
            "/store/ws/%s-pr/build/buildroot/%s/output/wk-slots/base"
            % (BUILDROOT_LANE, BUILDROOT_PROFILE))

    def test_a_collection_lives_under_its_own_lane(self):
        self.assertEqual(self._dir("image_pgo_dir %s-base pr" % LANE),
                         "/store/ws/%s-base/build/wk-pgo/pr" % LANE)

    def test_a_lane_that_is_not_one_has_no_slot_directory(self):
        cp = ask("image_slot_dir jsc-release base")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)


class TestTheInstrumentedSlotsName(WkTest):
    """image/pgo.sh builds and deploys it and cmd/pi collects into the measured
    slot's directory, so the suffix is named once."""

    def test_it_is_the_measured_slots_name_and_the_suffix(self):
        self.assertEqual(ask("image_pgo_instr_slot base").stdout.strip(), "base-instr")

    def test_and_the_measured_slot_comes_back_out_of_it(self):
        self.assertEqual(ask("image_pgo_measured_slot base-instr").stdout.strip(), "base")

    def test_a_slot_that_is_not_instrumented_is_left_alone(self):
        self.assertEqual(ask("image_pgo_measured_slot base").stdout.strip(), "base")


class TestWhichMachineHoldsIt(WkTest):
    def test_a_spec_that_names_one_is_believed(self):
        self.assertEqual(ask("image_lane_machine %s moose" % LANE).stdout.strip(), "moose")

    def test_otherwise_the_workspace_record_answers(self):
        self.assertEqual(
            ask("ws_target() { echo elsewhere; }\nimage_lane_machine %s ''" % LANE
                ).stdout.strip(), "elsewhere")

    def test_a_target_of_this_machines_own_is_this_machine(self):
        """`container` and `local` are targets, not machines: a lane on either
        is held by the machine the command is running on."""
        for target in ("container", "local"):
            with self.subTest(target=target):
                got = ask("wk_machine_name() { echo here; }\n"
                          "ws_target() { echo %s; }\n"
                          "image_lane_machine %s ''" % (target, LANE)).stdout.strip()
                self.assertEqual(got, "here")


class TestWhatTheSchedulerSerialisesABuildBy(WkTest):
    """One machine builds one thing at a time, so the machine is the whole
    resource -- two of its lanes build in turn and two machines build at once
    (lib/sched.py, and build_admit for the builds no one plan knows about)."""

    def test_the_resource_is_the_machine(self):
        self.assertEqual(ask("image_build_resource moose").stdout.strip(), "machine:moose")

    def test_two_machines_are_two_resources(self):
        self.assertNotEqual(ask("image_build_resource moose").stdout.strip(),
                            ask("image_build_resource tolken").stdout.strip())

    def test_the_lane_is_no_part_of_it(self):
        """A resource per lane would let one machine build two at once."""
        self.assertNotIn(LANE, ask("image_build_resource moose").stdout)


class TestTheDonePredicateIsRouted(WkTest):
    """A step asks the machine that would do the work, not the one that drew
    the graph: a slot built on moose reads as missing here, and building it
    again is the bug that answer causes."""

    def test_it_is_a_wk_command_a_person_could_type(self):
        got = ask("image_holds_predicate %s@moose %s --slot base --commit %s"
                  % (PROFILE, LANE, "a" * 40)).stdout.strip()
        self.assertEqual(
            got,
            '[ "$(wk sysimage holds %s@moose --workspace %s --slot base --commit %s)"'
            ' = yes ]' % (PROFILE, LANE, "a" * 40))

    def test_the_image_question_takes_no_slot(self):
        got = ask("image_holds_predicate %s %s" % (PROFILE, LANE)).stdout.strip()
        self.assertEqual(
            got, '[ "$(wk sysimage holds %s --workspace %s)" = yes ]' % (PROFILE, LANE))


class TestHoldsAnswersOnStdout(WkTest):
    """The verdict is printed, not returned as an exit status: a readonly
    command forwarded to a stopped podman machine reports that and exits 0,
    which as a status would read as `holds` and skip the build."""

    def _holds(self, *args):
        cp = run("sysimage", "holds", *args, timeout=240)
        return cp.stdout.strip(), cp

    def test_an_image_nothing_has_built_is_no(self):
        got, cp = self._holds(PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(got, "no")

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


class TestTheRoutingHooks(WkTest):
    """`name=derived` (the dispatcher): a command answers `--wsname` with the
    workspace its arguments act on and `--wstarget` with the target they name
    outright. Both are asked before anything runs."""

    def test_sysimage_holds_names_the_lane(self):
        got, cp = hook("sysimage", "--wsname", "holds", PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, LANE)

    def test_the_workspace_option_names_a_different_lane(self):
        got, _ = hook("sysimage", "--wsname", "holds", PROFILE,
                      "--workspace", LANE + "-base")
        self.assertEqual(got, LANE + "-base")

    def test_a_deploy_is_routed_by_the_lane_that_built_the_slot(self):
        got, cp = hook("pi", "--wsname", "deploy", PROFILE, "rpi5", "--slot", "base")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, LANE)

    def test_a_deploy_names_the_machine_the_spec_names(self):
        got, cp = hook("pi", "--wstarget", "deploy", PROFILE + "@moose", "rpi5")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, "moose")

    def test_a_collection_is_routed_by_the_lane_it_writes_into(self):
        got, cp = hook("pi", "--wsname", "bench", "rpi5", "speedometer3",
                       "--slot", "base-instr", "--pgo", PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, LANE)

    def test_a_measured_run_names_no_lane(self):
        """It reads the slot off the board and touches no store, so it runs
        where it was typed."""
        got, cp = hook("pi", "--wsname", "bench", "rpi5", "speedometer3", "--slot", "base")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(got, "")

    def test_the_other_verbs_name_no_lane(self):
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


class TestADeployRunsWhereTheLaneIs(WkTest):
    """A deploy reads the slot's bytes and reaches the board, so it runs on the
    machine holding the lane -- forwarded into the podman machine for a
    container lane, handed to a peer for that peer's. Nothing refuses a macOS
    workstation for it: the podman machine is a tailnet node of its own
    (host/macos/machine.sh), so the half that can read the store is the half
    that can reach the board."""

    def test_nothing_refuses_a_lane_for_being_in_the_podman_machine(self):
        for rel in ("cmd/pi", "lib/image.sh"):
            with self.subTest(file=rel):
                self.assertNotIn("image_lane_readable", (REPO / rel).read_text())

    def test_a_deploy_is_forwarded_like_any_other_workspace_command(self):
        """`forward=no` would keep it on the host half, which can reach the
        board and not read the store."""
        self.assertNotIn("forward=no", (REPO / "cmd" / "pi").read_text())


class TestASpecThatNamedAnotherMachine(WkTest):
    """The command should have been handed over. Still running here means
    nothing there answered for a store of its own -- a typo in the machine
    half, or a machine with no wk."""

    def test_it_refuses_and_names_the_setup(self):
        cp = bash(LIBS + "\nwk_machine_name() { echo here; }\n"
                  "image_lane_here %s@elsewhere\n" % PROFILE)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("elsewhere", out)
        self.assertIn("wk remote setup elsewhere", out)

    def test_this_machines_own_name_is_not_another_machine(self):
        cp = bash(LIBS + "\nwk_machine_name() { echo here; }\n"
                  "image_lane_here %s@here\necho PASS\n" % PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("PASS", cp.stdout)

    def test_a_spec_with_no_machine_half_names_no_other_machine(self):
        cp = bash(LIBS + "\nimage_lane_here %s\necho PASS\n" % PROFILE)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("PASS", cp.stdout)

    def test_the_two_commands_ask_it_before_they_touch_the_board(self):
        text = (REPO / "cmd" / "pi").read_text()
        self.assertIn('image_lane_here "$spec"', text)
        self.assertIn('image_lane_here "$PI_PGO_SPEC"', text)


class TestACollectionNamesItsLaneNotAPath(WkTest):
    """`--pgo <profile>[@<machine>]`: the collection lands in the build
    directory the next phase reads it back through, and which lane that is has
    to survive the hop to the machine holding it."""

    def test_the_help_and_the_usage_name_a_profile(self):
        text = (REPO / "cmd" / "pi").read_text()
        self.assertIn("--pgo <profile>[@<machine>] [--workspace LANE]", text)
        self.assertNotIn("[--pgo DIR]", text)

    def test_the_workspace_option_means_nothing_without_pgo(self):
        cp = run("pi", "bench", "rpi5", "speedometer3", "--workspace", LANE, timeout=240)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--workspace names the lane", cp.stdout)


if __name__ == "__main__":
    unittest.main()
