"""`wk sysimage` runs where the lane is.

A yocto or buildroot profile is built in a lane -- the workspace
`yocto-<profile>` or `buildroot-<profile>` -- so `build` and `webkit` are
workspace commands whose name is not a positional but the command's own
answer to `wk sysimage --wsname <args>` (`name=derived`, the dispatcher).
The dispatcher then routes them the way it routes every other workspace
command: into the podman VM for a container lane on macOS, over
`delegate_run` for a lane on another machine. A pmos or fetch profile has no
lane and stays `where=host`.

Covers: the two hooks' answers for every profile this checkout defines; the
dispatcher's reading of `name=derived` (no positional of its own, nothing
stripped from argv, no "no such workspace" refusal, the name vocabulary it
refuses outside); a lane on another machine delegated to that machine with
its arguments intact; and `wk sysimage ls`'s fleet walk and its columns.

Run: python3 -m unittest tests.test_sysimage_routing -v
"""
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, rand_suffix, run, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402

SYSIMAGE = REPO / "cmd" / "sysimage"


def _profiles():
    """Every configuration this checkout defines, with the builder its conf
    declares (empty for a built-in of image/profiles.sh, which has none)."""
    out = subprocess.run([str(REPO / "wk"), "sysimage", "--list"],
                         capture_output=True, text=True, cwd=str(REPO)).stdout
    names = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith(" ")]
    got = []
    for n in names:
        conf = REPO / "image" / "configs" / f"{n}.conf"
        m = re.search(r"(?m)^IMG_BUILDER=(\S+)", conf.read_text()) if conf.is_file() else None
        got.append((n, m.group(1) if m else ""))
    assert got, "'wk sysimage --list' named no configuration"
    return got


def _hook(*args):
    cp = subprocess.run([str(SYSIMAGE), *args], capture_output=True, text=True,
                        cwd=str(REPO))
    return cp.stdout.strip(), cp


class TestTheLaneAnswer(unittest.TestCase):
    """`wk sysimage --wsname` and `--where`: the two questions the dispatcher
    asks cmd/sysimage before it routes a build."""

    def test_every_profile_answers_for_its_builder(self):
        """a yocto or buildroot profile names its lane; nothing else does"""
        for profile, builder in _profiles():
            want = f"{builder}-{profile}" if builder in ("yocto", "buildroot") else ""
            for sub in ("build", "webkit"):
                got, cp = _hook("--wsname", sub, profile)
                self.assertEqual(cp.returncode, 0, cp.stderr)
                self.assertEqual(got, want, f"--wsname {sub} {profile}")

    def test_a_lane_is_a_workspace_and_anything_else_is_this_host(self):
        """`--where` answers workspace for a lane, host for a build with none"""
        for profile, builder in _profiles():
            want = "workspace" if builder in ("yocto", "buildroot") else "host"
            got, cp = _hook("--where", "build", profile)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(got, want, f"--where build {profile}")

    def test_the_workspace_option_names_the_lane_instead(self):
        """--workspace <name> is the lane the build follows"""
        profile = self._a_yocto_profile()
        for args in (["--workspace", "otherlane"], ["--workspace=otherlane"]):
            got, _ = _hook("--wsname", "build", profile, *args)
            self.assertEqual(got, "otherlane", args)

    def test_no_profile_and_no_such_profile_name_no_lane(self):
        """a question these arguments cannot answer is answered with nothing"""
        for args in (["build"], ["build", "nosuchprofile-" + rand_suffix()],
                     ["webkit"], ["write", "--from", "/tmp/x"]):
            got, cp = _hook("--wsname", *args)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(got, "", args)

    def _a_yocto_profile(self):
        for profile, builder in _profiles():
            if builder == "yocto":
                return profile
        self.skipTest("this checkout defines no yocto profile")


