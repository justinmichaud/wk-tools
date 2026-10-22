"""The services wk calls with a credential, each against a local stub: lib/wk/notify.py's ntfy publish (`WK_NTFY_API`)
and lib/wk/tailnet.py's tailnet API (retiring a fleet node, the credential check, key liveness and minting). Every
topic and key here is a placeholder."""
import contextlib
import credcheck
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tests.fakes import FlowTest
from tests.support import REPO, WkTest, clean_env

sys.path.insert(0, str(REPO / "lib"))
from wk import act, notify, tailnet  # noqa: E402
from wk.machine import Local  # noqa: E402


NOTIFY = REPO / "lib" / "wk" / "notify.py"
TOPIC = "placeholder-topic-for-this-test"
SHORT = "short"
DOWN = "http://127.0.0.1:1"


class FakeNtfy(BaseHTTPRequestHandler):
    """`POST /` takes the JSON publishing format; `GET /<topic>/json?poll=1` answers `get_status`."""

    post_status = 200
    get_status = 200
    published = []

    def _send(self, code, body=b""):
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "https://ntfy.sh/docs/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeNtfy.published.append(json.loads(raw or b"{}"))
        if FakeNtfy.post_status != 200:
            return self._send(FakeNtfy.post_status, json.dumps(
                {"error": "topic %s is not allowed" % FakeNtfy.published[-1].get("topic")}).encode())
        self._send(200, json.dumps({"id": "x", "event": "message"}).encode())

    def do_GET(self):
        self._send(FakeNtfy.get_status)

    def log_message(self, *a):
        pass


class _Ntfy(WkTest):
    def setUp(self):
        super().setUp()
        FakeNtfy.post_status, FakeNtfy.get_status, FakeNtfy.published = 200, 200, []
        self.server = HTTPServer(("127.0.0.1", 0), FakeNtfy)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patch = mock.patch.dict(os.environ, {"WK_NTFY_API": self.url})
        patch.start()
        self.addCleanup(patch.stop)

    def at(self, url):
        return mock.patch.dict(os.environ, {"WK_NTFY_API": url})


class TestItPublishesOrSaysWhyNot(_Ntfy):
    def test_a_headline_alone_is_the_message(self):
        self.assertEqual((0, ""), notify.publish(TOPIC, "the plant is waiting"))
        self.assertEqual([{"topic": TOPIC, "message": "the plant is waiting"}], FakeNtfy.published)

    def test_a_detail_makes_the_headline_the_title(self):
        self.assertEqual(0, notify.publish(TOPIC, "arm B stalled", "2727s of silence", "warning")[0])
        self.assertEqual([{"topic": TOPIC, "title": "arm B stalled", "message": "2727s of silence", "tags": ["warning"]}],
                         FakeNtfy.published)

    def test_a_refused_publish_is_not_reported_as_one(self):
        FakeNtfy.post_status = 400
        code, why = notify.publish(TOPIC, "the plant is waiting")
        self.assertEqual(5, code)
        self.assertIn("HTTP 400", why)

    def test_no_network_is_not_reported_as_a_publish(self):
        with self.at(DOWN):
            code, why = notify.publish(TOPIC, "the plant is waiting")
        self.assertEqual(6, code)
        self.assertEqual([], FakeNtfy.published)

    def test_an_address_that_is_no_https_url_is_refused_naming_the_variable(self):
        with self.at("ntfy.example"), self.assertRaises(SystemExit) as cm:
            notify.publish(TOPIC, "the plant is waiting")
        self.assertIn("WK_NTFY_API=ntfy.example", str(cm.exception))
        self.assertEqual([], FakeNtfy.published)

    def test_a_topic_that_is_not_one_is_refused_before_the_network(self):
        self.assertEqual(4, notify.publish("not one word", "x")[0])
        self.assertEqual([], FakeNtfy.published)


