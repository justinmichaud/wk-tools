"""`wk sysimage`'s read side in process (lib/wk/sysimage/): each builder's outputs are what `ls`, `holds` and
`path` read, recomputed on every read; `ls`'s rows and fleet walk; the routing answers the dispatcher asks for."""
import contextlib
import io
import json
import os
import sys
import types
import unittest
import unittest.mock

from tests.fakes import FakeRegistry, FakeDriver
from tests.support import NO_REGISTRY, REPO, WkTest, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import act, images, record  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import cli, ls, pmos  # noqa: E402

YOCTO = "webkit-2.52-yocto-rpi3-32"          # a profile-guided release
BUILDROOT = "wpewebkit-2.38-buildroot-rpi3-32"
YWS, BWS = "yocto-" + YOCTO, "buildroot-" + BUILDROOT
SHA = "a" * 40


def registry(store_dir, drivers=(), machine=None):
    """What cli.Sysimage and ls.Listing ask of the registry."""
    # A blind fleet: host_profiles()'s mac-volume check reads machines/<IMG_MACHINE>.conf through this env, and this
    # repo's real one names a real Mac's real volume. WK_IN_VM=1 keeps the fetch builder's cache in this store too.
    env = {"WK_MACHINES_DIR": NO_REGISTRY, "WK_IN_VM": "1", "WK_STORE": str(store_dir)}
    ts = {t.name: t for t in drivers}
    return FakeRegistry(env, machine or Local(), lambda n, e: ts[n], names=list(ts), default=lambda: "container")


def sysimage(store_dir, drivers=(), machine=None, building=()):
    s = cli.Sysimage(registry(store_dir, drivers, machine), clock=None)
    s.building = lambda ws: ws in building
    return s


