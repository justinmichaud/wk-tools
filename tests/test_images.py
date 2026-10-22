"""What an image is: lib/wk/images.py's preset loader and every name derived from a preset, the presets and
defconfigs as data, `wk sysimage`'s read side and routing, the layers a board image carries, and lib/wk/disk.py."""
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

from tests.fakes import Fake, FakeDriver, FakeRegistry
from tests.support import NO_REGISTRY, REPO, WkTest, load_cmd, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import act, decl, disk, dispatch, images, record  # noqa: E402
from wk.fleet import Fleet  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import cli, ls, macvolume, write  # noqa: E402


PRESET, BUILDROOT = "webkit-2.52-yocto-rpi5-64", "wpewebkit-2.38-buildroot-rpi3-32"
WS, BWS = "yocto-" + PRESET, "buildroot-" + BUILDROOT
STORE = {"WK_STORE": "/store", "WK_ROOT": str(REPO)}


class TestTheLoader(WkTest):
    """A WK_ROOT of this test's own, holding only the confs it writes."""

    def load(self, text, name="p", **more):
        (self.tmp / "image" / "presets").mkdir(parents=True, exist_ok=True)
        for n, t in dict(more, **{name: text}).items():
            (self.tmp / "image" / "presets" / (n + ".conf")).write_text(t)
        return images.load(name, {"WK_ROOT": str(self.tmp)})

    def test_a_field_left_out_is_its_default_and_a_second_load_inherits_nothing(self):
        p = self.load('# p -- a preset\nIMG_BUILDER=yocto\nCFG_NEEDS="one\n    two"  # why\n')
        self.assertEqual((p["IMG_WATCHDOG"], p["YOC_REMOTE"], p["CFG_NEEDS"], p["IMG_SPEC_DIR"]),
                         ("300", "origin", "one\n    two", str(self.tmp / "image" / "p")))
        self.assertEqual(self.load("IMG_BUILDER=guest\n", name="b")["CFG_NEEDS"], "")
        self.assertEqual(images.listing({"WK_ROOT": str(self.tmp)}).splitlines(),
                         ["b", "            ", "p", "            a preset", "            -- not buildable yet; 'wk sysimage build p' says what it needs"])

    def test_a_conf_is_literals_of_known_fields_and_a_name_none_answers_to_is_a_lookup_error(self):
        for text in ("IMG_BUILDER=$HOME\n", "IMG_NOPE=1\n", "IMG_ARCH=a b\n", 'CFG_NEEDS="never closed\n', "just prose\n"):
            with self.subTest(text=text), self.assertRaises(images.ConfError):
                self.load(text)
        for name in ("nosuch", "../configs/p", ""):
            with self.assertRaises(LookupError):
                images.load(name, {"WK_ROOT": str(self.tmp)})
        (self.tmp / "image" / "presets" / "p.conf").write_text("IMG_BUILDER=yocto\nIMG_NOPE=1\n")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            images.image_ws("p", {"WK_ROOT": str(self.tmp)})
        self.assertIn("p.conf:2: IMG_NOPE is not an image preset field", err.getvalue())

    def test_origin_s_branches_are_those_an_origin_preset_tracks(self):
        self.load("CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n", b="CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n",
                  c="CFG_REMOTE=wpe\nCFG_BRANCH=wpe-2.46\n")
        self.assertEqual(images.origin_branches({"WK_ROOT": str(self.tmp)}), ["webkitglib/2.52"])


class TestTheNames(unittest.TestCase):
    def test_the_spec_and_the_image_workspace(self):
        self.assertEqual((images.spec_preset(PRESET + "@moose"), images.spec_machine(PRESET + "@moose"), images.spec_machine(PRESET)),
                         (PRESET, "moose", ""))
        self.assertEqual([images.spec_place(m, "here", "container") for m in ("here", "moose")], ["container", "moose"])
        self.assertEqual([images.image_ws(s, STORE) for s in (PRESET, BUILDROOT, PRESET + "@moose", "macos-guest-base", "nosuch")],
                         [WS, BWS, WS, "", ""])
        self.assertEqual([images.ws_preset(w, STORE) for w in (WS, WS + "-base", WS + "-oc", "jsc-release", "yocto-nosuch")],
                         [PRESET, PRESET, PRESET + "-oc", None, None])

    def test_every_path_is_keyed_on_the_workspace(self):
        """One image preset may have a workspace per arm, and a slot or a collection in one is not the other's."""
        self.assertEqual([images.slot_dir(w, "base", STORE) for w in (WS + "-pr", BWS + "-pr", "jsc-release")],
                         ["/store/ws/%s-pr/build/wk-slots/base" % WS, "/store/ws/%s-pr/build/buildroot/%s/output/wk-slots/base" % (BWS, BUILDROOT), None])
        self.assertEqual(images.pgo_dir(WS + "-base", "pr", STORE), "/store/ws/%s-base/build/wk-pgo/pr" % WS)
        self.assertEqual((images.instr_slot("base"), images.measured_slot("base-instr"), images.measured_slot("base")), ("base-instr", "base", "base"))

    def test_pgo_the_machine_holding_it_and_a_slot_name(self):
        self.assertEqual([images.pgo_wanted(b, r) for b, r in (("yocto", "2.52"), ("yocto", "2.60"), ("yocto", "2.46"), ("buildroot", "2.52"), ("yocto", ""))],
                         [True, True, False, False, False])
        self.assertEqual([images.ws_machine(s, p, "here") for s, p in (("moose", "elsewhere"), ("", "elsewhere"), ("", "container"), ("", "local"))],
                         ["moose", "elsewhere", "here", "here"])
        self.assertEqual(images.build_resource("moose"), "machine:moose")
        for good in ("base", "pr-2", "a.b_c"):
            images.check_slot_name(good)
        for bad in ("", "-x", ".x", "a/b", "a b"):
            with self.subTest(slot=bad), self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()):
                images.check_slot_name(bad)


