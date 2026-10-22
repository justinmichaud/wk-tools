"""The yocto build as it runs inside a workspace (lib/wk/sysimage/yocto_ws.py) against a Fake machine."""
import configparser
import contextlib
import io
import os
import subprocess
import sys
import unittest

from tests.support import REPO
from tests.test_buildroot_ws import WsWorld

sys.path.insert(0, str(REPO / "lib"))
from wk import pgo  # noqa: E402
from wk.machine import Result, isolated_module  # noqa: E402
from wk.sysimage import Failed, yocto_ws as yt  # noqa: E402

PRESET, TARGET, SRC, TOOLS = "webkit-2.52-yocto-rpi4-64", "rpi4-64bits-mesa", "/src/WebKit", "/opt/wk-tools"
WORK = SRC + "/WebKitBuild/CrossToolChains/" + TARGET
CONF, LAYERS, IMAGE_DIR = WORK + "/build/conf/local.conf", WORK + "/build/conf/bblayers.conf", WORK + "/build/image"
ENV = {"DL_DIR": "/cache/yocto/downloads", "SSTATE_DIR": "/cache/yocto/sstate", "WK_MIRROR": "/mirror/WebKit.git", "LANG": "C.UTF-8"}
TARGETS = """[rpi4-64bits-mesa]
repo_manifest_path = rpi/manifest.xml
conf_local_path = rpi/local-rpi4-64bits-mesa.conf
image_basename = webkit-dev-ci-tools
environment[BUILD_WEBKIT_ARGS] = --no-bubblewrap-sandbox --cmakeargs="-DENABLE_X=OFF -DUSE_Y=ON"
"""
LOCAL = 'DISTRO = "webkitdevci"\nMACHINE = "raspberrypi4-64"\nBB_DISKMON_DIRS ??= "HALT,${TMPDIR},100M,1K"\n'
HELPER = SRC + "/Tools/Scripts/cross-toolchain-helper"


class World(WsWorld):
    """A workspace whose layers are synced: the checkout, the helper's workdir and its two confs."""

    BUILD, PARSE, ENV = yt.Build, staticmethod(lambda argv: yt.parse([PRESET, "--jobs", "16"] + argv)), ENV

    def __init__(self):
        super().__init__()
        self.dirs.update({SRC, SRC + "/.git", WORK + "/.git", TOOLS})
        self.files.update({CONF: 'MACHINE = "raspberrypi4-64"\n', LAYERS: 'BBLAYERS ?= "a b"\n',
                           WORK + "/.target-info-version": "rpi4-64bits-mesa 2.46.0\n",
                           SRC + "/Tools/yocto/targets.conf": TARGETS, SRC + "/Tools/yocto/rpi/local-rpi4-64bits-mesa.conf": LOCAL})
        for layer in yt.LAYERS + ("meta-wk-multilib",):
            self.files["%s/image/yocto/%s/conf/layer.conf" % (TOOLS, layer)] = ""
        for p in (["git"], ["bash"], ["env"], ["python3"], ["cp"], ["sh", "-c"]):
            self.answer(p)
        self.answer(["id", "-u"], out="1000\n")
        self.answer([HELPER, "--print-available-targets"], out="rpi3-32bits-mesa\n%s\n" % TARGET)
        self.react(["mv", "-T"], self._mv)
        self.react(["find", IMAGE_DIR], lambda a, f: Result(0, "%d.5\n" % f.clock.now()) if IMAGE_DIR in f.dirs else Result(1))

    def _mv(self, argv, f):
        src, dst = argv[2], argv[3]
        for d in [d for d in self.dirs if d == src or d.startswith(src + "/")]:
            self.dirs.discard(d)
            self.dirs.add(dst + d[len(src):])
        for p in [p for p in self.files if p.startswith(src + "/")]:
            self.files[dst + p[len(src):]] = self.files.pop(p)
        return Result(0)

    def stage(self, *more, environ=ENV):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = yt.main([PRESET, "--jobs", "16"] + list(more), environ=dict(environ), machine=self, clock=self.clock, tools=TOOLS)
        return rc, out.getvalue(), err.getvalue()

    def call(self, method, *a, argv=(), **preset):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                getattr(self.build(list(argv), **preset), method)(*a)
            except Failed as e:
                return out.getvalue(), str(e)
        return out.getvalue(), None

    def slot_ready(self, slot_json='{"build_id": "b1d"}'):
        sdk = WORK + "/build/toolchain"
        for f in (sdk + "/.toolchain_path_configured", sdk + "/environment-setup-cortexa72",
                  SRC + "/WebKitBuild/WPE/Release_%s/bin/MiniBrowser" % TARGET):
            self._set_file(f, "")
        self.react(["python3"], lambda a, f: (f._set_file(next(x for x in a if x.endswith("/slot.json")), slot_json)
                                             if "manifest" in a else None, Result(0))[-1])
        return self


