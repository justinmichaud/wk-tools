"""bench/mac-tailnet.sh -- the benchmark install's tailnet identity.

The macOS benchmark install has no packaged Tailscale it can use: every one of
them tunnels through NetworkExtension, whose VPN-configuration panel only a
person can answer. So this lane builds tailscaled from pinned source and runs it
as a root LaunchDaemon, which opens a utun with no panel. Without it an A/B is
unobservable from the moment the machine reboots, and since it powers itself off
when it finishes, "still measuring" and "finished" look identical from outside.

What is exercised here without a Mac: the pin agreeing with the fleet's one
tailscale version, every refusal, the checksum gate, and the whole shape of what
`stage` lays down on a volume -- read back through plistlib rather than grepped.
The Go build itself and `join` need root on the Mac; `join`'s root refusal is
tested, the rest is what the one boot proves.

Run: python3 -m unittest tests.test_mac_tailnet -v
"""
import os
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import unittest

from tests import support
from tests.support import REPO, WkTest, scratch_dir, stub_path

SCRIPT = REPO / "bench" / "mac-tailnet.sh"
PIN = REPO / "bench" / "mac-tailnet-pin.inc"
REL = (REPO / "image" / "yocto" / "meta-wk-tailnet" / "recipes-network"
       / "tailscale" / "tailscale-release.inc")
FIRSTBOOT = REPO / "bench" / "mac-bench-firstboot.sh"
SSH_CONFIG = REPO / "dotfiles" / "ssh" / "config"
VOLUME = REPO / "bench" / "mac-bench-volume.sh"

# The 8 bytes `mac-tailnet.sh` reads to decide a build is usable: Mach-O 64 magic
# and CPU_TYPE_ARM64. A test can write them and skip a 40-second Go build.
MACHO_ARM64 = struct.pack("<II", 0xfeedfacf, 0x0100000c) + b"\0" * 24

FAKE_KEY = "tskey-auth-kTESTONLY-notarealkeyatall"


def field(path, name):
    m = re.search(r'^%s = "(.*)"$' % re.escape(name), path.read_text(), re.M)
    return m.group(1) if m else None


def host_go_key():
    arch = {"aarch64": "arm64"}.get(os.uname().machine, os.uname().machine)
    return "GO_SHA256_%s_%s" % (os.uname().sysname.lower(), arch)


class Pin(unittest.TestCase):
    def test_source_checksum_names_the_fleets_one_tailscale_version(self):
        """The Mac runs the same tailscale as every board, so TS_VERSION lives
        in one file and TS_SRC_FOR only says which version the source checksum
        belongs to."""
        self.assertEqual(field(PIN, "TS_SRC_FOR"), field(REL, "TS_VERSION"))
        self.assertIsNone(field(PIN, "TS_VERSION"),
                          "the pin declares a second TS_VERSION; %s is the one" % REL)

    def test_every_checksum_is_a_sha256(self):
        names = [n for n in re.findall(r"^(\w+) = ", PIN.read_text(), re.M)
                 if "SHA256" in n]
        self.assertIn("TS_SRC_SHA256", names)
        self.assertTrue([n for n in names if n.startswith("GO_SHA256_")],
                        "no Go toolchain checksum in the pin")
        for n in names:
            self.assertRegex(field(PIN, n), r"^[0-9a-f]{64}$", n)

    def test_go_version_is_a_release(self):
        self.assertRegex(field(PIN, "GO_VERSION"), r"^\d+\.\d+(\.\d+)?$")


