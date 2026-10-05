"""A second system beside a rescue on one card (`<device>@second`): admin/wk-card-priv's split, gate and arming,
lifted with sed and run on plain files standing in for the card, and lib/wk/boot/pi.py's PiSd driver."""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

from tests.fake_boot import FakeBoard
from tests.support import REPO, WkTest, bash, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import act  # noqa: E402
from wk.boot.pi import PiSd  # noqa: E402

CARD_PRIV = REPO / "admin" / "wk-card-priv"


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


def _sfdisk_table(path):
    out = subprocess.run(["sfdisk", "-J", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["partitiontable"]


def _sfdisk_json(path):
    return {int(p["node"][len(str(path)):]): p for p in _sfdisk_table(path)["partitions"]}


class TestGateUnderSecond(WkTest):
    def setUp(self):
        super().setUp()
        loops = sorted(Path("/dev").glob("loop[0-9]*"))
        if not loops:
            self.skipTest("no block device to hand the gate (it never writes one)")
        self.dev = str(loops[0])

    def _gate(self, spec, booted, mounted_on_34=""):
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

    def test_what_the_gate_takes_and_refuses(self):
        me = os.path.basename(self.dev)
        for why, spec, booted, mounted, rc, want in (
                ("a card in a reader", "@second", "nvme0n1", "", 0, f"dev={self.dev} bootp=3 rootp=4 second=1"),
                ("the disk it runs from, @second", "@second", me, "", 0, "bootp=3 rootp=4"),
                ("the disk it runs from, whole", "", me, "", 3, "@second"),
                ("a mounted second system", "@second", "", "/mnt/x", 3, "partitions 3 and 4"),
                ("@third without the shared layout", "@third", "", "", 3, "shared layout"),
                ("no such system name", "@fourth", "", "", 3, "@second")):
            with self.subTest(why):
                cp = self._gate(self.dev + spec, booted=booted, mounted_on_34=mounted)
                self.assertEqual(cp.returncode, rc, cp.stdout + cp.stderr)
                self.assertIn(want, cp.stdout if rc == 0 else cp.stderr)


@unittest.skipUnless(shutil.which("sfdisk"),
                     "needs sfdisk (util-linux); the helper runs on a Linux card machine")
class TestSecondWrite(WkTest):
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
            fh.write(b"\0" * 4096)
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
        disk, img = self._disk(size_mb=16), self._image()
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
        """Extended 3 with logical pairs 5-6 and 7-8, sized from the disk alone so any write order converges."""
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

        cp = self._write(disk, img, shape="shared", slot=2)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("replacing the layout", cp.stdout, "a matching table was rebuilt anyway")
        self.assertEqual(Path(str(disk) + "7").read_bytes(), self.BOOT)
        self.assertEqual(Path(str(disk) + "8").read_bytes(), self.ROOT)
        self.assertEqual(Path(str(disk) + "5").read_bytes(), self.BOOT, "slot 1's boot was disturbed")
        self.assertEqual(_sfdisk_json(disk)[5]["start"], ext_start + 2048)

    def test_the_migration_keeps_the_disks_identifier(self):
        """The rescue names its root by `<disk id>-<nn>`; a new identifier would leave it naming nothing."""
        for old_pair in (False, True):
            with self.subTest(old_pair=old_pair):
                disk, img = self._disk(size_mb=2048), self._image()
                if old_pair:
                    subprocess.run(["sfdisk", "-q", "--append", "--no-reread", str(disk)],
                                   input="start=30720, size=4096, type=c\nstart=34816, size=8192, type=83\n",
                                   text=True, check=True, capture_output=True)
                before = _sfdisk_table(disk)["id"]
                cp = self._write(disk, img, shape="shared", slot=1)
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(old_pair, "replacing the layout" in cp.stdout, cp.stdout)
                self.assertEqual(sorted(_sfdisk_json(disk)), [1, 2, 3, 5, 6, 7, 8])
                self.assertEqual(_sfdisk_table(disk)["id"], before)

    def test_slot_resolve_reads_the_extended_layout_off_the_table(self):
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


class TestArming(WkTest):
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
        """Even an unmount of a read-write FAT rewrites its dirty flag."""
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

    def test_the_prefix_leads_the_armed_config(self):
        """The firmware resolves each filename as it reads it, and a section filter can drop a prefix inside one."""
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
        self.assertEqual(self._arm("second").returncode, 0)
        (self.boot / "config.txt.rescue").rename(self.boot / "config.txt")
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

    def test_a_boot_partition_mounted_already_is_used_where_it_is(self):
        script = ('set -euo pipefail\n' + _SAY + _lift(CARD_PRIV, "part", "_second_with_boot")
                  + '\nwith_mount() { echo "with_mount $1 -> $2"; }\nshow() { echo "boot=$1"; }\n'
                  + '_second_with_boot /dev/sdX show\n')
        with stub_path({"findmnt": "echo /run/media/boot"}) as binp:
            cp = bash(script, env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("boot=/run/media/boot", cp.stdout)


class TestTailnetIdentityAcrossARewrite(WkTest):
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
        cp = self._run(f'_tailnet_save_edit "{self.root}" "{self.stash}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("kept=yes", cp.stdout)
        self.assertFalse(self.stash.exists())

    def test_the_addressed_root_is_tried_before_the_others(self):
        """A board's systems share one bench node, so a system adopts a sibling's identity; its own wins."""
        def roots(rootp):
            cp = bash(_SAY + f"ROOTP={rootp}\n"
                      + _lift(CARD_PRIV, "_tailnet_roots") + '\n_tailnet_roots /dev/x\n')
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            return cp.stdout.split()
        self.assertEqual(roots(8), ["8", "4", "6"])
        self.assertEqual(roots(6), ["6", "4", "8"])
        self.assertEqual(roots(4), ["4", "6", "8"])

    def test_the_stash_is_per_medium_not_per_pair(self):
        cp = bash(_SAY + _lift(CARD_PRIV, "_tailnet_stash")
                  + '\n_tailnet_stash /dev/mmcblk0; echo; _tailnet_stash /dev/mmcblk0\n')
        a, b = cp.stdout.split()
        self.assertEqual(a, b)


class TestUnitsForABusyBoxInit(WkTest):
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
        return bash(_SAY + "SYSCTL_N=0\n" + _lift(CARD_PRIV, "_put", "_unit_target", "_units_sysctl", "_units_edit")
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
        (self.root / "etc" / "init.d").mkdir(parents=True)
        cp = self._edit()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((self.root / "etc" / "systemd" / "system" / "wk-self-return.service").is_file())
        self.assertFalse((self.root / "etc" / "init.d" / "S11wk-self-disarm").exists())
        self.assertIn("installed 2 file(s)", cp.stdout)

    def test_the_archive_may_name_init_scripts_and_nothing_else_new(self):
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
    CONF = {"name": "rpi3", "driver": "pi-sd", "device": "/dev/mmcblk0", "root": "/dev/mmcblk0p2",
            "role": "bench-device", "profile": "webkit-2.52-yocto-rpi3-32"}

    def board(self, *boots):
        fake = FakeBoard(self.CONF)
        fake.rescue("rescue-1")
        for n, boot in enumerate(boots):
            fake.write_system(boot, "img-%s" % "abc"[n])
        fake.channel = "host"
        return fake, PiSd(REPO, dict(self.CONF), fake)

    def arms(self, fake):
        return [e[2] for e in fake.effects if e[:2] == ("card_priv", "second-arm")]

    def refused(self, fn, *args):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertRaises(act.Refused, fn, *args)
        return err.getvalue()

    def test_arm_and_disarm_go_through_the_helper_on_the_rescue(self):
        fake, d = self.board("/dev/mmcblk0p3")
        d.disarm()
        self.assertFalse([e for e in fake.effects if "second-disarm" in e], "disarmed a board that was not armed")
        d.arm("/dev/mmcblk0p3")
        self.assertEqual(self.arms(fake), ["/dev/mmcblk0@second"])

    def test_arm_selects_the_named_system_and_skips_only_when_armed_for_it(self):
        fake, d = self.board("/dev/mmcblk0p5", "/dev/mmcblk0p7")
        self.assertIn("is not a bench system's boot partition", self.refused(d.arm, ""))
        d.arm("/dev/mmcblk0p7")
        d.arm("/dev/mmcblk0p5")
        self.assertEqual(self.arms(fake), ["/dev/mmcblk0@third", "/dev/mmcblk0@second"])
        d.arm("/dev/mmcblk0p5")
        self.assertEqual(len(self.arms(fake)), 2, "armed for this system already; the arm should be a no-op")

    def test_arm_refuses_a_card_with_no_second_system(self):
        fake, d = self.board()
        self.assertIn("@second", self.refused(d.arm, "/dev/mmcblk0p3"))
        self.assertEqual(self.arms(fake), [])

    def test_disarm_puts_the_rescue_back_when_armed(self):
        fake, d = self.board("/dev/mmcblk0p3")
        d.arm("/dev/mmcblk0p3")
        d.disarm()
        self.assertIn(("card_priv", "second-disarm", "/dev/mmcblk0@second"), fake.effects)
        d.reboot()
        self.assertTrue(fake.on_rescue())

    def test_the_bench_systems_boot_partition_is_the_third(self):
        self.assertEqual(self.board()[1].boot_part(), "/dev/mmcblk0p3")

    def test_the_self_disarm_puts_the_rescue_config_back(self):
        self.assertIn("config.txt.rescue", self.board()[1].self_disarm_sh())

    def test_evidence_comes_from_the_card_not_the_record(self):
        fake, d = self.board("/dev/mmcblk0p5")
        d.arm("/dev/mmcblk0p5")
        out = d.evidence()
        self.assertIn("armed=yes", out)
        self.assertIn("system=img-a (on /dev/mmcblk0p5)", out)
        self.assertIsNone(fake.record, "the evidence wrote or needed a record")

    def test_reprovisioning_writes_both_systems_from_a_reader(self):
        out = self.board()[1].reprovision()
        self.assertIn("--disk <reader>:/dev/mmcblk0 --rescue", out)
        self.assertIn("--disk <reader>:/dev/mmcblk0@second", out)
        self.assertIn("wk boot rpi3", out)


if __name__ == "__main__":
    unittest.main()
