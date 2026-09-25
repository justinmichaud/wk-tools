"""The yocto build as it runs inside a workspace (lib/wk/sysimage/yocto_target.py) against a Fake machine: the
environment bitbake is handed, local.conf and bblayers.conf and their convergence on a re-run, a target derived
for a branch that lacks it, the git index the toolchains can open, the slot's commit, the image moved aside and
checked for freshness, the SDK asked for before a slot, and each stage's commands.

Run: python3 tests/run.py -k test_yocto_target
"""
import configparser
import contextlib
import io
import os
import subprocess
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import yocto_target as yt  # noqa: E402

TARGET = "rpi4-64bits-mesa"
SRC = "/src/WebKit"
WORK = SRC + "/WebKitBuild/CrossToolChains/" + TARGET
CONF = WORK + "/build/conf/local.conf"
LAYERS = WORK + "/build/conf/bblayers.conf"
IMAGE_DIR = WORK + "/build/image"
TOOLS = "/opt/wk-tools"
ENV = {"DL_DIR": "/cache/yocto/downloads", "SSTATE_DIR": "/cache/yocto/sstate", "WK_MIRROR": "/mirror/WebKit.git",
       "LANG": "C.UTF-8"}
TARGETS = """[rpi4-64bits-mesa]
repo_manifest_path = rpi/manifest.xml
conf_bblayers_path = rpi/bblayers.conf
conf_local_path = rpi/local-rpi4-64bits-mesa.conf
image_basename = webkit-dev-ci-tools
image_types = tar.xz wic.xz
patch_file_path = meta-openembedded_and_meta-webkit.patch
environment[BUILD_WEBKIT_ARGS] = --no-bubblewrap-sandbox --cmakeargs="-DENABLE_X=OFF -DUSE_Y=ON"
"""
LOCAL = 'DISTRO = "webkitdevci"\n# Machine selection\nMACHINE = "raspberrypi4-64"\nBB_DISKMON_DIRS ??= "HALT,${TMPDIR},100M,1K"\n'


def args(*more):
    return yt.parse(["--target", TARGET, "--jobs", "16", "--image", "webkit-dev-ci-tools"] + list(more))


class World(Fake):
    """A workspace whose layers are synced: the checkout, the helper's workdir and its two confs."""

    def __init__(self):
        super().__init__("ws")
        self.dirs.update({SRC, SRC + "/.git", WORK + "/.git", TOOLS})
        self.files.update({CONF: 'MACHINE = "raspberrypi4-64"\n', LAYERS: 'BBLAYERS ?= "a b"\n',
                           WORK + "/.target-info-version": "rpi4-64bits-mesa 2.46.0\n",
                           SRC + "/Tools/yocto/targets.conf": TARGETS, SRC + "/Tools/yocto/rpi/local-rpi4-64bits-mesa.conf": LOCAL})
        for layer in yt.LAYERS + ("meta-wk-multilib",):
            self.files["%s/image/yocto/%s/conf/layer.conf" % (TOOLS, layer)] = ""
        self.answer(["sh", "-c"], out="")
        self.answer(["id", "-u"], out="1000\n")
        self.answer(["git"])
        self.answer([SRC + "/Tools/Scripts/cross-toolchain-helper", "--print-available-targets"], out="rpi3-32bits-mesa\n%s\n" % TARGET)
        self.answer(["bash"])
        self.answer(["env"])
        self.answer(["python3"])
        self.answer(["cp"])
        self.react(["mv", "-T"], self._mv)
        self.clock = FakeClock()
        self.react(["find", IMAGE_DIR], lambda a, f: Result(0, "%d.5\n" % f.clock.now()) if IMAGE_DIR in f.dirs else Result(1))

    def _mv(self, argv, f):
        src, dst = argv[2], argv[3]
        for d in [d for d in self.dirs if d == src or d.startswith(src + "/")]:
            self.dirs.discard(d)
            self.dirs.add(dst + d[len(src):])
        for p in [p for p in self.files if p.startswith(src + "/")]:
            self.files[dst + p[len(src):]] = self.files.pop(p)
        return Result(0)

    def build(self, a):
        return yt.Build(a, self, dict(ENV), self.clock, TOOLS)

    def ran(self, *prefix):
        return [e[1] for e in self.effects if e[0] in ("run", "run_tty") and tuple(e[1][:len(prefix)]) == prefix]


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        try:
            fn(*a)
        except yt.Failed as e:
            return out.getvalue(), str(e)
    return out.getvalue(), None