class TestTheEnvironment(unittest.TestCase):
    def test_the_sdk_s_environment_goes_and_git_bitbake_and_the_locale_are_pinned(self):
        base = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/x", "CPATH": "/y", "SSTATE_DIR": "/cache/sstate", "BB_ENV_PASSTHROUGH_ADDITIONS": "FOO"}
        env = yt.build_env(base, yt.parse([PRESET, "--mem-budget", "20480", "--sstate-ns", "ns"]), "C\nC.utf8\n")
        self.assertFalse({"LD_LIBRARY_PATH", "CPATH"} & set(env))
        self.assertEqual([env[k] for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1",
                                           "GIT_CONFIG_VALUE_1", "WEBKIT_CROSS_WIPE_ON_CHANGE", "LANG", "WK_MEM_BUDGET_MB", "SSTATE_DIR")],
                         ["2", "index.version", "2", "index.skipHash", "false", "0", "C.UTF-8", "20480", "/cache/sstate/ns"])
        words = env["BB_ENV_PASSTHROUGH_ADDITIONS"].split()
        self.assertEqual(words[0], "FOO")
        self.assertTrue({"DL_DIR", "SSTATE_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_VALUE_1"} <= set(words))
        self.assertEqual(yt.build_env(base, yt.parse([PRESET]), "en_US.utf8\n")["LC_ALL"], "en_US.UTF-8")


class TestTheConfiguration(unittest.TestCase):
    def test_threads_are_a_quarter_of_the_jobs(self):
        self.assertEqual((yt.threads(16), yt.threads(3)), ((4, 4), (1, 3)))

    def test_the_blocks_are_replaced_on_a_rerun_and_carry_the_caches_the_layers_and_the_board_last(self):
        w = World()
        w.files[TOOLS + "/image/boards/rpi5/local.conf.append"] = 'X = "1"\n'
        for _ in range(2):
            self.assertIsNone(w.call("configure", IMG_MACHINE="rpi5", YOC_MULTILIB="lib32", YOC_MULTILIB_TUNE="armv7athf")[1])
        conf, layers = w.files[CONF], w.files[LAYERS]
        self.assertEqual((conf.count(yt.MARKER), layers.count(yt.BB_MARKER)), (1, 1))
        self.assertTrue(conf.startswith('MACHINE = "raspberrypi4-64"\n'))
        for line in ('DL_DIR = "/cache/yocto/downloads"', 'BB_NUMBER_THREADS = "4"', 'MULTILIBS = "multilib:lib32"',
                     'DEFAULTTUNE:virtclass-multilib-lib32 = "armv7athf"', 'RM_WORK_EXCLUDE += "webkit-dev-ci-tools"') + tuple(yt.IMAGE_LINES):
            self.assertIn(line + "\n", conf)
        self.assertTrue(conf.rstrip("\n").endswith('X = "1"'))
        self.assertEqual([l for l in layers.splitlines() if l.startswith("BBLAYERS +=")],
                         ['BBLAYERS += "%s/image/yocto/%s"' % (TOOLS, n) for n in yt.LAYERS + ("meta-wk-multilib",)])

    def test_a_missing_layer_is_refused_before_anything_is_written(self):
        w = World()
        del w.files[TOOLS + "/image/yocto/meta-wk-tailnet/conf/layer.conf"]
        self.assertIn("no layer at %s/image/yocto/meta-wk-tailnet" % TOOLS, w.call("configure")[1])
        self.assertNotIn(yt.MARKER, w.files[CONF])


