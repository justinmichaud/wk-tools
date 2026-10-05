"""Commands, drivers and checkers exercised on a host with no workspace, no podman machine and no ssh: each runs
a real `wk` command or a library call against this tree and a scratch directory."""
import os
import platform
import re
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
    def test_wk_build_list_shows_all_configs(self):
        cp = run("build", "--list")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("jsc-release", cp.stdout)
        self.assertIn("mac-release", cp.stdout)

    def test_wk_build_list_starts_no_podman_machine(self):
        asked = self.tmp / "podman-asked"
        with stub_path({"podman": 'echo "$*" >> %s\nexit 125\n' % asked}) as binp:
            cp = run("build", "--list", env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout)
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
            bad = []
            cp = ws.run("run", "--profile", "--preset", "mac-release", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "DYLD_FRAMEWORK_PATH" not in cp.stdout:
                bad.append("no-dyld")
            if not re.search(r"/jsc.* --sample.* .?bench\.js", cp.stdout):
                bad.append("flags-after-script")

            cp = ws.run("run", "--preset", "mac-release", "--profile=native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "xctrace record" not in cp.stdout:
                bad.append("native-is-not-xctrace")
            self.assertEqual(bad, [], f"wk run --profile resolved wrongly: {bad}")

    def test_wk_profile_composes_the_right_environment(self):
        with fake_workspace() as ws:
            bad = []
            cp = ws.run("run", "--preset", "wpe-release", "--profile=native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "LD_LIBRARY_PATH" not in cp.stdout:
                bad.append("no-ld-library-path")
            if "samply record" not in cp.stdout:
                bad.append("native-is-not-samply")

            for m in ("sampling", "bytecode", "samply", "instruments", "heaptrack", "massif"):
                cp = ws.run("run", "--profile=" + m, "--dry-run", "bench.js")
                if cp.returncode != 0 and not (cp.stdout + cp.stderr).startswith("error:") \
                        and "error:" not in (cp.stdout + cp.stderr).splitlines()[0:1]:
                    if "error:" not in cp.stdout + cp.stderr:
                        bad.append(f"{m}(no-reason)")
            self.assertEqual(bad, [], f"wk run --profile resolved wrongly: {bad}")


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
            cp = subprocess.run(["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(d)],
                                capture_output=True, text=True)
            self.assertIn("kernel: " + n, cp.stderr, f"{n} resolved outside the root")

    def test_boot_files_check_catches_missing_kernel(self):
        d = self._fixture()
        (d / "current" / "vmlinuz").unlink()
        cp = subprocess.run(
            ["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(d),
             "--dtb", "bcm2711-rpi-4-b.dtb"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(cp.returncode, 0, "a tree with no kernel was reported bootable")
        self.assertIn("current/vmlinuz", cp.stdout + cp.stderr)

    def test_boot_files_accepts_autodetected_kernel(self):
        d = self._fixture()
        (d / "config.txt").write_text("[all]\ndtoverlay=vc4-kms-v3d\n")
        import shutil as _sh
        _sh.rmtree(d / "current")
        for name in ("kernel8.img", "cmdline.txt", "bcm2711-rpi-4-b.dtb"):
            (d / name).touch()
        cp = subprocess.run(
            ["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(d),
             "--dtb", "bcm2711-rpi-4-b.dtb"],
            capture_output=True, text=True,
        )
        self.assertEqual(cp.returncode, 0, f"an auto-detected kernel8.img image was refused: {cp.stdout + cp.stderr}")


def _broker_policy(body):
    script = f'''
import importlib.util, sys
root = {str(REPO)!r}
spec = importlib.util.spec_from_file_location(
    "wkbroker", root + "/container/broker/wk-broker.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
{body}
'''
    return subprocess.run(["python3", "-c", script], capture_output=True, text=True)


class TestBroker(WkTest):
    def test_broker_refuses_a_workstation_as_bench_device(self):
        cp = _broker_policy('''
bench = [n for n, v in m.fleet().items() if v["role"] == "bench-device"]
work  = [n for n, v in m.fleet().items() if v["role"] != "bench-device"]
if not bench or not work:
    print("FLEET", bench, work); raise SystemExit(0)
for n in work:
    try:
        m.want_bench_device({"machine": n})
    except m.Refused as e:
        if "bench-device" not in e.why:
            print("WRONGREASON", n, e.why); raise SystemExit(0)
    else:
        print("ACCEPTED", n); raise SystemExit(0)
print("OK")
''')
        out = cp.stdout + cp.stderr
        if cp.returncode != 0:
            self.fail(f"the policy would not load: {out}")
        if "FLEET" in out:
            self.skipTest(f"no fleet to test against: {out}")
        self.assertIn("OK", out, f"a workstation was not refused as a bench machine: {out}")

    def test_broker_plan_allowlist_is_closed(self):
        cp = _broker_policy('''
try:
    m.want_plan({"plan": "speedometer99"})
except m.Refused as e:
    if "allowlist" not in e.why:
        print("WRONGREASON", e.why); raise SystemExit(0)
else:
    print("ACCEPTED"); raise SystemExit(0)
one = sorted(m.ALLOWED_PLANS)[0]
if m.want_plan({"plan": one}) != one:
    print("REFUSEDALLOWED", one); raise SystemExit(0)
for bad in ("../../etc/passwd", "-rf", "a b"):
    try:
        m.want_plan({"plan": bad})
    except m.Refused:
        continue
    print("ACCEPTEDBAD", bad); raise SystemExit(0)
print("OK")
''')
        out = cp.stdout + cp.stderr
        self.assertIn("OK", out, f"the plan allowlist is not closed: {out}")


def _proxy_policy(body):
    script = f'''
import importlib.util, sys
root = {str(REPO)!r}
spec = importlib.util.spec_from_file_location(
    "wkproxy", root + "/container/proxy/wk-proxy.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
policy = m.Policy("/tmp")
{body}
'''
    return subprocess.run(["python3", "-c", script], capture_output=True, text=True)


class TestProxyAllowlist(WkTest):
    def test_ddebs_ubuntu_com_allowed_for_apt_only(self):
        cp = _proxy_policy('''
r = {p: policy.host_allowed("ddebs.ubuntu.com", p)[0] for p in (80, 443, 22)}
print("RESULT", r)
''')
        self.assertIn(
            "RESULT {80: True, 443: True, 22: False}", cp.stdout,
            f"stdout={cp.stdout!r} stderr={cp.stderr!r}",
        )


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
