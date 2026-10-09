#!/usr/bin/env python3
"""Swap the placeholder credential a workspace holds for a real one, on the
hosts whose TLS ends here. api.github.com takes a token in the Authorization
header: a read spends the standing one, a write only on the writing socket. bugs.webkit.org
takes an api_key query parameter, the held key, on the writing socket alone. The proxy picks the
reading socket for a workspace an agent runs in, and a write there is refused here, naming that.
api.anthropic.com, claude.ai and platform.claude.com take the claude.ai login's
access token for the placeholder bearer, and refuse their OAuth endpoints; the rest
passes as sent. WK_INJECT_CLAUDE_LOGIN names the holder; the podman machine's relays to the Mac's."""

import asyncio
import errno
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib")]
import relay  # noqa: E402
from wk import claudelogin  # noqa: E402
from wk.machine import in_podman_machine, replace_file  # noqa: E402
from wk.notify import sd_notify  # noqa: E402

GITHUB = "api.github.com"
BUGZILLA = "bugs.webkit.org"
CLAUDE_HOSTS = ("api.anthropic.com", "claude.ai", "platform.claude.com")
HOSTS = (GITHUB, BUGZILLA) + CLAUDE_HOSTS
INJECT_PORT = 443

READ_TIMEOUT = 30
# Connect and TLS only: the status line waits READ_TIMEOUT, which stays under the `curl -m` of lib/wk/wall.py so a stalled upstream is answered 504 rather than read as a dead injector.
UPSTREAM_TIMEOUT = 12
FAULT_HEADER = b"X-Wk-Injector"
IDLE_TIMEOUT = 300
UNREACHABLE = (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN, errno.ETIMEDOUT)
MAX_HEAD = 65536

DROP_FROM_FORWARDED = ("authorization", "connection", "proxy-connection",
                       "keep-alive", "proxy-authorization", "host",
                       "transfer-encoding", "content-length",
                       "x-bugzilla-api-key", "x-bugzilla-login",
                       "x-bugzilla-password", "x-bugzilla-token")

# Every spelling Bugzilla's REST API takes a login in; a workspace's own never goes on, the held api_key does.
BUGZILLA_PARAMS = ("login", "password", "api_key", "token", "bugzilla_api_key",
                   "bugzilla_login", "bugzilla_password", "bugzilla_token")

READ_METHODS = ("GET", "HEAD")

# Not one of the far end's own codes: GitHub's 401 and Bugzilla's 410 mean a credential was refused, and this one was never sent.
WRITE_REFUSED_STATUS = b"412 Precondition Failed"
WRITE_REFUSED_REASON = (
    b"the wk credential injector has no credential to write with and did not "
    b"forward this request: an agent runs in this workspace, or no token is "
    b"stored ('wk key set github-pat'). End the session, or run the command "
    b"from a shell of your own (wk enter <workspace>) once none runs here.\r\n")

# GitHub's API is all behind /graphql: only the document says if a POST writes.
_MUTATION = re.compile(rb"mutation", re.IGNORECASE)


# The Claude CLI's own claude.ai login constants (measured in its binary, beside CLAUDEAI_SUCCESS_URL).
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
MARGIN_MS = 300000
REFRESH_TIMEOUT = 20
DEFAULT_EXPIRES_S = 3600
BACKOFF_S = 60
OAUTH_PATHS = ("/v1/oauth/token", "/oauth/authorize")
FORWARD_SOCK = "claude-inject.sock"


def log(msg):
    print("[wk-github-inject] %s" % msg, file=sys.stderr, flush=True)


class LoginError(Exception):
    pass


