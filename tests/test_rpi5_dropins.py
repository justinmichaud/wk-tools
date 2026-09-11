"""The rpi5's two hand fixes, now shipped by rpi5-setup.sh instead of living
only on the box.

Both were applied by hand on the running rpi5 and nowhere in the repo:
(1) a NetworkManager drop-in telling NM to leave tailscale0 unmanaged --
without it NM tears the interface down on a reload and MagicDNS breaks --
and (2) the WiFi regulatory domain (CA), pinned by a oneshot service because
the kernel restores the world domain (00, 5 GHz no-IR) on every disconnect.

This is a source-level test: it extracts the heredoc rpi5-setup.sh installs
each file from and compares it against the content captured on the rpi5
itself (2026-09-10), rather than running the script against a real board.

Run: python3 -m unittest tests.test_rpi5_dropins -v
"""
import re
import subprocess
import unittest

from tests.support import REPO

SCRIPT = REPO / "host" / "linux" / "rpi5" / "rpi5-setup.sh"
TEXT = SCRIPT.read_text()

# Captured with: ssh rpi5 'cat <file>' -- the fixes as they exist on the board
# today, hand-applied and nowhere else in the repo before this change.
TAILSCALE_CONF = """\
# Keep NetworkManager away from the Tailscale tun.
#
# tailscale0 is created and configured by tailscaled, but NM sees it as an
# externally-managed device and tears it down on `nmcli networking off` and
# on an NM restart -- flushing the 100.64/10 address. tailscaled stays up
# and logged in but never re-adds it, so MagicDNS (100.100.100.100) and
# every tailnet peer fall through to the default route and fail to resolve.
[keyfile]
unmanaged-devices=interface-name:tailscale0
"""

CFG80211_CONF = """\
# Set the regulatory domain at module load, before any association.
# wireless-regdom.service installs the matching USER hint that survives
# the regdomain restore the kernel performs on every disconnect.
options cfg80211 ieee80211_regdom=CA
"""

REGDOM_SERVICE = """\
[Unit]
Description=Pin the 802.11 regulatory domain
Documentation=man:iw(8)
After=sys-subsystem-net-devices-wlan0.device
Wants=sys-subsystem-net-devices-wlan0.device
Before=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
# A USER hint is the only one restore_regulatory_settings() re-applies when
# the kernel drops a country-IE regdomain on disconnect. Without it the
# domain falls back to world (00), where 5170-5250 MHz is no-IR and the
# card can hear channel 36 but never transmit on it.
ExecStart=/usr/sbin/iw reg set CA

[Install]
WantedBy=multi-user.target
"""


def heredoc(marker):
    """The body of `<<marker ... marker` (quoted or not) in rpi5-setup.sh."""
    m = re.search(rf"<<'?{marker}'?\n(.*?)\n{marker}\n", TEXT, re.S)
    assert m, f"no <<{marker} heredoc in {SCRIPT}"
    return m.group(1) + "\n"