class Refusals(WkTest):
    """Each refusal is reached with a fake WK_ROOT: the script derives WK_ROOT
    from its own path, so a directory of symlinks plus one edited pin file
    exercises a bad pin without touching the tree."""

    def fake_root(self, tmp, pin_text):
        root = tmp / "root"
        (root / "bench").mkdir(parents=True)
        for d in ("lib", "boot", "image"):
            (root / d).symlink_to(REPO / d)
        (root / "bench" / "mac-tailnet.sh").symlink_to(SCRIPT)
        (root / "bench" / "mac-tailnet-pin.inc").write_text(pin_text)
        return root

    def call(self, root, *args, env=None):
        e = {"XDG_STATE_HOME": str(root.parent / "state"),
             "WK_STORE": str(root.parent / "store")}
        e.update(env or {})
        return support.bash(
            "bash %s %s" % (root / "bench" / "mac-tailnet.sh",
                            " ".join('"%s"' % a for a in args)), env=e)

    def test_no_subcommand_prints_usage(self):
        cp = support.bash("bash %s" % SCRIPT)
        self.assertEqual(cp.returncode, 1)
        for word in ("build", "stage", "remember", "join"):
            self.assertIn(word, cp.stderr)

    def test_a_pin_for_another_version_refuses_and_names_the_bump(self):
        with scratch_dir() as tmp:
            root = self.fake_root(tmp, PIN.read_text().replace(
                'TS_SRC_FOR = "%s"' % field(PIN, "TS_SRC_FOR"),
                'TS_SRC_FOR = "9.9.9"'))
            cp = self.call(root, "build")
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("9.9.9", cp.stderr)
            self.assertIn(field(REL, "TS_VERSION"), cp.stderr)
            self.assertIn("TS_SRC_SHA256", cp.stderr)

    def test_a_host_with_no_toolchain_checksum_refuses_and_names_the_key(self):
        key = host_go_key()
        with scratch_dir() as tmp:
            root = self.fake_root(tmp, PIN.read_text().replace(key + " =",
                                                               key + "_other ="))
            cp = self.call(root, "build")
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn(key, cp.stderr)
            self.assertIn("go.dev", cp.stderr)

    def test_stage_and_remember_need_a_volume_root(self):
        for sub in ("stage", "remember"):
            cp = support.bash("bash %s %s" % (SCRIPT, sub))
            self.assertNotEqual(cp.returncode, 0, sub)
            self.assertIn("<volume-root>", cp.stderr, sub)

    def test_stage_with_no_auth_key_refuses_before_it_builds_anything(self):
        with scratch_dir() as tmp:
            cp = support.bash(
                'bash %s stage "%s"' % (SCRIPT, tmp),
                env={"WK_TS_AUTHKEY": str(tmp / "absent"),
                     "XDG_STATE_HOME": str(tmp / "state"),
                     "WK_STORE": str(tmp / "store")})
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("wk key set tailnet", cp.stderr)
            self.assertFalse((tmp / "state").exists(),
                             "it started building before checking for the key")

    def test_stage_refuses_when_the_machine_declares_no_bench_node_name(self):
        with scratch_dir() as tmp:
            cp = support.bash(
                'bash %s stage "%s"' % (SCRIPT, tmp),
                env={"WK_MAC_MACHINE": "no-such-machine",
                     "XDG_STATE_HOME": str(tmp / "state"),
                     "WK_STORE": str(tmp / "store")})
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("NODE_BENCH_SSH", cp.stderr)

    def test_join_refuses_without_root(self):
        cp = support.bash("bash %s join" % SCRIPT)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("root", cp.stderr)


class ChecksumGate(WkTest):
    """`_sha256_check` lifted out and called directly, the way cmd/selftest
    lifts a function: it is the one thing between a download and a daemon that
    goes onto a machine which measures."""

    def check(self, expected, path, env=None):
        return support.bash(
            '. "$WK_ROOT/lib/common.sh"; '
            'eval "$(sed -n "/^_sha256_check/,/^}/p" %s)"; '
            '_sha256_check %s "%s"' % (SCRIPT, expected, path), env=env)

    def test_it_accepts_the_matching_digest_and_rejects_any_other(self):
        with scratch_dir() as tmp:
            f = tmp / "payload"
            f.write_bytes(b"tailscale")
            good = subprocess.run(["sha256sum", str(f)], capture_output=True,
                                  text=True, check=True).stdout.split()[0]
            self.assertEqual(self.check(good, f).returncode, 0)
            self.assertNotEqual(self.check("0" * 64, f).returncode, 0)

    def test_with_no_hashing_tool_it_refuses_rather_than_skipping(self):
        with scratch_dir() as tmp:
            f = tmp / "payload"
            f.write_bytes(b"tailscale")
            binp = tmp / "bin"          # bash and sed reachable, neither hashing tool
            binp.mkdir()
            for tool in ("bash", "sed"):
                (binp / tool).symlink_to(shutil.which(tool))
            cp = self.check("0" * 64, f, env={"PATH": str(binp)})
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("shasum", cp.stderr)


