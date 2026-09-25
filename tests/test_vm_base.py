"""The golden macOS base, `wk sysimage build macos-guest-base`, against a fake host whose tart keeps one VM's state
and whose ssh answers as the guest would: built once, sealed only on a clear screen, stale when its inputs move,
crash-only, and erased on two separate questions. The base is the one artifact whose mistakes every clone inherits.

Run: python3 tests/run.py --unit -k test_vm_base
"""
import contextlib
import functools
import importlib.util
import inspect
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, assert_guest_start_converges, live_selected

sys.path.insert(0, str(REPO / "lib"))
from wk import act, guest, targets, tools  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import cli, guestbase  # noqa: E402

TART = "/t/tart"
IP = "192.168.64.7"
PROVISION = REPO / "vm" / "provision-base.sh"
CLEAR = "Notification Center:21:1280x800@0,0;Terminal:0:863x499@40,51;"
PANE = "Setup Assistant:0:800x600;Terminal:0:863x499;"


@functools.lru_cache(maxsize=None)
def _bench(argv):
    """This tree's bench libraries, run for real: a reading of them is a pure function of its arguments."""
    return Local().run(list(argv))


class BaseWorld(Fake):
    """One Mac: tart holding at most `wk-base` (in `state`), and a guest behind ssh that says what `sa` and `screen` say."""

    def __init__(self, base):
        super().__init__("here")
        self.env = {"HOME": base + "/home", "WK_STORE": base + "/store", "WK_VM_STORE": base + "/vmstore",
                    "XDG_STATE_HOME": base + "/state", "WK_MACHINES_DIR": base + "/registry", "PATH": os.environ["PATH"]}
        self.state, self.disk, self.cached, self.sa, self.screen, self.prov_rc = "absent", 140, True, ["0"], CLEAR, "0"
        self.guest_cmds, self.dirty = [], False
        self.react([TART, "list"], self._list)
        self.react([TART, "get"], lambda a, f: Result(0, json.dumps({"CPU": 4, "Memory": 8192, "Disk": f.disk})))
        self.react([TART, "clone"], lambda a, f: f._to("stopped", disk=140))
        self.react([TART, "delete"], lambda a, f: f._to("absent"))
        self.react([TART, "stop"], lambda a, f: f._to("stopped" if f.state != "absent" else "absent"))
        self.react([TART, "set"], self._set)
        self.react([TART, "pull"], lambda a, f: f._cache())
        self.answer([TART, "ip"], out=IP + "\n")
        self.answer([TART, "prune"])
        self.answer([TART, "exec"])
        self.answer(["sysctl", "-n", "hw.ncpu"], out="10\n")
        self.answer(["sysctl", "-n", "hw.memsize"], out="34359738368\n")
        self.answer(["podman", "machine", "inspect"], rc=125)
        self.answer(["df", "-Pk", "/"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/d 1 1 524288000 1% /\n")
        self.answer(["pgrep"], rc=1)
        self.answer(["find"])
        self.answer(["chmod"])
        self.answer(["du", "-sh"], out="162G\t/x\n")
        self.react(["git", "-C"], lambda a, f: Result(0, " M wk\n" if f.dirty and "status" in a else ""))
        self.react(["ssh-keygen"], self._keygen)
        self.react(["bash", "-c"], lambda a, f: _bench(tuple(a)))
        self.react(["ssh"], self._guest)

    def _to(self, state, disk=None):
        self.state = state
        if disk is not None:
            self.disk = disk
        return Result(0)

    def _cache(self):
        self.cached = True
        return Result(0)

    def _list(self, argv, _):
        if "oci" in argv:
            return Result(0, json.dumps([{"Name": guestbase.IMAGE}] if self.cached else []))
        vms = [] if self.state == "absent" else [{"Name": "wk-base", "Source": "local", "State": self.state}]
        return Result(0, json.dumps(vms))

    def _set(self, argv, _):
        if "--disk-size" in argv:
            self.disk = int(argv[argv.index("--disk-size") + 1])
        return Result(0)

    def _keygen(self, argv, _):
        key = argv[argv.index("-f") + 1]
        self._set_file(key, "PRIVATE\n")
        self._set_file(key + ".pub", "ssh-ed25519 AAAA wk-vm\n")
        return Result(0)

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        if pid and "run" in argv:
            self.state = "running"
        return pid

    def _guest(self, argv, _):
        cmd = argv[-1]
        self.guest_cmds.append(cmd)
        if "Setup Assistant.app" in cmd:
            return Result(0, (self.sa.pop(0) if len(self.sa) > 1 else self.sa[0]) + "\n")
        if guestbase.PRC in cmd and cmd.startswith("sh -c") and "cat" in cmd and "nohup" not in cmd:
            return Result(0, self.prov_rc + "\n")
        if "wc -c" in cmd:
            return Result(0, "12\n")
        if cmd.startswith("cat ") and guestbase.PLOG in cmd:
            return Result(0, "==> base provisioning complete\n")
        if cmd == "bash -s":
            return Result(0, "windows=%s\n" % self.screen)
        return Result(0)


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-vm-base-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True),
                  mock.patch.object(targets.Vm, "tart", lambda s: TART),
                  mock.patch.object(tools, "push", lambda *a: True),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        for var in ("WK_DRY_RUN", "WK_YES", "WK_CONFIRMED", "WK_DESTRUCTIVE"):
            os.environ.pop(var, None)
        self.w = BaseWorld(self.tmp)
        self.clock = FakeClock()

    def base(self):
        vm = targets.Registry(str(REPO), env=self.w.env, machine=self.w).load("vm")
        return guestbase.Base(vm, self.clock)

    def build(self, *rest, answers=None):
        """(exit or Refused status, stderr); `answers` are the replies to each prompt, in order."""
        prompts, replies = [], list(answers or [])

        def confirm(prompt, stdin=None):
            prompts.append(prompt)
            ok = replies.pop(0) if replies else False
            if ok:
                os.environ["WK_CONFIRMED"] = "1"
            return ok
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(act, "confirm", confirm):
            try:
                rc = self.base().build(list(rest))
            except Refused as e:
                rc = e.status
        self.prompts = prompts
        return rc, err.getvalue()

    def marker(self):
        return self.w.files.get(os.path.join(self.w.env["WK_VM_STORE"], "vm", "base.ready"), "")

    def tart_acts(self):
        return [e[1][1:3] for e in self.w.effects if e[0] == "run" and e[1][0] == TART and e[1][1] not in ("list", "get", "ip")]


class TestTheBaseIsBuiltOnce(BaseTest):
    def test_from_nothing_it_is_cloned_grown_provisioned_rebooted_and_sealed_with_its_inputs(self):
        self.w.cached = False
        rc, err = self.build()
        self.assertEqual(0, rc, err)
        self.assertEqual("stopped", self.w.state)
        self.assertEqual(320, self.w.disk)
        self.assertIn(("pull", guestbase.IMAGE), self.tart_acts())
        self.assertIn(("clone", guestbase.IMAGE), self.tart_acts())
        self.assertIn("inputs=%s\n" % guestbase.inputs_hash(str(REPO), self.w.env), self.marker())
        self.assertIn("image=%s\n" % guestbase.IMAGE, self.marker())
        self.assertIn("golden base 'wk-base' is ready", err)

    def test_a_sealed_base_is_left_alone(self):
        self.build()
        self.w.effects = []
        rc, err = self.build()
        self.assertEqual(0, rc, err)
        self.assertEqual([], self.tart_acts())

    def test_a_base_that_never_finished_is_rubble_and_is_made_again(self):
        self.w.state = "stopped"
        rc, err = self.build()
        self.assertEqual(0, rc, err)
        self.assertIn("never finished", err)
        self.assertEqual([("stop", "wk-base"), ("delete", "wk-base")], self.tart_acts()[:2])
        self.assertTrue(self.marker())

    def test_the_base_boots_one_way_only_with_the_open_network(self):
        """Booting it once each way changes its subnet, and `tart ip` answers with the lease from before."""
        self.build()
        runs = [e[1] for e in self.w.effects if e[0] == "spawn"]
        self.assertEqual([(TART, "run", "--no-graphics", "wk-base")] * 2, runs)

    def test_provisioning_is_detached_and_polled(self):
        self.build()
        started = [c for c in self.w.guest_cmds if "nohup" in c]
        self.assertEqual(1, len(started))
        self.assertIn("vm/provision-base.sh", started[0])
        self.assertTrue([c for c in self.w.guest_cmds if "wc -c" in c], "nothing polled the detached log")

    def test_a_failed_provisioning_names_its_log_and_the_rerun(self):
        self.w.prov_rc = "3"
        rc, err = self.build()
        self.assertEqual(1, rc)
        self.assertIn("base provisioning failed (rc=3)", err)
        self.assertIn("base-provision.log", err)
        self.assertIn("--refresh", err)
        self.assertEqual("", self.marker())

    def test_killpoints_vm_base(self):
        """`unit killpoints[vm base]`: killed after any effect, a re-run ends in the same sealed base."""
        def world():
            w = BaseWorld(self.tmp)
            return type("W", (), {"fake": w})

        def run_once(w):
            self.w = w.fake
            self.build()

        def final(w):
            return w.fake.state, w.fake.disk, [l for l in self.marker().splitlines() if not l.startswith("finished=")]
        converges(self, world, run_once, final, max_effects=80)

    def test_a_dry_run_clones_nothing_and_boots_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build()
        self.assertEqual(0, rc, err)
        self.assertEqual("absent", self.w.state)
        self.assertEqual({}, {p: t for p, t in self.w.files.items() if "/locks/" not in p})
        self.assertIn("would run: %s clone %s wk-base" % (TART, guestbase.IMAGE), err)
        self.assertEqual([], [e for e in self.w.effects if e[0] == "spawn"])


class TestItIsSealedOnlyOnAClearScreen(BaseTest):
    def test_setup_assistant_is_driven_off_before_the_reboot_that_judges_it(self):
        self.w.sa = ["1", "0"]
        rc, err = self.build()
        self.assertEqual(0, rc, err)
        drove = next(i for i, c in enumerate(self.w.guest_cmds) if c == "/usr/bin/python3 -")
        stops = [i for i, e in enumerate(self.w.effects) if e[0] == "run" and e[1][:2] == (TART, "stop")]
        self.assertTrue(stops)
        self.assertIn("driving Setup Assistant off the screen", err)
        self.assertLess(drove, len(self.w.guest_cmds))

    def test_a_pane_that_comes_back_at_the_next_login_is_not_sealed(self):
        self.w.sa = ["0", "0", "1"]
        rc, err = self.build()
        self.assertEqual(1, rc)
        self.assertIn("came back at 'wk-base''s next login", err)
        self.assertEqual("", self.marker())

    def test_a_login_that_stays_clear_is_watched_for_the_settle(self):
        self.build()
        self.assertEqual(guestbase.LOGIN_SETTLE // 3, self.clock.slept.count(3))

    def test_a_pane_on_screen_is_named_and_the_base_is_not_sealed(self):
        self.w.screen = PANE
        rc, err = self.build()
        self.assertEqual(1, rc)
        named = err.split("nothing wk put there:")[1].split("\n")[0]
        self.assertIn("Setup Assistant:0:800x600", named)
        self.assertNotIn("Terminal", named)
        self.assertEqual("", self.marker())

    def test_a_screen_that_cannot_be_read_is_not_sealed(self):
        self.w.screen = "?"
        rc, err = self.build()
        self.assertEqual(1, rc)
        self.assertIn("could not ask 'wk-base' what is on its screen", err)


class TestItsStalenessIsRecomputed(BaseTest):
    def test_a_base_without_a_record_reads_stale(self):
        self.w.state = "stopped"
        self.assertEqual("provisioned before this record existed", self.base().stale())

    def test_a_sealed_base_matches_until_an_input_moves(self):
        self.build()
        self.assertEqual("", self.base().stale())
        self.assertIn("golden base 'wk-base' matches its provisioning inputs", self.base().findings())
        self.w.env["WK_VM_IMAGE"] = "ghcr.io/x@sha256:0"
        self.assertIn("WK_VM_IMAGE", self.base().stale())
        self.assertIn("--rebuild", self.base().findings())

    def test_an_edited_provisioning_script_makes_every_base_before_it_stale(self):
        root = os.path.join(self.tmp, "tree")
        for rel in guestbase.INPUTS:
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            shutil.copy(REPO / rel, os.path.join(root, rel))
        before = guestbase.inputs_hash(root, self.w.env)
        self.assertEqual(guestbase.inputs_hash(str(REPO), self.w.env), before)
        with open(os.path.join(root, "vm", "desktop.sh"), "a") as f:
            f.write("\n# one more line\n")
        self.assertNotEqual(before, guestbase.inputs_hash(root, self.w.env))

    def test_the_record_holds_no_password(self):
        """A short digest over public files and a trivial password is a password a reader could recover."""
        self.build()
        self.assertNotIn("password", self.marker().lower())

    def test_the_inputs_are_the_base_scripts_and_nothing_a_start_converges(self):
        self.assertNotIn("vm/shell-rc.sh", guestbase.INPUTS)
        for rel in guestbase.INPUTS:
            self.assertTrue((REPO / rel).is_file(), rel)

    def test_no_base_and_an_unfinished_one_each_name_their_remedy(self):
        self.assertIn("no golden base VM 'wk-base'", self.base().findings())
        self.w.state = "stopped"
        self.assertIn("--refresh", self.base().findings())


class TestTheDestructiveModes(BaseTest):
    def test_a_dirty_tree_is_refused_before_the_base_is_touched(self):
        self.build()
        self.w.dirty, self.w.effects = True, []
        rc, err = self.build("--rebuild", answers=[True])
        self.assertEqual(1, rc)
        self.assertIn("Nothing has been deleted", err)
        self.assertEqual([], self.prompts, "the prompt came before the check it would waste")
        self.assertEqual([], self.tart_acts())

    def test_a_declined_rebuild_changes_nothing(self):
        self.build()
        self.w.effects = []
        rc, _ = self.build("--rebuild", answers=[False])
        self.assertEqual(1, rc)
        self.assertEqual([], self.tart_acts())
        self.assertTrue(self.marker())

    def test_vm_base_rm_asks_twice(self):
        """`unit vm.base_rm_asks_twice`: the base, then separately the pulled image, which is re-downloadable."""
        self.build()
        self.w.dirs.add(os.path.join(self.w.env["HOME"], ".tart", "cache"))
        self.w._set_file(os.path.join(self.w.env["HOME"], ".tart", "cache", "OCIs", "x"), "")
        rc, err = self.build("--rm", answers=[True, False])
        self.assertEqual(0, rc, err)
        self.assertEqual(2, len(self.prompts), self.prompts)
        self.assertIn("rebuilding it is hours", self.prompts[0])
        self.assertIn("re-downloadable", self.prompts[1])
        self.assertEqual("absent", self.w.state)
        self.assertEqual("", self.marker())
        self.assertNotIn(("prune", "--space-budget"), self.tart_acts())
        self.assertIn("kept:", err)
        self.build()
        rc, _ = self.build("--rm", answers=[True, True])
        self.assertIn(("prune", "--space-budget"), self.tart_acts())

    def test_declining_the_first_question_erases_nothing(self):
        self.build()
        self.w.effects = []
        rc, _ = self.build("--rm", answers=[False])
        self.assertEqual(1, rc)
        self.assertEqual("stopped", self.w.state)
        self.assertEqual([], self.tart_acts())

    def test_erasing_or_refreshing_nothing_is_refused(self):
        self.assertEqual(1, self.build("--rm")[0])
        self.assertIn("no golden base yet", self.build("--refresh")[1])

    def test_one_mode_at_a_time_and_only_on_a_mac(self):
        self.assertIn("one of them", self.build("--rm", "--rebuild")[1])
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=False):
            self.assertIn("needs a macOS host", self.build()[1])


class TestTheBuilderConforms(BaseTest):
    def test_sysimage_builders_conform_guest(self):
        """`unit sysimage.builders_conform[guest]`: a profile names the builder, the command routes it, the rest of
        argv reaches it, and it runs on the host that holds the guests."""
        self.assertIn("guest", cli.BUILDERS)
        seen = []
        reg = targets.Registry(str(REPO), env=self.w.env, machine=self.w)
        with mock.patch.object(guestbase.Base, "build", lambda b, rest: seen.append((b.name, rest)) or 0):
            self.assertEqual(0, cli.Sysimage(reg, self.clock).build(guest.BASE_PROFILE, ["--refresh"]))
        self.assertEqual([("wk-base", ["--refresh"])], seen)
        self.assertEqual("host", cli.where(["build", guest.BASE_PROFILE, "--rm"], self.w.env))
        header = (REPO / "cmd" / "sysimage").read_text()
        self.assertIn("--rebuild,--rm", header.split("# wk: destructive ", 1)[1].split("\n")[0])

    def out(self, fn, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a)
        return rc, buf.getvalue()

    def test_path_and_holds_reach_the_sealed_base_marker(self):
        """`unit sysimage.builders_conform[guest]`, the read half: unbuilt answers nothing, sealed answers the marker."""
        reg = targets.Registry(str(REPO), env=self.w.env, machine=self.w)
        s = cli.Sysimage(reg, self.clock)
        self.assertEqual(self.out(s.path, guest.BASE_PROFILE, None), (1, ""))
        self.assertEqual(self.out(s.holds, guest.BASE_PROFILE, None, None, None, None, False)[1], "no\n")
        self.build()
        self.assertEqual(self.out(s.path, guest.BASE_PROFILE, None), (0, self.base().marker() + "\n"))
        self.assertEqual(self.out(s.holds, guest.BASE_PROFILE, None, None, None, None, False)[1], "yes\n")

    def test_ls_lists_it_only_once_sealed(self):
        """`unit sysimage.builders_conform[guest]`: `ls` has no workspace to anchor a placeholder row at,
        so the profile is silent until `builder_outputs` finds the sealed marker."""
        reg = targets.Registry(str(REPO), env=self.w.env, machine=self.w)
        s = cli.Sysimage(reg, self.clock)
        self.assertNotIn(guest.BASE_PROFILE, self.out(lambda: s.ls(False))[1])
        self.build()
        _rc, out = self.out(lambda: s.ls(False))
        line = next(l for l in out.splitlines() if l.startswith(guest.BASE_PROFILE))
        self.assertEqual(line.split()[:4], [guest.BASE_PROFILE, "-", "guest", "ready"])
        self.assertIn("    " + self.base().marker(), out)


class TestAGuestIsAdmittedOnlyWhereItFits(BaseTest):
    """Virtualization.framework has one limit for the whole host, and the podman machine spends a slot of it."""

    def admit(self, mine=8192):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                guest.admit(guest.Host(self.base().vm, self.clock), "wk-demo", mine)
                return 0, err.getvalue()
            except Refused as e:
                return e.status, err.getvalue()

    def test_a_running_podman_machine_is_one_of_the_two_and_is_named(self):
        self.w.state = "running"
        self.w.answer(["podman", "machine", "inspect"], out="running\n")
        rc, err = self.admit()
        self.assertEqual(1, rc)
        self.assertIn("2 VM(s) are already running", err)
        self.assertIn("podman machine wk", err)
        self.assertIn("podman machine stop wk", err)

    def test_one_guest_alone_is_let_through(self):
        self.w.state = "running"
        self.assertEqual(0, self.admit()[0])

    def test_an_idle_podman_machine_is_stopped_and_a_busy_one_is_not(self):
        self.w.answer(["podman", "machine", "inspect", "wk", "--format", "{{.State}}"], out="running\n")
        self.w.answer(["podman", "machine", "inspect", "wk", "--format", "{{.Resources.Memory}}"], out="16384\n")
        self.w.answer(["podman", "machine", "ssh"], out="3\n")
        rc, err = self.admit(mine=12000)
        self.assertEqual(1, rc)
        self.assertNotIn(("run", ("podman", "machine", "stop", "wk")), self.w.effects)
        self.assertIn("not enough memory", err)
        self.w.answer(["podman", "machine", "ssh"], rc=255)
        self.assertEqual(1, self.admit(mine=12000)[0], "an unreadable answer is busy")
        self.w.answer(["podman", "machine", "ssh"], out="0\n")
        state = ["podman", "machine", "inspect", "wk", "--format", "{{.State}}"]
        self.w.react(["podman", "machine", "stop"], lambda a, f: (f.answer(state, out="stopped\n"), Result(0))[1])
        rc, err = self.admit(mine=12000)
        self.assertEqual(0, rc, err)
        self.assertIn("stopping the idle podman machine", err)

    def test_a_host_nearly_out_of_disk_is_refused(self):
        self.w.answer(["df", "-Pk", "/"], out="F\n/dev/d 1 1 10485760 1% /\n")
        rc, err = self.admit()
        self.assertEqual(1, rc)
        self.assertIn("only 10 GB free on the host", err)


class TestWhatTheBaseCarries(unittest.TestCase):
    """A base is rebuilt for a new image and for nothing else: what depends on this tree, a credential or the host is
    made at a guest's first start, or mounted in, and converged on every start."""

    def test_provisioning_makes_no_checkout_mirror_or_tool(self):
        text = PROVISION.read_text()
        for word in ("git clone", "mirror_refresh_script", "wk_wiring_script", "wk_gitwebkit_setup_script",
                     "claude.ai/install.sh", "wk_claude_cli_script", "include.path", "shell-rc.sh", ".claude",
                     "WK_VM_MIRROR", "github.com", "wk-seed", "rsync"):
            with self.subTest(word=word):
                self.assertNotIn(word, text)

    def test_the_guest_gets_them_on_every_start(self):
        assert_guest_start_converges(self, '_write_checkout "$name" "$ip"')
        assert_guest_start_converges(self, '_install_claude_cli "$name" "$ip"')

    def test_one_installer_script_for_container_and_guest(self):
        self.assertIn("wk_claude_cli_script", (REPO / "container" / "firstrun.sh").read_text())
        self.assertIn("wk_claude_cli_script", inspect.getsource(guest.Guest.install_claude_cli))
        for rel in ("container/firstrun.sh", "lib/wk/guest.py", "vm/provision-base.sh"):
            self.assertNotIn("claude.ai/install.sh", (REPO / rel).read_text(), rel)

    def test_the_guest_keeps_the_images_password_and_one_name_holds_it(self):
        """`sysadminctl -oldPassword`, the only form the account itself can run, exits 0 having changed nothing."""
        code = "\n".join(l for l in PROVISION.read_text().splitlines() if not l.lstrip().startswith("#"))
        for word in ("-newPassword", "-oldPassword"):
            self.assertNotIn(word, code)
        self.assertIn('WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"', PROVISION.read_text())
        self.assertEqual("admin", guest.PASSWORD)
        gone = "WK_VM_IMAGE" + "_PASSWORD"
        self.assertEqual("", subprocess.run(["git", "grep", "-l", gone], cwd=REPO, capture_output=True, text=True).stdout)

    def test_every_tart_delete_goes_through_the_one_that_reaps_its_runner(self):
        for rel in ("lib/wk/targets.py", "lib/wk/sysimage/guestbase.py", "lib/wk/guest.py"):
            text = (REPO / rel).read_text()
            with self.subTest(file=rel):
                self.assertEqual(1 if rel.endswith("targets.py") else 0, text.count('"delete", v]') + text.count('"delete"]'))

    def test_the_login_is_stated_on_a_starts_one_exit_and_by_each_attach(self):
        body = inspect.getsource(guest.start)
        self.assertEqual(1, body.count("login_note("), body)
        self.assertEqual(1, body.count("return "), body)
        self.assertIn("login_note()", (REPO / "cmd" / "zed").read_text())
        self.assertIn("login_note()", inspect.getsource(targets.Vm.enter_argv))


# `ssh` to the guest as a directory here: /Users/admin and the mirror share are rewritten to a scratch guest, and the
# command runs with HOME there, so the real git runs.
class LocalGuest(Local):
    def __init__(self, home):
        self.home = home

    def _map(self, text):
        return text.replace("/Volumes/My Shared Files/mirror", self.home + "/share").replace("/Users/admin", self.home)

    def run(self, argv, input=None, timeout=None):
        env = dict(os.environ, HOME=self.home)
        cp = subprocess.run([self._map(a) for a in argv], input=self._map(input or ""), capture_output=True, text=True,
                            env=env, timeout=timeout)
        return Result(cp.returncode, cp.stdout, cp.stderr)


FAKE_GIT_WEBKIT = '''#!/bin/sh
echo "$*" >> "$HOME/git-webkit.calls"
case "$1" in
    setup)         [ "$2" = --defaults ] || exit 2; git config webkitscmpy.setup true ;;
    install-hooks) ;;
    *)             exit 2 ;;
esac
'''


class TestTheCheckoutIsMadeAtFirstStart(unittest.TestCase):
    """The first start clones --shared off the mirror on the share, wires it and runs setup; the next finds all three
    done and changes nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-checkout-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.guest = os.path.join(self.tmp, "guest")
        os.makedirs(os.path.join(self.guest, "share"))
        self.mirror = os.path.join(self.guest, "share", "WebKit.git")
        src = os.path.join(self.tmp, "seed")
        os.makedirs(os.path.join(src, "Tools", "Scripts"))
        gw = os.path.join(src, "Tools", "Scripts", "git-webkit")
        with open(gw, "w") as f:
            f.write(FAKE_GIT_WEBKIT)
        os.chmod(gw, 0o755)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git", "commit", "-q", "-m", "seed"]):
            subprocess.run(cmd, cwd=src, env=env, check=True)
        subprocess.run(["git", "clone", "-q", "--bare", src, self.mirror], check=True)
        p = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def start(self):
        env = {"HOME": self.tmp + "/home", "WK_VM_STORE": self.tmp + "/vmstore", "WK_STORE": self.tmp + "/store",
               "XDG_STATE_HOME": self.tmp + "/state", "WK_MACHINES_DIR": self.tmp + "/registry", "PATH": os.environ["PATH"],
               "WK_MIRROR_BRANCHES": "main"}
        vm = targets.Registry(str(REPO), env=env, machine=Local()).load("vm")
        g = guest.Guest(guest.Host(vm, FakeClock()), "demo", IP)
        g.m = LocalGuest(self.guest)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = g.write_checkout()
        return ok, err.getvalue()

    def config(self, key):
        return subprocess.run(["git", "-C", os.path.join(self.guest, "WebKit"), "config", "--get-all", key],
                              capture_output=True, text=True).stdout.split()

    def test_the_first_start_clones_wires_and_sets_up(self):
        ok, err = self.start()
        self.assertTrue(ok, err)
        self.assertIn("checkout made from its mirror in", err)
        self.assertIn("git-webkit is set up in demo", err)
        self.assertEqual(["true"], self.config("webkitscmpy.setup"))
        self.assertEqual(["https://github.com/WebKit/WebKit.git"], self.config("remote.origin.url"))
        with open(os.path.join(self.guest, ".gitconfig")) as f:
            self.assertIn("dotfiles/gitconfig", f.read(), "the identity include precedes the setup that reads it")
        self.assertTrue(os.path.exists(os.path.join(self.guest, "WebKit", ".git", "objects", "info", "alternates")),
                        "the clone copied the history rather than sharing the mirror's")

    def test_the_next_start_finds_it_done(self):
        self.start()
        ok, err = self.start()
        self.assertTrue(ok, err)
        self.assertNotIn("made from its mirror", err)
        with open(os.path.join(self.guest, "git-webkit.calls")) as f:
            calls = f.read().splitlines()
        self.assertEqual(["setup --defaults"], [c for c in calls if c.startswith("setup")])

    def test_no_mirror_is_a_failure_that_names_both_remedies(self):
        shutil.rmtree(self.mirror)
        ok, err = self.start()
        self.assertFalse(ok)
        self.assertIn("checkout=no-mirror", err)
        self.assertIn("wk start", err)
        self.assertIn("wk sync", err)
        self.assertFalse(os.path.exists(os.path.join(self.guest, "WebKit")))


@unittest.skipUnless(platform.system() == "Darwin", "the unblocker imports pyobjc's ApplicationServices, a macOS framework")
class TestSetupAssistantIsDrivenByIdentifier(unittest.TestCase):
    """Elements are chosen by AXIdentifier, never by where they draw: a coordinate lands on "Restart"."""

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
        """`live vm.base_matches_pin`: the base on this Mac was sealed from the pinned image and its current inputs."""
        if not live_selected() or sys.platform != "darwin":
            self.skipTest("live tier not selected, or not a macOS host")
        vm = targets.Registry(str(REPO)).load("vm")
        if not vm.tart():
            self.skipTest("tart is not installed here")
        base = guestbase.Base(vm)
        if not base.exists():
            self.skipTest("no golden base on this Mac")
        self.assertEqual("", base.stale())
        self.assertEqual(guestbase.image(vm.env), base.field("image"))


if __name__ == "__main__":
    unittest.main()
