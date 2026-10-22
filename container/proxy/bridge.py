#!/usr/bin/env python3
"""Forwards loopback to the egress proxy's unix socket for one exec, printing its port once: the proxy names a caller by the bridge's pid, so it is the child of the command ensure-bridge.sh execs into, carrying that command's ancestry, serving only that command's tree with no agent between (lib/wk/pushgate.py), and dying with it; a process group of its own leaves a terminal's ^C and ^Z to the command."""

import asyncio
import ctypes
import functools
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib")]

from relay import pipe  # noqa: E402
from wk import pushgate  # noqa: E402

SOCKET = os.environ.get("WK_PROXY_SOCKET", "/run/wk/proxy.sock")
PROC = "/proc"
PR_SET_PDEATHSIG = 1
ancestry = pushgate.Proc(PROC)


def client_inode(local, peer):
    """The client's end of a loopback connection: the socket whose local port is `peer` and remote port `local`."""
    for table in ("tcp", "tcp6"):
        try:
            with open(os.path.join(PROC, "net", table)) as f:
                rows = [line.split() for line in f.read().splitlines()[1:]]
        except OSError:
            continue
        for row in rows:
            if int(row[1].rsplit(":", 1)[1], 16) == peer and int(row[2].rsplit(":", 1)[1], 16) == local:
                return row[9]
    return None


def holders(inode):
    want, found = "socket:[%s]" % inode, []
    for pid in (p for p in os.listdir(PROC) if p.isdigit()):
        fds = os.path.join(PROC, pid, "fd")
        try:
            names = os.listdir(fds)
        except OSError:
            continue
        for fd in names:
            try:
                if os.readlink(os.path.join(fds, fd)) == want:
                    found.append(int(pid))
                    break
            except OSError:
                continue
    return found


def refusal(local, peer, command, ws):
    """Why the client on loopback port `peer` may not use the bridge on `local`, or None."""
    inode = client_inode(local, peer)
    pids = holders(inode) if inode else []
    if not pids:
        return ("no process this bridge can read holds the connection from port %d; one of another user, as under sudo, is given a bridge "
                "of its own:  sudo /opt/wk-tools/container/proxy/ensure-bridge.sh <command>" % peer)
    for pid in pids:
        try:
            pushgate.bridged(ancestry, pid, command, ws)
        except pushgate.Denied as e:
            return "%s; %s" % (e.why, e.remedy)
    return None


async def handle(command, ws, creader, cwriter):
    why = refusal(cwriter.get_extra_info("sockname")[1], cwriter.get_extra_info("peername")[1], command, ws)
    if why:
        await answer(cwriter, b"403 Forbidden", "wk bridge: " + why.replace("\n", " "))
        return
    try:
        ureader, uwriter = await asyncio.open_unix_connection(SOCKET)
    except OSError as exc:
        await answer(cwriter, b"502 Bad Gateway", f"wk egress proxy unreachable at {SOCKET}: {exc}")
        return
    await asyncio.gather(pipe(creader, uwriter), pipe(ureader, cwriter))


async def answer(cwriter, status, line):
    cwriter.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\n\r\n" + line.encode() + b"\r\n")
    try:
        await cwriter.drain()
    except OSError:
        pass
    cwriter.close()


def die_with(parent):
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")
    if os.getppid() != parent:
        sys.exit(0)


async def main(command, ws):
    server = await asyncio.start_server(functools.partial(handle, command, ws), "127.0.0.1", 0)
    print(server.sockets[0].getsockname()[1], flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    os.setpgid(0, 0)
    die_with(int(sys.argv[1]))
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))
