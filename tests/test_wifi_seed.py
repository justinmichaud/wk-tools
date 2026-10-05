"""WiFi and tailnet seeding for a card: the card helper's credential extraction from the disk machine, the write's
preflights (lib/wk/sysimage/write.py), and the join scripts the image runs."""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FLEET_ENV, REPO, WkTest, requires_machine, run, run_here

sys.path.insert(0, str(REPO / "lib"))
from wk import act, fleet, images, tailnet  # noqa: E402
from wk.machine import Local, Result  # noqa: E402
from wk.sysimage import write  # noqa: E402


CARD_PRIV = REPO / "admin" / "wk-card-priv"


def have_gnu_stat():
    """`_wifi_edit` reads a mode with GNU `stat -c`; the helper only runs on Linux."""
    return subprocess.run(["stat", "-c", "%a", "/"],
                          capture_output=True).returncode == 0


def _netplan_parser_source():
    """The python `_netplan_wifi` feeds to python3 -c, lifted from the helper."""
    text = CARD_PRIV.read_text(errors="replace")
    m = re.search(r"python3 -c '\n(.*?)\n' > \"\$tmp\"", text, re.S)
    assert m, "no embedded netplan parser in admin/wk-card-priv"
    return m.group(1)


def _lift(path, func):
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    return text


class TestCardHelperWifiGate(WkTest):
    def test_gate_denies_a_non_block_device_without_reading_stdin(self):
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

class TestWifiHostVerb(unittest.TestCase):
    """v_wifi_host's output shape: it names the SSID and never the PSK."""

    def _wifi_host(self, found, ssid="", psk=""):
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
    """The embedded netplan parser; skipped where python3 has no yaml (the helper runs on Linux, where netplan does)."""

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


