"""`wk machine setup` is a front to Ansible: the inventory made of the confs, and the one command line."""

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
from tests.provision import SHA, checkout, controller
from tests.world import World
from wk.core.conf import NAME
from wk.core.machine import Result
from wk.creds import injector
from wk.provision.ansible import LOCALE
from wk.webkit import PROJECT

CONFS = {"desk": "kind=workstation", "lin": "kind=linux\nuser=jm", "mac": "kind=mac\ncmake=/opt/cmake",
         "vault": "kind=controller\ninjector_port=3129\nlitellm_url=https://llm.example/v1"}
PLAYBOOK = ["ansible-playbook", "-i", ROOT + "/ansible/inventory.py"]
SITE = [ROOT + "/ansible/site.yml"]
ROLES = ["agents", "controller", "credentials", "mirror", "pueue", "sdk", "wk"]
INSTALL = "    sudo apt install ansible\n"


def make(**how):
    w = World(here="vault", confs=CONFS)
    checkout(w.machines["vault"], **how)
    controller(w.machines["vault"])
    w.machines["vault"].on(PLAYBOOK, "")
    return w


def played(run):
    """The ansible-playbook lines a command ran, as argvs: the question whether it is installed, then the play."""
    return [line[len("[vault] "):].split() for line in run.calls if line.startswith("[vault] ansible-playbook")]


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.inventory = json.loads(make().wk("machine", "inventory").out)

    def test_there_is_a_group_per_kind_and_a_workstation_is_no_host(self):
        self.assertEqual({name: group["hosts"] for name, group in self.inventory.items() if "hosts" in group},
                         {"linux": ["lin"], "mac": ["mac"], "controller": ["vault"]})

    def test_a_machines_kind_and_literals_are_its_variables_its_user_is_ansibles_login_and_the_controller_is_local(self):
        self.assertEqual(self.inventory["_meta"]["hostvars"], {
            "lin": {"wk_kind": "linux", "wk_user": "jm", "ansible_user": "jm"},
            "mac": {"wk_kind": "mac", "wk_cmake": "/opt/cmake"},
            "vault": {"wk_kind": "controller", "wk_injector_port": "3129", "wk_litellm_url": "https://llm.example/v1", "ansible_connection": "local"}})

    def test_every_host_gets_the_controllers_literals_name_and_wk_home_the_commit_and_the_project(self):
        home = "/home/u/wk"
        self.assertEqual(self.inventory["all"]["vars"], {
            "wk_injector_port": "3129", "wk_litellm_url": "https://llm.example/v1",
            "wk_root": ROOT, "wk_commit": SHA, "wk_project": PROJECT, "wk_controller": "vault", "wk_controller_home": home,
            "wk_store": home + "/secrets", "wk_websocat": home + "/cache/websocat-1.14.0/websocat",
            "wk_claude_placeholder": json.loads(injector.placeholder()),
            "ansible_ssh_common_args": "-o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"})

    def test_the_controllers_wk_home_is_where_wk_runs_from(self):
        inventory = json.loads(make().wk("machine", "inventory", env={"WK_HOME": "/srv/wk"}).out)["all"]["vars"]
        self.assertEqual((inventory["wk_controller_home"], inventory["wk_store"]), ("/srv/wk", "/srv/wk/secrets"))

    def test_it_changes_nothing_and_is_typed_on_the_controller(self):
        w = make()
        self.assertEqual(w.wk("machine", "inventory").effects, [])
        self.assertIn("error: wk machine inventory runs on the controller (vault), and this is lin", w.wk("machine", "inventory", on="lin").err)


class TestMachineSetup(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_it_is_one_ansible_playbook_limited_to_the_server_whose_exit_code_it_returns(self):
        self.assertEqual(played(self.w.wk("machine", "setup", "vault"))[1], PLAYBOOK + ["--limit", "vault"] + SITE)
        r = self.w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, played(r)), (0, [["ansible-playbook", "--version"], PLAYBOOK + ["--limit", "lin"] + SITE]))
        self.assertEqual(r.effects, ["[vault] " + " ".join(PLAYBOOK + ["--limit", "lin"] + SITE)])
        self.w.machines["vault"].on(PLAYBOOK, Result(2))
        self.assertEqual(self.w.wk("machine", "setup", "lin").code, 2)

    def test_stage_runs_the_role_of_that_name_alone(self):
        r = self.w.wk("machine", "setup", "mac", "--stage", "pueue")
        self.assertEqual(played(r)[1], PLAYBOOK + ["--limit", "mac", "--tags", "pueue"] + SITE)

    def test_the_stages_are_the_roles_and_an_unknown_one_is_refused_naming_them(self):
        r = self.w.wk("machine", "setup", "lin", "--stage", "fourth")
        self.assertEqual((r.code, r.err, r.calls), (1, "error: no stage named fourth: the stages are %s\n" % ", ".join(ROLES), []))

    def test_a_dry_run_is_ansibles_check_mode_with_diffs_and_is_run(self):
        r = self.w.wk("machine", "setup", "lin", "--dry-run")
        self.assertEqual((r.code, played(r)[1], r.effects, r.plan), (0, PLAYBOOK + ["--limit", "lin", "--check", "--diff"] + SITE, [], []))

    def test_every_ansible_playbook_runs_in_a_locale_the_controller_has(self):
        self.w.wk("machine", "setup", "lin")
        self.assertEqual([call.env for call in self.w.machines["vault"].calls if call.argv[0] == "ansible-playbook"], [LOCALE] * 2)
        self.assertEqual(LOCALE, {"LC_ALL": "C.UTF-8"})

    def test_a_controller_without_ansible_is_refused_naming_how_to_install_it(self):
        self.w.machines["vault"].on(("ansible-playbook", "--version"), Result(127, "", "sh: ansible-playbook: command not found\n"))
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

    def test_a_workstation_and_an_unknown_machine_are_refused(self):
        self.assertEqual(self.w.wk("machine", "setup", "desk").err,
                         "error: desk is a workstation, and setup converges a server or the controller: a workstation needs Zed, ssh and wk\n")
        self.assertEqual(self.w.wk("machine", "setup", "nope").err, "error: no machine named nope: machines/ has desk, lin, mac, vault\n")


