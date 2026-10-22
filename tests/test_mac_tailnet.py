"""The Mac volume builder (lib/wk/sysimage/macvolume.py) and its tailnet half (lib/wk/sysimage/mactailnet.py)
against a Mac in memory: FakeMac answers diskutil, the installer, the Go toolchain and the file tools the way the
Mac does and moves its files, so every step is checked by what it leaves."""
import contextlib
import hashlib
import io
import os
import plistlib
import re
import subprocess
import sys
import types
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, requires_machine, requires_tool, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.act import Refused  # noqa: E402
from tests.fakes import FakeClock  # noqa: E402
from wk.machine import Result  # noqa: E402
from tests.fakes import Fake  # noqa: E402
from wk.sysimage import macvolume, mactailnet  # noqa: E402
from wk.sysimage.macvolume import MacVolume  # noqa: E402
from wk.sysimage.mactailnet import GO, Tailnet  # noqa: E402
from wk.quiet import COMMON, DESKTOP, RENDER, lib_argv as quiet_lib  # noqa: E402

MACHO = bytes.fromhex(mactailnet.MACHO_ARM64.replace(" ", "")) + b"\0" * 24
STORE, STATE, HOME = "/st", "/home/u/.local/state", "/home/u"
ENV = {"HOME": HOME, "XDG_STATE_HOME": STATE, "WK_STORE": STORE, "TMPDIR": "/tmp"}
KEY = "/home/u/.config/wk/tailscale-authkey"
TS_VERSION = re.search(r'^TS_VERSION = "(.*)"$', (REPO / mactailnet.REL).read_text(), re.M).group(1)
ART = Tailnet(Fake(), ENV).artifacts()
OUT = "%s/darwin-arm64-%s" % (ART, TS_VERSION)
VOL, DATA = "/Volumes/WK Bench", "/Volumes/WK Bench - Data"
PRESET = images.load("perf-macos-tolken")
SCRUB = ("WK_DRY_RUN", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_YES", "WK_QUIET", "WK_BENCH_WIRED", "WK_BENCH_VOLUME")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


def blob(data):
    return data if isinstance(data, bytes) else data.encode()


class FakeMac(Fake):
    """A Mac: `downloads` is what each URL serves, `volumes` the diskutil names attached, and `sudo` runs the rest."""

    def __init__(self):
        super().__init__("mac")
        self.downloads = {"https://go.dev/dl/go%s.darwin-arm64.tar.gz" % GO["version"]: b"go-tarball",
                          "https://proxy.golang.org/tailscale.com/@v/v%s.zip" % TS_VERSION: b"ts-zip"}
        self.volumes = {"WK Bench"}
        self.modes = {}
        self.running_on = "Macintosh HD"
        self.free = 500 * 10 ** 9
        self.keychain = {"homenet": "pass word"}
        self.console = []
        self.dirs.update({"/", "/tmp", "/Applications"})
        self._set_file(os.path.join(str(REPO), mactailnet.REL), (REPO / mactailnet.REL).read_text())
        self._set_file(KEY, "tskey-auth-kTESTONLY")
        self._set_file(HOME + "/.ssh/authorized_keys", "ssh-ed25519 AAAA a\n\nssh-ed25519 BBBB b\n")
        self.react((), self.handle)

    def put(self, path, data):
        self._set_file(path, data)

    def mv(self, a, b):
        moved = {p: v for p, v in self.files.items() if p == a or p.startswith(a + "/")}
        dirs = {d for d in self.dirs if d == a or d.startswith(a + "/")}
        self._drop(a)
        for p, v in moved.items():
            self._set_file(b + p[len(a):], v)
        self.dirs |= {b + d[len(a):] for d in dirs}
        for p in [p for p in self.modes if p == a or p.startswith(a + "/")]:
            self.modes[b + p[len(a):]] = self.modes.pop(p)

    def install(self, args):
        mode = None
        if "-m" in args:
            mode = args[args.index("-m") + 1]
        paths = [a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] not in ("-m", "-o", "-g"))]
        if "-d" in args:
            for p in paths:
                self.dirs.add(p)
                self.modes[p] = mode
            return
        *srcs, dest = paths
        for s in srcs:
            if s not in self.files and s.startswith(str(REPO) + "/") and os.path.isfile(s):
                with open(s, "rb") as f:
                    self.files[s] = f.read()
            if s not in self.files:
                raise AssertionError("install of %s, which does not exist" % s)
            to = os.path.join(dest, os.path.basename(s)) if dest.endswith("/") or dest in self.dirs else dest
            if os.path.dirname(to) not in self.dirs:
                raise AssertionError("install into %s, which does not exist" % os.path.dirname(to))
            self._set_file(to, self.files[s])
            self.modes[to] = mode

    def handle(self, argv, fake):
        a = list(argv)
        if a[0] == "sudo":
            a = a[1:]
        cmd = a[0]
        if cmd == "uname":
            return Result(0, {"-s": "Darwin\n", "-m": "arm64\n"}[a[1]])
        if cmd == "od":
            data = blob(self.files.get(a[-1], b""))[:8]
            return Result(0, " " + " ".join("%02x" % b for b in data) + "\n")
        if cmd == "sha256sum":
            return Result(0, "%s  %s\n" % (hashlib.sha256(blob(self.files[a[1]])).hexdigest(), a[1])) if a[1] in self.files else Result(1)
        if cmd == "curl":
            url, dest = a[-1], a[a.index("-o") + 1]
            self._set_file(dest, self.downloads[url])
            return Result(0)
        if cmd == "mv":
            self.mv(a[1], a[2])
            return Result(0)
        if cmd == "rm":
            for p in a[2:]:
                self._drop(p)
            return Result(0)
        if cmd == "install":
            self.install(a[1:])
            return Result(0)
        if cmd == "chmod":
            for p in a[2:]:
                self.modes[p] = a[1]
            return Result(0)
        if cmd in ("chown", "pkgutil", "fdesetup", "tmutil"):
            return Result(0)
        if cmd == "tar":
            self._set_file(a[a.index("-C") + 1] + "/go/bin/go", "go")
            return Result(0)
        if cmd == "python3" and a[1:3] == ["-m", "zipfile"]:
            self._set_file("%s/tailscale.com@v%s/go.mod" % (a[-1], TS_VERSION), "module tailscale.com")
            return Result(0)
        if cmd == "env":
            out = a[a.index("-o") + 1]
            for n in ("tailscaled", "tailscale"):
                self._set_file(out + n, MACHO)
            return Result(0)
        if cmd == "test":
            return Result(0 if self.files.get(a[-1]) else 1)
        if cmd == "cat":
            return Result(0, self.files[a[1]]) if a[1] in self.files else Result(1)
        if cmd == "python3" and a[1].endswith("secretfile.py") and a[2] == "read":
            return Result(0, self.files.get(a[3], ""))
        if cmd == "diskutil":
            return self.diskutil(a[1:])
        if cmd in ("pkgbuild", "productbuild"):
            self._set_file(a[-1], "pkg")
            return Result(0)
        if cmd == "rsync":
            self.dirs.add(a[-1].rstrip("/"))
            self._set_file(a[-1] + "wk", "#!/usr/bin/env bash")
            return Result(0)
        if cmd == "id":
            return Result(0, "owner\n")
        if cmd in ("softwareupdate", "startosinstall") or cmd.endswith("/startosinstall"):
            self.console.append(a)
            return Result(0)
        if cmd == "networksetup":
            if a[1] == "-listallhardwareports":
                return Result(0, "\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: x\n")
            return Result(0, "Preferred networks on en0:\n\tcafe\n\thomenet\n")
        if cmd == "security":
            psk = self.keychain.get(a[a.index("-a") + 1])
            return Result(0, psk + "\n") if psk else Result(44)
        if cmd == "plutil":
            return Result(0, self.files.get(a[-1], "{}"))
        if cmd == "/usr/libexec/PlistBuddy":
            self.put(a[-1], '{"com.openssh.sshd": false}')
            return Result(0)
        return Result(0)

    def run(self, argv, input=None, timeout=None):
        a = [x for x in argv if x != "sudo"]
        if a[:1] == ["tee"]:
            self.record_run(argv)
            path = a[-1]
            self._set_file(path, (self.files.get(path, "") if "-a" in a else "") + (input or ""))
            return Result(0)
        return super().run(argv, input, timeout)

    def diskutil(self, a):
        if a[:2] == ["info", "-plist"]:
            doc = {"APFSContainerReference": "disk3", "APFSContainerFree": self.free, "VolumeName": self.running_on}
            return Result(0, plistlib.dumps(doc).decode())
        if a[0] == "info":
            return Result(0 if a[1] in self.volumes else 1)
        if a[:2] == ["apfs", "addVolume"]:
            self.volumes.add(a[-1])
            self.dirs.add("/Volumes/" + a[-1])
            return Result(0)
        return Result(1)

    def install_macos(self):
        self.dirs.update({VOL, DATA})
        self.put(VOL + "/System/Library/CoreServices/SystemVersion.plist",
                 plistlib.dumps({"ProductUserVisibleVersion": "26.6"}).decode())

    def runs(self, word):
        return [e[1] for e in self.effects if e[0] in ("run", "run_tty") and any(word in x for x in e[1])]


