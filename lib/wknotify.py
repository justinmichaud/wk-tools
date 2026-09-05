"""sd_notify for the Type=notify services here; no-op without NOTIFY_SOCKET, since the same programs run under nohup on the macOS host, whose python3 has no python3-systemd."""

import os
import socket


def sd_notify(state):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(state.encode())
