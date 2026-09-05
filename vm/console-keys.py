#!/usr/bin/env python3
"""Type into a guest's own virtual keyboard, through the VNC server
Virtualization.framework gives every VM (`tart run --vnc-experimental`).

This is the machine's console, not macOS automation: the key arrives at the
guest as if it came from a keyboard plugged into the VM, so nothing inside the
guest has to be granted Accessibility and nothing about the guest is loosened.
That matters because the one thing a guest cannot be told any other way is
"yes, I have seen this Setup Assistant pane" -- every preference that records it
is rewritten at the next login (docs/defects).

    console-keys.py <host> <port> <password> (<key> | click <x> <y>)...

Keys are names (`return`, `tab`, `space`, `escape`, `down`, `up`, `left`,
`right`) or a single character. Exits non-zero if the console refuses.

DES for the VNC challenge comes from `openssl`, which every macOS ships;
implementing it here would be a hundred lines of cipher to review.
"""
import socket
import struct
import subprocess
import sys
import time

KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "space": 0x0020,
    "escape": 0xFF1B, "down": 0xFF54, "up": 0xFF52, "left": 0xFF51,
    "right": 0xFF53, "backspace": 0xFF08,
}


def keysym(name):
    if name in KEYSYMS:
        return KEYSYMS[name]
    if len(name) == 1:
        return ord(name)
    raise SystemExit(f"console-keys: no key named {name!r}")


def _des_key(password):
    """VNC authentication reverses the bits of each byte of the password, which
    is a quirk of the original implementation and not of DES."""
    raw = password.encode()[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in raw)


def _respond(challenge, password):
    key = _des_key(password).hex()
    out = b""
    for i in (0, 8):
        cp = subprocess.run(
            ["/usr/bin/openssl", "enc", "-des-ecb", "-K", key, "-nopad"],
            input=challenge[i:i + 8], capture_output=True)
        if cp.returncode != 0 or len(cp.stdout) != 8:
            raise SystemExit(f"console-keys: openssl could not answer the challenge: "
                             f"{cp.stderr.decode(errors='replace').strip()}")
        out += cp.stdout
    return out


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise SystemExit("console-keys: the console closed the connection")
        buf += part
    return buf


def connect(host, port, password):
    sock = socket.create_connection((host, port), timeout=15)
    sock.settimeout(15)
    if not _recv(sock, 12).startswith(b"RFB 003."):
        raise SystemExit("console-keys: not an RFB console")
    sock.sendall(b"RFB 003.008\n")

    count = _recv(sock, 1)[0]
    if count == 0:
        reason = _recv(sock, struct.unpack(">I", _recv(sock, 4))[0])
        raise SystemExit(f"console-keys: the console refused: {reason.decode(errors='replace')}")
    types = _recv(sock, count)
    if 2 not in types:
        raise SystemExit(f"console-keys: the console offers no password auth (types {list(types)})")
    sock.sendall(b"\x02")
    sock.sendall(_respond(_recv(sock, 16), password))
    if struct.unpack(">I", _recv(sock, 4))[0] != 0:
        raise SystemExit("console-keys: the console rejected the password")

    sock.sendall(b"\x01")                      # shared, so an open window stays open
    _recv(sock, 20)                            # framebuffer size and pixel format
    return sock


# Not optional: with no SetEncodings the Virtualization.framework server aborts
# the connection with `FIXME IF: "It is unclear if we can support clients that
# don't support this pseudo encoding."` and takes the `tart run` down with it.
ENCODINGS = (0, -223, -308, -239, -240, -232)


def announce(sock):
    sock.sendall(struct.pack(">BBH", 2, 0, len(ENCODINGS))
                 + b"".join(struct.pack(">i", e) for e in ENCODINGS))


def _finish_init(sock):
    """ServerInit: 2+2 size, 16 pixel format, then a 4-byte length and a name."""
    name_len = struct.unpack(">I", _recv(sock, 4))[0]
    _recv(sock, name_len)


def press(sock, sym):
    for down in (1, 0):
        sock.sendall(struct.pack(">BBHI", 4, down, 0, sym))
        time.sleep(0.05)


# Setup Assistant's buttons take no keyboard focus, with or without full
# keyboard access: measured on a Tahoe 26.4 pane, escape, return, space, tab and
# tab-tab each left it up. The pointer is the only thing it answers.
def click(sock, x, y):
    sock.sendall(struct.pack(">BBHH", 5, 0, x, y))
    time.sleep(0.2)
    for mask in (1, 0):
        sock.sendall(struct.pack(">BBHH", 5, mask, x, y))
        time.sleep(0.1)


def main():
    if len(sys.argv) < 5:
        raise SystemExit(__doc__.strip().splitlines()[6].strip())
    host, port, password = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    sock = connect(host, port, password)
    _finish_init(sock)
    announce(sock)
    time.sleep(0.5)
    args = sys.argv[4:]
    while args:
        if args[0] == "click":
            click(sock, int(args[1]), int(args[2]))
            args = args[3:]
        else:
            press(sock, keysym(args[0]))
            args = args[1:]
        time.sleep(0.4)
    sock.close()


if __name__ == "__main__":
    main()