def quiet(fn, *args, **kw):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args, **kw), err.getvalue()
        except Refused:
            return Refused, err.getvalue()


def tailnet(mac, go=None):
    return Tailnet(mac, ENV, REPO, go=go)


def volume(mac, clock=None, **env):
    return MacVolume(mac, PRESET, dict(ENV, **env), clock or FakeClock(), root=REPO)


def source_sha(mac, sha):
    rel = os.path.join(str(REPO), mactailnet.REL)
    mac.put(rel, re.sub(r'(?m)^TS_SRC_SHA256 = ".*"$', 'TS_SRC_SHA256 = "%s"' % sha, mac.files[rel]))


def built(mac):
    for n in ("tailscaled", "tailscale"):
        mac.put(OUT + "/" + n, MACHO)
    return mac


class TheTailnetHalf(unittest.TestCase):
    def stage(self, mac, root="/vol", sudo=False, machine="mbp"):
        mac.dirs.add(root)
        with clean_env():
            return quiet(tailnet(mac).stage, root, machine, sudo)

    def test_it_fetches_verifies_and_builds_a_darwin_arm64_daemon_once(self):
        mac = FakeMac()
        source_sha(mac, hashlib.sha256(b"ts-zip").hexdigest())
        with clean_env():
            self.assertEqual(quiet(tailnet(mac, dict(GO, darwin_arm64=hashlib.sha256(b"go-tarball").hexdigest())).build)[0], OUT)
        self.assertEqual(blob(mac.files[OUT + "/tailscaled"])[:8], MACHO[:8])
        self.assertTrue({"GOOS=darwin", "GOARCH=arm64", "CGO_ENABLED=0", "-trimpath"} <= set(mac.runs("GOTOOLCHAIN=local")[0]))
        runs = len(mac.effects)
        with clean_env():
            self.assertEqual(quiet(tailnet(mac).build)[0], OUT)
        self.assertFalse([e for e in mac.effects[runs:] if e[0] == "run" and e[1][0] in ("curl", "env")])

    def test_a_download_off_its_pin_or_a_host_with_no_pin_is_refused_and_nothing_is_kept(self):
        for go, said in ((dict(GO, darwin_arm64="0" * 64), "unverified"), ({"version": GO["version"]}, 'GO["darwin_arm64"]')):
            mac = FakeMac()
            with clean_env():
                got, err = quiet(tailnet(mac, go).build)
            self.assertIs(got, Refused)
            self.assertIn(said, err)
            self.assertEqual(([p for p in mac.files if p.endswith(".part")], mac.runs("GOTOOLCHAIN")), ([], []))

    def test_the_stage_installs_the_daemon_its_jobs_a_root_only_key_and_the_machine_s_node_name(self):
        mac = built(FakeMac())
        self.assertIsNot(self.stage(mac)[0], Refused)
        self.assertEqual([(mac.files["/vol/usr/local/bin/" + n], mac.modes["/vol/usr/local/bin/" + n]) for n in ("tailscaled", "tailscale")],
                         [(MACHO, "0755")] * 2)
        self.assertEqual((mac.modes["/vol/private/var/db/wk/tailscale"], mac.modes["/vol/private/etc/wk/tailscale-authkey"]), ("0700", "0600"))
        self.assertEqual(mac.files["/vol/private/etc/wk/tailnet.conf"], "hostname=tolken-bench\ntag=tag:wk\n")
        d = plistlib.loads(blob(mac.files["/vol/Library/LaunchDaemons/com.wk.tailscaled.plist"]))
        self.assertTrue(d["RunAtLoad"] and d["KeepAlive"])
        j = plistlib.loads(blob(mac.files["/vol/Library/LaunchDaemons/com.wk.tailnet-join.plist"]))
        self.assertEqual((j["ProgramArguments"][1:], "KeepAlive" in j), ([mactailnet.PAYLOAD_TOOLS + "/bench/mac-tailnet.sh", "join"], False))
        mac = built(FakeMac())
        self.stage(mac, sudo=True)
        self.assertEqual({r[:2] for r in mac.runs("/vol") if r[0] != "od"}, {("sudo", "install")})

    def test_a_remembered_node_identity_comes_back_on_the_next_volume(self):
        mac = built(FakeMac())
        with clean_env():
            self.assertIn("no node identity", quiet(tailnet(mac).remember, "/vol", "mbp")[1])
        self.stage(mac)
        mac.put("/vol/private/var/db/wk/tailscale/tailscaled.state", "node-key")
        with clean_env():
            quiet(tailnet(mac).remember, "/vol", "mbp")
        self.assertEqual(mac.modes[STATE + "/wk/mac-tailnet/tolken-bench.state"], "0600")
        self.stage(mac, "/vol2")
        self.assertEqual(mac.files["/vol2/private/var/db/wk/tailscale/tailscaled.state"], "node-key")

    def test_no_auth_key_or_no_node_name_refuses_before_anything_is_built(self):
        mac = FakeMac()
        mac._drop(KEY)
        got, err = self.stage(mac)
        self.assertIn("wk key set tailnet", err)
        self.assertEqual(mac.runs("curl") + mac.runs("install"), [])
        self.assertIn("bench_ssh", self.stage(FakeMac(), machine="no-such-machine")[1])

    def test_a_dry_run_changes_nothing_and_a_killed_stage_converges(self):
        mac = built(FakeMac())
        mac.dirs.add("/vol")
        before = dict(mac.files)
        with clean_env(WK_DRY_RUN="1"):
            self.assertIn("would run", quiet(tailnet(mac).stage, "/vol", "mbp")[1])
        self.assertEqual(mac.files, before)

        def world():
            m = built(FakeMac())
            m.dirs.add("/vol")
            return types.SimpleNamespace(fake=m)

        def once(w):
            with clean_env():
                quiet(tailnet(w.fake).stage, "/vol", "mbp")
        converges(self, world, once, lambda w: {p: v for p, v in w.fake.files.items() if p.startswith("/vol/")}, max_effects=80)

    def test_the_install_half_needs_a_runnable_collected_payload_and_nothing_else(self):
        """Root and nothing else: the bench install runs it against itself, with no network and no credentials."""
        for binary, said in ((MACHO, None), (b"#!/bin/sh\n", "Mach-O arm64"), (None, "not a collected tailnet payload")):
            mac = FakeMac()
            mac.dirs.update({"/p", "/r"})
            for f in mactailnet.PAYLOAD if binary else ():
                mac.put("/p/" + f, binary if f.startswith("tailscale") and "." not in f else "x")
            with clean_env():
                got, err = quiet(tailnet(mac).install, "/r", "/p")
            self.assertEqual(got is Refused, said is not None, err)
            self.assertIn(said or "", err)
            self.assertEqual(mac.runs("curl"), [])
        with scratch_dir() as d:
            cp = subprocess.run(["python3", "-m", "wk.sysimage.mactailnet", "install", str(d), str(d)], capture_output=True,
                                text=True, timeout=20, env=dict(os.environ, PYTHONPATH=str(REPO / "lib")))
        self.assertIn("not a collected tailnet payload", cp.stderr)


