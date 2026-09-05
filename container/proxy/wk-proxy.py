#!/usr/bin/env python3
"""The workspace egress boundary: --network none, the outside reached only through this
unix socket, by hostname. Rootless podman re-emits container traffic from a random scope, so an interface filter would have nothing to see."""

import asyncio
import ipaddress
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from wknotify import sd_notify  # noqa: E402

DENIED_HOSTS = {
    "uploads.github.com": "GitHub's upload API is refused: nothing in a workspace may publish",
}

# The one host whose TLS is not tunnelled: CONNECT goes to the credential injector (github-inject.py). Exact, and first, or `github.com` tunnels it.
# SANDBOX AUDIT (docs/HANDOFF-sandboxing.md): a workspace reaches GitHub's API but cannot authenticate -- under `wk push off`, which `wk ai claude` sets, the injector holds no token and answers 401. `wk verify` measures both halves.
INJECTED_HOSTS = {
    "api.github.com": 443,
}

INJECT_SOCKET = os.environ.get(   # under the store root, not the mounted runtime dir
    "WK_INJECT_SOCK",
    os.path.join(os.environ.get("WK_STORE", "/var/lib/wk"), "github-inject.sock"))

# Port 80 as well as 443 where a client's own URLs are http (apt, poky's mirrors) or a site answers :80 with the redirect a browser follows to :443.
ALLOWED_HOSTS = {                      # dot-boundary suffix matches
    "anthropic.com": (80, 443),        # the API, the console, the CLI installer
    "claude.ai": (80, 443),
    "claude.com": (80, 443),
    "github.com": (22, 80, 443),       # https clones, ssh pushes to the fork
    "githubusercontent.com": (80, 443),
    "githubassets.com": (443,),
    "pypi.org": (443,),                # build-webkit autoinstalls setuptools
    "pythonhosted.org": (443,),
    "ports.ubuntu.com": (80, 443),     # apt: sources.list on arm
    "archive.ubuntu.com": (80, 443),   # apt: sources.list on x86_64
    "security.ubuntu.com": (80, 443),  # read by the same apt-get update
    "ddebs.ubuntu.com": (80, 443),     # debug symbols for system-library frames
    "agentclientprotocol.com": (443,), # zed's remote server reads its registry

    "webkit.org": (80, 443),           # pages MiniBrowser is driven at
    "browserbench.org": (80, 443),     # Speedometer, MotionMark, JetStream
    "igalia.com": (80, 443),
    "gnome.org": (80, 443),

    "google.com": (80, 443),           # top-10 by traffic, each with its CDN
    "gstatic.com": (80, 443),          # google's static assets
    "googleapis.com": (80, 443),
    "youtube.com": (80, 443),
    "ytimg.com": (80, 443),            # youtube thumbnails
    "googlevideo.com": (80, 443),      # youtube media
    "facebook.com": (80, 443),
    "fbcdn.net": (80, 443),            # facebook + instagram assets
    "instagram.com": (80, 443),
    "cdninstagram.com": (80, 443),
    "x.com": (80, 443),
    "twitter.com": (80, 443),          # still redirects here
    "twimg.com": (80, 443),            # x/twitter assets
    "wikipedia.org": (80, 443),
    "wikimedia.org": (80, 443),        # wikipedia images and static
    "reddit.com": (80, 443),
    "redditstatic.com": (80, 443),
    "redditmedia.com": (80, 443),
    "redd.it": (80, 443),
    "amazon.com": (80, 443),
    "media-amazon.com": (80, 443),
    "ssl-images-amazon.com": (80, 443),
    "tiktok.com": (80, 443),
    "tiktokcdn.com": (80, 443),
    "whatsapp.com": (80, 443),
    "baidu.com": (80, 443),

    # bitbake fetches the Yocto source mirror first (image/yocto-build.sh); this is the remainder, from --runall=fetch over 1492 tasks.
    # SANDBOX AUDIT: a real widening -- source-code hosts only, still by
    # hostname, BLOCKED_NETS unchanged, so no name here becomes a route onto the
    # LAN or the tailnet. It does let a workspace fetch distribution tarballs.
    "yoctoproject.org": (80, 443),     # git. and downloads. -- layers + the mirror
    "openembedded.org": (80, 443),     # git. (manifest.xml) and sources. (a MIRROR)
    "googlesource.com": (443,),        # `repo` clones its own git-repo from here
    "freedesktop.org": (80, 443),      # gitlab. -- polkit, wayland, mesa, libinput
    "kernel.org": (80, 443),           # mirrors. is one of poky's default PREMIRRORS
    "videolan.org": (80, 443),         # code. -- dav1d
    "metacpan.org": (80, 443),         # cpan. -- Archive-Zip
    "sourceforge.net": (443,),         # hyphen: downloads. 302s to a dl. mirror

    "sources.buildroot.net": (80, 443),  # BR2_PRIMARY_SITE, http by default
    "gnu.org": (80, 443),              # ftpmirror. -- host tools the mirror lacks
    "wpewebkit.org": (80, 443),        # libwpe, wpebackend-fdo, cog tarballs
    "tailscale.com": (443,),           # pkgs. -- meta-wk-tailnet's pinned tarball

    # SANDBOX AUDIT: the widest widening in *kind*. A package registry serves
    # whatever a project's manifest names, so this is third-party code chosen by
    # a file in the checkout. Deliberate: a workspace that cannot install a
    # package is not a development machine. Each name was measured as a refusal.
    "registry.npmjs.org": (443,),      # npm, and `npm install -g` for an agent
    "formulae.brew.sh": (443,),        # Homebrew's formula index
    "ghcr.io": (443,),                 # Homebrew bottle manifests
    "crates.io": (443,),               # cargo, and static. for the tarballs
    "rust-lang.org": (443,),           # static. -- rustup's toolchains
    "rustup.rs": (443,),               # sh. -- the rustup installer

    # The softwareupdate scan path -- swscan, swcdn, updates.cdn-apple.com, mesu, gdmf -- is deliberately absent: a guest is a clone of a pinned image, no guest can turn the check off (vm/desktop.sh), and an offer that arrives puts a Setup Assistant pane in front of the window a benchmark is measured in.
    "developer.apple.com": (443,),     # and download. -- Xcode + CLT
    "valid.apple.com": (80, 443),      # gatekeeper: unchecked, it will not run
    "ocsp.apple.com": (80, 443),
    "ocsp2.apple.com": (80, 443),
    "crl.apple.com": (80, 443),
    "pki.goog": (80, 443),             # i. -- Google Trust Services CRL/OCSP
}

