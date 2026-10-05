"""The claude.ai login: the injector's Holder refreshes it once however many requests wait, and a workspace holds
lib/wk/claudelogin.py's placeholder; against a loopback token endpoint, and live against the real one."""
import json
import os
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, owed, requires_container_place
from tests.test_credcheck import JsonHandler, serve
from tests.test_egress import INJECT, _load

sys.path.insert(0, str(REPO / "lib"))
from wk import claudelogin  # noqa: E402

NOW = 1_800_000_000.0
INJECTOR = _load(INJECT, "wkinject_login")


class FakeTokenEndpoint(JsonHandler):
    status = 200
    answer = {}
    seen = []
    delay = 0.0

    def do_POST(self):
        FakeTokenEndpoint.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        time.sleep(FakeTokenEndpoint.delay)
        self._send(FakeTokenEndpoint.status, FakeTokenEndpoint.answer)


class _Login(WkTest):
    def setUp(self):
        super().setUp()
        self.url = serve(FakeTokenEndpoint, self.addCleanup) + "/v1/oauth/token"
        FakeTokenEndpoint.status, FakeTokenEndpoint.seen, FakeTokenEndpoint.delay = 200, [], 0.0
        FakeTokenEndpoint.answer = {"access_token": "sk-ant-oat01-NEW", "refresh_token": "sk-ant-ort01-NEW",
                                    "expires_in": 28800, "scope": "user:inference user:profile"}
        self.path = self.tmp / ".credentials.json"

    def store(self, expires_at, **extra):
        self.path.write_text(json.dumps(dict({"claudeAiOauth": {"accessToken": "sk-ant-oat01-OLD",
                                                                "refreshToken": "sk-ant-ort01-OLD",
                                                                "expiresAt": expires_at}}, **extra)))

    def holder(self):
        return INJECTOR.Holder(str(self.path), self.url, clock=lambda: NOW)

    def held(self):
        return json.loads(self.path.read_text())


