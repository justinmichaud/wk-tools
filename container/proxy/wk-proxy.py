#!/usr/bin/env python3
"""The workspace egress boundary.

A workspace runs with --network none: its network namespace has a loopback
interface and nothing else -- no route, no address, no DNS. It reaches the
outside world through exactly one channel, the unix socket this program
listens on, and this program decides what is allowed.

Why this rather than the nftables policy the macOS side uses:

  Rootless podman has no filterable forward path. Its network helper
  (slirp4netns, or pasta on podman 5) terminates container traffic and
  re-emits it as ordinary sockets from a cgroup scope named
  `rootless-netns-<random>.scope`, so there is no stable selector for a
  packet filter to match on -- not an interface, not a uid, not a cgroup
  path. Making nftables work therefore meant running podman as root, and
  root in the daily path is a much worse trade than this proxy: a workspace
  that escapes its container escapes as root.

  With no interface at all there is nothing to filter and nothing to bypass.
  The boundary is structural rather than enforced, and it needs no privilege:
  this runs as an ordinary systemd --user service.

The policy is by hostname, which is both tighter and far less work than the
CIDR lists it replaces -- GitHub's, Fastly's (PyPI) and Anthropic's published
ranges all had to be refreshed by hand, and `resolved_hosts` existed only
because some names have no stable range at all.

TLS is tunnelled, never terminated: CONNECT gets a byte pipe and this process
sees only the hostname the client asked for. There is no interception and no
certificate to trust.
"""

import asyncio
import ipaddress
import os
import socket
import sys
import time

# --- policy ------------------------------------------------------------------
# Suffix matches: "github.com" matches github.com and api.github.com, but not
# evilgithub.com -- the match is on a dot boundary.
ALLOWED_HOSTS = {
    # Anthropic: the API, the console, and the CLI's own installer.
    "anthropic.com": (80, 443),
    "claude.ai": (80, 443),
    "claude.com": (80, 443),
    # GitHub: clones and fetches over HTTPS, pushes to the fork over SSH, and
    # the raw/codeload hosts that benchmark payloads come from.
    "github.com": (22, 80, 443),
    "githubusercontent.com": (80, 443),
    "githubassets.com": (443),
    # PyPI. build-webkit autoinstalls setuptools on every clean build, so this
    # is a build dependency rather than an escape hatch.
    "pypi.org": (443,),
    "pythonhosted.org": (443,),
}

# Addresses that are never permitted as a *destination*, whatever resolved to
# them. Without this an allowlisted name whose DNS is wrong -- or hostile --
# becomes a route onto the LAN, which is precisely what the boundary exists to
# prevent. The workstation is an unrestricted tailnet node, so this matters more
# here than it would on an isolated machine.
BLOCKED_NETS = [
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "224.0.0.0/4",
        "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
    )
]

MAX_CONNECTIONS = 64
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 900          # a git clone of WebKit is slow, but not this slow
DENY_LOG_INTERVAL = 60      # seconds between repeats of the same denial


def log(msg):
    print(f"[wk-proxy] {msg}", file=sys.stderr, flush=True)


class Policy:
    """Hostname and address policy, reloaded from the store on every request.

    Reloading rather than caching is deliberate: `wk pi setup` appends to
    pi-hosts and the change must take effect without restarting the boundary,
    and the file is tiny.
    """

    def __init__(self, store):
        self.store = store
        self._extra_mtime = None
        self._pi = set()
        self._pi_mtime = None

    def _pi_hosts(self):
        path = os.path.join(self.store, "pi-hosts")
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return set()
        if mtime != self._pi_mtime:
            with open(path) as f:
                self._pi = {
                    line.strip() for line in f
                    if line.strip() and not line.startswith("#")
                }
            self._pi_mtime = mtime
            if self._pi:
                log(f"pi allowlist: {', '.join(sorted(self._pi))}")
        return self._pi

    def host_allowed(self, host, port):
        """Allowlisted name, or a test device by address.

        Test devices are individual tailnet addresses, never the whole
        100.64.0.0/10 range: this workstation is itself an unrestricted tailnet
        node, so allowing the range would hand a workspace every machine the
        workstation can reach.
        """
        host = host.lower().rstrip(".")

        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None

        if addr is not None:
            return (host in self._pi_hosts() and port == 22), "pi test device"

        for suffix, ports in ALLOWED_HOSTS.items():
            if host == suffix or host.endswith("." + suffix):
                if port in (ports if isinstance(ports, tuple) else (ports,)):
                    return True, suffix
                return False, f"port {port} not allowed for {suffix}"
        return False, "not in the allowlist"

    def address_allowed(self, addr):
        ip = ipaddress.ip_address(addr)
        for net in BLOCKED_NETS:
            if ip.version == net.version and ip in net:
                return False, f"{addr} is in {net}"
        return True, ""


