"""Retiring a fleet node (lib/tailnet.py, `wk key tailnet-api`): the one
administrative act wk asks of the tailnet's control plane.

A board is reached by its tailnet name and nothing about how to reach it is
written down, so a card written for a board whose old node still holds that
name joins renamed and is unreachable. Retiring the leftover is what keeps a
reprovision from stopping at a person with a browser -- and the gate below
is what keeps that narrow: an exact name match, and never a node that is
online, which is a running board rather than a leftover.

The API is stubbed with a local HTTP server, so these drive the real
request-making code without a tailnet.

Run: python3 -m unittest tests.test_tailnet_retire -v
"""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TAILNET_PY = REPO / "lib" / "tailnet.py"


class FakeTailnet(BaseHTTPRequestHandler):
    devices = []
    deleted = []
    auth_fails = False

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if FakeTailnet.auth_fails:
            return self._send(401, {"message": "unauthorized"})
        if self.path.endswith("/devices"):
            return self._send(200, {"devices": FakeTailnet.devices})
        self._send(404, {})

    def do_DELETE(self):
        if FakeTailnet.auth_fails:
            return self._send(401, {"message": "unauthorized"})
        FakeTailnet.deleted.append(self.path.rsplit("/", 1)[-1])
        self._send(200, {})

    def log_message(self, *a):
        pass


class TestRetire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeTailnet)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeTailnet.devices = []
        FakeTailnet.deleted = []
        FakeTailnet.auth_fails = False
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-tsapi-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.key = self.tmp / "api-key"
        self.key.write_text("tskey-api-abc123\n")

    def _run(self, *args, key=None):
        env = dict(os.environ)
        env["WK_TS_API_SECRET_FILE"] = str(self.key if key is None else key)
        # The stub speaks plain HTTP on loopback, named through the same
        # variable lib/credcheck.py points at it with.
        env["WK_TAILNET_API"] = f"http://127.0.0.1:{self.port}/api/v2"
        return subprocess.run(["python3", str(TAILNET_PY), *args],
                              capture_output=True, text=True, env=env)

    def test_an_offline_leftover_is_retired(self):
        FakeTailnet.devices = [
            {"id": "111", "name": "rpi3-bench.tailnet.ts.net", "hostname": "rpi3-bench",
             "online": False, "lastSeen": "2026-08-31T19:00:00Z"},
        ]
        cp = self._run("retire", "rpi3-bench")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(FakeTailnet.deleted, ["111"])
        self.assertIn("retired", cp.stdout)

    def test_an_online_node_is_never_retired(self):
        """a live node of that name is a running board, and evicting one is
        the more expensive accident."""
        FakeTailnet.devices = [
            {"id": "111", "name": "rpi3-bench.tailnet.ts.net", "hostname": "rpi3-bench",
             "online": True, "lastSeen": "now"},
        ]
        cp = self._run("retire", "rpi3-bench")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertEqual(FakeTailnet.deleted, [])
        self.assertIn("online right now", cp.stderr)

    def test_a_prefix_is_not_a_match(self):
        """retiring 'rpi3' must not take 'rpi3-bench' with it."""
        FakeTailnet.devices = [
            {"id": "111", "name": "rpi3-bench.tailnet.ts.net", "hostname": "rpi3-bench",
             "online": False, "lastSeen": "x"},
            {"id": "222", "name": "rpi3-rescue.tailnet.ts.net", "hostname": "rpi3-rescue",
             "online": False, "lastSeen": "x"},
        ]
        cp = self._run("retire", "rpi3")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertEqual(FakeTailnet.deleted, [])

    def test_a_renamed_node_is_found_by_either_label(self):
        """a rename leaves the API's name and hostname disagreeing; the node
        still holds the name as far as a new join is concerned."""
        FakeTailnet.devices = [
            {"id": "333", "name": "rpi3-bench.tailnet.ts.net", "hostname": "buildroot",
             "online": False, "lastSeen": "x"},
        ]
        cp = self._run("retire", "rpi3-bench")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(FakeTailnet.deleted, ["333"])

    def test_no_such_node_says_so_and_deletes_nothing(self):
        cp = self._run("retire", "rpi9-bench")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertEqual(FakeTailnet.deleted, [])

    def test_a_refused_credential_names_the_rotation(self):
        FakeTailnet.auth_fails = True
        cp = self._run("check")
        self.assertEqual(cp.returncode, 5, cp.stdout + cp.stderr)
        self.assertIn("wk key tailnet-api --replace", cp.stderr)

    def test_an_auth_key_is_not_an_api_token(self):
        """the two credentials are spelled alike and do very different
        things; the wrong one here is refused rather than sent."""
        bad = self.tmp / "wrong"
        bad.write_text("tskey-auth-abc-def\n")
        cp = self._run("check", key=bad)
        self.assertEqual(cp.returncode, 4, cp.stdout + cp.stderr)
        self.assertIn("tskey-api-", cp.stderr)

    def test_a_missing_credential_is_its_own_exit_code(self):
        cp = self._run("check", key=self.tmp / "nope")
        self.assertEqual(cp.returncode, 4, cp.stdout + cp.stderr)


class TestWiring(unittest.TestCase):
    def test_the_write_retires_rather_than_naming_the_console(self):
        text = (REPO / "cmd" / "sysimage").read_text()
        self.assertIn("wk_tailnet_retire", text)
        self.assertIn("wk key tailnet-api", text,
                      "the refusal without a token must name the remedy that ends the hand step")

    def test_the_api_token_is_refused_where_an_auth_key_is_wanted(self):
        """it administers the whole tailnet and is copied nowhere: a card must
        never carry it."""
        rules = (REPO / "lib" / "credcheck.py").read_text()
        self.assertIn('key.startswith("tskey-api-")', rules)
        common = (REPO / "lib" / "common.sh").read_text()
        self.assertIn("wk_tailscale_api_reject", common)

    def test_doctor_declares_it_machine_local(self):
        text = (REPO / "cmd" / "doctor").read_text()
        self.assertIn("$(wk_tailscale_api_path)", text)  # one path, lib/common.sh's
        self.assertIn("re-authable", text)


if __name__ == "__main__":
    unittest.main()
