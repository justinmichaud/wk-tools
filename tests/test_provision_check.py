"""`wk machine check` is a front to Ansible: its refusals and the `ansible-playbook` line that runs ansible/check.yml.
The playbook is read as text here, and parsed by a real Ansible where there is one; it reaches no machine."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests import REAL_PATH, ROOT
from tests.provision import checkout, controller
from tests.world import World
from wk.core.machine import Result

CONFS = {"desk": "kind=workstation", "lin": "kind=linux\nuser=jm", "mac": "kind=mac", "vault": "kind=controller"}
CHECK = [ROOT + "/ansible/check.yml"]


def make():
    w = World(here="vault", confs=CONFS)
    checkout(w.machines["vault"])
    controller(w.machines["vault"])
    w.machines["vault"].on(("ansible-playbook", "-i"), "")
    return w


def played(run):
    return [line[len("[vault] "):].split() for line in run.calls if line.startswith("[vault] ansible-playbook -i")]


def playbook():
    with open(CHECK[0]) as f:
        return f.read()


class TestMachineCheck(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_it_is_one_ansible_playbook_of_check_yml_limited_to_the_server_whose_exit_code_it_returns(self):
        line = ["ansible-playbook", "-i", ROOT + "/ansible/inventory.py", "--limit", "mac"] + CHECK
        r = self.w.wk("machine", "check", "mac")
        self.assertEqual((r.code, played(r), r.effects), (0, [line], ["[vault] " + " ".join(line)]))
        self.w.machines["vault"].on(("ansible-playbook", "-i"), Result(2))
        self.assertEqual(self.w.wk("machine", "check", "lin").code, 2)

    def test_a_dry_run_is_ansibles_check_mode_with_diffs_and_is_run(self):
        r = self.w.wk("machine", "check", "lin", "--dry-run")
        self.assertEqual((r.code, played(r), r.effects),
                         (0, [["ansible-playbook", "-i", ROOT + "/ansible/inventory.py", "--limit", "lin", "--check", "--diff"] + CHECK], []))

    def test_only_a_server_is_checked(self):
        for name, said in (("vault", "vault holds no workspace: it is the controller"), ("desk", "desk holds no workspace: it is a workstation"),
                           ("nope", "no machine named nope: machines/ has desk, lin, mac, vault")):
            r = self.w.wk("machine", "check", name)
            self.assertEqual((r.code, r.calls), (1, []))
            self.assertIn("error: " + said, r.err)

    def test_it_is_typed_on_the_controller(self):
        r = self.w.wk("machine", "check", "lin", on="lin")
        self.assertIn("error: wk machine check runs on the controller (vault), and this is lin", r.err)

    def test_a_controller_without_ansible_is_refused_naming_how_to_install_it(self):
        self.w.machines["vault"].on(("ansible-playbook", "--version"), Result(127, "", "not found\n"))
        r = self.w.wk("machine", "check", "lin")
        self.assertEqual((r.code, r.effects), (4, []))
        self.assertIn("sudo apt install ansible", r.err)


class TestThePlaybook(unittest.TestCase):
    def test_each_step_is_wk_typed_in_the_logins_login_shell(self):
        steps = re.findall(r'argv: "\{\{ wk_check_wk \+ \[\'(\w+)\'', playbook())
        self.assertEqual(steps, ["new", "run", "enter", "test", "rm"])
        self.assertEqual(re.findall(r'argv: "\{\{ (wk_check_\w+)', playbook()), ["wk_check_wk", "wk_check_wk", "wk_check_take", "wk_check_agent",
                                                                               "wk_check_wk", "wk_check_take", "wk_check_wk", "wk_check_wk"])

    def test_an_agents_shell_is_refused_a_push_and_a_persons_is_not(self):
        text = playbook()
        self.assertIn("""wk_check_take: "{{ wk_check_wk + ['ai', 'claude', wk_check_ws, '--', '--version'] }}\"""", text)
        self.assertIn("""wk_check_agent: ["{{ ansible_facts['user_shell'] }}", -lc, 'WK_AGENT=check exec wk "$@"', wk]""", text)
        agent, person = text.index("- name: An agent's shell cannot push"), text.index("- name: A person's shell pushes")
        self.assertLess(text.index("- name: Start an agent session"), agent)
        self.assertLess(agent, person)
        self.assertIn("failed_when: wk_check_agent_push.rc == 0", text[agent:person])
        self.assertLess(person, text.index("- name: Take the person's credentials away again"))
        self.assertIn("""wk_check_wk: ["{{ ansible_facts['user_shell'] }}", -lc, 'exec wk "$@"', wk]""", playbook())

    def test_the_presets_are_built_in_turn_and_the_first_that_fails_ends_the_check(self):
        self.assertIn("""argv: "{{ [ansible_facts['user_shell'], '-lc', 'for p in \\"$@\\"; do wk build \\"$0\\" \\"$p\\" || exit; done', wk_check_ws] """
                      """+ wk_check_presets }}\"""", playbook())

    def test_jsc_release_is_checked_everywhere_and_mac_release_on_a_mac(self):
        self.assertIn("""wk_check_presets: "{{ ['jsc-release', 'mac-release'] if 'mac' in group_names else ['jsc-release'] }}\"""", playbook())

    def test_jsc_prints_the_lines_answer_and_one_stress_test_runs(self):
        text = playbook()
        self.assertIn("'--', '-e', 'print(1+1)'] }}\"\n", text)
        self.assertIn("failed_when: wk_check_run.rc != 0 or wk_check_run.stdout_lines != ['2']", text)
        self.assertIn("'--', 'stress/array-push.js'] }}\"\n", text)

    def test_the_workspace_is_named_once_and_removed_however_the_run_ends(self):
        text = playbook()
        self.assertEqual(text.count("now("), 1)
        self.assertRegex(text, r"(?s)\n      always:\n        - name: Remove the workspace\n          ansible\.builtin\.command:\n"
                               r"            argv: \"\{\{ wk_check_wk \+ \['rm', wk_check_ws, '--yes'\] \}\}\"\n$")


class TestLive(unittest.TestCase):
    def test_live_ansible_accepts_the_playbooks_syntax(self):
        ansible = shutil.which("ansible-playbook", path=REAL_PATH)
        if not ansible:
            self.skipTest("live: ansible-playbook is not installed on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "inventory.json"), "w") as f:
                json.dump({"linux": {"hosts": {"lin": {}}}, "mac": {"hosts": {"mac": {}}}}, f)
            r = subprocess.run([ansible, "-i", os.path.join(tmp, "inventory.json"), "--syntax-check"] + CHECK,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=tmp, env=dict(os.environ, PATH=REAL_PATH))
        self.assertEqual(r.returncode, 0, r.stdout.decode())
