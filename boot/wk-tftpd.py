#!/usr/bin/env python3
"""A read-only TFTP server, for handing boot files to Pi firmware.

Why this rather than tftpd-hpa, which is what the handoff first assumed.  The
serving role has to *float*: any idle machine should be able to serve, the Mac
included (docs/HANDOFF-netboot.md, "The serving role floats").  A distro system
service does not float -- it is a different package, a different unit name and a
different config file on every host, and there is no tftpd-hpa on macOS at all.
This is one stdlib file that runs identically wherever `wk` runs, which is the
same reason `container/proxy/wk-proxy.py` is stdlib-only.

Read-only, and that is a property rather than a default: netboot never needs to
write, and a writable TFTP server on a home LAN is a filesystem anyone on the
network can put files into.  There is no WRQ handler here at all, so there is
nothing to misconfigure.

Implements RFC 1350 (RRQ, octet) plus the option extensions the Pi firmware
actually negotiates: `blksize` (RFC 2348), `tsize` (RFC 2349) and `timeout`.
Unknown options are omitted from the OACK, which RFC 2347 requires and which is
how a client learns they were not honoured.
"""

import argparse
import grp
import os
import pwd
import socket
import struct
import sys
import threading

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR, OP_OACK = 1, 2, 3, 4, 5, 6

ERR_NOT_FOUND = 1
ERR_ACCESS = 2
ERR_ILLEGAL = 4

# The firmware's own default is 512; it usually negotiates ~1468 to fit an
# untagged Ethernet frame. Both are honoured, and anything larger is clamped:
# a block that exceeds the path MTU fragments, and fragmented TFTP on a
# bootloader's minimal IP stack is a class of failure nobody wants to debug
# from a machine with no console.
MAX_BLKSIZE = 8192
DEFAULT_BLKSIZE = 512
RETRIES = 5


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def error_packet(code, text):
    return struct.pack("!HH", OP_ERROR, code) + text.encode() + b"\0"


def parse_request(payload):
    """RRQ payload -> (filename, mode, options). Fields are NUL-separated."""
    parts = payload.split(b"\0")
    if len(parts) < 2:
        return None, None, {}
    filename = parts[0].decode("latin-1")
    mode = parts[1].decode("latin-1").lower()
    options = {}
    rest = parts[2:]
    for i in range(0, len(rest) - 1, 2):
        if not rest[i]:
            break
        options[rest[i].decode("latin-1").lower()] = rest[i + 1].decode("latin-1")
    return filename, mode, options


def _inside(root, wanted):
    """Join onto the root, or None if the result escapes it.

    Path traversal is refused by construction: the result must still be inside
    the root after resolution.  A client that asks for `../../etc/shadow` is a
    client whose request never names a file outside the served tree, no matter
    how it spells it.
    """
    path = os.path.realpath(os.path.join(root, wanted))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path if os.path.isfile(path) else None


def resolve(root, filename):
    """Map a request onto a real file, or refuse.

    With one deliberate fallback: Pi firmware prefixes every request with a
    directory named after the last eight hex digits of the board's serial
    number, and that serial is not knowable until the board asks -- which is
    the fiddliest part of setting up Pi netboot by hand, and the one most
    likely to be got wrong from a machine with no console.  So a request that
    misses under its leading directory is retried at the root.

    This cannot reach outside the served tree: the retry goes through the same
    containment check, on a path with no directory component at all.  And the
    log distinguishes the two, so "served from the root" never looks like
    "found where the client asked".
    """
    wanted = filename.lstrip("/").replace("\\", "/")
    path = _inside(root, wanted)
    if path is not None:
        return path, False
    if "/" in wanted:
        path = _inside(root, os.path.basename(wanted))
        if path is not None:
            return path, True
    return None, False


def serve_file(peer, path, options, quiet):
    """One transfer, on its own ephemeral socket, as TFTP requires."""
    blksize = DEFAULT_BLKSIZE
    timeout = 3.0
    acked = {}

    if "blksize" in options:
        try:
            blksize = max(8, min(MAX_BLKSIZE, int(options["blksize"])))
            acked["blksize"] = str(blksize)
        except ValueError:
            pass
    if "timeout" in options:
        try:
            timeout = max(1, min(60, int(options["timeout"])))
            acked["timeout"] = str(int(timeout))
        except ValueError:
            pass
    if "tsize" in options:
        acked["tsize"] = str(os.path.getsize(path))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        with open(path, "rb") as fh:
            block = 0
            if acked:
                packet = struct.pack("!H", OP_OACK)
                for key, value in acked.items():
                    packet += key.encode() + b"\0" + value.encode() + b"\0"
                if not exchange(sock, peer, packet, 0, timeout):
                    return
            while True:
                chunk = fh.read(blksize)
                # Block numbers are 16-bit and wrap. A file larger than
                # 65535 * blksize is legal and common (an initrd at 512-byte
                # blocks passes it), and a server that stops at the wrap
                # truncates the transfer with no error anywhere.
                block = (block + 1) & 0xFFFF
                packet = struct.pack("!HH", OP_DATA, block) + chunk
                if not exchange(sock, peer, packet, block, timeout):
                    return
                if len(chunk) < blksize:
                    break
        if not quiet:
            log(f"  sent {os.path.basename(path)} to {peer[0]}")
    except OSError as exc:
        log(f"  error sending {path} to {peer[0]}: {exc}")
    finally:
        sock.close()


