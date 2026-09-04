#!/usr/bin/env python3
"""The GitHub API credential injector.

`git-webkit pr` -- the one thing in a workspace that has to talk to GitHub's
API -- authenticates with a token it reads from its environment
(GITHUB_COM_USERNAME/GITHUB_COM_TOKEN, webkitcorepy; the keyring is unusable in
a container). A token in the environment is a token the agent in that workspace
can read, copy and reuse, and no agent may publish (cmd/push). So the workspace
holds a *placeholder* and the real token is here, outside it: this program
terminates TLS for api.github.com, replaces the Authorization header, and
forwards the request. Nothing inside ever sees the token.

The boundary is unchanged in shape: a workspace still has no network interface
and still reaches the outside only through wk-proxy's unix socket. wk-proxy
sends this one host's CONNECT here instead of opening a tunnel (INJECTED_HOSTS,
container/proxy/wk-proxy.py), and this is the only host anything terminates TLS
for -- everything else is still a byte pipe nobody reads.

Why a hundred lines of `ssl` rather than mitmproxy, which does this for a
living: the same injector has to run on the macOS host for the guest VMs, whose
only guaranteed python3 is /usr/bin/python3 (3.9.6, LibreSSL 2.8.3), and
mitmproxy needs 3.12 or newer. Measured in the podman machine as well: it
installs there from wheels but `mitmdump` dies with SIGILL, because
cryptography >= 47's Rust extension uses an instruction that guest does not
have. One implementation for both machines is worth more than the library.

Certificate material is made with the `openssl` CLI, which is on both machines,
rather than with a python module: `cryptography` is absent from the macOS
host's python altogether.

    WK_INJECT_SOCK    the unix socket wk-proxy connects to
    WK_INJECT_DIR     private directory for the CA and the leaf (0700)
    WK_INJECT_CA_OUT  where the CA's *public* certificate is published, for
                      the workspaces to trust
    WK_INJECT_PAT       the write token, the file `wk push on|off` writes and
                        removes
    WK_INJECT_READ_PAT  the read token, which stands whatever position the
                        switch is in

Reading and writing are separated, and that separation is the whole policy.
A read -- GET, HEAD, and a GraphQL document with no mutation in it -- is
forwarded on every path and authenticated from the standing token: an agent in
a workspace has to read a pull request, EWS statuses and the issue a branch
tracks, `gh` has to work, and no read changes anything of the person's. A write
is forwarded only where ALLOW below has it, row by row from what `git-webkit`
issues, and only with the switch's token: a workspace that could spend the
token on any path could delete a repository, add a deploy key or run an Actions
workflow as them.

Neither token is ever inside a workspace. A refused write is answered 403 here
and never reaches GitHub, naming the method, the path and this file, because a
`git-webkit` that grows an endpoint has to fail legibly and be fixed with one
row. A write with no token goes on unauthenticated and GitHub answers 401 for
itself: the switch withholds a credential, it does not pretend the API is
unreachable.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile

# systemd starts this by absolute path, so the import path is derived from this
# file's own rather than assumed (lib/wknotify.py).
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from wknotify import sd_notify  # noqa: E402

# Exactly one host, and it is not a list for a reason: every name that is
# terminated here is a name whose traffic something reads, so widening this is
# a decision and not a configuration change.
INJECT_HOST = "api.github.com"
INJECT_PORT = 443

READ_TIMEOUT = 30
IDLE_TIMEOUT = 300
MAX_HEAD = 65536          # a request head larger than this is not a request

# Headers dropped from every request before it is forwarded. Authorization,
# because replacing it is the point. The connection ones, because this handles
# one request per TLS connection: forwarding `Connection: close` makes GitHub
# close, the client learns it from GitHub's own response, and no response
# header has to be parsed here at all. Host, because this TLS session is pinned
# to INJECT_HOST and the token goes with it: a client that sent
# `Host: uploads.github.com` would otherwise reach a host the allowlist refuses
# (DENIED_HOSTS, container/proxy/wk-proxy.py) carrying the real credential.
# Transfer-Encoding and Content-Length, because the body this forwards is
# exactly the Content-Length bytes it read and it re-states that itself.
DROP = ("authorization", "connection", "proxy-connection", "keep-alive",
        "proxy-authorization", "host", "transfer-encoding", "content-length")


def log(msg):
    print("[wk-github-inject] %s" % msg, file=sys.stderr, flush=True)


# --- the policy table --------------------------------------------------------
# What may be *written*. Every row names the file in WebKit's
# Tools/Scripts/libraries that builds that URL, so the table is re-derived
# rather than trusted. Reading is not in here at all: READ_METHODS is open on
# every path.
#
#   *   exactly one path segment
#   #   exactly one path segment of ASCII digits -- a pull request or issue
#       number, so a rule for `pulls/1234` is not also a rule for `pulls/foo`
#
# A wildcard never crosses the fixed segments around it, so `POST
# /repos/*/*/pulls` is not also `POST /repos/*/*/actions/workflows/x/dispatches`.
POLICY_SOURCE = "container/proxy/github-inject.py"

# The methods that cannot change anything at GitHub. They are allowed on every
# path and authenticated whatever position `wk push` is in: reading is what an
# agent in a workspace has to do to work -- a pull request, EWS statuses, the
# issue a branch tracks, and `gh` -- and a read cannot be the thing a person
# has to be protected from. The switch is over writing.
READ_METHODS = ("GET", "HEAD")

ALLOW = (
    # Opening and updating a pull request, and the review conversation on it --
    # what `git-webkit pr` and `git-webkit review` exist to do
    # (webkitscmpy/remote/git_hub.py, PRGenerator.create/update/_make_comment
    # /review; GitHub takes POST, not PATCH, for the update).
    ("POST", "/repos/*/*/pulls"),
    ("POST", "/repos/*/*/pulls/#"),
    ("POST", "/repos/*/*/pulls/#/comments"),
    ("POST", "/repos/*/*/pulls/#/reviews"),
    # The issue side of the same act: file the bug, comment the PR's URL on it,
    # assign it, label it (webkitbugspy/github.py, Tracker.create/add_comment
    # /add_assignees/set; webkitscmpy/program/create_bug.py).
    ("POST", "/repos/*/*/issues"),
    ("POST", "/repos/*/*/issues/#/comments"),
    ("POST", "/repos/*/*/issues/#/assignees"),
    ("PATCH", "/repos/*/*/issues/#"),
    ("PUT", "/repos/*/*/issues/#/labels"),
    # Fast-forwarding the fork's branch to upstream instead of pushing it,
    # with webkitscmpy.update-fork set (webkitscmpy/program/pull_request.py).
    ("POST", "/repos/*/*/merge-upstream"),
)

# What is deliberately absent, because it is not something a workspace does:
# `git-webkit setup` creating and renaming a fork (POST /repos/*/*/forks, PATCH
# /repos/*/*, PUT /orgs/*/repos/*/*) -- `wk remotes` sets a workspace's remotes
# up from the host, and PATCH on a repository is its settings, including
# whether a private one stays private. Releases and their assets are absent for
# the same reason, and their upload host is refused outright anyway
# (DENIED_HOSTS, container/proxy/wk-proxy.py).

_NUMBER = re.compile(r"[0-9]+")

# A GraphQL document executes a mutation only if the keyword appears in it: the
# grammar has no other spelling, and the shorthand `{...}` is a query. Matched
# case-insensitively, which refuses more documents than GitHub would treat as
# mutations and never fewer -- `git-webkit`'s one query (PRGenerator.find) does
# not contain the word.
_MUTATION = re.compile(rb"mutation", re.IGNORECASE)


def path_segments(path):
    """The segments of a path this program is willing to match, or None.

    None for anything whose segments are not what GitHub will route on: a
    percent-escape (this matches the undecoded path, so `%2e%2e` would be a
    segment here and a parent directory there), an empty segment, and `.` or
    `..` -- which a server normalises away, so `commits/../../user` would match
    a rule for `commits/**` here and be `/user` at the far end.
    """
    if not path.startswith("/") or "%" in path:
        return None
    if path == "/":
        return []
    segments = path[1:].split("/")
    if any(s in ("", ".", "..") for s in segments):
        return None
    return segments


def request_line(head):
    """(method, target) from the request head, or None for one this cannot
    read. Both are what the workspace sent, undecoded."""
    parts = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").split(" ")
    if len(parts) != 3 or not parts[1].startswith("/"):
        return None
    return parts[0], parts[1]


def is_read(method, target, body):
    """Whether this request can only read.

    GET and HEAD by definition. POST /graphql is the exception in both
    directions: GraphQL is the whole API behind one path, so a document with no
    mutation in it is a read like any other, and one with a mutation is refused
    outright rather than weighed against the write table.
    """
    if method in READ_METHODS:
        return True
    return (method == "POST"
            and target.split("?", 1)[0] == "/graphql"
            and not _MUTATION.search(body))


def allowed(method, target):
    """Is this write one the table authenticates?

    `target` is the request line's target, query string and all: the query is
    dropped before matching, so `/user?x=/repos/a/b/pulls` is `/user` and
    nothing reaches a rule through a `?`.
    """
    segments = path_segments(target.split("?", 1)[0])
    if segments is None:
        return False
    for allowed_method, pattern in ALLOW:
        if allowed_method == method and _matches(pattern, segments):
            return True
    return False


def _matches(pattern, segments):
    parts = pattern[1:].split("/")
    for i, part in enumerate(parts):
        if i >= len(segments):
            return False
        if part == "*":
            continue
        if part == "#":
            if not _NUMBER.fullmatch(segments[i]):
                return False
            continue
        if part != segments[i]:
            return False
    return len(parts) == len(segments)


def policy_refusal(head, body):
    """None if this request may be forwarded, else (status line, reason).

    A read is always forwarded. A write is forwarded only where the table has
    it, and the reason a refused one gets is what a person in a workspace reads
    when `git-webkit` grows an endpoint the table does not have yet: it names
    the method, the path, the table and the file the table is in, so the fix is
    one row, in one place.
    """
    parsed = request_line(head)
    if parsed is None:
        return (b"400 Bad Request",
                b"a request line the wk credential injector cannot read is "
                b"refused; it matches METHOD /path HTTP/1.1 against the ALLOW "
                b"table in " + POLICY_SOURCE.encode("latin-1") + b"\r\n")
    method, target = parsed
    if method == "POST" and target.split("?", 1)[0] == "/graphql" \
            and _MUTATION.search(body):
        return (b"403 Forbidden",
                ("POST /graphql carrying a mutation is refused by the wk "
                 "credential injector: GraphQL is the whole API behind one "
                 "path, so a query is a read and a mutation is a write with no "
                 "path to name in the table.\r\nThe rule is next to ALLOW in "
                 "%s.\r\n" % POLICY_SOURCE).encode("latin-1"))
    if is_read(method, target, body) or allowed(method, target):
        return None
    return (b"403 Forbidden",
            ("%s %s is a write, and it is not in the wk credential injector's "
             "ALLOW table, so it was not sent to api.github.com and no token "
             "was spent on it.\r\nReading is open on every path; writing is "
             "this table. It is ALLOW in %s: add the (method, path) rule there "
             "if git-webkit needs this endpoint. A path with a percent-escape, "
             "an empty segment or a '.' or '..' segment never matches a "
             "rule.\r\n" % (method, target[:200], POLICY_SOURCE))
            .encode("latin-1"))


def rewrite_head(head, token, length=0):
    """The whole of the injection: one request head in, one out.

    Whatever the workspace sent as Authorization is dropped -- it is a
    placeholder, and forwarding it would only ever be a way to smuggle a
    credential out. The real token is added when there is one.

    With no token this forwards the request *unauthenticated* rather than
    refusing it: GitHub then answers for itself, 200 for a public endpoint and
    401 for one that needs an account. That is the honest answer -- the switch
    withholds a credential, it does not pretend the API is unreachable -- and
    it is what `wk verify` measures either side of the switch.

    The Host header is this program's own, never the client's: the connection
    it forwards on is a TLS session pinned to INJECT_HOST and carrying the real
    token, so a client-chosen Host is a way to spend that token against another
    name behind GitHub's front end.

    Content-Length is this program's own too, and `length` is the number of
    body bytes it actually relays: the client's framing headers are dropped
    (DROP) so that GitHub and this program cannot disagree about where the
    request ends, which is the disagreement a smuggled second request needs.

    A function rather than inline code because it is the part with a rule in
    it, and the rule is testable without a socket (tests/test_egress.py).
    """
    lines = head.split(b"\r\n")
    out = [lines[0], b"Host: " + INJECT_HOST.encode("latin-1")]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name.decode("latin-1", "replace") in DROP:
            continue
        out.append(line)
    if token:
        out.append(b"Authorization: Bearer " + token.encode("latin-1"))
    out.append(b"Content-Length: %d" % length)
    out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


def body_length(head):
    """How many body bytes this request declares, or a refusal.

    Returns (length, None) or (None, (status line, reason)). Everything after
    that many bytes is a *second* request on the same connection, and the head
    of a second request is a head this program never rewrote -- so it would
    carry the client's own Authorization straight to GitHub. Only what is
    declared here is relayed, and the connection is then closed.

    A chunked body would need this program to parse the framing to know where
    the request ends, and a framing this does not agree with GitHub about is
    exactly how a second request is smuggled through. Refused instead: the
    clients in a workspace (`git-webkit pr`, curl, requests) all send a length.
    """
    for line in head.split(b"\r\n")[1:]:
        name = line.split(b":", 1)[0].strip().lower()
        if name == b"transfer-encoding":
            return None, (b"411 Length Required",
                          b"a chunked request body is refused by the wk "
                          b"credential injector; send Content-Length\r\n")
    lengths = [line.split(b":", 1)[1].strip()
               for line in head.split(b"\r\n")[1:]
               if line.split(b":", 1)[0].strip().lower() == b"content-length"]
    if not lengths:
        return 0, None
    if len(lengths) > 1 or not lengths[0].isdigit():
        return None, (b"400 Bad Request",
                      b"a request with no single Content-Length is refused by "
                      b"the wk credential injector\r\n")
    return int(lengths[0]), None


def bare_lf(head):
    """A header line ended by a bare LF, which this program's own `\r\n` split
    would leave inside another line -- so `GET /x HTTP/1.1\nAuthorization: ...`
    passes the DROP filter as one request line and reaches GitHub with the
    client's credential in it. GitHub's own parser would read it as two lines,
    which is the disagreement that makes it a smuggle."""
    return b"\n" in head.replace(b"\r\n", b"")


def read_token(path):
    """Read on every request, never cached: `wk push off` removes this file and
    the next request must go unauthenticated, not be served from something
    remembered."""
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return ""


# --- certificate material ----------------------------------------------------
# Made once and kept: the CA's public certificate is what every workspace is
# told to trust, so regenerating it on each start would break every workspace
# that had already read it.
#
# The leaf's private key can impersonate api.github.com to a client that trusts
# this CA -- which is precisely what this program does on purpose -- and it can
# do nothing else. It is not a credential that publishes, so unlike the token it
# lives on this machine permanently, in a directory no container mounts.

_CONF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = %(cn)s
[ca_ext]
basicConstraints = critical,CA:true
keyUsage = critical,keyCertSign,cRLSign
[leaf_ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:%(cn)s
"""


