"""The injector: the forward proxy on the credential server that holds the one claude.ai login.

    python3 -I injector.py <directory> <port>

It listens on this machine's tailnet address. `CONNECT` to one of Claude's three hosts ends TLS here, with a leaf of
the CA in <directory>, and each request's placeholder bearer becomes the real access token; any other host is
tunnelled untouched. A plain request is answered with the injector's own state, which is what `wk doctor` reads.

<directory> holds ca.key, ca.pem (what every agent session trusts), the leaf, and login/.credentials.json, which
this process alone refreshes. The file imports nothing of wk's: the playbook's injector role copies it there alone."""

import errno
import fcntl
import json
import os
import select
import socket
import socketserver
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOSTS = ("api.anthropic.com", "claude.ai", "platform.claude.com")
PLACEHOLDER = "wk-injects-this"
SCOPES = ("user:inference", "user:profile", "user:sessions:claude_code")
NEVER_MS = 4102444800000

# The Claude CLI's own claude.ai login constants, measured in its binary.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_PATHS = ("/v1/oauth/token", "/oauth/authorize")
MARGIN_MS = 300000
REFRESH_TIMEOUT = 20
DEFAULT_EXPIRES_S = 3600
BACKOFF_S = 60

READ_TIMEOUT = 30
UPSTREAM_TIMEOUT = 12
IDLE_TIMEOUT = 300
MAX_HEAD = 65536
FAULT_HEADER = b"X-Wk-Injector"
BANNER = b"wk injector"
UNREACHABLE = (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN, errno.ETIMEDOUT)
DROPPED = ("authorization", "connection", "proxy-connection", "keep-alive", "proxy-authorization", "host",
           "transfer-encoding", "content-length")


def log(message):
    sys.stderr.write("wk-injector: %s\n" % message)
    sys.stderr.flush()


class LoginError(Exception):
    pass


def placeholder():
    """The login every server holds: it never expires, so no client asks the token endpoint."""
    return json.dumps({"claudeAiOauth": {"accessToken": PLACEHOLDER, "refreshToken": PLACEHOLDER,
                                         "expiresAt": NEVER_MS, "scopes": list(SCOPES)}}) + "\n"


def parse(text):
    """The claudeAiOauth object of a login document; ValueError says what is wrong with it."""
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError("it is not JSON (%s)" % e)
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        raise ValueError("it has no claudeAiOauth object, which the Claude CLI's /login writes")
    for key in ("accessToken", "refreshToken"):
        if not isinstance(oauth.get(key), str) or not oauth[key]:
            raise ValueError("its claudeAiOauth has no %s" % key)
    if PLACEHOLDER in (oauth["accessToken"], oauth["refreshToken"]):
        raise ValueError("it is a server's placeholder, not a login")
    if not isinstance(oauth.get("expiresAt"), (int, float)):
        raise ValueError("its claudeAiOauth has no expiresAt")
    return oauth


def fresh(oauth, now):
    return oauth["expiresAt"] - MARGIN_MS > now * 1000


def refresh_body(oauth):
    return json.dumps({"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"], "client_id": CLIENT_ID}).encode()


def refreshed(oauth, got, now):
    """The login after the token endpoint answered `got`."""
    if not isinstance(got, dict) or not got.get("access_token"):
        raise LoginError("%s answered a refresh with no access_token; it is tried again" % TOKEN_URL)
    new = dict(oauth, accessToken=got["access_token"], refreshToken=got.get("refresh_token") or oauth["refreshToken"],
               expiresAt=int((now + float(got.get("expires_in") or DEFAULT_EXPIRES_S)) * 1000))
    if got.get("scope"):
        new["scopes"] = got["scope"].split()
    return new


def refusal(body):
    """The token endpoint's own words for refusing: the code and description of an OAuth error body."""
    try:
        doc = json.loads(body)
    except ValueError:
        return ""
    return "".join(", %s" % doc[key][:100] for key in ("error", "error_description") if isinstance(doc, dict) and isinstance(doc.get(key), str))


