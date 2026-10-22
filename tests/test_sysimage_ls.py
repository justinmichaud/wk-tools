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

from tests.fakes import FakeDriver, FakeRegistry
from tests.support import NO_REGISTRY, REPO, WkTest, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import act, images, record  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import cli, ls, macvolume  # noqa: E402

YOCTO = "webkit-2.52-yocto-rpi3-32"          # a profile-guided release
BUILDROOT = "wpewebkit-2.38-buildroot-rpi3-32"
YWS, BWS = "yocto-" + YOCTO, "buildroot-" + BUILDROOT
SHA = "a" * 40


def registry(store_dir, drivers=(), machine=None):
    # A blind fleet: host_presets()'s mac-volume check reads machines/<IMG_MACHINE>.conf through this env, and this
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
    """`unit sysimage.builders_conform`, the read half: every builder names where its images land, and that is the only
    place any read looks."""

    def test_every_builder_a_preset_names_has_outputs_and_each_finds_only_its_own(self):
        named = {images.load(n)["IMG_BUILDER"] for n in images.names()}
        self.assertEqual(set(ls.OUTPUTS), set(images.WS_BUILDERS))
        self.assertLessEqual(named, set(images.WS_BUILDERS) | set(ls.HOST_BUILDERS))
        with scratch_dir() as d:
            y = yocto_image(d)
            image(d, YWS, "build/CrossToolChains/rpi3-32bits-mesa/build/image/core.wic")
            b = image(d, BWS, "build/buildroot/%s/output/images/sdcard.img" % BUILDROOT)
            st = Store({"WK_STORE": str(d)})
            self.assertEqual((ls.outputs(Local(), st, YWS), ls.outputs(Local(), st, BWS)), ([str(y)], [str(b)]))

    def test_a_scan_through_a_fake_machine_and_an_image_workspace_with_none_gets_a_placeholder(self):
        f = Fake()
        f._set_file("/st/ws/%s/build/CrossToolChains/t/build/image/a.wic.xz" % YWS, "x")
        f._set_file("/st/ws/%s/build/.hidden.wic.xz" % YWS, "x")
        f.dirs.update({"/st/ws/" + BWS, "/st/ws/jsc-release"})
        self.assertEqual(ls.scan(f, Store({"WK_STORE": "/st"})), [
            ls.Image("buildroot", BWS, None), ls.Image("yocto", YWS, "/st/ws/%s/build/CrossToolChains/t/build/image/a.wic.xz" % YWS)])


class TestTheListing(WkTest):
    """`wk sysimage ls` on this store: a row per image, its state, its slots, and then the fleet's."""

    def test_the_columns_name_the_board_and_the_state_is_the_build_over_the_image(self):
        """A yocto image stage removes the last image as it rebuilds, so a row with none is normal for hours."""
        for has_image, building, want in ((False, lambda ws: False, "none"), (False, lambda ws: True, "building"),
                                          (True, lambda ws: None, "unknown"), (True, lambda ws: False, "ready")):
            with self.subTest(want=want), scratch_dir() as d:
                p = yocto_image(d) if has_image else (d / "ws" / YWS / "build").mkdir(parents=True)
                s = sysimage(d)
                s.building = building
                lines = ran(s.ls, False).out.splitlines()
                self.assertEqual(lines[0].split(), list(ls.HEADER))
                self.assertEqual(lines[1].split()[:5], [YWS, "rpi3", "yocto", want, "1.0K" if has_image else "-"])
                self.assertEqual(lines[2].strip(), str(p) if has_image else "no image yet; 'wk sysimage build %s' builds one" % YOCTO)

    def test_each_slot_is_listed_under_its_image_and_continued_is_rows_under_its_label(self):
        with scratch_dir() as d:
            self.assertEqual(ran(sysimage(d).ls, False).out, "")
            yocto_image(d)
            where = slot(d, YWS, "base")
            s = sysimage(d)
            self.assertIn("    slot base         %s  wpe-cross-pgo-use  built 2026-09-01T00:00:00Z  (%s)" % (SHA[:12], where), ran(s.ls, False).out)
            s.reg.env["WK_ROW_LABEL"] = "moose"
            cp = ran(s.ls, True)
        self.assertEqual((cp.out.split()[:3], cp.err), ([YWS, "rpi3", "moose"], ""))
        self.assertEqual([ls.human_bytes(n) for n in (3, 1536, 10 * 1024, 5 * 1024 ** 3)], ["3B", "1.5K", "10K", "5.0G"])

    def test_each_answering_machine_adds_its_rows_after_this_stores(self):
        with scratch_dir() as d:
            yocto_image(d)
            far = FakeDriver("fakebox", out="yocto-faraway  rpi4  fakebox\r\n    /elsewhere/faraway.wic.xz\n")
            rows = ls.Listing(registry(d, [far]), "", lambda ws: False, [].append).rows()
        self.assertTrue(rows[0].startswith(YWS))
        self.assertEqual(rows[-2:], ["yocto-faraway  rpi4  fakebox", "    /elsewhere/faraway.wic.xz"])
        self.assertEqual(far.asked[0][0], ("sysimage", "ls", "--continued"))