ENV = {"WK_ROOT": str(REPO)}
CONFIGS = REPO / "image" / "buildroot" / "external" / "configs"
CLOCKS = re.compile(r"(?m)^\s*(arm_freq|over_voltage)\w*=")


def settings(path):
    return "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))


class TestImagePresetsAreData(unittest.TestCase):
    wk_tier = "lint"

    def test_each_preset_is_described_names_its_watchdog_only_to_differ_and_is_for_a_machine_the_fleet_declares(self):
        fleet = Fleet(REPO)
        keys = ("BR_KERNEL_DEB_URL", "BR_KERNEL_DEB_SHA256", "BR_KERNEL_RELEASE")
        for n in images.names(ENV):
            p = images.load(n, ENV)
            with self.subTest(preset=n):
                self.assertTrue(images.blurb(n, ENV), "no '# %s -- <description>' header" % n)
                self.assertNotEqual(images.parse(images.conf_path(n, ENV)).get("IMG_WATCHDOG"), images.FIELDS["IMG_WATCHDOG"])
                kind = {"guest": "", "mac-volume": "mac"}.get(p["IMG_BUILDER"], "board")
                self.assertEqual(fleet.kind(p["IMG_MACHINE"]) if p["IMG_MACHINE"] else "", kind)
                self.assertIn(sum(bool(p[k]) for k in keys), (0, 3))
                if p["IMG_BUILDER"] == "buildroot":
                    self.assertEqual(bool(p["CFG_NEEDS"]), not (CONFIGS / p["BR_DEFCONFIG"]).is_file() if p["BR_DEFCONFIG"] else True)
                    if images.pgo_wanted("yocto", p["CFG_RELEASE"]):
                        self.assertIn("PGO", p["CFG_NEEDS"])

    def test_the_rpi5_overclock_is_its_own_preset_of_the_same_image_and_nowhere_else(self):
        oc, stock = images.load("webkit-2.52-yocto-rpi5-64-oc", ENV), images.load("webkit-2.52-yocto-rpi5-64", ENV)
        same = lambda p: {k: v for k, v in p.items() if k not in ("IMG_PRESET", "IMG_SPEC_DIR")}  # noqa: E731
        self.assertEqual(same(oc), same(stock))
        self.assertEqual(set(CLOCKS.findall((REPO / "image" / oc["IMG_PRESET"] / "config.txt.append").read_text())), {"arm_freq", "over_voltage"})
        for path in [REPO / "image" / stock["IMG_PRESET"] / "config.txt.append", REPO / "host" / "linux" / "rpi5" / "rpi5-setup.sh"] \
                + sorted((REPO / "image" / "boards" / "rpi5").iterdir()):
            if path.is_file():
                    self.assertIsNone(CLOCKS.search(settings(path)), path)
        text = write.config_add(REPO, oc)
        self.assertLess(text.index("os_check=0"), text.index("arm_freq=2800"))


DEFCONFIGS = sorted((REPO / "image" / "buildroot" / "external" / "configs").glob("*_defconfig"))


def defconfig_settings(path):
    return dict(l.strip().partition("=")[::2] for l in path.read_text().splitlines() if l.strip() and not l.strip().startswith("#"))


