"""The golden macOS base, `wk sysimage build macos-guest-base`, typed into a Mac whose tart holds no base yet."""
import importlib.util
import io
import json
import os
import platform
import subprocess
import sys
import unittest
from unittest import mock

from tests.fakes import FlowTest, Result
from tests.support import REPO, live_selected
from tests.test_doctor_wall import report
from tests.test_guest import GuestStart, Mac
from tests.test_places_flows import BASE_IMAGE, Guests, sealed_inputs
from tests.test_wk_workspace import Flow, said

BASE = "macos-guest-base"
SHA = "a" * 40
CLEAR = "Notification Center:21:1280x800@0,0;Terminal:0:863x499@40,51;"
PANE = "Setup Assistant:0:800x600;Terminal:0:863x499;"
PATH = os.environ["PATH"]


def here(argv, input=None, **env):
    """`argv` run for real on this host's PATH: a world's own PATH holds only its fakes."""
    cp = subprocess.run(argv, input=input, capture_output=True, text=True, env=dict(env, PATH=PATH))
    return Result(cp.returncode, cp.stdout, cp.stderr)


class Unbuilt(Guests):
    """A Mac with tart and the base image pulled but no base; booted, the base's guest agent answers (`agent_down` it
    does not), Setup Assistant's count is `sa` read in turn, provisioning exits `prov_rc` and its screen reads `screen`.
    This tree is committed at SHA unless `dirty`."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        del self.vms["wk-base"], self.files[self.base_ready]
        self.disk, self.sa, self.screen, self.prov_rc, self.agent_down, self.dirty = 140, ["0"], CLEAR, "0", False, False
        self.guest_cmds = []
        t = self.tart
        self.react([t, "get"], lambda a, f: Result(0, json.dumps({"CPU": 4, "Memory": 8192, "Disk": f.disk})))
        self.react([t, "clone"], lambda a, f: (f.vms.__setitem__(a[3], "stopped"), setattr(f, "disk", 140), Result(0))[2])
        self.react([t, "stop"], lambda a, f: (a[-1] in f.vms and f.vms.__setitem__(a[-1], "stopped"), Result(0))[1])
        self.react([t, "set"], self._set)
        self.react([t, "pull"], lambda a, f: (f.pulled.append(a[-1]), Result(0))[1])
        self.answer([t, "prune"])
        self.react([t, "exec"], self._guest)
        self.answer(["du", "-sh"], out="162G\t/x\n")
        self.react(["git", "-C", str(REPO), "rev-parse", "--git-dir"], lambda a, f: Result(0, ".git\n"))
        self.react(["git", "-C", str(REPO), "status"], lambda a, f: Result(0, " M wk\n" if f.dirty else ""))
        self.answer(["git", "-C", str(REPO), "bundle"])
        self.react(["bash", "-c"], lambda a, f: Result(0) if a[2].startswith("set -o pipefail") else here(a))
        self.react(["ssh-keygen"], self._keygen)

    def _set(self, argv, _):
        if "--disk-size" in argv:
            self.disk = int(argv[argv.index("--disk-size") + 1])
        return Result(0)

    def _keygen(self, argv, _):
        key = argv[argv.index("-f") + 1]
        self.files[key], self.files[key + ".pub"] = "PRIVATE\n", "ssh-ed25519 AAAA wk-vm\n"
        return Result(0)

    def _guest(self, argv, _):
        cmd = argv[-1]
        self.guest_cmds.append(cmd)
        if cmd == "date -u +%s":
            return Result(0, "%d\n" % self.clock.now())
        if cmd == "true" and self.agent_down:
            return Result(255, "", "no guest agent\n")
        if "Setup Assistant.app" in cmd:
            return Result(0, (self.sa.pop(0) if len(self.sa) > 1 else self.sa[0]) + "\n")
        if "/tmp/wk-base-provision.rc" in cmd and cmd.startswith("sh -c") and "cat" in cmd and "nohup" not in cmd:
            return Result(0, self.prov_rc + "\n")
        if "wc -c" in cmd:
            return Result(0, "12\n")
        if cmd.startswith("cat /tmp/wk-base-provision.log"):
            return Result(0, "==> base provisioning complete\n")
        if cmd == "bash -s":
            return Result(0, "windows=%s\n" % self.screen)
        if cmd.endswith(SHA):
            return Result(0, SHA + "\n")
        return Result(0)

    def base_state(self):
        return self.vms.get("wk-base", "absent"), self.disk, self.files.get(self.base_ready, "")


class BaseFlow(Flow):
    world_class = Unbuilt

    def make_world(self, **kw):
        return super().make_world(kinds={"fakebox": "vm", "vm": "vm"}, **kw)

    def build(self, *argv, w=None):
        return self.typed("sysimage", "build", BASE, *argv, w=w)

    def tart_did(self):
        return [e[1][1:3] for e in self.w.effects if e[0] == "act" and e[1][0] == self.w.tart and e[1][1] != "exec"]


class Terminal(io.StringIO):
    """A person at a terminal, answering each prompt with the next line."""

    def isatty(self):
        return True


class TestTheBaseIsBuiltOnce(BaseFlow):
    def test_from_nothing_it_is_pulled_cloned_grown_provisioned_rebooted_and_sealed_with_its_inputs_once(self):
        self.w.pulled = []
        rc, err = self.build()
        self.assertEqual((rc, said(err)), (0, "==> golden base 'wk-base' is ready"), err)
        self.assertEqual(self.tart_did(), [("pull", BASE_IMAGE), ("clone", BASE_IMAGE), ("set", "wk-base"), ("set", "wk-base"),
                                           ("set", "wk-base"), ("stop", "wk-base"), ("stop", "wk-base")])
        self.assertEqual([a[1] for a in self.w.streamed], ["pull", "clone"], "the tens-of-GB pull and the clone stream to the log")
        self.assertEqual([e[1] for e in self.w.effects if e[0] == "spawn"], [(self.w.tart, "run", "--no-graphics", "wk-base")] * 2)
        (started,) = [c for c in self.w.guest_cmds if "nohup" in c]
        self.assertIn("WK_MOUNT_MIRROR=/usr/local/libexec/wk-mount-mirror", started)
        self.assertIn("ssh-ed25519 AAAA wk-vm", [c for c in self.w.guest_cmds if "authorized_keys" in c][0])
        state, disk, marker = self.w.base_state()
        self.assertEqual((state, disk, marker.split("finished=")[0]), ("stopped", 320, "image=%s\ninputs=%s\n" % (BASE_IMAGE, sealed_inputs())))
        rc, err = self.build()
        self.assertEqual((rc, self.tart_did(), self.w.base_state()[2]), (0, [], marker), "a sealed base is left alone")

    def test_a_base_that_never_finished_is_rubble_and_is_made_again(self):
        self.w.vms["wk-base"] = "stopped"
        rc, err = self.build()
        self.assertEqual((rc, err.splitlines()[0], self.tart_did()[:3]),
                         (0, "warning: 'wk-base' exists but was never finished -- destroying it and starting again",
                          [("stop", "wk-base"), ("delete", "wk-base"), ("clone", BASE_IMAGE)]))

    def test_each_failure_is_refused_unsealed_naming_what_is_next(self):
        for name, edit, line in (
                ("agent", lambda w: setattr(w, "agent_down", True),
                 "error: the tart guest agent in 'wk-base' never answered (a Cirrus Labs image ships it; a vanilla macOS image does not)."),
                ("provisioning", lambda w: setattr(w, "prov_rc", "3"), "error: base provisioning failed (rc=3). What it printed is in "
                 "/vmstore/vm/base-provision.log; a re-run:  wk sysimage build macos-guest-base --refresh"),
                ("pane back", lambda w: setattr(w, "sa", ["0", "0", "1"]), "error: Setup Assistant came back at 'wk-base''s next login. "
                 "The base is running at 192.168.64.9: answer it at its own window,"),
                ("pane", lambda w: setattr(w, "screen", PANE), "error: on the base's screen, and nothing wk put there: Setup Assistant:0:800x600"),
                ("unreadable", lambda w: setattr(w, "screen", "?"),
                 "error: could not ask 'wk-base' what is on its screen, and a base is not sealed unread.")):
            with self.subTest(name):
                self.w = self.make_world()
                edit(self.w)
                rc, err = self.build()
                self.assertEqual((rc, self.w.rel(said(err)), self.w.base_state()[2]), (1, line, ""))

    def test_setup_assistant_is_driven_off_before_the_reboot_that_judges_it(self):
        self.w.sa = ["1", "0"]
        rc, err = self.build()
        self.assertEqual((rc, [c for c in self.w.guest_cmds if c.startswith("/usr/bin/python3")]), (0, ["/usr/bin/python3 -"]), err)

    def test_a_dry_run_clones_nothing_and_boots_nothing(self):
        before = (dict(self.w.files), dict(self.w.vms))
        rc, err = self.build("--dry-run")
        self.assertIn("would run: %s clone %s wk-base" % (self.w.tart, BASE_IMAGE), err.splitlines())
        self.assertEqual((rc, (self.w.files, self.w.vms), [e for e in self.w.effects if e[0] == "spawn"]), (0, before, []))


class TestItsStalenessIsRecomputed(BaseFlow):
    def base_row(self):
        self.w.env["WK_NAME"] = "ws"
        _, _, err = self.wk(self.w, "doctor", "ws")
        return [r for r in report(err) if "base" in r][0]

    def test_doctor_reads_the_base_absent_unfinished_current_then_stale_each_with_its_remedy(self):
        self.w.make("ws")
        self.w.vms["wk-ws"] = "stopped"
        self.assertEqual(self.base_row(), "-- no golden base VM 'wk-base' to clone a guest from -> wk sysimage build macos-guest-base   (hours)")
        self.w.vms["wk-base"] = "stopped"
        self.assertEqual(self.base_row(), "-- 'wk-base' exists but provisioning never finished in it -> wk sysimage build macos-guest-base --refresh")
        del self.w.vms["wk-base"]
        self.build()
        self.assertEqual(self.base_row(), "ok golden base 'wk-base' matches its provisioning inputs")
        self.w.env["WK_VM_IMAGE"] = "ghcr.io/x@sha256:0"
        self.assertEqual(self.base_row(), "-- 'wk-base' predates its own provisioning inputs: vm/provision-base.sh, vm/desktop.sh, "
                         "vm/mount-mirror.sh, bench/mac-pyobjc.sh, WK_VM_IMAGE or WK_VM_USER has changed since it was built -> "
                         "wk sysimage build macos-guest-base --rebuild   (hours; existing guests are unaffected)")


class TestTheDestructiveModes(BaseFlow):
    def test_a_dirty_tree_is_refused_before_the_question_and_a_clean_one_deletes_and_rebuilds(self):
        self.build()
        self.w.dirty = True
        rc, err = self.build("--rebuild")
        self.assertEqual((rc, said(err), "declining" in err, self.tart_did()),
                         (1, "error: the golden base is given a commit, so it cannot be built from this tree:", False, []))
        self.w.dirty = False
        rc, err = self.build("--rebuild", "--yes")
        self.assertEqual((rc, self.tart_did()[:3]), (0, [("stop", "wk-base"), ("delete", "wk-base"), ("clone", BASE_IMAGE)]), err)

    def test_a_declined_question_changes_nothing(self):
        self.build()
        for mode in ("--rebuild", "--rm"):
            rc, err = self.build(mode)
            self.assertEqual((rc, said(err).split(" --")[0], self.tart_did(), self.w.base_state()[0]), (1, "error: aborted", [], "stopped"))

    def test_rm_asks_for_the_base_then_separately_for_the_pulled_image(self):
        self.build()
        cache = os.path.join(self.w.env["HOME"], ".tart", "cache")
        self.w.files[cache + "/OCIs/x"] = ""
        self.w.dirs.add(cache)
        with mock.patch("sys.stdin", Terminal("y\nn\n")):
            rc, err = self.build("--rm")
        asked = [l.split("? ")[0] for l in err.splitlines() if "[y/N]" in l]
        self.assertEqual((rc, self.w.base_state(), self.tart_did()), (0, ("absent", 320, ""), [("stop", "wk-base"), ("delete", "wk-base")]), err)
        self.assertEqual(asked, ["delete the golden base VM 'wk-base' (162G)", "also drop the pulled image cache (162G)"])
        self.build()
        rc, err = self.build("--rm", "--yes")
        self.assertEqual(self.tart_did()[-1], ("prune", "--space-budget"))

    def test_nothing_to_erase_nothing_to_refresh_or_two_modes_is_refused(self):
        for argv, line in ((["--rm", "--yes"], "error: no golden base 'wk-base' on this machine -- nothing to erase"),
                           (["--refresh"], "error: no golden base yet -- run 'wk sysimage build macos-guest-base'"),
                           (["--rm", "--rebuild"], "error: usage: wk sysimage build macos-guest-base [--refresh|--rebuild|--rm] [--dry-run] -- one of them")):
            rc, err = self.build(*argv)
            self.assertEqual((rc, said(err), self.tart_did()), (1, line, []), argv)


class TestOffAMac(FlowTest):
    def test_the_base_needs_a_macos_host(self):
        with mock.patch("os.uname", return_value=os.uname_result(("Darwin", "mac", "25.0", "", "arm64"))):
            w = Unbuilt(self.tmp, kinds={"fakebox": "vm", "vm": "vm"})
        with mock.patch("os.uname", return_value=os.uname_result(("Linux", "box", "6.1", "", "x86_64"))):
            rc, _, err = self.wk(w, "sysimage", "build", BASE)
        self.assertEqual((rc, said(err), w.mutations()), (1, "error: a macOS guest needs a macOS host (Virtualization.framework)", []))


class TestTheBuilderConforms(BaseFlow):
    def test_path_holds_and_ls_find_the_base_once_it_is_sealed(self):
        """`unit sysimage.builders_conform[guest]`."""
        before = [self.wk(self.w, "sysimage", verb, BASE)[:2] for verb in ("path", "holds")]
        self.assertEqual((before, self.wk(self.w, "sysimage", "ls")[1]), ([(1, ""), (0, "no\n")], ""))
        self.build()
        after = [self.wk(self.w, "sysimage", verb, BASE)[:2] for verb in ("path", "holds")]
        self.assertEqual(after, [(0, self.w.base_ready + "\n"), (0, "yes\n")])
        rc, out, _ = self.wk(self.w, "sysimage", "ls")
        self.assertEqual([l.split() for l in out.splitlines()[1:]], [[BASE, "-", "guest", "ready", "-", "-"], [self.w.base_ready]])


class TestTheBaseFitsBesideThePodmanMachine(BaseFlow):
    def test_an_idle_podman_machine_is_stopped_for_it_and_a_busy_or_unreadable_one_refuses_it(self):
        pod = ["podman", "machine", "inspect", "wk"]
        self.w.answer(pod, out=json.dumps([{"State": "running", "Resources": {"Memory": 16384}}]))
        for ps in (Result(0, "c1\nc2\n"), Result(255)):
            self.w.answer(["podman", "machine", "ssh"], ps.rc, ps.out)
            rc, err = self.build()
            self.assertEqual((rc, said(err), [e for e in self.w.effects if e[0] == "act" and e[1][0] == "podman"]),
                             (1, "error: not enough memory to start 'wk-base'.", []))
        self.w.answer(["podman", "machine", "ssh"])
        self.w.react(["podman", "machine", "stop"], lambda a, f: (f.answer(pod, out=json.dumps([{"State": "stopped"}])), Result(0))[1])
        rc, err = self.build()
        self.assertEqual((rc, [e[1] for e in self.w.effects if e[0] == "act" and e[1][0] == "podman"]),
                         (0, [("podman", "machine", "stop", "wk")]), err)
        self.assertIn("==> stopping the idle podman machine to free 16384MB for 'wk-base'", err.splitlines())


class Podman(Guests):
    """Guests with podman installed: its machine `wk` is answered per test."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        (self.tmp / "bin" / "podman").write_text("")
        (self.tmp / "bin" / "podman").chmod(0o755)
        self.answer([self.tart, "get"], out='{"CPU": 4, "Memory": 8192}')

    def machine_is(self, mb):
        self.answer(["podman", "machine", "inspect", "wk"], out=json.dumps([{"State": "stopped", "Resources": {"Memory": mb}}]))