class TestTheEnvironment(unittest.TestCase):
    def env(self, *more, environ=None, locales="C\nC.utf8\nPOSIX\n"):
        base = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/x", "CPATH": "/y", "SSTATE_DIR": "/cache/sstate",
                "BB_ENV_PASSTHROUGH_ADDITIONS": "FOO"}
        return yt.build_env(dict(base, **(environ or {})), args(*more), locales)

    def test_the_sdk_s_dev_environment_is_taken_away(self):
        env = self.env()
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("CPATH", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_git_writes_an_index_the_toolchains_can_open(self):
        env = self.env()
        self.assertEqual([env[k] for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                                           "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1")],
                         ["2", "index.version", "2", "index.skipHash", "false"])

    def test_bitbake_is_told_to_pass_the_caches_and_the_pin_through(self):
        words = self.env()["BB_ENV_PASSTHROUGH_ADDITIONS"].split()
        self.assertEqual(words[0], "FOO")
        for name in ("DL_DIR", "SSTATE_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_VALUE_1"):
            self.assertIn(name, words)

    def test_the_helper_keeps_the_lane_s_workdir_when_the_commit_moves(self):
        self.assertEqual(self.env()["WEBKIT_CROSS_WIPE_ON_CHANGE"], "0")

    def test_a_utf8_locale_is_en_us_when_there_is_one_and_c_otherwise(self):
        self.assertEqual(self.env()["LANG"], "C.UTF-8")
        self.assertEqual(self.env(locales="C\nen_US.utf8\n")["LC_ALL"], "en_US.UTF-8")

    def test_the_booked_budget_is_the_guard_s_and_sstate_is_namespaced_by_host_image(self):
        env = self.env("--mem-budget", "20480", "--sstate-ns", "wk-yocto-host-24.04-abcd")
        self.assertEqual(env["WK_MEM_BUDGET_MB"], "20480")
        self.assertEqual(env["SSTATE_DIR"], "/cache/sstate/wk-yocto-host-24.04-abcd")


class TestLocalConf(unittest.TestCase):
    def test_bitbake_threads_are_a_quarter_of_the_jobs(self):
        self.assertEqual(yt.threads(16), (4, 4))
        self.assertEqual(yt.threads(3), (1, 3))

    def test_the_block_names_the_caches_the_jobs_and_what_every_image_carries(self):
        text = yt.local_conf(args("--chromium", "1"), ENV, 16, None)
        for line in ('DL_DIR = "/cache/yocto/downloads"', 'BB_NUMBER_THREADS = "4"', 'PARALLEL_MAKE = "-j 4"',
                     'PARALLEL_MAKE:pn-clang = "-j 16"', 'IMAGE_INSTALL:append = " tailscale"',
                     'IMAGE_INSTALL:append = " wk-wifi-join"', 'IMAGE_INSTALL:append = " wk-card-priv"',
                     'INHERIT += "rm_work"', 'RM_WORK_EXCLUDE += "webkit-dev-ci-tools"'):
            self.assertIn(line + "\n", text)
        self.assertNotIn("chromium", text.replace("Chromium", ""))

    def test_the_flags_drop_what_they_name(self):
        text = yt.local_conf(args("--tailnet", "0", "--rm-work", "0", "--chromium", "0"), ENV, 16, None)
        self.assertNotIn(" tailscale", text)
        self.assertNotIn("rm_work", text)
        self.assertIn('IMAGE_INSTALL:remove = "chromium-ozone-wayland"', text)

    def test_a_multilib_image_names_its_tune(self):
        text = yt.local_conf(args("--multilib", "lib32", "--multilib-tune", "armv7athf-neon"), ENV, 16, None)
        self.assertIn('MULTILIBS = "multilib:lib32"\nDEFAULTTUNE:virtclass-multilib-lib32 = "armv7athf-neon"', text)

    def test_a_rerun_replaces_its_own_block_and_keeps_the_branch_s(self):
        w = World()
        b = w.build(args())
        quiet(b.configure_local_conf)
        once = w.files[CONF]
        quiet(b.configure_local_conf)
        self.assertEqual(w.files[CONF], once)
        self.assertTrue(once.startswith('MACHINE = "raspberrypi4-64"\n'))
        self.assertEqual(once.count(yt.MARKER), 1)

    def test_a_board_s_append_is_read_from_the_tree_and_lands_last(self):
        w = World()
        w.files[TOOLS + "/image/boards/rpi5/local.conf.append"] = 'KBUILD_DEFCONFIG:raspberrypi5 = "bcm2711_defconfig"\n'
        quiet(w.build(args("--board", "rpi5")).configure_local_conf)
        self.assertTrue(w.files[CONF].endswith('KBUILD_DEFCONFIG:raspberrypi5 = "bcm2711_defconfig"\n'))


