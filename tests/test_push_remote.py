"""`unit push.remote_forwarding`: a build box holds no deploy key at rest and nothing forwards one to it, so a push is made
from the workstation (`wk pr open`, which fetches the box's branch into this machine's mirror and pushes it with the deploy key)."""
import contextlib
import io
import re
import shlex
import subprocess
import sys
from unittest import mock

from tests import test_pr_workflow, test_wk_places
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


class TestABoxHoldsNoKey(test_wk_places.RemoteTest):
    def test_no_ssh_to_a_box_forwards_an_agent(self):
        """Neither an argv wk builds nor a block of dotfiles/ssh/config, the gateway's included."""
        for argv in (self.t.machine.argv("true"), self.t.build_argv("a", ["ninja"])[0], self.t.exec_argv("a", ["true"])[0],
                     self.t.exec_argv("a", ["claude"], tty=True)[0], self.t.enter_argv("a")[0]):
            self.assertEqual("ssh", argv[0])
            self.assertIsNone(forwarded(argv), argv)
        config = (REPO / "dotfiles" / "ssh" / "config").read_text()
        blocks = dict((m.group(1).strip(), m.group(2)) for m in re.finditer(r"^Host (.+)\n((?:[ \t].*\n|\n)*)", config, re.M))
        self.assertNotIn("*", blocks)
        self.assertIn("igalia.com", blocks)
        for host, block in blocks.items():
            self.assertNotIn("forwardagent", block.lower(), host)


class TestAPushFromABoxIsMadeHere(test_wk_places.RemoteTest):
    def test_wk_pr_open_fetches_the_box_branch_into_the_mirror_and_pushes_from_here(self):
        self.fake.answer(["git"])
        with mock.patch.object(CMD_PR, "pr_open_target", return_value=("WebKit/WebKit", "me:eng/b", "fork", "eng/b")), \
                mock.patch.object(CMD_PR.secrets, "Secrets", lambda *a: test_pr_workflow.BoxKeys()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.fake.files["/s/push-keys/build_key_fork"] = "KEY\n"
            CMD_PR.pr_open(self.t, "a", False, False)
        mirror = Store(self.env).mirror_dir()
        here = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "git"]
        self.assertIn(("git", "-C", mirror, "fetch", "--quiet", "box.example:/home/u/wk/ws/a/WebKit",
                       "+refs/heads/eng/b:refs/wk/push/box/eng/b"), here)
        push = [e[1] for e in self.fake.effects if e[0] == "run" and e[1][0] == "git" and "push" in e[1]]
        self.assertEqual(1, len(push), push)
        self.assertEqual(("-C", mirror), push[0][1:3])
        self.assertIn("-i /s/push-keys/build_key_fork", push[0][4])
        self.assertEqual(("push", "git@github.com:alice/WebKit.git", "refs/wk/push/box/eng/b:refs/heads/eng/b"), push[0][-3:])
        self.assertIn(("git", "-C", mirror, "update-ref", "-d", "refs/wk/push/box/eng/b"), here)
        self.assertEqual([], [c for c in self.fake.ssh_calls() if "push" in c[-1]])
        self.assertEqual(1, len([e for e in self.fake.effects if e[0] == "exec" and e[1][:3] == ("gh", "pr", "create")]))


class TestAPushOnTheBoxIsRefused(test_wk_places.RemoteTest):
    def test_every_fork_alias_on_a_box_stops_on_the_refusal_naming_wk_pr_open(self):
        """remote/provision.sh writes these blocks; the ProxyCommand ssh would run for `git push` is run here as ssh runs it."""
        text = secrets.box_alias_blocks(secrets.forks())
        proxies = re.findall(r"^    ProxyCommand (.+)$", text, re.M)
        self.assertEqual(len(secrets.forks()), len(proxies))
        cp = subprocess.run(shlex.split(proxies[0]), capture_output=True, text=True)
        self.assertEqual(1, cp.returncode)
        self.assertIn("wk pr open", cp.stderr)