def ask_token_endpoint(body, urlopen=urllib.request.urlopen):
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={"Content-Type": "application/json", "User-Agent": "wk"})
    try:
        with urlopen(request, timeout=REFRESH_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise LoginError("%s refused to refresh the claude.ai login (HTTP %d%s): it expired, was revoked, or something else "
                             "holds and spent it -- log in to Claude again, `wk key set claude-login`, then `wk machine setup` this "
                             "machine" % (TOKEN_URL, e.code, refusal(e.read())))
        raise LoginError("%s answered HTTP %d to a refresh; it is tried again" % (TOKEN_URL, e.code))
    except (OSError, ValueError) as e:
        raise LoginError("could not refresh the claude.ai login at %s (%s); it is tried again" % (TOKEN_URL, e))


class Holder:
    """The login file and its one refresher. A refresh spends the refresh token, so it happens under a lock, once."""

    def __init__(self, path, clock=time.time, ask=ask_token_endpoint):
        self.path, self.clock, self.ask, self.failed = path, clock, ask, None

    def read(self):
        try:
            with open(self.path) as f:
                return parse(f.read())
        except FileNotFoundError:
            raise LoginError("this machine holds no claude.ai login: `wk key set claude-login`, then `wk machine setup` this machine")
        except ValueError as e:
            raise LoginError("the claude.ai login here is unusable (%s): `wk key set claude-login`, then `wk machine setup` this machine" % e)

    def access_token(self):
        oauth = self.read()
        if fresh(oauth, self.clock()):
            return oauth["accessToken"]
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            oauth = self.read()     # a request that waited on the lock finds the refresh another made
            if not fresh(oauth, self.clock()):
                oauth = self.refresh(oauth)
        return oauth["accessToken"]

    def refresh(self, oauth):
        if self.failed and self.failed[0] == oauth["refreshToken"] and self.clock() < self.failed[1]:
            raise LoginError("%s (not asked again for %ds)" % (self.failed[2], self.failed[1] - self.clock()))
        try:
            oauth = refreshed(oauth, self.ask(refresh_body(oauth)), self.clock())
        except LoginError as e:
            self.failed = (oauth["refreshToken"], self.clock() + BACKOFF_S, str(e))
            raise
        with open(self.path) as f:
            doc = json.load(f)
        doc["claudeAiOauth"] = oauth
        with open(os.open(self.path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json.dumps(doc) + "\n")
        os.replace(self.path + ".tmp", self.path)
        log("refreshed the claude.ai login")
        return oauth

    def state(self):
        try:
            self.read()
        except LoginError as e:
            return str(e)
        return "login held"


def request_line(head):
    """(method, target) of a request head, or ("", "") when its first line is not three words."""
    parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
    return (parts[0], parts[1]) if len(parts) == 3 else ("", "")


def connect_host(target):
    """("host", port) of a CONNECT target."""
    host, _, port = target.rpartition(":")
    return host.lower().rstrip("."), int(port) if port.isdigit() else 0


def header(head, want):
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == want:
            return value.strip()
    return None


def bare_lf(head):
    # The far end's parser splits on a bare LF where the `\r\n` splits here do not.
    return b"\n" in head.replace(b"\r\n", b"")


def body_length(head):
    """(the body's length, None), or (None, (status, reason)) for a body whose length the far end could read differently."""
    if header(head, b"transfer-encoding") is not None:
        return None, (b"411 Length Required", b"a chunked request body is refused by the wk injector; send Content-Length\r\n")
    lengths = [line.split(b":", 1)[1].strip() for line in head.split(b"\r\n")[1:] if line.split(b":", 1)[0].strip().lower() == b"content-length"]
    if not lengths:
        return 0, None
    if len(lengths) > 1 or not lengths[0].isdigit():
        return None, (b"400 Bad Request", b"a request with no single Content-Length is refused by the wk injector\r\n")
    return int(lengths[0]), None


def is_oauth(target):
    return target.split("?", 1)[0].rstrip("/") in OAUTH_PATHS


def wants_token(head):
    return header(head, b"authorization") == b"Bearer " + PLACEHOLDER.encode()


def rewrite_head(head, host, token, length, upgrade):
    """The head sent upstream. Host and Content-Length are written here: a client's Host spends the token on another
    name, and a length the far end reads differently smuggles a second head. Without `token` the request's own
    Authorization passes as sent."""
    lines = head.split(b"\r\n")
    out = [lines[0], b"Host: " + host.encode("latin-1")]
    for line in lines[1:]:
        name = line.split(b":", 1)[0].strip().lower().decode("latin-1")
        if name not in DROPPED or (name == "authorization" and not token):
            out.append(line)
    if token:
        out.append(b"Authorization: Bearer " + token.encode("latin-1"))
    out.append(b"Content-Length: %d" % length)
    out.append(b"Connection: Upgrade" if upgrade else b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


def answer(status, reason, fault=b""):
    return (b"HTTP/1.1 " + status + b"\r\n" + (FAULT_HEADER + b": " + fault + b"\r\n" if fault else b"")
            + b"Content-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(reason) + reason)


def upstream_fault(host, error):
    """The answer for an upstream that could not be opened. A fault of this machine's own TLS or DNS carries the header."""
    name = type(error).__name__.encode()
    if isinstance(error, socket.timeout):
        return answer(b"504 Gateway Timeout", b"%s did not answer within %d seconds; the wk injector is up, the upstream is not\r\n"
                      % (host.encode(), UPSTREAM_TIMEOUT))
    if isinstance(error, ConnectionError) or error.errno in UNREACHABLE:
        return answer(b"502 Bad Gateway", b"the wk injector could not reach %s (%s)\r\n" % (host.encode(), name))
    return answer(b"502 Bad Gateway", b"the wk injector failed to verify or resolve %s (%s)\r\n" % (host.encode(), name), name)


def read_until(sock, mark, buffered=b""):
    """(the bytes up to `mark`, the bytes read past it); (None, b"") when the far end closes first or sends too much."""
    while mark not in buffered:
        chunk = sock.recv(4096)
        if not chunk or len(buffered) > MAX_HEAD:
            return None, b""
        buffered += chunk
    before, _, after = buffered.partition(mark)
    return before, after


def pipe(a, b):
    """Bytes both ways until either side ends or neither speaks for IDLE_TIMEOUT seconds."""
    other = {a: b, b: a}
    while True:
        # A TLS socket may hold decrypted bytes select cannot see.
        ready = [s for s in other if getattr(s, "pending", int)()] or select.select(list(other), [], [], IDLE_TIMEOUT)[0]
        if not ready:
            return
        for s in ready:
            data = s.recv(65536)
            if not data:
                return
            other[s].sendall(data)


# The key identifiers are stated: a strict verifier (Python 3.13 on) refuses a leaf without an authority key
# identifier, and OpenSSL 4 no longer adds one by itself.
CERT_CONF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = %s
[ca_ext]
basicConstraints = critical,CA:true
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[leaf_ext]
basicConstraints = critical,CA:false
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = %s
"""


def ensure_certs(directory):
    """Make the CA and the leaf where there is none; the CA is what every server trusts, so it is never remade."""
    def openssl(*args):
        subprocess.run(("openssl",) + args, check=True, cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    made = set(os.listdir(directory))
    with open(os.open(os.path.join(directory, "openssl.cnf"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(CERT_CONF % (HOSTS[0], ", ".join("DNS:" + h for h in HOSTS)))
    if not {"ca.key", "ca.pem"} <= made:
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "3650",
                "-config", "openssl.cnf", "-extensions", "ca_ext", "-subj", "/CN=wk injector CA")
        log("made a CA in %s" % directory)
    if not {"leaf.key", "chain.pem"} <= made:
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-config", "openssl.cnf")
        openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-set_serial", "1", "-days", "3650",
                "-out", "leaf.pem", "-extfile", "openssl.cnf", "-extensions", "leaf_ext")
        with open(os.path.join(directory, "leaf.pem")) as leaf, open(os.path.join(directory, "ca.pem")) as ca:
            chain = leaf.read() + ca.read()
        with open(os.path.join(directory, "chain.pem"), "w") as f:
            f.write(chain)
    for key in ("ca.key", "leaf.key"):
        os.chmod(os.path.join(directory, key), 0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(os.path.join(directory, "chain.pem"), os.path.join(directory, "leaf.key"))
    return context


def dial(host, port):
    return socket.create_connection((host, port), UPSTREAM_TIMEOUT)


class Injector(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, directory, holder, trust, dial=dial):
        """`trust` is the SSLContext that verifies Claude's hosts; `dial(host, port)` opens every outgoing connection."""
        self.tls, self.holder, self.trust, self.dial = ensure_certs(directory), holder, trust, dial
        super().__init__(address, Connection)


class Connection(socketserver.BaseRequestHandler):
    def handle(self):
        opened = [self.request]
        try:
            self.proxy(self.request, opened)
        except OSError as e:
            log("connection failed: %s: %s" % (type(e).__name__, e))
        finally:
            for sock in opened:
                sock.close()

    def proxy(self, client, opened):
        client.settimeout(READ_TIMEOUT)
        head, rest = read_until(client, b"\r\n\r\n")
        if head is None:
            return
        method, target = request_line(head)
        if method != "CONNECT":
            return client.sendall(answer(b"400 Bad Request", BANNER + b": a forward proxy, CONNECT only\r\n"
                                         + self.server.holder.state().encode() + b"\r\n"))
        host, port = connect_host(target)
        if host in HOSTS:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            opened.append(self.server.tls.wrap_socket(client, server_side=True))
            return self.claude(opened[-1], host, opened)
        try:
            opened.append(self.server.dial(host, port))
        except OSError as e:
            return client.sendall(upstream_fault(host, e))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        opened[-1].sendall(rest)
        pipe(client, opened[-1])

    def claude(self, client, host, opened):
        head, body = read_until(client, b"\r\n\r\n")
        if head is None:
            return
        if bare_lf(head):
            return client.sendall(answer(b"400 Bad Request", b"a header line ended by a bare LF is refused by the wk injector\r\n"))
        length, refusal = body_length(head)
        if refusal:
            return client.sendall(answer(*refusal))
        while len(body) < length:
            chunk = client.recv(length - len(body))
            if not chunk:
                return
            body += chunk
        body, early = body[:length], body[length:]
        method, target = request_line(head)
        said = "%s %s %s" % (host, method, target.split("?", 1)[0][:200])
        if is_oauth(target):
            log("%s refused: an OAuth endpoint" % said)
            return client.sendall(answer(b"403 Forbidden", b"the wk injector refuses the claude.ai OAuth endpoints: the login is its "
                                         b"alone, and a server holds a placeholder that never needs refreshing\r\n"))
        token = ""
        if wants_token(head):
            try:
                token = self.server.holder.access_token()
            except LoginError as e:
                log("%s refused: %s" % (said, e))
                return client.sendall(answer(b"401 Unauthorized", b"the wk injector put no claude.ai login on this request: %s\r\n" % str(e).encode()))
        upgrade = header(head, b"upgrade") is not None
        try:
            opened.append(self.server.dial(host, 443))
            opened.append(self.server.trust.wrap_socket(opened[-1], server_hostname=host))
        except OSError as e:
            log("%s upstream failed: %s: %s" % (said, type(e).__name__, e))
            return client.sendall(upstream_fault(host, e))
        upstream = opened[-1]
        upstream.settimeout(READ_TIMEOUT)
        upstream.sendall(rewrite_head(head, host, token, len(body), upgrade) + body)
        try:
            status, rest = read_until(upstream, b"\n")
        except socket.timeout:
            log("%s: no status line" % said)
            return client.sendall(answer(b"504 Gateway Timeout", b"%s did not answer within %d seconds (the request was sent; it may have "
                                         b"been applied); the wk injector is up, the upstream is not\r\n" % (host.encode(), READ_TIMEOUT)))
        if status is None:
            return
        log("%s %s -> %s" % (said, "with the login" if token else "as sent", status.decode("latin-1").strip()))
        client.sendall(status + b"\n" + rest)
        if upgrade and status.split(b" ")[1:2] == [b"101"]:
            upstream.sendall(early)
        pipe(client, upstream)


def main(argv):
    directory, port = argv
    address = subprocess.run(["tailscale", "ip", "-4"], check=True, stdout=subprocess.PIPE).stdout.decode().split()[0]
    holder = Holder(os.path.join(directory, "login", ".credentials.json"))
    with Injector((address, int(port)), directory, holder, ssl.create_default_context()) as server:
        log("listening on %s:%s for %s; every other host is tunnelled" % (address, port, ", ".join(HOSTS)))
        server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