class Holder:
    def __init__(self, path, token_url=TOKEN_URL, clock=time.time):
        self.path = path
        self.token_url = token_url
        self.clock = clock
        self.failed = None

    def read(self):
        try:
            with open(self.path) as f:
                return claudelogin.parse(f.read())
        except FileNotFoundError:
            raise LoginError("this machine holds no claude.ai login (%s): 'wk key set claude-login'" % self.path)
        except ValueError as e:
            raise LoginError("the claude.ai login at %s is unusable (%s): 'wk key set claude-login --replace'" % (self.path, e))

    def fresh(self, oauth):
        return oauth["expiresAt"] - MARGIN_MS > self.clock() * 1000

    def access_token(self):
        """Re-read once the lock is held: a request that waited on it finds the refresh another made."""
        oauth = self.read()
        if self.fresh(oauth):
            return oauth["accessToken"]
        try:
            with claudelogin.locked(self.path):
                oauth = self.read()
                if not self.fresh(oauth):
                    oauth = self.refresh_once(oauth)
        except TimeoutError as e:
            raise LoginError("the claude.ai login was not refreshed: %s" % e)
        return oauth["accessToken"]

    def refresh_once(self, oauth):
        if self.failed and self.failed[0] == oauth["refreshToken"] and self.clock() < self.failed[1]:
            raise LoginError("%s (not asked again for %ds)" % (self.failed[2], self.failed[1] - self.clock()))
        try:
            return self.refresh(oauth)
        except LoginError as e:
            self.failed = (oauth["refreshToken"], self.clock() + BACKOFF_S, str(e))
            raise

    def refresh(self, oauth):
        body = json.dumps({"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
                           "client_id": CLIENT_ID}).encode()
        req = urllib.request.Request(self.token_url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "wk"})
        try:
            with urllib.request.urlopen(req, timeout=REFRESH_TIMEOUT) as r:
                got = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise LoginError("%s refused to refresh the claude.ai login (HTTP %d): it was revoked, or something else holds "
                                 "and spent it -- 'wk key set claude-login --replace'" % (self.token_url, e.code))
            raise LoginError("%s answered HTTP %d to a refresh; it is tried again" % (self.token_url, e.code))
        except (OSError, ValueError) as e:
            raise LoginError("could not refresh the claude.ai login at %s (%s); it is tried again" % (self.token_url, e))
        if not isinstance(got, dict) or not got.get("access_token"):
            raise LoginError("%s answered a refresh with no access_token; it is tried again" % self.token_url)
        oauth = dict(oauth, accessToken=got["access_token"], refreshToken=got.get("refresh_token") or oauth["refreshToken"],
                     expiresAt=int((self.clock() + float(got.get("expires_in") or DEFAULT_EXPIRES_S)) * 1000))
        if got.get("scope"):
            oauth["scopes"] = got["scope"].split()
        with open(self.path) as f:
            doc = json.load(f)
        doc["claudeAiOauth"] = oauth
        replace_file(self.path, json.dumps(doc) + "\n", mode=0o600)
        log("refreshed the claude.ai login in %s" % self.path)
        return oauth


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


def header(head, want):
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == want:
            return value.strip()
    return None


def host_of(head):
    host = (header(head, b"host") or b"").decode("latin-1", "replace").lower().rstrip(".")
    return host.rsplit(":", 1)[0] if ":" in host else host


def bugzilla_target(target, key):
    path, _, query = target.partition("?")
    kept = [p for p in query.split("&")
            if p and urllib.parse.unquote(p.split("=", 1)[0]).lower()
            not in BUGZILLA_PARAMS]
    if key:
        kept.append("api_key=" + urllib.parse.quote(key, safe=""))
    return path + ("?" + "&".join(kept) if kept else "")


