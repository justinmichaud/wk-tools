"""`wk key push` -- the deploy-key switch, as a flow over the fake machine tests/test_wk_secrets.py builds."""
import contextlib
import io
import os
import shlex
import subprocess
import types
from unittest import mock

from tests.fakes import FakeRegistry
from tests.killpoints import converges
from tests.support import REPO, WkTest, bash, load_cmd
from tests.test_wk_secrets import SOCK, SecretsTest, World
from wk import act, guest, places, pushswitch, secrets
from wk.act import Refused
from wk.clock import FakeClock
from wk.machine import Result

KEY = load_cmd("key")


class Box(places.Driver):
    """The default place: workspaces with the claude pids each holds, and whether it names an agent socket."""

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


def registry(w, boxes):
    return FakeRegistry(w.env, w, lambda n, e: boxes[n], names=list(boxes), default=lambda: "container")


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
        """(exit status, stdout, stderr) of one `wk key push <action>`."""
        w = w or self.w
        out = io.StringIO()
        reg = registry(w, boxes or self.boxes)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                p = pushswitch.Push(reg, w.sec(macos=macos), self.clock, out)
                rc = p.run(action)
            except Refused as e:
                rc = e.status
        return rc, out.getvalue(), err.getvalue()

    def main(self, *argv, boxes=None):
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                rc = KEY.main(["push", *argv], env=self.w.env, reg=registry(self.w, boxes or self.boxes), out=out)
            except Refused as e:
                rc = e.status
        return rc, out.getvalue(), err.getvalue()