def ran(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(*args)
        except act.Refused as e:
            rc = e.status
    return types.SimpleNamespace(rc=rc, out=out.getvalue(), err=err.getvalue())


def image(store, ws, rel, size=1024):
    p = store / "ws" / ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def yocto_image(store, ws=YWS):
    return image(store, ws, "build/CrossToolChains/rpi3-32bits-mesa/build/image/core.wic.xz")


def slot(store, ws, name, commit=SHA, preset="wpe-cross-pgo-use", **extra):
    d = images.slot_dir(ws, name, {"WK_STORE": str(store)})
    os.makedirs(d, exist_ok=True)
    doc = dict(slot=name, commit=commit, build_preset=preset, built_at="2026-09-01T00:00:00Z", **extra)
    with open(os.path.join(d, "slot.json"), "w") as f:
        json.dump(doc, f)
    return d


class TestTheBuildersConform(unittest.TestCase):
    """`unit sysimage.builders_conform`, the read half: every builder that builds in a workspace
    names where its images land, and that is the only place any read looks."""

    def test_every_workspace_builder_has_outputs(self):
        self.assertEqual(sorted(b.kind for b in ls.BUILDERS), sorted(images.WS_BUILDERS))

    def test_each_builder_finds_what_it_leaves_and_nothing_beside_it(self):
        with scratch_dir() as d:
            y = yocto_image(d)
            image(d, YWS, "build/CrossToolChains/rpi3-32bits-mesa/build/image/core.wic")   # not the artifact
            b = image(d, BWS, "build/buildroot/%s/output/images/sdcard.img" % BUILDROOT)
            m = Local()
            self.assertEqual(ls.outputs(m, Store({"WK_STORE": str(d)}), YWS), [str(y)])
            self.assertEqual(ls.outputs(m, Store({"WK_STORE": str(d)}), BWS), [str(b)])

    def test_the_same_through_a_fake_machine(self):
        f = Fake()
        f._set_file("/st/ws/%s/build/CrossToolChains/t/build/image/a.wic.xz" % YWS, "x")
        f._set_file("/st/ws/%s/build/.hidden.wic.xz" % YWS, "x")
        self.assertEqual(ls.scan(f, Store({"WK_STORE": "/st"})),
                         [ls.Image("yocto", YWS, "/st/ws/%s/build/CrossToolChains/t/build/image/a.wic.xz" % YWS)])

    def test_the_mac_volume_finds_what_it_leaves_and_nothing_beside_it(self):
        """`sysimage.builders_conform[mac-volume]`: built is an installed volume carrying the board's marker."""
        from wk.sysimage import macvolume
        f = Fake()
        v = macvolume.MacVolume(f, images.load("perf-macos-tolken"), {"HOME": "/nonexistent"}, root=REPO)
        f._set_file("/Volumes/%s/etc/wk-image" % v.volume, "id=x\n")
        self.assertEqual(v.outputs(), [], "a marker on a volume with no macOS on it")
        f._set_file("/Volumes/%s/System/Library/CoreServices/SystemVersion.plist" % v.volume, "")
        self.assertEqual(v.outputs(), ["/Volumes/%s/etc/wk-image" % v.volume])
        self.assertIn("mac-volume", cli.BUILDERS)

    def test_every_builder_a_profile_names_has_outputs(self):
        named = {images.load(n)["IMG_BUILDER"] for n in images.names()}
        reachable = {b.kind for b in ls.BUILDERS} | set(ls.HOST_BUILDERS)
        self.assertLessEqual(named, reachable)


class TestPmosAndFetchImagesHaveAReader(unittest.TestCase):
    """A fetch image is left in this host's own cache; a pmos image on its build host."""

    def test_a_fetched_image_is_found_in_this_host_s_cache(self):
        with scratch_dir() as d:
            reg = registry(d)
            p = images.load("recovery-pinephone", reg.env)
            cached = os.path.join(str(d), "cache", "images", "pine64-pinephone.img.xz")
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            open(cached, "w").close()
            self.assertEqual(ls.builder_outputs(reg, None, p), [cached])

    def test_no_fetched_image_yet_is_no_marker(self):
        with scratch_dir() as d:
            reg = registry(d)
            p = images.load("recovery-pinephone", reg.env)
            self.assertEqual(ls.builder_outputs(reg, None, p), [])

    def test_a_pmos_image_is_asked_of_its_build_host(self):
        with scratch_dir() as d:
            reg = registry(d)
            reg.env["WK_PMOS_ROOT"] = "/p"
            p = images.load("bridge-pinephone", reg.env)
            machine = Fake("rpi5")

            def answer(argv, fake):
                text = argv[2]
                if "ls -1t" in text:
                    return Result(0, "bridge-pinephone-20260101T000000Z\n")
                if "result" in text or "disk.wic.xz" in text or text == "true":
                    return Result(0)
                return Result(1)
            machine.react(("sh", "-c"), answer)
            with unittest.mock.patch.object(pmos, "ssh_machine", return_value=machine) as m:
                self.assertEqual(ls.builder_outputs(reg, None, p),
                                 ["/p/out/bridge-pinephone-20260101T000000Z/disk.wic.xz"])
            self.assertEqual(m.call_args[0][-1], "rpi5")

    def test_a_pmos_build_host_that_does_not_answer_is_unknown_not_no_image(self):
        with scratch_dir() as d:
            reg = registry(d)
            reg.env["WK_PMOS_ROOT"] = "/p"
            p = images.load("bridge-pinephone", reg.env)
            machine = Fake("rpi5")
            machine.answer(("sh", "-c"), rc=255)
            with unittest.mock.patch.object(pmos, "ssh_machine", return_value=machine):
                with self.assertRaises(ls.Unknown) as cm:
                    ls.builder_outputs(reg, None, p)
                self.assertIn("does not answer", str(cm.exception))
                warned = []
                rows = ls.Listing(reg, "", "here", lambda ws: False, warned.append).host_rows()
        self.assertEqual(rows, [])
        self.assertIn("cannot tell whether bridge-pinephone is built: rpi5, the build host, does not answer over ssh", warned)


class TestAnUnreadableImageIsNoAnswer(unittest.TestCase):
    def test_holds_refuses_rather_than_saying_no(self):
        with scratch_dir() as d, unittest.mock.patch.object(pmos, "outputs", side_effect=ls.Unknown("rpi5 does not answer")):
            with self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()) as err, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                sysimage(d).holds("bridge-pinephone", None, None, None, None, False)
        self.assertIn("cannot tell whether bridge-pinephone is built: rpi5 does not answer", err.getvalue())
        self.assertEqual(out.getvalue(), "")


class NoPmosHost(WkTest):
    """A listing here asks no pmos build host."""

    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.object(pmos, "outputs", return_value=[])
        p.start()
        self.addCleanup(p.stop)