class TestLayers(unittest.TestCase):
    def test_the_local_layers_are_added_once(self):
        w = World()
        b = w.build(args("--multilib", "lib32", "--multilib-tune", "t"))
        quiet(b.configure_bblayers)
        quiet(b.configure_bblayers)
        text = w.files[LAYERS]
        for layer in yt.LAYERS + ("meta-wk-multilib",):
            self.assertEqual(text.count('BBLAYERS += "%s/image/yocto/%s"' % (TOOLS, layer)), 1, layer)

    def test_no_local_layer_is_the_branch_s_own_configuration(self):
        w = World()
        quiet(w.build(args()).configure_bblayers)
        quiet(w.build(args("--local-layer", "0")).configure_bblayers)
        self.assertEqual(w.files[LAYERS], 'BBLAYERS ?= "a b"\n')

    def test_a_missing_layer_is_refused(self):
        w = World()
        del w.files[TOOLS + "/image/yocto/meta-wk-rescue/conf/layer.conf"]
        self.assertIn("no layer at", quiet(w.build(args()).configure_bblayers)[1])


class TestPortingATarget(unittest.TestCase):
    """A release branch that predates a target gets its glue derived from one it has: the section with one
    path changed, pointing at that target's local.conf with one MACHINE line changed."""

    def port(self, target="rpi5-64bits-mesa", frm="rpi4-64bits-mesa", image="", local=LOCAL):
        try:
            return yt.port_target(TARGETS, lambda rel: local if rel.endswith("rpi4-64bits-mesa.conf") else None,
                                  target, frm, "raspberrypi5", image), None
        except yt.Failed as e:
            return None, str(e)

    def test_the_section_and_its_local_conf_are_derived(self):
        (conf, locals_), _ = self.port()
        c = configparser.ConfigParser()
        c.read_string(conf)
        new, old = c["rpi5-64bits-mesa"], c["rpi4-64bits-mesa"]
        self.assertEqual(new["conf_local_path"], "rpi/local-rpi5-64bits-mesa.conf")
        for k in ("repo_manifest_path", "conf_bblayers_path", "image_basename", "image_types", "patch_file_path"):
            self.assertEqual(new[k], old[k], k)
        text = locals_["rpi/local-rpi5-64bits-mesa.conf"]
        self.assertIn('MACHINE = "raspberrypi5"', text)
        self.assertNotIn("raspberrypi4-64", text)
        self.assertIn("BB_DISKMON_DIRS", text)

    def test_a_multilib_target_names_its_own_image_recipe(self):
        (conf, _), _ = self.port(target="rpi5-32bits-mesa", image="lib32-webkit-dev-ci-tools")
        c = configparser.ConfigParser()
        c.read_string(conf)
        self.assertEqual(c["rpi5-32bits-mesa"]["image_basename"], "lib32-webkit-dev-ci-tools")

    def test_a_branch_that_already_has_it_is_left_alone(self):
        self.assertEqual(self.port(target="rpi4-64bits-mesa"), (None, None))

    def test_what_cannot_be_derived_is_refused(self):
        self.assertIn("rpi4-64bits-mesa", self.port(frm="rpi9-64bits-mesa")[1])
        self.assertIn("MACHINE 0 times", self.port(local='DISTRO = "x"\n')[1])
        self.assertIn("not in this checkout", self.port(local=None)[1])

    def test_the_build_writes_both_into_the_checkout_and_converges(self):
        w = World()
        b = w.build(args("--target", "rpi5-64bits-mesa", "--port-target-from", "rpi4-64bits-mesa", "--port-machine", "raspberrypi5"))
        quiet(b.port)
        self.assertIn("[rpi5-64bits-mesa]", w.files[SRC + "/Tools/yocto/targets.conf"])
        self.assertIn(SRC + "/Tools/yocto/rpi/local-rpi5-64bits-mesa.conf", w.files)
        writes = len([e for e in w.effects if e[0] == "write"])
        out, _ = quiet(b.port)
        self.assertIn("already has", out)
        self.assertEqual(len([e for e in w.effects if e[0] == "write"]), writes)

    def test_a_port_without_a_machine_is_refused(self):
        w = World()
        self.assertIn("no YOC_MACHINE", quiet(w.build(args("--port-target-from", "rpi3-32bits-mesa")).port)[1])


