"""`wk sysimage write` (lib/wk/sysimage/write.py, disk.py) against a card machine faked at its Machine: the real Channel's
ssh argv is answered by a card model, so every helper verb, the stream and the unit archive are what the command sends."""
import base64
import contextlib
import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import FLEET_ENV, REPO, requires_machine

sys.path.insert(0, str(REPO / "lib"))

from wk import act, fleet, images, reach, tailnet  # noqa: E402
from wk.machine import HAVE, Result  # noqa: E402
from tests.fakes import Fake  # noqa: E402
from wk.sysimage import cli, disk, write  # noqa: E402

IMAGE = "/imgs/webkit-2.52-yocto-rpi5-64.wic"
KEY = "/keys/id.pub"
AUTHKEY = "/keys/tailscale-authkey"
ENV = {"WK_ROOT": str(REPO), "HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent", "WK_IMAGE_KEY": KEY}
OS_ENV = ("WK_DRY_RUN", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_FORCE", "WK_DEBUG")


class Card:
    """The medium in the reader, as the helper's verbs leave it."""

    def __init__(self, root="PARTUUID=deadbeef-02", ident="deadbeef", mounted=True):
        self.image_root, self.image_ident = root, ident
        self.mounted, self.data, self.node, self.stash = mounted, None, None, None
        self.clear()

    def clear(self):
        self.table = self.data is not None
        self.root, self.ident = (self.image_root, self.image_ident) if self.data is not None else ("", "")
        self.marker = self.key = self.role = self.boot_id = self.tailnet = None
        self.units, self.cmdline, self.config = (), [], []
        self.helper = self.autoboot = self.wifi = self.grown = self.off = False

    def state(self):
        s = dict(vars(self))
        unique = s.pop("ident") not in (self.image_ident, "") and s.pop("root") == "PARTUUID=%s-02" % self.ident
        s.update(unique=unique, marker=(self.marker or "").split("\nbuilt_by=")[0])
        return s


def sent(remote):
    """What the Channel asked of the card machine, as (fn, args): a command under `sh -c`, or a card helper verb."""
    words = shlex.split(remote)
    words = words[2:] if words[:2] == ["sudo", "-n"] else words
    return ("card_priv", *words[1:]) if words[0] == write.CARD_PRIV else ("m_ssh", words[2])


class World:
    def __init__(self, card=None, joins=True, udisks=True, peers=(("other", "100.1.1.1", "up"),), dev="/dev/sdX"):
        self.card, self.joins, self.udisks, self.peers, self.dev = card or Card(), joins, udisks, list(peers), dev
        self.armed = False
        self.fake = Fake("reader")
        self.fake.files.update({IMAGE: "the image's own bytes\n", KEY: "ssh-ed25519 AAAAtest t@x\n", AUTHKEY: "tskey-auth-a-b"})
        self.initial = set(self.fake.files)
        self.fake.react(("ssh",), lambda argv, f: self.answer(sent(argv[-1])))
        self.fake.react(("bash", "-o", "pipefail", "-c"), lambda argv, f: self.piped(argv[4]))
        self.fake.answer(("git",), out="abc1234\n")
        self.calls = []

    def piped(self, command):
        words = shlex.split(command)
        reader, rest = words[:words.index("|")], words[words.index("|") + 1:]
        tail = sent(rest[-1])
        if reader[0] == "tar":
            seed = reader[reader.index("-C") + 1]
            self.card.units = tuple(sorted(p[len(seed) + 1:] for p in self.fake.files if p.startswith(seed + "/")))
            return Result(0, "wk-card-priv: units installed\n")
        data = self.fake.files[reader[-1]]
        self.card.data = hashlib.sha256(data.encode()).hexdigest()
        self.card.node = None
        self.card.clear()
        out = "wk-card-priv: written\nstream_bytes=%d\nstream_sha=%s\n" % (len(data), self.card.data)
        if write.disk.is_second(tail[-1].split()[-1].strip("'")):
            out += "boot_bytes=10\nboot_sha=bb\nroot_bytes=20\nroot_sha=rr\n"
        return Result(0, out)

    def answer(self, tail):
        self.calls.append(tuple(tail))
        fn, args = tail[0], tail[1:]
        c = self.card
        if fn == "m_ssh":
            cmd = args[0]
            if self.armed and ("WK_RECORD" in cmd or "boot_id" in cmd):
                return Result(0, "image=sys-a\narmed_boot_id=b1\n" if "WK_RECORD" in cmd else "b1\n")
            if cmd.endswith(" udisksctl") and "command -v" in cmd:
                return Result(0 if self.udisks else 1)
            if cmd.startswith("udisksctl power-off"):
                c.off = True
            if cmd.startswith("lsblk -no PARTUUID"):
                return Result(0, "%s-01\n%s-02\n" % (c.ident, c.ident))
            return Result(0)
        verb, rest = args[0], args[1:]
        dec = lambda s: base64.b64decode(s).decode()  # noqa: E731
        if verb == "unmount":
            c.mounted = False
            return Result(0)
        if verb == "status":
            return Result(0, "wk-card-priv: ok\n")
        if verb == "check":
            return Result(3, "", "wk-card-priv: REFUSED: mounted filesystem(s) on it\n") if c.mounted else Result(0, "ok\n")
        if verb == "wifi-host":
            return Result(0, "wk-card-priv: wifi-host: yes ssid=Net\n")
        if verb == "tailnet-save":
            c.stash = c.node or c.stash
            return Result(0, "wk-card-priv: kept=%s\n" % ("yes" if c.stash else "no"))
        if verb == "tailnet-restore":
            c.node = c.stash
        if verb == "verify":
            return Result(0, c.data + "\n")
        if verb == "parts":
            return Result(0 if c.table else 1, "p1 p2\n")
        if verb == "root-spec":
            return Result(0, "wk-card-priv: root=%s\nroot=%s\n" % (c.root, c.root) if c.root else "")
        if verb == "retarget":
            c.root = "PARTUUID=%s-02" % c.ident
        if verb == "identity" and rest[1] == c.ident:
            c.root, c.ident = c.root.replace("PARTUUID=%s-" % rest[1], "PARTUUID=%s-" % rest[2]), rest[2]
        if verb in ("cmdline-append", "config-append"):
            (c.cmdline if verb == "cmdline-append" else c.config).append(dec(rest[1]))
        if verb == "boot-id":
            c.boot_id = rest[1]
        if verb == "fleet":
            c.marker, c.key = dec(rest[1]), dec(rest[2])
        if verb == "role":
            c.role = rest[1]
        if verb == "helper":
            c.helper = True
            return Result(0, "wk-card-priv: helper: this machine's card helper and boot-file checker are on it\n")
        if verb == "autoboot":
            c.autoboot = True
        if verb in ("joins", "wifi-joins"):
            what = "tailnet-join" if verb == "joins" else "wifi-join"
            return Result(0, "wk-card-priv: %s: %s\n" % (what, "yes" if self.joins else "no"))
        if verb == "tailnet":
            c.tailnet = tuple(rest[1:])
        if verb == "wifi-from-host":
            c.wifi = True
        if verb == "grow":
            c.grown = True
        return Result(0)

    def state(self):
        return self.card.state(), sorted(set(self.fake.files) - self.initial)

    def run(self, spec=None, grow=False, preset="webkit-2.52-yocto-rpi5-64", role="bench", mach="", src=IMAGE, env=None,
            rand=True):
        rand = (lambda: "%08x" % (len(self.calls) + 1)) if rand is True else rand
        w = write.Write(REPO, dict(ENV, **(env or {})), self.fake, None, rand=rand)
        os.environ.pop("WK_CONFIRMED", None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                w.run(src, spec or "rpi5:" + self.dev, grow, preset, role, mach)
                e = None
            except act.Refused as refused:
                e = refused
        self.err = err.getvalue()
        return e


class WriteTest(unittest.TestCase):
    def setUp(self):
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in OS_ENV:
            os.environ.pop(v, None)
        os.environ.update(WK_YES="1", WK_DESTRUCTIVE="1")
        self.retired = []
        for p in (mock.patch.object(tailnet.Fleet, "key_present", lambda fl: True),
                  mock.patch.object(tailnet.Fleet, "api_present", lambda fl: False),
                  mock.patch.object(tailnet.Fleet, "authkey", lambda fl: AUTHKEY),
                  mock.patch.object(tailnet.Fleet, "retire", lambda fl, n: self.retired.append(n) or Result(0, "retired\n")),
                  mock.patch.object(reach.Reach, "peers", lambda r: self.w.peers)):
            p.start()
            self.addCleanup(p.stop)
        self.w = World()



def quietly(fn, *args, **kw):
    """(result or the Refused, stderr)."""
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args, **kw), err.getvalue()
        except act.Refused as e:
            return e, err.getvalue()


class TestTheWholeWrite(WriteTest):
    def test_a_card_becomes_a_fleet_system_of_its_board(self):
        self.assertIsNone(self.w.run(), self.w.err)
        c = self.w.card
        self.assertTrue(c.state()["unique"])
        self.assertEqual(c.boot_id, "webkit-2.52-yocto-rpi5-64-" + c.data[:12])
        self.assertTrue({"role=bench", "wk_tools=abc1234"} <= set(c.marker.splitlines()))
        self.assertEqual((c.key, c.role, c.helper, c.tailnet, c.wifi, c.off, c.autoboot),
                         ("ssh-ed25519 AAAAtest t@x\n", "bench", True, ("rpi5-bench", "tag:wk"), True, True, False))
        self.assertIn("rootwait=30", c.cmdline[0])
        self.assertIn("os_check=0", c.config[0])
        self.assertIn("systemd/wk-self-return.timer", c.units)
        self.assertEqual([], self.w.state()[1], "the staged units were left behind")
        calls = [c[:2] for c in self.w.calls]
        self.assertLess(calls.index(("card_priv", "unmount")), calls.index(("card_priv", "check")))
        self.assertIn(("card_priv", "boot-check", "/dev/sdX", "bcm2712-rpi-5-b.dtb"), self.w.calls)

    def test_nothing_is_changed_before_the_write_is_confirmed_and_a_stale_node_is_retired_only_after(self):
        os.environ.pop("WK_YES")
        self.w.peers = [("rpi5-bench", "100.1.1.2", "offline")]
        with mock.patch.object(tailnet.Fleet, "api_present", lambda fl: True):
            self.assertIsInstance(self.w.run(), act.Refused)
            self.assertIn("declining (no terminal", self.w.err)
            self.assertEqual((self.w.fake.applied, self.w.card.mounted, self.retired), (0, True, []))
            os.environ["WK_YES"] = "1"
            self.assertIsNone(self.w.run(), self.w.err)
        self.assertEqual(self.retired, ["rpi5-bench"])

    def test_a_board_armed_for_a_one_shot_boot_is_not_written_under(self):
        self.w.armed = True
        self.assertIsInstance(self.w.run(), act.Refused)
        self.assertIn("is armed for system 'sys-a' and has not rebooted yet", self.w.err)
        self.assertEqual(self.w.fake.applied, 0)

    def test_an_image_the_checkout_does_not_know_gets_the_marker_and_key_only(self):
        self.assertIsNone(self.w.run(preset="", mach="rpi5"), self.w.err)
        c = self.w.card
        self.assertEqual((c.units, c.boot_id, c.cmdline), ((), None, []))
        self.assertIn("profile=unknown\n", c.marker)

    def test_a_second_system_keeps_the_rescues_identity_and_the_bench_node_and_gets_the_selector(self):
        self.w.card.node = "rpi5-bench's node"
        self.assertIsNone(self.w.run(spec="rpi5:/dev/sdX@second", grow=True), self.w.err)
        c = self.w.card
        self.assertEqual((c.ident, c.autoboot, c.node, c.grown), ("deadbeef", True, "rpi5-bench's node", True))
        self.assertIn("keeps the rescue disk's identity", self.w.err)
        self.assertIn(("card_priv", "verify", "/dev/sdX@second", "10", "bb", "20", "rr"), self.w.calls)

    def test_a_whole_bench_medium_keeps_its_node_saved_before_the_erase_and_a_rescue_never_does(self):
        w = World(dev="/dev/sda")
        w.card.node = "rpi5-bench's node"
        self.assertIsNone(w.run(), w.err)
        verbs = [c[1] if c[0] == "card_priv" else c[0] for c in w.calls]
        self.assertEqual(sorted(verbs.index(v) for v in ("unmount", "tailnet-save", "verify", "parts", "tailnet-restore", "tailnet")),
                         [verbs.index(v) for v in ("unmount", "tailnet-save", "verify", "parts", "tailnet-restore", "tailnet")])
        self.assertEqual(w.card.node, "rpi5-bench's node")
        self.assertIn("rpi5 is configured to boot from this disk", w.err)
        w = World(dev="/dev/sda")
        self.assertIsNone(w.run(role="rescue"), w.err)
        self.assertEqual((w.card.tailnet[0], w.card.role, w.card.helper), ("rpi5", "rescue", True))
        self.assertNotIn("tailnet-save", [c[1] for c in w.calls if c[0] == "card_priv"])

    def test_the_boot_files_are_checked_only_on_the_board_the_image_is_for_and_need_its_device_tree(self):
        w = World()
        w.fake.files["/imgs/rpi3.wic"] = "x"
        self.assertIsNone(w.run(preset="webkit-2.52-yocto-rpi3-32", src="/imgs/rpi3.wic"), w.err)
        self.assertIn("this is rpi3's image, so this card goes elsewhere", w.err)
        self.assertFalse([c for c in w.calls if c[1:2] == ("boot-check",)])
        with tempfile.TemporaryDirectory() as d:
            conf = (REPO / "machines" / "rpi5.conf").read_text()
            (Path(d) / "rpi5.conf").write_text(re.sub(r"(?m)^dtb=.*\n", "", conf))
            self.assertIsInstance(self.w.run(env={"WK_MACHINES_DIR": d}), act.Refused)
        self.assertIn("(machines/rpi5.conf) sets no dtb", self.w.err)

    def test_a_card_without_its_joiners_is_not_seeded_and_one_without_udisksctl_is_left_on(self):
        w = World(joins=False, udisks=False)
        self.assertIsNone(w.run(), w.err)
        self.assertEqual((w.card.tailnet, w.card.wifi, w.card.off), (None, False, False))
        self.assertIn("rpi5 has no udisksctl", w.err)


class TestIdentity(WriteTest):
    """`unit sysimage.write_identity`: two disks written from one image are two disks, LABEL= images included."""

    def test_two_disks_from_one_image_take_different_identities(self):
        seen = []
        for dev in ("/dev/sdX", "/dev/sdY"):
            w = World(dev=dev)
            self.assertIsNone(w.run(rand=None), w.err)
            self.assertTrue(w.card.state()["unique"])
            seen.append(w.card.ident)
        self.assertNotEqual(seen[0], seen[1])

    def test_a_label_image_is_retargeted_and_an_identity_that_did_not_take_is_refused(self):
        w = World(card=Card(root="LABEL=root"))
        self.assertIsNone(w.run(), w.err)
        self.assertTrue(w.card.state()["unique"], w.card.root)
        self.assertIn("LABEL=root -> a PARTUUID of this disk", w.err)
        w = World()
        w.fake.react(("ssh",), lambda argv, f: Result(0) if sent(argv[-1])[1:2] == ("identity",) else w.answer(sent(argv[-1])))
        self.assertIsInstance(w.run(), act.Refused)
        self.assertIn("did not take the new identity", w.err)


class TestKillpoints(WriteTest):
    def test_a_write_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[sysimage write]`: a re-run streams again, so no half-made card is ever trusted."""
        def run_once(w):
            e = w.run()
            if e is not None:
                raise AssertionError(w.err)
        converges(self, World, run_once, World.state, max_effects=80)

    def test_a_dry_run_says_each_step_and_touches_nothing(self):
        dry = World(card=Card(mounted=False))
        before = dry.state()
        os.environ["WK_DRY_RUN"] = "1"
        self.assertIsNone(dry.run(), dry.err)
        self.assertGreaterEqual(len([l for l in dry.err.splitlines() if l.startswith("  would ")]), 15, dry.err)
        self.assertEqual((dry.state(), dry.fake.applied), (before, 0))
        self.assertIn("dry run -- nothing was written.", dry.err)


class TestTheStream(WriteTest):
    def test_each_extension_names_its_decompressor_and_the_card_machine_runs_it(self):
        self.assertEqual([write.from_filter(p) for p in ("a.wic.xz", "a.img.zst", "a.img.gz", "a.img")],
                         ["xz -dc", "zstd -dc", "gzip -dc", "cat"])
        self.w.fake.files["/imgs/x.wic.xz"] = "compressed"
        self.w.run(src="/imgs/x.wic.xz")
        far = [c for c in self.w.fake.effects if c[0] == "run" and c[1][:2] == ("bash", "-o")][0][1][4]
        self.assertTrue(far.startswith("cat /imgs/x.wic.xz | "), far)
        self.assertIn("xz -dc |", far)
        w = World()
        w.fake.files["/imgs/x.wic.zst"] = "compressed"
        w.fake.react(("ssh",), lambda argv, f: Result(1) if sent(argv[-1]) == ("m_ssh", shlex.join(HAVE + ("zstd",)))
                     else w.answer(sent(argv[-1])))
        self.assertIsInstance(w.run(src="/imgs/x.wic.zst"), act.Refused)
        self.assertIn("rpi5 has no zstd", w.err)
        self.assertIsNone(w.card.data)

    def test_the_meter_passes_bytes_through_counted_and_hashed(self):
        with tempfile.NamedTemporaryFile() as f:
            cp = subprocess.run(["bash", "-c", '"$0" -c "$1" 3>"$2"', sys.executable, write.METER, f.name], input=b"bytes", capture_output=True)
            report = open(f.name).read()
        self.assertEqual(cp.stdout, b"bytes")
        self.assertIn("stream_bytes=5\nstream_sha=%s\n" % hashlib.sha256(b"bytes").hexdigest(), report)


class Channel:
    """A card machine at the Channel, for the steps asked one at a time."""

    def __init__(self, **answers):
        self.answers, self.calls, self.channel = answers, [], "host"

    def card(self, *args, input=None, mutates=False):
        self.calls.append(("card_priv",) + args)
        return Result(*self.answers.get(args[0], (0, "")))

    def run(self, argv, side=None, input=None, mutates=False):
        self.calls.append(("m_ssh", " ".join(argv)))
        return Result(*self.answers.get("m_ssh", (0, "")))


def step(fn, *args, kw=None, env=FLEET_ENV, **answers):
    w = write.Write(REPO, dict(ENV, **env), Fake(), None)
    w.conf, w.ch = {"name": "rescue", "ssh": "rescue"}, Channel(**answers)
    w.piped = lambda reader: w.ch
    got, err = quietly(getattr(w, fn), *args, **kw or {})
    return (act.Refused if isinstance(got, act.Refused) else got), err, w.ch.calls


class TestSteps(WriteTest):
    def test_the_tailnet_save_says_yes_or_no_and_refuses_a_guess(self):
        self.assertEqual([step("tailnet_save", "/dev/sdX@second", **{"tailnet-save": (0, a)})[0] for a in ("kept=yes", "kept=no", "maybe")],
                         [True, False, act.Refused])

    def test_each_helper_refusal_is_the_write_s_with_the_helper_s_words(self):
        got, err, _ = step("check_boot_files", "/dev/sdX", "rpi3", "x.dtb", **{"boot-check": (3, "REFUSED: there is no boot-file checker at x")})
        self.assertIs(got, act.Refused)
        self.assertIn("no boot-file checker", err)
        self.assertIn("could not install the fleet units", step("put_units", "/dev/sdX", {}, units=(1, "REFUSED: not a plain file name"))[1])
        got, err, _ = step("put_units", "/dev/sdX", {}, units=(0, "wk-card-priv: neither systemd nor /etc/init.d here"))
        self.assertIsNone(got)
        self.assertIn("were NOT installed", err)
        got, err, _ = step("simple", "mark", "role", "/dev/sdX", "bench", kw={"why": "no role"}, role=(1, "no space"))
        self.assertIs(got, act.Refused)
        self.assertIn("no role\n    no space", err)
        self.assertIn("did not report what it split", step("verify", "/dev/sdX@second", {"stream_bytes": "1", "stream_sha": "a"})[1])

    def test_a_root_on_the_wrong_kind_of_device_is_refused(self):
        got, err = quietly(write.check_root, "/dev/mmcblk0p2", "/dev/sda", "rpi5", {})
        self.assertIn("expects to boot from an SD card", err)
        self.assertIn("left as written (WK_ANY_ROOT)", quietly(write.check_root, "/dev/mmcblk0p2", "/dev/sda", "rpi5", {"WK_ANY_ROOT": "1"})[1])
        for spec in ("PARTUUID=a-02", "LABEL=x", "/dev/nfs", "", "/dev/sda2"):
            self.assertEqual(quietly(write.check_root, spec, "/dev/sda", "rpi5", {})[0], None)


class TestPreflights(WriteTest):
    """What a write refuses on before anything is erased, none of it crossed by --force."""

    PEERS = [("rpi3-1", "100.1.1.2", "offline"), ("rpi4", "100.1.1.3", "up")]

    def test_a_wifi_board_needs_the_card_machine_on_wifi(self):
        for answer, refused in (((0, "wifi-host: no"), True), ((1, "connection refused"), True), ((0, "wifi-host: yes ssid=N"), False)):
            got, err, _ = step("wifi_preflight", "rpi3", env=dict(FLEET_ENV, WK_FORCE="1"), **{"wifi-host": answer})
            self.assertEqual(got is act.Refused, refused, err)
        got, _, calls = step("wifi_preflight", "mbp", **{"wifi-host": (0, "wifi-host: no")})
        self.assertEqual((got, calls), (None, []))

    def test_a_name_on_the_tailnet_is_retired_or_refused_and_an_unreadable_view_refuses(self):
        self.w.peers = self.PEERS
        for name, refused in (("rpi4", True), ("rpi3", True), ("rpi5", False), ("", False)):
            got, err, _ = step("name_preflight", name, "bench", "rpi3")
            self.assertEqual(got is act.Refused, refused, (name, err))
        self.assertIn("rpi4-1", step("name_preflight", "rpi4", "bench", "rpi3")[1])
        with mock.patch.object(tailnet.Fleet, "api_present", lambda fl: True):
            self.assertIsNone(step("name_preflight", "rpi4", "bench", "rpi3")[0])
        self.assertEqual(self.retired, ["rpi4"])
        self.w.peers = []
        self.assertIn("cannot be skipped", step("name_preflight", "rpi3", "bench", "rpi3")[1])

    def test_only_a_rescue_replacing_itself_is_a_barrier_force_crosses(self):
        self.w.peers = [("rescue", "100.1.1.4", "up")]
        self.assertIn("running rescue", step("name_preflight", "rescue", "rescue", "rescue")[1])
        os.environ["WK_FORCE"] = "1"
        got, err, _ = step("name_preflight", "rescue", "rescue", "rescue")
        self.assertEqual(got, None)
        self.assertIn("FORCED past a barrier", err)
        self.assertIs(step("name_preflight", "rescue", "bench", "rescue")[0], act.Refused)

    def test_a_tailnet_key_and_a_name_to_join_under_are_needed(self):
        self.assertIn("--machine", step("key_preflight", "", "bench")[1])
        with mock.patch.object(tailnet.Fleet, "key_present", lambda fl: False):
            self.assertIn("wk key set tailnet", step("key_preflight", "rpi3", "bench")[1])
        self.assertIsNone(step("key_preflight", "rpi3", "bench")[0])

    def test_the_names_and_the_wifi_a_board_takes_come_from_its_conf(self):
        f = fleet.Fleet(REPO, FLEET_ENV)
        self.assertEqual([write.tailnet_name(f, b, r) for b, r in (("rpi3", "bench"), ("rpi4", "rescue"), ("benchvm", "bench"))],
                         ["rpi3-bench", "rpi4-rescue", ""])
        self.assertEqual([m for m in ("rpi3", "rpi4", "rpi5", "mbp", "benchvm", "") if write.wants_wifi(f, m)], ["rpi3", "rpi4", "rpi5"])
        bad = [n for n in images.names() if images.load(n)["IMG_BUILDER"] in images.WS_BUILDERS and not write.wants_wifi(f, images.load(n)["IMG_MACHINE"])]
        self.assertEqual(bad, [], "a board image for a board with no wifi seed")


class TestWhatTheImageSays(WriteTest):
    STORE = "/store"
    YOCTO = STORE + "/ws/yocto-wpewebkit-2.46-yocto-rpi5-64/build/CrossToolChains/x/build/image/x.wic.xz"

    def resolved(self, spec, answer=(1, ""), local=False):
        m = Fake()
        m._set_file(self.YOCTO, "image")
        m.dirs.add(self.STORE + "/ws/yocto-webkit-2.52-yocto-rpi3-32")
        m.answer((os.path.join(str(REPO), "wk"), "sysimage", "path"), *answer)
        w = write.Write(REPO, {"WK_ROOT": str(REPO)}, m, mock.Mock(store_dir=lambda: self.STORE))
        with mock.patch("wk.store.Store.is_local", lambda st: local):
            got, err = quietly(w.resolve, spec)
        return (None if isinstance(got, act.Refused) else got), err

    def test_an_image_preset_resolves_to_the_image_its_workspace_holds_and_a_path_stays_a_path(self):
        self.assertEqual(self.resolved("wpewebkit-2.46-yocto-rpi5-64")[0], self.YOCTO)
        for spec in ("/tmp/some.wic.xz", "vm:/var/lib/x.img", "./out.img", "../out.img"):
            self.assertEqual(self.resolved(spec)[0], spec)
        far = "/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/build/i.wic.xz"
        self.assertEqual(self.resolved("webkit-2.52-yocto-rpi5-64", (0, far + "\r\n"))[0], "vm:" + far)
        self.assertEqual(self.resolved("webkit-2.52-yocto-rpi5-64", (0, far), local=True)[0], far)

    def test_what_resolves_to_nothing_is_refused_naming_the_build_or_what_is_built(self):
        self.assertIn("wk sysimage build webkit-2.52-yocto-rpi3-32", self.resolved("webkit-2.52-yocto-rpi3-32")[1])
        path, err = self.resolved("nonsense")
        self.assertIn("neither a path nor an image preset", err)
        self.assertIn("      wpewebkit-2.46-yocto-rpi5-64\n", err)
        self.assertNotIn("webkit-2.52-yocto-rpi3-32", err)

    def test_a_workspace_path_names_its_preset_and_a_preset_that_does_not_parse_refuses(self):
        w = write.Write(REPO, ENV, Fake(), None)
        name, p = quietly(w.image_preset, "", "vm:/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64-armb/build/x.wic.xz")[0]
        self.assertEqual((name, p["IMG_MACHINE"]), ("webkit-2.52-yocto-rpi5-64", "rpi5"))
        self.assertEqual(quietly(w.image_preset, "", "/tmp/x.img")[0], ("", {}))
        with mock.patch.object(write.images, "load", side_effect=write.images.ConfError("bad.conf:3: not a KEY=value line")):
            self.assertIn("bad.conf:3:", quietly(w.image_preset, "bad", "/tmp/x.img")[1])

    def test_the_board_s_firmware_lines_lead_the_preset_s(self):
        with tempfile.TemporaryDirectory() as d:
            board, spec = Path(d, "image", "boards", "b"), Path(d, "spec")
            for where, text in ((board, "board"), (spec, "preset")):
                where.mkdir(parents=True)
                (where / "config.txt.append").write_text(text + "=1\n")
                (where / "cmdline.txt.append").write_text("# a comment\n" + text + "=1\n\n")
            p = {"IMG_MACHINE": "b", "IMG_SPEC_DIR": str(spec)}
            self.assertEqual((write.config_add(d, p), write.cmdline_add(d, p)), ("board=1\npreset=1\n", "board=1 preset=1"))
        for n in [n for n in images.names() if images.load(n)["IMG_MACHINE"] == "rpi5"]:
            self.assertIn("os_check=0", write.config_add(REPO, images.load(n)), n)
        self.assertEqual([d.name for d in (REPO / "image" / "boards").iterdir() if not (REPO / "machines" / (d.name + ".conf")).exists()], [])

    def test_the_selector_and_the_units_are_the_board_s(self):
        """An autoboot.txt on the rpi4's stick would make its tryboot flag boot the stick's second pair."""
        w = write.Write(REPO, ENV, Fake(), None)
        self.assertEqual([m for m in ("rpi3", "rpi4", "rpi5", "mbp") if w.selects_by_partition(m)], ["rpi5"])
        predicate = (REPO / "boot" / "onboard" / "watchdog-present.sh").read_text()
        units = quietly(write.stage_units, REPO, "600", "")[0]
        for unit in ("wk-self-return.timer", "S99wk-self-return"):
            self.assertIn(unit, predicate)
            self.assertTrue([u for u in units if u.endswith("/" + unit)], unit)
        units = quietly(write.stage_units, REPO, "900", write.driver_class("pi-sd")(REPO, {"name": "rpi3"}, None).self_disarm_sh())[0]
        line = [l for l in units["systemd/wk-self-disarm.service"].splitlines() if l.startswith("ExecStart=")][0]
        self.assertNotRegex(line, r"(?<!\$)\$(?!\$)", "systemd would expand the script's variables")


LSBLK = """{"blockdevices": [
  {"name":"/dev/loop0", "size":"4K", "tran":null, "rm":false, "type":"loop", "model":null},
  {"name":"/dev/sda", "size":"59.5G", "tran":"usb", "rm":true, "type":"disk", "model":"STORAGE DEVICE  ",
   "children": [{"name":"/dev/sda1", "type":"part", "label":"bootfs"}, {"name":"/dev/sda2", "type":"part", "label":"root"}]},
  {"name":"/dev/sdb", "size":"28.7G", "tran":"usb", "rm":false, "type":"disk", "model":"Extreme"},
  {"name":"/dev/mmcblk0", "size":"29.7G", "tran":"mmc", "rm":"0", "type":"disk", "model":null,
   "children": [{"name":"/dev/mmcblk0p1", "type":"part", "label":"boot"}]},
  {"name":"/dev/nvme0n1", "size":"476.9G", "tran":"nvme", "rm":false, "type":"disk", "model":"WD SN740"}]}"""
BOOTED = "wk-card-priv: REFUSED: '/dev/nvme0n1' is a disk this machine is running from (/, /boot, swap or root=)."


class TestTheDisks(unittest.TestCase):
    """The disks a card machine offers, from its lsblk and its helper's word on each (`unit sysimage.write_refusals`)."""

    def disks(self, lsblk=LSBLK, device="/dev/sda", **answers):
        ch = Channel(**dict({"m_ssh": (0, lsblk), "whose": (0, "marker: none\n"), "status": (0, "ok"), "check": (0, "ok")}, **answers))
        return disk.Disks(ch, {"name": "rpi5", "device": device}), ch

    def test_candidates_are_removable_or_usb_or_mmc_whole_disks(self):
        got = disk.parse_lsblk(LSBLK)
        self.assertEqual([(d.name, d.rm, d.labels) for d in got],
                         [("/dev/sda", "1", ["bootfs", "root"]), ("/dev/sdb", "0", []), ("/dev/mmcblk0", "0", ["boot"])])
        self.assertIsInstance(quietly(disk.parse_lsblk, "lsblk: unknown option -- 'J'\n")[0], act.Refused)
        self.assertEqual((disk.parse_spec("rpi5:/dev/sda@second"), disk.parse_spec("rpi5")), (("rpi5", "/dev/sda@second"), ("rpi5", "")))
        self.assertEqual([disk.tran_of_name(d) for d in ("/dev/sda", "/dev/mmcblk0", "/dev/nvme0n1", "/dev/vda")], ["usb", "mmc", "nvme", ""])

    def test_each_disk_says_what_holds_it_and_a_moved_kernel_name_is_warned(self):
        d, ch = self.disks()
        ch.card = lambda *a, input=None, mutates=False: Result(0, "machine=rpi5\n" if a[1:] == ("/dev/sdb",) else "marker: none\n")
        text, err = quietly(d.listing)
        self.assertIn("    /dev/sda 59.5G usb 1 disk STORAGE DEVICE   <- rpi5 is configured to boot from this one (wk boot rpi5)\n"
                      "        labels: bootfs,root  --  no wk system on it", text)
        self.assertIn("        empty -- no partition table  --  holds this machine's own system", text)
        self.assertIn("rpi5's conf says it boots /dev/sda, but the disk holding\n  rpi5's own system is /dev/sdb", err)
        self.assertEqual((d.for_machine("rpi5"), d.for_machine("rpi4")), ("/dev/sdb", ""))
        self.assertEqual(quietly(self.disks('{"blockdevices": []}')[0].listing)[0], "    (none -- no removable disk is attached to rpi5)")

    def test_a_write_asks_the_helper_s_rule_and_refuses_in_its_words(self):
        d, ch = self.disks()
        self.assertIsNone(quietly(d.refuse_unless_safe, "/dev/sda@third")[0])
        self.assertIn(("card_priv", "check", "/dev/sda@third"), ch.calls)
        d, ch = self.disks(status=(1, "sudo: a password is required"))
        self.assertIn("sudo -n /usr/local/libexec/wk-card-priv status", quietly(d.refuse_unless_safe, "/dev/sda")[1])
        self.assertNotIn(("card_priv", "check", "/dev/sda"), ch.calls)
        d, _ = self.disks(check=(3, BOOTED))
        err = quietly(d.refuse_unless_safe, "/dev/nvme0n1")[1]
        self.assertIn("rpi5 will not write /dev/nvme0n1:\n    " + BOOTED, err)
        self.assertIn("    Disks there:\n    /dev/sda 59.5G usb", err)

    def test_the_disks_verb_asks_the_machine_the_conf_names(self):
        m = Fake()
        m.react(("ssh",), lambda argv, f: Result(0, LSBLK if "lsblk" in argv[-1] else "marker: none\n"))
        s = cli.Sysimage(mock.Mock(env={"WK_ROOT": str(REPO), "WK_MACHINES_DIR": str(REPO / "machines")}, machine=m, store=None), clock=None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc, err = quietly(s.disks, "rpi4")
        self.assertEqual(rc, 0, err)
        self.assertIn("/dev/mmcblk0 29.7G mmc 0 disk", out.getvalue())
        self.assertEqual({e[1][-2] for e in m.effects if e[1][0] == "ssh"}, {"rpi4-rescue"})
        for name in ("", "nosuch"):
            self.assertIsInstance(quietly(s.disks, name)[0], act.Refused)


class TestLive(unittest.TestCase):
    @requires_machine("rpi5")
    def test_write_a_card_on_every_board(self):
        """`live sysimage.write[<board>]`: an image written onto a real card on each board's reader, read back and booted."""
        self.skipTest("owed: needs a card in rpi5's reader and a board to boot it; not run from this suite")

    @requires_machine("rpi5")
    def test_every_card_verb_on_a_real_card(self):
        """`live sysimage.card_verbs[rpi5]`: every card verb against a real card, read back before unmount."""
        self.skipTest("owed: needs a card in rpi5's reader; not run from this suite")


if __name__ == "__main__":
    unittest.main()