class TestThePlaybook(unittest.TestCase):
    def test_the_roles_on_disk_are_the_stages_and_site_yml_tags_each_with_its_own_name(self):
        with open(os.path.join(ROOT, "ansible", "site.yml")) as f:
            tagged = re.findall(r"import_role:\n +name: (\S+)\n(?: +when: .*\n)? +tags: \[(\S+)\]\n", f.read())
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, "ansible", "roles"))), ROLES)
        self.assertEqual(sorted(tagged), [(role, role) for role in ROLES])

    def test_the_shared_variables_are_one_json_file_that_pins_every_download(self):
        with open(os.path.join(ROOT, "ansible", "group_vars", "all.json")) as f:
            shared = json.load(f)
        self.assertEqual((shared["wk_pueue_version"], shared["wk_websocat_version"], shared["wk_claude_version"]), ("4.0.5", "1.14.0", "2.1.308"))
        digests = [d for assets in (shared["wk_pueue_assets"], shared["wk_websocat_assets"]) for asset in assets.values()
                   for name, d in asset.items() if name != "target"]
        self.assertEqual((len(digests), [d for d in digests if not re.fullmatch("[0-9a-f]{64}", d)]), (7, []))

    def test_the_forks_whose_push_keys_rotate_are_named_once(self):
        with open(os.path.join(ROOT, "ansible", "group_vars", "all.json")) as f:
            forks = json.load(f)["wk_forks"]
        self.assertEqual(forks, ["justinmichaud/WebKit", "justinmichaud/WPEWebKit", "justinmichaud/wk-tools"])
        self.assertIn("https://github.com/%s.git" % forks[0], PROJECT["fork"])

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

    def test_no_registered_result_or_fact_hides_a_variable_of_the_inventory(self):
        """A registered result outranks an inventory variable of its name for the rest of the play."""
        inventory = json.loads(make().wk("machine", "inventory").out)
        given = set(inventory["all"]["vars"]) | {k for hostvars in inventory["_meta"]["hostvars"].values() for k in hostvars}
        hidden = []
        for directory, _, names in os.walk(os.path.join(ROOT, "ansible")):
            for name in [n for n in names if n.endswith(".yml")]:
                with open(os.path.join(directory, name)) as f:
                    hidden += ["%s: %s" % (name, n) for n in re.findall(r"^ *register: (\w+)$", f.read(), re.M) if n in given]
        self.assertEqual(hidden, [])


class TestTheInventoryScript(unittest.TestCase):
    """ansible/inventory.py as Ansible runs it: a real process, a machines directory of the test's, the real git."""

    def setUp(self):
        self.host = socket.gethostname().split(".")[0].lower()
        if not NAME.fullmatch(self.host):
            self.skipTest("this machine's hostname, %s, cannot name a conf" % self.host)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.machines = tmp.name
        for name, text in (("srv-a", "kind=linux"), ("srv-b", "kind=mac"), (self.host, "kind=controller")):
            with open(os.path.join(self.machines, name + ".conf"), "w") as f:
                f.write(text + "\n")

    def test_it_prints_wk_machine_inventory_for_ansibles_list(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "ansible", "inventory.py"), "--list"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=dict(os.environ, WK_MACHINES_DIR=self.machines))
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        inventory = json.loads(r.stdout)
        self.assertEqual((inventory["controller"], inventory["linux"], inventory["mac"], sorted(inventory["_meta"]["hostvars"])),
                         ({"hosts": [self.host]}, {"hosts": ["srv-a"]}, {"hosts": ["srv-b"]}, sorted([self.host, "srv-a", "srv-b"])))
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