class TestTheRuleJudgesWhatTheTopicCanDo(_Ntfy):
    def test_a_topic_ntfy_serves_is_acceptable(self):
        code, why = notify.check(TOPIC)
        self.assertEqual(0, code, why)

    def test_a_guessable_name_is_wide_rather_than_acceptable(self):
        self.assertEqual(3, notify.check(SHORT)[0])

    def test_a_name_ntfy_carries_no_topic_by_is_bad(self):
        for status in (302, 404):
            with self.subTest(status=status):
                FakeNtfy.get_status = status
                self.assertEqual(5, notify.check(TOPIC)[0])

    def test_a_rate_limit_or_an_outage_is_not_a_bad_credential(self):
        for status in (429, 503):
            with self.subTest(status=status):
                FakeNtfy.get_status = status
                self.assertEqual(6, notify.check(TOPIC)[0])

    def test_nothing_stored_and_a_non_topic_are_refused_before_the_network(self):
        with self.at(DOWN):
            self.assertEqual(4, notify.check("")[0])
            self.assertEqual(4, notify.check("not one word")[0])

    def _verdict(self, topic, url=None):
        with mock.patch.dict(os.environ, {"WK_NTFY_API": url or self.url}):
            return credcheck.verdict("ntfy", topic).split("\t", 1)

    def test_the_credential_rule_reaches_the_same_verdicts(self):
        for topic, want in ((TOPIC, "ok"), (SHORT, "wide")):
            with self.subTest(topic=topic):
                self.assertEqual(want, self._verdict(topic)[0])
        FakeNtfy.get_status = 302
        self.assertEqual("bad", self._verdict(TOPIC)[0])
        FakeNtfy.get_status = 503
        self.assertEqual("unverified", self._verdict(TOPIC)[0])
        self.assertEqual("unverified", self._verdict(TOPIC, url=DOWN)[0])


class TestTheTopicIsMintedNotInvented(_Ntfy):
    def test_a_minted_topic_is_one_word_the_rule_calls_unguessable(self):
        topic = notify.mint()
        self.assertRegex(topic, notify.TOPIC.pattern)
        self.assertGreaterEqual(len(topic), notify.GUESSABLE)
        self.assertEqual(0, notify.check(topic)[0])

    def test_the_rule_mints_through_this_module(self):
        self.assertRegex(credcheck.rules()["ntfy"].mint().strip(), notify.TOPIC.pattern)


class TestTheLibraryCall(_Ntfy):
    def env(self):
        return clean_env({"WK_STORE": str(self.tmp / "store"), "WK_NTFY_API": self.url,
                          "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets")})

    def store_topic(self, topic=TOPIC):
        d = self.tmp / "store" / "notify"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ntfy-topic").write_text(topic + "\n")
        (d / "ntfy-topic").chmod(0o600)

    def send(self, *args):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = notify.send(REPO, *args, env=self.env(), machine=Local())
        return ok, err.getvalue()

    def test_no_topic_here_names_the_remedy_and_publishes_nothing(self):
        ok, err = self.send("the plant is waiting")
        self.assertFalse(ok)
        self.assertIn("wk key set ntfy", err)
        self.assertEqual([], FakeNtfy.published)

    def test_a_stored_topic_publishes(self):
        self.store_topic()
        ok, err = self.send("the plant is waiting", "round 17")
        self.assertTrue(ok, err)
        self.assertEqual([{"topic": TOPIC, "title": "the plant is waiting", "message": "round 17"}], FakeNtfy.published)
        self.assertNotIn(TOPIC, err)

    def test_a_refused_publish_is_false_with_a_reason_and_no_topic(self):
        self.store_topic()
        FakeNtfy.post_status = 400
        ok, err = self.send("the plant is waiting")
        self.assertFalse(ok)
        self.assertNotIn(TOPIC, err)


class TestTheCredentialIsDeclaredWhereARebuildLooks(unittest.TestCase):
    def test_the_machine_local_section_names_the_topic(self):
        from tests.test_doctor import fake_doctor
        doc = fake_doctor(True)
        rows = [w + " -> " + r for _, w, r in doc.machine_local()]
        path = doc.paths()["ntfy_topic"]   # under HOME only where Store.macos_host (a Darwin uname) puts the keyring there
        shown = "~" + path[len(doc.home):] if path.startswith(doc.home) else path
        line = [l for l in rows if l.startswith(shown + " ")]
        self.assertEqual(1, len(line), rows)
        self.assertIn("re-authable", line[0])
        self.assertIn("wk key set ntfy", line[0])

    def test_the_topic_path_doctor_reads_is_the_library_s(self):
        from wk import doctor
        from wk.secrets import Secrets
        env = clean_env({"WK_STORE": "/scratch/store", "WK_HOST_SECRETS": "/scratch/store/secrets"})
        self.assertEqual(Secrets(REPO, env=env).cred_path("ntfy"), doctor.Doctor(str(REPO), env=env).paths()["ntfy_topic"])

    def test_it_is_not_in_a_directory_a_workspace_can_read(self):
        from wk.store import Store
        store = Store(clean_env({"WK_STORE": "/scratch/store", "WK_HOST_SECRETS": "/scratch/store/secrets"}))
        for held in (store.keyring_ntfy_topic(), store.keyring_claude_login()):
            for mounted in (store.keyring_dir(), store.keyring_agent_rw_dir()):
                self.assertFalse(held.startswith(mounted + "/"), held)


