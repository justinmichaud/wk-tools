"""Every edit an image needs is made on the card, by the machine holding the
reader. `wk sysimage write --from` streams the image's own bytes onto the disk
(disk_write_source, boot/disk.sh) and then asks admin/wk-card-priv to retarget
the root, append the profile's cmdline and firmware settings, install the
fleet units, name the system on the boot partition and check that the firmware
can still reach a kernel -- so the driving machine needs no mtools, debugfs or
sfdisk, and the bytes on the card are the image's.

Each helper function is lifted out of admin/wk-card-priv with sed (the idiom
tests/test_wifi_seed.py uses) and run against temp directories standing in for
the mounted partitions, with `chown` and the privileged mount stubbed: this
machine is not root and holds no card. The gate, the dispatcher and what is
*not* referenced any more are checked statically.

Run: python3 -m unittest tests.test_card_edits -v
"""
import hashlib
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, stub_path

CARD_PRIV = REPO / "admin" / "wk-card-priv"
DISK_SH = REPO / "boot" / "disk.sh"
SYSIMAGE = REPO / "cmd" / "sysimage"

# Every verb this move added, and the driving function that calls it.
NEW_VERBS = {
    "parts": "v_parts",
    "root-spec": "v_root_spec",
    "retarget": "v_retarget",
    "cmdline-append": "v_cmdline_append",
    "config-append": "v_config_append",
    "boot-id": "v_boot_id",
    "units": "v_units",
    "boot-check": "v_boot_check",
    "helper": "v_helper",
    "diag": "v_diag",
}


def _lift(path, *funcs):
    """One or more function bodies, sed'd out of a shell file, so they can be
    called without sourcing a file that requires root at its top."""
    out = []
    for func in funcs:
        text = subprocess.run(
            ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
            capture_output=True, text=True,
        ).stdout
        assert text.strip(), f"could not lift {func} from {path}"
        out.append(text)
    return "\n".join(out)


# What the helper prints with, minus the privilege. `deny` and `fail` exit, as
# they do for real, so a refusal is a status a test can assert on.
_SAY = '''
say()  { printf 'wk-card-priv: %s\\n' "$*"; }
deny() { printf 'wk-card-priv: REFUSED: %s\\n' "$*" >&2; exit 3; }
fail() { printf 'wk-card-priv: %s\\n' "$*" >&2; exit 1; }
chown() { :; }
'''

# The gate and the mount, replaced by the two directories a test hands in:
# partition 1 is the boot filesystem, partition 2 the rootfs. What the gate
# refuses is admin/wk-card-priv's own contract (tests/test_wifi_seed.py), not
# what these edits do once it has allowed a disk.
_MOUNTED = '''
BOOTP=1; ROOTP=2; SECOND=""
gate() { GATED_DEV="$1"; }
part() { printf '%s%s' "$1" "$2"; }
with_mount() {
    [ "$1" = -r ] && shift
    local p="$1" fn="$2" m; shift 2
    case "$p" in
        *1) m="$BOOTDIR" ;;
        *2) m="$ROOTDIR" ;;
        *)  echo "with_mount: unexpected partition $p" >&2; return 1 ;;
    esac
    "$fn" "$m" "$@"
}
'''

# A partition table with an MBR signature, for the PARTUUID a retarget writes.
_SFDISK = '''#!/bin/sh
cat <<'JSON'
{"partitiontable": {"label": "dos", "id": "0x1c9dabbc", "device": "/dev/sdX",
  "partitions": [
    {"node": "/dev/sdX1", "start": 8192, "size": 1048576, "type": "c"},
    {"node": "/dev/sdX2", "start": 1056768, "size": 20971520, "type": "83"}]}}
JSON
'''

_SFDISK_NO_TABLE = '''#!/bin/sh
echo "sfdisk: does not contain a recognized partition table" >&2
exit 1
'''


class CardEditTest(WkTest):
    """A boot partition and a rootfs as plain directories."""

    def setUp(self):
        super().setUp()
        self.boot = self.tmp / "boot"
        self.root = self.tmp / "root"
        self.boot.mkdir()
        self.root.mkdir()

    def run_helper(self, script, path=None, stdin=None):
        prelude = f'BOOTDIR={self.boot!s}\nROOTDIR={self.root!s}\n{_SAY}{_MOUNTED}'
        env = {"PATH": f"{path}:/usr/bin:/bin"} if path else None
        return bash(prelude + script, env=env)


