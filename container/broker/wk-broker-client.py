#!/usr/bin/env python3
"""The workspace half of the fleet-request boundary.

Runs *inside* a workspace, where the only thing that exists is one unix socket
at /run/wk/broker.sock. It speaks the protocol and prints; it decides nothing.
Every refusal in the exchange comes from the far end, and this deliberately
adds none of its own beyond "there is no socket here" -- a client that
pre-validated would be a second copy of a policy, and the copy inside the
sandbox is the one that could be edited.

Usage, which is not a user interface: `wk boot` and `wk pi` build these lines
(lib/broker.sh), and a person sees their own command's words, not this.

    wk-broker-client.py <verb> [key=value ...]

Exit status is the brokered command's own, so a workspace script can branch on
it: 0 success, 1 the command failed or was refused, 2 no broker is listening.
"""

import json
import os
import socket
import sys

SOCKET = os.environ.get("WK_BROKER_SOCKET", "/run/wk/broker.sock")

# Colours match lib/common.sh's, so a brokered line is indistinguishable from a
# local one in a terminal -- which is the point: the request is a detail of how
# the command reached the hardware, not a different kind of output.
RED, YEL, GRN, OFF = "\033[31m", "\033[33m", "\033[32m", "\033[0m"
if not sys.stderr.isatty():
    RED = YEL = GRN = OFF = ""


def out(msg):
    print(msg, file=sys.stderr, flush=True)


def main(argv):
    if not argv:
        out("usage: wk-broker-client.py <verb> [key=value ...]")
        return 2
    verb, args = argv[0], {}
    for pair in argv[1:]:
        k, sep, v = pair.partition("=")
        if not sep:
            out(f"{RED}error:{OFF} '{pair}' is not key=value")
            return 2
        args[k] = v

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCKET)
    except OSError as exc:
        out(f"{RED}error:{OFF} no fleet-request broker at {SOCKET} ({exc.strerror}).")
        out("    This workspace has no route to the hardware and never will --")
        out("    the broker is how a request reaches it. Start it on the workstation:")
        out("        ./setup --stage broker      (then: wk doctor)")
        return 2

    s.sendall((json.dumps({"verb": verb, "args": args}) + "\n").encode())
    s.shutdown(socket.SHUT_WR)

    rc = 1
    buf = b""
    with s:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                rc = report(ev, rc)
    return rc


def report(ev, rc):
    kind = ev.get("event")
    if kind == "log":
        out(ev.get("line", ""))
    elif kind == "accepted":
        out(f"{GRN}==>{OFF} {ev.get('what')}")
        out(f"    on the workstation, through the broker: wk {ev.get('runs')}")
        out(f"    reached via {ev.get('reach')}")
        out(f"    request {ev.get('request')}")
    elif kind == "done":
        rc = 0 if ev.get("ok") else (int(ev.get("rc") or 1) or 1)
        if ev.get("ok"):
            out(f"{GRN}==>{OFF} done ({ev.get('request')})")
        else:
            out(f"{RED}error:{OFF} the brokered command failed (rc={ev.get('rc')})")
    elif kind == "refused":
        out(f"{RED}refused:{OFF} {ev.get('why')}")
        out(f"    {ev.get('remedy')}")
        rc = 1
    elif kind == "capabilities":
        rc = 0
        out(f"{GRN}==>{OFF} broker on {ev.get('host')} ({ev.get('host_os')})")
        out("  verbs:   " + ", ".join(ev.get("verbs") or []))
        out("  plans:   " + ", ".join(ev.get("plans") or []))
        out("  slots:   " + ", ".join(ev.get("slots") or []))
        out("  machines it will act on:")
        for name, m in (ev.get("machines") or {}).items():
            out(f"    {name:<10} {m.get('note')}")
            out(f"    {'':<10} profile {m.get('profile')}; {m.get('reach')}")
        refused = ev.get("not_bench_devices") or {}
        if refused:
            out("  refused by role (a workspace may not reboot a workstation):")
            for name, why in refused.items():
                out(f"    {name:<10} {why}")
        for f in ev.get("in_flight") or []:
            out(f"  {YEL}in flight:{OFF} {f.get('verb')} on {f.get('machine')} ({f.get('request')})")
    elif kind == "request":
        rc = 0 if ev.get("state") in ("ok",) else 1
        out(f"{GRN}==>{OFF} request {ev.get('request')}: {ev.get('state')}"
            f" ({ev.get('verb')} on {ev.get('machine')})")
        if ev.get("log_tail"):
            for line in ev["log_tail"].splitlines():
                out("  " + line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