class TestTheListing(NoPmosHost):
    """`wk sysimage ls` on this store: a row per image, its state, and what to do next."""

    def test_the_columns_name_the_board_and_leave_where_blank_here(self):
        with scratch_dir() as d:
            p = yocto_image(d)
            cp = ran(sysimage(d).ls, False)
        lines = cp.out.splitlines()
        self.assertEqual(lines[0].split(), list(ls.HEADER))
        self.assertEqual(lines[1].split()[:5], [YWS, "rpi3", "yocto", "ready", "1.0K"])
        self.assertEqual(lines[2].strip(), str(p))
        self.assertIn("1 image, each in the workspace that built it", cp.err)

    def test_a_workspace_with_no_image_still_has_a_row(self):
        """A yocto image stage removes the last image as it rebuilds, so this is normal for hours."""
        with scratch_dir() as d:
            (d / "ws" / YWS / "build").mkdir(parents=True)
            cp = ran(sysimage(d).ls, False)
        self.assertIn("none", cp.out.splitlines()[1])
        self.assertIn("no image here yet", cp.out)
        self.assertIn("'wk sysimage build %s' builds one" % YOCTO, cp.out)

    def test_a_running_build_is_stated_on_the_row(self):
        with scratch_dir() as d:
            (d / "ws" / YWS / "build").mkdir(parents=True)
            cp = ran(sysimage(d, building={YWS}).ls, False)
        self.assertIn("building", cp.out.splitlines()[1])
        self.assertIn("a build is running here -- 'wk status %s --log' follows it" % YWS, cp.out)

    def test_a_workspace_whose_build_state_cannot_be_read_says_unknown(self):
        with scratch_dir() as d:
            yocto_image(d)
            s = sysimage(d)
            s.building = lambda ws: None
            cp = ran(s.ls, False)
        self.assertEqual(cp.out.splitlines()[1].split()[3], "unknown")

    def test_an_image_present_while_a_build_runs_says_both(self):
        with scratch_dir() as d:
            yocto_image(d)
            cp = ran(sysimage(d, building={YWS}).ls, False)
        self.assertIn("building", cp.out.splitlines()[1])
        self.assertIn("these bytes are the previous image", cp.out)

    def test_each_slot_is_listed_under_its_image(self):
        with scratch_dir() as d:
            yocto_image(d)
            where = slot(d, YWS, "base")
            cp = ran(sysimage(d).ls, False)
        self.assertIn("    slot base         %s  wpe-cross-pgo-use  built 2026-09-01T00:00:00Z  (%s)"
                      % (SHA[:12], where), cp.out)

    def test_nothing_to_list_prints_no_header(self):
        with scratch_dir() as d:
            cp = ran(sysimage(d).ls, False)
        self.assertEqual(cp.out, "")
        self.assertIn("no workspace on any machine this one knows has built an image", cp.err)

    def test_continued_is_rows_and_nothing_else(self):
        with scratch_dir() as d:
            yocto_image(d)
            cp = ran(sysimage(d).ls, True)
        self.assertNotIn("WORKSPACE", cp.out)
        self.assertIn(YWS, cp.out)
        self.assertEqual(cp.err, "")

    def test_a_row_label_names_this_machine_to_the_one_that_asked(self):
        with scratch_dir() as d:
            yocto_image(d)
            s = sysimage(d)
            s.reg.env["WK_ROW_LABEL"] = "moose"
            cp = ran(s.ls, True)
        self.assertEqual(cp.out.split()[:3], [YWS, "rpi3", "moose"])

    def test_human_bytes_is_common_sh_s(self):
        self.assertEqual([ls.human_bytes(n) for n in (3, 1024, 1536, 10 * 1024, 5 * 1024 ** 3)],
                         ["3B", "1.0K", "1.5K", "10K", "5.0G"])


class TestTheFleetWalk(NoPmosHost):
    """This store's rows first, then each place whose machine answers for a store of its own,
    asked through its own wk with the label it is to print and no walk of its own."""

    def rows(self, d, drivers, warned):
        return ls.Listing(registry(d, drivers), "", "here", lambda ws: False, warned.append).rows()

    def test_each_answering_machine_adds_its_rows_after_this_stores(self):
        with scratch_dir() as d:
            yocto_image(d)
            far = FakeDriver("fakebox", out="yocto-faraway  rpi4  fakebox\r\n    /elsewhere/faraway.wic.xz\n")
            vm = FakeDriver("container", here=True, out="")
            rows = self.rows(d, [far, vm], [])
        self.assertTrue(rows[0].startswith(YWS))
        self.assertEqual(rows[-2:], ["yocto-faraway  rpi4  fakebox", "    /elsewhere/faraway.wic.xz"])
        args, env, _ = far.asked[0]
        self.assertEqual(args, ("sysimage", "ls", "--continued"))
        self.assertEqual((env["WK_ROW_LABEL"], env["WK_NO_DELEGATE"]), ("fakebox", "1"))
        self.assertEqual(vm.asked[0][1]["WK_ROW_LABEL"], "here", "a machine behind this one is labelled as this one")

    def test_a_stopped_machine_is_named_not_left_out(self):
        with scratch_dir() as d:
            warned = []
            self.assertEqual(self.rows(d, [FakeDriver("container", here=True, side="stopped")], warned), [])
        self.assertIn("is stopped, so the images in its", warned[0])

    def test_one_that_refuses_the_walk_names_the_remedy(self):
        with scratch_dir() as d:
            warned = []
            self.rows(d, [FakeDriver("oldbox", rc=2, out="")], warned)
        self.assertIn("'oldbox' did not answer the listing", warned[0])
        self.assertIn("wk sync --tools oldbox", warned[0])

    def test_one_with_no_store_of_its_own_is_not_asked(self):
        with scratch_dir() as d:
            quiet = [FakeDriver("c", side="none"), FakeDriver("far", side="unreachable")]
            warned = []
            self.assertEqual(self.rows(d, quiet, warned), [])
        self.assertEqual((warned, [t.asked for t in quiet]), ([], [[], []]))

    def test_an_unknown_target_is_named(self):
        with scratch_dir() as d:
            warned = []
            listing = ls.Listing(registry(d), "", "here", lambda ws: False, warned.append)
            listing.reg.walk = lambda: ["ghost"]
            self.assertEqual(listing.rows(), [])
        self.assertIn("unknown place 'ghost'", warned[0])