BLOCKED_NETS = [   # never a destination: this workstation is a tailnet node
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


def normalize_host(host):
    # `API.GITHUB.COM.` is one host to DNS and another to a dict, so the allowlist check and the route are taken on this one spelling.
    return host.lower().rstrip(".")


class Policy:
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
        if mtime != self._pi_mtime:    # `wk pi setup` appends without a restart
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
        host = normalize_host(host)

        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None

        if addr is not None:           # one address at a time, never 100.64/10
            return (host in self._pi_hosts() and port == 22), "pi test device"

        if host in INJECTED_HOSTS:
            if port == INJECTED_HOSTS[host]:
                return True, "credential injector"
            return False, "only port %d of %s is allowed, and only through the injector" % (
                INJECTED_HOSTS[host], host)

        for name, why in DENIED_HOSTS.items():
            if host == name or host.endswith("." + name):
                return False, why
        for suffix, ports in ALLOWED_HOSTS.items():
            if host == suffix or host.endswith("." + suffix):
                if port in (ports if isinstance(ports, tuple) else (ports,)):
                    return True, suffix
                return False, f"port {port} not allowed for {suffix}"
        return False, "not in the allowlist"

    def is_pi(self, host):
        return normalize_host(host) in self._pi_hosts()  # exempt from BLOCKED_NETS

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

    def deny(self, host, port, why):   # rate-limited: a retry loop fills a log,
        key = (host, port, why)
        now = time.time()
        last = self._denials.get(key, 0)
        if now - last > DENY_LOG_INTERVAL:
            self._denials[key] = now
            log(f"DENY {host}:{port} -- {why}")
        if len(self._denials) > 512:   # and enumeration grows one entry a name
            self._denials = {k: v for k, v in self._denials.items()
                             if now - v <= DENY_LOG_INTERVAL}

    async def open_upstream(self, host, port):
        if INJECTED_HOSTS.get(host) == port:
            return await asyncio.open_unix_connection(INJECT_SOCKET)

        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)

        pi = self.policy.is_pi(host)
        last_error = None
        for family, socktype, proto, _canon, sockaddr in infos:
            addr = sockaddr[0]
            ok, why = (True, "") if pi else self.policy.address_allowed(addr)
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
                # Drain the client's remaining header bytes, or they reach the server as the first TLS bytes.
                while True:
                    line = await asyncio.wait_for(creader.readline(), 30)
                    if line in (b"\r\n", b"\n", b""):
                        break
            else:                      # absolute-form plain HTTP, as apt sends
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

            host = normalize_host(host)

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
                uwriter.write(headers)  # the rewritten request line
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


async def main():
    store = os.environ.get("WK_STORE", "/var/lib/wk")
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    sock_dir = os.path.join(runtime, "wk")
    sock_path = os.path.join(sock_dir, "proxy.sock")

    proxy = Proxy(Policy(store))
    servers = []

    if os.environ.get("WK_PROXY_UNIX", "1") != "0":
        os.makedirs(sock_dir, mode=0o700, exist_ok=True)
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        servers.append(await asyncio.start_unix_server(proxy.handle, path=sock_path))
        os.chmod(sock_path, 0o600)     # same uid as the workspace (keep-id)
        log(f"listening on {sock_path}")

    tcp = os.environ.get("WK_PROXY_TCP")  # guest VMs, Softnet-fenced to here
    if tcp:
        host, _, port = tcp.rpartition(":")
        if not host:
            raise SystemExit("WK_PROXY_TCP must be <address>:<port>, not just a port")
        servers.append(await asyncio.start_server(proxy.handle, host=host, port=int(port)))
        log(f"listening on {host}:{port} (guest VMs)")

    if not servers:
        raise SystemExit("no listener configured (WK_PROXY_UNIX=0 and no WK_PROXY_TCP)")

    log(f"allowlist: {', '.join(sorted(ALLOWED_HOSTS))} (+ pi-hosts from {store})")
    log(f"injected: {', '.join(sorted(INJECTED_HOSTS))} via {INJECT_SOCKET}")
    sd_notify("READY=1")

    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
