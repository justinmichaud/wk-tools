"""`wk machine` (lib/wk/machine_cmd/): setup, rm, probe and ls over one fake host that is both ends."""
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
from tests.support import REAL_MACHINES, REPO, live_selected, machine_reachable, owed

sys.path.insert(0, str(REPO / "lib"))
from wk import act, machine_cmd, places, reach, tools  # noqa: E402
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
    """One Fake for both ends: its effects are the flow's effects, wherever they land."""

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
        self.motd = ""
        f.react(["sh", "-c"], self.sh)
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
        if text == places.PROBE_SCRIPT:
            return Result(0, PROBE)
        if text == tools.CONVERGE:
            return Result(0, SHA + "\n")
        if text == build.MOTD:
            return Result(0, self.motd)
        if text == build.OLD_TOOLS:
            return Result(0, "".join(d + "\n" for d in sorted(fake.dirs) if d.endswith("wk-tools")))
        if text.startswith("umask 077 && cat > "):
            fake.files[HOME + "/" + text.rsplit("/", 1)[1].strip("'")] = "copy"
        if text == build.DEPROVISION:
            fake.files.pop(HOME + "/.wk-remote", None)
        return Result(0, "")

    def env_run(self, argv, fake):
        if argv[-1].endswith("/remote/provision.sh"):
            fake.files[HOME + "/.wk-remote"] = "place=box\n"
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

    def verb(self, want, verb, *args, w=None, env=(), **kw):
        """`wk machine <verb>` over `w` (else a world made of `kw`), asserted to end `want`; the world and its stderr."""
        w = w or self.world(**kw)
        with mock.patch.dict(os.environ, dict(env)):
            rc, err = self.quiet(getattr(w.machines(), verb), *args)
        self.assertEqual(rc, want, err)
        return w, err

    def converges(self, world, verb, *args):
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                getattr(w.machines(), verb)(*args)
        converges(self, world, run_once, World.state)


