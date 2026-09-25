"""`wk sysimage write` (lib/wk/sysimage/write.py) against a card machine faked at its Machine: the real Channel's
ssh argv is answered by a card model, so every helper verb, the stream and the unit archive are what the command sends.
Closes `unit sysimage.write_identity` and `unit killpoints[sysimage write]`; `live sysimage.write[<board>]` and
`live sysimage.card_verbs[rpi5]` skip by name. No real disk, helper or credential is touched.

Run: python3 tests/run.py --unit -k test_sysimage_write
"""
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
from tests.support import REPO, requires_machine

sys.path.insert(0, str(REPO / "lib"))

from wk import act, reach, shell, tailnet  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import write  # noqa: E402

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
            if cmd.startswith("command -v udisksctl"):
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
            return Result(0, "wk-card-priv: ok\nsecond=yes\nthird=yes\ntailnet-keep=yes\n")
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

    def run(self, spec=None, grow=False, profile="webkit-2.52-yocto-rpi5-64", role="bench", mach="", src=IMAGE, env=None,
            rand=True):
        rand = (lambda: "%08x" % (len(self.calls) + 1)) if rand is True else rand
        w = write.Write(REPO, dict(ENV, **(env or {})), self.fake, None, rand=rand)
        os.environ.pop("WK_CONFIRMED", None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                w.run(src, spec or "rpi5:" + self.dev, grow, profile, role, mach)
                e = None
            except act.Refused as refused:
                e = refused
        self.plan, self.err = w.plan, err.getvalue()
        return e


class WriteTest(unittest.TestCase):
    def setUp(self):
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in OS_ENV:
            os.environ.pop(v, None)
        os.environ.update(WK_YES="1", WK_DESTRUCTIVE="1")
        for name, value in (("key_present", True), ("api_present", False)):
            p = mock.patch.object(tailnet.Fleet, name, lambda fl, v=value: v)
            p.start()
            self.addCleanup(p.stop)
        self.retired = []
        p = mock.patch.object(reach.Reach, "peers", lambda r: self.w.peers)
        p.start()
        self.addCleanup(p.stop)
        for name, fn in (("authkey", lambda fl: AUTHKEY),
                         ("retire", lambda fl, n: self.retired.append(n) or Result(0, "retired\n"))):
            p = mock.patch.object(tailnet.Fleet, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.w = World()


class TestTheWholeWrite(WriteTest):
    def test_a_card_becomes_a_fleet_system_of_its_board(self):
        self.assertIsNone(self.w.run(), self.w.err)
        c = self.w.card
        self.assertFalse(c.mounted)
        self.assertTrue(c.state()["unique"])
        self.assertEqual(c.boot_id, "webkit-2.52-yocto-rpi5-64-" + c.data[:12])
        self.assertIn("role=bench\n", c.marker)
        self.assertIn("wk_tools=abc1234\n", c.marker)
        self.assertEqual(c.key, "ssh-ed25519 AAAAtest t@x\n")
        self.assertEqual((c.role, c.helper, c.tailnet, c.wifi, c.off), ("bench", True, ("rpi5-bench", "tag:wk"), True, True))
        self.assertIn("rootwait=30", c.cmdline[0])
        self.assertIn("os_check=0", c.config[0])
        self.assertIn("systemd/wk-self-return.timer", c.units)
        self.assertFalse(c.autoboot, "a whole-disk write got the two-system selector")
        self.assertEqual([], self.w.state()[1], "the staged units were left behind")

    def test_nothing_is_changed_before_the_write_is_confirmed(self):
        os.environ.pop("WK_YES")
        e = self.w.run()
        self.assertIsInstance(e, act.Refused)
        self.assertIn("declining (no terminal", self.w.err)
        self.assertEqual(self.w.fake.applied, 0)
        self.assertTrue(self.w.card.mounted)

    def test_a_board_armed_for_a_one_shot_boot_is_not_written_under(self):
        self.w.armed = True
        self.assertIsInstance(self.w.run(), act.Refused)
        self.assertIn("is armed for system 'sys-a' and has not rebooted yet", self.w.err)
        self.assertEqual(self.w.fake.applied, 0)

    def test_an_automounted_card_is_unmounted_before_the_helper_is_asked(self):
        self.assertIsNone(self.w.run(), self.w.err)
        calls = [c[:2] for c in self.w.calls]
        self.assertLess(calls.index(("card_priv", "unmount")), calls.index(("card_priv", "check")))

    def test_an_image_the_checkout_does_not_know_gets_the_marker_and_key_only(self):
        e = self.w.run(profile="", mach="rpi5", src=IMAGE)
        self.assertIsNone(e, self.w.err)
        c = self.w.card
        self.assertIsNotNone(c.marker)
        self.assertEqual((c.units, c.boot_id, c.cmdline), ((), None, []))
        self.assertIn("profile=unknown\n", c.marker)

    def test_a_second_system_keeps_the_rescues_identity_and_gets_the_selector(self):
        self.w.card.node = "rpi5-bench's node"
        self.assertIsNone(self.w.run(spec="rpi5:/dev/sdX@second", grow=True), self.w.err)
        c = self.w.card
        self.assertEqual(c.ident, "deadbeef", "a second system restamped the rescue disk's identity")
        self.assertIn("keeps the rescue disk's identity", self.w.err)
        self.assertTrue(c.autoboot, "rpi5-usb selects by partition, so its second pair needs autoboot.txt")
        self.assertEqual(c.node, "rpi5-bench's node", "the bench node's identity was not put back")
        self.assertIn(("card_priv", "verify", "/dev/sdX@second", "10", "bb", "20", "rr"), self.w.calls)
        self.assertTrue(c.grown)

    def test_a_rescue_takes_the_rescue_name_and_the_helper_too(self):
        """A rescue writes bench media with the helper, a bench system arms its sibling with it."""
        self.assertIsNone(self.w.run(role="rescue"), self.w.err)
        self.assertEqual((self.w.card.tailnet[0], self.w.card.role, self.w.card.helper), ("rpi5", "rescue", True))
        self.assertFalse([c for c in self.w.calls if c[1:2] == ("tailnet-save",)])

    def test_the_boot_files_are_checked_only_on_the_board_the_image_is_for(self):
        self.w.run()
        self.assertIn(("card_priv", "boot-check", "/dev/sdX", "bcm2712-rpi-5-b.dtb"), self.w.calls)
        w = World()
        w.fake.files["/imgs/rpi3.wic"] = "x"
        self.assertIsNone(w.run(profile="webkit-2.52-yocto-rpi3-32", src="/imgs/rpi3.wic"), w.err)
        self.assertIn("this is rpi3's image, so this card goes elsewhere", w.err)
        self.assertFalse([c for c in w.calls if c[1:2] == ("boot-check",)])

    def test_a_board_whose_conf_names_no_device_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            conf = (REPO / "machines" / "rpi5.conf").read_text()
            (Path(d) / "rpi5.conf").write_text(re.sub(r"(?m)^NODE_DTB=.*\n", "", conf))
            self.assertIsInstance(self.w.run(env={"WK_MACHINES_DIR": d}), act.Refused)
        self.assertIn("(machines/rpi5.conf) sets no NODE_DTB", self.w.err)

    def test_a_card_without_its_joiners_is_not_seeded(self):
        w = World(joins=False)
        self.assertIsNone(w.run(), w.err)
        self.assertEqual((w.card.tailnet, w.card.wifi), (None, False))

    def test_a_machine_without_udisksctl_says_so_and_the_write_stands(self):
        w = World(udisks=False)
        self.assertIsNone(w.run(), w.err)
        self.assertIn("rpi5 has no udisksctl", w.err)
        self.assertIn("udisks2", w.err)
        self.assertFalse(w.card.off)


class TestTheBenchNode(WriteTest):
    """A bench system rewritten on a board's own bench medium, whole or @second, keeps its tailnet node: saved before
    anything is erased, the name preflight stood down, put back once the new partitions are there."""

    def test_a_whole_bench_medium_keeps_its_node_too(self):
        w = World(dev="/dev/sda")
        w.card.node = "rpi5-bench's node"
        self.assertIsNone(w.run(), w.err)
        verbs = [c[1] if c[0] == "card_priv" else c[0] for c in w.calls]
        self.assertLess(verbs.index("unmount"), verbs.index("tailnet-save"), "read off a card still mounted")
        self.assertLess(verbs.index("tailnet-save"), verbs.index("verify"), "saved after the card was erased")
        self.assertLess(verbs.index("parts"), verbs.index("tailnet-restore"))
        self.assertLess(verbs.index("tailnet-restore"), verbs.index("tailnet"))
        self.assertEqual(w.card.node, "rpi5-bench's node")
        self.assertIn("this system's own node, kept across the rewrite", w.err)
        self.assertIn("rpi5 is configured to boot from this disk", w.err)

    def test_a_rescue_never_keeps_one(self):
        w = World(dev="/dev/sda")
        self.assertIsNone(w.run(role="rescue"), w.err)
        self.assertNotIn("tailnet-save", [c[1] for c in w.calls if c[0] == "card_priv"])


class TestWhatTheImageSays(WriteTest):
    def test_a_workspace_path_names_its_profile(self):
        w = write.Write(REPO, ENV, Fake(), None)
        with contextlib.redirect_stderr(io.StringIO()):
            name, p = w.profile("", "vm:/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64-armb/build/x.wic.xz")
        self.assertEqual((name, p["IMG_MACHINE"]), ("webkit-2.52-yocto-rpi5-64", "rpi5"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(w.profile("", "/tmp/x.img"), ("", {}))

    def test_the_board_fact_leads_the_overclock(self):
        text = write.config_add(REPO, write.images.load("webkit-2.52-yocto-rpi5-64-oc"))
        self.assertLess(text.index("os_check=0"), text.index("arm_freq=2800"))

    def test_the_selector_is_only_written_where_the_firmware_uses_it(self):
        """An autoboot.txt on the rpi4's stick would make its tryboot flag boot the stick's second pair."""
        w = write.Write(REPO, ENV, Fake(), None)
        self.assertEqual([m for m in ("rpi3", "rpi4", "rpi5", "mbp") if w.selects_by_partition(m)], ["rpi5"])

    def test_the_watchdog_units_are_the_ones_the_board_looks_for(self):
        predicate = (REPO / "boot" / "onboard" / "watchdog-present.sh").read_text()
        with contextlib.redirect_stderr(io.StringIO()):
            units = write.stage_units(REPO, "600", "")
        for unit in ("wk-self-return.timer", "S99wk-self-return"):
            self.assertIn(unit, predicate)
            self.assertTrue([u for u in units if u.endswith("/" + unit)], unit)

    def test_a_self_disarm_unit_doubles_every_dollar(self):
        """systemd would expand the script's variables otherwise (pi-mbr's parks the partition type byte)."""
        d = write.driver_class("pi-mbr")(REPO, {"NODE_NAME": "rpi4"}, None)
        with contextlib.redirect_stderr(io.StringIO()):
            units = write.stage_units(REPO, "900", d.self_disarm_sh())
        line = [l for l in units["systemd/wk-self-disarm.service"].splitlines() if l.startswith("ExecStart=")][0]
        self.assertNotRegex(line, r"(?<!\$)\$(?!\$)", line)
        self.assertIn("$$mp", line)


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

    def test_a_label_image_is_retargeted_so_its_root_names_this_disk(self):
        w = World(card=Card(root="LABEL=root"))
        self.assertIsNone(w.run(), w.err)
        self.assertTrue(w.card.state()["unique"], w.card.root)
        self.assertIn("LABEL=root -> a PARTUUID of this disk", w.err)

    def test_an_identity_that_did_not_take_is_refused(self):
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

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        wet = World(card=Card(mounted=False))
        self.assertIsNone(wet.run(), wet.err)
        dry = World(card=Card(mounted=False))
        before = dry.state()
        os.environ["WK_DRY_RUN"] = "1"
        self.assertIsNone(dry.run(), dry.err)
        self.assertEqual(dry.plan, wet.plan)
        self.assertGreaterEqual(len(dry.plan), 15)
        self.assertEqual((dry.state(), dry.fake.applied), (before, 0))
        self.assertIn("dry run -- nothing was written.", dry.err)


class TestTheStream(WriteTest):
    def test_the_card_machine_decompresses_and_meters(self):
        self.w.fake.files["/imgs/x.wic.xz"] = "compressed"
        self.w.run(src="/imgs/x.wic.xz")
        far = [c for c in self.w.fake.effects if c[0] == "run" and c[1][:2] == ("bash", "-o")][0][1][4]
        self.assertTrue(far.startswith("cat /imgs/x.wic.xz | "), far)
        self.assertIn("exec 3>&1; xz -dc | python3 -c", far)
        self.assertIn("sudo -n /usr/local/libexec/wk-card-priv write", far)

    def test_a_decompressor_the_card_machine_lacks_is_refused_by_name(self):
        self.w.fake.files["/imgs/x.wic.zst"] = "compressed"
        self.w.fake.react(("ssh",), lambda argv, f: Result(1) if sent(argv[-1]) == ("m_ssh", "command -v zstd >/dev/null")
                          else self.w.answer(sent(argv[-1])))
        self.assertIsInstance(self.w.run(src="/imgs/x.wic.zst"), act.Refused)
        self.assertIn("rpi5 has no zstd", self.w.err)
        self.assertIsNone(self.w.card.data)

    def test_the_meter_passes_bytes_through_counted_and_hashed(self):
        with tempfile.NamedTemporaryFile() as f:
            cp = subprocess.run(["bash", "-c", '"$0" -c "$1" 3>"$2"', sys.executable, write.METER, f.name],
                                input=b"bytes", capture_output=True)
            report = open(f.name).read()
        self.assertEqual(cp.stdout, b"bytes")
        self.assertIn("stream_bytes=5\nstream_sha=%s\n" % hashlib.sha256(b"bytes").hexdigest(), report)

    def test_a_second_write_that_reported_no_split_cannot_be_verified(self):
        w = write.Write(REPO, ENV, Fake(), None)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            w.verify("/dev/sdX@second", {"stream_bytes": "1", "stream_sha": "a"})
        self.assertIn("did not report what it split", err.getvalue())



class Channel:
    """A card machine at the Channel, for the steps asked one at a time."""

    def __init__(self, **answers):
        self.answers, self.calls, self.channel = answers, [], "host"

    def call(self, fn, *args, input=None, mutates=False):
        self.calls.append((fn,) + args)
        rc, out = self.answers.get(args[0] if fn == "card_priv" else fn, (0, ""))
        return Result(rc, out)


def step(fn, *args, **answers):
    w = write.Write(REPO, ENV, Fake(), None)
    w.conf, w.ch = {"NODE_NAME": "rescue", "NODE_SSH": "rescue"}, Channel(**answers)
    w.piped = lambda reader: w.ch
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return getattr(w, fn)(*args), err.getvalue(), w.ch.calls
        except act.Refused:
            return act.Refused, err.getvalue(), w.ch.calls


class TestSteps(WriteTest):
    def test_the_tailnet_save_says_yes_or_no_and_refuses_a_guess(self):
        cap = "wk-card-priv: ok\ntailnet-keep=yes\n"
        self.assertEqual(step("tailnet_save", "/dev/sdX@second", status=(0, cap), **{"tailnet-save": (0, "kept=yes")})[0], True)
        self.assertEqual(step("tailnet_save", "/dev/sdX@second", status=(0, cap), **{"tailnet-save": (0, "kept=no")})[0], False)
        got, err, _ = step("tailnet_save", "/dev/sdX@second", status=(0, "second=yes"))
        self.assertEqual(got, False)
        self.assertIn("cannot keep", err)
        self.assertIs(step("tailnet_save", "/dev/sdX@second", status=(0, cap), **{"tailnet-save": (0, "maybe")})[0],
                      act.Refused)

    def test_no_checker_is_not_checked_and_says_so(self):
        got, err, _ = step("check_boot_files", "/dev/sdX", "rpi3", "x.dtb",
                           **{"boot-check": (3, "wk-card-priv: REFUSED: there is no boot-file checker at ...")})
        self.assertIsNone(got)
        self.assertIn("NOT checked", err)
        got, err, _ = step("check_boot_files", "/dev/sdX", "rpi3", "x.dtb", **{"boot-check": (1, "kernel: kernel8.img")})
        self.assertIs(got, act.Refused)
        self.assertIn("missing files a rpi3 needs", err)

    def test_what_the_units_verb_says_it_did_not_install(self):
        got, err, _ = step("put_units", "/dev/sdX", {}, units=(0, "wk-card-priv: no systemd on this disk; nothing installed"))
        self.assertIs(got, act.Refused)
        self.assertIn("predates BusyBox init scripts", err)
        got, err, _ = step("put_units", "/dev/sdX", {}, units=(0, "wk-card-priv: neither systemd nor /etc/init.d here"))
        self.assertIsNone(got)
        self.assertIn("were NOT installed", err)
        got, err, _ = step("put_units", "/dev/sdX", {}, units=(1, "wk-card-priv: REFUSED: not a plain file name"))
        self.assertIs(got, act.Refused)
        self.assertIn("could not install the fleet units", err)

    def test_an_older_helper_is_told_apart_from_a_refusal(self):
        for fn, verb in (("install_helper", "helper"), ("seed_role", "role"), ("install_autoboot", "autoboot")):
            args = ("/dev/sdX", "bench") if fn == "seed_role" else ("/dev/sdX",)
            got, err, _ = step(fn, *args, **{verb: (2, "usage: wk-card-priv status|check")})
            self.assertIs(got, act.Refused)
            self.assertIn("older than this checkout", err)
            got, err, _ = step(fn, *args, **{verb: (1, "no space")})
            self.assertNotIn("older than this checkout", err)

    def test_a_root_on_the_wrong_kind_of_device_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            write.check_root("/dev/mmcblk0p2", "/dev/sda", "rpi5", {})
        self.assertIn("expects to boot from an SD card", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            write.check_root("/dev/mmcblk0p2", "/dev/sda", "rpi5", {"WK_ANY_ROOT": "1"})
        self.assertIn("left as written (WK_ANY_ROOT)", err.getvalue())
        for spec in ("PARTUUID=a-02", "LABEL=x", "/dev/nfs", "", "/dev/sda2"):
            write.check_root(spec, "/dev/sda", "rpi5", {})


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