class TheVolume(unittest.TestCase):
    """`wk sysimage build perf-macos-tolken`: one action a run, on the Mac, done when the install carries the marker."""

    def build(self, mac, *args, clock=None, **env):
        with clean_env(**env):
            return quiet(volume(mac, clock, **env).build, list(args))

    def test_it_is_done_when_the_installed_volume_carries_the_marker(self):
        mac = FakeMac()
        v = volume(mac)
        mac.dirs.add(VOL)
        mac.put(VOL + "/etc/wk-image", "id=x\n")
        self.assertEqual(v.outputs(), [], "an empty volume with a marker on it boots nothing")
        mac.install_macos()
        self.assertEqual(v.outputs(), [VOL + "/etc/wk-image"])

    def test_off_a_mac_or_with_two_actions_it_refuses_and_the_report_changes_nothing(self):
        linux = FakeMac()
        linux.answer(["uname", "-s"], out="Linux\n")
        self.assertIn("on the Mac itself", self.build(linux, "--create")[1])
        self.assertIn("one action at a time", self.build(FakeMac(), "--create", "--repair")[1])
        mac = FakeMac()
        mac.install_macos()
        got, err = self.build(mac)
        self.assertEqual(got, 0)
        self.assertTrue("26.6" in err and "MISSING" in err, err)
        self.assertEqual([e for e in mac.effects if e[0] != "run"], [])

    def test_create_adds_the_volume_once_and_only_with_the_room(self):
        mac = FakeMac()
        mac.volumes, mac.free = set(), 10 ** 9
        self.assertIn("WK_BENCH_NEED_GB", self.build(mac, "--create")[1])
        self.assertIn("need 1 GB", self.build(mac, WK_BENCH_NEED_GB="1")[1])
        mac.free = 500 * 10 ** 9
        self.build(mac, "--create")
        self.build(mac, "--create")
        self.assertEqual(len(mac.runs("addVolume")), 1)

    def installer(self):
        mac = built(FakeMac())
        mac.put("/Applications/Install macOS Tahoe.app/Contents/Resources/startosinstall", "x")
        mac.dirs.add(VOL)
        return mac

    def test_install_keeps_the_node_identity_then_gives_startosinstall_the_package_and_an_owner(self):
        mac = self.installer()
        with mock.patch("sys.stdin", io.StringIO("n\n")):
            self.assertEqual(self.build(mac, "--install")[0], 0)
        self.assertEqual((mac.console, mac.runs("install -m")), ([], []))
        mac.put(VOL + "/private/var/db/wk/tailscale/tailscaled.state", "node-key")
        got, err = self.build(mac, "--install", WK_YES="1", WK_BENCH_ADMIN="other")
        self.assertEqual(got, 0, err)
        order = [e[1] for e in mac.effects if e[0] in ("run", "run_tty")]
        self.assertLess(next(i for i, r in enumerate(order) if r[:2] == ("sudo", "cat")),
                        next(i for i, r in enumerate(order) if r[1].endswith("startosinstall")))
        (argv,) = mac.console
        self.assertEqual([argv[argv.index(k) + 1] for k in ("--volume", "--user", "--installpackage")], [VOL, "other", "/tmp/wk-bench-provision.pkg"])
        self.assertIn("--passprompt", argv)
        mac.install_macos()
        mac.console = []
        self.build(mac, "--install")
        self.assertEqual(mac.console, [], "an installed volume is not installed again")

    def test_the_package_answers_setup_assistant_arms_first_boot_and_stages_the_payload_as_the_user(self):
        mac = built(FakeMac())
        with clean_env():
            quiet(volume(mac).build_pkg)
        root = "/tmp/wk-bench-pkgroot"
        writes = {e[1] for e in mac.effects if e[0] == "write"}
        self.assertTrue({os.path.join(root, f) for f in macvolume.SKIP_SETUP} | {root + macvolume.FIRSTBOOT} <= writes)
        self.assertNotIn("KeepAlive", plistlib.loads(macvolume.FIRSTBOOT_PLIST.encode()))
        dests = {r[-1] for r in mac.runs("install") if r[0] == "install"}
        self.assertTrue({root + "/" + dest for _s, dest, _m in macvolume.PAYLOAD} | {root + "/private/etc/wk/tailscale-authkey"} <= dests)
        self.assertFalse([r for r in mac.runs(root) if r[0] == "sudo"])
        self.assertEqual(mac.modes[root + "/usr/local/share/wk-bench/password"], "0600")

    def test_repair_restages_through_sudo_with_the_keychain_s_network_remote_login_and_first_boot(self):
        mac = built(FakeMac())
        self.assertIn("--install first", self.build(mac, "--repair")[1])
        mac.install_macos()
        dis = DATA + "/private/var/db/com.apple.xpc.launchd/disabled.plist"
        mac.put(dis, "{}")
        for _ in range(2):
            self.assertEqual(self.build(mac, "--repair")[0], 0)
        self.assertEqual([r[-2] for r in mac.runs("PlistBuddy")], ["Add :com.openssh.sshd bool false"])
        self.assertEqual([r for r in mac.runs(VOL + "/usr/local/share/wk-bench/wk-tools/") if "rsync" in r][0][:2], ("sudo", "rsync"))
        self.assertEqual(mac.files[VOL + "/usr/local/share/wk-bench/wifi.conf"], "WIFI_SSID=homenet\nWIFI_PSK='pass word'\n")
        self.assertEqual(mac.modes[VOL + "/usr/local/share/wk-bench/wifi.conf"], "0600")
        self.assertEqual(mac.files[VOL + macvolume.FIRSTBOOT], macvolume.FIRSTBOOT_PLIST)
        self.assertIn(("sudo", "chown", "root:wheel", VOL + macvolume.FIRSTBOOT), mac.runs("chown"))

    def test_repair_of_a_wired_install_copies_no_wifi_and_one_with_no_known_network_refuses(self):
        mac = built(FakeMac())
        mac.install_macos()
        self.build(mac, "--repair", WK_BENCH_WIRED="1")
        self.assertNotIn(VOL + "/usr/local/share/wk-bench/wifi.conf", mac.files)
        mac.keychain = {}
        self.assertIn("WK_BENCH_WIRED=1", self.build(mac, "--repair")[1])

    def provisioning(self, clean=True):
        mac = FakeMac()
        mac.running_on = "WK Bench"
        mac.dirs.add("/System/Volumes/Data")
        mac.react(["bash", "-c"], lambda argv, f: Result(0 if clean or "wk_quiet" not in argv[3] else 1, ""))
        return mac

    def test_provision_runs_only_on_the_volume_and_records_a_clean_readback(self):
        mac = FakeMac()
        self.assertIn("Boot 'WK Bench' first", self.build(mac, "--provision")[1])
        self.assertNotIn(macvolume.MARKER, mac.files)
        mac = self.provisioning()
        self.build(mac, "--provision", clock=FakeClock(start=1788000000.0))
        self.assertEqual(mac.files[macvolume.MARKER], "id=perf-macos-tolken-2026-08\nprofile=perf-macos-tolken\n")
        self.assertIn("=== provisioning complete (wk sysimage build perf-macos-tolken --provision", mac.files[macvolume.FIRSTBOOT_LOG])

    def test_a_readback_with_a_wrong_setting_or_a_dry_run_records_nothing(self):
        mac = self.provisioning()
        mac.answer(quiet_lib(str(REPO), DESKTOP, "wk_quiet_desktop_findings"), out="wrong\tspotlight indexes\tfix\n")
        mac.answer(quiet_lib(str(REPO), COMMON, RENDER), rc=1)
        self.assertIn("not recorded as provisioned", self.build(mac, "--provision")[1])
        self.assertNotIn(macvolume.FIRSTBOOT_LOG, mac.files)
        mac = self.provisioning()
        self.assertIn("but only on a readback", self.build(mac, "--provision", WK_DRY_RUN="1")[1])
        self.assertEqual({p for p in mac.files if p.startswith(("/etc", "/var"))}, set())