class TestRetarget(CardEditTest):
    """`retarget` gives the card a root reference that survives the kind of
    device it was written to, from the card's own partition table."""

    def _write_card(self):
        (self.boot / "cmdline.txt").write_text(
            "console=serial0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait\n")
        (self.root / "etc").mkdir()
        (self.root / "etc" / "fstab").write_text(
            "# a comment naming /dev/mmcblk0p1, which is not a line\n"
            "/dev/mmcblk0p2\t/\text4\tdefaults\t0\t1\n"
            "/dev/mmcblk0p1\t/boot\tvfat\tdefaults\t0\t2\n"
            "proc\t/proc\tproc\tdefaults\t0\t0\n")

    def test_root_becomes_the_cards_own_partuuid(self):
        self._write_card()
        with stub_path({"sfdisk": _SFDISK}) as binp:
            cp = self.run_helper(
                _lift(CARD_PRIV, "_table", "_boot_file", "_retarget_boot",
                      "_retarget_fstab", "v_retarget") + "\nv_retarget /dev/sdX\n",
                path=binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        cmdline = (self.boot / "cmdline.txt").read_text()
        self.assertIn("root=PARTUUID=1c9dabbc-02", cmdline, cmdline)
        self.assertNotIn("/dev/mmcblk0p2", cmdline, cmdline)
        # Everything else on the line is left exactly as it was.
        self.assertIn("console=serial0,115200", cmdline)
        self.assertIn("rootwait", cmdline)
        # And /boot, which is the line that actually names a card.
        fstab = (self.root / "etc" / "fstab").read_text()
        self.assertIn("PARTUUID=1c9dabbc-01\t/boot", fstab, fstab)
        self.assertIn("PARTUUID=1c9dabbc-02\t/", fstab, fstab)
        self.assertIn("proc\t/proc", fstab, fstab)
        # A comment is prose, not a mount.
        self.assertIn("# a comment naming /dev/mmcblk0p1", fstab, fstab)

    def test_an_fstab_already_retargeted_to_another_disk_id_is_rewritten(self):
        """The case that cost rpi3 a day. Anything that rewrites the card's MBR
        gives it a new disk identifier, and an fstab retargeted by an earlier
        write then carries PARTUUIDs of the *old* one. Those name no partition
        on this card: the root still mounts (the kernel gets it from cmdline)
        but /boot does not, local-fs.target fails, and every network unit
        ordered after it never starts -- a board that boots and goes quiet.

        The old code rewrote only fields starting with /dev/, and its read-back
        looked only for those, so it reported the card retargeted."""
        (self.boot / "cmdline.txt").write_text("root=PARTUUID=953569f6-02 rootwait\n")
        (self.root / "etc").mkdir()
        (self.root / "etc" / "fstab").write_text(
            "PARTUUID=953569f6-02\t/\text4\tdefaults\t0\t1\n"
            "PARTUUID=953569f6-01\t/boot\tvfat\tdefaults\t0\t2\n"
            "proc\t/proc\tproc\tdefaults\t0\t0\n")
        with stub_path({"sfdisk": _SFDISK}) as binp:
            cp = self.run_helper(
                _lift(CARD_PRIV, "_table", "_boot_file", "_retarget_boot",
                      "_retarget_fstab", "v_retarget") + "\nv_retarget /dev/sdX\n",
                path=binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        fstab = (self.root / "etc" / "fstab").read_text()
        self.assertIn("PARTUUID=1c9dabbc-01\t/boot", fstab, fstab)
        self.assertIn("PARTUUID=1c9dabbc-02\t/", fstab, fstab)
        self.assertNotIn("953569f6", fstab, "the old disk identifier survived:\n" + fstab)
        self.assertIn("proc\t/proc", fstab, "a line naming no partition was touched")
        self.assertIn("root=PARTUUID=1c9dabbc-02", (self.boot / "cmdline.txt").read_text())

    def test_dev_root_is_left_alone(self):
        """`/dev/root` is the kernel filling in what root= named, so it follows
        the card by itself. It names no partition number, which is why the
        rewrite keys on the trailing digit -- broaden the check without keeping
        that and a line which was always correct is reported as naming another
        disk, and the repair refuses to run (measured against rpi3's card,
        2026-09-01)."""
        (self.boot / "cmdline.txt").write_text("root=PARTUUID=953569f6-02 rootwait\n")
        (self.root / "etc").mkdir()
        (self.root / "etc" / "fstab").write_text(
            "/dev/root\t/\text4\tdefaults\t0\t1\n"
            "PARTUUID=953569f6-01\t/boot\tvfat\tdefaults\t0\t2\n")
        with stub_path({"sfdisk": _SFDISK}) as binp:
            cp = self.run_helper(
                _lift(CARD_PRIV, "_table", "_boot_file", "_retarget_boot",
                      "_retarget_fstab", "v_retarget") + "\nv_retarget /dev/sdX\n",
                path=binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        fstab = (self.root / "etc" / "fstab").read_text()
        self.assertIn("/dev/root\t/", fstab, "the kernel's own placeholder was rewritten")
        self.assertIn("PARTUUID=1c9dabbc-01\t/boot", fstab, fstab)
        # ...and it says what it moved, rather than only that it finished.
        self.assertIn("fstab: PARTUUID=953569f6-01 -> PARTUUID=1c9dabbc-01", cp.stdout, cp.stdout)

    def test_a_disk_with_no_partition_table_is_refused(self):
        self._write_card()
        with stub_path({"sfdisk": _SFDISK_NO_TABLE}) as binp:
            cp = self.run_helper(
                _lift(CARD_PRIV, "_table", "_boot_file", "_retarget_boot",
                      "_retarget_fstab", "v_retarget") + "\nv_retarget /dev/sdX\n",
                path=binp)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("REFUSED", cp.stdout + cp.stderr)

    def test_the_boot_file_under_os_prefix_wins(self):
        """an image carrying both boots the cmdline.txt under its os_prefix"""
        (self.boot / "cmdline.txt").write_text("root=/dev/sda2\n")
        (self.boot / "current").mkdir()
        (self.boot / "current" / "cmdline.txt").write_text("root=/dev/mmcblk0p2\n")
        cp = self.run_helper(
            _lift(CARD_PRIV, "_boot_file", "_retarget_boot")
            + "\n_retarget_boot \"$BOOTDIR\" 1c9dabbc-02\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("root=PARTUUID=1c9dabbc-02",
                      (self.boot / "current" / "cmdline.txt").read_text())
        self.assertIn("root=/dev/sda2", (self.boot / "cmdline.txt").read_text())


class TestRootSpec(CardEditTest):
    def test_reads_the_root_off_the_card(self):
        (self.boot / "cmdline.txt").write_text("console=tty1 root=PARTUUID=abc-02 rw\n")
        cp = self.run_helper(
            _lift(CARD_PRIV, "_boot_file", "_root_spec_probe", "v_root_spec")
            + "\nv_root_spec /dev/sdX\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "root=PARTUUID=abc-02", cp.stdout)

    def test_a_disk_with_no_cmdline_says_nothing_rather_than_failing(self):
        """a phone's bootloader has no cmdline.txt: a question that does not apply"""
        cp = self.run_helper(
            _lift(CARD_PRIV, "_boot_file", "_root_spec_probe", "v_root_spec")
            + "\nv_root_spec /dev/sdX\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout)


class TestCmdlineAppend(CardEditTest):
    def _run(self, text):
        import base64
        b64 = base64.b64encode(text.encode()).decode()
        return self.run_helper(
            _lift(CARD_PRIV, "check_b64", "check_text", "_boot_file",
                  "_cmdline_append_edit", "v_cmdline_append")
            + f"\nv_cmdline_append /dev/sdX {b64}\n")

    def test_appended_to_the_one_line_the_firmware_reads(self):
        (self.boot / "cmdline.txt").write_text("root=PARTUUID=abc-02 rootwait\n")
        cp = self._run("video=HDMI-A-1:1920x1080M@60D")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        text = (self.boot / "cmdline.txt").read_text()
        self.assertEqual(text.count("\n"), 1, f"more than one line: {text!r}")
        self.assertIn("root=PARTUUID=abc-02 rootwait video=HDMI-A-1:1920x1080M@60D", text)

    def test_appending_twice_leaves_one_copy(self):
        (self.boot / "cmdline.txt").write_text("root=PARTUUID=abc-02\n")
        self.assertEqual(self._run("quiet").returncode, 0)
        cp = self._run("quiet")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "cmdline.txt").read_text().count("quiet"), 1)
        self.assertIn("already carries", cp.stdout, cp.stdout)

    def test_a_second_line_is_refused(self):
        (self.boot / "cmdline.txt").write_text("root=PARTUUID=abc-02\n")
        cp = self._run("quiet\nsomething=else")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("one line", cp.stdout + cp.stderr)

    def test_a_disk_with_no_cmdline_is_a_failure_not_a_silent_no_op(self):
        cp = self._run("quiet")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no cmdline.txt", cp.stdout + cp.stderr)


class TestConfigAppend(CardEditTest):
    BLOCK = ("# --- wk sysimage: webkit-2.52-yocto-rpi5-64 ---------------\n"
             "[all]\n"
             "os_check=0\n")

    def _run(self, block):
        import base64
        b64 = base64.b64encode(block.encode()).decode()
        return self.run_helper(
            _lift(CARD_PRIV, "check_b64", "check_text", "_boot_file",
                  "_config_append_edit", "v_config_append")
            + f"\nv_config_append /dev/sdX {b64}\n")

    def test_the_block_lands_and_is_read_back(self):
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        cp = self._run(self.BLOCK)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        text = (self.boot / "config.txt").read_text()
        self.assertIn("arm_64bit=1", text)
        self.assertIn("os_check=0", text)

    def test_appending_twice_leaves_one_block(self):
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        self.assertEqual(self._run(self.BLOCK).returncode, 0)
        cp = self._run(self.BLOCK)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "config.txt").read_text().count("os_check=0"), 1)
        self.assertIn("already carries", cp.stdout, cp.stdout)

    def test_a_block_with_no_banner_is_refused(self):
        """the banner is the idempotency marker, so a block without one is refused"""
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        cp = self._run("os_check=0\n")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("wk sysimage:", cp.stdout + cp.stderr)


class TestBootId(CardEditTest):
    def test_the_id_lands_on_the_boot_partition(self):
        cp = self.run_helper(
            _lift(CARD_PRIV, "check_name", "_boot_id_edit", "v_boot_id")
            + "\nv_boot_id /dev/sdX webkit-2.52-yocto-rpi5-64-0123456789ab\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "wk-image.id").read_text().strip(),
                         "webkit-2.52-yocto-rpi5-64-0123456789ab")

    def test_an_id_that_is_not_a_name_is_refused(self):
        cp = self.run_helper(
            _lift(CARD_PRIV, "check_name", "_boot_id_edit", "v_boot_id")
            + "\nv_boot_id /dev/sdX 'not; a name'\n")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)


