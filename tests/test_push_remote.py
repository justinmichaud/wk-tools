"""`unit push.remote_forwarding`: a build box holds no deploy key at rest and nothing forwards one to it, so a push is made
from the workstation (`wk pr open`, which fetches the box's branch into this machine's mirror) and `wk push status` on the
box says off.

Run: python3 tests/run.py -k tests.test_push_remote
"""
import contextlib
import io
import re
import shlex
import subprocess
import sys
from unittest import mock

from tests import test_pr_workflow, test_push_switch, test_wk_targets
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import secrets  # noqa: E402
from wk.store import Store  # noqa: E402

CMD_PR = test_pr_workflow.CMD_PR_MODULE


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

    def test_no_ssh_argv_to_a_box_forwards_an_agent(self):
        """Every ssh to a box is built by one Ssh.argv; the tree names ForwardAgent nowhere, and no box's block in
        dotfiles/ssh/config turns it on."""
        for argv in (self.t.machine.argv("true"), self.t.build_argv("a", ["ninja"])[0], self.t.exec_argv("a", ["true"])[0]):
            self.assertEqual("ssh", argv[0])
            self.assertIsNone(forwarded(argv), argv)
        grep = subprocess.run(["grep", "-rniI", "forwardagent\\|ssh -A", "lib", "cmd", "remote", "admin", "container", "vm"],
                              cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual("", grep.stdout)
        config = (REPO / "dotfiles" / "ssh" / "config").read_text()
        blocks = dict((m.group(1).strip(), m.group(2)) for m in re.finditer(r"^Host (.+)\n((?:[ \t].*\n|\n)*)", config, re.M))
        self.assertNotIn("*", blocks)
        boxes = [p.stem for p in (REPO / "machines").glob("*.conf") if "driver=remote" in p.read_text()]
        self.assertTrue(boxes)
        for box in boxes:
            self.assertNotIn("forwardagent", blocks.get(box, "").lower(), box)


class TestAPushFromABoxIsMadeHere(test_wk_targets.RemoteTest):
    def test_wk_pr_open_fetches_the_box_branch_into_the_mirror_and_pushes_from_here(self):
        self.fake.answer(["git"])
        self.fake.answer(["sh", "-c"])
        with mock.patch.object(CMD_PR, "pr_open_target", return_value=("WebKit/WebKit", "me:eng/b", "fork", "eng/b")), \
                mock.patch.object(CMD_PR.act, "exec_into") as gh, contextlib.redirect_stderr(io.StringIO()):
            CMD_PR.pr_open(self.t, "a", False, False, push_status=lambda: 0)
        mirror = Store(self.env).mirror()
        here = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "git"]
        self.assertIn(("git", "-C", mirror, "fetch", "--quiet", "box.example:/home/u/wk/ws/a/WebKit",
                       "+refs/heads/eng/b:refs/wk/push/box/eng/b"), here)
        push = [a for a in here if "push" in a]
        self.assertEqual(1, len(push), here)
        self.assertEqual(["push", "git@github.com:justinmichaud/WebKit.git", "refs/wk/push/box/eng/b:refs/heads/eng/b"],
                         list(push[0][-3:]))
        self.assertIn("build_key_fork -o IdentitiesOnly=yes", " ".join(push[0]))
        self.assertIn(("git", "-C", mirror, "update-ref", "-d", "refs/wk/push/box/eng/b"), here)
        self.assertEqual([], [c for c in self.fake.ssh_calls() if "push" in c[-1]])
        gh.assert_called_once()


class TestAPushOnTheBoxIsRefused(test_wk_targets.RemoteTest):
    def test_every_fork_alias_on_a_box_stops_on_the_refusal_naming_wk_pr_open(self):
        """remote/provision.sh writes these blocks; the ProxyCommand ssh would run for `git push` is run here as ssh runs it."""
        text = secrets.box_alias_blocks(secrets.FORKS)
        proxies = re.findall(r"^    ProxyCommand (.+)$", text, re.M)
        self.assertEqual(len(secrets.FORKS), len(proxies))
        cp = subprocess.run(shlex.split(proxies[0]), capture_output=True, text=True)
        self.assertEqual(1, cp.returncode)
        self.assertEqual("error: a build box holds no deploy key; push from the workstation:  wk pr open <workspace>\n",
                         cp.stderr)


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
        p = test_push_switch.PUSH.Push(test_push_switch.registry(self.w, self.boxes), self.w.sec(), self.clock)
        self.assertEqual({"forwarded"}, {p.where(f, set()) for f, _, _ in secrets.FORKS})