class TestTheCheckout(unittest.TestCase):
    def test_the_index_is_pinned_to_a_format_libgit2_opens(self):
        w = World()
        quiet(w.build(args()).refresh_git_index, SRC)
        self.assertIn(("git", "-C", SRC, "config", "index.version", "2"), w.ran("git"))
        self.assertIn(("git", "-C", SRC, "config", "index.skipHash", "false"), w.ran("git"))
        self.assertFalse(w.ran("git", "-C", SRC, "read-tree"))

    def test_an_unreadable_index_is_rebuilt_from_head(self):
        w = World()
        w.answer(["git", "-C", SRC, "ls-files"], rc=128)
        w.answer(["git", "-C", SRC, "rev-parse", "--absolute-git-dir"], out=SRC + "/.git\n")
        out, _ = quiet(w.build(args()).refresh_git_index, SRC)
        self.assertIn(("remove", SRC + "/.git/index"), w.effects)
        self.assertTrue(w.ran("git", "-C", SRC, "read-tree", "HEAD"))
        self.assertIn("rebuilt an unreadable git index", out)

    def test_a_directory_that_is_no_repository_is_left_alone(self):
        w = World()
        quiet(w.build(args()).refresh_git_index, "/tmp/plain")
        self.assertFalse(w.ran("git"))

    def test_a_slot_s_commit_is_fetched_from_the_mirror_and_forced_in(self):
        w = World()
        w.answer(["git", "-C", SRC, "cat-file"], rc=1)
        w.answer(["git", "-C", SRC, "status", "--porcelain"], out=" M a\n?? b\n")
        out, why = quiet(w.build(args("--stage", "webkit", "--commit", "c" * 40)).checkout_slot_commit)
        self.assertIsNone(why)
        self.assertTrue(w.ran("git", "-C", SRC, "fetch", "--quiet", "/mirror/WebKit.git", "c" * 40))
        self.assertTrue(w.ran("git", "-C", SRC, "checkout", "--force", "--detach", "--quiet", "c" * 40))
        self.assertTrue(w.ran("git", "-C", SRC, "clean", "-qfd"))
        self.assertIn("discarding 2 uncommitted path(s)", out)

    def test_a_commit_the_mirror_lacks_names_where_it_comes_from(self):
        w = World()
        w.answer(["git", "-C", SRC, "cat-file"], rc=1)
        w.answer(["git", "-C", SRC, "fetch"], rc=1)
        self.assertIn("not in this machine's mirror", quiet(w.build(args("--commit", "c" * 40)).checkout_slot_commit)[1])