class TestTheDispatcherReadsDerived(unittest.TestCase):
    """`name=derived` in the dispatcher: what it declares, what it refuses,
    and the three places it is not the same as a positional name."""

    def _decl(self, impl="cmd/sysimage"):
        return D.Decl(REPO / impl)

    def test_build_and_webkit_derive_their_name_and_nothing_else_does(self):
        """cmd/sysimage declares name=derived for build and webkit alone"""
        d = self._decl()
        got = ["%s=%s" % (s, d.name_for([s])) for s in ("build", "webkit", "ls", "write", "disks", "rm", "flash")]
        self.assertEqual(got, ["build=derived", "webkit=derived", "ls=none", "write=none",
                               "disks=none", "rm=none", "flash=none"])

    def test_a_derived_name_is_no_positional_of_its_own(self):
        """name_slot: a derived name is in no argument slot, like none"""
        got = ["%s=%s" % (n, D.name_slot(n)) for n in ("derived", "none", "required", "optional", "optional@2")]
        self.assertEqual(got, ["derived=0", "none=0", "required=1", "optional=1", "optional@2=2"])

    def test_a_name_outside_the_vocabulary_is_refused(self):
        """a name= the dispatcher cannot read is refused by name"""
        impl = REPO / "cmd" / f"faux-{rand_suffix()}"
        impl.write_text("#!/usr/bin/env bash\n#\n# wk faux -- x\n"
                        "# wk: where=workspace name=inferred group=other\n")
        try:
            with self.assertRaises(D.DeclError) as cm:
                self._decl(impl.relative_to(REPO))
        finally:
            impl.unlink()
        self.assertIn("name=inferred is not one of", str(cm.exception))

    def _resolve(self, *, wstarget, derived, args):
        """resolve_target with its two collaborators answering as told: what
        the command names as the target, and what a located workspace
        resolves to."""
        inv = dispatch.Invocation("sysimage", self._decl(), args)
        with mock.patch.object(dispatch.Invocation, "named_target", lambda self: wstarget), \
                mock.patch.object(dispatch, "registry", lambda: mock.Mock(ws_target=lambda name: "target-of:" + name)), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WK_TARGET", None)
            return dispatch.resolve_target(inv, "derived", 0, "2", derived)

    def test_the_derived_name_decides_the_target(self):
        """resolve_target asks the command, not the argument list"""
        self.assertEqual(self._resolve(wstarget="", derived="yocto-demo", args=["build", "demo"]),
                         "target-of:yocto-demo")
        self.assertEqual(self._resolve(wstarget="", derived="", args=["build", "demo"]), "container")

    def test_a_target_the_command_names_outright_wins(self):
        """A lane spec that names its machine is not located: that machine
        holds the lane whether or not another one holds its name."""
        self.assertEqual(self._resolve(wstarget="moose", derived="yocto-demo", args=["build", "demo@moose"]),
                         "moose")


class TestALaneOnAnotherMachine(WkTest):
    """Where the build ends up. `fakebox` is a machine conf and a stubbed
    ssh, so a lane on it is driven there and nothing is built anywhere."""

    SSH_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_SSH_LOG"