def rewrite_head(head, host, token, length=0, upgrade=False):
    # Host and Content-Length are ours: a client's Host spends the token on
    # another name, and a length GitHub reads differently smuggles a second head.
    keep = ("authorization",) if host in CLAUDE_HOSTS and not token else ()
    lines = head.split(b"\r\n")
    request = lines[0]
    if host == BUGZILLA:
        parts = request.decode("latin-1", "replace").split(" ")
        if len(parts) != 3:
            return None
        parts[1] = bugzilla_target(parts[1], token)
        request = " ".join(parts).encode("latin-1")
    out = [request, b"Host: " + host.encode("latin-1")]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower().decode("latin-1", "replace")
        if name in DROP_FROM_FORWARDED and name not in keep:
            continue
        out.append(line)
    if token and (host == GITHUB or host in CLAUDE_HOSTS):
        out.append(b"Authorization: Bearer " + token.encode("latin-1"))
    out.append(b"Content-Length: %d" % length)
    out.append(b"Connection: Upgrade" if upgrade else b"Connection: close")
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
subjectAltName = %(san)s
"""


def _openssl(*args):
    subprocess.run(["openssl", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _conf(cn, names=(GITHUB,)):
    f = tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False)
    f.write(_CERT_CONF % {"cn": cn, "san": ", ".join("DNS:" + n for n in names)})
    f.close()
    return f.name


def leaf_names(leaf_crt):
    out = subprocess.run(["openssl", "x509", "-noout", "-text", "-in", leaf_crt],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True).stdout
    return set(re.findall(r"DNS:([^,\s]+)", out))


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

    # The leaf is remade when the set of names changes; the CA is what every workspace trusts and is kept.
    if os.path.exists(leaf_crt) and leaf_names(leaf_crt) != set(HOSTS):
        os.unlink(leaf_crt)
    if not (os.path.exists(leaf_key) and os.path.exists(leaf_crt)):
        cnf = _conf(GITHUB, HOSTS)
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
        log("made a leaf certificate for %s" % ", ".join(HOSTS))

    with open(chain, "wb") as out:
        for part in (leaf_crt, ca_crt):
            with open(part, "rb") as f:
                out.write(f.read())

    os.makedirs(os.path.dirname(ca_out), mode=0o700, exist_ok=True)
    with open(ca_crt, "rb") as f:
        replace_file(ca_out, f.read(), mode=0o644)
    return chain


pipe = functools.partial(relay.pipe, idle=IDLE_TIMEOUT)


class StatusTimeout(asyncio.TimeoutError):
    """The request was sent and the upstream sent no status line."""


NO_RELAY = (b"the wk credential injector put no claude.ai login on this request: the Mac's injector, its one holder, "
            b"does not answer at %s. On the Mac, 'launchctl print gui/$(id -u)/com.wk.inject' says whether launchd runs it: "
            b"if it does, inject.log in the wk state directory (~/.local/state/wk) says why it cannot publish here; if "
            b"not, './setup --stage inject' starts it\r\n")
NO_HOLDER = (b"the wk credential injector put no claude.ai login on this request: this machine's injector holds none "
             b"(no WK_INJECT_CLAUDE_LOGIN): './setup --stage inject' on a Mac, './setup --stage sdk' on Linux\r\n")
OAUTH_REFUSED = (b"the wk credential injector refuses the claude.ai OAuth endpoints: the login is its alone, and a "
                 b"workspace holds a placeholder that never needs refreshing ('wk key set claude-login' outside)\r\n")


class Forward:
    """Claude's hosts relayed whole to the injector that holds the login (a Mac's, from its podman machine)."""

    def __init__(self, path):
        self.path = path


class Injector:
    def __init__(self, pat_path, read_pat_path, bugzilla_key_path, claude, client_ctx, hosts=HOSTS, writes=True):
        self.writes = writes
        self.pat_path = pat_path
        self.read_pat_path = read_pat_path
        self.bugzilla_key_path = bugzilla_key_path
        self.claude = claude
        self.client_ctx = client_ctx
        self.hosts = hosts

    def token_for(self, host, reading):
        if host == BUGZILLA:
            return read_token(self.bugzilla_key_path) if self.writes else ""
        if reading:
            return read_token(self.read_pat_path) or read_token(self.pat_path)
        return read_token(self.pat_path) if self.writes else ""

    async def refuse(self, cwriter, status, reason, fault=b""):
        cwriter.write(b"HTTP/1.1 " + status + b"\r\n"
                      + (FAULT_HEADER + b": " + fault + b"\r\n" if fault else b"") +
                      b"Content-Type: text/plain\r\n"
                      b"Content-Length: " + str(len(reason)).encode("ascii") +
                      b"\r\nConnection: close\r\n\r\n" + reason)
        await cwriter.drain()

    async def exchange(self, host, request, opened):
        # The status line is read before the rest is piped back: without it an injected credential the far end refused and one it accepted are the same line in this log.
        ureader, uwriter = await asyncio.wait_for(asyncio.open_connection(
            host, INJECT_PORT, ssl=self.client_ctx, server_hostname=host), UPSTREAM_TIMEOUT)
        opened.append((ureader, uwriter))
        uwriter.write(request)
        await uwriter.drain()
        try:
            return await asyncio.wait_for(ureader.readline(), READ_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise StatusTimeout() from exc

    async def relay(self, request, creader, cwriter, host, method, target):
        try:
            freader, fwriter = await asyncio.open_unix_connection(self.claude.path)
        except OSError as e:
            log("%s %s %s refused: no holder at %s (%s)" % (host, method, target[:200], self.claude.path, e))
            await self.refuse(cwriter, b"401 Unauthorized", NO_RELAY % self.claude.path.encode())
            return
        log("%s login forward %s %s" % (host, method, target[:200]))
        fwriter.write(request)
        await asyncio.gather(pipe(freader, cwriter), pipe(creader, fwriter))

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

            body, early = body[:length], body[length:]
            while len(body) < length:
                chunk = await asyncio.wait_for(
                    creader.read(length - len(body)), READ_TIMEOUT)
                if not chunk:
                    break
                body += chunk

            host = host_of(head)
            if host not in self.hosts:
                await self.refuse(
                    cwriter, b"421 Misdirected Request",
                    b"the wk credential injector answers for " +
                    ", ".join(self.hosts).encode("latin-1") +
                    b", and this request's Host is none of them\r\n")
                return
            method, target = request_line(head)
            if host in CLAUDE_HOSTS and isinstance(self.claude, Forward):
                await self.relay(head + b"\r\n\r\n" + body + early, creader, cwriter, host, method, target)
                return
            if host in CLAUDE_HOSTS and target.split("?", 1)[0].rstrip("/") in OAUTH_PATHS:
                log("%s %s %s refused: an OAuth endpoint" % (host, method, target[:200]))
                await self.refuse(cwriter, b"403 Forbidden", OAUTH_REFUSED)
                return
            upgrade = host in CLAUDE_HOSTS and header(head, b"upgrade") is not None
            if host in CLAUDE_HOSTS:
                reading, token = True, ""
                if header(head, b"authorization") == b"Bearer " + claudelogin.PLACEHOLDER.encode():
                    if self.claude is None:
                        log("%s %s %s refused: no claude.ai login is held here" % (host, method, target[:200]))
                        await self.refuse(cwriter, b"401 Unauthorized", NO_HOLDER)
                        return
                    try:
                        token = await asyncio.to_thread(self.claude.access_token)
                    except LoginError as e:
                        log("%s %s %s refused: %s" % (host, method, target[:200], e))
                        await self.refuse(cwriter, b"401 Unauthorized",
                                          b"the wk credential injector put no claude.ai login on this request: "
                                          + str(e).encode("utf-8") + b"\r\n")
                        return
            else:
                reading = is_read(method, target, body)
                token = self.token_for(host, reading)
            new_head = rewrite_head(head, host, token, len(body), upgrade)
            if new_head is None:
                await self.refuse(
                    cwriter, b"400 Bad Request",
                    b"a request line the wk credential injector cannot read is "
                    b"refused for " + host.encode("latin-1") + b"\r\n")
                return
            # The client's target, never the rewritten one: that carries the key.
            log("%s %s %s %s %s" % (host, "login" if host in CLAUDE_HOSTS else "read" if reading else "write",
                                    "inject" if token else
                                    "as sent" if host in CLAUDE_HOSTS else
                                    "unauthenticated" if reading else
                                    "refused: no credential to write with",
                                    method, target[:200]))
            if not token and not reading:
                await self.refuse(cwriter, WRITE_REFUSED_STATUS, WRITE_REFUSED_REASON)
                return

            opened = []
            try:
                status = await self.exchange(host, new_head + body, opened)
            except asyncio.TimeoutError as exc:
                log("%s upstream failed: %s" % (host, type(exc).__name__))
                sent = isinstance(exc, StatusTimeout)
                await self.refuse(
                    cwriter, b"504 Gateway Timeout",
                    b"%s did not answer within %d seconds%s; the wk credential "
                    b"injector is up, the upstream is not\r\n"
                    % (host.encode("latin-1"),
                       READ_TIMEOUT if sent else UPSTREAM_TIMEOUT,
                       b" (the request was sent; it may have been applied)"
                       if sent else b""))
                return
            except OSError as exc:
                log("%s upstream failed: %s: %s" % (host, type(exc).__name__, exc))
                reached = isinstance(exc, ConnectionError) or exc.errno in UNREACHABLE
                await self.refuse(
                    cwriter, b"502 Bad Gateway",
                    (b"the wk credential injector could not reach " if reached else
                     b"the wk credential injector failed to verify or resolve ") +
                    host.encode("latin-1") + b" (" +
                    type(exc).__name__.encode("ascii") + b")\r\n",
                    b"" if reached else type(exc).__name__.encode("ascii"))
                return
            ureader, upstream = opened[0]
            log("%s %s %s -> %s" % (host, method, target[:120],
                                    status.decode("latin-1", "replace").strip()))
            cwriter.write(status)
            if upgrade and status.split(b" ")[1:2] == [b"101"]:
                upstream.write(early)
                await asyncio.gather(pipe(ureader, cwriter), pipe(creader, upstream))
            else:
                await pipe(ureader, cwriter)
        except (asyncio.TimeoutError, ConnectionResetError, OSError) as exc:
            log("connection failed: %s: %s" % (type(exc).__name__, exc))
        finally:
            relay.close(cwriter, upstream)


def _default_runtime(env=os.environ):
    return env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


def claude_source(env, in_machine):
    """The podman machine's injector relays to the Mac's and never holds; elsewhere only WK_INJECT_CLAUDE_LOGIN makes a holder."""
    login = env.get("WK_INJECT_CLAUDE_LOGIN")
    if in_machine:
        if login:
            sys.exit("[wk-github-inject] refused: WK_INJECT_CLAUDE_LOGIN=%s inside a podman machine, whose injector relays "
                     "Claude's hosts to the Mac's, the login's one holder" % login)
        return Forward(os.path.join(_default_runtime(env), FORWARD_SOCK))
    return Holder(login) if login else None


async def main():
    import ssl

    store = os.environ.get("WK_STORE", "/var/lib/wk")
    runtime = os.path.join(_default_runtime(), "wk")
    sock = os.environ.get("WK_INJECT_SOCK",
                          os.path.join(store, "github-inject.sock"))
    read_sock = os.environ.get("WK_INJECT_READ_SOCK",
                               os.path.join(store, "github-inject-read.sock"))
    certs = os.environ.get("WK_INJECT_DIR",
                           os.path.join(store, "github-inject"))
    ca_out = os.environ.get("WK_INJECT_CA_OUT",
                            os.path.join(runtime, "wk-github-ca.pem"))
    pat = os.environ.get("WK_INJECT_PAT",
                         os.path.join(store, "push-github-pat"))
    read_pat = os.environ.get("WK_INJECT_READ_PAT",
                              os.path.join(store, "read-github-pat"))
    bugzilla_key = os.environ.get("WK_INJECT_BUGZILLA_KEY",
                                  os.path.join(store, "push-bugzilla-api-key"))

    chain = ensure_certs(certs, ca_out)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(chain, os.path.join(certs, "leaf.key"))

    client_ctx = ssl.create_default_context()

    claude = claude_source(os.environ, in_podman_machine())
    injector = Injector(pat, read_pat, bugzilla_key, claude, client_ctx)

    reading = Injector(pat, read_pat, bugzilla_key, claude, client_ctx, writes=False)
    os.makedirs(os.path.dirname(sock), mode=0o700, exist_ok=True)
    tasks = []
    for path, who in ((sock, injector), (read_sock, reading)):
        if os.path.exists(path):
            os.unlink(path)
        tasks.append((await asyncio.start_unix_server(who.handle, path=path, ssl=server_ctx)).serve_forever())
        os.chmod(path, 0o600)
    log("listening on %s, and %s for a workspace an agent runs in, for %s (write token: %s, read token: %s, Bugzilla key: %s, "
        "claude.ai login: %s, CA published at %s)" % (sock, read_sock, ", ".join(HOSTS), pat, read_pat,
                                                      bugzilla_key, claude.path if claude else "none held", ca_out))
    machine = os.environ.get("WK_INJECT_PUBLISH_MACHINE")
    if machine:
        from wk import publish
        plain = os.environ["WK_INJECT_PLAIN_SOCK"]
        if os.path.exists(plain):
            os.unlink(plain)
        claude_only = Injector(pat, read_pat, bugzilla_key, claude, client_ctx, CLAUDE_HOSTS)
        tasks.append((await asyncio.start_unix_server(claude_only.handle, path=plain)).serve_forever())
        os.chmod(plain, 0o600)
        tasks.append(publish.publish(machine, plain, FORWARD_SOCK, log, "inject"))
    sd_notify("READY=1")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