class TestUnits(CardEditTest):
    UNIT = ("[Unit]\nDescription=Hand the machine back\n"
            "[Service]\nType=oneshot\nExecStart=/bin/true\n"
            "[Install]\nWantedBy=multi-user.target\n")

    def _staged(self):
        work = self.tmp / "staged"
        (work / "systemd").mkdir(parents=True)
        (work / "sysctl.d").mkdir(parents=True)
        (work / "systemd" / "wk-self-return.service").write_text(self.UNIT)
        (work / "sysctl.d" / "90-wk-perf.conf").write_text("kernel.perf_event_paranoid = -1\n")
        return work

    def test_units_land_under_etc_systemd_system_and_are_wanted(self):
        (self.root / "lib" / "systemd").mkdir(parents=True)
        (self.root / "lib" / "systemd" / "systemd").write_text("")
        work = self._staged()
        cp = self.run_helper(
            _lift(CARD_PRIV, "_unit_target", "_units_sysctl", "_units_edit")
            + f"\n_units_edit \"$ROOTDIR\" {work}\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        unit = self.root / "etc" / "systemd" / "system" / "wk-self-return.service"
        self.assertTrue(unit.is_file(), cp.stdout + cp.stderr)
        self.assertEqual(unit.read_text(), self.UNIT)
        want = (self.root / "etc" / "systemd" / "system"
                / "multi-user.target.wants" / "wk-self-return.service")
        self.assertTrue(want.is_symlink(), "the unit is not wanted by anything")
        self.assertEqual(os.readlink(want), "/etc/systemd/system/wk-self-return.service")
        self.assertEqual((self.root / "etc" / "sysctl.d" / "90-wk-perf.conf").read_text(),
                         "kernel.perf_event_paranoid = -1\n")
        self.assertIn("installed 2 file(s)", cp.stdout)

    def test_a_timer_and_the_service_it_starts_both_land(self):
        """The self-return watchdog is a timer plus a service that returns at
        once. The timer is wanted by timers.target; the service must NOT be
        wanted by anything -- a WantedBy= as well would run it at boot, which
        reboots the board the moment it comes up -- so no target is accepted
        for a service whose timer is in the same archive."""
        (self.root / "lib" / "systemd").mkdir(parents=True)
        (self.root / "lib" / "systemd" / "systemd").write_text("")
        work = self._staged()
        (work / "systemd" / "wk-self-return.timer").write_text(
            "[Unit]\nDescription=x\n[Timer]\nOnBootSec=900\n"
            "[Install]\nWantedBy=timers.target\n")
        (work / "systemd" / "wk-self-return.service").write_text(
            "[Unit]\nDescription=x\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
        cp = self.run_helper(
            _lift(CARD_PRIV, "_unit_target", "_units_sysctl", "_units_edit")
            + f"\n_units_edit \"$ROOTDIR\" {work}\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        sysd = self.root / "etc" / "systemd" / "system"
        self.assertTrue((sysd / "wk-self-return.timer").is_file())
        self.assertTrue((sysd / "wk-self-return.service").is_file())
        self.assertTrue((sysd / "timers.target.wants" / "wk-self-return.timer").is_symlink(),
                        "the timer is not wanted by timers.target")
        self.assertFalse((sysd / "multi-user.target.wants" / "wk-self-return.service").exists(),
                         "the timer's service is ALSO started at boot, which reboots the board")

    def test_a_timer_is_a_member_the_archive_may_carry(self):
        cp = self._names(["systemd/wk-self-return.timer",
                          "systemd/wk-self-return.service"])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_a_service_with_no_wantedby_and_no_timer_is_still_refused(self):
        """the carve-out is only for a service a timer in the same archive
        starts; without one, nothing would ever run it."""
        (self.root / "lib" / "systemd").mkdir(parents=True)
        (self.root / "lib" / "systemd" / "systemd").write_text("")
        work = self._staged()
        (work / "systemd" / "wk-orphan.service").write_text(
            "[Unit]\n[Service]\nExecStart=/bin/true\n")
        cp = self.run_helper(
            _lift(CARD_PRIV, "_unit_target", "_units_sysctl", "_units_edit")
            + f"\n_units_edit \"$ROOTDIR\" {work}\n")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WantedBy", cp.stdout + cp.stderr)

    def test_a_unit_with_no_wantedby_is_refused(self):
        """a unit nothing would ever start is a watchdog that is not there"""
        (self.root / "lib" / "systemd").mkdir(parents=True)
        (self.root / "lib" / "systemd" / "systemd").write_text("")
        work = self._staged()
        (work / "systemd" / "wk-self-return.service").write_text("[Unit]\n[Service]\n")
        cp = self.run_helper(
            _lift(CARD_PRIV, "_unit_target", "_units_sysctl", "_units_edit")
            + f"\n_units_edit \"$ROOTDIR\" {work}\n")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WantedBy", cp.stdout + cp.stderr)

    def test_an_image_without_any_init_takes_nothing_and_says_so(self):
        work = self._staged()
        cp = self.run_helper(
            _lift(CARD_PRIV, "_unit_target", "_units_sysctl", "_units_edit")
            + f"\n_units_edit \"$ROOTDIR\" {work}\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("neither systemd nor /etc/init.d", cp.stdout)
        self.assertFalse((self.root / "etc").exists(), "something was installed anyway")

    def _names(self, members):
        """The archive's member list, checked the way v_units checks it before
        anything is unpacked. Built with python's tarfile so the member names
        are exactly the ones under test -- a traversal included, which is the
        point, and which the tar CLIs spell differently."""
        import io
        import tarfile
        tar = self.tmp / "units.tar"
        body = b"[Install]\nWantedBy=multi-user.target\n"
        with tarfile.open(tar, "w") as tf:
            for name in members:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tf.addfile(info, io.BytesIO(body))
        return bash(_SAY + _lift(CARD_PRIV, "_units_names") + f"\n_units_names {tar}\n")

    def test_a_plain_member_list_is_accepted(self):
        cp = self._names(["systemd/wk-self-return.service"])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_a_path_traversal_member_is_refused(self):
        cp = self._names(["systemd/../../etc/passwd"])
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("REFUSED", cp.stdout + cp.stderr)

    def test_a_member_outside_the_two_directories_is_refused(self):
        cp = self._names(["etc/shadow"])
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("REFUSED", cp.stdout + cp.stderr)

    def test_an_unpacked_symlink_is_refused(self):
        """whatever a tar implementation made of the names, only files are copied"""
        work = self.tmp / "unpacked"
        (work / "systemd").mkdir(parents=True)
        (work / "systemd" / "evil.service").symlink_to("/etc/shadow")
        cp = bash(_SAY + _lift(CARD_PRIV, "_units_unpacked") + f"\n_units_unpacked {work}\n")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("symlink", cp.stdout + cp.stderr)


class TestBootCheck(CardEditTest):
    """The firmware model is boot/check-boot-files.py, run against the card's
    own boot partition -- there is no second copy of it in the helper."""

    def _boot_tree(self, missing=()):
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        for name in ("start4.elf", "fixup4.dat", "kernel8.img", "bcm2711-rpi-4-b.dtb"):
            if name in missing:
                continue
            (self.boot / name).write_text("firmware")

    def _run(self, checker=None):
        checker = checker or (REPO / "boot" / "check-boot-files.py")
        return self.run_helper(
            f'CHECK_BOOT_FILES={checker}\n'
            + _lift(CARD_PRIV, "check_name", "_boot_check_run", "v_boot_check")
            + "\nv_boot_check /dev/sdX bcm2711-rpi-4-b.dtb\n")

    def test_a_complete_boot_tree_passes(self):
        self._boot_tree()
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("every file the firmware asks for resolves", cp.stdout)

    def test_a_tree_with_no_second_stage_firmware_is_refused(self):
        self._boot_tree(missing=("start4.elf",))
        cp = self._run()
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("start4.elf", cp.stdout + cp.stderr)

    def test_a_tree_with_no_kernel_is_refused(self):
        self._boot_tree(missing=("kernel8.img",))
        cp = self._run()
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("kernel", cp.stdout + cp.stderr)

    def test_a_pi5_boot_tree_passes_with_its_own_kernel_name(self):
        # meta-raspberrypi's raspberrypi5.conf sets SDIMG_KERNELIMAGE to
        # kernel_2712.img, so a correct Pi 5 image carries that name and none
        # of the Pi 4's. The checker must not refuse the whole board.
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        for name in ("start4.elf", "fixup4.dat", "kernel_2712.img",
                     "bcm2712-rpi-5-b.dtb"):
            (self.boot / name).write_text("firmware")
        cp = self.run_helper(
            f'CHECK_BOOT_FILES={REPO / "boot" / "check-boot-files.py"}\n'
            + _lift(CARD_PRIV, "check_name", "_boot_check_run", "v_boot_check")
            + "\nv_boot_check /dev/sdX bcm2712-rpi-5-b.dtb\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("every file the firmware asks for resolves", cp.stdout)

    def test_a_pi5_tree_with_no_kernel_at_all_is_still_refused(self):
        # The positive control: widening the list must not stop it catching
        # a boot partition with no kernel on it.
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        for name in ("start4.elf", "fixup4.dat", "bcm2712-rpi-5-b.dtb"):
            (self.boot / name).write_text("firmware")
        cp = self.run_helper(
            f'CHECK_BOOT_FILES={REPO / "boot" / "check-boot-files.py"}\n'
            + _lift(CARD_PRIV, "check_name", "_boot_check_run", "v_boot_check")
            + "\nv_boot_check /dev/sdX bcm2712-rpi-5-b.dtb\n")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("kernel", cp.stdout + cp.stderr)

    def test_a_missing_checker_refuses_loudly_and_names_the_remedy(self):
        """root runs the checker, so it is a fixed path -- absent, the verb refuses"""
        self._boot_tree()
        cp = self._run(checker="/nonexistent/wk-check-boot-files.py")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("no boot-file checker", out)
        self.assertIn("./setup --stage quiesce", out)


class TestHelperShape(unittest.TestCase):
    """The rules every verb is held to, checked the way tests/test_quick.py's
    test_card_helper_gate checks the older ones."""

    def setUp(self):
        self.text = CARD_PRIV.read_text(errors="replace")

    def test_every_new_device_verb_calls_the_gate(self):
        bad = []
        for verb, fn in NEW_VERBS.items():
            m = re.search(rf"(?ms)^{fn}\(\) \{{.*?^\}}", self.text)
            if not m:
                bad.append(f"{fn} is not defined")
            elif "gate " not in m.group(0):
                bad.append(f"{fn} ({verb}) does not call gate")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_every_new_verb_is_dispatched(self):
        case_m = re.search(r'(?ms)^case "\$verb" in.*?^esac', self.text)
        self.assertIsNotNone(case_m, "no verb dispatcher found")
        body = case_m.group(0)
        for verb, fn in NEW_VERBS.items():
            self.assertRegex(body, rf"{re.escape(verb)}\)\s*{fn}\b",
                             f"{verb} is not dispatched to {fn}")

    def test_the_usage_line_names_every_new_verb(self):
        usage = re.search(r'usage: wk-card-priv [^"]*', self.text)
        self.assertIsNotNone(usage, "no usage line")
        for verb in NEW_VERBS:
            self.assertIn(verb, usage.group(0), f"the usage line does not name {verb}")

    def test_nothing_a_caller_sends_is_executed(self):
        """the file root runs is a fixed path, never one that came in on argv"""
        self.assertIn("CHECK_BOOT_FILES=/usr/local/libexec/", self.text)
        m = re.search(r"(?ms)^_boot_check_run\(\) \{.*?^\}", self.text)
        self.assertIsNotNone(m)
        self.assertIn('python3 "$CHECK_BOOT_FILES" --root "$1"', m.group(0))

    def test_the_unit_archive_is_size_bounded(self):
        m = re.search(r"(?ms)^v_units\(\) \{.*?^\}", self.text)
        self.assertIsNotNone(m, "v_units is not defined")
        self.assertIn("UNITS_MAX", m.group(0), "v_units reads stdin with no size bound")
        self.assertIn("_units_names", m.group(0), "v_units unpacks without checking the names")


class TestNothingIsEditedOnTheDrivingMachine(unittest.TestCase):
    """The image is never opened here: no filesystem tooling, and none of the
    functions that edited a local copy of it."""

    RETIRED = (
        "fat_offset", "part_offset", "image_partuuid", "install_unit",
        "install_file", "install_units", "install_fleet_integration",
        "install_driving_key", "install_disk_id", "retarget_root",
        "cmdline_root_spec", "apply_cmdline_append", "apply_config_append",
        "image_root_spec", "image_boot_offset", "image_check_boot_files",
        "_card_root_spec", "_root_line", "disk_write_dd", "disk_verify_dd",
    )
    TOOLS = ("mtype", "mcopy", "mtools", "debugfs", "sfdisk", "e2fsck", "resize2fs")

    def _code(self, path):
        """The file with its comment lines dropped: a tool named in prose is
        prose, and this is about what runs."""
        return "\n".join(
            "" if line.lstrip().startswith("#") else line
            for line in path.read_text(errors="replace").splitlines())

    def test_no_filesystem_tooling_runs_on_the_driving_machine(self):
        bad = []
        for path in (SYSIMAGE, REPO / "lib" / "image.sh", DISK_SH):
            code = self._code(path)
            for tool in self.TOOLS:
                for m in re.finditer(rf"(?m)^.*\b{tool}\b.*$", code):
                    bad.append(f"{path.relative_to(REPO)}: {m.group(0).strip()}")
        self.assertEqual(bad, [], "still runs image tooling here:\n" + "\n".join(bad))

    def test_no_retired_local_edit_survives(self):
        bad = []
        for path in (SYSIMAGE, REPO / "lib" / "image.sh", DISK_SH):
            code = self._code(path)
            for name in self.RETIRED:
                if re.search(rf"\b{re.escape(name)}\b", code):
                    bad.append(f"{path.relative_to(REPO)}: {name}")
        self.assertEqual(bad, [], "retired local edit still referenced:\n" + "\n".join(bad))

    def test_the_write_streams_the_source_straight_onto_the_card(self):
        code = self._code(SYSIMAGE)
        self.assertIn('disk_write_source "$DISK_DEV" "$reader"', code,
                      "the write no longer streams its source onto the card")
        self.assertNotIn("WRITE_TMP", code, "the local scratch copy is back")


class TestFarSideStreamMeter(WkTest):
    """The stream is metered on the card machine, after the decompressor and
    ahead of the privileged writer (disk_stream_meter_py, boot/disk.sh): the
    read-back verify has no local copy to compare against, and this end never
    sees the decompressed bytes at all. Its report goes to fd 3, which the
    far-side pipeline points at the ssh session's stdout."""

    def _meter(self, payload):
        out = self.tmp / "out"
        report = self.tmp / "report"
        cp = bash(
            f'. "{REPO}/lib/common.sh"\n. "{REPO}/boot/disk.sh"\n'
            f'printf %s {payload!r} | python3 -c "$(disk_stream_meter_py)" '
            f'3>{report} > {out}\n')
        return cp, report, out

    def test_the_bytes_pass_through_unchanged_and_are_counted_and_hashed(self):
        payload = "the image's own bytes"
        cp, report, out = self._meter(payload)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(out.read_text(), payload)
        fields = dict(
            line.split("=", 1) for line in report.read_text().splitlines())
        self.assertEqual(int(fields["stream_bytes"]), len(payload))
        self.assertEqual(fields["stream_sha"],
                         hashlib.sha256(payload.encode()).hexdigest())


class TestTheCardMachineDecompresses(WkTest):
    """`--from` hands the source's own bytes to the card machine, which
    decompresses, meters and writes them in one pipeline: the image crosses
    the network once, in the shape it was built in."""

    # A card machine that answers for real: `m_ssh` runs the command it is
    # given, `sudo` drops its -n, and the helper swallows the image and
    # reports as the real one does.
    _PRIV = '''#!/bin/sh
cat >/dev/null
echo "wk-card-priv: written"
'''
    _SUDO = '''#!/bin/sh
[ "$1" = -n ] && shift
exec "$@"
'''

    def _write(self, payload, filter_="cat", reader=None):
        src = self.tmp / "image"
        src.write_bytes(payload)
        meta = self.tmp / "meta"
        with stub_path({"sudo": self._SUDO, "wk-card-priv": self._PRIV}) as binp:
            cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/disk.sh"
NODE_NAME=testmach
DISK_DRY=""
CARD_PRIV={binp}/wk-card-priv
m_ssh() {{ bash -c "$*"; }}
disk_write_source /dev/sdX {reader or f"cat {src}"!r} {filter_!r} {meta}
''', env={"PATH": f"{binp}:{os.environ['PATH']}"})
        return cp, meta

    def test_the_meta_file_carries_what_the_far_side_metered(self):
        payload = b"a disk image, near enough\n" * 100
        cp, meta = self._write(payload)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(
            meta.read_text().splitlines()[0],
            f"{len(payload)} {hashlib.sha256(payload).hexdigest()}",
            cp.stdout + cp.stderr)
        self.assertIn("wk-card-priv: written", cp.stderr,
                      "the helper's own report is no longer shown")

    def test_a_compressed_source_is_decompressed_on_the_card_machine(self):
        payload = b"the bytes the card gets\n" * 50
        xz = shutil.which("xz")
        if not xz:
            self.skipTest("no xz on this machine to make a compressed source with")
        src = self.tmp / "image.xz"
        src.write_bytes(subprocess.run([xz, "-c"], input=payload,
                                       capture_output=True).stdout)
        cp, meta = self._write(b"", filter_="xz -dc", reader=f"cat {src}")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        # What was metered is the decompressed image, not the .xz that crossed.
        self.assertEqual(
            meta.read_text().splitlines()[0],
            f"{len(payload)} {hashlib.sha256(payload).hexdigest()}",
            cp.stdout + cp.stderr)
        self.assertIn("decompressed there with xz -dc", cp.stderr)

    def test_a_decompressor_the_card_machine_lacks_is_refused_by_name(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/disk.sh"
NODE_NAME=testmach
DISK_DRY=""
m_ssh() {{ case "$*" in *"command -v"*) return 1 ;; esac; echo "wrote it anyway"; }}
disk_write_source /dev/sdX "cat /dev/null" "xz -dc" {self.tmp}/meta
''')
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("wrote it anyway", cp.stdout,
                         "the write went ahead without the decompressor")
        self.assertIn("testmach has no xz", cp.stderr)
        self.assertIn("install xz on testmach", cp.stderr)

    def test_nothing_on_this_machine_decompresses_the_source(self):
        reader = _lift(SYSIMAGE, "_from_reader")
        for tool in ("xz -dc", "zstd -dc", "gzip -dc"):
            self.assertNotIn(tool, reader,
                             f"_from_reader still decompresses with {tool} here")
        self.assertIn('disk_write_source "$DISK_DEV" "$reader" "$filter"',
                      SYSIMAGE.read_text(),
                      "the decompressor is no longer handed to the card machine")


class TestEjectWithoutUdisks(WkTest):
    """udisksctl is what powers a written card off; a machine without it is
    said so, not silently skipped -- the write is complete either way."""

    def test_a_machine_without_udisksctl_is_named_along_with_the_package(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/disk.sh"
NODE_NAME=testmach
DISK_DRY=""
m_ssh() {{ case "$*" in *udisksctl*) return 1 ;; esac; }}
disk_eject /dev/sdX
echo "RC=$?"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("RC=0", cp.stdout, "a missing udisksctl failed the write")
        self.assertIn("testmach has no udisksctl", cp.stderr)
        self.assertIn("safe to pull", cp.stderr)
        self.assertIn("udisks2", cp.stderr, "the package to install is not named")


class TestListingAWorkspaceMidRebuild(WkTest):
    """A yocto image stage removes the last image as it rebuilds, so an image
    workspace with no image is normal for hours. It keeps its row in `wk
    sysimage ls` (image_workspace_scan's `-` path), which says what is
    missing and how to get one."""

    def test_a_workspace_with_no_image_still_has_a_row(self):
        store = self.tmp / "store"
        ws = "yocto-webkit-2.52-yocto-rpi3-32"
        (store / "ws" / ws / "build").mkdir(parents=True)
        cp = self.run_wk("sysimage", "ls", env={"WK_STORE": str(store)})
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn(ws, out, out)
        self.assertIn("no image here yet", out, out)
        self.assertIn("'wk sysimage build webkit-2.52-yocto-rpi3-32' builds one", out, out)
        self.assertNotIn("no workspace here has built an image", out, out)


class TestDryRunIsTheSameSteps(WkTest):
    """A dry run runs the write's own steps with every mutation suppressed
    (disk_would), so what it reports cannot drift from what a write does."""

    def _step(self, step, dry):
        return bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/lib/image.sh"
. "{REPO}/boot/disk.sh"
NODE_NAME=testmach
DISK_DRY={dry!r}
card_priv() {{ echo "card_priv should not have run: $*" >&2; exit 9; }}
m_ssh() {{ echo "m_ssh should not have run: $*" >&2; exit 9; }}
{step}
echo DONE
''')

    def test_every_card_step_is_suppressed_and_reports_itself(self):
        steps = [
            'disk_unmount /dev/sdX',
            'disk_write_source /dev/sdX "cat /dev/null" cat /dev/null',
            'disk_verify_stream /dev/sdX /dev/null',
            'disk_parts_present /dev/sdX',
            'disk_root_spec /dev/sdX',
            'disk_retarget_root /dev/sdX',
            'disk_cmdline_append /dev/sdX quiet',
            'disk_config_append /dev/sdX "# --- wk sysimage: p ---"',
            'disk_boot_id /dev/sdX an-id',
            'disk_install_units /dev/sdX /nonexistent',
            'disk_check_boot_files /dev/sdX testmach some.dtb',
            'disk_check_root /dev/sdX testmach',
            'disk_unique_identity /dev/sdX',
            'disk_install_fleet /dev/sdX "id=x" "ssh-ed25519 AAAA"',
            'disk_seed_role /dev/sdX bench',
            'disk_seed_tailnet /dev/sdX name',
            'disk_seed_wifi /dev/sdX rpi3',
            'disk_grow /dev/sdX',
            'disk_eject /dev/sdX',
        ]
        for step in steps:
            with self.subTest(step=step):
                cp = self._step(step, "1")
                out = cp.stdout + cp.stderr
                self.assertEqual(cp.returncode, 0, out)
                self.assertIn("DONE", out, out)
                self.assertRegex(out, r"(?m)^\s*would ", out)

    def test_without_the_dry_flag_a_step_really_asks_the_card(self):
        cp = self._step("disk_grow /dev/sdX", "")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("card_priv should not have run", cp.stdout + cp.stderr)


class TestWriteDryRunIsTheWholeSequence(WkTest):
    """The whole command, with the fleet machine faked by a stub `ssh`: a dry
    run reports the write's own steps, in order, from the functions that make
    them -- so an edit that moves, loses or reorders a step shows up here
    without a card in a reader."""

    _SSH = '''#!/bin/sh
# a fleet machine that answers, whose card helper allows the disk
case "$*" in
  *card-priv*status*) exit 0 ;;
  *card-priv*check*)  echo "wk-card-priv: /dev/sdX may be written: usb 64G"; exit 0 ;;
  *card-priv*wifi-host*) echo "wk-card-priv: wifi-host: yes ssid=TestNet"; exit 0 ;;
  *) exit 0 ;;
