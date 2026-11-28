"""The controller's API: the record of workspace names, what `wk serve` answers whom, and its HTTP."""

import contextlib
import io
import json
import threading
import unittest
from unittest import mock

from tests.world import CONFS, World
from wk.api import client, hooks, names, server
from wk.core import conf
from wk.core.act import die
from wk.core.fakes import Fake
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.core.machine import Result
from wk.creds import net, rotation

HOME = "/home/u/wk"


class TestNames(unittest.TestCase):
    def setUp(self):
        self.m = Fake("vault")

    def test_a_free_name_is_reserved_by_its_file_holding_the_server(self):
        self.assertEqual(names.reserve(self.m, HOME, "x", "lin"), "lin")
        self.assertEqual((self.m.files, self.m.journal.effects()), ({HOME + "/ws/x": "lin"}, ["[vault] create %s/ws/x" % HOME]))

    def test_a_held_name_answers_its_holder_and_is_left(self):
        self.m.files[HOME + "/ws/x"] = "mac"
        self.assertEqual((names.reserve(self.m, HOME, "x", "lin"), names.reserve(self.m, HOME, "x", "mac")), ("mac", "mac"))
        self.assertEqual(self.m.files, {HOME + "/ws/x": "mac"})

    def test_a_name_released_between_the_create_and_the_read_is_tried_again(self):
        tries = iter([False, True])
        self.m._create = lambda path, text: next(tries)
        self.assertEqual(names.reserve(self.m, HOME, "x", "lin"), "lin")

    def test_holders_lists_every_name_and_a_release_removes_one(self):
        self.m.files.update({HOME + "/ws/x": "lin", HOME + "/ws/y": "mac\n"})
        self.assertEqual(names.holders(self.m, HOME), {"x": "lin", "y": "mac"})
        names.release(self.m, HOME, "x")
        self.assertEqual((names.holders(self.m, HOME), names.holder(self.m, HOME, "x")), ({"y": "mac"}, None))


class TestAnswers(unittest.TestCase):
    """What `wk serve` answers; the asker is named by its address, as the world's whois names it."""

    def setUp(self):
        self.w = World(here="vault")
        self.vault = self.w.machines["vault"]

    def ask(self, method, path, who="lin"):
        return server.answer(self.w.inv("vault"), method, path, who)

    def test_a_server_reserves_a_free_name_and_again_finds_it_its_own(self):
        self.assertEqual(self.ask("PUT", "/ws/x"), (200, {"name": "x", "server": "lin"}))
        self.assertEqual(self.ask("PUT", "/ws/x"), (200, {"name": "x", "server": "lin"}))
        self.assertEqual(self.vault.files[HOME + "/ws/x"], "lin")

    def test_a_name_another_server_holds_is_409_naming_it_for_a_reservation_and_a_release(self):
        self.w.reserve("x", "mac")
        for method in ("PUT", "DELETE"):
            self.assertEqual(self.ask(method, "/ws/x"), (409, {"message": "x is mac's", "holder": "mac"}))
        self.assertEqual(self.vault.files[HOME + "/ws/x"], "mac")

    def test_a_release_by_the_holder_frees_the_name_and_an_unheld_one_is_404(self):
        self.w.reserve("x", "lin")
        self.assertEqual(self.ask("DELETE", "/ws/x"), (200, {"name": "x", "server": "lin"}))
        self.assertEqual(self.ask("DELETE", "/ws/x"), (404, {"message": "no server holds x"}))
        self.assertNotIn(HOME + "/ws/x", self.vault.files)

    def test_every_name_and_its_server_is_listed_to_any_machine_with_a_conf(self):
        self.w.reserve("x", "lin")
        self.w.reserve("y", "mac")
        for who in ("desk", "lin", "vault"):
            self.assertEqual(self.ask("GET", "/ws", who), (200, {"names": {"x": "lin", "y": "mac"}}))

    def test_a_conf_added_while_it_serves_is_read_at_the_next_request(self):
        inv = self.w.inv("vault")
        self.assertEqual(server.answer(inv, "GET", "/ws", "seat")[0], 403)
        self.w.confs = dict(self.w.confs, seat=conf.parse("seat", "kind=workstation"))
        self.assertEqual(server.answer(inv, "GET", "/ws", "seat"), (200, {"names": {}}))

    def test_a_node_with_no_conf_or_none_at_all_is_403(self):
        self.vault.on(("tailscale", "whois", "--json", "100.64.0.9"), Result(1, "", "no match for IP\n"))
        self.assertEqual(self.ask("GET", "/ws", "stranger"), (403, {"message": "stranger has no conf on the controller"}))
        self.assertEqual(self.ask("GET", "/ws", "100.64.0.9"), (403, {"message": "the tailnet names no node at 100.64.0.9"}))

    def test_only_a_server_reserves_releases_and_rotates(self):
        for method, path in (("PUT", "/ws/x"), ("DELETE", "/ws/x"), ("POST", "/rotate")):
            self.assertEqual(self.ask(method, path, "desk"), (403, {"message": "desk holds no workspace: it is a workstation"}))
        self.assertEqual(self.vault.listdir(HOME + "/ws"), [])

    def test_a_name_that_cannot_be_a_workspace_and_any_other_request_is_404(self):
        for method, path in (("PUT", "/ws/../x"), ("PUT", "/ws/X"), ("PUT", "/ws/"), ("GET", "/ws/x"), ("POST", "/ws"), ("PUT", "/other")):
            self.assertEqual(self.ask(method, path), (404, {"message": "no such request: %s %s" % (method, path)}), path)
        self.assertEqual(self.vault.files, {})

    def test_a_rotation_is_the_askers_and_one_that_fails_is_502_with_why(self):
        with mock.patch.object(rotation, "rotate", lambda inv, name: {"o/r": "key of %s" % name}):
            self.assertEqual(self.ask("POST", "/rotate", "mac"), (200, {"o/r": "key of mac"}))
        with mock.patch.object(rotation, "rotate", lambda inv, name: die("github-token is gone")):
            self.assertEqual(self.ask("POST", "/rotate"), (502, {"message": "github-token is gone"}))