class ThePodmanMachine(Flow):
    world_class = Podman

    def make_world(self, **kw):
        return super().make_world(kinds={"fakebox": "vm", "container": "container"}, **kw)

    def setUp(self):
        super().setUp()
        self.w.make("g")

    def start(self, mb, **env):
        self.w.machine_is(mb)
        self.w.env.update(env)
        rc, err = self.typed("start")
        return rc, err, [e[1] for e in self.w.effects if e[0] == "act" and e[1][:3] == ("podman", "machine", "start")]


class TestThePodmanMachineIsNotStartedBesideAGuest(ThePodmanMachine):
    def test_a_host_too_small_for_both_refuses_and_starts_nothing(self):
        rc, err, started = self.start(16384)
        self.assertEqual((rc, said(err), started), (1, "error: not starting the podman machine 'wk' (16384MB): 8192MB of macOS guests "
                                                       "are running, and the", []))

    def test_one_that_fits_beside_the_guest_or_with_no_guest_running_is_started(self):
        self.assertEqual(self.start(8192)[::2], (0, [("podman", "machine", "start", "wk")]))
        self.w.vms["wk-g"] = "stopped"
        self.assertEqual(self.start(65536)[::2], (0, [("podman", "machine", "start", "wk")]))

    def test_wk_vm_share_starts_it_anyway_and_says_so(self):
        rc, err, started = self.start(16384, WK_VM_SHARE="1")
        self.assertEqual((rc, err.splitlines()[0], started), (0, "warning: the podman machine 'wk' (16384MB) on top of 8192MB of running "
                                                                  "guests exceeds the 20480MB envelope -- starting it because WK_VM_SHARE is set",
                                                              [("podman", "machine", "start", "wk")]))


