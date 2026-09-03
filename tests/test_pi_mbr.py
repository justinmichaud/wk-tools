"""The pi-mbr boot driver and the buildroot fleet overlay: a board with two
media, armed by one byte of the bench medium's partition table, whose image
parks that medium and reboots unless claimed -- as systemd units on yocto and
as BusyBox init scripts on buildroot, from one string."""
import os
import re
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
    def test_rpi4_bench_medium_is_the_usb_drive_and_the_rescue_is_the_sd(self):
        """rpi4.conf: MACH_DEVICE is the USB drive, MACH_ROOT is on the SD
        card. The driver is pi-tryboot (tests/test_pi_tryboot.py): the
        bootloader will not MSD-boot the drive there, so pi-mbr's arrangement
        is exercised here with the conf's media and the driver loaded directly."""
        cp = bash(LOAD + 'machine_load rpi4; echo "$MACH_DRIVER $MACH_DEVICE $MACH_ROOT"')
        self.assertEqual(cp.stdout.strip(), "pi-tryboot /dev/sda /dev/mmcblk0p2", cp.stdout + cp.stderr)

    def test_media_and_reprovision_name_the_media_from_the_conf(self):
        """b_media and b_reprovision name each medium from the conf, whichever way round it is declared"""
        for conf, bench, rescue in (
            ("MACH_DEVICE=/dev/sda MACH_ROOT=/dev/mmcblk0p2", "USB stick", "SD card"),
            ("MACH_DEVICE=/dev/mmcblk0 MACH_ROOT=/dev/sda2", "SD card", "USB stick"),
        ):
            with self.subTest(conf=conf):
                cp = bash(LOAD + f'''
machine_load rpi4; load_driver pi-mbr; {conf}
MODE="bench x-1"; b_media; echo
MODE=""; b_reprovision
''')
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                first, rest = cp.stdout.split("\n", 1)
                self.assertIn(f"booted from its {bench}", first)
                self.assertIn(f"the {rescue} is the rescue", first)
                rescue_dev = "/dev/sda" if rescue == "USB stick" else "/dev/mmcblk0"
                bench_dev = "/dev/mmcblk0" if rescue == "USB stick" else "/dev/sda"
                self.assertIn(f"--disk <reader>:{rescue_dev} --rescue", rest)
                self.assertIn(f"--disk rpi4:{bench_dev}", rest)


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
            # Longest first: "/sys/dev/block/" contains "/dev/", so replacing
            # "/dev/" ahead of it rewrites the middle of that path and the
            # later replacement then matches nothing -- the script read the
            # host's real /sys and exited without writing a byte.
            fake = (s.replace("/sys/dev/block/", f"{d}/block/")
                     .replace("/proc/self/mountinfo", f"{d}/mountinfo")
                     .replace("/dev/", f"{d}/dev/"))
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
eval "$(sed -n '/^stage_unit()/,/^}}/p; /^stage_sysctl()/,/^}}/p; /^stage_init()/,/^}}/p; /^stage_units()/,/^}}/p' "{REPO}/cmd/sysimage")"
seed=$(mktemp -d); mkdir -p "$seed/systemd" "$seed/sysctl.d" "$seed/init.d"; IMG_WATCHDOG=900 IMG_PROFILE=p
machine_load rpi4; load_driver pi-mbr
stage_units "$seed" "$(b_self_disarm_sh)" >/dev/null 2>&1
sed -n 's/^ExecStart=//p' "$seed/systemd/wk-self-disarm.service"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        line = cp.stdout.strip()
        self.assertTrue(line, "no ExecStart staged")
        self.assertNotRegex(line, r"(?<!\$)\$(?!\$)", f"a bare $ in {line}")
        self.assertIn("$$mp", line)


