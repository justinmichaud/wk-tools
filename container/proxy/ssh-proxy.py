#!/usr/bin/env python3
"""ProxyCommand /opt/wk-tools/container/proxy/ssh-proxy.py %h %p -- onto the egress proxy's unix socket directly, so ssh works before the loopback bridge is up."""

import os
import select
import socket
import sys

SOCKET = os.environ.get("WK_PROXY_SOCKET", "/run/wk/proxy.sock")


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: ssh-proxy.py <host> <port>")
    host, port = sys.argv[1], sys.argv[2]

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCKET)
    except OSError as exc:
        sys.exit(f"wk egress proxy unreachable at {SOCKET}: {exc}")

    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
                 .encode())

    # Only up to the blank line: what follows belongs to the tunnel.
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(1)
        if not chunk:
            sys.exit("wk egress proxy closed the connection")
        response += chunk

    status = response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200 " not in status:
        sys.exit(f"wk egress policy refused {host}:{port}: {status}")

    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        readable, _, _ = select.select([sock, stdin], [], [])
        if sock in readable:
            data = sock.recv(65536)
            if not data:
                break
            stdout.write(data)
            stdout.flush()
        if stdin in readable:
            data = stdin.read1(65536)
            if not data:
                break
            sock.sendall(data)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