class TestDerivedDefconfigs(unittest.TestCase):
    def test_each_matches_its_board_and_can_be_reached(self):
        """wpa_supplicant for WiFi, OpenSSH (dropbear 2019.78 refuses ed25519), iptables and TUN for tailscaled."""
        self.assertTrue(DEFCONFIGS)
        for cfg in DEFCONFIGS:
            with self.subTest(defconfig=cfg.name):
                s = defconfig_settings(cfg)
                pi4 = s.get("BR2_PACKAGE_RPI_FIRMWARE_VARIANT_PI4") == "y"
                self.assertEqual(pi4, "board/raspberrypi4" in s.get("BR2_ROOTFS_POST_IMAGE_SCRIPT", ""))
                if pi4:
                    self.assertEqual(s.get("BR2_PACKAGE_RPI_FIRMWARE_3BPLUSNEW"), "y")
                if s.get("BR2_PACKAGE_COG_PLATFORM_DRM") == "y":
                    self.assertEqual(s.get("BR2_PACKAGE_WPEBACKEND_FDO"), "y")
                self.assertEqual([s.get(k) for k in ("BR2_PACKAGE_WPA_SUPPLICANT", "BR2_PACKAGE_OPENSSH", "BR2_PACKAGE_IPTABLES")], ["y"] * 3)
                self.assertNotIn("BR2_PACKAGE_DROPBEAR", s)
                frag = s.get("BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES", "").strip('"')
                self.assertEqual("linux-fleet.fragment" in frag, s.get("BR2_LINUX_KERNEL") == "y")
                release = cfg.name.split("_wpe_")[1].rsplit("_cog", 1)[0]
                self.assertEqual([k for k, v in s.items() if k.startswith("BR2_PACKAGE_WPEWEBKIT2_") and v == "y"],
                                 ["BR2_PACKAGE_WPEWEBKIT" + release.upper()])


BBAPPEND = (REPO / "image" / "yocto" / "meta-wk-multilib" / "recipes-webkit"
            / "images" / "webkit-dev-ci-tools.bbappend")


class FakeD(dict):
    """Enough of bitbake's datastore for the lifted function."""

    def getVar(self, k):
        return self.get(k)

    def setVar(self, k, v):
        self[k] = v


def run_bbappend(d):
    text = BBAPPEND.read_text()
    m = re.search(r"(?ms)^python \(\) \{\n(.*)^\}", text)
    assert m, "the bbappend no longer has an anonymous python function"
    body = "\n".join(l[4:] if l.startswith("    ") else l
                     for l in m.group(1).splitlines())
    exec(compile("def _f(d):\n" + "\n".join("    " + l for l in body.splitlines()),
                 str(BBAPPEND), "exec"), globals())
    return _f(d)  # noqa: F821


def a_lib32_image(**over):
    d = FakeD({
        "MLPREFIX": "lib32-",
        "IMAGE_INSTALL": "wpewebkit cog linux-raspberrypi",
        "WK_MULTILIB_KEEP": "linux-raspberrypi",
        "NON_MULTILIB_RECIPES": "",
    })
    d.update(over)
    return d


class TestTheImageInstallsOneWidth(unittest.TestCase):
    def test_every_package_takes_the_prefix_but_what_has_one_build_per_machine(self):
        d = a_lib32_image()
        run_bbappend(d)
        self.assertEqual(sorted(d["IMAGE_INSTALL"].split()), ["lib32-cog", "lib32-wpewebkit", "linux-raspberrypi"])

    def test_a_plain_image_is_left_alone(self):
        """No MLPREFIX, no rewriting -- the 64-bit build shares this recipe."""
        d = a_lib32_image(MLPREFIX="")
        run_bbappend(d)
        self.assertEqual("wpewebkit cog linux-raspberrypi", d["IMAGE_INSTALL"])


class TestTheRpi3SwapsToZram(unittest.TestCase):
    UNIT = REPO / "boot" / "firstboot" / "wk-no-swap.service"

    def swapped_off(self, swaps):
        line = next(l for l in self.UNIT.read_text().splitlines() if l.startswith("ExecStart="))
        argv = shlex.split(line[len("ExecStart="):].replace("$$", "$"))
        with tempfile.TemporaryDirectory() as d:
            Path(d, "swaps").write_text("Filename Type Size Used Priority\n" + swaps)
            Path(d, "swapoff").write_text('#!/bin/sh\necho "$@" >> "%s/off"\n' % d)
            os.chmod(Path(d, "swapoff"), 0o755)
            subprocess.run(argv[:2] + [argv[2].replace("/proc/swaps", d + "/swaps")], check=True,
                           env={"PATH": d + ":/usr/bin:/bin"})
            off = Path(d, "off")
            return off.read_text().split() if off.exists() else []

    def test_disk_swap_goes_and_zram_stays(self):
        self.assertEqual(["/dev/mmcblk0p3", "/swapfile"],
                         self.swapped_off("/dev/zram0 partition 474000 0 100\n/dev/mmcblk0p3 partition 1 0 -2\n"
                                          "/swapfile file 1 0 -3\n"))

    def test_no_swap_runs_nothing(self):
        self.assertEqual([], self.swapped_off(""))


