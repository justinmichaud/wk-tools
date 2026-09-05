"""How a workspace reaches the outside: what the proxy allows, and how the
variables naming that proxy get into a macOS guest's shells.

Three parts of one boundary. `container/proxy/wk-proxy.py` decides what may be
reached and is shared by containers and guests alike, so an entry widens both.
`container/proxy/github-inject.py` is the one host whose TLS is terminated: it
forwards every request and refuses none, and what it decides is which of the
two tokens the request spends.
`~/.wk-egress` carries the variables naming that proxy into a guest. The host
writes it on every start and vm/shell-rc.sh sources it from all four rc files,
so every shell that reads an rc gets it -- not login shells alone: an editor's
terminal pane is not a login shell, and a pane with no proxy has no egress at
all and reports it as every host in the world being unreachable.

Run: python3 -m unittest tests.test_egress -v
"""
import asyncio
import contextlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.support import assert_guest_start_converges, REPO, WkTest

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"
INJECT = REPO / "container" / "proxy" / "github-inject.py"
RC = REPO / "shell" / "bashrc"
SHELL_RC = REPO / "vm" / "shell-rc.sh"

# The six a proxied machine has to export. curl and git read the lowercase
# pair, some tools only the uppercase, and no_proxy keeps a workspace's own
# loopback services off the proxy.
VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "no_proxy", "NO_PROXY")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _policy():
    return _load(PROXY, "wkproxy").Policy(tempfile.mkdtemp(prefix="wk-test-store-"))


class FakeWriter:
    """An asyncio StreamWriter as far as either program uses one: it is
    written to, drained and closed, and the test reads back every byte."""

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
    """A StreamReader already holding `data`. Built inside the running loop:
    StreamReader binds the current event loop at construction."""
    r = asyncio.StreamReader()
    if data:
        r.feed_data(data)
    if eof:
        r.feed_eof()
    return r


def drive_injector(tmp, client_bytes,
                   upstream_reply=b"HTTP/1.1 204 No Content\r\n\r\n",
                   token="ghp-not-a-real-token", read_token=None):
    """Injector.handle against a fake upstream: returns what the client was
    sent, every byte that reached api.github.com, the hosts it connected to and
    what the injector logged.

    `token=None` is `wk push off`: no write token file. `read_token=` writes
    the standing read token, which no position of the switch removes. Each
    call gets its own directory, so a test may drive the injector twice with
    the machine in two states."""
    m = _load(INJECT, "wkinject")
    d = Path(tempfile.mkdtemp(dir=str(tmp)))
    pat = d / "push-github-pat"
    read_pat = d / "read-github-pat"
    if token is not None:
        pat.write_text(token + "\n")
    if read_token is not None:
        read_pat.write_text(read_token + "\n")
    inj = m.Injector(str(pat), str(read_pat), None)
    uwriter = FakeWriter()
    cwriter = FakeWriter()
    opened = []

    async def fake_open_connection(host, port, **kw):
        opened.append((host, port))
        return _reader(upstream_reply), uwriter

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


