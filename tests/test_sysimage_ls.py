"""`wk sysimage`'s read side and `rm`, in process (lib/wk/sysimage/).

Covers: each builder's outputs are what `ls`, `holds`, `path` and `rm` read,
recomputed on every read (`unit sysimage.builders_conform`, the read half);
`ls`'s rows, states and footnotes, and its fleet walk; `holds` and its
refusals; `path`; the routing answers the dispatcher asks for; and the live
row `sysimage.build[vm]`.

Run: python3 tests/run.py -k tests.test_sysimage_ls
"""
import contextlib
import io
import json
import os
import sys
import types
import unittest
import unittest.mock

from tests.support import NO_REGISTRY, REPO, WkTest, owed, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import act, images, record  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import cli, ls  # noqa: E402

YOCTO = "webkit-2.52-yocto-rpi3-32"          # a profile-guided release
BUILDROOT = "wpewebkit-2.38-buildroot-rpi3-32"
YWS, BWS = "yocto-" + YOCTO, "buildroot-" + BUILDROOT
SHA = "a" * 40


class FakeTarget:
    def __init__(self, name, here=False, side="answering", rc=0, out=""):
        self.name, self.here, self.side, self.rc, self.out = name, here, side, rc, out
        self.asked = []

    def probe(self):
        return self.side, ""

    def is_here(self):
        return self.here

    def wk(self, *args, env=None, quiet=False):
        self.asked.append((args, dict(env or {})))
        return self.rc, self.out


class FakeRegistry:
    """What cli.Sysimage and ls.Listing ask of targets.Registry."""

    def __init__(self, store_dir, targets=(), machine=None, env=None):
        # A blind fleet unless a test names its own: host_profiles()'s mac-volume check reads
        # machines/<IMG_MACHINE>.conf through this env, and this repo's real one names a real Mac's real volume.
        self.env = dict({"WK_MACHINES_DIR": NO_REGISTRY}, **(env or {}), WK_STORE=str(store_dir))
        self.env.pop("WK_ROW_LABEL", None)
        self.store = Store(self.env)
        self.machine = machine or Local()
        self.targets = {t.name: t for t in targets}

    def walk(self):
        return list(self.targets)

    def load(self, name):
        if name not in self.targets:
            raise LookupError("unknown target '%s'" % name)
        return self.targets[name]

    def default(self):
        return "container"


def sysimage(store_dir, targets=(), machine=None, building=()):
    s = cli.Sysimage(FakeRegistry(store_dir, targets, machine), clock=None)
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


def slot(store, ws, name, commit=SHA, config="wpe-cross-pgo-use", **extra):
    d = images.slot_dir(ws, name, {"WK_STORE": str(store)})
    os.makedirs(d, exist_ok=True)
    doc = dict(slot=name, commit=commit, build_config=config, built_at="2026-09-01T00:00:00Z", **extra)
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
        """mac-volume and guest have no workspace, so their marker is read through `ls.builder_outputs`
        (5.32, 5.34) -- `cli.Sysimage.builder_outputs`'s one implementation -- rather than
        `ls.BUILDERS`'s workspace globs. pmos and fetch stay owed below."""
        named = {images.load(n)["IMG_BUILDER"] for n in images.names()}
        reachable = {b.kind for b in ls.BUILDERS} | set(ls.HOST_BUILDERS)
        self.assertLessEqual(named - {"pmos", "fetch"}, reachable)

    @owed("pmos and fetch images are left on the host or the pmos build host, which no read reaches yet (5.17, 5.20)")
    def test_pmos_and_fetch_images_have_no_reader_yet(self):
        named = {images.load(n)["IMG_BUILDER"] for n in images.names()}
        reachable = {b.kind for b in ls.BUILDERS} | set(ls.HOST_BUILDERS)
        self.assertLessEqual(named, reachable)


class TestTheListing(WkTest):
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
        self.assertIn("a build is running here -- 'wk logs %s' follows it" % YWS, cp.out)

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