class TestPortingATarget(unittest.TestCase):
    """A release branch that predates a target gets its glue derived from one it has: the section with one
    path changed, pointing at that target's local.conf with one MACHINE line changed."""

    def port(self, target="rpi5-64bits-mesa", frm="rpi4-64bits-mesa", image="", local=LOCAL):
        try:
            return yt.port_target(TARGETS, lambda rel: local if rel.endswith("rpi4-64bits-mesa.conf") else None,
                                  target, frm, "raspberrypi5", image), None
        except Failed as e:
            return None, str(e)

    def test_the_section_and_its_local_conf_are_derived(self):
        (conf, locals_), _ = self.port(image="lib32-webkit-dev-ci-tools")
        c = configparser.ConfigParser()
        c.read_string(conf)
        new = c["rpi5-64bits-mesa"]
        self.assertEqual((new["conf_local_path"], new["repo_manifest_path"], new["image_basename"]),
                         ("rpi/local-rpi5-64bits-mesa.conf", "rpi/manifest.xml", "lib32-webkit-dev-ci-tools"))
        text = locals_["rpi/local-rpi5-64bits-mesa.conf"]
        self.assertIn('MACHINE = "raspberrypi5"', text)
        self.assertNotIn("raspberrypi4-64", text)
        self.assertEqual(self.port(target=TARGET), (None, None))

    def test_what_cannot_be_derived_is_refused(self):
        self.assertIn("rpi4-64bits-mesa", self.port(frm="rpi9-64bits-mesa")[1])
        self.assertIn("MACHINE 0 times", self.port(local='DISTRO = "x"\n')[1])
        self.assertIn("not in this checkout", self.port(local=None)[1])
        self.assertIn("no YOC_MACHINE", World().call("port", YOC_PORT_TARGET_FROM="rpi3-32bits-mesa")[1])

    def test_the_build_writes_both_into_the_checkout_and_converges(self):
        w = World()
        preset = dict(YOC_TARGET="rpi5-64bits-mesa", YOC_PORT_TARGET_FROM=TARGET, YOC_MACHINE="raspberrypi5")
        b = w.build([], **preset)
        b.target = preset["YOC_TARGET"]
        with contextlib.redirect_stdout(io.StringIO()):
            b.port()
            writes = len([e for e in w.effects if e[0] == "write"])
            b.port()
        self.assertIn("[rpi5-64bits-mesa]", w.files[SRC + "/Tools/yocto/targets.conf"])
        self.assertIn(SRC + "/Tools/yocto/rpi/local-rpi5-64bits-mesa.conf", w.files)
        self.assertEqual(len([e for e in w.effects if e[0] == "write"]), writes)


class TestTheCheckout(unittest.TestCase):
    def test_the_index_is_pinned_and_an_unreadable_one_rebuilt_from_head(self):
        w = World()
        w.call("refresh_git_index", SRC)
        self.assertIn(["git", "-C", SRC, "config", "index.skipHash", "false"], w.ran("git"))
        self.assertFalse(w.ran("git", "-C", SRC, "read-tree"))
        w.answer(["git", "-C", SRC, "ls-files"], rc=128)
        w.answer(["git", "-C", SRC, "rev-parse", "--absolute-git-dir"], out=SRC + "/.git\n")
        self.assertIn("rebuilt an unreadable git index", w.call("refresh_git_index", SRC)[0])
        self.assertIn(("remove", SRC + "/.git/index"), w.effects)
        w = World()
        w.call("refresh_git_index", "/tmp/plain")
        self.assertFalse(w.ran("git"))

    def test_a_slot_s_commit_is_fetched_from_the_mirror_and_forced_in(self):
        w = World()
        w.answer(["git", "-C", SRC, "cat-file"], rc=1)
        w.answer(["git", "-C", SRC, "status", "--porcelain"], out=" M a\n?? b\n")
        out, why = w.call("checkout_slot_commit", argv=["--stage", "webkit", "--commit", "c" * 40])
        self.assertIsNone(why)
        for argv in (("fetch", "--quiet", "/mirror/WebKit.git", "c" * 40), ("checkout", "--force", "--detach", "--quiet", "c" * 40), ("clean", "-qfd")):
            self.assertTrue(w.ran("git", "-C", SRC, *argv), argv)
        self.assertIn("discarding 2 uncommitted path(s)", out)
        w.answer(["git", "-C", SRC, "fetch"], rc=1)
        self.assertIn("not in this machine's mirror", w.call("checkout_slot_commit", argv=["--commit", "c" * 40])[1])


