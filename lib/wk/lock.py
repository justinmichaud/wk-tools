"""The lock lib/common.sh's hold_lock takes, on the same path with the same
payload: a symlink `pid=<pid> tok=<hex> at=<iso> cmd=<cmd>`, a dead holder
replaced under a `.breaking` symlink, a live one waited for."""

import atexit
import contextlib
import os
import re

from wk import act

PID = re.compile(r"pid=(\d+)")


def _pid_of(payload):
    m = PID.search(payload or "")
    return int(m.group(1)) if m else None


def _payload(link, payload_file):
    """A lock's payload: `link()`, else `payload_file()`, else "" -- the one shape both `holder_pid`s read."""
    return link() or payload_file() or ""


def holder_pid(path):
    """The pid in a lock's payload at `path` on this host, or None (`wk status`'s read-only lookup)."""
    def link():
        try:
            return os.readlink(path)
        except OSError:
            return None

    def payload_file():
        try:
            with open(os.path.join(path, "payload")) as f:
                return f.read()
        except OSError:
            return None

    return _pid_of(_payload(link, payload_file))


class Lock:
    def __init__(self, store, machine, clock):
        self.store = store
        self.machine = machine
        self.clock = clock
        self.token = os.urandom(4).hex()
        self.payload = "pid=%d tok=%s at=%s cmd=%s" % (
            os.getpid(), self.token, clock.iso(), store.env.get("WK_CMD") or "wk")
        self.holding = []
        self._registered = False

    def _target(self, path):
        return self.machine.readlink(path) or ""

    def _link(self, path):
        return self.machine.symlink(self.payload, path)

    def holder_pid(self, resource):
        path = self.store.lock_path(resource)

        def payload_file():
            if not self.machine.isdir(path):
                return None
            try:
                return self.machine.read(os.path.join(path, "payload"))
            except OSError:
                return None

        return _pid_of(_payload(lambda: self.machine.readlink(path), payload_file))

    def _break(self, path, seen):
        breaker = path + ".breaking"
        if not self._link(breaker):
            bpid = _pid_of(self._target(breaker))
            if bpid is None or not self.machine.alive(bpid):
                self.machine.remove_now(breaker)
            return False
        taken = False
        if self._target(path) == seen:
            tmp = "%s.new.%s" % (path, self.token)
            self.machine.remove_now(tmp)
            if self._link(tmp) and self.machine.rename(tmp, path):
                taken = True
            else:
                self.machine.remove_now(tmp)
        self.machine.remove_now(breaker)
        return taken

    def hold(self, resource, timeout=600):
        path = self.store.lock_path(resource)
        if path in self.holding:
            act.debug("lock: %s (already held here)" % resource)
            return
        self.machine.mkdir_now(os.path.dirname(path))
        started = self.clock.now()
        announced = False
        while True:
            opid = None
            unreadable = False
            if self.machine.isdir(path):
                try:
                    opid = _pid_of("pid=" + self.machine.read(os.path.join(path, "pid")).strip())
                except OSError:
                    opid = None
                if opid is None or not self.machine.alive(opid):
                    self.machine.remove_now(path)
                    continue
            elif self._link(path):
                break
            else:
                raw = self.machine.readlink(path)
                if raw is None:
                    # Unreadable is not evidence of free: a transient read failure could hide a live hold.
                    unreadable = True
                else:
                    opid = _pid_of(raw)
                    if opid is not None and not self.machine.alive(opid):
                        if self._break(path, raw):
                            break
                        continue
                    if opid is None:
                        act.warn("clearing a lock file with no holder in it: %s" % path)
                        self.machine.remove_now(path)
                        continue
            if not announced:
                announced = True
                act.info("waiting for the %s lock (%s)" % (resource,
                          "its holder cannot be read" if unreadable else "held by pid %d" % opid))
            if self.clock.now() - started >= timeout:
                act.die("could not take the %s lock within %ds -- %s" % (resource, timeout,
                        "its holder cannot be read" if unreadable else "pid %d still holds it" % opid))
            self.clock.sleep(1)
        self.holding.append(path)
        if not self._registered:
            self._registered = True
            atexit.register(self.release_all)
        act.debug("lock: %s" % resource)

    def release_all(self):
        for path in self.holding:
            if self._target(path) == self.payload:
                self.machine.remove_now(path)
        self.holding = []

    @contextlib.contextmanager
    def held(self, resource, timeout=600):
        self.hold(resource, timeout)
        try:
            yield
        finally:
            self.release(resource)

    def release(self, resource):
        path = self.store.lock_path(resource)
        if path in self.holding and self._target(path) == self.payload:
            self.machine.remove_now(path)
        self.holding = [p for p in self.holding if p != path]
