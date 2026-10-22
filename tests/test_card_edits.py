"""admin/wk-card-priv's card edits, one case each, every function lifted out with sed and run against temp directories
standing in for the mounted partitions; and the units and join scripts a write puts on a card."""
import base64
import contextlib
import io
import os
import re
import tarfile
import subprocess
import sys
import unittest

from tests.bashlift import lift as _lift
from tests.support import REPO, WkTest, bash, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk.machine import Fake  # noqa: E402
from wk.sysimage import write  # noqa: E402

CARD_PRIV = REPO / "admin" / "wk-card-priv"

# Every verb that takes a device: each must reach the gate, which refuses a non-block device.
DEVICE_VERBS = ("parts", "root-spec", "retarget", "cmdline-append", "config-append", "boot-id", "units",
                "boot-check", "helper", "boot-read", "wifi-from-host", "wifi-joins", "tailnet-save",
                "tailnet-restore")


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


class TestTheBootPartitionEdits(CardEditTest):
    def edit(self, *funcs, call):
        return self.run_helper(_lift(CARD_PRIV, *funcs) + "\n" + call + "\n")

    def test_retarget_makes_root_the_card_s_own_partuuid_and_leaves_dev_root(self):
        (self.boot / "current").mkdir()
        (self.boot / "cmdline.txt").write_text("root=/dev/sda2\n")
        (self.boot / "current" / "cmdline.txt").write_text("console=serial0,115200 root=/dev/mmcblk0p2 rootwait\n")
        (self.root / "etc").mkdir()
        (self.root / "etc" / "fstab").write_text("# names /dev/mmcblk0p1\n/dev/root\t/\text4\tdefaults\t0\t1\n"
                                                 "/dev/mmcblk0p1\t/boot\tvfat\tdefaults\t0\t2\nproc\t/proc\tproc\tdefaults\t0\t0\n")
        with stub_path({"sfdisk": _SFDISK}) as binp:
            cp = self.run_helper(_lift(CARD_PRIV, "_table", "_boot_file", "_root_spec_probe", "_retarget_boot", "_retarget_fstab",
                                       "v_retarget") + "\nv_retarget /dev/sdX\n", path=binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "current" / "cmdline.txt").read_text(), "console=serial0,115200 root=PARTUUID=1c9dabbc-02 rootwait\n")
        self.assertEqual((self.boot / "cmdline.txt").read_text(), "root=/dev/sda2\n", "the file under os_prefix wins")
        self.assertEqual((self.root / "etc" / "fstab").read_text(), "# names /dev/mmcblk0p1\n/dev/root\t/\text4\tdefaults\t0\t1\n"
                         "PARTUUID=1c9dabbc-01\t/boot\tvfat\tdefaults\t0\t2\nproc\t/proc\tproc\tdefaults\t0\t0\n")

    def test_root_spec_reads_the_root_off_the_card(self):
        (self.boot / "cmdline.txt").write_text("console=tty1 root=PARTUUID=abc-02 rw\n")
        cp = self.edit("_boot_file", "_root_spec_probe", "v_root_spec", call="v_root_spec /dev/sdX")
        self.assertEqual(cp.stdout.strip(), "root=PARTUUID=abc-02")

    def test_cmdline_append_lands_once_on_the_one_line(self):
        (self.boot / "cmdline.txt").write_text("root=PARTUUID=abc-02 rootwait\n")
        b64 = base64.b64encode(b"quiet").decode()
        for _ in range(2):
            cp = self.edit("check_b64", "check_text", "_boot_file", "_cmdline_append_edit", "v_cmdline_append", call="v_cmdline_append /dev/sdX " + b64)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "cmdline.txt").read_text(), "root=PARTUUID=abc-02 rootwait quiet\n")

    def test_config_append_lands_one_banner_block(self):
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        block = base64.b64encode(b"# --- wk sysimage: p ---\n[all]\nos_check=0\n").decode()
        for _ in range(2):
            cp = self.edit("check_b64", "check_text", "_boot_file", "_config_append_edit", "v_config_append", call="v_config_append /dev/sdX " + block)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((self.boot / "config.txt").read_text().count("os_check=0"), 1)

    def test_boot_id_lands_and_a_non_name_is_refused(self):
        ok = self.edit("check_name", "_boot_id_edit", "v_boot_id", call="v_boot_id /dev/sdX webkit-2.52-yocto-rpi5-64-0123456789ab")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual((self.boot / "wk-image.id").read_text().strip(), "webkit-2.52-yocto-rpi5-64-0123456789ab")
        self.assertEqual(self.edit("check_name", "_boot_id_edit", "v_boot_id", call="v_boot_id /dev/sdX 'not; a name'").returncode, 3)

    def test_boot_check_names_what_the_firmware_cannot_find(self):
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        for name in ("start4.elf", "fixup4.dat", "bcm2711-rpi-4-b.dtb"):
            (self.boot / name).write_text("firmware")
        cp = self.run_helper("CHECK_BOOT_FILES=%s\n" % (REPO / "boot" / "check-boot-files.py")
                             + _lift(CARD_PRIV, "check_name", "_boot_check_run", "v_boot_check") + "\nv_boot_check /dev/sdX bcm2711-rpi-4-b.dtb\n")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("kernel", cp.stdout + cp.stderr)

    def test_boot_read_prints_an_allowlisted_file_read_only_and_bounded(self):
        (self.boot / "wk-diag.txt").write_text("x" * 200000)
        read_only = 'eval "rw_$(declare -f with_mount)"\nwith_mount() { [ "$1" = -r ] || return 9; rw_with_mount "$@"; }\n'
        lift = _lift(CARD_PRIV, "check_partno", "_boot_read_probe", "v_boot_read") + "\nBOOT_READ_MAX=65536\n" + read_only
        cp = self.run_helper(lift + "v_boot_read /dev/sdX 1 wk-diag.txt\n")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertLessEqual(len(cp.stdout), 65536 + 200)
        for part, name in (("1", "../../etc/shadow"), ("1 ; rm -rf /", "wk-diag.txt")):
            self.assertEqual(self.run_helper(lift + "v_boot_read /dev/sdX '%s' '%s'\n" % (part, name)).returncode, 3)


