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
from tests.credsworld import Http
from tests.provision import checkout, controller
from tests.world import World
from wk.core import conf
from wk.core.machine import Result
from wk.core.vars import VARS
from wk.creds import net

BMC = "bmc=10.0.0.2\narch=arm64\nos=24.04"
CONFS = {"desk": "kind=workstation", "lin": "kind=linux\nuser=jm\nwifi=wlp1s0\nconsole=ttyAMA0,115200\n" + BMC, "bare": "kind=linux",
         "mac": "kind=mac\n" + BMC, "vault": "kind=controller\n" + BMC}
PLAYBOOK = ["ansible-playbook", "-i", ROOT + "/ansible/inventory.py", "--limit", "lin"]
INSTALL = [ROOT + "/ansible/install.yml"]
ASKED = "ERASE lin's disk and install Ubuntu on it, through its BMC at 10.0.0.2? [y/N]"
NEXT = "==> lin is installed and on the tailnet; converge it next: wk machine setup lin\n"
HELD = ("error: lin holds x, y, and its disk is erased: remove each with `wk rm <ws>`; forced, the names are released\n"
        "    --force crosses this\n")


def make():
    w = World(here="vault", confs=CONFS)
    checkout(w.machines["vault"])
    controller(w.machines["vault"])
    w.machines["vault"].on(("ansible-playbook", "-i"), "")
    return w


def played(run):
    return [line[len("[vault] "):].split() for line in run.calls if line.startswith("[vault] ansible-playbook -i")]


class TestMachineInstall(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_it_is_one_ansible_playbook_of_install_yml_limited_to_the_server_then_names_setup(self):
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, played(r), r.effects), (0, [PLAYBOOK + INSTALL], ["[vault] " + " ".join(PLAYBOOK + INSTALL)]))
        self.assertTrue(r.err.endswith(NEXT), r.err)

    def test_its_exit_code_is_ansibles_and_a_failed_install_names_no_next_step(self):
        self.w.machines["vault"].on(("ansible-playbook", "-i"), Result(2))
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
        self.assertIn("attach the install image to lin through its BMC at 10.0.0.2, in place of one an earlier run left? nothing boots [y/N]", r.err)
        self.assertNotIn(NEXT, r.err)

    def test_a_rehearsal_erases_nothing_so_it_meets_no_barrier_and_releases_no_name(self):
        self.w.reserve("x", "lin")
        held = self.w.wk("machine", "install", "lin", "--rehearse", "--yes")
        self.assertEqual((held.code, played(held)), (0, [PLAYBOOK + ["--skip-tags", "wipe"] + INSTALL]))
        self.assertEqual(self.w.machines["vault"].files["/home/u/wk/ws/x"], "lin")

    def test_a_server_that_holds_workspace_names_is_a_barrier_and_forced_its_names_are_released(self):
        self.w.reserve("x", "lin")
        self.w.reserve("y", "lin")
        self.w.reserve("z", "bare")
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, r.err, r.effects), (1, HELD, []))
        forced = self.w.wk("machine", "install", "lin", "--yes", "--force")
        self.assertEqual((forced.code, played(forced)), (0, [PLAYBOOK + INSTALL]))
        self.assertEqual(forced.effects[:2], ["[vault] remove /home/u/wk/ws/x", "[vault] remove /home/u/wk/ws/y"])
        self.assertIn("warning: this command was forced past: lin holds x, y", forced.err)
        self.assertEqual(self.w.machines["vault"].listdir("/home/u/wk/ws"), ["z"])

    def test_no_server_is_asked_what_it_holds(self):
        self.w.machines["lin"].down = self.w.machines["bare"].down = True
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, played(r)), (0, [PLAYBOOK + INSTALL]))
        self.assertEqual([line for line in r.calls if not line.startswith("[vault]")], [])

    def test_a_machine_with_no_bmc_is_refused_naming_the_key(self):
        r = self.w.wk("machine", "install", "bare", "--yes", "--force")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertEqual(r.err, "error: bare has no BMC to be installed through: set bmc=<its BMC's address> in machines/bare.conf "
                                "(`wk machine install -h` lists the other keys)\n")

    def test_a_workstation_and_the_controller_are_refused_whatever_is_forced(self):
        for name, what in (("desk", "a workstation"), ("vault", "the controller, which runs the install")):
            r = self.w.wk("machine", "install", name, "--yes", "--force")
            self.assertEqual((r.code, r.err, r.calls), (1, "error: %s is %s, and install puts an operating system on a server of yours\n" % (name, what), []))

    def test_it_is_typed_on_the_controller_and_an_unknown_machine_is_refused(self):
        self.assertIn("error: wk machine install runs on the controller (vault)", self.w.wk("machine", "install", "lin", "--yes", on="lin").err)
        self.assertIn("error: no machine named nope", self.w.wk("machine", "install", "nope", "--yes").err)

    def test_a_controller_without_ansible_is_refused_after_the_question(self):
        self.w.machines["vault"].on(("ansible-playbook", "--version"), Result(127, "", "not found\n"))
        r = self.w.wk("machine", "install", "lin", "--yes")
        self.assertEqual((r.code, r.effects), (4, []))

    def test_the_confs_install_keys_are_the_hosts_variables(self):
        hostvars = json.loads(self.w.wk("machine", "inventory").out)["_meta"]["hostvars"]
        self.assertEqual(hostvars["lin"], {
            "wk_kind": "linux", "wk_user": "jm", "ansible_user": "jm", "wk_wifi": "wlp1s0", "wk_console": "ttyAMA0,115200",
            "wk_bmc": "10.0.0.2", "wk_arch": "arm64", "wk_os": "24.04"})


