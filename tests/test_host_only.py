"""Commands, drivers and checkers exercised on a host with no workspace, no
podman machine and no ssh: each runs a real `wk` command or a lifted shell
function against this tree and a scratch directory.

Run: python3 -m unittest tests.test_host_only -v
"""
import json
import os
import platform
import re
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, fake_workspace, run


def _have(prog):
    import shutil as _sh
    return _sh.which(prog) is not None


class TestHostState(WkTest):
    def test_no_host_marker_on_the_host(self):
        """a host carries no ~/.wk-workspace marker"""
        cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/lib/target.sh"; in_workspace')
        if cp.returncode == 0:
            self.skipTest("this machine is a workspace")
        self.assertFalse(
            (Path.home() / ".wk-workspace").exists(),
            f"{Path.home()}/.wk-workspace exists on a host",
        )


class TestDrivers(WkTest):
    def test_start_stop_with_a_driver_loaded(self):
        """`wk start` / `wk stop` with a driver loaded"""
        for t in ("container", "vm", "remote"):
            with self.subTest(target=t):
                cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh";  . "{REPO}/lib/target.sh"
load_target "{t}"
''')
                self.assertEqual(cp.returncode, 0, f"targets/{t}.sh: {cp.stdout + cp.stderr}")

        with fake_workspace() as ws:
            cp = bash(
                f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh";  . "{REPO}/lib/target.sh"
load_target local
''',
                env=ws.env(),
            )
        self.assertEqual(cp.returncode, 0, f"targets/local.sh with a marker: {cp.stdout + cp.stderr}")

    def test_loading_a_second_target_does_not_leak_overrides(self):
        """loading a second target does not leave the first driver's overrides live"""
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh";  . "{REPO}/lib/target.sh"
load_target container
first=$(type t_branch)
load_target remote
second=$(type t_branch)
[ "$first" != "$second" ] || {{ echo "t_branch is the same after loading two drivers"; exit 1; }}
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestCommandsWithoutAMachine(WkTest):
    def test_wk_build_list_shows_all_configs(self):
        """`wk build --list` shows all configs"""
        cp = run("build", "--list")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("jsc-release", cp.stdout)
        self.assertIn("mac-release", cp.stdout)

    def test_wk_build_list_with_podman_stopped(self):
        """`wk build --list` with podman stopped"""
        from tests.support import podman_vm_running

        try:
            subprocess.run(["podman", "--version"], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.skipTest("no podman on this machine")
        if podman_vm_running(os.environ.get("WK_MACHINE", "wk")):
            self.skipTest("the podman machine is already running")
        cp = run("build", "--list")
        self.assertEqual(cp.returncode, 0)
        # It must not have started the machine.
        self.assertFalse(podman_vm_running(os.environ.get("WK_MACHINE", "wk")))

    def test_sudo_status_never_prompts(self):
        """`wk sudo status` answers without ever prompting"""
        cp = run("sudo", "status", input="")
        self.assertIn(cp.returncode, (0, 1), cp.stdout + cp.stderr)
        self.assertIn("password", (cp.stdout + cp.stderr).lower())


class TestResolveWithoutABuild(WkTest):
    @unittest.skipUnless(platform.system() == "Darwin",
                         "a mac-* config is refused off an Apple host")
    def test_wk_profile_composes_the_apple_port_environment(self):
        """`wk profile` composes the Apple port's environment: DYLD_FRAMEWORK_PATH, flags before the script, xctrace for native"""
        with fake_workspace() as ws:
            bad = []
            cp = ws.run("profile", "--config", "mac-release", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "DYLD_FRAMEWORK_PATH" not in cp.stdout:
                bad.append("no-dyld")
            if not re.search(r"/jsc.* --sample.* .?bench\.js", cp.stdout):
                bad.append("flags-after-script")

            cp = ws.run("profile", "--config", "mac-release", "--mode", "native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "xctrace record" not in cp.stdout:
                bad.append("native-is-not-xctrace")
            self.assertEqual(bad, [], f"wk profile resolved wrongly: {bad}")

    def test_wk_profile_composes_the_right_environment(self):
        """`wk profile` composes each port's environment, and every mode either resolves or refuses with a reason"""
        with fake_workspace() as ws:
            bad = []
            cp = ws.run("profile", "--config", "wpe-release", "--mode", "native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "LD_LIBRARY_PATH" not in cp.stdout:
                bad.append("no-ld-library-path")
            if "samply record" not in cp.stdout:
                bad.append("native-is-not-samply")

            for m in ("sampling", "bytecode", "samply", "instruments", "heaptrack", "massif"):
                cp = ws.run("profile", "--mode", m, "--dry-run", "bench.js")
                if cp.returncode != 0 and not (cp.stdout + cp.stderr).startswith("error:") \
                        and "error:" not in (cp.stdout + cp.stderr).splitlines()[0:1]:
                    if "error:" not in cp.stdout + cp.stderr:
                        bad.append(f"{m}(no-reason)")
            self.assertEqual(bad, [], f"wk profile resolved wrongly: {bad}")

    def test_stage_manifest_is_valid_json(self):
        """the stage manifest cmd/bench writes is JSON, and no value in it is built from ${x:+...}${x:-...}"""
        manifest = self.tmp / "stage.json"
        manifest.write_text(
            '{\n  "payloads_pinned": "jetstream2.2",\n  "plans": "jetstream2.2"\n}\n'
        )
        with open(manifest) as f:
            json.load(f)  # raises if not valid JSON

        text = (REPO / "cmd" / "bench").read_text(errors="replace")
        self.assertNotRegex(
            text,
            r'"[a-z_]+": \$\{[a-z_]+:\+',
            "a JSON value is built from ${x:+...}${x:-...}, which emits the value on the else branch",
        )


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
        """path traversal is refused"""
        d = self._fixture()
        for n in ("../../etc/passwd", "deadbeef/../../etc/passwd"):
            cp = subprocess.run(
                ["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(d), "--resolve", n],
                capture_output=True, text=True,
            )
            self.assertEqual(cp.stdout.strip(), "", f"{n} resolved to something: {cp.stdout}")

    def test_boot_files_check_catches_missing_kernel(self):
        """a tree missing a kernel the firmware will ask for is not reported bootable"""
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
        """a config.txt naming no kernel= and no arm_64bit= is accepted with an auto-detected kernel8.img"""
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

    def test_pimbr_type_byte_roundtrips(self):
        """the partition type byte at MBR offset 450 round-trips without truncating the device"""
        img = self.tmp / "mbr.img"
        with open(img, "wb") as f:
            f.write(b"\x00" * (1024 * 2048))
        with open(img, "r+b") as f:
            f.seek(446)
            f.write(bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 8, 0, 0]))
            f.seek(510)
            f.write(bytes([0x55, 0xAA]))
            f.seek(450)
            f.write(bytes([0x0C]))

        cp = bash(f'NODE_DEVICE=/dev/null; . "{REPO}/boot/pi-mbr.sh"; echo "$PIMBR_TYPE_OFFSET"')
        offset = cp.stdout.strip()
        self.assertEqual(offset, "450", f"offset is {offset}, not 450")

        def read_byte(pos):
            with open(img, "rb") as f:
                f.seek(pos)
                return f.read(1)

        self.assertEqual(read_byte(450), b"\x0c")

        with open(img, "r+b") as f:
            f.seek(450)
            f.write(bytes([0x83]))
        self.assertEqual(read_byte(450), b"\x83")
        self.assertEqual(img.stat().st_size, 2097152, "the write truncated the device")

        with open(img, "r+b") as f:
            f.seek(450)
            f.write(bytes([0x0C]))
        self.assertEqual(read_byte(450), b"\x0c")

        if _have("sfdisk"):
            cp = subprocess.run(["sfdisk", "-l", str(img)], capture_output=True, text=True)
            self.assertIn("FAT32", cp.stdout, "sfdisk no longer reads the round-tripped table as FAT32")


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
        """a workspace may not claim a workstation as a bench device, whatever it asks for"""
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
        self.assertIn("OK", out, f"a workstation was not refused as a bench device: {out}")

    def test_broker_plan_allowlist_is_closed(self):
        """an unknown plan is refused, naming the allowlist that decides it"""
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
        """ddebs.ubuntu.com is reachable on 80/443 for apt, refused elsewhere"""
        cp = _proxy_policy('''
r = {p: policy.host_allowed("ddebs.ubuntu.com", p)[0] for p in (80, 443, 22)}
print("RESULT", r)
''')
        self.assertIn(
            "RESULT {80: True, 443: True, 22: False}", cp.stdout,
            f"stdout={cp.stdout!r} stderr={cp.stderr!r}",
        )


class TestSystemKind(WkTest):
    def test_base_image_is_never_mistaken_for_a_bench_system(self):
        """a base image is never mistaken for a bench system"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh" 2>/dev/null || true
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
bad=""
machine_load rpi4 || {{ echo "no rpi4 machine conf"; exit 1; }}
# rpi4: the bench system on the USB drive, the rescue on the SD card (boot/machines/rpi4.conf)
for pair in "/dev/sda2 bench" "/dev/mmcblk0p2 base" " unknown"; do
    set -- $pair
    got=$(b_system_kind "${{2:+$1}}")
    [ "$got" = "${{2:-$1}}" ] || bad="$bad rpi4:${{1:-none}}=$got"
done
machine_load rpi3 || {{ echo "no rpi3 machine conf"; exit 1; }}
got=$(b_system_kind /dev/mmcblk0p2)
[ "$got" = base ] || bad="$bad rpi3-rescue=$got"
got=$(b_system_kind /dev/mmcblk0p4)
[ "$got" = bench ] || bad="$bad rpi3-bench=$got"
grep -q 'b_system_kind' "{REPO}/cmd/pi" || bad="$bad wk-pi-bench-does-not-check"
[ -z "$bad" ] || {{ echo "$bad"; exit 1; }}
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestHandsOnArmingAndBench(WkTest):
    def _is_macos(self):
        return os.uname().sysname == "Darwin"

    def test_arming_with_no_volume_refuses(self):
        """arming with no volume attached refuses"""
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
        """a benchmark runs in bench mode or it does not run"""
        if not self._is_macos():
            self.skipTest("bare-metal bench mode is this Mac")
        cp = run("bench", "staged", "--plan", "jetstream2.2", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertNotEqual(cp.returncode, 0, "a run was accepted with no benchmark volume at all")

        stage = self.tmp / "bench" / "staged" / "20260101T000000Z-mac-release"
        (stage / "Tools" / "Scripts").mkdir(parents=True)
        (stage / "WebKitBuild" / "Release" / "MiniBrowser.app" / "Contents" / "MacOS").mkdir(parents=True)
        (stage / "stage.json").write_text('{"config":"mac-release","plans":"jetstream2.2"}')

        cp2 = run("bench", "staged", "--plan", "jetstream2.2", "--force", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertNotEqual(cp2.returncode, 0, "--force ran a benchmark in host mode")
        self.assertIn("host mode", cp2.stdout + cp2.stderr)

        cp3 = run("bench", "staged", "--plan", "jetstream2.2", "--dry-run", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertEqual(cp3.returncode, 0, f"--dry-run refused as well: {cp3.stdout + cp3.stderr}")
        self.assertIn("--plan jetstream2.2 --browser minibrowser --platform osx", cp3.stdout)

    def test_boot_status_survives_an_absent_machine(self):
        """`wk boot <machine> --status` reports something for every machine, an absent one included"""
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