class TestTheHolderRefreshes(_Login):
    def test_a_token_that_outlasts_the_margin_is_used_and_nothing_is_asked(self):
        self.store(NOW * 1000 + INJECTOR.MARGIN_MS + 60000)
        self.assertEqual("sk-ant-oat01-OLD", self.holder().access_token())
        self.assertEqual([], FakeTokenEndpoint.seen)

    def test_one_inside_the_margin_is_refreshed_with_the_refresh_token_and_stored(self):
        self.store(NOW * 1000 + 1000, other="kept")
        self.assertEqual("sk-ant-oat01-NEW", self.holder().access_token())
        self.assertEqual([{"grant_type": "refresh_token", "refresh_token": "sk-ant-ort01-OLD",
                           "client_id": INJECTOR.CLIENT_ID}], FakeTokenEndpoint.seen)
        oauth = self.held()["claudeAiOauth"]
        self.assertEqual(("sk-ant-oat01-NEW", "sk-ant-ort01-NEW", int((NOW + 28800) * 1000), ["user:inference", "user:profile"]),
                         (oauth["accessToken"], oauth["refreshToken"], oauth["expiresAt"], oauth["scopes"]))
        self.assertEqual("kept", self.held()["other"])
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_an_answer_without_a_new_refresh_token_keeps_the_old_one(self):
        self.store(0)
        del FakeTokenEndpoint.answer["refresh_token"]
        self.holder().access_token()
        self.assertEqual("sk-ant-ort01-OLD", self.held()["claudeAiOauth"]["refreshToken"])

    def test_concurrent_requests_refresh_once(self):
        self.store(0)
        FakeTokenEndpoint.delay = 0.3
        got = []
        holder = self.holder()
        threads = [threading.Thread(target=lambda: got.append(holder.access_token())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(["sk-ant-oat01-NEW"] * 4, got)
        self.assertEqual(1, len(FakeTokenEndpoint.seen))

    def test_a_refused_refresh_names_the_replacement_and_changes_nothing(self):
        self.store(0)
        before = self.path.read_text()
        FakeTokenEndpoint.status, FakeTokenEndpoint.answer = 400, {"error": "invalid_grant"}
        with self.assertRaises(INJECTOR.LoginError) as e:
            self.holder().access_token()
        self.assertIn("'wk key set claude-login --replace'", str(e.exception))
        self.assertEqual(before, self.path.read_text())

    def test_a_failing_endpoint_is_tried_again_on_the_next_request(self):
        self.store(0)
        for status, answer in ((503, {}), (200, {"no": "token"})):
            with self.subTest(status=status):
                FakeTokenEndpoint.status, FakeTokenEndpoint.answer = status, answer
                with self.assertRaises(INJECTOR.LoginError) as e:
                    self.holder().access_token()
                self.assertNotIn("--replace", str(e.exception))
        with self.assertRaises(INJECTOR.LoginError) as e:
            INJECTOR.Holder(str(self.path), "http://127.0.0.1:1/token", clock=lambda: NOW).access_token()
        self.assertIn("it is tried again", str(e.exception))

    def test_an_answer_naming_no_lifetime_is_good_for_the_default_one(self):
        self.store(0)
        del FakeTokenEndpoint.answer["expires_in"]
        holder = self.holder()
        holder.access_token()
        self.assertEqual(int((NOW + INJECTOR.DEFAULT_EXPIRES_S) * 1000), self.held()["claudeAiOauth"]["expiresAt"])
        holder.access_token()
        self.assertEqual(1, len(FakeTokenEndpoint.seen))

    def test_a_failed_refresh_is_not_asked_again_until_the_backoff_has_passed(self):
        for status in (503, 429, 400):
            with self.subTest(status=status):
                self.store(0)
                FakeTokenEndpoint.seen, FakeTokenEndpoint.status, FakeTokenEndpoint.answer = [], status, {}
                now = [NOW]
                holder = INJECTOR.Holder(str(self.path), self.url, clock=lambda: now[0])
                for _ in range(3):
                    with self.assertRaises(INJECTOR.LoginError):
                        holder.access_token()
                self.assertEqual(1, len(FakeTokenEndpoint.seen))
                now[0] += INJECTOR.BACKOFF_S
                with self.assertRaises(INJECTOR.LoginError):
                    holder.access_token()
                self.assertEqual(2, len(FakeTokenEndpoint.seen))

    def test_a_replaced_login_is_tried_at_once_after_a_failure(self):
        self.store(0)
        FakeTokenEndpoint.status = 400
        holder = self.holder()
        with self.assertRaises(INJECTOR.LoginError):
            holder.access_token()
        self.path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-B", "refreshToken": "sk-ant-ort01-B",
                                                           "expiresAt": 0}}))
        FakeTokenEndpoint.status = 200
        self.assertEqual("sk-ant-oat01-NEW", holder.access_token())
        self.assertEqual(["sk-ant-ort01-OLD", "sk-ant-ort01-B"], [r["refresh_token"] for r in FakeTokenEndpoint.seen])

    def test_no_login_or_one_it_cannot_read_names_wk_key_set(self):
        with self.assertRaises(INJECTOR.LoginError) as e:
            self.holder().access_token()
        self.assertIn("'wk key set claude-login'", str(e.exception))
        self.path.write_text(claudelogin.placeholder())
        with self.assertRaises(INJECTOR.LoginError) as e:
            self.holder().access_token()
        self.assertIn("placeholder", str(e.exception))
        self.assertEqual([], FakeTokenEndpoint.seen)


class RotatingTokenEndpoint(JsonHandler):
    """Like the real one: a refresh token is good once, and a second use of it is refused."""
    current, seen, n = "", [], 0

    def do_POST(self):
        got = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["refresh_token"]
        RotatingTokenEndpoint.seen.append(got)
        time.sleep(1.0)
        if got != RotatingTokenEndpoint.current:
            self._send(400, {"error": "invalid_grant"})
            return
        RotatingTokenEndpoint.n += 1
        RotatingTokenEndpoint.current = "sk-ant-ort01-NEW%d" % RotatingTokenEndpoint.n
        self._send(200, {"access_token": "sk-ant-oat01-NEW%d" % RotatingTokenEndpoint.n,
                         "refresh_token": RotatingTokenEndpoint.current, "expires_in": 28800})


CHILD = """import importlib.util, sys
spec = importlib.util.spec_from_file_location("inj", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    print(m.Holder(sys.argv[2], sys.argv[3]).access_token())
except m.LoginError as e:
    print("refused:", e)
"""


