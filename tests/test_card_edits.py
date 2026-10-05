"""admin/wk-card-priv's card edits, each function lifted out with sed and run against temp directories standing
in for the mounted partitions, plus `wk sysimage write`'s dry run of the whole sequence."""
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

# Every device verb and the function that implements it.
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
    "wifi-from-host": "v_wifi_from_host",
    "wifi-joins": "v_wifi_joins",
    "tailnet-save": "v_tailnet_save",
    "tailnet-restore": "v_tailnet_restore",
}


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
'''

# The gate and the mount, replaced by two directories: partition 1 is boot, 2 the rootfs.
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
    def retarget(self, sfdisk=_SFDISK):
        with stub_path({"sfdisk": sfdisk}) as binp:
            return self.run_helper(
                _lift(CARD_PRIV, "_table", "_boot_file", "_root_spec_probe", "_retarget_boot",
                      "_retarget_fstab", "v_retarget") + "\nv_retarget /dev/sdX\n",
                path=binp)

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
        cp = self.retarget()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        cmdline = (self.boot / "cmdline.txt").read_text()
        self.assertIn("root=PARTUUID=1c9dabbc-02", cmdline, cmdline)
        self.assertNotIn("/dev/mmcblk0p2", cmdline, cmdline)
        self.assertIn("console=serial0,115200", cmdline)
        self.assertIn("rootwait", cmdline)
        fstab = (self.root / "etc" / "fstab").read_text()
        self.assertIn("PARTUUID=1c9dabbc-01\t/boot", fstab, fstab)
        self.assertIn("PARTUUID=1c9dabbc-02\t/", fstab, fstab)
        self.assertIn("proc\t/proc", fstab, fstab)
        self.assertIn("# a comment naming /dev/mmcblk0p1", fstab, fstab)

    def test_a_card_already_booting_by_partuuid_is_refused_naming_re_provisioning(self):
        (self.boot / "cmdline.txt").write_text("root=PARTUUID=953569f6-02 rootwait\n")
        (self.root / "etc").mkdir()
        (self.root / "etc" / "fstab").write_text("PARTUUID=953569f6-01\t/boot\tvfat\tdefaults\t0\t2\n")
        cp = self.retarget()
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("wk status", cp.stderr)
        self.assertIn("953569f6-02", (self.boot / "cmdline.txt").read_text())
        self.assertIn("953569f6-01", (self.root / "etc" / "fstab").read_text())

    def test_dev_root_is_left_alone(self):
        (self.boot / "cmdline.txt").write_text("root=/dev/mmcblk0p2 rootwait\n")
        (self.root / "etc").mkdir()
        (self.root / "etc" / "fstab").write_text(
            "/dev/root\t/\text4\tdefaults\t0\t1\n"
            "PARTUUID=953569f6-01\t/boot\tvfat\tdefaults\t0\t2\n")
        cp = self.retarget()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        fstab = (self.root / "etc" / "fstab").read_text()
        self.assertIn("/dev/root\t/", fstab, "the kernel's own placeholder was rewritten")
        self.assertIn("PARTUUID=1c9dabbc-01\t/boot", fstab, fstab)
        self.assertIn("fstab: PARTUUID=953569f6-01 -> PARTUUID=1c9dabbc-01", cp.stdout, cp.stdout)

    def test_a_disk_with_no_partition_table_is_refused(self):
        self._write_card()
        cp = self.retarget(_SFDISK_NO_TABLE)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("REFUSED", cp.stdout + cp.stderr)

    def test_the_boot_file_under_os_prefix_wins(self):
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

    def _edit(self, work, systemd=True):
        if systemd:
            (self.root / "lib" / "systemd").mkdir(parents=True)
            (self.root / "lib" / "systemd" / "systemd").write_text("")
        return self.run_helper(
            _lift(CARD_PRIV, "_unit_target", "_units_sysctl", "_units_edit")
            + f"\n_units_edit \"$ROOTDIR\" {work}\n")

    def _staged(self):
        work = self.tmp / "staged"
        (work / "systemd").mkdir(parents=True)
        (work / "sysctl.d").mkdir(parents=True)
        (work / "systemd" / "wk-self-return.service").write_text(self.UNIT)
        (work / "sysctl.d" / "90-wk-perf.conf").write_text("kernel.perf_event_paranoid = -1\n")
        return work

    def test_units_land_under_etc_systemd_system_and_are_wanted(self):
        work = self._staged()
        cp = self._edit(work)
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
        work = self._staged()
        (work / "systemd" / "wk-self-return.timer").write_text(
            "[Unit]\nDescription=x\n[Timer]\nOnBootSec=900\n"
            "[Install]\nWantedBy=timers.target\n")
        (work / "systemd" / "wk-self-return.service").write_text(
            "[Unit]\nDescription=x\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
        cp = self._edit(work)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        sysd = self.root / "etc" / "systemd" / "system"
        self.assertTrue((sysd / "wk-self-return.timer").is_file())
        self.assertTrue((sysd / "wk-self-return.service").is_file())
        self.assertTrue((sysd / "timers.target.wants" / "wk-self-return.timer").is_symlink(),
                        "the timer is not wanted by timers.target")
        self.assertFalse((sysd / "multi-user.target.wants" / "wk-self-return.service").exists(),
                         "the timer's service is ALSO started at boot, which reboots the board")

    def test_a_service_with_no_wantedby_and_no_timer_is_still_refused(self):
        work = self._staged()
        (work / "systemd" / "wk-orphan.service").write_text(
            "[Unit]\n[Service]\nExecStart=/bin/true\n")
        cp = self._edit(work)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WantedBy", cp.stdout + cp.stderr)

    def test_a_unit_with_no_wantedby_is_refused(self):
        work = self._staged()
        (work / "systemd" / "wk-self-return.service").write_text("[Unit]\n[Service]\n")
        cp = self._edit(work)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WantedBy", cp.stdout + cp.stderr)

    def test_an_image_without_any_init_takes_nothing_and_says_so(self):
        work = self._staged()
        cp = self._edit(work, systemd=False)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("neither systemd nor /etc/init.d", cp.stdout)
        self.assertFalse((self.root / "etc").exists(), "something was installed anyway")

    def _names(self, members):
        """The member list checked as v_units checks it; tarfile spells a traversal exactly."""
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

    def test_the_member_list_takes_only_the_two_directories(self):
        for members, rc in ((["systemd/wk-self-return.service", "systemd/wk-self-return.timer"], 0),
                            (["systemd/../../etc/passwd"], 3), (["etc/shadow"], 3)):
            with self.subTest(members=members):
                cp = self._names(members)
                self.assertEqual(cp.returncode, rc, cp.stdout + cp.stderr)

    def test_an_unpacked_symlink_is_refused(self):
        work = self.tmp / "unpacked"
        (work / "systemd").mkdir(parents=True)
        (work / "systemd" / "evil.service").symlink_to("/etc/shadow")
        cp = bash(_SAY + _lift(CARD_PRIV, "_units_unpacked") + f"\n_units_unpacked {work}\n")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("symlink", cp.stdout + cp.stderr)


class TestBootCheck(CardEditTest):
    PI4 = ("start4.elf", "fixup4.dat", "kernel8.img", "bcm2711-rpi-4-b.dtb")
    PI5 = ("start4.elf", "fixup4.dat", "kernel_2712.img", "bcm2712-rpi-5-b.dtb")

    def _boot_tree(self, missing=(), files=PI4):
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        for name in files:
            if name not in missing:
                (self.boot / name).write_text("firmware")

    def _run(self, checker=None, dtb="bcm2711-rpi-4-b.dtb"):
        checker = checker or (REPO / "boot" / "check-boot-files.py")
        return self.run_helper(
            f'CHECK_BOOT_FILES={checker}\n'
            + _lift(CARD_PRIV, "check_name", "_boot_check_run", "v_boot_check")
            + f"\nv_boot_check /dev/sdX {dtb}\n")

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

    def test_a_pi5_tree_is_checked_with_its_own_kernel_name(self):
        self._boot_tree(files=self.PI5)
        cp = self._run(dtb="bcm2712-rpi-5-b.dtb")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        (self.boot / "kernel_2712.img").unlink()
        cp = self._run(dtb="bcm2712-rpi-5-b.dtb")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("kernel", cp.stdout + cp.stderr)

    def test_a_missing_checker_refuses_loudly_and_names_the_remedy(self):
        self._boot_tree()
        cp = self._run(checker="/nonexistent/wk-check-boot-files.py")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("no boot-file checker", out)
        self.assertIn("./setup --stage quiesce", out)


class TestHelperShape(unittest.TestCase):
    def test_every_device_verb_calls_the_gate(self):
        text = CARD_PRIV.read_text(errors="replace")
        for verb, fn in NEW_VERBS.items():
            m = re.search(rf"(?ms)^{fn}\(\) \{{.*?^\}}", text)
            self.assertTrue(m and "gate " in m.group(0), f"{fn} ({verb}) does not call gate")

    def test_the_reader_hands_over_the_builders_own_bytes(self):
        w = write.Write(REPO, {}, Fake(), None)
        w.machine.files["/x.wic.xz"] = "x"
        self.assertEqual(w.reader("/x.wic.xz"), ["cat", "/x.wic.xz"])


class TestDryRunIsTheSameSteps(unittest.TestCase):

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
                    w.conf, w.ch = {"name": "testmach"}, self.Refuse()
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        getattr(w, name)(*args)
                    self.assertRegex(err.getvalue(), r"(?m)^\s*would ")
                    self.assertEqual(len(w.plan), 1)
                    self.assertEqual(w.machine.effects, [])


class TestWriteDryRunIsTheWholeSequence(WkTest):

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


class TestTheUnitsAreTheImageMachines(unittest.TestCase):

    def setUp(self):
        self.w = write.Write(REPO, {"HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent"}, Fake(), None)

    def staged(self, watchdog="600", disarm=""):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            return write.stage_units(REPO, watchdog, disarm, "test-profile"), err.getvalue()

    def test_a_medium_armed_board_gets_its_drivers_self_disarm(self):
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
        """A sleeping oneshot holds multi-user.target inactive for the whole wait (rpi4, measured)."""
        units, _ = self.staged()
        timer = units["systemd/wk-self-return.timer"]
        self.assertTrue(timer.endswith("[Timer]\nAccuracySec=1s\nOnBootSec=600\n"), timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("/etc/wk/rescue", timer, "the timer is not gated on the rescue marker")
        svc = units["systemd/wk-self-return.service"]
        self.assertNotIn("sleep", svc)
        self.assertNotIn("[Install]", svc, "a service also wanted by a target reboots the board at boot")
        self.assertIn("wk-keep-running", svc)
        self.assertIn("/etc/wk/rescue", svc, "the service is not gated on the rescue marker")

    def test_no_watchdog_seconds_stages_neither_half(self):
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
        units, _ = self.staged(watchdog="2")
        cp = subprocess.run(["sh", "-c", units["init.d/S99wk-self-return"].replace("/etc/wk/rescue", "/nonexistent")
                             .replace("reboot", "true"), "S99", "start"], capture_output=True, timeout=1)
        self.assertEqual(cp.returncode, 0)


class TestBootRead(CardEditTest):
    # A writable mount fails here, so every read below also asserts boot-read mounts read-only.
    READ_ONLY = 'eval "rw_$(declare -f with_mount)"\nwith_mount() { [ "$1" = -r ] || return 9; rw_with_mount "$@"; }\n'

    def _run(self, partition="1", name="wk-diag.txt"):
        return self.run_helper(
            _lift(CARD_PRIV, "check_partno", "_boot_read_probe", "v_boot_read")
            + "\nBOOT_READ_MAX=65536\n" + self.READ_ONLY
            + f"v_boot_read /dev/sdX '{partition}' '{name}'\n")

    def test_it_prints_the_file_the_image_wrote(self):
        (self.boot / "wk-diag.txt").write_text("id=some-image\nwlan0: no carrier\n")
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wlan0: no carrier", cp.stdout)

    def test_the_system_id_is_read_the_same_way(self):
        (self.boot / "wk-image.id").write_text("wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1\n")
        cp = self._run(name="wk-image.id")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1", cp.stdout)

    def test_an_absent_file_is_nothing_and_not_an_error(self):
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "")

    def test_the_firmware_and_kernel_inputs_are_readable(self):
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

class TestRescueHelper(CardEditTest):

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
        cp = self._run(extra="SECOND=1")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((self.root / "usr" / "local" / "libexec" / "wk-card-priv").is_file())

    def test_no_checker_on_this_machine_is_a_refusal_with_a_remedy(self):
        (self.tmp / "checker.py").unlink()
        cp = self._run()
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("--stage quiesce", cp.stderr)
        self.assertFalse((self.root / "usr").exists(), "nothing is written on a refusal")


class TestGrowAndEjectReportTheirFailures(CardEditTest):

    def tools(self, **rc):
        return {t: "exit %d\n" % rc.get(t, 0) for t in ("sfdisk", "partx", "e2fsck", "resize2fs", "blockdev", "sync")}

    def grow(self, **rc):
        with stub_path(self.tools(**rc)) as binp:
            return self.run_helper(_lift(CARD_PRIV, "v_grow") + "\nv_grow /dev/sdX\n", path=binp)

    def eject(self, **rc):
        with stub_path(self.tools(**rc)) as binp:
            return self.run_helper(_lift(CARD_PRIV, "v_eject") + "\nv_eject /dev/sdX\n", path=binp)

    def test_a_clean_or_corrected_filesystem_is_grown(self):
        for code in (0, 1):
            with self.subTest(e2fsck=code):
                cp = self.grow(e2fsck=code)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("grown to fill /dev/sdX", cp.stdout)

    def test_an_uncorrected_or_failed_check_is_a_failure_naming_e2fsck(self):
        for code in (4, 8):
            with self.subTest(e2fsck=code):
                cp = self.grow(e2fsck=code)
                self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("e2fsck", cp.stderr)
                self.assertIn("exit %d" % code, cp.stderr)
                self.assertNotIn("grown", cp.stdout)

    def test_a_table_the_kernel_was_not_told_about_is_a_failure_naming_partx(self):
        cp = self.grow(partx=1)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("partx", cp.stderr)
        self.assertNotIn("grown", cp.stdout)

    def test_a_flushed_card_says_so(self):
        cp = self.eject()
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("/dev/sdX flushed", cp.stdout)

    def test_a_failed_flush_is_a_failure_and_never_claims_flushed(self):
        cp = self.eject(blockdev=1)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("blockdev --flushbufs", cp.stderr)
        self.assertNotIn("flushed", cp.stdout)


if __name__ == "__main__":
    unittest.main()
