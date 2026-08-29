"""A second system beside a rescue on one card (`<device>@second`): the rpi3
keeps its rescue on partitions 1-2 and its bench system on 3-4 of the same SD
card. The card helper (admin/wk-card-priv) splits a whole-card image into
partitions 3 and 4, addresses only those under @second, whether the card is
in a reader or is the disk the rescue itself runs from, and arms the second
system with one os_prefix line in the rescue's config.txt; boot/pi-sd.sh is
the driver that asks for all of it.

Helper functions are lifted out of admin/wk-card-priv with sed (the idiom
tests/test_card_edits.py uses) and run against files and directories standing
in for the card: sfdisk edits a plain file's partition table, and the split
writes to `<disk>3` / `<disk>4` beside it, so the byte arithmetic runs for
real without root.

Run: python3 -m unittest tests.test_second_system -v
"""
import hashlib
import os
import re
import struct
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, stub_path

CARD_PRIV = REPO / "admin" / "wk-card-priv"
PI_SD = REPO / "boot" / "pi-sd.sh"


def _lift(path, *funcs):
    out = []
    for func in funcs:
        text = subprocess.run(
            ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
            capture_output=True, text=True,
        ).stdout
        assert text.strip(), f"could not lift {func} from {path}"
        out.append(text)
    return "\n".join(out)


_SAY = '''
say()  { printf 'wk-card-priv: %s\\n' "$*"; }
deny() { printf 'wk-card-priv: REFUSED: %s\\n' "$*" >&2; exit 3; }
fail() { printf 'wk-card-priv: %s\\n' "$*" >&2; exit 1; }
chown() { :; }
BOOTP=1; ROOTP=2; SECOND=""
'''


def _mbr(parts):
    """A 512-byte MBR with the given (type, start, size) entries, in sectors."""
    mbr = bytearray(512)
    for i, (ptype, start, size) in enumerate(parts):
        e = 446 + 16 * i
        mbr[e + 4] = ptype
        mbr[e + 8:e + 12] = struct.pack("<I", start)
        mbr[e + 12:e + 16] = struct.pack("<I", size)
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr)


def _sfdisk_json(path):
    import json
    out = subprocess.run(["sfdisk", "-J", str(path)], capture_output=True, text=True, check=True).stdout
    table = json.loads(out)["partitiontable"]
    return {int(p["node"][len(str(path)):]): p for p in table["partitions"]}


class TestGateUnderSecond(WkTest):
    """The gate's @second carve-out: partitions 3 and 4 of a disk that may be
    the one this machine runs from, or a card in a reader."""

    def setUp(self):
        super().setUp()
        loops = sorted(Path("/dev").glob("loop[0-9]*"))
        if not loops:
            self.skipTest("no block device to hand the gate (it never writes one)")
        self.dev = str(loops[0])

    def _gate(self, spec, booted, mounted_on_34=""):
        """Run the real gate with lsblk faked: every disk is a whole mmc disk,
        and partitions 3/4 report the mountpoint given."""
        lsblk = f'''
case "$*" in
  *TYPE,TRAN*) echo "disk mmc" ;;
  *MOUNTPOINT*) case "$*" in *3\\ *|*4\\ *|*3|*4) printf '%s\\n' "{mounted_on_34}" ;; *) echo "" ;; esac ;;
  *) echo "" ;;
esac
'''
        with stub_path({"lsblk": lsblk}) as binp:
            return bash(
                _SAY + _lift(CARD_PRIV, "part", "gate")
                + f'\nbooted_disks() {{ printf "%s\\n" "{booted}"; }}\n'
                + f'gate "{spec}" && printf "dev=%s bootp=%s rootp=%s second=%s\\n" "$GATED_DEV" "$BOOTP" "$ROOTP" "$SECOND"\n',
                env={"PATH": f"{binp}:{os.environ['PATH']}"},
            )

    def test_a_card_in_a_reader_takes_a_second_system(self):
        cp = self._gate(f"{self.dev}@second", booted="nvme0n1")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn(f"dev={self.dev} bootp=3 rootp=4 second=1", cp.stdout)

    def test_the_disk_this_machine_runs_from_takes_a_second_system(self):
        cp = self._gate(f"{self.dev}@second", booted=os.path.basename(self.dev))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("bootp=3 rootp=4", cp.stdout)

    def test_the_disk_this_machine_runs_from_refuses_a_whole_image(self):
        cp = self._gate(self.dev, booted=os.path.basename(self.dev))
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("@second", cp.stderr)

    def test_a_mounted_second_system_is_refused(self):
        cp = self._gate(f"{self.dev}@second", booted="", mounted_on_34="/mnt/x")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("partitions 3 and 4", cp.stderr)

    def test_only_second_is_a_system_name(self):
        cp = self._gate(f"{self.dev}@third", booted="")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)