class TestTwoProcessesRefreshOnce(WkTest):
    """Two injectors on one login (an old one launchd has not yet replaced, a restart): the lock beside it is the files'."""

    def test_the_second_finds_the_first_ones_refresh_and_spends_nothing(self):
        RotatingTokenEndpoint.current, RotatingTokenEndpoint.seen, RotatingTokenEndpoint.n = "sk-ant-ort01-OLD", [], 0
        url = serve(RotatingTokenEndpoint, self.addCleanup) + "/v1/oauth/token"
        path = self.tmp / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-OLD", "refreshToken": "sk-ant-ort01-OLD",
                                                      "expiresAt": 0}}))
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(INJECT), str(path), url], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
        got = [p.communicate(timeout=30)[0].strip() for p in procs]
        self.assertEqual(["sk-ant-oat01-NEW1"] * 2, got)
        self.assertEqual(["sk-ant-ort01-OLD"], RotatingTokenEndpoint.seen)
        self.assertEqual("sk-ant-ort01-NEW1", json.loads(path.read_text())["claudeAiOauth"]["refreshToken"])


class TestARewriteSurvivesAPowerLoss(WkTest):
    def test_the_new_file_is_synced_before_its_rename_and_the_directory_after(self):
        from wk import machine
        calls, real_fsync, real_replace = [], os.fsync, os.replace

        def fsync(fd):
            calls.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_fsync(fd)

        def replace(a, b):
            calls.append("rename")
            real_replace(a, b)
        with mock.patch.object(machine.os, "fsync", fsync), mock.patch.object(machine.os, "replace", replace):
            machine.replace_file(str(self.tmp / "login.json"), "{}\n", mode=0o600)
        self.assertEqual(["file", "rename", "dir"], calls)
        self.assertEqual("{}\n", (self.tmp / "login.json").read_text())


class TestWhoHoldsTheLogin(WkTest):
    """Only the injector configured with WK_INJECT_CLAUDE_LOGIN holds it; the podman machine's relays to the Mac's."""

    def test_an_injector_not_named_the_login_holds_none(self):
        self.assertIsNone(INJECTOR.claude_source({"XDG_RUNTIME_DIR": "/run/user/501"}, False))

    def test_the_one_named_it_holds_that_file(self):
        held = INJECTOR.claude_source({"WK_INJECT_CLAUDE_LOGIN": "/k/claude-login/.credentials.json"}, False)
        self.assertIsInstance(held, INJECTOR.Holder)
        self.assertEqual("/k/claude-login/.credentials.json", held.path)

    def test_the_podman_machines_relays_to_the_socket_the_mac_publishes(self):
        fwd = INJECTOR.claude_source({"XDG_RUNTIME_DIR": "/run/user/501"}, True)
        self.assertIsInstance(fwd, INJECTOR.Forward)
        self.assertEqual("/run/user/501/claude-inject.sock", fwd.path)

    def test_the_podman_machines_refuses_to_start_named_a_login(self):
        with self.assertRaises(SystemExit) as e:
            INJECTOR.claude_source({"WK_INJECT_CLAUDE_LOGIN": "/var/lib/wk/agent-rw/.credentials.json"}, True)
        self.assertIn("podman machine", str(e.exception.code))

    def test_loading_it_loads_no_place_code(self):
        cp = subprocess.run([sys.executable, "-c", CHILD.split("try:")[0] + "print('wk.places' in sys.modules)", str(INJECT)],
                            capture_output=True, text=True)
        self.assertEqual("False", cp.stdout.strip(), cp.stderr)


class TestTheLoginMovesOutOfAgentRw(WkTest):
    def secrets(self):
        from wk.secrets import Secrets
        from tests.support import clean_env
        return Secrets(str(REPO), env=clean_env({"WK_STORE": str(self.tmp / "store"),
                                                  "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets")}))

    def test_it_is_renamed_once_beside_the_keyring(self):
        s = self.secrets()
        old = self.tmp / "store" / "agent-rw" / ".credentials.json"
        old.parent.mkdir(parents=True)
        old.write_text("LOGIN\n")
        self.assertEqual("moved", s.claude_login_migrate())
        new = Path(s.cred_path("claude-login"))
        self.assertEqual(("LOGIN\n", False), (new.read_text(), old.exists()))
        self.assertEqual(0o700, new.parent.stat().st_mode & 0o777)
        self.assertEqual("unchanged", s.claude_login_migrate())

    def test_a_second_copy_is_named_and_neither_is_touched(self):
        s = self.secrets()
        old = self.tmp / "store" / "agent-rw" / ".credentials.json"
        new = Path(s.cred_path("claude-login"))
        for p, text in ((old, "OLD\n"), (new, "NEW\n")):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.assertEqual("both", s.claude_login_migrate())
        self.assertEqual(("OLD\n", "NEW\n"), (old.read_text(), new.read_text()))