class TestTheRootEdits(CardEditTest):
    def test_units_land_wanted_a_timer_s_service_is_not_and_an_image_without_init_takes_none(self):
        work = self.tmp / "staged"
        for d, name, text in (("systemd", "wk-self-return.timer", "[Timer]\nOnBootSec=900\n[Install]\nWantedBy=timers.target\n"),
                              ("systemd", "wk-self-return.service", "[Service]\nExecStart=/bin/true\n"),
                              ("sysctl.d", "90-wk-perf.conf", "kernel.perf_event_paranoid = -1\n")):
            (work / d).mkdir(parents=True, exist_ok=True)
            (work / d / name).write_text(text)
        edit = _lift(CARD_PRIV, "_put", "_unit_target", "_units_sysctl", "_units_edit") + "\n_units_edit \"$ROOTDIR\" %s\n" % work
        cp = self.run_helper(edit)
        self.assertIn("neither systemd nor /etc/init.d", cp.stdout)
        self.assertFalse((self.root / "etc").exists())
        (self.root / "lib" / "systemd").mkdir(parents=True)
        (self.root / "lib" / "systemd" / "systemd").write_text("")
        cp = self.run_helper(edit)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        sysd = self.root / "etc" / "systemd" / "system"
        self.assertTrue((sysd / "timers.target.wants" / "wk-self-return.timer").is_symlink())
        self.assertFalse((sysd / "multi-user.target.wants" / "wk-self-return.service").exists(), "the service would reboot at boot")
        self.assertEqual((self.root / "etc" / "sysctl.d" / "90-wk-perf.conf").read_text(), "kernel.perf_event_paranoid = -1\n")

    def test_units_take_only_their_two_directories_and_no_symlink(self):
        tar, body = self.tmp / "units.tar", b"[Install]\nWantedBy=multi-user.target\n"
        with tarfile.open(tar, "w") as tf:
            info = tarfile.TarInfo("systemd/../../etc/passwd")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        self.assertEqual(bash(_SAY + _lift(CARD_PRIV, "_units_names") + "\n_units_names %s\n" % tar).returncode, 3)
        (self.tmp / "u" / "systemd").mkdir(parents=True)
        (self.tmp / "u" / "systemd" / "evil.service").symlink_to("/etc/shadow")
        self.assertEqual(bash(_SAY + _lift(CARD_PRIV, "_units_unpacked") + "\n_units_unpacked %s\n" % (self.tmp / "u")).returncode, 3)

    def test_helper_puts_itself_and_its_checker_on_the_card(self):
        (self.tmp / "helper").write_text("#!/bin/bash\n")
        (self.tmp / "checker.py").write_text("#!/usr/bin/env python3\n")
        cp = self.run_helper(_lift(CARD_PRIV, "_put", "_helper_install", "v_helper") + '\nSELF=%s\nCHECK_BOOT_FILES=%s\nv_helper /dev/sdX\n'
                             % (self.tmp / "helper", self.tmp / "checker.py"))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name in ("wk-card-priv", "wk-check-boot-files.py"):
            self.assertTrue(os.access(self.root / "usr" / "local" / "libexec" / name, os.X_OK), name)

    def test_grow_and_eject_report_their_failures(self):
        def run(verb, **rc):
            with stub_path({t: "exit %d\n" % rc.get(t, 0) for t in ("sfdisk", "partx", "e2fsck", "resize2fs", "blockdev", "sync")}) as binp:
                return self.run_helper(_lift(CARD_PRIV, verb) + "\n%s /dev/sdX\n" % verb, path=binp)
        self.assertEqual([run("v_grow", e2fsck=n).returncode for n in (1, 4)], [0, 1])
        cp = run("v_eject", blockdev=1)
        self.assertEqual(cp.returncode, 1)
        self.assertNotIn("flushed", cp.stdout)

    def test_the_wifi_conf_lands_0600_and_reads_back_and_what_would_break_it_is_refused(self):
        def edit(ssid, psk):
            return bash(_SAY + _lift(CARD_PRIV, "check_wifi_value", "_wifi_edit") + "\nstat() { echo 600; }\n_wifi_edit %s %r %r\n"
                        % (self.root, ssid, psk))
        self.assertEqual(edit("My Net", "hunter2 pass").returncode, 0)
        conf = self.root / "etc" / "wpa_supplicant" / "wpa_supplicant-wlan0.conf"
        self.assertIn('network={\n    ssid="My Net"\n    psk="hunter2 pass"\n}', conf.read_text())
        cp = bash(_lift(CARD_PRIV, "_wpa_conf_wifi") + '\nout=$(_wpa_conf_wifi %s) || exit $?\ncat "$out"\n' % conf,
                  env={**os.environ, "TMPDIR": str(self.tmp)})
        self.assertEqual(cp.stdout, "ssid=My Net\npsk=hunter2 pass\n")
        check = _SAY + _lift(CARD_PRIV, "check_wifi_value") + '\ncheck_wifi_value "the SSID" "$1"\n'
        for value, rc in (("Plain Net", 0), ('bad"ssid', 3), ("bad\\psk", 3), ("", 3)):
            self.assertEqual(subprocess.run(["bash", "-c", check, "_", value], capture_output=True).returncode, rc, value)

    @unittest.skipUnless(subprocess.run(["python3", "-c", "import yaml"], capture_output=True).returncode == 0, "no python3 yaml here")
    def test_the_host_s_netplan_credential_is_found_and_wifi_host_never_says_the_passphrase(self):
        parser = re.search(r"python3 -c '\n(.*?)\n' > \"\$tmp\"", CARD_PRIV.read_text(), re.S).group(1)
        nm = 'network:\n  wifis:\n    NM-u:\n      access-points:\n        "NMNet":\n          auth:\n            password: "s3cret!"\n'
        cp = subprocess.run(["python3", "-c", parser], input=nm, capture_output=True, text=True)
        self.assertEqual((cp.returncode, cp.stdout), (0, "ssid=NMNet\npsk=s3cret!\n"))
        self.assertEqual(subprocess.run(["python3", "-c", parser], input="", capture_output=True).returncode, 3)
        stub = '_host_wifi() { tmp=$(mktemp); printf "ssid=MyNet\\npsk=hunter2\\n" > "$tmp"; printf "%s" "$tmp"; }'
        cp = bash(_SAY + stub + "\n" + _lift(CARD_PRIV, "v_wifi_host") + "\nv_wifi_host\n")
        self.assertIn("wifi-host: yes ssid=MyNet", cp.stdout)
        self.assertNotIn("hunter2", cp.stdout + cp.stderr)


