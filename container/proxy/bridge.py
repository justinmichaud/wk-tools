#!/usr/bin/env python3
"""Loopback-to-unix bridge, running inside the workspace.

The workspace has no network interface, so the egress proxy cannot be reached
at an address -- only through the unix socket bind-mounted at /run/wk/proxy.sock.
Nothing in the toolchain speaks "HTTP proxy over a unix socket": curl, git, pip
and the Claude CLI all want http_proxy=host:port. This bridges the gap by
listening on loopback inside the container's own namespace and forwarding each
connection, byte for byte, to that socket.

It is not a security boundary and does not try to be -- the policy lives on the
other end of the socket, outside the container, where the workspace cannot
reach it. Killing this process only cuts the workspace's own egress.
"""

import asyncio
import os
import sys

SOCKET = os.environ.get("WK_PROXY_SOCKET", "/run/wk/proxy.sock")
PORT = int(os.environ.get("WK_PROXY_PORT", "3128"))


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(creader, cwriter):
    try:
        ureader, uwriter = await asyncio.open_unix_connection(SOCKET)
    except OSError as exc:
        cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\n"
                      b"Content-Type: text/plain\r\n\r\n"
                      + f"wk egress proxy unreachable at {SOCKET}: {exc}\r\n".encode())
        try:
            await cwriter.drain()
        except OSError:
            pass
        cwriter.close()
        return
    await asyncio.gather(pipe(creader, uwriter), pipe(ureader, cwriter))


async def main():
    if not os.path.exists(SOCKET):
        print(f"[wk-bridge] no proxy socket at {SOCKET}; workspace has no egress",
              file=sys.stderr)
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    print(f"[wk-bridge] 127.0.0.1:{PORT} -> {SOCKET}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        # Almost always "address already in use": another wk command won the
        # race and the bridge is up, which is the desired state either way.
        print(f"[wk-bridge] not started: {exc}", file=sys.stderr)
