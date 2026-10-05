"""How a workspace reaches the outside: what the proxy allows, and how the variables naming that proxy get into a
macOS guest's shells."""
import asyncio
import contextlib
import errno
import os
import importlib.util
import io
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import targets as wk_targets  # noqa: E402
from wk.machine import Fake  # noqa: E402

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"
INJECT = REPO / "container" / "proxy" / "github-inject.py"
SHELL_RC = REPO / "vm" / "shell-rc.sh"

# The six a proxied machine has to export. curl and git read the lowercase
# pair, some tools only the uppercase, and no_proxy keeps a workspace's own
# loopback services off the proxy.
VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "no_proxy", "NO_PROXY")


# drive_injector patches asyncio.open_connection for the length of a run.
REAL_OPEN_CONNECTION = asyncio.open_connection


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _policy():
    return _load(PROXY, "wkproxy").Policy(tempfile.mkdtemp(prefix="wk-test-store-"))


class FakeWriter:
    """An asyncio StreamWriter as far as either program uses one: it is written to, drained and closed, and the
    test reads back every byte."""

    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, b):
        self.data += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    async def wait_closed(self):
        pass


def _reader(data=b"", eof=True):
    """A StreamReader already holding `data`. Built inside the running loop:"""
    r = asyncio.StreamReader()
    if data:
        r.feed_data(data)
    if eof:
        r.feed_eof()
    return r


def drive_injector(tmp, client_bytes,
                   upstream_reply=b"HTTP/1.1 204 No Content\r\n\r\n",
                   token="ghp-not-a-real-token", read_token=None,
                   bugzilla_key=None, connect=None, upstream_timeout=None,
                   read_timeout=None):
    """Injector.handle against a fake upstream: returns what the client was sent, every byte that reached the
    upstream, the hosts it connected to and what the injector logged."""
    m = _load(INJECT, "wkinject")
    d = Path(tempfile.mkdtemp(dir=str(tmp)))
    pat = d / "push-github-pat"
    read_pat = d / "read-github-pat"
    bz = d / "push-bugzilla-api-key"
    if token is not None:
        pat.write_text(token + "\n")
    if read_token is not None:
        read_pat.write_text(read_token + "\n")
    if bugzilla_key is not None:
        bz.write_text(bugzilla_key + "\n")
    inj = m.Injector(str(pat), str(read_pat), str(bz), None)
    uwriter = FakeWriter()
    cwriter = FakeWriter()
    opened = []

    async def fake_open_connection(host, port, **kw):
        opened.append((host, port))
        if connect is not None:
            return await connect()
        return _reader(upstream_reply), uwriter

    if upstream_timeout is not None:
        m.UPSTREAM_TIMEOUT = upstream_timeout
    if read_timeout is not None:
        m.READ_TIMEOUT = read_timeout

    async def drive():
        await inj.handle(_reader(client_bytes), cwriter)

    logged = io.StringIO()
    # patch.object, so the real `asyncio.open_connection` is restored even
    # though this module and the injector share one `asyncio`: assigning it
    # back by name after patching would store the fake for ever, and the next
    # test in the process to open a connection would get it.
    with unittest.mock.patch.object(asyncio, "open_connection",
                                    fake_open_connection), \
            contextlib.redirect_stderr(logged):
        asyncio.run(drive())
    return bytes(cwriter.data), bytes(uwriter.data), opened, logged.getvalue()


def assert_refused_with_the_switch_off(case, method, target,
                                       host="api.github.com", extra="", **kw):
    """One write with `wk key push off`: nothing leaves the injector, and what the client reads back names the switch
    rather than the far end answering for a credential it was never sent."""
    sep = "&" if "?" in target else "?"
    full = target + (sep + extra if extra else "")
    head = ("%s %s HTTP/1.1\r\nHost: %s\r\nContent-Length: 0\r\n\r\n"
            % (method, full, host)).encode("latin-1")
    client, upstream, opened, logged = drive_injector(case.tmp, head,
                                                      token=None, **kw)
    case.assertEqual([], opened,
                     "%s %s reached the network" % (method, target))
    case.assertEqual(b"", upstream)
    case.assertIn(b"412 Precondition Failed", client)
    case.assertIn(b"wk key push is off for this workspace's machine", client)
    case.assertIn(b"'wk key push on'", client)
    case.assertNotIn(b"wk-injects-this", client)
    case.assertIn("write refused: push is off", logged)
    return client, logged


# (host, ports, verdict): "tunnel" is allowed as a plain tunnel, "inject" is allowed and routed to the
# credential injector, "refuse" is refused. Suffix entries match on a dot boundary only.
ALLOWLIST = (
    [(h, (443,), "tunnel") for h in ("registry.npmjs.org", "formulae.brew.sh", "ghcr.io", "crates.io", "static.crates.io",
                                    "static.rust-lang.org", "sh.rustup.rs", "developer.apple.com",
                                    "download.developer.apple.com", "webkit.org", "trac.webkit.org", "build.webkit.org",
                                    "codeload.github.com", "raw.githubusercontent.com")]
    + [(h, (80, 443), "tunnel") for h in ("valid.apple.com", "ocsp.apple.com", "crl.apple.com", "i.pki.goog",
                                         "archive.ubuntu.com", "ports.ubuntu.com", "security.ubuntu.com",
                                         "ddebs.ubuntu.com")]
    + [("github.com", (443, 22), "tunnel")]
    + [(h, (443,), "inject") for h in ("api.github.com", "bugs.webkit.org")]
    + [("api.github.com", (22, 80, 9418), "refuse"), ("bugs.webkit.org", (80, 22), "refuse")]
    # The software update scan path: with it reachable Setup Assistant puts an update pane in front of a guest.
    + [(h, (443,), "refuse") for h in ("swscan.apple.com", "swcdn.apple.com", "swdist.apple.com",
                                      "updates.cdn-apple.com", "updates-http.cdn-apple.com", "mesu.apple.com",
                                      "gdmf.apple.com", "gdmf-ados.apple.com", "xp.apple.com")]
    + [(h, (443,), "refuse") for h in ("gsa.apple.com", "gateway.icloud.com", "weatherkit.apple.com",
                                      "iadsdk.apple.com", "api-spotlight-ausw2b.smoot.apple.com", "uploads.github.com",
                                      "evilregistry.npmjs.org.attacker.net", "notcrates.io", "ghcr.io.example.com",
                                      "evilapi.github.com.attacker.net", "api.github.com.attacker.net",
                                      "notapi.github.com.evil.example")]
)


class TestTheAllowlist(unittest.TestCase):
    def test_each_host_and_port_gets_its_verdict(self):
        p = _policy()
        for host, ports, verdict in ALLOWLIST:
            for port in ports:
                with self.subTest(host=host, port=port):
                    ok, why = p.host_allowed(host, port)
                    self.assertEqual(verdict != "refuse", ok, why)
                    if ok:
                        self.assertEqual(verdict == "inject", "injector" in why, why)

    def test_the_injected_host_is_routed_to_a_socket_under_the_store(self):
        m = _load(PROXY, "wkproxy")
        self.assertNotIn("/wk/", m.INJECT_SOCKET.replace("/var/lib/wk/", ""))
        self.assertTrue(m.INJECT_SOCKET.endswith("github-inject.sock"))


class TestAbsoluteFormHonoursTheScheme(unittest.TestCase):
    """A client that sends `GET https://host/...` instead of `CONNECT host:443` -- axios behind a proxy, which is
    how the Claude CLI fetches its org policy -- expects the proxy to originate TLS."""

    def setUp(self):
        self.m = _load(PROXY, "wkproxy_abs")

    def test_an_https_target_is_port_443_over_tls(self):
        host, port, tls, path = self.m.parse_absolute_target(
            "https://api.anthropic.com/api/claude_code/policy_limits")
        self.assertEqual(("api.anthropic.com", 443, True,
                          "/api/claude_code/policy_limits"), (host, port, tls, path))

    def test_an_http_target_stays_port_80_in_the_clear(self):
        host, port, tls, path = self.m.parse_absolute_target(
            "http://archive.ubuntu.com/ubuntu/pool")
        self.assertEqual(("archive.ubuntu.com", 80, False, "/ubuntu/pool"),
                         (host, port, tls, path))

    def test_an_explicit_port_is_kept_and_the_scheme_still_decides_tls(self):
        self.assertEqual(("h", 8443, True, "/"),
                         self.m.parse_absolute_target("https://h:8443/"))
        self.assertEqual(("h", 8080, False, "/"),
                         self.m.parse_absolute_target("http://h:8080/"))

    def test_open_upstream_wraps_the_socket_in_tls_only_when_asked(self):
        calls = []

        async def fake_open_connection(addr, port, **kw):
            calls.append(kw.get("ssl") is not None)
            class W:
                def close(self): pass
            return None, W()

        async def run(tls):
            pol = self.m.Policy(tempfile.mkdtemp(dir="/tmp"))
            proxy = self.m.Proxy(pol)
            with unittest.mock.patch.object(self.m.asyncio, "open_connection",
                                            fake_open_connection):
                await proxy.open_upstream("api.anthropic.com", 443 if tls else 80, tls)

        asyncio.run(run(True))
        asyncio.run(run(False))
        self.assertEqual([True, False], calls)