class Stage(WkTest):
    """What `stage` lays down, against a fake volume root and a pre-placed
    build, so no Go toolchain and no network are needed."""

    def volume(self, tmp):
        state = tmp / "state"
        # The pre-placed build goes where cmd_build looks: the artifact store,
        # not the state directory. Planted anywhere else, `stage` finds no build
        # and fetches a Go toolchain to make one.
        store = tmp / "store"
        out = store / "cache" / "mac-tailnet" / ("darwin-arm64-%s" % field(REL, "TS_VERSION"))
        out.mkdir(parents=True)
        for name in ("tailscaled", "tailscale"):
            (out / name).write_bytes(MACHO_ARM64)
        key = tmp / "authkey"
        key.write_text(FAKE_KEY)
        root = tmp / "vol"
        root.mkdir()
        env = {"XDG_STATE_HOME": str(state), "WK_TS_AUTHKEY": str(key),
               "WK_STORE": str(tmp / "store")}
        return root, env, state

    def stage(self, root, env, sub="stage"):
        cp = support.bash('bash %s %s "%s"' % (SCRIPT, sub, root), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp

    def test_it_installs_the_daemon_the_cli_and_the_state_directory(self):
        with scratch_dir() as tmp:
            root, env, _ = self.volume(tmp)
            self.stage(root, env)
            for name in ("tailscaled", "tailscale"):
                p = root / "usr" / "local" / "bin" / name
                self.assertEqual(p.read_bytes(), MACHO_ARM64, name)
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o755, name)
            d = root / "private" / "var" / "db" / "wk" / "tailscale"
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)

    def test_the_daemon_plist_starts_tailscaled_with_state_on_the_volume(self):
        with scratch_dir() as tmp:
            root, env, _ = self.volume(tmp)
            self.stage(root, env)
            p = root / "Library" / "LaunchDaemons" / "com.wk.tailscaled.plist"
            d = plistlib.loads(p.read_bytes())
            self.assertEqual(d["Label"], "com.wk.tailscaled")
            self.assertEqual(d["ProgramArguments"][0], "/usr/local/bin/tailscaled")
            self.assertIn("--state=/var/db/wk/tailscale/tailscaled.state",
                          d["ProgramArguments"])
            self.assertTrue(d["RunAtLoad"])
            self.assertTrue(d["KeepAlive"],
                            "a daemon that exits leaves the machine unobservable")

    def test_the_join_plist_retries_the_join_on_every_boot(self):
        with scratch_dir() as tmp:
            root, env, _ = self.volume(tmp)
            self.stage(root, env)
            p = root / "Library" / "LaunchDaemons" / "com.wk.tailnet-join.plist"
            d = plistlib.loads(p.read_bytes())
            self.assertEqual(d["ProgramArguments"][-1], "join")
            self.assertTrue(d["ProgramArguments"][-2].endswith("bench/mac-tailnet.sh"))
            self.assertTrue(d["RunAtLoad"])

    def test_the_auth_key_lands_readable_only_by_root_and_the_conf_names_the_node(self):
        with scratch_dir() as tmp:
            root, env, _ = self.volume(tmp)
            self.stage(root, env)
            key = root / "private" / "etc" / "wk" / "tailscale-authkey"
            self.assertEqual(key.read_text(), FAKE_KEY)
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            conf = (root / "private" / "etc" / "wk" / "tailnet.conf").read_text()
            self.assertIn("hostname=tolken-bench\n", conf)
            self.assertIn("tag=tag:wk\n", conf)

    def test_the_node_it_names_is_the_machine_confs_and_not_a_second_copy(self):
        mbp = (REPO / "boot" / "machines" / "mbp.conf").read_text()
        self.assertIn('NODE_BENCH_SSH="tolken-bench"', mbp)
        self.assertNotIn("tolken-bench", SCRIPT.read_text(),
                         "the node name is hardcoded; mbp.conf declares it")

    def test_a_remembered_node_identity_comes_back_on_the_next_volume(self):
        """The fleet's rule: a bench system's tailnet node is kept on the
        filesystem its rewrite never touches, so a fresh install rejoins as
        that node instead of registering a second one under a renamed name."""
        with scratch_dir() as tmp:
            root, env, state = self.volume(tmp)
            self.stage(root, env)
            live = root / "private" / "var" / "db" / "wk" / "tailscale" / "tailscaled.state"
            self.assertFalse(live.exists(), "a fresh volume was given a state file")

            live.write_text("node-key-of-tolken-bench")
            cp = self.stage(root, env, sub="remember")
            self.assertIn("tolken-bench", cp.stderr)
            kept = state / "wk" / "mac-tailnet" / "tolken-bench.state"
            self.assertEqual(kept.read_text(), "node-key-of-tolken-bench")
            self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)

            (tmp / "vol2").mkdir()
            self.stage(tmp / "vol2", env)
            again = (tmp / "vol2" / "private" / "var" / "db" / "wk"
                     / "tailscale" / "tailscaled.state")
            self.assertEqual(again.read_text(), "node-key-of-tolken-bench")
            self.assertEqual(stat.S_IMODE(again.stat().st_mode), 0o600)

    def test_remembering_a_volume_that_has_no_identity_is_not_a_failure(self):
        with scratch_dir() as tmp:
            root, env, state = self.volume(tmp)
            cp = self.stage(root, env, sub="remember")
            self.assertIn("no node identity", cp.stderr)
            self.assertFalse((state / "wk" / "mac-tailnet" / "tolken-bench.state").exists())

    def test_a_privilege_prefix_is_passed_through_to_every_write(self):
        """The volume writer stages a mounted volume through `sudo`; every
        write here must go through the prefix it is handed, or half the payload
        lands as the calling user."""
        with scratch_dir() as tmp:
            root, env, _ = self.volume(tmp)
            log = tmp / "prefix.log"
            with stub_path({"fakesudo": '#!/bin/sh\necho "$*" >> %s\nexec "$@"\n' % log}) as binp:
                e = dict(env)
                e["PATH"] = "%s:%s" % (binp, os.environ["PATH"])
                cp = support.bash('bash %s stage "%s" fakesudo' % (SCRIPT, root), env=e)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            seen = log.read_text()
            for verb in ("install -d", "install -m 0755", "install -m 0600",
                         "install -m 0644"):
                self.assertIn(verb, seen, verb)
            # The intent, not a verb list: no write may reach the volume except
            # through the prefix, so every logged line is a write and every
            # write in the function is prefixed.
            self.assertTrue(seen.strip())
            for line in seen.splitlines():
                self.assertTrue(line.startswith("install "), line)
            body = support.func_body(SCRIPT.read_text(), "cmd_install")
            for write in ("install ", "tee ", "chmod ", "cp ", "mv ", "rm "):
                for line in body.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(write):
                        self.fail(f"unprefixed write in cmd_install: {stripped}")


