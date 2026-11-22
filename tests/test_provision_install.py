"""`wk machine install` is a front to Ansible too: its refusals, the one question, and the `ansible-playbook` line that
runs ansible/install.yml. The playbook itself is parsed by a real Ansible where there is one, and its autoinstall
template rendered by it; neither reaches a machine."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests import REAL_PATH, ROOT
from tests.provision import checkout, client
from tests.world import World
from wk.core import conf
from wk.core.machine import Result
from wk.core.vars import VARS

BMC = "bmc=10.0.0.2\narch=arm64\nos=24.04\nmedia_host=bridge\nmedia_user=root\nmedia_address=10.0.0.1"
CONFS = {"desk": "kind=client", "lin": "kind=linux\nuser=jm\nwifi=wlp1s0\nmedia_chain=inet/oob/input\n" + BMC, "bare": "kind=linux",
         "mac": "kind=mac\n" + BMC, "box": "kind=borrowed\n" + BMC, "vault": "kind=credential\n" + BMC}
PLAYBOOK = ["ansible-playbook", "-i", ROOT + "/ansible/inventory.py", "--limit", "lin"]
INSTALL = [ROOT + "/ansible/install.yml"]
ASKED = "ERASE lin's disk and install Ubuntu on it, through its BMC at 10.0.0.2? [y/N]"
NEXT = "==> lin is installed and on the tailnet; converge it next: wk machine setup lin\n"
HELD = ("error: lin holds x, y, and its disk is erased: remove each with `wk rm <ws>`\n    --force crosses this\n")


def make():
    w = World(confs=CONFS)
    checkout(w.machines["desk"])
    client(w.machines["desk"])
    w.machines["desk"].on(("ansible-playbook", "-i"), "")
    return w


def played(run):
    return [line[len("[desk] "):].split() for line in run.calls if line.startswith("[desk] ansible-playbook -i")]


class TestMachineInstall(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_it_is_one_ansible_playbook_of_install_yml_limited_to_the_server_then_names_setup(self):
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, played(r), r.effects), (0, [PLAYBOOK + INSTALL], ["[desk] " + " ".join(PLAYBOOK + INSTALL)]))
        self.assertTrue(r.err.endswith(NEXT), r.err)

    def test_its_exit_code_is_ansibles_and_a_failed_install_names_no_next_step(self):
        self.w.machines["desk"].on(("ansible-playbook", "-i"), Result(2))
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, NEXT in r.err), (2, False))

    def test_it_asks_once_naming_the_machine_and_its_erased_disk(self):
        yes = self.w.wk("machine", "install", "lin", stdin="y\n", tty=True)
        self.assertEqual((yes.code, len(played(yes)), yes.err.count(ASKED)), (0, 1, 1))
        no = self.w.wk("machine", "install", "lin", stdin="\n", tty=True)
        self.assertEqual((no.code, no.effects), (1, []))
        self.assertIn("error: declined: nothing is changed\n", no.err)
        unasked = self.w.wk("machine", "install", "lin")
        self.assertEqual((unasked.code, unasked.effects), (4, []))

    def test_a_dry_run_is_ansibles_check_mode_and_is_run(self):
        r = self.w.wk("machine", "install", "lin", "--dry-run")
        self.assertEqual((r.code, played(r), r.effects), (0, [PLAYBOOK + ["--check", "--diff"] + INSTALL], []))
        self.assertIn("would ask: " + ASKED, r.err)
        self.assertNotIn(NEXT, r.err)

    def test_a_rehearsal_skips_what_is_tagged_wipe_and_asks_a_question_of_its_own(self):
        r = self.w.wk("machine", "install", "lin", "--rehearse", stdin="y\n", tty=True)
        self.assertEqual((r.code, played(r)), (0, [PLAYBOOK + ["--skip-tags", "wipe"] + INSTALL]))
        self.assertIn("insert the install image into lin's BMC at 10.0.0.2, in place of an image an earlier run left? nothing boots [y/N]", r.err)
        self.assertNotIn(NEXT, r.err)

    def test_a_server_that_holds_a_workspace_is_a_barrier_that_force_crosses(self):
        self.w.workspace("x")
        self.w.workspace("y")
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, r.err, r.effects), (1, HELD, []))
        forced = self.w.wk("machine", "install", "lin", "--yes", "--force")
        self.assertEqual((forced.code, played(forced)), (0, [PLAYBOOK + INSTALL]))
        self.assertIn("warning: this command was forced past: lin holds x, y", forced.err)

    def test_a_server_that_does_not_answer_is_installed_and_no_other_server_is_asked(self):
        self.w.machines["lin"].down = self.w.machines["bare"].down = True
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, played(r)), (0, [PLAYBOOK + INSTALL]))
        self.assertIn("==> lin does not answer, so what it holds is unknown: a machine being reinstalled usually does not\n", r.err)
        self.assertEqual([line for line in r.calls if not line.startswith("[desk]")], [])

    def test_a_machine_with_no_bmc_is_refused_naming_the_key(self):
        r = self.w.wk("machine", "install", "bare", "--yes", "--force")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertEqual(r.err, "error: bare has no BMC to be installed through: set bmc=<its BMC's address> in machines/bare.conf "
                                "(`wk machine install -h` lists the other keys)\n")

    def test_the_client_a_borrowed_box_and_a_mac_are_refused_whatever_is_forced(self):
        for name, what in (("desk", "the client"), ("box", "a borrowed box"), ("mac", "a Mac")):
            r = self.w.wk("machine", "install", name, "--yes", "--force")
            self.assertEqual((r.code, r.err, r.calls), (1, "error: %s is %s, and install puts Ubuntu on a Linux server of yours\n" % (name, what), []))

    def test_the_credential_server_is_a_barrier_that_force_crosses(self):
        r = self.w.wk("machine", "install", "vault", "--yes")
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertEqual(r.err, "error: vault is the credential server: its disk holds the store and the claude.ai login, and both go with it\n"
                                "    --force crosses this\n")
        forced = self.w.wk("machine", "install", "vault", "--yes", "--force")
        self.assertEqual((forced.code, played(forced)), (0, [PLAYBOOK[:3] + ["--limit", "vault"] + INSTALL]))

    def test_it_is_typed_on_the_client_and_an_unknown_machine_is_refused(self):
        self.assertIn("error: wk machine install runs on the client", self.w.wk("machine", "install", "lin", "--yes", on="lin").err)
        self.assertIn("error: no machine named nope", self.w.wk("machine", "install", "nope", "--yes").err)

    def test_a_client_without_ansible_is_refused_after_the_question(self):
        self.w.machines["desk"].on(("ansible-playbook", "--version"), Result(127, "", "not found\n"))
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, r.effects), (4, []))

    def test_the_confs_install_keys_are_the_hosts_variables(self):
        hostvars = json.loads(self.w.wk("machine", "inventory").out)["_meta"]["hostvars"]
        self.assertEqual(hostvars["lin"], {
            "wk_kind": "linux", "wk_user": "jm", "ansible_user": "jm", "wk_wifi": "wlp1s0", "wk_media_chain": "inet/oob/input",
            "wk_bmc": "10.0.0.2", "wk_arch": "arm64", "wk_os": "24.04", "wk_media_host": "bridge", "wk_media_user": "root",
            "wk_media_address": "10.0.0.1"})


def tasks():
    """The text of ansible/install.yml and of every file under ansible/install/."""
    text = {}
    for directory, name in [(ROOT + "/ansible", "install.yml")] + [(ROOT + "/ansible/install", n) for n in sorted(os.listdir(ROOT + "/ansible/install"))]:
        with open(os.path.join(directory, name)) as f:
            text[name] = f.read()
    return text


class TestThePlaybook(unittest.TestCase):
    def test_every_pinned_image_has_a_url_of_its_release_and_a_digest(self):
        for release, pins in VARS["wk_ubuntu"].items():
            self.assertRegex(pins["codename"], "^[a-z]+$")
            for arch in ("arm64", "amd64"):
                self.assertRegex(pins[arch]["url"], r"^https://(cdimage|releases)\.ubuntu\.com/.*/ubuntu-%s(\.\d+)?-live-server-%s\.iso$"
                                 % (re.escape(release), arch))
                self.assertRegex(pins[arch]["sha256"], "^[0-9a-f]{64}$")

    def test_what_boots_erases_or_retires_is_tagged_wipe_and_what_a_rehearsal_runs_is_not(self):
        boot = tasks()["boot.yml"]
        at = boot.index("- name: Erase the disk and install\n  tags: [wipe]\n")
        for word in ("SetOneTimeBoot", "PowerForceRestart", "PowerOn", "method: DELETE", "known_hosts", "wait_for_connection"):
            self.assertNotIn(word, boot[:at], word)
            self.assertIn(word, boot[at:], word)
        self.assertIn("VirtualMediaInsert", boot[:at])
        self.assertRegex(tasks()["install.yml"], r"command: DisableBootOverride\n +delegate_to: localhost\n +tags: \[wipe\]\n")

    def test_the_image_is_ejected_and_every_copy_removed_however_the_run_ends(self):
        play = tasks()["install.yml"]
        cleanup = play[play.index("      always:\n"):]
        self.assertEqual(re.findall(r"import_tasks: install/(\S+)", cleanup), ["eject.yml", "unserve.yml"])
        unserve = tasks()["unserve.yml"]
        self.assertEqual(re.findall(r'path: "{{ (\w+) }}"\n +state: absent', unserve), ["wk_install_build", "wk_install_staged"])
        self.assertIn("import_tasks: close.yml", unserve)

    def test_a_task_that_carries_a_secret_logs_nothing(self):
        for name, text in tasks().items():
            for task in re.split(r"\n(?= *- name: )", text):
                if re.search(r"lookup\('ansible.builtin.pipe'|Authorization|autoinstall\.yaml\.j2", task):
                    self.assertIn("no_log: true", task, "%s: %s" % (name, task.strip().splitlines()[0]))

    def test_no_secret_is_written_in_the_playbook(self):
        for name, text in tasks().items():
            self.assertIsNone(re.search(r"tskey-|password: [^{\s]", text.replace('password: "{{', "password: {{")), name)

    def test_the_real_confs_parse_and_name_only_pinned_releases(self):
        for machine in conf.load(os.path.join(ROOT, "machines")).values():
            if machine.get("bmc"):
                self.assertIn(machine.get("arch"), VARS["wk_ubuntu"][machine.get("os")], machine.name)


VARIABLES = dict(VARS, wk_install_name="lin", wk_install_user="jm", wk_install_codename="noble", wk_install_public_key="ssh-ed25519 AAAA me@desk",
                 wk_install_tailscale_key="-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQIN\n-----END PGP PUBLIC KEY BLOCK-----\n",
                 wk_install_secret={"ssid": 'Café "5G": #1', "psk": "pass: 'word' \\ {x}", "auth_key": "tskey-auth-k1"})
RENDER = "content={{ lookup('ansible.builtin.template', src) | from_yaml | to_json }} dest={{ dest }}"


class TestLive(unittest.TestCase):
    """A real Ansible, on this machine alone: it parses the playbook and renders the template. Skipped where there is none."""

    def setUp(self):
        self.ansible = shutil.which("ansible", path=REAL_PATH)
        if not (self.ansible and shutil.which("ansible-playbook", path=REAL_PATH)):
            self.skipTest("live: ansible is not installed on this machine")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def run_ansible(self, argv):
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.tmp,
                           env=dict(os.environ, PATH=REAL_PATH, ANSIBLE_LOCAL_TEMP=self.tmp + "/local", ANSIBLE_REMOTE_TEMP=self.tmp + "/remote"))
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def render(self, **more):
        """The autoinstall file as Ansible renders it and as Ansible's own YAML reads it back."""
        with open(self.tmp + "/vars.json", "w") as f:
            json.dump(dict(VARIABLES, src=ROOT + "/ansible/install/autoinstall.yaml.j2", dest=self.tmp + "/out.json", **more), f)
        self.run_ansible([self.ansible, "localhost", "-i", "localhost,", "-c", "local", "-m", "ansible.builtin.copy", "-a", RENDER,
                          "-e", "@" + self.tmp + "/vars.json", "-e", "ansible_python_interpreter={{ ansible_playbook_python }}"])
        with open(self.tmp + "/out.json") as f:
            return json.load(f)["autoinstall"]

    def test_live_ansible_accepts_the_playbooks_syntax(self):
        with open(self.tmp + "/inventory.json", "w") as f:
            json.dump({"all": {"hosts": {"lin": {}}}}, f)
        self.run_ansible([shutil.which("ansible-playbook", path=REAL_PATH), "-i", self.tmp + "/inventory.json", "--syntax-check"] + INSTALL)

    def test_live_the_autoinstall_file_installs_unasked_onto_the_whole_largest_disk_and_powers_off(self):
        auto = self.render()
        self.assertEqual((auto["version"], auto["interactive-sections"], auto["shutdown"]), (1, [], "poweroff"))
        self.assertEqual(auto["storage"], {"layout": {"name": "lvm", "sizing-policy": "all", "match": {"size": "largest"}}})
        self.assertEqual(auto["ssh"], {"install-server": True, "allow-pw": False})
        self.assertEqual((auto["locale"], auto["keyboard"], auto["timezone"], auto["apt"]),
                         ("en_US.UTF-8", {"layout": "us"}, "Etc/UTC", {"fallback": "offline-install"}))
        self.assertNotIn("identity", auto)

    def test_live_a_server_without_wifi_gets_wired_dhcp_alone(self):
        auto = self.render()
        self.assertEqual(auto["network"], {"version": 2, "ethernets": {"wired": {"match": {"name": "e*"}, "dhcp4": True, "optional": True}}})
        self.assertNotIn("packages", auto)

    def test_live_a_server_with_wifi_joins_the_stores_network_on_the_confs_interface(self):
        auto = self.render(wk_wifi="wlp1s0")
        self.assertEqual(auto["network"]["wifis"], {"wlp1s0": {"dhcp4": True, "access-points": {'Café "5G": #1': {"password": "pass: 'word' \\ {x}"}}}})
        self.assertEqual(auto["packages"], ["wpasupplicant"])

    def test_live_the_first_boot_makes_the_user_installs_the_packages_and_joins_the_tailnet(self):
        first = self.render()["user-data"]
        self.assertEqual((first["hostname"], first["ssh_pwauth"], first["package_update"]), ("lin", False, True))
        self.assertEqual(first["users"], [{"name": "jm", "gecos": "jm", "shell": "/bin/bash", "groups": ["adm", "sudo"], "sudo": "ALL=(ALL) NOPASSWD:ALL",
                                           "lock_passwd": True, "ssh_authorized_keys": ["ssh-ed25519 AAAA me@desk"]}])
        self.assertEqual(first["apt"]["sources"]["tailscale.list"], {
            "source": "deb [signed-by=$KEY_FILE] https://pkgs.tailscale.com/stable/ubuntu noble main", "key": VARIABLES["wk_install_tailscale_key"]})
        self.assertLessEqual({"git", "podman", "uidmap", "bubblewrap", "python3", "curl", "openssl", "tar", "tailscale"}, set(first["packages"]))
        self.assertEqual(first["write_files"], [{"path": "/etc/wk-tailnet-auth-key", "permissions": "0600", "content": "tskey-auth-k1"}])
        self.assertEqual(first["runcmd"], [
            ["loginctl", "enable-linger", "jm"],
            ["sh", "-c", "grep -q '^jm:' /etc/subuid /etc/subgid || usermod --add-subuids 100000-165535 --add-subgids 100000-165535 jm"],
            ["tailscale", "up", "--auth-key=file:/etc/wk-tailnet-auth-key", "--ssh", "--hostname=lin", "--advertise-tags=tag:wk"],
            ["rm", "-f", "/etc/wk-tailnet-auth-key"]])
