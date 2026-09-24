"""`wk push` -- the deploy-key switch, as a flow over the fake machine tests/test_wk_secrets.py builds: what
`on`, `off` and `status` read and change, what `on` does about a claude session already running, what
`--target` and `--all` ask, and that it never reports a move it did not make.

Run: python3 tests/run.py --unit -k test_push_switch
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import types
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WkTest, bash
from tests.test_wk_secrets import SOCK, SecretsTest, World
from wk import act, guest, shell, targets
from wk.act import Refused
from wk.clock import FakeClock
from wk.machine import Result


def load_push():
    path = str(REPO / "cmd" / "push")
    loader = importlib.machinery.SourceFileLoader("wk_cmd_push", path)
    spec = importlib.util.spec_from_file_location("wk_cmd_push", path, loader=loader)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PUSH = load_push()


def decl(name):
    """The dispatcher's own declaration line for one command."""
    cp = subprocess.run([str(REPO / "wk"), "--declarations"], cwd=str(REPO), stdout=subprocess.PIPE, text=True, timeout=60)
    for line in cp.stdout.splitlines():
        f = line.split("\t")
        if f and f[0] == name:
            return f
    raise AssertionError(f"{name} is not declared")


class Box(targets.Target):
    """The default target: workspaces with the claude pids each holds, and whether it names an agent socket."""

    def __init__(self, machine, env, name="container", sock="/run/wk/ssh-agent.sock"):
        super().__init__(name, str(REPO), env, machine)
        self.sock = sock
        self.claude = {}
        self.stubborn = set()
        self.side, self.far = "answering", (0, "fork       push allowed (in the agent)\n")
        self.asked = []

    def agent_sock(self):
        return self.sock

    def list(self):
        return [(ws, "Up") for ws in self.claude]

    def exec(self, ws, argv, tty=False, timeout=None):
        return Result(0, "".join(p + "\r\n" for p in self.claude.get(ws, [])))

    def act_exec(self, ws, argv):
        self.machine.effects.append(("act", ("exec", ws) + tuple(argv)))
        if not act.dry_run() and ws not in self.stubborn:
            self.claude[ws] = []
        return Result(0)

    def probe(self):
        return self.side, "timed out" if self.side == "unreachable" else ""

    def wk(self, *args, env=None, quiet=False):
        self.asked.append(args)
        return self.far


class Fleet(targets.Registry):
    def __init__(self, w, boxes):
        super().__init__(str(REPO), env=w.env, machine=w)
        self.boxes = boxes

    def default(self):
        return "container"

    def load(self, name):
        if name not in self.boxes:
            raise LookupError("unknown target '%s'" % name)
        return self.boxes[name]

    def machines(self):
        return [n for n in self.boxes if n != "container"]


class PushTest(SecretsTest):
    def setUp(self):
        super().setUp()
        self.box = Box(self.w, self.w.env)
        self.boxes = {"container": self.box}
        self.clock = FakeClock()
        self.guest = mock.patch.multiple(guest, vm_push_keys_converge=mock.DEFAULT, vm_push_agent_keys=mock.DEFAULT,
                                         vm_push_keys_state=mock.DEFAULT)
        self.vm = self.guest.start()
        self.addCleanup(self.guest.stop)
        self.vm["vm_push_keys_converge"].side_effect = lambda root, m, action: m.effects.append(("act", ("guests", action))) or True
        self.vm["vm_push_agent_keys"].return_value = None
        self.vm["vm_push_keys_state"].return_value = []

    def push(self, action, w=None, macos=False, boxes=None):
        """(exit status, stdout, stderr) of one `wk push <action>`."""
        w = w or self.w
        out = io.StringIO()
        reg = Fleet(w, boxes or self.boxes)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                p = PUSH.Push(reg, w.sec(macos=macos), self.clock, out)
                rc = p.run(action)
            except Refused as e:
                rc = e.status
        return rc, out.getvalue(), err.getvalue()

    def main(self, *argv, boxes=None):
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                rc = PUSH.main(list(argv), env=self.w.env, reg=Fleet(self.w, boxes or self.boxes), out=out)
            except Refused as e:
                rc = e.status
        return rc, out.getvalue(), err.getvalue()


