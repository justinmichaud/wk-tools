"""Commands, drivers and checkers exercised on a host with no workspace, no podman machine and no ssh: each runs
a real `wk` command or a library call against this tree and a scratch directory."""
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, fake_workspace, run, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, places  # noqa: E402
from wk.boot.driver import Driver  # noqa: E402


class TestHostState(WkTest):
    def test_no_host_marker_on_the_host(self):
        if places.Registry(REPO).in_workspace():
            self.skipTest("this machine is a workspace")
        self.assertFalse(
            (Path.home() / ".wk-workspace").exists(),
            f"{Path.home()}/.wk-workspace exists on a host",
        )


class TestCommandsWithoutAMachine(WkTest):
    def test_wk_build_list_shows_all_configs_and_starts_no_podman_machine(self):
        asked = self.tmp / "podman-asked"
        with stub_path({"podman": 'echo "$*" >> %s\nexit 125\n' % asked}) as binp:
            cp = run("build", "--list", env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("jsc-release", cp.stdout)
        self.assertIn("mac-release", cp.stdout)
        self.assertNotIn("start", asked.read_text() if asked.exists() else "")

    def test_sudo_status_never_prompts(self):
        cp = run("key", "sudo", "status", input="")
        self.assertIn(cp.returncode, (0, 1), cp.stdout + cp.stderr)
        self.assertIn("password", (cp.stdout + cp.stderr).lower())


class TestResolveWithoutABuild(WkTest):
    @unittest.skipUnless(platform.system() == "Darwin",
                         "a mac-* preset is refused off an Apple host")
    def test_wk_profile_composes_the_apple_port_environment(self):
        with fake_workspace() as ws:
            cp = ws.run("run", "--profile", "--preset", "mac-release", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("DYLD_FRAMEWORK_PATH", cp.stdout)
            self.assertRegex(cp.stdout, r"/jsc.* --sample.* .?bench\.js", "the flags go before the script")
            cp = ws.run("run", "--preset", "mac-release", "--profile=native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("xctrace record", cp.stdout)

    def test_wk_profile_composes_the_right_environment(self):
        with fake_workspace() as ws:
            cp = ws.run("run", "--preset", "wpe-release", "--profile=native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("LD_LIBRARY_PATH", cp.stdout)
            self.assertIn("samply record", cp.stdout)
            for m in ("sampling", "bytecode", "samply", "instruments", "heaptrack", "massif"):
                with self.subTest(profile=m):
                    cp = ws.run("run", "--profile=" + m, "--dry-run", "bench.js")
                    self.assertTrue(cp.returncode == 0 or "error:" in cp.stdout + cp.stderr, "refused with no reason")


def check_boot_files(root, *args):
    return subprocess.run(["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(root), *args],
                          capture_output=True, text=True)


class TestBootFiles(WkTest):
    def _fixture(self):
        d = self.tmp / f"bootfiles-{os.getpid()}"
        (d / "current" / "overlays").mkdir(parents=True)
        (d / "README").write_text("root\n")
        (d / "current" / "overlays" / "README").write_text("nested\n")
        for name in ("start4.elf", "fixup4.dat", "current/vmlinuz",
                     "current/initrd.img", "current/cmdline.txt",
                     "current/bcm2711-rpi-4-b.dtb"):
            (d / name).touch()
        (d / "config.txt").write_text(
            "[all]\nos_prefix=current/\n[tryboot]\nos_prefix=new/\n"
            "[all]\narm_64bit=1\nkernel=vmlinuz\ncmdline=cmdline.txt\n"
            "initramfs initrd.img followkernel\n"
        )
        return d

    def test_path_traversal_is_refused(self):
        d = self._fixture()
        for n in ("../../../../../../etc/passwd", "deadbeef/../../../../../../etc/passwd"):
            (d / "config.txt").write_text("[all]\nkernel=%s\n" % n)
            cp = check_boot_files(d)
            self.assertIn("kernel: " + n, cp.stderr, f"{n} resolved outside the root")

    def test_boot_files_check_catches_missing_kernel(self):
        d = self._fixture()
        (d / "current" / "vmlinuz").unlink()
        cp = check_boot_files(d, "--dtb", "bcm2711-rpi-4-b.dtb")
        self.assertNotEqual(cp.returncode, 0, "a tree with no kernel was reported bootable")
        self.assertIn("current/vmlinuz", cp.stdout + cp.stderr)

    def test_boot_files_accepts_autodetected_kernel(self):
        d = self._fixture()
        (d / "config.txt").write_text("[all]\ndtoverlay=vc4-kms-v3d\n")
        shutil.rmtree(d / "current")
        for name in ("kernel8.img", "cmdline.txt", "bcm2711-rpi-4-b.dtb"):
            (d / name).touch()
        cp = check_boot_files(d, "--dtb", "bcm2711-rpi-4-b.dtb")
        self.assertEqual(cp.returncode, 0, f"an auto-detected kernel8.img image was refused: {cp.stdout + cp.stderr}")


class TestBroker(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("wkbroker", REPO / "container" / "broker" / "wk-broker.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_broker_refuses_a_workstation_as_bench_device(self):
        fleet = self.m.fleet()
        work = [n for n, v in fleet.items() if v["role"] != "bench-device"]
        if not work or len(work) == len(fleet):
            self.skipTest("no fleet of both roles to test against")
        for n in work:
            with self.subTest(machine=n), self.assertRaisesRegex(self.m.Refused, "bench-device"):
                self.m.want_bench_device({"machine": n})

    def test_broker_plan_allowlist_is_closed(self):
        for bad in ("speedometer99", "../../etc/passwd", "-rf", "a b"):
            with self.subTest(plan=bad), self.assertRaises(self.m.Refused):
                self.m.want_plan({"plan": bad})
        with self.assertRaisesRegex(self.m.Refused, "allowlist"):
            self.m.want_plan({"plan": "speedometer99"})
        one = sorted(self.m.ALLOWED_PLANS)[0]
        self.assertEqual(one, self.m.want_plan({"plan": one}))


class TestSystemKind(unittest.TestCase):
    def test_base_image_is_never_mistaken_for_a_bench_system(self):
        def kind(machine, rootdev):
            conf = fleet.Fleet(REPO, {"HOME": "/nonexistent"}).load(machine)
            return Driver(REPO, conf, None).system_kind(rootdev)
        self.assertEqual(kind("rpi4", "/dev/sda2"), "bench")
        self.assertEqual(kind("rpi4", "/dev/mmcblk0p2"), "base")
        self.assertEqual(kind("rpi4", ""), "unknown")
        self.assertEqual(kind("rpi3", "/dev/mmcblk0p2"), "base")
        self.assertEqual(kind("rpi3", "/dev/mmcblk0p4"), "bench")


class TestHandsOnArmingAndBench(WkTest):
    def _is_macos(self):
        return os.uname().sysname == "Darwin"

    def test_arming_with_no_volume_refuses(self):
        if not self._is_macos():
            self.skipTest("the hands-on machine is this Mac")
        cp = run("boot", "mbp", "--status", env={"WK_BENCH_VOLUME": "wk-selftest-no-such-volume"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("not attached", cp.stdout + cp.stderr)

        cp2 = run("boot", "mbp", env={
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_BENCH_VOLUME": "wk-selftest-no-such-volume",
        })
        self.assertNotEqual(cp2.returncode, 0, "arming succeeded with no volume attached")
        self.assertFalse((self.tmp / "state" / "wk" / "boot-armed").exists(), "a refused arming still wrote a record")

    def test_bench_role_required_or_it_does_not_run(self):
        if not self._is_macos():
            self.skipTest("bare-metal bench mode is this Mac")
        cp = run("bench", "staged", "--plan", "jetstream2.2", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertNotEqual(cp.returncode, 0, "a run was accepted with no benchmark volume at all")

        stage = self.tmp / "bench" / "staged" / "20260101T000000Z-mac-release"
        (stage / "Tools" / "Scripts").mkdir(parents=True)
        (stage / "WebKitBuild" / "Release" / "MiniBrowser.app" / "Contents" / "MacOS").mkdir(parents=True)
        (stage / "stage.json").write_text('{"preset":"mac-release","plans":"jetstream2.2"}')

        cp2 = run("bench", "staged", "--plan", "jetstream2.2", "--force", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertNotEqual(cp2.returncode, 0, "--force ran a benchmark in host mode")
        self.assertIn("host mode", cp2.stdout + cp2.stderr)

        cp3 = run("bench", "staged", "--plan", "jetstream2.2", "--dry-run", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertEqual(cp3.returncode, 1, "a dry run in host mode is a leg that would be refused")
        self.assertIn("--browser minibrowser --platform osx --plan jetstream2.2", cp3.stdout + cp3.stderr)

    def test_boot_status_survives_an_absent_machine(self):
        cp = run("boot", "--list")
        machines = [line.split()[0] for line in cp.stdout.splitlines() if line.split()]
        bad = []
        for m in machines:
            cp2 = run("boot", m, "--status")
            out = cp2.stdout + cp2.stderr
            if cp2.returncode not in (0, 2, 3) and "driven from a" not in out:
                bad.append(f"'wk boot {m} --status' exited {cp2.returncode}: {out}")
            elif not out.strip():
                bad.append(f"'wk boot {m} --status' printed nothing (exit {cp2.returncode})")
        self.assertEqual(bad, [], "; ".join(bad))


if __name__ == "__main__":
    unittest.main()
