"""The podman machine is a tailnet node of its own (host/macos/machine.sh)."""
import unittest

from tests.support import REPO, WkTest, bash, stub_path

STAGE = REPO / "host" / "macos" / "machine.sh"

# `tailscale status --json` answers whatever the case wants; everything else
# succeeds and is recorded, so `up` can be asserted on without being run.
PODMAN_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$*" in
    "machine inspect wk")        printf '[{"State":"%s"}]\\n' "${WK_TEST_STATE:-running}" ;;
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
    start = text.index("# The machine holds this workstation's image workspaces")
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
                'WK_MACHINE=wk; export WK_MACHINE\n'
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
        cp = self.run_block({"WK_TEST_TS_JSON": RUNNING, "WK_DEBUG": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("probehost-vm", out)
        self.assertIn("100.1.2.3", out)
        self.assertNotIn("tailscale up", self.log, "it re-joined a node already on the tailnet")


class TestALoggedOutMachineJoins(TailnetStage):
    def test_it_brings_the_node_up_under_a_derived_name_and_the_key_never_reaches_argv(self):
        self.run_block()
        for word in ("tailscale up", "--hostname=probehost-vm", "--advertise-tags=tag:wk",
                     "--auth-key=file:/var/lib/tailscale/wk-authkey", "rm -f /var/lib/tailscale/wk-authkey"):
            self.assertIn(word, self.log)
        self.assertNotIn("tskey-auth-kAAAAAA-secret", self.log)

    def test_a_join_that_did_not_take_says_what_fails_and_still_removes_the_spent_key(self):
        cp = self.run_block({"WK_TEST_UP_RC": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)   # a stage reports; it does not abort setup
        self.assertIn("did not reach Running", out)
        self.assertIn("rm -f /var/lib/tailscale/wk-authkey", self.log)


class TestWhatItWillNotDo(TailnetStage):
    def test_each_reason_not_to_join_is_reported_and_nothing_joins_or_starts(self):
        for env, key, says in (({}, "", "wk key set tailnet"), ({"WK_TEST_STATE": "stopped"}, None, "not running"),
                               ({"WK_TEST_NO_TS": "1"}, None, "cannot be deployed"), ({"WK_DRY_RUN": "1"}, None, "dry run")):
            with self.subTest(says):
                cp = self.run_block(env, **({} if key is None else {"key": key}))
                self.assertIn(says, cp.stdout + cp.stderr)
                self.assertNotIn("tailscale up", self.log)
                self.assertNotIn("machine start", self.log)


if __name__ == "__main__":
    unittest.main()