class TestSharedHomeProvisioning(unittest.TestCase):
    def provision(self, home, driver, root):
        env = dict(os.environ, HOME=home, GIT_CONFIG_GLOBAL=home + "/.gitconfig", WK_REMOTE_MACHINE=driver,
                   WK_REMOTE_ROOT=root, WK_REMOTE_INPUTS="abc")
        cp = subprocess.run(["bash", str(REPO / "remote" / "provision.sh")], env=env, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return Path(home, ".wk-remote").read_text()

    def test_the_second_machine_leaves_the_first_as_it_was(self):
        home = tempfile.mkdtemp(prefix="wk-test-shared-home-")
        self.addCleanup(shutil.rmtree, home, True)
        first = self.provision(home, "boxa", home + "/wk-a")
        (Path(home) / "wk-a" / "secrets" / "token").write_text("a's")
        second = self.provision(home, "boxb", home + "/wk-b")
        self.assertEqual(first, second)
        self.assertEqual((Path(home) / "wk-a" / "secrets" / "token").read_text(), "a's")
        for root in ("wk-a", "wk-b"):
            self.assertTrue((Path(home) / root / "secrets").is_dir(), root)


class TestNodeForPi(unittest.TestCase):
    """remote/provision.sh against stand-in uname, curl and sha256sum: the pinned node goes into ~/.local once."""

    VERSION = "v22.23.3"
    DIR = "node-v22.23.3-linux-x64"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-node-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.stubs, self.log = self.tmp / "home", self.tmp / "stubs", self.tmp / "log"
        self.home.mkdir()
        self.stubs.mkdir()
        tree = self.tmp / "tree" / self.DIR / "bin"
        tree.mkdir(parents=True)
        (tree / "node").write_text("#!/bin/sh\necho %s\n" % self.VERSION)
        (tree / "node").chmod(0o755)
        subprocess.run(["tar", "-cJf", str(self.tmp / "node.tar.xz"), "-C", str(self.tmp / "tree"), self.DIR], check=True)
        self.stub("uname", 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; *) echo Linux x86_64 ;; esac')
        self.stub("curl", 'echo "curl $*" >> "%s"\ncp "%s" "$3"' % (self.log, self.tmp / "node.tar.xz"))
        self.sha = "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de"
        self.stub("sha256sum", 'echo "$SHA  $1"')

    def stub(self, name, body):
        (self.stubs / name).write_text("#!/bin/sh\n" + body + "\n")
        (self.stubs / name).chmod(0o755)

    def provision(self, **env):
        e = dict(os.environ, HOME=str(self.home), GIT_CONFIG_GLOBAL=str(self.home / ".gitconfig"), WK_REMOTE_MACHINE="boxa",
                 WK_REMOTE_ROOT=str(self.home / "wk"), WK_REMOTE_INPUTS="abc", SHA=self.sha,
                 PATH="%s:%s" % (self.stubs, os.environ["PATH"]))
        e.update(env)
        cp = subprocess.run(["bash", str(REPO / "remote" / "provision.sh")], env=e, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stderr + cp.stdout

    def downloads(self):
        return self.log.read_text().count("curl") if self.log.exists() else 0

    def test_the_pinned_node_is_installed_and_a_rerun_does_nothing(self):
        self.provision()
        node = self.home / ".local" / "bin" / "node"
        self.assertEqual(self.VERSION, subprocess.run([str(node)], capture_output=True, text=True).stdout.strip())
        self.assertTrue((self.home / ".local" / "bin" / "npm").is_symlink())
        self.provision()
        self.assertEqual(1, self.downloads())

    def test_a_tarball_that_fails_its_checksum_installs_nothing(self):
        out = self.provision(SHA="0" * 64)
        self.assertFalse((self.home / ".local" / "bin" / "node").exists())
        self.assertIn("did not download, verify or install", out)

    def test_another_version_is_replaced(self):
        self.provision()
        (self.home / ".local" / "bin" / "node").unlink()
        old = self.home / ".local" / "lib" / "node-v20.0.0-linux-x64"
        old.mkdir()
        (self.home / ".local" / "bin" / "node").symlink_to(old / "node")
        self.provision()
        self.assertFalse(old.exists())
        self.assertEqual(2, self.downloads())

    @owed("needs a real build box and nodejs.org: `live machine_cmd.node[buildbox4]` runs setup, then `wk new --on` installs pi")
    def test_a_build_box_runs_pi_after_setup(self):
        self.fail("live")


BUILD = "kind=build\ndriver=remote\n"
BOARD = "kind=board\nssh=box\ndriver=pi-sd\n"
DRY = {"WK_DRY_RUN": "1"}
SHARED = "Your home directory is shared across all of these boxes.\n"


class TestSetup(MachineTest):
    def test_a_first_setup_without_a_conf_needs_the_kind_and_after_that_the_conf_is_the_answer(self):
        w, err = self.verb(1, "setup", "box")
        self.assertIn("--kind build", err)
        self.assertEqual(w.fake.effects, [])
        w, _ = self.verb(0, "setup", "box", "build")
        self.assertIn("kind=build\n", w.fake.files[str(w.fleet / "box.conf")])
        w, _ = self.verb(1, "setup", "box", "build", conf="kind=peer\ndriver=remote\npeer=1\n")
        self.assertEqual(w.fake.effects, [])

    def test_a_shared_home_needs_a_root_of_its_own_and_nothing_is_changed_without_one(self):
        for conf, want in (("kind=build\n", 1), ("kind=build\nroot=%s/wk-box\n" % HOME, 0)):
            w = self.world(conf=conf)
            w.motd = SHARED
            _, err = self.verb(want, "setup", "box", w=w)
            if want:
                self.assertIn("root=", err)
                self.assertEqual([r for r in self.runs(w) if r[0] == "env"], [])

    def test_a_missing_required_tool_stops_it_before_any_change(self):
        w, err = self.verb(1, "setup", "box", "build", deps=DEPS_PROBE.replace("tool.git=/usr/bin/git", "tool.git="))
        self.assertIn("sudo apt-get update && sudo apt-get install -y git", err)
        self.assertEqual([e for e in w.fake.effects if e[0] != "run" or e[1][0] not in ("sh", "bash", "git", "python3", "id")], [])

    def test_the_machine_is_probed_once_and_provisioning_is_handed_the_inputs_hash(self):
        w, _ = self.verb(0, "setup", "box", conf=BUILD)
        self.assertEqual(1, len([r for r in self.runs(w) if r[:2] == ("sh", "-c") and r[2] == places.PROBE_SCRIPT]))
        self.assertEqual(1, len([r for r in self.runs(w) if r[:2] == ("bash", "-s")]))
        (prov,) = [r for r in self.runs(w) if r[0] == "env" and r[-1].endswith("remote/provision.sh")]
        self.assertIn("WK_REMOTE_INPUTS=" + deps.inputs_hash(REPO), prov)
        self.assertIn("WK_REMOTE_ROOT=%s/wk" % HOME, prov)

    def test_an_unreachable_machine_is_refused_by_ssh_word(self):
        self.assertIn("connect to host box port 22: Connection refused",
                      self.verb(1, "setup", "box", conf=BUILD, answers=False)[1])

    def test_a_key_no_target_reads_is_refused_before_anything_is_asked(self):
        w, err = self.verb(1, "setup", "box", conf="kind=build\nhots=box\n")
        self.assertIn("hots is not a key a build machine's conf takes", err)
        self.assertEqual(self.runs(w), [])

    def test_an_old_checkout_is_asked_about_once_and_removed_and_a_declined_one_left(self):
        old = HOME + "/Development/wk-tools"
        with mock.patch.object(act, "confirm", return_value=True) as asked:
            w, _ = self.verb(0, "setup", "box", conf=BUILD, old_tools=(old,))
        self.assertEqual(asked.call_count, 1)
        self.assertNotIn(old, w.fake.dirs)
        self.assertIn(("rm", "-rf", old), self.runs(w))
        with mock.patch.object(act, "confirm", return_value=False):
            w, _ = self.verb(0, "setup", "box", conf=BUILD, old_tools=(old,))
        self.assertIn(old, w.fake.dirs)
        self.assertIn(HOME + "/.wk-remote", w.fake.files)

    def test_a_setup_killed_after_any_effect_and_rerun_converges(self):
        self.converges(lambda: self.world(old_tools=(HOME + "/wk-tools",)), "setup", "box", "build")

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        wet, _ = self.verb(0, "setup", "box", "build", old_tools=(HOME + "/wk-tools",))
        acted = [r for r in self.runs(wet) if r[0] in ("env", "rm") or (r[:2] == ("sh", "-c") and r[2].startswith(("umask", "rm -f")))]
        self.assertGreaterEqual(len(acted), 5)
        dry = self.world(old_tools=(HOME + "/wk-tools",))
        before = dry.state()
        _, err = self.verb(0, "setup", "box", "build", w=dry, env=DRY)
        self.assertEqual(dry.state(), before)
        for r in acted:
            self.assertIn("would run on both: " + " ".join(shlex.quote(a) for a in r), err)
        self.assertIn(("write", str(dry.fleet / "box.conf")), dry.fake.effects)

    def test_a_peer_is_asked_for_its_own_wk_and_not_provisioned(self):
        w, _ = self.verb(0, "setup", "box", conf="kind=peer\ndriver=remote\npeer=1\ntools=Development/wk-tools\n")
        self.assertIn(("sh", "-c", "test -x %s/Development/wk-tools/wk" % HOME), self.runs(w))
        self.assertFalse([r for r in self.runs(w) if r[0] == "env"])


class TestRm(MachineTest):
    def deployed(self):
        w = self.world(conf=BUILD)
        w.fake.files[HOME + "/.wk-remote"] = "place=box\n"
        return w

    def test_a_machine_with_workspaces_is_refused_by_name(self):
        w, err = self.verb(1, "rm", "box", conf=BUILD, ws=("big",))
        self.assertIn("big", err)
        self.assertFalse([r for r in self.runs(w) if r[:2] == ("sh", "-c") and r[2] == build.DEPROVISION])

    def test_it_deprovisions_and_keeps_the_conf(self):
        w, err = self.verb(0, "rm", "box", w=self.deployed())
        self.assertNotIn(HOME + "/.wk-remote", w.fake.files)
        self.assertNotIn(HOME + "/wk", w.fake.dirs)
        self.assertTrue((w.fleet / "box.conf").exists())
        self.assertIn("git rm machines/box.conf", err.replace(str(w.fleet), "machines"))

    def test_an_unreachable_machine_or_board_only_loses_its_conf_here(self):
        for conf in (BUILD, BOARD):
            with self.subTest(conf=conf):
                w, err = self.verb(0, "rm", "box", conf=conf, answers=False)
                self.assertIn("Connection refused", err)
                self.assertEqual([e for e in w.fake.effects if e[0] == "remove"], [("remove", str(w.fleet / "box.conf"))])

    def test_an_rm_killed_after_any_effect_and_rerun_converges(self):
        self.converges(self.deployed, "rm", "box")


class TestBoard(MachineTest):
    HELPERS = ("/usr/local/libexec/wk-card-priv", "/usr/local/libexec/wk-check-boot-files.py")

    def test_setup_installs_the_card_helper(self):
        w, _ = self.verb(0, "setup", "box", conf=BOARD)
        self.assertEqual([e[2] for e in w.fake.effects if e[0] == "copy_in"], list(self.HELPERS))
        for helper in self.HELPERS:
            self.assertIn(("run", ("chmod", "+x", helper)), w.fake.effects)

    def test_an_unreachable_board_is_refused_and_nothing_is_changed(self):
        w, err = self.verb(1, "setup", "box", conf=BOARD, answers=False)
        self.assertIn("Connection refused", err)
        self.assertEqual([e for e in w.fake.effects if e[0] in ("copy_in", "write", "remove")], [])

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        dry = self.world(conf=BOARD)
        before = dry.state()
        self.assertIn("wk-card-priv", self.verb(0, "setup", "box", w=dry, env=DRY)[1])
        self.assertEqual(dry.state(), before)

    def test_a_setup_killed_after_any_effect_and_rerun_converges(self):
        self.converges(lambda: self.world(conf=BOARD), "setup", "box")

    def test_an_unreachable_board_or_mac_still_prints_the_plan_under_dry_run(self):
        for conf, plan in ((BOARD, "wk-card-priv"), ("kind=mac\nssh=box\n", "would push")):
            with self.subTest(conf=conf):
                self.assertIn(plan, self.verb(0, "setup", "box", conf=conf, answers=False, env=DRY)[1])

    def test_rm_removes_the_card_helper_and_the_conf(self):
        w, _ = self.verb(0, "rm", "box", conf=BOARD)
        for helper in self.HELPERS:
            self.assertIn(("run", ("rm", "-f", helper)), w.fake.effects)
        self.assertEqual([e for e in w.fake.effects if e[0] == "remove"], [("remove", str(w.fleet / "box.conf"))])


class TestBridgeDispatch(MachineTest):
    def bridge_world(self):
        w = self.world()
        (w.fleet / "phone.conf").write_text("kind=bridge\ndevice=pinephone\nsegment=10.9.0.0/24\nrouter=10.9.0.1\n")
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
        self.assertIn("not a bridge", self.verb(1, "tailnet", "box", conf=BUILD)[1])

    def test_the_bridge_flags_are_refused_for_a_build_machine(self):
        w = self.world(conf=BUILD)
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
        self.assertIn("not a bridge", self.verb(1, "status", "box", conf=BUILD)[1])


class TestProbeAndLs(MachineTest):
    def test_a_machine_that_does_not_answer_is_named_with_why(self):
        w = self.world(conf=BUILD, answers=False)
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
        w = self.world(conf=BUILD)
        (w.fleet / "other.conf").write_text("kind=peer\ndriver=remote\n")
        w.fake.answer(["tailscale", "status", "--json"], out=json.dumps({"Peer": {"k": {"DNSName": "box.ts.net.", "TailscaleIPs": ["100.64.0.9"], "Online": True}}}))
        out = io.StringIO()
        self.quiet(w.machines().ls, False, out)
        self.assertIn("box 100.64.0.9 (up)", out.getvalue())
        self.assertIn("other not a node", out.getvalue())
        self.assertEqual(len([e for e in w.fake.effects if e[1][0] == "tailscale"]), 1)


def _a_build_box():
    if not live_selected():
        return None
    from wk import fleet
    env = dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES))
    return next((n for n in fleet.Fleet(REPO, env).names(("build",)) if machine_reachable(n)), None)


class TestSetupOnARealBox(unittest.TestCase):
    wk_tier = "live"

    def test_setup_leaves_one_shape(self):
        box = _a_build_box()
        if box is None:
            self.skipTest("live tier not selected, or no build machine in machines/ answers over ssh")
        cp = subprocess.run([str(REPO / "wk"), "machine", "setup", box], cwd=str(REPO), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=1800)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("%s is ready" % box, cp.stderr)
        self.assertTrue("zsh:" in cp.stderr or "no zsh on this machine" in cp.stderr, cp.stderr)
        t = places.Registry(REPO, dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES))).load(box)
        self.assertIsNone(deps.stale(t, REPO), cp.stderr)


if __name__ == "__main__":
    unittest.main()