exit 0
"""

    def setUp(self):
        super().setUp()
        self.profile = next((p for p, b in _profiles() if b == "yocto"), "")
        if not self.profile:
            self.skipTest("this checkout defines no yocto profile")
        self.lane = "nolane-" + rand_suffix()
        self.log = self.tmp / "ssh.log"
        self.log.write_text("")
        registry = self.tmp / "hosts"
        registry.mkdir()
        (registry / "fakebox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_HOST=fakebox\n"
            f"WK_REMOTE_ROOT={self.tmp}/box\n")
        (self.tmp / "here").mkdir()
        self.env = {"WK_TARGET_REGISTRY": str(registry),
                    "WK_STORE": str(self.tmp / "here"),
                    "WK_TEST_SSH_LOG": str(self.log)}

    def test_the_build_is_delegated_with_its_arguments_intact(self):
        """a lane on another machine: `wk sysimage build` runs over there"""
        with stub_path({"ssh": self.SSH_STUB}) as binp:
            env = dict(self.env, PATH=f"{binp}:{os.environ['PATH']}",
                       WK_TARGET="fakebox")
            cp = run("sysimage", "build", self.profile,
                     "--workspace", self.lane, "--detach", env=env)
        sent = self.log.read_text()
        self.assertEqual(cp.returncode, 0, cp.stdout)
        # sh_quote's spelling, which is how the far side is handed a word.
        self.assertIn(
            f"'sysimage' 'build' '{self.profile}' '--workspace' '{self.lane}' '--detach'",
            sent)

    def test_the_spec_names_the_machine_with_no_target_set(self):
        """`<profile>@<machine>`: the machine a new lane goes on, said in the
        argument rather than in WK_TARGET. Nothing here holds this lane, so
        without the machine half there would be nothing to route by."""
        with stub_path({"ssh": self.SSH_STUB}) as binp:
            env = dict(self.env, PATH=f"{binp}:{os.environ['PATH']}")
            self.assertNotIn("WK_TARGET", env)
            cp = run("sysimage", "build", f"{self.profile}@fakebox",
                     "--workspace", self.lane, "--detach", env=env)
        sent = self.log.read_text()
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(f"'sysimage' 'build' '{self.profile}@fakebox'", sent,
                      f"not delegated to fakebox: {sent!r}")

    def test_a_lane_that_does_not_exist_yet_is_not_refused(self):
        """the build creates its lane, so the dispatcher does not refuse it"""
        # `vm` is a target the yocto builder itself refuses, so this runs the
        # whole dispatcher and stops one line into the command.
        with stub_path({"tart": "exit 0\n"}) as binp:
            cp = run("sysimage", "build", self.profile, "--workspace", self.lane,
                     env=dict(self.env, WK_TARGET="vm",
                              PATH=f"{binp}:{os.environ['PATH']}"))
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertNotIn("no such workspace", out)
        self.assertIn("this target is 'vm'", out)


class TestTheFleetWalk(WkTest):
    """`wk sysimage ls` is the fleet's answer: this machine's store, then
    every machine it knows that answers for a store of its own."""

    SSH_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_SSH_LOG"
case "$*" in
  *sysimage*) printf '%-40s %-8s %-10s %-10s %-9s %-8s %s\\n' \\
      yocto-faraway rpi4 fakebox yocto ready 1.2G 2026-01-01T00:00:00Z
              printf '    /elsewhere/faraway.wic.xz\\n' ;;
esac
exit 0
"""
    QUIET = "exit 0\n"

    # A far side whose wk does not know the flag, which is every machine
    # in the fleet until this tree reaches it.
    REFUSING_SSH = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_SSH_LOG"
case "$*" in
  *sysimage*) echo "warning: unknown option: --continued" >&2; exit 1 ;;
