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

Which token a request spends is the whole policy. A read -- GET, HEAD, and a
POST /graphql document with no mutation in it -- is authenticated from the
standing token whatever position the switch is in: an agent in a workspace has
to read a pull request, EWS statuses and the issue a branch tracks, `gh` has to
work, and no read changes anything of the person's. Everything else is a write,
authenticated from the switch's token alone: `wk push on` means a person is
watching what the workspace does, so it is not this program's place to decide
which of their own endpoints they may reach.

Every request is forwarded and this program refuses none of them on policy. A
write with no token goes on unauthenticated and GitHub answers 401 for itself:
the switch withholds a credential, it does not pretend the API is unreachable.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile

# systemd starts this by absolute path, so lib/ is derived from this file's own.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from wknotify import sd_notify  # noqa: E402

# Not a list, for a reason: every name terminated here is a name whose traffic
# something reads, so widening this is a decision and not a configuration change.
INJECT_HOST = "api.github.com"
INJECT_PORT = 443

READ_TIMEOUT = 30
IDLE_TIMEOUT = 300
MAX_HEAD = 65536          # a request head larger than this is not a request

# Dropped from every request before it is forwarded: Authorization because
# replacing it is the point, Host and the framing headers because rewrite_head
# states them itself, and the connection ones because this handles one request
# per TLS connection and so parses no response header at all.
DROP = ("authorization", "connection", "proxy-connection", "keep-alive",
        "proxy-authorization", "host", "transfer-encoding", "content-length")

READ_METHODS = ("GET", "HEAD")

# The grammar has no other spelling of a mutation, and the shorthand `{...}` is
# a query. Case-insensitive, which calls more documents writes than GitHub would
# treat as mutations and never fewer.
_MUTATION = re.compile(rb"mutation", re.IGNORECASE)


def log(msg):
    print("[wk-github-inject] %s" % msg, file=sys.stderr, flush=True)


def request_line(head):
    """(method, target) as the workspace sent them, undecoded, or ("", "") for
    a line this cannot read -- which is therefore not a read, spends the
    switch's token and nothing else, and goes on to GitHub exactly as it
    arrived to be answered there."""
    parts = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").split(" ")
    if len(parts) != 3 or not parts[1].startswith("/"):
        return "", ""
    return parts[0], parts[1]


def is_read(method, target, body):
    """Whether this request can only read.

    GET and HEAD by definition. GraphQL is the whole API behind one path, so
    the method cannot decide it there: a POST /graphql document with no
    mutation in it is a read like any other, which is what keeps
    `git-webkit`'s pull request lookup working with the switch off.
    """
    if method in READ_METHODS:
        return True
    return (method == "POST"
            and target.split("?", 1)[0] == "/graphql"
            and not _MUTATION.search(body))


def rewrite_head(head, token, length=0):
    """The whole of the injection: one request head in, one out.

    Whatever the workspace sent as Authorization is dropped -- it is a
    placeholder, and forwarding it would only ever be a way to smuggle a
    credential out -- and the real token is added when there is one. With no
    token the request goes on *unauthenticated* rather than being refused:
    GitHub answers for itself, 200 for a public endpoint and 401 for one that
    needs an account, and that is what `wk verify` measures either side of the
    switch.

    Host and Content-Length are this program's own. A client-chosen Host is a
    way to spend the token against another name behind GitHub's front end, and
    `length` is the number of body bytes actually relayed, so GitHub and this
    program cannot disagree about where the request ends -- the disagreement a
    smuggled second request needs.
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
    """How many body bytes this request declares, as (length, None), or
    (None, (status line, reason)).

    Everything after that many bytes is a *second* request on the same
    connection, whose head is one nothing here rewrote -- so it would carry the
    client's own Authorization straight to GitHub. A chunked body would need
    this program to parse the framing to know where the request ends, and a
    framing it does not agree with GitHub about is exactly how that second
    request is smuggled; the clients in a workspace (`git-webkit pr`, curl,
    requests) all send a length.
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
# told to trust, so remaking it would break every workspace that had read it.
# The leaf key impersonates api.github.com to a client that trusts this CA and
# can do nothing else, so unlike the token it lives here permanently, in a
# directory no container mounts.

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
    """CA, leaf and the published public certificate; returns the chain file,
    which is leaf-then-CA: a client that trusts the CA still needs to be sent
    it, and sending it costs nothing."""
    # `mode=` applies only to a directory this call creates, and this one holds
    # the leaf key.
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

    # 0644: a CA certificate is public, and every workspace has to read this one.
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

            # Exactly the declared body and no byte more: what follows it is a
            # second request head, one nothing here rewrote.
            body = body[:length]
            while len(body) < length:
                chunk = await asyncio.wait_for(
                    creader.read(length - len(body)), READ_TIMEOUT)
                if not chunk:
                    break
                body += chunk

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

    # Verified against the system trust store: this program must not become a
    # way to reach a forged api.github.com itself.
    client_ctx = ssl.create_default_context()

    injector = Injector(pat, read_pat, client_ctx)

    if os.path.exists(sock):
        os.unlink(sock)
    os.makedirs(os.path.dirname(sock), mode=0o700, exist_ok=True)
    server = await asyncio.start_unix_server(injector.handle, path=sock,
                                             ssl=server_ctx)
    # Only wk-proxy connects here, as this same user; 0600 keeps a workspace
    # from reaching the injector directly wherever the socket is placed.
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