class TestScriptParses(unittest.TestCase):
    def test_bash_n(self):
        cp = subprocess.run(["bash", "-n", str(SCRIPT)],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(0, cp.returncode, cp.stderr)


class TestTailscaleUnmanaged(unittest.TestCase):
    def test_shipped_content_equals_what_is_installed_on_the_board(self):
        self.assertEqual(TAILSCALE_CONF, heredoc("TSCONF"))

    def test_installed_idempotently_where_the_wifi_drop_in_is(self):
        # Same shape as the wifi-powersave-off.conf install just above it:
        # `install -d` the directory, `tee` the file, no error on a re-run.
        self.assertIn("sudo install -d /etc/NetworkManager/conf.d", TEXT)
        self.assertIn(
            "sudo tee /etc/NetworkManager/conf.d/99-tailscale-unmanaged.conf",
            TEXT)

    def test_nm_is_reloaded_and_tailscaled_restarted(self):
        body = re.search(r'(?s)log "4d.*?(?=\nlog "4e)', TEXT).group(0)
        self.assertIn("systemctl reload NetworkManager", body)
        self.assertIn("systemctl restart tailscaled", body)


class TestWifiRegulatoryDomain(unittest.TestCase):
    def test_the_country_is_one_declared_variable(self):
        self.assertIn('WIFI_REGDOM="${WIFI_REGDOM:-CA}"', TEXT)
        # Neither generated file hard-codes CA a second time.
        self.assertIn("ieee80211_regdom=$WIFI_REGDOM", TEXT)
        self.assertIn("iw reg set $WIFI_REGDOM", TEXT)

    def test_shipped_cfg80211_conf_equals_the_boards_default(self):
        self.assertEqual(CFG80211_CONF,
                         heredoc("CFGEOF").replace("$WIFI_REGDOM", "CA"))

    def test_shipped_service_equals_the_boards_default(self):
        self.assertEqual(REGDOM_SERVICE,
                         heredoc("REGDOMEOF").replace("$WIFI_REGDOM", "CA"))

    def test_service_runs_before_networkmanager(self):
        self.assertIn("Before=NetworkManager.service", heredoc("REGDOMEOF"))

    def test_service_is_enabled(self):
        body = re.search(r'(?s)log "4e.*', TEXT).group(0)
        self.assertIn("systemctl daemon-reload", body)
        self.assertIn("systemctl enable --now wireless-regdom.service", body)


class TestSetupReachesIt(unittest.TestCase):
    """host/linux/machine.sh, which every `./setup` run sources, is the one
    entry point -- a board is detected by a hardware fact
    (/proc/device-tree/model), never by a case naming a hostname."""

    MACHINE = (REPO / "host" / "linux" / "machine.sh").read_text()

    def test_detected_by_device_tree_model_not_by_hostname(self):
        self.assertIn("/proc/device-tree/model", self.MACHINE)
        self.assertIn("Raspberry Pi 5", self.MACHINE)
        self.assertNotRegex(self.MACHINE, r'"\$\(hostname\)"\s*=')

    def test_it_runs_the_one_script(self):
        self.assertIn(
            'bash "$WK_ROOT/host/linux/rpi5/rpi5-setup.sh"', self.MACHINE)

    def test_it_runs_in_the_foreground_and_keeps_the_report(self):
        """It asks sudo for a password: captured in a $(...), the prompt stands
        there with none of the steps that explain it, and `./setup` looks hung.
        The report is teed to a log as well, which is what the change is read
        from afterwards."""
        self.assertNotIn('_rpi5_out=$(bash', self.MACHINE)
        self.assertRegex(self.MACHINE, r'bash "\$WK_ROOT/host/linux/rpi5/rpi5-setup\.sh" 2>&1 \| tee ')

    def test_a_change_is_read_from_the_lines_it_prints_only_when_it_writes(self):
        """Every ✓ the script prints says the step is in the state it wants,
        including on a run that wrote nothing -- so `grep ✓` claims a change
        every time. `added:` (ensure_pi5_line) and `appended tuning block` are
        its two gated ones, printed only after it writes."""
        self.assertIn("grep -qE '(added:|appended tuning block)'", self.MACHINE)
        self.assertNotIn("grep -q '✓'", self.MACHINE)
        for gated in ('ok "added: $1"', 'ok "appended tuning block"'):
            self.assertIn(gated, TEXT, "the line the change is read from moved")

    def test_no_second_entry_point(self):
        """Nothing else in host/linux invokes rpi5-setup.sh -- one path in."""
        for f in (REPO / "host" / "linux").glob("*.sh"):
            if f.name == "machine.sh":
                continue
            with self.subTest(file=f.name):
                self.assertNotIn("rpi5-setup.sh", f.read_text())
        self.assertNotIn("rpi5-setup.sh", (REPO / "setup").read_text())


if __name__ == "__main__":
    unittest.main()