esac
'''

    def test_the_steps_are_reported_in_the_order_the_card_meets_them(self):
        key = self.tmp / "id.pub"
        key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAtest test@example\n")
        store = self.tmp / "store"
        with stub_path({"ssh": self._SSH}) as binp:
            cp = self.run_wk(
                "sysimage", "write", "--from", str(REPO / "README.md"),
                "--profile", "webkit-2.52-yocto-rpi5-64",
                "--disk", "rpi5:/dev/sdX", "--dry-run",
                env={"PATH": f"{binp}:{os.environ['PATH']}",
                     "WK_IMAGE_KEY": str(key), "WK_STORE": str(store)},
            )
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        want = [
            "would unmount",
            "would stream the image onto /dev/sdX",
            "would read /dev/sdX back",
            "would check that /dev/sdX came out of this with a partition table",
            "would retarget /dev/sdX's root=",
            "would append this profile's firmware block",
            "would name the system on /dev/sdX's boot partition",
            "would stamp a unique disk identity",
            "would install the identity marker and the driving ssh key",
            "would install the fleet units",
            "would check that every file a rpi5's firmware asks for resolves",
            "would check that the system on /dev/sdX names a root",
            "would mark /dev/sdX a bench system",
            "would seed the tailnet identity",
            "would seed rpi5's own WiFi credential",
            "would flush and power off",
        ]
        at = -1
        for step in want:
            here = out.find(step)
            self.assertNotEqual(here, -1, f"the dry run never says {step!r}:\n{out}")
            self.assertGreater(here, at, f"{step!r} is reported out of order:\n{out}")
            at = here
        self.assertIn("dry run -- nothing was written.", out, out)
        # Nothing on this machine opened the image: it was never read at all.
        self.assertNotIn("reading ", out, out)


class TestTheWriteStaysAddressedToTheReader(WkTest):
    """A card is written by the machine holding the reader, for whatever board
    the image is for -- rarely the same machine. Composing the units needs the
    *image* machine's driver (its self-disarm), so that lookup happens in a
    subshell: NODE_* is what every card edit is addressed to, and loading
    another machine into this shell sends the rest of the write to the wrong
    board."""

    _LIFTED = _lift(SYSIMAGE, "_self_disarm_for", "stage_unit", "stage_sysctl", "stage_init", "stage_units")

    def _sh(self, body, machine="rpi5"):
        return self.bash(f"""
set -eu
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/boot/machines.sh"
{self._LIFTED}
IMG_PROFILE=test-profile IMG_WATCHDOG=600
machine_load {machine}
{body}
""")

    def test_staging_the_units_leaves_the_reader_machine_loaded(self):
        seed = self.tmp / "seed"
        for d in ("systemd", "sysctl.d", "init.d"):
            (seed / d).mkdir(parents=True)
        cp = self._sh(f"""
IMG_MACHINE=rpi3     # the image is for another board, whose ssh name differs
stage_units {seed} "$(_self_disarm_for "$IMG_MACHINE")" >/dev/null 2>&1
printf 'name=%s ssh=%s role=%s driver=%s\n' \
    "$NODE_NAME" "$NODE_SSH" "$NODE_ROLE" "$NODE_DRIVER"
""")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(
            cp.stdout.strip(),
            "name=rpi5 ssh=rpi5 role=workstation driver=rpi5-usb",
            "staging the units re-aimed the write at the image's machine:\n"
            + cp.stdout + cp.stderr,
        )

    def test_a_medium_armed_board_gets_its_drivers_self_disarm(self):
        # Both boards park their own arming, each the thing its driver armed:
        # rpi3 puts the rescue's config.txt back (pi-sd), rpi4 removes the
        # tryboot staging from the SD (pi-tryboot) -- that board does not
        # consume the flag, so the staging is spent by the boot that used it or
        # the board never leaves its bench system.
        cp = self._sh('_self_disarm_for rpi4')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("tryboot.txt", cp.stdout, cp.stdout + cp.stderr)
        self.assertNotIn("'", cp.stdout, "a quote here would split systemd's ExecStart")
        cp = self._sh('_self_disarm_for rpi3')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("config.txt.rescue", cp.stdout, cp.stdout + cp.stderr)
        self.assertNotIn("'", cp.stdout, "a quote here would split systemd's ExecStart")

    def test_an_unknown_board_has_nothing_to_park(self):
        for machine in ("nosuchmachine", ""):
            with self.subTest(machine=machine):
                cp = self._sh(f'_self_disarm_for {machine or '""'}')
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(cp.stdout, "", cp.stdout + cp.stderr)

    def test_the_self_disarm_unit_is_skipped_when_there_is_nothing_to_park(self):
        seed = self.tmp / "seed2"
        for d in ("systemd", "sysctl.d", "init.d"):
            (seed / d).mkdir(parents=True)
        cp = self._sh(f'stage_units {seed} "" >/dev/null 2>&1; ls {seed}/systemd {seed}/init.d')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("wk-self-disarm", cp.stdout, cp.stdout)
        self.assertIn("wk-self-return.service", cp.stdout, cp.stdout)
        self.assertIn("S99wk-self-return", cp.stdout, cp.stdout)

    def test_the_watchdog_is_a_timer_that_blocks_no_target(self):
        """A Type=oneshot that sleeps is not active until it returns, so it
        holds a start job -- and multi-user.target, which wants it, stays
        inactive for the whole watchdog on every boot. Measured on the rpi4
        (2026-09-01): 15 minutes of every boot with multi-user.target inactive
        and the start job under TimeoutStartSec=infinity. The wait belongs to a
        timer."""
        seed = self.tmp / "seed-timer"
        for d in ("systemd", "sysctl.d", "init.d"):
            (seed / d).mkdir(parents=True)
        cp = self._sh(f'stage_units {seed} "" >/dev/null 2>&1; ls {seed}/systemd')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk-self-return.timer", cp.stdout, cp.stdout)

        timer = (seed / "systemd" / "wk-self-return.timer").read_text()
        self.assertIn("OnBootSec=", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("/etc/wk/rescue", timer, "the timer is not gated on the rescue marker")

        svc = (seed / "systemd" / "wk-self-return.service").read_text()
        self.assertNotIn("sleep", svc, "the service still waits inside its own ExecStart")
        self.assertNotIn("TimeoutStartSec", svc, "a service that returns at once needs no start timeout")
        self.assertNotIn("[Install]", svc,
                         "the timer's service is also wanted by a target, so it runs at "
                         "boot and reboots the board immediately")
        self.assertNotIn("multi-user.target", svc)
        self.assertIn("wk-keep-running", svc)
        self.assertIn("/etc/wk/rescue", svc, "the service is not gated on the rescue marker")

    def test_no_watchdog_seconds_stages_neither_half(self):
        """a timer with no OnBootSec fires at once and reboots the board, so a
        profile that names no watchdog gets no timer at all -- and is warned
        about, not silently left without one."""
        seed = self.tmp / "seed-nowd"
        for d in ("systemd", "sysctl.d", "init.d"):
            (seed / d).mkdir(parents=True)
        cp = self._sh(f'IMG_WATCHDOG=""; stage_units {seed} "" 2>&1; ls {seed}/systemd {seed}/init.d')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("wk-self-return", cp.stdout, cp.stdout)
        self.assertIn("will not hand its machine back", cp.stdout)

    def test_a_busybox_image_gets_the_same_two_jobs_as_init_scripts(self):
        seed = self.tmp / "seed3"
        for d in ("systemd", "sysctl.d", "init.d"):
            (seed / d).mkdir(parents=True)
        cp = self._sh(f'stage_units {seed} "$(_self_disarm_for rpi3)" >/dev/null 2>&1; ls {seed}/init.d')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split(), ["S11wk-self-disarm", "S99wk-self-return"])
        disarm = (seed / "init.d" / "S11wk-self-disarm").read_text()
        self.assertTrue(disarm.startswith("#!/bin/sh\n"))
        self.assertIn("/etc/wk/rescue", disarm, "the script is not gated on the rescue marker")
        self.assertIn("config.txt.rescue", disarm)
        ret = (seed / "init.d" / "S99wk-self-return").read_text()
        self.assertIn("sleep 600", ret)
        self.assertIn("wk-keep-running", ret)
        for script in (disarm, ret):
            self.assertEqual(subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()


class TestBootCheckThatCannotBeAsked(WkTest):
    """A writer whose helper has no checker beside it cannot answer the
    boot-file question. That is the same state as a card written for another
    machine -- not checked -- and is reported as such, not as missing files
    (a rescue's helper is its image's, so the remedy there is a rebuild)."""

    def _check(self, helper_out, rc):
        return bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/lib/image.sh"
NODE_NAME=rescue
DISK_DRY=""
card_priv() {{ printf '%s\\n' {helper_out!r}; return {rc}; }}
disk_check_boot_files /dev/sdX@second rpi3 some.dtb && echo CONTINUED
''')

    def test_no_checker_is_not_checked_and_says_so(self):
        cp = self._check("wk-card-priv: REFUSED: there is no boot-file checker at /usr/local/libexec/wk-check-boot-files.py on this machine.", 3)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("CONTINUED", cp.stdout)
        self.assertIn("NOT checked", cp.stderr)

    def test_missing_files_still_refuse(self):
        cp = self._check("wk-card-priv: this card's boot partition is missing files a some.dtb board's firmware asks for", 1)
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("CONTINUED", cp.stdout)
        self.assertIn("missing files", cp.stderr)