esac
exit 0
"""

    def setUp(self):
        super().setUp()
        self.profile = next((p for p, b in _profiles() if b == "yocto"), "")
        if not self.profile:
            self.skipTest("this checkout defines no yocto profile")
        self.board = re.search(
            r"(?m)^IMG_MACHINE=(\S+)",
            (REPO / "image" / "configs" / f"{self.profile}.conf").read_text()).group(1)
        (self.tmp / "store" / "ws" / f"yocto-{self.profile}").mkdir(parents=True)
        self.log = self.tmp / "ssh.log"
        self.log.write_text("")
        self.env = {"WK_STORE": str(self.tmp / "store"),
                    "WK_TEST_SSH_LOG": str(self.log)}

    def _ls(self, *args, registry=None):
        with stub_path({"ssh": self.SSH_STUB, "podman": self.QUIET,
                        "tart": self.QUIET}) as binp:
            env = dict(self.env, PATH=f"{binp}:{os.environ['PATH']}")
            if registry:
                env["WK_TARGET_REGISTRY"] = str(registry)
            return run("sysimage", "ls", *args, env=env)

    def test_the_columns_name_the_board_and_the_machine_holding_the_lane(self):
        """BOARD is what the image is for, WHERE the machine holding it"""
        cp = self._ls()
        head = [l for l in cp.stdout.splitlines() if l.startswith("WORKSPACE")]
        self.assertEqual(len(head), 1, cp.stdout)
        self.assertEqual(head[0].split(),
                         ["WORKSPACE", "BOARD", "WHERE", "BUILDER", "STATE", "SIZE", "BUILT"])
        row = [l for l in cp.stdout.splitlines()
               if l.startswith(f"yocto-{self.profile} ")]
        self.assertEqual(len(row), 1, cp.stdout)
        # This machine's own store: the board, and nothing in WHERE.
        self.assertEqual(row[0].split()[:3], [f"yocto-{self.profile}", self.board, "yocto"])

    def test_a_machine_with_a_wk_of_its_own_answers_for_its_store(self):
        """a lane on another machine is in the table, named by that machine"""
        registry = self.tmp / "hosts"
        registry.mkdir()
        (registry / "fakebox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_HOST=fakebox\n"
            f"WK_REMOTE_ROOT={self.tmp}/box\n")
        cp = self._ls(registry=registry)
        self.assertIn("yocto-faraway", cp.stdout)
        self.assertIn("/elsewhere/faraway.wic.xz", cp.stdout)
        self.assertIn("2 images", cp.stdout + cp.stderr)
        # The label is the machine holding it, and the walk does not recurse.
        sent = self.log.read_text()
        self.assertIn("WK_ROW_LABEL='fakebox'", sent)
        self.assertIn("WK_NO_DELEGATE=1", sent)
        self.assertIn("'sysimage' 'ls' '--continued'", sent)

    def test_continued_is_rows_and_nothing_else(self):
        """the half another machine's walk asks for is rows alone"""
        cp = self._ls("--continued")
        self.assertNotIn("WORKSPACE", cp.stdout)
        self.assertNotIn("There is no image store", cp.stdout)
        self.assertIn(f"yocto-{self.profile}", cp.stdout)

    @unittest.skipUnless(sys.platform == "darwin",
                         "only on macOS does the container target answer for a machine of its own")
    def test_a_machine_that_is_not_running_is_reported_not_left_out(self):
        """a store this machine cannot read is named, not silently missing"""
        cp = self._ls()   # the stubbed podman answers for no running machine
        self.assertIn("is stopped, so the images in its", cp.stdout)

    def test_a_machine_that_refuses_the_walk_names_the_remedy(self):
        """A checkout that predates this listing answers with its own
        refusal, which read as though the whole command had failed. It is one
        machine's rows missing, and the remedy is that machine's tree."""
        registry = self.tmp / "hosts"
        registry.mkdir()
        (registry / "oldbox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_HOST=oldbox\n"
            f"WK_REMOTE_ROOT={self.tmp}/box\n")
        with stub_path({"ssh": self.REFUSING_SSH, "podman": self.QUIET,
                        "tart": self.QUIET}) as binp:
            cp = run("sysimage", "ls",
                     env=dict(self.env, PATH=f"{binp}:{os.environ['PATH']}",
                              WK_TARGET_REGISTRY=str(registry)))
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("'oldbox' did not answer the listing", out)
        self.assertIn("wk sync --tools oldbox", out, "the remedy is not named")
        self.assertNotIn("unknown option", out, "the far side's own refusal is passed through")
        self.assertIn(f"yocto-{self.profile}", out, "this machine's rows are still listed")

    def test_the_where_question_is_answered_for_both_halves(self):
        """the walk runs here; the answer to another's walk is the store"""
        self.assertEqual(_hook("--where", "ls")[0], "local")
        self.assertEqual(_hook("--where", "ls", "--continued")[0], "store")



