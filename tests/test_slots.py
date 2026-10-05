"""WebKit slots: the manifest (lib/wk/slot.py) over a real linked ELF, the board driver's running-binary check, and
`wk sysimage webkit` refusals."""
import contextlib
import hashlib
import io
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

from tests.support import (REPO, WkTest, container_side, container_store, run_here,
                           requires_container_place, run)

sys.path.insert(0, str(REPO / "lib"))
from wk.bench import board  # noqa: E402
from wk.machine import Fake, isolated_module  # noqa: E402

BUILD_ID = "3dca0e504a7438009c3eadf6113833fcc6297428"


def wkslot(*args, **kw):
    return subprocess.run(isolated_module(REPO / "lib", "wk.slot", sys.executable) + list(args),
                          capture_output=True, text=True, **kw)


def linker_takes_build_id():
    """GNU ld and lld take -Wl,--build-id; the Apple linker refuses it."""
    if shutil.which("gcc") is None:
        return False
    with tempfile.TemporaryDirectory() as d:
        cp = subprocess.run(
            ["gcc", "-shared", "-x", "c", "-", "-o", str(Path(d) / "probe.so"),
             "-Wl,--build-id=none"],
            input="int wk_probe(void) { return 0; }\n", text=True,
            capture_output=True)
    return cp.returncode == 0


def make_root(root, build_id=BUILD_ID):
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
        doc = json.loads(self.slot.read_text())
        env = board.slot_env(doc, "/var/wk/slots/pr/root")
        self.assertIn("LD_LIBRARY_PATH=/var/wk/slots/pr/root/usr/lib", env)
        self.assertIn("WEBKIT_EXEC_PATH=/var/wk/slots/pr/root/usr/libexec/wpe-webkit-1.1", env)
        self.assertIn("WEBKIT_INJECTED_BUNDLE_PATH=/var/wk/slots/pr/root/usr/lib/wpe-webkit-1.1/injected-bundle", env)
        expect = board.slot_expect(doc, "/var/wk/slots/pr/root/")
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
        return board.slot_verified(str(f))

    def test_all_ok_counts_and_a_failure_or_no_evidence_is_not_verified(self):
        for lines, want in (([{"ok": True}, {"ok": True}], 2), ([{"ok": True}, {"ok": False}], 0), ([], 0)):
            with self.subTest(lines=lines):
                self.assertEqual(self._verified(lines), want)
        self.assertEqual(board.slot_verified(str(self.tmp / "missing")), 0)


