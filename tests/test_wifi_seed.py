"""WiFi credential seeding: rpi3/rpi4/rpi5 have no cable at the bench, so
their rescue and bench images bring up WiFi from a credential `wk sysimage
write` seeds onto the card -- the same shape as the tailnet auth key
(disk_seed_tailnet, boot/disk.sh), one seed further. There is no hand-made
credential file: the credential is this machine's own WiFi connection,
extracted at write time by the privileged card helper (which already runs as
root on the machine holding the reader) from that machine's netplan --
ordinary `sudo -n` is not passwordless there, only the helper is. See
boot/disk.sh (disk_seed_wifi, _image_wants_wifi), admin/wk-card-priv
(v_wifi_host, v_wifi_from_host, v_wifi_joins, _netplan_wifi,
check_wifi_value, _wifi_edit, the embedded netplan
parser, standalone and testable) and cmd/sysimage (_wifi_creds_preflight).

Run: python3 -m unittest tests.test_wifi_seed -v
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, requires_machine, run


CARD_PRIV = REPO / "admin" / "wk-card-priv"


def _netplan_parser_source():
    """The python `_netplan_wifi` (admin/wk-card-priv) feeds to python3 -c,
    lifted from the helper's text so the exact code that runs as root is
    what is tested -- there is no second copy of it anywhere."""
    text = CARD_PRIV.read_text(errors="replace")
    m = re.search(r"python3 -c '\n(.*?)\n' > \"\$tmp\"", text, re.S)
    assert m, "no embedded netplan parser in admin/wk-card-priv"
    return m.group(1)


def _lift(path, func):
    """A function's body, sed'd out of a shell file the way tests/test_quick.py
    lifts _ls_classify, _reaches and friends -- so it can be called directly
    without sourcing the whole file (some of these files require root, or
    ssh, at the top)."""
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    return text


class TestCardHelperWifiGate(WkTest):
    def test_wifi_from_host_verb_refuses_a_non_gated_device_not_a_crash(self):
        """the helper's wifi-from-host verb refuses a non-gated device, not a crash"""
        # admin/wk-card-priv is installed root:0755 by admin/install.sh; the
        # checked-in copy is not marked executable, so this runs it through
        # bash directly -- the same thing `sudo -n <path>` does on the far
        # end, minus the sudo this machine (and this test) is not root on.
        cp = subprocess.run(
            ["bash", str(CARD_PRIV), "wifi-from-host", "/dev/null"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(cp.returncode, 0, "a non-root, non-gated call to 'wifi-from-host' succeeded")
        out = cp.stdout + cp.stderr
        # A controlled refusal names itself; a crash is a bash traceback
        # (unbound variable, "command not found", a bare stack of line
        # numbers) with no "wk-card-priv:" prefix at all.
        self.assertIn("wk-card-priv:", out, f"no refusal message at all: {out}")
        self.assertNotIn("Traceback", out)

    def test_gate_denies_a_non_block_device_without_reading_stdin(self):
        """the device gate itself denies /dev/null, before anything touches stdin"""
        script = f'''
deny() {{ printf 'wk-card-priv: REFUSED: %s\\n' "$*" >&2; exit 3; }}
{_lift(CARD_PRIV, "booted_disks")}
{_lift(CARD_PRIV, "gate")}
gate /dev/null
'''
        cp = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            timeout=10, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("not a block device", cp.stdout + cp.stderr)

    def test_wifi_from_host_and_wifi_joins_call_the_gate(self):
        """v_wifi_from_host and v_wifi_joins call gate, like every other device verb"""
        # static, the same shape as test_quick.py's test_card_helper_gate
        text = CARD_PRIV.read_text(errors="replace")
        bad = []
        for v in ("v_wifi_from_host", "v_wifi_joins"):
            m = re.search(rf"(?ms)^{v}\(\) \{{.*?^\}}", text)
            if not m or "gate " not in m.group(0):
                bad.append(f"{v} does not call gate")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_wifi_host_takes_no_device_and_does_not_gate(self):
        """wifi-host is a read-only probe of this machine, not a card -- no gate"""
        text = CARD_PRIV.read_text(errors="replace")
        m = re.search(r"(?ms)^v_wifi_host\(\) \{.*?^\}", text)
        self.assertIsNotNone(m, "v_wifi_host not found")
        self.assertNotIn("gate ", m.group(0), "v_wifi_host gates a device it never touches")

    def test_wifi_verbs_are_in_the_dispatcher_and_usage(self):
        """wifi-host, wifi-from-host and wifi-joins are dispatched, and named in the usage line"""
        text = CARD_PRIV.read_text(errors="replace")
        case_m = re.search(r'(?ms)^case "\$verb" in.*?^esac', text)
        self.assertIsNotNone(case_m, "no verb dispatcher found")
        body = case_m.group(0)
        self.assertRegex(body, r"wifi-joins\)\s*v_wifi_joins")
        self.assertRegex(body, r"wifi-host\)\s*v_wifi_host")
        self.assertRegex(body, r"wifi-from-host\)\s*v_wifi_from_host")
        self.assertNotRegex(body, r"\bwifi\)\s*v_wifi\b",
                             "the old stdin-credentials 'wifi' verb is still dispatched")
        for name in ("wifi-joins", "wifi-host", "wifi-from-host"):
            self.assertIn(name, text, f"the usage line does not name {name}")


class TestWifiHostVerb(unittest.TestCase):
    """v_wifi_host's output shape: it names the SSID and never the PSK."""

    def _wifi_host(self, found, ssid="", psk=""):
        # printf's FORMAT argument is what interprets \n; ssid/psk arrive as
        # %s substitutions so a value cannot inject a second line.
        stub = (
            f'_host_wifi() {{ tmp=$(mktemp); printf "ssid=%s\\npsk=%s\\n" {ssid!r} {psk!r} > "$tmp"; printf "%s" "$tmp"; }}'
            if found else '_host_wifi() { return 3; }'
        )
        script = f'''
say() {{ printf 'wk-card-priv: %s\\n' "$*"; }}
{stub}
{_lift(CARD_PRIV, "v_wifi_host")}
v_wifi_host
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)

    def test_yes_shape_names_the_ssid_never_the_psk(self):
        cp = self._wifi_host(True, ssid="MyNet", psk="hunter2")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wifi-host: yes ssid=MyNet", cp.stdout)
        self.assertNotIn("hunter2", cp.stdout + cp.stderr, "the passphrase leaked into wifi-host's output")

    def test_no_shape(self):
        cp = self._wifi_host(False)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wifi-host: no", cp.stdout)


def _have_python_yaml():
    return subprocess.run(
        ["python3", "-c", "import yaml"], capture_output=True, timeout=10,
    ).returncode == 0


@unittest.skipUnless(_have_python_yaml(), "no python3 yaml module on this machine")
class TestNetplanWifiParser(unittest.TestCase):
    """admin/wk-card-priv's embedded netplan parser against synthetic
    netplan yaml. Skipped, not faked, when this machine's python3 has no
    yaml module (macOS): the helper only ever runs on a Linux machine, where
    netplan itself depends on python3-yaml."""

    def _run(self, yaml_text):
        return subprocess.run(
            ["python3", "-c", _netplan_parser_source()],
            input=yaml_text, capture_output=True, text=True, timeout=10,
        )

    def test_finds_a_bare_password(self):
        yaml_text = '''
network:
  wifis:
    wlan0:
      access-points:
        "TestNet":
          password: "hunter2"
'''
        cp = self._run(yaml_text)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout, "ssid=TestNet\npsk=hunter2\n")

    def test_finds_a_networkmanager_style_auth_password(self):
        """NetworkManager writes netplan with auth.password, not a bare password"""
        yaml_text = '''
network:
  wifis:
    NM-uuid:
      access-points:
        "NMNet":
          auth:
            key-management: psk
            password: "s3cret!"
'''
        cp = self._run(yaml_text)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout, "ssid=NMNet\npsk=s3cret!\n")

    def test_no_password_anywhere_exits_3(self):
        yaml_text = '''
network:
  wifis:
    wlan0:
      access-points:
        "OpenNet": {}
'''
        cp = self._run(yaml_text)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout, "")

    def test_empty_input_exits_3(self):
        cp = self._run("")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)


class TestNoWifiCredsFile(unittest.TestCase):
    """No hand-made WiFi credentials file survives anywhere the old design
    used to read it: the credential comes from the disk machine's own WiFi
    connection now (wifi-host/wifi-from-host, admin/wk-card-priv)."""

    def test_no_reference_to_the_old_wifi_creds_file(self):
        paths = []
        for d in ("cmd", "lib", "boot", "admin"):
            paths += sorted((REPO / d).rglob("*"))
        paths.append(REPO / "README.md")
        bad = []
        for p in paths:
            if not p.is_file():
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            if ".config/wk/wifi" in text or "WK_WIFI_CREDS" in text or "disk_wifi_creds" in text:
                bad.append(str(p.relative_to(REPO)))
        self.assertEqual(bad, [], f"still references the old WiFi credentials file: {bad}")


class TestWifiConfContent(WkTest):
    def _wifi_edit(self, ssid, psk, mnt):
        """Runs the helper's _wifi_edit against a plain directory (not a real
        mount), with chown stubbed out: this machine is not root, and what is
        under test here is the file it writes, not who owns it -- ownership is
        exercised by the gate/mount machinery elsewhere, on the machine that
        actually holds a card reader."""
        script = f'''
fail() {{ printf 'wk-card-priv: %s\\n' "$*" >&2; exit 1; }}
chown() {{ :; }}
{_lift(CARD_PRIV, "check_wifi_value")}
{_lift(CARD_PRIV, "_wifi_edit")}
_wifi_edit {mnt} {ssid!r} {psk!r}
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)

    def test_generated_wpa_supplicant_conf_is_well_formed(self):
        """the generated wpa_supplicant conf for a synthetic ssid/psk is well-formed"""
        with tempfile.TemporaryDirectory() as d:
            self._wifi_edit("My Test Net", "hunter2 pass", d)
            conf = Path(d) / "etc" / "wpa_supplicant" / "wpa_supplicant-wlan0.conf"
            self.assertTrue(conf.exists(), "no conf file was written")
            text = conf.read_text()
            self.assertIn("ctrl_interface=/var/run/wpa_supplicant", text)
            self.assertIn("update_config=0", text)
            self.assertRegex(text, r"(?m)^network=\{$")
            self.assertIn('    ssid="My Test Net"', text)
            self.assertIn('    psk="hunter2 pass"', text)
            self.assertIn("}", text)

    def test_check_wifi_value_rejects_what_would_break_the_conf(self):
        """a quote, a backslash, an empty value or a control character is refused"""
        script = f'''
deny() {{ printf 'wk-card-priv: REFUSED: %s\\n' "$*" >&2; exit 3; }}
{_lift(CARD_PRIV, "check_wifi_value")}
check_wifi_value "the SSID" "$1"
'''
        good = subprocess.run(["bash", "-c", script, "_", "PlainNet 123"], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)

        for bad_value, why in [
            ('bad"ssid', "quote"),
            ("bad\\psk", "backslash"),
            ("", "empty"),
        ]:
            with self.subTest(why=why):
                cp = subprocess.run(["bash", "-c", script, "_", bad_value], capture_output=True, text=True)
                self.assertEqual(cp.returncode, 3, f"{why}: {cp.stdout + cp.stderr}")
                self.assertIn("REFUSED", cp.stdout + cp.stderr)


class TestImageWantsWifi(WkTest):
    def test_only_rpi3_rpi4_rpi5_want_wifi(self):
        """WiFi is derived from IMG_MACHINE, for exactly the fleet's Pi boards"""
        script = f'''
. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"; . "{REPO}/boot/disk.sh"
for m in rpi3 rpi4 rpi5 mbp benchvm bogus ""; do
    if _image_wants_wifi "$m"; then echo "$m=yes"; else echo "$m=no"; fi
done
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        want = {"rpi3": "yes", "rpi4": "yes", "rpi5": "yes",
                "mbp": "no", "benchvm": "no", "bogus": "no", "": "no"}
        got = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
        self.assertEqual(got, want)

    def test_every_image_config_names_rpi3_rpi4_or_rpi5(self):
        """every image/configs/*.conf is for a board _image_wants_wifi knows about"""
        # static: the reason no IMG_NET field was added -- the fact is already
        # fully implied by IMG_MACHINE, since every profile targets one of the
        # three boards this defect is about.
        bad = []
        for f in sorted((REPO / "image" / "configs").glob("*.conf")):
            m = re.search(r"(?m)^IMG_MACHINE=(\S+)", f.read_text(errors="replace"))
            mach = m.group(1) if m else ""
            if mach not in ("rpi3", "rpi4", "rpi5"):
                bad.append(f"{f.name}: IMG_MACHINE={mach or '(none)'}")
        self.assertEqual(bad, [], f"confs naming a board wifi-seeding does not know: {bad}")


class TestWifiPreflight(WkTest):
    def _preflight(self, machine, card_priv_output, card_priv_rc=0):
        """Runs the real _wifi_creds_preflight with card_priv stubbed --
        exactly the shape it is invoked in, m_ssh included, but without a
        real machine or ssh session: the preflight asks $DISK_MACHINE
        (already loaded by cmd_write_from at this point), never a local
        file, so what it needs stubbed is card_priv's answer."""
        script = f'''
. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"; . "{REPO}/boot/disk.sh"
{_lift(REPO / "cmd" / "sysimage", "_wifi_creds_preflight")}
DISK_MACHINE="stub-disk-machine"
card_priv() {{ printf '%s\\n' {card_priv_output!r}; exit {card_priv_rc}; }}
_wifi_creds_preflight {machine!r}
'''
        return bash(script)

    def test_refuses_a_wifi_board_when_the_disk_machine_is_not_on_wifi(self):
        """refuses a WiFi board when the disk machine's own wifi-host says no, and names the remedy"""
        cp = self._preflight("rpi3", "wifi-host: no")
        self.assertNotEqual(cp.returncode, 0, "wifi-host: no, but did not refuse")
        out = cp.stdout + cp.stderr
        self.assertIn("no uplink", out)
        self.assertIn("stub-disk-machine", out, "the refusal does not name the disk machine")

    def test_refuses_when_card_priv_itself_fails(self):
        """an ssh/helper failure asking wifi-host refuses too -- no answer is not a yes"""
        cp = self._preflight("rpi5", "wk-card-priv: connection refused", card_priv_rc=1)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_force_does_not_cross_the_wifi_barrier(self):
        """--force does not appear anywhere in the wifi preflight -- there is no override"""
        # static: unlike _tailnet_key_preflight (a `barrier`, crossable by
        # --force), _wifi_creds_preflight is an unconditional `die` -- a
        # board with no cable and no WiFi credential has no uplink at all,
        # which CLAUDE.md's barrier rule does not license forcing past.
        text = (REPO / "cmd" / "sysimage").read_text(errors="replace")
        m = re.search(r"(?ms)^_wifi_creds_preflight\(\).*?^\}", text)
        self.assertIsNotNone(m, "_wifi_creds_preflight not found in cmd/sysimage")
        body = m.group(0)
        self.assertNotIn("barrier", body, "the wifi preflight is crossable, like a barrier")
        self.assertIn("die ", body)

    def test_passes_for_a_wired_board_regardless_of_the_disk_machine(self):
        """a board with a cable needs no WiFi credential at all"""
        cp = self._preflight("mbp", "wifi-host: no")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_passes_for_a_wifi_board_when_the_disk_machine_is_on_wifi(self):
        """passes once the disk machine's own wifi-host says yes"""
        cp = self._preflight("rpi4", "wifi-host: yes ssid=TestNet")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestSysimageWriteDryRun(WkTest):
    @requires_machine("rpi3")
    def test_write_dry_run_reports_the_disk_machines_wifi_state(self):
        """`wk sysimage write --dry-run` reports the disk machine's own WiFi state for a wifi board"""
        # Exercises the real command end to end (--from, so no store/manifest
        # is needed) against a profile this checkout defines, rather than a
        # lifted function -- the dry run path itself is what has to print the
        # right thing before the real write ever asks. Needs rpi3 itself
        # reachable over ssh (cmd_write_from checks the disk before printing
        # the dry run), which this checkout's development machine is not --
        # self-skips rather than asserting against a machine nobody has here.
        with tempfile.TemporaryDirectory() as store, tempfile.NamedTemporaryFile(suffix=".img") as img:
            img.write(b"\0" * 1024)
            img.flush()
            cp = run(
                "sysimage", "write", "--from", img.name,
                "--profile", "wpewebkit-2.46-yocto-rpi3-32",
                "--disk", "rpi3:/dev/sdX", "--dry-run",
                env={"WK_STORE": store},
            )
            out = cp.stdout
            self.assertRegex(out, r"(?m)^\s*wifi\s+(NO --|rpi3 is on WiFi)", out)

    def test_build_dry_run_shows_wifi_wired_into_yocto_and_buildroot(self):
        """`wk sysimage build --dry-run` names the wifi layer/overlay for both builders"""
        # No hardware needed: --dry-run resolves the profile and prints what
        # it would do, with no workspace, ssh or reachable board.
        for profile, want in [
            ("wpewebkit-2.46-yocto-rpi3-32", "wk-wifi-join in the image (meta-wk-wifi)"),
            ("wpewebkit-2.46-buildroot-rpi3-32", "wk-wifi-join (image/buildroot/wifi-overlay.sh)"),
        ]:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as store:
                cp = run("sysimage", "build", profile, "--dry-run", env={"WK_STORE": store})
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn(want, cp.stdout, f"{profile}: {cp.stdout}")


# --------------------------------------------------------------------------- #
# tailnet name collision (cmd/sysimage: _tailnet_name_collides /
# _tailnet_name_preflight) -- a stale or renamed node blocking a card the
# same way a missing tailnet key or WiFi credential does, before anything is
# erased. lib/reach.sh's wk_tailscale_peers is the one parser of `tailscale
# status --json`; these tests feed it a synthetic document through a stubbed
# `tailscale` binary rather than adding a second parser here.
# --------------------------------------------------------------------------- #

def _fake_tailscale(tmp, peers_json):
    """A `tailscale` stand-in that prints a fixed `status --json` document,
    so wk_tailscale_peers (the real, unmodified parser) runs against
    synthetic data with no live tailnet."""
    stub = tmp / "tailscale"
    stub.write_text(f"#!/bin/sh\ncat <<'EOF'\n{peers_json}\nEOF\n")
    stub.chmod(0o755)
    return stub


def _run_tailnet_preflight(name, tmp, peers_json=None, no_cli=False):
    ts_stub = "" if no_cli else str(_fake_tailscale(tmp, peers_json or "{}"))
    script = f'''
. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"
{_lift(REPO / "cmd" / "sysimage", "_tailnet_name_collides")}
{_lift(REPO / "cmd" / "sysimage", "_tailnet_name_preflight")}
wk_tailscale_cli() {{ {"return 1" if no_cli else f"printf '%s' {ts_stub!r}"}; }}
_tailnet_name_preflight {name!r}
'''
    return bash(script)


class TestTailnetNameCollision(WkTest):
    PEERS = '''{"Self":{"HostName":"driver-mac","TailscaleIPs":["100.1.1.1"],"Online":true},
 "Peer":{
   "a":{"HostName":"rpi3-1","TailscaleIPs":["100.1.1.2"],"Online":false},
   "b":{"HostName":"rpi4","TailscaleIPs":["100.1.1.3"],"Online":true}
 }}'''

    def test_exact_match_refuses_with_remedy(self):
        """an existing exact name match refuses, online or offline, and names the remedy"""
        cp = _run_tailnet_preflight("rpi4", self.tmp, self.PEERS)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        out = cp.stdout + cp.stderr
        self.assertIn("already on the tailnet", out)
        self.assertIn("rpi4-1", out, "does not name the rename tailscale would perform")
        self.assertIn("admin console", out)
        self.assertIn("--force", out, "does not say --force cannot cross this")

    def test_suffixed_match_refuses(self):
        """a '<name>-N' peer -- the trace of an earlier rename -- refuses too"""
        cp = _run_tailnet_preflight("rpi3", self.tmp, self.PEERS)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("rpi3-1", cp.stdout + cp.stderr)

    def test_no_match_passes(self):
        """a name nothing on the tailnet has taken passes"""
        cp = _run_tailnet_preflight("rpi5", self.tmp, self.PEERS)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_case_insensitive_match_refuses(self):
        """RPI4 on the tailnet still blocks a write for 'rpi4'"""
        peers = self.PEERS.replace('"rpi4"', '"RPI4"')
        cp = _run_tailnet_preflight("rpi4", self.tmp, peers)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)

    def test_empty_or_invalid_json_refuses_the_check_cannot_be_skipped(self):
        """empty/invalid tailscale output refuses -- the check cannot be skipped"""
        for label, doc in [("empty", ""), ("garbage", "not json at all")]:
            with self.subTest(doc=label):
                cp = _run_tailnet_preflight("rpi3", self.tmp, doc)
                self.assertNotEqual(cp.returncode, 0, f"{label}: {cp.stdout}")
                self.assertIn("cannot be skipped", cp.stdout + cp.stderr)

    def test_no_tailscale_cli_refuses(self):
        """no tailscale CLI on this machine refuses -- the same reason"""
        cp = _run_tailnet_preflight("rpi3", self.tmp, no_cli=True)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("cannot be skipped", cp.stdout + cp.stderr)

    def test_empty_name_is_a_no_op(self):
        """no name to seed (no --profile, unresolved machine) is not this check's problem"""
        cp = _run_tailnet_preflight("", self.tmp, self.PEERS)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_name_collision_preflight_is_wired_into_the_write_path(self):
        """the one write path (cmd_write_from) runs the collision check before erasing"""
        # static
        text = (REPO / "cmd" / "sysimage").read_text(errors="replace")
        body = {}
        for fn in ("cmd_write_from", "cmd_write"):
            m = re.search(rf"(?ms)^{fn}\(\).*?(?=^\w[\w_]*\(\) \{{|\Z)", text)
            self.assertIsNotNone(m, f"{fn} not found")
            body[fn] = m.group(0)
        self.assertIn("_tailnet_name_preflight", body["cmd_write_from"],
                      "cmd_write_from does not run the name-collision check")
        self.assertIn("cmd_write_from", body["cmd_write"],
                      "cmd_write does not delegate to cmd_write_from -- a second write path")


# --------------------------------------------------------------------------- #
# _tailnet_key_preflight (cmd/sysimage) -- the hard counterpart of
# _wifi_creds_preflight: an unresolved machine name or a missing tailnet auth
# key refuses the write outright, with no --force, before anything is erased
# (disk_seed_tailnet, boot/disk.sh, assumes both are already guaranteed and no
# longer barriers on them itself).
# --------------------------------------------------------------------------- #

def _tailnet_key_preflight(machine, authkey_path, force=False):
    script = f'''
. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"
{_lift(REPO / "cmd" / "sysimage", "_tailnet_name_for")}
{_lift(REPO / "cmd" / "sysimage", "_tailnet_key_preflight")}
_tailnet_key_preflight {machine!r}
'''
    env = {"WK_TS_AUTHKEY": authkey_path}
    if force:
        env["WK_FORCE"] = "1"
    return bash(script, env=env)


class TestTailnetKeyPreflight(WkTest):
    def test_refuses_with_no_machine_name_and_names_the_remedy(self):
        """an unresolved machine name refuses, and names --machine as the remedy"""
        key = self.tmp / "authkey"
        key.write_text("tskey-abc123\n")
        cp = _tailnet_key_preflight("", str(key))
        self.assertNotEqual(cp.returncode, 0, "wrote nothing, but did not refuse")
        self.assertIn("--machine", cp.stdout + cp.stderr)

    def test_refuses_with_no_key_and_names_the_remedy(self):
        """no tailnet auth key on this machine refuses, and names 'wk key tailnet'"""
        cp = _tailnet_key_preflight("rpi3", str(self.tmp / "no-such-key"))
        self.assertNotEqual(cp.returncode, 0, "wrote nothing, but did not refuse")
        self.assertIn("wk key tailnet", cp.stdout + cp.stderr)

    def test_wk_force_does_not_cross_the_missing_machine_name_refusal(self):
        """WK_FORCE=1 changes nothing -- there is no --force past this"""
        key = self.tmp / "authkey2"
        key.write_text("tskey-abc123\n")
        cp = _tailnet_key_preflight("", str(key), force=True)
        self.assertNotEqual(cp.returncode, 0, "WK_FORCE=1 let an unresolved machine name through")

    def test_wk_force_does_not_cross_the_missing_key_refusal(self):
        """WK_FORCE=1 changes nothing -- there is no --force past this"""
        cp = _tailnet_key_preflight("rpi3", str(self.tmp / "still-no-key"), force=True)
        self.assertNotEqual(cp.returncode, 0, "WK_FORCE=1 let a missing tailnet key through")

    def test_passes_with_a_resolved_machine_and_a_present_key(self):
        """a real machine name and a present key pass, with nothing left to refuse"""
        key = self.tmp / "authkey3"
        key.write_text("tskey-abc123\n")
        cp = _tailnet_key_preflight("rpi3", str(key))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


# --------------------------------------------------------------------------- #
# wk-tailnet-join (image/yocto/meta-wk-tailnet/recipes-network/tailscale/files
# -- byte-identical on the buildroot side, installed by
# image/buildroot/tailnet-overlay.sh) -- run against relocated paths and a
# stub tailscale, since the real paths are /etc/wk and /usr/bin/tailscale and
# this is not the image.
# --------------------------------------------------------------------------- #

TAILNET_JOIN_DIR = REPO / "image" / "yocto" / "meta-wk-tailnet" / "recipes-network" / "tailscale" / "files"
TAILNET_JOIN = TAILNET_JOIN_DIR / "wk-tailnet-join"


def _tailnet_join_script(tmp):
    """A runnable copy of wk-tailnet-join with KEY/CONF/TS relocated under
    tmp (a HOME/ROOT override, since the real script hardcodes /etc/wk and
    /usr/bin/tailscale) and a stub `tailscale` standing in for the real one --
    the script's own logic runs unmodified, against files this test controls,
    with no real tailnet daemon or auth key touched."""
    text = TAILNET_JOIN.read_text()
    key, conf, ts = tmp / "tailscale-authkey", tmp / "tailnet.conf", tmp / "tailscale"
    text = text.replace("KEY=/etc/wk/tailscale-authkey", f"KEY={key}")
    text = text.replace("CONF=/etc/wk/tailnet.conf", f"CONF={conf}")
    text = text.replace("TS=/usr/bin/tailscale", f"TS={ts}")
    script = tmp / "wk-tailnet-join"
    script.write_text(text)
    script.chmod(0o755)
    # Answers "not on the tailnet" to `ip -4` (and anything else), so the
    # script falls through to the key check this test is after rather than
    # taking the "already joined" early exit.
    ts.write_text("#!/bin/sh\nexit 1\n")
    ts.chmod(0o755)
    return script


class TestTailnetJoinScript(WkTest):
    def test_missing_key_exits_nonzero_and_names_the_remedy(self):
        """no auth key on disk: loud, non-zero, names the missing file and the remedy"""
        script = _tailnet_join_script(self.tmp)
        cp = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("tailscale-authkey", out, "does not name the missing file")
        self.assertIn("wk sysimage write --machine", out, "does not name the remedy")

    def test_already_joined_with_no_key_is_not_a_failure(self):
        """a later boot -- already on the tailnet, key long since spent -- is quiet and clean"""
        script = _tailnet_join_script(self.tmp)
        ts = self.tmp / "tailscale"
        # Answers "already joined" to `ip -4` and everything else; no key file
        # exists, the same as any boot after the first.
        ts.write_text('#!/bin/sh\nif [ "$1" = ip ]; then echo 100.1.1.1; exit 0; fi\nexit 0\n')
        ts.chmod(0o755)
        cp = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_service_unit_does_not_gate_on_the_key_file(self):
        """the systemd unit runs every boot rather than skipping quietly when the key is gone"""
        # static: an active ConditionPathExists= on the key would skip this
        # unit silently on exactly the boot that has to be loud about its
        # absence -- the comment explaining why there is none may still say
        # the word, so this checks for a live directive line, not the prose.
        unit = (TAILNET_JOIN_DIR / "wk-tailnet-join.service").read_text()
        self.assertNotRegex(unit, r"(?m)^ConditionPathExists")


if __name__ == "__main__":
    unittest.main()


class TestWifiJoinScriptIsLoud(unittest.TestCase):
    """wk-wifi-join refuses loudly, not quietly, when the card carries no credentials"""

    SCRIPT = (REPO / "image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join")
    SERVICE = SCRIPT.with_name("wk-wifi-join.service")

    def test_missing_conf_exits_nonzero_and_names_the_remedy(self):  # static
        text = self.SCRIPT.read_text()
        line = next(l for l in text.splitlines() if l.startswith('[ -r "$CONF" ]'))
        self.assertIn("exit 1", line)
        self.assertIn("wk sysimage write", line)
        self.assertNotIn("exit 0", line)

    def test_service_has_no_condition_that_skips_the_loud_boot(self):  # static
        self.assertNotIn("ConditionPathExists", self.SERVICE.read_text())



class TestRescueReadsItsOwnCredential(WkTest):
    """`_wpa_conf_wifi` (admin/wk-card-priv) -- what `wifi-host` and
    `wifi-from-host` read on a system wk wrote, which has no netplan -- gives
    back exactly what `_wifi_edit` seeded, so a board's rescue can seed the
    bench medium beside it with its own WiFi and no reader in the loop."""

    def _read(self, conf):
        script = f'''
{_lift(CARD_PRIV, "_wpa_conf_wifi")}
out=$(_wpa_conf_wifi {str(conf)!r}) || exit $?
cat "$out"; rm -f "$out"
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "TMPDIR": str(self.tmp)})

    def test_reads_back_what_wifi_edit_wrote(self):
        with tempfile.TemporaryDirectory() as d:
            script = f'''
fail() {{ printf 'wk-card-priv: %s\\n' "$*" >&2; exit 1; }}
chown() {{ :; }}
{_lift(CARD_PRIV, "check_wifi_value")}
{_lift(CARD_PRIV, "_wifi_edit")}
_wifi_edit {d} 'My Test Net' 'hunter2 pass'
'''
            subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10, check=True)
            cp = self._read(Path(d) / "etc/wpa_supplicant/wpa_supplicant-wlan0.conf")
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(cp.stdout, "ssid=My Test Net\npsk=hunter2 pass\n")

    def test_no_credential_is_status_3_like_netplan(self):
        self.assertEqual(self._read(Path("/dev/null")).returncode, 3)
        self.assertEqual(self._read(self.tmp / "missing.conf").returncode, 3)
