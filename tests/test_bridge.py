"""A bridge phone with no phone in the room: lib/wk/bridge's read half (ls, status, battery, resolve), the
devices table, and `wk machine setup [--disk]|tailnet|rm <bridge>` (wk.bridge.role, .plan, .provision) against a
fake phone, a fake writer and a fake clock, with `killpoints[machine setup]` for a bridge. `live
bridge.setup[moose-bmc]` and `live bridge.segment[<bridge>]` run against the real phones.

Run: python3 tests/run.py --unit -k test_bridge
"""
import base64
import contextlib
import fnmatch
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WK, WkTest, live_selected, machine_reachable, real_confs, run

sys.path.insert(0, str(REPO / "lib"))
from wk import act, bridge, reach  # noqa: E402
from wk.bridge import plan, provision, role  # noqa: E402
from wk.bridge.plan import AUTHKEY, LIB  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import pmos  # noqa: E402


class TestBridge(WkTest):
    def test_bridge_image_heads_are_distinguishable(self):
        """a bridge image and the recovery image for the same phone differ in content, not only in name"""
        cp = run("sysimage", "ls")
        a = b = None
        for line in cp.stdout.splitlines():
            first = line.split()[0] if line.split() else ""
            if first.startswith("bridge-pinephone-"):
                a = first
            if first.startswith("recovery-pinephone-"):
                b = first
        if not a or not b:
            self.skipTest("needs a bridge-pinephone and a recovery-pinephone image in the store")

        def disk_path(image_id):
            if os.uname().sysname == "Darwin":
                root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "wk"
            else:
                root = Path(os.environ.get("WK_STORE", "/var/lib/wk"))
            return root / "images" / image_id / "disk.img"

        pa, pb = disk_path(a), disk_path(b)
        if not (pa.exists() and pb.exists()):
            self.fail(f"one of the images has no disk.img: {pa} {pb}")
        for p in (pa, pb):
            with open(p, "rb") as f:
                f.seek(8196)
                self.assertEqual(f.read(8), b"eGON.BT0", f"{p} has no sunxi SPL at offset 8192")
        import hashlib
        with open(pa, "rb") as f:
            ha = hashlib.sha256(f.read(1048576)).hexdigest()
        with open(pb, "rb") as f:
            hb = hashlib.sha256(f.read(1048576)).hexdigest()
        self.assertNotEqual(ha, hb, "the two images' first mebibytes are identical")
        self.assertNotEqual(ha, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_bridge_pmos_profiles_declare_bands(self):
        """every postmarketOS profile declares the radio bands its dry run names"""
        bad = []
        for prof in ("bridge-pinephone", "bridge-librem5"):
            cp = run("sysimage", "build", prof, "--dry-run")
            if cp.returncode != 0:
                bad.append(f"{prof}: --dry-run does not resolve: {cp.stdout + cp.stderr}")
                continue
            if not re.search(r"(?m)^  radio ", cp.stdout):
                bad.append(f"{prof}: its dry run names no radio bands")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_bridge_profiles_match_bridge_confs(self):
        """the profile and the bridge conf name the same phone"""
        bad = []
        for prof in ("bridge-pinephone", "bridge-librem5"):
            cp = run("sysimage", "build", prof, "--dry-run")
            out = cp.stdout
            self.assertEqual(cp.returncode, 0, f"{prof}: does not resolve: {out + cp.stderr}")
            dev_m = re.search(r"(?m)^  device *(\S+)", out)
            br_m = re.search(r"(?m)^  for bridge *(\S+)", out)
            if not dev_m:
                bad.append(f"{prof}: the dry run names no device")
                continue
            device = dev_m.group(1)
            bridge = br_m.group(1) if br_m else ""
            conf = REPO / "machines" / f"{bridge}.conf"
            if not conf.exists():
                bad.append(f"{prof}: names bridge '{bridge}', which has no conf")
                continue
            decl_m = re.search(r"(?m)^BR_DEVICE=(.*)$", conf.read_text())
            declared = decl_m.group(1) if decl_m else ""
            if declared not in device:
                bad.append(f"{prof}: builds for '{device}' but {bridge}.conf says BR_DEVICE={declared}")
        self.assertEqual(bad, [], "; ".join(bad))


GOOD_FACTS = {
    "facts": "yes", "wifi_iface": "wlan0", "wifi_addr": "192.168.1.5/24", "wifi_power_save": "off",
    "seg_iface_exists": "yes", "seg_carrier": "1", "seg_addr": "10.99.1.1/24", "seg_usb_speed": "480",
    "leases_file_nonempty": "yes", "leases_ping": "",
    "ts_backend": "Running", "ts_online": "true", "ts_tags": '["tag:bridge"]', "ts_routes": '["10.99.1.0/24"]',
    "svc_wk-bridge-dhcp": "running", "svc_wk-bridge-nftables": "running", "svc_wk-bridge-netwatch": "running",
    "svc_wk-bridge-usb-host": "running", "svc_sshd": "running", "svc_chrony": "running", "svc_nm": "running",
    "svc_tailscale": "running", "nft_table": "yes", "ip_forward": "1",
    "resolv_exists": "yes", "resolv_nameservers": "1", "resolv_first": "nameserver 1.1.1.1", "dns_resolves": "yes",
    "clock_year": "2026", "clock_iso": "2026-09-24 00:00 UTC", "sshd_password_auth": "no",
    "watchdog_device": "yes", "watchdog_fed": "yes", "swap_nonzram": "0", "crashed": "", "uptime": "up 1 day",
    "battery": "",
}


def bridge_conf(name="tailnet-bridge-generic", **over):
    conf = {"KIND": "bridge", "BR_DEVICE": "pinephone", "BR_SEGMENT": "10.99.1.0/24", "BR_IF": "lan0",
            "BR_EGRESS": "none", "BR_CAMERA": "off", "BR_SSH": name, "BR_HOSTNAME": name, "BR_USER": "user"}
    conf.update(over)
    return bridge.BridgeConf(name, conf)


class TestClassify(unittest.TestCase):
    def test_a_refused_key_is_key_changed(self):
        """an unknown host key is not reported as an absent phone"""
        self.assertEqual(bridge.classify_ssh_error("Host key verification failed."), "key-changed")

    def test_a_changed_identification_banner_is_key_changed(self):
        self.assertEqual(bridge.classify_ssh_error(
            "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
            "Host key for tailnet-bridge-generic has changed and you have requested strict checking.\n"
            "Host key verification failed."), "key-changed")

    def test_an_unroutable_host_is_unreachable(self):
        self.assertEqual(bridge.classify_ssh_error("ssh: connect to host x port 22: No route to host"), "unreachable")


class TestSegmentDownVsOff(unittest.TestCase):
    """`unit bridge.segment_down_vs_off`: a bridge whose segment is down (cable unplugged, dock
    fine) reads differently from one that is off (the dock never enumerated at all)."""

    def test_a_missing_interface_is_off(self):
        self.assertEqual(bridge.segment_state({"seg_iface_exists": "no"}), "off")

    def test_an_interface_with_no_carrier_is_down(self):
        self.assertEqual(bridge.segment_state({"seg_iface_exists": "yes", "seg_carrier": "0"}), "down")

    def test_a_carrier_is_up(self):
        self.assertEqual(bridge.segment_state({"seg_iface_exists": "yes", "seg_carrier": "1"}), "up")


class TestJudge(unittest.TestCase):
    def test_every_check_passing_reports_no_failure(self):
        report = bridge.judge(GOOD_FACTS, bridge_conf())
        self.assertFalse(report.failed, report.rows)

    def test_no_carrier_fails_and_names_the_cable(self):
        report = bridge.judge(dict(GOOD_FACTS, seg_carrier="0"), bridge_conf())
        self.assertTrue(report.failed)
        self.assertTrue(any("no carrier" in t for lvl, t in report.rows if lvl == "bad"))

    def test_a_missing_interface_fails_and_names_the_dock_not_the_cable(self):
        facts = dict(GOOD_FACTS, seg_iface_exists="no", seg_typec_role="")
        facts.pop("seg_carrier", None)
        report = bridge.judge(facts, bridge_conf())
        self.assertTrue(report.failed)
        self.assertTrue(any("missing" in t for lvl, t in report.rows if lvl == "bad"))
        self.assertFalse(any("no carrier" in t for lvl, t in report.rows if lvl == "bad"))

    def test_an_unapproved_route_fails_by_segment_name(self):
        report = bridge.judge(dict(GOOD_FACTS, ts_routes="none"), bridge_conf())
        self.assertTrue(report.failed)
        self.assertTrue(any("no approved subnet route" in t for lvl, t in report.rows if lvl == "bad"))

    def test_camera_is_only_reported_when_declared(self):
        off = bridge.judge(GOOD_FACTS, bridge_conf())
        self.assertFalse(any(t == "Camera" for lvl, t in off.rows if lvl == "hdr"))
        on = bridge.judge(dict(GOOD_FACTS, camera_device="no"), bridge_conf(BR_CAMERA="http"))
        self.assertTrue(any(t == "Camera" for lvl, t in on.rows if lvl == "hdr"))


MACHINES_ENV = {"WK_MACHINES_DIR": str(REPO / "machines"), "HOME": "/nonexistent-wk-bridge-test-home",
                 "PATH": os.environ.get("PATH", "")}


def react_true(argv, fake):
    return Result(0) if argv[-1] == "true" else Result(255, "", "no answer registered for %r" % (argv,))


class TestResolve(unittest.TestCase):
    def bridge_with(self, machines_dir, reactor):
        fake = Fake("phone")
        fake.react(["ssh"], reactor)
        return bridge.Bridge(REPO, env=dict(MACHINES_ENV, WK_MACHINES_DIR=str(machines_dir)), machine=fake)

    def test_the_conf_name_resolves_without_discovery(self):
        b = self.bridge_with(REPO / "machines", react_true)
        self.assertEqual(b.resolve("tailnet-bridge-generic"), "root@tailnet-bridge-generic")

    def test_at_tries_root_then_the_declared_user(self):
        def reactor(argv, fake):
            return Result(0) if argv[-2].startswith("user@") else Result(255, "", "refused")
        b = self.bridge_with(REPO / "machines", reactor)
        self.assertEqual(b.resolve("tailnet-bridge-generic", at="10.0.0.9"), "user@10.0.0.9")

    def test_an_unreachable_bridge_names_the_provision_command(self):
        def reactor(argv, fake):
            return Result(255, "", "ssh: connect to host x port 22: No route to host")
        b = self.bridge_with(REPO / "machines", reactor)
        with self.assertRaises(bridge.Unreachable) as ctx:
            b.resolve("tailnet-bridge-generic", names_only=True)
        self.assertIn("wk machine setup tailnet-bridge-generic --disk", str(ctx.exception))

    def test_an_unknown_name_is_refused_by_name(self):
        b = self.bridge_with(REPO / "machines", react_true)
        with self.assertRaises(LookupError):
            b.resolve("not-a-bridge")

    def test_the_phone_is_found_on_reachs_sweep_by_its_own_hostname(self):
        def reactor(argv, fake):
            if argv[-1] == "true":
                return Result(0) if "10.0.0.7" in argv[-2] else Result(255, "", "refused")
            return Result(0, "tailnet-bridge-generic\n" if "10.0.0.7" in argv[-2] else "other\n")
        b = self.bridge_with(REPO / "machines", reactor)
        b.machine.answer(["ip", "-4", "-o", "addr", "show"], out="2: en0 inet 10.0.0.4/24 brd x\n")
        b.machine.answer(["sh", "-c", reach.SWEEP], out="10.0.0.5 dev en0 lladdr aa:00:00:00:00:05 REACHABLE\n"
                                                         "10.0.0.7 dev en0 lladdr aa:00:00:00:00:07 STALE\n")
        b.machine.answer(["nc"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.resolve("tailnet-bridge-generic"), "root@10.0.0.7")
        self.assertFalse([e for e in b.machine.effects if e[1][:1] == ("dig",) or any(".local" in w for w in e[1])])

    def test_a_machine_that_cannot_sweep_says_so_and_names_at(self):
        def reactor(argv, fake):
            return Result(255, "", "ssh: connect to host x port 22: No route to host")
        b = self.bridge_with(REPO / "machines", reactor)
        b.machine.answer(["ip", "-4", "-o", "addr", "show"], rc=127)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(bridge.Unreachable) as ctx:
            b.resolve("tailnet-bridge-generic")
        self.assertIn("--at", err.getvalue())
        self.assertIn("--at <address>", str(ctx.exception))


class TestLsRow(unittest.TestCase):
    def row(self, reactor):
        fake = Fake("phone")
        fake.react(["ssh"], reactor)
        b = bridge.Bridge(REPO, env=MACHINES_ENV, machine=fake)
        return b.ls_row("tailnet-bridge-generic")

    def test_a_provisioned_bridge_is_reported_provisioned(self):
        def reactor(argv, fake):
            return Result(0) if argv[-1] in ("true", "test -e /etc/wk-bridge.conf") else Result(255, "", "")
        self.assertEqual(self.row(reactor)["state"], "provisioned")

    def test_an_up_bridge_with_no_role_is_bare(self):
        def reactor(argv, fake):
            return Result(0) if argv[-1] == "true" else Result(1, "", "")
        self.assertEqual(self.row(reactor)["state"], "bare")

    def test_a_refused_key_is_reported_key_changed_not_unreachable(self):
        def reactor(argv, fake):
            return Result(255, "", "Host key verification failed.")
        self.assertEqual(self.row(reactor)["state"], "key-changed")


class TestBattery(unittest.TestCase):
    def test_reads_percent_status_and_cap_from_the_phone(self):
        def reactor(argv, fake):
            if argv[-1] == "true":
                return Result(0)
            return Result(0, "percent=72\nstatus=Charging\nlimit=80\ncurrent=80\n")
        fake = Fake("phone")
        fake.react(["ssh"], reactor)
        rendered = bridge.Bridge(REPO, env=MACHINES_ENV, machine=fake).battery("tailnet-bridge-generic")
        self.assertIn("percent=72", rendered)
        self.assertIn("status=Charging", rendered)
        self.assertIn("limit=80", rendered)


KEY = "tskey-auth-k1-secret"
BMC = "tailnet-bridge-moose-bmc"
FACTS = ("uplink=wlan0\nlan_dev=eth0\nlan_mac=9C:6B:00:00:00:01\nbattery_nodes=/sys/class/power_supply/bq25890\n"
         "watchdog=yes\ntun=yes\nelogind=yes\nswap_total=1\nswap_nonzram=0\ninit=networkmanager chronyd tailscale nftables\n")
JOINED = "/var/lib/tailscale/joined"
AUTHKEY_FILE, API_FILE = "/keys/tailscale-authkey", "/keys/tailscale-api-key"
SECRETFILE = str(REPO / "lib" / "secretfile.py")
ROUTES = "/var/lib/tailscale/routes"


class Phone(Fake):
    """The phone: the bundle and `sh -s` scripts arrive on stdin, which a plain Fake does not keep."""

    def run(self, argv, input=None, timeout=None):
        self.stdin = input
        return super().run(argv, input, timeout)


class PhoneWorld:
    """This machine (`here`) and the phone (`fake`, whose files are the state), answering as a pmOS phone
    that applies what it is sent: the bundle is really unpacked and the manifest really followed."""

    def __init__(self, uid="0", joined=False, approves=True, doas_password=False, apk=True, facts=FACTS, env=None):
        self.here, self.fake, self.clock = Fake("here"), Phone("phone"), FakeClock()
        self.approves, self.root_ok = approves, uid == "0"
        self.env = dict(MACHINES_ENV, WK_ROOT=str(REPO), WK_TS_AUTHKEY=AUTHKEY_FILE, WK_TS_API_SECRET=API_FILE)
        self.env.update(env or {})
        h, f = self.here, self.fake
        h.react(["ssh"], lambda a, fk: Result(0) if a[-1] == "true" and (self.root_ok or not a[-2].startswith("root@"))
                else Result(255, "", "refused"))
        h.react(["sh", "-c", role.PAUSED], self.give_root)
        self.stored(authkey=KEY)
        f.answer(["id", "-u"], out=uid + "\n")
        f.answer(["sh", "-c", "command -v apk"], rc=0 if apk else 1)
        f.answer(["sh", "-c", "command -v doas"])
        f.answer(["doas", "-n", "true"], rc=1 if doas_password else 0)
        f.react(["sh", "-c", role.SHIP], self.ship)
        f.react(["sh", LIB + "/provision.sh", "base"], self.apply(("file",)))
        f.react(["sh", LIB + "/provision.sh", "role"], self.apply(("file", "service", "enable", "drop")))
        f.react(["sh", "-s"], lambda a, fk: Result(0, facts) if fk.stdin == plan.FACTS else self.deprovision(fk))
        f.answer(["tailscale", "status"])
        f.react(["tailscale", "status", "--json"], self.ts_json)
        f.react(["tailscale", "set"], self.ts_set)
        f.react(["tailscale", "up"], self.ts_up)
        f.react(["sh", "-c", "umask 077; cat > " + AUTHKEY], lambda a, fk: (fk._set_file(AUTHKEY, fk.stdin), Result(0))[1])
        f.react(["rm", "-f", AUTHKEY], lambda a, fk: (fk._drop(AUTHKEY), Result(0))[1])
        f.react(["test", "-f", "/etc/wk-bridge.conf"], lambda a, fk: Result(0 if "/etc/wk-bridge.conf" in fk.files else 1))
        f.answer([bridge.HEALTHCHECK], out="".join("%s=%s\n" % kv for kv in GOOD_FACTS.items()).replace("10.99.1.0", "10.99.0.0"))
        if joined:
            f.files.update({JOINED: "1", ROUTES: "10.99.0.0/24" if approves else ""})

    def stored(self, authkey="", api=""):
        """This machine's two tailnet credentials, as lib/secretfile.py reads them."""
        self.here.answer(["python3", SECRETFILE, "read", AUTHKEY_FILE], out=authkey and authkey + "\n")
        self.here.answer(["python3", SECRETFILE, "read", API_FILE], out=api and api + "\n")

    def give_root(self, argv, fake):
        self.root_ok = True
        self.fake.answers.append((("id", "-u"), Result(0, "0\n")))
        return Result(0)

    def ship(self, argv, fake):
        fake._drop(LIB)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(fake.stdin)), mode="r:gz") as t:
            for m in t.getmembers():
                fake._set_file(LIB + "/" + m.name, t.extractfile(m).read().decode())
        return Result(0)

    def apply(self, verbs):
        def react(argv, fake):
            for line in fake.files[LIB + "/manifest"].splitlines():
                verb, *rest = line.split()
                if verb not in verbs:
                    continue
                if verb == "file":
                    fake._set_file(rest[1], fake.files[LIB + "/files" + rest[1]])
                elif verb == "service":
                    fake._set_file("/etc/init.d/" + rest[0], fake.files[LIB + "/init.d/" + rest[0]])
                elif verb == "drop":
                    fake._drop("/etc/init.d/" + rest[0])
                if verb in ("service", "enable"):
                    fake._set_file("/etc/runlevels/default/" + rest[0], "")
            return Result(0, "==> Done\n")
        return react

    def deprovision(self, fake):
        """Follows the script's own `for s in`, `rm -f` and `rm -rf` lines, and `tailscale logout`."""
        for line in fake.stdin.splitlines():
            words = [w.rstrip(";") for w in shlex.split(line)]
            if words[:3] == ["for", "s", "in"]:
                for s in words[3:-1]:
                    fake._drop("/etc/init.d/" + s)
                    fake._drop("/etc/runlevels/default/" + s)
            elif words[:1] == ["rm"]:
                for pat in (w for w in words[1:] if w.startswith("/")):
                    for path in [p for p in list(fake.files) + list(fake.dirs) if fnmatch.fnmatchcase(p, pat)]:
                        fake._drop(path)
            elif words[:2] == ["tailscale", "logout"]:
                fake._drop(JOINED)
                fake._drop(ROUTES)
        return Result(0, "the bridge role is gone\n")

    def ts_json(self, argv, fake):
        joined = JOINED in fake.files
        return Result(0, json.dumps({"BackendState": "Running" if joined else "NeedsLogin",
                                     "Self": {"Tags": ["tag:bridge"] if joined else None,
                                              "PrimaryRoutes": [r for r in fake.files.get(ROUTES, "").split() if r]}}))

    def ts_set(self, argv, fake):
        route = argv[2].split("=", 1)[1] if argv[2].startswith("--advertise-routes=") else None
        if route is not None:
            fake._set_file(ROUTES, route if self.approves else "")
        return Result(0)

    def ts_up(self, argv, fake):
        if fake.files.get(AUTHKEY, "").strip() != KEY:
            return Result(1, "", "no auth key")
        fake._set_file(JOINED, "1")
        fake._set_file(ROUTES, "10.99.0.0/24" if self.approves else "")
        return Result(0)

    def role(self, transport=None):
        return role.Role(REPO, env=self.env, here=self.here, phones=lambda dest: self.fake, clock=self.clock,
                         transport=transport or self.no_tailnet_api)

    @staticmethod
    def no_tailnet_api(method, url, headers, data):
        raise AssertionError("asked the tailnet API %s %s" % (method, url))

    def state(self):
        return dict(self.fake.files)

    def argvs(self):
        return [e[1] for e in self.fake.effects if e[0] == "run"]


def quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(*a, **kw)
        except act.Refused as e:
            rc = e.status
    return rc, out.getvalue() + err.getvalue()


class RoleTest(unittest.TestCase):
    """A setup marks itself asked (WK_CONFIRMED) in os.environ; each test gets a clean copy back."""

    def setUp(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for v in ("WK_DRY_RUN", "WK_CONFIRMED", "WK_DESTRUCTIVE", "WK_YES"):
            os.environ.pop(v, None)


class TestPlan(unittest.TestCase):
    def conf(self, name=BMC, **over):
        return dict(bridge.Bridge(REPO, env=MACHINES_ENV).conf(name).conf, **over)

    def test_every_declared_bridge_renders_both_bundles(self):
        for f in real_confs("bridge"):
            with self.subTest(bridge=f.stem):
                c = dict(bridge.Bridge(REPO, env=dict(MACHINES_ENV, WK_MACHINES_DIR=str(f.parent))).conf(f.stem).conf)
                for facts in (None, bridge.kv(FACTS)):
                    p = plan.Plan(f.stem, c, facts)
                    self.assertTrue(plan.bundle(REPO, p))

    def test_rm_removes_every_path_a_setup_can_render(self):
        w = PhoneWorld()
        script = w.role().deprovision(w.role().b.conf(BMC))
        rendered = plan.Plan(BMC, self.conf(), bridge.kv(FACTS)).paths()
        self.assertTrue(rendered)
        for path in rendered:
            self.assertIn(path, script)

    def test_nat_egress_masquerades_and_serves_dns(self):
        p = plan.Plan(BMC, self.conf(BR_EGRESS="nat"), bridge.kv(FACTS))
        files = {path: text for path, _m, text in p.files}
        self.assertIn("masquerade", files["/etc/nftables.d/wk-bridge.nft"])
        self.assertIn("dhcp-option=option:dns-server,10.99.0.1", files["/etc/dnsmasq.d/wk-bridge.conf"])

    def test_no_egress_forwards_nothing_out_and_serves_no_dns(self):
        files = {path: text for path, _m, text in plan.Plan(BMC, self.conf(), bridge.kv(FACTS)).files}
        self.assertNotIn("masquerade", files["/etc/nftables.d/wk-bridge.nft"])
        self.assertIn("port=0\n", files["/etc/dnsmasq.d/wk-bridge.conf"])

    def test_no_adapter_means_no_rename_and_a_warning(self):
        p = plan.Plan(BMC, self.conf(), dict(bridge.kv(FACTS), lan_mac=""))
        self.assertFalse([x for x in p.paths() if "udev" in x or "nmconnection" in x])
        self.assertTrue(any("no USB ethernet adapter" in w for w in p.warnings))

    def test_the_conf_mac_wins_over_the_detected_one_lowercased(self):
        p = plan.Plan(BMC, self.conf(BR_LAN_MAC="AA:BB:CC:DD:EE:FF"), bridge.kv(FACTS))
        self.assertIn("BR_LAN_MAC=aa:bb:cc:dd:ee:ff", p.env())

    def test_a_pinned_battery_node_is_the_only_one_taken(self):
        facts = dict(bridge.kv(FACTS), battery_nodes="/sys/class/power_supply/a,/sys/class/power_supply/b")
        self.assertEqual(plan.Plan(BMC, self.conf(BR_BATTERY="b"), facts).battery, "/sys/class/power_supply/b")
        missing = plan.Plan(BMC, self.conf(BR_BATTERY="c"), facts)
        self.assertEqual(missing.battery, "")
        self.assertNotIn("service wk-bridge-battery", missing.lines)

    def test_camera_off_drops_the_service(self):
        self.assertIn("drop wk-bridge-camera", plan.Plan(BMC, self.conf(BR_CAMERA="off"), bridge.kv(FACTS)).lines)
        self.assertIn("service wk-bridge-camera", plan.Plan(BMC, self.conf(), bridge.kv(FACTS)).lines)

    def test_the_packaged_names_come_from_the_phone(self):
        lines = plan.Plan(BMC, self.conf(), dict(bridge.kv(FACTS), init="NetworkManager chrony tailscaled")).lines
        self.assertEqual([l for l in lines if l.split()[0] in ("enable", "start", "restart", "disable")],
                         ["enable NetworkManager", "enable chrony", "restart chrony", "enable sshd",
                          "enable tailscaled", "start tailscaled"])

    def test_a_phone_with_no_tailscale_service_is_refused(self):
        with self.assertRaises(LookupError):
            plan.Plan(BMC, self.conf(), dict(bridge.kv(FACTS), init="networkmanager"))

    def test_the_bundle_is_the_same_bytes_for_the_same_inputs(self):
        p = plan.Plan(BMC, self.conf(), bridge.kv(FACTS))
        self.assertEqual(plan.bundle(REPO, p), plan.bundle(REPO, plan.Plan(BMC, self.conf(), bridge.kv(FACTS))))


class TestSetup(RoleTest):
    def test_setup_applies_the_role_and_joins_with_the_key(self):
        w = PhoneWorld()
        rc, out = quiet(w.role().setup, BMC)
        self.assertEqual(rc, 0, out)
        self.assertIn("/etc/wk-bridge.conf", w.fake.files)
        self.assertIn("/etc/init.d/wk-bridge-dhcp", w.fake.files)
        self.assertIn("/etc/udev/rules.d/70-wk-bridge-net.rules", w.fake.files)
        self.assertIn(JOINED, w.fake.files)
        self.assertNotIn(AUTHKEY, w.fake.files)
        self.assertIn('"autoApprovers"', out)

    def test_the_auth_key_never_reaches_argv(self):
        w = PhoneWorld()
        quiet(w.role().setup, BMC)
        self.assertFalse([a for a in w.argvs() + [e[1] for e in w.here.effects if e[0] == "run"] if KEY in " ".join(a)])

    def test_a_setup_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[machine setup]` for a bridge: two ships, both applies, the key and the join."""
        converges(self, PhoneWorld, lambda w: quiet(w.role().setup, BMC), PhoneWorld.state)

    def test_a_dry_run_changes_nothing_on_the_phone(self):
        w = PhoneWorld()
        before = w.state()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            rc, out = quiet(w.role().setup, BMC)
        self.assertEqual(rc, 0, out)
        self.assertEqual(w.state(), before)
        self.assertIn("would run on phone: sh %s/provision.sh role" % LIB, out)

    def test_no_tailnet_hands_over_no_key_and_does_not_join(self):
        w = PhoneWorld()
        rc, out = quiet(w.role().setup, BMC, no_tailnet=True)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(JOINED, w.fake.files)
        self.assertFalse([a for a in w.argvs() if a[:2] == ("tailscale", "up") or AUTHKEY in " ".join(a[2:3])])
        self.assertIn("wk machine tailnet %s" % BMC, out)

    def test_a_joined_node_reasserts_its_route_and_never_logs_in_again(self):
        w = PhoneWorld(joined=True)
        rc, out = quiet(w.role().setup, BMC)
        self.assertEqual(rc, 0, out)
        self.assertIn(("tailscale", "set", "--advertise-routes=10.99.0.0/24", "--accept-dns=false", "--ssh=true"), w.argvs())
        self.assertFalse([a for a in w.argvs() if a[:2] == ("tailscale", "up")])

    def test_an_unapproved_route_is_withdrawn_and_readvertised(self):
        w = PhoneWorld(joined=True, approves=False)
        rc, out = quiet(w.role().setup, BMC)
        self.assertEqual(rc, 0, out)
        self.assertIn(("tailscale", "set", "--advertise-routes="), w.argvs())
        self.assertEqual(w.clock.slept[-2:], [2, 5])
        self.assertIn("still not approved", out)

    def test_a_stored_key_the_tailnet_still_has_joins(self):
        """the fleet key, checked through the tailnet API's transport by the machine that holds the API credential"""
        w = PhoneWorld()
        w.stored(authkey=KEY, api="tskey-api-a1-secret")
        asked = []

        def tailnet_api(method, url, headers, data):
            asked.append((method, url.rsplit("/api/v2", 1)[-1]))
            return 200, json.dumps({"keys": [{"id": KEY.split("-")[2]}]}).encode()
        rc, out = quiet(w.role(tailnet_api).setup, BMC)
        self.assertEqual(rc, 0, out)
        self.assertIn(JOINED, w.fake.files)
        self.assertEqual(asked, [("GET", "/tailnet/-/keys")])

    def test_no_key_applies_the_role_and_says_so(self):
        w = PhoneWorld()
        w.stored()
        rc, out = quiet(w.role().setup, BMC)
        self.assertEqual(rc, 0, out)
        self.assertIn("/etc/wk-bridge.conf", w.fake.files)
        self.assertNotIn(JOINED, w.fake.files)
        self.assertFalse([a for a in w.argvs() if a[:2] == ("tailscale", "up")])

    def test_a_password_doas_gets_root_the_key_and_carries_on_as_root(self):
        w = PhoneWorld(uid="1000", doas_password=True)
        rc, out = quiet(w.role().setup, BMC)
        self.assertEqual(rc, 0, out)
        (bootstrap,) = [e[1] for e in w.here.effects if e[0] == "run" and e[1][:3] == ("sh", "-c", role.PAUSED)]
        self.assertEqual(bootstrap[4:6], ("ssh", "-tt"))
        self.assertIn(BMC, bootstrap)
        self.assertIn("/etc/wk-bridge.conf", w.fake.files)

    def test_a_phone_without_apk_is_refused_before_anything_is_sent(self):
        w = PhoneWorld(apk=False)
        rc, out = quiet(w.role().setup, BMC)
        self.assertEqual(rc, 1)
        self.assertIn("not running postmarketOS", out)
        self.assertFalse([e for e in w.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c")
                          and e[1][2] == role.SHIP])

    def test_an_unknown_name_is_refused_by_name(self):
        rc, out = quiet(PhoneWorld().role().setup, "not-a-bridge")
        self.assertEqual(rc, 1)
        self.assertIn("not a declared bridge", out)


class TestTailnetVerb(RoleTest):
    def test_a_phone_with_no_role_is_sent_to_setup(self):
        w = PhoneWorld()
        rc, out = quiet(w.role().tailnet, BMC)
        self.assertEqual(rc, 1)
        self.assertIn("--no-tailnet", out)

    def test_a_role_without_the_tailnet_joins(self):
        w = PhoneWorld()
        quiet(w.role().setup, BMC, no_tailnet=True)
        rc, out = quiet(w.role().tailnet, BMC)
        self.assertEqual(rc, 0, out)
        self.assertIn(JOINED, w.fake.files)


class TestRm(RoleTest):
    def test_rm_asks_first_and_a_no_changes_nothing(self):
        w = PhoneWorld()
        quiet(w.role().setup, BMC)
        before = w.state()
        with mock.patch.object(act, "confirm", return_value=False):
            rc, _out = quiet(w.role().rm, BMC)
        self.assertEqual(rc, 1)
        self.assertEqual(w.state(), before)

    def test_rm_leaves_nothing_setup_made_but_the_os_services_it_enabled(self):
        w = PhoneWorld()
        before = set(w.state())
        quiet(w.role().setup, BMC)
        with mock.patch.object(act, "confirm", return_value=True):
            rc, out = quiet(w.role().rm, BMC)
        self.assertEqual(rc, 0, out)
        os_services = {"/etc/runlevels/default/" + s for s in ("networkmanager", "chronyd", "sshd", "tailscale")}
        self.assertEqual(set(w.state()) - before - os_services, set())


DISK = "rpi5:/dev/sda"


class TestProvision(RoleTest):
    """`wk machine setup <bridge> --disk`: `wk sysimage write` is the one writer, then the wait and the role."""

    def world(self, newest=("bridge-librem5-1",)):
        w = PhoneWorld()
        self.wk = str(REPO / "wk")
        w.here.answer([self.wk, "sysimage"])
        self.fetched = []

        def fetch_out(host, env, id_, dest):
            self.fetched.append(dest)
            return dest
        for patch in (mock.patch.object(pmos, "newest_out", side_effect=list(newest)),
                      mock.patch.object(pmos, "fetch_out", side_effect=fetch_out)):
            patch.start()
            self.addCleanup(patch.stop)
        return w

    def children(self, w):
        return [e[1][1:] for e in w.here.effects if e[0] == "run_tty"]

    def test_a_write_asks_once_hands_the_disk_to_sysimage_write_and_applies_the_role(self):
        w = self.world()
        with mock.patch.dict(os.environ, {"WK_YES": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.children(w), [("sysimage", "write", "--from", self.fetched[0], "--disk", DISK, "--yes")])
        self.assertEqual(os.path.dirname(self.fetched[0]), provision.image_dir(Store(w.env)))
        self.assertIn(("remove", self.fetched[0]), w.here.effects)
        self.assertIn("/etc/wk-bridge.conf", w.fake.files)

    def test_the_hands_on_steps_name_the_phones_own_kill_switches(self):
        w = self.world()
        with mock.patch.dict(os.environ, {"WK_YES": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK)
        self.assertIn(bridge.devices(REPO)["librem5"][1], out)

    def test_no_terminal_and_no_yes_writes_nothing(self):
        w = self.world()
        before = w.state()
        rc, out = quiet(w.role().setup, BMC, disk=DISK)
        self.assertEqual(rc, 1, out)
        self.assertEqual((self.children(w), w.state()), ([], before))

    def test_a_dry_run_runs_no_child_and_waits_for_no_phone(self):
        w = self.world()
        before = w.state()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK)
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.children(w), w.state(), w.clock.slept), ([], before, []))
        self.assertIn("would run: %s sysimage write --from" % self.wk, out)

    def test_no_finished_build_builds_one_before_writing_it(self):
        w = self.world(newest=(None, "bridge-librem5-2"))
        with mock.patch.dict(os.environ, {"WK_YES": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK)
        self.assertEqual(rc, 0, out)
        self.assertEqual([c[:2] for c in self.children(w)], [("sysimage", "build"), ("sysimage", "write")])

    def test_rebuild_builds_even_with_a_finished_build(self):
        w = self.world(newest=("bridge-librem5-2",))
        with mock.patch.dict(os.environ, {"WK_YES": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK, rebuild=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.children(w)[0], ("sysimage", "build", "bridge-librem5"))

    def test_image_is_written_as_given_and_never_removed(self):
        w = self.world()
        with mock.patch.dict(os.environ, {"WK_YES": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK, image="/imgs/phone.img")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.children(w), [("sysimage", "write", "--from", "/imgs/phone.img", "--disk", DISK, "--yes")])
        self.assertEqual(self.fetched, [])

    def test_a_failed_write_stops_before_the_phone(self):
        w = self.world()
        w.here.answer([self.wk, "sysimage", "write"], rc=1)
        with mock.patch.dict(os.environ, {"WK_YES": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("/etc/wk-bridge.conf", w.fake.files)
        self.assertIn(("remove", self.fetched[0]), w.here.effects)

    def test_a_copy_that_fails_is_removed(self):
        w = self.world()
        with mock.patch.object(pmos, "fetch_out", side_effect=act.Refused(1)), mock.patch.dict(os.environ, {"WK_YES": "1"}):
            rc, out = quiet(w.role().setup, BMC, disk=DISK)
        self.assertEqual(rc, 1, out)
        path = os.path.join(provision.image_dir(Store(w.env)), BMC + ".img")
        self.assertIn(("remove", path), w.here.effects)
        self.assertEqual(self.children(w), [])

    def test_a_copy_a_kill_left_is_rubble_and_one_being_written_is_kept(self):
        here, store = Fake("here"), Store(MACHINES_ENV)
        d = provision.image_dir(store)
        here.dirs.add(d)
        here.files[os.path.join(d, BMC + ".img")] = "x"
        here.answer(["du"], out="4096\t-\n")
        lock = Lock(store, here, FakeClock())
        (row,) = provision.rubble(store, here, lock)
        self.assertEqual((row.kind, row.kb, row.why), ("bridge-image", 4096, ""))
        row.take()
        self.assertEqual(here.listdir(d), [])
        here.files[os.path.join(d, BMC + ".img")] = "x"
        with lock.held(provision.image_lock(BMC)):
            here.pids.add(os.getpid())
            (row,) = provision.rubble(store, here, lock)
        self.assertTrue(row.why)

    def test_image_and_rebuild_need_a_disk_and_contradict_each_other(self):
        w = self.world()
        for kw in ({"image": "/x.img"}, {"rebuild": True}, {"disk": DISK, "image": "/x.img", "rebuild": True}):
            with self.subTest(**kw):
                self.assertEqual(quiet(w.role().setup, BMC, **kw)[0], 1)
        self.assertEqual(self.children(w), [])

    def test_a_disk_that_is_not_machine_colon_device_is_refused(self):
        w = self.world()
        with mock.patch.dict(os.environ, {"WK_YES": "1"}):
            self.assertEqual(quiet(w.role().setup, BMC, disk="/dev/sda")[0], 1)
        self.assertEqual(self.children(w), [])

    def test_the_wait_tries_the_names_each_tick_and_sweeps_once_a_minute(self):
        w = self.world()
        r = w.role()
        calls = []

        def resolve(name, at=None, names_only=False):
            calls.append((r.clock.monotonic(), names_only))
            if len(calls) < 40:
                raise bridge.Unreachable("not yet")
            return "root@phone"
        with mock.patch.object(r.b, "resolve", side_effect=resolve):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(provision.Write(r).wait(r.b.conf(BMC), None), "root@phone")
        sweeps = [t for t, names_only in calls if not names_only]
        self.assertEqual(sweeps[0], calls[0][0])
        self.assertTrue(all(b - a >= provision.DISCOVER_EVERY for a, b in zip(sweeps, sweeps[1:])), sweeps)

    def test_a_phone_that_never_answers_is_refused_once_the_wait_is_spent(self):
        w = self.world()
        r = w.role()
        start = r.clock.monotonic()
        with mock.patch.object(r.b, "resolve", side_effect=bridge.Unreachable("no")):
            rc, out = quiet(provision.Write(r).wait, r.b.conf(BMC), None)
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(r.clock.monotonic() - start, provision.PHONE_WAIT)
        self.assertIn("wk machine setup %s" % BMC, out)


class TestDevices(unittest.TestCase):
    def test_every_declared_bridge_names_a_device_in_the_table(self):
        known = bridge.devices(REPO)
        for f in real_confs("bridge"):
            with self.subTest(bridge=f.stem):
                self.assertIn(bridge.Bridge(REPO, env=MACHINES_ENV).conf(f.stem).device, known)

    def test_an_unknown_device_is_refused_by_name(self):
        w = PhoneWorld()
        r = w.role()
        conf = dict(r.b.conf(BMC).conf, BR_DEVICE="nokia")
        with mock.patch.object(r.b, "conf", return_value=bridge.BridgeConf(BMC, conf)):
            rc, out = quiet(r.setup, BMC)
        self.assertEqual(rc, 1)
        self.assertIn("nokia", out)


class TestSegmentOnTheRealBridges(unittest.TestCase):
    wk_tier = "live"

    def test_the_segment_behind_each_bridge_is_up(self):
        """`live bridge.segment[<bridge>]`: its health check passes, the cable is connected and each reserved lease answers."""
        names = [f.stem for f in real_confs("bridge") if machine_reachable(f.stem)] if live_selected() else []
        if not names:
            self.skipTest("live tier not selected, or no bridge answers over ssh")
        for name in names:
            with self.subTest(bridge=name):
                cp = subprocess.run([str(WK), "machine", "status", name], cwd=str(REPO), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=120)
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn("cable connected", cp.stdout)


class TestSetupOnTheRealPhone(unittest.TestCase):
    wk_tier = "live"

    def test_setup_leaves_a_healthy_bridge(self):
        """`live bridge.setup[moose-bmc]`: re-applied from this tree, then its health check passes."""
        if not (live_selected() and machine_reachable(BMC)):
            self.skipTest("live tier not selected, or %s does not answer over ssh" % BMC)
        cp = subprocess.run([str(WK), "machine", "setup", BMC], cwd=str(REPO), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=1800)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("All checks passed.", cp.stdout)


if __name__ == "__main__":
    unittest.main()