def exchange(sock, peer, packet, expect_block, timeout):
    """Send one packet and wait for its ACK, retransmitting on silence."""
    for _ in range(RETRIES):
        sock.sendto(packet, peer)
        try:
            reply, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        if addr != peer or len(reply) < 4:
            continue
        opcode, block = struct.unpack("!HH", reply[:4])
        if opcode == OP_ERROR:
            return False
        if opcode == OP_ACK and block == expect_block:
            return True
    log(f"  timed out waiting for ack of block {expect_block} from {peer[0]}")
    return False


def drop_privileges():
    """Become the invoking user, before reading anything.

    This file is installed root-owned with a NOPASSWD sudoers rule, because
    firmware TFTP clients speak to port 69 and nothing below 1024 can be bound
    without privilege.  That grant is worth exactly one syscall, so the shape
    here is bind-then-drop: root opens the socket and is gone before a single
    path is resolved or a single byte is read.

    What that buys is the difference between two very different grants.  A
    root-owned server reading an arbitrary `--root` would hand any caller a
    read-only export of the entire filesystem, as root, over UDP -- and the
    sudoers rule would be the only thing standing between a home LAN and
    /etc/shadow.  After the drop, this process can read exactly what the user
    running `wk serve` can read, and the served tree is theirs.
    """
    if os.geteuid() != 0:
        return
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or not gid:
        # Running as root with nothing to drop to. Refusing is the only safe
        # answer: continuing would serve files as root, which is the single
        # thing this design exists to prevent.
        sys.exit(
            "refusing to serve as root: no SUDO_UID/SUDO_GID to drop to.\n"
            "This is meant to be reached through sudo, from `wk serve`."
        )
    uid, gid = int(uid), int(gid)
    name = pwd.getpwuid(uid).pw_name
    os.setgid(gid)
    os.initgroups(name, gid)
    os.setuid(uid)
    if os.geteuid() != uid or os.getuid() != uid:
        sys.exit("could not drop privileges; refusing to serve")
    log(f"tftpd: dropped privileges to {name} ({uid}:{gid}) after binding")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    ap.add_argument("--address", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=69)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    # Bind first, drop second, and only then look at the filesystem. The order
    # is the whole security argument -- see drop_privileges().
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.address, args.port))
    except PermissionError:
        sys.exit(
            f"cannot bind {args.address}:{args.port} -- ports below 1024 need "
            "privilege. The netboot helper binds it; './setup' installs that."
        )
    except OSError as exc:
        sys.exit(f"cannot bind {args.address}:{args.port}: {exc}")

    drop_privileges()

    root = os.path.realpath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"no such directory: {root}")
    log(f"tftpd: serving {root} on {args.address}:{args.port} (read-only)")

    while True:
        payload, peer = sock.recvfrom(65536)
        if len(payload) < 4:
            continue
        opcode = struct.unpack("!H", payload[:2])[0]

        if opcode == OP_WRQ:
            # Said explicitly rather than ignored, so a misconfigured client
            # gets an answer instead of a timeout it has to guess about.
            sock.sendto(error_packet(ERR_ACCESS, "server is read-only"), peer)
            log(f"  refused write request from {peer[0]}")
            continue
        if opcode != OP_RRQ:
            continue

        filename, mode, options = parse_request(payload[2:])
        if filename is None or mode != "octet":
            sock.sendto(error_packet(ERR_ILLEGAL, "octet mode only"), peer)
            continue

        path, via_root = resolve(root, filename)
        if path is None:
            # Not an error worth shouting about: Pi firmware probes for a
            # serial-number directory and several optional files on every
            # boot, so misses are the normal case and a noisy log hides the
            # request that actually mattered.
            if not args.quiet:
                log(f"  miss {filename} ({peer[0]})")
            sock.sendto(error_packet(ERR_NOT_FOUND, "not found"), peer)
            continue

        if not args.quiet:
            where = " [from the root, not the serial directory]" if via_root else ""
            log(f"  get  {filename} ({peer[0]}){where}")
        threading.Thread(
            target=serve_file, args=(peer, path, options, args.quiet), daemon=True
        ).start()


main()
