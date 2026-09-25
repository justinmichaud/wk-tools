"""Every edit an image needs is made on the card, by the machine holding the
reader. `wk sysimage write --from` streams the image's own bytes onto the disk
(lib/wk/sysimage/write.py) and then asks admin/wk-card-priv to retarget
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
import contextlib
import io
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, TAILSCALE_KNOWS_NOTHING, WkTest, bash, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk.machine import Fake  # noqa: E402
from wk.sysimage import write  # noqa: E402

CARD_PRIV = REPO / "admin" / "wk-card-priv"
WRITE = REPO / "lib" / "wk" / "sysimage" / "write.py"

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
    "boot-read": "v_boot_read",
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
    """The rules every verb is held to, checked the way tests/test_static_rules.py's
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
    PATHS = (WRITE, REPO / "lib" / "image.sh", REPO / "boot" / "disk.sh")

    def _code(self, path):
        """The file with its comment lines dropped: a tool named in prose is
        prose, and this is about what runs."""
        return "\n".join(
            "" if line.lstrip().startswith("#") else line
            for line in path.read_text(errors="replace").splitlines())

    def test_no_filesystem_tooling_runs_on_the_driving_machine(self):
        bad = []
        for path in self.PATHS:
            code = self._code(path)
            for tool in self.TOOLS:
                for m in re.finditer(rf"(?m)^.*\b{tool}\b.*$", code):
                    bad.append(f"{path.relative_to(REPO)}: {m.group(0).strip()}")
        self.assertEqual(bad, [], "still runs image tooling here:\n" + "\n".join(bad))

    def test_no_retired_local_edit_survives(self):
        bad = []
        for path in self.PATHS:
            code = self._code(path)
            for name in self.RETIRED:
                if re.search(rf"\b{re.escape(name)}\b", code):
                    bad.append(f"{path.relative_to(REPO)}: {name}")
        self.assertEqual(bad, [], "retired local edit still referenced:\n" + "\n".join(bad))

    def test_the_reader_hands_over_the_builders_own_bytes(self):
        """The decompressor runs on the card machine; this end only reads."""
        w = write.Write(REPO, {}, Fake(), None)
        w.machine.files["/x.wic.xz"] = "x"
        self.assertEqual(w.reader("/x.wic.xz"), ["cat", "/x.wic.xz"])


class TestDryRunIsTheSameSteps(unittest.TestCase):
    """A dry run runs the write's own steps with every card call suppressed,
    so what it reports cannot drift from what a write does."""

    DEV = "/dev/sdX"
    STEPS = (("unmount", DEV), ("tailnet_save", DEV), ("stream", DEV, ["cat", "/x"], "cat"), ("verify", DEV, {}),
             ("parts_present", DEV), ("retarget", DEV), ("unique_identity", DEV),
             ("fleet_install", DEV, "id=x", "ssh-ed25519 AAAA"), ("put_units", DEV, {}),
             ("check_boot_files", DEV, "rpi5", "some.dtb"), ("check_root", DEV, "rpi5"), ("seed_role", DEV, "bench"),
             ("install_helper", DEV), ("install_autoboot", DEV), ("seed_tailnet", DEV, "name"), ("seed_wifi", DEV, "rpi3"),
             ("eject", DEV))

    class Refuse:
        channel = "host"

        def call(self, *a, **kw):
            raise AssertionError("the card was asked under --dry-run: %r" % (a,))

    def test_every_card_step_is_suppressed_and_reports_itself(self):
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            for name, *args in self.STEPS:
                with self.subTest(step=name):
                    w = write.Write(REPO, {}, Fake(), None)
                    w.conf, w.ch = {"NODE_NAME": "testmach"}, self.Refuse()
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        getattr(w, name)(*args)
                    self.assertRegex(err.getvalue(), r"(?m)^\s*would ")
                    self.assertEqual(len(w.plan), 1)
                    self.assertEqual(w.machine.effects, [])


class TestWriteDryRunIsTheWholeSequence(WkTest):
    """The whole command, with the fleet machine faked by a stub `ssh`: a dry
    run reports the write's own steps, in order, from the functions that make
    them -- so an edit that moves, loses or reorders a step shows up here
    without a card in a reader."""

    _SSH = """#!/bin/sh
# a fleet machine that answers, whose card helper allows the disk
case "$*" in
  *card-priv*status*) exit 0 ;;
  *card-priv*check*)  echo "wk-card-priv: /dev/sdX may be written: usb 64G"; exit 0 ;;
  *card-priv*wifi-host*) echo "wk-card-priv: wifi-host: yes ssid=TestNet"; exit 0 ;;
  *) exit 0 ;;
esac
"""

    def test_the_steps_are_reported_in_the_order_the_card_meets_them(self):
        key = self.tmp / "id.pub"
        key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAtest test@example\n")
        store = self.tmp / "store"
        with stub_path({"ssh": self._SSH, "tailscale": TAILSCALE_KNOWS_NOTHING}) as binp:
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
            "would ask: write",
            "would unmount",
            # Retiring a node this card's name is held by is a state change
            # made outside this machine, so a dry run names it.
            "would read this machine's tailnet view",
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
        self.assertNotIn("reading ", out, out)