def _openssl(*args):
    subprocess.run(["openssl", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _conf(cn):
    f = tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False)
    f.write(_CONF % {"cn": cn})
    f.close()
    return f.name


def ensure_certs(d, ca_out):
    """CA, leaf and the published public certificate; returns the chain file.

    The chain is leaf-then-CA in one file: a client that trusts the CA still
    needs to be sent it, and sending it costs nothing.
    """
    # `mode=` applies only to a directory this call creates, so an existing one
    # keeps whatever mode it had -- and this directory holds the leaf key that
    # impersonates api.github.com to everything that trusts the CA.
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    ca_key = os.path.join(d, "ca.key")
    ca_crt = os.path.join(d, "ca.crt")
    leaf_key = os.path.join(d, "leaf.key")
    leaf_crt = os.path.join(d, "leaf.crt")
    chain = os.path.join(d, "chain.crt")

    if not (os.path.exists(ca_key) and os.path.exists(ca_crt)):
        cnf = _conf("wk github injector CA")
        try:
            _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
                     "-keyout", ca_key, "-out", ca_crt, "-days", "3650",
                     "-config", cnf, "-extensions", "ca_ext")
        finally:
            os.unlink(cnf)
        os.chmod(ca_key, 0o600)
        log("made a CA in %s" % d)

    if not (os.path.exists(leaf_key) and os.path.exists(leaf_crt)):
        cnf = _conf(INJECT_HOST)
        csr = os.path.join(d, "leaf.csr")
        try:
            _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes",
                     "-keyout", leaf_key, "-out", csr, "-config", cnf)
            _openssl("x509", "-req", "-in", csr, "-CA", ca_crt,
                     "-CAkey", ca_key, "-set_serial", "1", "-days", "3650",
                     "-out", leaf_crt, "-extfile", cnf,
                     "-extensions", "leaf_ext")
        finally:
            os.unlink(cnf)
            if os.path.exists(csr):
                os.unlink(csr)
        os.chmod(leaf_key, 0o600)
        log("made a leaf certificate for %s" % INJECT_HOST)

    with open(chain, "wb") as out:
        for part in (leaf_crt, ca_crt):
            with open(part, "rb") as f:
                out.write(f.read())

    # Published where a workspace can read it. 0644 and deliberately so: a
    # CA's certificate is public, and every workspace has to be able to read
    # this one or nothing in it can talk to the API at all.
    os.makedirs(os.path.dirname(ca_out), mode=0o700, exist_ok=True)
    tmp = ca_out + ".new"
    with open(ca_crt, "rb") as f:
        data = f.read()
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, ca_out)
    return chain