TABLE = load_cmd("sysimage").COMMAND


def args(*words):
    return decl.parse("sysimage", TABLE, list(words))[1]


class TestTheImageWorkspaceAnswer(unittest.TestCase):
    def test_every_preset_answers_for_its_builder_and_no_preset_names_none(self):
        reg = mock.Mock(env=os.environ, default=lambda: "container")
        for preset in images.names():
            builder = images.load(preset)["IMG_BUILDER"]
            in_ws = builder in images.WS_BUILDERS
            with self.subTest(preset=preset):
                self.assertEqual({cli.wsname(args(s, preset)) for s in ("build", "webkit", "holds", "path")},
                                 {"%s-%s" % (builder, preset) if in_ws else ""})
                self.assertEqual(cli.where(args("build", preset), reg), "workspace" if in_ws else "host")
        self.assertEqual(cli.derived(args("holds", "webkit-2.52-yocto-rpi5-64", "--workspace", "arm-b"), reg)[0], "arm-b")
        self.assertEqual(cli.derived(args("build", "nosuchpreset"), reg), ("", ""))


class TestTheDispatcherPlacesIt(unittest.TestCase):
    def test_only_declared_options_pass_one_word_per_value(self):
        self.assertEqual(args("build", "p", "--stage", "webkit", "--detach").options(), ["--stage", "webkit", "--detach"])
        for words in (("build", "p", "--bogus"), ("webkit", "p", "--keep-work"), ("build", "p", "--", "--stage", "image")):
            with self.assertRaises(decl.Usage):
                args(*words)

    def test_the_derived_name_decides_the_place_unless_the_spec_names_one(self):
        def resolve(wsplace, derived, words):
            with mock.patch.object(dispatch, "registry", lambda: mock.Mock(ws_place=lambda name: "place-of:" + name)), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WK_PLACE", None)
                return dispatch.resolve_place(args(*words), derived, wsplace)
        self.assertEqual([resolve("", "yocto-demo", ["build", "demo"]), resolve("", "", ["build", "demo"]),
                          resolve("moose", "yocto-demo", ["build", "demo@moose"])], ["place-of:yocto-demo", "container", "moose"])


YOCTO = "webkit-2.52-yocto-rpi3-32"          # a profile-guided release
YWS = "yocto-" + YOCTO
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
        from tests.support import load_cmd
        from wk import decl
        table = load_cmd("sysimage").COMMAND

        def answer(*words):
            t, args, _ = decl.parse("sysimage", table, list(words))
            return t.where(args, types.SimpleNamespace(env=os.environ)), args
        self.assertEqual((answer("ls")[0], answer("list", "--continued")[0]), ("local", "store"))
        with scratch_dir() as d:
            reg = registry(d)
            self.assertEqual([cli.derived(answer("holds", spec)[1], reg)[1] for spec in (YOCTO + "@moose", YOCTO + "@" + record.machine_name(reg.env),
                                                                             YOCTO, "macos-guest-base@moose")], ["moose", "container", "", ""])


class TestAnImageBuiltInAGuestReachesTheHost(unittest.TestCase):
    @requires_machine("benchvm")
    def test_build_in_a_guest(self):
        """`live sysimage.build[vm]`: an image build run from a Tart guest, and the image written from the
        host. Needs a guest with the tools and hours of build; the reach half is `ls` naming it."""
        self.skipTest("owed: a guest image build is hours long and has not been run from this suite")


class TestKb(unittest.TestCase):
    def test_an_absent_path_is_zero_and_a_readable_tree_its_size(self):
        with scratch_dir() as d:
            self.assertEqual(0, disk.kb(str(d / "absent")))
            (d / "f").write_bytes(b"x" * 65536)
            self.assertGreaterEqual(disk.kb(str(d)), 64)

    @unittest.skipIf(os.geteuid() == 0, "root reads every tree")
    def test_a_tree_du_cannot_read_whole_is_unknown_and_shows_so(self):
        with scratch_dir() as d:
            (d / "locked").mkdir()
            (d / "locked" / "f").write_text("x")
            (d / "locked").chmod(0)
            try:
                self.assertIsNone(disk.kb(str(d)))
            finally:
                (d / "locked").chmod(0o700)


class TestReport(unittest.TestCase):
    def test_an_unknown_row_reads_question_marks_and_adds_nothing_to_the_total(self):
        rep, err = disk.Report(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rep.render("??\tworkspace a\twk rm a\n2048\tworkspace b\twk rm b\n", add=True)
        self.assertEqual(2048, rep.total)
        self.assertRegex(err.getvalue(), r"\?\?\s+workspace a")
        self.assertRegex(err.getvalue(), r"2(\.0)? ?M\S*\s+workspace b")
