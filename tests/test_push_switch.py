"""`wk push` -- the deploy-key switch: where it runs, what `--all` covers,
what `on` does about a claude session already running, and that it never
reports a move it did not make.

Run: python3 -m unittest tests.test_push_switch -v
"""
import json
import os
import subprocess
import unittest

from tests.support import REPO, WK, WkTest, bash, func_body


def decl(name):
    """The dispatcher's own declaration line for one command."""
    cp = subprocess.run([str(WK), "--declarations"], cwd=str(REPO),
                        stdout=subprocess.PIPE, text=True, timeout=60)
    for line in cp.stdout.splitlines():
        f = line.split("\t")
        if f and f[0] == name:
            return f
    raise AssertionError(f"{name} is not declared")


class TestWhere(WkTest):
    def test_push_runs_on_the_machine_that_holds_the_keys(self):
        """`wk push` declares where=local: the keys are this device's own
        (wk_secrets_dir), and on macOS the podman machine reads them as a
        mount -- so nothing is forwarded and nothing has to be running"""
        self.assertEqual(decl("push")[1], "local")

    def test_nothing_forwards_it_into_the_podman_machine(self):
        """the hop is gone, not merely unused: no `--store` flag, no second
        `wk push` invocation of its own, no `podman machine ssh`"""
        src = (REPO / "cmd" / "push").read_text()
        for gone in ("--store", "store_half", "podman machine ssh"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

    def _as_build_machine(self):
        """The environment of a shared build machine: the ~/.wk-remote marker
        the dispatcher reads, and a store of its own that is local (so the
        macOS-only forward into the podman VM does not apply, exactly as on
        the Linux machine this stands in for)."""
        marker = self.tmp / "wk-remote"
        marker.write_text("target=buildbox4\n")
        store = self.tmp / "store"
        store.mkdir()
        return {"WK_REMOTE_MARKER": str(marker), "WK_STORE": str(store)}

    def test_a_store_command_runs_on_a_build_machine(self):
        """a build machine holds its own store, so `wk push` is not refused
        there -- which is what `wk push --all` needs to reach it"""
        cp = self.run_wk("push", "status", env=self._as_build_machine())
        self.assertNotIn("acts on a workstation", cp.stdout)

    def test_a_host_command_is_still_refused_on_a_build_machine(self):
        """where=host keeps its refusal: `wk quiesce` acts on hardware here"""
        cp = self.run_wk("quiesce", "status", env=self._as_build_machine())
        self.assertIn("acts on a workstation", cp.stdout)

    def test_a_store_command_is_refused_inside_a_workspace(self):
        """the keys are on the host and a workspace cannot reach them"""
        marker = self.tmp / "wk-workspace"
        marker.write_text("name=probe\ntarget=container\n")
        cp = self.run_wk("push", "status", env={"WK_MARKER": str(marker)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("workspace", cp.stdout)

    def test_the_fan_out_needs_no_flag_override(self):
        """--all/--target need the target confs and ssh reach to the fleet, and
        get them for free now that the whole command runs here: a `flag ...
        where=local` override would be a second copy of that decision"""
        src = (REPO / "cmd" / "push").read_text()
        self.assertNotIn("flag --all,--target", src)
        self.assertIn("# wk: where=local", src)


class TestAllCoversThisMachine(WkTest):
    def test_all_asks_this_machine_before_the_fleet(self):
        """`--all` means every machine: this one through its own `wk`, then
        for_each_machine for the rest"""
        src = (REPO / "cmd" / "push").read_text()
        body = src[src.index('if [ "$TARGET" = --all ]'):]
        self.assertLess(body.index("_here"), body.index("for_each_machine"),
                        "the fan-out runs before this machine's own row")


class TestNoFalseClaim(WkTest):
    def test_ensure_dir_dies_rather_than_claiming_a_dir_it_could_not_make(self):
        """ensure_dir reports an error, not '==> create', when mkdir fails"""
        d = self.tmp / "no-write" / "child"
        os.makedirs(self.tmp / "no-write")
        os.chmod(self.tmp / "no-write", 0o500)
        self.addCleanup(os.chmod, self.tmp / "no-write", 0o700)
        # ensure_dir narrates on stderr (info/die), like everything in lib.
        cp = bash(f'. "{REPO}/lib/common.sh"; ensure_dir "{d}" 0700')
        self.assertNotEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("cannot create", cp.stderr)
        self.assertNotIn("==> create", cp.stderr)
        self.assertFalse(d.exists())

    def test_nothing_moves_between_the_two_directories(self):
        """The switch is what an ssh-agent holds, and there is no file walk
        beside it: two positions on disk means a crash between them, and a key
        in the mounted directory is the position this command exists to
        prevent."""
        src = (REPO / "cmd" / "push").read_text()
        for gone in ("move_keys", "MOVED", "STUCK", "purge_copies"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

    def test_on_without_an_agent_fails_and_claims_nothing(self):
        """With no agent answering there is nowhere to put the keys, so `on`
        must not report a switch it did not throw."""
        # wk_secrets_dir (lib/store.sh) reads WK_HOST_SECRETS on a macOS host
        # and $WK_STORE/secrets everywhere else; one directory under both names.
        secrets = self.tmp / "store" / "secrets"
        held = self.tmp / "store" / "push-keys"
        held.mkdir(parents=True)
        secrets.mkdir(parents=True)
        (held / "build_key_fork").write_text("placeholder-not-a-key\n")

        cp = self.run_wk("push", "on", env={
            "WK_STORE": str(self.tmp / "store"),
            "WK_HOST_SECRETS": str(secrets),
            "WK_PUSH_AGENT_SOCK": str(self.tmp / "no-agent.sock"),
            "WK_PUSH_PAT_FILE": str(self.tmp / "pat"),
            "WK_MACHINE": "wk-no-such-machine",
        })
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no ssh-agent answers", cp.stdout)
        self.assertNotIn("push is ON", cp.stdout)
        self.assertTrue((held / "build_key_fork").exists(),
                        "a private half left the directory nothing mounts")

    def test_the_subshell_trap_is_real(self):
        """the mechanism the two above defend against: bash applies neither
        errexit nor a die's exit across an assignment's command substitution"""
        cp = bash('f() { false; echo reached; }; n=$(f); printf %s "$n"')
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "reached")


class TestStatusIsReadOnly(WkTest):
    def test_status_is_declared_readonly(self):
        """`wk push status` changes nothing, so a stopped podman machine is
        reported rather than started"""
        self.assertIn("# wk: readonly status", (REPO / "cmd" / "push").read_text())


class TestTheStatusRowIsCredentialsNotThePosition(WkTest):
    """`wk status` reads this machine's own directories; only `wk push status`
    asks the agent and the injector. So the row is named for what it measures
    -- a key on disk is not a thrown switch -- and it names the command that
    answers the other question."""

    FORKS = ("fork", "forkwpe")

    def _row(self, keys=(), pat=False, in_vm=False):
        import sys
        sys.path.insert(0, str(REPO / "lib"))
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
        """The keys are the host's; the VM mounts only the public halves, so a
        row from in there could only ever say `no keys` about a machine that
        holds two."""
        self.assertIsNone(self._row(keys=("fork", "forkwpe"), in_vm=True))

class TestTheClaudeSessionGate(unittest.TestCase):
    """`wk push on` with a claude session already running in a workspace.

    push_end_sessions_first is lifted out of cmd/push and run against a
    recording `end_agent_sessions`: the rest of that file needs real deploy
    keys, a real ssh-agent and a real workspace to reach this branch at all.
    """

    def _gate(self, args, env=None):
        text = (REPO / "cmd" / "push").read_text()
        return bash(
            f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n'
            'end_agent_sessions() { printf "ended: %s\\n" "$*" >&2; }\n'
            "push_end_sessions_first() {"
            + func_body(text, "push_end_sessions_first")
            + "}\n"
            f'push_end_sessions_first {args}\n',
            env=env)

    def test_no_session_is_nothing_to_do(self):
        cp = self._gate("")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("ended:", cp.stderr)

    def test_an_answered_question_ends_every_session_named(self):
        cp = self._gate("ws-one ws-two", env={"WK_YES": "1"})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("ended: ws-one ws-two", cp.stderr)

    def test_a_declined_question_ends_nothing_and_loads_nothing(self):
        """No terminal is a decline (confirm, lib/common.sh), and the decline
        stops the command rather than falling through to the keys."""
        cp = self._gate("ws-one")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("push stays off", cp.stderr)
        self.assertNotIn("ended:", cp.stderr)

    def test_force_leaves_the_session_running_and_goes_on(self):
        """The barrier --force crosses: the session keeps its commit wall and
        gains a push, which is the thing being asked for."""
        cp = self._gate("ws-one", env={"WK_FORCE": "1"})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("ended:", cp.stderr)
        self.assertIn("FORCED past a barrier", cp.stderr)
        self.assertIn("ws-one", cp.stderr)

    def test_the_forced_barrier_is_recorded_at_the_end_of_the_run(self):
        """barrier() registers the summary that prints again when the command
        ends, so one line atop a long run is not the only record of it."""
        cp = self._gate("ws-one", env={"WK_FORCE": "1"})
        self.assertIn("forced past 1 barrier(s)", cp.stderr)


class TestForceReachesTheCommand(WkTest):
    def test_on_takes_force_through_the_dispatcher(self):
        """--force is the dispatcher's own global flag, stripped before
        cmd/push parses argv: `wk push on --force` reaches the same place
        `wk push on` does rather than being refused as an unknown option."""
        secrets = self.tmp / "store" / "secrets"
        held = self.tmp / "store" / "push-keys"
        held.mkdir(parents=True)
        secrets.mkdir(parents=True)
        (held / "build_key_fork").write_text("placeholder-not-a-key\n")

        cp = self.run_wk("push", "on", "--force", env={
            "WK_STORE": str(self.tmp / "store"),
            "WK_HOST_SECRETS": str(secrets),
            "WK_PUSH_AGENT_SOCK": str(self.tmp / "no-agent.sock"),
            "WK_PUSH_PAT_FILE": str(self.tmp / "pat"),
            "WK_MACHINE": "wk-no-such-machine",
        })
        self.assertNotIn("usage:", cp.stdout)
        self.assertIn("no ssh-agent answers", cp.stdout)
