"""The credentials lane in `wk doctor`, and what it gives an agent session."""

import unittest

from tests.credsworld import Http
from tests.world import World
from tests.ws import inv
from wk import creds
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.creds import hooks, net

HOME = "/home/u/wk"
INJECTOR = ("GET", "http://vault:3128/")
FIX = "wk machine setup vault --stage controller"
STORE_FIX = "mkdir -m 700 %s/secrets, then `wk key set <name>` for each" % HOME
HELD = (200, b"wk injector\nlogin held\n")
DELIVERED = {HOME + "/claude-login/.credentials.json": "-rw-------", HOME + "/injector-ca.pem": "-rw-r--r--",
             HOME + "/secrets": "drwx------", HOME + "/secrets/deploy": "drwx------", HOME + "/secrets/env.sh": "-rw-------",
             HOME + "/secrets/ssh_config": "-rw-------", HOME + "/secrets/rotate-key": "-rw-------", "/home/u/.pi/agent/models.json": "-rw-------"}


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.inv = inv(self.w)

    def rows(self, name, modes, answers=None):
        self.w.machines[name].on(("ls", "-ld"), lambda c: "".join("%s 1 u u 9 Nov 22 16:07 %s\n" % (modes[p], p) for p in c.argv[2:] if p in modes))
        with Http(answers or {}).patch():
            return hooks.doctor(self.inv, self.w.confs[name], None)

    def test_a_server_with_everything_delivered_is_one_ok_row(self):
        for name in ("lin", "mac"):
            self.assertEqual(self.rows(name, DELIVERED), [DoctorRow("credentials", OK, "", "wk machine setup %s --stage credentials, typed on vault" % name)])

    def test_what_is_absent_or_moded_otherwise_is_named_and_the_credentials_role_is_the_remedy(self):
        modes = dict(DELIVERED, **{HOME + "/secrets": "drwxr-xr-x"})
        del modes[HOME + "/secrets/rotate-key"]
        self.assertEqual(self.rows("lin", modes), [DoctorRow("credentials", MISSING, "absent or wrongly moded: %s/secrets, %s/secrets/rotate-key"
                                                             % (HOME, HOME), "wk machine setup lin --stage credentials, typed on vault")])

    def test_the_controller_answers_for_its_secrets_directory_and_the_injector_and_is_delivered_nothing(self):
        self.assertEqual(self.rows("vault", {HOME + "/secrets": "drwx------"}, {INJECTOR: HELD}),
                         [DoctorRow("secrets", OK, "", STORE_FIX), DoctorRow("injector", OK, "it holds the claude.ai login", FIX)])

    def test_a_secrets_directory_that_is_absent_or_open_to_others_is_missing(self):
        for modes in ({}, {HOME + "/secrets": "drwxr-xr-x"}):
            self.assertEqual(self.rows("vault", modes, {INJECTOR: HELD})[0],
                             DoctorRow("secrets", MISSING, "%s/secrets is absent or not mode 0700" % HOME, STORE_FIX))

    def test_an_injector_without_a_login_or_something_else_at_its_port_is_missing(self):
        for body, detail in ((b"wk injector: a forward proxy\nthis machine holds no claude.ai login: `wk key set claude-login`\n",
                              "this machine holds no claude.ai login: `wk key set claude-login`"),
                             (b"<html>nginx</html>", "something else answers there"), (b"", "something else answers there")):
            self.assertEqual(self.rows("vault", {}, {INJECTOR: (200, body)})[1], DoctorRow("injector", MISSING, detail, FIX))

    def test_an_injector_that_does_not_answer_is_unknown_never_missing(self):
        row = self.rows("vault", {}, {INJECTOR: net.NoAnswer("timed out")})[1]
        self.assertEqual((row.status, row.detail), (UNKNOWN, "http://vault:3128/ did not answer (timed out)"))

    def test_a_workspace_has_no_row_of_its_own(self):
        self.assertEqual(hooks.doctor(self.inv, self.w.confs["lin"], self.w.workspace("x")), [])


class TestWhatASessionTakes(unittest.TestCase):
    def test_the_session_is_pointed_at_the_injector(self):
        w = World()
        self.assertEqual(creds.agent_env(inv(w, "lin")), {
            "HTTPS_PROXY": "http://vault:3128", "https_proxy": "http://vault:3128", "NODE_EXTRA_CA_CERTS": HOME + "/injector-ca.pem",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": HOME + "/claude-login"})
