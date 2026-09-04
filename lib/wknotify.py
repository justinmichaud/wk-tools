"""The sd_notify half of Type=notify, for the services this repo runs under
systemd --user (container/proxy/wk-proxy.py, container/proxy/github-inject.py,
container/broker/wk-broker.py).

Type=notify is what makes `systemctl start` mean "the service is ready to be
used": a Type=simple service is active the instant it is forked, so anything
that asks straight afterwards is told a service that is about to die is
running. The unit says Type=notify, and the program says READY=1 once its
socket is bound.

Not python3-systemd: it is absent from the macOS host's /usr/bin/python3, which
runs two of these three for the guest VMs, and the protocol is one datagram.
The one implementation lives here rather than a copy per service, and the three
find it from their own path, since systemd starts them by absolute path:

    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
    from wknotify import sd_notify
"""

import os
import socket


def sd_notify(state):
    """Send one systemd notification datagram, e.g. "READY=1".

    Silent no-op with no NOTIFY_SOCKET in the environment: the same programs
    run under nohup on the macOS host (targets/vm.sh), where there is no
    systemd to tell.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(state.encode())