class TestSdNotify(unittest.TestCase):
    def test_it_is_a_no_op_without_the_socket(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(notify.sd_notify("READY=1"))

    def test_it_sends_the_state_to_the_socket_it_is_given(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "notify.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            srv.bind(path)
            srv.settimeout(30)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                notify.sd_notify("READY=1")
            self.assertEqual(b"READY=1", srv.recv(64))
            srv.close()

    def test_the_module_imports_from_a_bare_python3(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(NOTIFY, d)
            cp = subprocess.run([sys.executable, "-S", "-c", "import notify; notify.sd_notify('READY=1')"],
                                cwd=d, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(0, cp.returncode, cp.stderr)


class FakeTailnet:
    """The control plane as a transport: devices, keys, and every request it was asked."""

    def __init__(self, devices=(), keys=(), status=None):
        self.devices, self.keys, self.status = list(devices), list(keys), status
        self.deleted, self.asked = [], []

    def __call__(self, method, url, headers, data):
        path = url[len(tailnet.API):]
        self.asked.append((method, path, headers, json.loads(data) if data else None))
        if self.status:
            return self.status, b'{"message": "no"}'
        if method == "GET" and path == "/tailnet/-/devices":
            return 200, json.dumps({"devices": self.devices}).encode()
        if method == "GET" and path == "/tailnet/-/keys":
            return 200, json.dumps({"keys": self.keys}).encode()
        if method == "POST" and path == "/tailnet/-/keys":
            return 200, json.dumps({"key": "tskey-auth-k1-minted"}).encode()
        if method == "DELETE" and path.startswith("/device/"):
            self.deleted.append(path.rsplit("/", 1)[-1])
            return 200, b""
        return 404, b"{}"


def node(id, name, hostname, online=False):
    return {"id": id, "name": name + ".tailnet.ts.net", "hostname": hostname, "online": online, "lastSeen": "x"}


class TailnetTest(FlowTest):
    def setUp(self):
        super().setUp()
        self.key = self.tmp / "api-key"
        self.key.write_text("tskey-api-abc123\n")

    def run_main(self, fake, *args, key=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"WK_TS_API_SECRET_FILE": str(self.key if key is None else key)}
        with contextlib.redirect_stderr(err):
            rc = tailnet.main(list(args), env=env, out=out, transport=fake)
        return rc, out.getvalue(), err.getvalue()

    def fleet(self, fake, api=True, authkey=None, **more):
        """wk.tailnet.Fleet over this host, its credentials in the test's own directory."""
        env = {**more, "HOME": str(self.tmp), "WK_TS_API_SECRET": str(self.key if api else self.tmp / "no-api"),
               "WK_TS_AUTHKEY": str(self.tmp / "authkey"), "WK_TAILNET_API": tailnet.API}
        if authkey is not None:
            (self.tmp / "authkey").write_text(authkey + "\n")
        return tailnet.Fleet(REPO, env, transport=fake)

    def quiet(self, fn, *args):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = fn(*args)
        return result, err.getvalue()


class TestRetire(TailnetTest):
    def retire(self, fake, name):
        r, _ = self.quiet(self.fleet(fake).retire, name)
        return r

    def test_an_offline_leftover_is_retired(self):
        fake = FakeTailnet([node("111", "rpi3-bench", "rpi3-bench")])
        r = self.retire(fake, "rpi3-bench")
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(fake.deleted, ["111"])
        self.assertIn("retired", r.out)

    def test_an_online_node_is_never_retired(self):
        fake = FakeTailnet([node("111", "rpi3-bench", "rpi3-bench", online=True)])
        self.assertEqual(self.retire(fake, "rpi3-bench").rc, 3)
        self.assertEqual(fake.deleted, [])

    def test_a_prefix_or_no_such_node_is_not_a_match_and_deletes_nothing(self):
        for devices, name in (([node("111", "rpi3-bench", "rpi3-bench"), node("222", "rpi3-rescue", "rpi3-rescue")], "rpi3"),
                              ([], "rpi9-bench")):
            with self.subTest(name=name):
                fake = FakeTailnet(devices)
                self.assertEqual(self.retire(fake, name).rc, 2)
                self.assertEqual(fake.deleted, [])

    def test_a_renamed_node_is_found_by_either_label(self):
        fake = FakeTailnet([node("333", "rpi3-bench", "buildroot")])
        self.assertEqual(self.retire(fake, "rpi3-bench").rc, 0)
        self.assertEqual(fake.deleted, ["333"])

    def test_without_the_api_credential_nothing_is_asked(self):
        fake = FakeTailnet([node("111", "rpi3-bench", "rpi3-bench")])
        r, _ = self.quiet(self.fleet(fake, api=False).retire, "rpi3-bench")
        self.assertEqual(r.rc, 4)
        self.assertEqual(fake.asked, [])

    def test_a_stored_credential_that_is_no_api_key_is_refused_rather_than_called_missing(self):
        self.key.write_text("tskey-auth-kXYZ-abc\n")
        fake = FakeTailnet([node("111", "rpi3-bench", "rpi3-bench")])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(act.Refused) as cm:
            self.fleet(fake).retire("rpi3-bench")
        self.assertEqual(cm.exception.status, 4)
        self.assertIn("does not hold a tailnet API access key", err.getvalue())
        self.assertIn("wk key set tailnet-api --replace", err.getvalue())
        self.assertEqual(fake.asked, [])


class TestCredential(TailnetTest):
    def test_a_refused_credential_names_the_rotation(self):
        rc, _out, err = self.run_main(FakeTailnet(status=401), "check")
        self.assertEqual(rc, 5, err)
        self.assertIn("wk key set tailnet-api --replace", err)

    def test_an_auth_key_or_no_key_is_no_api_token_and_nothing_is_asked(self):
        bad = self.tmp / "wrong"
        bad.write_text("tskey-auth-abc-def\n")
        for key in (bad, self.tmp / "nope"):
            with self.subTest(key=key.name):
                fake = FakeTailnet()
                self.assertEqual(self.run_main(fake, "check", key=key)[0], 4)
                self.assertEqual(fake.asked, [])

    def test_an_unreachable_api_is_its_own_exit_code(self):
        def down(*_a):
            raise tailnet.Unreachable("no route")
        self.assertEqual(self.run_main(down, "check")[0], 6)

    def test_the_credential_is_sent_as_basic_auth(self):
        fake = FakeTailnet()
        self.assertEqual(self.run_main(fake, "check")[0], 0)
        self.assertEqual(fake.asked[0][2]["Authorization"], "Basic dHNrZXktYXBpLWFiYzEyMzo=")

    def test_an_unknown_verb_is_the_usage(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(FakeTailnet(), "retire", "x")
        self.assertEqual(cm.exception.code, 2)


class TestAuthKey(TailnetTest):
    """The one auth key a machine joins nodes with: a stored key while the tailnet still has it, else a mint by
    the machine holding the API credential, else a refusal naming `wk key set tailnet`."""

    def test_a_stored_key_is_used_as_it_is_where_nothing_can_mint(self):
        fake = FakeTailnet()
        path, _ = self.quiet(self.fleet(fake, api=False, authkey="tskey-auth-kAAAA-secret").authkey)
        self.assertEqual(path, str(self.tmp / "authkey"))
        self.assertEqual(fake.asked, [])

    def test_a_stored_key_the_tailnet_still_has_is_used(self):
        fake = FakeTailnet(keys=[{"id": "kAAAA"}])
        path, _ = self.quiet(self.fleet(fake, authkey="tskey-auth-kAAAA-secret").authkey)
        self.assertEqual(path, str(self.tmp / "authkey"))
        self.assertEqual([a[0] for a in fake.asked], ["GET"])

    def test_a_key_the_tailnet_no_longer_has_is_replaced_by_a_mint(self):
        fake = FakeTailnet(keys=[{"id": "k2"}])
        path, _ = self.quiet(self.fleet(fake, authkey="tskey-auth-kAAAA-secret").authkey)
        self.assertEqual(path, str(self.tmp / "authkey"))
        self.assertEqual((self.tmp / "authkey").read_text(), "tskey-auth-k1-minted\n")
        self.assertEqual(oct((self.tmp / "authkey").stat().st_mode & 0o777), "0o600")

    def test_a_tailnet_that_cannot_be_asked_is_no_evidence_against_a_key(self):
        def down(*_a):
            raise tailnet.Unreachable("no route")
        path, _ = self.quiet(self.fleet(down, authkey="tskey-auth-kAAAA-secret").authkey)
        self.assertEqual(path, str(self.tmp / "authkey"))

    def test_a_minted_key_is_reusable_preauthorized_and_tagged(self):
        for env, tag in (({}, "tag:wk"), ({"WK_TAILNET_TAG": "tag:lab"}, "tag:lab")):
            fake = FakeTailnet()
            path, _ = self.quiet(self.fleet(fake, **env).authkey)
            self.assertEqual(path, str(self.tmp / "authkey"))
            create = fake.asked[0][3]["capabilities"]["devices"]["create"]
            self.assertEqual((create["reusable"], create["ephemeral"], create["preauthorized"], create["tags"]),
                             (True, False, True, [tag]))
            (self.tmp / "authkey").unlink()

    def test_a_refused_mint_names_both_ways_to_a_key(self):
        path, err = self.quiet(self.fleet(FakeTailnet(status=403)).authkey)
        self.assertEqual(path, "")
        self.assertIn("wk key set tailnet-api", err)
        self.assertRegex(err, r"wk key set tailnet(?!-api)")

    def test_no_key_and_no_way_to_mint_is_refused_and_writes_nothing(self):
        path, err = self.quiet(self.fleet(FakeTailnet(), api=False).authkey)
        self.assertEqual(path, "")
        self.assertIn("wk key set tailnet", err)
        self.assertFalse((self.tmp / "authkey").exists())

    def test_a_malformed_key_is_left_in_place(self):
        path, _ = self.quiet(self.fleet(FakeTailnet(), api=False, authkey="tskey-api-wrong").authkey)
        self.assertEqual(path, "")
        self.assertEqual((self.tmp / "authkey").read_text(), "tskey-api-wrong\n")

    def test_a_dry_run_mints_nothing(self):
        fake = FakeTailnet()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            path, _ = self.quiet(self.fleet(fake).authkey)
        self.assertEqual((path, fake.asked), ("", []))

    def test_the_shell_entry_prints_the_path(self):
        env = dict(os.environ, WK_ROOT=str(REPO), HOME=str(self.tmp), WK_TS_AUTHKEY=str(self.tmp / "authkey"),
                   WK_TS_API_SECRET=str(self.tmp / "no-api"))
        entry = lambda: subprocess.run(["bash", "-c", '. "$WK_ROOT/lib/common.sh"; wk_tailscale_authkey'],  # noqa: E731
                                       capture_output=True, text=True, env=env)
        self.assertEqual(entry().returncode, 1)
        (self.tmp / "authkey").write_text("tskey-auth-kAAAA-secret\n")
        cp = entry()
        self.assertEqual((cp.returncode, cp.stdout), (0, str(self.tmp / "authkey")), cp.stderr)


class Loopback(BaseHTTPRequestHandler):
    def do_GET(self):
        raw = json.dumps({"devices": [{"id": "1"}]}).encode()
        self.send_response(200 if self.headers.get("Authorization") else 401)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class TestUrllibTransport(TailnetTest):
    def test_the_real_transport_reaches_an_http_api(self):
        server = HTTPServer(("127.0.0.1", 0), Loopback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        api = tailnet.Api("tskey-api-abc123", "http://127.0.0.1:%d/api/v2" % server.server_port)
        self.assertEqual(api.devices(), [{"id": "1"}])


class TestDoctorRow(unittest.TestCase):
    def test_doctor_declares_it_machine_local(self):
        from tests.support import clean_env
        from tests.test_doctor import UNK, doctor
        key = str(Path(tempfile.mkdtemp(prefix="wk-test-tailnet-")) / "api-key")
        env = clean_env({"WK_TS_API_SECRET": key, "WK_IN_VM": "1"})
        self.assertEqual(key, doctor.Doctor(str(REPO), env=env).paths()["tailscale_api"])
        rows = [r for r in doctor.Doctor(str(REPO), env=env).machine_local() if r[1].startswith(key)]
        self.assertEqual(1, len(rows), rows)
        self.assertEqual(UNK, rows[0][0])
        self.assertIn("re-authable: wk key set tailnet-api", rows[0][2])