class TestEveryDeviceVerbIsGated(WkTest):
    def test_a_device_that_is_not_a_block_device_is_refused_before_any_edit(self):
        script = self.tmp / "helper"
        script.write_text(re.sub(r'(?m)^\[ "\$\(id -u\)" -eq 0 \].*$', ":", CARD_PRIV.read_text()))
        for verb in DEVICE_VERBS:
            with self.subTest(verb=verb):
                cp = subprocess.run(["bash", str(script), verb, "/dev/null", "x", "y"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
                self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
                self.assertIn("not a block device", cp.stderr)


class TestTheUnitsAWriteStages(unittest.TestCase):
    def staged(self, watchdog="600", machine=""):
        w = write.Write(REPO, {"HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent"}, Fake(), None)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            return write.stage_units(REPO, watchdog, w.self_disarm(machine), "p"), err.getvalue(), w

    def test_the_watchdog_is_a_timer_blocking_no_target_and_its_service_is_wanted_by_nothing(self):
        """A sleeping oneshot holds multi-user.target inactive for the whole wait (rpi4, measured)."""
        units, _, w = self.staged()
        timer, svc = units["systemd/wk-self-return.timer"], units["systemd/wk-self-return.service"]
        self.assertTrue(timer.endswith("[Timer]\nAccuracySec=1s\nOnBootSec=600\n"), timer)
        self.assertTrue(all("/etc/wk/rescue" in u for u in (timer, svc)))
        self.assertNotIn("[Install]", svc)
        self.assertNotIn("systemd/wk-self-disarm.service", units, "nothing to park")
        self.assertEqual([w.self_disarm(m) for m in ("nosuchmachine", "")], ["", ""])
        units, err, _ = self.staged(watchdog="")
        self.assertFalse([u for u in units if "wk-self-return" in u])
        self.assertIn("will not hand its machine back", err)

    def test_a_busybox_image_gets_the_same_two_jobs_as_init_scripts_their_sleep_in_the_background(self):
        units, _, w = self.staged(watchdog="2", machine="rpi3")
        self.assertIn("tryboot.txt", w.self_disarm("rpi4"))
        disarm, ret = units["init.d/S11wk-self-disarm"], units["init.d/S99wk-self-return"]
        for script in (disarm, ret):
            self.assertIn("/etc/wk/rescue", script)
            self.assertEqual(subprocess.run(["sh", "-n"], input=script, text=True).returncode, 0)
        self.assertIn("config.txt.rescue", disarm)
        cp = subprocess.run(["sh", "-c", ret.replace("/etc/wk/rescue", "/nonexistent").replace("reboot", "true"), "S99", "start"],
                            capture_output=True, timeout=1)
        self.assertEqual(cp.returncode, 0)


class TestTheJoinScripts(WkTest):
    """What a card's first boot runs: each says what is missing and how it is put there."""

    def test_the_tailnet_join_names_a_missing_key_and_an_already_joined_node_is_no_failure(self):
        text = (REPO / "image/yocto/meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join").read_text()
        ts = self.tmp / "tailscale"
        script = self.tmp / "join"
        script.write_text(text.replace("KEY=/etc/wk/tailscale-authkey", "KEY=%s/none" % self.tmp)
                          .replace("CONF=/etc/wk/tailnet.conf", "CONF=%s/none.conf" % self.tmp).replace("TS=/usr/bin/tailscale", "TS=%s" % ts))
        for body, rc in (("exit 1", 1), ('[ "$1" = ip ] && echo 100.1.1.1; exit 0', 0)):
            ts.write_text("#!/bin/sh\n%s\n" % body)
            ts.chmod(0o755)
            cp = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10)
            self.assertEqual(cp.returncode != 0, bool(rc), cp.stdout + cp.stderr)

    def test_the_wifi_join_names_a_missing_conf(self):
        script = self.tmp / "wifi"
        script.write_text((REPO / "image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join").read_text()
                          .replace("CONF=/etc/wpa_supplicant/wpa_supplicant-wlan0.conf", "CONF=%s/none.conf" % self.tmp))
        cp = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(1, cp.returncode)
        self.assertIn("wk sysimage write", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
