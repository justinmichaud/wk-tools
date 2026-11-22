import unittest

from tests import ROOT
from tests.credsworld import Http, login, world
from tests.killpoints import converges, dry_run_is_the_plan
from wk import creds
from wk.core.conf import parse
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Result
from wk.creds import net

D = "/home/u/wk/injector"
UNIT = "/home/u/.config/systemd/user/wk-injector.service"
PROGRAM = "the injector's source\n"
STATE = "/systemd/wk-injector"


def made(port=""):
    """A world whose credential server models systemd: the unit's state is a file, so a kill-point run compares it."""
    confs = {"vault": "kind=linux\nrole=credential" + port}
    w = world(claude_login=login())
    w.confs.update({name: parse(name, text) for name, text in confs.items()})
    w.machines["desk"].files[ROOT + "/lib/wk/creds/injector.py"] = PROGRAM
    vault = w.machines["vault"]
    del vault.files[D + "/ca.pem"]

    def systemctl(call):
        verb = call.argv[2]
        if verb == "stop":
            vault.files.pop(STATE + "/running", None)
        elif verb == "daemon-reload":
            vault.files[STATE + "/loaded"] = vault.files[UNIT]
        elif verb == "enable":
            vault.files[STATE + "/enabled"] = ""
        elif verb == "restart":
            vault.files[STATE + "/running"] = vault.files[STATE + "/loaded"] + vault.files[D + "/injector.py"]
        else:
            return Result(0 if STATE + {"is-enabled": "/enabled", "is-active": "/running"}[verb] in vault.files else 1)
    vault.on(("systemctl", "--user"), systemctl)
    vault.on(("id", "-un"), "u\n")
    vault.on(("loginctl", "enable-linger"), lambda call: vault.files.update({"/var/lib/systemd/linger/" + call.argv[2]: ""}))
    return w


class TestInjectorSetup(unittest.TestCase):
    def test_the_first_run_installs_the_program_the_login_and_the_unit_and_starts_it(self):
        w = made()
        r = w.wk("converge", "vault")
        self.assertEqual((r.code, r.out.splitlines()[0]), (0, "60 injector changed"))
        self.assertEqual(r.effects, ["[vault] " + e for e in (
            "install -d -m 700 %s %s/login" % (D, D), "systemctl --user stop wk-injector.service", "write %s/injector.py" % D, "write " + UNIT,
            "write %s/login/.credentials.json" % D, "write %s/login/delivered" % D, "loginctl enable-linger u",
            "systemctl --user daemon-reload", "systemctl --user enable wk-injector.service", "systemctl --user restart wk-injector.service")])
        vault = w.machines["vault"]
        self.assertEqual((vault.files[D + "/injector.py"], vault.files[D + "/login/.credentials.json"], vault.modes[D + "/login/.credentials.json"]),
                         (PROGRAM, login(), "600"))
        self.assertEqual(vault.dirs, {D: "700", D + "/login": "700"})

    def test_the_unit_runs_the_copied_program_isolated_on_the_default_port(self):
        w = made()
        w.wk("converge", "vault")
        self.assertEqual(w.machines["vault"].files[UNIT], """[Unit]
Description=wk injector: the one holder of the claude.ai login

[Service]
ExecStart=/usr/bin/env python3 -I /home/u/wk/injector/injector.py /home/u/wk/injector 3128
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""")

    def test_the_port_is_the_machine_confs(self):
        w = made("\ninjector_port=4000")
        w.wk("converge", "vault")
        self.assertIn("injector.py /home/u/wk/injector 4000\n", w.machines["vault"].files[UNIT])

    def test_a_second_run_changes_nothing(self):
        w = made()
        w.wk("converge", "vault")
        r = w.wk("converge", "vault")
        self.assertEqual((r.out.splitlines()[0], r.effects), ("60 injector unchanged", []))

    def test_a_login_the_injector_has_refreshed_is_not_overwritten_by_the_stores_older_copy(self):
        w = made()
        w.wk("converge", "vault")
        w.machines["vault"].files[D + "/login/.credentials.json"] = login(access="refreshed")
        self.assertEqual(w.wk("converge", "vault").effects, [])

    def test_a_new_login_in_the_store_is_delivered_without_a_restart(self):
        w = made()
        w.wk("converge", "vault")
        w.secrets.values["claude-login"] = login(access="again")
        self.assertEqual(w.wk("converge", "vault").effects, ["[vault] write %s/login/.credentials.json" % D, "[vault] write %s/login/delivered" % D])

    def test_a_changed_program_stops_the_service_replaces_it_and_starts_it(self):
        w = made()
        w.wk("converge", "vault")
        w.machines["desk"].files[ROOT + "/lib/wk/creds/injector.py"] = "newer\n"
        r = w.wk("converge", "vault")
        self.assertEqual(r.effects, ["[vault] " + e for e in (
            "systemctl --user stop wk-injector.service", "write %s/injector.py" % D, "systemctl --user daemon-reload",
            "systemctl --user enable wk-injector.service", "systemctl --user restart wk-injector.service")])

    def test_a_stopped_service_is_started(self):
        w = made()
        w.wk("converge", "vault")
        del w.machines["vault"].files[STATE + "/running"]
        self.assertEqual(len(w.wk("converge", "vault").effects), 3)

    def test_without_a_login_in_the_store_the_injector_is_installed_and_the_remedy_named(self):
        w = made()
        del w.secrets.values["claude-login"]
        r = w.wk("converge", "vault")
        self.assertEqual(r.code, 0)
        self.assertIn("warning: claude-login is not in the store, so the injector holds no login: `wk key set claude-login`", r.err)
        self.assertNotIn(D + "/login/.credentials.json", w.machines["vault"].files)

    def test_a_credential_server_that_is_not_linux_is_refused(self):
        w = made()
        w.confs["vault"] = parse("vault", "kind=mac\nrole=credential")
        r = w.wk("converge", "vault")
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("error: the credential server vault is kind=mac, and the injector is installed as a systemd user unit", r.err)

    def test_a_server_that_cannot_say_what_it_holds_is_refused(self):
        w = made()
        w.machines["vault"].on(("python3", "-I", "-c"), Result(1))
        r = w.wk("converge", "vault")
        self.assertEqual((r.code, r.effects), (4, []))

    def test_killed_after_any_effect_and_run_again_it_converges(self):
        converges(self, made, "converge", "vault")

    def test_killed_while_replacing_a_running_program_it_still_ends_running_the_new_one(self):
        def running_old():
            w = made()
            w.wk("converge", "vault")
            w.machines["desk"].files[ROOT + "/lib/wk/creds/injector.py"] = "newer\n"
            return w
        converges(self, running_old, "converge", "vault")
        w = running_old()
        w.wk("converge", "vault")
        self.assertTrue(w.machines["vault"].files[STATE + "/running"].endswith("newer\n"))

    def test_a_dry_run_is_the_plan(self):
        dry_run_is_the_plan(self, made, "converge", "vault")


