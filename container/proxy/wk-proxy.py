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
# Checked before the allowlist, on the same dot-boundary suffix match: a
# workspace fetches from github.com and nothing more. Its API is what `gh`
# (and anything else that can post, comment or upload as a person) talks to,
# and no agent in a workspace may publish -- the push keys are held back the
# same way (cmd/push). `wk verify` measures this refusal.
DENIED_HOSTS = {
    "api.github.com": "GitHub's API is refused: nothing in a workspace may post as a person",
    "uploads.github.com": "GitHub's upload API is refused: nothing in a workspace may publish",
}

# Suffix matches: "github.com" matches github.com and codeload.github.com, but
# not evilgithub.com -- the match is on a dot boundary. (api.github.com is
# taken out first, above.)
ALLOWED_HOSTS = {
    # Anthropic: the API, the console, and the CLI's own installer.
    "anthropic.com": (80, 443),
    "claude.ai": (80, 443),
    "claude.com": (80, 443),
    # GitHub: clones and fetches over HTTPS, pushes to the fork over SSH, and
    # the raw/codeload hosts that benchmark payloads come from.
    "github.com": (22, 80, 443),
    "githubusercontent.com": (80, 443),
    "githubassets.com": (443,),
    # PyPI. build-webkit autoinstalls setuptools on every clean build, so this
    # is a build dependency rather than an escape hatch.
    "pypi.org": (443,),
    "pythonhosted.org": (443,),
    # The distribution archive, so a workspace can install a package.
    #
    # What this boundary is for is unusual traffic -- a workspace reaching a
    # machine, a service or a network nobody expects a WebKit checkout to talk
    # to. `apt-get install` from Ubuntu's own archive is not that, and neither
    # is an editor: both are ordinary things to do in a development container,
    # and refusing them buys nothing an audit would thank us for. Signatures
    # are apt's own, and the BLOCKED_NETS check below is
    # unchanged -- so none of these names can become a route onto the LAN or the
    # tailnet, which is the property that actually matters.
    #
    # Explicit hostnames rather than the `ubuntu.com` suffix, because the
    # suffix would also cover every other host under that domain -- these are
    # the two the image's sources.list names (ports. on arm, archive. on
    # x86_64), security., which apt-get update reads on the same run, and
    # ddebs., the debug-symbol archive (container/firstrun.sh provisions it;
    # docs/Urgent/HANDOFF-debug.md). Port 80 as well as 443: apt's configured
    # URIs are http, and it verifies the signature rather than the transport.
    "ports.ubuntu.com": (80, 443),
    "archive.ubuntu.com": (80, 443),
    "security.ubuntu.com": (80, 443),
    "ddebs.ubuntu.com": (80, 443),     # debug symbols for backtraces through system libraries
    # Zed's own CDN, for the same reason. Its remote server asks this for the
    # agent registry as it starts, and the refusal was a red line in the log of
    # every remote session -- an editor failing at something that has nothing to
    # do with what the boundary is watching for. The server binary itself comes
    # from github.com, already above.
    "agentclientprotocol.com": (443,),

    # --- browsing and benchmarking -------------------------------------------
    # Everything below exists so a workspace can actually drive a browser at
    # real pages. Port 80 as well as 443 throughout: these are visited by
    # MiniBrowser, and a site that answers :80 with a redirect to :443 still
    # needs the first hop to be allowed or the load simply fails.
    #
    # This is a real widening of the boundary and it is deliberate. Note for
    # the sandbox audit (docs/HANDOFF-sandboxing.md): a workspace can now reach
    # general-purpose sites with user-generated content and ad/analytics
    # networks, which the Anthropic/GitHub/PyPI list did not permit. The
    # BLOCKED_NETS check below is unchanged and still keeps any of these names
    # from resolving onto the LAN or the tailnet.

    # Project and benchmark hosts.
    "webkit.org": (80, 443),
    "browserbench.org": (80, 443),     # Speedometer, MotionMark, JetStream
    "igalia.com": (80, 443),
    "gnome.org": (80, 443),

    # The top-10 sites by global traffic, as a general-purpose browsing set.
    # Each is paired with the asset/CDN domain it cannot render without --
    # allowing only the front door gets a half-loaded page, which is worse than
    # useless for judging a browser.
    "google.com": (80, 443),
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

    # --- building a Yocto image ----------------------------------------------
    # `wk sysimage build webkit-2.52-yocto-rpi4-64` runs bitbake in a workspace, and bitbake
    # fetches sources. A Yocto build touches, in principle, every upstream that
    # every recipe in six layers names -- which is not a list anyone can write
    # down, and is exactly the wrong shape for an allowlist.
    #
    # So the build is configured to fetch from the Yocto Project's own source
    # mirror first (`INHERIT += "own-mirrors"` with SOURCE_MIRROR_URL, in
    # image/yocto-build.sh). The mirror carries every source of every release
    # branch, so the overwhelming majority of fetches resolve to one host, and
    # this list is the *remainder*: the layer repositories named in the release
    # branch's own manifest.xml, plus the two hosts the `repo` tool needs to
    # bootstrap itself.
    #
    # NOTE FOR THE SANDBOX AUDIT (docs/HANDOFF-sandboxing.md): this is a real
    # widening, of the same kind as the browsing block above and smaller. It
    # adds source-code hosts only, it is still by hostname, and the BLOCKED_NETS
    # check below is unchanged -- so none of these names can become a route
    # onto the LAN or the tailnet. What it does mean is that a workspace can
    # fetch arbitrary tarballs from a distribution mirror, which the
    # Anthropic/GitHub/PyPI list did not permit.
    #
    # It is deliberately NOT the full set of upstreams a recipe might reach for
    # when the mirror lacks something. When a fetch is refused, the proxy logs
    # the name; add it here with a reason, rather than pre-emptively allowing a
    # hundred hosts against the day one of them is needed.
    # Port 80 as well as 443, and not out of laziness: poky's built-in PREMIRRORS
    # and MIRRORS lists are written with `http://` URLs, so the mirror this whole
    # arrangement depends on is reached over port 80 by default. Refusing it
    # sends every fetch to its upstream instead, which is the opposite of the
    # intent -- and shows up as `DENY downloads.yoctoproject.org:80`.
    "yoctoproject.org": (80, 443),     # git. and downloads. -- layers + the mirror
    "openembedded.org": (80, 443),     # git. (manifest.xml) and sources. (a MIRROR)
    "googlesource.com": (443,),        # `repo` clones its own git-repo from here
    # Added from refusals in this log, not in anticipation of them. A full
    # `--runall=fetch` pass over 1492 fetch tasks produced exactly these four
    # beyond the mirror itself, which is the measurement that made this list
    # short: point the build at the mirror and almost everything resolves there.
    "freedesktop.org": (80, 443),      # gitlab. -- polkit, wayland, mesa, libinput
    "kernel.org": (80, 443),           # mirrors. is one of poky's default PREMIRRORS
    "videolan.org": (80, 443),         # code. -- dav1d
    "metacpan.org": (80, 443),         # cpan. -- Archive-Zip
    # hunspell/hyphen, which meta-webkit fetches at WebKit 2.52 and did not at
    # 2.48 -- so this appeared the first time a 2.52 image was built.
    #
    # A suffix, where every other entry that could be a hostname is one, and it
    # is forced rather than chosen: `downloads.sourceforge.net` answers a
    # download with a 302 to a per-request mirror subdomain
    # (`gigenet.dl.sourceforge.net` on the run that produced this line), so
    # allowing only the name in the recipe allows the redirect and refuses the
    # bytes. 443 alone -- the recipe's URL is https, and nothing here needs the
    # port-80 argument the yocto mirror entries make.
    #
    # For the sandbox audit (docs/HANDOFF-sandboxing.md): this is a widening,
    # and it is the same kind as the five entries above it -- a host a build
    # fetches declared sources from, reached only through the proxy, with
    # BLOCKED_NETS unchanged so the name cannot resolve onto the LAN or the
    # tailnet. No allowed mirror carries this tarball: sources.openembedded.org,
    # downloads.yoctoproject.org/mirror/sources and mirrors.kernel.org were all
    # checked and answer 404 or nothing.
    "sourceforge.net": (443,),         # downloads. + its dl. mirrors -- hyphen

    # The buildroot lane. `sources.buildroot.net` is buildroot's own mirror of
    # every package it knows how to fetch, and image/buildroot-build.sh points
    # BR2_PRIMARY_SITE at it for the same reason yocto-build.sh points at the
    # OpenEmbedded mirror: one host answers almost every fetch, so the set of
    # names that has to be allowed collapses to something a person can read.
    # Port 80 as well as 443 because buildroot's own default for it is http.
    #
    # gnu.org is the fallback that is actually needed: `ftpmirror.gnu.org` is
    # where several host tools come from when the mirror does not carry the
    # exact version a 2020 tree pins. Both names came out of a build's own
    # refusals (DENY sources.buildroot.net:80, DENY ftpmirror.gnu.org:80), not
    # from guessing at a list.
    "sources.buildroot.net": (80, 443),
    "gnu.org": (80, 443),              # ftp. and ftpmirror. -- host tools
    # WPE's own release host, and not covered by `webkit.org` above -- a
    # different domain. libwpe, wpebackend-fdo and cog tarballs come from here
    # and the buildroot mirror does not carry them (it answers 404, which is how
    # this was found).
    "wpewebkit.org": (80, 443),
    # github.com and githubusercontent.com are already allowed above, and carry
    # meta-openembedded, meta-webkit, meta-clang, meta-browser and the
    # Raspberry Pi firmware and kernel.
    #
    # pkgs.tailscale.com, for one recipe: image/yocto/meta-wk-tailnet fetches
    # the pinned static tailscale build so that a board made from the image is
    # on the tailnet and nothing about how to reach it has to be written down
    # (CLAUDE.md, "Cattle, not pets"). The tarball is pinned by version and
    # sha256 in that layer, so what this allows is one file whose bytes are
    # decided before the fetch -- and the Yocto source mirror does not carry it,
    # which is why the fetch has to go upstream at all.
    "tailscale.com": (443,),
}