class TestTheImageDirectory(unittest.TestCase):
    def test_a_previous_image_is_moved_aside_before_the_helper_can_serve_it(self):
        w = World()
        w._set_file(IMAGE_DIR + "/x.wic.xz", "old")
        quiet(w.build(args()).copies_aside)
        self.assertNotIn(IMAGE_DIR + "/x.wic.xz", w.files)
        self.assertEqual(w.files[IMAGE_DIR + ".previous/x.wic.xz"], "old")

    def test_a_stage_that_built_none_gets_the_previous_one_back(self):
        w = World()
        w._set_file(IMAGE_DIR + ".previous/x.wic.xz", "old")
        quiet(w.build(args()).copies_back)
        self.assertEqual(w.files[IMAGE_DIR + "/x.wic.xz"], "old")

    def test_a_stage_that_built_one_drops_the_aside_copy(self):
        w = World()
        w._set_file(IMAGE_DIR + ".previous/x.wic.xz", "old")
        w._set_file(IMAGE_DIR + "/x.wic.xz", "new")
        quiet(w.build(args()).copies_back)
        self.assertEqual(w.files[IMAGE_DIR + "/x.wic.xz"], "new")
        self.assertFalse(w.isdir(IMAGE_DIR + ".previous"))

    def test_an_image_older_than_the_stage_is_refused(self):
        w = World()
        w._set_file(IMAGE_DIR + "/x.wic.xz", "")
        b = w.build(args())
        self.assertIsNone(quiet(b.verify_fresh, w.clock.now())[1])
        self.assertIn("no new image", quiet(b.verify_fresh, w.clock.now() + 10)[1])

    def test_no_image_at_all_is_refused(self):
        self.assertIn("left nothing behind", quiet(World().build(args()).verify_fresh, 0)[1])