class TestInjectorDoctor(unittest.TestCase):
    def row(self, answer, port=""):
        w = made(port)
        with Http({("GET", "http://vault:%s/" % (port[-4:] or "3128")): answer}).patch():
            line = w.wk("converge", "vault").out.splitlines()[-1]
        return line.split("|")

    def test_an_injector_holding_a_login_is_ok(self):
        self.assertEqual(self.row((400, b"wk injector: a forward proxy, CONNECT only\r\nlogin held\r\n")),
                         ["injector", "ok", "vault:3128 holds the claude.ai login", ""])

    def test_it_is_asked_on_the_confs_port(self):
        self.assertEqual(self.row((400, b"wk injector: x\r\nlogin held\r\n"), "\ninjector_port=4000")[1], "ok")

    def test_an_injector_without_a_usable_login_is_missing_and_says_why(self):
        self.assertEqual(self.row((400, b"wk injector: x\r\nthis machine holds no claude.ai login\r\n")),
                         ["injector", "missing", "this machine holds no claude.ai login", "wk key set claude-login && wk machine setup vault"])

    def test_something_else_on_the_port_is_missing(self):
        for body in (b"<html>squid</html>", b""):
            self.assertEqual(self.row((200, body)), ["injector", "missing", "something else answers at vault:3128", "wk machine setup vault"])

    def test_no_answer_is_unknown_never_ok(self):
        self.assertEqual(self.row(net.NoAnswer("refused")), ["injector", "unknown", "vault:3128 did not answer (refused)", "wk machine setup vault"])


class TestAgentEnv(unittest.TestCase):
    def test_an_agent_session_gets_the_injector_as_proxy_its_ca_and_the_placeholders_directory(self):
        for port, want in (("", "3128"), ("\ninjector_port=4000", "4000")):
            w = made(port)
            inv = Inv("/", {}, Fleet(lambda: w.confs, "lin", w.machines.get, w.machines["lin"], workers=1), w.clock, None, {}, w.seams)
            proxy = "http://vault:" + want
            self.assertEqual(creds.agent_env(inv), {"HTTPS_PROXY": proxy, "https_proxy": proxy, "NODE_EXTRA_CA_CERTS": "/home/u/wk/injector-ca.pem",
                                                    "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/home/u/wk/claude-login"})
