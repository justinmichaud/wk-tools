"""How a workspace reaches the outside: what the proxy allows, what the credential injector does to each request, and how
the variables naming that proxy get into a workspace's and a macOS guest's shells."""
import asyncio
import contextlib
import errno
import functools
import importlib.util
import io
import json
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from tests.support import REPO, WkTest
from tests.test_push_service import Proc

sys.path.insert(0, str(REPO / "lib"))
from wk import places as wk_places, pushgate  # noqa: E402
from tests.fakes import Fake  # noqa: E402

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"
INJECT = REPO / "container" / "proxy" / "github-inject.py"
SHELL_RC = REPO / "vm" / "shell-rc.sh"

# curl and git read the lowercase pair, some tools only the uppercase, and no_proxy keeps loopback off the proxy.
VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")

REAL_OPEN_CONNECTION = asyncio.open_connection   # inject() patches asyncio.open_connection for the length of a run


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Sink:
    """A StreamWriter as far as either program uses one."""

    def __init__(self):
        self.data = bytearray()

    def write(self, b):
        self.data += b

    async def drain(self):
        pass

    def close(self):
        pass


def _reader(data=b""):
    """A StreamReader holding `data` then EOF; built inside the running loop."""
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


# (host, ports, verdict): "tunnel" is allowed as a plain tunnel, "inject" is allowed and routed to the
# credential injector, "refuse" is refused. Suffix entries match on a dot boundary only.
ALLOWLIST = (
    [(h, (443,), "tunnel") for h in ("registry.npmjs.org", "formulae.brew.sh", "ghcr.io", "crates.io", "static.crates.io",
                                    "static.rust-lang.org", "sh.rustup.rs", "developer.apple.com",
                                    "download.developer.apple.com", "webkit.org", "trac.webkit.org", "build.webkit.org",
                                    "codeload.github.com", "raw.githubusercontent.com")]
    + [(h, (80, 443), "tunnel") for h in ("valid.apple.com", "ocsp.apple.com", "crl.apple.com", "i.pki.goog",
                                         "archive.ubuntu.com", "ports.ubuntu.com", "security.ubuntu.com",
                                         "ddebs.ubuntu.com", "apt.llvm.org")]
    + [("github.com", (443,), "tunnel"), ("github.com", (22,), "refuse")]
    + [(h, (443,), "inject") for h in ("api.github.com", "bugs.webkit.org", "api.anthropic.com", "claude.ai",
                                      "platform.claude.com")]
    + [("api.github.com", (22, 80, 9418), "refuse"), ("bugs.webkit.org", (80, 22), "refuse"),
       ("ddebs.ubuntu.com", (22,), "refuse")]
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
        p = _load(PROXY, "wkproxy").Policy(tempfile.mkdtemp(prefix="wk-test-store-"))
        for host, ports, verdict in ALLOWLIST:
            for port in ports:
                with self.subTest(host=host, port=port):
                    ok, why = p.host_allowed(host, port)
                    self.assertEqual(verdict != "refuse", ok, why)
                    if ok:
                        self.assertEqual(verdict == "inject", "injector" in why, why)


# (request, what `handle` asked open_upstream for, how the client's reply starts, how the upstream's bytes start). A host
# is case-insensitive and may carry a trailing dot; `GET https://...` (axios behind a proxy, the Claude CLI's policy
# fetch) expects the proxy to originate TLS.
ROUTES = (
    [(b"CONNECT %s HTTP/1.1\r\n\r\n" % t, [("api.github.com", 443, False)], b"HTTP/1.0 200 Connection established", b"")
     for t in (b"api.github.com:443", b"API.GITHUB.COM:443", b"api.github.com.:443", b"Api.GitHub.Com.:443")]
    + [(b"CONNECT UPLOADS.GITHUB.COM:443 HTTP/1.1\r\n\r\n", [], b"HTTP/1.1 403 Forbidden", b""),
       (b"GET https://api.anthropic.com/api/claude_code/policy_limits HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
        [("api.anthropic.com", 443, True)], b"HTTP/1.1 204 No Content", b"GET /api/claude_code/policy_limits HTTP/1.1\r\n")]
)


class TestTheProxy(unittest.TestCase):
    def setUp(self):
        self.m = _load(PROXY, "wkproxy")

    def test_each_request_is_routed_or_refused_on_one_spelling_of_its_host(self):
        for request, want, reply, forwarded in ROUTES:
            with self.subTest(request=request):
                proxy = self.m.Proxy(self.m.Policy(tempfile.mkdtemp(prefix="wk-test-store-")))
                seen, client, upstream = [], Sink(), Sink()

                async def open_upstream(host, port, tls=False, caller=None):
                    seen.append((host, port, tls))
                    return _reader(b"HTTP/1.1 204 No Content\r\n\r\n" if forwarded else b""), upstream

                async def drive():
                    await proxy.handle(_reader(request), client)
                proxy.open_upstream = open_upstream
                with contextlib.redirect_stderr(io.StringIO()):
                    asyncio.run(drive())
                self.assertEqual(want, seen, client.data)
                self.assertTrue(bytes(client.data).startswith(reply), client.data)
                self.assertTrue(bytes(upstream.data).startswith(forwarded), upstream.data)

    def test_the_scheme_decides_tls_and_the_default_port(self):
        for target, want in (
                ("https://api.anthropic.com/api/claude_code/policy_limits",
                 ("api.anthropic.com", 443, True, "/api/claude_code/policy_limits")),
                ("http://archive.ubuntu.com/ubuntu/pool", ("archive.ubuntu.com", 80, False, "/ubuntu/pool")),
                ("https://h:8443/", ("h", 8443, True, "/")),
                ("http://h:8080/", ("h", 8080, False, "/"))):
            with self.subTest(target=target):
                self.assertEqual(want, self.m.parse_absolute_target(target))

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_tls_is_originated_only_when_asked_and_an_injected_host_is_the_injectors_socket(self):
        """93.184.216.34 is an address literal, so nothing is resolved."""
        d = Path(tempfile.mkdtemp(prefix="wk-test-ca-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        _load(INJECT, "wkinject").ensure_certs(str(d / "certs"), str(d / "ca.pem"))
        self.m.INJECT_CA = str(d / "ca.pem")
        calls = []

        async def tcp(addr, port, **kw):
            calls.append((addr, kw.get("server_hostname"), kw.get("ssl") is not None))
            return None, Sink()

        async def unix(path, **kw):
            calls.append((path, kw.get("server_hostname"), kw.get("ssl") is not None))
            return None, Sink()

        async def run(host, port, tls):
            with unittest.mock.patch.object(self.m.asyncio, "open_connection", tcp), \
                    unittest.mock.patch.object(self.m.asyncio, "open_unix_connection", unix):
                await self.m.Proxy(self.m.Policy(str(d))).open_upstream(host, port, tls)
        for host, port, tls in (("93.184.216.34", 443, True), ("93.184.216.34", 80, False),
                                ("api.anthropic.com", 443, True), ("api.anthropic.com", 443, False)):
            asyncio.run(run(host, port, tls))
        sock = self.m.INJECT_SOCKET
        self.assertEqual([("93.184.216.34", "93.184.216.34", True), ("93.184.216.34", None, False),
                          (sock, "api.anthropic.com", True), (sock, None, False)], calls)


class _Peer:
    def __init__(self, sock):
        self.sock = sock

    def get_extra_info(self, name):
        return self.sock if name == "socket" else ("192.0.2.9", 5555)


class TestTheProxyPicksTheInjectorsSocket(unittest.TestCase):
    """A write goes to the writing socket only when the push service finds no agent in the caller's workspace."""

    def setUp(self):
        self.m = _load(PROXY, "wkproxy")
        self.tmp = tempfile.mkdtemp(prefix="wk-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.m.PUSH_SOCKET = os.path.join(self.tmp, "push.sock")
        self.asked = []

    def service(self, reply):
        async def handle(r, w):
            self.asked.append(json.loads(await r.readline()))
            w.write(reply)
            await w.drain()
            w.close()
        return asyncio.start_unix_server(handle, path=self.m.PUSH_SOCKET)

    def may_write(self, reply, peer=None):
        async def run():
            if reply is None:
                return await self.m.may_write(peer or {"pid": 4})
            server = await self.service(reply)
            try:
                return await self.m.may_write(peer or {"pid": 4})
            finally:
                server.close()
        return asyncio.run(run())

    def test_only_a_yes_is_a_yes(self):
        self.assertTrue(self.may_write(b'{"writes": true, "why": ""}\n'))
        self.assertEqual([{"pid": 4, "verb": "writes"}], self.asked)
        for reply in (b'{"writes": false, "why": "an agent"}\n', b"garbage\n", b'{"writes": "yes"}\n', b"", None):
            with self.subTest(reply=reply):
                self.assertFalse(self.may_write(reply))

    def test_a_guest_is_named_by_its_address(self):
        self.assertTrue(self.may_write(b'{"writes": true}\n', {"addr": "192.0.2.9"}))
        self.assertEqual({"addr": "192.0.2.9", "verb": "writes"}, self.asked[0])

    @unittest.skipUnless(sys.platform == "linux", "a unix caller is its pid by SO_PEERCRED, which only Linux has; a Mac's own proxy takes guests over TCP alone")
    def test_the_caller_is_a_pid_on_a_unix_socket_and_an_address_on_a_tcp_one(self):
        a, b = socket.socketpair(socket.AF_UNIX)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertEqual({"pid": os.getpid()}, self.m.peer_of(_Peer(a)))
        tcp = socket.socket(socket.AF_INET)
        self.addCleanup(tcp.close)
        self.assertEqual({"addr": "192.0.2.9"}, self.m.peer_of(_Peer(tcp)))

    @unittest.skipUnless(sys.platform == "linux", "a unix caller is its pid by SO_PEERCRED, which only Linux has; a Mac's own proxy takes guests over TCP alone")
    def test_each_host_goes_to_the_socket_it_may(self):
        calls, asked = [], []

        async def unix(path, **kw):
            calls.append(path)
            return None, Sink()

        async def may(peer):
            asked.append(peer)
            return self.allowed
        self.m.may_write = may
        for host, allowed, want in (("api.github.com", True, self.m.INJECT_SOCKET), ("api.github.com", False, self.m.INJECT_READ_SOCKET),
                                    ("bugs.webkit.org", False, self.m.INJECT_READ_SOCKET), ("bugs.webkit.org", True, self.m.INJECT_SOCKET),
                                    ("claude.ai", False, self.m.INJECT_SOCKET)):
            with self.subTest(host=host, allowed=allowed):
                self.allowed, calls[:], asked[:] = allowed, [], []
                a, b = socket.socketpair(socket.AF_UNIX)
                self.addCleanup(a.close)
                self.addCleanup(b.close)
                with unittest.mock.patch.object(self.m.asyncio, "open_unix_connection", unix):
                    asyncio.run(self.m.Proxy(self.m.Policy(self.tmp)).open_upstream(host, 443, False, _Peer(a)))
                self.assertEqual([want], calls)
                self.assertEqual(host in self.m.WRITE_HOSTS, bool(asked), "only a host a write can reach asks")
                if asked:
                    self.assertEqual([{"pid": os.getpid()}], asked, "the push service is asked about the process on the socket")


PAT, READ, BZ, REAL = "ghp-not-a-real-token", "ghp-read-only", "not-a-real-bugzilla-key", "sk-ant-oat01-REAL"
GH, BUGS, ANT = "api.github.com", "bugs.webkit.org", "api.anthropic.com"
CLAUDE_HOSTS = (ANT, "claude.ai", "platform.claude.com")
NO_CONTENT = b"HTTP/1.1 204 No Content\r\n\r\n"
LOGIN_PAIR = "login=me%40example.test&password=wk-injects-this"


def http(line, host=GH, *headers, body=None):
    """A request: `line`, a Host header unless host is None, `headers`, and a Content-Length when there is a body."""
    lines = [line] + (["Host: " + host] if host else []) + list(headers)
    if body is not None:
        lines.append("Content-Length: %d" % len(body))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")


def inject(tmp, request, reply=NO_CONTENT, pat=PAT, read=None, bz=None, login=True, forward=None, hosts=None,
           connect=None, timeouts=None, writes=True):
    """Injector.handle on `request` against a fake upstream: (what the client read, every byte the upstream got, the hosts
    opened, the log). login: True holds one, False names an absent file, None holds none. forward: "up" or "down" makes
    this a podman machine's injector, relaying to a holder that answers or not. writes: the socket the proxy picks for a
    person's shell; False is the reading one."""
    m = _load(INJECT, "wkinject")
    d = Path(tempfile.mkdtemp(dir=str(tmp)))
    for name, value in (("pat", pat), ("read", read), ("bz", bz)):
        if value is not None:
            (d / name).write_text(value + "\n")
    if login:
        (d / "login.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": REAL, "refreshToken": "sk-ant-ort01-REAL",
                                                                    "expiresAt": 4102444800000}}))
    if forward == "up":
        (d / "claude-inject.sock").write_text("")
    claude = m.Forward(str(d / "claude-inject.sock")) if forward else None if login is None else m.Holder(str(d / "login.json"))
    inj = m.Injector(str(d / "pat"), str(d / "read"), str(d / "bz"), claude, None, *([hosts] if hosts else []), writes=writes)
    upstream, client, opened = Sink(), Sink(), []

    async def tcp(host, port, **kw):
        opened.append((host, port))
        return await connect() if connect else (_reader(reply), upstream)

    async def unix(path, **kw):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        opened.append(("unix", 0))
        return _reader(b"HTTP/1.1 200 OK\r\n\r\nfrom the holder"), upstream

    async def drive():
        await inj.handle(_reader(request), client)
    if timeouts:
        m.UPSTREAM_TIMEOUT, m.READ_TIMEOUT = timeouts
    logged = io.StringIO()
    with unittest.mock.patch.object(asyncio, "open_connection", tcp), \
            unittest.mock.patch.object(asyncio, "open_unix_connection", unix), contextlib.redirect_stderr(logged):
        asyncio.run(drive())
    return bytes(client.data), bytes(upstream.data), opened, logged.getvalue()


def row(label, request, setup=None, to=GH, said=(), sent=(), unsent=(), once=(), first=None, logged=(), unlogged=(),
        whole=False):
    """to: the host the request reached, "unix" for the holder's socket, None for nothing sent anywhere. said: in the
    client's reply; sent/unsent/once: in the upstream's bytes (once: exactly one); first: its request line; whole: the
    upstream got the request byte for byte."""
    return label, request, setup or {}, dict(to=to, said=said, sent=sent, unsent=unsent, once=once, first=first,
                                              logged=logged, unlogged=unlogged, whole=whole)


NO_WRITE = dict(to=None, said=(b"412 Precondition Failed", b"no credential to write with", b"not from a shell of yours",
                               b"'wk key set github-pat'"), logged=("write refused: no credential to write with",))
READING = {"writes": False}

# Every request `git-webkit` makes of api.github.com, by the file that builds the URL.
GIT_WEBKIT_READS = (
    # webkitbugspy/github.py: Tracker.credentials, .user, .me
    ("GET", "/user"), ("GET", "/users/justinmichaud"),
    # webkitscmpy/remote/git_hub.py: GitHub.request with no path, .branches, .tags, .commits, .find, PRGenerator.statuses
    ("GET", "/repos/WebKit/WebKit"), ("GET", "/repos/WebKit/WebKit/branches"), ("GET", "/repos/WebKit/WebKit/tags"),
    ("GET", "/repos/WebKit/WebKit/commits?sha=abc123&per_page=20"), ("GET", "/repos/WebKit/WebKit/commits/abc123"),
    ("GET", "/repos/WebKit/WebKit/commits/abc123/statuses"), ("GET", "/repos/WebKit/WebKit/compare/main...abc123"),
    # webkitscmpy/remote/git_hub.py: PRGenerator.get, .reviewers, .diff
    ("GET", "/repos/WebKit/WebKit/pulls/1234"), ("GET", "/repos/WebKit/WebKit/pulls/1234/comments"),
    ("GET", "/repos/WebKit/WebKit/pulls/1234/reviews"), ("GET", "/repos/WebKit/WebKit/pulls/1234/requested_reviewers"),
    # webkitbugspy/github.py: Tracker.labels, .populate, Issue comments and timeline
    ("GET", "/repos/WebKit/WebKit/labels"), ("GET", "/repos/WebKit/WebKit/issues/1234"),
    ("GET", "/repos/WebKit/WebKit/issues/1234/comments"), ("GET", "/repos/WebKit/WebKit/issues/1234/timeline"),
    # webkitscmpy/remote/git_hub.py: PRGenerator.find -- one GraphQL search document, which carries no mutation
    ("POST", "/graphql"),
)
GIT_WEBKIT_WRITES = (
    # webkitscmpy/remote/git_hub.py: PRGenerator.create, .update, ._make_comment, .review
    ("POST", "/repos/WebKit/WebKit/pulls"), ("POST", "/repos/WebKit/WebKit/pulls/1234"),
    ("POST", "/repos/WebKit/WebKit/pulls/1234/comments"), ("POST", "/repos/WebKit/WebKit/pulls/1234/reviews"),
    # webkitbugspy/github.py: Tracker.create, .add_comment, .add_assignees, .set
    ("POST", "/repos/WebKit/WebKit/issues"), ("POST", "/repos/WebKit/WebKit/issues/1234/comments"),
    ("POST", "/repos/WebKit/WebKit/issues/1234/assignees"), ("PATCH", "/repos/WebKit/WebKit/issues/1234"),
    ("PUT", "/repos/WebKit/WebKit/issues/1234/labels"),
    # webkitscmpy/program/pull_request.py, with webkitscmpy.update-fork set
    ("POST", "/repos/justinmichaud/WebKit/merge-upstream"),
)
# `gh`, curl by hand, the probe `wk doctor <ws>` measures the injector with, and reads that are sensitive all the same.
OTHER_READS = ("/user", "/rate_limit", "/repos/a/b/actions/runs", "/orgs/x", "/", "/repos/WebKit/WebKit/actions/secrets",
               "/repos/an-employer/private-thing/contents/secrets.txt", "/user/emails")
# Real GitHub endpoints, each destructive, exfiltrating or both: the injector refuses nothing on policy.
HOSTILE_WRITES = (
    ("DELETE", "/repos/WebKit/WebKit"), ("PATCH", "/repos/WebKit/WebKit"), ("POST", "/repos/WebKit/WebKit/keys"),
    ("PUT", "/repos/WebKit/WebKit/collaborators/attacker"), ("POST", "/repos/WebKit/WebKit/actions/workflows/ci.yml/dispatches"),
    ("POST", "/repos/WebKit/WebKit/git/refs"), ("DELETE", "/repos/WebKit/WebKit/git/refs/heads/main"),
    ("PUT", "/repos/WebKit/WebKit/pulls/1234/merge"), ("POST", "/user/repos"), ("PATCH", "/user"), ("POST", "/gists"),
    ("POST", "/applications/id/token"), ("DELETE", "/user/keys/1"),
)
# Every request webkitbugspy makes of bugs.webkit.org (bugzilla.py); `_login_arguments` appends LOGIN_PAIR to each. A
# Bugzilla key has no read-only form, so the key goes on all of them or on none.
GIT_WEBKIT_BUGZILLA = (
    # Tracker.credentials' validater, .user, .me; .populate, comments, see_also, attachments
    ("GET", "/rest/user/me%40example.test"), ("GET", "/rest/user?names=me%40example.test"), ("GET", "/rest/bug/250000"),
    ("GET", "/rest/bug/250000/comment"), ("GET", "/rest/bug/250000?include_fields=see_also"),
    # Tracker.create, Issue.add_comment, .assign / .set, .open
    ("POST", "/rest/bug"), ("POST", "/rest/bug/250000/comment"), ("PUT", "/rest/bug/250000"),
)
GRAPHQL_SEARCH = (b'{"query": "query { search(query: \\"repo:WebKit/WebKit is:pr\\", type: ISSUE, last: 100) '
                  b'{ edges { node { number } } } }"}')
GRAPHQL_BOTH = (b'{"query": "query Find { viewer { login } } mutation Land { mergePullRequest(input: {}) '
                b'{ clientMutationId } }", "operationName": "Land"}')
PLACEHOLDER = http("GET /v1/models HTTP/1.1", ANT, "Authorization: Bearer wk-injects-this", "anthropic-version: 2023-06-01")
UPGRADE = ("Upgrade: websocket", "Connection: Upgrade")


def _bz(method, target):
    return "%s %s%s%s HTTP/1.1" % (method, target, "&" if "?" in target else "?", LOGIN_PAIR)


INJECTOR_ROWS = (
    # GitHub: the held token or the read token in, everything the workspace sent as a credential out.
    [row("a read spends the held token when there is no read token, and the rest survives",
         http("GET /user HTTP/1.1", GH, "Authorization: Basic d2s6d2staW5qZWN0cy10aGlz", "Connection: keep-alive",
              "Proxy-Authorization: Basic zzz", "User-Agent: python-requests/2.31.0"),
         first=b"GET /user HTTP/1.1", sent=(b"Authorization: Bearer " + PAT.encode(), b"Host: api.github.com\r\n",
                                            b"User-Agent: python-requests/2.31.0", b"Connection: close", b"\r\n\r\n"),
         unsent=(b"Basic", b"keep-alive", b"Proxy-Authorization"), once=(b"Authorization:", b"Connection:", b"Host:"),
         logged=("api.github.com read inject GET /user",)),
     row("with no token a read goes unauthenticated, the placeholder stripped",
         http("GET /user HTTP/1.1", GH, "Authorization: Basic d2s6d2staW5qZWN0cy10aGlz"), {"pat": None},
         unsent=(b"Authorization", b"Basic"), logged=("read unauthenticated GET /user",)),
     row("a read spends the read token over the write token", http("GET /user HTTP/1.1"), {"read": READ},
         sent=(b"Authorization: Bearer " + READ.encode(),), unsent=(PAT.encode(),)),
     row("a read on the reading socket carries the read token", http("GET /repos/WebKit/WebKit/pulls/1234 HTTP/1.1"),
         {**READING, "read": READ}, sent=(b"Authorization: Bearer " + READ.encode(),),
         logged=("read inject GET /repos/WebKit/WebKit/pulls/1234",)),
     row("a write spends the write token, never the read token", http("POST /user/repos HTTP/1.1", body=b""), {"read": READ},
         sent=(b"Authorization: Bearer " + PAT.encode(),), unsent=(READ.encode(),)),
     row("a write on the reading socket is refused by name", http("POST /user/repos HTTP/1.1", body=b""),
         {**READING, "read": READ}, **NO_WRITE),
     row("a write with no write token stored is refused by name", http("POST /user/repos HTTP/1.1", body=b""),
         {"pat": None, "read": READ}, **NO_WRITE),
     row("a GitHub write never spends the Bugzilla key", http("POST /repos/x/y/pulls HTTP/1.1", body=b""),
         {"pat": None, "bz": BZ}, **NO_WRITE),
     row("github is never upgraded", http("GET /x HTTP/1.1", GH, *UPGRADE), sent=(b"Connection: close",),
         unsent=(b"Connection: Upgrade",))]
    + [row("git-webkit's read %s %s" % r, http("%s %s HTTP/1.1" % r, body=b""), {"read": READ},
           first=("%s %s HTTP/1.1" % r).encode(), sent=(b"Bearer " + READ.encode(),), logged=("read inject",))
       for r in GIT_WEBKIT_READS]
    + [row("git-webkit's write %s %s" % r, http("%s %s HTTP/1.1" % r, body=b""),
           first=("%s %s HTTP/1.1" % r).encode(), sent=(b"Bearer " + PAT.encode(),), logged=("write inject",))
       for r in GIT_WEBKIT_WRITES]
    + [row("a read nothing in git-webkit makes: %s %s" % (m, t), http("%s %s HTTP/1.1" % (m, t)), {"read": READ},
           first=("%s %s HTTP/1.1" % (m, t)).encode(), sent=(b"Bearer " + READ.encode(),))
       for m in ("GET", "HEAD") for t in OTHER_READS]
    + [row("a hostile write carries the write token: %s %s" % r, http("%s %s HTTP/1.1" % r, body=b""),
           first=("%s %s HTTP/1.1" % r).encode(), sent=(b"Authorization: Bearer " + PAT.encode(),))
       for r in HOSTILE_WRITES]
    + [row("a hostile write on the reading socket: %s %s" % r, http("%s %s HTTP/1.1" % r, body=b""),
           {**READING, "read": READ}, **NO_WRITE) for r in HOSTILE_WRITES]
    + [row("a path a server would normalise is forwarded unchanged: " + t, http("POST %s HTTP/1.1" % t, body=b""),
           first=("POST %s HTTP/1.1" % t).encode())
       for t in ("/repos/a/b/pulls/../../../../user", "/repos/a/b/%2e%2e/%2e%2e/keys", "//repos/a/b/pulls")]
    + [row("a request line this cannot read is a write: " + line, http(line), {"read": READ}, sent=(line.encode(),
           b"Authorization: Bearer " + PAT.encode()), logged=("write inject",))
       for line in ("GET", "GET /user", "GET /user HTTP/1.1 extra", "GET https://api.github.com/user HTTP/1.1")]
    # GraphQL is the whole API behind one path, so the document decides read from write there.
    + [row("graphql %s %s %r" % (m, p, b), http("%s %s HTTP/1.1" % (m, p), body=b), {"read": READ},
           sent=(b"Authorization: Bearer " + (READ if half == "read" else PAT).encode(),),
           logged=("%s inject %s %s" % (half, m, p),))
       for m, p, b, half in (("POST", "/graphql", GRAPHQL_SEARCH, "read"),
                             ("POST", "/graphql", b'{"query": "query { viewer { login } }"}', "read"),
                             ("POST", "/graphql", b'{"query": "mutation { deleteRef(input: {}) }"}', "write"),
                             ("POST", "/graphql", GRAPHQL_BOTH, "write"),
                             ("POST", "/graphql", b'{"query": "Mutation { x }"}', "write"),
                             ("POST", "/graphql", b'{"query": "mUtAtIoN { x }"}', "write"),
                             ("POST", "/graphql?anything", b'{"query": "query { x }"}', "read"),
                             ("POST", "/graphql?anything", b'{"query": "mutation { x }"}', "write"),
                             ("PUT", "/graphql", b'{"query": "query { x }"}', "write"))]
    # Bugzilla takes its login in the query string: the workspace's never goes on, the held api_key does.
    + [row("bugzilla on the writing socket: %s %s" % r, http(_bz(*r), BUGS, body=b""), {"pat": None, "bz": BZ}, to=BUGS,
           sent=(("%s %s" % r).encode(), b"api_key=%s HTTP/1.1" % BZ.encode()), unsent=(b"login=", b"password="),
           logged=(" inject ",)) for r in GIT_WEBKIT_BUGZILLA]
    + [row("bugzilla read on the reading socket: %s %s" % r, http(_bz(*r), BUGS, body=b""), {**READING, "bz": BZ}, to=BUGS,
           sent=(("%s %s" % r).encode(),), unsent=(b"api_key", b"login=", b"password="), logged=(" unauthenticated ",))
       for r in GIT_WEBKIT_BUGZILLA if r[0] == "GET"]
    + [row("bugzilla write on the reading socket: %s %s" % r, http(_bz(*r), BUGS, body=b""), {**READING, "bz": BZ, "read": READ},
           **NO_WRITE) for r in GIT_WEBKIT_BUGZILLA if r[0] != "GET"]
    + [row("bugzilla never gets a GitHub token", http(_bz("GET", "/rest/bug/1"), BUGS), {"read": READ}, to=BUGS,
           unsent=(b"ghp-", b"Authorization", b"api_key"), logged=("/rest/bug/1",)),
       row("the key is url-encoded", http("GET /rest/bug/1 HTTP/1.1", BUGS), {"bz": "a b&c"}, to=BUGS,
           first=b"GET /rest/bug/1?api_key=a%20b%26c HTTP/1.1", unsent=(b"Authorization",)),
       row("a key in a header is dropped", http("GET /rest/bug/1 HTTP/1.1", BUGS, "X-BUGZILLA-API-KEY: the-workspaces-own"),
           to=BUGS, unsent=(b"the-workspaces-own",)),
       row("the login pair is dropped with no key to add",
           http("GET /rest/user/me%40example.test?login=me%40example.test&password=x HTTP/1.1", BUGS), to=BUGS,
           first=b"GET /rest/user/me%40example.test HTTP/1.1"),
       row("the login pair is dropped and the other parameters kept",
           http(_bz("GET", "/rest/bug/250000?include_fields=id%2Csummary"), BUGS), {"bz": BZ}, to=BUGS,
           first=b"GET /rest/bug/250000?include_fields=id%2Csummary&api_key=" + BZ.encode() + b" HTTP/1.1")]
    + [row("every spelling bugzilla takes a login in is dropped: " + name,
           http("GET /rest/bug?%s=the-workspaces-own&limit=1 HTTP/1.1" % name, BUGS), {"bz": BZ}, to=BUGS,
           sent=(b"/rest/bug?limit=1&api_key=%s HTTP/1.1" % BZ.encode(),), unsent=(b"the-workspaces-own",))
       for name in ("api_key", "Bugzilla_api_key", "token", "Bugzilla_login", "Bugzilla_password", "LOGIN", "pass%77ord")]
    + [row("a bugzilla request line this cannot read is refused: " + line, http(line, BUGS), {"bz": BZ}, to=None,
           said=(b"400 Bad Request",)) for line in ("GET", "GET /rest/bug?password=x", "GET /rest/bug HTTP/1.1 extra")]
    + [row("the far end's status is logged without its body: " + status, http(_bz("PUT", "/rest/bug/324270"), BUGS, body=b""),
           {"pat": None, "bz": BZ, "reply": reply}, to=BUGS, said=(reply,), logged=(status, "bugs.webkit.org"),
           unlogged=("secret",))
       for reply, status in ((b'HTTP/1.1 403 Forbidden\r\nContent-Length: 21\r\n\r\n{"message":"secret"}\n', "403 Forbidden"),
                             (b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", "200 OK"))]
    # The hosts it answers for, by one spelling each, and no other.
    + [row("it answers for %s and sends the host it verified" % h, http("GET /x HTTP/1.1", h), to=h,
           sent=(("Host: %s\r\n" % h).encode(),), once=(b"Host:",)) for h in (GH, BUGS) + CLAUDE_HOSTS]
    + [row("%s is api.github.com" % h, http("GET /x HTTP/1.1", h)) for h in ("API.GITHUB.COM", "api.github.com.",
                                                                              "api.github.com:443")]
    + [row("a host it does not answer for: %s" % h, http("POST /repos/x/y/releases HTTP/1.1", h, body=b""), to=None,
           said=(b"421 Misdirected Request", b"api.github.com, bugs.webkit.org, api.anthropic.com, claude.ai, "
                 b"platform.claude.com"))
       for h in ("uploads.github.com", "evil.example", "api.github.com.evil.example", "github.com", None)]
    # One request per connection, framed only the way GitHub would read it too.
    + [row("a pipelined second request never reaches github",
           http("POST /repos/x/y/pulls HTTP/1.1", body=b"hi") + http("GET /user HTTP/1.1", GH, "Authorization: token ghp-the-workspaces-own"),
           unsent=(b"ghp-the-workspaces-own",), once=(b"HTTP/1.1", b"Content-Length: 2\r\n", b"Content-Length:"),
           sent=(b"\r\n\r\nhi",))]
    + [row("framing it cannot trust: %s" % status.decode(), request, to=None, said=(status,)) for request, status in (
        (b"GET /x HTTP/1.1\nAuthorization: token ghp-the-workspaces-own\r\n\r\n", b"400 Bad Request"),
        (http("POST /user HTTP/1.1", GH, "Transfer-Encoding: chunked") + b"2\r\nhi\r\n0\r\n\r\n", b"411 Length Required"),
        (http("POST /user HTTP/1.1", GH, "Content-Length: 2", "Content-Length: 40") + b"hi", b"400 Bad Request"))]
    # Claude's hosts: the claude.ai login for the placeholder bearer, the OAuth endpoints refused, the rest as sent.
    + [row("the placeholder is swapped for the access token", PLACEHOLDER, to=ANT,
           sent=(b"Authorization: Bearer %s\r\n" % REAL.encode(), b"anthropic-version: 2023-06-01"), once=(b"Authorization:",),
           logged=("api.anthropic.com login inject GET /v1/models",)),
       row("with the login file gone it answers itself", PLACEHOLDER, {"login": False}, to=None,
           said=(b"401 Unauthorized", b"put no claude.ai login on this request", b"'wk key set claude-login'")),
       row("an injector holding no login answers the placeholder itself", PLACEHOLDER, {"login": None}, to=None,
           said=(b"401 Unauthorized", b"./setup --stage inject", b"./setup --stage sdk")),
       row("a websocket upgrade carries frames both ways",
           http("GET /v1/sessions/ws HTTP/1.1", ANT, "Authorization: Bearer wk-injects-this", *UPGRADE,
                "Sec-WebSocket-Key: a2V5", "Sec-WebSocket-Version: 13") + b"CLIENT-FRAME",
           {"reply": b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nSERVER-FRAME"},
           to=ANT, said=(b"HTTP/1.1 101 Switching Protocols\r\n", b"SERVER-FRAME"),
           sent=(b"Upgrade: websocket", b"Connection: Upgrade", b"Sec-WebSocket-Key: a2V5",
                 b"Authorization: Bearer " + REAL.encode(), b"\r\n\r\nCLIENT-FRAME"), unsent=(b"Connection: close",)),
       row("an upgrade the far end refuses relays nothing more",
           http("GET /v1/sessions/ws HTTP/1.1", ANT, *UPGRADE) + b"CLIENT-FRAME",
           {"reply": b"HTTP/1.1 400 Bad Request\r\n\r\nno"}, to=ANT, said=(b"HTTP/1.1 400 Bad Request",),
           unsent=(b"CLIENT-FRAME",))]
    + [row("an OAuth endpoint is refused: %s %s, login %s" % (h, line, held),
           http(line, h, "Authorization: Bearer wk-injects-this", body=b""), {"login": held}, to=None,
           said=(b"HTTP/1.1 403 Forbidden",), logged=("refused: an OAuth endpoint",))
       for h, line in (("platform.claude.com", "POST /v1/oauth/token HTTP/1.1"),
                       ("claude.ai", "GET /oauth/authorize?code=true HTTP/1.1"), (ANT, "POST /v1/oauth/token/ HTTP/1.1"))
       for held in (True, None)]
    + [row("its own credential or none passes as sent: %r" % auth,
           http("POST /v1/messages HTTP/1.1", ANT, *([auth] if auth else []), body=b"{}"), {"login": False}, to=ANT,
           sent=(auth.encode(), b"\r\n\r\n{}"), once=(b"Authorization:",) if auth.startswith("Authorization") else (),
           unsent=() if auth.startswith("Authorization") else (b"Authorization:",), logged=("login as sent POST /v1/messages",))
       for auth in ("Authorization: Bearer sk-ant-its-own", "x-api-key: sk-ant-api03-its-own", "")]
    # A podman machine's injector relays Claude's hosts whole to the Mac's, the login's one holder.
    + [row("a claude request is relayed whole and the holder's answer comes back",
           http("POST /v1/messages HTTP/1.1", "claude.ai", "Authorization: Bearer wk-injects-this", body=b"{}"),
           {"forward": "up"}, to="unix", whole=True, said=(b"from the holder",),
           logged=("claude.ai login forward POST /v1/messages",)),
       row("github stays with the podman machine's injector", http("GET /x HTTP/1.1"), {"forward": "down"}),
       row("no holder answering is a 401 naming its setup stage", http("GET /v1/models HTTP/1.1", ANT),
           {"forward": "down"}, to=None, said=(b"401 Unauthorized", b"'./setup --stage inject'", b"inject.log")),
       row("the socket it publishes answers for claude's hosts alone", http("GET /x HTTP/1.1"),
           {"forward": "down", "hosts": CLAUDE_HOSTS}, to=None,
           said=(b"421 Misdirected Request", b"api.anthropic.com, claude.ai, platform.claude.com, and"))]
)


class TestTheInjector(WkTest):
    def test_each_request_gets_its_credential_and_its_verdict(self):
        for label, request, setup, want in INJECTOR_ROWS:
            with self.subTest(label):
                client, upstream, opened, logged = inject(self.tmp, request, **setup)
                if want["to"] is None:
                    self.assertEqual(([], b""), (opened, upstream), client)
                else:
                    self.assertEqual([("unix", 0) if want["to"] == "unix" else (want["to"], 443)], opened, client)
                for b in want["said"]:
                    self.assertIn(b, client)
                for b in want["sent"]:
                    self.assertIn(b, upstream)
                for b in want["unsent"]:
                    self.assertNotIn(b, upstream)
                for b in want["once"]:
                    self.assertEqual(1, upstream.count(b), (b, upstream))
                if want["first"] is not None:
                    self.assertEqual(want["first"], upstream.split(b"\r\n")[0])
                if want["whole"]:
                    self.assertEqual(request, upstream)
                else:
                    self.assertNotIn(b"wk-injects-this", upstream)
                for s in want["logged"]:
                    self.assertIn(s, logged)
                for s in want["unlogged"]:
                    self.assertNotIn(s, logged)
                for s in tuple(v for k, v in setup.items() if k in ("pat", "read", "bz") and v) + (PAT, READ, BZ, REAL):
                    self.assertNotIn(s, logged)
                    self.assertNotIn(s.encode(), client)
                self.assertNotIn(b"wk-injects-this", client)

    def test_the_token_is_read_from_the_file_on_every_request(self):
        m = _load(INJECT, "wkinject")
        path = self.tmp / "pat"
        self.assertEqual("", m.read_token(str(path)))
        path.write_text(PAT + "\n")
        self.assertEqual(PAT, m.read_token(str(path)))

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_it_makes_its_own_ca_publishes_only_the_public_half_and_remakes_a_leaf_naming_fewer_hosts(self):
        m, certs, out = _load(INJECT, "wkinject"), self.tmp / "certs", self.tmp / "out" / "ca.pem"
        chain = m.ensure_certs(str(certs), str(out))
        self.assertIn("BEGIN CERTIFICATE", out.read_text())
        self.assertNotIn("PRIVATE KEY", out.read_text())
        self.assertEqual([0o644, 0o600, 0o600], [(p.stat().st_mode & 0o777) for p in (out, certs / "ca.key", certs / "leaf.key")])
        self.assertEqual(2, Path(chain).read_text().count("BEGIN CERTIFICATE"))
        text = subprocess.run(["openssl", "x509", "-noout", "-text", "-in", str(certs / "leaf.crt")],
                              stdout=subprocess.PIPE, text=True, check=True).stdout
        self.assertIn("CA:FALSE", text)
        self.assertEqual(set(m.HOSTS), m.leaf_names(str(certs / "leaf.crt")))
        ca = (certs / "ca.crt").read_text()
        cnf, csr = m._conf(m.GITHUB, (m.GITHUB,)), str(certs / "leaf.csr")
        try:
            m._openssl("req", "-new", "-key", str(certs / "leaf.key"), "-out", csr, "-config", cnf)
            m._openssl("x509", "-req", "-in", csr, "-CA", str(certs / "ca.crt"), "-CAkey", str(certs / "ca.key"),
                       "-set_serial", "2", "-days", "3650", "-out", str(certs / "leaf.crt"), "-extfile", cnf,
                       "-extensions", "leaf_ext")
        finally:
            os.unlink(cnf)
        self.assertEqual({"api.github.com"}, m.leaf_names(str(certs / "leaf.crt")))
        m.ensure_certs(str(certs), str(out))
        self.assertEqual(set(m.HOSTS), m.leaf_names(str(certs / "leaf.crt")))
        self.assertEqual(ca, (certs / "ca.crt").read_text())


class TestTheInjectorBoundsTheUpstream(WkTest):
    def drive(self, connect, timeouts=(0.3, 0.3)):
        client, _up, _opened, logged = inject(self.tmp, http("GET /user HTTP/1.1", body=b""), read="t", connect=connect,
                                              timeouts=timeouts)
        return client, logged

    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addCleanup(server.close)
        return await REAL_OPEN_CONNECTION("127.0.0.1", server.sockets[0].getsockname()[1])

    def test_a_connect_that_never_completes_is_a_504(self):
        async def never():
            await asyncio.sleep(60)
        client, logged = self.drive(never)
        self.assertIn(b"504 Gateway Timeout", client)
        self.assertIn(b"api.github.com did not answer", client)
        self.assertIn("TimeoutError", logged)
        self.assertNotIn(b"may have been applied", client)

    def test_a_server_that_accepts_and_stalls_is_a_504(self):
        async def hold(reader, writer):
            await asyncio.sleep(60)
        client, _ = self.drive(lambda: self.serve(hold))
        self.assertIn(b"504 Gateway Timeout", client)
        self.assertIn(b"the request was sent; it may have been applied", client)

    def test_the_status_line_outlasts_the_connect_bound(self):
        async def late(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(0.5)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
        client, _ = self.drive(lambda: self.serve(late), (0.2, 5))
        self.assertIn(b"200 OK", client)

    def test_a_failed_connect_is_a_502_and_only_a_tls_or_dns_failure_names_the_injector(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
        probe.close()
        for error, header, logged in (
                (ssl.SSLCertVerificationError("certificate verify failed"), b"SSLCertVerificationError", ""),
                (socket.gaierror(-2, "Name or service not known"), b"gaierror", ""),
                (OSError(errno.ENETUNREACH, "Network is unreachable"), None, ""),
                (None, None, "ConnectionRefusedError")):
            async def fail(error=error):
                if error is None:
                    return await REAL_OPEN_CONNECTION("127.0.0.1", closed)
                raise error
            with self.subTest(error=type(error).__name__):
                client, log = self.drive(fail)
                self.assertIn(b"502 Bad Gateway", client)
                self.assertIn(logged, log)
                if header:
                    self.assertIn(b"X-Wk-Injector: " + header, client)
                    self.assertIn(b"failed to verify or resolve api.github.com", client)
                else:
                    self.assertNotIn(b"X-Wk-Injector", client)
                    self.assertIn(b"could not reach api.github.com", client)


class TestAHostSocketIsPublishedIntoThePodmanMachine(WkTest):
    def test_a_mac_with_no_podman_says_so_once_and_publishes_nothing(self):
        from wk import publish
        said = []
        with unittest.mock.patch.object(publish.shutil, "which", lambda name: None), \
                unittest.mock.patch.object(publish, "publish_once", side_effect=AssertionError("asked the machine")):
            asyncio.run(asyncio.wait_for(publish.publish("wk", "/mac/s.sock", "claude-inject.sock", said.append, "inject"), 2))
        self.assertEqual(1, len(said))
        self.assertIn("./setup --stage inject", said[0])

    def test_one_forward_names_the_machines_runtime_directory(self):
        from wk import publish
        runs, spawned = [], []

        def fake_run(argv, **kw):
            runs.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, b"/run/user/501" if "XDG_RUNTIME_DIR" in argv[-1] else b"")

        class Proc:
            async def wait(self):
                return 0

        async def fake_exec(*argv):
            spawned.append(argv)
            return Proc()

        rec = {"Name": "wk", "SSHConfig": {"Port": 52000, "IdentityPath": "/k", "RemoteUsername": "core"}}
        with unittest.mock.patch.object(publish.places, "podman_vm", lambda *a, **kw: rec), \
                unittest.mock.patch.object(publish.subprocess, "run", fake_run), \
                unittest.mock.patch.object(publish.asyncio, "create_subprocess_exec", fake_exec):
            rc = asyncio.run(publish.publish_once("wk", "/mac/claude-inject.sock", "claude-inject.sock", lambda m: None))
        self.assertEqual(0, rc)
        self.assertEqual("mkdir -p /run/user/501 && rm -f /run/user/501/claude-inject.sock", runs[1])
        argv = list(spawned[0])
        self.assertEqual(["-N", "-R", "/run/user/501/claude-inject.sock:/mac/claude-inject.sock", "core@127.0.0.1"], argv[-4:])
        self.assertIn("52000", argv)


FAKE_PORT = "4242"


def _bridge_script(d, ca, bridge="print(%s, flush=True)\n" % FAKE_PORT, stores=("demo",)):
    """ensure-bridge.sh with its fixed paths moved under `d`; `ca` is the injector CA's text, or None; `bridge` is bridge.py's source;
    `stores` the workspace stores mounted at /var/lib/wk/ws."""
    secrets = d / "secrets"
    secrets.mkdir()
    tools = d / "wk-tools"
    (tools / "shell").mkdir(parents=True)
    (tools / "container" / "proxy").mkdir(parents=True)
    (tools / "shell" / "path.sh").write_text(":\n")
    (tools / "container" / "proxy" / "bridge.py").write_text(bridge)
    runwk = d / "run-wk"
    runwk.mkdir()
    if ca:
        (runwk / "wk-github-ca.pem").write_text(ca)
    for name in stores:
        (d / "ws" / name).mkdir(parents=True)
    sysca = d / "ca-certificates.crt"
    sysca.write_text("THE-SYSTEM-STORE\n")
    script = d / "ensure-bridge.sh"
    script.write_text(
        (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
        .replace("/opt/wk-tools", str(tools))
        .replace("/run/wk/wk-github-ca.pem", str(runwk / "wk-github-ca.pem"))
        .replace("/etc/ssl/certs/ca-certificates.crt", str(sysca))
        .replace("/secrets/", str(secrets) + "/")
        .replace("/var/lib/wk/ws", str(d / "ws")))
    return script, secrets


def run_bridge(ca, *print_vars, bugzilla_user=None, github_user=None,
               env_extra=None):
    """(what the wrapped command printed, the bundle's path, its bytes)."""
    d = Path(tempfile.mkdtemp(prefix="wk-test-gh-env-"))
    try:
        script, secrets = _bridge_script(d, "THE-INJECTORS-CA\n" if ca else None)
        if bugzilla_user is not None:
            (secrets / "bugzilla-user").write_text(bugzilla_user + "\n")
        if github_user is not None:
            (secrets / "github-user").write_text(github_user + "\n")
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
    def test_every_way_into_a_container_goes_through_the_wrapper(self):
        c = wk_places.Container("c", REPO, {"WK_CONTAINER_USER": "dev", "WK_IN_VM": "1"}, Fake("here"))
        bridge = "/opt/wk-tools/container/proxy/ensure-bridge.sh"
        for tty in (False, True):
            with self.subTest(exec_tty=tty):
                self.assertEqual([bridge, "true"], c.exec_argv("a", ["true"], tty)[0][-2:])
        person = "/opt/wk-tools/container/bin/wk-person"
        self.assertIn("exec %s %s " % (person, bridge), c.sshd_cmd("dev"), "the editor's terminal is a person's")
        self.assertIn(bridge, c.enter_argv("a")[0])
        self.assertEqual([person, "/bin/bash", "--login"], c.enter_argv("a")[0][-3:], "a shell is a person's")

    def test_a_full_temp_directory_still_runs_the_command(self):
        d = Path(tempfile.mkdtemp(prefix="wk-test-ro-tmp-"))
        try:
            script, _ = _bridge_script(d, "-----BEGIN CERTIFICATE-----\nnot a real one\n")
            ro = d / "ro"
            ro.mkdir()
            os.chmod(ro, 0o500)
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

BRIDGE_PY = REPO / "container" / "proxy" / "bridge.py"
ON_LINUX = "a bridge finds its client by the socket in /proc, which only Linux has"
PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


class TestEachExecHasABridgeOfItsOwn(unittest.TestCase):
    """The proxy judges a write by the pid on its socket, the bridge's: so the bridge is the exec's child, and the command is handed its port."""

    def wrap(self, bridge, command, stores=("demo",), *args, **env):
        d = Path(tempfile.mkdtemp(prefix="wk-test-bridge-exec-"))
        self.addCleanup(shutil.rmtree, d, True)
        script, _ = _bridge_script(d, None, bridge, stores)
        (d / "rw").mkdir()
        mine = {k: v for k, v in os.environ.items() if k != "WK_WORKSPACE"}
        return subprocess.run(["bash", str(script), "sh", "-c", command, *args], capture_output=True, text=True, timeout=60,
                              env=dict(mine, TMPDIR=str(d / "rw"), **env)), d

    def test_the_command_is_handed_its_bridges_port(self):
        printer = "; ".join('printf "%s=[%%s]\\n" "$%s"' % (v, v) for v in PROXY_VARS)
        cp, _ = self.wrap("print(%s, flush=True)\n" % FAKE_PORT, printer, http_proxy="http://127.0.0.1:3128")
        self.assertEqual(0, cp.returncode, cp.stderr)
        for v in PROXY_VARS:
            self.assertIn("%s=[http://127.0.0.1:%s]" % (v, FAKE_PORT), cp.stdout)

    def test_the_bridge_is_the_child_of_the_command_it_serves(self):
        bridge = ("import os, sys\n"
                  "open(os.path.join(os.path.dirname(__file__), 'parent'), 'w').write('%d %s %s' % (os.getppid(), sys.argv[1], sys.argv[2]))\n"
                  "print(4242, flush=True)\n")
        cp, d = self.wrap(bridge, "echo $$")
        self.assertEqual(0, cp.returncode, cp.stderr)
        ppid, named, ws = (d / "wk-tools" / "container" / "proxy" / "parent").read_text().split()
        self.assertEqual((cp.stdout.strip(), cp.stdout.strip()), (ppid, named), "the command execs into the bridge's parent")
        self.assertEqual("demo", ws, "the name is the one store mounted, which holds under sudo's reset environment, where the exec has none")

    def test_anything_but_one_workspace_store_runs_the_command_without_network_and_says_so(self):
        for stores in ((), ("demo", "other")):
            with self.subTest(stores=stores):
                cp, _ = self.wrap("print(4242, flush=True)\n", 'printf "[%s] %s" "${http_proxy:-}" "$#"', stores, "sh", "a", "b")
                self.assertEqual(0, cp.returncode, cp.stderr)
                self.assertEqual("[] 2", cp.stdout, "the command keeps its own arguments")
                self.assertIn("/ws, which 'wk new' mounts for every container, so this command has no network", cp.stderr)

    def test_a_bridge_that_does_not_start_leaves_the_command_without_network_and_says_so(self):
        cp, _ = self.wrap("import sys; sys.exit(1)\n", 'printf "[%s]" "$http_proxy"', http_proxy="http://127.0.0.1:3128")
        self.assertEqual(0, cp.returncode, cp.stderr)
        self.assertEqual("[http://127.0.0.1:3128]", cp.stdout)
        self.assertIn("did not start, so this command has no network", cp.stderr)


class TestTheBridge(unittest.TestCase):
    """container/proxy/bridge.py itself, against a scratch proxy socket."""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="wk-br-", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        self.sock = os.path.join(d, "proxy.sock")
        server = socket.socket(socket.AF_UNIX)
        server.bind(self.sock)
        server.listen(4)
        self.addCleanup(server.close)

        def echo():
            with contextlib.suppress(OSError):
                while True:
                    c, _ = server.accept()
                    with c:
                        c.sendall(b"proxy:" + c.recv(64))
        threading.Thread(target=echo, daemon=True).start()

    def start(self, parent):
        p = subprocess.Popen([sys.executable, str(BRIDGE_PY), str(parent), "demo"], stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ, WK_PROXY_SOCKET=self.sock))
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.kill)
        return p

    @unittest.skipUnless(sys.platform == "linux", ON_LINUX)
    def test_each_binds_a_port_of_its_own_prints_it_and_relays_to_the_proxy_socket(self):
        ports = [int(self.start(os.getpid()).stdout.readline()) for _ in range(2)]
        self.assertNotEqual(ports[0], ports[1])
        for port in ports:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
                c.sendall(b"hello")
                self.assertEqual(b"proxy:hello", c.recv(64))

    def test_a_bridge_whose_parent_is_already_gone_exits(self):
        p = self.start(1)
        self.assertEqual("", p.stdout.read())
        self.assertEqual(0, p.wait(10))

    @unittest.skipUnless(sys.platform == "linux", ON_LINUX)
    def test_it_serves_a_client_that_is_its_commands_child(self):
        port = int(self.start(os.getpid()).stdout.readline())
        client = ("import socket; c = socket.create_connection(('127.0.0.1', %d), timeout=10); c.sendall(b'hello'); "
                  "print(c.recv(64).decode())" % port)
        cp = subprocess.run([sys.executable, "-c", client], capture_output=True, text=True, timeout=30)
        self.assertEqual("proxy:hello", cp.stdout.strip(), cp.stderr)

    @unittest.skipUnless(sys.platform == "linux", ON_LINUX)
    def test_a_client_with_an_agent_between_is_refused_by_name(self):
        """The real socket lookup in this process's /proc; the ancestry a fake one, this process under claude under the command."""
        m = _load(BRIDGE_PY, "wkbridge")
        fake = Proc(Path(tempfile.mkdtemp(prefix="wk-br-proc-")))
        self.addCleanup(shutil.rmtree, fake.root, True)
        fake.chain([(os.getpid(), 600, "python3", ["python3"]), (600, 500, "claude", ["/home/u/.local/bin/claude"])] + PERSONS_COMMAND)
        m.ancestry = pushgate.Proc(str(fake.root))

        async def ask():
            server = await asyncio.start_server(functools.partial(m.handle, 500, "demo"), "127.0.0.1", 0)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            said = await reader.read()
            writer.close()
            server.close()
            return said.decode()
        said = asyncio.run(ask())
        self.assertTrue(said.startswith("HTTP/1.1 403 Forbidden"), said)
        self.assertIn("wk bridge: an agent session (claude, pid 600)", said)
        self.assertIn("wk enter demo", said)

    @unittest.skipUnless(sys.platform == "linux", "a bridge dies with its parent by PR_SET_PDEATHSIG, which only Linux has")
    def test_it_dies_with_the_command_its_exec_became(self):
        sh = subprocess.Popen(["bash", "-c", 'exec 3< <(exec %s %s $$ demo); read -r p <&3; echo "$! $p"; exec sleep 60'
                               % (shlex.quote(sys.executable), shlex.quote(str(BRIDGE_PY)))],
                              stdout=subprocess.PIPE, text=True, env=dict(os.environ, WK_PROXY_SOCKET=self.sock))
        self.addCleanup(sh.wait, 10)
        self.addCleanup(sh.kill)
        pid, port = sh.stdout.readline().split()
        socket.create_connection(("127.0.0.1", int(port)), timeout=10).close()
        sh.kill()
        sh.wait(10)
        for _ in range(100):
            state = Path("/proc/%s/status" % pid)
            if not state.exists() or "\nState:\tZ" in state.read_text():
                return
            time.sleep(0.1)
        self.fail("bridge %s outlived the command it served" % pid)


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

    def test_it_carries_the_bridge_of_the_exec_that_runs_sshd(self):
        line, _ = self.setenv_line(True, env_extra={
            "http_proxy": "http://127.0.0.1:3128",
            "NO_PROXY": "localhost,127.0.0.1,::1"})
        self.assertIn("http_proxy=http://127.0.0.1:%s" % FAKE_PORT, line.split(" "))
        self.assertNotIn("3128", line)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,::1", line.split(" "))

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
            cmd = (wk_places.Container("c", REPO, {}, Fake("here")).sshd_cmd("tester")
                   .replace("mkdir -p /run/sshd && ", "")
                   .replace("/opt/wk-tools/container/bin/wk-person", str(REPO / "container" / "bin" / "wk-person"))
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
        h = self.tmp / ("home" if egress else "bare-home")
        h.mkdir(exist_ok=True)
        if egress:
            (h / ".wk-egress").write_text(EGRESS)
        _wire(h)
        return h

    def test_every_shell_that_reads_an_rc_has_the_proxy_and_none_without_the_egress_file(self):
        proxy, local = "http://192.168.2.1:3128", "localhost,127.0.0.1,::1"
        for egress, want in ((True, dict(zip(VARS, (proxy,) * 4 + (local,) * 2))), (False, dict.fromkeys(VARS, ""))):
            home = self._home(egress)
            for what, (shell, args) in self.SHELLS.items():
                if shutil.which(shell):
                    with self.subTest(shell=what, egress=egress):
                        self.assertEqual(want, _shell_vars(shell, home, args))

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


PERSONS_COMMAND = [(500, 400, "bash", ["/bin/bash", "/opt/wk-tools/" + pushgate.PERSON, "/bin/bash", "--login"]),
                   (400, 1, "conmon", ["/usr/bin/conmon"])]
AGENTS_COMMAND = [(500, 400, "bash", ["bash", "-lc", "exec claude"]), (400, 1, "conmon", ["/usr/bin/conmon"])]


class TestTheBridgeFindsItsClient(unittest.TestCase):
    """bridge.py's lookup and verdict against a fake /proc: the client's end of the connection, the process holding it, its ancestry."""
    LOCAL, PEER, INODE = 4242, 5555, "31337"

    def setUp(self):
        self.m = _load(BRIDGE_PY, "wkbridge")
        self.proc = Proc(Path(tempfile.mkdtemp(prefix="wk-br-proc-")))
        self.addCleanup(shutil.rmtree, self.proc.root, True)
        (self.proc.root / "net").mkdir()
        header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        row = "   0: 0100007F:%04X 0100007F:%04X 01 00000000:00000000 00:00000000 00000000  1000        0 %s 1\n"
        (self.proc.root / "net" / "tcp").write_text(header + row % (self.LOCAL, self.PEER, "111") + row % (self.PEER, self.LOCAL, self.INODE))
        (self.proc.root / "net" / "tcp6").write_text(header)
        self.m.PROC, self.m.ancestry = str(self.proc.root), pushgate.Proc(str(self.proc.root))

    def holds(self, pid, inode):
        fd = self.proc.root / str(pid) / "fd"
        fd.mkdir(parents=True, exist_ok=True)
        (fd / "3").unlink(missing_ok=True)
        os.symlink("socket:[%s]" % inode, fd / "3")

    def refusal(self, rows):
        self.proc.chain(rows)
        self.holds(rows[0][0], self.INODE)
        return self.m.refusal(self.LOCAL, self.PEER, 500, "demo")

    def test_a_client_under_the_command_is_served(self):
        self.assertIsNone(self.refusal([(77, 500, "git", ["git", "fetch"])] + PERSONS_COMMAND))

    def test_an_agent_sessions_bridge_serves_a_tool_under_its_agent(self):
        self.assertIsNone(self.refusal([(77, 600, "gh", ["gh"]), (600, 500, "claude", ["/home/u/.local/bin/claude"])] + AGENTS_COMMAND))

    def test_a_persons_bridge_refuses_a_client_with_an_agent_between(self):
        why = self.refusal([(77, 600, "gh", ["gh"]), (600, 500, "claude", ["/home/u/.local/bin/claude"])] + PERSONS_COMMAND)
        self.assertIn("an agent session (claude, pid 600)", why)
        self.assertIn("wk enter demo", why)

    def test_a_client_not_under_the_command_is_refused(self):
        for command in (PERSONS_COMMAND, AGENTS_COMMAND):
            with self.subTest(command=command[0][3]):
                why = self.refusal([(77, 700, "sh", ["sh"]), (700, 1, "sh", ["sh"])] + command)
                self.assertIn("pid 77 is not under the command this bridge serves (pid 500)", why)

    def test_a_client_no_readable_process_holds_is_refused_naming_sudos_remedy(self):
        self.proc.chain([(77, 500, "git", ["git"])])
        self.holds(77, "999")
        why = self.m.refusal(self.LOCAL, self.PEER, 500, "demo")
        self.assertIn("no process this bridge can read holds the connection from port %d" % self.PEER, why)
        self.assertIn("sudo /opt/wk-tools/container/proxy/ensure-bridge.sh <command>", why)


class TestTheContainerEnvironment(unittest.TestCase):
    def test_the_container_is_created_naming_no_proxy_address(self):
        flags = wk_places.Container("container", str(REPO), {"XDG_RUNTIME_DIR": "/run/user/1"}, Fake("here")).sandbox_flags("armhf")
        env = dict(v.split("=", 1) for k, v in zip(flags[0::2], flags[1::2]) if k == "--env")
        self.assertEqual([], [v for v in PROXY_VARS if v in env], "only an exec's own bridge names one")
        self.assertEqual("localhost,127.0.0.1,::1", env["no_proxy"])


if __name__ == "__main__":
    unittest.main()