KEYS = ("POST", VARS["wk_tailnet_api"] + "/tailnet/-/keys")
AUTH_KEY = "tskey-auth-k1-SECRET"
JOINS = ('sudo -v && NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL %s)" && eval "$(/opt/homebrew/bin/brew shellenv)" '
         '&& brew install tailscale && sudo "$(brew --prefix)/bin/tailscaled" install-system-daemon '
         '&& sudo "$(brew --prefix)/bin/tailscale" up --ssh --auth-key=%s --hostname=mac\n' % (VARS["wk_homebrew_install"], AUTH_KEY))


class TestMacInstall(unittest.TestCase):
    def setUp(self):
        self.w = make()
        self.w.secrets.values["tailnet-api-key"] = "tskey-api-k2-API"
        self.http = Http({KEYS: (200, {"id": "k1", "key": AUTH_KEY})})
        patch = self.http.patch()
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_prints_the_line_that_joins_the_mac_with_a_one_off_pre_approved_tagged_key_and_erases_nothing(self):
        r = self.w.wk("machine", "install", "mac", "--yes")
        self.assertEqual((r.code, r.out, played(r)), (0, JOINS, []))
        self.assertIn("==> on mac, erased and set up with its conf's user, type this in Terminal; then: wk machine setup mac\n", r.err)
        method, url, headers, body = self.http.asked[0]
        self.assertEqual(headers["Authorization"], "Bearer tskey-api-k2-API")
        self.assertEqual(json.loads(body), {"capabilities": {"devices": {"create": {"reusable": False, "ephemeral": False, "preauthorized": True,
                                                                                    "tags": VARS["wk_server_tags"]}}},
                                            "expirySeconds": 86400, "description": "wk install mac"})

    def test_it_asks_before_it_makes_a_key(self):
        r = self.w.wk("machine", "install", "mac", stdin="n\n", tty=True)
        self.assertEqual((r.code, r.out, self.http.asked), (1, "", []))
        self.assertIn("make a one-off key that joins mac to the tailnet once a person has erased it? [y/N]", r.err)

    def test_a_dry_run_makes_no_key_and_prints_no_line(self):
        r = self.w.wk("machine", "install", "mac", "--dry-run")
        self.assertEqual((r.code, r.out, self.http.asked), (0, "", []))
        self.assertIn("would: [tailscale] POST /tailnet/-/keys for mac\n", r.err)

    def test_names_the_mac_holds_are_a_barrier_and_forced_they_are_released(self):
        self.w.reserve("x", "mac")
        r = self.w.wk("machine", "install", "mac", "--yes")
        self.assertEqual((r.code, r.out, self.http.asked), (1, "", []))
        self.assertIn("error: mac holds x, and its disk is erased", r.err)
        forced = self.w.wk("machine", "install", "mac", "--yes", "--force")
        self.assertEqual((forced.code, forced.out, forced.effects), (0, JOINS, ["[vault] remove /home/u/wk/ws/x"]))

    def test_no_api_key_is_refused_before_the_question(self):
        del self.w.secrets.values["tailnet-api-key"]
        r = self.w.wk("machine", "install", "mac", "--yes")
        self.assertEqual((r.code, r.err), (1, "error: the join key is made with tailnet-api-key, and the controller holds none: "
                                              "`wk key set tailnet-api-key`\n"))

    def test_a_key_the_api_does_not_make_needs_a_person_naming_the_tags(self):
        for answer, said in (((403, {"message": "tags not permitted"}), "answered HTTP 403: tags not permitted"),
                             ((200, {"key": "nope"}), "answered HTTP 200: "), (net.NoAnswer("timed out"), "did not answer (timed out)")):
            self.http.answers[KEYS] = answer
            r = self.w.wk("machine", "install", "mac", "--yes")
            self.assertEqual((r.code, r.out), (4, ""))
            self.assertIn("error: the tailnet's API made no auth key: it %s\n    tailnet-api-key must be allowed to make keys tagged %s"
                          % (said, ", ".join(VARS["wk_server_tags"])), r.err)

    def test_there_is_nothing_to_rehearse(self):
        r = self.w.wk("machine", "install", "mac", "--rehearse", "--yes")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertIn("a Mac is installed by a person, so there is nothing to rehearse", r.err)


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
        text = tasks()
        self.assertIn("          ansible.builtin.import_tasks: install/boot.yml\n          tags: [wipe]\n", text["install.yml"])
        self.assertRegex(text["install.yml"], r"BootSourceOverrideEnabled: Disabled\n +status_code: \[200, 204\]\n +tags: \[wipe\]\n")
        for word in ("BootSourceOverrideEnabled: Once", "shutdown", "wk_install_reset", "method: DELETE", "method: POST", "known_hosts"):
            self.assertIn(word, text["boot.yml"], word)
            for name in set(text) - {"boot.yml", "autoinstall.yaml.j2"}:
                self.assertNotIn(word, text[name], "%s: %s" % (name, word))
        self.assertEqual(re.findall(r"redfish_command", text["media.yml"] + text["image.yml"] + text["preflight.yml"] + text["detach.yml"]), [])

    def test_the_old_tailnet_node_is_retired_only_once_the_new_one_has_joined(self):
        boot = tasks()["boot.yml"]
        joined, retired, named = (boot.index("- name: " + name) for name in (
            "Wait for the installed system to join the tailnet", "Retire the old system's tailnet node", "Give the new node the server's name"))
        self.assertLess(joined, retired)
        self.assertLess(retired, named)
        self.assertIn("selectattr('created', 'ge', wk_install_began)", boot[joined:retired])
        self.assertIn("selectattr('created', 'lt', wk_install_began)", boot[retired:named])

    def test_the_image_is_detached_and_removed_however_the_run_ends(self):
        play = tasks()["install.yml"]
        cleanup = play[play.index("      always:\n"):]
        self.assertEqual(re.findall(r"import_tasks: install/(\S+)", cleanup), ["detach.yml"])
        self.assertEqual(re.findall(r'path: "{{ (\w+) }}"\n +state: absent', cleanup), ["wk_install_build"])
        self.assertIn('argv: [pkill, -f, "{{ wk_install_socket }}"]', tasks()["detach.yml"])
        for made in ("wk_install_iso", "wk_install_socket"):
            self.assertRegex(play, r'%s: "{{ wk_install_build }}/' % made)
        self.assertIn('dest: "{{ wk_install_build }}/', tasks()["media.yml"])

    def test_the_bridge_is_the_pinned_websocat_the_controller_role_fetched(self):
        self.assertIn('"$0" --binary --insecure --basic-auth-file "$1" "$2" "unix:$3"; sleep 5; done\',\n               "{{ wk_websocat }}",', tasks()["media.yml"])
        self.assertIn('{tool: "{{ wk_websocat }}", asks: --version', tasks()["install.yml"])

    def test_the_bmcs_password_reaches_the_bridge_in_a_file_and_no_command_line(self):
        media = tasks()["media.yml"]
        self.assertIn("--basic-auth-file", media)
        for line in media.splitlines():
            if "argv" in line:
                self.assertNotIn("secret", line)

    def test_the_boot_menus_kernel_lines_gain_autoinstall_and_the_consoles(self):
        image = tasks()["image.yml"]
        regexp, replace = re.search(r"regexp: '(.*)'\n +replace: '(.*)'", image).groups()
        replace = replace.replace("{{ wk_install_consoles }}", "console=ttyAMA0,115200 console=tty0")
        for line in ("\tlinux\t/casper/vmlinuz  --- console=tty0", "\tlinux\t/casper/vmlinuz  ---", "\tlinux\t/casper/hwe-vmlinuz  --- quiet"):
            self.assertEqual(re.sub(regexp, replace, line, flags=re.M),
                             "\tlinux\t%s autoinstall fsck.mode=skip --- console=ttyAMA0,115200 console=tty0" % line.split()[1])
        self.assertEqual(re.sub(regexp, replace, "\tinitrd\t/casper/initrd", flags=re.M), "\tinitrd\t/casper/initrd")

    def test_a_task_that_carries_a_secret_logs_nothing(self):
        for name, text in tasks().items():
            for task in re.split(r"\n(?= *- name: )", text):
                if re.search(r"wk_store ~|Authorization|autoinstall\.yaml\.j2|content: .*password", task):
                    self.assertIn("no_log: true", task, "%s: %s" % (name, task.strip().splitlines()[0]))

    def test_no_secret_is_written_in_the_playbook(self):
        for name, text in tasks().items():
            self.assertIsNone(re.search(r"tskey-|password: [^{\s]", text.replace('password: "{{', "password: {{")), name)

    def test_the_real_confs_parse_and_name_only_pinned_releases(self):
        for machine in conf.load(os.path.join(ROOT, "machines")).values():
            if machine.get("bmc"):
                self.assertIn(machine.get("arch"), VARS["wk_ubuntu"][machine.get("os")], machine.name)


