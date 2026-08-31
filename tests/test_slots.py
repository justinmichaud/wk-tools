"""WebKit slots: the manifest (lib/wkslot.py), the running-binary check the
wk-board run-benchmark driver makes (bench/wk_board_driver.py), the
in-workspace slot builder's refusals (image/buildroot-webkit.sh), and the
`wk sysimage webkit` / `wk ab` refusals that need no workspace or board.

The ELF the manifest describes is a real shared object linked here with
gcc and a chosen --build-id, read back through readelf.

Run: python3 -m unittest tests.test_slots -v
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import types
import os
import unittest
import unittest.mock
from pathlib import Path

from tests.support import REPO, WkTest, bash, run

WKSLOT = REPO / "lib" / "wkslot.py"
BUILD_ID = "3dca0e504a7438009c3eadf6113833fcc6297428"


def wkslot(*args, **kw):
    return subprocess.run([sys.executable, str(WKSLOT), *args], capture_output=True, text=True, **kw)


def linker_takes_build_id():
    """A build id is what a slot is identified by, so the ELF these tests
    read back has to carry one this machine's linker can be told to write.
    GNU ld and lld take -Wl,--build-id; the Apple linker refuses it, so on
    macOS there is nothing here to link and the tests skip. Probed once, by
    linking the same trivial shared object the tests do."""
    if shutil.which("gcc") is None:
        return False
    with tempfile.TemporaryDirectory() as d:
        cp = subprocess.run(
            ["gcc", "-shared", "-x", "c", "-", "-o", str(Path(d) / "probe.so"),
             "-Wl,--build-id=none"],
            input="int wk_probe(void) { return 0; }\n", text=True,
            capture_output=True)
    return cp.returncode == 0


def have_mirror():
    """`wk ab` resolves both of its commits in this machine's WebKit mirror
    (wk_mirror, lib/store.sh) and refuses without one, so a test that gets
    past the argument checks needs the mirror to be here."""
    cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; '
              '[ -d "$(wk_mirror)" ]')
    return cp.returncode == 0


def make_root(root, build_id=BUILD_ID):
    """A slot's install tree with one real shared object where the WebKit
    library goes, and the two other files the manifest looks for."""
    lib = root / "usr" / "lib"
    (root / "usr" / "libexec" / "wpe-webkit-1.1").mkdir(parents=True)
    (lib / "wpe-webkit-1.1" / "injected-bundle").mkdir(parents=True)
    flag = ["-Wl,--build-id=0x" + build_id] if build_id else ["-Wl,--build-id=none"]
    subprocess.run(["gcc", "-shared", "-x", "c", "-", "-o", str(lib / "libWPEWebKit-1.1.so.0.2.9"), *flag],
                   input="int wk_slot_probe(void) { return 42; }\n", text=True, check=True)
    (lib / "libWPEWebKit-1.1.so.0").symlink_to("libWPEWebKit-1.1.so.0.2.9")
    (root / "usr" / "libexec" / "wpe-webkit-1.1" / "WPEWebProcess").write_bytes(b"#!/bin/sh\n")
    (lib / "wpe-webkit-1.1" / "injected-bundle" / "libWPEInjectedBundle.so").write_bytes(b"bundle")


@unittest.skipUnless(linker_takes_build_id(),
                     "needs gcc and a linker that takes --build-id (GNU ld/lld)")
class TestManifest(WkTest):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        make_root(self.root)
        self.slot = self.tmp / "slot.json"
        cp = wkslot("manifest", str(self.root), str(self.slot),
                    "slot=pr", "profile=img", "commit=" + "a" * 40,
                    "browser=cog", "lib_dir=usr/lib", "exec_dir=usr/libexec/wpe-webkit-1.1",
                    "bundle_dir=usr/lib/wpe-webkit-1.1/injected-bundle")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.doc = json.loads(self.slot.read_text())

    def test_every_regular_file_is_listed_with_its_sha256(self):
        files = self.doc["files"]
        self.assertIn("usr/lib/libWPEWebKit-1.1.so.0.2.9", files)
        self.assertIn("usr/libexec/wpe-webkit-1.1/WPEWebProcess", files)
        self.assertNotIn("usr/lib/libWPEWebKit-1.1.so.0", files, "a symlink is not a file to hash")
        lib = self.root / "usr/lib/libWPEWebKit-1.1.so.0.2.9"
        self.assertEqual(files["usr/lib/libWPEWebKit-1.1.so.0.2.9"], hashlib.sha256(lib.read_bytes()).hexdigest())

    def test_build_id_is_what_readelf_reads(self):
        self.assertEqual(self.doc["build_id"], BUILD_ID)
        self.assertEqual(self.doc["lib_file"], "usr/lib/libWPEWebKit-1.1.so.0.2.9")

    def test_sums_are_sha256sum_c_input_for_an_unpacked_prefix(self):
        cp = wkslot("sums", str(self.slot), "--prefix", str(self.root))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        check = subprocess.run(["sha256sum", "-c", "-"], input=cp.stdout, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_env_and_expect_describe_the_deployed_prefix(self):
        env = wkslot("env", str(self.slot), "/var/wk/slots/pr/root").stdout.splitlines()
        self.assertIn("LD_LIBRARY_PATH=/var/wk/slots/pr/root/usr/lib", env)
        self.assertIn("WEBKIT_EXEC_PATH=/var/wk/slots/pr/root/usr/libexec/wpe-webkit-1.1", env)
        self.assertIn("WEBKIT_INJECTED_BUNDLE_PATH=/var/wk/slots/pr/root/usr/lib/wpe-webkit-1.1/injected-bundle", env)
        expect = json.loads(wkslot("expect", str(self.slot), "/var/wk/slots/pr/root").stdout)
        self.assertEqual(expect["process"], "WPEWebProcess")
        self.assertEqual(expect["exe"], "/var/wk/slots/pr/root/usr/libexec/wpe-webkit-1.1/WPEWebProcess")
        self.assertEqual(expect["lib"], "/var/wk/slots/pr/root/usr/lib/libWPEWebKit-1.1.so.0.2.9")
        self.assertEqual(expect["lib_sha256"], self.doc["files"]["usr/lib/libWPEWebKit-1.1.so.0.2.9"])
        self.assertEqual(expect["build_id"], BUILD_ID)


@unittest.skipUnless(linker_takes_build_id(),
                     "needs gcc and a linker that takes --build-id (GNU ld/lld)")
class TestManifestRefusesAnUnidentifiedBuild(WkTest):
    def test_no_build_id_note_no_slot(self):
        root = self.tmp / "root"
        make_root(root, build_id=None)
        cp = wkslot("manifest", str(root), str(self.tmp / "slot.json"), "lib_dir=usr/lib")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no build-id note", cp.stderr)


class TestVerified(WkTest):
    def _verified(self, lines):
        f = self.tmp / "verify.jsonl"
        f.write_text("".join(json.dumps(l) + "\n" for l in lines))
        return wkslot("verified", str(f))

    def test_all_ok_passes_and_counts(self):
        cp = self._verified([{"ok": True}, {"ok": True}])
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "2"))

    def test_one_failure_fails(self):
        self.assertNotEqual(self._verified([{"ok": True}, {"ok": False}]).returncode, 0)

    def test_no_evidence_is_not_verified(self):
        self.assertNotEqual(self._verified([]).returncode, 0)
        self.assertNotEqual(wkslot("verified", str(self.tmp / "missing")).returncode, 0)


def load_driver():
    """bench/wk_board_driver.py imports run-benchmark's BrowserDriver; a
    stand-in module lets the pure functions be tested without a checkout."""
    if "webkitpy.benchmark_runner.browser_driver.browser_driver" not in sys.modules:
        base = types.ModuleType("webkitpy.benchmark_runner.browser_driver.browser_driver")

        class BrowserDriver:
            def __init__(self, browser_args):
                self.browser_args = browser_args

        base.BrowserDriver = BrowserDriver
        for name in ("webkitpy", "webkitpy.benchmark_runner", "webkitpy.benchmark_runner.browser_driver"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules[base.__name__] = base
    import importlib.util

    spec = importlib.util.spec_from_file_location("wk_board_driver", REPO / "bench" / "wk_board_driver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBoardDriver(unittest.TestCase):
    """The two judgements the driver makes on its own: which URL the board
    opens, and whether the process that reported is the slot under test."""

    def setUp(self):
        self.d = load_driver()
        self.expect = {"process": "WPEWebProcess",
                       "exe": "/var/wk/slots/pr/root/usr/libexec/wpe-webkit-1.1/WPEWebProcess",
                       "lib": "/var/wk/slots/pr/root/usr/lib/libWPEWebKit-1.1.so.0.2.9",
                       "lib_sha256": "ab" * 32, "build_id": BUILD_ID}
        self.good = {"pids": "1", "exe": self.expect["exe"], "lib_inode": "4711",
                     "mapped": self.expect["lib"], "other_webkit": "", "lib_sha256": "ab" * 32}

    def test_every_launch_ends_the_old_browser_and_starts_cold(self):
        """prepare_env runs before each launch: the kill, then the cache
        reset -- a second launch at a cached URL never starts the benchmark
        (bench/wk_board_driver.py, prepare_env)."""
        env = {"WK_BOARD_SSH": "ssh board", "WK_BOARD_LAUNCH": "cog", "WK_BOARD_KILL": "killall cog",
               "WK_BOARD_RESET": "rm -rf /root/.cache/WebKitCache", "WK_BOARD_URL": "127.0.0.1:1"}
        with unittest.mock.patch.dict(os.environ, env):
            drv = self.d.WkBoardDriver([])
        ran = []
        drv._remote = lambda text, check=True, capture=False: ran.append((text, check)) or ""
        drv.prepare_env(None)
        self.assertEqual(ran, [("killall cog", False), ("rm -rf /root/.cache/WebKitCache", True)])

    def test_url_keeps_path_and_query_and_swaps_host(self):
        url = self.d.rewrite_url("http://127.0.0.1:41235/Speedometer/index.html?startAutomatically=true", "127.0.0.1:5000")
        self.assertEqual(url, "http://127.0.0.1:5000/Speedometer/index.html?startAutomatically=true")

    def test_the_slot_itself_passes(self):
        self.assertEqual(self.d.judge(self.expect, self.good), [])

    def test_the_images_own_webkit_is_caught(self):
        got = dict(self.good, mapped="", other_webkit="/usr/lib/libWPEWebKit-1.1.so.0.2.9 ", lib_sha256="0" * 64)
        problems = self.d.judge(self.expect, got)
        self.assertTrue(any("has not mapped" in p for p in problems), problems)
        self.assertTrue(any("another WebKit" in p for p in problems), problems)
        self.assertTrue(any("sha256" in p for p in problems), problems)

    def test_a_different_slots_bytes_are_caught(self):
        problems = self.d.judge(self.expect, dict(self.good, lib_sha256="f" * 64))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("sha256", problems[0])

    def test_no_process_is_its_own_problem(self):
        problems = self.d.judge(self.expect, {"pids": "0"})
        self.assertEqual(len(problems), 1)
        self.assertIn("no WPEWebProcess", problems[0])


class TestSlotBuilderRefusals(WkTest):
    """image/buildroot-webkit.sh checks its arguments before it touches a
    tree, so these run the script for real, outside any workspace."""

    SCRIPT = REPO / "image" / "buildroot-webkit.sh"

    def test_parses(self):
        self.assertEqual(bash(f'bash -n "{self.SCRIPT}"').returncode, 0)

    def test_short_sha_is_refused(self):
        cp = bash(f'"{self.SCRIPT}" --name img --commit abc123 --slot base')
        self.assertEqual(cp.returncode, 1)
        self.assertIn("full 40-character sha", cp.stderr)

    def test_every_argument_is_required(self):
        cp = bash(f'"{self.SCRIPT}" --commit {"a" * 40} --slot base')
        self.assertEqual(cp.returncode, 1)
        self.assertIn("--name is required", cp.stderr)

    def test_unknown_option_is_a_usage_error(self):
        self.assertEqual(bash(f'"{self.SCRIPT}" --bogus').returncode, 2)


class TestSysimageWebkitRefusals(WkTest):
    def test_a_yocto_slot_is_the_webkit_stage(self):
        """A yocto image's slot is WebKit's own build-webkit --cross-target
        (the workspace's `webkit` stage), planned by the yocto dry run."""
        cp = run("sysimage", "webkit", "webkit-2.52-yocto-rpi3-32", "--commit", "a" * 40, "--slot", "base", "--dry-run", timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("webkit", cp.stdout)

    def test_a_yocto_slot_needs_both_commit_and_slot(self):
        cp = run("sysimage", "webkit", "webkit-2.52-yocto-rpi3-32", "--slot", "base", "--dry-run", timeout=60)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("both --commit", cp.stdout)

    def test_commit_and_slot_are_required(self):
        cp = run("sysimage", "webkit", "wpewebkit-2.38-buildroot-rpi3-32", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("usage: wk sysimage webkit", cp.stdout)

    def test_a_short_sha_is_refused(self):
        cp = run("sysimage", "webkit", "wpewebkit-2.38-buildroot-rpi3-32", "--commit", "04abe098", "--slot", "base", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("40 hex digits", cp.stdout)

    def test_a_slot_name_is_a_directory_name(self):
        cp = run("sysimage", "webkit", "wpewebkit-2.38-buildroot-rpi3-32", "--commit", "a" * 40, "--slot", "../x", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("not usable", cp.stdout)


class TestSysimageLs(WkTest):
    """`wk sysimage ls` is read-only and answers on any host: every function
    it reaches for is defined, and a slot directory with no manifest yet (a
    build that died) is simply not a slot."""

    def test_ls_answers_cleanly(self):
        cp = run("sysimage", "ls", timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("command not found", cp.stdout)


class TestAbRefusals(WkTest):
    """`wk ab` refuses a malformed request before it reaches the mirror, a
    board or GitHub."""

    def test_devices_are_required(self):
        cp = run("ab", "wpe:1725", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--devices", cp.stdout)

    def test_bits_are_32_or_64(self):
        cp = run("ab", "wpe:1725", "--devices", "rpi3", "--bits", "16", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--bits takes 32 or 64", cp.stdout)

    def test_rounds_is_a_number(self):
        cp = run("ab", "wpe:1725", "--devices", "rpi3", "--rounds", "many", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--rounds takes a number", cp.stdout)

    @unittest.skipUnless(have_mirror(), "needs this machine's WebKit mirror ('wk sync' makes one)")
    def test_a_device_carries_its_own_width(self):
        """rpi3-32 beside rpi5-64 in one command: the suffix, not --bits,
        decides each device's image, so mixed widths are one run."""
        cp = run("ab", "wpe:1725", "--devices", "rpi3-32,rpi5-64", "--release", "2.38", "--dry-run", timeout=240)
        out = cp.stdout
        self.assertNotIn("more than one image", out)
        if "cannot be built yet" in out:
            self.skipTest("the rpi5 2.38 configuration declares owed work; the width was still resolved")
        self.assertIn("wpewebkit-2.38-buildroot-rpi3-32", out)
        self.assertIn("wpewebkit-2.38-buildroot-rpi5-64", out)

    def test_a_plan_is_a_name(self):
        cp = run("ab", "wpe:1725", "--devices", "rpi3", "--plan", "x;y", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("not a plan name", cp.stdout)

    def test_a_forks_branch_is_not_an_ab_spec(self):
        cp = run("ab", "alice:eng/branch", "--devices", "rpi3", timeout=30)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("pull request", cp.stdout)


if __name__ == "__main__":
    unittest.main()
