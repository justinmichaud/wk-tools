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
import shutil
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
BOOTP=1; ROOTP=2; SECOND=""; SLOT=1; PFX=second
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


def _sfdisk_id(path):
    """The MBR disk identifier, which is the first half of every PARTUUID on it."""
    import json
    out = subprocess.run(["sfdisk", "-J", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["partitiontable"]["id"]


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
                _SAY + _lift(CARD_PRIV, "part", "_slot_resolve", "gate")
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

    def test_third_needs_the_shared_layout(self):
        """@third on a medium whose partitions 3-4 are primaries (or absent)
        is refused with the remedy; the shared layout is a write's to make."""
        cp = self._gate(f"{self.dev}@third", booted="")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("shared layout", cp.stderr)

    def test_only_second_and_third_are_system_names(self):
        cp = self._gate(f"{self.dev}@fourth", booted="")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("@second", cp.stderr)


@unittest.skipUnless(shutil.which("sfdisk"),
                     "needs sfdisk (util-linux); the helper runs on a Linux card machine")
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

    def _write(self, disk, img, shape="dedicated", slot=1):
        rc = "1" if shape == "dedicated" else "0"
        return bash(_SAY + _lift(CARD_PRIV, "_second_write")
                    + f'\nSLOT={slot}\n_first_is_rescue() {{ return {rc}; }}\n'
                    + f'_second_write "{disk}" < "{img}"\n')

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

    def test_shared_layout_holds_two_systems_deterministically(self):
        """A medium whose first system is a rescue: the write lays out an
        extended partition 3 with logical pairs 5-6 and 7-8 whose geometry is
        a function of the disk size alone, so any write order converges on
        the same table and each system's bytes live in its own pair."""
        disk, img = self._disk(size_mb=2048), self._image()
        cp = self._write(disk, img, shape="shared", slot=1)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        table = _sfdisk_json(disk)
        self.assertEqual(sorted(table), [1, 2, 3, 5, 6, 7, 8])
        total, ext_start = 2048 * 2048, 30720
        half = (total - ext_start) // 2
        boot_sect = 256 * 2048
        self.assertEqual(table[3]["start"], ext_start)
        self.assertEqual(table[3]["start"] + table[3]["size"], total)
        for slot, z0, zend in ((1, ext_start, ext_start + half), (2, ext_start + half, total)):
            b, r = 3 + 2 * slot, 4 + 2 * slot
            self.assertEqual(table[b]["start"], z0 + 2048, f"slot {slot} boot start")
            self.assertEqual(table[b]["size"], boot_sect, f"slot {slot} boot size")
            self.assertEqual(table[r]["start"], z0 + 2048 + boot_sect + 2048, f"slot {slot} root start")
            self.assertEqual(table[r]["start"] + table[r]["size"], zend, f"slot {slot} root end")
        self.assertEqual(Path(str(disk) + "5").read_bytes(), self.BOOT)
        self.assertEqual(Path(str(disk) + "6").read_bytes(), self.ROOT)

        # The other slot lands in its own pair and leaves this one alone.
        cp = self._write(disk, img, shape="shared", slot=2)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("replacing the layout", cp.stdout, "a matching table was rebuilt anyway")
        self.assertEqual(Path(str(disk) + "7").read_bytes(), self.BOOT)
        self.assertEqual(Path(str(disk) + "8").read_bytes(), self.ROOT)
        self.assertEqual(Path(str(disk) + "5").read_bytes(), self.BOOT, "slot 1's boot was disturbed")
        self.assertEqual(_sfdisk_json(disk)[5]["start"], ext_start + 2048)

    def test_the_migration_keeps_the_disks_identifier(self):
        """Every PARTUUID on a card is `<disk id>-<nn>`, and the rescue on
        partitions 1-2 names its own root that way -- in a cmdline.txt and an
        fstab this write never touches and never retargets. sfdisk invents a
        fresh identifier for any script that does not name one, so a migration
        that let it would leave the rescue naming a root that no longer exists:
        the board boots to a kernel that can mount nothing, and only a card
        reader gets it back."""
        disk, img = self._disk(size_mb=2048), self._image()
        before = _sfdisk_id(disk)
        self.assertTrue(before, "the fixture disk has no identifier to keep")
        cp = self._write(disk, img, shape="shared", slot=1)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("shared layout made", cp.stdout)
        self.assertEqual(_sfdisk_id(disk), before,
                         "the migration changed the disk identifier, so every "
                         "PARTUUID on the card moved -- the rescue's included")
        # ...and the same when it is replacing an older primary 3-4 pair.
        disk2 = self._disk(size_mb=2048)
        subprocess.run(["sfdisk", "-q", "--append", "--no-reread", str(disk2)],
                       input="start=30720, size=4096, type=c\nstart=34816, size=8192, type=83\n",
                       text=True, check=True, capture_output=True)
        before2 = _sfdisk_id(disk2)
        cp = self._write(disk2, img, shape="shared", slot=1)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("replacing the layout", cp.stdout)
        self.assertEqual(_sfdisk_id(disk2), before2)

    def test_shared_layout_replaces_the_one_system_primaries(self):
        """the migration: a card with the old primary 3-4 pair beside its
        rescue is relaid; the write says what it destroyed."""
        disk, img = self._disk(size_mb=2048), self._image()
        subprocess.run(["sfdisk", "-q", "--append", "--no-reread", str(disk)],
                       input="start=30720, size=4096, type=c\nstart=34816, size=8192, type=83\n",
                       text=True, check=True, capture_output=True)
        cp = self._write(disk, img, shape="shared", slot=1)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("replacing the layout", cp.stdout)
        self.assertEqual(sorted(_sfdisk_json(disk)), [1, 2, 3, 5, 6, 7, 8])

    def test_slot_resolve_reads_the_extended_layout_off_the_table(self):
        """the real sfdisk output (' 5', indented) against a real table:
        @second resolves to 5-6 and @third to 7-8 once partition 3 is
        extended, and to 3-4 / a refusal before."""
        disk, img = self._disk(size_mb=2048), self._image()
        script_pre = _SAY + _lift(CARD_PRIV, "_slot_resolve")
        cp = bash(script_pre + f'\nSLOT=1; _slot_resolve "{disk}"; echo "$BOOTP $ROOTP"\n')
        self.assertEqual(cp.stdout.strip().splitlines()[-1], "3 4", cp.stdout + cp.stderr)
        self.assertEqual(self._write(disk, img, shape="shared", slot=1).returncode, 0)
        for slot, want in ((1, "5 6"), (2, "7 8")):
            cp = bash(script_pre + f'\nSLOT={slot}; _slot_resolve "{disk}"; echo "$BOOTP $ROOTP"\n')
            self.assertEqual(cp.stdout.strip().splitlines()[-1], want,
                             f"slot {slot}: " + cp.stdout + cp.stderr)

    def test_a_dedicated_medium_refuses_a_third(self):
        disk, img = self._disk(), self._image()
        cp = self._write(disk, img, shape="dedicated", slot=2)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no @third here", cp.stderr)

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

    def _arm(self, pfx="second"):
        return bash(_SAY + _lift(CARD_PRIV, "_second_stage_copy", "_second_arm_install")
                    + f'\n_second_stage_copy "{self.second}" "{self.stage}" && _second_arm_install "{self.boot}" "{self.stage}" "{pfx}"\n')

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

    def test_reading_the_state_mounts_read_only(self):
        """`second-state` answers a question and writes nothing. Mounting a FAT
        read-write and unmounting it rewrites the dirty flag and the FSInfo
        sector, which is a write to a card somebody only asked about; arming
        and disarming do edit it, so they mount read-write."""
        script = _SAY + _lift(CARD_PRIV, "_second_with_boot") + """
part() { printf '%s%s' "$1" "$2"; }
findmnt() { return 1; }
with_mount() { printf 'with_mount %s\\n' "$*"; }
_second_state_edit() { :; }
_second_arm_install() { :; }
_second_disarm_edit() { :; }
_second_with_boot -r /dev/sdX _second_state_edit
_second_with_boot /dev/sdX _second_disarm_edit
"""
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = cp.stdout.strip().splitlines()
        self.assertEqual(lines[0], "with_mount -r /dev/sdX1 _second_state_edit", cp.stdout)
        self.assertEqual(lines[1], "with_mount /dev/sdX1 _second_disarm_edit", cp.stdout)

    def test_the_state_verb_asks_for_read_only(self):
        """the dispatch half of the rule above: v_second_state is the only one
        of the three that passes -r."""
        text = CARD_PRIV.read_text()
        self.assertIn('_second_with_boot -r "$dev" _second_state_edit', text)
        self.assertNotIn('_second_with_boot -r "$dev" _second_arm_install', text)

    def test_the_prefix_leads_the_armed_config(self):
        """os_prefix comes before the armed system's own lines, and outside any
        conditional section it carries: the firmware resolves each filename as
        it reads the directive asking for it, and a filter can drop a prefix
        that lands inside a section. Its own os_prefix does not survive."""
        (self.second / "config.txt").write_text(
            "os_prefix=stale/\ndtoverlay=vc4-fkms-v3d\n[pi3]\ndtparam=audio=on\n")
        cp = self._arm()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = [l for l in (self.boot / "config.txt").read_text().splitlines() if l.strip()]
        self.assertEqual(lines[1], "os_prefix=second/", lines)
        self.assertLess(lines.index("os_prefix=second/"), lines.index("dtoverlay=vc4-fkms-v3d"))
        self.assertLess(lines.index("os_prefix=second/"),
                        next(i for i, l in enumerate(lines) if l.startswith("[")))
        self.assertNotIn("os_prefix=stale/", lines)

    def test_arming_the_third_system_uses_its_own_prefix(self):
        """third/ lands, os_prefix says third/, and a stale second/ from an
        earlier arming is gone -- a card reads as what it is."""
        self.assertEqual(self._arm("second").returncode, 0)
        (self.boot / "config.txt.rescue").rename(self.boot / "config.txt")  # disarm by hand
        cp = self._arm("third")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        config = (self.boot / "config.txt").read_text()
        self.assertRegex(config, r"(?m)^os_prefix=third/$")
        self.assertTrue((self.boot / "third" / "cmdline.txt").exists())
        self.assertFalse((self.boot / "second").exists(), "a stale second/ was left beside the armed third/")
        state = _SAY + _lift(CARD_PRIV, "_second_state_edit") + f'\n_second_state_edit "{self.boot}"\n'
        out = bash(state).stdout
        self.assertIn("armed=yes", out)
        self.assertIn("armed_prefix=third", out)

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
        for verb in ("v_second_arm", "v_second_disarm", "v_second_state"):
            self.assertIn("_second_with_boot", _lift(CARD_PRIV, verb), f"{verb} does not go through _second_with_boot")
        # Under the helper's own `set -euo pipefail`, with findmnt saying "not
        # mounted" (exit 1) the card in a reader is mounted here; with a
        # mountpoint it is used as is.
        script = ('set -euo pipefail\n' + _SAY + _lift(CARD_PRIV, "part", "_second_with_boot")
                  + '\nwith_mount() { echo "with_mount $1 -> $2"; }\nshow() { echo "boot=$1"; }\n'
                  + '_second_with_boot /dev/sdX show\n')
        with stub_path({"findmnt": "exit 1"}) as binp:
            cp = bash(script, env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("with_mount /dev/sdX1 -> show", cp.stdout)
        with stub_path({"findmnt": "echo /run/media/boot"}) as binp:
            cp = bash(script, env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("boot=/run/media/boot", cp.stdout)


class TestTailnetIdentityAcrossARewrite(WkTest):
    """A second system's tailscaled state is kept aside before the split and
    put back after it, so a rewritten bench system is the node it was."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "p4"
        (self.root / "var" / "lib" / "tailscale").mkdir(parents=True)
        self.stash = self.tmp / "stash"

    def _run(self, call):
        return bash(_SAY + "TAILNET_STATE=var/lib/tailscale/tailscaled.state\n"
                    + _lift(CARD_PRIV, "_tailnet_save_edit", "_tailnet_restore_edit") + "\n" + call + "\n")

    def test_the_state_is_kept_and_put_back_root_only(self):
        (self.root / "var" / "lib" / "tailscale" / "tailscaled.state").write_text("node-key\n")
        cp = self._run(f'_tailnet_save_edit "{self.root}" "{self.stash}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("kept=yes", cp.stdout)
        self.assertEqual(self.stash.read_text(), "node-key\n")
        self.assertEqual(self.stash.stat().st_mode & 0o777, 0o600)
        fresh = self.tmp / "p4-new"; fresh.mkdir()
        cp = self._run(f'_tailnet_restore_edit "{fresh}" "{self.stash}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        state = fresh / "var" / "lib" / "tailscale" / "tailscaled.state"
        self.assertEqual(state.read_text(), "node-key\n")
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("restored", cp.stdout)

    def test_a_system_with_no_state_keeps_nothing(self):
        """the per-candidate edit is silent and writes nothing when a root
        carries no identity; whether the *medium* holds one is the verb's
        answer, over the candidates below."""
        cp = self._run(f'_tailnet_save_edit "{self.root}" "{self.stash}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("kept=yes", cp.stdout)
        self.assertFalse(self.stash.exists())

    def test_the_addressed_root_is_tried_before_the_others(self):
        """A board's bench role is one tailnet node and its systems take
        turns being it, so a system written beside one that already holds the
        identity adopts it rather than joining under a name that is taken.
        The addressed pair still wins when it has its own."""
        def roots(rootp):
            cp = bash(_SAY + f"ROOTP={rootp}\n"
                      + _lift(CARD_PRIV, "_tailnet_roots") + '\n_tailnet_roots /dev/x\n')
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            return cp.stdout.split()
        self.assertEqual(roots(8), ["8", "4", "6"])
        self.assertEqual(roots(6), ["6", "4", "8"])
        self.assertEqual(roots(4), ["4", "6", "8"])

    def test_the_stash_is_per_medium_not_per_pair(self):
        """two stash names would be two identities, and the second system to
        join would collide with the first on one name."""
        cp = bash(_SAY + _lift(CARD_PRIV, "_tailnet_stash")
                  + '\n_tailnet_stash /dev/mmcblk0; echo; _tailnet_stash /dev/mmcblk0\n')
        a, b = cp.stdout.split()
        self.assertEqual(a, b)
        body = _lift(CARD_PRIV, "v_tailnet_save") + _lift(CARD_PRIV, "v_tailnet_restore")
        self.assertNotIn('_tailnet_stash "$dev$PFX"', body,
                         "the stash is keyed per pair again; the two bench systems would want two nodes")

    def test_the_save_verb_reports_an_adoption(self):
        body = _lift(CARD_PRIV, "v_tailnet_save")
        self.assertIn('say "adopted=$p"', body)
        self.assertIn('[ "$p" = "$ROOTP" ]', body,
                      "an adoption is only reported when the identity came from another pair")

    def test_the_board_remembers_its_bench_node_off_the_bench_medium(self):
        """A fresh bench card must rejoin as the node it was, or writing one
        needs something with the power to retire the leftover. So the identity
        is kept on the rescue's own root -- the filesystem a bench rewrite
        never touches -- not in /run for the length of one command."""
        stash = _lift(CARD_PRIV, "_tailnet_stash")
        self.assertNotIn("/run/", stash, "the identity is forgotten at the next reboot")
        self.assertIn("TAILNET_KEEP_DIR", stash)
        save = _lift(CARD_PRIV, "v_tailnet_save")
        self.assertIn("adopted=remembered", save,
                      "a medium with no live identity does not fall back on what the board remembers")
        restore = _lift(CARD_PRIV, "v_tailnet_restore")
        self.assertNotIn('rm -f "$stash"', restore,
                         "the remembered identity is spent by one restore; the next fresh card collides")

    def test_a_live_identity_refreshes_what_the_board_remembers(self):
        """the remembered copy must never become the older of two identities."""
        save = _lift(CARD_PRIV, "v_tailnet_save")
        self.assertIn('mv -f "$stash.new" "$stash"', save)

    def test_the_verbs_are_gated_and_dispatched(self):
        """Gated like every other verb -- the gate is what refuses a disk this
        machine runs from -- but not @second-only: a dedicated bench medium is
        rewritten whole, and the node it holds is the board's bench node
        exactly as a second system's is."""
        text = CARD_PRIV.read_text()
        for verb, fn in (("tailnet-save", "v_tailnet_save"), ("tailnet-restore", "v_tailnet_restore")):
            body = _lift(CARD_PRIV, fn)
            self.assertIn('gate "${1:-}"', body)
            self.assertRegex(text, rf"(?m)^\s*{verb}\)\s+{fn}")
            self.assertIn(verb, re.search(r'usage: wk-card-priv [^"]*', text).group(0))

    def test_a_whole_bench_medium_keeps_its_node_too(self):
        """cmd/sysimage runs the save for any bench write onto a board's own
        bench medium, not only a second system -- and never for a rescue,
        whose identity is on the medium being replaced."""
        body = re.search(r"(?ms)^cmd_write_from\(\).*?^}", (REPO / "cmd" / "sysimage").read_text()).group(0)
        save = re.search(r'kept=\$\(disk_tailnet_save.*', body).group(0)
        self.assertIn("disk_tailnet_save", save)
        guard = body[body.index("local kept=no"):body.index("kept=$(disk_tailnet_save")]
        self.assertIn('"$role" != rescue', guard)
        self.assertIn('MACH_DEVICE', guard,
                      "a whole-disk bench write onto the board's own medium must keep its node")

    def test_the_write_keeps_the_identity_across_the_split(self):
        """cmd/sysimage: saved before anything is erased, the name preflight
        stood down when it was, put back once the new partitions are there."""
        body = re.search(r"(?ms)^cmd_write_from\(\).*?^}", (REPO / "cmd" / "sysimage").read_text()).group(0)
        save = body.index("disk_tailnet_save")
        self.assertLess(save, body.index("disk_unmount"), "the identity is saved after the card is touched")
        self.assertLess(save, body.index("_tailnet_name_preflight"))
        self.assertIn('if [ "$kept" = yes ]; then', body)
        restore = body.index("disk_tailnet_restore")
        self.assertGreater(restore, body.index("disk_parts_present"))
        self.assertLess(restore, body.index("disk_seed_tailnet"))


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
ARM_SYS_PART=/dev/mmcblk0p3 b_arm
b_disarm
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("card_priv second-arm /dev/mmcblk0@second", cp.stderr)
        self.assertNotIn("second-disarm", cp.stderr, "b_disarm disarmed a board that was not armed")

    def test_arm_selects_the_named_system(self):
        """the shared layout's pairs map to the helper's addresses: 5-6 is
        @second, 7-8 is @third; an arm with no selection is refused."""
        cp = self._load('''
card_priv() { echo "card_priv $*" >&2; case "$1" in second-state) echo "wk-card-priv: armed=no"; echo "wk-card-priv: present=yes" ;; esac; }
ARM_SYS_PART=/dev/mmcblk0p7 b_arm
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("card_priv second-arm /dev/mmcblk0@third", cp.stderr)
        cp = self._load("b_arm")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("machine_select_system", cp.stderr)

    def test_arm_skips_only_when_armed_for_the_same_system(self):
        """armed for the other system is not armed for this one: the arm
        re-stages rather than trusting a yes."""
        cp = self._load('''
card_priv() { echo "card_priv $*" >&2; case "$1" in second-state) echo "wk-card-priv: armed=yes"; echo "wk-card-priv: armed_prefix=third"; echo "wk-card-priv: present=yes" ;; esac; }
ARM_SYS_PART=/dev/mmcblk0p5 b_arm
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("card_priv second-arm /dev/mmcblk0@second", cp.stderr)
        cp = self._load('''
card_priv() { echo "card_priv $*" >&2; case "$1" in second-state) echo "wk-card-priv: armed=yes"; echo "wk-card-priv: armed_prefix=second"; echo "wk-card-priv: present=yes" ;; esac; }
ARM_SYS_PART=/dev/mmcblk0p5 b_arm
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("card_priv second-arm", cp.stderr, "armed for this system already; the arm should be a no-op")

    def test_arm_refuses_a_card_with_no_second_system(self):
        cp = self._load('''
card_priv() { case "$1" in second-state) echo "wk-card-priv: armed=no"; echo "wk-card-priv: present=no" ;; *) echo "card_priv $*" >&2 ;; esac; }
ARM_SYS_PART=/dev/mmcblk0p3 b_arm
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
b_systems() { printf "%s\\n" "/dev/mmcblk0p5 img-a"; }
b_evidence
''')
        self.assertIn("armed=yes", cp.stdout)
        self.assertIn("system=img-a (on /dev/mmcblk0p5)", cp.stdout)

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

    def test_the_kept_identity_is_reported_and_put_back_only_when_kept(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/lib/image.sh"
MACH_NAME=testmach
DISK_DRY=""
CAP="tailnet-keep=yes"
card_priv() {{ echo "card_priv $*" >&2; case "$1" in status) echo "wk-card-priv: ok"; echo "wk-card-priv: $CAP" ;; tailnet-save) echo "wk-card-priv: kept=$KEPT" ;; esac; }}
KEPT=yes; k=$(disk_tailnet_save /dev/sdX@second); echo "kept=$k"
KEPT=no;  k=$(disk_tailnet_save /dev/sdX@second); echo "kept=$k"
disk_tailnet_restore /dev/sdX@second
CAP="second=yes"; k=$(disk_tailnet_save /dev/sdX@second 2>old.err); echo "old=$k"; grep -c "cannot keep" old.err
CAP="tailnet-keep=yes"; KEPT=maybe; disk_tailnet_save /dev/sdX@second && echo "guessed"
''', cwd=str(self.tmp))
        self.assertNotEqual(cp.returncode, 0, "an answer that is neither yes nor no was accepted")
        self.assertIn("kept=yes\nkept=no\n", cp.stdout)
        self.assertIn("old=no\n1\n", cp.stdout, "an old helper is not a loud 'no'")
        self.assertNotIn("guessed", cp.stdout)
        self.assertIn("card_priv tailnet-restore /dev/sdX@second", cp.stderr)

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