class TestWhere(WkTest):
    def test_push_runs_on_the_machine_that_holds_the_keys(self):
        """where=local: the keys are this device's own, and on macOS the podman machine reads them as a mount"""
        self.assertEqual(decl("push")[1], "local")

    def test_nothing_forwards_it_into_the_podman_machine(self):
        src = (REPO / "cmd" / "push").read_text()
        for gone in ("--store", "store_half", "podman machine ssh"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

    def _as_build_machine(self):
        marker = self.tmp / "wk-remote"
        marker.write_text("target=buildbox4\n")
        store = self.tmp / "store"
        store.mkdir()
        return {"WK_REMOTE_MARKER": str(marker), "WK_STORE": str(store)}

    def test_a_store_command_runs_on_a_build_machine(self):
        cp = self.run_wk("push", "status", env=self._as_build_machine())
        self.assertNotIn("acts on a workstation", cp.stdout)

    def test_a_host_command_is_still_refused_on_a_build_machine(self):
        cp = self.run_wk("quiesce", "status", env=self._as_build_machine())
        self.assertIn("acts on a workstation", cp.stdout)

    def test_a_store_command_is_refused_inside_a_workspace(self):
        marker = self.tmp / "wk-workspace"
        marker.write_text("name=probe\ntarget=container\n")
        cp = self.run_wk("push", "status", env={"WK_MARKER": str(marker)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("this is workspace 'probe'", cp.stdout)

    def test_on_and_off_have_a_dry_run_and_status_is_read_only(self):
        src = (REPO / "cmd" / "push").read_text()
        self.assertIn("dryrun on,off", src)
        self.assertIn("# wk: readonly status", src)
        self.assertNotIn("flag --all,--target", src)


class TestOn(PushTest):
    def test_it_loads_every_key_and_hands_the_injector_both_credentials(self):
        self.w.seed()
        rc, out, err = self.push("on")
        self.assertEqual(0, rc, err)
        self.assertEqual({"KEY:fork", "KEY:forkwpe"}, self.w.agents[SOCK])
        self.assertEqual("ghp-held\n", self.w.files[self.tmp + "/store/push-github-pat"])
        self.assertEqual("bz-held\n", self.w.files[self.tmp + "/store/push-bugzilla-api-key"])
        self.assertIn("push is ON -- 2 deploy key(s)", err)
        self.assertIn("(justinmichaud)", err)
        self.assertIn("no login in the mirror", err)
        self.assertIn("wk key check", err)

    def test_the_standing_read_token_is_not_the_switchs(self):
        self.w.seed()
        self.push("on")
        self.assertNotIn(self.tmp + "/store/read-github-pat", self.w.files)

    def test_no_agent_is_a_failure_that_claims_nothing(self):
        self.w.seed()
        del self.w.agents[SOCK]
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertIn("no ssh-agent answers", err)
        self.assertIn("wk-ssh-agent.service", err)
        self.assertNotIn("push is ON", err)
        self.assertEqual([], [e for e in self.w.acts() if e[0] == "write"])

    def test_no_deploy_key_at_all_is_4(self):
        rc, _, err = self.push("on")
        self.assertEqual(4, rc)
        self.assertIn("'wk key deploy' makes them", err)

    def test_a_key_that_will_not_load_is_not_on(self):
        self.w.seed()
        self.w.files[self.w.held + "/build_key_forkwpe"] = "garbage\n"
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertIn("1 deploy key(s) would not load", err)
        self.assertNotIn("push is ON", err)

    def test_a_missing_credential_is_cleared_there_and_named(self):
        self.w.seed(pat=None, bz=None)
        self.w.files[self.tmp + "/store/push-github-pat"] = "left from before\n"
        rc, _, err = self.push("on")
        self.assertEqual(0, rc, err)
        self.assertNotIn(self.tmp + "/store/push-github-pat", self.w.files)
        self.assertIn("'wk key set github-pat' stores one", err)
        self.assertIn("'wk key set bugzilla-api-key' stores one", err)

    def test_the_config_every_workspace_includes_is_written_before_the_keys(self):
        self.w.seed()
        self.push("on")
        acts = [e for e in self.w.acts()]
        cfg = acts.index(("write", self.w.secrets_dir + "/ssh_config"))
        load = next(i for i, e in enumerate(acts) if e[0] == "act" and "ssh-add -" in e[1][-1])
        self.assertLess(cfg, load)


class TestOff(PushTest):
    def on(self):
        self.w.seed()
        self.push("on")

    def test_it_empties_the_agent_and_removes_both_files(self):
        self.on()
        rc, _, err = self.push("off")
        self.assertEqual(0, rc, err)
        self.assertEqual(set(), self.w.agents[SOCK])
        self.assertNotIn(self.tmp + "/store/push-github-pat", self.w.files)
        self.assertNotIn(self.tmp + "/store/push-bugzilla-api-key", self.w.files)
        self.assertIn("push is OFF", err)
        self.assertIn("ssh_config", self.w.listdir(self.w.secrets_dir))

    def test_it_reads_the_agent_back_rather_than_trusting_the_clear(self):
        self.on()
        self.w.stubborn = True
        rc, _, err = self.push("off")
        self.assertEqual(1, rc)
        self.assertIn("still holds 2 identity/identities", err)
        self.assertNotIn("push is OFF", err)

    def test_it_reads_the_files_back_too(self):
        self.on()
        self.w.react(["sh", "-c", "rm -f %s" % shell.sh_quote(self.tmp + "/store/push-bugzilla-api-key")], lambda a, f: Result(0))
        rc, _, err = self.push("off")
        self.assertEqual(1, rc)
        self.assertIn("the Bugzilla API key is still at", err)

    def test_the_private_halves_never_move(self):
        self.on()
        before = {p: t for p, t in self.w.files.items() if p.startswith(self.w.held)}
        self.push("off")
        self.assertEqual(before, {p: t for p, t in self.w.files.items() if p.startswith(self.w.held)})


class TestStatus(PushTest):
    def test_held_back_is_off(self):
        self.w.seed()
        rc, out, err = self.push("status")
        self.assertEqual(1, rc)
        self.assertIn("fork       held back (%s)" % self.w.held, out)
        self.assertIn("api        held back (%s/github-pat)" % self.w.held, out)
        self.assertIn("push is OFF", err)
        self.assertNotIn("ghp-held", out + err)

    def test_loaded_is_on(self):
        self.w.seed()
        self.push("on")
        rc, out, err = self.push("status")
        self.assertEqual(0, rc)
        self.assertIn("fork       push allowed (in the agent)", out)
        self.assertIn("bugzilla   'git-webkit pr' can file the bug", out)

    def test_a_write_credential_with_no_key_loaded_is_still_on(self):
        self.w.seed()
        self.w.files[self.tmp + "/store/push-github-pat"] = "ghp\n"
        rc, out, err = self.push("status")
        self.assertEqual(0, rc)
        self.assertIn("the injector has a write credential", err)

    def test_nothing_at_all_is_4(self):
        rc, out, err = self.push("status")
        self.assertEqual(4, rc)
        self.assertIn("no key ('wk key deploy')", out)
        self.assertIn("no token ('wk key set github-pat')", out)
        self.assertIn("no key ('wk key set bugzilla-api-key')", out)

    def test_a_private_half_in_the_mounted_directory_is_named(self):
        self.w._set_file(self.w.secrets_dir + "/build_key_strays", "x\n")
        _, _, err = self.push("status")
        self.assertIn("readable by every workspace", err)
        self.assertIn("build_key_strays", err)

    def test_it_changes_nothing(self):
        self.w.seed()
        self.push("status")
        self.assertEqual([], self.w.acts())


class TestAMachineWithNoSwitch(PushTest):
    """A build box names no agent socket and its core.sshCommand names the private half: the key is live."""

    def setUp(self):
        super().setUp()
        self.box.sock = None

    def test_status_says_the_key_is_live_and_is_on(self):
        self.w.seed()
        rc, out, err = self.push("status")
        self.assertEqual(0, rc)
        self.assertIn("always live", out)
        self.assertNotIn("held back (", out.split("api")[0])
        self.assertIn("no ssh-agent socket", err)
        self.assertIn("cannot be switched off", err)

    def test_on_and_off_refuse_with_5_and_touch_nothing(self):
        self.w.seed()
        for action in ("on", "off"):
            with self.subTest(action=action):
                rc, _, err = self.push(action)
                self.assertEqual(5, rc)
                self.assertIn("docs/PLAN.md", err)
                self.assertEqual([], self.w.acts())


class TestTheGuests(PushTest):
    def test_the_machine_half_runs_first_and_the_guests_are_told_the_action(self):
        self.w.seed()
        for action in ("on", "off"):
            with self.subTest(action=action):
                self.push(action, macos=True)
                acts = [e[1] for e in self.w.acts() if e[0] == "act"]
                self.assertEqual(("guests", action), acts[-1])
                self.assertTrue(any("ssh-add" in a[-1] for a in acts[:-1]))

    def test_a_guest_that_did_not_converge_fails_the_switch(self):
        self.w.seed()
        self.vm["vm_push_keys_converge"].side_effect = lambda *a: False
        rc, _, err = self.push("off", macos=True)
        self.assertEqual(1, rc)
        self.assertIn("may still reach the agent", err)

    def test_no_guest_half_off_a_macos_host(self):
        self.w.seed()
        self.push("on")
        self.assertFalse(self.vm["vm_push_keys_converge"].called)

    def test_a_key_in_the_guests_agent_is_on_with_no_guest_up(self):
        self.w.seed()
        self.vm["vm_push_agent_keys"].return_value = 1
        self.vm["vm_push_keys_state"].return_value = [["demo", "stopped", ""]]
        rc, out, err = self.push("status", macos=True)
        self.assertEqual(0, rc)
        self.assertIn("1 key(s) in the agent this host runs for them", out)
        self.assertIn("guest demo       stopped -- not read; 'wk vm start demo' converges it", out)

    def test_a_running_guest_that_reaches_it_is_on(self):
        self.w.seed()
        self.vm["vm_push_agent_keys"].return_value = 0
        self.vm["vm_push_keys_state"].return_value = [["demo", "running", "1 key(s) through the agent on this host"]]
        rc, out, _ = self.push("status", macos=True)
        self.assertEqual(0, rc)
        self.assertIn("the agent this host runs for them holds nothing", out)

    def test_nothing_reaching_it_is_off(self):
        self.w.seed()
        self.vm["vm_push_agent_keys"].return_value = 0
        self.vm["vm_push_keys_state"].return_value = [["demo", "running", ""]]
        rc, out, _ = self.push("status", macos=True)
        self.assertEqual(1, rc)
        self.assertIn("no agent socket -- a push from in there is refused", out)


class TestTheClaudeSessionGate(PushTest):
    """`wk push on` with a claude session already running in a workspace."""

    def setUp(self):
        super().setUp()
        self.w.seed()
        self.box.claude = {"ws-one": ["4242"], "ws-two": ["77"], "idle": []}
        os.environ["WK_DESTRUCTIVE"] = "1"

    def test_no_session_is_nothing_to_ask(self):
        self.box.claude = {"idle": []}
        rc, _, err = self.push("on")
        self.assertEqual(0, rc, err)
        self.assertNotIn("claude session", err)
        self.assertEqual("1", os.environ.get("WK_DESTRUCTIVE"), "the declaration was stripped rather than answered")

    def test_an_answered_question_ends_every_session_before_the_keys_load(self):
        os.environ["WK_YES"] = "1"
        rc, _, err = self.push("on")
        self.assertEqual(0, rc, err)
        acts = [e[1] for e in self.w.acts() if e[0] == "act"]
        kills = [i for i, a in enumerate(acts) if a[0] == "exec"]
        load = next(i for i, a in enumerate(acts) if "ssh-add -" in a[-1])
        self.assertEqual(["ws-one", "ws-two"], [acts[i][1] for i in kills])
        self.assertLess(max(kills), load)
        self.assertIn("kill 4242 2>/dev/null; exit 0", acts[kills[0]][-1])

    def test_a_declined_question_ends_nothing_and_loads_nothing(self):
        """No terminal is a decline, and the decline stops the command rather than falling through to the keys."""
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertIn("push stays off", err)
        self.assertEqual(set(), self.w.agents[SOCK])
        self.assertEqual({"ws-one": ["4242"], "ws-two": ["77"], "idle": []}, self.box.claude)

    def test_force_leaves_the_sessions_running_through_a_recorded_barrier(self):
        os.environ["WK_FORCE"] = "1"
        rc, _, err = self.push("on")
        self.assertEqual(0, rc, err)
        self.assertEqual(["4242"], self.box.claude["ws-one"])
        self.assertIn("FORCED past a barrier", err)
        self.assertIn("ws-one ws-two", err)
        self.assertEqual(1, len(act._forced))

    def test_a_session_that_outlives_the_kill_keeps_the_keys_out(self):
        os.environ["WK_YES"] = "1"
        self.box.stubborn = {"ws-one"}
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertIn("did not stop on TERM after 15s -- killing", err)
        self.assertIn("outlived a KILL", err)
        self.assertEqual(set(), self.w.agents[SOCK])
        self.assertEqual([1] * 20, self.clock.slept)


class TestAskingAnotherMachine(PushTest):
    def setUp(self):
        super().setUp()
        self.peer = Box(self.w, self.w.env, name="peerbox")
        self.boxes["peerbox"] = self.peer
        self.w.react(["sh", "-c", '"$0" push "$1" 2>&1'], lambda a, f: f.effects.append(("here", a[-1])) or Result(1, "api  held\n"))
        self.w.react(["hostname"], lambda a, f: Result(0, "thishost\n"))

    def test_the_far_side_runs_the_same_command_and_its_verdict_is_this_ones(self):
        for rc_there in (0, 1):
            with self.subTest(rc=rc_there):
                self.peer.far = (rc_there, "fork       push allowed (in the agent)\n")
                rc, out, _ = self.main("status", "--target", "peerbox")
                self.assertEqual(rc_there, rc)
                self.assertEqual(("push", "status"), self.peer.asked[-1])
                self.assertIn("peerbox               fork       push allowed", out)

    def test_a_machine_that_did_not_answer_is_3_and_never_off(self):
        self.peer.side = "unreachable"
        rc, out, err = self.main("off", "--target", "peerbox")
        self.assertEqual(3, rc)
        self.assertIn("peerbox                unreachable over ssh: timed out", out)
        self.assertIn("not asked: peerbox", err)
        self.assertEqual([], self.peer.asked)

    def test_all_asks_this_machine_then_every_other_and_the_worst_wins(self):
        self.peer.far = (0, "on\n")
        rc, out, _ = self.main("status", "--all")
        self.assertEqual(1, rc)
        self.assertEqual([("here", "status")], [e for e in self.w.effects if e[0] == "here"])
        self.assertLess(out.index("thishost"), out.index("peerbox"))

    def test_an_unknown_machine_is_refused_by_name(self):
        rc, _, err = self.main("status", "--target", "nosuch")
        self.assertEqual(1, rc)
        self.assertIn("unknown target 'nosuch'", err)

    def test_an_unknown_word_is_the_usage(self):
        rc, _, err = self.main("maybe")
        self.assertEqual(1, rc)
        self.assertIn("usage: wk push on|off|status", err)


class TestCrashOnlyAndDryRun(PushTest):
    def world(self, action):
        w = World(self.tmp)
        w.seed()
        if action == "off":
            w.agents[SOCK] = {"KEY:fork", "KEY:forkwpe"}
            w.files[self.tmp + "/store/push-github-pat"] = "ghp-held\n"
        return w

    def test_killpoints_push(self):
        """`killpoints[push]`: killed after any effect of `on` or `off` and re-run, the switch lands where an
        uninterrupted run leaves it."""
        for action in ("on", "off"):
            with self.subTest(action=action):
                converges(self, lambda: types.SimpleNamespace(fake=self.world(action)),
                          lambda n: self.push(action, w=n.fake), lambda n: n.fake.state())

    def test_a_dry_run_is_the_wet_runs_plan_and_changes_nothing(self):
        for action in ("on", "off"):
            with self.subTest(action=action):
                wet = self.world(action)
                self.push(action, w=wet)
                dry = self.world(action)
                before = dry.state()
                os.environ["WK_DRY_RUN"] = "1"
                try:
                    rc, _, err = self.push(action, w=dry)
                finally:
                    del os.environ["WK_DRY_RUN"]
                self.assertEqual(0, rc, err)
                self.assertEqual(wet.acts(), dry.acts())
                self.assertGreaterEqual(len(dry.acts()), 5)
                self.assertEqual(before, dry.state())
                self.assertIn("dry run: push is", err)


class TestNoFalseClaim(WkTest):
    def test_ensure_dir_dies_rather_than_claiming_a_dir_it_could_not_make(self):
        """ensure_dir (lib/common.sh) reports an error, not '==> create', when mkdir fails"""
        d = self.tmp / "no-write" / "child"
        os.makedirs(self.tmp / "no-write")
        os.chmod(self.tmp / "no-write", 0o500)
        self.addCleanup(os.chmod, self.tmp / "no-write", 0o700)
        cp = bash(f'. "{REPO}/lib/common.sh"; ensure_dir "{d}" 0700')
        self.assertNotEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("cannot create", cp.stderr)
        self.assertNotIn("==> create", cp.stderr)
        self.assertFalse(d.exists())


class TestTheStatusRowIsCredentialsNotThePosition(WkTest):
    """`wk status` reads this machine's own directories; only `wk push status` asks the agent and the
    injector. So the row is named for what it measures and names the command that answers the other question."""

    FORKS = ("fork", "forkwpe")

    def _row(self, keys=(), pat=False, in_vm=False):
        from wk import status
        from wk.store import Store
        secrets = self.tmp / "store" / "secrets"
        held = self.tmp / "store" / "push-keys"
        for d in (secrets, held):
            d.mkdir(parents=True, exist_ok=True)
        for k in keys:
            (held / f"build_key_{k}").write_text("not-a-key\n")
        if pat:
            (held / "github-pat").write_text("ghp_notatoken\n")
        store = Store({"WK_STORE": str(self.tmp / "store"), "WK_HOST_SECRETS": str(secrets), "HOME": str(self.tmp)})
        return status.push_record(store, "testmachine", list(self.FORKS), in_vm)

    def test_the_forks_are_the_stores_table(self):
        cp = bash('. lib/common.sh; . lib/store.sh; wk_push_forks | awk \'NF {print $1}\'')
        self.assertEqual(tuple(cp.stdout.split()), self.FORKS)

    def test_the_row_is_named_for_the_credentials_it_read(self):
        row = self._row(keys=("fork", "forkwpe"))
        self.assertEqual("push credentials", row["name"])
        self.assertIn("'wk push status' says whether they are loaded", row["detail"])

    def test_it_never_reports_a_switch_position(self):
        for keys in ((), ("fork",), ("fork", "forkwpe")):
            with self.subTest(keys=keys):
                self.assertNotIn(self._row(keys=keys)["state"], ("on", "off"))

    def test_every_key_held(self):
        self.assertEqual("keys held", self._row(keys=("fork", "forkwpe"))["state"])

    def test_some_of_them(self):
        row = self._row(keys=("fork",))
        self.assertEqual("some keys held", row["state"])
        self.assertIn("1 deploy key(s), 1 absent", row["detail"])

    def test_none_of_them(self):
        row = self._row()
        self.assertEqual("no keys", row["state"])
        self.assertIn("0 deploy key(s), 2 absent", row["detail"])

    def test_the_api_token_is_reported_beside_them(self):
        self.assertIn("no API token", self._row(keys=("fork",))["detail"])
        self.assertIn("an API token", self._row(pat=True)["detail"])

    def test_the_podman_vm_reports_no_row_at_all(self):
        self.assertIsNone(self._row(keys=("fork", "forkwpe"), in_vm=True))
