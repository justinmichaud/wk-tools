"""The pi-mbr boot driver and the buildroot fleet overlay: a board with two
media, armed by one byte of the bench medium's partition table, whose image
parks that medium and reboots unless claimed -- as systemd units on yocto and
as BusyBox init scripts on buildroot, from one string."""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def bash(script, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)


LOAD = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/boot/disk.sh"
'''


class TestDiskOfPart(unittest.TestCase):
    def test_partition_to_disk_for_every_transport(self):
        """disk_of_part inverts disk_part for sd, mmc and nvme names"""
        cp = bash(LOAD + '''
for pair in "/dev/sda2 /dev/sda" "/dev/mmcblk0p2 /dev/mmcblk0" "/dev/nvme0n1p2 /dev/nvme0n1" "/dev/sdb1 /dev/sdb"; do
    set -- $pair
    got=$(disk_of_part "$1"); [ "$got" = "$2" ] || { echo "disk_of_part $1 = $got, want $2"; exit 1; }
    back=$(disk_part "$got" "${1##*[!0-9]}"); [ "$back" = "$1" ] || { echo "disk_part $got = $back, want $1"; exit 1; }
done
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestRpi4Arrangement(unittest.TestCase):
    def test_rpi4_bench_medium_is_the_sd_and_the_rescue_is_the_stick(self):
        """rpi4.conf: MACH_DEVICE is the SD card, MACH_ROOT is on the USB stick, driver pi-mbr"""
        cp = bash(LOAD + 'machine_load rpi4; echo "$MACH_DRIVER $MACH_DEVICE $MACH_ROOT"')
        self.assertEqual(cp.stdout.strip(), "pi-mbr /dev/mmcblk0 /dev/sda2", cp.stdout + cp.stderr)

    def test_media_and_reprovision_name_the_media_from_the_conf(self):
        """b_media and b_reprovision say 'SD card' for the bench medium and 'USB stick' for the rescue"""
        cp = bash(LOAD + '''
machine_load rpi4; load_driver pi-mbr
MODE="bench x-1"; b_media; echo
MODE=""; b_reprovision
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        first, rest = cp.stdout.split("\n", 1)
        self.assertIn("booted from its SD card", first)
        self.assertIn("the USB stick is the rescue", first)
        self.assertIn("--disk <reader>:/dev/sda --rescue", rest)
        self.assertIn("--disk rpi4:/dev/mmcblk0", rest)
        self.assertNotIn("stick --", rest.split("--rescue")[0], "the rescue line names the stick, the bench line the card")


class TestSelfDisarm(unittest.TestCase):
    def _disarm(self):
        cp = bash(LOAD + 'machine_load rpi4; load_driver pi-mbr; b_self_disarm_sh')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_disarm_is_posix_sh_without_util_linux(self):
        """parses under sh -n, reads /proc and /sys only, no findmnt or lsblk, no single quote"""
        s = self._disarm()
        self.assertNotIn("'", s)
        for tool in ("findmnt", "lsblk"):
            self.assertNotIn(tool, s)
        self.assertIn("/proc/self/mountinfo", s)
        self.assertIn("/sys/dev/block/", s)
        self.assertIn("seek=450", s)
        self.assertIn("conv=notrunc", s)
        cp = subprocess.run(["sh", "-n"], input=s, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_disarm_finds_the_disk_the_root_is_on(self):
        """run against a fake /proc and /sys: the byte lands on the parent disk of the root partition"""
        if not Path("/proc/self/mountinfo").exists():
            self.skipTest("needs a Linux /proc")
        s = self._disarm()
        with tempfile.TemporaryDirectory() as d:
            img = Path(d, "disk")
            img.write_bytes(b"\0" * 512)
            # The script names /dev/<disk>; re-point /dev via PATH-free text substitution
            # of the two absolute paths it uses, which is what makes it testable at all.
            fake = s.replace("/dev/", f"{d}/dev/").replace("/proc/self/mountinfo", f"{d}/mountinfo").replace("/sys/dev/block/", f"{d}/block/")
            Path(d, "dev").mkdir()
            os.symlink(img, Path(d, "dev", "fakedisk"))
            Path(d, "block").mkdir()
            Path(d, "block", "fakedisk").mkdir()
            Path(d, "block", "fakedisk", "fakedisk2").mkdir()
            os.symlink(Path(d, "block", "fakedisk", "fakedisk2"), Path(d, "block", "8:2"))
            Path(d, "mountinfo").write_text("20 1 8:2 / / rw - ext4 /dev/root rw\n")
            cp = subprocess.run(["sh", "-c", fake], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(img.read_bytes()[450], 0x83)

    def test_systemd_unit_doubles_every_dollar(self):
        """the yocto unit's ExecStart carries $$ for each $ so systemd does not expand the script's variables"""
        cp = bash(LOAD + f'''
. "{REPO}/lib/store.sh"; . "{REPO}/lib/image.sh"
. "{REPO}/lib/target.sh" 2>/dev/null || true
wkslot() {{ :; }}
# the staging half of cmd/sysimage, without the command's dispatch
eval "$(sed -n '/^stage_unit()/,/^}}/p; /^stage_sysctl()/,/^}}/p; /^stage_units()/,/^}}/p' "{REPO}/cmd/sysimage")"
seed=$(mktemp -d); mkdir -p "$seed/systemd" "$seed/sysctl.d"; IMG_WATCHDOG=900 IMG_PROFILE=p
machine_load rpi4; load_driver pi-mbr
stage_units "$seed" "$(b_self_disarm_sh)" >/dev/null 2>&1
sed -n 's/^ExecStart=//p' "$seed/systemd/wk-self-disarm.service"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        line = cp.stdout.strip()
        self.assertTrue(line, "no ExecStart staged")
        self.assertNotRegex(line, r"(?<!\$)\$(?!\$)", f"a bare $ in {line}")
        self.assertIn("$$mp", line)


class TestFleetOverlay(unittest.TestCase):
    def _gen(self, profile):
        d = tempfile.mkdtemp()
        cp = subprocess.run([str(REPO / "image/buildroot/fleet-overlay.sh"), profile, d + "/stage"],
                            capture_output=True, text=True)
        return cp, Path(d, "stage", "etc", "init.d")

    def test_rpi4_image_gets_self_disarm_and_self_return(self):
        """wpewebkit-2.38-buildroot-rpi4-32: S00wk-self-disarm from pi-mbr, S01wk-self-return at IMG_WATCHDOG"""
        cp, initd = self._gen("wpewebkit-2.38-buildroot-rpi4-32")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        disarm = initd / "S00wk-self-disarm"
        ret = initd / "S01wk-self-return"
        for f in (disarm, ret):
            self.assertTrue(f.is_file(), f"{f} missing:\n{cp.stderr}")
            self.assertTrue(f.stat().st_mode & stat.S_IXUSR, f"{f} not executable")
            self.assertEqual(subprocess.run(["sh", "-n", str(f)]).returncode, 0)
            self.assertIn("/etc/wk/rescue", f.read_text(), "not gated on the rescue marker")
        self.assertIn("conv=notrunc", disarm.read_text())
        self.assertIn("seek=450", disarm.read_text())
        self.assertIn("sleep 900", ret.read_text())
        self.assertIn("/run/wk-keep-running", ret.read_text())

    def test_rescue_marker_makes_both_scripts_inert(self):
        """with /etc/wk/rescue present, start does nothing and touches no disk"""
        cp, initd = self._gen("wpewebkit-2.38-buildroot-rpi4-32")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        for name in ("S00wk-self-disarm", "S01wk-self-return"):
            text = (initd / name).read_text().replace("/etc/wk/rescue", str(initd / "rescue"))
            (initd / "rescue").write_text("rescue\n")
            r = subprocess.run(["sh", "-c", text, "sh", "start"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "", f"{name} did something on a rescue: {r.stdout}")

    def test_a_hands_on_board_gets_no_self_disarm(self):
        """rpi3 (pi-sd) has no b_self_disarm_sh: only the self-return is generated"""
        cp, initd = self._gen("wpewebkit-2.38-buildroot-rpi3-32")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse((initd / "S00wk-self-disarm").exists())
        self.assertTrue((initd / "S01wk-self-return").is_file())

    def test_build_script_stages_the_fleet_overlay(self):
        """image/buildroot-build.sh adds the fleet overlay to BR2_ROOTFS_OVERLAY for every image"""
        text = (REPO / "image/buildroot-build.sh").read_text()
        self.assertIn("fleet-overlay.sh", text)
        self.assertRegex(text, r'OVERLAY="\$\{OVERLAY:\+\$OVERLAY \}\$FLEET_OVERLAY"')


class TestFleetTailnetLine(unittest.TestCase):
    def test_reached_line_names_each_role_node(self):
        """wk status: a bench device is reached under its rescue and bench names, not its machine name"""
        peers = "rpi4-rescue\t100.1.1.1\tup\n"
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/reach.sh"
wk_tailscale_peers() {{ printf '%s' "$PEERS"; }}
eval "$(sed -n '/^fleet_tailnet()/,/^}}/p' "{REPO}/cmd/status")"
fleet_tailnet rpi4; echo
fleet_tailnet rpi5
''', env={"PEERS": peers, "WK_ROOT": str(REPO)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = cp.stdout.splitlines()
        self.assertEqual(lines[0], "rpi4-rescue 100.1.1.1 (up); rpi4-bench not a node")
        self.assertEqual(lines[1], "rpi5 not a node; rpi5-bench not a node")

    def test_without_tailscale_says_nothing_for_a_board_on_the_tailnet_by_role_name(self):
        """reach_without_tailnet is silent when MACH_SSH or MACH_BENCH_SSH is a node"""
        for peers in ("rpi4-rescue\t100.1.1.1\tup\n", "rpi4-bench\t100.1.1.2\tup\n"):
            cp = bash(f'''
. "{REPO}/lib/common.sh"; . "{REPO}/lib/reach.sh"
wk_tailscale_peers() {{ printf '%s' "$PEERS"; }}
reach_without_tailnet rpi4
''', env={"PEERS": peers, "WK_ROOT": str(REPO)})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(cp.stdout, "", f"said {cp.stdout!r} with peers {peers!r}")


if __name__ == "__main__":
    unittest.main()