class GuestHome(Mac):
    """A Mac whose guest's checkout step runs for real in the directory `guest`, its home, with /Users/admin and the mirror
    share mapped into it."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        self.home = str(self.tmp / "guest")
        self.env["WK_MIRROR_BRANCHES"] = "main"
        self.react([self.tart, "exec"], self._checkout)

    def _map(self, text):
        return text.replace("/Volumes/wk-mirror/mirror", self.home + "/share").replace("/Users/admin", self.home)

    def _checkout(self, argv, f):
        if not argv[-1].startswith("env WK_SRC="):
            return self._guest(argv, f)
        return here(["sh", "-c", self._map(argv[-1])], self._map(self.last_input), HOME=self.home)


FAKE_GIT_WEBKIT = """#!/bin/sh
echo "$*" >> "$HOME/git-webkit.calls"
case "$1" in
    setup)         [ "$2" = --defaults ] || exit 2; git config webkitscmpy.setup true ;;
    install-hooks) ;;
    *)             exit 2 ;;
esac
"""


class TestTheCheckoutIsMadeAtFirstStart(GuestStart):
    world_class = GuestHome

    def setUp(self):
        super().setUp()
        self.mirror = os.path.join(self.w.home, "share", "WebKit.git")
        src = os.path.join(self.w.tmp, "seed")
        os.makedirs(os.path.join(src, "Tools", "Scripts"))
        os.makedirs(os.path.dirname(self.mirror))
        gw = os.path.join(src, "Tools", "Scripts", "git-webkit")
        with open(gw, "w") as f:
            f.write(FAKE_GIT_WEBKIT)
        os.chmod(gw, 0o755)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git", "commit", "-q", "-m", "seed"]):
            subprocess.run(cmd, cwd=src, env=env, check=True)
        subprocess.run(["git", "clone", "-q", "--bare", src, self.mirror], check=True)

    def config(self, key):
        return here(["git", "-C", os.path.join(self.w.home, "WebKit"), "config", "--get-all", key], HOME=self.w.home).out.split()

    def test_the_first_start_clones_wires_and_sets_up(self):
        rc, err = self.start()
        self.assertEqual((rc, self.lines(err, "==> ws's WebKit checkout made")), (0, ["==> ws's WebKit checkout made from its mirror in 0s"]), err)
        self.assertEqual((self.config("webkitscmpy.setup"), self.config("remote.origin.url")), (["true"], ["https://github.com/WebKit/WebKit.git"]))
        with open(os.path.join(self.w.home, ".gitconfig")) as f:
            self.assertIn("dotfiles/gitconfig", f.read(), "the identity include precedes the setup that reads it")
        self.assertTrue(os.path.exists(os.path.join(self.w.home, "WebKit", ".git", "objects", "info", "alternates")),
                        "the clone copied the history rather than sharing the mirror's")

    def test_the_next_start_finds_it_done(self):
        self.start()
        rc, err = self.start()
        with open(os.path.join(self.w.home, "git-webkit.calls")) as f:
            calls = f.read().splitlines()
        self.assertEqual((rc, self.lines(err, "==> ws's WebKit checkout made"), [c for c in calls if c.startswith("setup")]), (0, [], ["setup --defaults"]))

    def test_no_mirror_is_a_failure_that_names_both_remedies(self):
        subprocess.run(["rm", "-rf", self.mirror], check=True)
        rc, err = self.start()
        self.assertEqual(self.lines(err, "warning: ws's checkout"), ["warning: ws's checkout is not wired and set up (above); 'wk sync ws --fix' once it is up"])
        self.assertIn("      'wk stop <name>', then 'wk start <name>' -- or the host has no mirror yet: wk sync", err.splitlines())
        self.assertFalse(os.path.exists(os.path.join(self.w.home, "WebKit")))


# Not reachable from the command line: the picker inside the guest-side unblocker, and the live tier's look at this Mac's own base.
@unittest.skipUnless(platform.system() == "Darwin", "the unblocker imports pyobjc's ApplicationServices, a macOS framework")
class TestSetupAssistantIsDrivenByIdentifier(unittest.TestCase):
    def pick(self, pairs):
        spec = importlib.util.spec_from_file_location("wk_unblock", REPO / "vm" / "desktop-unblock.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._pick([(i, t, object()) for i, t in pairs])[0]

    def test_a_confirmation_sheet_outranks_the_pane_behind_it(self):
        self.assertEqual("action-button-1", self.pick([("Next Button", "Continue"), ("action-button-1", "Skip"),
                                                       ("action-button-2", "Don’t Skip")]))

    def test_the_account_pane_is_declined_through_its_own_menu_item(self):
        self.assertEqual("userDeclinediCloud", self.pick([("Alternate Button", "Other Sign-In Options"),
                                                          ("userDeclinediCloud", "Sign in Later in Settings")]))

    def test_an_ordinary_pane_takes_its_primary_button(self):
        self.assertEqual("Next Button", self.pick([("Next Button", "Continue"), ("Alternate Button", "Only Download Automatically")]))

    def test_the_flow_is_never_walked_backwards_nor_guessed_at(self):
        self.assertIsNone(self.pick([("Previous Button", "Back")]))
        self.assertIsNone(self.pick([("", "")]))


class TestTheLiveBase(unittest.TestCase):
    wk_tier = "live"

    def test_vm_base_matches_pin(self):
        if not live_selected() or sys.platform != "darwin":
            self.skipTest("live tier not selected, or not a macOS host")
        sys.path.insert(0, str(REPO / "lib"))
        from wk import places
        from wk.sysimage import guestbase
        vm = places.Registry(str(REPO)).load("vm")
        if not vm.tart():
            self.skipTest("tart is not installed here")
        base = guestbase.Base(vm)
        if not base.exists():
            self.skipTest("no golden base on this Mac")
        self.assertEqual("", base.stale())
        self.assertEqual(guestbase.image(vm.env), base.field("image"))


if __name__ == "__main__":
    unittest.main()