class TheTwoHalvesSplitOnPrivilegeAndNetwork(unittest.TestCase):
    """`collect` needs a network, a Go toolchain and this machine's auth key,
    and no root. `install` needs root and none of those. That is the whole
    reason the benchmark install can stage itself -- it has passwordless root
    over its own paths and neither a network nor credentials -- and so no step
    of the mac lane asks for a password on the host install."""

    def test_collect_takes_no_privilege_prefix(self):
        """A prefix here would be a root operation on the machine that has the
        credentials, which is the thing being avoided."""
        body = support.func_body(SCRIPT.read_text(), "cmd_collect")
        self.assertNotIn('"$@"', body)
        self.assertNotIn("sudo", body)

    def test_install_reads_no_credential_and_no_network(self):
        body = support.func_body(SCRIPT.read_text(), "cmd_install")
        for forbidden in ("wk_tailscale_authkey", "cmd_build", "bench_node_name",
                          "curl", "go_toolchain", "remembered_state"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_install_refuses_a_directory_that_is_not_a_payload(self):
        """Laying half a payload down is an install that comes up unreachable
        with nothing to say why."""
        with scratch_dir() as tmp:
            (tmp / "empty").mkdir()
            cp = support.bash('bash %s install "%s" "%s"'
                              % (SCRIPT, tmp, tmp / "empty"))
            self.assertNotEqual(0, cp.returncode)
            self.assertIn("not a collected tailnet payload", cp.stderr)

    def test_install_refuses_a_binary_the_bench_install_cannot_run(self):
        with scratch_dir() as tmp:
            d = tmp / "payload"; d.mkdir()
            for name in ("tailscaled", "tailscale"):
                (d / name).write_bytes(b"#!/bin/sh\necho not mach-o\n")
            for name in ("authkey", "tailnet.conf",
                         "com.wk.tailscaled.plist", "com.wk.tailnet-join.plist"):
                (d / name).write_text("x")
            cp = support.bash('bash %s install "%s" "%s"' % (SCRIPT, tmp, d))
            self.assertNotEqual(0, cp.returncode)
            self.assertIn("Mach-O arm64", cp.stderr)

    def test_stage_is_the_two_of_them_and_not_a_third_path(self):
        body = support.func_body(SCRIPT.read_text(), "cmd_stage")
        self.assertIn("cmd_collect", body)
        self.assertIn("cmd_install", body)

    def test_the_autorun_installs_and_joins_with_its_own_sudo(self):
        text = (REPO / "bench" / "mac-bench-autorun.sh").read_text()
        body = support.func_body(text, "converge_self")
        self.assertIn('mac-tailnet.sh" install /', body)
        self.assertIn('mac-tailnet.sh" join', body)
        self.assertIn("sudo -n", body)
        # It must not try the half that needs a network it does not have.
        self.assertNotIn("collect", body)

    def test_the_plant_collects_it_where_the_credentials_are(self):
        body = support.func_body((REPO / "bench" / "mac-ab.sh").read_text(), "phase_plant")
        self.assertIn('mac-tailnet.sh" collect', body)
        self.assertNotIn('mac-tailnet.sh" install', body)


class Firstboot(unittest.TestCase):
    def test_the_first_boot_joins_the_tailnet_after_the_network(self):
        text = FIRSTBOOT.read_text()
        self.assertIn('"$TAILNET" join', text)
        self.assertIn("wk-tools/bench/mac-tailnet.sh", text)
        self.assertLess(text.index("WIFI_SSID"), text.index('"$TAILNET" join'),
                        "the join runs before the network it needs")

    def test_it_no_longer_claims_this_install_can_have_no_tailnet_identity(self):
        text = FIRSTBOOT.read_text()
        self.assertNotIn("No tailnet identity", text)
        self.assertNotIn("publishes no darwin daemon", text)


class SshConfig(unittest.TestCase):
    def stanza(self):
        out, seen = [], False
        for line in SSH_CONFIG.read_text().splitlines():
            if line.startswith("Host "):
                seen = line.split()[1] == "tolken-bench"
                continue
            if seen and line.strip() and not line.startswith("#"):
                out.append(line.strip())
        return out

    def test_the_bench_install_is_reached_by_its_tailnet_name_alone(self):
        """This file's own rule: a node this repo owns is reached by its tailnet
        name and the name is the whole address, so no HostName is written down."""
        keys = [line.split()[0] for line in self.stanza()]
        self.assertIn("HostKeyAlias", keys)
        self.assertIn("User", keys)
        self.assertNotIn("HostName", keys)

    def test_it_no_longer_says_the_bench_install_cannot_take_a_tailnet_identity(self):
        text = SSH_CONFIG.read_text()
        self.assertNotIn("it has no tailnet identity at all", text)
        self.assertNotIn("cannot take a tailnet identity", text)
        self.assertIn("bench/mac-tailnet.sh", text)


def built_here():
    """Where a real build lands: the artifact store, beside ccache and yocto's
    sstate -- not the state directory, which holds records and is walked file by
    file by whatever fingerprints it."""
    out = os.path.join(os.environ.get("WK_STORE",
                                      os.path.expanduser("~/.local/share/wk")),
                       "cache", "mac-tailnet",
                       "darwin-arm64-%s" % field(REL, "TS_VERSION"), "tailscaled")
    return out if os.path.exists(out) else None


class Built(unittest.TestCase):
    """The real build needs a Go toolchain download and 40 seconds of CPU, so
    it is not run here; when this machine has already built it, what came out
    is checked. `wk bench mac` builds it at stage time."""

    @unittest.skipUnless(built_here(), "this machine has not built the darwin tailscaled")
    def test_what_was_built_is_a_mach_o_arm64_executable(self):
        with open(built_here(), "rb") as fh:
            magic, cputype = struct.unpack("<II", fh.read(8))
        self.assertEqual(magic, 0xfeedfacf)
        self.assertEqual(cputype, 0x0100000c)


class WiredIntoTheVolume(WkTest):
    """The daemon reaches a volume only through bench/mac-bench-volume.sh. A
    staging call that is not there is a feature nothing installs."""

    def _func(self, name):
        body = support.func_body(VOLUME.read_text(), name)
        self.assertTrue(body, "%s() is gone from %s" % (name, VOLUME.name))
        return body

    def test_both_host_side_writers_stage_the_daemon(self):
        """A fresh install and --repair each stage it, and neither may be the
        one that forgets. It is no longer inside stage_payload: the benchmark
        install shares that writer and cannot collect a tailnet payload, having
        neither a network nor credentials -- so each caller does its own half."""
        volume = (REPO / "bench" / "mac-bench-volume.sh").read_text()
        for writer in ("do_build_pkg", "do_repair"):
            with self.subTest(writer=writer):
                body = support.func_body(volume, writer)
                self.assertIn("stage_payload", body)
                self.assertIn('mac-tailnet.sh" stage', body)

    def test_the_identity_is_taken_aside_before_the_volume_is_erased(self):
        """startosinstall erases the volume; a reinstall that has not kept the
        node's state rejoins as a second node and is renamed '<name>-1'."""
        body = self._func("do_install")
        remember = body.index("mac-tailnet.sh\" remember")
        self.assertLess(remember, body.index("Resources/startosinstall"))

    def test_the_packaged_client_is_still_tombstoned(self):
        """The pkg whose NetworkExtension panel only a person can answer stays
        removed from the payload, on a path that is not the daemon's."""
        body = self._func("do_repair")
        self.assertIn("Tailscale-macos.pkg", body)
        self.assertNotIn("/etc/wk/", body)


if __name__ == "__main__":
    unittest.main()