# --- the relay ---------------------------------------------------------------

async def pipe(reader, writer):
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class Injector:
    def __init__(self, pat_path, read_pat_path, client_ctx):
        self.pat_path = pat_path
        self.read_pat_path = read_pat_path
        self.client_ctx = client_ctx

    def token_for(self, reading):
        """A write spends the switch's token and nothing else: `wk push off`
        removes that file, and a write then goes on unauthenticated so GitHub
        answers 401 for itself. A read spends whichever token this machine has
        -- the standing one, or the switch's when there is no other -- because
        the switch is over writing."""
        if reading:
            return read_token(self.read_pat_path) or read_token(self.pat_path)
        return read_token(self.pat_path)

    async def refuse(self, cwriter, status, reason):
        cwriter.write(b"HTTP/1.1 " + status + b"\r\n"
                      b"Content-Type: text/plain\r\n"
                      b"Content-Length: " + str(len(reason)).encode("ascii") +
                      b"\r\nConnection: close\r\n\r\n" + reason)
        await cwriter.drain()

    async def handle(self, creader, cwriter):
        upstream = None
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await asyncio.wait_for(creader.read(4096), READ_TIMEOUT)
                if not chunk:
                    return
                head += chunk
                if len(head) > MAX_HEAD:
                    return
            head, _, body = head.partition(b"\r\n\r\n")

            if bare_lf(head):
                await self.refuse(
                    cwriter, b"400 Bad Request",
                    b"a header line ended by a bare LF is refused by the wk "
                    b"credential injector\r\n")
                return
            length, refusal = body_length(head)
            if refusal is not None:
                await self.refuse(cwriter, *refusal)
                return

            # Exactly the declared body and no byte more. What follows it on
            # this connection is a second request head, and the second head is
            # one nothing here rewrote -- so it is not relayed and the client
            # side is never piped upstream at all.
            body = body[:length]
            while len(body) < length:
                chunk = await asyncio.wait_for(
                    creader.read(length - len(body)), READ_TIMEOUT)
                if not chunk:
                    break
                body += chunk

            refusal = policy_refusal(head, body)
            if refusal is not None:
                log(" ".join(refusal[1].decode("latin-1", "replace").split()))
                await self.refuse(cwriter, *refusal)
                return

            method, target = request_line(head)
            reading = is_read(method, target, body)
            token = self.token_for(reading)
            new_head = rewrite_head(head, token, len(body))
            log("%s %s %s %s" % ("read" if reading else "write",
                                 "inject" if token else "unauthenticated",
                                 method, target[:200]))

            ureader, uwriter = await asyncio.open_connection(
                INJECT_HOST, INJECT_PORT, ssl=self.client_ctx,
                server_hostname=INJECT_HOST)
            upstream = uwriter
            uwriter.write(new_head + body)
            await uwriter.drain()

            await pipe(ureader, cwriter)
        except (asyncio.TimeoutError, ConnectionResetError, OSError) as exc:
            log("connection failed: %s" % exc)
        finally:
            for w in (cwriter, upstream):
                if w is not None:
                    try:
                        w.close()
                    except OSError:
                        pass


