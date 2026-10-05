"""lib/wk/notify.py against a local ntfy stub (`WK_NTFY_API`): `publish` answers 0 only when ntfy took the message,
and a reason naming the remedy every other time. Every topic here is a placeholder."""
import contextlib
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

from tests.support import REPO, WkTest, clean_env

sys.path.insert(0, str(REPO / "lib"))
from wk import notify  # noqa: E402
from wk.machine import Local  # noqa: E402

NOTIFY = REPO / "lib" / "wk" / "notify.py"
CREDCHECK = REPO / "lib" / "credcheck.py"
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
        cp = subprocess.run(["python3", str(CREDCHECK), "check", "ntfy"], input=topic, capture_output=True, text=True,
                            timeout=60, env={"WK_NTFY_API": url or self.url, "PATH": "/usr/bin:/bin"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return cp.stdout.split("\t", 1)

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
        cp = subprocess.run(["python3", str(CREDCHECK), "mint", "ntfy"], capture_output=True, text=True, timeout=60,
                            env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertRegex(cp.stdout.strip(), notify.TOPIC.pattern)


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


if __name__ == "__main__":
    unittest.main()