class TestWhere(WkTest):
    def _as_build_machine(self):
        marker = self.tmp / "wk-remote"
        marker.write_text("place=buildbox4\n")
        store = self.tmp / "store"
        store.mkdir()
        # The far end knows which place it is by its hostname, from its conf.
        machines = self.tmp / "machines"
        machines.mkdir()
        host = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip().lower()
        (machines / "buildbox4.conf").write_text("kind=build\ndriver=remote\nhostname=%s\n" % host)
        return {"WK_REMOTE_MARKER": str(marker), "WK_STORE": str(store), "WK_MACHINES_DIR": str(machines)}

    def test_a_store_command_runs_on_a_build_machine(self):
        cp = self.run_wk("key", "push", "status", env=self._as_build_machine())
        self.assertNotIn("acts on a workstation", cp.stdout)

    def test_a_host_command_is_still_refused_on_a_build_machine(self):
        cp = self.run_wk("quiesce", "status", env=self._as_build_machine())
        self.assertIn("acts on a workstation", cp.stdout)

    def test_a_store_command_is_refused_inside_a_workspace(self):
        marker = self.tmp / "wk-workspace"
        marker.write_text("name=probe\ntarget=container\n")
        cp = self.run_wk("key", "push", "status", env={"WK_MARKER": str(marker)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("this is workspace 'probe'", cp.stdout)

class TestOn(PushTest):
    def test_it_loads_every_key_and_hands_the_injector_both_credentials(self):
        self.w.seed()
        rc, out, err = self.push("on")
        self.assertEqual(0, rc, err)
        self.assertEqual({"KEY:" + k[0] for k in secrets.push_keys()}, self.w.agents[SOCK])
        self.assertEqual("ghp-held\n", self.w.files[self.tmp + "/store/push-github-pat"])
        self.assertEqual("bz-held\n", self.w.files[self.tmp + "/store/push-bugzilla-api-key"])

    def test_the_standing_read_token_is_not_the_switchs(self):
        self.w.seed()
        self.push("on")
        self.assertNotIn(self.tmp + "/store/read-github-pat", self.w.files)

    def test_no_agent_is_a_failure_that_claims_nothing(self):
        self.w.seed()
        del self.w.agents[SOCK]
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertIn("wk-ssh-agent.service", err)
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
        acts = self.w.acts()
        cfg = acts.index(("write", self.w.keyring_dir + "/ssh_config"))
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
        self.assertIn("ssh_config", self.w.listdir(self.w.keyring_dir))

    def test_it_reads_the_agent_back_rather_than_trusting_the_clear(self):
        self.on()
        self.w.stubborn = True
        rc, _, err = self.push("off")
        self.assertEqual(1, rc)
        self.assertEqual({"KEY:" + k[0] for k in secrets.push_keys()}, self.w.agents[SOCK])

    def test_it_reads_the_files_back_too(self):
        self.on()
        self.w.react(["sh", "-c", "rm -f %s" % shlex.quote(self.tmp + "/store/push-bugzilla-api-key")], lambda a, f: Result(0))
        rc, _, err = self.push("off")
        self.assertEqual(1, rc)

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
        self.assertNotIn("ghp-held", out + err)

    def test_loaded_is_on(self):
        self.w.seed()
        self.push("on")
        rc, out, err = self.push("status")
        self.assertEqual(0, rc)

    def test_a_write_credential_with_no_key_loaded_is_still_on(self):
        self.w.seed()
        self.w.files[self.tmp + "/store/push-github-pat"] = "ghp\n"
        rc, out, err = self.push("status")
        self.assertEqual(0, rc)

    def test_nothing_at_all_is_4(self):
        rc, out, err = self.push("status")
        self.assertEqual(4, rc)
        self.assertIn("no key ('wk key deploy')", out)
        self.assertIn("no token ('wk key set github-pat')", out)
        self.assertIn("no key ('wk key set bugzilla-api-key')", out)

    def test_a_private_half_in_the_mounted_directory_is_named(self):
        self.w._set_file(self.w.keyring_dir + "/build_key_strays", "x\n")
        _, _, err = self.push("status")
        self.assertIn("build_key_strays", err)

    def test_it_changes_nothing(self):
        self.w.seed()
        self.push("status")
        self.assertEqual([], self.w.acts())


class TestAMachineWithNoSwitch(PushTest):
    """A build box names no agent socket and holds no key at rest: one found there is live."""

    def setUp(self):
        super().setUp()
        self.box.sock = None

    def test_status_is_on_with_a_key_at_rest_and_off_without(self):
        self.assertEqual(1, self.push("status")[0])
        self.w.seed()
        self.assertEqual(0, self.push("status")[0])

    def test_on_and_off_refuse_with_5_and_touch_nothing(self):
        self.w.seed()
        for action in ("on", "off"):
            with self.subTest(action=action):
                rc, _, err = self.push(action)
                self.assertEqual(5, rc)
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
        self.assertIn("1 key(s) in the agent this host runs for them and its own pushes", out)
        self.assertIn("guest demo       stopped -- not read; 'wk start demo' converges it", out)

    def test_a_running_guest_that_reaches_it_is_on(self):
        self.w.seed()
        self.vm["vm_push_agent_keys"].return_value = 0
        self.vm["vm_push_keys_state"].return_value = [["demo", "running", "1 key(s) through the agent on this host"]]
        rc, out, _ = self.push("status", macos=True)
        self.assertEqual(0, rc)
        self.assertIn("the agent this host runs for them and its own pushes holds nothing", out)

    def test_nothing_reaching_it_is_off(self):
        self.w.seed()
        self.vm["vm_push_agent_keys"].return_value = 0
        self.vm["vm_push_keys_state"].return_value = [["demo", "running", ""]]
        self.assertEqual(1, self.push("status", macos=True)[0])


class TestTheClaudeSessionGate(PushTest):
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
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertEqual(set(), self.w.agents[SOCK])
        self.assertEqual({"ws-one": ["4242"], "ws-two": ["77"], "idle": []}, self.box.claude)

    def test_force_leaves_the_sessions_running_through_a_recorded_barrier(self):
        os.environ["WK_FORCE"] = "1"
        rc, _, err = self.push("on")
        self.assertEqual(0, rc, err)
        self.assertEqual(["4242"], self.box.claude["ws-one"])
        self.assertEqual(1, len(act._forced))

    def test_a_session_that_outlives_the_kill_keeps_the_keys_out(self):
        os.environ["WK_YES"] = "1"
        self.box.stubborn = {"ws-one"}
        rc, _, err = self.push("on")
        self.assertEqual(1, rc)
        self.assertEqual(set(), self.w.agents[SOCK])
        self.assertEqual([1] * 20, self.clock.slept)


class TestAskingAnotherMachine(PushTest):
    def setUp(self):
        super().setUp()
        self.peer = Box(self.w, self.w.env, name="peerbox")
        self.boxes["peerbox"] = self.peer
        self.w.react(["sh", "-c", '"$0" key push "$1" 2>&1'], lambda a, f: f.effects.append(("here", a[-1])) or Result(1, "api  held\n"))
        self.w.react(["hostname"], lambda a, f: Result(0, "thishost\n"))

    def test_the_far_side_runs_the_same_command_and_its_verdict_is_this_ones(self):
        for rc_there in (0, 1):
            with self.subTest(rc=rc_there):
                self.peer.far = (rc_there, "fork       push allowed (in the agent)\n")
                rc, out, _ = self.main("status", "--on", "peerbox")
                self.assertEqual(rc_there, rc)
                self.assertEqual(("key", "push", "status"), self.peer.asked[-1])
                self.assertIn("peerbox               fork       push allowed", out)

    def test_a_machine_that_did_not_answer_is_3_and_never_off(self):
        self.peer.side = "unreachable"
        rc, out, err = self.main("off", "--on", "peerbox")
        self.assertEqual(3, rc)
        self.assertIn("peerbox", out)
        self.assertEqual([], self.peer.asked)

    def test_all_asks_this_machine_then_every_other_and_the_worst_wins(self):
        self.peer.far = (0, "on\n")
        rc, out, _ = self.main("status", "--all")
        self.assertEqual(1, rc)
        self.assertEqual([("here", "status")], [e for e in self.w.effects if e[0] == "here"])
        self.assertLess(out.index("thishost"), out.index("peerbox"))

    def test_an_unknown_machine_is_refused_by_name(self):
        rc, _, err = self.main("status", "--on", "nosuch")
        self.assertEqual(1, rc)
        self.assertIn("nosuch", err)

    def test_an_unknown_word_is_refused_naming_the_verbs(self):
        rc, _, err = self.main("maybe")
        self.assertEqual(1, rc)
        self.assertIn("on, off or status", err)


class TestCrashOnlyAndDryRun(PushTest):
    def world(self, action):
        w = World(self.tmp)
        w.seed()
        if action == "off":
            w.agents[SOCK] = {"KEY:fork", "KEY:forkwpe"}
            w.files[self.tmp + "/store/push-github-pat"] = "ghp-held\n"
        return w

    def test_killpoints_push(self):
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


class TestNoFalseClaim(WkTest):
    def test_ensure_dir_dies_rather_than_claiming_a_dir_it_could_not_make(self):
        d = self.tmp / "no-write" / "child"
        os.makedirs(self.tmp / "no-write")
        os.chmod(self.tmp / "no-write", 0o500)
        self.addCleanup(os.chmod, self.tmp / "no-write", 0o700)
        cp = bash(f'. "{REPO}/lib/common.sh"; ensure_dir "{d}" 0700')
        self.assertNotEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("==> create", cp.stderr)
        self.assertFalse(d.exists())


class TestEveryPlaceThisMachineHoldsIsAsked(PushTest):
    """`on` ends a claude session in a macOS guest like one in a container."""

    def setUp(self):
        super().setUp()
        self.w.seed()
        self.guests = Box(self.w, self.w.env, name="vm", sock="/Users/admin/.wk-ssh-agent.sock")
        self.boxes["vm"] = self.guests
        self.box.claude = {"ctr": []}
        self.guests.claude = {"mac-rel": ["4242"]}

    def test_on_ends_it_in_the_guest_before_the_keys_load(self):
        os.environ["WK_YES"] = "1"
        rc, _, err = self.push("on", macos=True)
        self.assertEqual(0, rc, err)
        self.assertEqual([], self.guests.claude["mac-rel"])

    def test_a_machine_with_no_guests_asks_only_its_containers(self):
        del self.boxes["vm"]
        self.assertEqual([], pushswitch.Push(registry(self.w, self.boxes), self.w.sec(macos=True), self.clock).agent_sessions())


class TestThePodmanMachineIsHalfTheSwitch(PushTest):
    """In a macOS host's podman machine the guests' agent on the host is out of reach."""

    def setUp(self):
        super().setUp()
        self.w.seed()
        self.w.env["WK_IN_VM"] = "1"

    def test_off_empties_its_half_and_says_the_host_is_not_reached(self):
        rc, _, err = self.push("off")
        self.assertEqual(pushswitch.UNASKED, rc, err)
        self.assertIn("wk key push off", err)
        self.assertEqual(set(), self.w.agents[SOCK])

    def test_status_names_the_half_it_cannot_read(self):
        _, out, _ = self.push("status")
        self.assertIn("not read from the podman machine", out)

    def test_on_a_mac_itself_the_same_variable_is_not_the_vm(self):
        rc, _, err = self.push("off", macos=True)
        self.assertEqual(0, rc, err)


class TestTheScanReadsPsWhereThereIsNoProc(WkTest):
    """A macOS guest has no /proc: its `ps` names the executable, and only Claude Code's, from any install, is a session."""

    def test_only_claude_codes_executable_is_a_session(self):
        ps = ("#!/bin/sh\nprintf '%s\\n' '11 /opt/homebrew/Caskroom/claude-code/2.1.236/claude' '12 /Users/admin/.local/bin/claude' "
              "'13 /Users/admin/.local/share/claude/versions/2.1.270' '14 /Applications/Claude.app/Contents/MacOS/Claude' "
              "'15 node' '16 claude' '17 /bin/zsh' '18 /usr/bin/claudette'\n")
        (self.tmp / "ps").write_text(ps)
        (self.tmp / "ps").chmod(0o755)
        scan = pushswitch.AGENT_PID_SCAN.replace("if [ -d /proc/self ]", "if false")
        cp = subprocess.run(["sh", "-c", scan], env={"PATH": "%s:/usr/bin:/bin" % self.tmp}, capture_output=True, text=True)
        self.assertEqual(["11", "12", "13", "16"], cp.stdout.split(), cp.stderr)
