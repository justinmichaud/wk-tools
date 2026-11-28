"""DESIGN.md, Trust: agents neither push nor change anything on GitHub, and a person's session does both.

This was lost once, in a redesign that delivered the push keys and github-token to every server at setup, where any
agent read them. Push keys are cheap to make and to revoke so that a person's session can be given them and an agent's
never holds them. These tests are that rule; a change that fails one is a change to the design, made there first."""

import glob
import os
import re
import unittest
from unittest import mock

from tests import ROOT
from tests.world import World
from wk.agent import session
from wk.creds import rotation

# Store names a playbook may read for a server: pi's model key is the one an agent may hold, and sudo's password is
# Ansible's to become root with on a Mac, never written there.
AGENT_MAY_HOLD = {"litellm-key", "sudo-password"}
HOME = "/home/u/wk"


def server_playbooks():
    """Every task file, template and playbook that runs against a server: all but the controller's role and the install."""
    paths = glob.glob(os.path.join(ROOT, "ansible", "roles", "*", "**", "*"), recursive=True) + \
        [os.path.join(ROOT, "ansible", name) for name in ("site.yml", "check.yml")]
    return {p: open(p).read() for p in paths if os.path.isfile(p) and "/roles/controller/" not in p}


class TestNothingDeliversAPersonsCredentialsToAServer(unittest.TestCase):
    def test_no_playbook_reads_a_github_or_bugzilla_login_from_the_store_for_a_server(self):
        read = {name for text in server_playbooks().values() for name in re.findall(r"wk_store ~ '/([\w/-]+)'", text)}
        self.assertEqual(read - AGENT_MAY_HOLD, set())

    def test_no_playbook_writes_a_push_key(self):
        for path, text in server_playbooks().items():
            self.assertNotRegex(text, r"dest: .*/deploy/", path)

    def test_env_sh_which_an_agent_sources_holds_no_secret(self):
        with open(os.path.join(ROOT, "ansible", "roles", "credentials", "templates", "env.sh.j2")) as f:
            text = f.read()
        self.assertNotIn("lookup(", text)
        self.assertIn("credentials.sh", text)


class TestAnAgentStartsWithNone(unittest.TestCase):
    def setUp(self):
        self.revoked = []
        patch = mock.patch.object(rotation, "revoke", lambda inv, server: self.revoked.append(server))
        patch.start()
        self.addCleanup(patch.stop)
        self.w = World(here="lin")
        self.w.workspace("x")
        self.w.machines["lin"].files.update({HOME + "/secrets/credentials.sh": "export GH_TOKEN=t\n",
                                              HOME + "/secrets/deploy/o--r": "key", session.pi_bin(HOME): "pi"})

    def test_a_persons_credentials_are_removed_and_the_keys_revoked_before_the_agent_starts_marked(self):
        r = self.w.wk("ai", "pi", "x", tty=True)
        self.assertEqual(r.effects[:3], ["[lin] remove %s/secrets/credentials.sh" % HOME, "[lin] remove %s/secrets/deploy/o--r" % HOME,
                                         "[vault] POST /revoke"])
        self.assertIn("WK_AGENT=pi", r.effects[3])
        self.assertEqual(self.revoked, ["lin"])
        self.assertNotIn(HOME + "/secrets/credentials.sh", self.w.machines["lin"].files)

    def test_an_agents_shell_asks_for_none(self):
        del self.w.machines["lin"].files[HOME + "/secrets/credentials.sh"]
        r = self.w.wk("enter", "x", "--", "git", "push", env={"WK_AGENT": "claude"})
        self.assertNotIn("[vault] POST /credentials", r.effects)


class TestTheCheckProvesIt(unittest.TestCase):
    def test_wk_machine_check_refuses_an_agents_push_and_makes_a_persons(self):
        with open(os.path.join(ROOT, "ansible", "check.yml")) as f:
            text = f.read()
        self.assertIn("- name: An agent's shell cannot push", text)
        self.assertIn("- name: A person's shell pushes to the fork, as a dry run", text)


class TestTheDesignSaysIt(unittest.TestCase):
    def test_design_md_states_the_rule(self):
        with open(os.path.join(ROOT, "DESIGN.md")) as f:
            self.assertIn("Agents neither push nor change anything on GitHub", f.read())