class TestReplacingABoardsOwnRescue(unittest.TestCase):
    """`wk sysimage write --rescue` onto a board's other medium, from the rescue
    it replaces: the name is held by the system doing the writing, which is
    the one case the collision refusal is crossed -- by --force, recorded."""

    def _preflight(self, name, role, img_machine, peers, force=False):
        lift = f"eval \"$(sed -n '/^_tailnet_name_collides()/,/^}}/p; /^_tailnet_name_preflight()/,/^}}/p' \"{REPO}/cmd/sysimage\")\""
        env = {"PEERS": peers, "WK_ROOT": str(REPO)}
        if force:
            env["WK_FORCE"] = "1"
        return bash(LOAD + f'''
{lift}
wk_tailscale_peers() {{ printf '%s' "$PEERS"; }}
machine_load rpi4; IMG_MACHINE={img_machine}
_tailnet_name_preflight {name} {role}
echo "rc=$?"
''', env=env)

    PEERS = "rpi4-rescue\t100.1.1.1\tup\n"

    def test_a_rescue_written_from_itself_is_a_barrier(self):
        """refuses without --force, and the refusal names --force and the admin-console step"""
        cp = self._preflight("rpi4-rescue", "rescue", "rpi4", self.PEERS)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("--force", cp.stderr)
        self.assertIn("admin console", cp.stderr)
        self.assertIn("before", cp.stderr)

    def test_force_crosses_it_and_is_recorded(self):
        cp = self._preflight("rpi4-rescue", "rescue", "rpi4", self.PEERS, force=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("rc=0", cp.stdout)
        self.assertIn("FORCED", cp.stderr)

    def test_a_bench_name_or_another_boards_name_is_still_refused_outright(self):
        """the exception is exactly one case: this board, its own rescue name, a rescue write"""
        for name, role, mach, peers in (
            ("rpi4-bench", "bench", "rpi4", "rpi4-bench\t100.1.1.2\tup\n"),
            ("rpi4-rescue", "bench", "rpi4", self.PEERS),
            ("rpi4-rescue", "rescue", "rpi3", self.PEERS),
        ):
            with self.subTest(name=name, role=role, machine=mach):
                cp = self._preflight(name, role, mach, peers, force=True)
                self.assertNotEqual(cp.returncode, 0, "crossed a refusal that has no --force:\n" + cp.stdout + cp.stderr)
                self.assertIn("no --force", cp.stderr)


class TestDisarmWithoutARecord(unittest.TestCase):
    def _disarm(self, arming):
        lift = f"eval \"$(sed -n '/^cmd_disarm()/,/^}}/p' \"{REPO}/cmd/boot\")\""
        return bash(f'''
. "{REPO}/lib/common.sh"
{lift}
MACHINE=rpi4 DRY="" BOOT_ARMING={arming}
read_state() {{ ARMED_IMG=""; SPENT=""; }}
b_disarm() {{ echo "b_disarm ran"; }}
b_disarm_note() {{ :; }}
record_clear() {{ echo "record cleared"; }}
cmd_disarm 2>&1
''')

    def test_a_medium_armed_machine_is_disarmed_whoever_armed_it(self):
        """wk boot <m> --disarm parks the medium even with no arming record: the byte is the arming"""
        cp = self._disarm("medium")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("b_disarm ran", cp.stdout)
        self.assertIn("record cleared", cp.stdout)

    def test_a_one_shot_machine_with_no_record_has_nothing_to_disarm(self):
        cp = self._disarm("one-shot")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no arming record", cp.stdout)
        self.assertNotIn("b_disarm ran", cp.stdout)


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


class TestBootPartFollowsTheMedium(unittest.TestCase):
    """b_boot_part reads wk-image.id from the medium the board resolves
    (pimbr_dev), not MACH_DEVICE's name: with another USB disk enumerating
    first, the stick is sdb."""

    def test_boot_part_uses_the_resolved_disk(self):
        cp = bash(f'''
set -eu
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
machine_load rpi4
load_driver pi-mbr
disk_own_or_declared() {{ printf /dev/sdb; }}
b_boot_part
''')
        self.assertEqual(cp.stdout.strip(), "/dev/sdb1", cp.stdout + cp.stderr)
