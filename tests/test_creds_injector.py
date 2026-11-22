import contextlib
import fcntl
import io
import json
import os
import shutil
import socket
import socketserver
import ssl
import stat
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from tests.credsworld import login
from wk.core.fakes import FakeClock
from wk.creds import injector
from wk.creds.injector import Holder, LoginError

LOG = injector.log
HEAD = (b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: evil.example\r\nAuthorization: Bearer wk-injects-this\r\n"
        b"X-Api: kept\r\nConnection: keep-alive\r\nContent-Length: 2")


class TestLoginDocument(unittest.TestCase):
    def test_the_placeholder_never_expires_and_is_not_a_login(self):
        oauth = json.loads(injector.placeholder())["claudeAiOauth"]
        self.assertEqual((oauth["accessToken"], oauth["refreshToken"], oauth["expiresAt"]), ("wk-injects-this", "wk-injects-this", 4102444800000))
        self.assertTrue(injector.fresh(oauth, 2000000000))
        self.assertRaisesRegex(ValueError, "a server's placeholder, not a login", injector.parse, injector.placeholder())

    def test_a_login_parses_to_its_oauth_object(self):
        self.assertEqual(injector.parse(login(5))["accessToken"], "access-one")

    def test_what_is_not_a_login_says_why(self):
        for text, why in (("{", "not JSON"), ("[]", "no claudeAiOauth"), ('{"claudeAiOauth": 1}', "no claudeAiOauth"),
                          ('{"claudeAiOauth": {"accessToken": "a"}}', "no refreshToken"), ('{"claudeAiOauth": {"accessToken": "", "refreshToken": "r"}}', "no accessToken"),
                          ('{"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}', "no expiresAt")):
            self.assertRaisesRegex(ValueError, why, injector.parse, text)

    def test_a_login_is_fresh_until_five_minutes_before_it_expires(self):
        self.assertEqual([injector.fresh({"expiresAt": 1000000}, now) for now in (699.9, 700.0)], [True, False])

    def test_the_refresh_request_is_the_clis_own(self):
        self.assertEqual(json.loads(injector.refresh_body({"refreshToken": "r1"})),
                         {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"})
        self.assertEqual(injector.TOKEN_URL, "https://platform.claude.com/v1/oauth/token")

    def test_a_refresh_answer_becomes_the_new_login(self):
        old = {"accessToken": "a1", "refreshToken": "r1", "expiresAt": 1, "scopes": ["s"]}
        self.assertEqual(injector.refreshed(old, {"access_token": "a2", "refresh_token": "r2", "expires_in": 100, "scope": "x y"}, 1000.0),
                         {"accessToken": "a2", "refreshToken": "r2", "expiresAt": 1100000, "scopes": ["x", "y"]})
        self.assertEqual(injector.refreshed(old, {"access_token": "a2"}, 1000.0), {"accessToken": "a2", "refreshToken": "r1", "expiresAt": 4600000, "scopes": ["s"]})

    def test_a_refresh_answer_without_a_token_is_an_error(self):
        for got in ({}, [], {"access_token": ""}):
            self.assertRaisesRegex(LoginError, "no access_token", injector.refreshed, {}, got, 0)


class TestTokenEndpoint(unittest.TestCase):
    def ask(self, outcome):
        urlopen = mock.Mock(side_effect=outcome)
        try:
            return injector.ask_token_endpoint(b"{}", urlopen), urlopen
        finally:
            self.request = urlopen.call_args[0][0]

    def test_it_posts_json_and_returns_the_answer(self):
        got, urlopen = self.ask([io.BytesIO(b'{"access_token": "a"}')])
        self.assertEqual((got, self.request.get_method(), self.request.full_url, self.request.data, urlopen.call_args[1]),
                         ({"access_token": "a"}, "POST", injector.TOKEN_URL, b"{}", {"timeout": 20}))

    def error(self, code, body=b""):
        return urllib.error.HTTPError(injector.TOKEN_URL, code, "x", {}, io.BytesIO(body))

    def test_a_refusal_means_the_login_expired_was_revoked_or_spent(self):
        for code in (400, 401, 403):
            with self.error(code) as e:
                self.assertRaisesRegex(LoginError, r"refused to refresh the claude.ai login \(HTTP %d\): it expired, was revoked.*"
                                                   r"log in to Claude again, `wk key set claude-login`" % code, self.ask, e)

    def test_a_refusal_carries_the_endpoints_own_code_and_description(self):
        with self.error(400, b'{"error": "invalid_grant", "error_description": "Refresh token expired", "other": "x"}') as e:
            self.assertRaisesRegex(LoginError, r"\(HTTP 400, invalid_grant, Refresh token expired\): it expired", self.ask, e)
        for body in (b"<html>", b"[]", b'{"error": {"type": "x"}}'):
            with self.error(400, body) as e:
                self.assertRaisesRegex(LoginError, r"\(HTTP 400\): it expired", self.ask, e)
        self.assertEqual(injector.refusal(json.dumps({"error": "e" * 300}).encode()), ", " + "e" * 100)

    def test_any_other_failure_is_tried_again(self):
        with self.error(500) as e:
            self.assertRaisesRegex(LoginError, "answered HTTP 500 to a refresh; it is tried again", self.ask, e)
        self.assertRaisesRegex(LoginError, "could not refresh.*down.*tried again", self.ask, OSError("down"))
        self.assertRaisesRegex(LoginError, "could not refresh", self.ask, [io.BytesIO(b"<html>")])


class Held(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, ".credentials.json")
        self.clock = FakeClock(1000.0)
        self.asked, self.answers = [], []
        self.holder = Holder(self.path, self.clock.now, self.ask)
        patch = mock.patch.object(injector, "log", lambda message: None)
        patch.start()
        self.addCleanup(patch.stop)

    def ask(self, body):
        self.asked.append(json.loads(body)["refresh_token"])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def hold(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def doc(self):
        with open(self.path) as f:
            return json.load(f)


class TestHolder(Held):
    def test_a_fresh_login_is_used_without_asking(self):
        self.hold(login(1000000 + 300001))
        self.assertEqual((self.holder.access_token(), self.asked), ("access-one", []))

    def test_inside_the_margin_it_is_refreshed_and_written_back_owner_only(self):
        doc = json.loads(login(1000000 + 300000))
        doc["other"] = "kept"
        self.hold(json.dumps(doc))
        self.answers = [{"access_token": "access-two", "refresh_token": "refresh-two", "expires_in": 7200}]
        self.assertEqual((self.holder.access_token(), self.asked), ("access-two", ["refresh-one"]))
        self.assertEqual((self.doc()["other"], self.doc()["claudeAiOauth"]["refreshToken"], self.doc()["claudeAiOauth"]["expiresAt"]),
                         ("kept", "refresh-two", 8200000))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual((self.holder.access_token(), self.asked), ("access-two", ["refresh-one"]))

    def test_a_failed_refresh_is_not_asked_again_for_a_minute(self):
        self.hold(login(0))
        self.answers = [LoginError("the endpoint is down"), {"access_token": "access-two"}]
        self.assertRaisesRegex(LoginError, "^the endpoint is down$", self.holder.access_token)
        self.clock.sleep(59)
        self.assertRaisesRegex(LoginError, r"the endpoint is down \(not asked again for 1s\)", self.holder.access_token)
        self.assertEqual(self.asked, ["refresh-one"])
        self.clock.sleep(1)
        self.assertEqual((self.holder.access_token(), self.asked), ("access-two", ["refresh-one"] * 2))

    def test_a_newly_delivered_login_is_asked_about_at_once(self):
        self.hold(login(0))
        self.answers = [LoginError("spent"), {"access_token": "access-three"}]
        self.assertRaises(LoginError, self.holder.access_token)
        self.hold(login(0, refresh="refresh-new"))
        self.assertEqual((self.holder.access_token(), self.asked), ("access-three", ["refresh-one", "refresh-new"]))

    def test_a_request_that_waited_on_the_lock_uses_the_refresh_another_made(self):
        self.hold(login(0))
        compared, got = threading.Event(), []

        def clock():
            compared.set()
            return 1000.0
        waiter = threading.Thread(target=lambda: got.append(Holder(self.path, clock, self.ask).access_token()))
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            waiter.start()
            self.assertTrue(compared.wait(10))
            self.hold(login(access="made-by-another"))
        waiter.join(10)
        self.assertEqual((got, self.asked), (["made-by-another"], []))

    def test_no_login_or_an_unusable_one_names_the_remedy(self):
        self.assertRaisesRegex(LoginError, "holds no claude.ai login: `wk key set claude-login`, then `wk machine setup`", self.holder.access_token)
        self.assertIn("holds no claude.ai login", self.holder.state())
        self.hold(injector.placeholder())
        self.assertRaisesRegex(LoginError, r"unusable \(it is a server's placeholder", self.holder.access_token)

    def test_its_state_is_one_line(self):
        self.hold(login())
        self.assertEqual(self.holder.state(), "login held")

    def test_a_login_due_a_refresh_is_held_until_the_refresh_is_refused_and_then_its_state_is_the_refusal(self):
        self.hold(login(0))
        self.assertEqual(self.holder.state(), "login held")
        self.answers = [LoginError("refused: invalid_grant")]
        self.assertRaises(LoginError, self.holder.access_token)
        self.assertEqual(self.holder.state(), "refused: invalid_grant")
        self.hold(login(0, refresh="refresh-new"))
        self.assertEqual(self.holder.state(), "login held")

    def test_a_login_whose_refresh_token_has_expired_is_not_held(self):
        dead = json.loads(login(0))
        dead["claudeAiOauth"]["refreshTokenExpiresAt"] = 999000
        self.hold(json.dumps(dead))
        self.assertIn("has expired, its refresh token too: log in to Claude again, `wk key set claude-login`", self.holder.state())
        dead["claudeAiOauth"]["refreshTokenExpiresAt"] = 1001000
        self.hold(json.dumps(dead))
        self.assertEqual(self.holder.state(), "login held")


class TestRequestRewriting(unittest.TestCase):
    def test_the_request_line_is_method_and_target_or_nothing(self):
        self.assertEqual([injector.request_line(h) for h in (HEAD, b"CONNECT a:443 HTTP/1.1", b"GET /", b"")],
                         [("POST", "/v1/messages?beta=true"), ("CONNECT", "a:443"), ("", ""), ("", "")])

    def test_a_connect_target_is_a_lowercase_host_and_a_port(self):
        self.assertEqual([injector.connect_host(t) for t in ("API.Anthropic.com.:443", "h:8443", "h", "h:x")],
                         [("api.anthropic.com", 443), ("h", 8443), ("", 0), ("h", 0)])

    def test_a_header_is_found_whatever_its_case(self):
        self.assertEqual((injector.header(HEAD, b"x-api"), injector.header(HEAD, b"upgrade")), (b"kept", None))

    def test_only_the_placeholder_bearer_asks_for_the_login(self):
        self.assertEqual([injector.wants_token(h) for h in (HEAD, HEAD.replace(b"wk-injects-this", b"sk-other"), b"GET / HTTP/1.1")], [True, False, False])

    def test_the_oauth_endpoints_are_recognised(self):
        self.assertEqual([injector.is_oauth(t) for t in ("/v1/oauth/token", "/v1/oauth/token/?x=1", "/oauth/authorize?a=b", "/v1/messages", "/v1/oauth/tokens")],
                         [True, True, True, False, False])

    def test_a_bare_lf_in_the_head_is_seen(self):
        self.assertEqual([injector.bare_lf(h) for h in (HEAD, HEAD + b"\nX: smuggled")], [False, True])

    def test_the_body_length_is_one_content_length_or_none(self):
        self.assertEqual((injector.body_length(HEAD), injector.body_length(b"GET / HTTP/1.1\r\nHost: h")), ((2, None), (0, None)))

    def test_a_chunked_body_or_an_ambiguous_length_is_refused(self):
        self.assertEqual(injector.body_length(HEAD + b"\r\nTransfer-Encoding: chunked")[1][0], b"411 Length Required")
        for extra in (b"\r\ncontent-length: 3", b"\r\nContent-Length: 2"):
            self.assertEqual(injector.body_length(HEAD + extra)[1][0], b"400 Bad Request")
        self.assertEqual(injector.body_length(b"POST / HTTP/1.1\r\nContent-Length: -1")[1][0], b"400 Bad Request")

    def test_the_placeholder_is_swapped_and_host_length_and_connection_are_the_injectors(self):
        self.assertEqual(injector.rewrite_head(HEAD + b"\r\nProxy-Authorization: p", "api.anthropic.com", "real", 2, False),
                         b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: api.anthropic.com\r\nX-Api: kept\r\n"
                         b"Authorization: Bearer real\r\nContent-Length: 2\r\nConnection: close\r\n\r\n")

    def test_without_a_token_the_requests_own_authorization_passes_as_sent(self):
        head = HEAD.replace(b"wk-injects-this", b"sk-other")
        self.assertEqual(injector.rewrite_head(head, "claude.ai", "", 0, True),
                         b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: claude.ai\r\nAuthorization: Bearer sk-other\r\nX-Api: kept\r\n"
                         b"Content-Length: 0\r\nConnection: Upgrade\r\n\r\n")

    def test_an_answer_of_the_injectors_own_carries_its_length_and_a_fault_header_when_it_is_at_fault(self):
        self.assertEqual(injector.answer(b"403 Forbidden", b"no\r\n"),
                         b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: 4\r\nConnection: close\r\n\r\nno\r\n")
        self.assertTrue(injector.answer(b"502 Bad Gateway", b"", b"gaierror").startswith(b"HTTP/1.1 502 Bad Gateway\r\nX-Wk-Injector: gaierror\r\n"))

    def test_an_upstream_that_cannot_be_opened_is_504_stalled_502_unreachable_or_502_this_machines_fault(self):
        def fault(error):
            return injector.upstream_fault("claude.ai", error).split(b"\r\n")[:2]
        self.assertEqual(fault(socket.timeout("timed out"))[0], b"HTTP/1.1 504 Gateway Timeout")
        for error in (ConnectionRefusedError(61, "refused"), OSError(injector.UNREACHABLE[0], "unreachable")):
            self.assertEqual(fault(error), [b"HTTP/1.1 502 Bad Gateway", b"Content-Type: text/plain"])
        self.assertEqual(fault(socket.gaierror(8, "unknown name")), [b"HTTP/1.1 502 Bad Gateway", b"X-Wk-Injector: gaierror"])
        self.assertEqual(fault(ssl.SSLCertVerificationError(1, "bad cert")), [b"HTTP/1.1 502 Bad Gateway", b"X-Wk-Injector: SSLCertVerificationError"])


class Upstream(socketserver.ThreadingTCPServer):
    """Claude's hosts, on loopback: it records each request and answers by its path."""
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A test hangs up on it on purpose."""


class UpstreamRequest(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.tls.wrap_socket(self.request, server_side=True) as tls:
            head, body = injector.read_until(tls, b"\r\n\r\n")
            length = injector.body_length(head)[0]
            while len(body) < length:
                body += tls.recv(4096)
            self.server.seen.append((head, body))
            path = injector.request_line(head)[1]
            if path == "/ws":
                tls.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
                while True:
                    data = tls.recv(4096)
                    if not data:
                        return
                    tls.sendall(data.upper())
            elif path == "/stall":
                self.server.release.wait(10)
            elif path != "/hangup":
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(b"echo:" + data)


class Loopback(Held):
    """The injector, Claude's hosts and one other host, all on 127.0.0.1; `dial` is the only route out."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.dir)
        cls.logged = []
        patch = mock.patch.object(injector, "log", cls.logged.append)
        patch.start()
        cls.addClassCleanup(patch.stop)
        cls.faults = {}
        cls.holder = Holder(os.path.join(cls.dir, "login.json"))
        cls.trust = ssl.create_default_context(cafile=None)
        cls.server = injector.Injector(("127.0.0.1", 0), cls.dir, cls.holder, cls.trust, cls.dial)
        cls.trust.load_verify_locations(os.path.join(cls.dir, "ca.pem"))
        cls.upstream = Upstream(("127.0.0.1", 0), UpstreamRequest)
        cls.upstream.tls, cls.upstream.seen, cls.upstream.release = cls.server.tls, [], threading.Event()
        cls.echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        cls.echo.daemon_threads = True
        for server in (cls.server, cls.upstream, cls.echo):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.addClassCleanup(server.server_close)
            cls.addClassCleanup(server.shutdown)

    @classmethod
    def dial(cls, host, port):
        if host in cls.faults:
            raise cls.faults[host]
        to = cls.upstream if (host, port) in [(h, 443) for h in injector.HOSTS] else cls.echo
        return socket.create_connection(to.server_address, 5)

    def setUp(self):
        self.faults.clear()
        del self.upstream.seen[:]
        del self.logged[:]
        with open(self.holder.path, "w") as f:
            f.write(login(access="the-real-token"))

    def connect(self, target, extra=b""):
        sock = socket.create_connection(self.server.server_address, 5)
        self.addCleanup(sock.close)
        sock.settimeout(10)
        sock.sendall(b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\n" + extra)
        return sock

    def established(self, sock):
        self.assertEqual(injector.read_until(sock, b"\r\n\r\n")[0], b"HTTP/1.1 200 Connection established")

    def claude(self, host="api.anthropic.com"):
        sock = self.connect(host.encode() + b":443")
        self.established(sock)
        tls = self.trust.wrap_socket(sock, server_hostname=host)
        self.addCleanup(tls.close)
        return tls

    def everything(self, sock):
        got = b""
        while True:
            try:
                data = sock.recv(65536)
            except ConnectionResetError:
                return got
            if not data:
                return got
            got += data

    def send(self, request, host="api.anthropic.com"):
        tls = self.claude(host)
        tls.sendall(request)
        return self.everything(tls)


class TestInjectorEndToEnd(Loopback):
    def test_each_of_claudes_hosts_is_answered_with_a_leaf_of_the_injectors_ca_and_the_real_token(self):
        for host in injector.HOSTS:
            self.assertEqual(self.send(HEAD + b"\r\n\r\nhi", host), b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        head, body = self.upstream.seen[0]
        self.assertEqual((head, body), (b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: api.anthropic.com\r\nX-Api: kept\r\n"
                                       b"Authorization: Bearer the-real-token\r\nContent-Length: 2\r\nConnection: close", b"hi"))
        self.assertEqual([injector.header(h, b"host") for h, _ in self.upstream.seen], [h.encode() for h in injector.HOSTS])
        self.assertIn("api.anthropic.com POST /v1/messages with the login -> HTTP/1.1 200 OK", self.logged)
        self.assertNotIn("the-real-token", repr(self.logged))

    def test_a_body_that_arrives_in_pieces_is_read_to_its_length(self):
        tls = self.claude()
        tls.sendall(HEAD + b"\r\n\r\nh")
        tls.sendall(b"i")
        self.assertTrue(self.everything(tls).endswith(b"ok"))
        self.assertEqual(self.upstream.seen[0][1], b"hi")

    def test_a_request_with_its_own_authorization_passes_as_sent(self):
        self.send(b"GET /v1/models HTTP/1.1\r\nHost: api.anthropic.com\r\nAuthorization: Bearer sk-own\r\n\r\n")
        self.assertEqual(injector.header(self.upstream.seen[0][0], b"authorization"), b"Bearer sk-own")
        self.assertIn("api.anthropic.com GET /v1/models as sent -> HTTP/1.1 200 OK", self.logged)

    def test_the_oauth_endpoints_are_refused_and_never_forwarded(self):
        for target in (b"/v1/oauth/token", b"/oauth/authorize?client_id=x"):
            got = self.send(b"POST " + target + b" HTTP/1.1\r\nHost: platform.claude.com\r\nContent-Length: 0\r\n\r\n", "platform.claude.com")
            self.assertTrue(got.startswith(b"HTTP/1.1 403 Forbidden\r\n"), got)
            self.assertIn(b"refuses the claude.ai OAuth endpoints", got)
        self.assertEqual(self.upstream.seen, [])

    def test_a_chunked_an_ambiguous_or_a_bare_lf_request_is_refused_and_never_forwarded(self):
        for extra, status in ((b"\r\nTransfer-Encoding: chunked", b"411"), (b"\r\nContent-Length: 2", b"400"), (b"\nX: y", b"400")):
            self.assertEqual(self.send(HEAD + extra + b"\r\n\r\nhi").split(b" ")[1], status)
        self.assertEqual(self.upstream.seen, [])

    def test_without_a_usable_login_the_placeholder_is_answered_401_naming_the_remedy(self):
        os.unlink(self.holder.path)
        got = self.send(HEAD + b"\r\n\r\nhi")
        self.assertTrue(got.startswith(b"HTTP/1.1 401 Unauthorized\r\n"), got)
        self.assertIn(b"put no claude.ai login on this request: this machine holds no claude.ai login: `wk key set claude-login`", got)
        self.assertEqual(self.upstream.seen, [])

    def test_a_websocket_upgrade_passes_both_ways_with_the_bytes_sent_early(self):
        tls = self.claude("claude.ai")
        tls.sendall(b"GET /ws HTTP/1.1\r\nHost: claude.ai\r\nUpgrade: websocket\r\nAuthorization: Bearer wk-injects-this\r\n\r\nearly")
        self.assertEqual(injector.read_until(tls, b"\r\n\r\n")[0], b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket")
        self.assertEqual(tls.recv(100), b"EARLY")
        tls.sendall(b"later")
        self.assertEqual(tls.recv(100), b"LATER")
        head = self.upstream.seen[0][0]
        self.assertEqual((injector.header(head, b"connection"), injector.header(head, b"upgrade"), injector.header(head, b"authorization")),
                         (b"Upgrade", b"websocket", b"Bearer the-real-token"))

    def test_an_upstream_that_takes_the_request_and_says_nothing_is_504_saying_it_was_sent(self):
        self.upstream.release.clear()
        self.addCleanup(self.upstream.release.set)
        with mock.patch.object(injector, "READ_TIMEOUT", 0.3):
            got = self.send(b"GET /stall HTTP/1.1\r\nHost: claude.ai\r\n\r\n")
        self.assertTrue(got.startswith(b"HTTP/1.1 504 Gateway Timeout\r\n"), got)
        self.assertIn(b"(the request was sent; it may have been applied)", got)

    def test_an_upstream_that_hangs_up_without_a_status_ends_the_connection(self):
        self.assertEqual(self.send(b"GET /hangup HTTP/1.1\r\nHost: claude.ai\r\n\r\n"), b"")

    def test_an_upstream_that_cannot_be_opened_is_answered_by_the_injector(self):
        for error, status, fault in ((socket.timeout("timed out"), b"504", None), (ConnectionRefusedError(61, "refused"), b"502", None),
                                     (socket.gaierror(8, "unknown name"), b"502", b"gaierror")):
            self.faults["claude.ai"] = error
            got = self.send(b"GET / HTTP/1.1\r\nHost: claude.ai\r\n\r\n", "claude.ai")
            self.assertEqual((got.split(b" ")[1], injector.header(got.split(b"\r\n\r\n")[0], b"x-wk-injector")), (status, fault))

    def test_a_certificate_the_injector_cannot_verify_is_its_own_fault(self):
        with mock.patch.object(self.server, "trust", ssl.create_default_context()):
            got = self.send(b"GET / HTTP/1.1\r\nHost: claude.ai\r\n\r\n")
        self.assertEqual(injector.header(got.split(b"\r\n\r\n")[0], b"x-wk-injector"), b"SSLCertVerificationError")

    def test_any_other_host_is_tunnelled_untouched(self):
        sock = self.connect(b"other.example:8443", b"sent with the connect")
        self.established(sock)
        self.assertEqual(sock.recv(100), b"echo:sent with the connect")
        sock.sendall(b"Authorization: Bearer wk-injects-this")
        self.assertEqual(sock.recv(100), b"echo:Authorization: Bearer wk-injects-this")

    def test_a_tunnel_ends_when_neither_side_speaks(self):
        with mock.patch.object(injector, "IDLE_TIMEOUT", 0.2):
            sock = self.connect(b"other.example:443")
            self.established(sock)
            self.assertEqual(self.everything(sock), b"")

    def test_another_host_that_cannot_be_reached_is_502(self):
        self.faults["other.example"] = ConnectionRefusedError(61, "refused")
        self.assertTrue(self.everything(self.connect(b"other.example:443")).startswith(b"HTTP/1.1 502 Bad Gateway\r\n"))

    def test_a_plain_request_is_answered_with_the_banner_and_the_logins_state(self):
        sock = socket.create_connection(self.server.server_address, 5)
        self.addCleanup(sock.close)
        sock.sendall(b"GET / HTTP/1.1\r\nHost: vault:3128\r\n\r\n")
        got = self.everything(sock)
        self.assertTrue(got.startswith(b"HTTP/1.1 400 Bad Request\r\n"), got)
        self.assertEqual(got.split(b"\r\n\r\n")[1], b"wk injector: a forward proxy, CONNECT only\r\nlogin held\r\n")

    def test_a_client_that_leaves_at_any_point_ends_its_connection_alone(self):
        socket.create_connection(self.server.server_address, 5).close()
        self.claude().close()
        tls = self.claude()
        tls.sendall(HEAD + b"\r\n\r\nh")
        self.assertRaises(ssl.SSLEOFError, tls.unwrap)
        self.assertTrue(self.send(HEAD + b"\r\n\r\nhi").endswith(b"ok"))

    def test_a_client_that_does_not_speak_tls_to_a_claude_host_is_dropped_and_logged(self):
        sock = self.connect(b"claude.ai:443")
        self.established(sock)
        sock.sendall(b"GET / HTTP/1.1\r\n\r\n" * 20)
        self.everything(sock)
        self.assertTrue(any(line.startswith("connection failed: SSL") for line in self.logged), self.logged)


class TestCerts(Loopback):
    def test_the_keys_are_owner_only_and_a_second_start_keeps_the_ca(self):
        def ca():
            with open(os.path.join(self.dir, "ca.pem")) as f:
                return f.read()
        before = ca()
        self.assertEqual([stat.S_IMODE(os.stat(os.path.join(self.dir, k)).st_mode) for k in ("ca.key", "leaf.key")], [0o600, 0o600])
        with mock.patch.object(injector.subprocess, "run") as run:
            injector.ensure_certs(self.dir)
        self.assertEqual((run.call_count, ca()), (0, before))

    def test_a_lost_leaf_is_remade_under_the_same_ca(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fresh = os.path.join(tmp, "injector")
        shutil.copytree(self.dir, fresh)
        os.unlink(os.path.join(fresh, "chain.pem"))
        context = injector.ensure_certs(fresh)
        self.assertIsInstance(context, ssl.SSLContext)
        with open(os.path.join(fresh, "ca.pem")) as new, open(os.path.join(self.dir, "ca.pem")) as old, open(os.path.join(fresh, "chain.pem")) as chain:
            ca = old.read()
            self.assertEqual(new.read(), ca)
            self.assertTrue(chain.read().endswith(ca))

    def test_a_new_directory_is_made_owner_only(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with mock.patch.object(injector.subprocess, "run"), self.assertRaises(FileNotFoundError):
            injector.ensure_certs(os.path.join(tmp, "new"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(tmp, "new")).st_mode), 0o700)


class TestMain(Loopback):
    def test_it_listens_on_the_tailnet_address_with_the_login_under_its_directory(self):
        served = []

        def forever(server):
            served.append((server.server_address[0], server.holder.path, server.trust.verify_mode))
        with mock.patch.object(injector.subprocess, "run", return_value=mock.Mock(stdout=b"127.0.0.1\nfd7a::1\n")) as run, \
                mock.patch.object(injector.Injector, "serve_forever", forever):
            injector.main([self.dir, "0"])
        self.assertEqual(run.call_args[0][0], ["tailscale", "ip", "-4"])
        self.assertEqual(served, [("127.0.0.1", os.path.join(self.dir, "login", ".credentials.json"), ssl.CERT_REQUIRED)])
        self.assertTrue(self.logged[-1].startswith("listening on 127.0.0.1:0 for api.anthropic.com, claude.ai, platform.claude.com"))

    def test_the_default_route_out_is_a_plain_connection(self):
        with injector.dial(*self.echo.server_address) as sock:
            sock.sendall(b"x")
            self.assertEqual(sock.recv(10), b"echo:x")

    def test_what_it_says_goes_to_stderr_for_the_journal(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            LOG("listening")
        self.assertEqual(err.getvalue(), "wk-injector: listening\n")