class TestTheLoginDocument(WkTest):
    def test_the_placeholder_holds_no_token_and_never_expires_for_the_cli(self):
        oauth = json.loads(claudelogin.placeholder())["claudeAiOauth"]
        self.assertEqual({claudelogin.PLACEHOLDER}, {oauth["accessToken"], oauth["refreshToken"]})
        self.assertGreater(oauth["expiresAt"], time.time() * 1000 + 50 * 365 * 86400 * 1000)
        self.assertIn("user:sessions:claude_code", oauth["scopes"])

    def test_a_document_missing_a_field_says_which(self):
        for doc, why in (("[]", "no claudeAiOauth"), ('{"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}', "expiresAt"),
                         ('{"claudeAiOauth": {"refreshToken": "r", "expiresAt": 1}}', "accessToken")):
            with self.subTest(why=why):
                with self.assertRaises(ValueError) as e:
                    claudelogin.parse(doc)
                self.assertIn(why, str(e.exception))


def _wk(*args, timeout=300):
    return subprocess.run([str(REPO / "wk"), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          timeout=timeout)


def _workspace(case):
    names = [f[0] for f in (ln.split() for ln in _wk("ls", timeout=120).stdout.splitlines()[1:])
             if len(f) > 2 and f[1].split(":")[-1] == "container" and f[2] == "running"]
    if not names:
        case.skipTest("no running container workspace here ('wk new <name>')")
    return names[0]


@requires_container_place()
class TestLiveTheInjectorHoldsTheLogin(unittest.TestCase):
    """Read-only against the first running container workspace `wk ls` names; it makes and removes nothing."""

    def test_inject_claude_login(self):
        ws = _workspace(self)
        doctor = _wk("doctor", ws).stdout
        for row in ("the claude.ai login in here is the placeholder", "no claude.ai token is readable in here",
                    "the placeholder login is authenticated through the injector (HTTP 200)", "is authenticated (claude.ai)"):
            self.assertIn(row, doctor)
        reply = _wk("enter", ws, "--", "bash", "-lc", "claude -p 'Reply with the single word OK.'")
        self.assertIn("OK", reply.stdout)


@requires_container_place()
class TestLiveTheLoginGoesOnlyWhereTheCliSendsIt(unittest.TestCase):
    @owed("live inject.claude_paths: the path prefixes the CLI sends the placeholder bearer to are measured only from the "
          "injector's log over a real session; the injector then injects on those alone")
    def test_inject_claude_paths(self):
        self.assertTrue(set(INJECTOR.CLAUDE_INJECT_PATHS))


@requires_container_place()
@unittest.skipUnless(os.environ.get("WK_TEST_LIVE_REFRESH") == "1", "spends a refresh of the real login; set WK_TEST_LIVE_REFRESH=1")
class TestLiveARefreshRotatesTheLogin(unittest.TestCase):
    @owed("live inject.claude_refresh: the token endpoint's answer, and the podman machine's injector refreshing it, are "
          "measured only against the real endpoint")
    def test_inject_claude_refresh(self):
        from wk.secrets import Secrets
        path = Path(Secrets(str(REPO)).cred_path("claude-login"))
        doc = json.loads(path.read_text())
        before = doc["claudeAiOauth"]["refreshToken"]
        doc["claudeAiOauth"]["expiresAt"] = 0
        path.write_text(json.dumps(doc))
        self.assertIn("authenticated through the injector (HTTP 200)", _wk("doctor", _workspace(self)).stdout)
        after = claudelogin.parse(path.read_text())
        self.assertGreater(after["expiresAt"], time.time() * 1000)
        self.assertNotEqual(before, after["refreshToken"])


if __name__ == "__main__":
    unittest.main()
