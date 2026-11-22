import io
import json
import unittest
import urllib.error
from unittest import mock

from tests import toy
from tests.world import World
from wk.core import act
from wk.core.errors import Refused
from wk.core.fakes import Fake
from wk.core.fleet import Fleet
from wk.core.inv import SEAMS, Inv
from wk.core.machine import Result
from wk.creds import net
from wk.creds.bao import Bao

# Pinned from openbao.org/docs/commands/kv: `kv get -field=value -mount=secret <path>` prints the one field,
# `kv put -mount=secret <path> value=-` reads the value from stdin, `kv list -format=json -mount=secret <path>` prints
# a JSON array in which a folder ends in `/`; a path that holds nothing is "No value found at ..." and exit 2.
ADDR = "http://vault:8200"
ENV = {"BAO_ADDR": ADDR}
GET = ("bao", "kv", "get", "-field=value", "-mount=secret")
LIST = ("bao", "kv", "list", "-format=json", "-mount=secret")
PUT = ("bao", "kv", "put", "-mount=secret")
ABSENT = Result(2, "", "No value found at secret/data/wk/nope\n")


class TestBao(unittest.TestCase):
    def setUp(self):
        self.saved = act.flags
        act.begin(act.Flags())
        self.addCleanup(act.begin, self.saved)
        self.m = Fake("desk")
        self.bao = Bao(self.m, ADDR)

    def test_get_asks_the_cli_for_the_one_field_at_the_address_wk_names(self):
        self.m.on(GET + ("wk/github-token",), "v1\n")
        self.assertEqual((self.bao.get("github-token"), self.m.calls[-1]), ("v1", self.m.calls[-1].__class__(GET + ("wk/github-token",), None, None, ENV)))

    def test_a_value_of_several_lines_keeps_them(self):
        self.m.on(GET, "-----BEGIN-----\nabc\n-----END-----\n")
        self.assertEqual(self.bao.get("deploy-key/o/r"), "-----BEGIN-----\nabc\n-----END-----")

    def test_a_name_the_store_lacks_is_none(self):
        self.m.on(GET, ABSENT)
        self.assertIsNone(self.bao.get("nope"))

    def test_put_sends_the_value_on_stdin_and_never_in_the_argv(self):
        self.bao.put("deploy-key/o/r", "s3cret")
        call = self.m.calls[-1]
        self.assertEqual((call.argv, call.input, call.env), (PUT + ("wk/deploy-key/o/r", "value=-"), "s3cret", ENV))
        self.assertEqual(self.m.journal.effects(), ["[desk] bao kv put -mount=secret wk/deploy-key/o/r value=-"])

    def test_list_follows_each_folder_and_returns_the_sorted_names(self):
        self.m.on(LIST + ("wk/",), json.dumps(["github-token", "deploy-key/"]))
        self.m.on(LIST + ("wk/deploy-key/",), json.dumps(["o/"]))
        self.m.on(LIST + ("wk/deploy-key/o/",), json.dumps(["r", "q"]))
        self.assertEqual(self.bao.list(), ["deploy-key/o/q", "deploy-key/o/r", "github-token"])

    def test_an_empty_store_lists_nothing(self):
        self.m.on(LIST, Result(2, "", "No value found at secret/metadata/wk\n"))
        self.assertEqual(self.bao.list(), [])

    def refused(self, answer, ask, *said):
        self.m.on(("bao",), answer)
        with self.assertRaises(Refused) as e:
            ask()
        self.assertEqual(e.exception.status, 4)
        for text in said:
            self.assertIn(text, str(e.exception))

    def test_a_missing_cli_is_refused_naming_how_to_install_it(self):
        self.refused(Result(127, "", "sh: bao: command not found\n"), lambda: self.bao.get("n"),
                     "`bao kv get -field=value -mount=secret wk/n` failed (exit 127): sh: bao: command not found", "brew install openbao")

    def test_no_login_is_refused_naming_bao_login_at_the_stores_address_and_is_never_absent(self):
        for ask in (lambda: self.bao.get("n"), self.bao.list, lambda: self.bao.put("n", "v")):
            self.refused(Result(2, "", "Error making API request.\nCode: 403. Errors:\n* permission denied\n"), ask,
                         "permission denied", "the store is http://vault:8200", "BAO_ADDR=http://vault:8200 bao login")


class TestPutIsAnEffect(unittest.TestCase):
    def test_a_dry_run_prints_the_put_and_runs_no_cli(self):
        w = World(commands=[toy], here="lin", seams={"secrets": lambda inv: Bao(inv.here, ADDR)})
        r = w.wk("--dry-run", "seamy", "x")
        self.assertEqual((r.code, [c for c in r.calls if "bao" in c]), (0, []))
        self.assertIn("[lin] bao kv put -mount=secret wk/token value=-", r.plan)


class TestConnect(unittest.TestCase):
    def test_the_real_tree_wires_the_seam_to_this_client_and_the_credential_servers_address(self):
        w = World()
        inv = Inv("/", {}, Fleet(lambda: w.confs, "desk", None, w.machines["desk"]), None, None, {}, SEAMS)
        store = inv.secrets()
        self.assertEqual((type(store), store.m, store.addr), (Bao, w.machines["desk"], "http://vault:8200"))


class TestKeyLs(unittest.TestCase):
    def test_it_prints_the_stores_names_one_to_a_line_which_is_what_the_playbook_reads(self):
        w = World()
        w.secrets.values.update({"github-token": "t", "deploy-key/o/r": "k"})
        r = w.wk("key", "ls")
        self.assertEqual((r.code, r.out, r.effects), (0, "deploy-key/o/r\ngithub-token\n", []))
        self.assertEqual(World().wk("key", "ls").out, "")


class TestRequest(unittest.TestCase):
    def ask(self, outcome):
        with mock.patch("urllib.request.urlopen", side_effect=outcome) as urlopen:
            return net.request("POST", "https://h.example/p", {"X-A": "1"}, b"body"), urlopen.call_args

    def test_an_answer_is_its_status_and_body_and_the_request_carries_method_headers_and_body(self):
        class Answer(io.BytesIO):
            status = 201
        (got, (args, kwargs)) = self.ask([Answer(b"made")])
        req = args[0]
        self.assertEqual((got, req.get_method(), req.full_url, req.data, req.get_header("X-a"), req.get_header("User-agent"), kwargs),
                         ((201, b"made"), "POST", "https://h.example/p", b"body", "1", "wk", {"timeout": 20}))

    def test_an_error_status_is_an_answer(self):
        error = urllib.error.HTTPError("https://h.example/p", 404, "Not Found", {}, io.BytesIO(b"gone"))
        self.assertEqual(self.ask(error)[0], (404, b"gone"))

    def test_no_answer_raises_naming_why(self):
        with self.assertRaisesRegex(net.NoAnswer, "URLError: .*refused"):
            self.ask(urllib.error.URLError("refused"))