class TestTheStages(unittest.TestCase):
    def run_stage(self, w, *more):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = yt.main(["--target", TARGET, "--jobs", "16", "--image", "webkit-dev-ci-tools"] + list(more),
                         environ=dict(ENV), machine=w, clock=w.clock, tools=TOOLS)
        return rc, out.getvalue(), err.getvalue()

    def test_the_image_stage_bitbakes_through_the_helper_under_the_guard_and_says_done(self):
        w = World()
        w.react([SRC + "/Tools/Scripts/cross-toolchain-helper"], lambda a, f: Result(0, "rpi3-32bits-mesa\n%s\n" % TARGET))
        w.react(["bash", "-c"], lambda a, f: (f._set_file(IMAGE_DIR + "/i.wic.xz", ""), Result(0))[1])
        rc, out, err = self.run_stage(w)
        self.assertEqual(rc, 0, out + err)
        (helper,) = [a for a in w.ran("bash", "-c") if "--build-image" in a]
        self.assertEqual(helper[:5], ("bash", "-c", '. %s/build/guard.sh && guard_run "$0" -- "$@"' % TOOLS, "16",
                                      SRC + "/Tools/Scripts/cross-toolchain-helper"))
        self.assertTrue(out.endswith("wk-yocto: stage 'image' done\n"), out)

    def test_a_helper_that_fails_fails_the_stage(self):
        w = World()
        w.answer(["bash", "-c"], rc=1)
        rc, out, err = self.run_stage(w, "--stage", "toolchain")
        self.assertEqual(rc, 1)
        self.assertIn("wk-yocto: error: bitbake populate_sdk (the cross toolchain) failed", err)
        self.assertNotIn("done", out)

    def test_bitbake_is_reached_as_one_line_through_oe_init_build_env(self):
        w = World()
        rc, out, err = self.run_stage(w, "--stage", "fetch")
        self.assertEqual(rc, 0, out + err)
        (bb,) = w.ran("env", "WEBKIT_CROSS_TARGET=rpi4-64bits-mesa")
        self.assertEqual(bb[2], "WEBKIT_CROSS_VERSION=2.46.0")
        self.assertIn(". ./oe-init-build-env %s/build >/dev/null && " % WORK, bb[5])
        self.assertEqual(bb[6:], ("bitbake", "--runall=fetch", "-k", "webkit-dev-ci-tools"))

    def test_a_target_this_checkout_lacks_is_refused_listing_what_it_has(self):
        w = World()
        rc, out, err = self.run_stage(w, "--target", "rpi9-64bits-mesa")
        self.assertEqual(rc, 1)
        self.assertIn("'rpi9-64bits-mesa' is not a cross-target in this checkout", err)
        self.assertIn("      rpi3-32bits-mesa\n", err)

    def test_missing_host_tooling_is_named_together(self):
        w = World()
        w.answer(["sh", "-c"], out="chrpath\npython3-git\n")
        rc, _, err = self.run_stage(w)
        self.assertIn("missing Yocto host tooling: chrpath python3-git", err)

    def test_a_root_shell_and_a_missing_cache_are_refused(self):
        w = World()
        w.answer(["id", "-u"], out="0\n")
        self.assertIn("refuses to run as root", self.run_stage(w)[2])
        w = World()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            yt.main(["--target", TARGET, "--jobs", "4"], environ={"DL_DIR": "/d"}, machine=w, clock=w.clock, tools=TOOLS)
        self.assertIn("DL_DIR/SSTATE_DIR are not set", err.getvalue())

    def test_a_slot_needs_the_sdk_first(self):
        w = World()
        rc, _, err = self.run_stage(w, "--stage", "webkit")
        self.assertEqual(rc, 1)
        self.assertIn("--stage toolchain", err)
        self.assertFalse([a for a in w.ran("env") if "build-webkit" in " ".join(a)])

    def test_a_slot_is_built_with_the_branch_s_flags_and_ours_after_and_described(self):
        w = World()
        sdk = WORK + "/build/toolchain"
        for f in (sdk + "/.toolchain_path_configured", sdk + "/environment-setup-cortexa72",
                  SRC + "/WebKitBuild/WPE/Release_%s/bin/MiniBrowser" % TARGET):
            w._set_file(f, "")
        rc, out, err = self.run_stage(w, "--stage", "webkit", "--commit", "c" * 40, "--slot", "pr", "--profile", "p",
                                      "--webkit-jobs", "6", "--cross-config", "wpe-cross-pgo-collect", "--cross-cc", "clang",
                                      "--cross-cxx", "clang++", "--cross-cmake=-DENABLE_LLVM_PROFILE_GENERATION=ON")
        self.assertEqual(rc, 0, out + err)
        (bw,) = [a for a in w.ran("env", "WK_MB_PER_JOB=2560") if "build-webkit" in " ".join(a)]
        self.assertEqual(bw[5], "6")
        self.assertIn("--no-bubblewrap-sandbox", bw)
        self.assertIn("--makeargs=-j6", bw)
        self.assertEqual(bw[-1], "--cmakeargs=-DENABLE_X=OFF -DUSE_Y=ON %s -DENABLE_LLVM_PROFILE_GENERATION=ON" % yt.WEBKIT_CMAKE)
        self.assertIn(("env", "CC=clang", "CXX=clang++"), [tuple(bw[6:9])])
        (manifest,) = w.ran("python3", TOOLS + "/lib/wkslot.py", "manifest")
        self.assertIn("build_config=wpe-cross-pgo-collect", manifest)
        self.assertIn("slot=pr", manifest)

    def test_the_mix_runs_in_the_cross_environment_and_checks_after_it_merges(self):
        w = World()
        w.dirs.add("/src/WebKit/WebKitBuild/wk-pgo/pr")
        rc, out, err = self.run_stage(w, "--stage", "pgo-mix", "--pgo-dir", "/src/WebKit/WebKitBuild/wk-pgo/pr", "--pgo-lib", "WPEWebKit")
        self.assertEqual(rc, 0, out + err)
        runs = [a for a in w.ran("bash", "-c") if "--cross-toolchain-run-cmd" in a]
        self.assertEqual([a[a.index("wk.pgo") + 1] for a in runs], ["mix", "check"])
        self.assertTrue(all("PYTHONPATH=%s/lib" % TOOLS in a for a in runs), runs)

    def test_the_mix_needs_its_collection(self):
        w = World()
        rc, _, err = self.run_stage(w, "--stage", "pgo-mix", "--pgo-dir", "/nowhere", "--pgo-lib", "WPEWebKit")
        self.assertIn("no collection at /nowhere", err)

    def test_it_runs_as_a_file_in_the_workspace(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wk" / "sysimage" / "yocto_target.py"), "--help"],
                            capture_output=True, text=True, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("--target", cp.stdout)


if __name__ == "__main__":
    unittest.main()
