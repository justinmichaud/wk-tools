"""The Mac volume builder (lib/wk/sysimage/macvolume.py) and its tailnet half (lib/wk/sysimage/mactailnet.py)
against a Mac in memory: FakeMac answers diskutil, the installer, the Go toolchain and the file tools the way the
Mac does and moves its files, so every step is checked by what it leaves. The on-board join
(bench/mac-tailnet.sh) runs on the install; its refusal off root and its layout are checked here.

Rows landed here: `unit killpoints[sysimage build mac-volume]` (the tailnet stage); marks live
`sysimage.mac_volume_provision`.

Run: python3 tests/run.py --unit -k test_mac_tailnet
"""
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
from tests.support import REPO, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import macvolume, mactailnet  # noqa: E402
from wk.sysimage.macvolume import MacVolume  # noqa: E402
from wk.sysimage.mactailnet import PIN, Tailnet  # noqa: E402
from wk.quiet import COMMON, DESKTOP, RENDER, lib_argv as quiet_lib  # noqa: E402

MACHO = bytes.fromhex(mactailnet.MACHO_ARM64.replace(" ", "")) + b"\0" * 24
STORE, STATE, HOME = "/st", "/home/u/.local/state", "/home/u"
ENV = {"HOME": HOME, "XDG_STATE_HOME": STATE, "WK_STORE": STORE, "TMPDIR": "/tmp"}
KEY = "/home/u/.config/wk/tailscale-authkey"
TS_VERSION = re.search(r'^TS_VERSION = "(.*)"$', (REPO / mactailnet.REL).read_text(), re.M).group(1)
ART = Tailnet(Fake(), ENV).artifacts()
OUT = "%s/darwin-arm64-%s" % (ART, TS_VERSION)
VOL, DATA = "/Volumes/WK Bench", "/Volumes/WK Bench - Data"
PROFILE = images.load("perf-macos-tolken")
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
        self.downloads = {"https://go.dev/dl/go%s.darwin-arm64.tar.gz" % PIN["GO_VERSION"]: b"go-tarball",
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


def tailnet(mac, pin=None):
    return Tailnet(mac, ENV, REPO, pin=pin)


def volume(mac, clock=None, **env):
    return MacVolume(mac, PROFILE, dict(ENV, **env), clock or FakeClock(), root=REPO)


class ThePin(unittest.TestCase):
    def test_the_source_checksum_is_for_the_fleets_one_tailscale_version(self):
        self.assertEqual(PIN["TS_SRC_FOR"], TS_VERSION)

    def test_every_checksum_is_a_sha256(self):
        for sha in [PIN["TS_SRC_SHA256"]] + list(PIN["GO_SHA256"].values()):
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertIn("darwin_arm64", PIN["GO_SHA256"])

    def test_go_version_is_a_release(self):
        self.assertRegex(PIN["GO_VERSION"], r"^\d+\.\d+(\.\d+)?$")

    def test_a_pin_for_another_version_refuses_and_names_the_bump(self):
        with clean_env():
            got, err = quiet(tailnet(FakeMac(), dict(PIN, TS_SRC_FOR="9.9.9")).build)
        self.assertIs(got, Refused)
        for word in ("9.9.9", TS_VERSION, "TS_SRC_SHA256"):
            self.assertIn(word, err)

    def test_a_host_with_no_toolchain_checksum_refuses_and_names_the_key(self):
        mac = FakeMac()
        with clean_env():
            got, err = quiet(tailnet(mac, dict(PIN, GO_SHA256={})).build)
        self.assertIs(got, Refused)
        self.assertIn('GO_SHA256["darwin_arm64"]', err)
        self.assertIn("go.dev", err)
        self.assertEqual(mac.runs("curl"), [])


class TheBuild(unittest.TestCase):
    def test_it_fetches_verifies_and_builds_a_darwin_arm64_daemon(self):
        mac = FakeMac()
        pin = dict(PIN, TS_SRC_SHA256=hashlib.sha256(b"ts-zip").hexdigest(),
                   GO_SHA256={"darwin_arm64": hashlib.sha256(b"go-tarball").hexdigest()})
        with clean_env():
            out, err = quiet(tailnet(mac, pin).build)
        self.assertEqual(out, OUT)
        self.assertEqual(blob(mac.files[out + "/tailscaled"])[:8], MACHO[:8])
        go = [r for r in mac.runs("GOTOOLCHAIN=local")][0]
        for word in ("GOOS=darwin", "GOARCH=arm64", "CGO_ENABLED=0", "-trimpath"):
            self.assertIn(word, go)

    def test_a_download_that_does_not_match_its_checksum_is_refused_and_not_kept(self):
        mac = FakeMac()
        with clean_env():
            got, err = quiet(tailnet(mac, dict(PIN, GO_SHA256={"darwin_arm64": "0" * 64})).build)
        self.assertIs(got, Refused)
        self.assertIn("unverified", err)
        self.assertFalse([p for p in mac.files if p.endswith(".part")])
        self.assertEqual(mac.runs("GOTOOLCHAIN"), [])

    def test_a_finished_build_is_not_built_again(self):
        mac = FakeMac()
        built(mac)
        with clean_env():
            self.assertEqual(quiet(tailnet(mac).build)[0], OUT)
        self.assertEqual(mac.runs("curl") + mac.runs("GOTOOLCHAIN"), [])


def built(mac):
    for n in ("tailscaled", "tailscale"):
        mac.put(OUT + "/" + n, MACHO)
    return mac


class TheStage(unittest.TestCase):
    def stage(self, mac, root="/vol", sudo=False):
        mac.dirs.add(root)
        with clean_env():
            got, err = quiet(tailnet(mac).stage, root, "mbp", sudo)
        self.assertIsNot(got, Refused, err)
        return err

    def test_it_installs_the_daemon_the_cli_and_a_private_state_directory(self):
        mac = built(FakeMac())
        self.stage(mac)
        for n in ("tailscaled", "tailscale"):
            self.assertEqual(mac.files["/vol/usr/local/bin/" + n], MACHO)
            self.assertEqual(mac.modes["/vol/usr/local/bin/" + n], "0755")
        self.assertEqual(mac.modes["/vol/private/var/db/wk/tailscale"], "0700")

    def test_the_daemon_keeps_its_state_on_the_volume_and_is_kept_alive(self):
        mac = built(FakeMac())
        self.stage(mac)
        d = plistlib.loads(blob(mac.files["/vol/Library/LaunchDaemons/com.wk.tailscaled.plist"]))
        self.assertEqual(d["ProgramArguments"][0], "/usr/local/bin/tailscaled")
        self.assertIn("--state=/var/db/wk/tailscale/tailscaled.state", d["ProgramArguments"])
        self.assertTrue(d["RunAtLoad"] and d["KeepAlive"])

    def test_the_join_is_retried_on_every_boot_by_the_on_board_script(self):
        mac = built(FakeMac())
        self.stage(mac)
        d = plistlib.loads(blob(mac.files["/vol/Library/LaunchDaemons/com.wk.tailnet-join.plist"]))
        self.assertEqual(d["ProgramArguments"][1:], [mactailnet.PAYLOAD_TOOLS + "/bench/mac-tailnet.sh", "join"])
        self.assertTrue(d["RunAtLoad"])

    def test_the_key_is_root_only_and_the_conf_names_the_node_the_machine_declares(self):
        mac = built(FakeMac())
        self.stage(mac)
        self.assertEqual(mac.files["/vol/private/etc/wk/tailscale-authkey"], "tskey-auth-kTESTONLY")
        self.assertEqual(mac.modes["/vol/private/etc/wk/tailscale-authkey"], "0600")
        self.assertEqual(mac.files["/vol/private/etc/wk/tailnet.conf"], "hostname=tolken-bench\ntag=tag:wk\n")

    def test_every_write_to_the_volume_goes_through_sudo_when_asked(self):
        mac = built(FakeMac())
        self.stage(mac, sudo=True)
        writes = [r for r in mac.runs("/vol") if r[0] != "od"]
        self.assertTrue(writes)
        for r in writes:
            self.assertEqual(r[:2], ("sudo", "install"), r)

    def test_a_remembered_node_identity_comes_back_on_the_next_volume(self):
        mac = built(FakeMac())
        self.stage(mac)
        state = "/vol/private/var/db/wk/tailscale/tailscaled.state"
        self.assertNotIn(state, mac.files)
        mac.put(state, "node-key")
        with clean_env():
            _, err = quiet(tailnet(mac).remember, "/vol", "mbp")
        kept = STATE + "/wk/mac-tailnet/tolken-bench.state"
        self.assertEqual(mac.files[kept], "node-key")
        self.assertEqual(mac.modes[kept], "0600")
        self.stage(mac, "/vol2")
        self.assertEqual(mac.files["/vol2/private/var/db/wk/tailscale/tailscaled.state"], "node-key")

    def test_remembering_a_volume_with_no_identity_is_not_a_failure(self):
        mac = FakeMac()
        with clean_env():
            got, err = quiet(tailnet(mac).remember, "/vol", "mbp")
        self.assertIsNot(got, Refused)
        self.assertIn("no node identity", err)

    def test_no_auth_key_refuses_before_anything_is_built(self):
        mac = FakeMac()
        mac._drop(KEY)
        with clean_env():
            got, err = quiet(tailnet(mac).stage, "/vol", "mbp")
        self.assertIs(got, Refused)
        self.assertIn("wk key set tailnet", err)
        self.assertEqual(mac.runs("curl") + mac.runs("install"), [])

    def test_a_machine_with_no_bench_node_name_refuses(self):
        with clean_env():
            got, err = quiet(tailnet(FakeMac()).stage, "/vol", "no-such-machine")
        self.assertIs(got, Refused)
        self.assertIn("NODE_BENCH_SSH", err)

    def test_a_dry_run_changes_nothing(self):
        mac = built(FakeMac())
        mac.dirs.add("/vol")
        before = dict(mac.files)
        with clean_env(WK_DRY_RUN="1"):
            got, err = quiet(tailnet(mac).stage, "/vol", "mbp")
        self.assertIsNot(got, Refused, err)
        self.assertEqual(mac.files, before)
        self.assertIn("would run", err)

    def test_a_stage_killed_after_any_effect_converges_on_a_rerun(self):
        def world():
            mac = built(FakeMac())
            mac.dirs.add("/vol")
            return types.SimpleNamespace(fake=mac)

        def once(w):
            with clean_env():
                quiet(tailnet(w.fake).stage, "/vol", "mbp")

        converges(self, world, once, lambda w: {p: v for p, v in w.fake.files.items() if p.startswith("/vol/")},
                  max_effects=80)


class TheInstallHalf(unittest.TestCase):
    """Root and nothing else: the bench install runs it against itself, with no network and no credentials."""

    def payload(self, mac, binary=MACHO):
        for f in mactailnet.PAYLOAD:
            mac.put("/p/" + f, binary if f.startswith("tailscale") and "." not in f else "x")
        mac.dirs.add("/r")

    def test_it_reads_no_credential_and_reaches_no_network(self):
        mac = FakeMac()
        self.payload(mac)
        with clean_env():
            got, err = quiet(tailnet(mac).install, "/r", "/p")
        self.assertIsNot(got, Refused, err)
        self.assertEqual(mac.runs("curl") + mac.runs("wk_tailscale"), [])

    def test_a_directory_that_is_not_a_payload_is_refused(self):
        mac = FakeMac()
        mac.dirs.add("/p")
        with clean_env():
            got, err = quiet(tailnet(mac).install, "/r", "/p")
        self.assertIs(got, Refused)
        self.assertIn("not a collected tailnet payload", err)

    def test_a_binary_the_bench_install_cannot_run_is_refused(self):
        mac = FakeMac()
        self.payload(mac, binary=b"#!/bin/sh\n")
        with clean_env():
            got, err = quiet(tailnet(mac).install, "/r", "/p")
        self.assertIs(got, Refused)
        self.assertIn("Mach-O arm64", err)
        self.assertEqual(mac.runs("install"), [])


class TheOnBoardJoin(unittest.TestCase):
    SCRIPT = REPO / "bench" / "mac-tailnet.sh"

    def test_its_paths_are_the_layout_the_host_half_installs(self):
        text = self.SCRIPT.read_text()
        for name in ("TS_BIN", "TS_KEY", "TS_CONF", "DAEMON_LABEL", "LAUNCHD"):
            self.assertIn("\n%s=%s\n" % (name, getattr(mactailnet, name)), text, name)

    def test_join_refuses_without_root(self):
        if os.getuid() == 0:
            self.skipTest("running as root")
        cp = subprocess.run(["bash", str(self.SCRIPT), "join"], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("root", cp.stderr)

    def test_the_install_the_autorun_calls_reaches_the_python(self):
        with scratch_dir() as d:
            cp = subprocess.run(["python3", "-m", "wk.sysimage.mactailnet", "install", str(d), str(d)], capture_output=True,
                                text=True, timeout=20, env=dict(os.environ, PYTHONPATH=str(REPO / "lib")))
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not a collected tailnet payload", cp.stderr)


class TheBuilder(unittest.TestCase):
    """`wk sysimage build perf-macos-tolken`: one action a run, on the Mac, done when the install carries the marker."""

    def test_it_is_done_when_the_installed_volume_carries_the_marker(self):
        mac = FakeMac()
        v = volume(mac)
        self.assertEqual(v.outputs(), [])
        mac.dirs.add(VOL)
        mac.put(VOL + "/etc/wk-image", "id=x\n")
        self.assertEqual(v.outputs(), [], "an empty volume with a marker on it boots nothing")
        mac.install_macos()
        self.assertEqual(v.outputs(), [VOL + "/etc/wk-image"])

    def test_off_a_mac_it_refuses(self):
        mac = FakeMac()
        mac.answer(["uname", "-s"], out="Linux\n")
        with clean_env():
            got, err = quiet(volume(mac).build, ["--create"])
        self.assertIs(got, Refused)
        self.assertIn("on the Mac itself", err)

    def test_one_action_at_a_time(self):
        with clean_env():
            got, err = quiet(volume(FakeMac()).build, ["--create", "--repair"])
        self.assertIs(got, Refused)
        self.assertIn("one action at a time", err)

    def test_the_report_changes_nothing(self):
        mac = FakeMac()
        mac.install_macos()
        with clean_env():
            got, err = quiet(volume(mac).build, [])
        self.assertEqual(got, 0)
        self.assertIn("26.6", err)
        self.assertIn("MISSING", err)
        self.assertEqual([e for e in mac.effects if e[0] != "run"], [])

    def test_create_refuses_a_container_without_the_room(self):
        mac = FakeMac()
        mac.volumes = set()
        mac.free = 10 ** 9
        with clean_env():
            got, err = quiet(volume(mac).build, ["--create"])
        self.assertIs(got, Refused)
        self.assertIn("WK_BENCH_NEED_GB", err)
        self.assertEqual(mac.runs("addVolume"), [])

    def test_the_room_it_needs_and_the_volume_it_names_can_be_set(self):
        mac = FakeMac()
        mac.volumes = set()
        with clean_env():
            _, err = quiet(volume(mac, WK_BENCH_NEED_GB="1", WK_BENCH_VOLUME="Other").build, [])
        self.assertIn("need 1 GB", err)
        self.assertIn("volume name:    Other", err)

    def test_create_adds_the_volume_once(self):
        mac = FakeMac()
        mac.volumes = set()
        with clean_env():
            quiet(volume(mac).build, ["--create"])
            quiet(volume(mac).build, ["--create"])
        self.assertEqual(mac.runs("addVolume"), [("sudo", "diskutil", "apfs", "addVolume", "disk3", "APFS", "WK Bench")])


class TheInstall(unittest.TestCase):
    def mac(self):
        mac = built(FakeMac())
        mac.put("/Applications/Install macOS Tahoe.app/Contents/Resources/startosinstall", "x")
        mac.dirs.add(VOL)
        return mac

    def test_declined_it_changes_nothing(self):
        mac = self.mac()
        with clean_env(), mock.patch("sys.stdin", io.StringIO("n\n")):
            got, err = quiet(volume(mac).build, ["--install"])
        self.assertEqual(got, 0)
        self.assertEqual(mac.console, [])
        self.assertEqual(mac.runs("install -m"), [])

    def test_it_keeps_the_node_identity_before_the_volume_is_erased(self):
        mac = self.mac()
        mac.put(VOL + "/private/var/db/wk/tailscale/tailscaled.state", "node-key")
        with clean_env(WK_YES="1"):
            got, err = quiet(volume(mac).build, ["--install"])
        self.assertEqual(got, 0, err)
        order = [e[1] for e in mac.effects if e[0] in ("run", "run_tty")]
        remember = next(i for i, r in enumerate(order) if r[:2] == ("sudo", "cat"))
        erase = next(i for i, r in enumerate(order) if r[1].endswith("startosinstall"))
        self.assertLess(remember, erase)
        self.assertEqual(mac.files[STATE + "/wk/mac-tailnet/tolken-bench.state"], "node-key")

    def test_startosinstall_is_given_the_package_and_an_owners_credential(self):
        mac = self.mac()
        with clean_env(WK_YES="1"):
            quiet(volume(mac).build, ["--install"])
        (argv,) = mac.console
        self.assertEqual(argv[argv.index("--volume") + 1], VOL)
        self.assertEqual(argv[argv.index("--user") + 1], "owner")
        self.assertIn("--passprompt", argv)
        self.assertEqual(argv[argv.index("--installpackage") + 1], "/tmp/wk-bench-provision.pkg")

    def test_the_authorising_account_can_be_named(self):
        mac = self.mac()
        with clean_env(WK_YES="1"):
            quiet(MacVolume(mac, PROFILE, dict(ENV, WK_BENCH_ADMIN="other"), FakeClock(), root=REPO).build, ["--install"])
        self.assertEqual(mac.console[0][mac.console[0].index("--user") + 1], "other")

    def test_an_installed_volume_is_not_installed_again_and_nothing_is_asked(self):
        mac = self.mac()
        mac.install_macos()
        with clean_env():
            got, err = quiet(volume(mac).build, ["--install"])
        self.assertEqual(got, 0)
        self.assertEqual(mac.console, [])


class ThePackage(unittest.TestCase):
    def build(self, mac):
        with clean_env():
            got, err = quiet(volume(mac).build_pkg)
        self.assertEqual(got, "/tmp/wk-bench-provision.pkg", err)
        return self.staged(mac)

    @staticmethod
    def staged(mac):
        return {tuple(r[1:]) for r in mac.runs("install") if r[0] == "install"}

    def test_it_answers_setup_assistant_and_arms_first_boot(self):
        mac = built(FakeMac())
        with clean_env():
            quiet(volume(mac).build_pkg)
        pkgroot = [r for r in mac.runs("pkgbuild")][0]
        root = pkgroot[pkgroot.index("--root") + 1]
        self.assertTrue(root.startswith("/tmp/"))
        writes = {e[1] for e in mac.effects if e[0] == "write"}
        for f in macvolume.SKIP_SETUP:
            self.assertIn(os.path.join(root, f), writes)
        d = plistlib.loads(macvolume.FIRSTBOOT_PLIST.encode())
        self.assertEqual(d["StandardOutPath"], macvolume.FIRSTBOOT_LOG)
        self.assertNotIn("KeepAlive", d)
        self.assertIn(root + macvolume.FIRSTBOOT, writes)

    def test_every_payload_row_and_the_tailnet_are_staged_as_the_user(self):
        mac = built(FakeMac())
        with clean_env():
            quiet(volume(mac).build_pkg)
        dests = {r[-1] for r in mac.runs("install") if r[0] == "install"}
        for _src, dest, _mode in macvolume.PAYLOAD:
            self.assertIn("/tmp/wk-bench-pkgroot/" + dest, dests)
        self.assertIn("/tmp/wk-bench-pkgroot/private/etc/wk/tailscale-authkey", dests)
        self.assertFalse([r for r in mac.runs("/tmp/wk-bench-pkgroot") if r[0] == "sudo"])

    def test_the_account_password_is_private_on_both_sides(self):
        mac = built(FakeMac())
        with clean_env():
            quiet(volume(mac).build_pkg)
        self.assertEqual(mac.modes[STATE + "/wk/bench-password"], "0600")
        self.assertEqual(mac.modes["/tmp/wk-bench-pkgroot/usr/local/share/wk-bench/password"], "0600")


class TheRepair(unittest.TestCase):
    def mac(self):
        mac = built(FakeMac())
        mac.install_macos()
        return mac

    def repair(self, mac, **env):
        with clean_env(**env):
            got, err = quiet(volume(mac, **env).build, ["--repair"])
        self.assertEqual(got, 0, err)
        return err

    def test_it_stages_the_payload_and_the_tailnet_through_sudo(self):
        mac = self.mac()
        self.repair(mac)
        tools = [r for r in mac.runs(VOL + "/usr/local/share/wk-bench/wk-tools/") if "rsync" in r]
        self.assertEqual(tools[0][:2], ("sudo", "rsync"))
        self.assertIn(("sudo", "install", "-m", "0600", ART + "/collected/authkey",
                       VOL + "/private/etc/wk/tailscale-authkey"), mac.runs("install"))

    def test_it_copies_the_first_preferred_network_the_keychain_holds(self):
        mac = self.mac()
        self.repair(mac)
        self.assertEqual(mac.files[VOL + "/usr/local/share/wk-bench/wifi.conf"],
                         "WIFI_SSID=homenet\nWIFI_PSK='pass word'\n")
        self.assertEqual(mac.modes[VOL + "/usr/local/share/wk-bench/wifi.conf"], "0600")

    def test_a_wired_bench_install_is_given_no_wifi(self):
        mac = self.mac()
        self.repair(mac, WK_BENCH_WIRED="1")
        self.assertNotIn(VOL + "/usr/local/share/wk-bench/wifi.conf", mac.files)

    def test_no_known_network_refuses(self):
        mac = self.mac()
        mac.keychain = {}
        with clean_env():
            got, err = quiet(volume(mac).build, ["--repair"])
        self.assertIs(got, Refused)
        self.assertIn("WK_BENCH_WIRED=1", err)

    def test_the_packaged_client_and_its_key_are_removed(self):
        mac = self.mac()
        for f in macvolume.STALE:
            mac.put(VOL + "/usr/local/share/wk-bench/" + f, "old")
        self.repair(mac)
        for f in macvolume.STALE:
            self.assertNotIn(VOL + "/usr/local/share/wk-bench/" + f, mac.files)

    def test_remote_login_is_added_when_absent_and_left_when_on(self):
        mac = self.mac()
        dis = DATA + "/private/var/db/com.apple.xpc.launchd/disabled.plist"
        mac.put(dis, "{}")
        self.repair(mac)
        self.assertEqual([r[-2] for r in mac.runs("PlistBuddy")], ["Add :com.openssh.sshd bool false"])
        self.repair(mac)
        self.assertEqual(len(mac.runs("PlistBuddy")), 1)

    def test_first_boot_is_rearmed_as_root(self):
        mac = self.mac()
        self.repair(mac)
        self.assertEqual(mac.files[VOL + macvolume.FIRSTBOOT], macvolume.FIRSTBOOT_PLIST)
        self.assertIn(("sudo", "chown", "root:wheel", VOL + macvolume.FIRSTBOOT), mac.runs("chown"))

    def test_a_volume_without_macos_is_refused(self):
        mac = built(FakeMac())
        with clean_env():
            got, err = quiet(volume(mac).build, ["--repair"])
        self.assertIs(got, Refused)
        self.assertIn("--install first", err)


class TheProvision(unittest.TestCase):
    def mac(self, clean=True):
        mac = FakeMac()
        mac.running_on = "WK Bench"
        mac.dirs.add("/System/Volumes/Data")
        mac.react(["bash", "-c"], lambda argv, f: Result(0 if clean or "wk_quiet" not in argv[3] else 1, ""))
        return mac

    def test_on_the_host_install_it_refuses(self):
        mac = FakeMac()
        with clean_env():
            got, err = quiet(volume(mac).build, ["--provision"])
        self.assertIs(got, Refused)
        self.assertIn("Boot 'WK Bench' first", err)
        self.assertNotIn(macvolume.MARKER, mac.files)

    def test_it_writes_the_marker_naming_the_profile_and_the_month(self):
        mac = self.mac()
        clock = FakeClock(start=1788000000.0)   # 2026-08
        with clean_env():
            quiet(volume(mac, clock).build, ["--provision"])
        self.assertEqual(mac.files[macvolume.MARKER], "id=perf-macos-tolken-2026-08\nprofile=perf-macos-tolken\n")

    def test_a_clean_readback_is_recorded_as_provisioned(self):
        mac = self.mac()
        with clean_env():
            quiet(volume(mac).build, ["--provision"])
        self.assertIn("=== provisioning complete (wk sysimage build perf-macos-tolken --provision",
                      mac.files[macvolume.FIRSTBOOT_LOG])

    def test_a_readback_with_a_wrong_setting_is_not(self):
        mac = self.mac()
        mac.answer(quiet_lib(str(REPO), DESKTOP, "wk_quiet_desktop_findings"), out="wrong\tspotlight indexes\tfix\n")
        mac.answer(quiet_lib(str(REPO), COMMON, RENDER), rc=1)
        with clean_env():
            _, err = quiet(volume(mac).build, ["--provision"])
        self.assertNotIn(macvolume.FIRSTBOOT_LOG, mac.files)
        self.assertIn("not recorded as provisioned", err)

    def test_a_dry_run_promises_no_record(self):
        mac = self.mac()
        with clean_env(WK_DRY_RUN="1"):
            _, err = quiet(volume(mac).build, ["--provision"])
        self.assertEqual({p for p in mac.files if p.startswith(("/etc", "/var"))}, set())
        self.assertIn("but only on a readback", err)


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
        m = Ssh(fleet.Fleet(REPO).load(PROFILE["IMG_MACHINE"])["NODE_SSH"])
        self.assertTrue(MacVolume(m, PROFILE, dict(os.environ)).outputs(), "no marker on an installed bench volume")


if __name__ == "__main__":
    unittest.main()
