#!/usr/bin/env python3
"""Swap the placeholder Authorization a workspace holds for a real token: a read
spends the standing one, a write only `wk push on`'s. See `wk help push`."""

import asyncio
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from wknotify import sd_notify  # noqa: E402

INJECT_HOST = "api.github.com"
INJECT_PORT = 443

READ_TIMEOUT = 30
IDLE_TIMEOUT = 300
MAX_HEAD = 65536

DROP_FROM_FORWARDED = ("authorization", "connection", "proxy-connection",
                       "keep-alive", "proxy-authorization", "host",
                       "transfer-encoding", "content-length")

READ_METHODS = ("GET", "HEAD")

# GitHub's API is all behind /graphql: only the document says if a POST writes.
_MUTATION = re.compile(rb"mutation", re.IGNORECASE)


def log(msg):
    print("[wk-github-inject] %s" % msg, file=sys.stderr, flush=True)


def request_line(head):
    parts = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").split(" ")
    if len(parts) != 3 or not parts[1].startswith("/"):
        return "", ""
    return parts[0], parts[1]


def is_read(method, target, body):
    if method in READ_METHODS:
        return True
    return (method == "POST"
            and target.split("?", 1)[0] == "/graphql"
            and not _MUTATION.search(body))


def rewrite_head(head, token, length=0):
    # Host and Content-Length are ours: a client's Host spends the token on
    # another name, and a length GitHub reads differently smuggles a second head.
    lines = head.split(b"\r\n")
    out = [lines[0], b"Host: " + INJECT_HOST.encode("latin-1")]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name.decode("latin-1", "replace") in DROP_FROM_FORWARDED:
            continue
        out.append(line)
    if token:
        out.append(b"Authorization: Bearer " + token.encode("latin-1"))
    out.append(b"Content-Length: %d" % length)
    out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


def body_length(head):
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
    # GitHub's parser splits on a bare LF where the `\r\n` splits here do not.
    return b"\n" in head.replace(b"\r\n", b"")


def read_token(path):
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return ""


# openssl CLI and stdlib `ssl`, not mitmproxy or `cryptography`: this also runs
# on the macOS host, whose only guaranteed python3 is 3.9.6 with neither.
_CERT_CONF = """[req]
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
    f.write(_CERT_CONF % {"cn": cn})
    f.close()
    return f.name


def ensure_certs(d, ca_out):
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

    os.makedirs(os.path.dirname(ca_out), mode=0o700, exist_ok=True)
    tmp = ca_out + ".new"
    with open(ca_crt, "rb") as f:
        data = f.read()
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, ca_out)
    return chain


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

    client_ctx = ssl.create_default_context()

    injector = Injector(pat, read_pat, client_ctx)

    if os.path.exists(sock):
        os.unlink(sock)
    os.makedirs(os.path.dirname(sock), mode=0o700, exist_ok=True)
    server = await asyncio.start_unix_server(injector.handle, path=sock,
                                             ssl=server_ctx)
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
