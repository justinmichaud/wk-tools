#!/usr/bin/env python3
"""Forwards loopback to the egress proxy's unix socket, since the toolchain wants http_proxy=host:port. Not a boundary: the policy is on the socket's far end."""

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
        # Almost always "address already in use": another wk command won the race and the bridge is up.
        print(f"[wk-bridge] not started: {exc}", file=sys.stderr)
