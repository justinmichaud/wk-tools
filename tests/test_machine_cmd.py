"""`wk machine` (cmd/machine, lib/wk/machine_cmd/): setup and rm of a build machine or a peer over one fake
host that is both this machine and the far one, `killpoints[machine setup]`, `killpoints[machine rm]`, the
dry run as the wet run's plan, the --kind decision, `machine.probed_once_per_invocation`,
`machine.unreachable_is_named`, the sweep's one document, and `live machine_cmd.setup[<box>]`.

Run: python3 tests/run.py --unit -k test_machine_cmd
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REAL_MACHINES, REPO, live_selected, machine_reachable, run

sys.path.insert(0, str(REPO / "lib"))
from wk import act, machine_cmd, reach, targets, tools  # noqa: E402
from wk.machine_cmd import build, deps  # noqa: E402
from wk import bridge  # noqa: E402
from wk.bridge import role  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

HOME = "/home/me"
SHA = "a" * 40
PROBE = """%s
Linux
8
0.52 0.58 0.61 2/1234 56789
===MEM===
MemAvailable:   20480000 kB
===IONICE===
yes
""" % HOME
DEPS_PROBE = """host=box
os=Debian GNU/Linux 13
family=debian
arch=x86_64
cores=8
tool.git=/usr/bin/git
tool.cmake=/usr/bin/cmake
tool.ninja=/usr/bin/ninja
tool.clang=/usr/bin/clang
tool.python3=/usr/bin/python3
tool.ccache=/usr/bin/ccache
tool.zsh=/usr/bin/zsh
git.fsmonitor=true
git.manyfiles=true
marker=no
"""
SECRETS = ("claude        claude-token        .wk-agent-token             CLAUDE_CODE_OAUTH_TOKEN  value  remote\n"
           "litellm       litellm-key         .wk-litellm-key             LITELLM_API_KEY          value  container,vm,remote\n"
           "claude-login  .credentials.json   .claude/.credentials.json   -                        file   container,vm\n")


class World:
    """One Fake for both ends: its effects are the flow's effects, wherever they land. The far side's
    marker, credential copies and old checkouts are files the fake's reactions move."""

    def __init__(self, tmp, conf=None, deps=DEPS_PROBE, old_tools=(), answers=True, ws=()):
        self.fake = Fake("both")
        self.tmp = Path(tmp)
        self.fleet = self.tmp / "machines"
        shutil.rmtree(self.fleet, ignore_errors=True)
        self.fleet.mkdir(parents=True)
        if conf is not None:
            (self.fleet / "box.conf").write_text(conf)
        self.env = {"HOME": HOME, "WK_MACHINES_DIR": str(self.fleet), "XDG_STATE_HOME": str(self.tmp / "state"),
                    "XDG_CONFIG_HOME": str(self.tmp / "config"), "WK_STORE": str(self.tmp / "store"),
                    "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets"), "PATH": os.environ.get("PATH", "")}
        f = self.fake
        f.dirs |= {HOME + "/wk", HOME + "/wk/ws"}
        for d in old_tools:
            f.dirs.add(d)
        for w in ws:
            f.dirs.add(HOME + "/wk/ws/" + w)
        self.answers = answers
        f.react(["sh", "-c"], self.sh)
        # A board or a Mac is asked for over a literal `ssh`, not the `sh -c` probe a Remote target's
        # Fake substitution shortcuts -- Machines.answers() builds a real Ssh wrapping this same Fake.
        if answers:
            f.answer(["ssh"], rc=0)
        else:
            f.answer(["ssh"], rc=255, err="ssh: connect to host box port 22: Connection refused\n")
        f.answer(["chmod"], rc=0)
        f.answer(["bash", "-s"], out=deps)
        f.answer(["bash", "-c"], out=SECRETS)
        f.answer(["python3"], out="")
        f.answer(["git", "config"], rc=1)
        f.answer(["git", "-C", str(REPO), "rev-parse", "--git-dir"], out=".git\n")
        f.answer(["git", "-C", str(REPO), "status"], out="")
        f.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out=SHA + "\n")
        f.answer(["git", "-C", str(REPO), "bundle"])
        f.react(["env"], self.env_run)
        f.answer(["id", "-un"], out="me\n")
        f.answer(["sudo"], rc=1)
        f.files["/etc/sudoers.d/zz-me-passwd"] = ""
        f.react(["rm", "-rf"], lambda a, fk: (fk._drop(a[2]), Result(0))[1])

    def sh(self, argv, fake):
        text = argv[2]
        if not self.answers:
            return Result(255, "", "ssh: connect to host box port 22: Connection refused\n")
        if text == targets.PROBE_SCRIPT:
            return Result(0, PROBE)
        if text == tools.CONVERGE:
            return Result(0, SHA + "\n")
        if text == build.OLD_TOOLS:
            return Result(0, "".join(d + "\n" for d in sorted(fake.dirs) if d.endswith("wk-tools")))
        if text.startswith("umask 077 && cat > "):
            fake.files[HOME + "/" + text.rsplit("/", 1)[1].strip("'")] = "copy"
        if text == build.DEPROVISION:
            fake.files.pop(HOME + "/.wk-remote", None)
        return Result(0, "")

    def env_run(self, argv, fake):
        if argv[-1].endswith("/remote/provision.sh"):
            fake.files[HOME + "/.wk-remote"] = "target=box\n"
        return Result(0)

    def machines(self):
        return machine_cmd.Machines(REPO, env=self.env, here=self.fake, far=self.fake)

    def state(self):
        return sorted((k, str(v)) for k, v in self.fake.files.items()), sorted(self.fake.dirs)


class MachineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-machine-")
        self.env_patch = mock.patch.dict(os.environ, {"WK_YES": "1"})
        self.env_patch.start()
        for v in ("WK_DRY_RUN", "WK_CONFIRMED", "WK_DESTRUCTIVE", "WK_FORCE"):
            os.environ.pop(v, None)
        self.stdin = mock.patch.object(sys, "stdin", io.StringIO())
        self.stdin.start()

    def tearDown(self):
        self.stdin.stop()
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def world(self, **kw):
        return World(self.tmp, **kw)

    def quiet(self, fn, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                rc = fn(*args)
            except act.Refused as e:
                rc = e.status
        return rc, err.getvalue()

    def runs(self, w):
        return [e[1] for e in w.fake.effects if e[0] == "run"]


class TestSetup(MachineTest):
    def test_a_first_setup_without_a_conf_needs_the_kind_and_changes_nothing(self):
        w = self.world()
        rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 1)
        self.assertIn("--kind build", err)
        self.assertEqual(w.fake.effects, [])

    def test_a_first_setup_writes_the_conf_of_the_kind_it_was_given(self):
        w = self.world()
        rc, err = self.quiet(w.machines().setup, "box", "build")
        self.assertEqual(rc, 0, err)
        conf = w.fake.files[str(w.fleet / "box.conf")]
        self.assertIn("KIND=build\n", conf)

    def test_after_that_the_conf_is_the_answer(self):
        w = self.world(conf="KIND=peer\nWK_TARGET_KIND=remote\nWK_REMOTE_PEER=1\n")
        rc, err = self.quiet(w.machines().setup, "box", "build")
        self.assertEqual(rc, 1)
        self.assertEqual(w.fake.effects, [])

    def test_a_missing_required_tool_stops_it_before_any_change(self):
        w = self.world(deps=DEPS_PROBE.replace("tool.git=/usr/bin/git", "tool.git="))
        rc, err = self.quiet(w.machines().setup, "box", "build")
        self.assertEqual(rc, 1)
        self.assertIn("sudo apt-get update && sudo apt-get install -y git", err)
        self.assertEqual([e for e in w.fake.effects if e[0] != "run" or e[1][0] not in ("sh", "bash", "git", "python3", "id")], [])

    def test_the_machine_is_probed_once_for_the_whole_setup(self):
        """`machine.probed_once_per_invocation`: every question setup asks of the driver is the one probe's answer."""
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n")
        self.quiet(w.machines().setup, "box")
        probes = [r for r in self.runs(w) if r[:2] == ("sh", "-c") and r[2] == targets.PROBE_SCRIPT]
        self.assertEqual(len(probes), 1)
        self.assertEqual(len([r for r in self.runs(w) if r[:2] == ("bash", "-s")]), 1)

    def test_an_unreachable_machine_is_refused_by_ssh_word(self):
        """`machine.unreachable_is_named`: the refusal carries what ssh said, not a guess that it is off."""
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n", answers=False)
        rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 1)
        self.assertIn("connect to host box port 22: Connection refused", err)

    def test_provisioning_is_handed_the_inputs_hash(self):
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n")
        self.quiet(w.machines().setup, "box")
        prov = [r for r in self.runs(w) if r[0] == "env" and r[-1].endswith("remote/provision.sh")]
        self.assertEqual(len(prov), 1)
        self.assertIn("WK_REMOTE_INPUTS=" + deps.inputs_hash(REPO), prov[0])
        self.assertIn("WK_REMOTE_ROOT=%s/wk" % HOME, prov[0])

    def test_an_old_checkout_is_asked_about_once_and_removed(self):
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n", old_tools=(HOME + "/Development/wk-tools",))
        with mock.patch.object(act, "confirm", return_value=True) as asked:
            rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 0, err)
        self.assertEqual(asked.call_count, 1)
        self.assertNotIn(HOME + "/Development/wk-tools", w.fake.dirs)
        self.assertIn(("rm", "-rf", HOME + "/Development/wk-tools"), self.runs(w))

    def test_a_declined_cleanup_leaves_it_and_provisions_anyway(self):
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n", old_tools=(HOME + "/wk-tools",))
        with mock.patch.object(act, "confirm", return_value=False):
            rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 0, err)
        self.assertIn(HOME + "/wk-tools", w.fake.dirs)
        self.assertIn(HOME + "/.wk-remote", w.fake.files)

    def test_a_setup_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[machine setup]`: the conf, the push, the provisioning, the credentials and the cleanup."""
        worlds = []

        def world():
            worlds.append(self.world(old_tools=(HOME + "/wk-tools",)))
            return worlds[-1]

        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.machines().setup("box", "build")
        converges(self, world, run_once, World.state)

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        wet = self.world(old_tools=(HOME + "/wk-tools",))
        self.quiet(wet.machines().setup, "box", "build")
        acted = [r for r in self.runs(wet) if r[0] in ("env", "rm") or (r[:2] == ("sh", "-c") and r[2].startswith(("umask", "rm -f")))]
        self.assertGreaterEqual(len(acted), 5)
        dry = self.world(old_tools=(HOME + "/wk-tools",))
        before = dry.state()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            rc, err = self.quiet(dry.machines().setup, "box", "build")
        self.assertEqual(rc, 0, err)
        self.assertEqual(dry.state(), before)
        for r in acted:
            self.assertIn("would run on both: " + " ".join(shlex.quote(a) for a in r), err)
        self.assertIn(("write", str(dry.fleet / "box.conf")), dry.fake.effects)

    def test_a_peer_is_asked_for_its_own_wk_and_not_provisioned(self):
        w = self.world(conf="KIND=peer\nWK_TARGET_KIND=remote\nWK_REMOTE_PEER=1\nWK_REMOTE_TOOLS=Development/wk-tools\n")
        rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 0, err)
        self.assertIn(("sh", "-c", "test -x %s/Development/wk-tools/wk" % HOME), self.runs(w))
        self.assertFalse([r for r in self.runs(w) if r[0] == "env"])


class TestRm(MachineTest):
    CONF = "KIND=build\nWK_TARGET_KIND=remote\n"

    def test_a_machine_with_workspaces_is_refused_by_name(self):
        w = self.world(conf=self.CONF, ws=("big",))
        rc, err = self.quiet(w.machines().rm, "box")
        self.assertEqual(rc, 1)
        self.assertIn("big", err)
        self.assertFalse([r for r in self.runs(w) if r[:2] == ("sh", "-c") and r[2] == build.DEPROVISION])

    def test_it_deprovisions_and_keeps_the_conf(self):
        w = self.world(conf=self.CONF)
        w.fake.files[HOME + "/.wk-remote"] = "target=box\n"
        rc, err = self.quiet(w.machines().rm, "box")
        self.assertEqual(rc, 0, err)
        self.assertNotIn(HOME + "/.wk-remote", w.fake.files)
        self.assertNotIn(HOME + "/wk", w.fake.dirs)
        self.assertTrue((w.fleet / "box.conf").exists())
        self.assertIn("git rm machines/box.conf", err.replace(str(w.fleet), "machines"))

    def test_an_unreachable_machine_only_loses_its_conf_here(self):
        w = self.world(conf=self.CONF, answers=False)
        rc, err = self.quiet(w.machines().rm, "box")
        self.assertEqual(rc, 0, err)
        self.assertIn("Connection refused", err)
        self.assertEqual([e for e in w.fake.effects if e[0] == "remove"], [("remove", str(w.fleet / "box.conf"))])

    def test_an_rm_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[machine rm]`."""
        def world():
            w = self.world(conf=self.CONF)
            w.fake.files[HOME + "/.wk-remote"] = "target=box\n"
            return w

        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.machines().rm("box")
        converges(self, world, run_once, World.state)


class TestBoardSetup(MachineTest):
    CONF = "KIND=board\nNODE_SSH=box\nNODE_DRIVER=pi-sd\n"

    def test_it_installs_the_card_helper(self):
        w = self.world(conf=self.CONF)
        rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 0, err)
        copies = [e[2] for e in w.fake.effects if e[0] == "copy_in"]
        self.assertEqual(copies, ["/usr/local/libexec/wk-card-priv", "/usr/local/libexec/wk-check-boot-files.py"])
        self.assertIn(("run", ("chmod", "+x", "/usr/local/libexec/wk-card-priv")), w.fake.effects)
        self.assertIn(("run", ("chmod", "+x", "/usr/local/libexec/wk-check-boot-files.py")), w.fake.effects)

    def test_an_unreachable_board_is_refused_and_nothing_is_changed(self):
        """`machine.unreachable_is_named`, for a board: the same ssh word a build machine's refusal carries."""
        w = self.world(conf=self.CONF, answers=False)
        rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 1)
        self.assertIn("Connection refused", err)
        self.assertEqual([e for e in w.fake.effects if e[0] in ("copy_in", "write", "remove")], [])

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        dry = self.world(conf=self.CONF)
        before = dry.state()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            rc, err = self.quiet(dry.machines().setup, "box")
        self.assertEqual(rc, 0, err)
        self.assertEqual(dry.state(), before)
        self.assertIn("wk-card-priv", err)

    def test_a_setup_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[machine setup]` for a board: the two card-helper copies and their chmods."""
        def world():
            return self.world(conf=self.CONF)

        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.machines().setup("box")
        converges(self, world, run_once, World.state)

    def test_an_unreachable_board_still_prints_the_plan_under_dry_run(self):
        """A dry run against a board the unit tier's ssh shim cannot reach still names what it would do,
        rather than refusing -- the shim, not a real network answer, is what 'unreachable' means here."""
        w = self.world(conf=self.CONF, answers=False)
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 0, err)
        self.assertIn("wk-card-priv", err)


class TestBoardRm(MachineTest):
    CONF = "KIND=board\nNODE_SSH=box\nNODE_DRIVER=pi-sd\n"

    def test_it_removes_the_card_helper_and_the_conf(self):
        w = self.world(conf=self.CONF)
        rc, err = self.quiet(w.machines().rm, "box")
        self.assertEqual(rc, 0, err)
        self.assertIn(("run", ("rm", "-f", "/usr/local/libexec/wk-card-priv")), w.fake.effects)
        self.assertIn(("run", ("rm", "-f", "/usr/local/libexec/wk-check-boot-files.py")), w.fake.effects)
        self.assertEqual([e for e in w.fake.effects if e[0] == "remove"], [("remove", str(w.fleet / "box.conf"))])

    def test_an_unreachable_board_only_loses_its_conf_here(self):
        w = self.world(conf=self.CONF, answers=False)
        rc, err = self.quiet(w.machines().rm, "box")
        self.assertEqual(rc, 0, err)
        self.assertIn("Connection refused", err)
        self.assertEqual([e for e in w.fake.effects if e[0] == "remove"], [("remove", str(w.fleet / "box.conf"))])


class TestMacSetup(MachineTest):
    CONF = "KIND=mac\nNODE_SSH=box\n"

    def test_it_pushes_the_tree_and_stops_at_the_sudo_with_no_terminal(self):
        w = self.world(conf=self.CONF)
        w.fake.answer(["sh", "-c", 'printf "%s" "$HOME"'], out=HOME)
        rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 1, err)
        self.assertIn(HOME + "/Development/wk-tools", err)
        self.assertIn("no terminal", err)
        self.assertIn("sudoers.d", err)

    def test_an_unreachable_mac_still_prints_the_plan_under_dry_run(self):
        w = self.world(conf=self.CONF, answers=False)
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            rc, err = self.quiet(w.machines().setup, "box")
        self.assertEqual(rc, 0, err)
        self.assertIn("would push", err)


class TestBridgeDispatch(MachineTest):
    """A bridge's setup, tailnet and rm are wk.bridge.role's (tests/test_bridge.py tests the role itself)."""

    def bridge_world(self):
        w = self.world()
        (w.fleet / "phone.conf").write_text("KIND=bridge\nBR_DEVICE=pinephone\nBR_SEGMENT=10.9.0.0/24\nBR_ROUTER=10.9.0.1\n")
        return w

    def test_a_bridge_setup_is_the_bridge_roles_with_its_flags(self):
        w = self.bridge_world()
        with mock.patch.object(role.Role, "setup", return_value=0) as setup:
            self.assertEqual(self.quiet(lambda: w.machines().setup("phone", at="10.0.0.9", no_tailnet=True))[0], 0)
        setup.assert_called_once_with("phone", at="10.0.0.9", no_tailnet=True, disk=None, image=None, rebuild=False)

    def test_a_bridge_rm_and_tailnet_are_the_bridge_roles(self):
        w = self.bridge_world()
        with mock.patch.object(role.Role, "rm", return_value=0) as rm, mock.patch.object(role.Role, "tailnet", return_value=0) as tn:
            self.quiet(lambda: w.machines().rm("phone", at="10.0.0.9"))
            self.quiet(lambda: w.machines().tailnet("phone"))
        rm.assert_called_once_with("phone", at="10.0.0.9")
        tn.assert_called_once_with("phone", at=None)

    def test_tailnet_is_refused_for_anything_but_a_bridge(self):
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n")
        rc, err = self.quiet(w.machines().tailnet, "box")
        self.assertEqual(rc, 1)
        self.assertIn("not a bridge", err)

    def test_the_bridge_flags_are_refused_for_a_build_machine(self):
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n")
        rc, err = self.quiet(lambda: w.machines().setup("box", at="10.0.0.9"))
        self.assertEqual(rc, 1)
        self.assertIn("are a bridge's", err)
        self.assertEqual(self.quiet(lambda: w.machines().setup("box", disk="rpi5:/dev/sda"))[0], 1)
        self.assertEqual(self.quiet(lambda: w.machines().rm("box", at="10.0.0.9"))[0], 1)
        self.assertFalse(self.runs(w))

    def test_status_is_the_bridge_health_check_and_refused_for_anything_else(self):
        w = self.bridge_world()
        with mock.patch.object(bridge.Bridge, "status", return_value=0) as status, \
                mock.patch.object(bridge.Bridge, "ls", return_value=0) as ls:
            self.quiet(lambda: w.machines().status("phone", at="10.0.0.9"))
            self.quiet(lambda: w.machines().status())
        status.assert_called_once_with("phone", at="10.0.0.9")
        ls.assert_called_once_with()
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n")
        rc, err = self.quiet(w.machines().status, "box")
        self.assertEqual(rc, 1)
        self.assertIn("not a bridge", err)


class TestProbeAndLs(MachineTest):
    def test_a_machine_that_does_not_answer_is_named_with_why(self):
        """`machine.unreachable_is_named`: probe says unreachable and ssh's own word, and exits 1."""
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n", answers=False)
        w.fake.answer(["tailscale"], rc=1)
        w.fake.answer(["ssh", "-G"], rc=1)
        out = io.StringIO()
        rc, err = self.quiet(w.machines().probe_one, "box", reach.Survey(w.machines().reach), False, out)
        self.assertEqual(rc, 1)
        self.assertIn("no -- unreachable: connect to host box port 22: Connection refused", out.getvalue())

    def test_the_sweep_is_one_json_document(self):
        w = self.world()
        m = w.machines()
        m.reach._peers = []
        w.fake.answer(["sh", "-c", reach.SWEEP], out="203.0.113.1 dev en0 lladdr B8:27:EB:00:00:01 REACHABLE\n")
        out = io.StringIO()
        rc, _ = self.quiet(m.sweep, reach.Survey(m.reach, "203.0.113.0/31", ssh=False), True, out)
        self.assertEqual(rc, 0)
        doc = json.loads(out.getvalue())
        for key in ("want", "want_mac", "seen", "swept", "blind", "vantages", "hits"):
            self.assertIn(key, doc)
        self.assertEqual((doc["seen"], doc["swept"]), (1, 1))
        self.assertEqual(doc["hits"][0]["vendor"], "Raspberry Pi (pre-4)")
        self.assertEqual(sorted(doc["hits"][0]), sorted(reach.HIT_KEYS))
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)

    def test_ls_reads_the_tailnet_once(self):
        w = self.world(conf="KIND=build\nWK_TARGET_KIND=remote\n")
        (w.fleet / "other.conf").write_text("KIND=peer\nWK_TARGET_KIND=remote\n")
        w.fake.answer(["tailscale", "status", "--json"], out=json.dumps({"Peer": {"k": {"DNSName": "box.ts.net.", "TailscaleIPs": ["100.64.0.9"], "Online": True}}}))
        out = io.StringIO()
        self.quiet(w.machines().ls, False, out)
        self.assertIn("box 100.64.0.9 (up)", out.getvalue())
        self.assertIn("other not a node", out.getvalue())
        self.assertEqual(len([e for e in w.fake.effects if e[1][0] == "tailscale"]), 1)


class TestTheOldSpellings(unittest.TestCase):
    def test_remote_and_find_are_tombstones_naming_the_new_command(self):
        for old, new in (("remote", "wk machine setup|rm"), ("find", "wk machine probe")):
            cp = run(old)
            with self.subTest(old=old):
                self.assertEqual(cp.returncode, 1)
                self.assertIn(new, cp.stdout)
                self.assertFalse((REPO / "cmd" / old).exists())


def _a_build_box():
    if not live_selected():
        return None
    from wk import fleet
    env = dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES))
    return next((n for n in fleet.Fleet(REPO, env).names(("build",)) if machine_reachable(n)), None)


class TestSetupOnARealBox(unittest.TestCase):
    wk_tier = "live"

    def test_setup_leaves_one_shape(self):
        """`live machine_cmd.setup[<box>]`: provisioned from this tree, with zsh or a named warning; no prompt is answered."""
        box = _a_build_box()
        if box is None:
            self.skipTest("live tier not selected, or no build machine in machines/ answers over ssh")
        cp = subprocess.run([str(REPO / "wk"), "machine", "setup", box], cwd=str(REPO), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=1800)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("%s is ready" % box, cp.stderr)
        self.assertTrue("zsh:" in cp.stderr or "no zsh on this machine" in cp.stderr, cp.stderr)
        t = targets.Registry(REPO, dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES))).load(box)
        self.assertIsNone(deps.stale(t, REPO), cp.stderr)


if __name__ == "__main__":
    unittest.main()