class TestWifiConfContent(WkTest):
    def _wifi_edit(self, ssid, psk, mnt):
        script = f'''
fail() {{ printf 'wk-card-priv: %s\\n' "$*" >&2; exit 1; }}
chown() {{ :; }}
{_lift(CARD_PRIV, "check_wifi_value")}
{_lift(CARD_PRIV, "_wifi_edit")}
_wifi_edit {mnt} {ssid!r} {psk!r}
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)

    def test_generated_wpa_supplicant_conf_is_well_formed(self):
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


class Channel:
    """The disk machine's card helper at the Channel, answering one verb."""

    def __init__(self, **answers):
        self.answers, self.calls, self.channel = answers, [], "host"

    def call(self, fn, *args, input=None, mutates=False):
        self.calls.append((fn,) + args)
        return Result(*self.answers.get(args[0], (0, "")))


def writer(**answers):
    w = write.Write(REPO, dict(os.environ, **FLEET_ENV), Local(), None)
    w.conf, w.ch = {"name": "stub-disk-machine", "ssh": "stub-disk-machine"}, Channel(**answers)
    return w


def refusal(fn, *args):
    """The refusal's words, or None when `fn` passed."""
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            fn(*args)
        except act.Refused:
            return err.getvalue()
    return None


class TestImageWantsWifi(WkTest):
    def test_only_rpi3_rpi4_rpi5_want_wifi(self):
        f = fleet.Fleet(REPO, FLEET_ENV)
        got = {m: write.wants_wifi(f, m) for m in ("rpi3", "rpi4", "rpi5", "mbp", "benchvm", "bogus", "")}
        self.assertEqual(got, {"rpi3": True, "rpi4": True, "rpi5": True,
                               "mbp": False, "benchvm": False, "bogus": False, "": False})

    def test_a_machine_wk_writes_no_card_for_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("wifimach", "net=wifi\ndevice=/dev/sda\n"),
                               ("ethmach", "net=ethernet\ndevice=/dev/sda\n"),
                               ("nocard", "net=wifi\n")):
                Path(d, name + ".conf").write_text("kind=board\ndriver=pi-sd\nnote=x\n" + body)
            f = fleet.Fleet(REPO, dict(FLEET_ENV, WK_MACHINES_DIR=d))
            self.assertEqual([write.wants_wifi(f, m) for m in ("wifimach", "ethmach", "nocard")], [True, False, False])

    def test_every_board_image_names_rpi3_rpi4_or_rpi5(self):
        bad = []
        for n in images.names():
            p = images.load(n)
            if p["IMG_BUILDER"] in images.WS_BUILDERS and p["IMG_MACHINE"] not in ("rpi3", "rpi4", "rpi5"):
                bad.append(f"{n}: IMG_MACHINE={p['IMG_MACHINE'] or '(none)'}")
        self.assertEqual(bad, [], f"confs naming a board wifi-seeding does not know: {bad}")


class TestWifiPreflight(unittest.TestCase):
    """The preflight asks the disk machine's own card helper, never a local file; there is no --force past it."""

    def test_refuses_a_wifi_board_when_the_disk_machine_is_not_on_wifi(self):
        err = refusal(writer(**{"wifi-host": (0, "wifi-host: no")}).wifi_preflight, "rpi3")
        self.assertIn("no uplink", err)
        self.assertIn("stub-disk-machine", err, "the refusal does not name the disk machine")

    def test_refuses_when_card_priv_itself_fails(self):
        """no answer is not a yes"""
        self.assertIsNotNone(refusal(writer(**{"wifi-host": (1, "connection refused")}).wifi_preflight, "rpi5"))

    def test_force_does_not_cross_the_wifi_barrier(self):
        with mock.patch.dict(os.environ, {"WK_FORCE": "1"}):
            self.assertIsNotNone(refusal(writer(**{"wifi-host": (0, "wifi-host: no")}).wifi_preflight, "rpi3"))

    def test_passes_for_a_wired_board_regardless_of_the_disk_machine(self):
        w = writer(**{"wifi-host": (0, "wifi-host: no")})
        self.assertIsNone(refusal(w.wifi_preflight, "mbp"))
        self.assertEqual(w.ch.calls, [])

    def test_passes_for_a_wifi_board_when_the_disk_machine_is_on_wifi(self):
        self.assertIsNone(refusal(writer(**{"wifi-host": (0, "wifi-host: yes ssid=TestNet")}).wifi_preflight, "rpi4"))


class TestSysimageWriteDryRun(WkTest):
    @requires_machine("rpi3")
    def test_write_dry_run_reports_the_disk_machines_wifi_state(self):
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
        for profile in ("wpewebkit-2.46-yocto-rpi3-32", "wpewebkit-2.46-buildroot-rpi3-32"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as store:
                cp = run_here("sysimage", "build", profile, "--dry-run", env={"WK_STORE": store})
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("wk-wifi-join", cp.stdout, f"{profile}: {cp.stdout}")


# The tailnet name a card joins under, checked against a stubbed `tailscale status --json`.

def _name_preflight(name, tmp, peers_json="{}", role="bench", machine="rpi3", env=None):
    stub = tmp / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "tailscale").write_text(f"#!/bin/sh\ncat <<'EOF'\n{peers_json}\nEOF\n")
    (stub / "tailscale").chmod(0o755)
    over = {"PATH": f"{stub}:{os.environ['PATH']}", "WK_TS_API_SECRET": str(tmp / "none")}
    with mock.patch.dict(os.environ, dict(over, **(env or {}))):
        return refusal(writer().name_preflight, name, role, machine)


class TestTailnetNameCollision(WkTest):
    PEERS = '''{"Self":{"HostName":"driver-mac","DNSName":"driver-mac.tail0.ts.net.","TailscaleIPs":["100.1.1.1"],"Online":true},
 "Peer":{
   "a":{"HostName":"rpi3-1","DNSName":"rpi3-1.tail0.ts.net.","TailscaleIPs":["100.1.1.2"],"Online":false},
   "b":{"HostName":"rpi4","DNSName":"rpi4.tail0.ts.net.","TailscaleIPs":["100.1.1.3"],"Online":true}
 }}'''

    def test_exact_match_refuses_with_remedy(self):
        err = _name_preflight("rpi4", self.tmp, self.PEERS)
        self.assertIn("already on the tailnet", err)
        self.assertIn("rpi4-1", err, "does not name the rename tailscale would perform")
        self.assertIn("admin console", err)
        self.assertIn("--force", err, "does not say --force cannot cross this")

    def test_suffixed_match_refuses(self):
        """a '<name>-N' peer -- the trace of an earlier rename -- refuses too"""
        self.assertIn("rpi3-1", _name_preflight("rpi3", self.tmp, self.PEERS))

    def test_no_match_passes(self):
        self.assertIsNone(_name_preflight("rpi5", self.tmp, self.PEERS))

    def test_case_insensitive_match_refuses(self):
        """RPI4 on the tailnet still blocks a write for 'rpi4'"""
        self.assertIsNotNone(_name_preflight("rpi4", self.tmp, self.PEERS.replace('"rpi4.tail0', '"RPI4.tail0')))

    def test_a_peer_is_keyed_by_its_magicdns_label_not_its_os_hostname(self):
        """A Mac keeps its hostname's capitals and two phones both answer 'localhost'."""
        peers = self.PEERS.replace('"b":{"HostName":"rpi4","DNSName":"rpi4.tail0.ts.net."',
                                   '"b":{"HostName":"Tolken","DNSName":"rpi4.tail0.ts.net."')
        self.assertIn("already on the tailnet", _name_preflight("rpi4", self.tmp, peers))

    def test_a_peer_with_no_magicdns_name_is_no_name_at_all(self):
        """MagicDNS off means there is no name to dial, so the peer yields no row."""
        self.assertIsNone(_name_preflight("rpi4", self.tmp, self.PEERS.replace('"DNSName":"rpi4.tail0.ts.net.",', "")))

    def test_empty_or_invalid_json_refuses_the_check_cannot_be_skipped(self):
        for label, doc in [("empty", ""), ("garbage", "not json at all")]:
            with self.subTest(doc=label):
                self.assertIn("cannot be skipped", _name_preflight("rpi3", self.tmp, doc))

    def test_empty_name_is_a_no_op(self):
        """no name to seed (no --profile, unresolved machine) is not this check's problem"""
        self.assertIsNone(_name_preflight("", self.tmp, self.PEERS))

    def test_a_rescue_replacing_itself_is_a_barrier_that_force_crosses(self):
        peers = self.PEERS.replace("rpi4.tail0", "stub-disk-machine.tail0")
        args = ("stub-disk-machine", self.tmp, peers, "rescue", "stub-disk-machine")
        self.assertIn("running rescue", _name_preflight(*args))
        self.assertIsNone(_name_preflight(*args, env={"WK_FORCE": "1"}))

    def test_the_exception_is_exactly_one_case_and_force_is_recorded(self):
        """this board, its own rescue name, a rescue write; nothing else crosses, whatever --force says"""
        peers = self.PEERS.replace("rpi4.tail0", "stub-disk-machine.tail0")
        w = writer()
        with mock.patch.dict(os.environ, {"PATH": f"{self.tmp}/bin:{os.environ['PATH']}", "WK_FORCE": "1",
                                          "WK_TS_API_SECRET": str(self.tmp / "none")}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            _name_preflight("stub-disk-machine", self.tmp, peers)
            w.name_preflight("stub-disk-machine", "rescue", "stub-disk-machine")
        self.assertIn("FORCED past a barrier", err.getvalue())
        for role, machine in (("bench", "stub-disk-machine"), ("rescue", "rpi3")):
            with self.subTest(role=role, machine=machine):
                self.assertIn("no --force", _name_preflight("stub-disk-machine", self.tmp, peers, role, machine,
                                                            env={"WK_FORCE": "1"}))

    def test_a_stored_token_retires_the_stale_node(self):
        retired = []
        with mock.patch.object(tailnet.Fleet, "api_present", lambda fl: True), \
                mock.patch.object(tailnet.Fleet, "retire", lambda fl, n: retired.append(n) or Result(0, "retired\n")):
            self.assertIsNone(_name_preflight("rpi4", self.tmp, self.PEERS))
        self.assertEqual(retired, ["rpi4"])


class TestTailnetKeyPreflight(WkTest):

    def _preflight(self, machine, key, force=False):
        env = {"WK_TS_AUTHKEY": str(key), "WK_TS_API_SECRET": str(self.tmp / "no-api-key")}
        if force:
            env["WK_FORCE"] = "1"
        with mock.patch.dict(os.environ, env):
            return refusal(writer().key_preflight, machine, "bench")

    def key(self):
        key = self.tmp / "authkey"
        key.write_text("tskey-auth-abc123-secret\n")
        return key

    def test_refuses_with_no_machine_name_and_names_the_remedy(self):
        self.assertIn("--machine", self._preflight("", self.key()))

    def test_refuses_with_no_key_and_names_the_remedy(self):
        self.assertIn("wk key set tailnet", self._preflight("rpi3", self.tmp / "no-such-key"))

    def test_wk_force_does_not_cross_either_refusal(self):
        self.assertIsNotNone(self._preflight("", self.key(), force=True))
        self.assertIsNotNone(self._preflight("rpi3", self.tmp / "still-no-key", force=True))

    def test_passes_with_a_resolved_machine_and_a_present_key(self):
        self.assertIsNone(self._preflight("rpi3", self.key()))



TAILNET_JOIN_DIR = REPO / "image" / "yocto" / "meta-wk-tailnet" / "recipes-network" / "tailscale" / "files"
TAILNET_JOIN = TAILNET_JOIN_DIR / "wk-tailnet-join"


def _tailnet_join_script(tmp):
    """wk-tailnet-join with its paths relocated under tmp and a `tailscale` that is not joined."""
    text = TAILNET_JOIN.read_text()
    key, conf, ts = tmp / "tailscale-authkey", tmp / "tailnet.conf", tmp / "tailscale"
    text = text.replace("KEY=/etc/wk/tailscale-authkey", f"KEY={key}")
    text = text.replace("CONF=/etc/wk/tailnet.conf", f"CONF={conf}")
    text = text.replace("TS=/usr/bin/tailscale", f"TS={ts}")
    script = tmp / "wk-tailnet-join"
    script.write_text(text)
    script.chmod(0o755)
    ts.write_text("#!/bin/sh\nexit 1\n")
    ts.chmod(0o755)
    return script


class TestTailnetJoinScript(WkTest):
    def test_missing_key_exits_nonzero_and_names_the_remedy(self):
        script = _tailnet_join_script(self.tmp)
        cp = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("tailscale-authkey", out, "does not name the missing file")
        self.assertIn("wk sysimage write --machine", out, "does not name the remedy")

    def test_already_joined_with_no_key_is_not_a_failure(self):
        script = _tailnet_join_script(self.tmp)
        ts = self.tmp / "tailscale"
        ts.write_text('#!/bin/sh\nif [ "$1" = ip ]; then echo 100.1.1.1; exit 0; fi\nexit 0\n')
        ts.chmod(0o755)
        cp = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

class TestWifiJoinScriptIsLoud(WkTest):
    SCRIPT = (REPO / "image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join")

    def test_missing_conf_exits_nonzero_and_names_the_remedy(self):
        script = self.tmp / "wk-wifi-join"
        script.write_text(self.SCRIPT.read_text().replace(
            "CONF=/etc/wpa_supplicant/wpa_supplicant-wlan0.conf", "CONF=%s" % (self.tmp / "none.conf")))
        cp = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("wk sysimage write", cp.stdout + cp.stderr)


class TestRescueReadsItsOwnCredential(WkTest):
    """`_wpa_conf_wifi` reads back what `_wifi_edit` seeded, so a rescue can seed its sibling with its own WiFi."""

    def _read(self, conf):
        script = f'''
{_lift(CARD_PRIV, "_wpa_conf_wifi")}
out=$(_wpa_conf_wifi {str(conf)!r}) || exit $?
cat "$out"; rm -f "$out"
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "TMPDIR": str(self.tmp)})

    @unittest.skipUnless(have_gnu_stat(),
                         "the helper runs on a Linux card machine (GNU stat)")
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


if __name__ == "__main__":
    unittest.main()