class TestSecondWrite(WkTest):
    """The split: a whole-card image (MBR, boot, root) into partitions 3 and 4
    of a disk that already has 1 and 2, on plain files."""

    BOOT = b"B" * (4096 * 512)
    ROOT = b"R" * (8192 * 512)

    def _image(self):
        img = self.tmp / "image.img"
        b_start, r_start = 2048, 2048 + 4096
        with open(img, "wb") as fh:
            fh.write(_mbr([(0x0c, b_start, 4096), (0x83, r_start, 8192)]))
            fh.write(b"\0" * ((b_start * 512) - 512))
            fh.write(self.BOOT)
            fh.write(self.ROOT)
            fh.write(b"\0" * 4096)   # padding after the root, as genimage leaves
        return img

    def _disk(self, size_mb=64):
        disk = self.tmp / "disk"
        subprocess.run(["truncate", "-s", f"{size_mb}M", str(disk)], check=True)
        subprocess.run(["sfdisk", "-q", str(disk)], input="label: dos\nstart=2048, size=8192, type=c\nstart=10240, size=20480, type=83\n",
                       text=True, check=True, capture_output=True)
        return disk

    def _write(self, disk, img):
        return bash(_SAY + _lift(CARD_PRIV, "_second_write")
                    + f'\n_second_write "{disk}" < "{img}"\n')

    def test_the_image_is_split_into_partitions_3_and_4(self):
        disk, img = self._disk(), self._image()
        cp = self._write(disk, img)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        table = _sfdisk_json(disk)
        self.assertEqual(sorted(table), [1, 2, 3, 4], "partitions 3 and 4 were not made")
        self.assertEqual(table[3]["start"], 10240 + 20480, "partition 3 does not follow the rescue's root")
        self.assertEqual(table[3]["size"], 4096, "partition 3 is not the image's boot partition size")
        self.assertEqual(table[4]["start"] + table[4]["size"], 64 * 2048, "partition 4 does not reach the end of the disk")
        self.assertEqual(Path(str(disk) + "3").read_bytes(), self.BOOT)
        self.assertEqual(Path(str(disk) + "4").read_bytes(), self.ROOT)
        self.assertIn(f"boot_sha={hashlib.sha256(self.BOOT).hexdigest()}", cp.stdout)
        self.assertIn(f"root_sha={hashlib.sha256(self.ROOT).hexdigest()}", cp.stdout)
        self.assertIn(f"boot_bytes={len(self.BOOT)}", cp.stdout)
        self.assertIn(f"root_bytes={len(self.ROOT)}", cp.stdout)

    def test_a_second_write_reuses_the_partitions_it_made(self):
        disk, img = self._disk(), self._image()
        self.assertEqual(self._write(disk, img).returncode, 0)
        cp = self._write(disk, img)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("made beside", cp.stdout)
        self.assertEqual(sorted(_sfdisk_json(disk)), [1, 2, 3, 4])

    def test_an_image_that_does_not_fit_is_refused_before_anything_is_written(self):
        disk, img = self._disk(size_mb=20), self._image()   # 20 MB: 14 MB used by 1-2, root needs 4 MB + boot 2 MB... fits; shrink further
        disk = self._disk(size_mb=16)
        cp = self._write(disk, img)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("do not fit", cp.stderr)
        self.assertFalse(Path(str(disk) + "3").exists(), "partition 3 was written anyway")

    def test_an_image_with_one_partition_is_refused(self):
        disk = self._disk()
        img = self.tmp / "one.img"
        img.write_bytes(_mbr([(0x83, 2048, 2048)]) + b"\0" * 2048 * 512)
        cp = self._write(disk, img)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("carries 1 partition", cp.stderr)
        self.assertEqual(sorted(_sfdisk_json(disk)), [1, 2], "the disk's table was touched")

    def test_the_read_back_compares_both_partitions(self):
        disk, img = self._disk(), self._image()
        cp = self._write(disk, img)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        args = " ".join(re.search(rf"^{k}=(\S+)", cp.stdout, re.M).group(1)
                        for k in ("boot_bytes", "boot_sha", "root_bytes", "root_sha"))
        verify = (_SAY + _lift(CARD_PRIV, "part", "check_hex", "v_second_verify")
                  + f'\ngate() {{ SECOND=1; BOOTP=3; ROOTP=4; GATED_DEV="${{1%@second}}"; }}\n'
                  + f'v_second_verify "{disk}@second" {args}\n')
        cp = bash(verify)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("verified", cp.stdout)
        with open(str(disk) + "4", "r+b") as fh:
            fh.seek(100); fh.write(b"X")
        cp = bash(verify)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("partition 4 reads back", cp.stderr)