class TestTheRouteAndTheCheckReadOneSpelling(unittest.TestCase):
    """A host name is case-insensitive and may carry a trailing dot, so one name has several spellings."""

    def _routed(self, target):
        """Where `handle` sent this CONNECT: the host and port open_upstream was asked for, with the real
        allowlist in front of it."""
        m = _load(PROXY, "wkproxy")
        proxy = m.Proxy(m.Policy(tempfile.mkdtemp(prefix="wk-test-store-")))
        seen = []

        async def fake_open_upstream(host, port, tls=False):
            seen.append((host, port))
            return _reader(), FakeWriter()

        proxy.open_upstream = fake_open_upstream
        cwriter = FakeWriter()

        async def drive():
            await proxy.handle(
                _reader(b"CONNECT %s HTTP/1.1\r\n\r\n" % target.encode()), cwriter)

        asyncio.run(drive())
        return seen, bytes(cwriter.data)

    def test_every_spelling_of_the_api_reaches_the_injector(self):
        for target in ("api.github.com:443", "API.GITHUB.COM:443",
                       "api.github.com.:443", "Api.GitHub.Com.:443"):
            with self.subTest(target=target):
                seen, out = self._routed(target)
                self.assertEqual([("api.github.com", 443)], seen, out)
                self.assertIn(b"200 Connection established", out)

    def test_the_tunnel_is_granted_in_http_1_0(self):
        _seen, out = self._routed("api.github.com:443")
        self.assertIn(b"HTTP/1.0 200 Connection established", out)
        self.assertNotIn(b"HTTP/1.1 200", out)

    def test_a_shouted_denied_host_is_still_denied(self):
        seen, out = self._routed("UPLOADS.GITHUB.COM:443")
        self.assertEqual([], seen, out)
        self.assertIn(b"403 Forbidden", out)


