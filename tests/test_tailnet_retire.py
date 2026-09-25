"""The tailnet API (lib/wk/tailnet.py, `wk key set tailnet-api`): retiring a fleet node, the one
administrative act wk asks of the tailnet's control plane, and the credential check, key liveness and
key minting beside it.

A board is reached by its tailnet name and nothing about how to reach it is written down, so a card
written for a board whose old node still holds that name joins renamed and is unreachable. Retiring the
leftover keeps a reprovision from stopping at a person with a browser; the gate is an exact name match,
and never a node that is online.

Every request crosses `Api.transport`, which a FakeTailnet answers here; one test drives the real
urllib transport against a loopback server.

Run: python3 tests/run.py --unit -k test_tailnet_retire
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))
from wk import tailnet  # noqa: E402


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


class TailnetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-tsapi-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.key = self.tmp / "api-key"
        self.key.write_text("tskey-api-abc123\n")

    def run_main(self, fake, *args, key=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"WK_TS_API_SECRET_FILE": str(self.key if key is None else key)}
        stderr, sys.stderr = sys.stderr, err
        try:
            rc = tailnet.main(list(args), env=env, out=out, transport=fake)
        finally:
            sys.stderr = stderr
        return rc, out.getvalue(), err.getvalue()

    def fleet(self, fake, api=True, authkey=None):
        """wk.tailnet.Fleet over this host, its credentials in the test's own directory."""
        env = {"HOME": str(self.tmp), "WK_TS_API_SECRET": str(self.key if api else self.tmp / "no-api"),
               "WK_TS_AUTHKEY": str(self.tmp / "authkey"), "WK_TAILNET_API": tailnet.API}
        if authkey is not None:
            (self.tmp / "authkey").write_text(authkey + "\n")
        return tailnet.Fleet(REPO, env, transport=fake)

    def quiet(self, fn, *args):
        err = io.StringIO()
        stderr, sys.stderr = sys.stderr, err
        try:
            return fn(*args), err.getvalue()
        finally:
            sys.stderr = stderr


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

    def test_a_prefix_is_not_a_match(self):
        fake = FakeTailnet([node("111", "rpi3-bench", "rpi3-bench"), node("222", "rpi3-rescue", "rpi3-rescue")])
        self.assertEqual(self.retire(fake, "rpi3").rc, 2)
        self.assertEqual(fake.deleted, [])

    def test_a_renamed_node_is_found_by_either_label(self):
        fake = FakeTailnet([node("333", "rpi3-bench", "buildroot")])
        self.assertEqual(self.retire(fake, "rpi3-bench").rc, 0)
        self.assertEqual(fake.deleted, ["333"])

    def test_no_such_node_deletes_nothing(self):
        fake = FakeTailnet()
        self.assertEqual(self.retire(fake, "rpi9-bench").rc, 2)
        self.assertEqual(fake.deleted, [])

    def test_without_the_api_credential_nothing_is_asked(self):
        fake = FakeTailnet([node("111", "rpi3-bench", "rpi3-bench")])
        r, _ = self.quiet(self.fleet(fake, api=False).retire, "rpi3-bench")
        self.assertEqual(r.rc, 4)
        self.assertEqual(fake.asked, [])


class TestCredential(TailnetTest):
    def test_a_refused_credential_names_the_rotation(self):
        rc, _out, err = self.run_main(FakeTailnet(status=401), "check")
        self.assertEqual(rc, 5, err)
        self.assertIn("wk key set tailnet-api --replace", err)

    def test_an_auth_key_is_not_an_api_token(self):
        """the two credentials are spelled alike; the wrong one is refused before anything is sent."""
        bad = self.tmp / "wrong"
        bad.write_text("tskey-auth-abc-def\n")
        fake = FakeTailnet()
        self.assertEqual(self.run_main(fake, "check", key=bad)[0], 4)
        self.assertEqual(fake.asked, [])

    def test_a_missing_credential_is_its_own_exit_code(self):
        self.assertEqual(self.run_main(FakeTailnet(), "check", key=self.tmp / "nope")[0], 4)

    def test_an_unreachable_api_is_its_own_exit_code(self):
        def down(*_a):
            raise tailnet.Unreachable("no route")
        self.assertEqual(self.run_main(down, "check")[0], 6)

    def test_the_credential_is_sent_as_basic_auth(self):
        fake = FakeTailnet()
        self.assertEqual(self.run_main(fake, "check")[0], 0)
        self.assertEqual(fake.asked[0][2]["Authorization"], "Basic dHNrZXktYXBpLWFiYzEyMzo=")

    def test_an_unknown_verb_is_the_usage(self):
        self.assertEqual(self.run_main(FakeTailnet(), "retire", "x")[0], 1)