VARIABLES = dict(VARS, wk_install_name="lin", wk_install_user="jm", wk_install_codename="noble",
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

    def test_live_the_autoinstall_file_installs_unasked_onto_the_whole_largest_disk_and_restarts(self):
        auto = self.render()
        self.assertEqual((auto["version"], auto["interactive-sections"], auto["shutdown"]), (1, [], "reboot"))
        self.assertEqual(auto["storage"], {"layout": {"name": "lvm", "sizing-policy": "all", "match": {"size": "largest"}}})
        self.assertEqual(auto["ssh"], {"install-server": False})
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
        self.assertEqual((first["hostname"], first["package_update"]), ("lin", True))
        self.assertEqual(first["users"], [{"name": "jm", "gecos": "jm", "shell": "/bin/bash", "groups": ["adm", "sudo"], "sudo": "ALL=(ALL) NOPASSWD:ALL",
                                           "lock_passwd": True}])
        self.assertEqual(first["apt"]["sources"]["tailscale.list"], {
            "source": "deb [signed-by=$KEY_FILE] https://pkgs.tailscale.com/stable/ubuntu noble main", "key": VARIABLES["wk_install_tailscale_key"]})
        self.assertLessEqual({"git", "podman", "uidmap", "bubblewrap", "python3", "curl", "openssl", "tar", "tailscale"}, set(first["packages"]))
        self.assertEqual(first["write_files"], [{"path": "/etc/wk-tailnet-auth-key", "permissions": "0600", "content": "tskey-auth-k1"}])
        self.assertEqual(first["runcmd"], [
            ["loginctl", "enable-linger", "jm"],
            ["sh", "-c", "grep -q '^jm:' /etc/subuid /etc/subgid || usermod --add-subuids 100000-165535 --add-subgids 100000-165535 jm"],
            ["tailscale", "up", "--auth-key=file:/etc/wk-tailnet-auth-key", "--ssh", "--hostname=lin"],
            ["rm", "-f", "/etc/wk-tailnet-auth-key"]])
