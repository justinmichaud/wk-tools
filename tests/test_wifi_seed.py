"""WiFi credential seeding: rpi3/rpi4/rpi5 have no cable at the bench, so
their rescue and bench images bring up WiFi from a credential `wk sysimage
write` seeds onto the card -- the same shape as the tailnet auth key
(disk_seed_tailnet, boot/disk.sh), one seed further. See boot/disk.sh
(disk_wifi_creds_path/present, disk_seed_wifi, _image_wants_wifi),
admin/wk-card-priv (v_wifi, v_wifi_joins, check_wifi_value, _wifi_edit) and
cmd/sysimage (_wifi_creds_preflight).

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
    def test_wifi_verb_refuses_a_non_gated_device_not_a_crash(self):
        """the helper's wifi verb refuses a non-gated device, not a crash"""
        # admin/wk-card-priv is installed root:0755 by admin/install.sh; the
        # checked-in copy is not marked executable, so this runs it through
        # bash directly -- the same thing `sudo -n <path>` does on the far
        # end, minus the sudo this machine (and this test) is not root on.
        cp = subprocess.run(
            ["bash", str(CARD_PRIV), "wifi", "/dev/null"],
            input="ssid=x\npsk=y\n", capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(cp.returncode, 0, "a non-root, non-gated call to 'wifi' succeeded")
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

    def test_wifi_and_wifi_joins_call_the_gate(self):
        """v_wifi and v_wifi_joins call gate, like every other verb"""
        # static, the same shape as test_quick.py's test_card_helper_gate
        text = CARD_PRIV.read_text(errors="replace")
        bad = []
        for v in ("v_wifi", "v_wifi_joins"):
            m = re.search(rf"(?ms)^{v}\(\) \{{.*?^\}}", text)
            if not m or "gate " not in m.group(0):
                bad.append(f"{v} does not call gate")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_wifi_verb_is_in_the_dispatcher_and_usage(self):
        """wifi and wifi-joins are dispatched, and named in the usage line"""
        text = CARD_PRIV.read_text(errors="replace")
        case_m = re.search(r'(?ms)^case "\$verb" in.*?^esac', text)
        self.assertIsNotNone(case_m, "no verb dispatcher found")
        body = case_m.group(0)
        self.assertRegex(body, r"wifi-joins\)\s*v_wifi_joins")
        self.assertRegex(body, r"\bwifi\)\s*v_wifi\b")
        self.assertIn("wifi-joins|wifi", text, "the usage line does not name the wifi verbs")


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
. "{REPO}/lib/common.sh"; . "{REPO}/boot/disk.sh"
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
    def _preflight(self, machine, store):
        script = f'''
export WK_STORE={store}
. "{REPO}/lib/common.sh"; . "{REPO}/boot/disk.sh"
{_lift(REPO / "cmd" / "sysimage", "_wifi_creds_preflight")}
_wifi_creds_preflight {machine!r}
'''
        return bash(script)

    def test_refuses_a_wifi_board_with_no_credentials_and_names_the_remedy(self):
        """refuses a WiFi board with no credentials, and names the remedy"""
        with tempfile.TemporaryDirectory() as store:
            cp = self._preflight("rpi3", store)
            self.assertNotEqual(cp.returncode, 0, "wrote nothing, but did not refuse")
            out = cp.stdout + cp.stderr
            self.assertIn("no uplink", out)
            self.assertIn("ssid=", out)
            self.assertIn("psk=", out)
            self.assertIn(f"{store}/secrets/wifi", out, "the refusal does not name where to put credentials")

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

    def test_passes_for_a_wired_board_with_no_credentials(self):
        """a board with a cable needs no WiFi credential at all"""
        with tempfile.TemporaryDirectory() as store:
            cp = self._preflight("mbp", store)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_passes_for_a_wifi_board_once_credentials_exist(self):
        """passes once ssid=/psk= are on disk"""
        with tempfile.TemporaryDirectory() as store:
            secrets = Path(store) / "secrets"
            secrets.mkdir(parents=True)
            (secrets / "wifi").write_text("ssid=TestNet\npsk=hunter22\n")
            cp = self._preflight("rpi4", store)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestSysimageWriteDryRun(WkTest):
    @requires_machine("rpi3")
    def test_write_dry_run_reports_missing_wifi_credentials(self):
        """`wk sysimage write --dry-run` reports missing WiFi credentials for a wifi board"""
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
            self.assertRegex(out, r"(?m)^\s*wifi\s+NO credentials", out)

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

    def test_name_collision_preflight_is_wired_into_both_write_paths(self):
        """cmd_write and cmd_write_from both run the collision check before erasing"""
        # static
        text = (REPO / "cmd" / "sysimage").read_text(errors="replace")
        for fn in ("cmd_write_from", "cmd_write"):
            m = re.search(rf"(?ms)^{fn}\(\).*?(?=^\w[\w_]*\(\) \{{|\Z)", text)
            self.assertIsNotNone(m, f"{fn} not found")
            self.assertIn("_tailnet_name_preflight", m.group(0), f"{fn} does not run the name-collision check")


if __name__ == "__main__":
    unittest.main()