class TestTheUnitsAreTheImageMachinesAndTheWriteIsTheReaders(unittest.TestCase):
    """A card is written by the machine holding the reader, for whatever board
    the image is for -- rarely the same machine. The units carry the image
    machine's own self-disarm (lib/wk/boot); every card call is addressed to
    the reader (tests/test_sysimage_write.py)."""

    def setUp(self):
        self.w = write.Write(REPO, {"HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent"}, Fake(), None)

    def staged(self, watchdog="600", disarm=""):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            return write.stage_units(REPO, watchdog, disarm, "test-profile"), err.getvalue()

    def test_a_medium_armed_board_gets_its_drivers_self_disarm(self):
        # rpi3 puts the rescue's config.txt back (pi-sd); rpi4 removes the
        # tryboot staging from the SD (pi-tryboot), which that board does not
        # consume by itself.
        for machine, want in (("rpi4", "tryboot.txt"), ("rpi3", "config.txt.rescue")):
            with self.subTest(machine=machine):
                line = self.w.self_disarm(machine)
                self.assertIn(want, line)
                self.assertNotIn("'", line, "a quote here would split systemd's ExecStart")

    def test_an_unknown_board_has_nothing_to_park(self):
        for machine in ("nosuchmachine", ""):
            self.assertEqual(self.w.self_disarm(machine), "")

    def test_the_self_disarm_unit_is_skipped_when_there_is_nothing_to_park(self):
        units, _ = self.staged()
        self.assertNotIn("systemd/wk-self-disarm.service", units)
        self.assertIn("systemd/wk-self-return.service", units)
        self.assertIn("init.d/S99wk-self-return", units)

    def test_the_watchdog_is_a_timer_that_blocks_no_target(self):
        """A Type=oneshot that sleeps is not active until it returns, so it
        holds a start job -- and multi-user.target, which wants it, stays
        inactive for the whole watchdog on every boot. Measured on the rpi4
        (2026-09-01): 15 minutes of every boot with multi-user.target inactive
        and the start job under TimeoutStartSec=infinity. The wait belongs to a
        timer."""
        units, _ = self.staged()
        timer = units["systemd/wk-self-return.timer"]
        self.assertTrue(timer.endswith("[Timer]\nAccuracySec=1s\nOnBootSec=600\n"), timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("/etc/wk/rescue", timer, "the timer is not gated on the rescue marker")
        svc = units["systemd/wk-self-return.service"]
        self.assertNotIn("sleep", svc, "the service still waits inside its own ExecStart")
        self.assertNotIn("TimeoutStartSec", svc, "a service that returns at once needs no start timeout")
        self.assertNotIn("[Install]", svc,
                         "the timer's service is also wanted by a target, so it runs at "
                         "boot and reboots the board immediately")
        self.assertIn("wk-keep-running", svc)
        self.assertIn("/etc/wk/rescue", svc, "the service is not gated on the rescue marker")

    def test_no_watchdog_seconds_stages_neither_half(self):
        """a timer with no OnBootSec fires at once and reboots the board, so a
        profile that names no watchdog gets no timer at all -- and is warned
        about, not silently left without one."""
        units, err = self.staged(watchdog="")
        self.assertFalse([u for u in units if "wk-self-return" in u], units)
        self.assertIn("will not hand its machine back", err)

    def test_the_self_disarm_lands_in_the_units_last_section(self):
        units, _ = self.staged(disarm='a=$(x); echo "$a"')
        unit = units["systemd/wk-self-disarm.service"]
        self.assertTrue(unit.endswith("[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                                      "ExecStart=/bin/sh -c 'a=$$(x); echo \"$$a\"'\n"), unit)

    def test_a_busybox_image_gets_the_same_two_jobs_as_init_scripts(self):
        units, _ = self.staged(disarm=self.w.self_disarm("rpi3"))
        self.assertEqual(sorted(u for u in units if u.startswith("init.d/")),
                         ["init.d/S11wk-self-disarm", "init.d/S99wk-self-return"])
        disarm, ret = units["init.d/S11wk-self-disarm"], units["init.d/S99wk-self-return"]
        for script in (disarm, ret):
            self.assertTrue(script.startswith("#!/bin/sh\n"))
            self.assertIn("/etc/wk/rescue", script, "the script is not gated on the rescue marker")
            self.assertEqual(subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True).returncode, 0)
        self.assertIn("config.txt.rescue", disarm)
        self.assertIn("WK_WATCHDOG=600\n", ret)
        self.assertIn('sleep "$WK_WATCHDOG"', ret)
        self.assertIn("wk-keep-running", ret)

    def test_the_watchdog_scripts_run_their_sleep_in_the_background(self):
        """Last in rcS so the browser is up when the clock starts, and backgrounded so init does not wait it out."""
        units, _ = self.staged(watchdog="2")
        cp = subprocess.run(["sh", "-c", units["init.d/S99wk-self-return"].replace("/etc/wk/rescue", "/nonexistent")
                             .replace("reboot", "true"), "S99", "start"], capture_output=True, timeout=1)
        self.assertEqual(cp.returncode, 0)


class TestBootRead(CardEditTest):
    """`boot-read` is how a **workstation** reads its medium at all. The machine
    holding the card runs nothing privileged but this helper, so a plain
    `sudo -n mount` there answers "interactive authentication is required" and
    every reader above it -- the system id, the boot dump, rpi5's pair selector
    -- saw an empty medium rather than a refusal (rpi5, 2026-09-03).

    Its whole surface is the allowlist: three fixed filenames, a partition
    number, read-only, bounded."""

    def _run(self, partition="1", name="wk-diag.txt"):
        return self.run_helper(
            _lift(CARD_PRIV, "check_partno", "_boot_read_probe", "v_boot_read")
            + "\nBOOT_READ_MAX=65536\n"
            + f"v_boot_read /dev/sdX '{partition}' '{name}'\n")

    def test_it_prints_the_file_the_image_wrote(self):
        (self.boot / "wk-diag.txt").write_text("id=some-image\nwlan0: no carrier\n")
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wlan0: no carrier", cp.stdout)

    def test_the_system_id_is_read_the_same_way(self):
        """One verb for all three files: b_device_image's read is this one."""
        (self.boot / "wk-image.id").write_text("wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1\n")
        cp = self._run(name="wk-image.id")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1", cp.stdout)

    def test_an_absent_file_is_nothing_and_not_an_error(self):
        """A partition holding no system is a different fact from a medium that
        cannot be read, and only the second is an error: b_systems counts the
        first as 'not a system' and walks on."""
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "")

    def test_the_firmware_and_kernel_inputs_are_readable(self):
        """A write's own appends land in these two, and nothing else could
        read them back: whether `os_check=0` had reached an rpi5 card was a
        hypothesis for two boots this answers in one line (2026-09-04)."""
        for name, text in (("config.txt", "[all]\nos_check=0\n"),
                           ("cmdline.txt", "root=PARTUUID=987478fd-02 rootwait\n")):
            with self.subTest(name=name):
                (self.boot / name).write_text(text)
                cp = self._run(name=name)
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn(text.splitlines()[-1], cp.stdout)

    def test_a_filename_off_the_allowlist_is_refused(self):
        (self.boot / "wk-image.id").write_text("x\n")
        for name in ("../../etc/shadow", "id_rsa", "wk-image.id.bak", ""):
            with self.subTest(name=name):
                cp = self._run(name=name)
                self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
                self.assertIn("REFUSED", cp.stderr)

    def test_a_partition_that_is_not_a_partition_number_is_refused(self):
        for p in ("0", "17", "1x", "-1", "1 ; rm -rf /", ""):
            with self.subTest(partition=p):
                cp = self._run(partition=p)
                self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
                self.assertIn("REFUSED", cp.stderr)

    def test_it_is_bounded(self):
        (self.boot / "wk-diag.txt").write_text("x" * 200000)
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertLessEqual(len(cp.stdout), 65536 + 200, "a card can hand back any amount of text")

    def test_an_already_mounted_partition_is_read_where_it_is(self):
        """An automounter usually has the card on a workstation with a desktop
        session, and `mount` refuses a second mountpoint for a device it
        already holds -- so mounting unconditionally turned a read into
        "could not mount /dev/sda1" (rpi5, 2026-09-04)."""
        body = re.search(r"(?ms)^v_boot_read\(\) \{.*?^\}", CARD_PRIV.read_text())
        self.assertIsNotNone(body, "v_boot_read is not defined")
        self.assertIn("_boot_read_at", body.group(0),
                      "boot-read mounts even when something already has the partition")
        probe = re.search(r"(?ms)^_boot_read_at\(\) \{.*?^\}", CARD_PRIV.read_text())
        self.assertIsNotNone(probe, "_boot_read_at is not defined")
        self.assertIn("/proc/mounts", probe.group(0),
                      "the mountpoint does not come from /proc/mounts")

    def test_the_mountpoint_is_never_the_callers(self):
        """Reading somewhere it did not choose is fine; reading somewhere it
        was told is a different and much larger grant."""
        body = re.search(r"(?ms)^v_boot_read\(\) \{.*?^\}", CARD_PRIV.read_text()).group(0)
        self.assertNotIn('"$4"', body)
        self.assertIn('_boot_read_at "$(part "$dev" "$2")"', body)

    def test_it_mounts_read_only(self):
        body = re.search(r"(?ms)^v_boot_read\(\) \{.*?^\}", CARD_PRIV.read_text())
        self.assertIsNotNone(body, "v_boot_read is not defined")
        self.assertIn("with_mount -r", body.group(0), "a read verb mounts the medium writable")


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


if __name__ == "__main__":
    unittest.main()