def _default_runtime():
    return os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


async def main():
    import ssl

    store = os.environ.get("WK_STORE", "/var/lib/wk")
    runtime = os.path.join(_default_runtime(), "wk")
    sock = os.environ.get("WK_INJECT_SOCK",
                          os.path.join(store, "github-inject.sock"))
    certs = os.environ.get("WK_INJECT_DIR",
                           os.path.join(store, "github-inject"))
    ca_out = os.environ.get("WK_INJECT_CA_OUT",
                            os.path.join(runtime, "wk-github-ca.pem"))
    pat = os.environ.get("WK_INJECT_PAT",
                         os.path.join(store, "push-github-pat"))
    read_pat = os.environ.get("WK_INJECT_READ_PAT",
                              os.path.join(store, "read-github-pat"))

    chain = ensure_certs(certs, ca_out)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(chain, os.path.join(certs, "leaf.key"))

    # The upstream leg verifies normally against the system trust store: this
    # program is the only thing that gets to see inside the workspace's TLS,
    # and it must not become a way to reach a forged api.github.com itself.
    client_ctx = ssl.create_default_context()

    injector = Injector(pat, read_pat, client_ctx)

    if os.path.exists(sock):
        os.unlink(sock)
    os.makedirs(os.path.dirname(sock), mode=0o700, exist_ok=True)
    server = await asyncio.start_unix_server(injector.handle, path=sock)
    # Only wk-proxy connects here, and it runs as this same user. 0600 keeps a
    # workspace from reaching the injector directly even if the socket were
    # ever placed somewhere a container could see.
    os.chmod(sock, 0o600)
    log("listening on %s for %s (write token: %s, read token: %s, CA published at %s)"
        % (sock, INJECT_HOST, pat, read_pat, ca_out))
    sd_notify("READY=1")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