class TestTheInjectorsRule(unittest.TestCase):
    """The header rewrite, driven directly: no socket, no network, no GitHub."""

    def setUp(self):
        self.m = _load(INJECT, "wkinject")

    def head(self, extra=b""):
        return (b"GET /user HTTP/1.1\r\n"
                b"Host: api.github.com\r\n"
                b"Authorization: Basic d2s6d2staW5qZWN0cy10aGlz\r\n"
                b"Connection: keep-alive\r\n"
                b"User-Agent: python-requests/2.31.0" + extra)

    def test_the_real_token_replaces_whatever_the_workspace_sent(self):
        out = self.m.rewrite_head(self.head(), self.m.GITHUB, "ghp-not-a-real-token")
        self.assertIn(b"Authorization: Bearer ghp-not-a-real-token", out)
        self.assertNotIn(b"Basic", out)
        self.assertEqual(1, out.count(b"Authorization:"))

    def test_the_request_line_and_the_other_headers_survive(self):
        out = self.m.rewrite_head(self.head(), self.m.GITHUB, "ghp-x")
        self.assertTrue(out.startswith(b"GET /user HTTP/1.1\r\n"))
        self.assertIn(b"Host: api.github.com", out)
        self.assertIn(b"User-Agent: python-requests/2.31.0", out)
        self.assertTrue(out.endswith(b"\r\n\r\n"))

    def test_with_no_token_the_placeholder_is_stripped_and_nothing_added(self):
        out = self.m.rewrite_head(self.head(), self.m.GITHUB, "")
        self.assertNotIn(b"Authorization", out)
        self.assertNotIn(b"Basic", out)

    def test_the_connection_is_closed_per_request(self):
        out = self.m.rewrite_head(self.head(), self.m.GITHUB, "ghp-x")
        self.assertIn(b"Connection: close", out)
        self.assertEqual(1, out.count(b"Connection:"))
        self.assertNotIn(b"keep-alive", out)

    def test_a_smuggled_proxy_credential_is_dropped_too(self):
        head = self.head(b"\r\nProxy-Authorization: Basic zzz")
        out = self.m.rewrite_head(head, self.m.GITHUB, "ghp-x")
        self.assertNotIn(b"Proxy-Authorization", out)

    def test_the_token_is_read_from_the_file_on_every_request(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "pat")
            self.assertEqual("", self.m.read_token(path))
            Path(path).write_text("ghp-not-a-real-token\n")
            self.assertEqual("ghp-not-a-real-token", self.m.read_token(path))
            Path(path).unlink()
            self.assertEqual("", self.m.read_token(path))

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_it_makes_its_own_ca_and_publishes_only_the_public_half(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            chain = self.m.ensure_certs(str(d / "certs"), str(d / "out" / "ca.pem"))
            published = (d / "out" / "ca.pem").read_text()
            self.assertIn("BEGIN CERTIFICATE", published)
            self.assertNotIn("PRIVATE KEY", published)
            self.assertEqual(0o644, (d / "out" / "ca.pem").stat().st_mode & 0o777)
            self.assertEqual(0o600, (d / "certs" / "ca.key").stat().st_mode & 0o777)
            self.assertEqual(0o600, (d / "certs" / "leaf.key").stat().st_mode & 0o777)
            self.assertEqual(2, Path(chain).read_text().count("BEGIN CERTIFICATE"))

            again = (d / "certs" / "ca.crt").read_text()
            self.m.ensure_certs(str(d / "certs"), str(d / "out" / "ca.pem"))
            self.assertEqual(again, (d / "certs" / "ca.crt").read_text())

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_the_leaf_is_for_that_host_and_is_not_a_ca(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.m.ensure_certs(str(d / "certs"), str(d / "out" / "ca.pem"))
            out = subprocess.run(["openssl", "x509", "-noout", "-text",
                                  "-in", str(d / "certs" / "leaf.crt")],
                                 stdout=subprocess.PIPE, text=True, check=True).stdout
            self.assertIn("DNS:api.github.com", out)
            self.assertIn("DNS:bugs.webkit.org", out)
            self.assertIn("CA:FALSE", out)

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_a_leaf_naming_fewer_hosts_is_remade_and_the_ca_kept(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            certs = d / "certs"
            self.m.ensure_certs(str(certs), str(d / "out" / "ca.pem"))
            ca = (certs / "ca.crt").read_text()
            cnf = self.m._conf(self.m.GITHUB, (self.m.GITHUB,))
            csr = str(certs / "leaf.csr")
            try:
                self.m._openssl("req", "-new", "-key", str(certs / "leaf.key"),
                                "-out", csr, "-config", cnf)
                self.m._openssl("x509", "-req", "-in", csr, "-CA",
                                str(certs / "ca.crt"), "-CAkey",
                                str(certs / "ca.key"), "-set_serial", "2",
                                "-days", "3650", "-out", str(certs / "leaf.crt"),
                                "-extfile", cnf, "-extensions", "leaf_ext")
            finally:
                os.unlink(cnf)
            self.assertEqual({"api.github.com"},
                             self.m.leaf_names(str(certs / "leaf.crt")))
            self.m.ensure_certs(str(certs), str(d / "out" / "ca.pem"))
            self.assertEqual(set(self.m.HOSTS),
                             self.m.leaf_names(str(certs / "leaf.crt")))
            self.assertEqual(ca, (certs / "ca.crt").read_text())


class TestTheInjectorsBugzillaRule(WkTest):
    """Bugzilla takes its login in the query string, where git-webkit puts `login` and `password` on every
    request (webkitbugspy/bugzilla.py, _login_arguments)."""

    TARGET = ("/rest/bug/250000?login=me%40example.test&password=wk-injects-this"
              "&include_fields=id%2Csummary")

    def setUp(self):
        super().setUp()
        self.m = _load(INJECT, "wkinject")

    def head(self, target, extra=b""):
        return (("GET %s HTTP/1.1\r\nHost: bugs.webkit.org\r\n"
                 "User-Agent: python-requests/2.31.0" % target).encode("latin-1")
                + extra)

    def test_the_login_pair_is_dropped_and_the_switchs_key_added(self):
        out = self.m.rewrite_head(self.head(self.TARGET), self.m.BUGZILLA,
                                  "not-a-real-key")
        self.assertEqual(b"GET /rest/bug/250000?include_fields=id%2Csummary"
                         b"&api_key=not-a-real-key HTTP/1.1",
                         out.split(b"\r\n")[0])
        self.assertNotIn(b"wk-injects-this", out)
        self.assertNotIn(b"login=", out)
        self.assertNotIn(b"Authorization", out)
        self.assertIn(b"Host: bugs.webkit.org\r\n", out)

    def test_with_no_key_the_request_goes_anonymous(self):
        out = self.m.rewrite_head(self.head(self.TARGET), self.m.BUGZILLA, "")
        self.assertTrue(out.startswith(
            b"GET /rest/bug/250000?include_fields=id%2Csummary HTTP/1.1\r\n"), out)
        self.assertNotIn(b"api_key", out)
        self.assertNotIn(b"wk-injects-this", out)

    def test_a_query_of_only_the_login_pair_leaves_a_bare_path(self):
        out = self.m.rewrite_head(
            self.head("/rest/user/me%40example.test?login=me%40example.test&password=x"),
            self.m.BUGZILLA, "")
        self.assertTrue(out.startswith(b"GET /rest/user/me%40example.test HTTP/1.1\r\n"), out)

    def test_every_spelling_bugzilla_takes_a_login_in_is_dropped(self):
        for name in ("api_key", "Bugzilla_api_key", "token", "Bugzilla_login",
                     "Bugzilla_password", "LOGIN", "pass%77ord"):
            with self.subTest(name=name):
                out = self.m.rewrite_head(
                    self.head("/rest/bug?%s=the-workspaces-own&limit=1" % name),
                    self.m.BUGZILLA, "k")
                self.assertNotIn(b"the-workspaces-own", out)
                self.assertIn(b"/rest/bug?limit=1&api_key=k HTTP/1.1", out)

    def test_a_key_in_a_header_is_dropped_too(self):
        out = self.m.rewrite_head(
            self.head("/rest/bug/1", b"\r\nX-BUGZILLA-API-KEY: the-workspaces-own"),
            self.m.BUGZILLA, "")
        self.assertNotIn(b"the-workspaces-own", out)

    def test_the_key_is_url_encoded(self):
        out = self.m.rewrite_head(self.head("/rest/bug/1"), self.m.BUGZILLA, "a b&c")
        self.assertIn(b"/rest/bug/1?api_key=a%20b%26c HTTP/1.1", out)

    def test_a_request_line_this_cannot_read_is_refused_not_forwarded(self):
        for line in (b"GET", b"GET /rest/bug?password=x",
                     b"GET /rest/bug HTTP/1.1 extra"):
            with self.subTest(line=line):
                self.assertIsNone(self.m.rewrite_head(
                    line + b"\r\nHost: bugs.webkit.org", self.m.BUGZILLA, "k"))
                client, upstream, opened, _ = drive_injector(
                    self.tmp, line + b"\r\nHost: bugs.webkit.org\r\n\r\n",
                    bugzilla_key="k")
                self.assertEqual([], opened)
                self.assertEqual(b"", upstream)
                self.assertIn(b"400 Bad Request", client)


class TestTheInjectorAnswersForItsHostsOnly(WkTest):
    """The forwarded connection is a TLS session to the name the request's Host header carries, and only one of
    the two this answers for."""

    def test_a_host_this_does_not_answer_for_is_refused_and_nothing_opened(self):
        for host in (b"uploads.github.com", b"evil.example",
                     b"api.github.com.evil.example", b"github.com"):
            with self.subTest(host=host):
                client, upstream, opened, _ = drive_injector(
                    self.tmp, b"POST /repos/x/y/releases HTTP/1.1\r\nHost: " + host
                    + b"\r\nContent-Length: 0\r\n\r\n")
                self.assertEqual([], opened)
                self.assertEqual(b"", upstream)
                self.assertIn(b"421 Misdirected Request", client)
                self.assertIn(b"api.github.com and bugs.webkit.org", client)

    def test_a_request_with_no_host_at_all_is_refused(self):
        client, _, opened, _ = drive_injector(self.tmp, b"GET /user HTTP/1.1\r\n\r\n")
        self.assertEqual([], opened)
        self.assertIn(b"421 Misdirected Request", client)

    def test_the_host_it_sends_is_the_host_it_verified(self):
        for host in ("api.github.com", "bugs.webkit.org"):
            with self.subTest(host=host):
                _, upstream, opened, _ = drive_injector(
                    self.tmp, ("GET /x HTTP/1.1\r\nHost: %s\r\n\r\n" % host).encode())
                self.assertEqual([(host, 443)], opened)
                self.assertIn(("Host: %s\r\n" % host).encode(), upstream)
                self.assertEqual(1, upstream.count(b"Host:"))

    def test_every_spelling_of_a_host_is_one_host(self):
        for spelling in (b"API.GITHUB.COM", b"api.github.com.", b"api.github.com:443"):
            with self.subTest(spelling=spelling):
                _, _, opened, _ = drive_injector(
                    self.tmp, b"GET /x HTTP/1.1\r\nHost: " + spelling + b"\r\n\r\n")
                self.assertEqual([("api.github.com", 443)], opened)


class TestTheInjectorReadsOneRequestAndNoMore(WkTest):
    """Everything past the first request head is relayed by nothing: a second request on the same connection is a
    head this program never rewrote, so it would reach GitHub carrying the client's own Authorization."""

    def _drive(self, client_bytes, upstream_reply=b"HTTP/1.1 204 No Content\r\n\r\n"):
        client, upstream, opened, _ = drive_injector(
            self.tmp, client_bytes, upstream_reply)
        return client, upstream, opened

    def test_a_pipelined_second_request_never_reaches_github(self):
        client, upstream, opened = self._drive(
            b"POST /repos/x/y/pulls HTTP/1.1\r\nHost: api.github.com\r\n"
            b"Content-Length: 2\r\n\r\nhi"
            b"GET /user HTTP/1.1\r\nHost: api.github.com\r\n"
            b"Authorization: token ghp-the-workspaces-own\r\n\r\n")
        self.assertEqual([("api.github.com", 443)], opened)
        self.assertEqual(1, upstream.count(b"HTTP/1.1"), upstream)
        self.assertNotIn(b"ghp-the-workspaces-own", upstream)
        self.assertTrue(upstream.endswith(b"\r\n\r\nhi"), upstream)
        self.assertIn(b"Content-Length: 2\r\n", upstream)

    def test_the_declared_body_does_reach_it(self):
        _, upstream, _ = self._drive(
            b"POST /repos/x/y/pulls HTTP/1.1\r\nHost: api.github.com\r\n"
            b'Content-Length: 11\r\n\r\n{"a":"bcd"}')
        self.assertTrue(upstream.endswith(b'{"a":"bcd"}'), upstream)
        self.assertIn(b"Content-Length: 11\r\n", upstream)

    def test_a_bare_lf_header_is_refused_and_nothing_is_forwarded(self):
        client, upstream, opened = self._drive(
            b"GET /x HTTP/1.1\nAuthorization: token ghp-the-workspaces-own\r\n\r\n")
        self.assertEqual([], opened, upstream)
        self.assertEqual(b"", upstream)
        self.assertIn(b"400 Bad Request", client)
        self.assertIn(b"bare LF", client)

    def test_a_chunked_body_is_refused_rather_than_guessed_at(self):
        client, upstream, opened = self._drive(
            b"POST /user HTTP/1.1\r\nHost: api.github.com\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"2\r\nhi\r\n0\r\n\r\n")
        self.assertEqual([], opened, upstream)
        self.assertIn(b"411 Length Required", client)

    def test_two_content_lengths_are_refused(self):
        client, _, opened = self._drive(
            b"POST /user HTTP/1.1\r\nHost: api.github.com\r\n"
            b"Content-Length: 2\r\nContent-Length: 40\r\n\r\nhi")
        self.assertEqual([], opened)
        self.assertIn(b"400 Bad Request", client)

    def test_the_clients_own_framing_headers_never_go_on(self):
        _, upstream, _ = self._drive(
            b"POST /repos/x/y/pulls HTTP/1.1\r\nHost: api.github.com\r\n"
            b"Content-Length: 2\r\n\r\nhi")
        self.assertEqual(1, upstream.count(b"Content-Length:"), upstream)
        self.assertNotIn(b"Transfer-Encoding", upstream)


# Every request `git-webkit` makes of api.github.com, by the file that builds
# the URL, split the way the injector splits them: a read spends the standing
# token, a write spends the switch's.
GIT_WEBKIT_READS = (
    # webkitbugspy/github.py: Tracker.credentials, Tracker.user, Tracker.me
    ("GET", "/user"),
    ("GET", "/users/justinmichaud"),
    # webkitscmpy/remote/git_hub.py: GitHub.request with no path, .branches,
    # .tags, .commits, .find, PRGenerator.statuses
    ("GET", "/repos/WebKit/WebKit"),
    ("GET", "/repos/WebKit/WebKit/branches"),
    ("GET", "/repos/WebKit/WebKit/tags"),
    ("GET", "/repos/WebKit/WebKit/commits?sha=abc123&per_page=20"),
    ("GET", "/repos/WebKit/WebKit/commits/abc123"),
    ("GET", "/repos/WebKit/WebKit/commits/abc123/statuses"),
    ("GET", "/repos/WebKit/WebKit/compare/main...abc123"),
    # webkitscmpy/remote/git_hub.py: PRGenerator.get, .reviewers, .diff
    ("GET", "/repos/WebKit/WebKit/pulls/1234"),
    ("GET", "/repos/WebKit/WebKit/pulls/1234/comments"),
    ("GET", "/repos/WebKit/WebKit/pulls/1234/reviews"),
    ("GET", "/repos/WebKit/WebKit/pulls/1234/requested_reviewers"),
    # webkitbugspy/github.py: Tracker.labels, Tracker.populate, Issue comments
    # and timeline
    ("GET", "/repos/WebKit/WebKit/labels"),
    ("GET", "/repos/WebKit/WebKit/issues/1234"),
    ("GET", "/repos/WebKit/WebKit/issues/1234/comments"),
    ("GET", "/repos/WebKit/WebKit/issues/1234/timeline"),
    # webkitscmpy/remote/git_hub.py: PRGenerator.find -- one GraphQL search
    # document, which carries no mutation
    ("POST", "/graphql"),
)

GIT_WEBKIT_WRITES = (
    # webkitscmpy/remote/git_hub.py: PRGenerator.create, .update,
    # ._make_comment, .review
    ("POST", "/repos/WebKit/WebKit/pulls"),
    ("POST", "/repos/WebKit/WebKit/pulls/1234"),
    ("POST", "/repos/WebKit/WebKit/pulls/1234/comments"),
    ("POST", "/repos/WebKit/WebKit/pulls/1234/reviews"),
    # webkitbugspy/github.py: Tracker.create, .add_comment, .add_assignees,
    # .set
    ("POST", "/repos/WebKit/WebKit/issues"),
    ("POST", "/repos/WebKit/WebKit/issues/1234/comments"),
    ("POST", "/repos/WebKit/WebKit/issues/1234/assignees"),
    ("PATCH", "/repos/WebKit/WebKit/issues/1234"),
    ("PUT", "/repos/WebKit/WebKit/issues/1234/labels"),
    # webkitscmpy/program/pull_request.py, with webkitscmpy.update-fork set
    ("POST", "/repos/justinmichaud/WebKit/merge-upstream"),
)

# Paths a workspace reaches that `git-webkit` never asks for -- `gh`, curl by
# hand, and the reachability probe `wk doctor <ws>` measures the injector with.
READS_WITH_NO_GIT_WEBKIT_CALLER = ("/user", "/rate_limit",
                                   "/repos/a/b/actions/runs", "/orgs/x", "/")

# Real GitHub endpoints, each destructive, exfiltrating or both. Every one of
# them is forwarded, and every one of them spends the switch's token when there
# is one.
HOSTILE_WRITES = (
    ("DELETE", "/repos/WebKit/WebKit"),                        # delete the repo
    ("PATCH", "/repos/WebKit/WebKit"),                         # settings: private -> public
    ("POST", "/repos/WebKit/WebKit/keys"),                     # add a deploy key
    ("PUT", "/repos/WebKit/WebKit/collaborators/attacker"),    # add a collaborator
    ("POST", "/repos/WebKit/WebKit/actions/workflows/ci.yml/dispatches"),
    ("POST", "/repos/WebKit/WebKit/git/refs"),                 # push without the deploy key
    ("DELETE", "/repos/WebKit/WebKit/git/refs/heads/main"),
    ("PUT", "/repos/WebKit/WebKit/pulls/1234/merge"),          # land it unreviewed
    ("POST", "/user/repos"),                                   # a repo to exfiltrate into
    ("PATCH", "/user"),                                        # the account's own settings
    ("POST", "/gists"),                                        # exfiltration in one call
    ("POST", "/applications/id/token"),
    ("DELETE", "/user/keys/1"),
)


# Every request webkitbugspy makes of bugs.webkit.org (bugzilla.py), each with
# the login pair `_login_arguments` appends. A Bugzilla key is one account with
# no read-only form, so there is no standing half: the switch's key on all of
# them or on none.
GIT_WEBKIT_BUGZILLA = (
    # Tracker.credentials' validater, Tracker.user, Tracker.me
    ("GET", "/rest/user/me%40example.test"),
    ("GET", "/rest/user?names=me%40example.test"),
    # Tracker.populate, comments, see_also, attachments
    ("GET", "/rest/bug/250000"),
    ("GET", "/rest/bug/250000/comment"),
    ("GET", "/rest/bug/250000?include_fields=see_also"),
    # Tracker.create, Issue.add_comment, .assign / .set, .open
    ("POST", "/rest/bug"),
    ("POST", "/rest/bug/250000/comment"),
    ("PUT", "/rest/bug/250000"),
)


class TestTheInjectorForwardsBugzilla(WkTest):
    def forward(self, method, target, **kw):
        sep = "&" if "?" in target else "?"
        head = ("%s %s%slogin=me%%40example.test&password=wk-injects-this HTTP/1.1"
                "\r\nHost: bugs.webkit.org\r\nContent-Length: 0\r\n\r\n"
                % (method, target, sep)).encode("latin-1")
        _, upstream, opened, logged = drive_injector(self.tmp, head, token=None, **kw)
        self.assertEqual([("bugs.webkit.org", 443)], opened,
                         "%s %s never left the injector" % (method, target))
        self.assertNotIn(b"wk-injects-this", upstream)
        self.assertNotIn(b"login=", upstream)
        self.assertNotIn(b"password=", upstream)
        self.assertIn(("%s %s" % (method, target)).encode("latin-1"), upstream)
        return upstream, logged

    def test_every_request_carries_the_key_while_push_is_on(self):
        for method, target in GIT_WEBKIT_BUGZILLA:
            with self.subTest(request="%s %s" % (method, target)):
                upstream, logged = self.forward(method, target,
                                                bugzilla_key="not-a-real-bugzilla-key")
                self.assertIn(b"api_key=not-a-real-bugzilla-key HTTP/1.1", upstream)
                self.assertIn("bugs.webkit.org", logged)
                self.assertIn(" inject ", logged)

    def test_every_read_goes_anonymous_while_push_is_off(self):
        for method, target in GIT_WEBKIT_BUGZILLA:
            if method not in ("GET", "HEAD"):
                continue
            with self.subTest(request="%s %s" % (method, target)):
                upstream, logged = self.forward(method, target)
                self.assertNotIn(b"api_key", upstream)
                self.assertIn(" unauthenticated ", logged)

    def test_every_write_is_refused_by_name_while_push_is_off(self):
        for method, target in GIT_WEBKIT_BUGZILLA:
            if method in ("GET", "HEAD"):
                continue
            with self.subTest(request="%s %s" % (method, target)):
                assert_refused_with_the_switch_off(
                    self, method, target, host="bugs.webkit.org",
                    extra="login=me%40example.test&password=wk-injects-this")

    def test_neither_credential_crosses_to_the_other_host(self):
        _, upstream, _, _ = drive_injector(
            self.tmp, b"POST /rest/bug?login=x&password=wk-injects-this HTTP/1.1\r\n"
            b"Host: bugs.webkit.org\r\nContent-Length: 0\r\n\r\n",
            token="ghp-write", read_token="ghp-read")
        self.assertNotIn(b"ghp-", upstream)
        self.assertNotIn(b"Authorization", upstream)
        _, upstream, _, logged = drive_injector(
            self.tmp, b"POST /repos/x/y/pulls HTTP/1.1\r\nHost: api.github.com\r\n"
            b"Content-Length: 0\r\n\r\n", token=None, bugzilla_key="bz-key")
        self.assertEqual(b"", upstream, "a write left the injector with push off")
        self.assertIn("write refused: push is off", logged)

    def test_the_log_names_the_clients_target_and_never_the_key(self):
        _, _, _, logged = drive_injector(
            self.tmp, b"GET /rest/bug/1?login=x&password=wk-injects-this HTTP/1.1\r\n"
            b"Host: bugs.webkit.org\r\n\r\n", token=None, bugzilla_key="bz-secret")
        self.assertIn("/rest/bug/1", logged)
        self.assertNotIn("bz-secret", logged)


class TestTheInjectorRecordsWhatTheFarEndAnswered(WkTest):
    """The request log alone cannot tell an injected credential the far end accepted from one it refused -- both
    read `..."""

    def drive(self, reply):
        head = (b"PUT /rest/bug/324270?login=me%40example.test&password=wk-injects-this"
                b" HTTP/1.1\r\nHost: bugs.webkit.org\r\nContent-Length: 0\r\n\r\n")
        client, _upstream, _opened, logged = drive_injector(
            self.tmp, head, token=None, bugzilla_key="not-a-real-bugzilla-key",
            upstream_reply=reply)
        return client, logged

    def test_a_refusal_is_named_in_the_log(self):
        client, logged = self.drive(
            b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        self.assertIn("400 Bad Request", logged, logged)
        self.assertIn("bugs.webkit.org", logged)

    def test_so_is_an_acceptance(self):
        _client, logged = self.drive(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        self.assertIn("200 OK", logged, logged)

    def test_the_reply_still_reaches_the_client_whole(self):
        reply = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 9\r\n\r\n{\"e\":1}\r\n"
        client, _logged = self.drive(reply)
        self.assertEqual(client, reply, client)

    def test_no_response_body_is_logged(self):
        reply = (b"HTTP/1.1 403 Forbidden\r\nContent-Length: 21\r\n\r\n"
                 b'{"message":"secret"}\n')
        _client, logged = self.drive(reply)
        self.assertIn("403 Forbidden", logged)
        self.assertNotIn("secret", logged)


class TestTheInjectorBoundsTheUpstream(WkTest):
    """A stalled or unreachable far end is answered 504 or 502 inside UPSTREAM_TIMEOUT, so the caller's own
    timeout never fires on a live injector."""

    HEAD = b"GET /user HTTP/1.1\r\nHost: api.github.com\r\nContent-Length: 0\r\n\r\n"

    def drive(self, connect):
        client, _up, _opened, logged = drive_injector(
            self.tmp, self.HEAD, read_token="t", connect=connect, upstream_timeout=0.3, read_timeout=0.3)
        return client, logged

    def test_a_connect_that_never_completes_is_a_504(self):
        async def never():
            await asyncio.sleep(60)
        client, logged = self.drive(never)
        self.assertIn(b"504 Gateway Timeout", client)
        self.assertIn(b"api.github.com did not answer", client)
        self.assertIn("TimeoutError", logged)
        self.assertNotIn(b"may have been applied", client)

    def test_a_server_that_accepts_and_stalls_is_a_504(self):
        async def stalled():
            async def hold(reader, writer):
                await asyncio.sleep(60)
            server = await asyncio.start_server(hold, "127.0.0.1", 0)
            self.addCleanup(server.close)
            return await REAL_OPEN_CONNECTION("127.0.0.1", server.sockets[0].getsockname()[1])
        client, logged = self.drive(stalled)
        self.assertIn(b"504 Gateway Timeout", client)
        self.assertIn(b"the request was sent; it may have been applied", client)

    def test_the_status_line_outlasts_the_connect_bound(self):
        async def slow():
            async def late(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                await asyncio.sleep(0.5)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
            server = await asyncio.start_server(late, "127.0.0.1", 0)
            self.addCleanup(server.close)
            return await REAL_OPEN_CONNECTION("127.0.0.1", server.sockets[0].getsockname()[1])
        client, _up, _o, _l = drive_injector(
            self.tmp, self.HEAD, read_token="t", connect=slow, upstream_timeout=0.2, read_timeout=5)
        self.assertIn(b"200 OK", client)

    def test_a_tls_verification_failure_is_a_502_naming_the_injector(self):
        async def bad_ca():
            raise ssl.SSLCertVerificationError("certificate verify failed")
        client, logged = self.drive(bad_ca)
        self.assertIn(b"502 Bad Gateway", client)
        self.assertIn(b"X-Wk-Injector: SSLCertVerificationError", client)
        self.assertIn(b"failed to verify or resolve api.github.com", client)

    def test_a_dns_failure_is_a_502_naming_the_injector(self):
        async def no_dns():
            raise socket.gaierror(-2, "Name or service not known")
        client, _ = self.drive(no_dns)
        self.assertIn(b"502 Bad Gateway", client)
        self.assertIn(b"X-Wk-Injector: gaierror", client)

    def test_an_unreachable_network_is_a_502(self):
        async def unreachable():
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        client, _ = self.drive(unreachable)
        self.assertIn(b"502 Bad Gateway", client)
        self.assertNotIn(b"X-Wk-Injector", client)

    def test_a_refused_connect_is_a_502_naming_the_host(self):
        async def refused():
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            return await REAL_OPEN_CONNECTION("127.0.0.1", port)
        client, logged = self.drive(refused)
        self.assertIn(b"502 Bad Gateway", client)
        self.assertIn(b"api.github.com", client)
        self.assertIn("ConnectionRefusedError", logged)


class TestTheInjectorForwardsEverything(WkTest):
    """The decision, and it is a decision rather than an oversight: the injector refuses nothing on policy."""

    def forward(self, method, target, body=b"", **kw):
        """Drives the relay and returns what api.github.com received."""
        head = ("%s %s HTTP/1.1\r\nHost: api.github.com\r\n"
                "Content-Length: %d\r\n\r\n"
                % (method, target, len(body))).encode("latin-1")
        client, upstream, opened, logged = drive_injector(
            self.tmp, head + body, **kw)
        self.assertEqual([("api.github.com", 443)], opened,
                         "%s %s never left the injector: %r" % (method, target, client))
        self.assertIn(("%s %s HTTP/1.1" % (method, target)).encode("latin-1"),
                      upstream)
        return upstream, logged

    def test_every_read_git_webkit_makes_is_forwarded(self):
        for method, target in GIT_WEBKIT_READS:
            with self.subTest(request="%s %s" % (method, target)):
                _, logged = self.forward(method, target, read_token="ghp-read-only")
                self.assertIn("read inject", logged)

    def test_every_write_git_webkit_makes_is_forwarded(self):
        for method, target in GIT_WEBKIT_WRITES:
            with self.subTest(request="%s %s" % (method, target)):
                _, logged = self.forward(method, target)
                self.assertIn("write inject", logged)

    def test_a_path_git_webkit_never_asks_for_is_read_all_the_same(self):
        for method in ("GET", "HEAD"):
            for target in READS_WITH_NO_GIT_WEBKIT_CALLER:
                with self.subTest(request="%s %s" % (method, target)):
                    self.forward(method, target, read_token="ghp-read-only")

    def test_a_read_is_open_where_it_is_sensitive_too(self):
        for target in ("/repos/WebKit/WebKit/actions/secrets",
                       "/repos/an-employer/private-thing/contents/secrets.txt",
                       "/user/emails"):
            with self.subTest(target=target):
                self.forward("GET", target, read_token="ghp-read-only")

    def test_a_hostile_write_is_forwarded_and_carries_the_switchs_token(self):
        for method, target in HOSTILE_WRITES:
            with self.subTest(request="%s %s" % (method, target)):
                upstream, _ = self.forward(method, target)
                self.assertIn(b"Authorization: Bearer ghp-not-a-real-token",
                              upstream)

    def test_a_hostile_write_is_refused_by_name_with_the_switch_off(self):
        for method, target in HOSTILE_WRITES:
            with self.subTest(request="%s %s" % (method, target)):
                assert_refused_with_the_switch_off(
                    self, method, target, read_token="ghp-read-only")

    def test_a_path_a_server_would_normalise_is_forwarded_unchanged(self):
        for target in ("/repos/a/b/pulls/../../../../user",
                       "/repos/a/b/%2e%2e/%2e%2e/keys",
                       "//repos/a/b/pulls"):
            with self.subTest(target=target):
                self.forward("POST", target)

    def test_a_request_line_this_cannot_read_is_a_write(self):
        for line in (b"GET", b"GET /user", b"GET /user HTTP/1.1 extra",
                     b"GET https://api.github.com/user HTTP/1.1"):
            with self.subTest(line=line):
                _, upstream, opened, logged = drive_injector(
                    self.tmp, line + b"\r\nHost: api.github.com\r\n\r\n",
                    read_token="ghp-read-only")
                self.assertEqual([("api.github.com", 443)], opened)
                self.assertIn(line, upstream)
                self.assertIn(b"Authorization: Bearer ghp-not-a-real-token",
                              upstream)
                self.assertIn("write inject", logged)


class TestTheGraphQLReadWriteSplit(WkTest):
    """GraphQL is the whole API behind one path, so the method cannot decide read from write there and the
    document does."""

    def setUp(self):
        super().setUp()
        self.m = _load(INJECT, "wkinject")

    def test_a_query_is_a_read_and_anything_else_a_write(self):
        search = (b'{"query": "query { search(query: \\"repo:WebKit/WebKit is:pr\\", type: ISSUE, last: 100) '
                  b'{ edges { node { number } } } }"}')
        both = (b'{"query": "query Find { viewer { login } } mutation Land { mergePullRequest(input: {}) '
                b'{ clientMutationId } }", "operationName": "Land"}')
        for method, path, body, read in (
                ("POST", "/graphql", search, True),
                ("POST", "/graphql", b'{"query": "mutation { deleteRef(input: {}) }"}', False),
                ("POST", "/graphql", both, False),
                ("POST", "/graphql", b'{"query": "Mutation { x }"}', False),
                ("POST", "/graphql", b'{"query": "mUtAtIoN { x }"}', False),
                ("POST", "/graphql?anything", b'{"query": "query { x }"}', True),
                ("POST", "/graphql?anything", b'{"query": "mutation { x }"}', False),
                ("PUT", "/graphql", b'{"query": "query { x }"}', False)):
            with self.subTest(method=method, path=path, body=body):
                self.assertEqual(read, self.m.is_read(method, path, body))

    def test_a_query_spends_the_read_token_and_a_mutation_the_switchs(self):
        for body, token, half in (
                (b'{"query": "query { viewer { login } }"}', b"ghp-read-only", "read"),
                (b'{"query": "mutation { deleteRef(input: {}) }"}',
                 b"ghp-not-a-real-token", "write")):
            with self.subTest(half=half):
                _, upstream, opened, logged = drive_injector(
                    self.tmp,
                    b"POST /graphql HTTP/1.1\r\nHost: api.github.com\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(body) + body,
                    read_token="ghp-read-only")
                self.assertEqual([("api.github.com", 443)], opened)
                self.assertIn(b"Authorization: Bearer " + token, upstream)
                self.assertIn("%s inject POST /graphql" % half, logged)


class TestTheTwoTokens(WkTest):
    """Which token a request spends, which is the whole of what `wk key push` switches."""

    def setUp(self):
        super().setUp()
        self.m = _load(INJECT, "wkinject")
        self.pat = self.tmp / "push-github-pat"
        self.read_pat = self.tmp / "read-github-pat"
        self.inj = self.m.Injector(str(self.pat), str(self.read_pat),
                                   str(self.tmp / "push-bugzilla-api-key"), None)

    def test_bugzilla_spends_the_switchs_key_and_never_a_github_token(self):
        self.read_pat.write_text("ghp-read-only\n")
        self.pat.write_text("ghp-the-write-token\n")
        for reading in (True, False):
            self.assertEqual("", self.inj.token_for(self.m.BUGZILLA, reading))
        (self.tmp / "push-bugzilla-api-key").write_text("not-a-real-bugzilla-key\n")
        for reading in (True, False):
            self.assertEqual("not-a-real-bugzilla-key",
                             self.inj.token_for(self.m.BUGZILLA, reading))

    def test_a_read_spends_the_read_token_else_the_switchs_and_a_write_only_the_switchs(self):
        for write, read, reading, want in (
                (None, "r", True, "r"), ("w", None, True, "w"), ("w", "r", True, "r"),
                (None, "r", False, ""), ("w", "r", False, "w")):
            with self.subTest(write=write, read=read, reading=reading):
                for path, value in ((self.pat, write), (self.read_pat, read)):
                    path.unlink(missing_ok=True)
                    if value:
                        path.write_text(value + "\n")
                self.assertEqual(want, self.inj.token_for(self.m.GITHUB, reading))

    def test_a_read_carries_the_read_token_with_the_switch_off(self):
        _, upstream, opened, logged = drive_injector(
            self.tmp,
            b"GET /repos/WebKit/WebKit/pulls/1234 HTTP/1.1\r\n"
            b"Host: api.github.com\r\n\r\n",
            token=None, read_token="ghp-read-only")
        self.assertEqual([("api.github.com", 443)], opened)
        self.assertIn(b"Authorization: Bearer ghp-read-only", upstream)
        self.assertIn("read inject GET /repos/WebKit/WebKit/pulls/1234", logged)

def run_bridge(ca, *print_vars, bugzilla_user=None, github_user=None,
               env_extra=None):
    """(what the wrapped command printed, the bundle's path, its bytes)."""
    d = Path(tempfile.mkdtemp(prefix="wk-test-gh-env-"))
    try:
        secrets = d / "secrets"
        secrets.mkdir()
        if bugzilla_user is not None:
            (secrets / "bugzilla-user").write_text(bugzilla_user + "\n")
        if github_user is not None:
            (secrets / "github-user").write_text(github_user + "\n")
        tools = d / "wk-tools"
        (tools / "shell").mkdir(parents=True)
        (tools / "container" / "proxy").mkdir(parents=True)
        (tools / "shell" / "path.sh").write_text(":\n")
        (tools / "container" / "proxy" / "bridge.py").write_text(
            "import time; time.sleep(30)\n")
        runwk = d / "run-wk"
        runwk.mkdir()
        if ca:
            (runwk / "wk-github-ca.pem").write_text("THE-INJECTORS-CA\n")
        sysca = d / "ca-certificates.crt"
        sysca.write_text("THE-SYSTEM-STORE\n")
        script = d / "ensure-bridge.sh"
        script.write_text(
            (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
            .replace("/opt/wk-tools", str(tools))
            .replace("/run/wk/wk-github-ca.pem", str(runwk / "wk-github-ca.pem"))
            .replace("/etc/ssl/certs/ca-certificates.crt", str(sysca))
            .replace("/secrets/", str(secrets) + "/"))
        rw = d / "rw"
        rw.mkdir()
        show = "; ".join('printf "%s=[%s]\\n" ' + v + ' "${' + v + ':-}"'
                         for v in print_vars)
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_TOKEN", "SSL_CERT_FILE", "SSL_CERT_DIR",
                            "PYTHON_KEYRING_BACKEND", "BUGS_WEBKIT_ORG_USERNAME",
                            "BUGS_WEBKIT_ORG_PASSWORD")}
        env["TMPDIR"] = str(rw)
        env.update(env_extra or {})
        cp = subprocess.run(["bash", str(script), "sh", "-c", show],
                            env=env, capture_output=True, text=True,
                            timeout=60)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        bundle = rw / ".wk-ca-bundle.pem"
        text = bundle.read_text() if bundle.exists() else ""
        return cp.stdout, bundle, text
    finally:
        shutil.rmtree(d, ignore_errors=True)


class TestWhatGhNeeds(unittest.TestCase):
    """`gh` reads GitHub through the same injector as everything else, so it needs two things nothing else does:
    a token to put in an Authorization header for the injector to replace, and the CA in the two variables
    Go's crypto/x509 reads -- it reads none of REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE or GIT_SSL_CAINFO."""

    def test_what_each_workspace_is_handed(self):
        names = ("GH_TOKEN", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHON_KEYRING_BACKEND", "BUGS_WEBKIT_ORG_USERNAME",
                 "BUGS_WEBKIT_ORG_PASSWORD")
        null = "keyring.backends.null.Keyring"
        for ca, user, want in (
                (True, "me@example.test", ("wk-injects-this", "BUNDLE", "/etc/ssl/certs", null, "me@example.test",
                                           "wk-injects-this")),
                (True, None, ("wk-injects-this", "BUNDLE", "/etc/ssl/certs", null, "", "")),
                (False, None, ("", "", "", null, "", ""))):
            with self.subTest(ca=ca, user=user):
                out, bundle, _ = run_bridge(ca, *names, bugzilla_user=user)
                for name, value in zip(names, want):
                    self.assertIn("%s=[%s]" % (name, str(bundle) if value == "BUNDLE" else value), out)

    def test_the_bundle_it_names_is_the_systems_plus_the_ca(self):
        _, _, text = run_bridge(True, "SSL_CERT_FILE")
        self.assertEqual("THE-SYSTEM-STORE\nTHE-INJECTORS-CA\n", text)

class TestTheWorkspaceHoldsThePlaceholder(unittest.TestCase):
    """Both targets set the same two variables and the same CA bundle, from the one wrapper each of them already
    goes through."""

    def test_every_way_into_a_container_goes_through_the_wrapper(self):
        c = wk_targets.Container("c", REPO, {"WK_CONTAINER_USER": "dev", "WK_IN_VM": "1"}, Fake("here"))
        bridge = "/opt/wk-tools/container/proxy/ensure-bridge.sh"
        for tty in (False, True):
            with self.subTest(exec_tty=tty):
                argv, _ = c.exec_argv("a", ["true"], tty)
                self.assertEqual([bridge, "true"], argv[-2:])
        self.assertIn("exec %s " % bridge, c.sshd_cmd("dev"))

    def test_enter_argv_goes_through_the_bridge_too(self):
        c = wk_targets.Container("c", REPO, {"WK_CONTAINER_USER": "dev"}, Fake("here"))
        argv, _ = c.enter_argv("a")
        self.assertIn("/opt/wk-tools/container/proxy/ensure-bridge.sh", argv)
        self.assertIn("--login", argv)

    def test_a_full_temp_directory_still_runs_the_command(self):
        import os
        import stat
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="wk-test-ro-tmp-"))
        try:
            tools = d / "wk-tools"
            (tools / "shell").mkdir(parents=True)
            (tools / "container" / "proxy").mkdir(parents=True)
            (tools / "shell" / "path.sh").write_text(":\n")
            (tools / "container" / "proxy" / "bridge.py").write_text(
                "import time; time.sleep(30)\n")
            runwk = d / "run-wk"
            runwk.mkdir()
            (runwk / "wk-github-ca.pem").write_text(
                "-----BEGIN CERTIFICATE-----\nnot a real one\n")
            sysca = d / "ca-certificates.crt"
            sysca.write_text("-----BEGIN CERTIFICATE-----\nsystem\n")

            script = d / "ensure-bridge.sh"
            script.write_text(
                (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
                .replace("/opt/wk-tools", str(tools))
                .replace("/run/wk/wk-github-ca.pem", str(runwk / "wk-github-ca.pem"))
                .replace("/etc/ssl/certs/ca-certificates.crt", str(sysca)))

            ro = d / "ro"
            ro.mkdir()
            os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
            cp = subprocess.run(
                ["bash", str(script), "sh", "-c", "echo the-command-ran"],
                env={**os.environ, "TMPDIR": str(ro)},
                capture_output=True, text=True, timeout=60)
            os.chmod(ro, 0o755)
            left = sorted(p.name for p in ro.iterdir())
        finally:
            shutil.rmtree(d, ignore_errors=True)

        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("the-command-ran", cp.stdout)
        self.assertIn("wk:", cp.stderr, "it failed silently rather than saying so")
        self.assertEqual([], left,
                         "a half-written temp file was left in the full directory")

EGRESS = (
    "# wk: written by lib/wk/guest.py on every start\n"
    "export http_proxy=http://192.168.2.1:3128\n"
    "export https_proxy=http://192.168.2.1:3128\n"
    "export HTTP_PROXY=http://192.168.2.1:3128\n"
    "export HTTPS_PROXY=http://192.168.2.1:3128\n"
    "export no_proxy=localhost,127.0.0.1,::1\n"
    "export NO_PROXY=localhost,127.0.0.1,::1\n"
)

LEGACY = (
    "# wk-tools: egress goes through the proxy on the host; Softnet denies the rest.\n"
    "export http_proxy=http://192.168.64.1:3128\n"
    "export https_proxy=http://192.168.64.1:3128\n"
    "export HTTP_PROXY=http://192.168.64.1:3128\n"
    "export HTTPS_PROXY=http://192.168.64.1:3128\n"
    "export no_proxy=localhost,127.0.0.1,::1\n"
    "export NO_PROXY=localhost,127.0.0.1,::1\n"
)


def _wire(home, times=1):
    """Run the guest's shell wiring, as lib/wk/guest.py streams it in on every start."""
    for _ in range(times):
        cp = subprocess.run(
            ["bash", str(SHELL_RC), str(REPO), str(home / "agent-rw")],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60,
        )
        assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


def _shell_vars(shell, home, args):
    """Every proxy variable a shell started with `args` ends up with."""
    printer = "; ".join(f'echo "{v}=${v}"' for v in VARS)
    cp = subprocess.run(
        [shell, *args, printer],
        cwd=str(REPO),
        env={"HOME": str(home), "TERM": "dumb",
             "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120,
    )
    out = {}
    for line in cp.stdout.splitlines():
        k, _, v = line.partition("=")
        if k in VARS:
            out[k] = v
    return out


class TestTheEditorsTerminalGetsTheSameEnvironment(unittest.TestCase):
    """An editor reaches a container over ssh, and sshd builds a session's environment from scratch: it hands on
    nothing of the wrapper's, so a terminal pane would go without the injected credentials and the keyring
    backend that every other way in has -- `git-webkit pr` there hunts a keyring no container has and exits
    reporting a locked macOS Keychain."""

    def setenv_line(self, *args, **kwargs):
        """(the line the wrapper published, the bundle it named in it)."""
        out, bundle, _ = run_bridge(*args, "WK_SSH_SETENV", **kwargs)
        for line in out.splitlines():
            if line.startswith("WK_SSH_SETENV="):
                return line[len("WK_SSH_SETENV=["):-1], bundle
        self.fail("the wrapper printed no WK_SSH_SETENV: " + out)

    def test_it_carries_every_variable_the_wrapper_sets(self):
        line, bundle = self.setenv_line(True, github_user="justinmichaud",
                                        bugzilla_user="me@example.test")
        for pair in ("PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring",
                     "GITHUB_COM_USERNAME=justinmichaud",
                     "GITHUB_COM_TOKEN=wk-injects-this",
                     "BUGS_WEBKIT_ORG_USERNAME=me@example.test",
                     "BUGS_WEBKIT_ORG_PASSWORD=wk-injects-this",
                     "GH_TOKEN=wk-injects-this",
                     "SSL_CERT_FILE=%s" % bundle):
            with self.subTest(pair=pair):
                self.assertIn(pair, line.split(" "))

    def test_it_carries_the_proxy_the_container_was_created_with(self):
        line, _ = self.setenv_line(True, env_extra={
            "http_proxy": "http://127.0.0.1:3128",
            "NO_PROXY": "localhost,127.0.0.1,::1"})
        self.assertIn("http_proxy=http://127.0.0.1:3128", line)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,::1", line)

    def test_a_value_with_whitespace_is_left_out_rather_than_truncating_it(self):
        out, _, _ = run_bridge(True, "WK_SSH_SETENV", "GITHUB_COM_USERNAME",
                               github_user="two words")
        self.assertIn("GITHUB_COM_USERNAME=[two words]", out)
        self.assertNotIn("GITHUB_COM_USERNAME=two", out)
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", out)

    def test_sshd_is_exec_d_with_that_line_in_one_setenv(self):
        d = Path(tempfile.mkdtemp(prefix="wk-test-sshd-env-"))
        try:
            wrapper = d / "wrapper.sh"
            wrapper.write_text('#!/bin/bash\n'
                               'export WK_SSH_SETENV="A=1 B=2"\nexec "$@"\n')
            sshd = d / "sshd"
            sshd.write_text('#!/bin/bash\n'
                            'for a in "$@"; do printf "ARG=[%s]\\n" "$a"; done\n')
            for f in (wrapper, sshd):
                f.chmod(0o755)
            cmd = (wk_targets.Container("c", REPO, {}, Fake("here")).sshd_cmd("tester")
                   .replace("mkdir -p /run/sshd && ", "")
                   .replace("/opt/wk-tools/container/proxy/ensure-bridge.sh",
                            str(wrapper))
                   .replace("/usr/sbin/sshd", str(sshd)))
            cp = subprocess.run(["sh", "-c", cmd], capture_output=True,
                                text=True, timeout=60)
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            args = cp.stdout.splitlines()
            self.assertEqual(["ARG=[SetEnv=A=1 B=2]"],
                             [a for a in args if "SetEnv" in a])
            self.assertIn("ARG=[Subsystem=sftp internal-sftp]", args)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestGuestProxyEnvironment(WkTest):
    """Which shells in a guest have egress."""

    # What a person and what `wk` actually start, and how each is spelled.
    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh (the guest's own window)": ("zsh", ["-l", "-c"]),
        "bash -lc (every Target.exec)": ("bash", ["-lc"]),
        "interactive bash": ("bash", ["-i", "-c"]),
    }

    def _home(self, egress=True):
        h = self.tmp / "home"
        h.mkdir(exist_ok=True)
        if egress:
            (h / ".wk-egress").write_text(EGRESS)
        _wire(h)
        return h

    def test_every_shell_that_reads_an_rc_has_the_proxy(self):
        home = self._home()
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                got = _shell_vars(shell, home, args)
                self.assertEqual(got.get("http_proxy"), "http://192.168.2.1:3128", got)
                self.assertEqual(got.get("HTTPS_PROXY"), "http://192.168.2.1:3128", got)
                self.assertEqual(got.get("NO_PROXY"), "localhost,127.0.0.1,::1", got)

    def test_a_guest_with_no_egress_file_gets_no_proxy(self):
        home = self._home(egress=False)
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                got = _shell_vars(shell, home, args)
                for v in VARS:
                    self.assertEqual(got.get(v), "", f"{v} set from nothing: {got}")

    def test_the_wiring_is_idempotent(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / ".wk-egress").write_text(EGRESS)
        _wire(home, times=3)
        for rc in (".zshrc", ".zprofile", ".bash_profile", ".bashrc"):
            text = (home / rc).read_text()
            with self.subTest(rc=rc):
                self.assertEqual(1, text.count('if [ -r "$HOME/.wk-egress" ]; then'), text)
                self.assertEqual(1, text.count("shell/bashrc"), text)


class TestGuestConvergence(WkTest):
    """A guest cloned from a base that baked the address into its profiles converges on the next start, rather
    than needing the base rebuilt."""

    def test_a_stale_baked_in_address_is_stripped_and_replaced(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / ".zprofile").write_text(LEGACY)
        (home / ".bash_profile").write_text(LEGACY + 'echo "mine stays"\n')
        (home / ".wk-egress").write_text(EGRESS)

        _wire(home, times=2)

        for rc in (".zprofile", ".bash_profile", ".zshrc", ".bashrc"):
            text = (home / rc).read_text()
            with self.subTest(rc=rc):
                self.assertNotIn("192.168.64.1", text, text)
                self.assertNotIn("egress goes through", text, text)
                self.assertIn(".wk-egress", text, text)
        self.assertIn("mine stays", (home / ".bash_profile").read_text())

    @unittest.skipUnless(shutil.which("zsh"), "no zsh on this machine")
    def test_the_converged_guest_ends_up_with_the_live_address(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / ".zprofile").write_text(LEGACY)
        (home / ".wk-egress").write_text(EGRESS)
        _wire(home)
        got = _shell_vars("zsh", home, ["-l", "-c"])
        self.assertEqual(got.get("https_proxy"), "http://192.168.2.1:3128", got)


FIRSTRUN = REPO / "container" / "firstrun.sh"


def _shell_function(path, name):
    """One function lifted out of a shell file, so a test can call it without running the rest of the file
    (firstrun.sh installs a whole workspace)."""
    text = path.read_text()
    start = text.index(f"{name}() {{")
    return text[start:text.index("\n}\n", start) + 3]


def _sandbox_env():
    """The proxy variables the container target starts a container with (`Container.sandbox_flags`), asked of a
    fake machine: the runtime directory is made under /run, which a test may not do."""
    sys.path.insert(0, str(REPO / "lib"))
    from wk import targets
    from wk.machine import Fake
    flags = targets.Container("container", str(REPO), {"XDG_RUNTIME_DIR": "/run/user/1"}, Fake("here")).sandbox_flags("armhf")
    return dict(v.split("=", 1) for k, v in zip(flags[0::2], flags[1::2]) if k == "--env")


class TestAptGoesThroughTheProxy(unittest.TestCase):
    """Every apt step in container/firstrun.sh runs under sudo, whose env_reset drops http_proxy/https_proxy, and
    the container is `--network none`: apt then dials the archive directly and has no route at all."""

    def _drop_in(self, **env):
        cp = subprocess.run(
            ["bash", "-c", _shell_function(FIRSTRUN, "apt_proxy_conf") + "\napt_proxy_conf\n"],
            cwd=str(REPO), capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", **env})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_both_schemes_are_named(self):
        out = self._drop_in(http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9")
        self.assertIn('Acquire::http::Proxy "http://127.0.0.1:9";', out)
        self.assertIn('Acquire::https::Proxy "http://127.0.0.1:9";', out)

    def test_the_address_is_the_one_the_container_is_started_with(self):
        env = _sandbox_env()
        for var in ("http_proxy", "https_proxy"):
            self.assertIn(var, env, env)
        out = self._drop_in(http_proxy=env["http_proxy"], https_proxy=env["https_proxy"])
        self.assertIn('Acquire::http::Proxy "%s";' % env["http_proxy"], out)
        self.assertIn('Acquire::https::Proxy "%s";' % env["https_proxy"], out)

    def _write_step(self, **env):
        """The block that writes the drop-in, lifted and run with `sudo` and the file path made harmless."""
        text = FIRSTRUN.read_text()
        start = text.index('if [ -n "${http_proxy:-}"')
        block = text[start:text.index("\nfi\n", start) + 4]
        conf = Path(tempfile.mkdtemp(prefix="wk-test-aptconf-")) / "99-wk-proxy"
        script = (
            'set -euo pipefail\n'
            'log()  { printf "LOG %s\\n" "$*"; }\n'
            'warn() { printf "WARN %s\\n" "$*"; }\n'
            'sudo() { "$@"; }\n'
            + "APT_PROXY_CONF=" + str(conf) + "\n"
            + _shell_function(FIRSTRUN, "apt_proxy_conf") + "\n" + block)
        cp = subprocess.run(["bash", "-c", script], cwd=str(REPO), capture_output=True,
                            text=True, timeout=30, env={"PATH": "/usr/bin:/bin", **env})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr, (conf.read_text() if conf.exists() else "")

    def test_an_address_carrying_apt_conf_syntax_is_refused_by_name(self):
        out, wrote = self._write_step(
            http_proxy='http://127.0.0.1:9";Acquire::http::Proxy "http://elsewhere:8080',
            https_proxy="http://127.0.0.1:9")
        self.assertIn("WARN", out, out)
        self.assertIn("apt.conf's own syntax", out, out)
        self.assertEqual(wrote, "", "it wrote the drop-in anyway")
        out, wrote = self._write_step(http_proxy="http://127.0.0.1:9",
                                      https_proxy="http://127.0.0.1:9;")
        self.assertIn("WARN", out, out)
        self.assertEqual(wrote, "", "a semicolon in the https address was written")

    def test_an_ordinary_address_is_still_written(self):
        out, wrote = self._write_step(http_proxy="http://127.0.0.1:9",
                                      https_proxy="http://127.0.0.1:9")
        self.assertIn("LOG apt goes through the workspace proxy", out, out)
        self.assertIn('Acquire::http::Proxy "http://127.0.0.1:9";', wrote)


if __name__ == "__main__":
    unittest.main()
