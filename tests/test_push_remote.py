"""`unit push.remote_forwarding`: a build box holds no deploy key at rest and nothing forwards one to it, so a push is made
from the workstation and `wk push status` on the box says off.

Run: python3 tests/run.py -k tests.test_push_remote
"""
import sys

from tests import test_push_switch, test_wk_targets
from tests.support import REPO, owed

sys.path.insert(0, str(REPO / "lib"))
from wk import secrets  # noqa: E402


def forwarded(argv):
    """The agent socket this ssh command line forwards, or None; ssh takes an option's first value."""
    if "-A" in argv:
        return "$SSH_AUTH_SOCK"
    values = [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv[:-1])
              if a == "-o" and argv[i + 1].lower().startswith("forwardagent=")]
    return values[0] if values and values[0].lower() != "no" else None


class TestABoxHoldsNoKey(test_wk_targets.RemoteTest):
    def test_no_alias_names_an_identity_file(self):
        """The ssh config provision.sh writes on a box selects a fork by alias and names no IdentityFile."""
        text = secrets.alias_blocks(secrets.FORKS, "")
        self.assertIn("Host ", text)
        self.assertNotIn("IdentityFile", text)

    def test_an_agent_session_forwards_no_agent(self):
        argv = self.t.exec_argv("a", ["claude"], tty=True)[0]
        self.assertEqual("ssh", argv[0])
        self.assertIsNone(forwarded(argv), argv)

    def test_an_enter_shell_forwards_no_agent(self):
        argv = self.t.enter_argv("a")[0]
        self.assertEqual("ssh", argv[0])
        self.assertIsNone(forwarded(argv), argv)

    @owed("forwarding a push credential to a shared build box needs a design the user has not chosen: "
          "see docs/PLAN.md Decisions for the user")
    def test_an_explicit_push_from_a_box_works(self):
        """The shell a person opens on the box to push reaches a signing identity."""
        argv = self.t.enter_argv("a")[0]
        self.assertIsNotNone(forwarded(argv), argv)


class TestStatusOnTheBox(test_push_switch.PushTest):
    def setUp(self):
        super().setUp()
        self.box = test_push_switch.Box(self.w, self.w.env, name="buildbox", sock=None)
        self.boxes = {"container": self.box}

    def test_status_on_a_box_with_no_key_at_rest_is_not_a_missing_key(self):
        """Off, the position `wk ai` reads as nothing to hold back; 4 is a workstation missing its keys."""
        rc, out, err = self.push("status")
        self.assertEqual(1, rc, out + err)

    def test_each_fork_is_neither_held_nor_absent(self):
        p = test_push_switch.PUSH.Push(test_push_switch.Fleet(self.w, self.boxes), self.w.sec(), self.clock)
        self.assertEqual({"forwarded"}, {p.where(f, set()) for f, _, _ in secrets.FORKS})