class TestAuthKey(TailnetTest):
    """The one auth key a machine joins nodes with: a stored key while the tailnet still has it, else a mint by the
    machine holding the API credential, else a refusal naming `wk key set tailnet`."""

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
        fake = FakeTailnet()
        path, _ = self.quiet(self.fleet(fake).authkey)
        self.assertEqual(path, str(self.tmp / "authkey"))
        create = fake.asked[0][3]["capabilities"]["devices"]["create"]
        self.assertEqual((create["reusable"], create["ephemeral"], create["preauthorized"], create["tags"]),
                         (True, False, True, ["tag:wk"]))

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
        cp = subprocess.run(["bash", "-c", '. "$WK_ROOT/lib/common.sh"; wk_tailscale_authkey'], capture_output=True, text=True,
                            env=dict(os.environ, WK_ROOT=str(REPO), HOME=str(self.tmp), WK_TS_AUTHKEY=str(self.tmp / "authkey"),
                                     WK_TS_API_SECRET=str(self.tmp / "no-api")))
        self.assertEqual(cp.returncode, 1, cp.stderr)
        (self.tmp / "authkey").write_text("tskey-auth-kAAAA-secret\n")
        cp = subprocess.run(["bash", "-c", '. "$WK_ROOT/lib/common.sh"; wk_tailscale_authkey'], capture_output=True, text=True,
                            env=dict(os.environ, WK_ROOT=str(REPO), HOME=str(self.tmp), WK_TS_AUTHKEY=str(self.tmp / "authkey"),
                                     WK_TS_API_SECRET=str(self.tmp / "no-api")))
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

    def test_the_module_runs_as_a_program(self):
        """lib/common.sh and lib/credcheck.py call `python3 -m wk.tailnet`."""
        cp = subprocess.run([sys.executable, "-m", "wk.tailnet", "check"], capture_output=True, text=True,
                            env=dict(os.environ, PYTHONPATH=str(REPO / "lib"), WK_TS_API_SECRET_FILE=str(self.tmp / "nope")))
        self.assertEqual(cp.returncode, 4, cp.stderr)


class TestWiring(unittest.TestCase):
    def test_the_write_retires_rather_than_naming_the_console(self):
        text = (REPO / "lib" / "wk" / "sysimage" / "write.py").read_text()
        self.assertIn(".retire(name)", text)
        self.assertIn("wk key set tailnet-api", text,
                      "the refusal without a token must name the remedy that ends the hand step")

    def test_the_api_token_is_refused_where_an_auth_key_is_wanted(self):
        """it administers the whole tailnet and is copied nowhere: a card must
        never carry it."""
        rules = (REPO / "lib" / "credcheck.py").read_text()
        self.assertIn('key.startswith("tskey-api-")', rules)
        tailnet = (REPO / "lib" / "wk" / "tailnet.py").read_text()
        self.assertIn('usable("tailnet-api", key)', tailnet)

    def test_doctor_declares_it_machine_local(self):
        """One path, Secrets.cred_path's: doctor reads it from there."""
        from tests.support import clean_env
        from tests.test_doctor import UNK, doctor
        key = str(Path(tempfile.mkdtemp(prefix="wk-test-tailnet-")) / "api-key")
        env = clean_env({"WK_TS_API_SECRET": key, "WK_IN_VM": "1"})
        self.assertEqual(key, doctor.Doctor(str(REPO), env=env).paths()["tailscale_api"])
        rows = [r for r in doctor.Doctor(str(REPO), env=env).machine_local() if r[1].startswith(key)]
        self.assertEqual(1, len(rows), rows)
        self.assertEqual(UNK, rows[0][0])
        self.assertIn("re-authable: wk key set tailnet-api", rows[0][2])


if __name__ == "__main__":
    unittest.main()