class TestTheImageDirectory(unittest.TestCase):
    def test_a_previous_image_is_moved_aside_and_back_unless_a_new_one_was_built(self):
        w = World()
        w._set_file(IMAGE_DIR + "/x.wic.xz", "old")
        w.call("copies_aside")
        self.assertEqual((IMAGE_DIR + "/x.wic.xz" in w.files, w.files[IMAGE_DIR + ".previous/x.wic.xz"]), (False, "old"))
        w.call("copies_back")
        self.assertEqual(w.files[IMAGE_DIR + "/x.wic.xz"], "old")
        w.call("copies_aside")
        w._set_file(IMAGE_DIR + "/x.wic.xz", "new")
        w.call("copies_back")
        self.assertEqual((w.files[IMAGE_DIR + "/x.wic.xz"], w.isdir(IMAGE_DIR + ".previous")), ("new", False))


class TestTheStages(unittest.TestCase):
    def test_the_image_stage_bitbakes_through_the_helper_under_the_guard_and_says_done(self):
        w = World()
        w.react(["bash", "-c"], lambda a, f: (f._set_file(IMAGE_DIR + "/i.wic.xz", ""), Result(0))[1])
        rc, out, err = w.stage()
        self.assertEqual(rc, 0, out + err)
        (helper,) = [a for a in w.ran("bash", "-c") if "--build-image" in a]
        self.assertEqual(helper[:5], ["bash", "-c", '. %s/build/guard.sh && guard_run "$0" -- "$@"' % TOOLS, "16", HELPER])
        self.assertTrue(out.endswith("wk-yocto: stage 'image' done\n"), out)

    def test_a_helper_that_fails_fails_the_stage(self):
        w = World()
        w.answer(["bash", "-c"], rc=1)
        rc, out, err = w.stage("--stage", "toolchain")
        self.assertEqual(rc, 1)
        self.assertIn("wk-yocto: error: bitbake populate_sdk (the cross toolchain) failed", err)
        self.assertNotIn("done", out)

    def test_bitbake_is_reached_as_one_line_through_oe_init_build_env(self):
        w = World()
        self.assertEqual(w.stage("--stage", "fetch")[0], 0)
        (bb,) = w.ran("env", "WEBKIT_CROSS_TARGET=" + TARGET)
        self.assertEqual(bb[2], "WEBKIT_CROSS_VERSION=2.46.0")
        self.assertIn(". ./oe-init-build-env %s/build >/dev/null && " % WORK, bb[5])
        self.assertEqual(bb[6:], ["bitbake", "--runall=fetch", "-k", "webkit-dev-ci-tools"])

    def test_each_host_refusal_is_named(self):
        for name, edit, environ, said in (
                ("no such target", lambda w: w.answer([HELPER, "--print-available-targets"], out="rpi3-32bits-mesa\n"), ENV,
                 "'%s' is not a cross-target in this checkout" % TARGET),
                ("tooling", lambda w: w.answer(["sh", "-c"], out="chrpath\npython3-git\n"), ENV, "missing Yocto host tooling: chrpath python3-git"),
                ("root", lambda w: w.answer(["id", "-u"], out="0\n"), ENV, "refuses to run as root"),
                ("caches", lambda w: None, {"DL_DIR": "/d"}, "DL_DIR/SSTATE_DIR are not set")):
            with self.subTest(name):
                w = World()
                edit(w)
                rc, _, err = w.stage(environ=environ)
                self.assertEqual(rc, 1)
                self.assertIn(said, err)

    def test_a_slot_needs_the_sdk_first(self):
        w = World()
        rc, _, err = w.stage("--stage", "webkit")
        self.assertIn("--stage toolchain", err)
        self.assertFalse([a for a in w.ran("env") if "build-webkit" in " ".join(a)])

    def test_a_slot_is_built_with_the_branch_s_flags_its_preset_s_and_described(self):
        w = World().slot_ready()
        rc, out, err = w.stage("--stage", "webkit", "--commit", "c" * 40, "--slot", "pr", "--webkit-jobs", "6", "--cross-preset", pgo.COLLECT)
        self.assertEqual(rc, 0, out + err)
        (bw,) = [a for a in w.ran("env", "WK_MB_PER_JOB=2560") if "build-webkit" in " ".join(a)]
        self.assertEqual(bw[5:9], ["6", "env", "CC=clang", "CXX=clang++"])
        self.assertTrue({"--no-bubblewrap-sandbox", "--makeargs=-j6"} <= set(bw))
        self.assertEqual(bw[-1], "--cmakeargs=-DENABLE_X=OFF -DUSE_Y=ON %s %s" % (yt.WEBKIT_CMAKE, yt.cross_preset(pgo.COLLECT, "")[2]))
        (manifest,) = w.ran(*isolated_module(TOOLS + "/lib", "wk.slot"), "manifest")
        self.assertTrue({"build_preset=" + pgo.COLLECT, "slot=pr", "profile=" + PRESET} <= set(manifest))
        self.assertIn("build-id b1d", out)
        w = World().slot_ready('{"slot": "pr"}')
        rc, out, err = w.stage("--stage", "webkit", "--commit", "c" * 40, "--slot", "pr")
        self.assertIn("records no build_id", err)
        self.assertNotIn("slot ready", out)

    def test_the_cross_presets(self):
        self.assertEqual(yt.cross_preset("wpe-cross", ""), ("", "", ""))
        cc, cxx, cmake = yt.cross_preset(pgo.COLLECT, "")
        self.assertEqual((cc, cxx), ("clang", "clang++"))
        self.assertTrue({"-DLTO_MODE=thin", "-DENABLE_LLVM_PROFILE_GENERATION=ON", "-DUSE_PGO_PROFILE=OFF",
                         "-DPGO_PROFILE_DIR=" + pgo.BOARD_DIR} <= set(cmake.split()))
        self.assertTrue({"-DLTO_MODE=full", "-DENABLE_LLVM_PROFILE_GENERATION=OFF", "-DUSE_PGO_PROFILE=ON",
                         "-DPGO_PROFILE_PATH=/p.profdata"} <= set(yt.cross_preset(pgo.USE, "/p.profdata")[2].split()))
        for name, profile, said in (("nope", "", "no such cross preset"), (pgo.USE, "", "needs --pgo-profile"),
                                    (pgo.COLLECT, "/p", "only '%s' does" % pgo.USE)):
            with self.assertRaises(Failed) as cm:
                yt.cross_preset(name, profile)
            self.assertIn(said, str(cm.exception))

    def test_the_mix_runs_in_the_cross_environment_and_checks_after_it_merges(self):
        w = World()
        w.dirs.add(pgo.pgo_dir_in("pr"))
        rc, out, err = w.stage("--stage", "pgo-mix", "--slot", "pr")
        self.assertEqual(rc, 0, out + err)
        runs = [a for a in w.ran("bash", "-c") if "--cross-toolchain-run-cmd" in a]
        self.assertEqual([a[a.index("wk.pgo") + 1] for a in runs], ["mix", "check"])
        self.assertIn("no collection at " + pgo.pgo_dir_in("gone"), World().stage("--stage", "pgo-mix", "--slot", "gone")[2])

    def test_it_runs_as_a_file_in_the_workspace(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wk" / "sysimage" / "yocto_ws.py"), "--help"],
                            capture_output=True, text=True, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("--stage", cp.stdout)


if __name__ == "__main__":
    unittest.main()