# Addresses that are never permitted as a *destination*, whatever resolved to
# them. Without this an allowlisted name whose DNS is wrong -- or hostile --
# becomes a route onto the LAN, which is precisely what the boundary exists to
# prevent. The workstation is an unrestricted tailnet node, so this matters more
# here than it would on an isolated machine. The boards sit on the house LAN,
# not an isolated segment: this block and the pi-hosts exemption below are
# the whole boundary between a workspace and them.
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
        """A Pi's address sits inside the tailnet range that BLOCKED_NETS
        refuses. The block exists to stop an allowlisted *name* resolving onto
        the tailnet, not to unlist the device itself -- so a destination that
        is the allowlisted address must skip the address check."""
        return host.lower().rstrip(".") in self._pi_hosts()

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
        # This service runs for months; one entry per distinct denial would
        # grow without bound if something enumerates hostnames.
        if len(self._denials) > 512:
            self._denials = {k: v for k, v in self._denials.items()
                             if now - v <= DENY_LOG_INTERVAL}

    async def open_upstream(self, host, port):
        """Resolve, check every resolved address, then connect.

        Checking after resolution rather than before is the point: the
        allowlist is by name, but the danger is by address, and one name can
        resolve to many.
        """
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

    proxy = Proxy(Policy(store))
    servers = []

    # A unix socket for containers, which have no network interface at all and
    # reach this only through a bind-mounted socket. Creating its directory is
    # part of the unix branch, not preamble: a TCP-only run (the macOS host,
    # serving guest VMs) has no runtime directory to make and would die trying.
    if os.environ.get("WK_PROXY_UNIX", "1") != "0":
        os.makedirs(sock_dir, mode=0o700, exist_ok=True)
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        servers.append(await asyncio.start_unix_server(proxy.handle, path=sock_path))
        # The workspace runs as the same uid (--userns keep-id), so 0600 is
        # enough and is the tightest thing that works.
        os.chmod(sock_path, 0o600)
        log(f"listening on {sock_path}")

    # A TCP socket for macOS guest VMs, which cannot see a unix socket across
    # the hypervisor boundary. Their egress is default-denied by Softnet on the
    # host except to this address, so this listener is the guest's only way
    # out -- the same structural position the unix socket holds for a
    # container, reached differently.
    #
    # Bind address is explicit and never 0.0.0.0: this speaks for a policy
    # boundary, and a proxy listening on every interface is an open relay for
    # anything else that can reach the machine.
    tcp = os.environ.get("WK_PROXY_TCP")
    if tcp:
        host, _, port = tcp.rpartition(":")
        if not host:
            raise SystemExit("WK_PROXY_TCP must be <address>:<port>, not just a port")
        servers.append(await asyncio.start_server(proxy.handle, host=host, port=int(port)))
        log(f"listening on {host}:{port} (guest VMs)")

    if not servers:
        raise SystemExit("no listener configured (WK_PROXY_UNIX=0 and no WK_PROXY_TCP)")

    log(f"allowlist: {', '.join(sorted(ALLOWED_HOSTS))} (+ pi-hosts from {store})")
    sd_notify("READY=1")

    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