class TestHoldsAndPath(WkTest):
    """`holds` prints yes or no, the done predicate a step asks of the machine holding the workspace; `path` the image."""

    def holds(self, d, *args):
        return ran(sysimage(d).holds, *args).out

    def test_the_image_and_the_toolchain(self):
        with scratch_dir() as d:
            s = sysimage(d)
            self.assertEqual((self.holds(d, YOCTO, None, None, None, None, False), ran(s.path, YOCTO, None).rc), ("no\n", 1))
            p = yocto_image(d)
            self.assertEqual((self.holds(d, YOCTO, None, None, None, None, False), ran(s.path, YOCTO, None).out), ("yes\n", "%s\n" % p))
            self.assertEqual(self.holds(d, YOCTO + "@moose", "other-ws", None, None, None, False), "no\n")
            self.assertEqual(ran(s.path, "macos-guest-base", None).rc, 1, "a host-built preset has no workspace")
            tc = d / "ws" / YWS / "build" / "CrossToolChains" / "rpi3-32bits-mesa" / "build" / "toolchain"
            tc.mkdir(parents=True)
            self.assertEqual(self.holds(d, YOCTO, None, None, None, None, True), "no\n")
            (tc / ".toolchain_path_configured").write_text("")
            (tc / "environment-setup-cortexa7").write_text("")
            self.assertEqual(self.holds(d, YOCTO, None, None, None, None, True), "yes\n")

    def test_a_slot_on_a_profile_guided_release_is_its_measured_build_and_elsewhere_the_commit(self):
        with scratch_dir() as d:
            slot(d, YWS, "base", preset="wpe-cross-pgo-collect")
            self.assertEqual([self.holds(d, YOCTO, None, "base", SHA, p, False) for p in (None, "wpe-cross-pgo-collect")], ["no\n", "yes\n"])
            slot(d, YWS, "base")
            self.assertEqual([self.holds(d, YOCTO, None, "base", c, None, False) for c in (SHA, "b" * 40)], ["yes\n", "no\n"])
            slot(d, BWS, "base", preset="")
            self.assertEqual([self.holds(d, BUILDROOT, None, n, SHA, None, False) for n in ("base", "nope")], ["yes\n", "no\n"])

    def test_every_malformed_question_is_refused_with_no_verdict(self):
        with scratch_dir() as d:
            for args in ((YOCTO, None, "base", "abc", None, False), (YOCTO, None, None, SHA, None, False),
                         (YOCTO, None, "base", None, None, True), (BUILDROOT, None, None, None, None, True),
                         (YOCTO, None, "-x", SHA, None, False), ("nosuch", None, None, None, None, False), ("", None, None, None, None, False)):
                with self.subTest(args=args):
                    cp = ran(sysimage(d).holds, *args)
                    self.assertEqual((cp.rc, cp.out), (1, ""))

    def test_an_unreadable_host_builder_is_refused_rather_than_said_no(self):
        with scratch_dir() as d, unittest.mock.patch.object(macvolume.MacVolume, "outputs", side_effect=ls.Unknown("the Mac does not answer")):
            cp = ran(sysimage(d).holds, "perf-macos-tolken", None, None, None, None, False)
        self.assertIn("cannot tell whether perf-macos-tolken is built: the Mac does not answer", cp.err)
        self.assertEqual((cp.rc, cp.out), (1, ""))

    def test_the_mac_volume_answers_only_once_installed_and_marked(self):
        """`unit sysimage.builders_conform[mac-volume]`: a builder with no workspace answers off the machine it builds on."""
        with scratch_dir() as d:
            f = Fake()
            s = sysimage(d, machine=f)
            v = macvolume.MacVolume(f, images.load("perf-macos-tolken"), s.reg.env)
            f._set_file(v.s + "/etc/wk-image", "id=x\n")
            self.assertEqual((ran(s.path, "perf-macos-tolken", None).rc, self.holds(d, "perf-macos-tolken", None, None, None, None, False)), (1, "no\n"))
            f._set_file(v.s + "/System/Library/CoreServices/SystemVersion.plist", "")
            self.assertEqual(ran(s.path, "perf-macos-tolken", None).out, "%s/etc/wk-image\n" % v.s)
            line = next(l for l in ran(s.ls, False).out.splitlines() if l.startswith("perf-macos-tolken"))
        self.assertEqual(line.split()[:5], ["perf-macos-tolken", "mbp", "mac-volume", "ready", "-"])


class TestTheRoutingAnswers(unittest.TestCase):
    """The dispatcher's questions, answered before anything else runs."""

    def test_ls_walks_from_here_and_a_spec_naming_a_machine_is_its_place(self):
        self.assertEqual((cli.where(["ls"]), cli.where(["list", "--continued"])), ("local", "store"))
        with scratch_dir() as d:
            reg = registry(d)
            self.assertEqual([cli.wsplace(["holds", spec], reg) for spec in (YOCTO + "@moose", YOCTO + "@" + record.machine_name(reg.env),
                                                                             YOCTO, "macos-guest-base@moose")], ["moose", "container", "", ""])


class TestAnImageBuiltInAGuestReachesTheHost(unittest.TestCase):
    @requires_machine("benchvm")
    def test_build_in_a_guest(self):
        """`live sysimage.build[vm]`: an image build run from a Tart guest, and the image written from the
        host. Needs a guest with the tools and hours of build; the reach half is `ls` naming it."""
        self.skipTest("owed: a guest image build is hours long and has not been run from this suite")


if __name__ == "__main__":
    unittest.main()