class TestGrowAddressesTheRoot(unittest.TestCase):
    def test_grow_uses_the_partition_the_gate_chose(self):
        text = _lift(CARD_PRIV, "v_grow")
        self.assertIn('-N "$ROOTP"', text, "v_grow grows partition 2 by name, not the root the gate chose")
        self.assertNotIn("part \"$dev\" 2", text)


class TestArming(WkTest):
    """second-arm / second-disarm / second-state on directories standing in
    for the rescue's boot partition and the second system's."""

    def setUp(self):
        super().setUp()
        self.boot = self.tmp / "boot"
        self.second = self.tmp / "p3"
        self.stage = self.tmp / "stage"
        for d in (self.boot, self.second, self.stage):
            d.mkdir()
        (self.boot / "config.txt").write_text("kernel=rescue.img\n")
        (self.second / "config.txt").write_text("kernel=zImage\nforce_turbo=1\n")
        (self.second / "cmdline.txt").write_text("root=PARTUUID=aa-04 rootwait\n")
        (self.second / "zImage").write_bytes(b"kernel")
        (self.second / "overlays").mkdir()
        (self.second / "overlays" / "x.dtbo").write_bytes(b"dtbo")

    def _arm(self):
        return bash(_SAY + _lift(CARD_PRIV, "_second_stage_copy", "_second_arm_install")
                    + f'\n_second_stage_copy "{self.second}" "{self.stage}" && _second_arm_install "{self.boot}" "{self.stage}"\n')

    def test_arming_selects_the_second_system_for_one_boot(self):
        cp = self._arm()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        config = (self.boot / "config.txt").read_text()
        self.assertIn("kernel=zImage", config)
        self.assertRegex(config, r"(?m)^os_prefix=second/$")
        self.assertEqual((self.boot / "config.txt.rescue").read_text(), "kernel=rescue.img\n")
        self.assertEqual((self.boot / "second" / "cmdline.txt").read_text(), "root=PARTUUID=aa-04 rootwait\n")
        self.assertEqual((self.boot / "second" / "overlays" / "x.dtbo").read_bytes(), b"dtbo")
        self.assertFalse((self.boot / "second.part").exists())

    def test_arming_twice_keeps_the_rescues_own_config(self):
        self.assertEqual(self._arm().returncode, 0)
        cp = self._arm()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "config.txt.rescue").read_text(), "kernel=rescue.img\n",
                         "the second arm overwrote the rescue's config.txt with the armed one")

    def test_a_second_system_with_no_cmdline_is_refused(self):
        (self.second / "cmdline.txt").unlink()
        cp = self._arm()
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no cmdline.txt", cp.stderr)
        self.assertEqual((self.boot / "config.txt").read_text(), "kernel=rescue.img\n", "the rescue's config.txt was touched")

    def test_disarm_puts_the_rescues_config_back_and_state_says_so(self):
        self.assertEqual(self._arm().returncode, 0)
        state = _SAY + _lift(CARD_PRIV, "_second_state_edit") + f'\n_second_state_edit "{self.boot}"\n'
        self.assertIn("armed=yes", bash(state).stdout)
        cp = bash(_SAY + _lift(CARD_PRIV, "_second_disarm_edit") + f'\n_second_disarm_edit "{self.boot}"\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "config.txt").read_text(), "kernel=rescue.img\n")
        self.assertFalse((self.boot / "config.txt.rescue").exists())
        self.assertIn("armed=no", bash(state).stdout)
        cp = bash(_SAY + _lift(CARD_PRIV, "_second_disarm_edit") + f'\n_second_disarm_edit "{self.boot}"\n')
        self.assertIn("not armed", cp.stdout)

    def test_the_verbs_take_the_boot_partition_wherever_it_is(self):
        """mounted already (the rescue running from the disk) or mounted here (a card in a reader)"""
        text = _lift(CARD_PRIV, "_second_with_boot")
        self.assertIn("findmnt", text)
        self.assertIn("with_mount", text)
        for verb in ("v_second_arm", "v_second_disarm", "v_second_state"):
            self.assertIn("_second_with_boot", _lift(CARD_PRIV, verb), f"{verb} does not go through _second_with_boot")


class TestUnitsForABusyBoxInit(WkTest):
    """`units` installs the archive's init.d scripts on an image with no
    systemd but an /etc/init.d, so a buildroot bench system gets its
    self-disarm and self-return at write time like a yocto one gets units."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.work = self.tmp / "staged"
        for d in ("systemd", "sysctl.d", "init.d"):
            (self.work / d).mkdir(parents=True)
        (self.work / "systemd" / "wk-self-return.service").write_text(
            "[Unit]\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n")
        (self.work / "sysctl.d" / "90-wk-perf.conf").write_text("kernel.kptr_restrict = 0\n")
        (self.work / "init.d" / "S11wk-self-disarm").write_text("#!/bin/sh\nexit 0\n")

    def _edit(self):
        return bash(_SAY + "SYSCTL_N=0\n" + _lift(CARD_PRIV, "_unit_target", "_units_sysctl", "_units_edit")
                    + f'\n_units_edit "{self.root}" "{self.work}"\n')

    def test_a_busybox_image_takes_the_init_scripts_and_the_sysctls(self):
        (self.root / "etc" / "init.d").mkdir(parents=True)
        cp = self._edit()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        script = self.root / "etc" / "init.d" / "S11wk-self-disarm"
        self.assertTrue(script.is_file())
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        self.assertTrue((self.root / "etc" / "sysctl.d" / "90-wk-perf.conf").is_file())
        self.assertFalse((self.root / "etc" / "systemd").exists(), "systemd units landed on a BusyBox image")
        self.assertIn("installed 2 init.d script(s) and sysctl drop-in(s)", cp.stdout)

    def test_a_systemd_image_takes_the_units_not_the_scripts(self):
        (self.root / "lib" / "systemd").mkdir(parents=True)
        (self.root / "lib" / "systemd" / "systemd").write_text("")
        (self.root / "etc" / "init.d").mkdir(parents=True)   # some systemd images carry one too
        cp = self._edit()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((self.root / "etc" / "systemd" / "system" / "wk-self-return.service").is_file())
        self.assertFalse((self.root / "etc" / "init.d" / "S11wk-self-disarm").exists())
        self.assertIn("installed 2 file(s)", cp.stdout)

    def test_an_image_with_neither_init_takes_nothing_and_says_so(self):
        cp = self._edit()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("neither systemd nor /etc/init.d", cp.stdout)
        self.assertFalse((self.root / "etc").exists())

    def test_the_archive_may_name_init_scripts_and_nothing_else_new(self):
        import io
        import tarfile
        def names(members):
            tar = self.tmp / "u.tar"
            with tarfile.open(tar, "w") as tf:
                for name in members:
                    info = tarfile.TarInfo(name); info.size = 1
                    tf.addfile(info, io.BytesIO(b"x"))
            return bash(_SAY + _lift(CARD_PRIV, "_units_names") + f"\n_units_names {tar}\n")
        self.assertEqual(names(["init.d/S11wk-self-disarm", "init.d/S99wk-self-return"]).returncode, 0)
        self.assertEqual(names(["init.d/rcS"]).returncode, 3, "a script that is not S<nn>* was accepted")
        self.assertEqual(names(["init.d/S11../x"]).returncode, 3)


class TestPiSdDriver(unittest.TestCase):
    """boot/pi-sd.sh: arming through the helper's @second verbs, on the rescue."""

    def _load(self, script):
        return bash(f'''
set -eu
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/image.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
machine_load rpi3
load_driver pi-sd
{script}
''')

    def test_arming_is_on_the_medium(self):
        cp = self._load('echo "$BOOT_ARMING"')
        self.assertEqual(cp.stdout.strip(), "medium", cp.stdout + cp.stderr)

    def test_arm_and_disarm_go_through_the_helper_on_the_rescue(self):
        cp = self._load('''
card_priv() { echo "card_priv $*" >&2; case "$1" in second-state) echo "wk-card-priv: armed=no"; echo "wk-card-priv: present=yes" ;; esac; }
b_arm
b_disarm
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("card_priv second-arm /dev/mmcblk0@second", cp.stderr)
        self.assertNotIn("second-disarm", cp.stderr, "b_disarm disarmed a board that was not armed")

    def test_arm_refuses_a_card_with_no_second_system(self):
        cp = self._load('''
card_priv() { case "$1" in second-state) echo "wk-card-priv: armed=no"; echo "wk-card-priv: present=no" ;; *) echo "card_priv $*" >&2 ;; esac; }
b_arm
''')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("@second", cp.stderr)
        self.assertNotIn("card_priv second-arm", cp.stderr)

    def test_disarm_puts_the_rescue_back_when_armed(self):
        cp = self._load('''
