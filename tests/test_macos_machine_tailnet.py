"""The podman machine is a tailnet node of its own (host/macos/machine.sh).

It holds this workstation's lanes, and a lane's bytes reach a board over the
tailnet -- so the half that can read the store has to be the half that can
reach the board, or neither can. gvproxy answers a 100.x address itself
(measured 2026-09-16: ping replies in 0.13ms and a connection to port 22 is
accepted) and delivers nothing, so the machine joins rather than being routed.

Its workspaces do not join with it: they run `--network none` and reach the
world only through the egress proxy's socket (targets/container.sh), so this
is not a hole in the sandbox -- the sandbox is the container, not the VM.

The block is lifted out of the stage and driven against a `podman` stub that
records its argv, the tests/test_macos_machine_disk.py idiom: no machine is
created, started or joined, and no key leaves this test.

Run: python3 -m unittest tests.test_macos_machine_tailnet -v
"""
import unittest

from tests.support import REPO, WkTest, bash, stub_path

STAGE = REPO / "host" / "macos" / "machine.sh"

# `tailscale status --json` answers whatever the case wants; everything else
# succeeds and is recorded, so `up` can be asserted on without being run.
PODMAN_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$*" in
    *"--format {{.State}}"*)     echo "${WK_TEST_STATE:-running}" ;;
    *"command -v tailscale"*)    [ -n "$WK_TEST_NO_TS" ] && exit 1 ;;
    *"tailscale status --json"*) printf '%s' "$WK_TEST_TS_JSON" ;;
    *"tailscale up"*)            exit "${WK_TEST_UP_RC:-0}" ;;
esac
exit 0
"""

LOGGED_OUT = '{"BackendState": "NeedsLogin", "Self": {"TailscaleIPs": []}}'
RUNNING = '{"BackendState": "Running", "Self": {"TailscaleIPs": ["100.1.2.3"]}}'


def tailnet_block():
    """From the comment that opens it to the `unset` that closes it."""
    text = STAGE.read_text()
    start = text.index("# The machine holds this workstation's lanes")
    end = text.index("unset -f _ts _ts_state", start) + len("unset -f _ts _ts_state")
    return text[start:end]


class TailnetStage(WkTest):
    def run_block(self, env=None, key="tskey-auth-kAAAAAA-secret"):
        log = self.tmp / "podman.log"
        keyfile = self.tmp / "authkey"
        keyfile.write_text(key + "\n")
        with stub_path({"podman": PODMAN_STUB}) as binp:
            cp = bash(
                f'. "{REPO}/lib/common.sh"\n'
                f'. "{REPO}/lib/store.sh"\n'
                'WK_MACHINE=wk\n'
                'wk_machine_name() { echo probehost; }\n'
                + ("" if key else 'wk_tailscale_authkey() { return 1; }\n')
                + tailnet_block() + "\n",
                env={"PATH": f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin",
                     "WK_TEST_PODMAN_LOG": str(log),
                     "WK_TS_AUTHKEY": str(keyfile),
                     "WK_TEST_TS_JSON": LOGGED_OUT,
                     **(env or {})})
        self.log = log.read_text() if log.exists() else ""
        return cp


class TestAMachineAlreadyOnTheTailnetIsLeftAlone(TailnetStage):
    def test_it_reports_the_node_and_joins_nothing(self):
        # `unchanged` is a debug line: a stage that changed nothing says so
        # only when asked, like every other reconcile in this file.
        cp = self.run_block({"WK_TEST_TS_JSON": RUNNING, "WK_DEBUG": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("probehost-vm", out)
        self.assertIn("100.1.2.3", out)
        self.assertNotIn("tailscale up", self.log, "it re-joined a node already on the tailnet")


class TestALoggedOutMachineJoins(TailnetStage):
    def test_it_brings_the_node_up_under_a_derived_name(self):
        """The name is this machine's own plus `-vm`, so nothing about how to
        reach it is written down (CLAUDE.md, 'Cattle, not pets')."""
        self.run_block()
        self.assertIn("tailscale up", self.log)
        self.assertIn("--hostname=probehost-vm", self.log)
        self.assertIn("--advertise-tags=tag:wk", self.log)

    def test_the_key_never_reaches_argv(self):
        """/proc makes a command line world readable, so the key goes into a
        0600 file in the guest and is removed again."""
        self.run_block()
        self.assertNotIn("tskey-auth-kAAAAAA-secret", self.log)
        self.assertIn("--auth-key=file:/var/lib/tailscale/wk-authkey", self.log)
        self.assertIn("rm -f /var/lib/tailscale/wk-authkey", self.log)

    def test_a_join_that_did_not_take_says_what_fails_exactly_there(self):
        cp = self.run_block({"WK_TEST_UP_RC": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)   # a stage reports; it does not abort setup
        self.assertIn("did not reach Running", out)
        self.assertIn("single-use, expired", out)
        self.assertIn("re-running this stage is safe", out)

    def test_the_spent_key_is_removed_even_when_the_join_failed(self):
        self.run_block({"WK_TEST_UP_RC": "1"})
        self.assertIn("rm -f /var/lib/tailscale/wk-authkey", self.log)


class TestWhatItWillNotDo(TailnetStage):
    def test_no_key_is_reported_rather_than_guessed_at(self):
        cp = self.run_block(key="")
        out = cp.stdout + cp.stderr
        self.assertIn("wk key set tailnet", out)
        self.assertNotIn("tailscale up", self.log)

    def test_a_stopped_machine_is_not_started_to_ask(self):
        cp = self.run_block({"WK_TEST_STATE": "stopped"})
        self.assertIn("not running", cp.stdout + cp.stderr)
        self.assertNotIn("machine start", self.log)

    def test_a_machine_without_tailscale_says_what_that_costs(self):
        cp = self.run_block({"WK_TEST_NO_TS": "1"})
        self.assertIn("cannot be deployed", cp.stdout + cp.stderr)
        self.assertNotIn("tailscale up", self.log)

    def test_a_dry_run_joins_nothing(self):
        cp = self.run_block({"WK_DRY_RUN": "1"})
        self.assertIn("dry run", cp.stdout + cp.stderr)
        self.assertNotIn("tailscale up", self.log)


class TestTheWorkspacesDoNotJoinWithIt(unittest.TestCase):
    """The sandbox is the container, not the VM: a workspace has no network
    interface at all, so putting the machine on the tailnet reaches none of
    them."""

    def test_a_workspace_container_has_no_network(self):
        text = (REPO / "targets" / "container.sh").read_text()
        self.assertIn("--network none", text)


if __name__ == "__main__":
    unittest.main()