class TestListingSaysWhichImagesAreInProgress(WkTest):
    """`wk sysimage ls` carries the state in a column: an image being built
    right now is the one most likely being looked for, and `-` in the size
    column is not an answer. `building` is the workspace's own live answer
    (_ws_building), never a record."""

    def test_a_running_build_is_stated_on_the_row(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh"
{_lift(REPO / "cmd" / "sysimage", "_ws_profile")}
{_lift(REPO / "cmd" / "sysimage", "cmd_ls")}
image_workspace_scan() {{ printf 'yocto\\tyocto-p\\t-\\t0\\t-\\n'; }}
_ws_building() {{ return 0; }}
cmd_ls
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        row = [l for l in cp.stdout.splitlines() if l.startswith("yocto-p")]
        self.assertTrue(row, cp.stdout)
        self.assertIn("building", row[0], row[0])
        self.assertIn("STATE", cp.stdout, "the listing has no state column")
        self.assertIn("wk logs yocto-p", cp.stdout, cp.stdout)

    def test_an_image_present_while_a_build_runs_says_both(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh"
{_lift(REPO / "cmd" / "sysimage", "_ws_profile")}
{_lift(REPO / "cmd" / "sysimage", "cmd_ls")}
image_workspace_scan() {{ printf 'yocto\\tyocto-p\\t/img/a.wic.xz\\t1024\\t2026-08-30\\n'; }}
_ws_building() {{ return 0; }}
cmd_ls
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        row = [l for l in cp.stdout.splitlines() if l.startswith("yocto-p")]
        self.assertIn("building", row[0], row[0])
        self.assertIn("previous image", cp.stdout, cp.stdout)

    def test_a_finished_image_is_ready_and_an_empty_workspace_is_none(self):
        for path, want in (("/img/a.wic.xz", "ready"), ("-", "none")):
            cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh"
{_lift(REPO / "cmd" / "sysimage", "_ws_profile")}
{_lift(REPO / "cmd" / "sysimage", "cmd_ls")}
image_workspace_scan() {{ printf 'yocto\\tyocto-p\\t{path}\\t1024\\t2026-08-30\\n'; }}
_ws_building() {{ return 1; }}
cmd_ls
''')
            with self.subTest(path=path):
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                row = [l for l in cp.stdout.splitlines() if l.startswith("yocto-p")]
                self.assertIn(want, row[0], row[0])

    def test_nothing_to_list_prints_no_header(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh"
{_lift(REPO / "cmd" / "sysimage", "_ws_profile")}
{_lift(REPO / "cmd" / "sysimage", "cmd_ls")}
image_workspace_scan() {{ :; }}
cmd_ls
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("STATE", cp.stdout, "a header over an empty table")
        self.assertIn("no workspace here has built an image", cp.stdout + cp.stderr)

class TestDiag(CardEditTest):
    """`diag` reads a system's own account of its last boot off its boot
    partition. It exists for the system that never became reachable, which is
    the one that cannot be asked over ssh -- so with the card in a reader this
    is the only path to it (rpi3, 2026-09-01: booted, never appeared, and the
    account it had written could not be read)."""

    def _run(self):
        return self.run_helper(_lift(CARD_PRIV, "_diag_probe", "v_diag")
                               + "\nDIAG_MAX=65536\nv_diag /dev/sdX\n")

    def test_it_prints_the_account_the_image_wrote(self):
        (self.boot / "wk-diag.txt").write_text("id=some-image\nwlan0: no carrier\n")
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wlan0: no carrier", cp.stdout)

    def test_an_image_that_never_got_that_far_says_so(self):
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("did not get that far", cp.stdout)

    def test_it_is_bounded(self):
        (self.boot / "wk-diag.txt").write_text("x" * 200000)
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertLessEqual(len(cp.stdout), 65536 + 200, "a card can hand back any amount of text")


class TestRescueHelper(CardEditTest):
    """`helper` copies the writing machine's own card helper onto the system it
    is writing -- every system, rescue or bench. A rescue writes bench media
    with it; a bench system arms its sibling with it where the arming is an edit
    to the card (pi-sd), which is a boot per A/B leg saved."""

    def _run(self, extra=""):
        script = (_lift(CARD_PRIV, "_helper_install", "v_helper")
                  + f'\nSELF={self.tmp / "helper"!s}\n'
                  + f'CHECK_BOOT_FILES={self.tmp / "checker.py"!s}\n'
                  + extra + '\nv_helper /dev/sdX\n')
        return self.run_helper(script)

    def setUp(self):
        super().setUp()
        (self.tmp / "helper").write_text("#!/bin/bash\n# the helper\n")
        (self.tmp / "checker.py").write_text("#!/usr/bin/env python3\n")

    def test_both_files_land_root_owned_and_executable(self):
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        d = self.root / "usr" / "local" / "libexec"
        self.assertEqual((d / "wk-card-priv").read_text(), "#!/bin/bash\n# the helper\n")
        self.assertTrue((d / "wk-check-boot-files.py").exists())
        for f in (d / "wk-card-priv", d / "wk-check-boot-files.py"):
            self.assertTrue(os.access(f, os.X_OK), f"{f.name} is not executable")
        self.assertIn("helper:", cp.stdout)

    def test_a_bench_pair_takes_it_too(self):
        """The case that used to be refused. A bench system arms its sibling
        where the arming is an edit to the card, so it needs the helper; the
        gate already lets a system address its sibling pair, which is what the
        write that puts it there just did."""
        cp = self._run(extra="SECOND=1")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((self.root / "usr" / "local" / "libexec" / "wk-card-priv").is_file())

    def test_no_checker_on_this_machine_is_a_refusal_with_a_remedy(self):
        (self.tmp / "checker.py").unlink()
        cp = self._run()
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("--stage quiesce", cp.stderr)
        self.assertFalse((self.root / "usr").exists(), "nothing is written on a refusal")


class TestRescueHelperIsWiredIntoARescueWrite(unittest.TestCase):
    def test_every_system_a_write_makes_takes_it(self):
        """cmd/sysimage installs it unconditionally: a rescue writes bench media
        with it, a bench system arms its sibling with it."""
        text = SYSIMAGE.read_text()
        self.assertIn("disk_install_helper \"$DISK_DEV\"", text)
        self.assertNotIn('[ "$role" != rescue ] || disk_install', text,
                         "the helper is installed on every system a write makes")
        self.assertIn("disk_install_helper()", DISK_SH.read_text())

    def test_an_older_helper_on_the_reader_is_told_apart_from_a_refusal(self):
        """a reader whose checkout predates the verb gets the one-line remedy,
        not 'could not'."""
        text = DISK_SH.read_text()
        block = text[text.index("disk_install_helper()"):]
        block = block[:block.index("\n}\n")]
        self.assertIn("usage: wk-card-priv", block)
        self.assertIn("--stage quiesce", block)
