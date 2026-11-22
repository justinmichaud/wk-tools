"""`wk machine setup` and `wk key sync` are fronts to Ansible: the inventory made of the confs, and the one command line."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

from tests import REAL_PATH, ROOT
from tests.provision import SHA, checkout, client
from tests.world import World
from wk.core.conf import NAME
from wk.core.machine import Result
from wk.creds import injector
from wk.webkit import PROJECT

CONFS = {"desk": "kind=client\nlitellm_url=https://llm.example/v1\ndefault_server=lin", "lin": "kind=linux\nuser=jm",
         "mac": "kind=mac\ncmake=/opt/cmake", "box": "kind=borrowed", "vault": "kind=credential\ninjector_port=3129"}
PLAYBOOK = ["ansible-playbook", "-i", ROOT + "/ansible/inventory.py"]
SITE = [ROOT + "/ansible/site.yml"]
ROLES = ["agents", "borrowed-claude", "credentials", "injector", "mirror", "openbao", "pueue", "sdk", "wk"]
INSTALL = "    brew install ansible, or your distribution's ansible package\n"


def make(**how):
    w = World(confs=CONFS)
    checkout(w.machines["desk"], **how)
    client(w.machines["desk"])
    w.machines["desk"].on(PLAYBOOK, "")
    return w


def played(run):
    """The ansible-playbook lines a command ran, as argvs: the question whether it is installed, then the play."""
    return [line[len("[desk] "):].split() for line in run.calls if line.startswith("[desk] ansible-playbook")]


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.inventory = json.loads(make().wk("machine", "inventory").out)

    def test_there_is_a_group_per_kind_and_the_client_is_no_host(self):
        self.assertEqual({name: group["hosts"] for name, group in self.inventory.items() if "hosts" in group},
                         {"linux": ["lin"], "mac": ["mac"], "borrowed": ["box"], "credential": ["vault"]})

    def test_a_machines_kind_and_literals_are_its_variables_and_its_user_is_ansibles_login(self):
        self.assertEqual(self.inventory["_meta"]["hostvars"], {
            "lin": {"wk_kind": "linux", "wk_user": "jm", "ansible_user": "jm"},
            "mac": {"wk_kind": "mac", "wk_cmake": "/opt/cmake"},
            "box": {"wk_kind": "borrowed"},
            "vault": {"wk_kind": "credential", "wk_injector_port": "3129"}})

    def test_every_host_gets_the_clients_literals_the_commit_the_store_and_the_project(self):
        self.assertEqual(self.inventory["all"]["vars"], {
            "wk_kind": "client", "wk_litellm_url": "https://llm.example/v1", "wk_default_server": "lin",
            "wk_root": ROOT, "wk_commit": SHA, "wk_project": PROJECT, "wk_credential_server": "vault", "wk_bao_addr": "http://vault:8200",
            "wk_claude_placeholder": json.loads(injector.placeholder()),
            "ansible_ssh_common_args": "-o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"})

    def test_it_changes_nothing_and_is_typed_on_the_client(self):
        w = make()
        self.assertEqual(w.wk("machine", "inventory").effects, [])
        self.assertIn("error: wk machine inventory runs on the client", w.wk("machine", "inventory", on="lin").err)

    def test_a_fleet_with_no_credential_server_is_refused_naming_the_remedy(self):
        w = World(confs={"desk": "kind=client", "lin": "kind=linux"})
        checkout(w.machines["desk"])
        self.assertEqual(w.wk("machine", "inventory").err, "error: no credential server: set kind=credential in one machines/<name>.conf\n")


class TestMachineSetup(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_it_is_one_ansible_playbook_limited_to_the_server_whose_exit_code_it_returns(self):
        self.assertEqual(played(self.w.wk("machine", "setup", "vault"))[1], PLAYBOOK + ["--limit", "vault"] + SITE)
        r = self.w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, played(r)), (0, [["ansible-playbook", "--version"], PLAYBOOK + ["--limit", "lin"] + SITE]))
        self.assertEqual(r.effects, ["[desk] " + " ".join(PLAYBOOK + ["--limit", "lin"] + SITE)])
        self.w.machines["desk"].on(PLAYBOOK, Result(2))
        self.assertEqual(self.w.wk("machine", "setup", "lin").code, 2)

    def test_stage_runs_the_role_of_that_name_alone(self):
        r = self.w.wk("machine", "setup", "box", "--stage", "pueue")
        self.assertEqual(played(r)[1], PLAYBOOK + ["--limit", "box", "--tags", "pueue"] + SITE)

    def test_the_stages_are_the_roles_and_an_unknown_one_is_refused_naming_them(self):
        r = self.w.wk("machine", "setup", "lin", "--stage", "fourth")
        self.assertEqual((r.code, r.err, r.calls), (1, "error: no stage named fourth: the stages are %s\n" % ", ".join(ROLES), []))

    def test_a_dry_run_is_ansibles_check_mode_with_diffs_and_is_run(self):
        r = self.w.wk("machine", "setup", "lin", "--dry-run")
        self.assertEqual((r.code, played(r)[1], r.effects, r.plan), (0, PLAYBOOK + ["--limit", "lin", "--check", "--diff"] + SITE, [], []))

    def test_a_client_without_ansible_is_refused_naming_how_to_install_it(self):
        self.w.machines["desk"].on(("ansible-playbook", "--version"), Result(127, "", "sh: ansible-playbook: command not found\n"))
        r = self.w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, r.err, r.effects),
                         (4, "error: this machine has no ansible-playbook, which is what provisions a server\n" + INSTALL, []))

    def test_uncommitted_changes_are_a_barrier_that_force_crosses(self):
        w = make(status=" M wk\n")
        r = w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertEqual(r.err, "error: this checkout has uncommitted changes, and a machine is given a commit and nothing else\n"
                                "    commit or remove them: git -C %s status --short\n    --force crosses this\n" % ROOT)
        forced = w.wk("machine", "setup", "lin", "--force")
        self.assertEqual((forced.code, played(forced)[1]), (0, PLAYBOOK + ["--limit", "lin"] + SITE))
        self.assertIn("warning: this command was forced past: this checkout has uncommitted changes", forced.err)

    def test_a_commit_on_no_remote_branch_is_a_barrier_that_force_crosses(self):
        w = make(pushed="")
        r = w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertEqual(r.err, "error: commit 5e1f5e1f5e1f is on no remote branch, and a machine fetches its commit from "
                                "https://github.com/justinmichaud/wk-tools.git\n    git -C %s push\n    --force crosses this\n" % ROOT)
        self.assertEqual(w.wk("machine", "setup", "lin", "--force").code, 0)

    def test_a_checkout_with_no_commit_has_nothing_to_give(self):
        r = make(sha="").wk("machine", "setup", "lin", "--force")
        self.assertEqual((r.code, r.err, r.effects),
                         (1, "error: %s has no commit to give a machine: run wk from a clone of its repository\n" % ROOT, []))

    def test_the_client_and_an_unknown_machine_are_refused(self):
        self.assertEqual(self.w.wk("machine", "setup", "desk").err, "error: desk is the client, and setup converges a server: `wk doctor` reports what the client needs\n")
        self.assertEqual(self.w.wk("machine", "setup", "nope").err, "error: no machine named nope: machines/ has box, desk, lin, mac, vault\n")


class TestKeySync(unittest.TestCase):
    def setUp(self):
        self.w = make(status=" M wk\n")

    def test_it_is_the_credentials_role_on_every_server_or_on_the_one_named_whatever_the_checkouts_state(self):
        self.assertEqual(played(self.w.wk("key", "sync"))[1], PLAYBOOK + ["--tags", "credentials"] + SITE)
        self.assertEqual(played(self.w.wk("key", "sync", "mac"))[1], PLAYBOOK + ["--limit", "mac", "--tags", "credentials"] + SITE)

    def test_a_dry_run_is_check_mode(self):
        self.assertEqual(played(self.w.wk("key", "sync", "mac", "-n"))[1], PLAYBOOK + ["--limit", "mac", "--tags", "credentials", "--check", "--diff"] + SITE)

    def test_the_client_and_the_credential_server_are_delivered_nothing(self):
        for name in ("desk", "vault"):
            r = self.w.wk("key", "sync", name)
            self.assertEqual((r.code, r.calls), (1, []))


class TestThePlaybook(unittest.TestCase):
    def test_the_roles_on_disk_are_the_stages_and_site_yml_tags_each_with_its_own_name(self):
        with open(os.path.join(ROOT, "ansible", "site.yml")) as f:
            tagged = re.findall(r"import_role:\n +name: (\S+)\n(?: +when: .*\n)? +tags: \[(\S+)\]\n", f.read())
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, "ansible", "roles"))), ROLES)
        self.assertEqual(sorted(tagged), [(role, role) for role in ROLES])

    def test_the_shared_variables_are_one_json_file_that_pins_every_download(self):
        with open(os.path.join(ROOT, "ansible", "group_vars", "all.json")) as f:
            shared = json.load(f)
        self.assertEqual((shared["wk_pueue_version"], shared["wk_openbao_version"], shared["wk_claude_version"]), ("4.0.5", "2.7.3", "2.1.308"))
        digests = [d for assets in (shared["wk_pueue_assets"], shared["wk_openbao_assets"]) for asset in assets.values()
                   for name, d in asset.items() if name != "target"]
        self.assertEqual((len(digests), [d for d in digests if not re.fullmatch("[0-9a-f]{64}", d)]), (8, []))

    def test_every_module_is_named_in_full(self):
        """A module is a key of its task, beside the task's name; the same word deeper in is a module's own option."""
        short = []
        for directory, _, names in os.walk(os.path.join(ROOT, "ansible")):
            for name in [n for n in names if n.endswith(".yml")]:
                with open(os.path.join(directory, name)) as f:
                    for line in f:
                        task = re.match(r"( *)- ", line)
                        key = re.match(r"( *)(- )?(shell|command|copy|file|template|git|uri|stat|debug|assert|set_fact|get_url|lineinfile):", line)
                        if task:
                            beside = len(task.group(1)) + 2
                        if key and (key.group(2) or len(key.group(1)) == beside):
                            short.append("%s: %s" % (name, line.strip()))
        self.assertEqual(short, [])


class TestTheInventoryScript(unittest.TestCase):
    """ansible/inventory.py as Ansible runs it: a real process, a machines directory of the test's, the real git."""

    def setUp(self):
        self.host = socket.gethostname().split(".")[0].lower()
        if not NAME.fullmatch(self.host):
            self.skipTest("this machine's hostname, %s, cannot name a conf" % self.host)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.machines = tmp.name
        for name, text in (("srv-a", "kind=credential"), ("srv-b", "kind=borrowed"), (self.host, "kind=client")):
            with open(os.path.join(self.machines, name + ".conf"), "w") as f:
                f.write(text + "\n")

    def test_it_prints_wk_machine_inventory_for_ansibles_list(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "ansible", "inventory.py"), "--list"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=dict(os.environ, WK_MACHINES_DIR=self.machines))
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        inventory = json.loads(r.stdout)
        self.assertEqual((inventory["credential"], inventory["borrowed"], sorted(inventory["_meta"]["hostvars"])),
                         ({"hosts": ["srv-a"]}, {"hosts": ["srv-b"]}, ["srv-a", "srv-b"]))
        self.assertRegex(inventory["all"]["vars"]["wk_commit"], "^[0-9a-f]{40}$")

    def test_it_is_executable_as_ansibles_script_inventory_plugin_needs(self):
        self.assertTrue(os.access(os.path.join(ROOT, "ansible", "inventory.py"), os.X_OK))

    def test_live_ansible_accepts_the_playbooks_syntax(self):
        ansible = shutil.which("ansible-playbook", path=REAL_PATH)
        if not ansible:
            self.skipTest("live: ansible-playbook is not installed on this machine")
        r = subprocess.run([ansible, "-i", os.path.join(ROOT, "ansible", "inventory.py"), "--syntax-check", os.path.join(ROOT, "ansible", "site.yml")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ, PATH=REAL_PATH, WK_MACHINES_DIR=self.machines))
        self.assertEqual(r.returncode, 0, r.stdout.decode())