class TestTheFleetWalk(WkTest):
    """This store's rows first, then each target whose machine answers for a store of its own,
    asked through its own wk with the label it is to print and no walk of its own."""

    def rows(self, d, targets, warned):
        return ls.Listing(FakeRegistry(d, targets), "", "here", lambda ws: False, warned.append).rows()

    def test_each_answering_machine_adds_its_rows_after_this_stores(self):
        with scratch_dir() as d:
            yocto_image(d)
            far = FakeTarget("fakebox", out="yocto-faraway  rpi4  fakebox\r\n    /elsewhere/faraway.wic.xz\n")
            vm = FakeTarget("container", here=True, out="")
            rows = self.rows(d, [far, vm], [])
        self.assertTrue(rows[0].startswith(YWS))
        self.assertEqual(rows[-2:], ["yocto-faraway  rpi4  fakebox", "    /elsewhere/faraway.wic.xz"])
        args, env = far.asked[0]
        self.assertEqual(args, ("sysimage", "ls", "--continued"))
        self.assertEqual((env["WK_ROW_LABEL"], env["WK_NO_DELEGATE"]), ("fakebox", "1"))
        self.assertEqual(vm.asked[0][1]["WK_ROW_LABEL"], "here", "a machine behind this one is labelled as this one")

    def test_a_stopped_machine_is_named_not_left_out(self):
        with scratch_dir() as d:
            warned = []
            self.assertEqual(self.rows(d, [FakeTarget("container", here=True, side="stopped")], warned), [])
        self.assertIn("is stopped, so the images in its", warned[0])

    def test_one_that_refuses_the_walk_names_the_remedy(self):
        with scratch_dir() as d:
            warned = []
            self.rows(d, [FakeTarget("oldbox", rc=2, out="")], warned)
        self.assertIn("'oldbox' did not answer the listing", warned[0])
        self.assertIn("wk sync --tools oldbox", warned[0])

    def test_one_with_no_store_of_its_own_is_not_asked(self):
        with scratch_dir() as d:
            quiet = [FakeTarget("c", side="none"), FakeTarget("far", side="unreachable")]
            warned = []
            self.assertEqual(self.rows(d, quiet, warned), [])
        self.assertEqual((warned, [t.asked for t in quiet]), ([], [[], []]))

    def test_an_unknown_target_is_named(self):
        with scratch_dir() as d:
            warned = []
            listing = ls.Listing(FakeRegistry(d), "", "here", lambda ws: False, warned.append)
            listing.reg.walk = lambda: ["ghost"]
            self.assertEqual(listing.rows(), [])
        self.assertIn("unknown target 'ghost'", warned[0])


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
            slot(d, YWS, "base", config="wpe-cross-pgo-collect")
            self.assertEqual(self.holds(d, YOCTO, None, "base", SHA, None, False).out, "no\n")
            self.assertEqual(self.holds(d, YOCTO, None, "base", SHA, "wpe-cross-pgo-collect", False).out, "yes\n")
            slot(d, YWS, "base")
            self.assertEqual(self.holds(d, YOCTO, None, "base", SHA, None, False).out, "yes\n")
            self.assertEqual(self.holds(d, YOCTO, None, "base", "b" * 40, None, False).out, "no\n")

    def test_a_slot_elsewhere_is_the_commit_alone(self):
        with scratch_dir() as d:
            slot(d, BWS, "base", config="")
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
        self.assertEqual(cli.lane(["path", YOCTO, "--workspace", "arm-b"]), "arm-b")

    def test_a_spec_naming_a_machine_is_its_target_and_this_machine_its_default(self):
        with scratch_dir() as d:
            reg = FakeRegistry(d)
            self.assertEqual(cli.wstarget(["holds", YOCTO + "@moose"], reg), "moose")
            self.assertEqual(cli.wstarget(["holds", YOCTO + "@" + record.machine_name(reg.env)], reg), "container")
            self.assertEqual(cli.wstarget(["holds", YOCTO], reg), "")
            self.assertEqual(cli.wstarget(["holds", "bridge-pinephone@moose"], reg), "")


class TestTheFlashTombstone(unittest.TestCase):
    def test_it_names_disks_write_and_boot(self):
        cp = ran(cli.Sysimage.flash, "rpi3")
        self.assertEqual(cp.rc, 1)
        for words in ("wk sysimage disks rpi3", "--disk rpi3:<device>", "wk boot rpi3"):
            self.assertIn(words, cp.err)


class TestAnImageBuiltInAGuestReachesTheHost(unittest.TestCase):
    @requires_machine("benchvm")
    def test_build_in_a_guest(self):
        """`live sysimage.build[vm]`: an image build run from a Tart guest, and the image written from the
        host. Needs a guest with the tools and hours of build; the reach half is `ls` naming it."""
        self.skipTest("owed: a guest image build is hours long and has not been run from this suite")


if __name__ == "__main__":
    unittest.main()