card_priv() { echo "card_priv $*" >&2; case "$1" in second-state) echo "wk-card-priv: armed=yes"; echo "wk-card-priv: present=yes" ;; esac; }
b_disarm
''')
        self.assertIn("card_priv second-disarm /dev/mmcblk0@second", cp.stderr)

    def test_the_bench_systems_boot_partition_is_the_third(self):
        cp = self._load("b_boot_part")
        self.assertEqual(cp.stdout.strip(), "/dev/mmcblk0p3")

    def test_the_self_disarm_is_one_systemd_safe_sh_command(self):
        cp = self._load("b_self_disarm_sh")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        body = cp.stdout
        self.assertTrue(body.strip())
        for bad in ("'", "%"):
            self.assertNotIn(bad, body, f"the self-disarm carries {bad!r}, which systemd's ExecStart would mangle")
        self.assertIn("config.txt.rescue", body)
        self.assertEqual(subprocess.run(["sh", "-n"], input=body, text=True, capture_output=True).returncode, 0)

    def test_evidence_comes_from_the_card_not_the_record(self):
        cp = self._load('''
card_priv() { echo "wk-card-priv: armed=yes"; echo "wk-card-priv: present=yes"; }
b_evidence
''')
        self.assertIn("armed=yes", cp.stdout)
        self.assertIn("bench_system_present=yes", cp.stdout)

    def test_reprovisioning_writes_both_systems_from_a_reader(self):
        cp = self._load("b_reprovision")
        self.assertIn("--disk <reader>:/dev/mmcblk0 --rescue", cp.stdout)
        self.assertIn("--disk <reader>:/dev/mmcblk0@second", cp.stdout)
        self.assertIn("wk boot rpi3", cp.stdout)


class TestDiskLayerUnderSecond(WkTest):
    """boot/disk.sh: the two steps that differ under @second."""

    def _step(self, step, dry="1"):
        return bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/lib/image.sh"
MACH_NAME=testmach
DISK_DRY={dry!r}
card_priv() {{ echo "card_priv should not have run: $*" >&2; exit 9; }}
m_ssh() {{ echo "m_ssh should not have run: $*" >&2; exit 9; }}
{step}
echo DONE
''')

    def test_the_identity_is_left_to_the_rescue_disk(self):
        cp = self._step("disk_unique_identity /dev/sdX@second", dry="")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("keeps the rescue disk's identity", cp.stdout + cp.stderr)
        self.assertNotIn("card_priv should not", cp.stdout + cp.stderr)

    def test_the_read_back_uses_the_two_partition_hashes(self):
        meta = self.tmp / "meta"
        meta.write_text("100 aaaa\n64 bb 128 cc\n")
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/lib/image.sh"
MACH_NAME=testmach
DISK_DRY=""
card_priv() {{ echo "card_priv $*" >&2; echo aaaa; }}
disk_verify_stream /dev/sdX@second "{meta}"
disk_verify_stream /dev/sdX "{meta}"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("card_priv verify /dev/sdX@second 64 bb 128 cc", cp.stderr)
        self.assertIn("card_priv verify /dev/sdX 100", cp.stderr)

    def test_a_second_write_that_reported_nothing_cannot_be_verified(self):
        meta = self.tmp / "meta"
        meta.write_text("100 aaaa\n")
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/lib/image.sh"
MACH_NAME=testmach
DISK_DRY=""
card_priv() {{ echo "card_priv $*"; }}
disk_verify_stream /dev/sdX@second "{meta}"
''')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("did not report", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