class TestHolds(WkTest):
    """`holds` prints yes or no: the done predicate a step asks of the machine holding the workspace."""

    def holds(self, d, *args):
        return ran(sysimage(d).holds, *args)

    def test_the_image_itself(self):
        with scratch_dir() as d:
            self.assertEqual(self.holds(d, YOCTO, None, None, None, None, False).out, "no\n")
            yocto_image(d)
            self.assertEqual(self.holds(d, YOCTO, None, None, None, None, False).out, "yes\n")
            self.assertEqual(self.holds(d, YOCTO + "@moose", "other-ws", None, None, None, False).out, "no\n")

    def test_the_toolchain(self):
        with scratch_dir() as d:
            tc = d / "ws" / YWS / "build" / "CrossToolChains" / "rpi3-32bits-mesa" / "build" / "toolchain"
            tc.mkdir(parents=True)
            self.assertEqual(self.holds(d, YOCTO, None, None, None, None, True).out, "no\n")
            (tc / ".toolchain_path_configured").write_text("")
            (tc / "environment-setup-cortexa7").write_text("")
            self.assertEqual(self.holds(d, YOCTO, None, None, None, None, True).out, "yes\n")

    def test_a_slot_on_a_profile_guided_release_is_its_measured_build(self):
        with scratch_dir() as d:
            slot(d, YWS, "base", preset="wpe-cross-pgo-collect")
            self.assertEqual(self.holds(d, YOCTO, None, "base", SHA, None, False).out, "no\n")
            self.assertEqual(self.holds(d, YOCTO, None, "base", SHA, "wpe-cross-pgo-collect", False).out, "yes\n")
            slot(d, YWS, "base")
            self.assertEqual(self.holds(d, YOCTO, None, "base", SHA, None, False).out, "yes\n")
            self.assertEqual(self.holds(d, YOCTO, None, "base", "b" * 40, None, False).out, "no\n")

    def test_a_slot_elsewhere_is_the_commit_alone(self):
        with scratch_dir() as d:
            slot(d, BWS, "base", preset="")
            self.assertEqual(self.holds(d, BUILDROOT, None, "base", SHA, None, False).out, "yes\n")
            self.assertEqual(self.holds(d, BUILDROOT, None, "nope", SHA, None, False).out, "no\n")

    def test_the_refusals_name_what_is_wrong(self):
        with scratch_dir() as d:
            for args, words in (((YOCTO, None, "base", "abc", None, False), "full 40-digit sha"),
                                ((YOCTO, None, None, SHA, None, False), "they need --slot"),
                                ((YOCTO, None, "base", None, None, True), "takes nothing else"),
                                ((BUILDROOT, None, None, None, None, True), "no cross toolchain"),
                                ((YOCTO, None, "-x", SHA, None, False), "is not usable"),
                                (("nosuch", None, None, None, None, False), "unknown profile 'nosuch'"),
                                (("", None, None, None, None, False), "usage: wk sysimage holds")):
                with self.subTest(args=args):
                    cp = self.holds(d, *args)
                    self.assertEqual((cp.rc, cp.out), (1, ""))
                    self.assertIn(words, cp.err)