class TestHttp(unittest.TestCase):
    def test_a_request_is_answered_as_json_with_the_askers_address(self):
        asked, log = [], io.StringIO()
        with contextlib.redirect_stderr(log), server.Server(("127.0.0.1", 0), lambda method, path, at: asked.append((method, path, at)) or (409, {"holder": "mac"})) as s:
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.shutdown)
            for method in ("GET", "PUT", "DELETE", "POST"):
                status, body = net.request(method, "http://127.0.0.1:%d/ws/x" % s.server_address[1])
                self.assertEqual((status, json.loads(body)), (409, {"holder": "mac"}))
        self.assertEqual(asked, [(method, "/ws/x", "127.0.0.1") for method in ("GET", "PUT", "DELETE", "POST")])
        self.assertIn('"PUT /ws/x HTTP/1.1" 409', log.getvalue())


class TestServe(unittest.TestCase):
    def test_it_serves_on_the_tailnet_address_and_the_port_the_controllers_conf_names(self):
        w = World(here="vault", confs=dict(CONFS, vault="kind=controller\napi_port=0"))
        w.machines["vault"].on(("tailscale", "ip", "-4"), "127.0.0.1\nfd7a::1\n")
        served = []
        with mock.patch.object(server.Server, "serve_forever", lambda s: served.append(s.server_address[0])):
            r = w.wk("serve")
        self.assertEqual((r.code, served), (0, ["127.0.0.1"]))
        self.assertIn("==> serving the controller's API on 127.0.0.1:", r.err)

    def test_the_port_is_wk_api_port_unless_the_conf_names_one(self):
        w = World()
        self.assertEqual((client.port(w.confs["vault"]), client.port(w.confs["vault"].__class__("v", "controller", {"api_port": "9"}))), (3129, 9))

    def test_no_tailnet_address_needs_a_person(self):
        w = World(here="vault")
        w.machines["vault"].on(("tailscale", "ip", "-4"), Result(1, "", "not running\n"))
        r = w.wk("serve")
        self.assertEqual((r.code, r.err), (4, "error: this machine has no tailnet address to serve on: `tailscale ip -4` said not running\n"
                                              "    check `tailscale status`\n"))

    def test_it_runs_on_the_controller_alone(self):
        self.assertIn("error: wk serve runs on the controller (vault)", World(here="lin").wk("serve").err)


class TestDoctor(unittest.TestCase):
    def rows(self, w, on="lin"):
        with mock.patch.object(net, "request", w._api(on)):
            return hooks.doctor(w.inv(on), w.confs[on], None)

    def test_an_api_that_answers_this_machine_is_ok(self):
        w = World(here="lin")
        w.workspace("x")
        self.assertEqual(self.rows(w), [DoctorRow("api", OK, "vault answers: 1 workspace names")])

    def test_an_api_that_does_not_answer_is_unknown_and_one_that_refuses_is_missing_naming_the_policy(self):
        w = World(here="lin")
        remedy = "wk machine setup vault --stage controller; and the tailnet's policy lets lin reach vault:3129"
        w.machines["vault"].down = True
        self.assertEqual(self.rows(w), [DoctorRow("api", UNKNOWN, "the controller, vault, did not answer (the fake is down)", remedy)])
        w.machines["vault"].down = False
        w.machines["vault"].on(("tailscale", "whois", "--json", "lin"), "")
        self.assertEqual(self.rows(w), [DoctorRow("api", MISSING, "the controller, vault, answered HTTP 403: the tailnet names no node at lin", remedy)])

    def test_a_controller_the_tailnet_gives_no_address_is_unknown(self):
        w = World(here="lin")
        w.machines["lin"].on(("tailscale", "ip", "-4"), Result(1, "", "no such peer\n"))
        row = self.rows(w)[0]
        self.assertEqual((row.name, row.status, row.detail), ("api", UNKNOWN, "the controller, vault, did not answer (`tailscale ip -4 vault` said no such peer)"))

    def test_a_workspace_has_no_row_of_its_own(self):
        w = World(here="lin")
        self.assertEqual(hooks.doctor(w.inv("lin"), w.confs["lin"], w.workspace("x")), [])
