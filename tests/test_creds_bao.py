import io
import unittest
import urllib.error
from unittest import mock

from tests import toy
from tests.credsworld import Http
from tests.world import World
from wk import creds
from wk.core import act
from wk.core.errors import Refused
from wk.core.fakes import Fake
from wk.core.fleet import Fleet
from wk.core.inv import SEAMS, Inv
from wk.creds import net
from wk.creds.bao import Bao

# Pinned from openbao.org/docs/api/secret/kv/kv-v2 and /docs/commands: the token travels in X-Vault-Token, a KV v2
# value is read and written at <mount>/data/<path> and listed at <mount>/metadata/<path>?list=true, where a folder
# ends in `/` and an empty one is a 404; the CLI reads BAO_ADDR and BAO_TOKEN, and `bao login` writes ~/.vault-token.
ADDR = "https://vault.example:8200"
DATA = ADDR + "/v1/secret/data/wk/"
META = ADDR + "/v1/secret/metadata/wk/"
READ = {"data": {"data": {"value": "v1"}, "metadata": {"version": 3}}}


class Typed(unittest.TestCase):
    def setUp(self):
        self.saved = act.flags
        act.begin(act.Flags())

    def tearDown(self):
        act.begin(self.saved)

    def bao(self, answers):
        http = Http(answers)
        patch = http.patch()
        patch.start()
        self.addCleanup(patch.stop)
        return Bao(ADDR + "/", "tok"), http


class TestBao(Typed):
    def test_get_reads_the_value_from_the_kv2_data_route_with_the_token_header(self):
        bao, http = self.bao({("GET", DATA + "github-token"): (200, READ)})
        self.assertEqual((bao.get("github-token"), http.asked), ("v1", [("GET", DATA + "github-token", {"X-Vault-Token": "tok"}, None)]))

    def test_a_name_the_store_lacks_is_none(self):
        bao, _ = self.bao({("GET", DATA + "nope"): (404, {"errors": []})})
        self.assertIsNone(bao.get("nope"))

    def test_put_posts_the_value_under_data(self):
        bao, http = self.bao({("POST", DATA + "deploy-key/o/r"): (200, {"data": {"version": 1}})})
        bao.put("deploy-key/o/r", "v")
        self.assertEqual(http.asked, [("POST", DATA + "deploy-key/o/r", {"X-Vault-Token": "tok"}, b'{"data": {"value": "v"}}')])

    def test_a_put_that_answers_204_is_stored(self):
        bao, _ = self.bao({("POST", DATA + "n"): (204, b"")})
        bao.put("n", "v")

    def test_list_walks_the_folders_and_sorts(self):
        bao, _ = self.bao({("GET", META + "?list=true"): (200, {"data": {"keys": ["github-token", "deploy-key/"]}}),
                           ("GET", META + "deploy-key/?list=true"): (200, {"data": {"keys": ["o/"]}}),
                           ("GET", META + "deploy-key/o/?list=true"): (200, {"data": {"keys": ["r", "q"]}})})
        self.assertEqual(bao.list(), ["deploy-key/o/q", "deploy-key/o/r", "github-token"])

    def test_an_empty_store_lists_nothing(self):
        bao, _ = self.bao({("GET", META + "?list=true"): (404, {"errors": []})})
        self.assertEqual(bao.list(), [])

    def refused(self, answer, status, *said):
        bao, _ = self.bao({("GET", DATA + "n"): answer})
        with self.assertRaises(Refused) as e:
            bao.get("n")
        self.assertEqual(e.exception.status, status)
        for text in said:
            self.assertIn(text, str(e.exception))

    def test_a_store_that_does_not_answer_is_unknown_not_absent(self):
        self.refused(net.NoAnswer("URLError: refused"), 4, "did not answer (URLError: refused), so what it holds is unknown", "`bao status`")

    def test_a_refused_token_names_bao_login(self):
        self.refused((403, {"errors": ["permission denied"]}), 4, "refuses this token (HTTP 403)", "`bao login`")

    def test_any_other_status_is_a_failure_and_never_absent(self):
        self.refused((503, {"errors": ["sealed"]}), 1, "answered HTTP 503 to GET secret/data/wk", "sealed")


class TestPutIsAnEffect(unittest.TestCase):
    def test_a_dry_run_prints_the_put_and_asks_nothing(self):
        http = Http({})
        w = World(commands=[toy], here="lin", seams={"secrets": lambda inv: Bao(ADDR, "tok")})
        with http.patch():
            r = w.wk("--dry-run", "seamy", "x")
        self.assertEqual((r.code, http.asked), (0, []))
        self.assertIn("[store] secret put token", r.plan)

    def test_a_wet_run_posts_once(self):
        http = Http({("POST", DATA + "token"): (200, {})})
        w = World(commands=[toy], here="lin", seams={"secrets": lambda inv: Bao(ADDR, "tok")})
        with http.patch():
            self.assertEqual(w.wk("seamy", "x").code, 0)
        self.assertEqual([(method, url) for method, url, _, _ in http.asked], [("POST", DATA + "token")])


class TestConnect(unittest.TestCase):
    def secrets(self, env, token_file=None):
        w = World()
        if token_file is not None:
            w.machines["desk"].files["/home/u/.vault-token"] = token_file
        seen = []
        w.seams["secrets"] = lambda inv: seen.append(creds.secrets(inv)) or w.secrets
        r = w.wk("key", "check", env=env)
        return r, seen

    def test_the_address_and_token_come_from_the_environment(self):
        _, seen = self.secrets({"BAO_ADDR": ADDR, "BAO_TOKEN": "tok"})
        self.assertEqual((seen[0].addr, seen[0].token), (ADDR, "tok"))

    def test_without_a_token_variable_the_file_bao_login_writes_is_read(self):
        _, seen = self.secrets({"BAO_ADDR": ADDR}, "filed\n")
        self.assertEqual(seen[0].token, "filed")

    def test_no_address_is_refused_naming_the_variable_and_bao_login(self):
        r, _ = self.secrets({})
        self.assertEqual(r.code, 4)
        self.assertIn("error: BAO_ADDR is not set", r.err)
        self.assertIn("`bao login`", r.err)

    def test_no_token_is_refused_naming_bao_login(self):
        r, _ = self.secrets({"BAO_ADDR": ADDR})
        self.assertEqual(r.code, 4)
        self.assertIn("error: no OpenBao token: BAO_TOKEN is not set and there is no ~/.vault-token\n    log in: `bao login`", r.err)

    def test_the_real_tree_wires_the_seam_to_this_client(self):
        inv = Inv("/", {"BAO_ADDR": ADDR, "BAO_TOKEN": "tok"}, Fleet(dict, "desk", None, Fake("desk")), None, None, {}, SEAMS)
        self.assertIsInstance(inv.secrets(), Bao)


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
