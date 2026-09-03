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
    WK_INJECT_PAT     the token file `wk push on|off` writes and removes

With no token file the request goes on unauthenticated, so GitHub answers for
itself: 200 for a public endpoint, 401 for one that needs an account. The
switch withholds a credential; it does not pretend the API is unreachable.
"""

import asyncio
import os
import subprocess
import sys
import tempfile

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


def rewrite_head(head, token):
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
    os.makedirs(d, mode=0o700, exist_ok=True)
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
    def __init__(self, pat_path, client_ctx):
        self.pat_path = pat_path
        self.client_ctx = client_ctx

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

            token = read_token(self.pat_path)
            new_head = rewrite_head(head, token)
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            log("%s %s" % ("inject" if token else "unauthenticated", first))

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

    chain = ensure_certs(certs, ca_out)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(chain, os.path.join(certs, "leaf.key"))

    # The upstream leg verifies normally against the system trust store: this
    # program is the only thing that gets to see inside the workspace's TLS,
    # and it must not become a way to reach a forged api.github.com itself.
    client_ctx = ssl.create_default_context()

    injector = Injector(pat, client_ctx)

    if os.path.exists(sock):
        os.unlink(sock)
    os.makedirs(os.path.dirname(sock), mode=0o700, exist_ok=True)
    server = await asyncio.start_unix_server(injector.handle, path=sock)
    # Only wk-proxy connects here, and it runs as this same user. 0600 keeps a
    # workspace from reaching the injector directly even if the socket were
    # ever placed somewhere a container could see.
    os.chmod(sock, 0o600)
    log("listening on %s for %s (token: %s, CA published at %s)"
        % (sock, INJECT_HOST, pat, ca_out))

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
