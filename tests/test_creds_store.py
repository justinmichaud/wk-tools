import io
import os
import stat
import tempfile
import unittest
import urllib.error
from unittest import mock

from tests import toy
from tests.world import World
from wk.core import act
from wk.core.fleet import Fleet
from wk.core.inv import SEAMS, Inv
from wk.core.machine import Local
from wk.creds import net
from wk.creds.store import Files


class TestFiles(unittest.TestCase):
    """The store as it runs on the controller: real files in a directory of the test's."""

    def setUp(self):
        self.saved = act.flags
        act.begin(act.Flags())
        self.addCleanup(act.begin, self.saved)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name + "/secrets"
        self.store = Files(Local(), self.dir)

    def mode(self, path):
        return stat.S_IMODE(os.stat(os.path.join(self.dir, path) if path else self.dir).st_mode)

    def test_a_value_is_a_file_of_its_name_mode_0600_holding_it_and_one_newline(self):
        self.store.put("github-token", "v1")
        with open(self.dir + "/github-token") as f:
            self.assertEqual((f.read(), self.mode("github-token"), self.mode("")), ("v1\n", 0o600, 0o700))
        self.assertEqual(self.store.get("github-token"), "v1")

    def test_a_name_with_a_slash_is_a_path_whose_directories_are_0700(self):
        self.store.put("bmc/moose/user", "admin")
        self.assertEqual((self.mode("bmc"), self.mode("bmc/moose"), self.mode("bmc/moose/user")), (0o700, 0o700, 0o600))
        self.assertEqual(self.store.get("bmc/moose/user"), "admin")

    def test_a_value_of_several_lines_keeps_them_and_a_put_replaces_the_whole_file(self):
        self.store.put("claude-login", "{\n}")
        self.store.put("claude-login", "a\nb")
        self.assertEqual((self.store.get("claude-login"), self.store.list()), ("a\nb", ["claude-login"]))

    def test_a_file_written_by_hand_without_its_newline_is_read_whole(self):
        self.store.put("wifi-ssid", "x")
        with open(self.dir + "/wifi-ssid", "w") as f:
            f.write("home\n\n")
        with open(self.dir + "/wifi-psk", "w") as f:
            f.write("p4ss")
        self.assertEqual((self.store.get("wifi-ssid"), self.store.get("wifi-psk")), ("home\n", "p4ss"))

    def test_a_name_the_store_lacks_is_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_list_is_every_file_under_it_sorted_and_not_a_write_a_kill_left_half_made(self):
        self.assertEqual(self.store.list(), [])
        for name in ("tailnet-api-key", "bmc/moose/user", "github-token"):
            self.store.put(name, "v")
        with open(self.dir + "/github-token.tmp", "w") as f:
            f.write("half")
        self.assertEqual(self.store.list(), ["bmc/moose/user", "github-token", "tailnet-api-key"])

    def test_the_value_is_never_in_an_argv(self):
        w = World(here="vault")
        store = Files(w.machines["vault"], "/s")
        store.put("github-token", "s3cret")
        self.assertEqual(([c.argv for c in w.machines["vault"].calls], w.machines["vault"].files),
                         ([("mkdir", "-p", "-m", "700", "/s")], {"/s/github-token": "s3cret\n"}))
        self.assertEqual(w.machines["vault"].modes, {"/s/github-token": "600"})


class TestPutIsAnEffect(unittest.TestCase):
    def test_a_dry_run_prints_the_put_and_writes_nothing(self):
        w = World(commands=[toy], here="lin", seams={"secrets": lambda inv: Files(inv.here, "/s")})
        r = w.wk("--dry-run", "seamy", "x")
        self.assertEqual((r.code, w.machines["lin"].files), (0, {}))
        self.assertEqual([line for line in r.plan if "/s" in line], ["[lin] mkdir -p -m 700 /s", "[lin] write /s/token"])


class TestConnect(unittest.TestCase):
    def test_the_real_tree_wires_the_seam_to_this_machines_wk_home(self):
        w = World()
        inv = Inv("/", {"WK_HOME": "/w"}, Fleet(lambda: w.confs, "vault", None, w.machines["vault"]), None, None, {}, SEAMS)
        store = inv.secrets()
        self.assertEqual((type(store), store.m, store.dir), (Files, w.machines["vault"], "/w/secrets"))


class TestKeyLs(unittest.TestCase):
    def test_it_prints_the_names_one_to_a_line_which_is_what_the_playbook_reads(self):
        w = World(here="vault")
        w.secrets.values.update({"github-token": "t", "bmc/lin/user": "k"})
        r = w.wk("key", "ls")
        self.assertEqual((r.code, r.out, r.effects), (0, "bmc/lin/user\ngithub-token\n", []))
        self.assertEqual(World(here="vault").wk("key", "ls").out, "")


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
