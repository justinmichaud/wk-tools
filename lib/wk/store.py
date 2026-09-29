"""Where a machine keeps things."""

import argparse
import os
import shlex
import sys

from wk import record

BROKER_SOCKET = "/run/wk/broker.sock"


def _env(env):
    return os.environ if env is None else env


def in_vm(env=None):
    return bool(_env(env).get("WK_IN_VM"))


def ws_name(env=None, take=False):  # `take` removes the answer too
    env = _env(env)
    return env.pop("WK_NAME", "") if take else env.get("WK_NAME", "")


def build_config(env=None, take=False):  # the config lifted out of argv, or None
    env = _env(env)
    return env.pop("WK_CONFIG", None) if take else env.get("WK_CONFIG")


def dispatch_target(env=None, default=None):
    return _env(env).get("WK_TARGET", default)


class Store:
    def __init__(self, env=None):
        self.env = _env(env)

    @property
    def macos_host(self):
        return os.uname().sysname == "Darwin" and not in_vm(self.env)

    def home(self):
        return self.env.get("HOME") or os.path.expanduser("~")

    def podman_machine(self):
        """The podman machine a macOS host's container workspaces run in."""
        return self.env.get("WK_MACHINE") or "wk"

    def state_dir(self):
        return os.path.join(self.env.get("XDG_STATE_HOME") or os.path.join(self.home(), ".local", "state"), "wk")

    def lock_dir(self):
        return self.env.get("WK_LOCK_DIR") or os.path.join(self.state_dir(), "locks")

    def lock_path(self, resource):
        return os.path.join(self.lock_dir(), "%s@%s.lock" % (resource, record.host_name() or "local"))

    def default(self):
        if in_vm(self.env) or os.uname().sysname == "Darwin":
            return "/var/lib/wk"
        if os.path.isdir("/var/lib/wk") and os.access("/var/lib/wk", os.W_OK):
            return "/var/lib/wk"
        return os.path.join(self.env.get("XDG_DATA_HOME") or os.path.join(self.home(), ".local", "share"), "wk")

    def named_broker_socket(self):
        return self.env.get("WK_BROKER_SOCKET")

    def broker_socket(self):
        if self.env.get("XDG_RUNTIME_DIR"):
            default = os.path.join(self.env["XDG_RUNTIME_DIR"], "wk", "broker.sock")
        else:
            default = os.path.join(self.state_dir(), "broker.sock")
        return self.named_broker_socket() or default

    def workspace_broker_socket(self):
        return self.named_broker_socket() or BROKER_SOCKET

    def container_mirror(self):
        return self.env.get("WK_MIRROR")

    def named_root(self):
        return self.env.get("WK_STORE")

    def admission_dir(self):
        return self.named_root() or self.home()

    def root(self):
        return self.named_root() or self.default()

    def provisioned_root(self):
        return self.named_root() or "/var/lib/wk"

    def vm_root(self):
        return self.env.get("WK_VM_STORE") or self.record_dir()

    def vm_store(self):
        """The vm target's store, or None off a macOS host and where it would be the container's."""
        if not self.macos_host:
            return None
        d = self.vm_root()
        return None if os.path.realpath(d) == os.path.realpath(self.root()) else d

    def machine_store(self):
        return self.env.get("WK_STORE_DEFAULT") or self.root()

    def record_dir(self):
        if self.macos_host and self.root() == self.default():
            return self.state_dir()
        return self.root()

    def mirror(self):
        if self.macos_host:
            return os.path.join(self.state_dir(), "git", "WebKit.git")
        return os.path.join(self.root(), "git", "WebKit.git")

    def base_dir(self):
        return os.path.join(self.root(), "base")

    def base_path(self, base_id):
        return os.path.join(self.base_dir(), base_id, "WebKit")

    def base_sha_file(self, base_id):
        return os.path.join(self.base_dir(), base_id, "sha")

    def ws_dir(self, name):
        return os.path.join(self.root(), "ws", name)

    def ws_base_id(self, name):
        try:
            with open(os.path.join(self.ws_dir(name), "base-id")) as f:
                return f.read().strip()
        except OSError:
            return None

    def workspaces(self):
        try:
            return sorted(os.listdir(os.path.join(self.root(), "ws")))
        except OSError:
            return []

    def secrets_dir(self):
        if self.macos_host:
            return self.env.get("WK_HOST_SECRETS") or os.path.join(
                self.env.get("XDG_CONFIG_HOME") or os.path.join(self.home(), ".config"), "wk", "secrets")
        return os.path.join(self.machine_store(), "secrets")

    def secrets_view_dir(self, kind):
        return os.path.join(self.secrets_dir(), "view", kind)

    def agent_rw_dir(self):
        return os.path.join(os.path.dirname(self.secrets_dir()), "agent-rw")

    def push_held_dir(self):
        return os.path.join(os.path.dirname(self.secrets_dir()), "push-keys")

    def ntfy_topic_path(self):
        return os.path.join(os.path.dirname(self.secrets_dir()), "notify", "ntfy-topic")

    def is_local(self):
        """Whether this process can write the store; on a macOS host the default one is the podman VM's."""
        if in_vm(self.env) or os.uname().sysname != "Darwin":
            return True
        return os.path.isdir(self.root()) and os.access(self.root(), os.W_OK)

    def bench_dir(self):
        return os.path.join(self.record_dir(), "bench")

    def artifact_dir(self):
        return os.path.join(self.record_dir(), "cache")