@requires_tool("rsync")
class ThePayloadTheAutorunStages(unittest.TestCase):
    """`python3 -m wk.sysimage.macvolume stage-payload /`, the bench install's convergence (lib/wk/bench/autorun.py)."""

    def test_every_row_lands_under_a_root_of_the_tests_own(self):
        with scratch_dir() as d:
            cp = subprocess.run(["python3", "-m", "wk.sysimage.macvolume", "stage-payload", str(d)], capture_output=True,
                                text=True, timeout=30, env=dict(os.environ, HOME=str(d), PYTHONPATH=str(REPO / "lib")))
            self.assertEqual(cp.returncode, 0, cp.stderr)
            for _src, dest, _mode in macvolume.PAYLOAD:
                self.assertTrue((d / dest).is_file(), dest)
            self.assertTrue((d / "usr/local/share/wk-bench/wk-tools/wk").is_file())


@requires_machine("mbp")
class TheProvisionedVolume(unittest.TestCase):
    """`live sysimage.mac_volume_provision`: the volume on the Mac is installed and carries the marker."""

    def test_the_volume_is_built(self):
        from wk import fleet
        from wk.machine import Ssh
        m = Ssh(fleet.Fleet(REPO).load(PRESET["IMG_MACHINE"])["ssh"])
        self.assertTrue(MacVolume(m, PRESET, dict(os.environ)).outputs(), "no marker on an installed bench volume")


if __name__ == "__main__":
    unittest.main()