class Proxy:
    def __init__(self, policy):
        self.policy = policy
        self.active = 0
        self._denials = {}

    def deny(self, host, port, why):
        """Rate-limited, because a retry loop must not fill the journal -- and
        must not hide the first occurrence either."""
        key = (host, port, why)
        now = time.time()
        last = self._denials.get(key, 0)
        if now - last > DENY_LOG_INTERVAL:
            self._denials[key] = now
            log(f"DENY {host}:{port} -- {why}")

    async def open_upstream(self, host, port):
        """Resolve, check every resolved address, then connect.

        Checking after resolution rather than before is the point: the
        allowlist is by name, but the danger is by address, and one name can
        resolve to many.
        """
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)

        last_error = None
        for family, socktype, proto, _canon, sockaddr in infos:
            addr = sockaddr[0]
            ok, why = self.policy.address_allowed(addr)
            if not ok:
                self.deny(host, port, f"resolved to a blocked address: {why}")
                last_error = PermissionError(why)
                continue
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(addr, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
        raise last_error or OSError("no usable address")

    async def pipe(self, reader, writer):
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

    async def handle(self, creader, cwriter):
        if self.active >= MAX_CONNECTIONS:
            cwriter.write(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
            await cwriter.drain()
            cwriter.close()
            return

        self.active += 1
        upstream = None
        try:
            request = await asyncio.wait_for(creader.readline(), 30)
            if not request:
                return
            parts = request.decode("latin-1").split()
            if len(parts) < 3:
                cwriter.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await cwriter.drain()
                return

            method, target = parts[0].upper(), parts[1]

            if method == "CONNECT":
                host, _, port_s = target.rpartition(":")
                port = int(port_s or 443)
                headers = b""
                # Drain the rest of the client's request. Without this the
                # remaining header bytes are still buffered when the tunnel
                # opens, and they are forwarded to the server as the first
                # bytes of the TLS stream -- which fails as
                # "SSL routines::wrong version number", a long way from the
                # actual mistake.
                while True:
                    line = await asyncio.wait_for(creader.readline(), 30)
                    if line in (b"\r\n", b"\n", b""):
                        break
            else:
                # Absolute-form request for plain HTTP. Rare -- almost
                # everything is HTTPS -- but apt-style clients and some
                # installers still use it.
                if "://" not in target:
                    cwriter.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await cwriter.drain()
                    return
                rest = target.split("://", 1)[1]
                hostport = rest.split("/", 1)[0]
                path = "/" + (rest.split("/", 1)[1] if "/" in rest else "")
                host, _, port_s = hostport.rpartition(":")
                if not host:
                    host, port = hostport, 80
                else:
                    port = int(port_s)
                headers = f"{method} {path} {parts[2]}\r\n".encode("latin-1")

            allowed, why = self.policy.host_allowed(host, port)
            if not allowed:
                self.deny(host, port, why)
                cwriter.write(b"HTTP/1.1 403 Forbidden\r\n"
                              b"Content-Type: text/plain\r\n\r\n"
                              b"blocked by the wk workspace egress policy\r\n")
                await cwriter.drain()
                return

            try:
                ureader, uwriter = await self.open_upstream(host, port)
            except Exception as exc:                      # noqa: BLE001
                log(f"upstream {host}:{port} failed: {exc}")
                cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await cwriter.drain()
                return

            upstream = uwriter
            log(f"allow {host}:{port} ({why})")

            if method == "CONNECT":
                cwriter.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await cwriter.drain()
            else:
                uwriter.write(headers)
                # Relay the client's headers verbatim after rewriting the
                # request line above.
                while True:
                    line = await asyncio.wait_for(creader.readline(), 30)
                    uwriter.write(line)
                    if line in (b"\r\n", b"\n", b""):
                        break
                await uwriter.drain()

            await asyncio.gather(
                self.pipe(creader, uwriter),
                self.pipe(ureader, cwriter),
            )
        except (asyncio.TimeoutError, ConnectionResetError, OSError):
            pass
        finally:
            self.active -= 1
            for w in (cwriter, upstream):
                if w is not None:
                    try:
                        w.close()
                    except OSError:
                        pass


def sd_notify(state):
    """Tell systemd we are listening, without depending on python3-systemd.

    Type=notify matters here: `wk` must be able to treat "the service is
    active" as "the boundary is up", and a Type=simple service is active
    before the socket exists.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(state.encode())


async def main():
    store = os.environ.get("WK_STORE", "/var/lib/wk")
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    sock_dir = os.path.join(runtime, "wk")
    sock_path = os.path.join(sock_dir, "proxy.sock")

    os.makedirs(sock_dir, mode=0o700, exist_ok=True)
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    proxy = Proxy(Policy(store))
    server = await asyncio.start_unix_server(proxy.handle, path=sock_path)
    # The workspace runs as the same uid (--userns keep-id), so 0600 is enough
    # and is the tightest thing that works.
    os.chmod(sock_path, 0o600)

    log(f"listening on {sock_path}")
    log(f"allowlist: {', '.join(sorted(ALLOWED_HOSTS))} (+ pi-hosts from {store})")
    sd_notify("READY=1")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