class Bases:
    def __init__(self, store, machine):
        self.store = store
        self.machine = machine

    def _read(self, path):
        try:
            return self.machine.read(path)
        except OSError:
            return None

    def _ls(self, d):
        try:
            return sorted(self.machine.listdir(d))
        except OSError:
            return []

    def _git(self, bid, *args):
        r = self.machine.run(["git", "-C", self.store.base_path(bid)] + list(args))
        return r.out.strip() if r.ok else ""

    def ids(self):
        return self._ls(self.store.base_dir())

    # `wk sync` publishes into a hardlinked copy of the last snapshot, so a kill mid-publish leaves a newer directory than any good one; the sha lands last.
    def complete(self, bid):
        return bool(self._read(self.store.base_sha_file(bid)))

    def recorded_branch(self, bid):
        return (self._read(os.path.join(self.store.base_dir(), bid, "branch")) or "").strip()

    def verify(self, bid):
        if not self.machine.isdir(os.path.join(self.store.base_dir(), bid)):
            return "snapshot %s does not exist" % bid
        if not self.complete(bid):
            return ("snapshot %s was never finished publishing (no completion marker).\n"
                    "    An interrupted 'wk sync' leaves one; the next 'wk gc' removes it." % bid)
        if not self.machine.isdir(os.path.join(self.store.base_path(bid), ".git")):
            return "snapshot %s is not a git checkout" % bid
        want = (self._read(self.store.base_sha_file(bid)) or "").strip()
        got = self._git(bid, "rev-parse", "HEAD")
        if not got:
            return "snapshot %s has no readable HEAD" % bid
        if want != got:
            return ("snapshot %s no longer matches what was published:\n    recorded %s\n    tree     %s\n"
                    "    Something fetched or checked out inside a snapshot. Snapshots are\n"
                    "    immutable by design -- publish a new one with 'wk sync'." % (bid, want, got))
        return self.head_verify(bid)

    # Every workspace overlays this tree and inherits its HEAD, so a snapshot left on a raw sha is a detached `git status` in every workspace made from it.
    def head_verify(self, bid):
        branch = self.recorded_branch(bid)
        if not branch:
            return ("snapshot %s does not record the branch it was published from,\n"
                    "    so whether its HEAD is that branch cannot be known:  wk sync    publishes\n"
                    "    one that does, for the next 'wk new'." % bid)
        head = self._git(bid, "symbolic-ref", "--quiet", "HEAD")
        up = self._git(bid, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        local = branch.split("/", 1)[-1]
        if head == "refs/heads/" + local and up == branch:
            return ""
        return ("snapshot %s is not on branch %s tracking %s (HEAD is\n    %s%s), so every workspace overlaid on it starts\n"
                "    detached:  wk sync    publishes one that is." % (bid, local, branch, head or "a raw sha",
                                                                      ", tracking " + up if up else ""))

    def current(self):
        return next((b for b in reversed(self.ids()) if not self.verify(b)), "")

    # What `wk gc` keeps: weaker than `current` on purpose, since a snapshot published before the branch record is still the only one a machine may have.
    def newest_complete(self):
        return next((b for b in reversed(self.ids()) if self.complete(b)), "")

    def workspaces(self):
        return self._ls(os.path.join(self.store.root(), "ws"))

    def pin(self, ws):
        text = self._read(os.path.join(self.store.ws_dir(ws), "base-id"))
        return None if text is None else text.strip()

    def unpinned(self):
        return [w for w in self.workspaces() if self.pin(w) is None]

    def unreferenced(self):
        """The snapshots `wk gc` may take; none while a workspace's pin is unknown."""
        if self.unpinned():
            return []
        used = {self.pin(w) for w in self.workspaces()}
        keep = self.newest_complete()
        return [b for b in self.ids() if b != keep and b not in used]


def rubble(store, machine, mirror_here):
    """A snapshot no workspace is on goes with a plain `wk gc`; the mirror and every snapshot only with --purge-mirror, and never under a workspace."""
    from wk.rubble import du_kb, remover, row
    bases = Bases(store, machine)
    rows, ws = [], bases.workspaces()
    unpinned = bases.unpinned()
    if unpinned and [b for b in bases.ids() if b != bases.newest_complete()]:
        rows.append(row("snapshot", "base snapshots", du_kb(machine, store.base_dir()), take=remover(machine),
                        why="kept -- %s never recorded a base, so any snapshot may be under it: 'wk new <name>' remakes "
                            "one, 'wk rm <name>' removes it" % " ".join(unpinned)))
    for b in bases.unreferenced():
        d = os.path.join(store.base_dir(), b)
        rows.append(row("snapshot", "snapshot %s, no workspace on it" % b, du_kb(machine, d), take=remover(machine, d)))
    paths = [p for p in [store.base_dir()] + ([store.mirror()] if mirror_here else []) if machine.isdir(p)]
    if paths:
        kbs = [du_kb(machine, p) for p in paths]
        rows.append(row("mirror", "the mirror and every base snapshot" if store.mirror() in paths else "every base snapshot",
                        None if None in kbs else sum(kbs), "--purge-mirror", remover(machine, *paths),
                        "kept -- every workspace is overlaid on a snapshot here: 'wk rm' %s first" % " ".join(ws) if ws else ""))
    return rows


def main(argv):
    p = argparse.ArgumentParser(prog="python3 -m wk.store")
    p.add_argument("verb", choices=("paths",), help="the store's directories, as shell assignments")
    p.parse_args(argv)
    s = Store()
    for k, v in (("WK_STORE", s.root()), ("secrets_dir", s.secrets_dir()), ("agent_rw_dir", s.agent_rw_dir()),
                 ("push_held_dir", s.push_held_dir()), ("mirror_dir", os.path.dirname(s.mirror()))):
        print("%s=%s" % (k, shlex.quote(v)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