class TestDevelopmentAllowlist(unittest.TestCase):
    """The hosts ordinary development needs, each measured as a refusal before
    it was added. Suffix matching is on a dot boundary, so one entry covers the
    CDN subdomain a registry actually serves from -- and covers nothing else."""

    def test_package_registries_are_reachable(self):
        p = _policy()
        for host in ("registry.npmjs.org", "formulae.brew.sh", "ghcr.io",
                     "crates.io", "static.crates.io", "static.rust-lang.org",
                     "sh.rustup.rs"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertTrue(ok, why)

    def test_xcode_is_reachable(self):
        p = _policy()
        for host in ("developer.apple.com", "download.developer.apple.com"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertTrue(ok, why)

    def test_the_software_update_scan_path_is_refused(self):
        """Measured on a Tahoe 26.4 guest on 2026-09-05: with these reachable,
        softwareupdated found macOS 26.6.2 and Setup Assistant put its "Update
        Mac Automatically" pane in front of the window. No guest can turn the
        check off (vm/desktop.sh), so the only place it can be stopped is here,
        and a guest is a clone of a pinned image that upgrading means nothing to."""
        p = _policy()
        for host in ("swscan.apple.com", "swcdn.apple.com", "swdist.apple.com",
                     "updates.cdn-apple.com", "updates-http.cdn-apple.com",
                     "mesu.apple.com", "gdmf.apple.com", "gdmf-ados.apple.com",
                     "xp.apple.com"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertFalse(ok, f"{host} is reachable: {why}")

    def test_certificate_validation_works_on_80(self):
        """Gatekeeper will not launch a downloaded binary without a
        notarization check, and OCSP and CRL are http by design."""
        p = _policy()
        for host in ("valid.apple.com", "ocsp.apple.com", "crl.apple.com",
                     "i.pki.goog"):
            with self.subTest(host=host):
                self.assertTrue(p.host_allowed(host, 80)[0])
                self.assertTrue(p.host_allowed(host, 443)[0])

    def test_apple_is_named_host_by_host_not_by_suffix(self):
        """The refusals a guest produces are mostly iCloud, Siri, Spotlight,
        ads and news. None of that is development, and an `apple.com` suffix
        would have swept it all in."""
        p = _policy()
        for host in ("gsa.apple.com", "gateway.icloud.com",
                     "weatherkit.apple.com", "iadsdk.apple.com",
                     "api-spotlight-ausw2b.smoot.apple.com"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertFalse(ok, why)

    def test_a_registry_lookalike_is_still_refused(self):
        """The match is on a dot boundary, not a substring."""
        p = _policy()
        for host in ("evilregistry.npmjs.org.attacker.net", "notcrates.io",
                     "ghcr.io.example.com"):
            with self.subTest(host=host):
                self.assertFalse(p.host_allowed(host, 443)[0])

    def test_each_widening_is_declared_for_the_audit(self):
        """docs/HANDOFF-sandboxing.md audits the allowlist; every widening
        says so in the file, where the next person reading it will look."""
        text = PROXY.read_text()
        self.assertIn("SANDBOX AUDIT", text[:text.index('"registry.npmjs.org"')][-2000:])
        self.assertIn("BLOCKED_NETS", text)


class TestTheApiGoesToTheInjector(unittest.TestCase):
    """api.github.com is the one host whose TLS is not tunnelled: its CONNECT
    goes to the credential injector, which puts the real token in the
    Authorization header so that no workspace has to hold it."""

    def test_the_api_is_allowed_on_443_and_named_as_the_injectors(self):
        ok, why = _policy().host_allowed("api.github.com", 443)
        self.assertTrue(ok, why)
        self.assertIn("injector", why)

    def test_and_on_no_other_port(self):
        """A tunnel on 22 or 80 would be a way past the injector, and the
        generic `github.com` suffix would grant both if this were not an exact
        match checked before it."""
        p = _policy()
        for port in (22, 80, 9418):
            with self.subTest(port=port):
                ok, why = p.host_allowed("api.github.com", port)
                self.assertFalse(ok, why)

    def test_the_upload_api_is_still_refused(self):
        ok, why = _policy().host_allowed("uploads.github.com", 443)
        self.assertFalse(ok)
        self.assertIn("refused", why)

    def test_a_lookalike_is_neither_injected_nor_allowed(self):
        p = _policy()
        for host in ("evilapi.github.com.attacker.net",
                     "api.github.com.attacker.net",
                     "notapi.github.com.evil.example"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertFalse(ok, f"{host}: {why}")

    def test_github_itself_and_codeload_are_untouched(self):
        p = _policy()
        for host, port in (("github.com", 443), ("github.com", 22),
                           ("codeload.github.com", 443),
                           ("raw.githubusercontent.com", 443)):
            with self.subTest(host=host, port=port):
                ok, why = p.host_allowed(host, port)
                self.assertTrue(ok, f"{host}:{port} {why}")
                self.assertNotIn("injector", why)

    def test_the_injected_host_is_routed_to_a_socket_under_the_store(self):
        """Never under $XDG_RUNTIME_DIR/wk: that is the directory every
        container bind-mounts, and a workspace must reach the injector through
        this policy rather than around it."""
        m = _load(PROXY, "wkproxy")
        self.assertNotIn("/wk/", m.INJECT_SOCKET.replace("/var/lib/wk/", ""))
        self.assertTrue(m.INJECT_SOCKET.endswith("github-inject.sock"))
        text = PROXY.read_text()
        self.assertIn("asyncio.open_unix_connection(INJECT_SOCKET)", text)

    def test_the_widening_is_declared_for_the_audit(self):
        self.assertIn("SANDBOX AUDIT", PROXY.read_text())


class TestTheRouteAndTheCheckReadOneSpelling(unittest.TestCase):
    """A host name is case-insensitive and may carry a trailing dot, so one
    name has several spellings. The allowlist normalised and the route did
    not, so `CONNECT API.GITHUB.COM:443` passed the check as the injected host
    and then got a plain tunnel to GitHub -- the credential injector out of the
    path, and TLS the workspace terminates itself."""

    def _routed(self, target):
        """Where `handle` sent this CONNECT: the host and port open_upstream
        was asked for, with the real allowlist in front of it."""
        m = _load(PROXY, "wkproxy")
        proxy = m.Proxy(m.Policy(tempfile.mkdtemp(prefix="wk-test-store-")))
        seen = []

        async def fake_open_upstream(host, port):
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

    def test_the_injector_branch_matches_that_one_spelling(self):
        """open_upstream routes on an exact dict lookup, so the normalisation
        has to have happened before it -- this is the assertion that the two
        cannot drift apart again."""
        m = _load(PROXY, "wkproxy")
        for spelled in ("API.GITHUB.COM", "api.github.com."):
            with self.subTest(spelled=spelled):
                self.assertNotIn(spelled, m.INJECTED_HOSTS)
                self.assertEqual("api.github.com", m.normalize_host(spelled))

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
        out = self.m.rewrite_head(self.head(), "ghp-not-a-real-token")
        self.assertIn(b"Authorization: Bearer ghp-not-a-real-token", out)
        self.assertNotIn(b"Basic", out)
        self.assertEqual(1, out.count(b"Authorization:"))

    def test_the_request_line_and_the_other_headers_survive(self):
        out = self.m.rewrite_head(self.head(), "ghp-x")
        self.assertTrue(out.startswith(b"GET /user HTTP/1.1\r\n"))
        self.assertIn(b"Host: api.github.com", out)
        self.assertIn(b"User-Agent: python-requests/2.31.0", out)
        self.assertTrue(out.endswith(b"\r\n\r\n"))

    def test_with_no_token_the_placeholder_is_stripped_and_nothing_added(self):
        """GitHub then answers for itself -- 200 for a public endpoint, 401 for
        one that needs an account -- which is what `wk verify` measures either
        side of the switch. Forwarding the placeholder would only ever be a way
        to smuggle a credential out."""
        out = self.m.rewrite_head(self.head(), "")
        self.assertNotIn(b"Authorization", out)
        self.assertNotIn(b"Basic", out)

    def test_the_connection_is_closed_per_request(self):
        """One request per TLS connection, so GitHub closes and the client
        learns it from GitHub's own response: no response header is parsed
        here, and no keep-alive stream is left half-rewritten."""
        out = self.m.rewrite_head(self.head(), "ghp-x")
        self.assertIn(b"Connection: close", out)
        self.assertEqual(1, out.count(b"Connection:"))
        self.assertNotIn(b"keep-alive", out)

    def test_a_smuggled_proxy_credential_is_dropped_too(self):
        head = self.head(b"\r\nProxy-Authorization: Basic zzz")
        out = self.m.rewrite_head(head, "ghp-x")
        self.assertNotIn(b"Proxy-Authorization", out)

    def test_the_token_is_read_from_the_file_on_every_request(self):
        """`wk push off` removes it, and the next request must go
        unauthenticated rather than be served from something remembered."""
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "pat")
            self.assertEqual("", self.m.read_token(path))
            Path(path).write_text("ghp-not-a-real-token\n")
            self.assertEqual("ghp-not-a-real-token", self.m.read_token(path))
            Path(path).unlink()
            self.assertEqual("", self.m.read_token(path))

    def test_only_one_host_is_ever_terminated(self):
        self.assertEqual("api.github.com", self.m.INJECT_HOST)
        self.assertEqual(443, self.m.INJECT_PORT)

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_it_makes_its_own_ca_and_publishes_only_the_public_half(self):
        """`cryptography` is absent from the macOS host's python, so the
        material is made with the CLI that is on both machines."""
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

            # A second run keeps it: the CA is what every workspace was told to
            # trust, so regenerating it would break every one of them.
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
            self.assertIn("CA:FALSE", out)


class TestTheInjectorSendsItsOwnHost(unittest.TestCase):
    """The forwarded connection is a TLS session pinned to api.github.com and
    carrying the real token. GitHub's front end routes on the Host header, so a
    client-chosen one is a way to spend that token against a name the allowlist
    refuses -- `Host: uploads.github.com` above all, which DENIED_HOSTS exists
    to keep out."""

    def setUp(self):
        self.m = _load(INJECT, "wkinject")

    def test_a_foreign_host_is_replaced_by_the_one_this_is_pinned_to(self):
        out = self.m.rewrite_head(
            b"POST /repos/x/y/releases HTTP/1.1\r\n"
            b"Host: uploads.github.com\r\n"
            b"Content-Length: 0", "ghp-x")
        self.assertIn(b"Host: api.github.com\r\n", out)
        self.assertNotIn(b"uploads.github.com", out)
        self.assertEqual(1, out.count(b"Host:"))

    def test_a_request_with_no_host_at_all_still_gets_one(self):
        out = self.m.rewrite_head(b"GET /user HTTP/1.1", "ghp-x")
        self.assertIn(b"Host: api.github.com\r\n", out)

    def test_the_host_it_sends_is_the_host_it_verified(self):
        self.assertIn(b"Host: " + self.m.INJECT_HOST.encode(),
                      self.m.rewrite_head(b"GET / HTTP/1.1\r\nHost: evil.example", ""))


class TestTheInjectorReadsOneRequestAndNoMore(WkTest):
    """Everything past the first request head is relayed by nothing: a second
    request on the same connection is a head this program never rewrote, so it
    would reach GitHub carrying the client's own Authorization. And a header
    line ended by a bare LF is one line to this program's `\r\n` split and two
    to GitHub's parser -- the same smuggle, inside the first head."""

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
        """The truncation is at the declared length, not at zero: a real
        `git-webkit pr` is a POST with a JSON body."""
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
        """This program states the length of what it actually relayed; the
        client's Content-Length and Transfer-Encoding are dropped, so nothing
        downstream can read a different end-of-request than this one did."""
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
# hand, and the reachability probe `wk verify` measures the injector with.
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


class TestTheInjectorForwardsEverything(WkTest):
    """The decision, and it is a decision rather than an oversight: the
    injector refuses nothing on policy. Every method on every path reaches
    api.github.com and GitHub answers for itself. `wk push on` means a person
    is watching what the workspace does, so which of their own endpoints it
    reaches is theirs to decide; `wk push off` leaves a write no credential at
    all, and GitHub answers 401."""

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
        """The read token reaches whatever the account it belongs to can read.
        What bounds that is the token's own scope, not this program."""
        for target in ("/repos/WebKit/WebKit/actions/secrets",
                       "/repos/an-employer/private-thing/contents/secrets.txt",
                       "/user/emails"):
            with self.subTest(target=target):
                self.forward("GET", target, read_token="ghp-read-only")

    def test_a_hostile_write_is_forwarded_and_carries_the_switchs_token(self):
        """Deliberate. Each of these could delete a repository, add a deploy
        key or exfiltrate, and with push on a person is watching every one of
        them; the workspace is not the thing deciding, and neither is this
        program."""
        for method, target in HOSTILE_WRITES:
            with self.subTest(request="%s %s" % (method, target)):
                upstream, _ = self.forward(method, target)
                self.assertIn(b"Authorization: Bearer ghp-not-a-real-token",
                              upstream)

    def test_a_hostile_write_carries_nothing_with_the_switch_off(self):
        for method, target in HOSTILE_WRITES:
            with self.subTest(request="%s %s" % (method, target)):
                upstream, logged = self.forward(
                    method, target, token=None, read_token="ghp-read-only")
                self.assertNotIn(b"Authorization", upstream)
                self.assertNotIn(b"ghp-read-only", upstream)
                self.assertIn("write unauthenticated", logged)

    def test_a_path_a_server_would_normalise_is_forwarded_unchanged(self):
        """There is no rule left for a `..` or a percent-escape to walk past,
        so the path GitHub is sent is the path the workspace wrote."""
        for target in ("/repos/a/b/pulls/../../../../user",
                       "/repos/a/b/%2e%2e/%2e%2e/keys",
                       "//repos/a/b/pulls"):
            with self.subTest(target=target):
                self.forward("POST", target)

    def test_a_request_line_this_cannot_read_is_a_write(self):
        """`request_line` answers ("", "") rather than refusing, which is not
        a read: the line goes on to api.github.com exactly as it arrived and
        is answered there, having spent the switch's token and nothing else."""
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
    """GraphQL is the whole API behind one path, so the method cannot decide
    read from write there and the document does. Neither answer is a refusal:
    a query spends the standing read token, which is what keeps `git-webkit`'s
    pull request lookup (PRGenerator.find) working with the switch off, and a
    mutation is a write like any other."""

    def setUp(self):
        super().setUp()
        self.m = _load(INJECT, "wkinject")

    def test_the_query_git_webkit_sends_is_a_read(self):
        body = (b'{"query": "query { search(query: \\"repo:WebKit/WebKit '
                b'is:pr\\", type: ISSUE, last: 100) { edges { node { number } '
                b'} } }"}')
        self.assertTrue(self.m.is_read("POST", "/graphql", body))

    def test_a_mutation_is_a_write(self):
        self.assertFalse(self.m.is_read(
            "POST", "/graphql", b'{"query": "mutation { deleteRef(input: {}) }"}'))

    def test_a_mutation_after_a_query_in_one_document_is_still_a_mutation(self):
        """A GraphQL document holds as many operations as it likes, and GitHub
        runs the one it is asked for: a rule that read only the first would be
        a rule anything could walk past."""
        body = (b'{"query": "query Find { viewer { login } } '
                b'mutation Land { mergePullRequest(input: {}) { clientMutationId } }",'
                b' "operationName": "Land"}')
        self.assertFalse(self.m.is_read("POST", "/graphql", body))

    def test_the_keyword_is_matched_however_it_is_spelled(self):
        """Case-insensitively, which calls documents writes that GitHub would
        run as queries and never the other way round."""
        for spelled in (b"Mutation", b"MUTATION", b"mUtAtIoN"):
            with self.subTest(spelled=spelled):
                self.assertFalse(self.m.is_read(
                    "POST", "/graphql", b'{"query": "' + spelled + b' { x }"}'))

    def test_a_query_string_does_not_hide_the_body(self):
        self.assertTrue(self.m.is_read("POST", "/graphql?anything",
                                       b'{"query": "query { x }"}'))
        self.assertFalse(self.m.is_read("POST", "/graphql?anything",
                                        b'{"query": "mutation { x }"}'))

    def test_graphql_by_any_other_method_is_a_write(self):
        self.assertFalse(self.m.is_read("PUT", "/graphql",
                                        b'{"query": "query { x }"}'))

    def test_a_query_spends_the_read_token_and_a_mutation_the_switchs(self):
        """End to end, both ways, on a machine holding both tokens."""
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
    """Which token a request spends, which is the whole of what `wk push`
    switches. The read token stands whatever position the switch is in; the
    write token is the file `wk push on|off` writes and removes, and it is the
    only thing a write may ever spend."""

    def setUp(self):
        super().setUp()
        self.m = _load(INJECT, "wkinject")
        self.pat = self.tmp / "push-github-pat"
        self.read_pat = self.tmp / "read-github-pat"
        self.inj = self.m.Injector(str(self.pat), str(self.read_pat), None)

    def test_a_read_spends_the_read_token(self):
        self.read_pat.write_text("ghp-read-only\n")
        self.assertEqual("ghp-read-only", self.inj.token_for(True))

    def test_a_read_falls_back_to_the_switchs_token(self):
        """A machine with no read token installed reads with the one it has:
        reading is what the switch is not about."""
        self.pat.write_text("ghp-the-write-token\n")
        self.assertEqual("ghp-the-write-token", self.inj.token_for(True))

    def test_the_read_token_wins_when_both_are_there(self):
        self.pat.write_text("ghp-the-write-token\n")
        self.read_pat.write_text("ghp-read-only\n")
        self.assertEqual("ghp-read-only", self.inj.token_for(True))

    def test_the_read_token_is_not_a_write_token(self):
        """The decision the whole split rests on. A read token on a machine
        with the switch off must not let a write through: the write goes
        unauthenticated and GitHub answers 401 for itself."""
        self.read_pat.write_text("ghp-read-only\n")
        self.assertEqual("", self.inj.token_for(False))

    def test_a_write_spends_the_switchs_token(self):
        self.pat.write_text("ghp-the-write-token\n")
        self.assertEqual("ghp-the-write-token", self.inj.token_for(False))

    def test_a_read_carries_the_read_token_with_the_switch_off(self):
        """End to end, and the state a workspace is in for most of its life:
        no write token on the machine at all, and `gh` still works."""
        _, upstream, opened, logged = drive_injector(
            self.tmp,
            b"GET /repos/WebKit/WebKit/pulls/1234 HTTP/1.1\r\n"
            b"Host: api.github.com\r\n\r\n",
            token=None, read_token="ghp-read-only")
        self.assertEqual([("api.github.com", 443)], opened)
        self.assertIn(b"Authorization: Bearer ghp-read-only", upstream)
        self.assertIn("read inject GET /repos/WebKit/WebKit/pulls/1234", logged)

    def test_a_write_carries_nothing_with_the_switch_off(self):
        """The same machine and the same read token: the write is forwarded,
        because refusing it would be this program pretending the API is
        unreachable, and unauthenticated, because the switch is off."""
        _, upstream, opened, logged = drive_injector(
            self.tmp,
            b"POST /repos/WebKit/WebKit/pulls HTTP/1.1\r\n"
            b"Host: api.github.com\r\nContent-Length: 2\r\n\r\nhi",
            token=None, read_token="ghp-read-only")
        self.assertEqual([("api.github.com", 443)], opened)
        self.assertNotIn(b"Authorization", upstream)
        self.assertNotIn(b"ghp-read-only", upstream)
        self.assertIn("write unauthenticated POST /repos/WebKit/WebKit/pulls",
                      logged)


class TestWhatGhNeeds(unittest.TestCase):
    """`gh` reads GitHub through the same injector as everything else, so it
    needs two things nothing else does: a token to put in an Authorization
    header for the injector to replace, and the CA in the two variables Go's
    crypto/x509 reads -- it reads none of REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE
    or GIT_SSL_CAINFO. Both only where the bundle was built: naming a file
    that is not there fails every HTTPS request in the workspace.

    Driven, both branches, against a scratch tree standing in for the three
    paths that exist only in a container.
    """

    def run_bridge(self, ca, *print_vars):
        """(what the wrapped command printed, the bundle's path, its bytes)."""
        import os
        d = Path(tempfile.mkdtemp(prefix="wk-test-gh-env-"))
        try:
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
                .replace("/etc/ssl/certs/ca-certificates.crt", str(sysca)))
            rw = d / "rw"
            rw.mkdir()
            show = "; ".join('printf "%s=[%s]\\n" ' + v + ' "${' + v + ':-}"'
                             for v in print_vars)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("GH_TOKEN", "SSL_CERT_FILE", "SSL_CERT_DIR")}
            env["TMPDIR"] = str(rw)
            cp = subprocess.run(["bash", str(script), "sh", "-c", show],
                                env=env, capture_output=True, text=True,
                                timeout=60)
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            bundle = rw / ".wk-ca-bundle.pem"
            text = bundle.read_text() if bundle.exists() else ""
            return cp.stdout, bundle, text
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_gh_gets_the_placeholder_and_the_bundle_go_reads(self):
        out, bundle, _ = self.run_bridge(True, "GH_TOKEN", "SSL_CERT_FILE",
                                         "SSL_CERT_DIR")
        self.assertIn("GH_TOKEN=[wk-injects-this]", out)
        self.assertIn("SSL_CERT_FILE=[%s]" % bundle, out)
        self.assertIn("SSL_CERT_DIR=[/etc/ssl/certs]", out)

    def test_a_workspace_with_no_injector_ca_gets_neither(self):
        """No CA is no injector in the path, and a placeholder token would
        then be an Authorization header nothing replaces."""
        out, _, _ = self.run_bridge(False, "GH_TOKEN", "SSL_CERT_FILE",
                                    "SSL_CERT_DIR")
        self.assertIn("GH_TOKEN=[]", out)
        self.assertIn("SSL_CERT_FILE=[]", out)
        self.assertIn("SSL_CERT_DIR=[]", out)

    def test_the_bundle_it_names_is_the_systems_plus_the_ca(self):
        _, _, text = self.run_bridge(True, "SSL_CERT_FILE")
        self.assertEqual("THE-SYSTEM-STORE\nTHE-INJECTORS-CA\n", text)

    def test_a_guest_gets_the_same_two(self):
        """A guest's injector is the host's, and `wk verify` measures the
        placeholder on both targets, so the file that writes a guest's
        environment carries them too (targets/vm.sh, ~/.wk-egress)."""
        vm = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn("export GH_TOKEN=wk-injects-this", vm)
        self.assertIn("export SSL_CERT_FILE=", vm)


class TestTheWorkspaceHoldsThePlaceholder(unittest.TestCase):
    """Both targets set the same two variables and the same CA bundle, from the
    one wrapper each of them already goes through."""

    def test_a_container_gets_them_from_ensure_bridge(self):
        text = (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", text)
        self.assertIn("GITHUB_COM_USERNAME", text)
        for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            with self.subTest(var=var):
                self.assertIn(var, text)

    def test_a_full_temp_directory_still_runs_the_command(self):
        """ensure-bridge.sh wraps every `wk run` and `wk build`, and under
        `set -euo pipefail` the pidfile write and the CA-bundle build abort the
        whole exec when the temp directory cannot be written -- so a workspace
        whose /tmp filled up could run nothing at all, and each attempt left
        another `.wk-ca-bundle.pem.$$` behind in the directory that was full.

        The three paths that exist only in a container (/opt/wk-tools, /run/wk,
        the system CA bundle) are pointed at a scratch tree; every line under
        test is the file's own.
        """
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

    def test_a_writable_temp_directory_still_builds_the_bundle(self):
        """The other side of the same branch: nothing above may be bought by
        the bundle no longer being built where it can be."""
        import os
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="wk-test-rw-tmp-"))
        try:
            tools = d / "wk-tools"
            (tools / "shell").mkdir(parents=True)
            (tools / "container" / "proxy").mkdir(parents=True)
            (tools / "shell" / "path.sh").write_text(":\n")
            (tools / "container" / "proxy" / "bridge.py").write_text(
                "import time; time.sleep(30)\n")
            runwk = d / "run-wk"
            runwk.mkdir()
            (runwk / "wk-github-ca.pem").write_text("THE-INJECTORS-CA\n")
            sysca = d / "ca-certificates.crt"
            sysca.write_text("THE-SYSTEM-STORE\n")
            script = d / "ensure-bridge.sh"
            script.write_text(
                (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
                .replace("/opt/wk-tools", str(tools))
                .replace("/run/wk/wk-github-ca.pem", str(runwk / "wk-github-ca.pem"))
                .replace("/etc/ssl/certs/ca-certificates.crt", str(sysca)))
            rw = d / "rw"
            rw.mkdir()
            cp = subprocess.run(
                ["bash", str(script), "sh", "-c", 'echo "$CURL_CA_BUNDLE"'],
                env={**os.environ, "TMPDIR": str(rw)},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            bundle = rw / ".wk-ca-bundle.pem"
            self.assertEqual(str(bundle), cp.stdout.strip(), cp.stdout)
            self.assertEqual("THE-SYSTEM-STORE\nTHE-INJECTORS-CA\n",
                             bundle.read_text())
            self.assertEqual([], sorted(rw.glob(".wk-ca-bundle.pem.*")))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_bundle_is_the_systems_plus_the_ca(self):
        """Those variables replace the trust store outright, so a bundle
        holding one certificate would fail every other HTTPS request."""
        text = (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
        self.assertIn("cat /etc/ssl/certs/ca-certificates.crt", text)

    def test_a_guest_gets_them_with_its_egress(self):
        text = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", text)
        for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            with self.subTest(var=var):
                self.assertIn(var, text)

    def test_no_real_token_is_anywhere_in_the_tree(self):
        """The placeholder is the only thing that may be written down: the
        token itself is in wk_push_held_dir on one machine."""
        for f in (REPO / "container" / "proxy" / "ensure-bridge.sh",
                  REPO / "targets" / "vm.sh",
                  REPO / "container" / "proxy" / "github-inject.py"):
            with self.subTest(f=f.name):
                self.assertNotIn("ghp_", f.read_text())


EGRESS = (
    "# wk: written by targets/vm.sh on every start\n"
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
    """Run the guest's shell wiring, as targets/vm.sh streams it in on every
    start. More than once to prove a second start changes nothing."""
    for _ in range(times):
        cp = subprocess.run(
            ["bash", str(SHELL_RC), str(REPO)],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60,
        )
        assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


def _shell_vars(shell, home, args):
    """Every proxy variable a shell started with `args` ends up with. The rc
    files are read for real -- no `source` by hand -- because which of them a
    given invocation reads is the whole question."""
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


class TestGuestProxyEnvironment(WkTest):
    """Which shells in a guest have egress. The pane is the case that was
    broken; the others must not regress while fixing it."""

    # What a person and what `wk` actually start, and how each is spelled.
    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh (the guest's own window)": ("zsh", ["-l", "-c"]),
        "bash -lc (every t_exec)": ("bash", ["-lc"]),
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
        """An unfiltered guest, and the moment before the host has written it.
        Absence has to mean absence rather than a stale default."""
        home = self._home(egress=False)
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                got = _shell_vars(shell, home, args)
                for v in VARS:
                    self.assertEqual(got.get(v), "", f"{v} set from nothing: {got}")

    def test_the_wiring_is_idempotent(self):
        """It runs on every start; a guest started fifty times has one of each
        line, not fifty."""
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
    """A guest cloned from a base that baked the address into its profiles
    converges on the next start, rather than needing the base rebuilt."""

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
        # Only wk's own stanza goes; anything else in the file is the guest's.
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


class TestNothingBakesTheAddressIn(unittest.TestCase):
    """One writer for one fact. The address is the host's own on the guest
    bridge and changes, so provisioning may not record it."""

    def test_provisioning_writes_no_proxy_exports(self):
        text = (REPO / "vm" / "provision-base.sh").read_text()
        self.assertNotIn("export http_proxy", text)
        self.assertNotIn("WK_VM_PROXY_ADDR", text)

    def test_the_start_path_writes_the_egress_file(self):
        """One `_converge_guest`, called from both t_start arms: a guest that
        was already running gets its egress written exactly like one this
        start booted."""
        self.assertIn(".wk-egress", (REPO / "targets" / "vm.sh").read_text())
        assert_guest_start_converges(self, '_set_guest_egress "$name" "$ip"')


if __name__ == "__main__":
    unittest.main()
