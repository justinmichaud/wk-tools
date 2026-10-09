#!/usr/bin/env python3
"""core.sshCommand in every workspace: git runs `<this> [options] [user@]host <command>` where it would run ssh, and the push service on the host runs the GitHub session with the deploy key.
It speaks and relays; every refusal comes from the far end. Exit status is the session's own, 1 when refused, 255 when the service does not answer."""

import json
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from wk import pushgate  # noqa: E402
from wk.store import Store  # noqa: E402

WITH_ARGUMENT = set("-o -p -i -l -F -J -c -E -b -L -R -D -S -W -w".split())


def words(argv):
    """(host, command) of the ssh command line git builds: options, the host, then the remote command."""
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in WITH_ARGUMENT else 1
    return (argv[i], " ".join(argv[i + 1:])) if i < len(argv) else ("", "")


def read_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def forward_stdin(sock):
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            sock.sendall(data)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def main(argv):
    host, line = words(argv)
    path = Store().workspace_push_socket()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError as e:
        sys.stderr.write("error: no push service at %s (%s)\n    On the host:  ./setup, then wk doctor\n" % (path, e.strerror))
        return 255
    sock.sendall((json.dumps({"verb": "push", "host": host, "command": line, "protocol": os.environ.get("GIT_PROTOCOL", "")}) + "\n").encode())
    ack = b""
    while not ack.endswith(b"\n"):
        byte = sock.recv(1)
        if not byte:
            sys.stderr.write("error: the push service at %s closed the connection\n" % path)
            return 255
        ack += byte
    reply = json.loads(ack)
    if not reply.get("ok"):
        sys.stderr.write("error: %s\n    %s\n" % (reply.get("refused"), reply.get("remedy")))
        return 1
    threading.Thread(target=forward_stdin, args=(sock,), daemon=True).start()
    while True:
        head = read_exact(sock, struct.calcsize(">BI"))
        if head is None:
            return 255
        kind, size = struct.unpack(">BI", head)
        data = read_exact(sock, size)
        if data is None:
            return 255
        if kind == pushgate.EXIT:
            return struct.unpack(">i", data)[0] & 0xFF
        os.write(1 if kind == pushgate.STDOUT else 2, data)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