class TestTheLaneSpec(unittest.TestCase):
    """`<profile>@<machine>`: which machine a lane is on, for one that nothing
    holds yet or that a second machine is to hold beside another's. The machine
    half never reaches the builder -- it is the dispatcher's target answer
    (`--wstarget`), and what is built is the profile."""

    def _src(self, snippet):
        return bash(f'. "{REPO}/cmd/sysimage" functions\n{snippet}\n')

    def test_the_machine_half_is_split_off_the_profile(self):
        cp = self._src('image_spec_profile webkit-2.52-yocto-rpi5-64@moose; echo; '
                       'image_spec_machine webkit-2.52-yocto-rpi5-64@moose')
        self.assertEqual(cp.stdout.split(), ["webkit-2.52-yocto-rpi5-64", "moose"],
                         cp.stdout + cp.stderr)

    def test_a_profile_without_one_names_no_machine(self):
        cp = self._src('image_spec_profile webkit-2.52-yocto-rpi5-64; echo; '
                       'echo "[$(image_spec_machine webkit-2.52-yocto-rpi5-64)]"')
        self.assertEqual(cp.stdout.split(), ["webkit-2.52-yocto-rpi5-64", "[]"],
                         cp.stdout + cp.stderr)

    def test_the_lane_name_is_the_same_either_way(self):
        """The workspace is named for the profile, so a machine half does not
        make a second lane of one profile on one machine."""
        plain = _hook("--wsname", "build", "webkit-2.52-yocto-rpi5-64")[0]
        spec = _hook("--wsname", "build", "webkit-2.52-yocto-rpi5-64@moose")[0]
        self.assertEqual(plain, "yocto-webkit-2.52-yocto-rpi5-64", plain)
        self.assertEqual(spec, plain, f"{spec!r} != {plain!r}")

    def test_the_target_is_the_machine_the_spec_names(self):
        out, cp = _hook("--wstarget", "build", "webkit-2.52-yocto-rpi5-64@moose")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(out, "moose", cp.stdout + cp.stderr)

    def test_no_machine_named_leaves_the_lane_to_be_located(self):
        """Empty: the dispatcher then resolves the lane by where it exists."""
        out, cp = _hook("--wstarget", "build", "webkit-2.52-yocto-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(out, "", f"named a target with no machine in the spec: {out!r}")

    def test_a_profile_with_no_lane_names_no_target(self):
        """A pmos or fetch profile is built by the host and has no lane, so
        there is no machine to answer for even if one is typed."""
        out, cp = _hook("--wstarget", "build", "bridge-pinephone@moose")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(out, "", f"a lane-less profile answered a target: {out!r}")

    def test_this_machine_resolves_to_its_own_default_target(self):
        """Named with this machine's own name, the lane is a local one: the
        answer is the target its workspaces live on, not the hostname."""
        cp = self._src('wk_machine_name() { echo here; }\n'
                       'default_target() { echo container; }\n'
                       'image_spec_target here')
        self.assertEqual(cp.stdout.strip(), "container", cp.stdout + cp.stderr)


class TestTheProfileBehindALane(unittest.TestCase):
    """`image_lane_profile` (lib/image.sh): a lane is named <builder>-<profile>
    and may carry an arm's suffix of its own, so the profile is recovered by
    matching the configurations this checkout defines rather than by stripping
    a prefix."""

    def _profile_of(self, ws):
        cp = bash(f'. "{REPO}/cmd/sysimage" functions\n'
                  f'image_lane_profile {ws} || echo "REFUSED"\n')
        return cp.stdout.strip()

    def test_a_plain_lane_names_its_profile(self):
        self.assertEqual(self._profile_of("yocto-webkit-2.52-yocto-rpi5-64"),
                         "webkit-2.52-yocto-rpi5-64")
        self.assertEqual(self._profile_of("buildroot-wpewebkit-2.38-buildroot-rpi3-32"),
                         "wpewebkit-2.38-buildroot-rpi3-32")

    def test_a_suffixed_lane_names_the_same_profile(self):
        """Two lanes of one profile -- one per arm of an A/B -- are one
        profile's images to `ls` and to `write --from`."""
        for suffix in ("base", "pr1725", "arm-2"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    self._profile_of(f"yocto-webkit-2.52-yocto-rpi5-64-{suffix}"),
                    "webkit-2.52-yocto-rpi5-64")

    def test_the_longest_configuration_wins(self):
        """`webkit-2.52-yocto-rpi4-32` and `-rpi4-64` are both configurations;
        a shorter one that is a prefix of the lane must not claim it."""
        every = [n for n, _ in _profiles()]
        self.assertIn("webkit-2.52-yocto-rpi4-64", every)
        self.assertEqual(self._profile_of("yocto-webkit-2.52-yocto-rpi4-64"),
                         "webkit-2.52-yocto-rpi4-64")

    def test_a_workspace_that_is_no_lane_is_refused(self):
        for ws in ("wk-test-abc", "yocto-not-a-configuration", "buildroot-nope"):
            with self.subTest(ws=ws):
                self.assertEqual(self._profile_of(ws), "REFUSED",
                                 f"{ws} was read as a lane")


if __name__ == "__main__":
    unittest.main()
