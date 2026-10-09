#!/usr/bin/env python3
"""The workspace push boundary: a workspace's `git push` reaches GitHub through the ssh session this service runs itself with the repository's deploy key, so no key is ever in a workspace, an agent or a file one can reach.
Its own program and not a broker verb: a push is a byte stream where a verb is one JSON reply, and the broker's socket is published into a Mac's podman machine, which hides the caller's pid, the one thing this identifies a workspace by.
A request is one JSON line; the reply is a JSON line, then for a push the ssh session's framed output (lib/wk/pushgate.py)."""

import asyncio
import functools
import json
import os
import re
import socket
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from wk import images, pushgate, secrets  # noqa: E402
from wk.machine import Local, in_podman_machine  # noqa: E402
from wk.notify import sd_notify  # noqa: E402
from wk.places import Registry  # noqa: E402
from wk.store import Store  # noqa: E402

WK_ROOT = images.root()
MAX_REQUEST_BYTES = 4096
MAX_CONNECTIONS = 16
IDLE_TIMEOUT = 900
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def log(msg):
    print("[wk-push] %s" % msg, file=sys.stderr, flush=True)


def send(w, obj):
    w.write((json.dumps(obj) + "\n").encode())


class Service:
    def __init__(self, gate, sec, guest_dir=None):
        self.gate, self.sec, self.guest_dir = gate, sec, guest_dir
        self.listeners = {}
        self.active = 0

    def peer_pid(self, w):
        if not hasattr(socket, "SO_PEERCRED"):
            return 0
        raw = w.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[0]

    def caller(self, w, fixed):
        """(place, workspace) a connection is from: a guest's listener names it, a container's cgroup does."""
        if fixed:
            return self.gate.reg.load("vm"), fixed
        ws = self.gate.container_of(self.peer_pid(w))
        if ws is None:
            raise pushgate.Denied("this caller is in no workspace", "only a workspace's git push uses this service")
        return self.gate.reg.load("container"), ws

    async def push(self, r, w, req, fixed):
        t, ws = await asyncio.to_thread(self.caller, w, fixed)
        await asyncio.to_thread(pushgate.allow, t, ws)
        rows = await asyncio.to_thread(self.gate.rows, t, ws)
        key, line = pushgate.command(rows, str(req.get("host", "")), str(req.get("command", "")))
        path = self.sec.push_key_path(key)
        if not os.path.isfile(path):
            raise pushgate.Denied("this machine holds no deploy key '%s'" % key, "'wk key deploy' makes it, './setup' puts it where this service reads it")
        protocol = str(req.get("protocol") or "")
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": os.environ.get("HOME", "")}
        if pushgate.PROTOCOL.match(protocol):
            env["GIT_PROTOCOL"] = protocol
        proc = await asyncio.create_subprocess_exec(*pushgate.ssh_argv(path, line, protocol), env=env, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        log("%s: %s via %s" % (ws, line, key))
        send(w, {"ok": True})
        up = asyncio.ensure_future(self.up(r, proc.stdin))
        await asyncio.gather(self.down(proc.stdout, w, pushgate.STDOUT), self.down(proc.stderr, w, pushgate.STDERR))
        up.cancel()
        w.write(pushgate.frame(pushgate.EXIT, struct.pack(">i", await proc.wait())))

    async def up(self, r, stdin):
        try:
            while True:
                data = await asyncio.wait_for(r.read(65536), IDLE_TIMEOUT)
                if not data:
                    break
                stdin.write(data)
                await stdin.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            stdin.close()

    async def down(self, stream, w, kind):
        while True:
            data = await stream.read(65536)
            if not data:
                return
            w.write(pushgate.frame(kind, data))
            await w.drain()

    async def writes(self, w, req):
        """Whether the caller may spend a write credential: the egress proxy asks, naming who connected to it."""
        if isinstance(req.get("pid"), int):
            ws, kind = await asyncio.to_thread(self.gate.container_of, req["pid"]), "container"
        elif isinstance(req.get("addr"), str):
            ws, kind = await asyncio.to_thread(self.gate.guest_at, req["addr"]), "vm"
        else:
            raise pushgate.Denied("a writes request names a pid or an addr", "see container/proxy/wk-proxy.py")
        why = "no workspace is there"
        if ws is not None:
            try:
                await asyncio.to_thread(pushgate.allow, self.gate.reg.load(kind), ws)
                why = ""
            except pushgate.Denied as e:
                why = e.why
        send(w, {"writes": not why, "why": why})

    async def listen(self, w, req):
        """A macOS guest's own socket, which its forward ends at: the socket names the guest."""
        ws = req.get("workspace")
        if not isinstance(ws, str) or not NAME_RE.match(ws) or len(ws) > 128:
            raise pushgate.Denied("'%s' is not a workspace name" % str(ws)[:60], "[A-Za-z0-9._-], not starting with '-'")
        path = os.path.join(self.guest_dir, ws + ".push.sock")
        if ws not in self.listeners:
            if os.path.exists(path):
                os.unlink(path)
            self.listeners[ws] = await asyncio.start_unix_server(functools.partial(self.handle, fixed=ws), path=path)
            os.chmod(path, 0o600)
        send(w, {"listening": path})

    async def handle(self, r, w, fixed=None):
        if self.active >= MAX_CONNECTIONS:
            send(w, {"refused": "the push service is busy", "remedy": "retry in a moment"})
            w.close()
            return
        self.active += 1
        try:
            line = await asyncio.wait_for(r.readline(), 30)
            try:
                req = json.loads(line.decode("utf-8", "replace")) if 0 < len(line) <= MAX_REQUEST_BYTES else None
            except ValueError:
                req = None
            if not isinstance(req, dict):
                raise pushgate.Denied("that is not a request", "one JSON object on one line")
            verb = req.get("verb")
            if verb == "push":
                await self.push(r, w, req, fixed)
            elif verb == "writes" and not fixed:
                await self.writes(w, req)
            elif verb == "listen" and self.guest_dir and not fixed:
                await self.listen(w, req)
            else:
                raise pushgate.Denied("unknown verb '%s'" % str(verb)[:40], "the vocabulary is: push, writes")
            await w.drain()
        except pushgate.Denied as e:
            log("REFUSE %s" % e.why)
            send(w, {"refused": e.why, "remedy": e.remedy})
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        except Exception as e:                              # noqa: BLE001
            log("internal error: %r" % (e,))
            send(w, {"refused": "push service error: %s" % e, "remedy": "its log has the detail: journalctl --user -u wk-push"})
        finally:
            self.active -= 1
            w.close()


async def main():
    env = dict(os.environ, **({"WK_IN_VM": "1"} if in_podman_machine() else {}))
    path = Store(env).push_socket()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    svc = Service(pushgate.Gate(Registry(WK_ROOT, env, Local())), secrets.Secrets(WK_ROOT, env), os.environ.get("WK_PUSH_GUEST_DIR"))
    server = await asyncio.start_unix_server(svc.handle, path=path)
    os.chmod(path, 0o600)   # same uid as the workspace (keep-id)
    log("listening on %s" % path)
    sd_notify("READY=1")
    await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