class TestPath(WkTest):
    def test_the_image_the_workspace_holds_or_nothing(self):
        with scratch_dir() as d:
            s = sysimage(d)
            self.assertEqual((ran(s.path, YOCTO, None).rc, ran(s.path, YOCTO, None).out), (1, ""))
            p = yocto_image(d)
            self.assertEqual(ran(s.path, YOCTO, None).out, "%s\n" % p)
            self.assertEqual(ran(s.path, "bridge-pinephone", None).rc, 1, "a host-built profile has no workspace")
            self.assertIn("usage: wk sysimage path", ran(s.path, "", None).err)


class TestPathAndHoldsReachTheMacVolumeMarker(WkTest):
    """`unit sysimage.builders_conform[mac-volume]`: a builder with no workspace still answers `path` and `holds`,
    read straight off the machine it builds on."""

    def volume(self, f, env):
        from wk.sysimage import macvolume
        return macvolume.MacVolume(f, images.load("perf-macos-tolken"), env)

    def test_path_is_the_marker_once_installed(self):
        with scratch_dir() as d:
            f = Fake()
            s = sysimage(d, machine=f)
            v = self.volume(f, s.reg.env)
            self.assertEqual(ran(s.path, "perf-macos-tolken", None).rc, 1)
            f._set_file(v.s + "/System/Library/CoreServices/SystemVersion.plist", "")
            f._set_file(v.s + "/etc/wk-image", "id=x\n")
            self.assertEqual(ran(s.path, "perf-macos-tolken", None).out, "%s/etc/wk-image\n" % v.s)

    def test_ls_lists_it_only_once_installed(self):
        """`unit sysimage.builders_conform[mac-volume]`: `ls` has no workspace to anchor a placeholder row at,
        so the profile is silent until `builder_outputs` finds its marker."""
        with scratch_dir() as d:
            f = Fake()
            s = sysimage(d, machine=f)
            v = self.volume(f, s.reg.env)
            self.assertNotIn("perf-macos-tolken", ran(s.ls, False).out)
            f._set_file(v.s + "/System/Library/CoreServices/SystemVersion.plist", "")
            f._set_file(v.s + "/etc/wk-image", "id=x\n")
            cp = ran(s.ls, False)
        line = next(l for l in cp.out.splitlines() if l.startswith("perf-macos-tolken"))
        self.assertEqual(line.split()[:5], ["perf-macos-tolken", "mbp", "mac-volume", "ready", "-"])
        self.assertIn("    " + v.s + "/etc/wk-image", cp.out)

    def test_holds_the_image_answers_yes_once_installed(self):
        with scratch_dir() as d:
            f = Fake()
            s = sysimage(d, machine=f)
            v = self.volume(f, s.reg.env)
            self.assertEqual(ran(s.holds, "perf-macos-tolken", None, None, None, None, False).out, "no\n")
            f._set_file(v.s + "/System/Library/CoreServices/SystemVersion.plist", "")
            f._set_file(v.s + "/etc/wk-image", "id=x\n")
            self.assertEqual(ran(s.holds, "perf-macos-tolken", None, None, None, None, False).out, "yes\n")


class TestTheRoutingAnswers(unittest.TestCase):
    """The dispatcher's questions, answered before anything else runs."""

    def test_ls_walks_from_here_and_answers_a_walk_from_the_store(self):
        self.assertEqual(cli.where(["ls"]), "local")
        self.assertEqual(cli.where(["list", "--continued"]), "store")

    def test_a_verb_naming_an_image_workspace_runs_where_it_is(self):
        self.assertEqual(cli.where(["path", YOCTO]), "workspace")
        self.assertEqual(cli.where(["path", "bridge-pinephone"]), "host")
        self.assertEqual(cli.wsname(["path", YOCTO, "--workspace", "arm-b"]), "arm-b")

    def test_a_spec_naming_a_machine_is_its_target_and_this_machine_its_default(self):
        with scratch_dir() as d:
            reg = registry(d)
            self.assertEqual(cli.wsplace(["holds", YOCTO + "@moose"], reg), "moose")
            self.assertEqual(cli.wsplace(["holds", YOCTO + "@" + record.machine_name(reg.env)], reg), "container")
            self.assertEqual(cli.wsplace(["holds", YOCTO], reg), "")
            self.assertEqual(cli.wsplace(["holds", "bridge-pinephone@moose"], reg), "")




class TestAnImageBuiltInAGuestReachesTheHost(unittest.TestCase):
    @requires_machine("benchvm")
    def test_build_in_a_guest(self):
        """`live sysimage.build[vm]`: an image build run from a Tart guest, and the image written from the
        host. Needs a guest with the tools and hours of build; the reach half is `ls` naming it."""
        self.skipTest("owed: a guest image build is hours long and has not been run from this suite")


if __name__ == "__main__":
    unittest.main()