def load_driver():
    """board_driver.py with a stand-in for run-benchmark's BrowserDriver."""
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

    spec = importlib.util.spec_from_file_location("wk_board_driver", REPO / "lib" / "wk" / "bench" / "board_driver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBoardDriver(unittest.TestCase):

    def setUp(self):
        self.d = load_driver()
        self.expect = {"process": "WPEWebProcess",
                       "exe": "/var/wk/slots/pr/root/usr/libexec/wpe-webkit-1.1/WPEWebProcess",
                       "lib": "/var/wk/slots/pr/root/usr/lib/libWPEWebKit-1.1.so.0.2.9",
                       "lib_sha256": "ab" * 32, "build_id": BUILD_ID}
        self.good = {"pids": "1", "exe": self.expect["exe"], "lib_inode": "4711",
                     "mapped": self.expect["lib"], "other_webkit": "", "lib_sha256": "ab" * 32}

    def test_every_launch_ends_the_old_browser_and_starts_cold(self):
        env = {"WK_BOARD_DEST": "board", "WK_BOARD_OPTS": "[]", "WK_BOARD_LIB": str(REPO / "lib"), "WK_BOARD_LAUNCH": "cog", "WK_BOARD_KILL": "killall cog",
               "WK_BOARD_RESET": "rm -rf /root/.cache/WebKitCache", "WK_BOARD_URL": "127.0.0.1:1"}
        with unittest.mock.patch.dict(os.environ, env):
            drv = self.d.WkBoardDriver([])
        ran = []
        drv._remote = lambda text, check=True, capture=False: ran.append((text, check)) or ""
        drv.prepare_env(None)
        self.assertEqual(ran, [("killall cog", False), ("rm -rf /root/.cache/WebKitCache", True)])

    def driver(self, board):
        drv = self.d.WkBoardDriver.__new__(self.d.WkBoardDriver)
        drv._board, drv._here = board, Fake()
        return drv

    def test_a_board_command_is_an_effect_on_the_board_machine(self):
        board = Fake("board")
        board.answer(["sh", "-c"])
        self.driver(board)._remote("killall cog")
        self.assertEqual(board.effects, [("run", ("sh", "-c", "killall cog"))])
        with unittest.mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.driver(Fake("board"))._remote("killall cog")
        self.assertIn("killall cog", err.getvalue())

    def test_a_failed_board_command_names_its_status(self):
        board = Fake("board")
        board.answer(["sh", "-c"], rc=3, err="boom")
        with self.assertRaisesRegex(RuntimeError, r"(?s)\(3\).*boom"):
            self.driver(board)._remote("false")
        self.assertEqual(self.driver(board)._remote("false", check=False), "")

    def test_url_keeps_path_and_query_and_swaps_host(self):
        url = self.d.rewrite_url("http://127.0.0.1:41235/Speedometer/index.html?startAutomatically=true", "127.0.0.1:5000")
        self.assertEqual(url, "http://127.0.0.1:5000/Speedometer/index.html?startAutomatically=true")

    def test_the_slot_itself_passes(self):
        self.assertEqual(self.d.judge(self.expect, self.good), [])

    def test_the_images_own_webkit_is_caught(self):
        got = dict(self.good, mapped="", other_webkit="/usr/lib/libWPEWebKit-1.1.so.0.2.9 ", lib_sha256="0" * 64)
        self.assertEqual(len(self.d.judge(self.expect, got)), 3)

    def test_a_different_slots_bytes_or_no_process_is_one_problem(self):
        self.assertEqual(len(self.d.judge(self.expect, dict(self.good, lib_sha256="f" * 64))), 1)
        self.assertEqual(len(self.d.judge(self.expect, {"pids": "0"})), 1)


class TestSysimageWebkitRefusals(WkTest):
    def test_a_yocto_slot_is_the_webkit_stage(self):
        cp = run_here("sysimage", "webkit", "webkit-2.52-yocto-rpi3-32", "--commit", "a" * 40, "--slot", "base", "--dry-run", timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("webkit", cp.stdout)

    def test_a_yocto_slot_needs_both_commit_and_slot(self):
        cp = run_here("sysimage", "webkit", "webkit-2.52-yocto-rpi3-32", "--slot", "base", "--dry-run", timeout=60)
        self.assertEqual(cp.returncode, 1, cp.stdout)


class TestSysimageLs(WkTest):
    """`wk sysimage ls` is asked of the machine holding the store."""

    def test_ls_answers_cleanly(self):
        cp = run("sysimage", "ls", timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("command not found", cp.stdout)

    @requires_container_place()
    @unittest.skipUnless(sys.platform == "darwin",
                         "only a macOS workstation keeps the store off this machine")
    def test_a_store_this_machine_cannot_read_is_asked_of_the_machine_holding_it(self):
        ws = "yocto-webkit-2.52-yocto-rpi5-64"
        img = ("%s/ws/%s/build/CrossToolChains/rpi5-64bits-mesa"
               "/build/image/webkit-dev-ci-tools.wic.xz" % (container_store(), ws))
        made = container_side("mkdir -p %s && : > %s" % (os.path.dirname(img), img))
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.addCleanup(container_side, "rm -rf %s/ws/%s" % (container_store(), ws))
        cp = run("sysimage", "ls", env={"WK_STORE": "/nonexistent-store"}, timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(ws, cp.stdout)


if __name__ == "__main__":
    unittest.main()
