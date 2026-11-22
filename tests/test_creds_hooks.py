"""The credentials lane in `wk doctor` and `wk gc`, and what it gives an agent session."""

import unittest

from tests.credsworld import Http
from tests.world import CONFS, World
from tests.ws import inv
from wk import creds
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.creds import hooks, net

HOME = "/home/u/wk"
INJECTOR = ("GET", "http://vault:3128/")
STORE = ("GET", "http://vault:8200/v1/sys/seal-status")
UNSEALED = (200, {"initialized": True, "sealed": False})
HELD = (200, b"wk injector\nlogin held\n")
SESSION = {HOME + "/claude-login/.credentials.json": "-rw-------", HOME + "/injector-ca.pem": "-rw-r--r--"}
DELIVERED = dict(SESSION, **{HOME + "/secrets": "drwx------", HOME + "/secrets/deploy": "drwx------", HOME + "/secrets/env.sh": "-rw-------",
                             HOME + "/secrets/ssh_config": "-rw-------", "/home/u/.pi/agent/models.json": "-rw-------"})


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
            self.assertEqual(self.rows(name, DELIVERED), [DoctorRow("credentials", OK, "", "wk key sync %s" % name)])

    def test_what_is_absent_or_moded_otherwise_is_named_and_key_sync_is_the_remedy(self):
        modes = dict(DELIVERED, **{HOME + "/secrets": "drwxr-xr-x"})
        del modes[HOME + "/secrets/env.sh"]
        self.assertEqual(self.rows("lin", modes), [DoctorRow("credentials", MISSING, "absent or wrongly moded: %s/secrets, %s/secrets/env.sh"
                                                             % (HOME, HOME), "wk key sync lin")])

    def test_a_borrowed_box_is_asked_only_for_the_two_files_a_claude_session_reads(self):
        self.assertEqual(self.rows("box", SESSION), [DoctorRow("claude login", OK, "", "wk machine setup box")])
        self.assertEqual(self.rows("box", {}), [DoctorRow("claude login", MISSING, "absent or wrongly moded: %s/claude-login/.credentials.json, "
                                                          "%s/injector-ca.pem" % (HOME, HOME), "wk machine setup box")])
        self.assertEqual(self.w.machines["box"].calls[-1].argv, ("ls", "-ld") + tuple(SESSION))

    def test_the_credential_server_answers_for_the_injector_and_the_store_and_is_delivered_nothing(self):
        fix = "wk machine setup vault"
        self.assertEqual(self.rows("vault", {}, {INJECTOR: HELD, STORE: UNSEALED}),
                         [DoctorRow("injector", OK, "it holds the claude.ai login", fix),
                          DoctorRow("openbao", OK, "HTTP 200, initialized True, sealed False", fix)])

    def test_an_injector_without_a_login_or_something_else_at_its_port_is_missing(self):
        for body, detail in ((b"wk injector: a forward proxy\nthis machine holds no claude.ai login: `wk key set claude-login`\n",
                              "this machine holds no claude.ai login: `wk key set claude-login`"),
                             (b"<html>nginx</html>", "something else answers there"), (b"", "something else answers there")):
            self.assertEqual(self.rows("vault", {}, {INJECTOR: (200, body), STORE: UNSEALED})[0],
                             DoctorRow("injector", MISSING, detail, "wk machine setup vault"))

    def test_a_sealed_or_uninitialised_store_or_one_that_answers_an_error_is_missing(self):
        for answer, detail in (((200, {"initialized": True, "sealed": True}), "HTTP 200, initialized True, sealed True"),
                               ((200, {"initialized": False, "sealed": True}), "HTTP 200, initialized False, sealed True"),
                               ((503, b"busy"), "HTTP 503, initialized None, sealed None")):
            self.assertEqual(self.rows("vault", {}, {INJECTOR: HELD, STORE: answer})[1],
                             DoctorRow("openbao", MISSING, detail, "wk machine setup vault"))

    def test_a_service_that_does_not_answer_is_unknown_never_missing(self):
        rows = self.rows("vault", {}, {INJECTOR: net.NoAnswer("timed out"), STORE: net.NoAnswer("refused")})
        self.assertEqual([(r.status, r.detail) for r in rows], [(UNKNOWN, "http://vault:3128/ did not answer (timed out)"),
                                                                    (UNKNOWN, "http://vault:8200/v1/sys/seal-status did not answer (refused)")])

    def test_a_workspace_has_no_row_of_its_own(self):
        self.assertEqual(hooks.doctor(self.inv, self.w.confs["lin"], self.w.workspace("x")), [])

    def test_the_pinned_openbao_is_kept_by_gc(self):
        self.assertEqual(hooks.pins(self.inv, self.w.confs["vault"]), ["openbao-2.7.3"])


class TestWhatASessionTakes(unittest.TestCase):
    def test_only_a_borrowed_box_is_brought_the_injector_at_the_credential_servers_port(self):
        w = World(confs=dict(CONFS, vault=CONFS["vault"] + "\ninjector_port=9000"))
        self.assertEqual([creds.forward(inv(w), w.confs[name]) for name in ("lin", "mac", "box")], [None, None, "vault:9000"])

    def test_on_a_server_of_the_owners_the_session_is_pointed_at_the_injector_itself(self):
        w = World()
        self.assertEqual(creds.agent_env(inv(w, "lin", {"WK_FORWARD": "20001"})), {
            "HTTPS_PROXY": "http://vault:3128", "https_proxy": "http://vault:3128", "NODE_EXTRA_CA_CERTS": HOME + "/injector-ca.pem",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": HOME + "/claude-login"})

    def test_on_a_borrowed_box_it_is_pointed_at_the_forwarded_port(self):
        got = creds.agent_env(inv(World(), "box", {"WK_FORWARD": "20001"}))
        self.assertEqual((got["HTTPS_PROXY"], got["https_proxy"], got["NODE_EXTRA_CA_CERTS"]),
                         ("http://127.0.0.1:20001", "http://127.0.0.1:20001", HOME + "/injector-ca.pem"))
